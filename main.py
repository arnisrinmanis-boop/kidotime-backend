"""
KidoTime Backend API v2
Added: PC registration via QR code tokens, add/delete kids
"""
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime, date
from typing import Optional
import sqlite3, json, os, secrets

app = FastAPI(title="KidoTime API", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

API_KEY = os.environ.get("KIDOTIME_API_KEY") or os.environ.get("KIDSGUARD_API_KEY", "change-this-secret-key")
DB_PATH = "kidotime.db"

def verify_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db(); c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS pcs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nickname TEXT NOT NULL,
        token TEXT UNIQUE NOT NULL,
        registered INTEGER DEFAULT 0,
        last_seen TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS kids (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        daily_limit_minutes INTEGER DEFAULT 120,
        is_locked INTEGER DEFAULT 0,
        pc_id INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kid_id INTEGER NOT NULL, app_name TEXT,
        started_at TEXT, ended_at TEXT,
        duration_minutes INTEGER DEFAULT 0, date TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS schedules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kid_id INTEGER NOT NULL, label TEXT, days TEXT,
        block_from TEXT, block_until TEXT, is_active INTEGER DEFAULT 1)""")
    c.execute("""CREATE TABLE IF NOT EXISTS commands (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kid_id INTEGER NOT NULL, command TEXT NOT NULL,
        payload TEXT, status TEXT DEFAULT 'pending',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    conn.commit(); conn.close()

init_db()

class KidCreate(BaseModel):
    name: str
    daily_limit_minutes: int = 120
    pc_id: Optional[int] = None

class KidUpdate(BaseModel):
    name: Optional[str] = None
    daily_limit_minutes: Optional[int] = None

class PCRegister(BaseModel):
    token: str
    nickname: str

class SessionReport(BaseModel):
    kid_id: int; app_name: str
    started_at: str; ended_at: str; duration_minutes: int

class ScheduleCreate(BaseModel):
    kid_id: int; label: str; days: list[str]
    block_from: str; block_until: str; is_active: bool = True

class LockCommand(BaseModel):
    kid_id: int; action: str

# ── QR / PC Registration ──────────────────────────────────────────────────────
@app.post("/api/pcs/generate-token")
def generate_pc_token(nickname: str = "Family PC", key=Depends(verify_key)):
    token = ''.join([str(secrets.randbelow(10)) for _ in range(10)])
    conn = get_db()
    conn.execute("INSERT INTO pcs (nickname, token, registered) VALUES (?,?,0)", (nickname, token))
    conn.commit()
    pc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return {"pc_id": pc_id, "token": token, "nickname": nickname}

@app.post("/api/pcs/register")
def register_pc(data: PCRegister, key=Depends(verify_key)):
    conn = get_db()
    pc = conn.execute("SELECT * FROM pcs WHERE token=?", (data.token,)).fetchone()
    if not pc:
        conn.close(); raise HTTPException(404, "Invalid QR code")
    conn.execute("UPDATE pcs SET registered=1, nickname=?, last_seen=? WHERE token=?",
                 (data.nickname, datetime.now().isoformat(), data.token))
    conn.commit()
    result = dict(conn.execute("SELECT * FROM pcs WHERE token=?", (data.token,)).fetchone())
    conn.close()
    return {"ok": True, "pc": result}

@app.get("/api/pcs")
def get_pcs(key=Depends(verify_key)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM pcs WHERE registered=1").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/api/pcs/{token}/status")
def check_registration(token: str, key=Depends(verify_key)):
    conn = get_db()
    pc = conn.execute("SELECT * FROM pcs WHERE token=?", (token,)).fetchone()
    if not pc: conn.close(); raise HTTPException(404, "Token not found")
    conn.execute("UPDATE pcs SET last_seen=? WHERE token=?", (datetime.now().isoformat(), token))
    conn.commit(); conn.close()
    return {"registered": bool(pc["registered"]), "pc_id": pc["id"]}

# ── Kids ──────────────────────────────────────────────────────────────────────
@app.get("/api/kids")
def get_kids(key=Depends(verify_key)):
    conn = get_db(); today = date.today().isoformat()
    kids = conn.execute("SELECT * FROM kids").fetchall()
    result = []
    for kid in kids:
        usage = conn.execute("SELECT COALESCE(SUM(duration_minutes),0) as total FROM sessions WHERE kid_id=? AND date=?",
                             (kid["id"], today)).fetchone()["total"]
        result.append({**dict(kid), "usage_today_minutes": usage, "limit_reached": usage >= kid["daily_limit_minutes"]})
    conn.close(); return result

@app.post("/api/kids")
def create_kid(data: KidCreate, key=Depends(verify_key)):
    conn = get_db()
    conn.execute("INSERT INTO kids (name, daily_limit_minutes, pc_id) VALUES (?,?,?)",
                 (data.name, data.daily_limit_minutes, data.pc_id))
    conn.commit()
    kid_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close(); return {"ok": True, "id": kid_id}

@app.patch("/api/kids/{kid_id}")
def update_kid(kid_id: int, data: KidUpdate, key=Depends(verify_key)):
    conn = get_db()
    if data.name: conn.execute("UPDATE kids SET name=? WHERE id=?", (data.name, kid_id))
    if data.daily_limit_minutes is not None:
        conn.execute("UPDATE kids SET daily_limit_minutes=? WHERE id=?", (data.daily_limit_minutes, kid_id))
    conn.commit(); conn.close(); return {"ok": True}

@app.put("/api/kids/{kid_id}")
def update_kid_put(kid_id: int, data: KidUpdate, key=Depends(verify_key)):
    conn = get_db()
    if data.name: conn.execute("UPDATE kids SET name=? WHERE id=?", (data.name, kid_id))
    if data.daily_limit_minutes is not None:
        conn.execute("UPDATE kids SET daily_limit_minutes=? WHERE id=?", (data.daily_limit_minutes, kid_id))
    conn.commit(); conn.close(); return {"ok": True}

@app.delete("/api/kids/{kid_id}")
def delete_kid(kid_id: int, key=Depends(verify_key)):
    conn = get_db()
    conn.execute("DELETE FROM kids WHERE id=?", (kid_id,))
    conn.commit(); conn.close(); return {"ok": True}

@app.post("/api/kids/{kid_id}/lock")
def lock_kid(kid_id: int, body: dict, key=Depends(verify_key)):
    locked = 1 if body.get("locked") else 0
    conn = get_db()
    conn.execute("UPDATE kids SET is_locked=? WHERE id=?", (locked, kid_id))
    # Insert command so PC picks it up via polling
    command = "lock" if locked else "unlock"
    conn.execute("INSERT INTO commands (kid_id, command, status) VALUES (?,?,'pending')", (kid_id, command))
    conn.commit(); conn.close(); return {"ok": True}

# ── Lock / Commands ───────────────────────────────────────────────────────────
@app.post("/api/lock")
def send_lock(cmd: LockCommand, key=Depends(verify_key)):
    if cmd.action not in ("lock","unlock"): raise HTTPException(400, "invalid action")
    conn = get_db()
    conn.execute("UPDATE kids SET is_locked=? WHERE id=?", (1 if cmd.action=="lock" else 0, cmd.kid_id))
    conn.execute("INSERT INTO commands (kid_id, command, status) VALUES (?,?,'pending')", (cmd.kid_id, cmd.action))
    conn.commit(); conn.close(); return {"ok": True}

@app.get("/api/commands/pending")
def get_pending(key=Depends(verify_key)):
    conn = get_db()
    cmds = conn.execute("SELECT * FROM commands WHERE status='pending' ORDER BY created_at ASC").fetchall()
    conn.close(); return [dict(c) for c in cmds]

@app.post("/api/commands/{cmd_id}/done")
def cmd_done(cmd_id: int, key=Depends(verify_key)):
    conn = get_db()
    conn.execute("UPDATE commands SET status='done' WHERE id=?", (cmd_id,))
    conn.commit(); conn.close(); return {"ok": True}

# ── Sessions ──────────────────────────────────────────────────────────────────
@app.post("/api/sessions")
def report_session(session: SessionReport, key=Depends(verify_key)):
    conn = get_db(); today = date.today().isoformat()
    conn.execute("INSERT INTO sessions (kid_id,app_name,started_at,ended_at,duration_minutes,date) VALUES (?,?,?,?,?,?)",
                 (session.kid_id,session.app_name,session.started_at,session.ended_at,session.duration_minutes,today))
    conn.commit()
    usage = conn.execute("SELECT COALESCE(SUM(duration_minutes),0) as total FROM sessions WHERE kid_id=? AND date=?",
                         (session.kid_id, today)).fetchone()["total"]
    kid = conn.execute("SELECT * FROM kids WHERE id=?", (session.kid_id,)).fetchone()
    if usage >= kid["daily_limit_minutes"] and not kid["is_locked"]:
        conn.execute("UPDATE kids SET is_locked=1 WHERE id=?", (session.kid_id,))
        conn.execute("INSERT INTO commands (kid_id,command) VALUES (?,'lock')", (session.kid_id,))
        conn.commit()
    conn.close(); return {"ok": True, "total_today": usage}

@app.get("/api/usage/{kid_id}")
def get_usage(kid_id: int, days: int = 7, key=Depends(verify_key)):
    conn = get_db()
    rows = conn.execute("SELECT date,app_name,SUM(duration_minutes) as total FROM sessions WHERE kid_id=? GROUP BY date,app_name ORDER BY date DESC LIMIT ?",
                        (kid_id, days*10)).fetchall()
    conn.close(); return [dict(r) for r in rows]

# ── Schedules ─────────────────────────────────────────────────────────────────
@app.get("/api/schedules")
def get_schedules(key=Depends(verify_key)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM schedules").fetchall()
    conn.close()
    result = [dict(r) for r in rows]
    for r in result: r["days"] = json.loads(r["days"])
    return result

@app.post("/api/schedules")
def create_schedule(s: ScheduleCreate, key=Depends(verify_key)):
    conn = get_db()
    conn.execute("INSERT INTO schedules (kid_id,label,days,block_from,block_until,is_active) VALUES (?,?,?,?,?,?)",
                 (s.kid_id,s.label,json.dumps(s.days),s.block_from,s.block_until,int(s.is_active)))
    conn.commit(); conn.close(); return {"ok": True}

@app.get("/")
def health():
    return {"status": "KidoTime API v2 ✅", "version": "2.0.0"}

"""
KidoTime Backend API v2
PostgreSQL version — data persists across Railway restarts
"""
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime, date
from typing import Optional
import json, os, secrets
import psycopg2

app = FastAPI(title="KidoTime API", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

API_KEY = os.environ.get("KIDOTIME_API_KEY") or os.environ.get("KIDSGUARD_API_KEY", "change-this-secret-key")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

def verify_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    return conn

def row_to_dict(row, cursor):
    cols = [desc[0] for desc in cursor.description]
    return dict(zip(cols, row))

def rows_to_dicts(rows, cursor):
    cols = [desc[0] for desc in cursor.description]
    return [dict(zip(cols, row)) for row in rows]

def init_db():
    conn = get_db(); c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS pcs (
        id SERIAL PRIMARY KEY, nickname TEXT NOT NULL,
        token TEXT UNIQUE NOT NULL, registered INTEGER DEFAULT 0,
        active_kid_id INTEGER DEFAULT NULL,
        last_seen TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS kids (
        id SERIAL PRIMARY KEY, name TEXT NOT NULL,
        daily_limit_minutes INTEGER DEFAULT 120, is_locked INTEGER DEFAULT 0,
        pc_id INTEGER, pin TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS sessions (
        id SERIAL PRIMARY KEY, kid_id INTEGER NOT NULL, app_name TEXT,
        started_at TEXT, ended_at TEXT, duration_minutes INTEGER DEFAULT 0, date TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS schedules (
        id SERIAL PRIMARY KEY, kid_id INTEGER NOT NULL, label TEXT, days TEXT,
        block_from TEXT, block_until TEXT, is_active INTEGER DEFAULT 1)""")
    c.execute("""CREATE TABLE IF NOT EXISTS commands (
        id SERIAL PRIMARY KEY, kid_id INTEGER NOT NULL, command TEXT NOT NULL,
        payload TEXT, status TEXT DEFAULT 'pending', created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    conn.commit(); conn.close()

init_db()

class KidCreate(BaseModel):
    name: str; daily_limit_minutes: int = 120; pc_id: Optional[int] = None

class KidUpdate(BaseModel):
    name: Optional[str] = None; daily_limit_minutes: Optional[int] = None

class PCRegister(BaseModel):
    token: str; nickname: str

class SessionReport(BaseModel):
    kid_id: int; app_name: str; started_at: str; ended_at: str; duration_minutes: int

class ScheduleCreate(BaseModel):
    kid_id: int; label: str; days: list[str]
    block_from: str; block_until: str; is_active: bool = True

class LockCommand(BaseModel):
    kid_id: int; action: str

@app.post("/api/pcs/generate-token")
def generate_pc_token(nickname: str = "Family PC", key=Depends(verify_key)):
    token = ''.join([str(secrets.randbelow(10)) for _ in range(10)])
    conn = get_db(); c = conn.cursor()
    c.execute("INSERT INTO pcs (nickname, token, registered) VALUES (%s,%s,0) RETURNING id", (nickname, token))
    pc_id = c.fetchone()[0]
    conn.commit(); conn.close()
    return {"pc_id": pc_id, "token": token, "nickname": nickname}

@app.post("/api/pcs/register")
def register_pc(data: PCRegister, key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT * FROM pcs WHERE token=%s", (data.token,))
    row = c.fetchone()
    if not row: conn.close(); raise HTTPException(404, "Invalid QR code")
    c.execute("UPDATE pcs SET registered=1, nickname=%s, last_seen=%s WHERE token=%s",
              (data.nickname, datetime.now().isoformat(), data.token))
    c.execute("SELECT * FROM pcs WHERE token=%s", (data.token,))
    result = row_to_dict(c.fetchone(), c)
    conn.commit(); conn.close()
    return {"ok": True, "pc": result}

@app.get("/api/pcs")
def get_pcs(key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT * FROM pcs WHERE registered=1")
    result = rows_to_dicts(c.fetchall(), c); conn.close(); return result

@app.get("/api/pcs/{token}/status")
def check_registration(token: str, key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT * FROM pcs WHERE token=%s", (token,))
    row = c.fetchone()
    if not row: conn.close(); raise HTTPException(404, "Token not found")
    pc = row_to_dict(row, c)
    c.execute("UPDATE pcs SET last_seen=%s WHERE token=%s", (datetime.now().isoformat(), token))
    conn.commit(); conn.close()
    return {"registered": bool(pc["registered"]), "pc_id": pc["id"]}

@app.post("/api/pcs/{token}/active-kid")
def set_active_kid(token: str, body: dict, key=Depends(verify_key)):
    kid_id = body.get("kid_id")  # None = parent session / no active kid
    conn = get_db(); c = conn.cursor()
    c.execute("UPDATE pcs SET active_kid_id=%s, last_seen=%s WHERE token=%s",
              (kid_id, datetime.now().isoformat(), token))
    conn.commit(); conn.close(); return {"ok": True}

@app.get("/api/kids")
def get_kids(key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    today = date.today().isoformat()
    c.execute("SELECT * FROM kids")
    kids = rows_to_dicts(c.fetchall(), c)
    result = []
    # Get active kid IDs from all PCs
    c.execute("SELECT active_kid_id FROM pcs WHERE active_kid_id IS NOT NULL")
    active_ids = {row[0] for row in c.fetchall()}

    for kid in kids:
        c.execute("SELECT COALESCE(SUM(duration_minutes),0) FROM sessions WHERE kid_id=%s AND date=%s", (kid["id"], today))
        usage = c.fetchone()[0]
        result.append({**kid, "usage_today_minutes": usage, "limit_reached": usage >= kid["daily_limit_minutes"], "active": kid["id"] in active_ids})
    conn.close(); return result

@app.post("/api/kids")
def create_kid(data: KidCreate, key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    c.execute("INSERT INTO kids (name, daily_limit_minutes, pc_id) VALUES (%s,%s,%s) RETURNING id",
              (data.name, data.daily_limit_minutes, data.pc_id))
    kid_id = c.fetchone()[0]
    conn.commit(); conn.close(); return {"ok": True, "id": kid_id}

@app.patch("/api/kids/{kid_id}")
@app.put("/api/kids/{kid_id}")
def update_kid(kid_id: int, data: KidUpdate, key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    if data.name: c.execute("UPDATE kids SET name=%s WHERE id=%s", (data.name, kid_id))
    if data.daily_limit_minutes is not None:
        c.execute("UPDATE kids SET daily_limit_minutes=%s WHERE id=%s", (data.daily_limit_minutes, kid_id))
    conn.commit(); conn.close(); return {"ok": True}

@app.delete("/api/kids/{kid_id}")
def delete_kid(kid_id: int, key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    c.execute("DELETE FROM kids WHERE id=%s", (kid_id,))
    conn.commit(); conn.close(); return {"ok": True}

@app.post("/api/kids/{kid_id}/pin")
def set_kid_pin(kid_id: int, body: dict, key=Depends(verify_key)):
    pin = str(body.get("pin", ""))
    if not pin: raise HTTPException(400, "pin required")
    conn = get_db(); c = conn.cursor()
    c.execute("UPDATE kids SET pin=%s WHERE id=%s", (pin, kid_id))
    conn.commit(); conn.close(); return {"ok": True}

@app.post("/api/kids/{kid_id}/lock")
def lock_kid(kid_id: int, body: dict, key=Depends(verify_key)):
    locked = 1 if body.get("locked") else 0
    command = "lock" if locked else "unlock"
    conn = get_db(); c = conn.cursor()
    c.execute("UPDATE kids SET is_locked=%s WHERE id=%s", (locked, kid_id))
    c.execute("INSERT INTO commands (kid_id, command, status) VALUES (%s,%s,'pending')", (kid_id, command))
    conn.commit(); conn.close(); return {"ok": True}

@app.post("/api/lock")
def send_lock(cmd: LockCommand, key=Depends(verify_key)):
    if cmd.action not in ("lock","unlock"): raise HTTPException(400, "invalid action")
    conn = get_db(); c = conn.cursor()
    c.execute("UPDATE kids SET is_locked=%s WHERE id=%s", (1 if cmd.action=="lock" else 0, cmd.kid_id))
    c.execute("INSERT INTO commands (kid_id, command, status) VALUES (%s,%s,'pending')", (cmd.kid_id, cmd.action))
    conn.commit(); conn.close(); return {"ok": True}

@app.get("/api/commands/pending")
def get_pending(key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT * FROM commands WHERE status='pending' ORDER BY created_at ASC")
    result = rows_to_dicts(c.fetchall(), c); conn.close(); return result

@app.post("/api/commands/{cmd_id}/done")
def cmd_done(cmd_id: int, key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    c.execute("UPDATE commands SET status='done' WHERE id=%s", (cmd_id,))
    conn.commit(); conn.close(); return {"ok": True}

@app.post("/api/sessions")
def report_session(session: SessionReport, key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    today = date.today().isoformat()
    c.execute("INSERT INTO sessions (kid_id,app_name,started_at,ended_at,duration_minutes,date) VALUES (%s,%s,%s,%s,%s,%s)",
              (session.kid_id,session.app_name,session.started_at,session.ended_at,session.duration_minutes,today))
    c.execute("SELECT COALESCE(SUM(duration_minutes),0) FROM sessions WHERE kid_id=%s AND date=%s", (session.kid_id, today))
    usage = c.fetchone()[0]
    c.execute("SELECT * FROM kids WHERE id=%s", (session.kid_id,))
    kid = row_to_dict(c.fetchone(), c)
    if usage >= kid["daily_limit_minutes"] and not kid["is_locked"]:
        c.execute("UPDATE kids SET is_locked=1 WHERE id=%s", (session.kid_id,))
        c.execute("INSERT INTO commands (kid_id,command) VALUES (%s,'lock')", (session.kid_id,))
    conn.commit(); conn.close()
    return {"ok": True, "total_today": usage}

@app.get("/api/usage/{kid_id}")
def get_usage(kid_id: int, days: int = 7, key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT date,app_name,SUM(duration_minutes) as total FROM sessions WHERE kid_id=%s GROUP BY date,app_name ORDER BY date DESC LIMIT %s",
              (kid_id, days*10))
    result = rows_to_dicts(c.fetchall(), c); conn.close(); return result

@app.get("/api/schedules")
def get_schedules(key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT * FROM schedules")
    result = rows_to_dicts(c.fetchall(), c); conn.close()
    for r in result: r["days"] = json.loads(r["days"])
    return result

@app.post("/api/schedules")
def create_schedule(s: ScheduleCreate, key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    c.execute("INSERT INTO schedules (kid_id,label,days,block_from,block_until,is_active) VALUES (%s,%s,%s,%s,%s,%s)",
              (s.kid_id,s.label,json.dumps(s.days),s.block_from,s.block_until,int(s.is_active)))
    conn.commit(); conn.close(); return {"ok": True}

@app.get("/")
def health():
    return {"status": "KidoTime API v2 ✅", "version": "2.0.0"}

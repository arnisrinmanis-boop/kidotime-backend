"""
KidoTime Backend API v2
PostgreSQL version — data persists across Railway restarts
"""
from fastapi import FastAPI, HTTPException, Depends, Header, Request
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
    try:
        c.execute("ALTER TABLE pcs ADD COLUMN active_kid_id INTEGER DEFAULT NULL")
        conn.commit()
    except Exception:
        conn.rollback()
    c.execute(
        "CREATE TABLE IF NOT EXISTS weekly_limits ("
        "id SERIAL PRIMARY KEY, kid_id INTEGER UNIQUE, "
        "mon INTEGER DEFAULT 120, tue INTEGER DEFAULT 120, "
        "wed INTEGER DEFAULT 120, thu INTEGER DEFAULT 120, "
        "fri INTEGER DEFAULT 120, sat INTEGER DEFAULT 180, "
        "sun INTEGER DEFAULT 180)"
    )
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
    if not row: conn.close(); raise HTTPException(404, "Invalid token")
    c.execute("UPDATE pcs SET registered=1, nickname=%s, last_seen=%s WHERE token=%s",
              (data.nickname, datetime.utcnow().isoformat(), data.token))
    c.execute("SELECT * FROM pcs WHERE token=%s", (data.token,))
    result = row_to_dict(c.fetchone(), c)
    conn.commit(); conn.close()
    return {"ok": True, "pc": result}

@app.get("/api/pcs")
def get_pcs(key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT * FROM pcs WHERE registered=1")
    result = rows_to_dicts(c.fetchall(), c); conn.close(); return result

@app.delete("/api/pcs/by-id/{pc_id}")
def delete_pc(pc_id: int, key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    c.execute("DELETE FROM pcs WHERE id=%s", (pc_id,))
    conn.commit(); conn.close(); return {"ok": True}

@app.get("/api/pcs/{token}/status")
def check_registration(token: str, key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT * FROM pcs WHERE token=%s", (token,))
    row = c.fetchone()
    if not row: conn.close(); raise HTTPException(404, "Token not found")
    pc = row_to_dict(row, c)
    c.execute("UPDATE pcs SET last_seen=%s WHERE token=%s", (datetime.utcnow().isoformat(), token))
    conn.commit(); conn.close()
    return {"registered": bool(pc["registered"]), "pc_id": pc["id"]}

@app.post("/api/pcs/{token}/heartbeat")
def pc_heartbeat(token: str, key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT id FROM pcs WHERE token=%s", (token,))
    row = c.fetchone()
    if not row:
        conn.close(); return {"ok": False, "reason": "not_registered"}
    c.execute("UPDATE pcs SET last_seen=%s WHERE token=%s", (datetime.utcnow().isoformat(), token))
    conn.commit(); conn.close(); return {"ok": True}

@app.post("/api/pcs/{token}/offline")
def pc_offline(token: str, key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    c.execute("UPDATE pcs SET active_kid_id=NULL, last_seen=%s WHERE token=%s",
              (datetime.utcnow().isoformat(), token))
    conn.commit(); conn.close(); return {"ok": True}

@app.post("/api/pcs/{token}/active-kid")
def set_active_kid(token: str, body: dict, key=Depends(verify_key)):
    kid_id = body.get("kid_id")
    conn = get_db(); c = conn.cursor()
    c.execute("UPDATE pcs SET active_kid_id=%s, last_seen=%s WHERE token=%s",
              (kid_id, datetime.utcnow().isoformat(), token))
    conn.commit(); conn.close(); return {"ok": True}

@app.get("/api/kids")
def get_kids(key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    today = date.today().isoformat()
    day_col = ['mon','tue','wed','thu','fri','sat','sun'][date.today().weekday()]
    c.execute("SELECT * FROM kids")
    kids = rows_to_dicts(c.fetchall(), c)
    result = []
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(seconds=60)).isoformat()
    c.execute("SELECT active_kid_id, nickname FROM pcs WHERE active_kid_id IS NOT NULL AND last_seen > %s", (cutoff,))
    active_pcs = {row[0]: row[1] for row in c.fetchall()}
    active_ids = set(active_pcs.keys())
    for kid in kids:
        c.execute("SELECT COALESCE(SUM(duration_minutes),0) FROM sessions WHERE kid_id=%s AND date=%s", (kid["id"], today))
        usage = c.fetchone()[0]
        c.execute(f"SELECT {day_col} FROM weekly_limits WHERE kid_id=%s", (kid["id"],))
        wl_row = c.fetchone()
        effective_limit = wl_row[0] if wl_row and wl_row[0] is not None else kid["daily_limit_minutes"]
        active_pc_name = active_pcs.get(kid["id"])
        result.append({**kid, "usage_today_minutes": usage, "effective_limit_today": effective_limit, "limit_reached": usage >= effective_limit, "active": bool(kid["id"] in active_ids), "active_pc_name": active_pc_name})
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
    locked = body.get("locked", True)
    conn = get_db(); c = conn.cursor()
    c.execute("UPDATE kids SET is_locked=%s WHERE id=%s", (1 if locked else 0, kid_id))
    # Clear all pending commands first to avoid stale lock/unlock flicker
    c.execute("DELETE FROM commands WHERE kid_id=%s AND status='pending'", (kid_id,))
    if locked:
        c.execute("INSERT INTO commands (kid_id, command, payload, status) VALUES (%s,'lock','{}','pending')", (kid_id,))
    else:
        c.execute("INSERT INTO commands (kid_id, command, payload, status) VALUES (%s,'unlock','{}','pending')", (kid_id,))
    conn.commit(); conn.close(); return {"ok": True}

@app.post("/api/lock")
def lock_command(data: LockCommand, key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    c.execute("INSERT INTO commands (kid_id, command, payload, status) VALUES (%s,%s,'{}','pending')",
              (data.kid_id, data.action))
    conn.commit(); conn.close(); return {"ok": True}

@app.get("/api/commands/pending")
def get_pending_commands(kid_id: int, key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT * FROM commands WHERE kid_id=%s AND status='pending' ORDER BY created_at", (kid_id,))
    cmds = rows_to_dicts(c.fetchall(), c); conn.close(); return cmds

@app.post("/api/commands/{cmd_id}/done")
def mark_command_done(cmd_id: int, key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    c.execute("UPDATE commands SET status='done' WHERE id=%s", (cmd_id,))
    conn.commit(); conn.close(); return {"ok": True}

@app.post("/api/sessions")
def report_session(data: SessionReport, key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    session_date = data.started_at[:10] if data.started_at else date.today().isoformat()
    c.execute("INSERT INTO sessions (kid_id, app_name, started_at, ended_at, duration_minutes, date) VALUES (%s,%s,%s,%s,%s,%s)",
              (data.kid_id, data.app_name, data.started_at, data.ended_at, data.duration_minutes, session_date))
    # Return total usage today so PC can check limit
    c.execute("SELECT COALESCE(SUM(duration_minutes),0) FROM sessions WHERE kid_id=%s AND date=%s",
              (data.kid_id, session_date))
    total_today = c.fetchone()[0]
    conn.commit(); conn.close()
    return {"ok": True, "total_today": total_today}

@app.get("/api/weekly-limits/{kid_id}")
def get_weekly_limits(kid_id: int, key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT * FROM weekly_limits WHERE kid_id=%s", (kid_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return {"kid_id": kid_id, "mon":120,"tue":120,"wed":120,"thu":120,"fri":120,"sat":180,"sun":180}
    result = row_to_dict(row, c); conn.close(); return result

@app.put("/api/weekly-limits/{kid_id}")
def set_weekly_limits(kid_id: int, body: dict, key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    days = ["mon","tue","wed","thu","fri","sat","sun"]
    c.execute("SELECT mon,tue,wed,thu,fri,sat,sun FROM weekly_limits WHERE kid_id=%s", (kid_id,))
    existing = c.fetchone()
    if existing:
        # Merge: only update days that are in body, keep existing for others
        vals = [body[d] if d in body else existing[i] for i, d in enumerate(days)]
        c.execute("UPDATE weekly_limits SET mon=%s,tue=%s,wed=%s,thu=%s,fri=%s,sat=%s,sun=%s WHERE kid_id=%s",
                  (*vals, kid_id))
    else:
        vals = [body.get(d, 30) for d in days]
        c.execute("INSERT INTO weekly_limits (kid_id,mon,tue,wed,thu,fri,sat,sun) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                  (kid_id, *vals))
    conn.commit(); conn.close(); return {"ok": True}

@app.get("/api/usage/{kid_id}")
def get_usage(kid_id: int, days: int = 7, key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT date, app_name, SUM(duration_minutes) as total FROM sessions WHERE kid_id=%s GROUP BY date, app_name ORDER BY date DESC LIMIT %s",
              (kid_id, days * 20))
    result = rows_to_dicts(c.fetchall(), c); conn.close(); return result

@app.get("/api/schedules")
def get_schedules(kid_id: Optional[int] = None, key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    if kid_id:
        c.execute("SELECT * FROM schedules WHERE kid_id=%s ORDER BY id", (kid_id,))
    else:
        c.execute("SELECT * FROM schedules ORDER BY kid_id, id")
    rows = rows_to_dicts(c.fetchall(), c)
    for r in rows:
        if isinstance(r.get("days"), str):
            try: r["days"] = json.loads(r["days"])
            except: pass
    conn.close(); return rows

@app.delete("/api/schedules/{schedule_id}")
def delete_schedule(schedule_id: int, key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    c.execute("DELETE FROM schedules WHERE id=%s", (schedule_id,))
    conn.commit(); conn.close(); return {"ok": True}

@app.post("/api/schedules")
def create_schedule(s: ScheduleCreate, key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    c.execute("INSERT INTO schedules (kid_id,label,days,block_from,block_until,is_active) VALUES (%s,%s,%s,%s,%s,%s)",
              (s.kid_id,s.label,json.dumps(s.days),s.block_from,s.block_until,int(s.is_active)))
    conn.commit(); conn.close(); return {"ok": True}

@app.get("/api/debug/pcs")
def debug_pcs(key=Depends(verify_key)):
    from datetime import datetime, timedelta
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT id, nickname, token, active_kid_id, last_seen FROM pcs")
    rows = rows_to_dicts(c.fetchall(), c)
    now = datetime.utcnow()
    cutoff = (now - timedelta(seconds=60)).isoformat()
    conn.close()
    return {"now_utc": now.isoformat(), "cutoff": cutoff, "pcs": rows}

@app.post("/api/admin/reset-weekly-limits")
def reset_weekly_limits(key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    c.execute("UPDATE weekly_limits SET mon=30,tue=30,wed=30,thu=30,fri=30,sat=30,sun=30")
    conn.commit(); conn.close()
    return {"ok": True, "message": "All weekly limits reset to 30 min"}

@app.post("/api/admin/reset-weekly-limits")
def reset_weekly_limits(key=Depends(verify_key)):
    conn = get_db(); c = conn.cursor()
    c.execute("UPDATE weekly_limits SET mon=30,tue=30,wed=30,thu=30,fri=30,sat=30,sun=30")
    conn.commit(); conn.close()
    return {"ok": True, "message": "All weekly limits reset to 30 min"}

@app.get("/")
def health():
    return {"status": "KidoTime API v2 ✅", "version": "2.0.0"}

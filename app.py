#!/usr/bin/env python3
"""
Horse Racing SPA — FastAPI backend.
Login: hardcoded password, no username.
Serves dashboard + API + WebSocket for live odds.
"""

import os, sys, io, json, hashlib, secrets, asyncio, sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from contextlib import asynccontextmanager
import uvicorn

# ─── Config ───────────────────────────────────────────────
HARDCODED_PASSWORD = "168888"
JWT_SECRET = secrets.token_hex(32)
TOKENS = set()

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"
DB_PATH = DATA_DIR / "racing.db"
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ─── Auth ──────────────────────────────────────────────────
security = HTTPBearer(auto_error=False)

def verify_token(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    if credentials is None or credentials.credentials not in TOKENS:
        raise HTTPException(status_code=401, detail="未登入")
    return True

# ─── WebSocket Broadcaster ─────────────────────────────────
class Broadcaster:
    def __init__(self):
        self.clients: list[WebSocket] = []
    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.clients.append(ws)
    def disconnect(self, ws: WebSocket):
        if ws in self.clients:
            self.clients.remove(ws)
    async def broadcast(self, data: dict):
        dead = []
        for ws in self.clients:
            try:
                await ws.send_json(data)
            except:
                dead.append(ws)
        for ws in dead:
            self.clients.remove(ws)

broadcaster = Broadcaster()

# ─── Scheduler ─────────────────────────────────────────────
latest_odds = {}
latest_results = {}
latest_predictions = {}

async def periodic_scrape_odds():
    """Scrape odds every 60 seconds on race days."""
    while True:
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            # Check if today is a race day (Wed/Sun)
            if datetime.now().weekday() in (2, 6):  # Wed=2, Sun=6
                # For now, use existing scraped odds if available
                # In production: run Playwright scraper
                pass
        except Exception as e:
            print(f"Odds scrape error: {e}")
        await asyncio.sleep(60)

# ─── App Lifecycle ─────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    asyncio.create_task(periodic_scrape_odds())
    yield
    # Shutdown

app = FastAPI(title="Horse Racing V10", lifespan=lifespan)

# ─── Auth Routes ───────────────────────────────────────────
@app.post("/api/auth/login")
async def login(request: Request):
    body = await request.json()
    password = body.get("password", "")
    if password == HARDCODED_PASSWORD:
        token = secrets.token_hex(32)
        TOKENS.add(token)
        return {"token": token, "success": True}
    raise HTTPException(status_code=401, detail="密碼錯誤")

@app.post("/api/auth/logout")
async def logout(credentials = Depends(security)):
    TOKENS.discard(credentials.credentials)
    return {"success": True}

@app.get("/api/auth/check")
async def check_auth(auth = Depends(verify_token)):
    return {"authenticated": True}

# ─── Dashboard API ─────────────────────────────────────────
@app.get("/api/dashboard")
async def dashboard(auth = Depends(verify_token)):
    """Return current state: latest race, odds, predictions."""
    today = datetime.now().strftime("%Y-%m-%d")
    return {
        "date": today,
        "odds": latest_odds,
        "results": latest_results,
        "predictions": latest_predictions,
        "last_updated": datetime.now().isoformat()
    }

@app.get("/api/races/{date}")
async def get_races(date: str, auth = Depends(verify_token)):
    """Get race card for a specific date."""
    # Look for race data in predictions folder
    pred_dir = BASE_DIR.parent / "predictions" / date
    if pred_dir.exists():
        rc_path = pred_dir / "racecard_parsed.json"
        if rc_path.exists():
            with open(rc_path) as f:
                return json.load(f)
    return {"error": "No data for this date", "races": {}}

@app.get("/api/models")
async def list_models(auth = Depends(verify_token)):
    """List available models and their latest backtest results."""
    models_dir = BASE_DIR.parent / "models"
    models = []
    if (models_dir / "V10_iterations").exists():
        rundir = models_dir / "V10_iterations"
        for d in sorted(os.listdir(rundir)):
            if d.startswith("V10.") and os.path.isdir(rundir / d):
                summary_path = rundir / d / "SUMMARY.txt"
                if summary_path.exists():
                    with open(summary_path) as f:
                        summary = f.read()
                    models.append({"name": d, "summary": summary})
    return {"models": models}

# ─── Data Exploration APIs ──────────────────────────────────

@app.get("/api/search")
async def global_search(q: str = "", auth = Depends(verify_token)):
    """Search across horses, jockeys, trainers."""
    if len(q) < 1:
        return {"horses": [], "jockeys": [], "trainers": []}
    db = get_db()
    q_like = f"%{q}%"
    horses = [dict(r) for r in db.execute(
        """SELECT h.brand, COALESCE(rn.name, '') as name, h.age, h.sex, h.rating, h.race_count
           FROM horses h LEFT JOIN (SELECT brand, MAX(horse_name) as name FROM results GROUP BY brand) rn ON h.brand = rn.brand
           WHERE h.brand LIKE ? OR rn.name LIKE ? LIMIT 20""",
        (q_like, q_like)).fetchall()]
    jockeys = [dict(r) for r in db.execute(
        "SELECT DISTINCT jockey as name, COUNT(*) as rides, SUM(won) as wins FROM results WHERE jockey LIKE ? GROUP BY jockey ORDER BY wins DESC LIMIT 20",
        (q_like,)).fetchall()]
    trainers = [dict(r) for r in db.execute(
        "SELECT DISTINCT trainer as name, COUNT(*) as rides, SUM(won) as wins FROM results WHERE trainer LIKE ? GROUP BY trainer ORDER BY wins DESC LIMIT 20",
        (q_like,)).fetchall()]
    db.close()
    return {"horses": horses, "jockeys": jockeys, "trainers": trainers}


@app.get("/api/horses")
async def list_horses(
    auth = Depends(verify_token),
    page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=200),
    name: str = "", brand: str = "", sex: str = "", age_min: int = 0, age_max: int = 99,
    rating_min: int = 0, rating_max: int = 150, trainer: str = "",
    sort: str = "rating", order: str = "desc"
):
    """List horses with filters and pagination."""
    db = get_db()
    where = ["1=1"]
    params = []
    if name:
        where.append("rn.name LIKE ?"); params.append(f"%{name}%")
    if brand:
        where.append("h.brand LIKE ?"); params.append(f"%{brand}%")
    if sex:
        where.append("h.sex = ?"); params.append(sex)
    if age_min: where.append("h.age >= ?"); params.append(age_min)
    if age_max < 99: where.append("h.age <= ?"); params.append(age_max)
    if rating_min: where.append("h.rating >= ?"); params.append(rating_min)
    if rating_max < 150: where.append("h.rating <= ?"); params.append(rating_max)
    if trainer:
        where.append("r.trainer LIKE ?"); params.append(f"%{trainer}%")

    allowed_sorts = {"name":"rn.name","brand":"h.brand","age":"h.age","rating":"h.rating",
                     "races":"h.race_count","wins":"wins"}
    sort_col = allowed_sorts.get(sort, "h.rating")
    order_dir = "DESC" if order == "desc" else "ASC"

    sql = f"""SELECT h.brand, COALESCE(rn.name, h.brand) as name, h.age, h.sex,
        CASE WHEN h.sex = 'Gelding' THEN '閹' WHEN h.sex = 'Mare' THEN '雌' WHEN h.sex IN ('Colt','Horse') THEN '雄' WHEN h.sex = 'Rig' THEN '隱睪' WHEN h.sex = 'Filly' THEN '雌' ELSE h.sex END as sex_cn,
        h.rating, h.race_count as races,
        COALESCE(w.wins,0) as wins, COALESCE(w.win_rate,0) as win_rate
        FROM horses h
        LEFT JOIN (SELECT brand, MAX(horse_name) as name FROM results GROUP BY brand) rn ON h.brand = rn.brand
        LEFT JOIN (SELECT brand, SUM(won) as wins, ROUND(CAST(SUM(won) AS FLOAT)/MAX(COUNT(*),1),3) as win_rate FROM results GROUP BY brand) w ON h.brand = w.brand
        WHERE {' AND '.join(where)}
        GROUP BY h.brand ORDER BY {sort_col} {order_dir}
        LIMIT ? OFFSET ?"""
    params.extend([limit, (page-1)*limit])

    rows = [dict(r) for r in db.execute(sql, params).fetchall()]
    total = db.execute(f"SELECT COUNT(DISTINCT h.brand) FROM horses h WHERE {' AND '.join(where)}", params[:-2]).fetchone()[0]
    db.close()
    return {"horses": rows, "total": total, "page": page, "limit": limit}


@app.get("/api/horses/{brand}")
async def horse_detail(brand: str, auth = Depends(verify_token)):
    """Full dashboard for a single horse."""
    db = get_db()
    row = db.execute("SELECT *, CASE WHEN sex = 'Gelding' THEN '閹' WHEN sex = 'Mare' THEN '雌' WHEN sex IN ('Colt','Horse') THEN '雄' WHEN sex = 'Rig' THEN '隱睪' WHEN sex = 'Filly' THEN '雌' ELSE sex END as sex_cn FROM horses WHERE brand = ?", (brand,)).fetchone()
    horse = dict(row) if row else {}
    if not horse:
        raise HTTPException(status_code=404, detail="Horse not found")

    # Career stats
    stats = dict(db.execute("""
        SELECT COUNT(*) as total_races, SUM(won) as wins,
        ROUND(CAST(SUM(won) AS FLOAT)/MAX(COUNT(*),1)*100,1) as win_pct,
        ROUND(AVG(odds),1) as avg_odds, ROUND(AVG(position),1) as avg_pos,
        MAX(rating) as peak_rating
        FROM results WHERE brand = ?
    """, (brand,)).fetchone() or {})

    # Win rate by distance
    dist_stats = [dict(r) for r in db.execute("""
        SELECT r.distance, COUNT(*) as runs, SUM(r.won) as wins,
        ROUND(CAST(SUM(r.won) AS FLOAT)/MAX(COUNT(*),1)*100,1) as win_pct
        FROM results r JOIN races rc ON r.race_id = rc.id
        WHERE r.brand = ? AND r.distance IS NOT NULL
        GROUP BY r.distance ORDER BY runs DESC LIMIT 10
    """, (brand,)).fetchall()]

    # Recent results
    recent = [dict(r) for r in db.execute("""
        SELECT date, course, race_no, position, odds, jockey, trainer, draw, act_wt
        FROM results WHERE brand = ? ORDER BY date DESC LIMIT 10
    """, (brand,)).fetchall()]

    # Jockey partnerships
    jockey_stats = [dict(r) for r in db.execute("""
        SELECT jockey, COUNT(*) as rides, SUM(won) as wins,
        ROUND(CAST(SUM(won) AS FLOAT)/MAX(COUNT(*),1)*100,1) as win_pct
        FROM results WHERE brand = ? GROUP BY jockey ORDER BY rides DESC LIMIT 10
    """, (brand,)).fetchall()]

    db.close()
    return {"horse": horse, "stats": stats, "distance": dist_stats,
            "recent": recent, "jockeys": jockey_stats}


@app.get("/api/jockeys")
async def list_jockeys(
    auth = Depends(verify_token),
    page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=200),
    name: str = "", sort: str = "wins", order: str = "desc"
):
    """List jockeys with stats."""
    db = get_db()
    where = ["1=1"]; params = []
    if name: where.append("jockey LIKE ?"); params.append(f"%{name}%")
    allowed = {"name":"jockey","wins":"wins","rides":"rides","win_rate":"win_rate"}
    sc = allowed.get(sort, "wins")
    od = "DESC" if order == "desc" else "ASC"

    sql = f"""SELECT jockey as name, COUNT(*) as rides, SUM(won) as wins,
        ROUND(CAST(SUM(won) AS FLOAT)/MAX(COUNT(*),1)*100,1) as win_rate,
        ROUND(AVG(CASE WHEN position <= 3 THEN 1 ELSE 0 END)*100,1) as place_pct
        FROM results WHERE {' AND '.join(where)}
        GROUP BY jockey ORDER BY {sc} {od} LIMIT ? OFFSET ?"""
    params.extend([limit, (page-1)*limit])
    rows = [dict(r) for r in db.execute(sql, params).fetchall()]
    total = db.execute(f"SELECT COUNT(DISTINCT jockey) FROM results WHERE {' AND '.join(where)}", params[:-2]).fetchone()[0]
    db.close()
    return {"jockeys": rows, "total": total, "page": page}


@app.get("/api/jockeys/{name}")
async def jockey_detail(name: str, auth = Depends(verify_token)):
    """Full dashboard for a jockey."""
    db = get_db()
    stats = dict(db.execute("""
        SELECT COUNT(*) as rides, SUM(won) as wins,
        ROUND(CAST(SUM(won) AS FLOAT)/MAX(COUNT(*),1)*100,1) as win_rate,
        ROUND(AVG(CASE WHEN position <= 3 THEN 1 ELSE 0 END)*100,1) as place_rate,
        ROUND(AVG(odds),1) as avg_odds
        FROM results WHERE jockey = ?
    """, (name,)).fetchone() or {})

    trainer_pairs = [dict(r) for r in db.execute("""
        SELECT trainer, COUNT(*) as rides, SUM(won) as wins,
        ROUND(CAST(SUM(won) AS FLOAT)/MAX(COUNT(*),1)*100,1) as win_pct
        FROM results WHERE jockey = ? GROUP BY trainer ORDER BY rides DESC LIMIT 15
    """, (name,)).fetchall()]

    horse_pairs = [dict(r) for r in db.execute("""
        SELECT brand, horse_name, COUNT(*) as rides, SUM(won) as wins,
        ROUND(CAST(SUM(won) AS FLOAT)/MAX(COUNT(*),1)*100,1) as win_pct
        FROM results WHERE jockey = ? GROUP BY brand ORDER BY rides DESC LIMIT 20
    """, (name,)).fetchall()]

    monthly = [dict(r) for r in db.execute("""
        SELECT substr(date,1,7) as month, COUNT(*) as rides, SUM(won) as wins
        FROM results WHERE jockey = ? AND date >= '2024-01-01'
        GROUP BY month ORDER BY month DESC LIMIT 24
    """, (name,)).fetchall()]

    db.close()
    return {"stats": stats, "trainers": trainer_pairs, "horses": horse_pairs, "monthly": monthly}


@app.get("/api/trainers")
async def list_trainers(
    auth = Depends(verify_token),
    page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=200),
    name: str = "", sort: str = "wins", order: str = "desc"
):
    """List trainers with stats."""
    db = get_db()
    where = ["1=1"]; params = []
    if name: where.append("trainer LIKE ?"); params.append(f"%{name}%")
    allowed = {"name":"trainer","wins":"wins","rides":"rides","win_rate":"win_rate"}
    sc = allowed.get(sort, "wins")
    od = "DESC" if order == "desc" else "ASC"

    sql = f"""SELECT trainer as name, COUNT(*) as rides, SUM(won) as wins,
        ROUND(CAST(SUM(won) AS FLOAT)/MAX(COUNT(*),1)*100,1) as win_rate,
        ROUND(AVG(CASE WHEN position <= 3 THEN 1 ELSE 0 END)*100,1) as place_pct
        FROM results WHERE {' AND '.join(where)}
        GROUP BY trainer ORDER BY {sc} {od} LIMIT ? OFFSET ?"""
    params.extend([limit, (page-1)*limit])
    rows = [dict(r) for r in db.execute(sql, params).fetchall()]
    total = db.execute(f"SELECT COUNT(DISTINCT trainer) FROM results WHERE {' AND '.join(where)}", params[:-2]).fetchone()[0]
    db.close()
    return {"trainers": rows, "total": total, "page": page}


@app.get("/api/trainers/{name}")
async def trainer_detail(name: str, auth = Depends(verify_token)):
    """Full dashboard for a trainer."""
    db = get_db()
    stats = dict(db.execute("""
        SELECT COUNT(*) as rides, SUM(won) as wins,
        ROUND(CAST(SUM(won) AS FLOAT)/MAX(COUNT(*),1)*100,1) as win_rate,
        ROUND(AVG(CASE WHEN position <= 3 THEN 1 ELSE 0 END)*100,1) as place_rate
        FROM results WHERE trainer = ?
    """, (name,)).fetchone() or {})

    jockeys = [dict(r) for r in db.execute("""
        SELECT jockey, COUNT(*) as rides, SUM(won) as wins,
        ROUND(CAST(SUM(won) AS FLOAT)/MAX(COUNT(*),1)*100,1) as win_pct
        FROM results WHERE trainer = ? GROUP BY jockey ORDER BY rides DESC LIMIT 15
    """, (name,)).fetchall()]

    horses = [dict(r) for r in db.execute("""
        SELECT brand, horse_name, COUNT(*) as rides, SUM(won) as wins,
        ROUND(CAST(SUM(won) AS FLOAT)/MAX(COUNT(*),1)*100,1) as win_pct
        FROM results WHERE trainer = ? GROUP BY brand ORDER BY rides DESC LIMIT 20
    """, (name,)).fetchall()]

    monthly = [dict(r) for r in db.execute("""
        SELECT substr(date,1,7) as month, COUNT(*) as rides, SUM(won) as wins
        FROM results WHERE trainer = ? AND date >= '2024-01-01'
        GROUP BY month ORDER BY month DESC LIMIT 24
    """, (name,)).fetchall()]

    db.close()
    return {"stats": stats, "jockeys": jockeys, "horses": horses, "monthly": monthly}


# ─── Helpers ─────────────────────────────────────────────────
SEX_MAP = {'Gelding':'閹','Mare':'雌','Colt':'雄','Rig':'隱睪','Horse':'雄','Filly':'雌'}

@app.get("/api/filters")
async def get_filter_options(auth = Depends(verify_token)):
    """Get available filter values for dropdowns."""
    db = get_db()
    sexes = [r[0] for r in db.execute("SELECT DISTINCT sex FROM horses WHERE sex IS NOT NULL").fetchall()]
    # Map English sex terms to Chinese
    sex_map = {'Gelding':'閹','Mare':'雌','Colt':'雄','Rig':'隱睪','Horse':'雄','Filly':'雌'}
    sexes_display = [{'value': s, 'label': SEX_MAP.get(s, s)} for s in sexes]
    trainers = [r[0] for r in db.execute("SELECT DISTINCT trainer FROM results ORDER BY trainer LIMIT 100").fetchall()]
    jockeys = [r[0] for r in db.execute("SELECT DISTINCT jockey FROM results ORDER BY jockey LIMIT 100").fetchall()]
    db.close()
    return {"sexes": sexes_display, "trainers": trainers, "jockeys": jockeys}
@app.websocket("/ws/odds")
async def ws_odds(websocket: WebSocket):
    await broadcaster.connect(websocket)
    try:
        # Send current state immediately
        if latest_odds:
            await websocket.send_json({"type": "odds", "data": latest_odds})
        while True:
            await websocket.receive_text()  # keep alive
    except WebSocketDisconnect:
        broadcaster.disconnect(websocket)

# ─── Static SPA ────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def spa_index():
    """Serve the SPA HTML."""
    spa_path = STATIC_DIR / "index.html"
    if spa_path.exists():
        return spa_path.read_text(encoding="utf-8")
    # Inline fallback
    return """
    <!DOCTYPE html><html><head><title>Horse Racing V10</title></head>
    <body><h1>Horse Racing V10</h1><p>SPA loading...</p></body></html>
    """

@app.get("/api/health")
async def health():
    return {"status": "ok", "time": datetime.now().isoformat()}

# ─── Main ──────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8005)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)

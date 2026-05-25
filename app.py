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
from model_config import list_models as _list_model_configs, load_config, FEATURES, FEATURE_CATEGORIES

# ─── Config ───────────────────────────────────────────────
HARDCODED_PASSWORD = "168888"
JWT_SECRET = secrets.token_hex(32)
TOKENS = set()

BASE_DIR   = Path(__file__).parent
DATA_DIR   = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"
MODELS_DIR = BASE_DIR / "models"
DB_PATH    = DATA_DIR / "racing.db"
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

app = FastAPI(title="馬場分析", lifespan=lifespan)

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
    """Return current state: latest races, predictions, results, and stats."""
    db = get_db()

    # Latest race date with results
    latest_date = db.execute("SELECT MAX(date) FROM results").fetchone()[0]

    # Today's races (if any)
    today = datetime.now().strftime("%Y-%m-%d")
    today_races = [dict(r) for r in db.execute("""
        SELECT raceno as race_no, course, CAST(distance AS INTEGER) as distance,
               CASE WHEN CAST(class AS INTEGER) = 1 THEN 'G1' WHEN CAST(class AS INTEGER) = 2 THEN 'G2'
                    WHEN CAST(class AS INTEGER) = 3 THEN 'G3' ELSE CAST(CAST(class AS INTEGER) AS TEXT) END as class,
               going, participants
        FROM races WHERE date = ? ORDER BY raceno
    """, (today,)).fetchall()]

    # Latest results (last 20)
    recent_results = [dict(r) for r in db.execute("""
        SELECT r.date, r.race_no, r.course, r.position, r.horse_name, r.jockey, r.trainer, r.odds, rc.distance, rc.class
        FROM results r JOIN races rc ON r.date = rc.date AND r.race_no = rc.raceno AND r.course = rc.course
        WHERE r.position = '1' AND r.date = ?
        ORDER BY r.race_no LIMIT 20
    """, (latest_date,)).fetchall()] if latest_date else []

    # Model accuracy stats
    total_races = db.execute("SELECT COUNT(DISTINCT date || '-' || race_no) FROM results WHERE position IS NOT NULL").fetchone()[0]
    total_results = db.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    horse_count = db.execute("SELECT COUNT(*) FROM horses").fetchone()[0]
    jockey_count = db.execute("SELECT COUNT(DISTINCT jockey) FROM results").fetchone()[0]
    trainer_count = db.execute("SELECT COUNT(DISTINCT trainer) FROM results").fetchone()[0]

    # Top jockeys by wins
    top_jockeys = [dict(r) for r in db.execute("""
        SELECT jockey, SUM(won) as wins, COUNT(*) as rides,
        ROUND(CAST(SUM(won) AS FLOAT)/MAX(COUNT(*),1)*100,1) as win_rate
        FROM results GROUP BY jockey ORDER BY wins DESC LIMIT 10
    """).fetchall()]

    # Top trainers
    top_trainers = [dict(r) for r in db.execute("""
        SELECT trainer, SUM(won) as wins, COUNT(*) as rides,
        ROUND(CAST(SUM(won) AS FLOAT)/MAX(COUNT(*),1)*100,1) as win_rate
        FROM results GROUP BY trainer ORDER BY wins DESC LIMIT 10
    """).fetchall()]

    # Win rate by draw position
    draw_stats = [dict(r) for r in db.execute("""
        SELECT draw, COUNT(*) as total, SUM(won) as wins,
        ROUND(CAST(SUM(won) AS FLOAT)/MAX(COUNT(*),1)*100,1) as win_pct
        FROM results WHERE CAST(draw AS INTEGER) BETWEEN 1 AND 14 GROUP BY draw ORDER BY CAST(draw AS INTEGER)
    """).fetchall()]

    # Win rate by odds range
    odds_stats = [dict(r) for r in db.execute("""
        SELECT CASE WHEN CAST(odds AS REAL) < 3 THEN '1-3x' WHEN CAST(odds AS REAL) < 6 THEN '3-6x'
        WHEN CAST(odds AS REAL) < 10 THEN '6-10x' WHEN CAST(odds AS REAL) < 20 THEN '10-20x' ELSE '20x+' END as odds_range,
        COUNT(*) as total, SUM(won) as wins
        FROM results WHERE CAST(odds AS REAL) > 1 GROUP BY odds_range ORDER BY MIN(CAST(odds AS REAL))
    """).fetchall()]

    db.close()

    return {
        "date": today,
        "latest_race_date": latest_date,
        "today_races": today_races,
        "recent_winners": recent_results,
        "stats": {
            "total_races": total_races,
            "total_results": total_results,
            "horses": horse_count,
            "jockeys": jockey_count,
            "trainers": trainer_count,
        },
        "top_jockeys": top_jockeys,
        "top_trainers": top_trainers,
        "draw_stats": draw_stats,
        "odds_stats": odds_stats,
        "last_updated": datetime.now().isoformat()
    }

@app.get("/api/races/{date}")
async def get_races(date: str, auth = Depends(verify_token), model: str = Query(None)):
    """Get merged race data: card + results + predictions for a date."""
    db = get_db()

    # Racecard always comes from predictions/ (raw scraped data)
    card_dir = BASE_DIR / "predictions" / date
    racecard = {}
    if card_dir.exists():
        rc_path = card_dir / "racecard_parsed.json"
        if rc_path.exists():
            with open(rc_path) as f:
                racecard = json.load(f)

    # Predictions come from model results dir if model is specified
    if model:
        pred_json_path = MODELS_DIR / model / "results" / date / "predictions.json"
    else:
        pred_json_path = card_dir / "predictions.json"

    # Load model predictions (prob/edge per horse)
    predictions = {}
    if pred_json_path.exists():
        with open(pred_json_path, encoding='utf-8') as f:
            predictions = json.load(f)

    # Results from DB, keyed by normalised race_no then by brand
    results_by_race = {}
    results_by_brand = {}  # (race_no_str, brand) -> result row
    for r in db.execute("""
        SELECT r.race_no, r.brand, r.position, r.horse_name, r.jockey, r.trainer,
               r.odds as res_odds, r.draw, r.act_wt, r.lbw, r.running_style
        FROM results r WHERE r.date = ? ORDER BY r.race_no, CAST(r.position AS INTEGER)
    """, (date,)).fetchall():
        rn = str(int(r['race_no']))
        if rn not in results_by_race:
            results_by_race[rn] = []
        row = dict(r)
        results_by_race[rn].append(row)
        results_by_brand[(rn, r['brand'])] = row

    # Race metadata from DB
    db_races = {}
    for r in db.execute("SELECT * FROM races WHERE date = ? ORDER BY raceno", (date,)).fetchall():
        db_races[str(int(r['raceno']))] = dict(r)

    BET_EDGE_THRESHOLD = 1.0  # bet when edge (win_prob * odds) > 1.0 (positive EV)

    # Collect all race numbers from all sources
    all_keys = set()
    for k in list(racecard.keys()) + list(results_by_race.keys()) + list(db_races.keys()):
        try: all_keys.add(str(int(k)))
        except: pass

    races_output = []
    for rn in sorted(all_keys, key=int):
        rc         = racecard.get(rn) or racecard.get(rn.zfill(2), {})
        race_info  = db_races.get(rn, {})
        race_results = results_by_race.get(rn, [])
        pred_race  = (predictions.get(rn) or predictions.get(rn.zfill(2))
                      or predictions.get(str(int(rn))) or {})
        pred_horses_list = pred_race.get('horses', [])
        pred_by_brand = {ph.get('brand', ''): ph for ph in pred_horses_list}
        pred_by_no    = {str(ph.get('no', '')): ph for ph in pred_horses_list}

        dist_raw = race_info.get("distance") or rc.get("distance", "")
        dist_str = str(dist_raw).rstrip('0').rstrip('.') if dist_raw else "?"
        cls_raw  = race_info.get("class") or pred_race.get("class") or rc.get("class", "?")
        cls_str  = str(cls_raw).rstrip('0').rstrip('.') if cls_raw else "?"

        # Build horse list: use prediction horses as primary (they cover all participants)
        # Fall back to racecard horses, then results-only
        card_horses = rc.get("horses", [])
        if pred_horses_list:
            horse_source = pred_horses_list
        elif card_horses:
            horse_source = card_horses
        else:
            horse_source = [{"no": r.get("race_no", ""), "name": r.get("horse_name", ""),
                             "brand": r.get("brand", ""), "jockey": r.get("jockey", ""),
                             "trainer": r.get("trainer", ""), "draw": r.get("draw", "")}
                            for r in race_results]

        horses_out = []
        for h in horse_source:
            brand = h.get("brand", "")
            # Get result for this horse
            res = results_by_brand.get((rn, brand))
            if not res:
                # Try matching by horse number from racecard
                card_h = next((ch for ch in card_horses if str(ch.get("no","")) == str(h.get("no",""))), None)
                if card_h:
                    res = results_by_brand.get((rn, card_h.get("brand", "")))
            # Get prediction
            ph = pred_by_brand.get(brand) or pred_by_no.get(str(h.get("no", "")))
            pos = int(res["position"]) if res and res.get("position") else None
            res_odds = float(res["res_odds"]) if res and res.get("res_odds") else None
            prob  = ph.get("prob")   if ph else None
            edge  = ph.get("edge")   if ph else None

            horses_out.append({
                "no":          h.get("no", ""),
                "name":        h.get("name", ""),
                "brand":       brand,
                "jockey":      h.get("jockey", ""),
                "trainer":     h.get("trainer", ""),
                "draw":        h.get("draw", ""),
                "weight":      h.get("weight", ""),
                "rating":      h.get("rating", ""),
                "win_odds":    h.get("win_odds", ""),
                "prob":        prob,
                "win_prob":    ph.get("win_prob") if ph else None,
                "edge":        edge,
                "features":    ph.get("features") if ph else None,
                "position":    pos,
                "result_odds": res_odds,
                "lbw":         res.get("lbw")          if res else None,
                "running":     res.get("running_style") if res else None,
            })

        # Per-race betting analysis
        # Find top predicted horse (highest prob among horses with edge > threshold)
        bettable = [h for h in horses_out if h["prob"] is not None and (h["edge"] or 0) > BET_EDGE_THRESHOLD]
        top_pred  = max(horses_out, key=lambda h: h["prob"] or 0, default=None) if horses_out else None
        bet_horse = max(bettable, key=lambda h: h["prob"], default=None)
        actual_winner = next((h for h in horses_out if h["position"] == 1), None)

        bet_pnl = None
        if bet_horse and actual_winner:
            if bet_horse["brand"] == actual_winner["brand"]:
                odds_used = bet_horse.get("result_odds") or float(bet_horse.get("win_odds") or 0)
                bet_pnl = round(odds_used - 1, 2) if odds_used else 1.0
            else:
                bet_pnl = -1.0

        race = {
            "race_no":       int(rn),
            "distance":      dist_str,
            "class":         cls_str,
            "going":         race_info.get("going", ""),
            "participants":  len(horses_out),
            "has_results":   bool(race_results),
            "horses":        horses_out,
            "results":       race_results,
            "bet": {
                "placed":        bet_horse is not None,
                "horse_no":      bet_horse["no"]   if bet_horse else None,
                "horse_name":    bet_horse["name"]  if bet_horse else None,
                "horse_brand":   bet_horse["brand"] if bet_horse else None,
                "prob":          bet_horse["prob"]  if bet_horse else None,
                "edge":          bet_horse["edge"]  if bet_horse else None,
                "win_odds":      bet_horse.get("win_odds") if bet_horse else None,
                "result_odds":   bet_horse.get("result_odds") if bet_horse else None,
                "correct":       (bet_horse and actual_winner and
                                  bet_horse["brand"] == actual_winner["brand"]),
                "pnl":           bet_pnl,
                "top_predicted": top_pred["name"] if top_pred else None,
                "actual_winner": actual_winner["name"] if actual_winner else None,
            } if pred_horses_list else None,
        }
        races_output.append(race)

    db.close()
    return {
        "date":          date,
        "races":         races_output,
        "count":         len(races_output),
        "_feature_cols": predictions.get("_feature_cols", []),
        "model":         model,
    }

@app.get("/api/models")
async def list_models_endpoint(auth = Depends(verify_token)):
    """List all model configs with summary stats."""
    configs = _list_model_configs()
    out = []
    for cfg in configs:
        summary = cfg.pop('_summary', {})
        out.append({
            "name":          cfg.get("name"),
            "description":   cfg.get("description", ""),
            "strategy_type": cfg.get("strategy_type", "xgb_walkforward"),
            "version":       cfg.get("version", ""),
            "parent":        cfg.get("parent"),
            "notes":         cfg.get("notes", ""),
            "active":        cfg.get("active", False),
            "created":       cfg.get("created", ""),
            "summary":       summary,
        })
    return {"models": out}


@app.get("/api/model-config/{name}")
async def get_model_config(name: str, auth = Depends(verify_token)):
    """Return full config + feature catalogue for a named model."""
    try:
        cfg = load_config(name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Model '{name}' not found")
    return {
        "config":     cfg,
        "features":   FEATURES,
        "categories": FEATURE_CATEGORIES,
    }


@app.post("/api/models/{name}/activate")
async def activate_model(name: str, auth = Depends(verify_token)):
    """Set a model as the active model."""
    from model_config import set_active_model
    cfg_path = MODELS_DIR / name / "config.json"
    if not cfg_path.exists():
        raise HTTPException(status_code=404, detail=f"Model '{name}' not found")
    set_active_model(name)
    return {"success": True, "active": name}

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
        SELECT rc.distance, COUNT(*) as runs, SUM(r.won) as wins,
        ROUND(CAST(SUM(r.won) AS FLOAT)/MAX(COUNT(*),1)*100,1) as win_pct
        FROM results r JOIN races rc ON r.date = rc.date AND r.race_no = rc.raceno AND r.course = rc.course
        WHERE r.brand = ? AND rc.distance IS NOT NULL
        GROUP BY rc.distance ORDER BY runs DESC LIMIT 10
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
    <!DOCTYPE html><html><head><title>馬場分析</title></head>
    <body><h1>馬場分析</h1><p>載入中...</p></body></html>
    """

@app.get("/api/health")
async def health():
    return {"status": "ok", "time": datetime.now().isoformat()}

@app.get("/api/dates")
async def available_dates(auth = Depends(verify_token), model: str = Query(None)):
    """List dates that have results in DB, with prediction status per model."""
    db = get_db()
    db_dates = [r[0] for r in db.execute(
        "SELECT DISTINCT date FROM results ORDER BY date DESC"
    ).fetchall()]
    db.close()

    # Determine prediction source directory
    if model:
        pred_dir = MODELS_DIR / model / "results"
    else:
        pred_dir = BASE_DIR / "predictions"

    dates = []
    for d in db_dates:
        if len(d) != 10 or d[4] != '-':
            continue
        dp = pred_dir / d
        has_predictions = (dp / "predictions.json").exists() if dp.exists() else False

        db = get_db()
        res_count   = db.execute("SELECT COUNT(DISTINCT race_no) FROM results WHERE date=?", (d,)).fetchone()[0]
        horse_count = db.execute("SELECT COUNT(*) FROM results WHERE date=?", (d,)).fetchone()[0]
        db.close()

        dates.append({
            "date":            d,
            "has_predictions": has_predictions,
            "race_count":      res_count,
            "horse_count":     horse_count,
        })
    return {"dates": dates}

# ─── Main ──────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8005)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)

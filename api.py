"""FastAPI router + scheduler-action registrar.

Mounted into app.py at startup.
Routes land under `/api/...`. Action registration (called from app.py
lifespan) wires scraper subprocesses into the same scheduler that runs
v1 actions, so the SPA's Schedules page works for both worlds.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, HTTPException

import json as _json

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "racing.db"

router = APIRouter(prefix="/api", tags=["api"])


# ─── Cached feature catalogue + bibliography (loaded once at first request) ──
_FEATURE_CACHE: dict | None = None
_BIBLIO_CACHE: dict | None = None


def _features_path() -> Path:
    return BASE_DIR / "features" / "descriptions.json"


def _bibliography_path() -> Path:
    return BASE_DIR / "features" / "bibliography.json"


def _load_features() -> dict:
    global _FEATURE_CACHE
    if _FEATURE_CACHE is None:
        p = _features_path()
        _FEATURE_CACHE = _json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return _FEATURE_CACHE


def _load_bibliography() -> dict:
    global _BIBLIO_CACHE
    if _BIBLIO_CACHE is None:
        p = _bibliography_path()
        _BIBLIO_CACHE = _json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return _BIBLIO_CACHE


def invalidate_feature_cache() -> None:
    """Call when descriptions.json or bibliography.json is regenerated."""
    global _FEATURE_CACHE, _BIBLIO_CACHE
    _FEATURE_CACHE = None
    _BIBLIO_CACHE = None


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ─── Health & introspection ───────────────────────────────────────────────────

@router.get("/health")
def health() -> dict:
    info: dict = {
        "enabled": True,
        "db_path": str(DB_PATH),
        "db_exists": DB_PATH.exists(),
        "pid": os.getpid(),
    }
    if DB_PATH.exists():
        conn = _connect()
        try:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            info["table_count"] = len(tables)
        finally:
            conn.close()
    return info


@router.get("/schema-info")
def schema_info() -> dict:
    if not DB_PATH.exists():
        return {"error": "DB not initialized", "db_path": str(DB_PATH)}
    conn = _connect()
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()]
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    finally:
        conn.close()
    return {"db_path": str(DB_PATH), "tables": counts}


# ─── Scraper subprocess invocation (shared with scheduler actions) ───────────

# Map slug -> (script_path_relative_to_base, default_argv_extras)
SCRAPERS: dict[str, tuple[str, list[str]]] = {
    "race_card":          ("scrapers/scrape_race_card.py",          ["--next"]),
    "results":               ("scrapers/scrape_results.py",               ["--recent"]),
    "horse_pedigree":        ("scrapers/scrape_horse_pedigree.py",        ["--limit", "50"]),
    "barrier_trials":        ("scrapers/scrape_barrier_trials.py",        ["--recent"]),
    "trackwork":             ("scrapers/scrape_trackwork.py",             ["--recent"]),
    "vet_records":           ("scrapers/scrape_vet_records.py",           ["--recent"]),
    "roarers":               ("scrapers/scrape_roarers.py",               []),
    "weather":               ("scrapers/scrape_weather.py",               []),  # caller passes --date/--course
    "track_bias":            ("scrapers/compute_track_bias.py",           []),  # caller passes --date or --since/--until
}


async def _spawn_scraper(slug: str, extra_argv: list[str], scraper_job: dict) -> int:
    """Run one scraper as a subprocess, occupying the shared scraper_job slot."""
    if scraper_job["active"]:
        raise HTTPException(status_code=409, detail=f"scraper already running ({scraper_job.get('current_task')})")
    spec = SCRAPERS.get(slug)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"unknown scraper slug: {slug}")
    from datetime import datetime
    script, default_extras = spec
    argv = [sys.executable, "-u", str(BASE_DIR / script), *(extra_argv or default_extras)]
    scraper_job.update({
        "active": True, "stopping": False,
        "started_at": datetime.now().isoformat(), "current_task": f"scraper:{slug}", "_proc": None,
    })
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(BASE_DIR),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        scraper_job["_proc"] = proc
        rc = await proc.wait()
        return rc
    finally:
        scraper_job.update({"active": False, "stopping": False, "current_task": None, "_proc": None})


def register_actions(action_registry: dict[str, Callable], scraper_job: dict, is_shutting_down: Callable[[], bool]) -> None:
    """Called by app.py lifespan. Registers one scheduler
    action per scraper (slug-keyed so cron entries are precise)."""
    for slug, (script, default_extras) in SCRAPERS.items():
        action_key = f"scrape_{slug}"

        def _make_action(slug=slug, default_extras=default_extras):
            def _act(args: dict) -> None:
                if scraper_job["active"]:
                    raise RuntimeError("scraper already running")
                if is_shutting_down():
                    raise RuntimeError("server shutting down")
                extra = args.get("argv") or default_extras
                asyncio.create_task(_spawn_scraper(slug, list(extra), scraper_job))
            return _act

        action_registry[action_key] = _make_action()


# ─── On-demand fire endpoints ────────────────────────────────────────────────

@router.get("/scrapers")
def list_scrapers() -> dict:
    """List scrapers and their default argv."""
    return {slug: {"script": s, "default_argv": d} for slug, (s, d) in SCRAPERS.items()}


@router.post("/scrapers/{slug}/run")
async def run_scraper(slug: str, argv: list[str] | None = None):
    """Fire a scraper on-demand. Falls back to default argv if none provided.

    Note: relies on app.py's `scraper_job` shared dict; this endpoint imports it
    lazily to keep the the module standalone-testable.
    """
    from app import scraper_job  # type: ignore  (lazy import; app.py owns the dict)
    rc = await _spawn_scraper(slug, argv or [], scraper_job)
    return {"slug": slug, "exit_code": rc}


# ─── Checkpoint introspection ────────────────────────────────────────────────

@router.get("/checkpoints")
def list_checkpoints() -> dict:
    import json as _json
    cdir = DATA_DIR / "checkpoints"
    if not cdir.exists():
        return {}
    out: dict = {}
    for f in cdir.glob("*.json"):
        try:
            out[f.stem] = _json.loads(f.read_text())
        except Exception as exc:
            out[f.stem] = {"error": str(exc)}
    return out


# ─── Kill switch ─────────────────────────────────────────────────────────────

@router.get("/kill-switch")
def kill_switch_state() -> dict:
    if not DB_PATH.exists():
        return {"error": "DB not initialized"}
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT halted, halt_reason, halted_at, halted_by, last_heartbeat "
            "FROM kill_switch_state WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"halted": False}
    return {
        "halted": bool(row[0]),
        "halt_reason": row[1],
        "halted_at": row[2],
        "halted_by": row[3],
        "last_heartbeat": row[4],
    }


@router.post("/kill-switch")
def kill_switch_set(halted: bool, reason: str | None = None, by: str | None = None) -> dict:
    if not DB_PATH.exists():
        raise HTTPException(404, "DB not initialized")
    from datetime import datetime
    conn = _connect()
    try:
        conn.execute(
            "UPDATE kill_switch_state SET halted = ?, halt_reason = ?, halted_at = ?, halted_by = ? WHERE id = 1",
            (1 if halted else 0, reason, datetime.now().isoformat() if halted else None, by),
        )
        conn.commit()
    finally:
        conn.close()
    return kill_switch_state()


# ─── Feature catalogue + bibliography ─────────────────────────────────────────

@router.get("/features")
def list_features(lang: str = "zh") -> dict:
    """Full 174-feature catalogue grouped by category.

    `lang` defaults to "zh" — the field names returned (`description`, `notes`,
    `display_name`) carry that language; both languages are always present as
    `*_zh` and `*_en` so the SPA can switch live.
    """
    feats = _load_features()
    biblio = _load_bibliography()
    # Annotate each feature with its expanded source citations
    out_by_cat: dict[int, list[dict]] = {}
    for fid, rec in feats.items():
        sources_expanded = [biblio[b] for b in (rec.get("all_sources") or []) if b in biblio]
        flat = {**rec, "sources_expanded": sources_expanded}
        out_by_cat.setdefault(rec["category"], []).append(flat)
    for cat in out_by_cat:
        out_by_cat[cat].sort(key=lambda r: r["id"])
    # Category labels — mirrored from features_expanded_zh_hant.md table headers.
    cat_labels = {
        1: ("馬匹檔案", "Horse profile"),
        2: ("勝率與報酬指標", "Win-rate & returns"),
        3: ("適應性", "Adaptability"),
        4: ("練馬師狀態", "Trainer form"),
        5: ("閘號", "Draw"),
        6: ("負磅", "Weight"),
        7: ("賽事背景", "Race context"),
        8: ("近期狀態", "Recent form"),
        9: ("裝備與獸醫", "Gear & vet"),
        10: ("步速與跑法", "Pace & style"),
        11: ("綜合速度與班次指標", "Composite speed/class"),
        12: ("交互特徵", "Interactions"),
        13: ("連贏與序位結構", "Exotic & order"),
        14: ("市場訊號", "Market signals"),
        15: ("場地動態", "Track dynamics"),
        16: ("生物力學/外部數據", "Biomechanics"),
    }
    categories = []
    for cat in sorted(out_by_cat):
        zh, en = cat_labels.get(cat, (f"Cat {cat}", f"Category {cat}"))
        categories.append({
            "id": cat,
            "name_zh": zh,
            "name_en": en,
            "count": len(out_by_cat[cat]),
            "features": out_by_cat[cat],
        })
    return {"lang": lang, "categories": categories, "total": sum(len(v) for v in out_by_cat.values())}


@router.get("/features/{feature_id}")
def get_feature(feature_id: str) -> dict:
    feats = _load_features()
    if feature_id not in feats:
        raise HTTPException(404, f"unknown feature {feature_id}")
    biblio = _load_bibliography()
    rec = feats[feature_id]
    sources_expanded = [biblio[b] for b in (rec.get("all_sources") or []) if b in biblio]
    return {**rec, "sources_expanded": sources_expanded}


@router.get("/bibliography")
def list_bibliography() -> dict:
    """All B-id citations from `features_expanded_zh_hant.md` Appendix B."""
    biblio = _load_bibliography()
    items = sorted(biblio.values(), key=lambda x: int(x["id"][1:]))
    return {"items": items, "total": len(items)}


# ─── Strategies CRUD (lightweight; full UI in P7) ─────────────────────────────

@router.get("/strategies")
def list_strategies() -> list[dict]:
    if not DB_PATH.exists():
        return []
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, name, name_zh, name_en, enabled, stage2_enabled, stage2_alpha, "
            "stage2_beta, calibration, edge_threshold, bet_max_odds, kelly_fraction "
            "FROM strategies ORDER BY id"
        ).fetchall()
        cols = ("id","name","name_zh","name_en","enabled","stage2_enabled","stage2_alpha",
                "stage2_beta","calibration","edge_threshold","bet_max_odds","kelly_fraction")
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()


@router.post("/strategies")
def create_strategy(name: str, name_zh: str | None = None, name_en: str | None = None) -> dict:
    if not DB_PATH.exists():
        raise HTTPException(404, "DB not initialized")
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO strategies (name, name_zh, name_en, enabled) VALUES (?,?,?,1)",
            (name, name_zh or name, name_en or name),
        )
        conn.commit()
        sid = conn.execute("SELECT id FROM strategies WHERE name = ?", (name,)).fetchone()[0]
    finally:
        conn.close()
    return {"id": sid, "name": name}


# ─── Audit + CLV reports ──────────────────────────────────────────────────────

@router.get("/strategies/{strategy_id}/audit")
def strategy_audit(strategy_id: int, date_from: str, date_to: str,
                   max_odds: float = 25.0, edge: float = 1.05,
                   kelly: float = 0.25, bankroll: float = 10000.0) -> dict:
    from betting import filters as filt, audit
    if not DB_PATH.exists():
        raise HTTPException(404, "DB not initialized")
    conn = _connect()
    try:
        s = filt.FilterSettings(bet_max_odds=max_odds, edge_threshold=edge)
        return audit.audit(conn, strategy_id, date_from, date_to,
                           settings=s, kelly_fraction_strat=kelly, bankroll=bankroll)
    finally:
        conn.close()


@router.get("/strategies/{strategy_id}/sweep")
def strategy_sweep(strategy_id: int, date_from: str, date_to: str) -> dict:
    from betting import audit
    if not DB_PATH.exists():
        raise HTTPException(404, "DB not initialized")
    conn = _connect()
    try:
        return {"sweep": audit.sweep(conn, strategy_id, date_from, date_to)}
    finally:
        conn.close()


@router.get("/strategies/{strategy_id}/health")
def strategy_health(strategy_id: int) -> dict:
    """Recent ECE / Brier / log-loss windows."""
    if not DB_PATH.exists():
        raise HTTPException(404, "DB not initialized")
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT window_start, window_end, brier, log_loss, ece, sample_count "
            "FROM calibration_metrics WHERE strategy_id = ? ORDER BY window_end DESC LIMIT 30",
            (strategy_id,),
        ).fetchall()
        cols = ("window_start","window_end","brier","log_loss","ece","sample_count")
    finally:
        conn.close()
    return {"strategy_id": strategy_id, "windows": [dict(zip(cols, r)) for r in rows]}


# ─── Strategy dashboard: aggregate summary for a single strategy ──────────────

@router.get("/strategies/{strategy_id}/dashboard")
def strategy_dashboard(strategy_id: int) -> dict:
    """Single-call summary feeding the SPA's Strategy Dashboard tab.

    Returns: strategy config, full calibration trend, prediction coverage,
    counterfactual audit on the full prediction window, and the top recent
    bets (placed by the audit pipeline) ranked by edge.
    """
    if not DB_PATH.exists():
        raise HTTPException(404, "DB not initialized")
    conn = _connect()
    try:
        srow = conn.execute(
            "SELECT id, name, name_zh, name_en, description, enabled, stage2_enabled, "
            "stage2_alpha, stage2_beta, calibration, edge_threshold, min_prob, "
            "bet_min_odds, bet_max_odds, kelly_fraction, kelly_max_bankroll_pct, "
            "pool_impact_max_pct, circuit_daily_loss_pct, circuit_weekly_loss_pct, "
            "features_enabled_json "
            "FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        if not srow:
            raise HTTPException(404, f"strategy {strategy_id} not found")
        strat_cols = ("id","name","name_zh","name_en","description","enabled",
                      "stage2_enabled","stage2_alpha","stage2_beta","calibration",
                      "edge_threshold","min_prob","bet_min_odds","bet_max_odds",
                      "kelly_fraction","kelly_max_bankroll_pct","pool_impact_max_pct",
                      "circuit_daily_loss_pct","circuit_weekly_loss_pct",
                      "features_enabled_json")
        strategy = dict(zip(strat_cols, srow))

        # Prediction coverage (date range + race-day count)
        cov = conn.execute(
            "SELECT MIN(substr(snapshot_basis, 1, 10)) AS d0, "
            "       MAX(substr(snapshot_basis, 1, 10)) AS d1, "
            "       COUNT(DISTINCT substr(snapshot_basis, 1, 10)) AS race_days, "
            "       COUNT(*) AS n_predictions, "
            "       COUNT(DISTINCT race_id) AS n_races "
            "FROM predictions WHERE strategy_id = ?",
            (strategy_id,),
        ).fetchone()
        coverage = {
            "from": cov[0], "to": cov[1],
            "race_days": cov[2] or 0,
            "predictions": cov[3] or 0,
            "races": cov[4] or 0,
        }

        # Calibration trend (all windows)
        cm = conn.execute(
            "SELECT window_start, window_end, brier, log_loss, ece, sample_count "
            "FROM calibration_metrics WHERE strategy_id = ? ORDER BY window_end",
            (strategy_id,),
        ).fetchall()
        cm_cols = ("window_start","window_end","brier","log_loss","ece","sample_count")
        calibration_trend = [dict(zip(cm_cols, r)) for r in cm]

        # Counterfactual audit over the full coverage window
        audit_summary: dict = {}
        if coverage["from"] and coverage["to"]:
            from betting import audit as audit_mod, filters as filt
            settings = filt.FilterSettings(
                bet_max_odds=float(strategy["bet_max_odds"] or 20.0),
                edge_threshold=float(strategy["edge_threshold"] or 1.05),
                min_prob=float(strategy["min_prob"] or 0.05),
                bet_min_odds=float(strategy["bet_min_odds"] or 2.5),
            )
            audit_summary = audit_mod.audit(
                conn, strategy_id, coverage["from"], coverage["to"],
                settings=settings,
                kelly_fraction_strat=float(strategy["kelly_fraction"] or 0.25),
                bankroll=10000.0,
            )

        # Top 10 recent edges (highest-edge predictions in coverage window)
        top_edges_rows = conn.execute(
            "SELECT p.snapshot_basis, p.race_id, ra.course, ra.race_no, p.brand, "
            "       p.calibrated_prob, p.odds_at_prediction, p.edge, r.position "
            "FROM predictions p "
            "JOIN races ra ON ra.id = p.race_id "
            "LEFT JOIN results r ON r.race_id = p.race_id AND r.brand = p.brand "
            "WHERE p.strategy_id = ? AND p.edge IS NOT NULL "
            "ORDER BY p.edge DESC LIMIT 10",
            (strategy_id,),
        ).fetchall()
        edge_cols = ("snapshot_basis","race_id","course","race_no","brand",
                     "calibrated_prob","odds","edge","position")
        top_edges = [dict(zip(edge_cols, r)) for r in top_edges_rows]

        # ── Top-pick accuracy metrics. Group predictions by race, rank by
        #    calibrated_prob desc, find the winner's rank. Aggregate across
        #    races to compute top-1 / top-3 hit rate and winner log-loss.
        #    Tells you "how often is my model's #1 pick the actual winner"
        #    independent of betting filters / sizing.
        per_race_rows = conn.execute(
            """
            SELECT p.race_id, p.brand, p.calibrated_prob,
                   CASE WHEN r.position = '1' OR r.position = 1 THEN 1 ELSE 0 END AS won
            FROM predictions p
            LEFT JOIN results r ON r.race_id = p.race_id AND r.brand = p.brand
            WHERE p.strategy_id = ? AND p.calibrated_prob IS NOT NULL
            """,
            (strategy_id,),
        ).fetchall()
        by_race: dict[int, list[tuple[float, int]]] = {}
        for race_id, brand, prob, won in per_race_rows:
            by_race.setdefault(race_id, []).append((float(prob), int(won)))
        import math as _m
        races_with_winner = 0
        top1_hits = top3_hits = 0
        log_loss_sum = 0.0
        for race_id, items in by_race.items():
            if not any(w for _, w in items):
                continue   # race result missing or void
            races_with_winner += 1
            items.sort(key=lambda x: -x[0])
            for rank, (_p, won) in enumerate(items, start=1):
                if won:
                    if rank == 1: top1_hits += 1
                    if rank <= 3: top3_hits += 1
                    winner_prob = max(min(items[rank - 1][0], 1 - 1e-9), 1e-9)
                    log_loss_sum += -_m.log(winner_prob)
                    break
        accuracy = {
            "races_with_winner": races_with_winner,
            "top1": top1_hits / races_with_winner if races_with_winner else None,
            "top3": top3_hits / races_with_winner if races_with_winner else None,
            "winner_log_loss": log_loss_sum / races_with_winner if races_with_winner else None,
            # Baseline = uniform 1/avg-field-size to gauge improvement over random
            "random_baseline_top1": 1.0 / (sum(len(v) for v in by_race.values()) / max(len(by_race), 1)),
        }
    finally:
        conn.close()

    return {
        "strategy": strategy,
        "coverage": coverage,
        "calibration_trend": calibration_trend,
        "audit_summary": audit_summary,
        "top_edges": top_edges,
        "accuracy": accuracy,
    }


# ─── Strategy performance charts (cumulative PnL, reliability, odds buckets) ──

@router.get("/strategies/{strategy_id}/charts")
def strategy_charts(strategy_id: int,
                    date_from: str | None = None,
                    date_to: str | None = None) -> dict:
    """Three quant-racing performance charts the SPA renders as inline SVG:

      cumulative_pnl   — running total (stake/payout/pnl) per race day. Tells
                          you 'is this model making money?'.
      reliability      — predicted-prob deciles vs realised win-rate, the
                          textbook calibration plot per whitepaper §5.4. Points
                          on the diagonal = perfectly calibrated.
      odds_buckets     — win-rate + ROI per (≈) odds band among placed bets.
                          Tells you where on the price ladder the model works.

    Bets are filtered using the strategy's own thresholds (bet_max_odds /
    bet_min_odds / min_prob / edge_threshold), mirroring betting.filters.evaluate.
    Stakes are sized by fractional Kelly capped at bankroll-pct, mirroring
    betting.sizing.size_bet. Bankroll defaults to HK$10,000 (matches sim_mode).
    """
    if not DB_PATH.exists():
        raise HTTPException(404, "DB not initialized")
    conn = _connect()
    try:
        strat = conn.execute(
            "SELECT bet_max_odds, bet_min_odds, min_prob, edge_threshold, "
            "       kelly_fraction, kelly_max_bankroll_pct "
            "FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        if not strat:
            raise HTTPException(404, f"strategy {strategy_id} not found")
        bet_max_odds, bet_min_odds, min_prob, edge_thr, kelly_frac, kelly_max_bank = strat
        # Use explicit None checks — `value or default` evaluates `0` as
        # falsy, which would silently override an intentional kelly_fraction=0
        # (flat staking) with the 0.25 default.
        bet_max_odds   = float(20.0   if bet_max_odds  is None else bet_max_odds)
        bet_min_odds   = float(2.5    if bet_min_odds  is None else bet_min_odds)
        min_prob       = float(0.05   if min_prob      is None else min_prob)
        edge_thr       = float(1.05   if edge_thr      is None else edge_thr)
        kelly_frac     = float(0.25   if kelly_frac    is None else kelly_frac)
        kelly_max_bank = float(0.05   if kelly_max_bank is None else kelly_max_bank)
        bankroll = 10000.0

        where = "WHERE p.strategy_id = ?"
        params: list = [strategy_id]
        if date_from:
            where += " AND substr(p.snapshot_basis, 1, 10) >= ?"
            params.append(date_from)
        if date_to:
            where += " AND substr(p.snapshot_basis, 1, 10) <= ?"
            params.append(date_to)
        rows = conn.execute(
            f"""
            SELECT substr(p.snapshot_basis, 1, 10) AS d,
                   p.calibrated_prob,
                   p.odds_at_prediction,
                   CASE WHEN r.position = '1' OR r.position = 1 THEN 1 ELSE 0 END AS won,
                   p.race_id,
                   ra.course,
                   ra.class,
                   ra.distance
            FROM predictions p
            LEFT JOIN results r ON r.race_id = p.race_id AND r.brand = p.brand
            JOIN races ra ON ra.id = p.race_id
            {where}
            ORDER BY p.snapshot_basis, p.race_id
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    # ── Reliability: bin every prediction's calibrated_prob into deciles and
    #    compare mean-predicted to empirical win-rate. Uses ALL predictions
    #    (not just placed bets) — calibration is a global property of the model.
    n_bins = 10
    bin_data: list[list[tuple[float, int]]] = [[] for _ in range(n_bins)]
    for row in rows:
        prob, won = row[1], row[3]
        if prob is None:
            continue
        b = min(int(prob * n_bins), n_bins - 1)
        bin_data[b].append((float(prob), int(won)))
    reliability = []
    for i, b in enumerate(bin_data):
        if not b:
            continue
        reliability.append({
            "bin_lo": i / n_bins,
            "bin_hi": (i + 1) / n_bins,
            "predicted_mean": sum(p for p, _ in b) / len(b),
            "observed_rate": sum(w for _, w in b) / len(b),
            "n_samples": len(b),
        })

    # ── Pass 1: build the "qualified" list — every bet that passed the
    #    strategy's filters AND has a positive Kelly. Stake sizing happens
    #    in pass 2; this lets us reuse the same qualifying set to compute
    #    Kelly what-ifs (flat / 1/4 / 1/2 / 3/4 / full).
    qualified: list[dict] = []
    for d, prob, odds, won, race_id, course, race_class, distance in rows:
        if prob is None or odds is None:
            continue
        if not (bet_min_odds <= odds <= bet_max_odds):
            continue
        if prob < min_prob:
            continue
        edge = prob * odds
        if edge < edge_thr:
            continue
        kelly_full = ((prob * odds - 1) / (odds - 1)) if odds > 1 else 0.0
        if kelly_full <= 0:
            continue
        qualified.append({
            "date": d, "prob": float(prob), "odds": float(odds),
            "won": int(won), "kelly_full": float(kelly_full),
            "race_id": race_id, "course": course or "",
            "class": (race_class or "").strip(),
            "distance": int(distance) if distance else 0,
            "edge": float(edge),
        })

    # Pass 2: production sizing — fractional Kelly capped by bankroll-pct.
    placed: list[dict] = []
    for q in qualified:
        stake = (kelly_max_bank * bankroll if kelly_frac == 0
                 else min(q["kelly_full"] * kelly_frac * bankroll, kelly_max_bank * bankroll))
        if stake <= 0:
            continue
        placed.append({
            "date": q["date"], "prob": q["prob"], "odds": q["odds"],
            "won": q["won"], "stake": stake,
            "payout": stake * q["odds"] if q["won"] else 0.0,
        })

    # ── Cumulative PnL per day (sorted chronologically). Also track wins so the
    #    daily-races stacked-bar chart can show wins / losses / no-bet split.
    daily: dict[str, dict] = {}
    for p in placed:
        d = daily.setdefault(p["date"], {"stake": 0.0, "payout": 0.0, "n": 0, "wins": 0})
        d["stake"] += p["stake"]
        d["payout"] += p["payout"]
        d["n"] += 1
        d["wins"] += p["won"]

    # Total race count per meeting date (independent of whether we bet)
    race_count_where = "WHERE 1=1"
    rc_params: list = []
    if date_from:
        race_count_where += " AND date >= ?"; rc_params.append(date_from)
    if date_to:
        race_count_where += " AND date <= ?"; rc_params.append(date_to)
    conn2 = _connect()
    try:
        rc_rows = conn2.execute(
            f"SELECT date, COUNT(*) FROM races {race_count_where} GROUP BY date",
            rc_params,
        ).fetchall()
    finally:
        conn2.close()
    race_counts = {d: n for d, n in rc_rows}

    cum_pnl = []
    s = pay = 0.0; n = 0
    for date in sorted(daily):
        d = daily[date]
        s += d["stake"]; pay += d["payout"]; n += d["n"]
        cum_pnl.append({
            "date": date,
            "stake_cum": round(s, 2),
            "payout_cum": round(pay, 2),
            "pnl_cum": round(pay - s, 2),
            "n_bets_cum": n,
            "daily_stake": round(d["stake"], 2),
            "daily_pnl": round(d["payout"] - d["stake"], 2),
            "daily_bets": d["n"],
            "daily_wins": d["wins"],
            "total_races": race_counts.get(date, 0),
        })

    # ── Win rate + ROI per odds bucket (placed bets only)
    buckets_def = [(2.5, 4), (4, 6), (6, 8), (8, 12), (12, 16), (16, 20)]
    odds_buckets = []
    for lo, hi in buckets_def:
        sel = [p for p in placed if lo <= p["odds"] < hi]
        if not sel:
            continue
        wins = sum(1 for p in sel if p["won"])
        stake = sum(p["stake"] for p in sel)
        payout = sum(p["payout"] for p in sel)
        odds_buckets.append({
            "lo": lo, "hi": hi,
            "n_bets": len(sel),
            "n_wins": wins,
            "win_rate": wins / len(sel),
            "expected_win_rate": sum(p["prob"] for p in sel) / len(sel),
            "stake": round(stake, 2),
            "payout": round(payout, 2),
            "roi": (payout - stake) / stake if stake else 0.0,
        })

    # ── Kelly "what-if" scenarios on the same qualifying set. Flat = a fixed
    #    unit stake of (kelly_max_bankroll_pct × bankroll), i.e. always bet
    #    the position cap — same notional risk per bet, no edge weighting.
    flat_stake = kelly_max_bank * bankroll
    kelly_scenarios = []
    for label, k in (("flat", 0.0), ("1/4", 0.25), ("1/2", 0.5),
                     ("3/4", 0.75), ("full", 1.0)):
        s = pay = 0.0
        wins = 0
        n = 0
        for q in qualified:
            if k == 0:
                stake = flat_stake
            else:
                stake = min(q["kelly_full"] * k * bankroll, flat_stake)
            if stake <= 0:
                continue
            s += stake
            n += 1
            if q["won"]:
                pay += stake * q["odds"]
                wins += 1
        kelly_scenarios.append({
            "label": label,
            "fraction": k,
            "n_bets": n,
            "n_wins": wins,
            "stake": round(s, 2),
            "payout": round(pay, 2),
            "pnl": round(pay - s, 2),
            "roi": (pay - s) / s if s > 0 else 0.0,
            "is_current": (
                (k == 0 and kelly_frac == 0) or
                (k > 0 and abs(k - kelly_frac) < 1e-6)
            ),
        })

    # ── Threshold sweeps. For each knob we re-run the qualifying logic
    #    relative to the original `rows` list (NOT the filtered `qualified`,
    #    because the knob being swept needs to be the one varying). All
    #    other thresholds stay at the strategy's current values.
    def _sweep(values: list[float], knob: str, current_value: float) -> list[dict]:
        out = []
        for v in values:
            n = wins = 0
            stake = pay = 0.0
            for d, prob, odds, won, _rid, _co, _cl, _di in rows:
                if prob is None or odds is None:
                    continue
                _max  = v if knob == "max_odds" else bet_max_odds
                _min  = v if knob == "min_odds" else bet_min_odds
                _mp   = v if knob == "min_prob" else min_prob
                _ethr = v if knob == "edge" else edge_thr
                if not (_min <= odds <= _max): continue
                if prob < _mp: continue
                e = prob * odds
                if e < _ethr: continue
                kf = ((prob*odds - 1) / (odds - 1)) if odds > 1 else 0.0
                if kf <= 0: continue
                s = (kelly_max_bank * bankroll if kelly_frac == 0
                     else min(kf * kelly_frac * bankroll, kelly_max_bank * bankroll))
                if s <= 0: continue
                stake += s
                n += 1
                if won:
                    pay += s * odds
                    wins += 1
            out.append({
                "value": v, "n_bets": n, "n_wins": wins,
                "stake": round(stake, 2), "payout": round(pay, 2),
                "pnl": round(pay - stake, 2),
                "roi": (pay - stake) / stake if stake > 0 else 0.0,
                "is_current": abs(v - current_value) < 1e-9,
            })
        return out

    threshold_sweeps = {
        "max_odds":  _sweep([10, 12, 15, 20, 25, 30, 40], "max_odds",  bet_max_odds),
        "min_odds":  _sweep([1.5, 2.0, 2.5, 3.0, 4.0],     "min_odds",  bet_min_odds),
        "min_prob":  _sweep([0.02, 0.05, 0.08, 0.10, 0.15], "min_prob", min_prob),
        "edge":      _sweep([1.00, 1.03, 1.05, 1.10, 1.20], "edge",     edge_thr),
    }

    # ── Segment analysis: stratify placed bets by track / class / distance.
    def _seg_agg(items: list[dict]) -> dict:
        n = len(items)
        wins = sum(1 for p in items if p["won"])
        stake = sum(p["stake"] for p in items)
        pay = sum(p["payout"] for p in items)
        return {
            "n_bets": n, "n_wins": wins,
            "stake": round(stake, 2), "payout": round(pay, 2),
            "pnl": round(pay - stake, 2),
            "win_rate": wins / n if n else 0.0,
            "roi": (pay - stake) / stake if stake > 0 else 0.0,
        }

    def _class_bucket(cls: str) -> str:
        c = (cls or "").upper().strip()
        if c.startswith("G") or "LISTED" in c: return "G/L"
        if c in ("1","C1","2","C2"): return "C1-2"
        if c in ("3","C3","4","C4","5","C5"): return "C3-5"
        return "other"

    def _distance_bucket(d: int) -> str:
        if d <= 1200: return "≤1200m"
        if d <= 1800: return "1400-1800m"
        return "≥2000m"

    by_track    = {"ST": [], "HV": []}
    by_class    = {"G/L": [], "C1-2": [], "C3-5": [], "other": []}
    by_distance = {"≤1200m": [], "1400-1800m": [], "≥2000m": []}
    for p in placed:
        # Walk back to the qualified row that produced this `placed` entry so
        # we can read race_id/course/class/distance (placed itself was trimmed).
        pass
    # Simpler: re-derive placed-with-context directly from qualified using the
    # same sizing logic (one more pass — qualified is small).
    placed_ctx: list[dict] = []
    for q in qualified:
        stake_q = min(q["kelly_full"] * kelly_frac * bankroll, kelly_max_bank * bankroll)
        if stake_q <= 0: continue
        placed_ctx.append({
            **q, "stake": stake_q,
            "payout": stake_q * q["odds"] if q["won"] else 0.0,
        })
    for p in placed_ctx:
        if p["course"] in by_track:
            by_track[p["course"]].append(p)
        by_class[_class_bucket(p["class"])].append(p)
        by_distance[_distance_bucket(p["distance"])].append(p)

    segments = {
        "by_track":    [{"label": k, **_seg_agg(v)} for k, v in by_track.items()    if v],
        "by_class":    [{"label": k, **_seg_agg(v)} for k, v in by_class.items()    if v],
        "by_distance": [{"label": k, **_seg_agg(v)} for k, v in by_distance.items() if v],
    }

    # ── Top-N per race: only bet the highest-edge horse(s) per race.
    by_race: dict[int, list[dict]] = {}
    for q in qualified:
        by_race.setdefault(q["race_id"], []).append(q)
    top_n_scenarios = []
    for n_top in (1, 2, 3, 99):   # 99 = "all qualifying"
        items = []
        for rid, ranked in by_race.items():
            ranked_sorted = sorted(ranked, key=lambda x: -x["edge"])
            for q in ranked_sorted[:n_top]:
                stake_q = min(q["kelly_full"] * kelly_frac * bankroll,
                              kelly_max_bank * bankroll)
                if stake_q <= 0: continue
                items.append({**q, "stake": stake_q,
                              "payout": stake_q * q["odds"] if q["won"] else 0.0})
        agg = _seg_agg(items)
        top_n_scenarios.append({
            "label": "all" if n_top == 99 else f"top {n_top}",
            "n_top": n_top,
            **agg,
        })

    # ── Bankroll growth (compounding) + running peak-to-trough drawdown.
    #    Replays placed_ctx in chronological order, re-betting accumulated
    #    bankroll. Stakes still capped by kelly_max_bankroll_pct of current bank.
    bank_path = []
    bank = bankroll
    peak = bank
    placed_ctx_sorted = sorted(placed_ctx, key=lambda x: x["date"])
    # Group by date so we report end-of-day bankroll
    by_date_p: dict[str, list[dict]] = {}
    for p in placed_ctx_sorted:
        by_date_p.setdefault(p["date"], []).append(p)
    for date in sorted(by_date_p):
        for p in by_date_p[date]:
            # Recompute stake from compounded bank, not the static one
            stake_c = min(p["kelly_full"] * kelly_frac * bank,
                          kelly_max_bank * bank)
            if stake_c <= 0: continue
            if p["won"]:
                bank += stake_c * (p["odds"] - 1)
            else:
                bank -= stake_c
        peak = max(peak, bank)
        drawdown = (peak - bank) / peak if peak > 0 else 0.0
        bank_path.append({
            "date": date,
            "bankroll": round(bank, 2),
            "drawdown": round(drawdown * 100, 2),  # %
            "peak": round(peak, 2),
        })

    return {
        "strategy_id": strategy_id,
        "from": date_from, "to": date_to,
        "cumulative_pnl": cum_pnl,
        "reliability": reliability,
        "odds_buckets": odds_buckets,
        "kelly_scenarios": kelly_scenarios,
        "threshold_sweeps": threshold_sweeps,
        "segments": segments,
        "top_n_scenarios": top_n_scenarios,
        "bankroll_path": bank_path,
        "totals": {
            "n_bets": len(placed),
            "n_wins": sum(1 for p in placed if p["won"]),
            "stake": round(sum(p["stake"] for p in placed), 2),
            "payout": round(sum(p["payout"] for p in placed), 2),
        },
        "thresholds": {
            "bet_max_odds": bet_max_odds, "bet_min_odds": bet_min_odds,
            "min_prob": min_prob, "edge_threshold": edge_thr,
            "kelly_fraction": kelly_frac, "bankroll": bankroll,
        },
    }


# ─── Race viewer: per-day race cards with merged predictions + results ────────

@router.get("/dates")
def list_predicted_dates(strategy_id: int | None = None) -> dict:
    """Calendar of every meeting date in the DB, with a flag for whether the
    requested strategy has predictions for that date. Used by the Races tab's
    date picker so the user can navigate to past meetings."""
    if not DB_PATH.exists():
        raise HTTPException(404, "DB not initialized")
    conn = _connect()
    try:
        if strategy_id is not None:
            rows = conn.execute(
                """
                SELECT ra.date,
                       COUNT(DISTINCT ra.id) AS races,
                       COUNT(DISTINCT r.id)  AS results,
                       (SELECT COUNT(DISTINCT p.race_id) FROM predictions p
                          JOIN races ra2 ON ra2.id = p.race_id
                          WHERE p.strategy_id = ? AND ra2.date = ra.date) AS predicted_races
                FROM races ra
                LEFT JOIN results r ON r.race_id = ra.id
                GROUP BY ra.date
                ORDER BY ra.date DESC
                """,
                (strategy_id,),
            ).fetchall()
            cols = ("date","races","results","predicted_races")
        else:
            rows = conn.execute(
                "SELECT ra.date, COUNT(DISTINCT ra.id) AS races, "
                "       COUNT(DISTINCT r.id) AS results "
                "FROM races ra LEFT JOIN results r ON r.race_id = ra.id "
                "GROUP BY ra.date ORDER BY ra.date DESC"
            ).fetchall()
            cols = ("date","races","results")
    finally:
        conn.close()
    return {"dates": [dict(zip(cols, r)) for r in rows]}


def _compute_feature_drivers(conn, race_id: int, horse_brands: list[str],
                             max_features: int = 3) -> dict:
    """For each horse in the race, identify the features that pushed its
    score up vs the field (top) and down (bottom).

    Method: compute z-score per feature within this race's field — z = (this
    horse's value − field mean) / field stdev. Features with the largest
    positive z are 'tailwinds', largest negative z are 'headwinds'. Pure-
    field-variance comparison is interpretable to a layman ("rating +12 vs
    field, jockey win rate 22% vs 8% field avg") and doesn't need SHAP /
    gain attribution which would require the trained booster at request time.

    Returns: {brand: {"top": [..3 drivers..], "bottom": [..3 drivers..]}}
    Each driver: {feature_id, name_zh, name_en, value, field_mean, z}.
    """
    rows = conn.execute(
        "SELECT brand, feature_id, value FROM feature_values "
        "WHERE race_id = ? AND value IS NOT NULL",
        (race_id,),
    ).fetchall()
    if not rows:
        return {b: {"top": [], "bottom": []} for b in horse_brands}

    descriptions = _load_features()  # cached after first call

    # Group values per feature
    by_feat: dict[str, list[tuple[str, float]]] = {}
    for brand, fid, val in rows:
        by_feat.setdefault(fid, []).append((brand, val))

    # Compute z-score per (feature, horse), skip features with no variance
    # across the field (uninformative for ranking).
    horse_z: dict[str, list[tuple[str, float, float, float]]] = {b: [] for b in horse_brands}
    for fid, items in by_feat.items():
        if len(items) < 2:
            continue
        vals = [v for _, v in items]
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        sd = var ** 0.5
        if sd < 1e-9:
            continue
        for brand, v in items:
            if brand in horse_z:
                horse_z[brand].append((fid, v, mean, (v - mean) / sd))

    def _enrich(item):
        fid, val, mean, z = item
        desc = descriptions.get(fid, {})
        return {
            "feature_id": fid,
            "name_zh": desc.get("name_zh", fid),
            "name_en": desc.get("name_en", fid),
            "description_zh": desc.get("description_zh", ""),
            "description_en": desc.get("description_en", ""),
            "value": round(val, 3),
            "field_mean": round(mean, 3),
            "z": round(z, 2),
        }

    out: dict[str, dict] = {}
    for brand, items in horse_z.items():
        if not items:
            out[brand] = {"top": [], "bottom": []}
            continue
        items.sort(key=lambda x: x[3])
        bottom = items[:max_features]                  # most negative z
        top = items[-max_features:][::-1]              # most positive z
        out[brand] = {
            "top": [_enrich(t) for t in top if t[3] > 0.5],     # only meaningful tailwinds
            "bottom": [_enrich(b) for b in bottom if b[3] < -0.5],  # only meaningful headwinds
        }
    return out


@router.get("/races/{date}")
def get_races_for_date(date: str, strategy_id: int | None = None) -> dict:
    """Race cards for `date`, merged with optional strategy predictions and
    actual results. Powers the SPA's Race Viewer tab. Horses are sorted by
    calibrated_prob (predictions present) or by position (results present)
    or by horse_no as a last resort.
    """
    if not DB_PATH.exists():
        raise HTTPException(404, "DB not initialized")
    conn = _connect()
    try:
        races = conn.execute(
            "SELECT id, course, race_no, distance, class, going, participants, "
            "       prize, race_name, post_time "
            "FROM races WHERE date = ? ORDER BY course, race_no",
            (date,),
        ).fetchall()
        if not races:
            return {"date": date, "races": []}
        race_cols = ("id","course","race_no","distance","class","going","participants",
                     "prize","race_name","post_time")
        out_races: list[dict] = []
        for r in races:
            race = dict(zip(race_cols, r))
            race_id = race["id"]
            # Horse rows joined with optional predictions and horse profile
            if strategy_id is not None:
                horse_rows = conn.execute(
                    """
                    SELECT r.brand, r.horse_name, r.jockey, r.trainer, r.draw,
                           r.act_wt, r.decl_wt, r.odds, r.finish_time, r.lbw,
                           r.running_style, r.position,
                           h.age, h.sex, h.rating,
                           p.fundamental_prob, p.market_implied_prob,
                           p.blended_prob, p.calibrated_prob, p.edge,
                           p.recommendation
                    FROM results r
                    LEFT JOIN horses h ON h.brand = r.brand
                    LEFT JOIN predictions p ON p.race_id = r.race_id
                                            AND p.brand = r.brand
                                            AND p.strategy_id = ?
                    WHERE r.race_id = ?
                    """,
                    (strategy_id, race_id),
                ).fetchall()
                cols = ("brand","horse_name","jockey","trainer","draw","act_wt",
                        "decl_wt","odds","finish_time","lbw","running_style","position",
                        "age","sex","rating",
                        "fundamental_prob","market_implied_prob","blended_prob",
                        "calibrated_prob","edge","recommendation")
            else:
                horse_rows = conn.execute(
                    """
                    SELECT r.brand, r.horse_name, r.jockey, r.trainer, r.draw,
                           r.act_wt, r.decl_wt, r.odds, r.finish_time, r.lbw,
                           r.running_style, r.position,
                           h.age, h.sex, h.rating
                    FROM results r
                    LEFT JOIN horses h ON h.brand = r.brand
                    WHERE r.race_id = ?
                    """,
                    (race_id,),
                ).fetchall()
                cols = ("brand","horse_name","jockey","trainer","draw","act_wt",
                        "decl_wt","odds","finish_time","lbw","running_style","position",
                        "age","sex","rating")
            horses = [dict(zip(cols, h)) for h in horse_rows]
            # Sort: by calibrated_prob desc when available, else by position asc,
            # else by horse_no (use draw as proxy).
            def _sort_key(h: dict) -> tuple:
                if h.get("calibrated_prob") is not None:
                    return (0, -float(h["calibrated_prob"]))
                pos = h.get("position")
                if pos is not None and str(pos).strip().isdigit():
                    return (1, int(pos))
                return (2, h.get("draw") or 99)
            horses.sort(key=_sort_key)
            # Attach per-horse feature drivers (z-scores vs the field) when
            # we have predictions for this race — explains in layman terms
            # which features pushed the score up / down.
            if strategy_id is not None and any(h.get("calibrated_prob") is not None for h in horses):
                drivers = _compute_feature_drivers(conn, race_id, [h["brand"] for h in horses])
                for h in horses:
                    if h.get("calibrated_prob") is not None:
                        h["feature_drivers"] = drivers.get(h["brand"], {"top": [], "bottom": []})
            race["horses"] = horses
            race["has_results"] = any(h.get("position") for h in horses)
            race["has_predictions"] = any(h.get("calibrated_prob") is not None for h in horses)
            out_races.append(race)
        # Dividends (if any) for the day
        div_rows = conn.execute(
            "SELECT course, race_no, pool, combination, dividend "
            "FROM dividends WHERE date = ? ORDER BY course, race_no, pool",
            (date,),
        ).fetchall()
        div_cols = ("course","race_no","pool","combination","dividend")
        dividends = [dict(zip(div_cols, r)) for r in div_rows]
    finally:
        conn.close()
    return {"date": date, "races": out_races, "dividends": dividends}


# ─── Live status (decision loop heartbeat + circuit breaker) ──────────────────

@router.get("/live/mode")
def live_mode_get() -> dict:
    """Report whether the global real-money toggle is on. Default: off (paper)."""
    from live import decision_loop as _dl
    return {"live_mode": _dl.is_live_mode(),
            "note": "paper mode by default; flip with POST /api/live/mode?enabled=true (P6 gate)"}


@router.post("/live/mode")
def live_mode_set(enabled: bool, confirm: str | None = None) -> dict:
    """Flip the real-money toggle. Requires `confirm=I_AM_SURE` for safety.

    Halts immediately if the global kill switch is engaged.
    """
    from live import decision_loop as _dl
    if enabled and confirm != "I_AM_SURE":
        raise HTTPException(400, "to enable live mode, pass confirm=I_AM_SURE")
    if enabled and DB_PATH.exists():
        conn = _connect()
        try:
            row = conn.execute("SELECT halted FROM kill_switch_state WHERE id = 1").fetchone()
            if row and row[0]:
                raise HTTPException(409, "kill switch is engaged; release it before enabling live mode")
        finally:
            conn.close()
    _dl.set_live_mode(bool(enabled))
    return {"live_mode": _dl.is_live_mode()}


@router.get("/live/status")
def live_status() -> dict:
    """Composite status: kill switch, decision loop heartbeat freshness, and
    per-strategy circuit breaker state for today."""
    from datetime import date as _date
    if not DB_PATH.exists():
        return {"initialized": False}
    conn = _connect()
    try:
        ks = kill_switch_state()
        today = _date.today().isoformat()
        cb_rows = conn.execute(
            "SELECT s.id, s.name, c.daily_pnl, c.weekly_pnl, c.halted, c.halt_reason "
            "FROM strategies s LEFT JOIN circuit_breaker_state c ON c.strategy_id = s.id AND c.date = ? "
            "WHERE s.enabled = 1",
            (today,),
        ).fetchall()
    finally:
        conn.close()
    return {
        "initialized": True,
        "kill_switch": ks,
        "today": today,
        "circuit_breakers": [
            {"strategy_id": r[0], "strategy": r[1], "daily_pnl": r[2],
             "weekly_pnl": r[3], "halted": bool(r[4]), "halt_reason": r[5]}
            for r in cb_rows
        ],
    }

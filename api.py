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
import json    # alias kept for the bet-strategies endpoints (added 2026-05-27)

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
    # 30s timeout because read endpoints can race ablation/backfill writers;
    # the default 5s was surfacing as "database is locked" 500s mid-request.
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000")
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
    "persons":               ("scrapers/scrape_persons.py",               ["--since", "2025-09-01"]),  # bilingual jockey/trainer registry keyed by HKJC IDs
    "multi_leg_dividends":   ("scrapers/scrape_multi_leg_dividends.py",   []),                          # DBL/TBL/DT/TT/SixUP — LIVE capture only (next-meeting window)
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

    # Schedulable integrity check (runs the full DB scan, optional auto-heal).
    def _integrity_action(args: dict) -> None:
        from monitoring.integrity_check import run as _ic_run
        scope = {"date": args.get("date")} if args.get("date") else {"scope": "full"}
        _ic_run(scope, heal=bool(args.get("heal", True)))
    action_registry["integrity_check"] = _integrity_action

    # Pre-race chain for upcoming meetings: scrape card -> clean stubs ->
    # features -> predictions. Trains a model (slow), so run it as a detached
    # subprocess to keep the scheduler loop responsive; it self-reports via
    # status.py so the dashboard shows progress.
    def _prepare_upcoming_action(args: dict) -> None:
        import subprocess as _sp
        sid = str(args.get("strategy_id", 1))
        _sp.Popen([sys.executable, "-m", "scripts.prepare_upcoming", sid], cwd=str(BASE_DIR))
    action_registry["prepare_upcoming"] = _prepare_upcoming_action


# ─── On-demand fire endpoints ────────────────────────────────────────────────

@router.get("/scrapers")
def list_scrapers() -> dict:
    """List scrapers and their default argv."""
    return {slug: {"script": s, "default_argv": d} for slug, (s, d) in SCRAPERS.items()}


@router.get("/scrapers/coverage")
def scrapers_coverage() -> dict:
    """Per-scraper bilingual description + record count + date range spanned.
    Powers the dashboard 'Data sources' panel."""
    from scrapers.catalog import coverage
    if not DB_PATH.exists():
        return {"sources": []}
    conn = _connect()
    try:
        return {"sources": coverage(conn)}
    finally:
        conn.close()


# ─── Data-integrity dashboard endpoint ───────────────────────────────────────
@router.get("/integrity")
def integrity_summary(limit: int = 10) -> dict:
    """Return the most recent integrity_check_runs + a roll-up of open
    violations by severity. Powers the SPA's data-integrity badge."""
    if not DB_PATH.exists():
        raise HTTPException(404, "DB not initialized")
    conn = _connect()
    try:
        # Latest run summary
        run = conn.execute(
            "SELECT id, ts, scope, total_checks, passed, failed FROM integrity_check_runs "
            "ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if not run:
            return {"latest": None, "by_severity": {}, "runs": []}
        run_id = run[0]
        # Violation breakdown for that run
        sev = conn.execute(
            "SELECT severity, COUNT(*) FROM integrity_check_violations "
            "WHERE run_id=? GROUP BY severity",
            (run_id,),
        ).fetchall()
        by_check = conn.execute(
            "SELECT check_name, severity, COUNT(*), SUM(auto_healed) FROM integrity_check_violations "
            "WHERE run_id=? GROUP BY check_name, severity ORDER BY 3 DESC",
            (run_id,),
        ).fetchall()
        runs = conn.execute(
            "SELECT id, ts, scope, total_checks, passed, failed FROM integrity_check_runs "
            "ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return {
            "latest": {"id": run[0], "ts": run[1], "scope": run[2],
                       "total_checks": run[3], "passed": run[4],
                       "failed": run[5]},
            "by_severity": {s: c for s, c in sev},
            "by_check": [{"check_name": c[0], "severity": c[1],
                          "count": c[2], "auto_healed": c[3]} for c in by_check],
            "runs": [{"id": r[0], "ts": r[1], "scope": r[2],
                      "total_checks": r[3], "passed": r[4],
                      "failed": r[5]} for r in runs],
        }
    finally:
        conn.close()


@router.post("/integrity/run")
def integrity_run(date: str | None = None, heal: bool = False) -> dict:
    """Trigger an integrity check on-demand. Returns the new run summary."""
    if not DB_PATH.exists():
        raise HTTPException(404, "DB not initialized")
    from monitoring.integrity_check import run as _ic_run
    scope = {"date": date} if date else {"scope": "full"}
    return _ic_run(scope, heal=heal)


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


_NOTES_CACHE = None
_IMPORTANCE_CACHE = None


def _load_notes() -> dict:
    global _NOTES_CACHE
    if _NOTES_CACHE is None:
        p = BASE_DIR / "features" / "notes_rich.json"
        _NOTES_CACHE = _json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return _NOTES_CACHE


def _load_importance() -> dict:
    # Re-read each call (cheap file) so a refresh shows up without a restart.
    p = BASE_DIR / "data" / "feature_importance.json"
    return _json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


_CAT_LABELS = {
    1: ("馬匹檔案", "Horse profile"), 2: ("勝率與報酬指標", "Win-rate & returns"),
    3: ("適應性", "Adaptability"), 4: ("練馬師狀態", "Trainer form"), 5: ("閘號", "Draw"),
    6: ("負磅", "Weight"), 7: ("賽事背景", "Race context"), 8: ("近期狀態", "Recent form"),
    9: ("裝備與獸醫", "Gear & vet"), 10: ("步速與跑法", "Pace & style"),
    11: ("綜合速度與班次指標", "Composite speed/class"), 12: ("交互特徵", "Interactions"),
    13: ("連贏與序位結構", "Exotic & order"), 14: ("市場訊號", "Market signals"),
    15: ("場地動態", "Track dynamics"), 16: ("生物力學/外部數據", "Biomechanics"),
    17: ("事件歷史", "Incident history"),
}


@router.get("/strategies/{strategy_id}/feature_cards")
def strategy_feature_cards(strategy_id: int) -> dict:
    """One rich, importance-ranked card per feature for the strategy page:
    bilingual name + description + technical/layman notes, the compute logic and
    parameters, source citations, enabled state, XGBoost gain importance, and
    feature-value statistics. Sorted most → least important."""
    from features.catalog import FEATURES
    feats = _load_features()
    notes = _load_notes()
    biblio = _load_bibliography()
    imp = _load_importance()
    imp_feats = imp.get("features", {})
    max_gain = imp.get("max_gain") or 1.0

    conn = _connect()
    try:
        row = conn.execute("SELECT name, features_enabled_json FROM strategies WHERE id=?", (strategy_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, f"strategy {strategy_id} not found")
    strat_name, fe_json = row
    overrides = _json.loads(fe_json) if fe_json else {}

    cards = []
    for f in FEATURES:
        d = feats.get(f.id, {})
        n = notes.get(f.id, {})
        m = imp_feats.get(f.id, {})
        zh_cat, en_cat = _CAT_LABELS.get(f.category, (f"類{f.category}", f"Cat {f.category}"))
        gain = float(m.get("gain", 0) or 0)
        cards.append({
            "id": f.id, "category": f.category, "category_zh": zh_cat, "category_en": en_cat,
            "name_zh": d.get("name_zh") or f.name_zh, "name_en": d.get("name_en") or f.name_en,
            "description_zh": d.get("description_zh") or f.definition,
            "description_en": d.get("description_en") or f.definition,
            "notes_zh": n.get("notes_zh"), "notes_en": n.get("notes_en"),
            # parameters / logic
            "compute_fn": f.compute_fn_name,
            "depends_on": f.depends_on or None,
            "nan_permitted": bool(f.nan_permitted),
            "is_stub": f.compute_fn_name == "_nan_stub",
            "sources": [biblio[b] for b in (d.get("all_sources") or f.source_refs.split(",")) if b in biblio],
            # state
            "enabled": bool(overrides.get(f.id, f.enabled_default)),
            # importance
            "gain": round(gain, 1),
            "gain_pct": round(gain / max_gain, 4) if max_gain else 0,
            "splits": int(m.get("splits", 0) or 0),
            # statistics
            "stats": {k: m.get(k) for k in ("n", "coverage", "mean", "min", "max") if k in m},
        })
    # Most important first; then enabled; then id.
    cards.sort(key=lambda c: (-c["gain"], not c["enabled"], c["id"]))
    for i, c in enumerate(cards, 1):
        c["rank"] = i
    return {
        "strategy": strat_name,
        "computed_at": imp.get("computed_at"),
        "n_train_rows": imp.get("n_train_rows"),
        "n_used": imp.get("n_features_used"),
        "total": len(cards),
        "cards": cards,
    }


@router.get("/upcoming")
def upcoming_race(strategy_id: int = 1) -> dict:
    """Readiness of the pipeline for the next upcoming meeting: race card
    (entries, stage 1) → features → predictions → odds (stage 2) → bet
    decisions. Powers the dashboard's 'Upcoming Race' pane."""
    from datetime import date as _date
    conn = _connect()
    try:
        today = _date.today().isoformat()
        row = conn.execute(
            "SELECT date, course FROM races WHERE date >= ? "
            "GROUP BY date, course ORDER BY date, course LIMIT 1", (today,)).fetchone()
        if not row:
            return {"has_meeting": False}
        d, course = row
        rids = [r[0] for r in conn.execute(
            "SELECT id FROM races WHERE date=? AND course=?", (d, course))]
        ph = ",".join("?" * len(rids))

        def one(sql):
            return conn.execute(sql, rids).fetchone()[0] or 0
        n_races = len(rids)
        n_runners = one(f"SELECT COUNT(*) FROM results WHERE race_id IN ({ph})")
        n_feat = one(f"SELECT COUNT(DISTINCT race_id || '|' || brand) FROM feature_values WHERE race_id IN ({ph})")
        n_pred = one(f"SELECT COUNT(*) FROM predictions WHERE strategy_id={int(strategy_id)} AND race_id IN ({ph})")
        n_odds = one(f"SELECT COUNT(*) FROM odds_snapshots WHERE race_id IN ({ph})")
        n_bets = one(f"SELECT COUNT(*) FROM bet_ledger WHERE race_id IN ({ph})")
        post = conn.execute(f"SELECT MIN(post_time) FROM races WHERE id IN ({ph})", rids).fetchone()[0]
        stage = 2 if n_odds > 0 else 1

        def step(key, zh, en, done, total, *, gate_stage2=False):
            if gate_stage2 and stage == 1:
                status = "waiting"           # legitimately not due yet (pre stage-2)
            elif total and done >= total:
                status = "done"
            elif done > 0:
                status = "partial"
            else:
                status = "pending"
            return {"key": key, "zh": zh, "en": en, "done": int(done),
                    "total": int(total), "status": status}

        steps = [
            step("card", "賽馬卡（出馬表）", "Race card (entries)", n_runners, n_runners or 1),
            step("features", "特徵計算", "Features", n_feat, n_runners or 1),
            step("predictions", "模型預測", "Predictions", n_pred, n_runners or 1),
            step("odds", "賠率（第二階段）", "Odds (stage 2)", n_odds, n_runners or 1, gate_stage2=True),
            step("bets", "下注決策", "Bet decisions", n_bets, n_races or 1, gate_stage2=True),
        ]
        # "Ready for race day" once entries + features + predictions are complete.
        core_ready = all(s["status"] == "done" for s in steps[:3])
        return {
            "has_meeting": True, "date": d, "course": course,
            "n_races": n_races, "n_runners": n_runners, "post_time": post,
            "stage": stage, "core_ready": core_ready, "steps": steps,
        }
    finally:
        conn.close()


@router.get("/race_news/{date}")
def race_news(date: str, course: str | None = None) -> dict:
    """AI news/preview overlay for a meeting (advisory only; never a model
    feature). Populated by scripts.fetch_race_news for upcoming meetings."""
    if not DB_PATH.exists():
        return {"has_news": False}
    conn = _connect()
    try:
        if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='race_news'").fetchone():
            return {"has_news": False}
        if course:
            row = conn.execute("SELECT date,course,summary_en,summary_zh,tipped_json,sources_json,fetched_at "
                               "FROM race_news WHERE date=? AND course=?", (date, course)).fetchone()
        else:
            row = conn.execute("SELECT date,course,summary_en,summary_zh,tipped_json,sources_json,fetched_at "
                               "FROM race_news WHERE date=? ORDER BY course LIMIT 1", (date,)).fetchone()
        if not row:
            return {"has_news": False}
        return {
            "has_news": True, "date": row[0], "course": row[1],
            "summary_en": row[2], "summary_zh": row[3],
            "tipped": _json.loads(row[4] or "[]"),
            "sources": _json.loads(row[5] or "[]"),
            "fetched_at": row[6],
        }
    finally:
        conn.close()


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


# ─── Bet strategies (post-prediction rules on top of a model) ─────────────────
# Each bet_strategy reads predictions from a model strategy and applies a
# rule_kind ('flat_top1', 'kelly_top1', 'dutch_topN', etc.) to produce one
# or more bets per race (written to bet_ledger). Multiple bet strategies can
# share one model — they're cheap layers over an expensive walk-forward.

@router.get("/strategies/{model_strategy_id}/bet_strategies")
def list_bet_strategies(model_strategy_id: int) -> list[dict]:
    """All bet strategies layered on top of this model, plus their headline
    aggregate metrics (n_bets / n_wins / ROI) computed from bet_ledger."""
    if not DB_PATH.exists():
        return []
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT bs.id, bs.name, bs.name_en, bs.name_zh, bs.rule_kind,
                   bs.params_json, bs.enabled, bs.chart_color, bs.notes,
                   bs.created_at,
                   COALESCE(SUM(bl.stake), 0)         AS stake,
                   COALESCE(SUM(bl.payout), 0)        AS payout,
                   COALESCE(SUM(bl.pnl), 0)           AS pnl,
                   COUNT(bl.id)                       AS n_bets,
                   COALESCE(SUM(bl.won = 1), 0)       AS n_wins,
                   COUNT(DISTINCT bl.race_date)       AS race_days,
                   MIN(bl.race_date)                  AS first_date,
                   MAX(bl.race_date)                  AS last_date
            FROM bet_strategies bs
            LEFT JOIN bet_ledger bl ON bl.bet_strategy_id = bs.id
            WHERE bs.model_strategy_id = ?
            GROUP BY bs.id
            ORDER BY bs.id
            """,
            (model_strategy_id,),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = {
            "id": r[0], "name": r[1], "name_en": r[2], "name_zh": r[3],
            "rule_kind": r[4],
            "params": json.loads(r[5] or "{}"),
            "enabled": bool(r[6]),
            "chart_color": r[7],
            "notes": r[8],
            "created_at": r[9],
            "stake": round(r[10], 2),
            "payout": round(r[11], 2),
            "pnl": round(r[12], 2),
            "n_bets": r[13],
            "n_wins": r[14],
            "race_days": r[15],
            "first_date": r[16],
            "last_date": r[17],
            "roi_pct": round(100.0 * r[12] / r[10], 2) if r[10] > 0 else 0.0,
            "strike_rate_pct": round(100.0 * r[14] / r[13], 2) if r[13] > 0 else 0.0,
        }
        out.append(d)
    return out


class BetStrategyIn(dict):
    """Loose JSON body — FastAPI accepts dict directly."""
    pass


@router.post("/strategies/{model_strategy_id}/bet_strategies")
def create_bet_strategy(model_strategy_id: int, body: dict) -> dict:
    """Body: { name, name_en?, name_zh?, rule_kind, params?, chart_color? }"""
    name = body.get("name")
    rule_kind = body.get("rule_kind")
    if not name or not rule_kind:
        raise HTTPException(400, "name and rule_kind required")
    from betting.bet_runner import RULES
    if rule_kind not in RULES:
        raise HTTPException(400, f"unknown rule_kind: {rule_kind}; valid: {list(RULES)}")
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO bet_strategies
              (name, name_en, name_zh, model_strategy_id, rule_kind,
               params_json, chart_color, notes, enabled)
            VALUES (?,?,?,?,?,?,?,?,1)
            """,
            (name, body.get("name_en") or name, body.get("name_zh") or name,
             model_strategy_id, rule_kind,
             json.dumps(body.get("params") or {}),
             body.get("chart_color") or "#888888",
             body.get("notes")),
        )
        conn.commit()
        bid = conn.execute("SELECT id FROM bet_strategies WHERE name = ?", (name,)).fetchone()[0]
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, f"bet strategy already exists: {exc}")
    finally:
        conn.close()
    return {"id": bid, "name": name}


@router.delete("/bet_strategies/{bet_strategy_id}")
def delete_bet_strategy(bet_strategy_id: int) -> dict:
    conn = _connect()
    try:
        conn.execute("DELETE FROM bet_ledger WHERE bet_strategy_id = ?", (bet_strategy_id,))
        n = conn.execute("DELETE FROM bet_strategies WHERE id = ?", (bet_strategy_id,)).rowcount
        conn.commit()
    finally:
        conn.close()
    if not n:
        raise HTTPException(404, f"bet strategy {bet_strategy_id} not found")
    return {"deleted": bet_strategy_id}


@router.patch("/bet_strategies/{bet_strategy_id}")
def update_bet_strategy(bet_strategy_id: int, body: dict) -> dict:
    """Update enabled / name / chart_color / params / notes."""
    allowed = {"enabled", "name_en", "name_zh", "chart_color", "notes", "params"}
    sets = []; vals: list = []
    for k, v in body.items():
        if k not in allowed:
            continue
        if k == "params":
            sets.append("params_json = ?"); vals.append(json.dumps(v))
        elif k == "enabled":
            sets.append("enabled = ?"); vals.append(1 if v else 0)
        else:
            sets.append(f"{k} = ?"); vals.append(v)
    if not sets:
        raise HTTPException(400, "no updatable fields in body")
    vals.append(bet_strategy_id)
    conn = _connect()
    try:
        n = conn.execute(
            f"UPDATE bet_strategies SET {', '.join(sets)} WHERE id = ?", vals,
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    if not n:
        raise HTTPException(404, f"bet strategy {bet_strategy_id} not found")
    return {"updated": bet_strategy_id, "fields": list(body.keys())}


@router.post("/bet_strategies/{bet_strategy_id}/run")
def run_bet_strategy(bet_strategy_id: int, date_from: str | None = None,
                     date_to: str | None = None) -> dict:
    """(Re)build this bet strategy's rows in bet_ledger. Wipes existing rows
    in the date window first."""
    from betting.bet_runner import run_for_bet_strategy
    conn = _connect()
    try:
        return run_for_bet_strategy(conn, bet_strategy_id, date_from, date_to)
    finally:
        conn.close()


@router.get("/bet_strategies/{bet_strategy_id}/charts")
def bet_strategy_charts(bet_strategy_id: int,
                        date_from: str | None = None,
                        date_to: str | None = None) -> dict:
    """Per-bet-strategy chart payload — same SHAPE as
    /strategies/{id}/charts so the SPA can render with the existing chart
    components, but every series is computed from `bet_ledger` rows
    instead of predictions + a hardcoded selection rule.

    Returns:
      cumulative_pnl   — daily stake/payout/pnl + running cum
      odds_buckets     — strike + ROI per odds band
      segments         — by_track / by_class / by_distance (from JOINed races)
      bankroll_path    — compounded bank assuming bet_ledger.stake is fixed
      totals           — n_bets / n_wins / stake / payout / pnl
      thresholds       — config snapshot for the UI
    """
    if not DB_PATH.exists():
        raise HTTPException(404, "DB not initialized")
    conn = _connect()
    try:
        meta = conn.execute(
            "SELECT name, name_en, name_zh, rule_kind, params_json, "
            "       chart_color, model_strategy_id "
            "FROM bet_strategies WHERE id = ?",
            (bet_strategy_id,),
        ).fetchone()
        if not meta:
            raise HTTPException(404, f"bet strategy {bet_strategy_id} not found")
        name, name_en, name_zh, rule_kind, params_json, chart_color, model_id = meta

        where = ["bl.bet_strategy_id = ?"]
        params: list = [bet_strategy_id]
        if date_from:
            where.append("bl.race_date >= ?"); params.append(date_from)
        if date_to:
            where.append("bl.race_date <= ?"); params.append(date_to)

        # ── Cumulative PnL ─────────────────────────────────────────────
        daily = conn.execute(
            f"""
            SELECT bl.race_date AS date,
                   SUM(bl.stake)   AS stake,
                   SUM(bl.payout)  AS payout,
                   SUM(bl.pnl)     AS pnl,
                   COUNT(*)        AS n_bets,
                   SUM(bl.won = 1) AS n_wins
            FROM bet_ledger bl
            WHERE {' AND '.join(where)}
            GROUP BY bl.race_date
            ORDER BY bl.race_date
            """,
            params,
        ).fetchall()
        # Total race count per meeting day (any race that the strategy COULD
        # have bet on) — so the stacked-races chart still works.
        race_count_rows = conn.execute(
            """
            SELECT date, COUNT(*) FROM races
            WHERE date IN (SELECT DISTINCT race_date FROM bet_ledger
                           WHERE bet_strategy_id = ?)
            GROUP BY date
            """,
            (bet_strategy_id,),
        ).fetchall()
        race_counts = {d: n for d, n in race_count_rows}

        cum_pnl = []
        s_cum = p_cum = 0.0; n_cum = 0
        for d, stake, payout, pnl, nb, nw in daily:
            s_cum += float(stake or 0); p_cum += float(payout or 0); n_cum += nb
            cum_pnl.append({
                "date": d,
                "stake_cum": round(s_cum, 2),
                "payout_cum": round(p_cum, 2),
                "pnl_cum": round(p_cum - s_cum, 2),
                "n_bets_cum": n_cum,
                "daily_stake": round(float(stake or 0), 2),
                "daily_pnl": round(float(pnl or 0), 2),
                "daily_bets": nb,
                "daily_wins": nw,
                "total_races": race_counts.get(d, 0),
            })

        # ── Odds buckets ───────────────────────────────────────────────
        bucket_def = [(2.5, 4), (4, 6), (6, 8), (8, 12), (12, 16), (16, 20), (20, 99)]
        odds_buckets = []
        for lo, hi in bucket_def:
            row = conn.execute(
                f"""
                SELECT COUNT(*), SUM(bl.won = 1),
                       SUM(bl.stake), SUM(bl.payout)
                FROM bet_ledger bl
                WHERE {' AND '.join(where)}
                  AND bl.odds_at_bet >= ? AND bl.odds_at_bet < ?
                """,
                params + [lo, hi],
            ).fetchone()
            if not row or not row[0]:
                continue
            n_bets, n_wins, stake, payout = row
            stake = float(stake or 0); payout = float(payout or 0)
            odds_buckets.append({
                "lo": lo, "hi": hi,
                "n_bets": n_bets, "n_wins": n_wins or 0,
                "win_rate": (n_wins / n_bets) if n_bets else 0,
                "expected_win_rate": None,    # not derivable from ledger alone
                "stake": round(stake, 2),
                "payout": round(payout, 2),
                "roi": ((payout - stake) / stake) if stake > 0 else 0,
            })

        # ── Segments by track / class / distance ───────────────────────
        seg_rows = conn.execute(
            f"""
            SELECT ra.course, ra.class, ra.distance,
                   SUM(bl.stake), SUM(bl.payout), COUNT(*), SUM(bl.won = 1)
            FROM bet_ledger bl
            JOIN races ra ON ra.id = bl.race_id
            WHERE {' AND '.join(where)}
            GROUP BY ra.course, ra.class, ra.distance
            """,
            params,
        ).fetchall()

        def _agg(items):
            n = sum(i[5] for i in items); w = sum(i[6] for i in items)
            s = sum(i[3] or 0 for i in items); p = sum(i[4] or 0 for i in items)
            return {"n_bets": n, "n_wins": w,
                    "stake": round(s, 2), "payout": round(p, 2),
                    "pnl": round(p - s, 2),
                    "win_rate": (w / n) if n else 0,
                    "roi": ((p - s) / s) if s > 0 else 0}
        def _class_bucket(cls):
            try:
                n = int(float(str(cls or '').strip().replace('CLASS','').replace('C','').strip()))
                return "C1-2" if n <= 2 else "C3-5"
            except Exception:
                s = str(cls or '').upper()
                return "G/L" if (s.startswith("G") or "LISTED" in s) else "other"
        def _dist_bucket(d):
            try: d = int(d or 0)
            except Exception: return "?"
            if d <= 1200: return "≤1200m"
            if d <= 1800: return "1400-1800m"
            return "≥2000m"

        by_track: dict = {}; by_class: dict = {}; by_distance: dict = {}
        for course, cls, dist, stake, payout, n_bets, n_wins in seg_rows:
            row = (course, cls, dist, stake, payout, n_bets, n_wins)
            by_track.setdefault(course or "?", []).append(row)
            by_class.setdefault(_class_bucket(cls), []).append(row)
            by_distance.setdefault(_dist_bucket(dist), []).append(row)
        segments = {
            "by_track":    [{"label": k, **_agg(v)} for k, v in by_track.items()    if v],
            "by_class":    [{"label": k, **_agg(v)} for k, v in by_class.items()    if v],
            "by_distance": [{"label": k, **_agg(v)} for k, v in by_distance.items() if v],
        }

        # ── Bankroll path (compounded) ────────────────────────────────
        bank_path = []
        bank = 10000.0; peak = bank
        for entry in cum_pnl:
            bank += entry["daily_pnl"]
            peak = max(peak, bank)
            drawdown = ((peak - bank) / peak * 100) if peak > 0 else 0
            bank_path.append({
                "date": entry["date"],
                "bankroll": round(bank, 2),
                "drawdown": round(drawdown, 2),
                "peak": round(peak, 2),
            })

        totals = {
            "n_bets": sum(c["daily_bets"] for c in cum_pnl),
            "n_wins": sum(c["daily_wins"] for c in cum_pnl),
            "stake": round(sum(c["daily_stake"] for c in cum_pnl), 2),
            "payout": round(sum(c["daily_stake"] + c["daily_pnl"] for c in cum_pnl), 2),
        }
    finally:
        conn.close()

    return {
        "bet_strategy_id": bet_strategy_id,
        "name": name, "name_en": name_en, "name_zh": name_zh,
        "rule_kind": rule_kind, "chart_color": chart_color,
        "model_strategy_id": model_id,
        "from": date_from, "to": date_to,
        "cumulative_pnl": cum_pnl,
        "odds_buckets": odds_buckets,
        "segments": segments,
        "bankroll_path": bank_path,
        "totals": totals,
        # Stubs for charts that don't apply to a bet strategy:
        "kelly_scenarios": None,
        "threshold_sweeps": None,
        "top_n_scenarios": None,
        "reliability": None,
    }


@router.get("/bet_strategies/{bet_strategy_id}/curve")
def bet_strategy_curve(bet_strategy_id: int,
                       date_from: str | None = None,
                       date_to: str | None = None) -> dict:
    """Daily PnL curve for chart overlay: one point per race day with
    stake / payout / pnl / cumulative_pnl / n_bets / n_wins."""
    where = ["bet_strategy_id = ?"]
    args: list = [bet_strategy_id]
    if date_from:
        where.append("race_date >= ?"); args.append(date_from)
    if date_to:
        where.append("race_date <= ?"); args.append(date_to)
    conn = _connect()
    try:
        rows = conn.execute(
            f"""
            SELECT race_date,
                   COUNT(*)                    AS n_bets,
                   SUM(won = 1)                AS n_wins,
                   SUM(stake)                  AS stake,
                   SUM(payout)                 AS payout,
                   SUM(pnl)                    AS pnl
            FROM bet_ledger
            WHERE {' AND '.join(where)}
            GROUP BY race_date
            ORDER BY race_date
            """,
            args,
        ).fetchall()
        meta = conn.execute(
            "SELECT name, name_en, name_zh, chart_color FROM bet_strategies WHERE id = ?",
            (bet_strategy_id,),
        ).fetchone()
    finally:
        conn.close()
    if meta is None:
        raise HTTPException(404, "bet strategy not found")
    points = []
    cum = 0.0
    for d, n_bets, n_wins, stake, payout, pnl in rows:
        cum += float(pnl or 0)
        points.append({
            "date": d, "n_bets": n_bets, "n_wins": n_wins,
            "stake": round(float(stake or 0), 2),
            "payout": round(float(payout or 0), 2),
            "pnl": round(float(pnl or 0), 2),
            "cumulative_pnl": round(cum, 2),
        })
    return {
        "id": bet_strategy_id,
        "name": meta[0], "name_en": meta[1], "name_zh": meta[2],
        "chart_color": meta[3],
        "points": points,
    }


@router.get("/bet_tags")
def list_bet_tags(lang: str = "zh") -> dict:
    """Tag-lookup table for the post-mortem RCA system. The SPA renders
    coloured chips next to each bet and reads `description_{lang}` for
    the tooltip / drill-down body."""
    if not DB_PATH.exists():
        raise HTTPException(404, "DB not initialized")
    from betting.post_mortem import seed_tags
    conn = _connect()
    try:
        seed_tags(conn)
        rows = conn.execute(
            "SELECT code, category, severity, label_zh, label_en, "
            "       description_zh, description_en FROM bet_tags "
            "ORDER BY category, severity DESC, code"
        ).fetchall()
    finally:
        conn.close()
    return {"tags": [
        {"code": r[0], "category": r[1], "severity": r[2],
         "label_zh": r[3], "label_en": r[4],
         "description_zh": r[5], "description_en": r[6]}
        for r in rows
    ]}


@router.post("/races/{race_id}/post_mortem")
def compute_post_mortem(race_id: int, force: bool = False) -> dict:
    """Force-compute (or recompute) post-mortem tags for every placed bet
    in `race_id`. Normally called automatically on first /api/races/{date}
    read; this endpoint is for manual refresh after a tag-catalog update."""
    if not DB_PATH.exists():
        raise HTTPException(404, "DB not initialized")
    from betting.post_mortem import tag_race, fetch_for_race
    conn = _connect()
    try:
        n = tag_race(conn, race_id, force=force)
        pms = fetch_for_race(conn, race_id)
    finally:
        conn.close()
    return {"race_id": race_id, "tagged": n, "post_mortem": pms}


@router.get("/horse_eval/{race_id}/{brand}")
def horse_eval(race_id: int, brand: str, lang: str = "zh",
               force: bool = False) -> dict:
    """馬評-style commentary on why this horse won / lost. Cached; first
    call may take ~3 s when DEEPSEEK_API_KEY is set (DeepSeek round-trip).
    Subsequent calls return immediately from horse_eval_text."""
    if not DB_PATH.exists():
        raise HTTPException(404, "DB not initialized")
    from betting.eval_reason import generate
    conn = _connect()
    try:
        text, source = generate(conn, race_id, brand, lang=lang, force_refresh=force)
    finally:
        conn.close()
    return {"race_id": race_id, "brand": brand, "lang": lang,
            "source": source, "text": text}


@router.get("/bet_strategies/rule_kinds")
def list_rule_kinds() -> dict:
    """Catalog of rule kinds + their expected params (for the UI dropdown).

    Singles (WIN/PLACE pools) settle from results.odds × stake. Exotics
    settle by looking up `dividends.dividend` for the canonical sorted
    combination — the dividends table needs to be scraped per meeting
    for these to produce non-zero payouts.
    """
    return {
        # ─── Singles (WIN / PLACE pools) ───────────────────────────────
        "flat_top1": {"params": {"stake": 500},
                      "label": "Flat stake on top pick (WIN pool)"},
        "kelly_top1": {"params": {"bankroll": 10000, "kelly_frac": 0.25, "max_pct": 0.05},
                       "label": "Fractional Kelly on top pick (WIN pool)"},
        "flat_top1_filtered": {"params": {"stake": 500, "min_prob": 0.20, "max_field": 12},
                               "label": "Flat top — filter by min_prob and/or max_field"},
        "dutch_topN": {"params": {"total_stake": 500, "n": 2},
                       "label": "Dutch split across top-N (equal payoff)"},
        "place_top1": {"params": {"stake": 500},
                       "label": "Top pick as PLACE bet"},
        "each_way_top1": {"params": {"stake": 500},
                          "label": "Half WIN + half PLACE on top pick"},
        "market_fav": {"params": {"stake": 500},
                       "label": "Bet the market favourite (baseline; no model)"},
        "market_blended_top1": {"params": {"stake": 500, "alpha": 1.5, "beta": 0.7},
                                "label": "Top pick after Benter α/β re-rank"},
        # ─── Exotics (multi-horse, settled via dividends table) ────────
        "quinella_top2": {"params": {"stake": 100},
                          "label": "Quinella — top-2 picks (any order)"},
        "qpl_top2": {"params": {"stake": 100},
                     "label": "Quinella Place — top-2 picks (any in top-3)"},
        "qpl_top3_box": {"params": {"stake_per_pair": 100},
                         "label": "QPL box — 3 pairs from top-3 picks"},
        "forecast_top2": {"params": {"stake": 100},
                          "label": "Exacta (EXA) — top-2 in EXACT order"},
        "trifecta_top3": {"params": {"stake": 100},
                          "label": "Trifecta (TRI) — top-3 in EXACT order"},
        "trio_top3": {"params": {"stake": 100},
                      "label": "Trio (TRIO) — top-3 in any order"},
        "first_four_top4": {"params": {"stake": 100},
                            "label": "First Four (F4) — top-4 in any order"},
        "quartet_top4": {"params": {"stake": 100},
                         "label": "Quartet (QTT) — top-4 in EXACT order"},
    }


# ─── Audit + CLV reports ──────────────────────────────────────────────────────

@router.get("/strategies/{strategy_id}/audit")
def strategy_audit(strategy_id: int, date_from: str, date_to: str,
                   max_odds: float = 25.0,
                   kelly: float = 0.25, bankroll: float = 10000.0) -> dict:
    from betting import filters as filt, audit
    if not DB_PATH.exists():
        raise HTTPException(404, "DB not initialized")
    conn = _connect()
    try:
        s = filt.FilterSettings(bet_max_odds=max_odds)
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
    bet_min_odds / min_prob), mirroring betting.filters.evaluate, and only the
    top-probability horse in each race is eligible (one bet per race rule —
    edge gating was removed). Stakes are sized by fractional Kelly capped at
    bankroll-pct (kelly_fraction=0 = flat). Bankroll = HK$10,000.
    """
    if not DB_PATH.exists():
        raise HTTPException(404, "DB not initialized")
    conn = _connect()
    try:
        strat = conn.execute(
            "SELECT bet_max_odds, bet_min_odds, min_prob, "
            "       kelly_fraction, kelly_max_bankroll_pct "
            "FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        if not strat:
            raise HTTPException(404, f"strategy {strategy_id} not found")
        bet_max_odds, bet_min_odds, min_prob, kelly_frac, kelly_max_bank = strat
        bet_max_odds   = float(20.0   if bet_max_odds  is None else bet_max_odds)
        bet_min_odds   = float(2.5    if bet_min_odds  is None else bet_min_odds)
        min_prob       = float(0.05   if min_prob      is None else min_prob)
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

    # ── Pass 1: build the "qualified" list. Strategy rule: one bet per race
    #    on the horse with the highest edge (prob × odds), subject to the
    #    odds / min-prob safety rails.
    by_race: dict[int, list[dict]] = {}
    for d, prob, odds, won, race_id, course, race_class, distance in rows:
        if prob is None or odds is None:
            continue
        by_race.setdefault(race_id, []).append({
            "date": d, "prob": float(prob),
            "odds": float(odds),
            "won": int(won), "race_id": race_id,
            "course": course or "",
            "class": (race_class or "").strip(),
            "distance": int(distance) if distance else 0,
            "edge": float(prob) * float(odds),
        })

    qualified: list[dict] = []
    for race_id, cand in by_race.items():
        if not cand:
            continue
        # Rank by calibrated probability, not edge (prob × odds). Cross-
        # validation on 5 splits showed edge ranking picks longshots that
        # almost never win (top-1 hit ~2-5% vs ~33% for prob ranking).
        top = max(cand, key=lambda x: x["prob"])
        odds = top["odds"]
        prob = top["prob"]
        kelly_full = ((prob * odds - 1) / (odds - 1)) if odds > 1 else 0.0
        kelly_full = max(0.0, kelly_full)
        qualified.append({**top, "kelly_full": float(kelly_full)})

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
        # Sweeps still operate on the top-edge-per-race set (matches the live
        # strategy rule). For each candidate threshold, re-pick the top edge
        # under that knob value and recompute totals.
        out = []
        for v in values:
            n = wins = 0
            stake = pay = 0.0
            for race_id, cand in by_race.items():
                if not cand: continue
                top = max(cand, key=lambda x: x["edge"])
                odds, prob, won = top["odds"], top["prob"], top["won"]
                _max  = v if knob == "max_odds" else bet_max_odds
                _min  = v if knob == "min_odds" else bet_min_odds
                _mp   = v if knob == "min_prob" else min_prob
                if not (_min <= odds <= _max): continue
                if prob < _mp: continue
                kf = ((prob * odds - 1) / (odds - 1)) if odds > 1 else 0.0
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

    # ── Top-N per race what-if: ranked by edge (matches the live "top edge
    #    per race" rule when n_top=1). Re-derived from the full `by_race`
    #    candidate map so we can show the trade-off of betting more horses
    #    per race.
    top_n_scenarios = []
    for n_top in (1, 2, 3, 99):   # 99 = "everyone passing the safety rails"
        items = []
        for rid, cand in by_race.items():
            ranked = sorted(cand, key=lambda x: -x["edge"])
            for q in ranked[:n_top]:
                odds = q["odds"]
                if not (bet_min_odds <= odds <= bet_max_odds): continue
                if q["prob"] < min_prob: continue
                kf = ((q["prob"] * odds - 1) / (odds - 1)) if odds > 1 else 0.0
                if kf <= 0: continue
                stake_q = (kelly_max_bank * bankroll if kelly_frac == 0
                           else min(kf * kelly_frac * bankroll, kelly_max_bank * bankroll))
                if stake_q <= 0: continue
                items.append({**q, "stake": stake_q,
                              "payout": stake_q * odds if q["won"] else 0.0})
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
            "min_prob": min_prob,
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
def get_races_for_date(date: str, strategy_id: int | None = None,
                       bet_strategy_id: int | None = None) -> dict:
    """Race cards for `date`, merged with optional strategy predictions and
    actual results. Powers the SPA's Race Viewer tab.

    If `bet_strategy_id` is supplied, every race that has bet_ledger rows
    for that strategy gets a `bets` array listing the picks (brand, pool,
    stake, won, payout, pnl). The SPA highlights those horses + replaces
    the "Today's Bets" summary with the bet strategy's actual picks.
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
            # Horse rows joined with optional predictions and horse profile.
            # `live_odds` LEFT JOIN against the most recent odds_snapshots row
            # (keyed by horse_no) so pre-race displays surface the latest tote
            # odds when results.odds is still null. `place_div` LEFT JOIN to
            # the dividends table so historical placed horses (top-3 finishers)
            # surface the actual PLACE payout (dividend / 10, since HKJC
            # quotes per $10 stake). place_odds prefers the dividend when
            # available (final payout for top-3) and falls back to the last
            # pre-race live tick for non-placers, so settled races show
            # both: top-3 → actual payout, rest → last pre-race place price.
            live_join = (
                "LEFT JOIN ("
                "  SELECT date, course, race_no, horse_no, win_odds, place_odds, "
                "         pool_total FROM odds_snapshots o1 "
                "  WHERE id = (SELECT MAX(id) FROM odds_snapshots o2 "
                "              WHERE o2.date=o1.date AND o2.course=o1.course "
                "                AND o2.race_no=o1.race_no AND o2.horse_no=o1.horse_no)"
                ") lo ON lo.date = r.date AND lo.course = r.course "
                "        AND lo.race_no = r.race_no AND lo.horse_no = r.horse_no "
                "LEFT JOIN dividends pd ON pd.date = r.date AND pd.course = r.course "
                "        AND pd.race_no = r.race_no AND pd.pool = 'PLACE' "
                "        AND pd.combination = r.brand "
            )
            if strategy_id is not None:
                horse_rows = conn.execute(
                    f"""
                    SELECT r.brand, r.horse_name, r.jockey, r.trainer, r.draw,
                           r.act_wt, r.decl_wt,
                           COALESCE(r.odds, lo.win_odds) AS odds,
                           lo.win_odds AS live_win_odds,
                           COALESCE(pd.dividend / 10.0, lo.place_odds) AS place_odds,
                           r.finish_time, r.lbw,
                           r.running_style, r.position, r.horse_no,
                           h.age, h.sex, h.rating,
                           h.name_en AS horse_name_en, h.name_zh AS horse_name_zh,
                           p.fundamental_prob, p.market_implied_prob,
                           p.blended_prob, p.calibrated_prob, p.edge,
                           p.recommendation
                    FROM results r
                    LEFT JOIN horses h ON h.brand = r.brand
                    {live_join}
                    LEFT JOIN predictions p ON p.race_id = r.race_id
                                            AND p.brand = r.brand
                                            AND p.strategy_id = ?
                    WHERE r.race_id = ?
                    """,
                    (strategy_id, race_id),
                ).fetchall()
                cols = ("brand","horse_name","jockey","trainer","draw","act_wt",
                        "decl_wt","odds","live_win_odds","place_odds",
                        "finish_time","lbw","running_style","position","horse_no",
                        "age","sex","rating","horse_name_en","horse_name_zh",
                        "fundamental_prob","market_implied_prob","blended_prob",
                        "calibrated_prob","edge","recommendation")
            else:
                horse_rows = conn.execute(
                    f"""
                    SELECT r.brand, r.horse_name, r.jockey, r.trainer, r.draw,
                           r.act_wt, r.decl_wt,
                           COALESCE(r.odds, lo.win_odds) AS odds,
                           lo.win_odds AS live_win_odds,
                           COALESCE(pd.dividend / 10.0, lo.place_odds) AS place_odds,
                           r.finish_time, r.lbw,
                           r.running_style, r.position, r.horse_no,
                           h.age, h.sex, h.rating,
                           h.name_en AS horse_name_en, h.name_zh AS horse_name_zh
                    FROM results r
                    LEFT JOIN horses h ON h.brand = r.brand
                    {live_join}
                    WHERE r.race_id = ?
                    """,
                    (race_id,),
                ).fetchall()
                cols = ("brand","horse_name","jockey","trainer","draw","act_wt",
                        "decl_wt","odds","live_win_odds","place_odds",
                        "finish_time","lbw","running_style","position","horse_no",
                        "age","sex","rating","horse_name_en","horse_name_zh")
            horses = [dict(zip(cols, h)) for h in horse_rows]
            # Data sufficiency: count each horse's prior completed races in our DB
            # before this date. Debutants (0) / lightly-raced horses have little
            # form for the model to use, so the SPA flags them.
            _brands_here = [h["brand"] for h in horses]
            if _brands_here:
                _ph2 = ",".join("?" * len(_brands_here))
                _prior = dict(conn.execute(
                    f"SELECT rh.brand, COUNT(*) FROM results rh "
                    f"JOIN races rah ON rah.id = rh.race_id "
                    f"WHERE rh.brand IN ({_ph2}) AND rah.date < ? "
                    f"  AND rh.position IS NOT NULL GROUP BY rh.brand",
                    (*_brands_here, date)).fetchall())
                for h in horses:
                    h["prior_starts"] = _prior.get(h["brand"], 0)
                # Most recent barrier trial before this date (form line for
                # debutants especially). ASC order so the latest row wins.
                _trials: dict[str, dict] = {}
                for _b, _pos, _fs, _td in conn.execute(
                    f"SELECT brand, position, field_size, date FROM barrier_trials "
                    f"WHERE brand IN ({_ph2}) AND date < ? ORDER BY date ASC",
                    (*_brands_here, date)):
                    _trials[_b] = {"pos": _pos, "field": _fs, "date": _td}
                for h in horses:
                    h["last_trial"] = _trials.get(h["brand"])
            # Bilingual jockey/trainer names from the persons registry
            # (populated by scrape_persons.py, keyed by HKJC official IDs).
            # We do the lookup in Python so we can strip the apprentice claim
            # suffix '(-7)' before matching name_en.
            import re as _re
            _claim_re = _re.compile(r"\s*\(-?\d+\)\s*$")
            _strip = lambda s: _claim_re.sub("", (s or "").strip()).strip()
            persons_rows = conn.execute(
                "SELECT kind, name_en, name_zh, hkjc_id FROM persons "
                "WHERE name_en IS NOT NULL"
            ).fetchall()
            _by_kind: dict[tuple[str, str], tuple[str | None, str]] = {}
            for kind, name_en, name_zh, hkjc_id in persons_rows:
                _by_kind[(kind, name_en)] = (name_zh, hkjc_id)
            for h in horses:
                j_en = _strip(h.get("jockey"))
                t_en = _strip(h.get("trainer"))
                j = _by_kind.get(("jockey", j_en))
                t = _by_kind.get(("trainer", t_en))
                h["jockey_name_en"] = j_en or None
                h["jockey_name_zh"] = j[0] if j else None
                h["jockey_hkjc_id"] = j[1] if j else None
                h["trainer_name_en"] = t_en or None
                h["trainer_name_zh"] = t[0] if t else None
                h["trainer_hkjc_id"] = t[1] if t else None
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
            # Attach the full odds_snapshots history per horse so the SPA
            # can draw tiny win- and place-odds sparklines next to each
            # odds cell. Ordered by ts so the renderer can map points to
            # time directly.
            snap_rows = conn.execute(
                "SELECT horse_no, ts, win_odds, place_odds "
                "FROM odds_snapshots "
                "WHERE race_id = ? "
                "  AND (win_odds IS NOT NULL OR place_odds IS NOT NULL) "
                "ORDER BY horse_no, ts",
                (race_id,),
            ).fetchall()
            by_horse: dict[int, list[dict]] = {}
            for hno, ts, wo, po in snap_rows:
                by_horse.setdefault(hno, []).append(
                    {"ts": ts, "win_odds": wo, "place_odds": po}
                )
            for h in horses:
                h["odds_history"] = by_horse.get(h.get("horse_no"), [])
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

        # If user selected a bet strategy, attach every bet_ledger row for
        # this date keyed by race_id. The SPA uses these to highlight
        # the bet horses + replace the "Today's Bets" summary.
        bet_strategy_meta = None
        if bet_strategy_id is not None:
            bs = conn.execute(
                "SELECT id, name, name_en, name_zh, rule_kind, chart_color "
                "FROM bet_strategies WHERE id = ?", (bet_strategy_id,),
            ).fetchone()
            if bs:
                bet_strategy_meta = {
                    "id": bs[0], "name": bs[1], "name_en": bs[2],
                    "name_zh": bs[3], "rule_kind": bs[4], "chart_color": bs[5],
                }
                ledger_rows = conn.execute(
                    """
                    SELECT id, race_id, brand, pool, stake, odds_at_bet, won,
                           payout, pnl, pick_rank, reason
                    FROM bet_ledger
                    WHERE bet_strategy_id = ? AND race_date = ?
                    """,
                    (bet_strategy_id, date),
                ).fetchall()
                bets_by_race: dict[int, list] = {}
                for bid, race_id_, brand, pool, stake, odds, won, payout, pnl, rank, reason in ledger_rows:
                    bets_by_race.setdefault(race_id_, []).append({
                        "id": bid, "brand": brand, "pool": pool, "stake": stake,
                        "odds_at_bet": odds, "won": won, "payout": payout,
                        "pnl": pnl, "pick_rank": rank, "reason": reason,
                    })
                for race in out_races:
                    race["bets"] = bets_by_race.get(race["id"], [])

                # First-read trigger for post-mortem RCA. For each settled
                # race that has bets in this ledger but no post-mortem rows
                # yet, compute tags inline. Cheap on re-reads thanks to the
                # bet_id UNIQUE constraint.
                from betting import post_mortem as _pm
                for race in out_races:
                    if not race.get("has_results") or not race.get("bets"):
                        continue
                    bet_ids = [b for b in conn.execute(
                        "SELECT id FROM bet_ledger WHERE race_id = ? "
                        "AND bet_strategy_id = ? AND won IN (0, 1)",
                        (race["id"], bet_strategy_id),
                    ).fetchall()]
                    if not bet_ids:
                        continue
                    have = conn.execute(
                        "SELECT COUNT(*) FROM bet_post_mortem "
                        "WHERE race_id = ? AND bet_id IN "
                        "(SELECT id FROM bet_ledger WHERE race_id = ? "
                        "AND bet_strategy_id = ?)",
                        (race["id"], race["id"], bet_strategy_id),
                    ).fetchone()[0]
                    if have < len(bet_ids):
                        # Inline write — give up fast (500ms) if a concurrent
                        # writer holds the lock. Falling through is harmless:
                        # the missing tags get computed on the next read when
                        # the lock is free, and the response still carries
                        # every race + any post_mortem rows already on disk.
                        # Without this, a single busy writer (ablation backfill,
                        # decision loop, integrity heal) silently 30s-stalls
                        # every Races-page load until busy_timeout pops.
                        conn.execute("PRAGMA busy_timeout = 500")
                        try:
                            _pm.tag_race(conn, race["id"])
                        except sqlite3.OperationalError:
                            pass
                        finally:
                            conn.execute("PRAGMA busy_timeout = 30000")
                    race["post_mortem"] = _pm.fetch_for_race(conn, race["id"])
    finally:
        conn.close()
    return {"date": date, "races": out_races, "dividends": dividends,
            "bet_strategy": bet_strategy_meta}


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

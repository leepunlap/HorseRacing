#!/usr/bin/env python3
"""
Horse Racing v2 SQLite Database — schema + raw-table migration shim from v1.

This is the side-by-side v2 store. v1's `data/racing.db` is left untouched.
v2 lives at `data/racing_v2.db` and is owned by code under the `*_v2/` package
directories (scrapers_v2, features_v2, models_v2, betting_v2, live_v2,
monitoring_v2).

Schema philosophy:
  * Raw HKJC tables mirror v1 column-for-column so the migration shim is a
    trivial `INSERT INTO ... SELECT FROM` across an ATTACHed v1 database.
  * New v2-only tables (odds_snapshots, barrier_trials, trackwork, vet_records,
    horse_pedigree, weather, per_horse_sectionals, feature_catalog,
    feature_values, strategies_v2, predictions_v2, live_bets, circuit_breaker,
    calibration_metrics, drift_alerts, kill_switch) are appended.
  * `feature_catalog`/`feature_values` use a tall key-value layout so adding a
    175th feature is a row insert, never a column-add. Same idea for paid feeds
    later — drop in a new table, no destructive migration.

Run:
  python3 db_v2.py --init        Create empty v2 DB with schema.
  python3 db_v2.py --migrate     Copy raw tables from v1 into v2.
  python3 db_v2.py --stats       Print row counts per table.
"""

import argparse
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
V1_DB = DATA_DIR / "racing.db"
V2_DB = DATA_DIR / "racing_v2.db"

# Raw-data tables mirrored from v1 — kept column-identical so the migrator is
# `INSERT INTO v2.x SELECT * FROM v1.x`. Schema drift between v1 versions is
# tolerated by the migrator's column-intersection logic.
RAW_SCHEMA = """
CREATE TABLE IF NOT EXISTS races (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    course TEXT NOT NULL,
    race_no INTEGER NOT NULL,
    distance INTEGER,
    class TEXT,
    going TEXT,
    participants INTEGER,
    prize TEXT,
    race_name TEXT,
    season TEXT,
    UNIQUE(date, course, race_no)
);

CREATE TABLE IF NOT EXISTS horses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand TEXT UNIQUE NOT NULL,
    name TEXT,
    age INTEGER,
    sex TEXT,
    colour TEXT,
    origin TEXT,
    rating INTEGER,
    season_start_rating INTEGER,
    race_count INTEGER,
    import_date TEXT,
    trainer TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id INTEGER REFERENCES races(id),
    horse_id INTEGER REFERENCES horses(id),
    date TEXT,
    race_no INTEGER,
    course TEXT,
    brand TEXT NOT NULL,
    horse_name TEXT,
    jockey TEXT,
    trainer TEXT,
    position INTEGER,
    draw INTEGER,
    act_wt REAL,
    decl_wt REAL,
    odds REAL,
    finish_time REAL,
    lbw REAL,
    running_style TEXT,
    won INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sectionals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id INTEGER REFERENCES races(id),
    date TEXT,
    course TEXT,
    race_no INTEGER,
    distance INTEGER,
    total_time REAL,
    splits TEXT,
    cumulatives TEXT,
    num_sections INTEGER,
    early_pace REAL,
    late_pace REAL,
    pace_score REAL
);

CREATE TABLE IF NOT EXISTS race_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    horse_id INTEGER REFERENCES horses(id),
    brandno TEXT NOT NULL,
    age INTEGER,
    sex TEXT,
    meetingcode TEXT,
    pla INTEGER,
    date TEXT,
    venue TEXT,
    distance INTEGER,
    going TEXT,
    class TEXT,
    draw INTEGER,
    rating INTEGER,
    trainercn TEXT,
    jockeycn TEXT,
    lbw REAL,
    odds REAL,
    actwt REAL,
    declwt REAL,
    running TEXT,
    finishtime REAL,
    gear TEXT
);

CREATE TABLE IF NOT EXISTS dividends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    course TEXT NOT NULL,
    race_no INTEGER NOT NULL,
    pool TEXT NOT NULL,
    combination TEXT NOT NULL,
    dividend REAL NOT NULL,
    UNIQUE(date, course, race_no, pool, combination)
);

CREATE INDEX IF NOT EXISTS idx_v2_races_date ON races(date);
CREATE INDEX IF NOT EXISTS idx_v2_horses_brand ON horses(brand);
CREATE INDEX IF NOT EXISTS idx_v2_results_race ON results(race_id);
CREATE INDEX IF NOT EXISTS idx_v2_results_horse ON results(horse_id);
CREATE INDEX IF NOT EXISTS idx_v2_results_brand ON results(brand);
CREATE INDEX IF NOT EXISTS idx_v2_results_date ON results(date);
CREATE INDEX IF NOT EXISTS idx_v2_sectionals_race ON sectionals(race_id);
CREATE INDEX IF NOT EXISTS idx_v2_history_brand ON race_history(brandno, date);
CREATE INDEX IF NOT EXISTS idx_v2_dividends_race ON dividends(date, course, race_no);
"""

# v2-only tables. Each is independent; adding more later is additive.
V2_SCHEMA = """
-- ─── Pre-race odds polling (Cat 14 source) ─────────────────────────────────
CREATE TABLE IF NOT EXISTS odds_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id INTEGER REFERENCES races(id),
    date TEXT NOT NULL,
    course TEXT NOT NULL,
    race_no INTEGER NOT NULL,
    horse_no INTEGER NOT NULL,            -- saddle number 1..N
    brand TEXT,
    ts TEXT NOT NULL,                     -- ISO-8601, the poll timestamp
    win_odds REAL,
    place_odds REAL,
    pool_total REAL,                      -- total WIN pool in HK$ at snapshot
    source TEXT DEFAULT 'hkjc_tote',
    UNIQUE(date, course, race_no, horse_no, ts)
);
CREATE INDEX IF NOT EXISTS idx_v2_odds_race ON odds_snapshots(date, course, race_no);
CREATE INDEX IF NOT EXISTS idx_v2_odds_ts ON odds_snapshots(ts);

-- ─── Barrier trials (Cat 3, 8 source) ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS barrier_trials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    horse_id INTEGER REFERENCES horses(id),
    brand TEXT,
    date TEXT NOT NULL,
    venue TEXT,
    surface TEXT,                          -- Turf, AWT
    distance INTEGER,
    going TEXT,
    position INTEGER,                      -- finishing position in trial
    field_size INTEGER,
    time_sec REAL,
    sectional_400 REAL,                    -- final 400m sectional (HKJC reports)
    gear TEXT,
    jockey TEXT,
    trainer TEXT,
    notes TEXT,
    UNIQUE(brand, date, venue, distance)
);
CREATE INDEX IF NOT EXISTS idx_v2_bt_brand ON barrier_trials(brand, date);

-- ─── Trackwork / morning gallops (Cat 8, 16 source) ────────────────────────
CREATE TABLE IF NOT EXISTS trackwork (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    horse_id INTEGER REFERENCES horses(id),
    brand TEXT,
    date TEXT NOT NULL,
    venue TEXT,
    surface TEXT,                          -- Turf, AWT, Dirt
    distance INTEGER,                      -- gallop distance (m)
    time_sec REAL,                         -- total time
    gear TEXT,
    rider TEXT,
    trainer TEXT,
    notes TEXT,
    UNIQUE(brand, date, venue, distance, time_sec)
);
CREATE INDEX IF NOT EXISTS idx_v2_tw_brand ON trackwork(brand, date);

-- ─── Vet records (Cat 9 source) ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS vet_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    horse_id INTEGER REFERENCES horses(id),
    brand TEXT,
    date TEXT NOT NULL,
    type TEXT NOT NULL,                    -- e.g. bleeder, lameness, roarer-surgery, off-vet
    severity TEXT,
    notes TEXT,
    cleared_date TEXT,
    UNIQUE(brand, date, type)
);
CREATE INDEX IF NOT EXISTS idx_v2_vet_brand ON vet_records(brand, date);

-- ─── Pedigree / Dosage (Cat 1 source) ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS horse_pedigree (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    horse_id INTEGER REFERENCES horses(id),
    brand TEXT UNIQUE,
    sire TEXT,
    dam TEXT,
    dam_sire TEXT,
    dosage_brilliant INTEGER,
    dosage_intermediate INTEGER,
    dosage_classic INTEGER,
    dosage_solid INTEGER,
    dosage_professional INTEGER,
    dosage_index REAL,                     -- DI
    centre_of_distribution REAL,           -- CD
    origin_country TEXT,
    birth_month INTEGER,
    hemisphere TEXT,                       -- N or S
    fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ─── Rail position per meeting (Cat 15 source) ─────────────────────────────
CREATE TABLE IF NOT EXISTS rail_position (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    course TEXT NOT NULL,
    rail TEXT,                             -- A, B, C, C+3 etc.
    watering_cm REAL,
    grass_height_cm REAL,
    notes TEXT,
    UNIQUE(date, course)
);

-- ─── Weather observations per race (Cat 7, 15 source) ──────────────────────
CREATE TABLE IF NOT EXISTS weather_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id INTEGER REFERENCES races(id),
    date TEXT NOT NULL,
    course TEXT NOT NULL,
    race_no INTEGER,
    observed_at TEXT,
    temperature_c REAL,
    rainfall_mm REAL,
    wind_speed_kmh REAL,
    wind_direction_deg INTEGER,
    humidity_pct REAL,
    UNIQUE(date, course, race_no)
);

-- ─── Per-horse sectionals (Cat 10 source; HKJC published per-furlong) ──────
CREATE TABLE IF NOT EXISTS per_horse_sectionals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id INTEGER REFERENCES results(id),
    race_id INTEGER REFERENCES races(id),
    brand TEXT,
    furlong_idx INTEGER NOT NULL,          -- 1..N from start
    split_time REAL,                       -- seconds for this furlong
    cumulative_time REAL,
    position INTEGER,                      -- position at end of this furlong
    lengths_from_lead REAL,
    UNIQUE(race_id, brand, furlong_idx)
);
CREATE INDEX IF NOT EXISTS idx_v2_phs_race ON per_horse_sectionals(race_id);

-- ─── Feature catalog & values (tall key-value: 175th feature is a row) ─────
CREATE TABLE IF NOT EXISTS feature_catalog (
    feature_id TEXT PRIMARY KEY,           -- H001..H174 (and beyond)
    category INTEGER NOT NULL,             -- 1..16
    name_zh TEXT NOT NULL,
    name_en TEXT NOT NULL,
    definition TEXT,
    source_refs TEXT,                      -- bibliography keys: B1,B2,...
    compute_module TEXT,                   -- e.g. features_v2.compute.h001_age
    depends_on TEXT,                       -- comma-sep feature_ids this feature needs
    enabled_default INTEGER DEFAULT 1,
    nan_permitted INTEGER DEFAULT 1,
    added_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS feature_values (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id INTEGER NOT NULL REFERENCES races(id),
    horse_id INTEGER REFERENCES horses(id),
    brand TEXT NOT NULL,
    feature_id TEXT NOT NULL REFERENCES feature_catalog(feature_id),
    value REAL,                            -- NaN stored as NULL
    snapshot_basis TEXT,                   -- ISO-8601 cutoff used for point-in-time
    computed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(race_id, brand, feature_id, snapshot_basis)
);
CREATE INDEX IF NOT EXISTS idx_v2_fv_race ON feature_values(race_id);
CREATE INDEX IF NOT EXISTS idx_v2_fv_feature ON feature_values(feature_id);
CREATE INDEX IF NOT EXISTS idx_v2_fv_brand ON feature_values(brand);

-- ─── v2 strategies ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS strategies_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    name_zh TEXT,
    name_en TEXT,
    description TEXT,
    enabled INTEGER DEFAULT 0,
    -- modelling
    stage1_algo TEXT DEFAULT 'xgb_lambdamart',
    stage2_enabled INTEGER DEFAULT 1,      -- Benter two-stage
    stage2_alpha REAL DEFAULT 0.5,         -- weight on fundamental log-prob
    stage2_beta REAL DEFAULT 0.5,          -- weight on market log-prob
    calibration TEXT DEFAULT 'isotonic',   -- isotonic | platt | bucketed | none
    -- features (JSON map feature_id -> bool, only overrides; missing = enabled_default)
    features_enabled_json TEXT,
    -- betting
    bet_types_json TEXT DEFAULT '["win"]',
    edge_threshold REAL DEFAULT 1.05,
    min_prob REAL DEFAULT 0.02,
    bet_min_odds REAL DEFAULT 2.0,
    bet_max_odds REAL DEFAULT 25.0,
    kelly_fraction REAL DEFAULT 0.25,
    kelly_max_bankroll_pct REAL DEFAULT 0.05,
    pool_impact_max_pct REAL DEFAULT 0.005,
    -- safety
    circuit_daily_loss_pct REAL DEFAULT 0.10,
    circuit_weekly_loss_pct REAL DEFAULT 0.25,
    -- xgb hyperparams (JSON)
    xgb_params_json TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ─── v2 predictions (per-strategy per-race per-horse) ──────────────────────
CREATE TABLE IF NOT EXISTS predictions_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL REFERENCES strategies_v2(id),
    race_id INTEGER NOT NULL REFERENCES races(id),
    horse_id INTEGER REFERENCES horses(id),
    brand TEXT NOT NULL,
    fundamental_prob REAL,                 -- stage 1
    market_implied_prob REAL,              -- from latest odds snapshot
    blended_prob REAL,                     -- stage 2 (Benter)
    calibrated_prob REAL,                  -- after calibration layer
    odds_at_prediction REAL,
    edge REAL,                             -- calibrated_prob * odds
    kelly_stake REAL,
    recommendation TEXT,                   -- bet | skip | blocked
    decision_reason TEXT,
    snapshot_basis TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(strategy_id, race_id, brand, snapshot_basis)
);
CREATE INDEX IF NOT EXISTS idx_v2_preds_strategy ON predictions_v2(strategy_id);
CREATE INDEX IF NOT EXISTS idx_v2_preds_race ON predictions_v2(race_id);

-- ─── Live bets (append-only audit log) ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS live_bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL REFERENCES strategies_v2(id),
    race_id INTEGER NOT NULL REFERENCES races(id),
    horse_id INTEGER REFERENCES horses(id),
    brand TEXT NOT NULL,
    bet_type TEXT NOT NULL,                -- win | place | quinella | trifecta | ...
    placed_at TEXT NOT NULL,               -- ISO-8601
    stake REAL NOT NULL,                   -- HK$
    odds_at_placement REAL,
    expected_value REAL,
    mode TEXT NOT NULL DEFAULT 'paper',    -- paper | live
    settled_result TEXT,                   -- win | lose | void
    payout REAL,
    closing_odds REAL,                     -- for internal CLV
    clv_internal REAL,                     -- placed_odds vs closing
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_v2_bets_strategy ON live_bets(strategy_id, placed_at);
CREATE INDEX IF NOT EXISTS idx_v2_bets_race ON live_bets(race_id);

-- ─── Circuit breaker state ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS circuit_breaker_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL REFERENCES strategies_v2(id),
    date TEXT NOT NULL,
    daily_pnl REAL DEFAULT 0,
    weekly_pnl REAL DEFAULT 0,
    bankroll_start REAL,
    halted INTEGER DEFAULT 0,
    halt_reason TEXT,
    halt_until TEXT,
    UNIQUE(strategy_id, date)
);

-- ─── Calibration & drift health ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS calibration_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL REFERENCES strategies_v2(id),
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    brier REAL,
    log_loss REAL,
    ece REAL,                              -- Expected Calibration Error
    sample_count INTEGER,
    UNIQUE(strategy_id, window_end)
);

CREATE TABLE IF NOT EXISTS drift_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_id TEXT REFERENCES feature_catalog(feature_id),
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    metric TEXT NOT NULL,                  -- psi | ks | js | chi2
    value REAL,
    threshold REAL,
    breached INTEGER DEFAULT 0,
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ─── Kill switch (single-row global) ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS kill_switch_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    halted INTEGER DEFAULT 0,
    halt_reason TEXT,
    halted_at TEXT,
    halted_by TEXT,
    last_heartbeat TEXT
);
INSERT OR IGNORE INTO kill_switch_state (id, halted) VALUES (1, 0);
"""


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create empty v2 DB with full schema. Idempotent."""
    DATA_DIR.mkdir(exist_ok=True)
    conn = _connect(V2_DB)
    conn.executescript(RAW_SCHEMA)
    conn.executescript(V2_SCHEMA)
    conn.commit()
    conn.close()
    print(f"v2 DB initialized: {V2_DB}")


def _columns(conn: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    cur = conn.execute(f"PRAGMA {schema}.table_info({table})")
    return [r[1] for r in cur.fetchall()]


def migrate_from_v1() -> None:
    """Copy raw HKJC tables (races, horses, results, sectionals, race_history,
    dividends) from v1 racing.db into v2 racing_v2.db.

    Idempotent: uses INSERT OR IGNORE so re-runs don't duplicate.
    Tolerant of v1 schema drift via per-table v1→v2 column-rename maps; v1
    naming is inconsistent (races.raceno vs results.race_no) and v2 normalises
    everything to snake_case `race_no`.
    """
    if not V1_DB.exists():
        raise SystemExit(f"v1 DB not found at {V1_DB}; nothing to migrate")
    if not V2_DB.exists():
        init_db()

    # v1_col -> v2_col, per table. Anything not listed copies as-is.
    rename_maps: dict[str, dict[str, str]] = {
        "races":      {"raceno": "race_no"},
        "sectionals": {"raceno": "race_no", "totaltime": "total_time",
                       "numsections": "num_sections", "earlypace": "early_pace",
                       "latepace": "late_pace", "pacescore": "pace_score"},
    }

    conn = _connect(V2_DB)
    conn.execute(f"ATTACH DATABASE '{V1_DB}' AS v1")

    tables = ["races", "horses", "results", "sectionals", "race_history", "dividends"]
    for table in tables:
        v1_cols = _columns(conn, table, "v1")
        v2_cols = _columns(conn, table, "main")
        rmap = rename_maps.get(table, {})
        # (v1_col, v2_col) pairs where v2_col exists in v2.
        pairs = [(c, rmap.get(c, c)) for c in v1_cols if c != "id"]
        pairs = [(src, dst) for src, dst in pairs if dst in v2_cols]
        if not pairs:
            print(f"  {table}: no common columns, skipped")
            continue
        src_list = ", ".join(src for src, _ in pairs)
        dst_list = ", ".join(dst for _, dst in pairs)
        before = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        conn.execute(
            f"INSERT OR IGNORE INTO {table} ({dst_list}) "
            f"SELECT {src_list} FROM v1.{table}"
        )
        after = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {after - before:+d} rows (now {after:,})")

    # Backfill results.race_id and results.horse_id where possible.
    conn.execute("""
        UPDATE results SET race_id = (
            SELECT r.id FROM races r
            WHERE r.date = results.date
              AND r.course = results.course
              AND r.race_no = results.race_no
        ) WHERE race_id IS NULL
    """)
    conn.execute("""
        UPDATE results SET horse_id = (
            SELECT h.id FROM horses h WHERE h.brand = results.brand
        ) WHERE horse_id IS NULL
    """)
    conn.execute("""
        UPDATE race_history SET horse_id = (
            SELECT h.id FROM horses h WHERE h.brand = race_history.brandno
        ) WHERE horse_id IS NULL
    """)
    conn.commit()
    conn.execute("DETACH DATABASE v1")
    conn.close()
    print(f"Migration complete: {V2_DB}")


def show_stats() -> None:
    if not V2_DB.exists():
        raise SystemExit(f"v2 DB not found at {V2_DB}; run --init first")
    conn = _connect(V2_DB)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()]
    width = max(len(t) for t in tables)
    for t in tables:
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t.ljust(width)}  {n:>10,}")
    conn.close()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--init", action="store_true", help="Create empty v2 DB with schema")
    p.add_argument("--migrate", action="store_true", help="Copy raw tables from v1 → v2")
    p.add_argument("--stats", action="store_true", help="Show v2 table row counts")
    args = p.parse_args()

    if args.init:
        init_db()
    if args.migrate:
        migrate_from_v1()
    if args.stats:
        show_stats()
    if not (args.init or args.migrate or args.stats):
        p.print_help()


if __name__ == "__main__":
    main()

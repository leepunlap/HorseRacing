#!/usr/bin/env python3
"""
Horse Racing SQLite Database — schema.

Database lives at `data/racing.db` and is owned by code under the package
directories (scrapers, features, models, betting, live, monitoring).

Schema philosophy:
  * Raw HKJC tables (races, horses, results, sectionals, race_history,
    dividends) hold the scraped HKJC data column-for-column.
  * Derived/operational tables (odds_snapshots, barrier_trials, trackwork,
    vet_records, horse_pedigree, weather, per_horse_sectionals,
    feature_catalog, feature_values, strategies, predictions, live_bets,
    circuit_breaker, calibration_metrics, drift_alerts, kill_switch) are
    appended.
  * `feature_catalog`/`feature_values` use a tall key-value layout so adding a
    175th feature is a row insert, never a column-add. Same idea for paid feeds
    later — drop in a new table, no destructive migration.

Run:
  python3 db.py --init    Create empty DB with schema.
  python3 db.py --stats   Print row counts per table.
"""

import argparse
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "racing.db"

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
    post_time TEXT,
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

CREATE INDEX IF NOT EXISTS idx_races_date ON races(date);
CREATE INDEX IF NOT EXISTS idx_horses_brand ON horses(brand);
CREATE INDEX IF NOT EXISTS idx_results_race ON results(race_id);
CREATE INDEX IF NOT EXISTS idx_results_horse ON results(horse_id);
CREATE INDEX IF NOT EXISTS idx_results_brand ON results(brand);
CREATE INDEX IF NOT EXISTS idx_results_date ON results(date);
CREATE INDEX IF NOT EXISTS idx_sectionals_race ON sectionals(race_id);
CREATE INDEX IF NOT EXISTS idx_history_brand ON race_history(brandno, date);
CREATE INDEX IF NOT EXISTS idx_dividends_race ON dividends(date, course, race_no);
"""

# tables. Each is independent; adding more later is additive.
SCHEMA = """
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
CREATE INDEX IF NOT EXISTS idx_odds_race ON odds_snapshots(date, course, race_no);
CREATE INDEX IF NOT EXISTS idx_odds_ts ON odds_snapshots(ts);

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
CREATE INDEX IF NOT EXISTS idx_bt_brand ON barrier_trials(brand, date);

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
CREATE INDEX IF NOT EXISTS idx_tw_brand ON trackwork(brand, date);

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
CREATE INDEX IF NOT EXISTS idx_vet_brand ON vet_records(brand, date);

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
CREATE INDEX IF NOT EXISTS idx_phs_race ON per_horse_sectionals(race_id);

-- ─── Feature catalog & values (tall key-value: 175th feature is a row) ─────
CREATE TABLE IF NOT EXISTS feature_catalog (
    feature_id TEXT PRIMARY KEY,           -- H001..H174 (and beyond)
    category INTEGER NOT NULL,             -- 1..16
    name_zh TEXT NOT NULL,
    name_en TEXT NOT NULL,
    definition TEXT,
    source_refs TEXT,                      -- bibliography keys: B1,B2,...
    compute_module TEXT,                   -- e.g. features.compute.h001_age
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
CREATE INDEX IF NOT EXISTS idx_fv_race ON feature_values(race_id);
CREATE INDEX IF NOT EXISTS idx_fv_feature ON feature_values(feature_id);
CREATE INDEX IF NOT EXISTS idx_fv_brand ON feature_values(brand);

-- ─── strategies ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS strategies (
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

-- ─── predictions (per-strategy per-race per-horse) ──────────────────────
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL REFERENCES strategies(id),
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
CREATE INDEX IF NOT EXISTS idx_preds_strategy ON predictions(strategy_id);
CREATE INDEX IF NOT EXISTS idx_preds_race ON predictions(race_id);

-- ─── Live bets (append-only audit log) ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS live_bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL REFERENCES strategies(id),
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
CREATE INDEX IF NOT EXISTS idx_bets_strategy ON live_bets(strategy_id, placed_at);
CREATE INDEX IF NOT EXISTS idx_bets_race ON live_bets(race_id);

-- ─── Circuit breaker state ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS circuit_breaker_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL REFERENCES strategies(id),
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
    strategy_id INTEGER NOT NULL REFERENCES strategies(id),
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

-- ─── Track bias snapshot per meeting (Cat 15 source) ──────────────────────
CREATE TABLE IF NOT EXISTS track_bias_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    course TEXT NOT NULL,
    rail TEXT,
    inside_win_rate_residual REAL,         -- today inner-draw WR minus long-term
    front_runner_win_rate_residual REAL,   -- leaders winning today minus baseline
    par_time_residual REAL,                -- today par-time minus long-term par
    sample_races INTEGER,                  -- # races aggregated on this card
    notes TEXT,
    computed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(date, course)
);

-- ─── Calibrator artifacts (one per strategy×window) ───────────────────────
CREATE TABLE IF NOT EXISTS calibrator_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL REFERENCES strategies(id),
    fit_window_start TEXT NOT NULL,
    fit_window_end TEXT NOT NULL,
    kind TEXT NOT NULL,                    -- isotonic | platt | bucketed | none
    params_json TEXT NOT NULL,             -- pickled fit; restorable via models.calibration
    fitted_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(strategy_id, fit_window_end, kind)
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

-- ─── Strategy run summary ──────────────────────────────────────────────────
-- One row per walk-forward run. Captures everything needed to compare
-- strategies side-by-side without re-aggregating predictions on every page
-- load: counts, money, hit rates, model-health metrics, plus a config_hash
-- so two runs with identical setup are recognisable as the same algo.
CREATE TABLE IF NOT EXISTS strategy_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL REFERENCES strategies(id),
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    -- counts + money
    n_races INTEGER, n_bets INTEGER, n_wins INTEGER,
    total_stake REAL, total_payout REAL, pnl REAL,
    -- headline metrics
    roi_pct REAL, strike_rate_pct REAL,
    top1_hit_rate REAL, top3_hit_rate REAL,
    -- model-health metrics
    brier REAL, log_loss REAL, ece REAL, ndcg3 REAL,
    -- config + provenance
    n_features INTEGER, time_decay_tau REAL,
    config_hash TEXT,                   -- sha1 of (features_json + hyperparams)
    elapsed_s REAL,
    notes TEXT,
    computed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(strategy_id, window_end)
);
CREATE INDEX IF NOT EXISTS idx_runs_strategy ON strategy_runs(strategy_id);
CREATE INDEX IF NOT EXISTS idx_runs_roi ON strategy_runs(roi_pct DESC);

-- ─── Bet strategy (post-prediction rule on top of a model) ────────────────
-- Each bet strategy reads predictions from `model_strategy_id` and decides
-- which horse(s) to bet on, with what stake. Many bet strategies can share
-- one model — they're cheap layers over an expensive walk-forward.
--
-- rule_kind values (handled by betting.bet_runner):
--   flat_top1         — top-prob horse, flat stake
--   kelly_top1        — top-prob horse, fractional Kelly
--   flat_top1_filtered- top-prob horse but skip races failing params filter
--                       (e.g. {"max_field":12,"min_prob":0.20})
--   dutch_topN        — split stake across top-N for equal payoff
--   place_top1        — top-prob horse as PLACE bet
--   each_way_top1     — half WIN / half PLACE on top-prob
--   market_fav        — bet lowest-odds horse (baseline; no model)
--   market_blended    — re-rank model probs with Benter blend, then top
CREATE TABLE IF NOT EXISTS bet_strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    name_en TEXT, name_zh TEXT,
    model_strategy_id INTEGER NOT NULL REFERENCES strategies(id),
    rule_kind TEXT NOT NULL,
    params_json TEXT,                   -- JSON dict of per-rule knobs
    enabled INTEGER DEFAULT 1,
    chart_color TEXT,                   -- hex for SPA chart overlay
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_betstrat_model ON bet_strategies(model_strategy_id);

-- ─── Bet ledger (one row per placed bet per strategy) ─────────────────────
-- bet_runner writes here. A race can contribute multiple rows for dutch /
-- each-way / multi-horse strategies. PnL is settled when the race finishes
-- (CAST(results.position AS INT) populated); unsettled bets have won = -1.
CREATE TABLE IF NOT EXISTS bet_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_strategy_id INTEGER NOT NULL REFERENCES bet_strategies(id),
    race_id INTEGER NOT NULL REFERENCES races(id),
    race_date TEXT NOT NULL,
    brand TEXT NOT NULL,
    pool TEXT NOT NULL DEFAULT 'WIN',   -- WIN | PLACE
    stake REAL NOT NULL,
    odds_at_bet REAL,                   -- final close odds (results.odds) as proxy
    won INTEGER DEFAULT -1,             -- 1=won, 0=lost, -1=unsettled
    payout REAL DEFAULT 0,              -- realised cash back (= stake*odds if won, else 0)
    pnl REAL DEFAULT 0,                 -- payout - stake
    pick_rank INTEGER,                  -- this horse's rank within the race's predictions
    reason TEXT,                        -- short tag, e.g. 'top_prob' / 'skipped_big_field'
    computed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(bet_strategy_id, race_id, brand, pool)
);
CREATE INDEX IF NOT EXISTS idx_ledger_strat ON bet_ledger(bet_strategy_id, race_date);
CREATE INDEX IF NOT EXISTS idx_ledger_race ON bet_ledger(race_id);

-- ─── Model experiment ledger ───────────────────────────────────────────────
-- Every quick_eval run appends here. Lets us answer "what config got the
-- best ROI on the 2026-01 split" with a SQL query instead of grepping
-- shell logs.
CREATE TABLE IF NOT EXISTS model_experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER REFERENCES strategies(id),
    tag TEXT,                           -- user-supplied label per run
    split_date TEXT, until_date TEXT,
    n_features INTEGER,
    objective TEXT, max_depth INTEGER, num_round INTEGER,
    eta REAL, subsample REAL, colsample REAL,
    benter_alpha REAL, benter_beta REAL, use_market INTEGER,
    select_by TEXT, label_scheme TEXT, time_decay_tau REAL,
    -- outputs
    n_races INTEGER, n_bets INTEGER, n_wins INTEGER,
    top1_hit_rate REAL, top3_hit_rate REAL, ndcg3 REAL,
    winner_log_loss REAL,
    total_stake REAL, total_payout REAL, pnl REAL, roi_pct REAL,
    elapsed_s REAL,
    run_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_exp_roi ON model_experiments(roi_pct DESC);
CREATE INDEX IF NOT EXISTS idx_exp_run ON model_experiments(run_at DESC);
"""


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create empty DB with full schema. Idempotent."""
    DATA_DIR.mkdir(exist_ok=True)
    conn = _connect(DB_PATH)
    conn.executescript(RAW_SCHEMA)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    print(f"DB initialized: {DB_PATH}")


def _columns(conn: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    cur = conn.execute(f"PRAGMA {schema}.table_info({table})")
    return [r[1] for r in cur.fetchall()]


def show_stats() -> None:
    if not DB_PATH.exists():
        raise SystemExit(f"DB not found at {DB_PATH}; run --init first")
    conn = _connect(DB_PATH)
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
    p.add_argument("--init", action="store_true", help="Create empty DB with schema")
    p.add_argument("--stats", action="store_true", help="Show table row counts")
    args = p.parse_args()

    if args.init:
        init_db()
    if args.stats:
        show_stats()
    if not (args.init or args.stats):
        p.print_help()


if __name__ == "__main__":
    main()

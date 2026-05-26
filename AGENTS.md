# Horse Racing Project — Agent Notes

## Project overview
HKJC horse racing prediction engine. Walk-forward XGBoost model with 49 features across 9 strategies.
- `backtest.py` — core prediction engine (train/predict/tally/retally)
- `app.py` — FastAPI web app with `/api/races`, `/api/dates`, `/api/models`, `/api/model-stats`
- `scrape_results.py` / `scrape_racecard.py` — Playwright-based HKJC scrapers
- `db.py` — SQLite schema + CSV import
- `static/index.html` — entire SPA frontend (Vanilla JS + ECharts, ~2500 lines)

## Key rules for this codebase

### 1. Never trust position/result fields as integers
Finishing position can be `FE`, `PU`, `UR`, `WV` (fell, pulled up, unseated rider, withdrawn vet).
Always use `str(v).isdigit()` before `int()`. The app now stores both `position` (int or None) and `position_code` (str or None).

### 2. Watch for falsy `0.0` in Python config defaults
`float(cfg.get('key') or 1.0)` breaks when the value is legitimately `0.0` because `0.0 or 1.0` → `1.0`.
Use `float(cfg.get('key', 0.0) or 0.0)` for thresholds that can be zero (disabled).

### 3. DB results come from two sources
- CSV (hkjc_all_results_CN.csv): dates up to ~2026-04-29, has all fields
- Scraped HTML (predictions/*/result_*.html): recent dates, scraped by playwright
- `racecard_parsed.json` is the racecard data; must exist for every date in date picker
- Date picker shows only dates in DB `results` table (257 dates)

### 4. Model staleness system
- `model_config.py:staleness(model_name)` checks config hashes vs summary.json
- `_BET_KEYS` = params that only affect bet selection (need retally, not rerun)
- `_META_KEYS` = identity fields (never trigger staleness)
- Everything else = model params (trigger full rerun)
- Stale predictions: suppressed in race page, dim gray in dashboard, `📭` in date picker

### 5. Features that exist but are NOT in config.json
- Harville: `place_edge_threshold`, `q_edge_threshold`, `qp_edge_threshold`, `q_top_n`
- Kelly: `kelly_fraction`, `kelly_max_bet`
- Isotonic: `use_isotonic_calibration`
All default to 0.0 (disabled) in configs.

### 6. Frontend architecture
- Single `index.html` file, no framework
- `raceCacheMeta` = full `/api/races` response
- `modelConfigCache` = loaded from `/api/model-config/{name}`
- `_staleness()` called server-side only; frontend gets `stale` object in API responses
- `modelStale` = `stale.needs_rerun` — controls prediction column visibility
- Date picker refreshed by `_reloadDateList()` after scrape/batch completion

### 7. Running position data (沿途走位)
CSV stores concatenated digits like "121091". `fmtRunning()` in frontend parses greedily (positions 1–14) and joins with hyphens: "12-10-9-1". Scraped HTML has space-separated positions.

### 8. DB tables
- `results` — 29,676 rows, columns: date, race_no, course, brand, horse_name, jockey, trainer, position, draw, act_wt, odds, finish_time, lbw, running_style, won
- `races` — metadata (distance, class, going, participants)
- `dividends` — 1,008 rows for 7 dates, pools: WIN/PLACE/QIN/QPL/TRIO/F4/EXA/TRI/QTT
- `sectionals` — 2,363 rows with EarlyPace/LatePace/PaceScore

### 9. Harville formulas
Index-based API: `harville_place_prob(idx, prob_array)`, `harville_q_prob(idx_i, idx_j, prob_array)`, etc.
Used by `retally()` to evaluate PLACE/Q/QP bets against dividend data.

### 10. What's NOT implemented
- Real-time live odds integration (H136) — edge uses historical odds
- Harville is implemented but disabled by default (thresholds = 0.0)

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
- Proper Q architecture (H3) — only quick proxy features exist; full architecture below
- Pre-race place pool odds — needed for valid PLC betting backtest; currently only post-race dividends available

### 11. Calibration system
- **Platt scaling** (LogisticRegression on XGBoost scores) replaces IsotonicRegression. Produces smooth sigmoid — no 0.0 or 1.0 probability cliffs. All horses get non-zero probability.
- **WalkForwardCalibration** class: per-date accumulator of (pred_sum, actual_sum) per odds bucket per pool. Factors computed from dates before target date only. Applied to WIN/PLACE/Q/QP in retally.
- **Optimal bet_max_odds**: 5.0x for baseline strategies, 8.0x for 黑馬獵手策略 (from audit sweep).

### 12. Known profitability facts
- Model is conservative at odds <7x (under-predicts win rates), overconfident at >10x (3.5x inflated at 25x+)
- HKJC commission 16.5% → break-even edge ≈ 1.197
- Best profit on Good going, Class 3-5 races. Worst on Soft/Yielding and G1 races.
- 穩健保守策略 (0/52 wins) — effectively broken, needs reset or removal
- Cap 5.0x blocks 4,201 horses vs 125 bets placed — prevents worst bets but doesn't guarantee profit
- 深度推算策略 has best hit rate (19.2%) — deeper trees may help but risk overfitting

### 13. Q architecture (H3) — proper vs quick version

H3: \"連贏需要考慮配搭互動，簡單排名不夠\" (Q needs pair interactions, not simple ranking)

**Current (quick)**: 2 proxy features per horse — `q_style_compat` (complementary-style partner count) and `q_field_strength` (strong-jockey count). These let XGBoost learn some Q pair signal from per-horse data. No separate Q model.

**Proper (not built)**: Separate pair-level XGBoost classifier. Training data = C(n,2) pairs × 250 dates (~180K rows). Features per pair:
- `style_compat` — Harville measure of running-style complement
- `draw_spread` — |draw_i - draw_j| 
- `rating_gap` — |rating_i - rating_j|
- `jockey_wr_product` — geometric mean of both jockey WRs
- `trainer_wr_product` — geometric mean of both trainer WRs
- `harville_q_prob` — baseline from win probabilities
- `shared_history` — have they raced together before?
- `pace_match` — do both benefit from same race pace?

Binary target: did this pair finish 1-2? (~1% positive rate). Model stacked on top of win-prob model. Output replaces current Harville Q probabilities with calibrated pair probabilities.

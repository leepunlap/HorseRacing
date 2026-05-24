# Horse Racing V10 — Usage Guide

## Overview

A self-hosted Hong Kong horse racing analytics platform. It scrapes HKJC race results, engineers 44 features per horse, runs XGBoost walk-forward predictions, and serves a web dashboard with per-race betting analysis.

---

## Project Structure

```
/var/www/horseracing/
├── app.py                  ← FastAPI backend (port 8005)
├── backtest.py             ← Walk-forward prediction runner
├── scrape_results.py       ← HKJC results scraper (Playwright)
├── model_config.py         ← Feature catalogue + model config loader
│
├── models/
│   └── v10_base/
│       ├── config.json     ← All tunable parameters for this model
│       └── results/
│           ├── summary.json
│           └── {YYYY-MM-DD}/
│               └── predictions.json
│
├── predictions/            ← Production output (published from active model)
│   └── {YYYY-MM-DD}/
│       ├── racecard_parsed.json
│       └── predictions.json
│
├── data/
│   ├── racing.db           ← SQLite: races, results, horses, sectionals
│   ├── hkjc_all_results_CN.csv
│   ├── hkjc_horse_profiles_CN.csv
│   ├── hkjc_horse_race_history_CN.csv
│   ├── hkjc_race_meta_CN.csv
│   └── hkjc_sectionals_CN.csv
│
└── static/
    └── index.html          ← Single-page app (Bootstrap 5)
```

---

## 1. Scraping Results

Scrapes HKJC race results for one or more dates into the SQLite database. Automatically detects Sha Tin (ST) vs Happy Valley (HV).

### Single date
```bash
python3 scrape_results.py 2026-05-24
```

### Multiple dates
```bash
python3 scrape_results.py 2026-05-17 2026-05-20 2026-05-24
```

### Date range
```bash
python3 scrape_results.py --from 2026-05-01 --to 2026-05-24
```

### Dry run (parse without writing to DB)
```bash
python3 scrape_results.py --dry-run 2026-05-24
```

**What it does:**
- Scrapes each race's horse table (position, draw, weight, odds, finishing time, LBW, running style)
- Parses race metadata (distance, class, going)
- Upserts into `races` and `results` tables in `data/racing.db`
- Creates a `predictions/{date}/racecard_parsed.json` stub so the date appears in the dashboard dropdown

**HKJC race days** are typically Wednesday evening (Happy Valley) and Sunday afternoon (Sha Tin), with occasional Saturday and public holiday meetings.

---

## 2. Running Predictions / Backtest

The same algorithm is used for both backtesting (past races) and live prediction. Walk-forward: train on all data before date X, predict date X. No lookahead.

### Single date
```bash
python3 backtest.py --model v10_base 2026-05-03
```

### Multiple dates
```bash
python3 backtest.py --model v10_base 2026-05-03 2026-05-06 2026-05-09
```

### Date range
```bash
python3 backtest.py --model v10_base --from 2026-05-01 --to 2026-05-24
```

### All dates in the database
```bash
python3 backtest.py --model v10_base --all
```
> This runs ~250 race days; expect ~55 seconds per date → roughly 4 hours total.

### Skip already-computed dates (default) or force recompute
```bash
python3 backtest.py --model v10_base --all            # skips existing
python3 backtest.py --model v10_base --all --force    # recomputes all
```

### Publish to production
```bash
python3 backtest.py --model v10_base --publish 2026-05-24
```
Copies `models/v10_base/results/2026-05-24/predictions.json` to `predictions/2026-05-24/predictions.json`.

### Output
Results are saved to `models/{model}/results/{date}/predictions.json`. After all dates are run, a summary is written to `models/{model}/results/summary.json`.

---

## 3. Model Configuration

Each model lives in `models/{name}/config.json`. The active model is flagged `"active": true`.

### View all models
```bash
python3 -c "from model_config import list_models; import json; [print(m['name'], m.get('active')) for m in list_models()]"
```

### Switch the active model
```bash
python3 -c "from model_config import set_active_model; set_active_model('v10_base')"
```

### Create a new model variant

Copy an existing config and edit it:
```bash
cp -r models/v10_base models/v10_nodraw
# Edit models/v10_nodraw/config.json: change "name", set "active": false,
# add feature names to "features_disabled": ["draw_inner", "draw_outer", "wide_draw"]
```

Then backtest it:
```bash
python3 backtest.py --model v10_nodraw --all
```

### Key tunable parameters in `config.json`

| Parameter | Description |
|-----------|-------------|
| `xgb.max_depth` | Tree depth (3–7). Deeper = more complex, risk of overfit |
| `xgb.learning_rate` | Step size (0.01–0.1). Lower = slower but more stable |
| `xgb.scale_pos_weight` | Class imbalance weight (~field_size - 1) |
| `num_boost_rounds` | Number of trees (50–300) |
| `draw_inner_max` | Gates ≤ this are "inner" (default: 5) |
| `draw_outer_min` | Gates ≥ this are "outer" (default: 10) |
| `pace_draw` | Bonus/penalty matrix: pace bucket × draw group |
| `layoff.long_days` | Days-absent threshold for heavy penalty |
| `cold_stable_threshold` | Trainer win rate below this = "cold stable" |
| `chri` | Weights for Composite Handicap Relief Index components |
| `pace_match` | Style-pace fit bonuses (leader in slow race, closer in fast race) |
| `features_disabled` | List of feature names to exclude from training |

---

## 4. Web Dashboard

### Start the server
```bash
python3 app.py --port 8005
# or
uvicorn app:app --host 0.0.0.0 --port 8005
```

Access at `http://your-server:8005`. Default password: `168888`.

### Dashboard tabs

**儀表板 (Dashboard)**
- Select a model from the dropdown (top-left) — switches which model's predictions are shown
- Select a race date — shows all races for that day
- Day-level bet summary: number of bets placed, wins, total P&L in units
- Race tabs show ✓/✗ icons for correct/wrong predictions
- Click a race tab to see the full horse table with:
  - Win probability and edge per horse
  - 8-category feature bar chart (hover for detail)
  - Predicted rank, actual finishing position, LBW, running style
  - Per-race bet block: predicted horse → bet placed → actual winner → P&L

**🤖 模型 (Model)**
- Switch active model (click "設為使用中")
- **特徵列表** — all 44 features: category, description, tunable flag, enabled/disabled
- **XGB 參數** — XGBoost hyperparameter values
- **步速×檔位矩陣** — pace-draw bonus matrix (colour-coded)
- **場地編碼** — going condition numeric encoding
- **久休懲罰** — layoff penalty thresholds and form parameters

**🐴 馬匹 / 🏇 騎師 / 👨‍🏫 練馬師**
- Searchable lists with win rates, ride counts
- Click any horse for career stats, distance breakdown, jockey partnerships

---

## 5. Betting Logic

Edge is calculated as: `edge = win_probability × decimal_odds`

- `edge > 1.0` → positive expected value (bet)
- `edge > 1.3` → strong edge → $200 stake
- `edge > 0.9` → marginal edge → $100 stake
- `edge ≤ 0.9` → no bet

Horses with odds > 6.5x are excluded from betting regardless of model probability (longshot filter).

P&L is reported in **units** (1 unit = 1 stake). A correct bet returns `odds - 1` units; an incorrect bet returns `-1` unit.

---

## 6. API Reference

All endpoints require `Authorization: Bearer {token}` header (obtained from `/api/auth/login`).

| Endpoint | Description |
|----------|-------------|
| `POST /api/auth/login` | `{"password": "..."}` → `{"token": "..."}` |
| `GET /api/models` | List all models with summary stats |
| `GET /api/model-config/{name}` | Full config + 44-feature catalogue |
| `POST /api/models/{name}/activate` | Set model as active |
| `GET /api/dates?model=v10_base` | Dates with results; prediction status per model |
| `GET /api/races/{date}?model=v10_base` | Race data + predictions + results + bet analysis |
| `GET /api/horses` | Paginated horse list with filters |
| `GET /api/horses/{brand}` | Horse career stats and history |
| `GET /api/jockeys` | Paginated jockey list |
| `GET /api/jockeys/{name}` | Jockey stats, trainer pairs, monthly form |
| `GET /api/trainers` | Paginated trainer list |
| `GET /api/trainers/{name}` | Trainer stats, jockey pairs, monthly form |

---

## 7. Database Schema

```sql
races   (date, course, raceno, distance, class, going, participants)
results (date, race_no, course, brand, horse_name, jockey, trainer,
         position, draw, act_wt, odds, finish_time, lbw, running_style, won)
horses  (brand, age, sex, rating, race_count)
```

Query examples:
```bash
sqlite3 data/racing.db "SELECT date, COUNT(*) FROM results GROUP BY date ORDER BY date DESC LIMIT 10"
sqlite3 data/racing.db "SELECT jockey, SUM(won), COUNT(*) FROM results GROUP BY jockey ORDER BY SUM(won) DESC LIMIT 10"
```

---

## 8. Typical Workflow

```bash
# 1. After a race day — scrape results
python3 scrape_results.py 2026-05-28

# 2. Run predictions for that date
python3 backtest.py --model v10_base 2026-05-28

# 3. View in dashboard (server already running)
# → Open http://your-server:8005
# → Select model v10_base, select date 2026-05-28

# 4. Tune parameters — create a new variant
cp -r models/v10_base models/v10_test
# edit models/v10_test/config.json
python3 backtest.py --model v10_test --from 2026-01-01 --to 2026-05-28

# 5. Compare models in the UI
# → Dashboard: switch model dropdown to v10_test
# → Model page: inspect features and parameters
```

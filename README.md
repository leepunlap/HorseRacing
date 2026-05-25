# 馬場分析 — Horse Racing Analytics Platform

A self-hosted Hong Kong Jockey Club (HKJC) horse racing analytics system. Scrapes race results, engineers 44 features per horse, runs XGBoost walk-forward predictions across multiple named strategies, tracks bet P&L, and serves a Traditional Chinese web dashboard with live progress streaming.

---

## Table of Contents

1. [What This Is](#what-this-is)
2. [Quick Start](#quick-start)
3. [System Overview](#system-overview)
4. [Documentation Map](#documentation-map)
5. [File Layout](#file-layout)
6. [Key Concepts](#key-concepts)
7. [Common Workflows](#common-workflows)
8. [Tech Stack](#tech-stack)
9. [Extension Points](#extension-points)
10. [Working Conventions](#working-conventions-for-future-contributors)

---

## What This Is

A complete pipeline for HKJC horse-race prediction:

| Component | Role |
|-----------|------|
| **scrape_results.py** | Pulls race results from HKJC web pages (Playwright + BS4) into SQLite |
| **backtest.py** | Walk-forward XGBoost engine: trains on pre-date data, predicts target date |
| **model_config.py** | Schema + loader for the 44-feature catalogue and per-strategy configs |
| **app.py** | FastAPI backend: serves predictions, models list, real-time WebSocket progress |
| **static/index.html** | SPA dashboard (Bootstrap 5): strategy switcher, race tables, run-prediction button |
| **models/{策略}/** | One folder per strategy variant with its config + cached results |

The system is **multi-strategy**: each named strategy in `models/` has its own config and result history. You can switch strategies in the UI dropdown to compare predictions and ROI side by side.

---

## Quick Start

```bash
cd /var/www/horseracing

# 1. Start the server (defaults to port 8005, password 168888)
python3 app.py --port 8005
# or with uvicorn directly:
uvicorn app:app --host 0.0.0.0 --port 8005

# 2. Open the dashboard
#    http://<server>:8005/    → password: 168888

# 3. (optional) Scrape latest race results
python3 scrape_results.py --from 2026-05-01 --to 2026-05-25

# 4. (optional) Generate predictions for a date
python3 backtest.py --model 均衡基礎策略 2026-05-24

# 5. Or trigger the backtest from the UI:
#    Dashboard → select strategy + date → click "執行回測"
#    The WebSocket streams live progress; predictions appear when done.
```

---

## System Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            DATA PIPELINE                                │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│   HKJC web pages                                                        │
│         │                                                               │
│         │ scrape_results.py (Playwright)                                │
│         ▼                                                               │
│   ┌──────────────┐         ┌──────────────────────────┐                 │
│   │ data/        │         │ predictions/{date}/      │                 │
│   │   racing.db  │◄────────│   result_*.html (raw)    │                 │
│   │  (SQLite)    │         │   racecard_parsed.json   │                 │
│   └──────┬───────┘         └──────────────────────────┘                 │
│          │                                                              │
│          │ backtest.py (XGBoost walk-forward)                           │
│          ▼                                                              │
│   ┌──────────────────────────────────────┐                              │
│   │ models/{strategy}/                    │                             │
│   │   config.json                         │                             │
│   │   results/{date}/predictions.json     │  ← canonical output         │
│   │   results/summary.json                │  ← top-1 accuracy, ROI      │
│   └──────────┬───────────────────────────┘                              │
│              │                                                          │
│              │ app.py (FastAPI) reads + merges                          │
│              ▼                                                          │
│   ┌──────────────────────────────────────┐                              │
│   │ HTTP API                              │  WebSocket                  │
│   │   /api/dates       list dates         │  /ws/progress               │
│   │   /api/races/{d}   merged race view   │    ↓ live backtest logs     │
│   │   /api/models      strategy catalog   │                             │
│   │   /api/run         spawn backtest     │                             │
│   └──────────┬───────────────────────────┘                              │
│              │                                                          │
│              │ static/index.html (SPA)                                  │
│              ▼                                                          │
│   ┌──────────────────────────────────────┐                              │
│   │ Browser dashboard                     │                             │
│   │  ┌─────────────────────────────────┐  │                             │
│   │  │ [strategy ▾] [date ▾] [scrape⏱] │  │                             │
│   │  │ ⚠ Banner if no predictions       │  │                             │
│   │  │ [▶ run] live log box (if run)    │  │                             │
│   │  │ R1 R2 R3 R4 ... (race tabs)      │  │                             │
│   │  │ Horse table with predictions     │  │                             │
│   │  │ Per-race bet analysis            │  │                             │
│   │  └─────────────────────────────────┘  │                             │
│   └──────────────────────────────────────┘                              │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Documentation Map

Start with this README for orientation. Then dive into the relevant guide:

| Document | Audience | Contents |
|----------|----------|----------|
| **README.md** *(this file)* | Everyone | System overview, doc index, conventions |
| **[USAGE.md](USAGE.md)** | Operators | CLI commands, dashboard walkthrough, API reference |
| **[ARCHITECTURE.md](ARCHITECTURE.md)** | Developers / AI agents | Strategy vs parameter tuning, config schema, code/config boundary |
| **[ADVISORY.md](ADVISORY.md)** | Quant researchers | Cold-start mathematics, Bayesian shrinkage, Harville formula for exotics (Q/QP) |
| **[STRATEGIES.md](STRATEGIES.md)** | Strategy designers | Catalogue of 10 strategy variants with hypothesis, parameters, execution steps |

**Reading order for a new contributor:**
1. This README (orientation + conventions)
2. USAGE.md (how to operate the system)
3. ARCHITECTURE.md (when does code change vs config change)
4. STRATEGIES.md (existing variants + how to add new ones)
5. ADVISORY.md (deeper math: cold-start handling, exotic bet types)

---

## File Layout

```
/var/www/horseracing/
│
├── README.md                 ← this file
├── USAGE.md                  ← CLI + dashboard + API
├── ARCHITECTURE.md           ← strategy vs tuning, config schema
├── ADVISORY.md               ← cold-start math, Harville Q formula
├── STRATEGIES.md             ← 10 strategy recipes
│
├── app.py                    ← FastAPI backend (HTTP + WebSocket)
├── backtest.py               ← Walk-forward XGBoost engine
├── scrape_results.py         ← HKJC results scraper (Playwright)
├── model_config.py           ← 44-feature catalogue + config loader
├── db.py                     ← (legacy SQLite helpers)
│
├── data/
│   ├── racing.db             ← SQLite: races, results, horses, sectionals
│   ├── hkjc_all_results_CN.csv         ← historical results CSV
│   ├── hkjc_race_meta_CN.csv           ← race metadata
│   ├── hkjc_sectionals_CN.csv          ← sectional times (for pace analysis)
│   ├── hkjc_horse_profiles_CN.csv      ← per-horse demographics
│   └── hkjc_horse_race_history_CN.csv  ← per-horse race history
│
├── models/                   ← ONE DIRECTORY PER NAMED STRATEGY
│   ├── 均衡基礎策略/         ← baseline (active)
│   │   ├── config.json       ← all tunable parameters
│   │   └── results/
│   │       ├── summary.json  ← top1_pct, ROI, units_net, …
│   │       └── {date}/
│   │           └── predictions.json
│   ├── 穩健保守策略/         ← conservative variant
│   ├── 深度推算策略/         ← deep XGBoost variant
│   ├── 步速主導策略/         ← pace-led variant
│   ├── 黑馬獵手策略/         ← longshot hunter
│   ├── 熱門過濾策略/         ← favourite filter
│   ├── 純技術指標策略/       ← no pair-features
│   ├── 大樣本信任策略/       ← weakened shrinkage
│   └── 強平滑策略/           ← strong shrinkage
│
├── predictions/              ← per-date scrape artifacts + production output
│   └── {YYYY-MM-DD}/
│       ├── racecard_parsed.json   ← (currently a stub `{}`)
│       ├── result_ST_R01_2026-05-03.html
│       ├── …
│       └── predictions.json       ← published from active model (via --publish)
│
└── static/
    └── index.html            ← single-page app (Bootstrap 5 + vanilla JS)
```

---

## Key Concepts

### 1. Walk-forward training (no look-ahead)

To predict date **D**, the engine trains on ALL races with date `< D`. It then re-trains for date **D+1**, **D+2**, etc. This guarantees no temporal data leakage. The trade-off: training cost is paid per target date (~55 seconds for our dataset).

### 2. Strategy vs Parameter Tuning

The single most important distinction in the codebase. See **ARCHITECTURE.md** for the full rule book.

| Change | Type |
|--------|------|
| Replace XGBoost with LightGBM | **New strategy** (code change needed) |
| Tighten `max_depth` from 5 to 3 | **Parameter tuning** (config change only) |
| Disable `chri_score` feature | **Parameter tuning** (`features_disabled` list) |
| Add a new feature category | **New strategy** (code change needed) |

If you can change it by editing `config.json`, it's tuning. Anything else is a new strategy.

### 3. Bayesian Shrinkage (cold-start fix)

New horses, jockeys, and combinations no longer score `0.0` (which the model misreads as "chronic loser"). Instead:

```
smoothed_rate = (wins + α × prior) / (races + α)
```

Priors are hierarchical: new horses borrow their trainer's rate, pairs borrow the geometric mean of the individuals' rates, etc. All α values are tunable per-strategy in the `shrinkage` config block.

See **ADVISORY.md §1** for the full mathematical treatment.

### 4. Multi-strategy comparison

Each `models/{strategy}/config.json` defines one named strategy. The 9 strategies currently shipping test different hypotheses:

- 均衡基礎策略 — baseline control
- 穩健保守策略 — shallow trees, low variance, stable ROI
- 深度推算策略 — deeper trees, captures subtle interactions, higher overfit risk
- 步速主導策略 — pace × draw matters more than raw rates
- 黑馬獵手策略 — only bet 6-15x odds, edge ≥ 1.4
- 熱門過濾策略 — only bet 1.5-6.5x odds with model+market agreement
- 純技術指標策略 — disable pair features to test if they leak
- 大樣本信任策略 — weakened shrinkage (trust observed data)
- 強平滑策略 — strong shrinkage (trust priors)

See **STRATEGIES.md** for the full recipe per variant, including hypothesis, config diff, expected behaviour, and exact CLI execution steps.

### 5. The 44-feature schema

Defined once in `model_config.py:FEATURES`. Twelve categories: Horse Profile, Win Rates, Adaptability, Trainer Form, Draw, Weight, Race Context, Form, Gear, Pace, Composite, Interactions.

Each feature has `name`, `category`, `description`, `tunable` (whether the feature's behaviour can be tuned via config). The model card in the UI renders this catalogue with current status (enabled/disabled per strategy).

### 6. Bet edge

```
edge = win_probability × decimal_odds
```

- `edge > 1.0` → positive expected value
- `edge > 1.3` → strong play (UI recommends $200 stake)
- `edge > 0.9` → marginal (UI recommends $100 stake)

The threshold for placing bets is in `bet_edge_threshold` (config). Strategies can also set `bet_min_odds` / `bet_max_odds` to filter on the price band (longshot-hunter vs favourite-filter).

### 7. Self-describing predictions.json

Every prediction file embeds metadata:

```json
{
  "_model": "均衡基礎策略",
  "_version": "1.1",
  "_strategy_type": "xgb_walkforward",
  "_generated_at": "2026-05-25T09:21:35",
  "_feature_cols": [...],
  "_feature_weights": {...},
  "01": { ...race 1... },
  "02": { ...race 2... }
}
```

A downstream consumer can reconstruct exactly what produced the file.

### 8. Live progress streaming

When a backtest runs (either via CLI or via the UI "執行回測" button), the FastAPI server spawns `python3 -u backtest.py …` as a subprocess and pipes its stdout. Every line is broadcast over `/ws/progress` as a JSON event:

```
{type:"start",  model:"...", date:"..."}
{type:"log",    text:"  2026-05-03 [DB]: 11 races..."}
{type:"done",   code:0,      model:"...", date:"..."}
```

The UI shows a terminal-style log box during the run and auto-reloads the dashboard when complete.

---

## Common Workflows

### Scrape latest race results

```bash
python3 scrape_results.py --from 2026-05-01 --to 2026-05-25
```

Auto-detects Sha Tin vs Happy Valley. Upserts into the `races` and `results` tables. Creates a stub `predictions/{date}/racecard_parsed.json` so the date appears in the dashboard.

### Run a backtest for one strategy

CLI:
```bash
python3 backtest.py --model 均衡基礎策略 --all          # all historical dates
python3 backtest.py --model 均衡基礎策略 --from 2026-05-01 --to 2026-05-25
python3 backtest.py --model 均衡基礎策略 2026-05-24      # single date
```

UI: open dashboard, pick strategy + date, click "執行回測". Progress streams live.

### Compare strategy ROIs

```bash
python3 -c "
from model_config import list_models
print(f'{\"策略\":25s}  {\"命中率\":>8s}  {\"下注\":>6s}  {\"ROI\":>10s}')
print('─' * 60)
for m in list_models():
    s = m.get('_summary', {})
    print(f\"{m['name']:25s}  {s.get('top1_pct',0):>7.1f}%  \"
          f\"{s.get('bets_placed',0):>6d}  {s.get('roi_units',0):>+9.2f}u\")
"
```

Or in the UI: navigate to 模型 page; each strategy card shows top-1 accuracy + ROI badge.

### Create a new strategy variant

```bash
cp -r models/均衡基礎策略 models/我的新策略
```

Edit `models/我的新策略/config.json`:
- Change `name` to `"我的新策略"`
- Set `parent` to `"均衡基礎策略"`
- Update `version` to `"1.0"`
- Add a `notes` field explaining the change
- Set `active` to `false` (don't auto-activate)
- Tweak the parameters you want to test

Then backtest:
```bash
python3 backtest.py --model 我的新策略 --all
```

See **STRATEGIES.md** for full recipe templates and tested variants.

### Set the active model (production)

The "active" model is what the UI shows by default and what the convenience exports in `model_config.py` resolve to:

```bash
python3 -c "from model_config import set_active_model; set_active_model('均衡基礎策略')"
```

### Publish predictions to `predictions/` (production output)

```bash
python3 backtest.py --model {strategy} --all --publish
```

Copies the strategy's `models/{name}/results/{date}/predictions.json` to `predictions/{date}/predictions.json` (the production location read by the legacy code path).

---

## Tech Stack

| Layer | Technology | Why |
|-------|-----------|-----|
| Scraper | Playwright (async Chromium) + BeautifulSoup4 | HKJC pages are server-rendered with anti-bot measures; Playwright handles JS + cookies cleanly |
| Storage | SQLite (`data/racing.db`) + CSV exports | Single-file DB, no server needed; CSVs let pandas slice fast |
| ML | XGBoost (`binary:logistic`) | Fast, robust on tabular features, importance scoring built in |
| Engineering | pandas + numpy + Python `defaultdict` | Standard tabular toolkit; defaultdict makes win-rate accumulation clean |
| Backend | FastAPI + uvicorn + asyncio + native WebSocket | Async subprocess streaming is trivial; no Django overhead |
| Frontend | Vanilla JS + Bootstrap 5 (CDN) + ECharts (CDN) | No build step; everything in one HTML file; loads instantly |
| Live updates | Native WebSocket (FastAPI `/ws/...`) | Lighter than socket.io for a single-server app |

---

## Extension Points

### Add a new feature

1. Add it to `FEATURES` in `model_config.py` (name, category, description, tunable).
2. Compute it in `build_horse_features()` in `backtest.py` under the matching category section.
3. The trainer will pick it up automatically — it reads `FEATURE_COLS` dynamically.

### Add a new strategy variant (no code)

Just copy a config — see "Create a new strategy variant" above.

### Add a new strategy *type* (algorithm change — requires code)

Currently only `strategy_type: "xgb_walkforward"` exists. To add LightGBM, neural nets, etc.:

1. Write `backtest_{type}.py` exposing a `run_date()` function with the same signature.
2. Dispatch on `cfg['strategy_type']` in `backtest.py:main()`.
3. New configs use `"strategy_type": "{type}"` to opt in.

### Add a new bet type (Q/QP/Place)

Currently bet P&L tracks Win bets only. To add Quinella (Q):

1. After `_normalise_per_race`, compute `q_prob[i,j]` for all pairs using Harville (see ADVISORY.md §2).
2. Scrape Q market dividends in `scrape_results.py` (new endpoint, new DB column).
3. Add Q-bet P&L tracking in `_tally_race`.
4. Surface in UI as a separate "連贏" panel per race.

### Add filters (course-specific, distance-specific strategies)

Add `train_filter` / `predict_filter` blocks to config schema:
```json
"train_filter":   {"course": "HV", "distance_min": 1000, "distance_max": 1200},
"predict_filter": {"course": "HV"}
```

Honour them in `_engineer_features()` before passing rows to `build_features()`. See STRATEGIES.md Part G for the full plan.

---

## Working Conventions (for future contributors)

These are non-negotiable in this codebase. Violating them creates bugs.

### Conventions

1. **Strategy names are Traditional Chinese.** `均衡基礎策略` not `balanced_baseline`. Folder names, CLI args, API params — all Chinese. Modern Linux + Python + browsers handle UTF-8 paths fine.

2. **No look-ahead.** Code that reads `res_csv[res_csv['Date'] < target_date]` is correct. Code that uses `<=` is a bug. The `target_date` row itself must be EXCLUDED from training.

3. **All tunables in config.json.** If you find yourself hard-coding a number in `backtest.py`, ask whether it should live in the config block instead. The named `DEFAULT_*` constants at the top of `backtest.py` are fallbacks for when configs are silent, not the canonical values.

4. **Embed metadata in outputs.** Every `predictions.json` and `summary.json` carries `_model` / `_version` / `_strategy_type` / `_generated_at`. Don't write outputs without these.

5. **Bayesian shrinkage everywhere.** When you add a new rate-style feature (e.g. `jockey_at_hv_wr`), use `smoothed()` with an appropriate prior — never raw `wins/races`. See `compute_win_rates()` and the win-rate block in `build_horse_features()`.

6. **One job at a time.** The `/api/run` endpoint rejects concurrent jobs (returns 409). Don't introduce parallel job queues — backtests pin a CPU core for ~minutes; concurrent runs would thrash.

### Anti-patterns to avoid

- ❌ Adding a `if model_name == "X": ...` branch in `backtest.py`. Use config flags or a new `strategy_type` instead.
- ❌ Storing per-strategy data outside `models/{name}/`. Everything strategy-specific goes in that folder.
- ❌ Adding a new threshold as a Python global. Put it in `config.json` and read via `cfg.get(...)`.
- ❌ Caching state in `app.py` globals across requests. Only `current_run` and `progress_broadcast` are exempt (transient run state).
- ❌ Calling out to subprocesses without `python -u` / `PYTHONUNBUFFERED=1`. The UI relies on live stdout streaming.

### When in doubt

- **Reading conventions:** check existing similar code in `backtest.py` or `app.py`.
- **Reading math:** ADVISORY.md has the Bayesian and Harville derivations.
- **Reading strategy design:** STRATEGIES.md shows worked examples.
- **Architectural questions:** ARCHITECTURE.md states the strategy-vs-tuning rule.

---

## License & Attribution

This codebase is a private analytics platform for HKJC race data. All race data belongs to HKJC; the code itself has no public licence attached. Authored interactively with Claude Sonnet 4.6 / Opus 4.7.

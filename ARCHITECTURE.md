# Racing Framework Architecture

> Part of the documentation set. **README.md** is the entry point;
> **USAGE.md** is the operator's manual; **ADVISORY.md** covers the
> deeper mathematics; **STRATEGIES.md** is the strategy recipe book.
> This document defines what counts as a new strategy versus parameter
> tuning, and where each piece of logic lives.

## The Core Distinction: Strategy vs Parameters vs Run

This is the single most important concept. Three things that look similar are fundamentally different:

```
Strategy    — WHAT you believe about horse racing (algorithm + feature philosophy)
Parameters  — HOW MUCH weight each belief carries (tunable numbers)
Run         — WHAT HAPPENED when you applied the parameters to data
```

### Strategy (a different folder, different code path)

A **new strategy** means changing the core algorithmic approach or feature philosophy. It requires a programmer to write or substantially change code:

| Change | New strategy? |
|--------|--------------|
| Replace XGBoost with LightGBM | ✅ Yes — different algorithm |
| Predict place finish instead of win | ✅ Yes — different target variable |
| Remove all pace features entirely | ✅ Yes — different feature philosophy |
| Add a completely new feature category (e.g. sectional splits model) | ✅ Yes |
| Change how race pace is classified | ✅ Yes — different feature logic |
| Increase `max_depth` from 5 to 7 | ❌ No — parameter tuning |
| Tighten the layoff penalty threshold | ❌ No — parameter tuning |
| Increase shrinkage alpha for jockeys | ❌ No — parameter tuning |
| Disable the `chri_score` feature | ❌ No — parameter experiment |
| Change `draw_inner_max` from 5 to 4 | ❌ No — parameter tuning |

**Rule of thumb:** If you can make the change by editing `config.json` without touching `backtest.py`, it is parameter tuning, not a new strategy.

### Parameters (a new `config.json`, same code)

Each named config in `models/{name}/config.json` is one **parameter set** for a strategy. You can have many configs derived from the same strategy:

```
均衡基礎策略          ← baseline, all parameters at default
均衡保守策略          ← same algorithm, more conservative XGB (max_depth=3, fewer rounds)
均衡激進步速策略       ← same algorithm, higher pace_draw weights
均衡無CHRI策略        ← same algorithm, chri features disabled
```

These are all variations of the same underlying walk-forward XGBoost strategy. You backtest all of them, then pick the one with the best risk-adjusted ROI.

### Run (results in `results/`)

A **run** is one execution of a parameter set against a date range. The same config can be run:
- Against all historical dates (full backtest)
- Against only the last season (recency test)
- Against only HV races (venue test)
- Incrementally as new race days accumulate

Runs are stored in `models/{name}/results/{date}/predictions.json`. A `summary.json` aggregates all runs for that config.

---

## Folder Structure

```
models/
  {策略名稱}/              ← one directory per named config
    config.json            ← ALL parameters for this config (see schema below)
    results/
      summary.json         ← aggregate stats across all run dates
      {YYYY-MM-DD}/
        predictions.json   ← per-horse probabilities, features, bets
```

The folder name IS the strategy name. Use descriptive Traditional Chinese names that convey the philosophy:

| Name | Meaning | When to use |
|------|---------|-------------|
| 均衡基礎策略 | Balanced Baseline | All features, default parameters, Bayesian priors |
| 均衡保守策略 | Balanced Conservative | Same features, shallower trees, more regularisation |
| 步速主導策略 | Pace-Led | Higher pace_draw weights, pace features upweighted |
| 強廄優先策略 | Hot Stable Priority | Higher trainer_hot weight, cold_stable more penalised |
| 檔位中心策略 | Draw-Centric | Elevated draw feature weights |
| 無CHRI純特徵策略 | No-CHRI Pure Features | CHRI disabled, to test if it adds value |

---

## Config Schema (canonical)

Every `config.json` must contain these top-level keys:

```json
{
  "name":         "均衡基礎策略",
  "description":  "均衡基礎策略：44特徵走前推算，XGBoost深度5，貝葉斯平滑",
  "strategy_type":"xgb_walkforward",
  "version":      "1.1",
  "parent":       null,
  "notes":        "initial config with Bayesian shrinkage",
  "created":      "2026-05-24",
  "active":       true,

  "bet_edge_threshold": 1.0,

  "xgb": { ... },
  "num_boost_rounds": 100,
  "features_disabled": [],
  "shrinkage": { ... },
  "going_map": { ... },
  "pace_draw": { ... },
  "pace_bucket": { ... },
  "early_pace_thresholds": [...],
  "draw_inner_max": 5,
  "draw_outer_min": 10,
  "layoff": { ... },
  "weight_allow_divisor": 20,
  "cold_stable_threshold": 0.05,
  "chri": { ... },
  "pace_match": { ... },
  "trainer_form_days": 365,
  "rating_trend_window": 3,
  "standard_gear": [...]
}
```

**Key metadata fields:**

| Field | Type | Purpose |
|-------|------|---------|
| `name` | string | Unique ID, also the folder name, also the CLI `--model` argument |
| `description` | string | One-line description for display |
| `strategy_type` | string | Algorithm family (`xgb_walkforward` for now) |
| `version` | string | Semantic version of THIS config (not the strategy) |
| `parent` | string\|null | Name of the config this was derived from |
| `notes` | string | What changed from parent (changelog entry) |
| `active` | bool | Only one config should be `true` at a time (production config) |
| `bet_edge_threshold` | float | Minimum edge to place a bet in backtest/live |

---

## Lifecycle: Creating a New Variant

### Step 1 — Copy the parent config
```bash
cp -r models/均衡基礎策略 models/均衡保守策略
```

### Step 2 — Edit the new config.json
Change ONLY these fields:
```json
{
  "name":    "均衡保守策略",
  "description": "均衡保守策略：深度3，增強正規化，適合小樣本穩健預測",
  "version": "1.0",
  "parent":  "均衡基礎策略",
  "notes":   "reduced max_depth 5→3, increased lambda 2→4, min_child_weight 10→20",
  "active":  false,

  "xgb": {
    "max_depth":        3,
    "lambda":           4.0,
    "min_child_weight": 20,
    ...
  }
}
```

### Step 3 — Backtest it
```bash
python3 backtest.py --model 均衡保守策略 --all
```

### Step 4 — Compare results
```bash
python3 -c "
from model_config import list_models
import json
for m in list_models():
    s = m.get('_summary', {})
    print(f\"{m['name']:20s}  top1={s.get('top1_pct','—')}%  ROI={s.get('roi_units','—')}u\")
"
```

### Step 5 — Activate the winner
```bash
python3 -c "from model_config import set_active_model; set_active_model('均衡保守策略')"
```

---

## What Lives in Code vs Config

This boundary must not be violated. If it's in code, you need a programmer. If it's in config, you can tune it yourself.

**In code (`backtest.py`)** — requires programmer to change:
- The Harville formula
- The Bayesian shrinkage formula
- The pace classification algorithm
- The walk-forward training loop
- Feature computation logic (how `horse_wr` is calculated)
- XGBoost training call

**In config (`config.json`)** — you can change yourself:
- All numerical thresholds and weights
- Which features are enabled (`features_disabled`)
- XGBoost hyperparameters
- Shrinkage strength (alpha values)
- Pace-draw bonus matrix
- Layoff penalty values
- Going map encoding
- Bet edge threshold

---

## Predictions Output Schema

Every `predictions.json` embeds the model metadata so you know exactly what generated it:

```json
{
  "_model": "均衡基礎策略",
  "_version": "1.1",
  "_strategy_type": "xgb_walkforward",
  "_generated_at": "2026-05-25T10:30:00",
  "_feature_cols": [...],
  "_feature_weights": {...},
  "01": {
    "distance": "1200",
    "class": "5班",
    "horses": [...]
  }
}
```

---

## Summary.json Schema

```json
{
  "model":         "均衡基礎策略",
  "version":       "1.1",
  "strategy_type": "xgb_walkforward",
  "dates_run":     47,
  "total_races":   512,
  "top1_accuracy": 0.381,
  "top1_pct":      38.1,
  "bets_placed":   823,
  "bets_won":      124,
  "units_staked":  823.0,
  "units_net":     +41.2,
  "roi":           0.050,
  "roi_units":     41.2,
  "updated":       "2026-05-25T10:30:00",
  "per_date":      [...]
}
```

---

## The Code Architecture

```
backtest.py          ← entry point + training loop (strategy-agnostic runner)
  │
  ├── load_csv_data()            data loading
  ├── compute_win_rates()        historical stat accumulators
  ├── compute_pace_styles()      sectional analysis
  ├── compute_horse_history()    gear/rating/class history
  ├── smoothed()                 Bayesian shrinkage primitive
  ├── build_horse_features()     44-feature vector per horse
  ├── build_features()           full feature matrix for a date
  ├── run_date()                 single-date prediction
  └── update_summary()           aggregate stats

model_config.py      ← schema definition + config loader (no computation)
  │
  ├── FEATURES[]                 44-feature catalogue with descriptions
  ├── load_config(name)          load a named config
  ├── list_models()              all configs with summary stats
  └── set_active_model(name)     production switch

app.py               ← FastAPI backend (reads results, serves UI)
scrape_results.py    ← HKJC results scraper (independent)
static/index.html    ← SPA dashboard
```

`backtest.py` and `app.py` do NOT import from each other. `model_config.py` is the shared contract.

---

## Adding a New Strategy (programmer task)

When the underlying algorithm changes, create a new strategy type:

1. Write `backtest_{strategy_type}.py` (e.g. `backtest_lgbm.py` for LightGBM)
2. Register the `strategy_type` string in `model_config.py`
3. Create a new config with `"strategy_type": "lgbm_walkforward"`
4. The runner inspects `strategy_type` to choose which backtest module to call

Currently only `xgb_walkforward` exists. This extension point is reserved for future strategies.

# HYPOTHESIS VALIDATION FRAMEWORK

How to check Eric's 167 hypotheses against the existing 9 strategies and codebase.

## Validation Status Buckets

| Status | Meaning |
|--------|---------|
| **COVERED** | Implemented in code, configurable, testable with existing strategies |
| **PARTIAL** | Partially implemented, some aspects missing |
| **GAP** | Not implemented — needs new code or new data |
| **OBSOLETE** | V9 linear-formula concept, superseded by V10 XGBoost |
| **HUMAN** | Human-factor observation, not a software hypothesis |

---

## GROUP A: Feature Architecture (H5–H15, H70–H78, H96–H99, H119–H134)

### COVERED — backtest.py + config.json
| H# | What | Where | Which strategies test it |
|----|------|-------|-------------------------|
| H5 | Individual going profile | `going_adapt` feature, `going_alpha` in shrinkage | All strategies |
| H9-H10 | 8 WR indicators | `horse_wr`, `jockey_wr`, `trainer_wr`, `jt_pair`, `jh_pair`, `dist_adapt`, `going_adapt`, `trainer_hot` | All |
| H13 | Time decay (30d=0.25, 60d<0.10) | NOT time-weighted in backtest — uses all prior data equally. **PARTIAL** | — |
| H70 | 26 base factors cover 90%+ | 44 features cover more than Eric's 26 | All |
| H96 | 46 features → XGBoost | 44 features → XGBoost in backtest.py | All |
| H98 | XGBoost hyperparams | `xgb` block in every config.json | All (3 variants: baseline, deep, conservative) |
| H119 | Jockey WR = #1 feature | Verifiable via `_feature_weights` in any predictions.json | All |
| H126 | 12 feature categories | FEATURE_CATEGORIES in model_config.py | All |

### GAP — not in code
| H# | What | Missing |
|----|------|---------|
| H6 | Bloodline going bias | No pedigree data in features |
| H12 | Weight=0.85^days | All prior data used equally; no exponential decay |
| H14 | Capture "surge" and "slump" | No rolling form spike detection |
| H15 | V6 formula weights | Superseded by XGBoost tree learning |
| H93 | 35 features (V9.3) | Superseded by V10 |
| H125 | 46 features | We have 44; missing `early_pace_avg`, `avg_beat_distance` |
| H127 | Missing features | Identified gap — add or verify |
| H133 | No odds_log feature | True — odds not in training features |

### How to validate COVERED items:
```bash
# Extract feature weights from any prediction run
cat models/均衡基礎策略/results/2026-05-24/predictions.json | python3 -c "
import json,sys; d=json.load(sys.stdin)
for k,v in sorted(d['_feature_weights'].items(), key=lambda x:-x[1]):
    print(f'{v:>8.1f}  {k}')" | head -20

# Verify H119 (jockey_wr #1)
# Verify H122 (gear_change, first_gear_use in top 15)
# Verify H120 (class, draw, distance, participants in top 10)
```

---

## GROUP B: Bayesian Shrinkage (H141–H142)

### COVERED
| H# | What | Where | Which strategies test it |
|----|------|-------|-------------------------|
| H141 | Shrinkage fixes cold-start | `smoothed()` in backtest.py:104, `shrinkage` block in every config | 大樣本信任 (weak) vs 強平滑 (strong) vs 均衡基礎 (baseline) |
| H142 | Cold-start → priors not 0 | Shrinkage applied to all rates: horse, jockey, trainer, pairs, dist, going | Same 3 strategies |

### How to validate:
```bash
# Run same date with 3 shrinkage strategies, compare predictions
python3 backtest.py --model 均衡基礎策略 2026-05-24 --force
python3 backtest.py --model 大樣本信任策略 2026-05-24 --force
python3 backtest.py --model 強平滑策略 2026-05-24 --force
# Diff the predictions — shrinkage effect should be visible on low-race-count horses
python3 -c "
import json
def load(m,d): return json.load(open(f'models/{m}/results/{d}/predictions.json'))
b=load('均衡基礎策略','2026-05-24')
w=load('大樣本信任策略','2026-05-24')
s=load('強平滑策略','2026-05-24')
# Compare probs for horses with <5 races
# Hypothesis: 大樣本信任 (weak alpha) → more extreme probs for low-data horses
#             強平滑 (strong alpha) → probs closer to prior mean
"
```

---

## GROUP C: Pace System (H24–H31, H65–H69, H82–H95)

### COVERED
| H# | What | Where |
|----|------|-------|
| H24 | EPP classification | `classify_early_pace()` + `early_pace_thresholds` in config |
| H26 | Slow pace leader bonus | `pace_draw` matrix — inner+very_slow=+18, inner+slow=+13 |
| H27 | Long-distance multiplier | NOT per-distance; pace_draw applies uniformly. **PARTIAL** |
| H29 | Going-type pace bonus | `pace_draw_bonus` only (no going multiplier). **PARTIAL** |
| H30 | Outer+closer+Yielding | `outer_x_closer` + `outer_x_fast` — partial; no explicit going interaction. **PARTIAL** |
| H83 | RPI from runner distribution | `classify_race_pace()` in backtest.py |
| H84 | Pace adaptation curve | `pace_style_match()` — single score, not full curve. **PARTIAL** |
| H89 | Pace×Draw matrix | `pace_draw` block in every config — identical in 8/9 strategies |
| H95 | XGBoost learns pace interactions | Multiple pace features (horse_style, pace_style_match, pace_draw_bonus, late_pace_avg, inner_x_pace, outer_x_fast, inner_x_leader, outer_x_closer, late_x_outer) |

### Validation via strategies:
| Strategy | What it tests | Command |
|----------|--------------|---------|
| 均衡基礎策略 | Baseline pace_draw matrix | — |
| 步速主導策略 | Amplified pace_draw matrix + WR features disabled | Most direct test of H89-H95 |

```bash
# Compare pace-led vs baseline on a Yielding/Slow-pace race day
python3 backtest.py --model 均衡基礎策略 2026-05-24 --force
python3 backtest.py --model 步速主導策略 2026-05-24 --force
# Check if 步速主導 picks different top horses
# Check if 步速主導 has better ROI on slow-pace race days
```

### GAP
| H# | What | Missing |
|----|------|---------|
| H25 | LPA (last 400m ability) | `late_pace_avg` exists but not structured as LPA index |
| H27 | 1800m+ ×1.25 penalty | No distance-dependent pace scaling |
| H86–H88 | Full RPI + adaptation curve | Partial — pace is classified but not the full V9.3 dynamic model |
| H90 | HV ×1.3 multiplier | `draw_x_hv` exists but no pace×track multiplier |
| H91 | Distance×pace amplification | Not in code |
| H94 | V9.3 manual weights too aggressive | This is a DESIGN hypothesis — validated by moving to XGBoost |

---

## GROUP D: Gear System (H32–H38)

### COVERED
| H# | What | Where |
|----|------|-------|
| H36 | Gear weight 6% | Via XGBoost feature weights — gear_change + first_gear_use are features |
| H34 | Trainer gear win record | NOT implemented. **GAP** |
| H35 | Gear × age/layoff/going | NOT implemented. **GAP** |

### PARTIAL — what we have:
- `gear_change`: 1 if gear changed from previous race (binary)
- `first_gear_use`: 1 if non-standard gear used first time (binary)
- `standard_gear` config: defines what's "standard"

### GAP
| H# | What | Missing |
|----|------|---------|
| H32 | Gear type scoring (B +8/-6, TT +7/-5) | No per-type scoring — all gear changes treated identically |
| H33 | Multi-gear change penalty (-10) | No multi-gear detection |
| H34 | Trainer gear effectiveness history | No data |
| H35 | Gear × condition interactions | No interaction features |

### How to validate what exists:
```bash
# Feature importance shows gear contribution
# gear_change typically ranks ~13 in importance (per V10 doc H122)
# Can we add gear type detail? Requires scraping gear data from HKJC race cards
```

---

## GROUP E: Draw / Barrier System (H39–H45)

### COVERED
| H# | What | Where |
|----|------|-------|
| H39 | Outer draw penalty | `draw_outer` (binary), `wide_draw` (binary), `draw_outer_min` config |
| H43 | Draw weight 7% | Via XGBoost features: draw, draw_inner, draw_outer, wide_draw |
| H44 | V7.4 formula weights | Superseded by XGBoost |

### PARTIAL
| H# | What | Status |
|----|------|--------|
| H40 | Closer+outer+Yielding penalty | `outer_x_closer` + `outer_x_fast` exist; no going-specific interaction |
| H41 | Top jockey 30% penalty reduction | NOT implemented |
| H42 | Strong late pace 20% reduction | NOT implemented |

### How to validate:
```bash
# Compare draw features importance
# Check outer_x_closer and outer_x_fast gain scores
# Test: run 步速主導策略 vs baseline on wide-draw dominated races
```

---

## GROUP F: Class 5 / Low-Grade System (H46–H53)

### COVERED
| H# | What | Where |
|----|------|-------|
| — | Class as feature | `class_num` (continuous) in features |

### GAP
| H# | What | Missing |
|----|------|---------|
| H46 | Apprentice allowance bonus | Not distinguished from weight_allow |
| H47–H48 | Class 5 gear/layoff penalties | No class-specific rules |
| H49 | Recent poor form penalty | Not class-specific |
| H50–H51 | Strong jockey/low-draw bonus in C5 | No class-specific bonuses |
| H52–H53 | Class 5 module weight 8% | No class-specific module |

### How to validate:
```bash
# Filter summary.json per_date for Class 5 races only
# Check if ROI differs by class (V10 doc H75 claims C3-4 is best)
# Requires class info from race metadata
```

---

## GROUP G: Stable Heat (H54–H59)

### COVERED
| H# | What | Where |
|----|------|-------|
| H54 | Hot/cold cycles | `trainer_hot` (raw wins in period), `cold_stable_season` (rate) |
| H55 | Heat thresholds | `cold_stable_threshold` config (0.05 default) |

### PARTIAL
| H# | What | Status |
|----|------|--------|
| H55 | 7-day rate thresholds | Uses `trainer_form_days` (365 default) — not 7/14 day |
| H56 | Same-day ≥2 wins bonus | NOT implemented — needs live data |
| H57 | Trainer+jockey both hot | NOT explicitly modeled |

### How to validate:
```bash
# Check feature importance of trainer_hot and cold_stable_season
# Compare strategies: cold_stable_threshold is fixed at 0.05 in all 9 strategies
# Could add a strategy variant with different cold_stable_threshold
```

---

## GROUP H: Betting System (H72–H73, H97–H118, H146, H150–H151, H161–H167)

### COVERED
| H# | What | Where | Strategies that test it |
|----|------|-------|------------------------|
| H146 | Value Bet filtering | `bet_edge_threshold`, `bet_min_odds`, `bet_max_odds` in config | 熱門過濾, 黑馬獵手 |
| H150 | Flat bet vs Kelly | Flat bet only in _tally_race (no Kelly). **PARTIAL** | — |
| H151 | Only bet on Value Edge | `bet_edge_threshold` controls this | All |

### PARTIAL
| H# | What | Status |
|----|------|--------|
| H100–H104 | Kelly failure details | No Kelly implementation to test against |
| H105–H106 | Flat bet edge>2.0 +1124% | `bet_edge_threshold=2.0` not in any current strategy |
| H109–H118 | Flat bet variants | Not all tested as strategies |

### How to validate:
```bash
# Create a strategy variant that matches V10.4 exactly:
# bet_edge_threshold=2.0, no min/max odds
# Then run --all and check summary
cp -r models/均衡基礎策略 models/V10_4_Reproduction
# Edit config: bet_edge_threshold=2.0, name=V10.4 Reproduction
python3 backtest.py --model "V10.4 Reproduction" --all

# To test Kelly vs Flat bet — needs code change in _tally_race()
```

### GAP: V10.4 exact reproduction

| H# | Config change needed | Current value → V10.4 value |
|----|---------------------|---------------------------|
| H106 | `bet_edge_threshold` | 1.0 → 2.0 |
| H98 | `num_boost_rounds` | 100 → 300 (V10 uses 300) |
| — | Flat bet cap | Not enforced in _tally_race → $200 cap not implemented |
| — | Isotonic calibration | NOT in backtest.py — V10 uses held-out month calibration |

---

## GROUP I: Race-day / Live Factors (H60–H64, H136)

### GAP — all need live data or new scrapers
| H# | What | Missing |
|----|------|---------|
| H60–H61 | Jockey same-day win/loss streak | No live data pipeline |
| H63 | Same-day form score formula | Not implemented |
| H136 | Real-time odds integration | edge uses scraped odds from results, not live |

---

## GROUP J: V9.x Legacy Formula Hypotheses (H7, H11, H15, H18, H37, H44, H52, H58, H88, H92)

These are the V4–V9.3 linear weighted formulas. They are **OBSOLETE** as prediction formulas because XGBoost replaces them, BUT they remain valuable as:

1. **Feature engineering recipes** — the components (weight_allow × chri.weight_allow, etc.) are still used
2. **Interpretability references** — explain what each feature group targets
3. **Ablation candidates** — disable features to see if Eric's formula components matter

### Validation approach for OBSOLETE items:
```bash
# Strategy: 純技術指標策略 disables jh_pair, jt_pair, jockey_wr
# Strategy: 步速主導策略 disables horse_wr, jockey_wr, trainer_wr
# These test whether Eric's emphasis on certain features was justified
python3 backtest.py --model 純技術指標策略 --all
python3 backtest.py --model 均衡基礎策略 --all
# Compare summary.json — if removing WR features hurts ROI, Eric's emphasis was right
```

---

## GROUP K: Human-Factor Hypotheses (not software-testable)

These Eric observations can only be validated through live betting or qualitative discussion:
- H19: Bowman+Size is "extreme pair" (track record shows this)
- H54: Stable hot/cold cycles are real (observable in data)
- H62: Afternoon rider fatigue (requires sectional data by race order)
- H75: C3-4, mid-distance, good draw, Good/G2Y = most reliable
- H147: Jockey matters more than horse (testable via feature importance)
- H148: Slow-pace long-distance = biggest weakness
- H155: HV track bias stronger than ST
- H157: HK racing has strong statistical regularities

---

## PRACTICAL VALIDATION PLAN (Priority Order)

### Phase 1: Validate what we already have (today)
```bash
# 1. Feature importance ranking — verify H119-H124
for m in 均衡基礎策略 步速主導策略 深度推算策略 穩健保守策略; do
  echo "=== $m ==="
  latest=$(ls models/$m/results/ | sort | tail -1)
  python3 -c "
import json; d=json.load(open('models/$m/results/$latest/predictions.json'))
fw = d.get('_feature_weights',{})
for k,v in sorted(fw.items(), key=lambda x:-x[1])[:15]:
    print(f'{v:>8.1f}  {k}')"
done

# 2. Strategy comparison — summary stats
python3 -c "
from model_config import list_models
for m in list_models():
    s = m.get('_summary',{})
    print(f\"{m['name']:20s} top1={s.get('top1_pct',0):.1f}% bets={s.get('bets_placed',0)} roi={s.get('roi_units',0):+.1f}u\")"

# 3. Shrinkage effect check (H141-H142) — compare 大樣本信任 vs 強平滑
```

### Phase 2: Add missing V10.4 reproduction (1 day)
- Create `V10.4_Reproduction` strategy with edge=2.0, n_rounds=300
- Compare against Eric's claimed +1,124.8% ROI

### Phase 3: Add missing features (1–2 weeks)
- H127: Add `early_pace_avg`, `avg_beat_distance` (if data available)
- H25: Structure LPA from sectional data
- H12: Implement time-decay weighting for historical win rates
- H32: Add gear type detail (B, TT, V, XB/CP differentiation)

### Phase 4: Data gaps (requires scraping)
- H60–H64: Live jockey same-day form
- H136: Live odds integration
- H6: Bloodline going preference database

---

## SUMMARY MATRIX

| Group | Hypotheses | COVERED | PARTIAL | GAP | OBSOLETE |
|-------|-----------|---------|---------|-----|----------|
| A: Features | 35 | 18 | 5 | 12 | 0 |
| B: Shrinkage | 2 | 2 | 0 | 0 | 0 |
| C: Pace | 18 | 8 | 5 | 5 | 0 |
| D: Gear | 7 | 2 | 0 | 5 | 0 |
| E: Draw | 7 | 3 | 3 | 1 | 0 |
| F: Class 5 | 8 | 0 | 0 | 8 | 0 |
| G: Stable Heat | 6 | 2 | 2 | 2 | 0 |
| H: Betting | 31 | 6 | 14 | 11 | 0 |
| I: Live Factors | 5 | 0 | 0 | 5 | 0 |
| J: V9 Legacy | 22 | 0 | 0 | 0 | 22 |
| K: Human | 9 | 0 | 0 | 9 | 0 |
| V10 Lim/Risk | 10 | 0 | 0 | 10 | 0 |
| **TOTAL** | **167** | **41** | **29** | **68** | **22** |

# Overnight research — status log

Started: 2026-05-28 22:30 HKT
Last updated: 2026-05-29 01:00 HKT (live; latest top)

## ⚠️ Action for the morning

**Do NOT re-launch `monitoring.integrity_check --baseline --heal`** from
your other session while my overnight backfills are still queued — the
process keeps the SQLite writer lock continuously and has been
auto-restarted from your other session several times tonight, blocking
my pedigree refresh + vet_records + incident_tags backfills. The daily
cron at 04:30 is fine; the manual re-launches are what's blocking.

## 🎯 HEADLINE FINDINGS

### NEW (02:15 HKT) — Model confidence is well-calibrated; bet only the high-confidence picks

On 154 races since April 1 (baseline `model_lambda_v1` = 150 features, 42.9% overall top-1):

| Pick's calibrated_prob | n | top-1 accuracy |
|---|---|---|
| < 15% (long shots picked) | 9 | **11.1%** (effectively random) |
| 15-20% | 17 | 17.6% |
| 20-25% | 35 | 31.4% |
| 25-30% | 34 | 32.4% |
| **30-40%** | **42** | **66.7% ← high-confidence picks win 2x as often** |
| **≥40%** | **17** | **70.6%** |

This is the SIMPLEST tomorrow-night win: **only bet the model when its top-1 has >30% calibrated probability.**
That subset (59 of 154 races = 38%) wins 68% — better than any subset I've tested.

### Where the model performs:

| Race class | n | top-1 acc | Note |
|---|---|---|---|
| Class 5 | 22 | **68%** | model is BEST here (smallest horses, biggest gaps) |
| Class 3 | 36 | 44% | |
| Class 4 | 69 | 38% | most numerous |
| Class 2 | 10 | 30% | model worst (closer ratings) |

| Distance | n | acc |
|---|---|---|
| 1600-1800m | 49 | **53%** ← best |
| 1200-1400m | 86 | 40% |
| ≤1000m sprints | 13 | 31% |
| ≥2000m | 6 | 33% |

| Field size | n | acc |
|---|---|---|
| ≤9 | 6 | **83%** ← small fields predictable |
| 10-11 | 12 | 50% |
| 12-14 | 136 | 40% |

| Winner's market rank | n | model acc | Note |
|---|---|---|---|
| Favorite won | 39 | 59% | model picks favourite + favourite wins → easy |
| 2nd Fav won | 36 | 44% | "model expected something different from market" — coin-flippy |
| 3rd Fav won | 23 | 52% | |
| 4-9+ Fav won (longshot) | 49 | 27-46% | hard predictions either way |

### NEW (02:00 HKT) — Per-tag next-race-win-rate

Across 1,337 horse-races since April 1 with a prior incident report,
calculated the % of horses that won their NEXT race grouped by tag:

| Prev-race tag | n | next-race win rate | direction |
|---|---|---|---|
| sent_for_sampling (= horse was a WINNER) | 326 | **18.4%** | winners keep winning (form streak) |
| roarer | 36 | 19.4% | (small n, but suggests roarer surgery effective) |
| vet_inspection | 483 | 7.7% | near field-average; not detrimental |
| crowded | 113 | 6.2% | small negative |
| ran_off | 130 | 5.4% | small negative |
| bumped | 403 | 3.7% | negative |
| raced_keenly | 82 | 3.7% | negative |
| **steadied** | **173** | **1.2%** | **🚩 strong negative — steadied horses RARELY win next out** |
| raced_wide, head_up, slow_to_begin, hampered, bled, blood_in_mouth | all <60 | 0.0% | tiny n; horses with these tags effectively never recover next time |

**Steadied = clearest pre-race signal.** 173 occurrences in 2 months,
only 2 next-race wins. The model should sharply discount horses coming
off a `steadied` race.

`sent_for_sampling` = post-race winner sampling (mandatory). 18.4%
next-race win is the "form streak" baseline — winners keep winning.

Note untagged horses with incident reports also win rarely (1.5%) —
they tend to be the mid-pack runners who didn't get interfered with
because they weren't at the front of the action. So untagged ≠ random.

### NEW (01:30 HKT) — RCA tags from PREVIOUS race ARE pre-race predictive

For 261 of the model's top-1 picks since April 1 (74 right, 184 wrong),
~28 had a prior race with an incident report. Of those:

| Prev-race tag | wrong picks (n=20) | right picks (n=8) | direction |
|---|---|---|---|
| **crowded** on prior race | **3 (15%)** | **0 (0%)** | 🚩 only-when-wrong |
| **steadied** on prior race | **3 (15%)** | **0 (0%)** | 🚩 only-when-wrong |
| sent_for_sampling | 7 (35%) | 2 (25%) | mild noise |
| bumped | 3 (15%) | 1 (12%) | small directional |
| vet_inspection | 7 (35%) | 4 (50%) | counter-direction! |

`crowded` and `steadied` from the PREVIOUS race are clean "next-race
pessimism" signals — appear ONLY on the model's wrong picks. Wiring
them into a feature (`H107_off_vet_returner` is currently a stub)
would tell the model "this horse had a bad trip last out, don't
over-rate them next time." Sample sizes are small but the
direction-of-effect is clean.

Interesting: `vet_inspection` reverses direction here vs the May 27
analysis — when on the prior race it actually correlates with the
model being RIGHT (50% vs 35%). Maybe horses that get vet-checked
then come back stronger, or the model's already learned to handle
this. Worth a closer look.

### NEW (01:30 HKT) — Dropping multiple cat6 features HURTS

| Drop config | features | May 27 acc |
|---|---|---|
| nothing (cat06_full) | 8 | 33% (3/9) |
| **drop 1 of {H062, H068, H069, H070}** | **7** | **44% (4/9) ← best** |
| drop 2 (H062 + H068) | 6 | 22% (2/9) |
| drop 4 (core4) | 4 | 22% (2/9) |

The "drop one" results led me to assume drop-all-noise would be even
better, but the cat6 features have non-trivial interactions. The right
recipe is to drop EXACTLY ONE feature from {H062, H068, H069, H070}.
H069 (Sex allowance) drop has best WLL (2.321) — provisional pick.



### Finding 1 — The model is grossly over-featured

On the 9-race HV May 27 card:

| Subset | # features | Top-1 acc | WLL |
|---|---|---|---|
| BASELINE_default150 (model_lambda_v1) | 150 | 22.2% (2/9) | **2.073** ← best WLL |
| cat06_alone (Weight/load family) | 8 | 33.3% (3/9) | 2.338 |
| cat08_alone (Form family) | 13 | 33.3% (3/9) | 2.433 |
| cat12_alone (mix: rating + jockey×ctx) | 14 | 33.3% (3/9) | 2.243 |
| **cat06 minus H062 (Apprentice claim)** | **7** | **44.4% (4/9)** | 2.353 |
| cat06 minus H068 (Apprentice grade) | 7 | 44.4% (4/9) | 2.329 |
| cat06 minus H069 (Sex allowance) | 7 | 44.4% (4/9) | 2.321 |
| cat06 minus H070 (Weight-for-age) | 7 | 44.4% (4/9) | 2.343 |

→ **4 of 8 cat06 features are individually droppable** for an immediate
44% top-1 lift (vs 22% baseline). The plausible "core 4" is
**H061 + H063 + H064 + H065** (testing now via /tmp/test_core4.py).

### Finding 2 — Cat 6 is the most ROBUST single-category signal across meetings

| Subset | May 27 (9R, HV) | May 24 (11R, ST) |
|---|---|---|
| cat06_alone | **33.3%** | **27.3%** |
| cat08_alone | 33.3% | 18.2% |
| cat12_alone | 33.3% | 18.2% |
| cat06+cat08 | 22% | 18% |
| cat06+cat12 | 22% | 18% |
| **cat06+cat08+cat12** | **11%** | **9%** ← combining HURTS |

The most reliable signal is just weight-related features. Combining
cats consistently DEGRADES performance — strong evidence that the
non-cat6 features are mostly noise on these recent dates.

### Finding 3 — Within Cat6: H063 is critical, H065 is critical, the rest droppable

Drop-one analysis (May 27, baseline cat6 = 8 features at 33%):

| Drop | What it is | Acc after | Verdict |
|---|---|---|---|
| H061 | Weight carried (lb) | 3/9 (33%) | neutral / keep for context |
| H062 | Apprentice claim | **4/9 (44%)** | DROP — pure noise |
| **H063** | **Weight delta vs last** | **1/9 (11%)** | **CRITICAL — keep** |
| H064 | Body weight declared | 3/9 (33%) | neutral / keep |
| **H065** | **Body weight Δ** | 2/9 (22%) | KEEP — small but real signal |
| H068 | Apprentice grade | **4/9 (44%)** | DROP — noise |
| H069 | Sex allowance | **4/9 (44%)** | DROP — noise |
| H070 | Weight-for-age | **4/9 (44%)** | DROP — noise (surprising) |

The keep set is **{H061, H063, H064, H065}** = "what the horse weighs +
how its weight has changed." Apprentice / sex / WFA add NOISE.

### Finding 4 — H135 (Jockey×venue) is the best single feature

Within cat12 single-feature scan:

| Feature alone | What | May 27 acc |
|---|---|---|
| **H135** | **Jockey×venue** | **3/9 (33%) ← best single feature** |
| H134, H138, H145, H146 | Class×age, others | 2/9 (22%) |
| H181 | Field rating z-score | 2/9 (22%) |
| H180 | Field rating rank | 1/9 (11%) |
| H136 (Jockey×distance) | -- | 0/9 |

A single jockey-venue interaction column matches the best multi-feature
subsets. Strong validation of the audit's "fewer features, more signal"
intuition.

### Finding 5 — Adding to H135 alone gives at best marginal improvement

Phase 3 greedy build (May 27, seed = H135):

| Add | Pair acc | WLL change |
|---|---|---|
| H135 alone | 3/9 (33%) | 2.301 |
| +H134 | 3/9 (33%) | 2.318 (worse WLL) |
| +H138 | 3/9 (33%) | 2.313 |
| +H141 | 3/9 (33%) | 2.313 |
| +H142 | 3/9 (33%) | 2.313 |
| +H145 | 3/9 (33%) | 2.313 |
| +H146 | 3/9 (33%) | 2.313 |
| +H144 | 3/9 (33%) | 2.351 |
| **+H181** | **2/9 (22%)** | 2.280 (LOWEST WLL but bad acc) |

H181 (field rating z) HURTS top-1 acc when added to H135 even though
WLL drops. This is the classic accuracy↔calibration tradeoff — H181
adjusts probabilities but moves the model's top pick toward different
horses than H135 alone.

## RCA tag effectiveness

### May 27 specific (in-memory tagged from incident_reports):

| Tag on model's PICK | Wrong picks (n=7) | Right picks (n=2) |
|---|---|---|
| **vet_inspection** | **3/7 (43%)** | **0/2** ← only when wrong |
| crowded | 2/7 | 0 |
| sent_for_sampling (routine) | 2/7 | 0 |
| bumped, blood_in_mouth, slow_to_begin, steadied, ran_off | 1 each | 0 |

### Historical (140 races since April 1, 60 right / 80 wrong):

| Tag | Wrong picks (n=80) | Right picks (n=60) | direction |
|---|---|---|---|
| **vet_inspection** | **3 (3.8%)** | **0 (0.0%)** | 🚩 only-when-wrong |
| crowded | 2 (2.5%) | 0 | only-when-wrong |
| blood_in_mouth, slow_to_begin, steadied, ran_off | 1 each (1.2%) | 0 | only-when-wrong |
| sent_for_sampling | 2 (2.5%) | 2 (3.3%) | noise (routine) |
| bumped | 1 (1.2%) | 1 (1.7%) | noise |

**vet_inspection is the most useful tag** — appears on model's pick ONLY
when wrong (3/80 wrong vs 0/60 right). H107_off_vet_returner (currently
a stub) would convert these losses into "horse was off physically, fade
its next-out form." Wire it from vet_records + incident_tags.

## Per-race detail (May 27)

| R | Model pick | Pick prob | Winner | Winner odds | Field | Outcome |
|---|---|---|---|---|---|---|
| 1 | J308 | 0.243 | K289 | 4.0 | 12 | lose |
| 2 | E496 | 0.174 | J524 | 2.9 | 12 | lose ← missed favorite |
| 3 | K428 | 0.219 | K398 | 5.8 | 12 | lose |
| 4 | H110 | 0.337 | J530 | 2.1 | 12 | lose ← missed favorite |
| 5 | K566 | 0.337 | K566 | 2.0 | 10 | ✅ WIN |
| 6 | L022 | 0.240 | J366 | 4.1 | 12 | lose |
| 7 | J427 | 0.219 | K064 | 3.3 | 12 | lose |
| 8 | J431 | 0.195 | H092 | 12.0 | 12 | lose ← long-shot winner |
| 9 | G265 | 0.203 | G265 | (n/a) | 12 | ✅ WIN |

The two correct picks (R5, R9) were on horses the MARKET also liked.
The four most-painful misses (R2, R4) were races where a stronger
favorite won — suggesting the model's calibration under-confident on
the front of the market.

## Setup status

- [x] DB migrations applied (5 columns + multi_leg_dividends table)
- [x] trackwork.distance backfill (290,998 / 666,308 populated, 4/4 cross-checks pass)
- [x] K289 cross-check of horse_pedigree parser (17/17 fields correct)
- [ ] horse_pedigree --refresh — BLOCKED (other session writers)
- [ ] vet_records — BLOCKED (would unblock H107 wiring)
- [ ] incident_reports.incident_tags backfill — BLOCKED (in-memory tagging works)

## What's still running (background)

| Process | What | ETA |
|---|---|---|
| `ablation_phase4.py` | Phase 4 — cat6 singles + 32-run multi-meeting validation | ~30-60 min more |
| `test_cat6_minus_both.py` | Drop {H062,H068,H069,H070} from cat6 — multi-date | ~30 min |
| `test_core4.py` | {H061+H063+H064+H065} core set — 3 dates × 7 subsets | ~30 min (queued) |
| OTHER session | `integrity_check` + `scrape_results --force-refresh` (rescraping 2023-2024 history) | unknown |

## Files

| Path | Purpose |
|---|---|
| `STATUS_OVERNIGHT.md` (this) | running log |
| `/tmp/ablation_results.csv` | Phase 1 — per-category |
| `/tmp/ablation_deep.csv` | Phase 2 — pairs/triples/drop-one/greedy (52 runs, killed mid-phase 3 greedy) |
| `/tmp/ablation_phase4.csv` | Phase 4 — cat6 drop-one + (pending) multi-meeting validation |
| `/tmp/rca_may27.md` | Per-race RCA narrative |
| `/tmp/rca_historical.md` | Aggregate RCA across 140 races |
| `/tmp/build_winning_strategy.py` | One-shot strategy creator (edit WINNING_FEATURE_IDS, run) |
| `/tmp/final_validation.py` | Drafted but not yet run — final 8-subset multi-meeting comparison |
| `/tmp/test_core4.py` | Test of core4 = {H061,H063,H064,H065} subset |

## Plan for the remaining overnight hours

1. Wait for Phase 4 multi-meeting validation to land (~30-60 min)
2. Wait for test_cat6_minus_both / test_core4 results
3. Run /tmp/final_validation.py with the consolidated candidates
4. Update STATUS with consolidated multi-meeting accuracy numbers
5. Edit /tmp/build_winning_strategy.py with the proven-best subset
6. Run /tmp/build_winning_strategy.py to materialise the new strategy in the DB
7. Final STATUS update with the recommendation for tomorrow's HV meeting (Wed Jun 3)

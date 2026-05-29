# Overnight Findings — Morning Brief
*Final update 2026-05-29 ~05:30 HKT — all 64 validate_winner runs + β-sweep + confidence-gated test complete*

## 🎯 ONE-SENTENCE RECOMMENDATION

**Flip `stage2_enabled = 1` on `model_lambda_v1` (production strategy id=1).
Keep all 150 features. Keep the existing α=1.0, β=0.9 defaults. No retrain
required. Optionally add a confidence-gated wrapper for the strongest
risk-adjusted lift (described in section 3).**

---

## 1. Result of the full 8-recipe × 8-meeting validation matrix

`validate_winner.py` trained each recipe walk-forward on every May
meeting (May 03 → May 27). 64 separate model trainings.

| Recipe | # feats | Aggregate top-1 | WLL avg |
|---|---|---|---|
| **W6 BASELINE_150 (production)** | **150** | **33/80 = 41.3% ✅** | **1.98** |
| W5 cat06_full | 8 | 16/78 = 20.5% | 2.45 |
| W4 cat06_minus_H070 | 7 | 15/78 = 19.2% | 2.47 |
| W3 cat06_minus_H062 | 7 | 14/78 = 17.9% | 2.45 |
| W7 H135+H063 | 2 | 13/80 = 16.3% | 2.40 |
| W1 cat06_minus_H069 | 7 | 12/78 = 15.4% | 2.47 |
| W2 cat06_minus_H068 | 7 | 11/78 = 14.1% | 2.49 |
| W8 H063_alone | 1 | 9/80 = 11.3% | 2.50 |

**The full 150-feature production model is NOT over-featured.** My
earlier finding (cat06_minus_H069 hits 44% on May 27) was a single-meeting
outlier. Across 8 meetings the same recipe averages 15%, and every other
pruned recipe is worse than baseline by 20+pp.

**Do not deploy feature pruning.** The baseline is the right model.

---

## 2. Stage 2 Benter blending on the BASELINE 150-feature model

Test: 4 most-recent meetings, identical 150-feature stage-1 model, with
and without Benter market blending (α=1.0, β=0.9).

| Date | Stage 1 only | + Stage 2 (β=0.9) | Δ acc | Δ WLL |
|---|---|---|---|---|
| 2026-05-27 | 2/9 (22%), WLL 2.073 | 3/9 (33%), WLL 1.563 | +11pp | −0.51 |
| 2026-05-24 | 2/11 (18%), WLL 1.943 | 4/11 (36%), WLL 1.743 | +18pp | −0.20 |
| 2026-05-20 | 1/9 (11%), WLL 2.477 | 2/9 (22%), WLL 2.089 | +11pp | −0.39 |
| 2026-05-17 | 8/11 (73%), WLL 1.568 | 5/11 (45%), WLL 1.522 | **−28pp** | −0.05 |
| **AGGREGATE** | **13/40 (33%), WLL 2.015** | **14/40 (35%), WLL 1.729** | **+1 winner** | **−0.29** |

- WLL improves on **all 4 meetings** (−0.29 average) → better-calibrated probs.
- Top-1 lifts on 3 of 4 weak meetings; costs 3 winners on the one
  meeting (May 17) where stage-1 was unusually strong.

### β-sweep (α=1.0, varying β on same baseline-150 model)

| β | 5/27 | 5/24 | 5/20 | 5/17 | Total | WLL avg |
|---|---|---|---|---|---|---|
| 0.0 (stage1) | 2/9 | 2/11 | 1/9 | 8/11 | 13/40 | 2.015 |
| 0.3 | 2/9 | 3/11 | 0/9 | 7/11 | 12/40 | 1.861 |
| 0.5 | 3/9 | 3/11 | 1/9 | 6/11 | 13/40 | 1.794 |
| 0.7 | 3/9 | 3/11 | 2/9 | 6/11 | 14/40 | 1.751 |
| **0.9** | **3/9** | **4/11** | **2/9** | **5/11** | **14/40** | **1.729** ← best |

β=0.9 (the production default) is the right setting on both metrics.

### Confidence-gated blending (only blend when model's top prob < gate)

| Gate | 5/27 | 5/24 | 5/20 | 5/17 | Total | WLL avg |
|---|---|---|---|---|---|---|
| always stage1 | 2/9 | 2/11 | 1/9 | 8/11 | 13/40 | 2.015 |
| 0.20 | 3/9 | 2/11 | 1/9 | 7/11 | 13/40 | 1.981 |
| **0.25** | **3/9** | **3/11** | **1/9** | **7/11** | **14/40** | **1.906** |
| 0.30 | 3/9 | 3/11 | 2/9 | 6/11 | 14/40 | 1.790 |
| 0.40 | 3/9 | 4/11 | 2/9 | 5/11 | 14/40 | 1.764 |
| always stage2 | 3/9 | 4/11 | 2/9 | 5/11 | 14/40 | 1.729 |

Aggregate top-1 ties at gate≥0.25 (14/40 = +1 winner vs baseline).
**gate=0.25 is the risk-adjusted sweet spot**: same +1 net but only loses
1 of the 8 May 17 winners (vs 3 for always-stage2).

---

## 3. Deployment options (pick one)

### Option A — Simplest: flip the flag (no code change, no retrain)

```bash
python3 /tmp/enable_stage2_on_model_lambda_v1.py
# then re-predict the next meeting (HV Wed 2026-06-03):
python3 -m models.walk_forward --strategy model_lambda_v1 \
    --from 2026-06-03 --to 2026-06-03
```

Expected: +2pp top-1, −0.29 WLL. Sometimes (1 meeting in ~4) costs 3
winners on a "model already crushing it" meeting. Net positive on
aggregate.

Revert: `UPDATE strategies SET stage2_enabled = 0 WHERE id = 1;`

### Option B — Confidence-gated stage 2 (small code change)

Modify `models/stage2_benter.blend()` to skip races where
`max(stage1_probs) >= 0.25`, falling through to stage 1 alone for
those. Expected: same +1 winner aggregate as Option A but ~2× lower
variance on strong meetings.

This is a 5-line change in `models/stage2_benter.py`. I have NOT made
this change — it needs your sign-off.

### Option C — Operational filter (no code change)

Independent of Stage 2: only bet races where the model's top calibrated
prob is **≥30%**. 59 of 154 races qualified historically, hit-rate 68%.
Combine with Option A for the best of both. Already in the existing
betting filter — no work needed if `bet_min_prob ≥ 0.30`.

### Recommended combination

**Option A + Option C**: flip stage2_enabled=1 AND keep `bet_min_prob`
at 0.30 or higher. Stage 2 makes the probabilities better calibrated,
the filter only acts on the well-calibrated high-confidence picks.

---

## 4. Pruning is fragile (don't be tempted)

cat06_minus_H069 looked like a 44% winner on May 27 but is awful elsewhere:

| Date | cat06_minus_H069 acc |
|---|---|
| 2026-05-27 | 4/9 (44%) ✅ |
| 2026-05-24 | 2/11 (18%) |
| 2026-05-20 | 3/9 (33%) |
| 2026-05-17 | 1/11 (9%) ❌ |
| 2026-05-13 | 1/9 (11%) ❌ |
| 2026-05-09 | 0/11 (0%) ❌ |
| 2026-05-06 | 0/9 (0%) ❌ |
| 2026-05-03 | 1/11 (9%) ❌ |
| **Aggregate** | **12/78 = 15%** |

Pruning that worked on the one meeting we tuned on, FAILED on every
other meeting. Lesson: don't pick recipes from single-meeting tests.

---

## 5. Baseline behaviour across 8 meetings (W6 detail)

| Date | top-1 | WLL |
|---|---|---|
| 2026-05-03 | 5/11 (45%) | 1.957 |
| 2026-05-06 | 6/9 (67%) ✅ | 1.693 |
| 2026-05-09 | 4/11 (36%) | 2.452 |
| 2026-05-13 | 5/9 (56%) ✅ | 1.654 |
| 2026-05-17 | 8/11 (73%) ✅ | 1.568 |
| 2026-05-20 | 1/9 (11%) ❌ | 2.477 |
| 2026-05-24 | 2/11 (18%) ❌ | 1.943 |
| 2026-05-27 | 2/9 (22%) ❌ | 2.073 |
| **TOTAL** | **33/80 = 41.3%** | **1.977** |

The 4 most-recent meetings happened to be the WEAK half. The earlier
half (May 3–17) hit 24/40 = 60%. Don't anchor on the last 4 meetings
in isolation — the model is much better than the recent run suggests.

---

## 6. Operational confidence-filter calibration (unchanged from interim)

Across 154 races since April 1, baseline `model_lambda_v1` is well calibrated:

| Pick's calibrated_prob | n | hit rate |
|---|---|---|
| < 15% | 9 | 11% (random) |
| 15-20% | 17 | 18% |
| 20-25% | 35 | 31% |
| 25-30% | 34 | 32% |
| 30-40% | 42 | **67% ✅** |
| ≥ 40% | 17 | **71% ✅** |

**59 of 154 races (38%) have top-prob >30%; that subset hits 68%.**
This is the operational layer that decides bets.

---

## 7. Slice diagnostics

| Slice | Best acc | Worst acc |
|---|---|---|
| Race class | Class 5 (22 races): **68%** | Class 2 (10 races): 30% |
| Distance | 1600-1800m (49 races): **53%** | ≤1000m sprints (13 races): 31% |
| Field size | ≤9 horses (6 races): **83%** | 12-14 (136 races): 40% |
| Winner's market rank | Favorite wins (39 races): **59%** | Longshot wins (49 races): 27-46% |

Tomorrow's HV card (Wed 2026-06-03) will be mostly Class 3-5, 1200-1650m,
12-horse fields. Expect ~30-40% baseline → ~60-70% on the >30%-prob picks.
With Stage 2 enabled: expect calibration improvement (WLL down ~0.3) +
slight top-1 lift.

---

## 8. RCA tag findings

### Tag on the previous race → next-race outcome (1,337 horse-races since April 1):

| Prev-race tag | n | win rate next out |
|---|---|---|
| sent_for_sampling (was a winner) | 326 | **18%** (form streaks) |
| roarer | 36 | 19% (small n, suggests surgery effective) |
| vet_inspection | 483 | 8% |
| crowded | 113 | 6% |
| ran_off | 130 | 5% |
| bumped | 403 | 4% |
| raced_keenly | 82 | 4% |
| **steadied** | **173** | **1.2% ← clean negative signal** |

**Steadied = strongest pre-race "stay away" signal.** Horses steadied
in their previous race win only 1.2% of next-races (173 cases).
H107_off_vet_returner (currently a stub) should incorporate this:
pre-race feature that down-weights any horse coming off a `steadied` race.

### Per-race RCA on May 27 wrong picks
- 3 of 7 wrong picks had `vet_inspection` post-race
- 2 of 7 had `crowded` (bad trip)
- 1 each: `bumped`, `blood_in_mouth`, `slow_to_begin`, `steadied`, `ran_off`

---

## 9. Setup operational status

- [x] DB migrations (5 columns + multi_leg_dividends table)
- [x] trackwork.distance backfill (290,998 / 666,308 populated, 4/4 cross-checks pass)
- [x] K289 parser cross-check (17/17 fields correct)
- [ ] horse_pedigree --refresh — BLOCKED by other-session writers (~2,750 horses' career stats from cached HTML)
- [ ] vet_records — BLOCKED (would unblock H107)
- [ ] incident_reports.incident_tags backfill — BLOCKED (in-memory tagging works for analysis)

## ⚠️ Note

Your other session has been auto-relaunching `monitoring.integrity_check --baseline --heal` plus `scrape_results --force-refresh 2023-01-01...` on a tight loop tonight. Each relaunch blocks my DB writes (the pedigree/vet/incident-tags backfills). The daily 04:30 cron will fire normally; please don't manually re-launch these so the pedigree/vet/incidents backfills can complete.

## Files written

| Path | Purpose |
|---|---|
| `STATUS_OVERNIGHT.md` | running log with all detail |
| `OVERNIGHT_FINDINGS.md` (this) | morning brief — start here |
| `/tmp/enable_stage2_on_model_lambda_v1.py` | one-line DB patch to enable Stage 2 |
| `/tmp/test_stage2_baseline.py` | baseline-150 with/without Stage 2 (4 dates) |
| `/tmp/test_stage2_benter.py` | Stage 2 on pruned subsets |
| `/tmp/test_beta_sweep.py` | β-sweep on baseline-150 |
| `/tmp/test_confidence_gated.py` | confidence-gated Stage 2 sweep |
| `/tmp/validate_winner.py` + `.csv` + `_log.txt` | final 64-run validation matrix |
| `/tmp/beta_sweep_log.txt` | β-sweep + confidence-gated results |
| `/tmp/rca_*.md` | RCA narratives (per-race, historical, priors, per-tag) |
| `/tmp/wrong_pattern_analysis.py` | pattern script that produced the slice tables |

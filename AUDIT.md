# Stack Audit — Phase 0 Output

Read against `userdocs/global_research.md` (whitepaper, May 2026) and `userdocs/features_expanded_zh_hant.md` (174-feature catalog). Driving question: is the existing the scaffolding faithful enough to keep, or do we need to rewrite parts? Verdict per module: **keep / patch / rewrite**.

## TL;DR

Most the modules are sound and should be kept; the bugs are small and localised. The biggest gaps are **(a)** the `races` table has no `post_time` column but `live/scheduler.py` and `decision_loop.py` both SELECT it (silently fails — scheduler will never fire); **(b)** three scrapers are missing (per-horse sectionals, archival odds, daily track-bias); **(c)** `features/compute.py` has a handful of broken expressions (most importantly `h087_class_drop` is unparseable and `h165_late_steam` is dead placeholder code); **(d)** `pipeline.py` builds ~5 global-stats keys but the catalog references 13 — the rest silently return None at compute time; **(e)** the catalog already has only 15 categories (Cat 16 biometric was dropped before commit), so the HKJC-only scope trim is partially in effect already.

## Per-module verdicts

### `db.py` (584 LOC) — **patch**

- Schema is clean, indexes are sensible, migration shim from v1 is idempotent and tolerant of v1 column drift.
- Missing column: `races.post_time TEXT` — needed by `live/scheduler.py:51` and `decision_loop.py`. Add in Phase 1.
- Missing tables (planned in Phase 1): `track_bias_daily`, `calibrator_artifacts`.
- The `horse_pedigree` columns `dosage_*`, `birth_month`, `hemisphere` will stay NULL forever under HKJC-only scope — leave the columns (no harm) and let the corresponding features return NaN.

### `scrapers/_base.py` (236 LOC) — **keep**

- Signal handling, checkpoint I/O, raw-HTML cache, UPSERT helper, retry/backoff fetch — all correct and production-grade.
- No issues found.

### `scrapers/odds_poller.py` (160 LOC) — **patch**

- Correct cadence (30s on race days, 60s otherwise), parses both JSON shapes.
- Real concern: only stores forward-going snapshots. There is **no opening-odds baseline** for any historical race in the DB, so all Cat 14 features (H156 opening, H158 drift, H165 late-steam) will return None for back-tests. Phase 3a's new `scrape_odds_archive.py` plugs this gap.
- Minor: `_is_race_day()` is a weekday heuristic (Wed + Sun). Should defer to `races.date` calendar once that table is current. Low-priority polish.

### `scrapers/scrape_race_card.py` (164 LOC) — **patch**

- `POSTTIME_RE` defined at line 32 but never used. The scraper never writes post_time. Combined with the missing column in `races`, this is the root cause of the live-scheduler bug.
- Fix: add `races.post_time` column; parse and persist it in `_extract_per_race_blocks` (and propagate through the UPDATE in `_scrape_card`).
- Rail/prize/race_name extraction works.

### `scrapers/scrape_barrier_trials.py` (148 LOC) — **keep**

- Correct HKJC URL, robust regex, sensible UNIQUE key. Trial metadata parsing (`_parse_trial_meta`) is heuristic but acceptable.
- Minor: `field_size`, `sectional_400`, `gear`, `notes` columns exist in the table but the scraper does not populate them. Not blocking the spine.

### `scrapers/scrape_horse_pedigree.py` (116 LOC) — **keep**

- Captures sire, dam, dam_sire, origin_country from HKJC horse profile page — exactly what HKJC-only scope wants.
- Dosage Index / Centre of Distribution intentionally not scraped — already aligned with our HKJC-only decision.

### `scrapers/scrape_trackwork.py` (124 LOC) — **keep**

- URL + parser fine. Distance + time per gallop captured. Sufficient for Cat 8 H095 ("recent trackwork distance sum, last 14d").

### `scrapers/scrape_vet_records.py` (79 LOC) — **keep**

- Iterates HKJC OVE database table; UPSERTs on (brand, date, type). Adequate.

### `scrapers/scrape_roarers.py` (61 LOC) — **keep**

- Single-purpose; writes `type='roarer-surgery'` into `vet_records`. Adequate.

### `scrapers/scrape_weather.py` (106 LOC) — **patch**

- HKO fallback works for temperature/humidity/rainfall, **but wind is never extracted** even though the HKO API exposes wind under a different endpoint and `weather_observations.wind_speed_kmh` is populated by the dict access that always returns None. Either fetch HKO `lhl` (wind) endpoint, or be honest and drop the wind feature.
- Stores one row per race (good — required for race-no-level joins).

### `features/catalog.py` (314 LOC) — **patch**

- Asserts 174 features; passes. But the catalog is structured into **15 categories**, not 16 — Cat 16 (GPS/biometric) was already dropped before commit. The HKJC-only trim is therefore partly already in effect. Document this in the catalog header so future readers don't expect 16.
- Bibliography reference fields are present and consistent with `userdocs/features_expanded_zh_hant.md`.
- For Phase 1 HKJC-only trim: flip `enabled_default=False` on H013 (DI), H123–H131 (Beyer/Timeform/RPR/Brisnet/Topspeed/Equibase/Ragozin), H162 (BSP), H164 (exchange depth). That leaves ~163 active features. Delete the body of their compute functions and let them all bind to `_nan_stub`.
- Duplicate features called out by ID (e.g. H064/H007, H065/H008, H144/H033, H169/H058) are intentionally kept as separate slots per the source document — leave them.

### `features/compute.py` (721 LOC) — **patch (a handful of real bugs)**

Bugs that must be fixed before walk-forward will produce sensible output:

1. **`h087_class_drop` (line 384)** — completely broken expression: `1.0 if h074_class({"race": {"class": cur_cls}, "entry": {}}.__getitem__("race")["class"] is None and 0) else 0.0 if last_cls == cur_cls else (1.0 if (last_cls < cur_cls) else 0.0)`. This raises or returns garbage. Rewrite to compare class numeric encodings via `h074_class` with proper context shims.
2. **`h165_late_steam` (line 657)** — contains `cutoff = basis.replace().__sub__ if False else None  # placeholder` and proceeds to use a wrong filter. Rewrite to filter snapshots within the last 15 minutes before `snapshot_basis`.
3. **`h097_gear_change` and `h098_first_gear` are identical** — `h098` should detect previously-untried gear (compare current gear to set of all prior gears), not just any change. Fix or document the alias.
4. **`h062_claim`** — subtracts `decl_wt - act_wt`, but in HK that doesn't equal the apprentice claim. Real claim is published per jockey separately. Either route through a jockeys table (not currently scraped) or set to NaN until a jockey-master scraper exists.
5. **`h120_style_purity`** — the back-mapping from style code to leader position via `s * 2 + 1` is opaque magic. Either document the encoding inline or rewrite using the same `style` thresholds used in `h109_style`.
6. **`h133_dist_surface`** — uses the `"dist"` prior key with a small-sample shrinkage; that prior was tuned for distance buckets, not joint distance×surface buckets. Cosmetic / minor.

Missing global_stats keys: `pipeline.py:_compute_global_stats` builds only **5** maps that the compute layer actually populates (`jockey_wr`, `trainer_wr`, `jt_pair`, `jockey_at_HV`, `jockey_at_ST`, `trainer_at_HV`, `trainer_at_ST`, plus `field_avg_wr` constant). It hands back **empty** dicts for `trainer_hot`, `trainer_cold`, `trainer_x_class`, `trainer_x_dist`, `trainer_x_venue`, `trainer_x_phase`, `trainer_first_timer_wr`, `trainer_returner_wr`, `trainer_density_30d`, `stable_size`, `jockey_x_venue`, `jockey_x_dist`, `horse_avg_implied`. As a result, H039, H040, H041, H042, H043, H044, H045, H046, H047, H048, H135, H136 all return None today. Either populate them in `pipeline.py` or accept the NaN cost.

### `features/pipeline.py` (248 LOC) — **patch**

- PIT enforcement is correct on training history (`date < race.date`) and on odds snapshots (via `snapshot_basis`).
- The global-stats computation strictly uses `date < before`, so no look-ahead. Good.
- It recomputes the global stats per `cur_date` (one per distinct race date), which is sound but slow. For 2 seasons (~150 race days) we'll see ~150 recomputes; each scans `results` once. Tolerable.
- **Wiring gap**: most `global_stats` keys never get built — see compute audit above. Phase 3 should expand `_compute_global_stats` to fill at least `trainer_x_class`, `trainer_x_venue`, `trainer_x_dist`, `jockey_x_venue`, `jockey_x_dist`, and rolling-30d `trainer_density_30d` / `trainer_hot` / `trainer_cold`.
- Default `snapshot_basis = race.date + 'T23:59:59'` is wrong for back-testing (it's post-race). Should default to `race.date + 'T' + post_time` once `post_time` exists, otherwise something like 'T12:00:00' to give a stable pre-race cut.

### `models/stage1_xgb.py` (87 LOC) — **keep**

- LambdaMART setup (`rank:pairwise`), per-race group construction, position-to-label mapping, softmax-per-group are all textbook-correct.
- DEFAULT_PARAMS are conservative and sensible (`eta=0.05`, `max_depth=6`).

### `models/stage2_benter.py` (70 LOC) — **keep**

- Formula matches whitepaper §2.2 exactly: `c_i ∝ exp(α·log(f_i) + β·log(π_i))`, numerically stable softmax per race.
- `fit_alpha_beta` does a 15×15 grid search over [0.1, 1.5] in 0.1 steps — coarse but appropriate. A second-pass refinement on the best cell would be a cheap improvement; not blocking.
- The schema default `stage2_alpha=0.5, stage2_beta=0.5` in `db.py:337` is conservative; Benter's results were closer to α≈1.0, β≈0.9. Override in the strategy seed in Phase 3c.

### `models/calibration.py` (126 LOC) — **keep**

- Isotonic / Platt / bucketed / none branching is clean; sklearn-availability fallback to bucketed is graceful.
- `brier`, `log_loss`, `ece` implementations are correct.

### `models/walk_forward.py` (332 LOC) — **patch**

- Walk-forward semantics are correct: trains on `date < d`, predicts on `date == d`.
- **Calibration hack at lines 240–248**: the hold-out is the last 2000 *training* rows re-predicted by the same model that trained on them — this gives over-optimistic calibration scores. Real practice: hold out the most recent N days strictly from training and use only those for calibrator fit. Fix in Phase 3c.
- The "won" lookup at line 278 runs a separate `SELECT position` even though `odds_row` is already fetched a few lines earlier — minor perf, but the bigger issue is the bracket around `(odds_row and ... .fetchone()[0] == 1)` will throw if `fetchone()` returns None (no result row for that race+brand). Add a `r = ... .fetchone()` and guard `r is not None`.
- Persists into `calibration_metrics` with `window_end = date_to`; UNIQUE on `(strategy_id, window_end)` so re-runs UPDATE rather than accumulate. Good.

### `models/harville_henery.py` (88 LOC) — **patch**

- Harville top-2 / top-3 / quinella implementations are correct.
- "Henery" function (`henery_top2`) is actually Plackett-Luce with an exponent, not the true Henery exponential-time model. Either rename `henery_top2` → `pl_top2_with_gamma` or implement the actual Henery integral. Cosmetic for now.
- Not wired into `walk_forward.py` — exotic-pool features H147–H151 currently bind to `_nan_stub`. Plug in during Phase 4 when expanding the catalog.

### `betting/filters.py` (48 LOC) — **keep**

- Explicit `math.isfinite` guards on prob, odds, edge cover the NaN class identified in `project_nan_odds_filter` memory. Correct.
- Bounds enforced: `bet_min_odds`, `bet_max_odds` (matches `project_bet_max_odds` memory), `min_prob`, `edge_threshold`, `pool_depth_floor`.
- Note `edge_threshold` default of 1.05 is in multiplicative form (`prob*odds >= 1.05`), not additive — confirmed by `_tick` / `audit.py` callers. Documentation consistency only.

### `betting/sizing.py` (66 LOC) — **keep**

- Fractional Kelly with three clamps (bankroll %, pool %, absolute). Matches Benter §4.2 and whitepaper §5.2.
- NaN guards in `kelly_fraction()`.

### `betting/circuit_breaker.py` (84 LOC) — **keep**

- Daily/weekly loss limits per strategy, halts on breach, halt persists in `circuit_breaker_state`. Matches whitepaper §5.3.
- Minor: `record_settlement` calls `datetime.fromisoformat(date).weekday()` twice — readable but recomputes; cosmetic.

### `betting/kill_switch.py` (49 LOC) — **keep**

- Global single-row halt + heartbeat ("dead-man's switch"). Matches whitepaper §5.3.
- `heartbeat_fresh` default `max_age_sec=300` is sensible.

### `betting/clv.py` (47 LOC) — **keep**

- Correct sign convention: `(1/close) − (1/placed)` is positive when you got a better price than the eventual close. Aligns with whitepaper §5.4 (CLV as the gold-standard real-time test).
- "Closing" here is the latest snapshot recorded (not the official SP); acceptable given HKJC has no BSP.

### `betting/audit.py` (116 LOC) — **keep**

- Counterfactual replay correctly reuses `filters.evaluate` and `sizing.size_bet`. Sweep grid is reasonable.
- The audit reports ROI from a single bankroll value (1000.0 default) and does not advance bankroll across bets — this is by design (sweep mode), but a future "growth-mode" audit would compound. Out of scope.

### `live/decision_loop.py` (176 LOC) — **patch**

- T-10→T-0 tick loop, paper mode default, real-money toggle gated by global kill switch — all aligned with whitepaper §5.6.
- **Blocker bug**: the loop is launched by `scheduler.py` which SELECTs `post_time` from `races` — column doesn't exist (see `db.py` finding). Until that column lands and is populated, no race loop will start.
- Bankroll is hard-coded to 10000.0 at line 145 with a TODO; fine for now, add per-strategy bankroll later.

### `live/scheduler.py` (82 LOC) — **patch**

- Same `post_time` issue. Add column, populate via `scrape_race_card.py`, then test.
- Tick interval (60s) is conservative; race loop's own 60s tick gives 10-minute window with 10 ticks. Adequate.

### `live/sim_mode.py` (110 LOC) — **keep**

- Replays a closed date through `filters` + `sizing` and writes `mode='paper'` bets with settlement and payout. Updates circuit-breaker pnl correctly.
- Useful for spine validation in Phase 3 (test the full betting pipeline on historical predictions before turning on live mode).

### `api.py` (357 LOC) — **keep**

- Endpoints align with the v1 router we want to retire: health, schema-info, scrapers list/run, checkpoints, kill-switch GET/POST, strategies GET/POST, audit, sweep, health metrics, live/mode GET/POST, live/status.
- `live_mode_set` requires `confirm=I_AM_SURE` for enabling — good safety.
- Action registration into the shared scheduler is clean; scrapers slot into the existing schedules system.
- One nit: `create_strategy` exposes only name/name_zh/name_en — Phase 3c will need to seed many more columns. Either extend the endpoint or do the seed via direct SQL/migration (recommended; less endpoint surface).

## What this means for the next phases

Phase 1 work expands to:
- Add `races.post_time TEXT` column.
- Add `track_bias_daily` and `calibrator_artifacts` tables (already planned).
- Disable Cat-11 paid-figure features + H013 / H162 / H164 (catalog flip + compute deletion).
- Drop dead wind references from `scrape_weather.py` or fix HKO `lhl` fetch.

Phase 2 (v1 wipe) is unchanged.

Phase 3a (new scrapers) expands to:
- `scrape_per_horse_sectionals.py` — new.
- `scrape_odds_archive.py` — new.
- `compute_track_bias.py` — new (daily aggregator).
- **Patch `scrape_race_card.py`** to capture `post_time` (now requires the new column).

Phase 3c (spine) expands to fix-list:
- Rewrite `h087_class_drop`, `h165_late_steam`; alias `h097_gear_change` / `h098_first_gear` correctly.
- Expand `pipeline._compute_global_stats` to populate at least the trainer × class / venue / dist / phase / density / size and jockey × venue / dist maps.
- Replace walk-forward's "calibrate on last 2000 training rows" with a strict held-out window.
- Default `snapshot_basis` from `race.date + 'T12:00:00'` (or actual post_time once captured).

Phase 4 (catalog expansion) inherits the Harville / Henery / Plackett-Luce wiring for H147–H151.

Phase 5 (live + UI) inherits the post_time fix from Phase 1; without it the live scheduler is a no-op.

## State of `racing.db` right now

- 6 raw tables migrated from v1 (races 2,435 / horses 1,311 / results 29,676 / sectionals 2,363 / race_history 26,869 / dividends 1,008).
- 174-row `feature_catalog` seeded.
- 2,436 rows in `feature_values` — looks like a single test race or partial run; effectively empty for back-test purposes.
- All other tables (odds_snapshots, barrier_trials, trackwork, horse_pedigree, vet_records, rail_position, weather_observations, per_horse_sectionals, strategies, predictions, live_bets, circuit_breaker_state, calibration_metrics, drift_alerts) are empty.
- `kill_switch_state` has the single row, halted = 0.

Phase 1 starts next.

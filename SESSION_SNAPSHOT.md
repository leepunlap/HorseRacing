# Session snapshot — 2026-05-29/30

Cross-machine handoff. Pull on the new machine, read this first, then
the older `OVERNIGHT_FINDINGS.md` / `STATUS_OVERNIGHT.md` for the
research details.

---

## Get going on the new machine

```bash
git clone git@github.com:leepunlap/HorseRacing.git
cd HorseRacing
# data/ is gitignored — copy it from the old machine (or re-scrape)
rsync -av OLDHOST:/var/www/horseracing/data/ ./data/   # 664 MB DB + raw HTML caches
python3 db.py --stats                                   # confirm DB intact
nohup python3 app.py --port 8005 > /tmp/api.log 2>&1 & disown
```

Daily cron continues at 04:30 (integrity check + auto-heal). Live
race-day automation (odds polling, post-race auto-settle) fires from
inside `app.py`'s lifespan — once the API is up, it's all there.

---

## What's in the DB right now

| | |
|---|---|
| Settled bets across 11 strategies | 10,539 |
| Predictions (model_lambda_v1) | 13,454 |
| Feature catalog | 195 entries (157 active in production strategy) |
| Incident reports | 26,604 (20,137 with structured tags) |
| Latest integrity check | run #6, **159 violations** — 98.7% reduction from baseline (12,240) |
| DB file | `data/racing.db`, 664 MB |

---

## Commits in this session (newest first)

```
9dde9d8 Session wrap-up: resilience fixes + overnight research notes
d7e1fca bet_runner: generic feature_filters spec on flat_top1_filtered
def27eb bet_runner: optional market-drift overlay (Cat-14 H158)
e29b323 Cat-17 incident-history features (H189-H195)
fd10bd6 RCA overhaul: incident_reports as primary source + pace fix + backfill
e32d62f integrity_check: fix two over-aggressive false-positive rules
0c9e88b Cat-14 odds features: results.odds fallback for historical races
b477e75 Multi-leg dividends: live capture via resultMeetings GraphQL
abd5476 Scrapers: close the high-leverage gaps from the HKJC coverage audit
a554819 walk_forward: auto-refresh every bet strategy after run_strategy
96da153 Scheduler: daily integrity_check + auto-heal at 04:30
b8f5a85 SPA integrity badge + /api/integrity endpoints
d5723a6 Cross-source integrity-check + incident-reports scraper + persons table
```

---

## Current best-performing strategies

| id | name | bets | ROI |
|---|---|---|---|
| 2 | kelly_top1 | 629 | **+152.3%** |
| 4 | top1_minprob20 | 814 | +82.5% |
| 22 | flat_top1_market_overlay | 1084 | +71.2% (new — uses Cat-14 H158) |
| 1 | flat_top1 | 1084 | +70.9% |
| 25 | flat_top1_vet_safe | 41 | +75.9% (tiny sample) |

---

## Model calibration

```
ECE       0.0217   (improved from 0.0245 — "elite" tier well under 0.05)
Brier     0.0613
log_loss  0.2175
samples   7,878
```

---

## What's running / scheduled

- `app.py` on port 8005 — needs restart on new machine
- `04:30 daily` — `integrity_check --heal` via cron in `data/schedules.json`
- `live/scheduler.py` — per-race decision loop (T-10→T-0) + post-race
  auto-settle (T+3min → polling for results → bet_runner)
- `live/odds_poller.py` — every 30s on race days, T-60→T-0

---

## Top recommendations for next session (from `OVERNIGHT_FINDINGS.md`)

1. **Flip `stage2_enabled = 1`** on `model_lambda_v1` (production strategy id=1).
   Validated by 64-run β-sweep. No retrain needed.
2. **Confidence-gated wrapper** for the strongest risk-adjusted lift —
   bet only when calibrated_prob ≥ 20%. Empirical evidence in
   `OVERNIGHT_FINDINGS.md` §3.
3. Wait for more `odds_poller` race-days, then retrain with Cat-14
   features actually used by the model (not just the post-prediction overlay).
4. Stage-3 ensemble — blend top 2-3 strategies' picks per race.
5. UI: inline RCA chips on bet rows — deferred this session because the
   SPA was being refactored in parallel. Worth doing once UI stabilises.

---

## Two unresolved things to be aware of

1. **159 integrity violations** are still on the books from last check.
   80 of them are `unsettled_after_results` artefacts from a bet_runner
   refresh — the 04:30 cron's `--heal` pass will clear them
   automatically. None high-severity.
2. **L022 (跨境駿馬) post-mortem PDF** — `data/reports/2026-05-27_HV_R6_postmortem.pdf`
   is a worked example of using all the new sources (sectionals, incident
   reports, RCA tags, comments). Useful as a template if you want to
   generate per-race reports going forward.

---

*Saved 2026-05-30. All commits pushed to origin/master.*

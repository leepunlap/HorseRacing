#!/usr/bin/env python3
"""One-shot repair: renormalise `predictions.calibrated_prob` per race.

The isotonic calibrator in `models.walk_forward` was applied per-row
without restoring per-race-sum=1, which collapsed non-favourites to ~0
and pushed favourites toward ~1. That made every `edge = cp × odds`
either 0 (for the crushed losers) or unrealistically large (for the
single survivor). This script:

  1. Groups `predictions` by (strategy_id, race_id).
  2. Divides each row's calibrated_prob by its race sum so the field
     sums to 1.0 (uniform 1/n fallback if the race sum is 0).
  3. Recomputes `edge = calibrated_prob × odds_at_prediction` using
     the existing odds column, or pulls it from `results.odds` when
     missing.
  4. UPDATEs the rows in a single transaction per strategy.

Idempotent: running twice is a no-op since renormalising a vector
that already sums to 1 is the identity.

Usage:
    python3 -m reports.repair_calibrated_probs               # all strategies
    python3 -m reports.repair_calibrated_probs --strategy 1  # one strategy
    python3 -m reports.repair_calibrated_probs --dry-run     # print, don't write
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models.walk_forward import _odds_for  # noqa: E402

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "racing.db"


def repair(strategy_id: int | None, dry_run: bool) -> tuple[int, int]:
    """Returns (races_touched, rows_updated)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        where = ""
        params: tuple = ()
        if strategy_id is not None:
            where = "WHERE strategy_id = ?"
            params = (strategy_id,)

        race_rows = conn.execute(
            f"SELECT DISTINCT strategy_id, race_id FROM predictions {where}",
            params,
        ).fetchall()
        n_races = 0
        n_rows = 0
        for sid, rid in race_rows:
            preds = conn.execute(
                "SELECT id, brand, calibrated_prob, blended_prob, "
                "       fundamental_prob, odds_at_prediction, snapshot_basis "
                "FROM predictions WHERE strategy_id = ? AND race_id = ? "
                "ORDER BY id",
                (sid, rid),
            ).fetchall()
            if not preds:
                continue
            cal_vals = [p["calibrated_prob"] or 0.0 for p in preds]
            cal_sum = sum(cal_vals)
            n = len(preds)
            # If the isotonic calibrator crushed the field (>1 row at zero
            # AND cal_sum << 1), rebuild from blended_prob (which still
            # carries the per-race softmax distribution). Otherwise just
            # renormalise the existing cal_prob.
            # Failure modes the calibrator can leave behind:
            #   (a) cal_sum << 1 — never renormalised per race.
            #   (b) one horse near 1.0, the rest at calibration.EPS (1e-9)
            #       — saturated favourite + EPS-clipped losers.
            #   (c) a handful of horses share the same mass, the rest at
            #       literal 0.0 — happens when isotonic mapped many low
            #       scores to its y_min and the renormalise then divided
            #       the surviving mass equally. Spotted on race 2437
            #       (today's HV R2) where 4 horses sat at 0.25 and 8 at 0.
            # Conservative test: in a healthy multi-horse race no horse
            # carries less than 1e-6 mass. Both literal 0 and EPS-clipped
            # 1e-9 trigger this; the latter is what happens when isotonic
            # mapped many low scores to its y_min before a previous repair
            # renormalised — the survivors hit 0.25 / 0.5 and the rest
            # stayed at EPS.
            min_v = min(cal_vals) if cal_vals else 0.0
            max_v = max(cal_vals) if cal_vals else 0.0
            near_zeros = sum(1 for v in cal_vals if v < 1e-6)
            corrupted = (
                cal_sum < 0.99
                or (max_v > 0.7 and min_v < 1e-6)
                or (n > 1 and near_zeros > 0)
            )
            if corrupted:
                source = [p["blended_prob"] or p["fundamental_prob"] or 0.0
                          for p in preds]
                s = sum(source)
                new_cal = ([v / s for v in source] if s > 1e-12
                          else [1.0 / n] * n)
            else:
                # Already mostly healthy — just renormalise to fix tiny drift
                if cal_sum > 1e-12:
                    new_cal = [v / cal_sum for v in cal_vals]
                else:
                    new_cal = [1.0 / n] * n
                if abs(cal_sum - 1.0) < 1e-6 and all(v > 0 for v in cal_vals):
                    continue
            if dry_run:
                print(f"  strategy {sid} race {rid}: sum={s:.4f} → "
                      f"top {max(cal_vals):.3f}→{max(new_cal):.3f}")
            else:
                # Resolve odds: prefer odds_at_prediction; otherwise the latest
                # odds_snapshots tick at or before snapshot_basis, falling back
                # to results.odds. Mirrors models.walk_forward._odds_for so a
                # repaired row matches a freshly written one.
                for p, new_p in zip(preds, new_cal):
                    odds = p["odds_at_prediction"]
                    if odds is None:
                        odds, _pos = _odds_for(conn, rid, p["brand"],
                                               p["snapshot_basis"])
                    edge = (new_p * odds) if odds else None
                    conn.execute(
                        "UPDATE predictions SET calibrated_prob = ?, edge = ? "
                        "WHERE id = ?",
                        (new_p, edge, p["id"]),
                    )
                    n_rows += 1
            n_races += 1
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return n_races, n_rows


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--strategy", type=int, default=None,
                   help="Restrict to one strategy_id (default: all)")
    p.add_argument("--dry-run", action="store_true")
    ns = p.parse_args()
    n_races, n_rows = repair(ns.strategy, ns.dry_run)
    verb = "would update" if ns.dry_run else "updated"
    print(f"[repair_calibrated_probs] {verb} {n_rows} rows across {n_races} races")
    return 0


if __name__ == "__main__":
    sys.exit(main())

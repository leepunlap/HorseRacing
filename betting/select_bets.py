"""Bake the "one bet per race" selection into predictions.recommendation.

For every race covered by the strategy's predictions:
  * Default: bet the horse with highest calibrated_prob (rank by prob).
  * Hybrid (--low-conf-thresh): if the top horse's fundamental_prob is
    BELOW the threshold, fall back to the market favourite (lowest
    odds_at_prediction). Iter 14 found: 16% of bets have fund_prob<0.15
    and those bets had a -73% ROI (worse than random); routing them to
    the market favourite lifts overall ROI by +5.7pp on a 9-month window.

Ranking by edge (prob × odds) was tested against rank-by-prob in Iter 8
and lost badly: edge picked longshots that almost never won (top-1 hit
rate ~3% vs ~33%). Always rank by prob.

Usage:
    python3 -m betting.select_bets --strategy benter_baseline
    python3 -m betting.select_bets --strategy benter_baseline \\
        --from 2026-05-01 --to 2026-05-24
    python3 -m betting.select_bets --strategy benter_baseline \\
        --low-conf-thresh 0.15        # enable hybrid routing
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "racing.db"


def select(strategy_name: str, date_from: str | None, date_to: str | None,
           low_conf_thresh: float | None = None) -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode = WAL")
    row = conn.execute("SELECT id FROM strategies WHERE name = ?", (strategy_name,)).fetchone()
    if not row:
        conn.close()
        raise SystemExit(f"strategy not found: {strategy_name}")
    strategy_id = row[0]

    where = ["p.strategy_id = ?"]
    params: list = [strategy_id]
    if date_from:
        where.append("ra.date >= ?")
        params.append(date_from)
    if date_to:
        where.append("ra.date <= ?")
        params.append(date_to)

    rows = conn.execute(
        f"""
        SELECT p.id, p.race_id, p.brand, p.calibrated_prob, p.odds_at_prediction,
               p.fundamental_prob
        FROM predictions p
        JOIN races ra ON ra.id = p.race_id
        WHERE {' AND '.join(where)}
        """,
        params,
    ).fetchall()

    # Group by race_id
    by_race: dict[int, list[tuple]] = {}
    for r in rows:
        by_race.setdefault(r[1], []).append(r)

    bet_updates: list[tuple[str, int]] = []
    skip_updates: list[tuple[str, int]] = []
    races_with_pick = 0
    races_without_pick = 0
    hybrid_routed = 0

    for rid, race_rows in by_race.items():
        # Default rank: calibrated_prob (deterministic tie-break by brand).
        scored = [(pid, brand, prob, odds, fund)
                  for pid, _rid, brand, prob, odds, fund in race_rows
                  if prob is not None]
        if not scored:
            races_without_pick += 1
            for pid, _rid, _b, _p, _o, _f in race_rows:
                skip_updates.append(("no_data", pid))
            continue
        scored.sort(key=lambda x: (-x[2], x[1]))
        top_pid, top_brand, top_prob, top_odds, top_fund = scored[0]
        reason = "not_top_prob"

        # Hybrid routing: when model confidence is low (top fundamental_prob
        # below threshold), the model's pick has been empirically worse than
        # random — Iter 14 measured 4.9% strike vs 8.3% uniform expectation.
        # Fall back to the market favourite (smallest valid odds_at_prediction)
        # for those races. We keep one bet per race; just change who.
        if (low_conf_thresh is not None and top_fund is not None
                and top_fund < low_conf_thresh):
            candidates = [(pid, brand, prob, odds)
                          for pid, brand, prob, odds, _f in scored
                          if odds is not None and odds > 0]
            if candidates:
                fav = min(candidates, key=lambda x: x[3])
                top_pid = fav[0]
                reason = "model_topprob_low_conf"
                hybrid_routed += 1

        races_with_pick += 1
        for pid, _rid, _b, _p, _o, _f in race_rows:
            if pid == top_pid:
                bet_updates.append(("bet", pid))
            else:
                skip_updates.append((reason if pid == scored[0][0] else "not_top_prob", pid))

    conn.executemany(
        "UPDATE predictions SET recommendation = ?, decision_reason = NULL WHERE id = ?",
        bet_updates,
    )
    conn.executemany(
        "UPDATE predictions SET recommendation = 'skip', decision_reason = ? WHERE id = ?",
        skip_updates,
    )
    conn.commit()
    conn.close()
    return {
        "strategy": strategy_name,
        "from": date_from, "to": date_to,
        "races_with_pick": races_with_pick,
        "races_without_pick": races_without_pick,
        "marked_bet": len(bet_updates),
        "marked_skip": len(skip_updates),
        "hybrid_routed": hybrid_routed,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--strategy", required=True)
    p.add_argument("--from", dest="date_from", default=None)
    p.add_argument("--to", dest="date_to", default=None)
    p.add_argument("--low-conf-thresh", type=float, default=None,
                   help="if set, route races whose top-prob horse has "
                        "fundamental_prob below this value to the market "
                        "favourite (Iter 14: 0.15 lifted ROI +5.7pp)")
    ns = p.parse_args()
    out = select(ns.strategy, ns.date_from, ns.date_to, ns.low_conf_thresh)
    for k, v in out.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()

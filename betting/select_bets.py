"""Bake the "one bet per race" selection into predictions.recommendation.

For every race covered by the strategy's predictions, mark the horse with the
highest edge (calibrated_prob × odds_at_prediction) as `bet` and every other
horse in that race as `not_top_edge`. Ties (very rare) break by lower brand.

Run after walk-forward completes so the persisted recommendation matches the
live audit/charts/SPA selection logic.

Usage:
    python3 -m betting.select_bets --strategy benter_baseline
    python3 -m betting.select_bets --strategy benter_baseline \
        --from 2026-05-01 --to 2026-05-24
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "racing.db"


def select(strategy_name: str, date_from: str | None, date_to: str | None) -> dict:
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
        SELECT p.id, p.race_id, p.brand, p.calibrated_prob, p.odds_at_prediction
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

    for rid, race_rows in by_race.items():
        # Preferred: rank by edge (prob × odds). Fallback when odds aren't
        # polled yet (pre-race day): rank by calibrated_prob so the rule
        # "exactly one chosen horse per race" still holds.
        scored_edge: list[tuple] = []
        scored_prob: list[tuple] = []
        for pid, _rid, brand, prob, odds in race_rows:
            if prob is None:
                continue
            if odds is not None:
                scored_edge.append((pid, brand, prob * odds))
            scored_prob.append((pid, brand, prob))
        if scored_edge:
            scored_edge.sort(key=lambda x: (-x[2], x[1]))
            top_pid, fallback_reason = scored_edge[0][0], "not_top_edge"
        elif scored_prob:
            scored_prob.sort(key=lambda x: (-x[2], x[1]))
            top_pid, fallback_reason = scored_prob[0][0], "not_top_prob"
        else:
            races_without_pick += 1
            for pid, _rid, _b, _p, _o in race_rows:
                skip_updates.append(("no_data", pid))
            continue
        races_with_pick += 1
        for pid, _rid, _b, _p, _o in race_rows:
            if pid == top_pid:
                bet_updates.append(("bet", pid))
            else:
                skip_updates.append((fallback_reason, pid))

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
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--strategy", required=True)
    p.add_argument("--from", dest="date_from", default=None)
    p.add_argument("--to", dest="date_to", default=None)
    ns = p.parse_args()
    out = select(ns.strategy, ns.date_from, ns.date_to)
    for k, v in out.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()

"""Paper-trading simulator harness.

Replays a closed date through the live decision loop's filters + sizing, but
without a real-time poll — uses recorded odds_snapshots to fake the
T-10→T-0 window. Useful for validating the live stack before flipping the
real-money toggle.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "racing.db"

from betting import filters as filt, sizing, kill_switch, circuit_breaker


def _coerce_position(raw) -> int | None:
    """HKJC `results.position` is mostly int but also carries 'WV' / 'FE' /
    'PU' / 'UR' / 'DQ' / '---' codes for non-finishers."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _coerce_odds(raw) -> float | None:
    """`results.odds` is mostly REAL but '---' creeps in for non-runners."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            f = float(raw)
            return f if f > 0 else None
        except (TypeError, ValueError):
            return None
    try:
        f = float(str(raw).strip())
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def replay_date(date: str, *, bankroll_per_strategy: float = 10000.0) -> dict:
    """Re-evaluate every race on `date` against the strategies enabled flags;
    write rows to live_bets with mode='paper'.

    Returns a summary {strategy_id, placed, blocked, total_stake, payout, roi}.
    """
    if not DB_PATH.exists():
        raise SystemExit(f"DB not found at {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        strats = conn.execute(
            "SELECT id, name, edge_threshold, min_prob, bet_min_odds, bet_max_odds, "
            "kelly_fraction, kelly_max_bankroll_pct, pool_impact_max_pct FROM strategies WHERE enabled = 1"
        ).fetchall()
        races = conn.execute(
            "SELECT id, course, race_no FROM races WHERE date = ? ORDER BY course, race_no", (date,)
        ).fetchall()
        summary: dict = {}
        for srow in strats:
            (sid, sname, edge_thr, min_prob, bet_min, bet_max, kf, kelly_pct, pool_pct) = srow
            placed = blocked = 0
            stake_sum = payout_sum = 0.0
            for race_id, _course, _race_no in races:
                # Most-recent prediction per brand (post-walk-forward)
                rows = conn.execute(
                    "SELECT brand, calibrated_prob, odds_at_prediction FROM predictions "
                    "WHERE strategy_id = ? AND race_id = ? ORDER BY snapshot_basis DESC",
                    (sid, race_id),
                ).fetchall()
                seen: dict[str, tuple[float, float]] = {}
                for b, p, o in rows:
                    if b not in seen:
                        seen[b] = (p, o)
                settings = filt.FilterSettings(
                    edge_threshold=edge_thr or 1.05, min_prob=min_prob or 0.02,
                    bet_min_odds=bet_min or 2.0, bet_max_odds=bet_max or 25.0,
                )
                for brand, (prob, raw_odds) in seen.items():
                    odds = _coerce_odds(raw_odds)
                    edge = (prob * odds) if (prob and odds) else None
                    ok, why = filt.evaluate(prob=prob, odds=odds, edge=edge, settings=settings)
                    if not ok:
                        blocked += 1
                        continue
                    sr = sizing.size_bet(prob=prob, decimal_odds=odds,
                                         bankroll=bankroll_per_strategy,
                                         kelly_fraction_strat=kf or 0.25,
                                         kelly_max_bankroll_pct=kelly_pct or 0.05,
                                         pool_impact_max_pct=pool_pct or 0.005)
                    if sr.stake <= 0:
                        blocked += 1; continue
                    placed += 1; stake_sum += sr.stake
                    pos_row = conn.execute("SELECT position FROM results WHERE race_id=? AND brand=?",
                                           (race_id, brand)).fetchone()
                    settled = "win" if (pos_row and _coerce_position(pos_row[0]) == 1) else "lose"
                    payout = sr.stake * odds if settled == "win" else 0.0
                    payout_sum += payout
                    conn.execute(
                        """
                        INSERT INTO live_bets (strategy_id, race_id, horse_id, brand, bet_type,
                                                placed_at, stake, odds_at_placement, expected_value,
                                                mode, settled_result, payout, notes)
                        VALUES (?, ?, (SELECT id FROM horses WHERE brand = ?), ?, 'win',
                                ?, ?, ?, ?, 'paper', ?, ?, ?)
                        """,
                        (sid, race_id, brand, brand, datetime.now().isoformat(),
                         sr.stake, odds, edge, settled, payout, f"sim_replay {date}"),
                    )
                    # Update circuit breaker pnl for the day
                    delta = payout - sr.stake
                    circuit_breaker.record_settlement(conn, sid, date, delta,
                                                     bankroll_hint=bankroll_per_strategy)
            roi = ((payout_sum - stake_sum) / stake_sum) if stake_sum else 0.0
            summary[sid] = {
                "name": sname, "placed": placed, "blocked": blocked,
                "stake": round(stake_sum, 2), "payout": round(payout_sum, 2),
                "roi": round(roi, 4),
            }
        conn.commit()
        return summary
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse, json
    p = argparse.ArgumentParser()
    p.add_argument("--date", required=True)
    args = p.parse_args()
    print(json.dumps(replay_date(args.date), indent=2, ensure_ascii=False))

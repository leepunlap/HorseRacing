"""Internal closing-line value tracking.

We don't have Betfair BSP in HK, so CLV uses our own HKJC tote odds: the
spread between the snapshot at bet-placement time and the latest snapshot
(close). CLV > 0 means we got a better price than the market eventually settled
on — the gold-standard real-time test of edge per [B13].

Records into live_bets.clv_internal on settlement.
"""

from __future__ import annotations

import sqlite3


def closing_odds(conn: sqlite3.Connection, race_id: int, brand: str) -> float | None:
    row = conn.execute(
        "SELECT win_odds FROM odds_snapshots WHERE race_id = ? AND brand = ? "
        "ORDER BY ts DESC LIMIT 1",
        (race_id, brand),
    ).fetchone()
    return row[0] if row and row[0] else None


def compute_clv(odds_at_placement: float, closing: float) -> float:
    """In implied-prob units: implied(close) - implied(placed)."""
    if odds_at_placement and odds_at_placement > 0 and closing and closing > 0:
        return (1.0 / closing) - (1.0 / odds_at_placement)
    return 0.0


def attach_clv_to_bet(conn: sqlite3.Connection, bet_id: int) -> float | None:
    row = conn.execute(
        "SELECT race_id, brand, odds_at_placement FROM live_bets WHERE id = ?",
        (bet_id,),
    ).fetchone()
    if not row:
        return None
    race_id, brand, placed = row
    close = closing_odds(conn, race_id, brand)
    if close is None or placed is None:
        return None
    clv = compute_clv(placed, close)
    conn.execute("UPDATE live_bets SET closing_odds = ?, clv_internal = ? WHERE id = ?",
                 (close, clv, bet_id))
    conn.commit()
    return clv

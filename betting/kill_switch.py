"""Global kill switch + dead-man's-switch heartbeat.

Single-row `kill_switch_state` table. Three operations:
  is_halted(conn)            -> bool
  set_halted(conn, halted, reason, by)
  heartbeat(conn)            -> writes `last_heartbeat = now`. Decision loops
                                refuse to place bets if heartbeat is older
                                than `max_age_sec`.

The decision is reusable by any actor (live decision loop, manual operator
button in SPA, scheduler entry).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta


def is_halted(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT halted FROM kill_switch_state WHERE id = 1").fetchone()
    return bool(row and row[0])


def set_halted(conn: sqlite3.Connection, halted: bool, reason: str | None = None,
               by: str | None = None) -> None:
    conn.execute(
        "UPDATE kill_switch_state SET halted = ?, halt_reason = ?, "
        "halted_at = ?, halted_by = ? WHERE id = 1",
        (1 if halted else 0, reason,
         datetime.now().isoformat() if halted else None, by),
    )
    conn.commit()


def heartbeat(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE kill_switch_state SET last_heartbeat = ? WHERE id = 1",
                 (datetime.now().isoformat(),))
    conn.commit()


def heartbeat_fresh(conn: sqlite3.Connection, max_age_sec: int = 300) -> bool:
    """Decision loops call this before placing a bet; if the operator stops
    refreshing (UI dead, network gone), bets pause within `max_age_sec`."""
    row = conn.execute("SELECT last_heartbeat FROM kill_switch_state WHERE id = 1").fetchone()
    if not row or not row[0]:
        return False
    age = (datetime.now() - datetime.fromisoformat(row[0])).total_seconds()
    return age <= max_age_sec

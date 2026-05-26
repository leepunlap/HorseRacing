"""Daily / weekly loss-limit circuit breaker.

Maintains `circuit_breaker_state` per strategy per day. Halts new bets when
daily_pnl < -daily_pct * bankroll_start OR weekly_pnl < -weekly_pct * bankroll_start.

Call:
  check_allowed(conn, strategy_id, date)  -> (allowed: bool, reason: str)
  record_settlement(conn, strategy_id, date, delta_pnl)  -> None

The kill-switch (global) is separate; see `betting.kill_switch`.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta


def check_allowed(conn: sqlite3.Connection, strategy_id: int, date: str) -> tuple[bool, str]:
    # Strategy limits
    row = conn.execute(
        "SELECT circuit_daily_loss_pct, circuit_weekly_loss_pct FROM strategies WHERE id = ?",
        (strategy_id,),
    ).fetchone()
    if not row:
        return False, "unknown_strategy"
    daily_pct, weekly_pct = row
    # Current day state
    state = conn.execute(
        "SELECT daily_pnl, weekly_pnl, bankroll_start, halted, halt_reason FROM circuit_breaker_state "
        "WHERE strategy_id = ? AND date = ?",
        (strategy_id, date),
    ).fetchone()
    if not state:
        return True, ""
    daily_pnl, weekly_pnl, bankroll_start, halted, halt_reason = state
    if halted:
        return False, halt_reason or "halted"
    bank = float(bankroll_start or 1.0)
    if daily_pnl is not None and daily_pnl < -(daily_pct or 0.10) * bank:
        _halt(conn, strategy_id, date, "daily_loss_limit")
        return False, "daily_loss_limit"
    if weekly_pnl is not None and weekly_pnl < -(weekly_pct or 0.25) * bank:
        _halt(conn, strategy_id, date, "weekly_loss_limit")
        return False, "weekly_loss_limit"
    return True, ""


def _halt(conn: sqlite3.Connection, strategy_id: int, date: str, reason: str) -> None:
    conn.execute(
        "UPDATE circuit_breaker_state SET halted = 1, halt_reason = ? WHERE strategy_id = ? AND date = ?",
        (reason, strategy_id, date),
    )
    conn.commit()


def record_settlement(conn: sqlite3.Connection, strategy_id: int, date: str,
                      delta_pnl: float, *, bankroll_hint: float | None = None) -> None:
    """Add `delta_pnl` to today's running pnl. Creates the day's row if absent."""
    row = conn.execute(
        "SELECT id, daily_pnl, weekly_pnl, bankroll_start FROM circuit_breaker_state "
        "WHERE strategy_id = ? AND date = ?",
        (strategy_id, date),
    ).fetchone()
    if row is None:
        # Roll weekly_pnl forward from same week
        week_start = (datetime.fromisoformat(date) - timedelta(days=datetime.fromisoformat(date).weekday())).date().isoformat()
        prior_week = conn.execute(
            "SELECT COALESCE(SUM(daily_pnl), 0) FROM circuit_breaker_state "
            "WHERE strategy_id = ? AND date >= ? AND date < ?",
            (strategy_id, week_start, date),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO circuit_breaker_state (strategy_id, date, daily_pnl, weekly_pnl, bankroll_start, halted) "
            "VALUES (?,?,?,?,?,0)",
            (strategy_id, date, delta_pnl, (prior_week or 0) + delta_pnl, bankroll_hint),
        )
    else:
        conn.execute(
            "UPDATE circuit_breaker_state SET daily_pnl = COALESCE(daily_pnl,0) + ?, "
            "weekly_pnl = COALESCE(weekly_pnl,0) + ? WHERE id = ?",
            (delta_pnl, delta_pnl, row[0]),
        )
    conn.commit()

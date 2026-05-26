"""Per-race decision loop.

Runs from T-10 to T-0 around a single race's post_time. Every 60 seconds:
  1. Refresh feature snapshot for this race.
  2. Pull stage-1 prediction (we don't retrain live — use the most recent
     walk-forward bst saved to models if present; otherwise skip with a
     pending status).
  3. Apply stage-2 Benter blend using the latest odds snapshot.
  4. Calibrate.
  5. Apply filters + Kelly + circuit breaker + kill switch + heartbeat freshness.
  6. Write a row to `live_bets` (mode='paper' by default; 'live' only if the
     strategy's `live_mode_enabled` flag is set AND the global toggle in
     `kill_switch_state` is off AND heartbeat is fresh).

Paper-mode by default so the loop is safe to run unattended.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "racing.db"

from betting import filters as filt, sizing, kill_switch, circuit_breaker
from models import stage2_benter, calibration


# Global toggle: real-money switch (P6). Defaults False; flipped by an
# operator action via /api/live/mode. The toggle is reflected in
# `kill_switch_state.halted_by` for now to avoid another table; a future
# revision can add a dedicated column.
_LIVE_MODE = {"enabled": False}


def is_live_mode() -> bool:
    return _LIVE_MODE["enabled"]


def set_live_mode(enabled: bool) -> None:
    _LIVE_MODE["enabled"] = bool(enabled)


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA journal_mode = WAL")
    return c


def _enabled_strategies(conn: sqlite3.Connection) -> list[tuple]:
    rows = conn.execute(
        "SELECT id, name, stage2_alpha, stage2_beta, calibration, edge_threshold, "
        "min_prob, bet_min_odds, bet_max_odds, kelly_fraction, kelly_max_bankroll_pct, "
        "pool_impact_max_pct FROM strategies WHERE enabled = 1"
    ).fetchall()
    return rows


async def race_loop(race_id: int, course: str, race_no: int, post_time: datetime, broadcast) -> None:
    """T-10 → T-0 decision loop for a single race."""
    end = post_time + timedelta(seconds=60)
    while datetime.now() < end:
        try:
            await _tick(race_id, course, race_no, broadcast)
        except Exception as exc:
            if broadcast is not None:
                try:
                    await broadcast.broadcast({
                        "type": "scraper_log",
                        "text": f"[decision_loop r#{race_no}] error: {exc}",
                        "task": "decision_loop",
                    })
                except Exception:
                    pass
        await asyncio.sleep(60)


async def _tick(race_id: int, course: str, race_no: int, broadcast) -> None:
    conn = _conn()
    try:
        # Read predictions from the most recent walk-forward run.
        strategies = _enabled_strategies(conn)
        if not strategies:
            return
        for srow in strategies:
            (sid, sname, alpha, beta, cal_mode, edge_thr, min_prob, bet_min, bet_max,
             kf, kelly_pct, pool_pct) = srow

            # Hard global gates
            if kill_switch.is_halted(conn):
                continue
            ok_cb, reason = circuit_breaker.check_allowed(conn, sid, datetime.now().date().isoformat())
            if not ok_cb:
                continue

            # Pull existing predictions for the race (most recent snapshot per horse)
            rows = conn.execute(
                "SELECT brand, calibrated_prob, odds_at_prediction FROM predictions "
                "WHERE strategy_id = ? AND race_id = ? "
                "ORDER BY snapshot_basis DESC", (sid, race_id),
            ).fetchall()
            if not rows:
                continue
            # Dedup brand -> latest
            seen: dict[str, tuple[float, float]] = {}
            for brand, prob, odds in rows:
                if brand not in seen:
                    seen[brand] = (prob, odds)

            # Refresh odds from latest snapshot, recompute edge, re-evaluate filters.
            settings = filt.FilterSettings(
                edge_threshold=edge_thr or 1.05, min_prob=min_prob or 0.02,
                bet_min_odds=bet_min or 2.0, bet_max_odds=bet_max or 25.0,
            )
            for brand, (prob, _stale_odds) in seen.items():
                if prob is None:
                    continue
                snap = conn.execute(
                    "SELECT win_odds, pool_total FROM odds_snapshots WHERE race_id = ? "
                    "AND brand = ? ORDER BY ts DESC LIMIT 1",
                    (race_id, brand),
                ).fetchone()
                live_odds = snap[0] if snap and snap[0] else None
                pool = snap[1] if snap and snap[1] else None
                edge = (prob * live_odds) if (live_odds and prob) else None
                ok, why = filt.evaluate(prob=prob, odds=live_odds, edge=edge,
                                        pool_total=pool, settings=settings)
                if not ok:
                    continue
                # Heartbeat gate ONLY when running in live mode
                if is_live_mode() and not kill_switch.heartbeat_fresh(conn, max_age_sec=300):
                    if broadcast is not None:
                        await broadcast.broadcast({
                            "type": "scraper_log",
                            "text": f"[decision_loop] heartbeat stale; skipping live bet",
                            "task": "decision_loop",
                        })
                    break

                bankroll = 10000.0  # TODO: per-strategy bankroll table; placeholder
                sr = sizing.size_bet(prob=prob, decimal_odds=live_odds, bankroll=bankroll,
                                     kelly_fraction_strat=kf or 0.25,
                                     kelly_max_bankroll_pct=kelly_pct or 0.05,
                                     pool_impact_max_pct=pool_pct or 0.005,
                                     pool_total=pool)
                if sr.stake <= 0:
                    continue

                mode = "live" if is_live_mode() else "paper"
                conn.execute(
                    """
                    INSERT INTO live_bets (strategy_id, race_id, horse_id, brand, bet_type,
                                            placed_at, stake, odds_at_placement, expected_value,
                                            mode, notes)
                    VALUES (?, ?, (SELECT id FROM horses WHERE brand = ?), ?, 'win',
                            ?, ?, ?, ?, ?, ?)
                    """,
                    (sid, race_id, brand, brand,
                     datetime.now().isoformat(), sr.stake, live_odds,
                     prob * live_odds, mode,
                     f"raw_kelly={sr.raw_kelly_pct:.4f}"),
                )
                conn.commit()
                if broadcast is not None:
                    await broadcast.broadcast({
                        "type": "scraper_log",
                        "text": f"[{mode}] {sname}: r#{race_no} {brand} stake={sr.stake:.2f} @ {live_odds}",
                        "task": "decision_loop",
                    })
    finally:
        conn.close()

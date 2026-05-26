"""Counterfactual bet audit (sweep over caps).

Given a strategy and date range, replay predictions vs. results and report:
  * placed: bets that passed all filters
  * blocked: bets blocked by edge / odds / Kelly / circuit-breaker
  * Each grouped by blocking reason
  * A sweep across alternative (max_odds, edge_threshold, kelly_fraction) grids
    so the operator can see how the strategy would have performed with
    different settings.

Reuses `betting.filters` + `betting.sizing` so the audit is faithful to
the live decision logic.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable

from betting import filters as filt
from betting import sizing as siz


def _won(position) -> bool:
    """HKJC `results.position` is mostly int but also carries 'WV' / 'FE' / 'PU'
    / 'UR' / 'DQ' / '---' codes for non-finishers."""
    if position is None:
        return False
    if isinstance(position, (int, float)):
        return int(position) == 1
    try:
        return int(str(position).strip()) == 1
    except (TypeError, ValueError):
        return False


@dataclass
class AuditRow:
    race_id: int
    brand: str
    prob: float
    odds: float
    edge: float
    passed: bool
    reason: str
    stake: float
    settled: int  # 1 if won, 0 lose, -1 unknown
    pnl: float


def audit(
    conn: sqlite3.Connection,
    strategy_id: int,
    date_from: str,
    date_to: str,
    *,
    settings: filt.FilterSettings | None = None,
    kelly_fraction_strat: float = 0.25,
    bankroll: float = 1000.0,
) -> dict:
    settings = settings or filt.FilterSettings()
    rows = conn.execute(
        """
        SELECT p.race_id, p.brand, p.calibrated_prob, p.odds_at_prediction, p.edge,
               r.position
        FROM predictions p
        JOIN races ra ON ra.id = p.race_id
        LEFT JOIN results r ON r.race_id = p.race_id AND r.brand = p.brand
        WHERE p.strategy_id = ? AND ra.date BETWEEN ? AND ?
        ORDER BY ra.date, p.race_id, p.brand
        """,
        (strategy_id, date_from, date_to),
    ).fetchall()

    placed = blocked = wins = 0
    by_reason: dict[str, int] = {}
    total_stake = total_payout = 0.0
    for race_id, brand, prob, odds, edge, position in rows:
        ok, reason = filt.evaluate(prob=prob, odds=odds, edge=edge, settings=settings)
        if not ok:
            blocked += 1
            by_reason[reason] = by_reason.get(reason, 0) + 1
            continue
        sr = siz.size_bet(prob=prob or 0.0, decimal_odds=odds or 0.0, bankroll=bankroll,
                          kelly_fraction_strat=kelly_fraction_strat)
        if sr.stake <= 0:
            blocked += 1
            by_reason[sr.reason or "size_zero"] = by_reason.get(sr.reason or "size_zero", 0) + 1
            continue
        placed += 1
        total_stake += sr.stake
        if _won(position):
            wins += 1
            total_payout += sr.stake * float(odds)
    roi = ((total_payout - total_stake) / total_stake) if total_stake > 0 else 0.0
    return {
        "strategy_id": strategy_id,
        "from": date_from, "to": date_to,
        "placed": placed,
        "blocked": blocked,
        "block_reasons": by_reason,
        "wins": wins,
        "win_rate": (wins / placed) if placed else None,
        "total_stake": round(total_stake, 2),
        "total_payout": round(total_payout, 2),
        "roi": round(roi, 4),
    }


def sweep(
    conn: sqlite3.Connection,
    strategy_id: int,
    date_from: str,
    date_to: str,
    *,
    max_odds_grid: Iterable[float] = (5, 8, 12, 16, 20, 25, 40),
    edge_grid: Iterable[float] = (1.00, 1.05, 1.10, 1.20),
    kelly_grid: Iterable[float] = (0.0625, 0.125, 0.25, 0.5),
) -> list[dict]:
    out: list[dict] = []
    for mo in max_odds_grid:
        for et in edge_grid:
            for kf in kelly_grid:
                s = filt.FilterSettings(bet_max_odds=float(mo), edge_threshold=float(et))
                row = audit(conn, strategy_id, date_from, date_to,
                            settings=s, kelly_fraction_strat=float(kf))
                out.append({**row, "_grid": {"max_odds": mo, "edge": et, "kelly": kf}})
    return out

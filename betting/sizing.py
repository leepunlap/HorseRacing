"""Bet sizing: fractional Kelly with multiple safety clamps.

Decision: stake S = clamp(K_frac * kelly_stake, 0, min(abs_cap, % bankroll, % pool))
where Kelly stake fraction f = (p * d - 1) / (d - 1), and
  K_frac = strategy.kelly_fraction (default 0.25)
  abs_cap = strategy.kelly_max_bankroll_pct * bankroll
  % pool = strategy.pool_impact_max_pct * pool_total

Returns 0 stake (and a reason string) if any guard trips.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class SizingResult:
    stake: float
    reason: str = ""           # populated when stake == 0 (why)
    raw_kelly_pct: float = 0.0 # f, unclamped (for audit)


def kelly_fraction(prob: float, decimal_odds: float) -> float:
    """Full Kelly fraction. Returns 0 if no edge."""
    if not math.isfinite(prob) or not math.isfinite(decimal_odds):
        return 0.0
    if decimal_odds <= 1.0 or prob <= 0:
        return 0.0
    f = (prob * decimal_odds - 1.0) / (decimal_odds - 1.0)
    return max(0.0, f)


def size_bet(
    *,
    prob: float,
    decimal_odds: float,
    bankroll: float,
    kelly_fraction_strat: float = 0.25,
    kelly_max_bankroll_pct: float = 0.05,
    pool_impact_max_pct: float = 0.005,
    pool_total: float | None = None,
    absolute_max: float | None = None,
) -> SizingResult:
    f = kelly_fraction(prob, decimal_odds)
    if f <= 0:
        return SizingResult(stake=0.0, reason="no_edge", raw_kelly_pct=0.0)

    # kelly_fraction_strat == 0 means FLAT STAKING — every qualifying bet
    # uses the bankroll-percentage cap as its stake regardless of edge size.
    # Lets users disable Kelly without leaving the same code path. Previously
    # this returned stake = 0 which silently killed every bet.
    if kelly_fraction_strat == 0:
        raw_stake = kelly_max_bankroll_pct * bankroll
    else:
        raw_stake = f * kelly_fraction_strat * bankroll
    clamp_pct = kelly_max_bankroll_pct * bankroll
    clamp_pool = (pool_impact_max_pct * pool_total) if pool_total else float("inf")
    clamp_abs = absolute_max if absolute_max is not None else float("inf")

    stake = min(raw_stake, clamp_pct, clamp_pool, clamp_abs)
    if stake <= 0:
        return SizingResult(stake=0.0, reason="clamped_zero", raw_kelly_pct=f)

    reason = "flat" if kelly_fraction_strat == 0 else ""
    if reason != "flat":
        if raw_stake > clamp_pct:
            reason = "bankroll_cap"
        elif pool_total and raw_stake > clamp_pool:
            reason = "pool_cap"
        elif absolute_max and raw_stake > clamp_abs:
            reason = "abs_cap"
    return SizingResult(stake=stake, reason=reason, raw_kelly_pct=f)

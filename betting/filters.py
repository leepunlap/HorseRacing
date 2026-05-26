"""Pre-bet filters: NaN guards, odds bounds, prob floor, pool depth.

Selection rule lives upstream: each race picks the single horse with the
highest edge (prob × odds). This module then checks the chosen horse against
the safety rails (odds range, min prob, NaN guards). Edge is no longer a
hard gate — ranking by edge already implements the EV preference.

`evaluate(prob, odds, pool_total, settings)` returns (passed, reason).
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class FilterSettings:
    min_prob: float = 0.02
    bet_min_odds: float = 2.0
    bet_max_odds: float = 25.0
    pool_depth_floor: float = 0.0


def evaluate(
    *,
    prob: float | None,
    odds: float | None,
    pool_total: float | None = None,
    settings: FilterSettings,
) -> tuple[bool, str]:
    """Return (passed, reason). reason is empty when passed."""
    if prob is None or not math.isfinite(prob):
        return False, "nan_prob"
    if odds is None or not math.isfinite(odds):
        return False, "nan_odds"
    if odds < settings.bet_min_odds:
        return False, f"odds<{settings.bet_min_odds}"
    if odds > settings.bet_max_odds:
        return False, f"odds>{settings.bet_max_odds}"
    if prob < settings.min_prob:
        return False, f"prob<{settings.min_prob}"
    if settings.pool_depth_floor > 0 and (pool_total or 0) < settings.pool_depth_floor:
        return False, "pool_too_thin"
    return True, ""

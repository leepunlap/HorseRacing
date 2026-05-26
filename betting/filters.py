"""Pre-bet filters: NaN guards, odds bounds, edge floor, prob floor, pool depth.

`evaluate(prob, odds, edge, pool, strategy)` returns (passed: bool, reason: str).
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class FilterSettings:
    edge_threshold: float = 1.05
    min_prob: float = 0.02
    bet_min_odds: float = 2.0
    bet_max_odds: float = 25.0
    pool_depth_floor: float = 0.0


def evaluate(
    *,
    prob: float | None,
    odds: float | None,
    edge: float | None,
    pool_total: float | None = None,
    settings: FilterSettings,
) -> tuple[bool, str]:
    """Return (passed, reason). reason is empty when passed."""
    # NaN-class guards (per project memory: math.isnan needed; raw <=/>= lie about NaN)
    if prob is None or not math.isfinite(prob):
        return False, "nan_prob"
    if odds is None or not math.isfinite(odds):
        return False, "nan_odds"
    if edge is None or not math.isfinite(edge):
        return False, "nan_edge"
    # Bounds
    if odds < settings.bet_min_odds:
        return False, f"odds<{settings.bet_min_odds}"
    if odds > settings.bet_max_odds:
        return False, f"odds>{settings.bet_max_odds}"
    if prob < settings.min_prob:
        return False, f"prob<{settings.min_prob}"
    if edge < settings.edge_threshold:
        return False, f"edge<{settings.edge_threshold}"
    if settings.pool_depth_floor > 0 and (pool_total or 0) < settings.pool_depth_floor:
        return False, "pool_too_thin"
    return True, ""

"""Stage-1 fundamental model: XGBoost LambdaMART ranker.

Per-race group ranking with pairwise objective `rank:pairwise` — matches the
Korean LtR study [B46][B47] that showed pairwise > pointwise for racing.

Input: a feature matrix `X` (n_horses × n_features) with a parallel `group`
vector listing the number of horses per race.

Output: a raw score per horse. To convert to a probability distribution per
race we softmax within each race group; this is the `fundamental_prob` (f_i)
that stage-2 then blends with market implied prob.
"""

from __future__ import annotations

import numpy as np
import xgboost as xgb


# Tuned 2026-05-27 against a 5-split cross-validation window (235-648 races
# each) under the "one bet per race, flat stake" constraint. Best config was
# rank:ndcg (top-of-list emphasis) with shallow trees + many rounds + no
# Benter market blend — consistent +36% to +66% ROI across all splits.
#
# See scripts/quick_eval.py + the AUDIT_V2 doc for the full sweep.
DEFAULT_PARAMS = {
    "objective": "rank:ndcg",
    "tree_method": "hist",
    "eta": 0.05,
    "max_depth": 4,
    "subsample": 0.85,
    "colsample_bytree": 0.8,
    "min_child_weight": 1.0,
    "gamma": 0.0,
    "verbosity": 0,
}
DEFAULT_NUM_BOOST_ROUND = 400


def train(X: np.ndarray, y: np.ndarray, group: list[int], *, params: dict | None = None,
          num_boost_round: int = DEFAULT_NUM_BOOST_ROUND,
          weight: np.ndarray | None = None) -> xgb.Booster:
    """Train a LambdaMART ranker.

    Args:
        X: (N, D) features.
        y: (N,) ranking label per row; higher = better (we use `4 - position`
           clamped to [0,4] so winners get 4, 5th+ gets 0).
        group: list of group sizes summing to N. Each entry is one race's
               number of horses.
        weight: optional (N,) per-row sample weight. Used for time-decay
               where recent races count more than old ones.
    """
    if len(X) == 0:
        raise ValueError("no training rows")
    dtrain = xgb.DMatrix(X, label=y, weight=weight)
    dtrain.set_group(group)
    p = {**DEFAULT_PARAMS, **(params or {})}
    bst = xgb.train(p, dtrain, num_boost_round=num_boost_round)
    return bst


def predict_scores(bst: xgb.Booster, X: np.ndarray) -> np.ndarray:
    """Raw model scores (one per row)."""
    return np.asarray(bst.predict(xgb.DMatrix(X)), dtype=float)


def scores_to_probs(scores: np.ndarray, group: list[int]) -> np.ndarray:
    """Per-race softmax of raw scores → probability distribution per race."""
    out = np.empty_like(scores, dtype=float)
    i = 0
    for g in group:
        seg = scores[i : i + g]
        # Numerically stable softmax.
        m = float(np.max(seg)) if g else 0.0
        e = np.exp(seg - m)
        s = float(np.sum(e))
        out[i : i + g] = (e / s) if s > 0 else (np.ones(g) / max(g, 1))
        i += g
    return out


def position_to_label(positions: np.ndarray) -> np.ndarray:
    """Convert finishing positions (1=best, large=worst) into LtR labels.

    Winner gets 4, then 3,2,1,0 for 2nd..5th, 0 for everyone below 5th. The
    relative ordering matters for pairwise; absolute values are a soft prior.
    """
    out = np.zeros(len(positions), dtype=float)
    for i, p in enumerate(positions):
        if p is None or not np.isfinite(p):
            continue
        p_int = int(p)
        if p_int <= 0:
            continue
        out[i] = max(0, 5 - p_int)
    return out

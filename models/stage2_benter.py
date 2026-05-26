"""Stage-2 Benter two-stage logit.

Combines fundamental probability `f_i` (from stage 1) with public's implied
probability `π_i` (from latest odds snapshot):

    c_i ∝ exp(α · log(f_i) + β · log(π_i))

Then per-race renormalised so Σ c_i = 1. (α, β) are fit by maximum likelihood
on historical (race, winner) pairs using a simple grid search — adequate for
two parameters; SGD/L-BFGS overkill at this scale.

This is item #1 in the global_research ranking. The Δ ECE / Δ log-loss vs.
stage-1 alone is logged into calibration_metrics.
"""

from __future__ import annotations

import numpy as np


EPS = 1e-9


def blend(f: np.ndarray, pi: np.ndarray, group: list[int],
          alpha: float, beta: float) -> np.ndarray:
    """Apply the Benter two-stage logit per race group.

    Both `f` and `pi` should be strictly positive (NaN/0 are filled with 1/N).
    """
    out = np.empty_like(f, dtype=float)
    i = 0
    for g in group:
        f_seg = np.where(np.isfinite(f[i : i + g]) & (f[i : i + g] > 0),
                         f[i : i + g], 1.0 / max(g, 1))
        pi_seg = np.where(np.isfinite(pi[i : i + g]) & (pi[i : i + g] > 0),
                          pi[i : i + g], 1.0 / max(g, 1))
        log_score = alpha * np.log(f_seg + EPS) + beta * np.log(pi_seg + EPS)
        m = float(np.max(log_score)) if g else 0.0
        e = np.exp(log_score - m)
        s = float(np.sum(e))
        out[i : i + g] = (e / s) if s > 0 else (np.ones(g) / max(g, 1))
        i += g
    return out


def fit_alpha_beta(f: np.ndarray, pi: np.ndarray, group: list[int],
                   winner_idx_per_race: list[int],
                   *, grid: tuple[float, ...] = tuple(np.arange(0.1, 1.51, 0.1))) -> tuple[float, float, float]:
    """Find (α, β) maximising log-likelihood of observed winners.

    Returns (alpha, beta, mean_log_likelihood).
    `winner_idx_per_race` is a list of within-race indices (0-based) of the
    winner per race group; -1 if no winner observed (skipped).
    """
    best = (-1.0, -1.0, -1e18)
    for a in grid:
        for b in grid:
            probs = blend(f, pi, group, float(a), float(b))
            ll = 0.0
            n = 0
            i = 0
            for g, wi in zip(group, winner_idx_per_race):
                if wi >= 0 and wi < g:
                    ll += np.log(probs[i + wi] + EPS)
                    n += 1
                i += g
            mean_ll = ll / max(n, 1)
            if mean_ll > best[2]:
                best = (float(a), float(b), float(mean_ll))
    return best

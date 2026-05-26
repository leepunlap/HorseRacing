"""Harville (1973) and Henery (1981) place/show/exacta inversion.

From per-horse win probabilities `p_i` we derive higher-order ordering
probabilities used to price PLACE, QUINELLA, QUINELLA-PLACE, TRIO, F4.

Harville (cheap): P(j finishes 2nd | i wins) = p_j / (1 − p_i). Tractable but
biased (over-rates favourites in 2nd/3rd).

Henery (refinement): treats running times as exponential / normal; better at
2nd/3rd. We approximate via the Plackett-Luce normalisation with a γ exponent
on log-probabilities ≈ 0.81 for 2nd, 0.65 for 3rd per Benter's constants [B2].

We expose `p_top2(p)` and `p_top3(p)` as the most-used cases.
"""

from __future__ import annotations

import numpy as np


def harville_top2(p: np.ndarray) -> np.ndarray:
    """For each i, return P(i finishes top-2) under Harville.

    P(i in top 2) = p_i + Σ_{j≠i} p_j · p_i/(1-p_j).
    """
    p = np.asarray(p, dtype=float)
    n = len(p)
    out = p.copy()
    for i in range(n):
        for j in range(n):
            if j == i: continue
            denom = 1.0 - p[j]
            if denom > 1e-9:
                out[i] += p[j] * (p[i] / denom)
    return np.clip(out, 0.0, 1.0)


def harville_top3(p: np.ndarray) -> np.ndarray:
    """For each i, return P(i finishes top-3) under Harville."""
    p = np.asarray(p, dtype=float)
    n = len(p)
    out = p.copy()
    for i in range(n):
        for j in range(n):
            if j == i: continue
            denom_j = 1.0 - p[j]
            if denom_j <= 1e-9: continue
            out[i] += p[j] * (p[i] / denom_j)   # 2nd
            for k in range(n):
                if k == i or k == j: continue
                denom_jk = 1.0 - p[j] - p[k]
                if denom_jk <= 1e-9: continue
                out[i] += p[j] * (p[k] / denom_j) * (p[i] / denom_jk)
    return np.clip(out, 0.0, 1.0)


def henery_top2(p: np.ndarray, gamma_2nd: float = 0.81) -> np.ndarray:
    """Plackett-Luce with γ exponent for 2nd; closer to empirical than Harville
    on longshots. Benter's reported γ ≈ 0.81."""
    p = np.asarray(p, dtype=float)
    n = len(p)
    out = p.copy()
    for i in range(n):
        for j in range(n):
            if j == i: continue
            num = (p[i] ** gamma_2nd)
            denom = np.sum([p[k] ** gamma_2nd for k in range(n) if k != j])
            if denom > 1e-9:
                out[i] += p[j] * (num / denom)
    return np.clip(out, 0.0, 1.0)


def harville_quinella(p: np.ndarray) -> np.ndarray:
    """N×N matrix of P(i and j both in top 2)."""
    p = np.asarray(p, dtype=float)
    n = len(p)
    M = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            if i == j: continue
            denom = 1.0 - p[i]
            if denom <= 1e-9: continue
            pij = p[i] * (p[j] / denom)
            denom2 = 1.0 - p[j]
            if denom2 > 1e-9:
                pij += p[j] * (p[i] / denom2)
            M[i, j] = min(1.0, pij)
    return M

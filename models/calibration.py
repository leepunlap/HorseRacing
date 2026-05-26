"""Calibration layer.

Three calibration modes selectable per strategy:
  * "isotonic"  — sklearn IsotonicRegression on hold-out predictions. Preferred
                  for >1000 samples. Non-parametric, monotone.
  * "platt"     — logistic regression on score → outcome; better for small N.
  * "bucketed"  — odds-bucketed multiplier (current v1 behaviour, reused as
                  cheap fallback when sklearn unavailable).
  * "none"      — identity.

Public API:
  fit(scores, outcomes, mode) -> Calibrator
  cal = Calibrator.transform(scores) -> calibrated_probs

Plus metrics:
  brier(p, y), log_loss(p, y), ece(p, y, n_bins=10).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

try:
    from sklearn.isotonic import IsotonicRegression
    from sklearn.linear_model import LogisticRegression
    HAS_SKLEARN = True
except Exception:
    HAS_SKLEARN = False


EPS = 1e-9


@dataclass
class Calibrator:
    mode: str
    model: object | None = None
    bucket_edges: list[float] | None = None
    bucket_factors: list[float] | None = None

    def transform(self, scores: np.ndarray) -> np.ndarray:
        s = np.asarray(scores, dtype=float)
        if self.mode == "none" or self.model is None and self.bucket_factors is None:
            return np.clip(s, EPS, 1 - EPS)
        if self.mode == "isotonic":
            return np.clip(self.model.transform(s), EPS, 1 - EPS)
        if self.mode == "platt":
            return np.clip(self.model.predict_proba(s.reshape(-1, 1))[:, 1], EPS, 1 - EPS)
        if self.mode == "bucketed":
            return np.array([_bucket_apply(x, self.bucket_edges, self.bucket_factors) for x in s])
        return s


def fit(scores: np.ndarray, outcomes: np.ndarray, mode: str = "isotonic") -> Calibrator:
    scores = np.asarray(scores, dtype=float)
    outcomes = np.asarray(outcomes, dtype=float)
    if not HAS_SKLEARN and mode in {"isotonic", "platt"}:
        mode = "bucketed"
    if mode == "isotonic":
        m = IsotonicRegression(out_of_bounds="clip", y_min=EPS, y_max=1 - EPS)
        m.fit(scores, outcomes)
        return Calibrator(mode="isotonic", model=m)
    if mode == "platt":
        m = LogisticRegression(C=1.0)
        m.fit(scores.reshape(-1, 1), outcomes)
        return Calibrator(mode="platt", model=m)
    if mode == "bucketed":
        edges, factors = _bucket_fit(scores, outcomes)
        return Calibrator(mode="bucketed", bucket_edges=edges, bucket_factors=factors)
    return Calibrator(mode="none")


def _bucket_fit(scores: np.ndarray, outcomes: np.ndarray,
                n_bins: int = 10) -> tuple[list[float], list[float]]:
    edges = list(np.linspace(0.0, 1.0, n_bins + 1))
    factors: list[float] = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (scores >= lo) & (scores < hi if hi < 1.0 else scores <= hi)
        if mask.sum() < 5:
            factors.append(1.0)
            continue
        emp = float(outcomes[mask].mean())
        pred = float(scores[mask].mean())
        factors.append(emp / pred if pred > 0 else 1.0)
    return edges, factors


def _bucket_apply(x: float, edges: list[float], factors: list[float]) -> float:
    if x <= edges[0]: return float(min(max(x * factors[0], EPS), 1 - EPS))
    if x >= edges[-1]: return float(min(max(x * factors[-1], EPS), 1 - EPS))
    for i, (lo, hi) in enumerate(zip(edges[:-1], edges[1:])):
        if lo <= x < hi:
            return float(min(max(x * factors[i], EPS), 1 - EPS))
    return x


# ─── metrics ──────────────────────────────────────────────────────────────────

def brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((np.asarray(p) - np.asarray(y)) ** 2))


def log_loss(p: np.ndarray, y: np.ndarray) -> float:
    p = np.clip(np.asarray(p), EPS, 1 - EPS)
    y = np.asarray(y)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def ece(p: np.ndarray, y: np.ndarray, n_bins: int = 10) -> float:
    """Expected Calibration Error."""
    p = np.asarray(p); y = np.asarray(y)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    ece_val = 0.0
    n = len(p)
    if n == 0:
        return 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (p >= lo) & (p < hi if hi < 1.0 else p <= hi)
        if not mask.any():
            continue
        bin_acc = float(y[mask].mean())
        bin_conf = float(p[mask].mean())
        ece_val += (mask.sum() / n) * abs(bin_acc - bin_conf)
    return float(ece_val)

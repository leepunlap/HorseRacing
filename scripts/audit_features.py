"""Feature-health audit: classify every feature by coverage + variance.

For each feature_id present in `feature_values` (over a training window),
report:
  - n_rows, n_valid, n_distinct, lo, hi, mean, stdev
  - health: usable / sparse / low_variance / constant / all_null

Writes the "usable" set to stdout as a JSON list so the strategy row's
`features_enabled_json` can be set directly:

    python3 -m scripts.audit_features --since 2025-01-01 --until 2026-05-24 \
        --json > /tmp/usable.json
    sqlite3 data/racing.db "UPDATE strategies SET features_enabled_json = ? \
        WHERE name='benter_baseline'" < /tmp/usable.json

The pruning rationale: XGBoost handles useless features in theory, but in
practice they (a) slow training, (b) expand the hyperparameter search
space, (c) create spurious correlations in small samples — and 85 / 174
features are confirmed all-null on HKJC-only data scope.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "racing.db"


def classify(n_valid: int, n_rows: int, n_distinct: int) -> str:
    if n_valid == 0: return "all_null"
    if n_distinct <= 1: return "constant"
    coverage = n_valid / n_rows if n_rows else 0
    if coverage < 0.5: return "sparse"
    if n_distinct <= 5: return "low_variance"
    return "usable"


def audit(date_from: str, date_to: str) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """
        SELECT fv.feature_id,
               COUNT(*) AS n_rows,
               SUM(CASE WHEN fv.value IS NOT NULL THEN 1 ELSE 0 END) AS n_valid,
               COUNT(DISTINCT fv.value) AS n_distinct,
               MIN(fv.value) AS lo,
               MAX(fv.value) AS hi,
               AVG(fv.value) AS mean
        FROM feature_values fv
        JOIN races ra ON ra.id = fv.race_id
        WHERE ra.date BETWEEN ? AND ?
        GROUP BY fv.feature_id
        """,
        (date_from, date_to),
    ).fetchall()
    conn.close()
    out = []
    for fid, n, nv, nd, lo, hi, mean in rows:
        out.append({
            "id": fid, "n_rows": n, "n_valid": nv,
            "n_distinct": nd, "lo": lo, "hi": hi,
            "mean": round(mean, 4) if mean is not None else None,
            "coverage": round(nv / n, 3) if n else 0,
            "health": classify(nv, n, nd),
        })
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--since", required=True)
    p.add_argument("--until", required=True)
    p.add_argument("--json", action="store_true",
                   help="output just the usable feature_id list as JSON")
    p.add_argument("--summary", action="store_true",
                   help="print health counts only")
    ns = p.parse_args()

    feats = audit(ns.since, ns.until)
    counts: dict[str, int] = {}
    for f in feats:
        counts[f["health"]] = counts.get(f["health"], 0) + 1

    if ns.json:
        usable = [f["id"] for f in feats if f["health"] == "usable"]
        print(json.dumps(usable))
        return

    if ns.summary:
        for k, v in sorted(counts.items(), key=lambda x: -x[1]):
            print(f"  {k:14s}{v}")
        print(f"  {'total':14s}{sum(counts.values())}")
        return

    # Default: full table
    for f in sorted(feats, key=lambda x: (x["health"], x["id"])):
        print(f"{f['id']:6s} {f['health']:12s} n_valid={f['n_valid']:5d}"
              f" n_distinct={f['n_distinct']:5d} coverage={f['coverage']:.2f}"
              f" mean={f['mean']}")
    print("\nSummary:")
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {k:14s}{v}")


if __name__ == "__main__":
    main()

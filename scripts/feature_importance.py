"""Train the winner config once and dump feature importance.

Identifies which of the 53 surviving features are actually doing work; rare
weak features are candidates for further pruning. Useful for the iterative
research loop — drop bottom-N, retest ROI.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "racing.db"
sys.path.insert(0, str(BASE_DIR))

from features.catalog import FEATURES                # noqa: E402
from models import stage1_xgb                         # noqa: E402
from scripts.quick_eval import _load_split            # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--split", default="2026-05-01")
    p.add_argument("--features-json", required=True)
    args = p.parse_args()

    fids_all = [f.id for f in FEATURES]
    feature_filter = json.loads(Path(args.features_json).read_text())
    fids = [fid for fid in fids_all if fid in feature_filter]

    conn = sqlite3.connect(DB_PATH)
    X, y, g, _, _ = _load_split(conn, before=args.split, between=None, feature_ids=fids)
    conn.close()
    print(f"Training on {len(X)} rows, {len(fids)} features...")
    bst = stage1_xgb.train(X, y, g)
    imp = bst.get_score(importance_type="gain")
    # Map f0, f1, ... back to feature_id
    name_zh = {f.id: f.name_zh for f in FEATURES}
    rows = []
    for k, v in imp.items():
        idx = int(k[1:])
        fid = fids[idx]
        rows.append((fid, v, name_zh.get(fid, "")))
    rows.sort(key=lambda x: -x[1])
    used = set(r[0] for r in rows)
    print(f"\n{len(used)}/{len(fids)} features used by the model.\n")
    print(f"{'feature':8s} {'gain':>12s}  zh")
    for fid, v, zh in rows[:25]:
        print(f"{fid:8s} {v:12.2f}  {zh}")
    print("\n... bottom 10 used (candidates for further pruning):")
    for fid, v, zh in rows[-10:]:
        print(f"{fid:8s} {v:12.2f}  {zh}")

    unused = [fid for fid in fids if fid not in used]
    print(f"\n{len(unused)} features in input but UNUSED by tree splits:")
    for fid in unused:
        print(f"  {fid}  {name_zh.get(fid, '')}")


if __name__ == "__main__":
    main()

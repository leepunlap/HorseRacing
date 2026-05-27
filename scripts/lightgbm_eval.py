"""LightGBM equivalent of scripts/quick_eval.py — same data, same selection
rule, just swap the booster. Tests whether leaf-wise growth + LightGBM's
ranking implementation beats XGBoost rank:ndcg on this task.

Usage:
    python3 -m scripts.lightgbm_eval --split 2026-03-01 --until 2026-05-24 \\
        --features-json data/usable_features.json
"""
from __future__ import annotations
import argparse, json, math, sqlite3, sys, time
from pathlib import Path
import numpy as np
import lightgbm as lgb

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "data" / "racing.db"
sys.path.insert(0, str(BASE))

from features.catalog import FEATURES
from scripts.quick_eval import _load_split, _flat_metrics_v2


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--split", required=True)
    p.add_argument("--until", required=True)
    p.add_argument("--features-json", default=str(BASE / "data" / "usable_features.json"))
    p.add_argument("--num-leaves", type=int, default=31)
    p.add_argument("--max-depth", type=int, default=-1)
    p.add_argument("--learning-rate", type=float, default=0.05)
    p.add_argument("--num-round", type=int, default=400)
    p.add_argument("--objective", default="lambdarank",
                   choices=["lambdarank", "rank_xendcg"])
    args = p.parse_args()

    fids_all = [f.id for f in FEATURES]
    feature_filter = json.loads(Path(args.features_json).read_text())
    fids = [fid for fid in fids_all if fid in feature_filter]

    conn = sqlite3.connect(DB)
    print(f"Loading split…")
    X_tr, y_tr, g_tr, _, _ = _load_split(conn, before=args.split, between=None, feature_ids=fids)
    X_te, y_te, g_te, rids_te, pos_te = _load_split(conn, before=None, between=(args.split, args.until), feature_ids=fids)
    rows_te = conn.execute(
        "SELECT ra.id, r.brand FROM races ra JOIN results r ON r.race_id = ra.id "
        "WHERE ra.date BETWEEN ? AND ? ORDER BY ra.date, ra.id, r.id",
        (args.split, args.until),
    ).fetchall()
    keys_te = [(rid, b) for rid, b in rows_te]
    placeholders = ",".join("?" for _ in set(rids_te))
    odds_rows = conn.execute(
        f"SELECT race_id, brand, odds FROM results WHERE race_id IN ({placeholders})",
        list(set(rids_te)),
    ).fetchall()
    odds_map = {}
    for rid, brand, odds in odds_rows:
        try:
            if odds is None: continue
            f = float(odds)
            if f > 0: odds_map[(rid, brand)] = f
        except (TypeError, ValueError): pass
    conn.close()

    print(f"Training LightGBM ({args.objective}, leaves={args.num_leaves}, "
          f"depth={args.max_depth}, lr={args.learning_rate}, rounds={args.num_round})…")
    t0 = time.time()
    dtrain = lgb.Dataset(X_tr, label=y_tr, group=g_tr)
    params = {
        "objective": args.objective,
        "num_leaves": args.num_leaves,
        "max_depth": args.max_depth,
        "learning_rate": args.learning_rate,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.85,
        "bagging_freq": 5,
        "verbose": -1,
        "label_gain": [0, 1, 2, 4, 8],  # winner=4 weighted highest
    }
    bst = lgb.train(params, dtrain, num_boost_round=args.num_round)

    scores = bst.predict(X_te)
    # Softmax per race for prob output
    probs = np.empty_like(scores, dtype=float)
    i = 0
    for g in g_te:
        seg = scores[i:i+g]
        m = float(seg.max()); e = np.exp(seg - m); s = float(e.sum())
        probs[i:i+g] = e / s if s > 0 else np.ones(g) / max(g, 1)
        i += g

    m = _flat_metrics_v2(probs, g_te, keys_te, pos_te, odds_map)
    m["elapsed_s"] = round(time.time() - t0, 1)
    m["objective"] = args.objective
    m["num_leaves"] = args.num_leaves
    print(json.dumps(m, indent=2))


if __name__ == "__main__":
    main()

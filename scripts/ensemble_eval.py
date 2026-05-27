"""Test bagging-style ensemble: train N XGBoost models with different seeds,
average their per-race softmaxed probabilities, then rank by averaged prob.

Hypothesis: a single XGBoost run has variance from subsample/colsample
randomness; averaging multiple should reduce that variance and bump ROI.

Usage:
    python3 -m scripts.ensemble_eval --split 2026-03-01 --until 2026-05-24 \
        --features-json /tmp/usable.json --n-models 5
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "racing.db"
sys.path.insert(0, str(BASE_DIR))

from features.catalog import FEATURES                                   # noqa: E402
from models import stage1_xgb                                            # noqa: E402
from scripts.quick_eval import _load_split, _flat_metrics_v2             # noqa: E402


def run(split: str, until: str, feature_filter: list[str], n_models: int,
        time_decay_tau: float | None = None) -> dict:
    conn = sqlite3.connect(DB_PATH)
    fids = [f.id for f in FEATURES if f.id in feature_filter]

    t0 = time.time()
    X_tr, y_tr, g_tr, rids_tr, _ = _load_split(conn, before=split, between=None, feature_ids=fids)
    X_te, y_te, g_te, rids_te, pos_te = _load_split(conn, before=None, between=(split, until), feature_ids=fids)

    rows_te = conn.execute(
        "SELECT ra.id, r.brand FROM races ra JOIN results r ON r.race_id = ra.id "
        "WHERE ra.date BETWEEN ? AND ? ORDER BY ra.date, ra.id, r.id",
        (split, until),
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
        except (TypeError, ValueError):
            pass
    conn.close()

    # Time-decay weight (one per race group) if requested
    sample_w = None
    if time_decay_tau and time_decay_tau > 0:
        from datetime import date as _date
        rows_dates = sqlite3.connect(DB_PATH).execute(
            f"SELECT id, date FROM races WHERE id IN ({','.join('?'*len(set(rids_tr)))})",
            list(set(rids_tr)),
        ).fetchall()
        date_map = {rid: dt for rid, dt in rows_dates}
        cutoff = _date.fromisoformat(split)
        # rids_tr is per-row; collapse to per-group
        per_group = []
        race_ids_ordered = list(dict.fromkeys(rids_tr))  # preserve order
        for rid in race_ids_ordered:
            dt = date_map.get(rid)
            if dt:
                days = (cutoff - _date.fromisoformat(dt)).days
                per_group.append(math.exp(-days / float(time_decay_tau)))
            else:
                per_group.append(1.0)
        sample_w = np.array(per_group, dtype=float)
    avg_probs = np.zeros(len(X_te), dtype=float)
    for seed in range(n_models):
        params = {**stage1_xgb.DEFAULT_PARAMS, "seed": seed}
        bst = stage1_xgb.train(X_tr, y_tr, g_tr, params=params,
                               num_boost_round=stage1_xgb.DEFAULT_NUM_BOOST_ROUND,
                               weight=sample_w)
        scores = stage1_xgb.predict_scores(bst, X_te)
        probs = stage1_xgb.scores_to_probs(scores, g_te)
        avg_probs += probs
        print(f"  seed={seed} done in {time.time()-t0:.0f}s")
    avg_probs /= n_models

    m = _flat_metrics_v2(avg_probs, g_te, keys_te, pos_te, odds_map)
    m["elapsed_s"] = round(time.time() - t0, 1)
    m["n_models"] = n_models
    m["features"] = len(fids)
    return m


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--split", required=True)
    p.add_argument("--until", required=True)
    p.add_argument("--features-json", required=True)
    p.add_argument("--n-models", type=int, default=5)
    p.add_argument("--time-decay-tau", type=float, default=None)
    ns = p.parse_args()
    feats = json.loads(Path(ns.features_json).read_text())
    print(json.dumps(run(ns.split, ns.until, feats, ns.n_models,
                         time_decay_tau=ns.time_decay_tau), indent=2))


if __name__ == "__main__":
    main()

"""Compute XGBoost gain/weight importance + feature-value statistics for a
strategy's model, cache to data/feature_importance.json. The strategy page's
per-feature cards read this cache (training is too slow for a live request).

Usage:  python3 -m scripts.compute_feature_importance [strategy_id]
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
DB = BASE / "data" / "racing.db"
OUT = BASE / "data" / "feature_importance.json"

from models import walk_forward as wf       # noqa: E402
from models import stage1_xgb               # noqa: E402


def main(strategy_id: int = 1) -> None:
    conn = sqlite3.connect(DB)
    feature_ids = wf._feature_ids_for_strategy(conn, strategy_id)

    # xgb params from the strategy row (same path the live model uses).
    raw = conn.execute("SELECT xgb_params_json FROM strategies WHERE id=?", (strategy_id,)).fetchone()
    params = dict(stage1_xgb.DEFAULT_PARAMS)
    rounds = stage1_xgb.DEFAULT_NUM_BOOST_ROUND
    if raw and raw[0]:
        d = dict(json.loads(raw[0])); rounds = int(d.pop("num_boost_round", rounds)); params.update(d)

    # Train on everything (date < a future bound) — one booster, like production.
    cut = "2099-01-01"
    X, y, gr, keys, _ = wf._load_matrix(conn, cut, feature_ids)
    print(f"[importance] training on {len(X)} rows / {len(gr)} races / {len(feature_ids)} features", flush=True)
    bst = stage1_xgb.train(X, y, gr, params=params, num_boost_round=rounds)

    gain = bst.get_score(importance_type="gain")
    weight = bst.get_score(importance_type="weight")
    # XGBoost names columns f0..fD-1 in matrix order == feature_ids order.
    def idx(fk):  # 'f12' -> 12
        return int(fk[1:])
    gain_by_fid = {feature_ids[idx(k)]: round(v, 2) for k, v in gain.items() if idx(k) < len(feature_ids)}
    wt_by_fid = {feature_ids[idx(k)]: int(v) for k, v in weight.items() if idx(k) < len(feature_ids)}

    # Feature-value statistics (coverage / mean / min / max), recent window for
    # relevance + speed.
    stats_rows = conn.execute(
        """SELECT fv.feature_id, COUNT(*) n,
                  AVG(fv.value) mean, MIN(fv.value) mn, MAX(fv.value) mx,
                  SUM(CASE WHEN fv.value IS NOT NULL THEN 1 ELSE 0 END) nonnull
           FROM feature_values fv JOIN races ra ON ra.id=fv.race_id
           WHERE ra.date >= date('now','-120 day')
           GROUP BY fv.feature_id""").fetchall()
    stats = {}
    for fid, n, mean, mn, mx, nonnull in stats_rows:
        stats[fid] = {
            "n": n, "coverage": round(nonnull / n, 3) if n else 0,
            "mean": round(mean, 4) if mean is not None else None,
            "min": round(mn, 4) if mn is not None else None,
            "max": round(mx, 4) if mx is not None else None,
        }

    features = {}
    for fid in feature_ids:
        features[fid] = {
            "gain": gain_by_fid.get(fid, 0.0),
            "splits": wt_by_fid.get(fid, 0),
            **(stats.get(fid, {})),
        }
    # Also record disabled features (importance 0, but show their stats if any).
    for fid, st in stats.items():
        features.setdefault(fid, {"gain": 0.0, "splits": 0, **st})

    payload = {
        "computed_at": datetime.now().isoformat(timespec="seconds"),
        "strategy_id": strategy_id,
        "n_train_rows": len(X),
        "n_features_used": len(gain_by_fid),
        "max_gain": max(gain_by_fid.values()) if gain_by_fid else 0,
        "features": features,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    conn.close()
    top = sorted(gain_by_fid.items(), key=lambda x: -x[1])[:8]
    print(f"[importance] wrote {OUT}  ({len(gain_by_fid)} features with gain)")
    print("[importance] top-8 by gain:", ", ".join(f"{k}={v:.0f}" for k, v in top))


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 1)

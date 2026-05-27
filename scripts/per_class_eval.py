"""Per-class quick-eval: does splitting by class bucket improve top-1 ROI?

Hypothesis: race dynamics differ by class — G/L races have professional
fields with tight form, C5 has more journeymen with messier signal. A
unified model averages across both and may underfit each bucket.

This script trains TWO ways on the same train window, then scores each
race in the test window with whichever model corresponds to its bucket:

  - Unified  : one model on all races (baseline).
  - Bucketed : separate model per class bucket (G/L vs C1-2 vs C3-5).

Per-race we still pick the top-prob horse and flat-bet $500.

Usage:
    python3 -m scripts.per_class_eval --split 2026-03-01 --until 2026-05-24
"""
from __future__ import annotations
import argparse, json, math, sqlite3, sys, time
from pathlib import Path
import numpy as np

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "data" / "racing.db"
sys.path.insert(0, str(BASE))

from features.catalog import FEATURES
from models import stage1_xgb
from scripts.quick_eval import _load_split, _flat_metrics_v2


def class_bucket(cls) -> str:
    """Map a HKJC class value to one of three buckets.

    HKJC stores class as: float-like ('4.0', '3.0'), text ('Class 4'),
    or group ('G1', 'G2', 'G3', 'Listed'). Normalise then bucket.
    """
    if cls is None:
        return "other"
    s = str(cls).strip().upper()
    if not s:
        return "other"
    if s.startswith("G") or "LISTED" in s:
        return "G_L"
    # Strip "Class " prefix; coerce "4.0" → 4
    s = s.replace("CLASS", "").strip()
    try:
        n = int(float(s))
    except (ValueError, TypeError):
        return "other"
    if n in (1, 2):
        return "C1_2"
    if n in (3, 4, 5):
        return "C3_5"
    return "other"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--split", required=True)
    p.add_argument("--until", required=True)
    p.add_argument("--features-json", default=str(BASE / "data" / "usable_features.json"))
    args = p.parse_args()

    fids_all = [f.id for f in FEATURES]
    if Path(args.features_json).exists():
        feature_filter = json.loads(Path(args.features_json).read_text())
    else:
        feature_filter = fids_all
    fids = [fid for fid in fids_all if fid in feature_filter]

    conn = sqlite3.connect(DB)
    print(f"Loading split (train < {args.split}, test {args.split} → {args.until})…")
    X_tr, y_tr, g_tr, rids_tr, _ = _load_split(conn, before=args.split, between=None, feature_ids=fids)
    X_te, y_te, g_te, rids_te, pos_te = _load_split(conn, before=None, between=(args.split, args.until), feature_ids=fids)

    # Per-race class lookup (both train + test).
    all_rids = list(set(rids_tr) | set(rids_te))
    placeholders = ",".join("?" * len(all_rids))
    rows = conn.execute(
        f"SELECT id, class FROM races WHERE id IN ({placeholders})",
        all_rids,
    ).fetchall()
    bucket_of_race = {rid: class_bucket(cls) for rid, cls in rows}

    # Test-set keys (for ROI calc)
    rows_te = conn.execute(
        "SELECT ra.id, r.brand FROM races ra JOIN results r ON r.race_id = ra.id "
        "WHERE ra.date BETWEEN ? AND ? ORDER BY ra.date, ra.id, r.id",
        (args.split, args.until),
    ).fetchall()
    keys_te = [(rid, b) for rid, b in rows_te]
    placeholders2 = ",".join("?" for _ in set(rids_te))
    odds_rows = conn.execute(
        f"SELECT race_id, brand, odds FROM results WHERE race_id IN ({placeholders2})",
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

    # Build the per-row race_id arrays we need to slice by bucket
    def expand_race_ids(rids: list[int], groups: list[int]) -> list[int]:
        out = []
        for rid, g in zip(rids, groups):
            out.extend([rid] * g)
        return out

    # rids_tr is already per-row from _load_split. groups sum to len(rids_tr).
    assert len(rids_tr) == len(X_tr), (len(rids_tr), len(X_tr))
    rids_te_per_row = rids_te
    assert len(rids_te_per_row) == len(X_te), (len(rids_te_per_row), len(X_te))

    bucket_tr = np.array([bucket_of_race.get(rid, "other") for rid in rids_tr])
    bucket_te = np.array([bucket_of_race.get(rid, "other") for rid in rids_te_per_row])

    # Group-level bucket arrays for sample-weight / sub-training
    g_tr_starts = np.cumsum([0] + g_tr[:-1])
    g_te_starts = np.cumsum([0] + g_te[:-1])
    group_bucket_tr = np.array([bucket_tr[s] for s in g_tr_starts])
    group_bucket_te = np.array([bucket_te[s] for s in g_te_starts])

    print(f"Train race-group buckets: {dict(zip(*np.unique(group_bucket_tr, return_counts=True)))}")
    print(f"Test race-group buckets:  {dict(zip(*np.unique(group_bucket_te, return_counts=True)))}")

    # --- Strategy U: unified model
    t0 = time.time()
    bst_u = stage1_xgb.train(X_tr, y_tr, g_tr,
                             num_boost_round=stage1_xgb.DEFAULT_NUM_BOOST_ROUND)
    scores_u = stage1_xgb.predict_scores(bst_u, X_te)
    probs_u = stage1_xgb.scores_to_probs(scores_u, g_te)
    m_u = _flat_metrics_v2(probs_u, g_te, keys_te, pos_te, odds_map)
    print(f"Unified  ROI={m_u['roi_pct']}%  top1={m_u['top1_hit_rate']*100:.1f}%  "
          f"({time.time()-t0:.0f}s)")

    # --- Strategy B: bucketed (one model per bucket; fall back to unified
    #    for 'other' rows).
    bucket_models: dict[str, "xgb.Booster"] = {}
    for bkt in sorted(set(group_bucket_tr)):
        mask_groups = (group_bucket_tr == bkt)
        idx_pairs = [(s, s+g) for s, g, m in zip(g_tr_starts, g_tr, mask_groups) if m]
        if not idx_pairs:
            continue
        row_mask = np.zeros(len(X_tr), dtype=bool)
        for s, e in idx_pairs:
            row_mask[s:e] = True
        X_b = X_tr[row_mask]
        y_b = y_tr[row_mask]
        g_b = [e - s for s, e in idx_pairs]
        if not g_b:
            continue
        t1 = time.time()
        bucket_models[bkt] = stage1_xgb.train(X_b, y_b, g_b,
            num_boost_round=stage1_xgb.DEFAULT_NUM_BOOST_ROUND)
        print(f"  bucket {bkt}: trained on {len(X_b)} rows / {len(g_b)} races ({time.time()-t1:.0f}s)")

    # Predict — per-test-race choose the bucket's model (fallback unified)
    probs_b = np.empty(len(X_te), dtype=float)
    for s, g, bkt in zip(g_te_starts, g_te, group_bucket_te):
        Xs = X_te[s:s+g]
        bst = bucket_models.get(bkt, bst_u)
        sc = stage1_xgb.predict_scores(bst, Xs)
        # softmax within race
        m = float(sc.max()); e = np.exp(sc - m); ssum = float(e.sum())
        probs_b[s:s+g] = e / ssum if ssum > 0 else np.ones(g) / max(g, 1)
    m_b = _flat_metrics_v2(probs_b, g_te, keys_te, pos_te, odds_map)
    print(f"Bucketed ROI={m_b['roi_pct']}%  top1={m_b['top1_hit_rate']*100:.1f}%")

    # Per-bucket breakdown of the bucketed strategy
    print("\nPer-bucket test-window summary (bucketed model):")
    for bkt in sorted(set(group_bucket_te)):
        idx = [(s, g) for s, g, b in zip(g_te_starts, g_te, group_bucket_te) if b == bkt]
        if not idx: continue
        keys_b = []; probs_bb = []; pos_bb = []; g_bb = []
        for s, g in idx:
            keys_b.extend(keys_te[s:s+g])
            probs_bb.extend(probs_b[s:s+g])
            pos_bb.extend(pos_te[s:s+g])
            g_bb.append(g)
        m_bb = _flat_metrics_v2(np.array(probs_bb), g_bb, keys_b, pos_bb, odds_map)
        print(f"  {bkt:6s} races={m_bb['n_races']:>4d}  top1={m_bb['top1_hit_rate']*100:5.1f}%  "
              f"ROI={m_bb['roi_pct']:>+6.1f}%")


if __name__ == "__main__":
    main()

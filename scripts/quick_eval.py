"""Fast model-variant evaluation for the one-pick-per-race + flat-bet
strategy.

Walk-forward over date range is expensive (~30 min). For iterating on model
hyperparams / feature sets / objectives we use a single train/test split:

  - train on every race day with date < `--split`
  - predict on every race day with `--split` ≤ date ≤ `--until`
  - for each race in the test window, pick the top-prob horse
  - score: top-1 hit rate, flat-bet ROI, NDCG@3, win log-loss

Reports a compact JSON summary so multiple variants can be diffed.

Usage:
    python3 -m scripts.quick_eval --split 2026-03-01 --until 2026-05-24
    python3 -m scripts.quick_eval --split 2026-03-01 --until 2026-05-24 \
        --features-json /tmp/usable.json \
        --objective rank:ndcg --max-depth 8

The features-json file should contain a JSON list of feature_ids to keep
(everything else is dropped from the matrix).
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

from features.catalog import FEATURES                        # noqa: E402
from models import stage1_xgb, stage2_benter, calibration    # noqa: E402


def _coerce_int(raw) -> int | None:
    if raw is None: return None
    try: return int(raw)
    except (TypeError, ValueError): return None


def _load_split(conn: sqlite3.Connection, before: str | None, between: tuple[str, str] | None,
                feature_ids: list[str]) -> tuple[np.ndarray, np.ndarray, list[int], list[int], list[int]]:
    """Return X, y_label (4..0 ranking labels), group sizes, race_ids, positions.

    `before`: load races strictly < this date (training).
    `between`: load races where `lo <= date <= hi` (test).
    """
    if before is not None:
        races = conn.execute(
            "SELECT id FROM races WHERE date < ? ORDER BY date, id", (before,),
        ).fetchall()
    else:
        lo, hi = between
        races = conn.execute(
            "SELECT id FROM races WHERE date BETWEEN ? AND ? ORDER BY date, id",
            (lo, hi),
        ).fetchall()
    race_ids = [r[0] for r in races]
    if not race_ids:
        return np.empty((0, len(feature_ids))), np.empty(0), [], [], []

    # Pull horse entries per race
    entries: dict[int, list[dict]] = {}
    for rid in race_ids:
        rows = conn.execute(
            "SELECT brand, position FROM results WHERE race_id = ? ORDER BY id",
            (rid,),
        ).fetchall()
        entries[rid] = [{"brand": b, "position": _coerce_int(p)} for b, p in rows]

    # Pull features for all rows in one shot
    placeholders = ",".join("?" for _ in race_ids)
    fv_rows = conn.execute(
        f"SELECT race_id, brand, feature_id, value FROM feature_values "
        f"WHERE race_id IN ({placeholders})",
        race_ids,
    ).fetchall()
    fv_map: dict[tuple[int, str, str], float] = {}
    for rid, brand, fid, v in fv_rows:
        if v is None: continue
        fv_map[(rid, brand, fid)] = float(v)

    fid_index = {fid: i for i, fid in enumerate(feature_ids)}
    keys_order: list[tuple[int, str]] = []
    positions: list[int] = []
    groups: list[int] = []
    for rid in race_ids:
        es = entries.get(rid, [])
        if not es: continue
        groups.append(len(es))
        for e in es:
            keys_order.append((rid, e["brand"]))
            positions.append(e["position"] if e["position"] is not None else 99)

    X = np.full((len(keys_order), len(feature_ids)), np.nan, dtype=float)
    for i, (rid, brand) in enumerate(keys_order):
        for fid, j in fid_index.items():
            v = fv_map.get((rid, brand, fid))
            if v is not None:
                X[i, j] = v

    y = np.array([max(0.0, 5 - p) for p in positions])  # 1st=4, 5th+=0
    return X, y, groups, [k[0] for k in keys_order], positions


def _flat_metrics(probs: np.ndarray, groups: list[int],
                  race_ids: list[int], positions: list[int],
                  conn: sqlite3.Connection) -> dict:
    """Top-1 hit rate, flat-bet ROI ($500 per race), NDCG@3, win log-loss."""
    top1 = 0
    top3 = 0
    n_races = 0
    n_bets = 0
    total_stake = 0.0
    total_payout = 0.0
    log_loss_sum = 0.0
    log_loss_n = 0
    ndcg3_sum = 0.0

    # Odds lookup (post-race tote close) for ROI
    odds_map: dict[tuple[int, str], float] = {}
    keys = []
    placeholders = ",".join("?" for _ in set(race_ids))
    rid_list = list(set(race_ids))
    rows = conn.execute(
        f"SELECT race_id, brand, odds FROM results WHERE race_id IN ({placeholders})",
        rid_list,
    ).fetchall()
    for rid, brand, odds in rows:
        try:
            if odds is None: continue
            f = float(odds)
            if f > 0: odds_map[(rid, brand)] = f
        except (TypeError, ValueError):
            pass

    # Re-walk groups
    i = 0
    # need keys_order; re-derive by reading positions order alongside probs
    # but we don't have brands here, so caller must pass them.
    raise NotImplementedError("use _flat_metrics_v2 instead")


def _flat_metrics_v2(probs: np.ndarray, groups: list[int], keys_order: list[tuple[int, str]],
                     positions: list[int], odds_map: dict[tuple[int, str], float],
                     flat_stake: float = 500.0) -> dict:
    top1 = top3 = n_races = bets = wins = 0
    total_stake = 0.0
    total_payout = 0.0
    log_loss_sum = 0.0
    ndcg3_sum = 0.0
    bias_log_loss_sum = 0.0   # uniform 1/n baseline
    i = 0
    for g in groups:
        # slice
        race_probs = probs[i:i+g]
        race_pos = positions[i:i+g]
        race_keys = keys_order[i:i+g]
        i += g
        if g == 0: continue
        n_races += 1
        ranked = np.argsort(-race_probs)
        top_idx = ranked[0]
        top1 += 1 if race_pos[top_idx] == 1 else 0
        top3 += 1 if any(race_pos[ranked[k]] == 1 for k in range(min(3, g))) else 0

        # NDCG@3 (winner gets gain 1, others 0)
        ideal_dcg = 1.0  # 1/log2(1+1) = 1
        dcg = 0.0
        for k in range(min(3, g)):
            if race_pos[ranked[k]] == 1:
                dcg = 1.0 / math.log2(k + 2)
                break
        ndcg3_sum += (dcg / ideal_dcg) if ideal_dcg else 0

        # Win log-loss (model's prob for the winner)
        for j, p in enumerate(race_probs):
            won = 1 if race_pos[j] == 1 else 0
            if won:
                pclip = max(1e-9, min(1 - 1e-9, float(p)))
                log_loss_sum -= math.log(pclip)
                bias_log_loss_sum -= math.log(1.0 / g)

        # Flat bet on top
        bets += 1
        total_stake += flat_stake
        if race_pos[top_idx] == 1:
            wins += 1
            odds = odds_map.get(race_keys[top_idx])
            if odds is not None:
                total_payout += flat_stake * odds

    pnl = total_payout - total_stake
    return {
        "n_races": n_races,
        "n_bets": bets,
        "n_wins": wins,
        "top1_hit_rate": round(top1 / n_races, 4) if n_races else 0,
        "top3_hit_rate": round(top3 / n_races, 4) if n_races else 0,
        "ndcg3": round(ndcg3_sum / n_races, 4) if n_races else 0,
        "winner_log_loss": round(log_loss_sum / n_races, 4) if n_races else 0,
        "baseline_uniform_log_loss": round(bias_log_loss_sum / n_races, 4) if n_races else 0,
        "total_stake": round(total_stake, 2),
        "total_payout": round(total_payout, 2),
        "pnl": round(pnl, 2),
        "roi_pct": round(100 * pnl / total_stake, 2) if total_stake > 0 else 0,
        "strike_rate_pct": round(100 * wins / bets, 2) if bets else 0,
    }


def run(split: str, until: str, *, feature_filter: list[str] | None = None,
        objective: str = "rank:pairwise", eta: float = 0.05, max_depth: int = 6,
        num_round: int = 200, subsample: float = 0.8, colsample: float = 0.8,
        benter_alpha: float = 1.0, benter_beta: float = 0.9,
        use_market: bool = True, select_by: str = "prob") -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA query_only = 1")

    fids_all = [f.id for f in FEATURES]
    fids = [fid for fid in fids_all if (not feature_filter or fid in feature_filter)]

    t0 = time.time()
    X_tr, y_tr, g_tr, rids_tr, _ = _load_split(conn, before=split, between=None, feature_ids=fids)
    X_te, y_te, g_te, rids_te, pos_te = _load_split(conn, before=None, between=(split, until), feature_ids=fids)
    if len(X_tr) == 0 or len(X_te) == 0:
        return {"error": "empty train or test set"}

    # We also need keys_order for the test set to map probs → odds
    rows_te = conn.execute(
        f"""
        SELECT ra.id, r.brand
        FROM races ra
        JOIN results r ON r.race_id = ra.id
        WHERE ra.date BETWEEN ? AND ?
        ORDER BY ra.date, ra.id, r.id
        """,
        (split, until),
    ).fetchall()
    keys_te: list[tuple[int, str]] = [(rid, b) for rid, b in rows_te]

    # Odds map
    placeholders = ",".join("?" for _ in set(rids_te))
    odds_rows = conn.execute(
        f"SELECT race_id, brand, odds FROM results WHERE race_id IN ({placeholders})",
        list(set(rids_te)),
    ).fetchall()
    odds_map: dict[tuple[int, str], float] = {}
    for rid, brand, odds in odds_rows:
        try:
            if odds is None: continue
            f = float(odds)
            if f > 0: odds_map[(rid, brand)] = f
        except (TypeError, ValueError):
            pass

    # Train
    params = {
        "objective": objective, "eta": eta, "max_depth": max_depth,
        "subsample": subsample, "colsample_bytree": colsample,
        "tree_method": "hist", "verbosity": 0,
    }
    bst = stage1_xgb.train(X_tr, y_tr, g_tr, params=params, num_boost_round=num_round)
    scores_te = stage1_xgb.predict_scores(bst, X_te)
    f_probs = stage1_xgb.scores_to_probs(scores_te, g_te)

    # Stage-2 Benter blend (optional)
    if use_market:
        # Pull market implied prob from latest pre-race odds (use results.odds as final pool — same as walk_forward fallback)
        mkt = np.array([
            (1.0 / odds_map[(rid, b)]) if (rid, b) in odds_map else float("nan")
            for rid, b in keys_te
        ])
        # Normalise per race so π sums to 1
        i = 0
        for g in g_te:
            seg = mkt[i:i+g]
            valid = np.isfinite(seg)
            if valid.any():
                s = np.nansum(seg[valid])
                if s > 0:
                    seg = np.where(valid, seg / s, np.nan)
                    mkt[i:i+g] = seg
            i += g
        blended = stage2_benter.blend(f_probs, mkt, g_te, benter_alpha, benter_beta)
    else:
        blended = f_probs

    # Calibrate (isotonic on train via re-predicting train)
    try:
        scores_tr = stage1_xgb.predict_scores(bst, X_tr)
        f_tr = stage1_xgb.scores_to_probs(scores_tr, g_tr)
        y_win_tr = np.array([1 if p == 1 else 0 for p in
                             [int(pp) if 0 < pp < 99 else 0 for pp in
                              [max(0, 5 - p) for p in y_tr]]])
        # Skipping: too noisy for quick eval; use blended directly.
        cal_probs = blended
    except Exception:
        cal_probs = blended

    # Optional edge re-ranking: multiply prob by odds before per-race argmax.
    # Pre-race we'd use the latest polled odds; here we use the closing odds
    # in results as a proxy (matches how live select_bets would tier).
    rank_probs = cal_probs
    if select_by == "edge":
        rank_probs = np.array([
            cal_probs[i] * odds_map.get(keys_te[i], 1.0) if keys_te[i] in odds_map else cal_probs[i] * 1e-6
            for i in range(len(keys_te))
        ])
    metrics = _flat_metrics_v2(rank_probs, g_te, keys_te, pos_te, odds_map)
    metrics["elapsed_s"] = round(time.time() - t0, 1)
    metrics["train_size"] = int(len(X_tr))
    metrics["test_size"] = int(len(X_te))
    metrics["features"] = len(fids)
    metrics["objective"] = objective
    metrics["eta"] = eta
    metrics["max_depth"] = max_depth
    metrics["num_round"] = num_round
    return metrics


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--split", required=True, help="YYYY-MM-DD — first test date")
    p.add_argument("--until", required=True, help="YYYY-MM-DD — last test date")
    p.add_argument("--features-json", help="path to JSON list of feature_ids to keep")
    p.add_argument("--objective", default="rank:pairwise")
    p.add_argument("--eta", type=float, default=0.05)
    p.add_argument("--max-depth", type=int, default=6)
    p.add_argument("--num-round", type=int, default=200)
    p.add_argument("--subsample", type=float, default=0.8)
    p.add_argument("--colsample", type=float, default=0.8)
    p.add_argument("--alpha", type=float, default=1.0)
    p.add_argument("--beta", type=float, default=0.9)
    p.add_argument("--no-market", action="store_true")
    p.add_argument("--select-by", choices=["prob", "edge"], default="prob",
                   help="rank horses per race by prob (default) or by prob*odds")
    p.add_argument("--tag", default="run")
    ns = p.parse_args()

    feats = None
    if ns.features_json:
        feats = json.loads(Path(ns.features_json).read_text())

    out = run(
        ns.split, ns.until,
        feature_filter=feats,
        objective=ns.objective, eta=ns.eta, max_depth=ns.max_depth,
        num_round=ns.num_round, subsample=ns.subsample, colsample=ns.colsample,
        benter_alpha=ns.alpha, benter_beta=ns.beta,
        use_market=not ns.no_market, select_by=ns.select_by,
    )
    out["tag"] = ns.tag
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()

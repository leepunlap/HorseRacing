#!/usr/bin/env python3
"""Hyperparameter sweep for the Benter / XGBoost-LambdaMART pipeline.

Strategy: walk-forward over a sample of target dates, for each XGB
hyperparameter cell train the ranker, blend with market via stage-2 Benter
(α, β auto-fitted on a holdout), calibrate, and score the resulting predictions
on three metrics:
  * winner_log_loss  — −log(calibrated_prob[winner]) averaged across races.
                       Lower is better; this is what the model "should" be
                       optimising. Most reliable single-number proxy for
                       prediction quality.
  * top1_acc        — % of races where the highest-prob horse won.
  * ndcg3           — NDCG@3 (a learning-to-rank quality metric: how often
                       the winner appears near the top of the predicted order).

Sweep cells are evaluated on a date range that's strictly forward of training
(walk-forward, point-in-time). To keep wall time reasonable the script
defaults to a sampled subset of meeting dates (every Nth date in the window).

Usage:
  python3 scripts/sweep_hyperparams.py \
    --strategy benter_baseline \
    --from 2025-09-01 --to 2026-05-24 \
    --sample-every 4 \
    --grid quick      # one of: quick (12 cells, ~10min) | full (~80 cells, ~3hr)

Writes a CSV to data/sweep_results_<timestamp>.csv and prints the top 10
cells sorted by winner_log_loss.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from features.catalog import FEATURES                  # noqa: E402
from models import stage1_xgb, stage2_benter, calibration  # noqa: E402

DB_PATH = ROOT / "data" / "racing.db"


# ─── XGBoost hyperparameter grids ──────────────────────────────────────────
# "quick" is a small Cartesian product on the highest-leverage knobs.
# "full" is the production sweep — run overnight.
GRIDS = {
    "quick": {
        "eta":              [0.03, 0.05, 0.10],
        "max_depth":        [4, 6, 8],
        "num_boost_round":  [120],   # fixed
        "min_child_weight": [1, 10],
        "subsample":        [0.8],
        "colsample_bytree": [0.8],
    },
    "full": {
        "eta":              [0.02, 0.05, 0.08, 0.12],
        "max_depth":        [4, 5, 6, 7, 8],
        "num_boost_round":  [100, 200, 300, 500],
        "min_child_weight": [1, 5, 10, 30],
        "subsample":        [0.6, 0.8, 1.0],
        "colsample_bytree": [0.6, 0.8, 1.0],
    },
}


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA journal_mode = WAL")
    return c


def _feature_ids_for_strategy(conn: sqlite3.Connection, strategy_id: int) -> list[str]:
    row = conn.execute(
        "SELECT features_enabled_json FROM strategies WHERE id = ?",
        (strategy_id,),
    ).fetchone()
    enabled_overrides = json.loads(row[0]) if row and row[0] else {}
    return [f.id for f in FEATURES if enabled_overrides.get(f.id, f.enabled_default)]


def _load_matrix(conn, before_date: str, feature_ids: list[str]):
    """Load PIT feature matrix for races strictly before `before_date`."""
    races = conn.execute(
        "SELECT id FROM races WHERE date < ? ORDER BY date, course, race_no",
        (before_date,),
    ).fetchall()
    if not races:
        return np.empty((0, len(feature_ids))), np.empty(0), [], []
    race_ids = [r[0] for r in races]
    ph = ",".join("?" * len(race_ids))
    rows = conn.execute(
        f"""
        SELECT fv.race_id, fv.brand, fv.feature_id, fv.value, r.position
        FROM feature_values fv
        LEFT JOIN results r ON r.race_id = fv.race_id AND r.brand = fv.brand
        WHERE fv.race_id IN ({ph})
        """,
        race_ids,
    ).fetchall()
    cell, pos = {}, {}
    for race_id, brand, fid, val, position in rows:
        cell.setdefault((race_id, brand), {})[fid] = val
        pos[(race_id, brand)] = position
    fid_index = {fid: i for i, fid in enumerate(feature_ids)}
    keys = sorted(cell.keys(), key=lambda k: (k[0], k[1]))
    X = np.full((len(keys), len(feature_ids)), np.nan, dtype=float)
    y = np.zeros(len(keys), dtype=float)
    for ri, k in enumerate(keys):
        for fid, val in cell[k].items():
            if fid in fid_index and val is not None:
                try:
                    X[ri, fid_index[fid]] = float(val)
                except (TypeError, ValueError):
                    pass
        p = pos[k]
        try:
            pi = int(p) if p is not None else 0
        except (ValueError, TypeError):
            pi = 0
        y[ri] = max(0, 5 - pi) if pi > 0 else 0
    group: list[int] = []
    cur_rid, g = None, 0
    for race_id, _ in keys:
        if race_id != cur_rid:
            if cur_rid is not None:
                group.append(g)
            cur_rid = race_id; g = 0
        g += 1
    if cur_rid is not None:
        group.append(g)
    return np.nan_to_num(X, nan=0.0), y, group, keys


def _load_test(conn, date: str, feature_ids: list[str]):
    races = conn.execute(
        "SELECT id FROM races WHERE date = ? ORDER BY course, race_no", (date,),
    ).fetchall()
    if not races:
        return np.empty((0, len(feature_ids))), [], []
    race_ids = [r[0] for r in races]
    ph = ",".join("?" * len(race_ids))
    rows = conn.execute(
        f"SELECT race_id, brand, feature_id, value FROM feature_values WHERE race_id IN ({ph})",
        race_ids,
    ).fetchall()
    cell = {}
    for race_id, brand, fid, val in rows:
        cell.setdefault((race_id, brand), {})[fid] = val
    fid_index = {fid: i for i, fid in enumerate(feature_ids)}
    keys = sorted(cell.keys(), key=lambda k: (k[0], k[1]))
    X = np.full((len(keys), len(feature_ids)), np.nan, dtype=float)
    for ri, k in enumerate(keys):
        for fid, val in cell[k].items():
            if fid in fid_index and val is not None:
                try:
                    X[ri, fid_index[fid]] = float(val)
                except (TypeError, ValueError):
                    pass
    X = np.nan_to_num(X, nan=0.0)
    group, cur, g = [], None, 0
    for rid, _ in keys:
        if rid != cur:
            if cur is not None: group.append(g)
            cur, g = rid, 0
        g += 1
    if cur is not None: group.append(g)
    return X, group, keys


def _market_implied(conn, keys):
    pi = np.full(len(keys), np.nan, dtype=float)
    for i, (rid, brand) in enumerate(keys):
        r = conn.execute("SELECT odds FROM results WHERE race_id = ? AND brand = ?",
                         (rid, brand)).fetchone()
        if r and r[0]:
            try:
                o = float(r[0])
                if o > 0:
                    pi[i] = 1.0 / o
            except (TypeError, ValueError):
                pass
    return pi


def _race_winners(conn, keys, group):
    """Per race: index of winner (within the per-race slice) or -1."""
    out = []
    i = 0
    for g in group:
        winner = -1
        for j in range(g):
            rid, brand = keys[i + j]
            r = conn.execute("SELECT position FROM results WHERE race_id = ? AND brand = ?",
                             (rid, brand)).fetchone()
            try:
                if r and int(str(r[0]).strip()) == 1:
                    winner = j; break
            except (ValueError, TypeError, AttributeError):
                pass
        out.append(winner)
        i += g
    return out


def _evaluate_cell(conn, dates: list[str], feature_ids: list[str], xgb_params: dict) -> dict:
    """Train+score one hyperparameter cell across all sample dates."""
    all_log_loss: list[float] = []
    all_top1 = 0
    all_top3 = 0
    all_ndcg3 = 0.0
    n_races = 0
    n_trains = 0
    for d in dates:
        X_tr, y_tr, gr_tr, _ = _load_matrix(conn, d, feature_ids)
        if len(X_tr) == 0 or sum(gr_tr) != len(X_tr):
            continue
        try:
            bst = stage1_xgb.train(
                X_tr, y_tr, gr_tr,
                params={k: v for k, v in xgb_params.items() if k != "num_boost_round"},
                num_boost_round=xgb_params.get("num_boost_round", 120),
            )
        except Exception:
            continue
        X_te, gr_te, keys_te = _load_test(conn, d, feature_ids)
        if not len(X_te):
            continue
        scores = stage1_xgb.predict_scores(bst, X_te)
        f_probs = stage1_xgb.scores_to_probs(scores, gr_te)
        pi = _market_implied(conn, keys_te)
        # Auto-fit α, β on this date's predictions vs realised winners
        winners = _race_winners(conn, keys_te, gr_te)
        try:
            alpha, beta, _ = stage2_benter.fit_alpha_beta(f_probs, pi, gr_te, winners)
        except Exception:
            alpha, beta = 1.0, 0.9
        blended = stage2_benter.blend(f_probs, pi, gr_te, alpha, beta)
        # Per-race accuracy
        i = 0
        for g, wi in zip(gr_te, winners):
            if wi >= 0 and wi < g:
                seg = blended[i : i + g]
                # rank winners
                order = np.argsort(-seg)
                rank = int(np.where(order == wi)[0][0]) + 1
                if rank == 1: all_top1 += 1
                if rank <= 3: all_top3 += 1
                # NDCG@3 = (2^rel_at_rank - 1) / log2(rank+1), idealized = (2^1 - 1)/log2(2)=1
                if rank <= 3:
                    all_ndcg3 += 1.0 / math.log2(rank + 1)
                wp = max(min(float(seg[wi]), 1 - 1e-9), 1e-9)
                all_log_loss.append(-math.log(wp))
                n_races += 1
            i += g
        n_trains += 1
    if n_races == 0:
        return {"winner_log_loss": float("inf"), "top1": 0, "top3": 0, "ndcg3": 0, "n": 0, "trains": n_trains}
    return {
        "winner_log_loss": sum(all_log_loss) / n_races,
        "top1": all_top1 / n_races,
        "top3": all_top3 / n_races,
        "ndcg3": all_ndcg3 / n_races,
        "n": n_races,
        "trains": n_trains,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--strategy", default="benter_baseline")
    p.add_argument("--from", dest="d_from", required=True)
    p.add_argument("--to", dest="d_to", required=True)
    p.add_argument("--sample-every", type=int, default=4,
                   help="evaluate every Nth meeting date in window (default 4)")
    p.add_argument("--grid", choices=["quick", "full"], default="quick")
    p.add_argument("--out", help="output CSV path (default: data/sweep_results_<ts>.csv)")
    args = p.parse_args()

    conn = _conn()
    sid = conn.execute("SELECT id FROM strategies WHERE name = ?", (args.strategy,)).fetchone()
    if not sid:
        sys.exit(f"strategy {args.strategy} not found")
    sid = sid[0]
    feature_ids = _feature_ids_for_strategy(conn, sid)

    all_dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM races WHERE date BETWEEN ? AND ? ORDER BY date",
        (args.d_from, args.d_to),
    ).fetchall()]
    dates = all_dates[::args.sample_every]
    if not dates:
        sys.exit("no dates in window")

    grid = GRIDS[args.grid]
    keys = list(grid.keys())
    cells = list(itertools.product(*[grid[k] for k in keys]))
    print(f"sweep: {len(cells)} cells × {len(dates)} dates = up to {len(cells)*len(dates)} trains "
          f"(features={len(feature_ids)})")

    out_rows = []
    t0 = time.time()
    for i, vals in enumerate(cells, start=1):
        params = dict(zip(keys, vals))
        # baseline XGB defaults for things not in the grid
        base = {"objective": "rank:pairwise", "tree_method": "hist",
                "subsample": 0.8, "colsample_bytree": 0.8,
                "min_child_weight": 1.0, "gamma": 0.0, "verbosity": 0}
        xgb_params = {**base, **params}
        cell_t0 = time.time()
        res = _evaluate_cell(conn, dates, feature_ids, xgb_params)
        elapsed = time.time() - cell_t0
        row = {**params, **res, "elapsed_s": round(elapsed, 1)}
        out_rows.append(row)
        print(f"  [{i}/{len(cells)}] {params}  "
              f"log_loss={res['winner_log_loss']:.4f}  top1={res['top1']:.3f}  "
              f"top3={res['top3']:.3f}  ndcg3={res['ndcg3']:.3f}  ({elapsed:.0f}s)")

    out_path = Path(args.out) if args.out else (
        ROOT / "data" / f"sweep_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out_rows[0].keys()))
        w.writeheader(); w.writerows(out_rows)
    print(f"\nwrote {out_path} ({len(out_rows)} cells, {time.time()-t0:.0f}s total)")

    # Top 10 by winner_log_loss (lower = better)
    out_rows.sort(key=lambda r: r["winner_log_loss"])
    print("\n=== top 10 cells (sorted by winner_log_loss ↓) ===")
    cols = list(grid.keys()) + ["winner_log_loss", "top1", "top3", "ndcg3", "n"]
    print("  " + "  ".join(f"{c:>15s}" if isinstance(c, str) else str(c) for c in cols))
    for r in out_rows[:10]:
        vals = [r[c] for c in cols]
        fmt = []
        for v in vals:
            if isinstance(v, float):
                fmt.append(f"{v:>15.4f}")
            else:
                fmt.append(f"{v:>15}")
        print("  " + "  ".join(fmt))


if __name__ == "__main__":
    main()

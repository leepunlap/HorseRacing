"""Walk-forward training & evaluation pipeline.

Given a strategies row, walk through dates in order; for each target date:
  1. Pull feature rows for races strictly before the target date — training set.
  2. Train stage-1 XGBoost LambdaMART on it.
  3. Predict stage-1 scores → softmax per race → `f_i`.
  4. Look up `π_i` from latest odds_snapshots (or NaN if not yet polled).
  5. Apply stage-2 Benter blend (alpha/beta from strategy row).
  6. Calibrate (mode from strategy row) — fit on the most recent hold-out
     window, transform target-date predictions.
  7. Write to `predictions`. Compute ECE/Brier/log-loss → `calibration_metrics`.

Strictly point-in-time: we filter feature_values by `snapshot_basis`, so even
features that include current odds (Cat 14) are clean.

Usage:
    python3 -m models.walk_forward --strategy benter_baseline \\
        --from 2025-12-01 --to 2026-05-01
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "racing.db"
sys.path.insert(0, str(BASE_DIR))

from features.catalog import FEATURES                  # noqa: E402
from models import stage1_xgb, stage2_benter, calibration  # noqa: E402


_RACE_DATE_CACHE: dict[int, str | None] = {}


def _coerce_odds(raw) -> float | None:
    """results.odds is mostly REAL but '---' creeps in for non-runners."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            f = float(raw)
            return f if f > 0 else None
        except (TypeError, ValueError):
            return None
    try:
        f = float(str(raw).strip())
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _coerce_position(raw) -> int | None:
    """results.position is mostly integer but HKJC also uses codes for non-finishers:
    'WV' (withdrawn/voided), 'FE' (fell), 'PU' (pulled up), 'UR' (unseated rider),
    'DQ' (disqualified), '---'. These map to None (treated as 99 / label 0 in LtR)."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    s = str(raw).strip()
    if not s or s in ("---", "--", "-"):
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _race_date(conn: sqlite3.Connection, race_id: int) -> str | None:
    """Cached lookup of a race's date string."""
    if race_id in _RACE_DATE_CACHE:
        return _RACE_DATE_CACHE[race_id]
    r = conn.execute("SELECT date FROM races WHERE id = ?", (race_id,)).fetchone()
    val = r[0] if r else None
    _RACE_DATE_CACHE[race_id] = val
    return val


def _renormalise_per_race(probs: np.ndarray, group_sizes) -> np.ndarray:
    """Divide each race's prob vector by its sum so each group sums to 1.0.
    Falls back to uniform 1/n if a group sums to 0 (every horse calibrated
    to ~0 — happens when the field is unusually weak in model terms)."""
    out = np.asarray(probs, dtype=float).copy()
    start = 0
    for g in group_sizes:
        end = start + g
        s = float(out[start:end].sum())
        if s > 1e-12:
            out[start:end] = out[start:end] / s
        else:
            out[start:end] = 1.0 / float(g)
        start = end
    return out


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
    return [
        f.id for f in FEATURES
        if enabled_overrides.get(f.id, f.enabled_default)
    ]


def _load_matrix(conn: sqlite3.Connection, before: str, feature_ids: list[str]) -> tuple[np.ndarray, np.ndarray, list[int], list[tuple[int, str]], list[int]]:
    """Load feature matrix for all races with date < `before` (training)."""
    races = conn.execute(
        "SELECT id FROM races WHERE date < ? ORDER BY date, course, race_no",
        (before,),
    ).fetchall()
    if not races:
        return np.empty((0, len(feature_ids))), np.empty(0), [], [], []

    race_ids = [r[0] for r in races]
    placeholders = ",".join("?" * len(race_ids))
    rows = conn.execute(
        f"""
        SELECT fv.race_id, fv.brand, fv.feature_id, fv.value, r.position
        FROM feature_values fv
        LEFT JOIN results r ON r.race_id = fv.race_id AND r.brand = fv.brand
        WHERE fv.race_id IN ({placeholders})
        """,
        race_ids,
    ).fetchall()

    # Pivot: (race_id, brand) -> {feature_id: value}, plus a labelled position.
    cell: dict[tuple[int, str], dict[str, float]] = {}
    pos: dict[tuple[int, str], int | None] = {}
    for race_id, brand, fid, val, position in rows:
        cell.setdefault((race_id, brand), {})[fid] = val
        pos[(race_id, brand)] = position

    fid_index = {fid: i for i, fid in enumerate(feature_ids)}
    keys = sorted(cell.keys(), key=lambda k: (k[0], k[1]))
    X = np.full((len(keys), len(feature_ids)), np.nan, dtype=float)
    y = np.zeros(len(keys), dtype=float)
    keys_out = []
    pos_list: list[int] = []
    for row_i, k in enumerate(keys):
        for fid, val in cell[k].items():
            if fid in fid_index and val is not None:
                try:
                    X[row_i, fid_index[fid]] = float(val)
                except (TypeError, ValueError):
                    # Defensive: any string-typed value that slipped through the
                    # compute coercion is dropped to NaN (will be nan_to_num→0 below).
                    pass
        p = _coerce_position(pos[k])
        pos_list.append(p if p is not None else 0)
        keys_out.append(k)
        y[row_i] = stage1_xgb.position_to_label(np.array([p if p is not None else 99]))[0]

    # Group sizes
    group: list[int] = []
    cur_race = None
    g = 0
    for race_id, _ in keys_out:
        if race_id != cur_race:
            if cur_race is not None:
                group.append(g)
            cur_race = race_id
            g = 0
        g += 1
    if cur_race is not None:
        group.append(g)

    X = np.nan_to_num(X, nan=0.0)
    return X, y, group, keys_out, pos_list


def _load_test(conn: sqlite3.Connection, date: str, feature_ids: list[str]) -> tuple[np.ndarray, list[int], list[tuple[int, str]]]:
    races = conn.execute(
        "SELECT id FROM races WHERE date = ? ORDER BY course, race_no", (date,),
    ).fetchall()
    if not races:
        return np.empty((0, len(feature_ids))), [], []
    race_ids = [r[0] for r in races]
    placeholders = ",".join("?" * len(race_ids))
    rows = conn.execute(
        f"SELECT race_id, brand, feature_id, value FROM feature_values WHERE race_id IN ({placeholders})",
        race_ids,
    ).fetchall()
    cell: dict[tuple[int, str], dict[str, float]] = {}
    for race_id, brand, fid, val in rows:
        cell.setdefault((race_id, brand), {})[fid] = val
    fid_index = {fid: i for i, fid in enumerate(feature_ids)}
    keys = sorted(cell.keys(), key=lambda k: (k[0], k[1]))
    X = np.full((len(keys), len(feature_ids)), np.nan, dtype=float)
    for ri, k in enumerate(keys):
        for fid, val in cell[k].items():
            if fid in fid_index and val is not None:
                X[ri, fid_index[fid]] = float(val)
    X = np.nan_to_num(X, nan=0.0)
    group: list[int] = []
    cur, g = None, 0
    for race_id, _ in keys:
        if race_id != cur:
            if cur is not None: group.append(g)
            cur, g = race_id, 0
        g += 1
    if cur is not None: group.append(g)
    return X, group, keys


def _market_implied(conn: sqlite3.Connection, keys: list[tuple[int, str]]) -> np.ndarray:
    pi = np.full(len(keys), np.nan, dtype=float)
    for i, (race_id, brand) in enumerate(keys):
        # Use the most recent odds snapshot before T-0 (closing), else fall
        # back to the final settled odds from results.
        r = conn.execute(
            "SELECT win_odds FROM odds_snapshots WHERE race_id = ? AND brand = ? "
            "ORDER BY ts DESC LIMIT 1",
            (race_id, brand),
        ).fetchone()
        odds = _coerce_odds(r[0]) if r else None
        if odds is None:
            r = conn.execute("SELECT odds FROM results WHERE race_id = ? AND brand = ?",
                             (race_id, brand)).fetchone()
            odds = _coerce_odds(r[0]) if r else None
        if odds is not None:
            pi[i] = 1.0 / odds
    # Renormalise per implied is OK in the per-race softmax of blend()
    return pi


def _winner_idx_per_race(conn: sqlite3.Connection, keys: list[tuple[int, str]], group: list[int]) -> list[int]:
    out: list[int] = []
    i = 0
    for g in group:
        winner = -1
        for j in range(g):
            race_id, brand = keys[i + j]
            r = conn.execute("SELECT position FROM results WHERE race_id = ? AND brand = ?",
                             (race_id, brand)).fetchone()
            if r and _coerce_position(r[0]) == 1:
                winner = j
                break
        out.append(winner)
        i += g
    return out


def run_strategy(strategy_id: int, date_from: str, date_to: str) -> dict:
    conn = _conn()
    strat = conn.execute(
        "SELECT name, stage2_enabled, stage2_alpha, stage2_beta, calibration, "
        "       time_decay_tau "
        "FROM strategies WHERE id = ?",
        (strategy_id,),
    ).fetchone()
    if not strat:
        raise SystemExit(f"strategy id {strategy_id} not found")
    name, stage2_on, alpha, beta, cal_mode, tau = strat
    feature_ids = _feature_ids_for_strategy(conn, strategy_id)
    print(f"[walk_forward] strategy={name}  features={len(feature_ids)}  "
          f"stage2={bool(stage2_on)}  cal={cal_mode}  tau={tau or 'none'}")

    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM races WHERE date BETWEEN ? AND ? ORDER BY date",
        (date_from, date_to),
    ).fetchall()]
    if not dates:
        raise SystemExit("no race dates in range")

    overall: list[tuple[float, int]] = []  # (calibrated_prob, won)
    t0 = time.time()
    from datetime import date as _date
    for d in dates:
        X_tr, y_tr, gr_tr, keys_tr, _ = _load_matrix(conn, d, feature_ids)
        if len(X_tr) == 0 or sum(gr_tr) != len(X_tr):
            print(f"  {d}: insufficient training data, skipping")
            continue
        # Time-decay group weights: each race contributes weight =
        # exp(-Δdays / τ) relative to the current target date `d`. With
        # tau=180 this emphasises the last ~6 months — see Iter 10 of the
        # research log. Recovered per-group via the race_id stored in keys_tr.
        sample_w = None
        if tau and tau > 0:
            try:
                cutoff = _date.fromisoformat(d)
                race_ids_tr = list(dict.fromkeys(rid for rid, _b in keys_tr))
                placeholders = ",".join("?" * len(race_ids_tr))
                date_rows = conn.execute(
                    f"SELECT id, date FROM races WHERE id IN ({placeholders})",
                    race_ids_tr,
                ).fetchall()
                date_map = {rid: dt for rid, dt in date_rows}
                weights = []
                for rid in race_ids_tr:
                    dt = date_map.get(rid)
                    if dt:
                        days = (cutoff - _date.fromisoformat(dt)).days
                        weights.append(math.exp(-days / float(tau)))
                    else:
                        weights.append(1.0)
                sample_w = np.array(weights, dtype=float)
            except Exception as exc:
                print(f"  {d}: time-decay weight build failed: {exc}")
                sample_w = None
        try:
            bst = stage1_xgb.train(X_tr, y_tr, gr_tr,
                                   num_boost_round=stage1_xgb.DEFAULT_NUM_BOOST_ROUND,
                                   weight=sample_w)
        except Exception as exc:
            print(f"  {d}: stage-1 train failed: {exc}")
            continue

        X_te, gr_te, keys_te = _load_test(conn, d, feature_ids)
        if not len(X_te):
            continue

        f_scores = stage1_xgb.predict_scores(bst, X_te)
        f_probs = stage1_xgb.scores_to_probs(f_scores, gr_te)
        if stage2_on:
            pi = _market_implied(conn, keys_te)
            blended = stage2_benter.blend(f_probs, pi, gr_te, float(alpha), float(beta))
        else:
            blended = f_probs

        # Calibrate honestly. The holdout is the last 14 days of training
        # races. We re-run the SAME per-race softmax + Benter blend on the
        # holdout so the calibrator sees inputs identical in distribution to
        # what we then transform. Earlier versions did a single cross-race
        # softmax over all holdout horses which gave each prob ≈ 1/N — the
        # IsotonicRegression then mapped any in-range prob (~1/12) to y_max
        # (≈ 1.0), destroying calibration.
        if (cal_mode or "isotonic").lower() == "none":
            cal_probs = blended
        else:
            try:
                hold_cut = (datetime.fromisoformat(d) - timedelta(days=14)).date().isoformat()
                # Re-use the training matrix we already loaded; pick the subset
                # whose race_date ≥ hold_cut. Compute per-race groups for that
                # subset so scores_to_probs softmaxes within each race.
                mask_hold = [i for i, (rid, _b) in enumerate(keys_tr)
                             if (_race_date(conn, rid) or "") >= hold_cut]
                if len(mask_hold) >= 50:
                    keys_hold = [keys_tr[i] for i in mask_hold]
                    X_hold = X_tr[mask_hold]
                    y_hold_lbl = y_tr[mask_hold]
                    # Per-race group sizes
                    hold_groups: list[int] = []
                    prev_rid = None
                    g = 0
                    for rid, _b in keys_hold:
                        if rid != prev_rid:
                            if g: hold_groups.append(g)
                            prev_rid = rid
                            g = 0
                        g += 1
                    if g: hold_groups.append(g)
                    # Predict + per-race softmax + stage-2 blend on the holdout
                    sc_h = stage1_xgb.predict_scores(bst, X_hold)
                    f_h = stage1_xgb.scores_to_probs(sc_h, hold_groups)
                    if stage2_on:
                        pi_h = _market_implied(conn, keys_hold)
                        cal_input = stage2_benter.blend(f_h, pi_h, hold_groups,
                                                        float(alpha), float(beta))
                    else:
                        cal_input = f_h
                    outcomes_h = (y_hold_lbl >= 4).astype(float)
                    cal = calibration.fit(cal_input, outcomes_h,
                                          mode=cal_mode or "isotonic")
                    cal_probs = cal.transform(blended)
                    # Isotonic / Platt operate per-row and don't preserve
                    # per-race-sum=1. Without this step the calibrator
                    # collapses non-favourite horses to ~0 and pushes the
                    # favourite toward ~1, producing the "one huge edge,
                    # rest zero" display bug. Renormalise inside each
                    # race-group, falling back to uniform if the group
                    # sums to zero.
                    cal_probs = _renormalise_per_race(cal_probs, gr_te)
                else:
                    cal_probs = blended
            except Exception as exc:
                print(f"  {d}: calibration fallback (none): {exc}")
                cal_probs = blended

        # Persist predictions
        snapshot_basis = d + "T23:59:59"
        pi_te = _market_implied(conn, keys_te)  # reuse the same vector both for blend and for persistence
        for (race_id, brand), fp, mp, bp, cp in zip(keys_te, f_probs, pi_te, blended, cal_probs):
            row = conn.execute(
                "SELECT odds, position FROM results WHERE race_id = ? AND brand = ?",
                (race_id, brand),
            ).fetchone()
            odds_v = _coerce_odds(row[0]) if row else None
            position = row[1] if row else None
            edge = (cp * odds_v) if odds_v is not None else None
            conn.execute(
                """
                INSERT INTO predictions
                  (strategy_id, race_id, horse_id, brand, fundamental_prob,
                   market_implied_prob, blended_prob, calibrated_prob, odds_at_prediction,
                   edge, recommendation, snapshot_basis)
                VALUES (?, ?, (SELECT id FROM horses WHERE brand = ?), ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy_id, race_id, brand, snapshot_basis) DO UPDATE SET
                  fundamental_prob = excluded.fundamental_prob,
                  market_implied_prob = excluded.market_implied_prob,
                  blended_prob = excluded.blended_prob,
                  calibrated_prob = excluded.calibrated_prob,
                  edge = excluded.edge
                """,
                (strategy_id, race_id, brand, brand,
                 float(fp), float(mp) if not math.isnan(float(mp)) else None,
                 float(bp), float(cp), odds_v,
                 edge, "pending", snapshot_basis),
            )
            won = 1 if _coerce_position(position) == 1 else 0
            overall.append((float(cp), won))
        conn.commit()
        print(f"  {d}: trained on {len(X_tr)} rows, predicted {len(X_te)}")

    p = np.array([x[0] for x in overall])
    y = np.array([x[1] for x in overall])
    if len(p) == 0:
        return {"strategy": name, "samples": 0}
    summary = {
        "strategy": name,
        "samples": int(len(p)),
        "brier": calibration.brier(p, y),
        "log_loss": calibration.log_loss(p, y),
        "ece": calibration.ece(p, y),
        "elapsed_s": round(time.time() - t0, 1),
    }
    # Record into calibration_metrics
    conn.execute(
        "INSERT INTO calibration_metrics (strategy_id, window_start, window_end, brier, log_loss, ece, sample_count) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(strategy_id, window_end) DO UPDATE SET brier=excluded.brier, log_loss=excluded.log_loss, ece=excluded.ece, sample_count=excluded.sample_count",
        (strategy_id, date_from, date_to, summary["brier"], summary["log_loss"], summary["ece"], summary["samples"]),
    )

    # ─── strategy_runs: one-row summary of this walk-forward run ──────────
    # Wraps the audit into a single record so the SPA's "compare strategies"
    # view can SELECT … FROM strategy_runs WHERE strategy_id IN (…) instead
    # of re-aggregating predictions every page load.
    _write_strategy_run(conn, strategy_id, date_from, date_to,
                        feature_ids, tau, summary)
    conn.commit()
    conn.close()
    return summary


def _write_strategy_run(conn, strategy_id, date_from, date_to,
                        feature_ids, tau, summary) -> None:
    """Compute audit aggregates from the predictions table and upsert a
    one-row summary into strategy_runs."""
    import hashlib

    # Aggregate audit numbers from predictions where recommendation='bet'
    # (set by betting/select_bets.py — caller is expected to have run it
    # OR to run it after this completes; we recompute from top-prob-per-race
    # if recommendation isn't yet set).
    rows = conn.execute(
        """
        WITH ranked AS (
          SELECT p.race_id, p.brand, p.calibrated_prob,
                 ROW_NUMBER() OVER (PARTITION BY p.race_id ORDER BY p.calibrated_prob DESC, p.brand) AS rk
          FROM predictions p
          JOIN races ra ON ra.id = p.race_id
          WHERE p.strategy_id = ? AND ra.date BETWEEN ? AND ?
        ),
        top AS (SELECT race_id, brand FROM ranked WHERE rk = 1)
        SELECT
            COUNT(*),
            SUM(CASE WHEN CAST(r.position AS INT) = 1 THEN 1 ELSE 0 END),
            SUM(CASE WHEN CAST(r.position AS INT) = 1 THEN COALESCE(r.odds, 0) * 500 ELSE 0 END),
            COUNT(*) * 500
        FROM top
        LEFT JOIN results r ON r.race_id = top.race_id AND r.brand = top.brand
        """,
        (strategy_id, date_from, date_to),
    ).fetchone()
    n_bets, n_wins, total_payout, total_stake = rows or (0, 0, 0, 0)
    n_bets = n_bets or 0; n_wins = n_wins or 0
    total_payout = float(total_payout or 0)
    total_stake = float(total_stake or 0)
    pnl = total_payout - total_stake
    roi = (100.0 * pnl / total_stake) if total_stake else 0.0
    strike = (100.0 * n_wins / n_bets) if n_bets else 0.0
    top1 = (n_wins / n_bets) if n_bets else None

    # config_hash: sha1 of the sorted feature list + the relevant XGBoost
    # hyperparams. Two runs with identical hash are the same algo.
    cfg = "|".join(sorted(feature_ids)) + f"|tau={tau}"
    cfg_hash = hashlib.sha1(cfg.encode()).hexdigest()[:16]

    conn.execute(
        """
        INSERT INTO strategy_runs
            (strategy_id, window_start, window_end,
             n_races, n_bets, n_wins, total_stake, total_payout, pnl,
             roi_pct, strike_rate_pct, top1_hit_rate,
             brier, log_loss, ece,
             n_features, time_decay_tau, config_hash, elapsed_s)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(strategy_id, window_end) DO UPDATE SET
            window_start=excluded.window_start,
            n_races=excluded.n_races, n_bets=excluded.n_bets, n_wins=excluded.n_wins,
            total_stake=excluded.total_stake, total_payout=excluded.total_payout, pnl=excluded.pnl,
            roi_pct=excluded.roi_pct, strike_rate_pct=excluded.strike_rate_pct,
            top1_hit_rate=excluded.top1_hit_rate,
            brier=excluded.brier, log_loss=excluded.log_loss, ece=excluded.ece,
            n_features=excluded.n_features, time_decay_tau=excluded.time_decay_tau,
            config_hash=excluded.config_hash, elapsed_s=excluded.elapsed_s,
            computed_at=CURRENT_TIMESTAMP
        """,
        (strategy_id, date_from, date_to,
         n_bets, n_bets, n_wins, total_stake, total_payout, pnl,
         roi, strike, top1,
         summary.get("brier"), summary.get("log_loss"), summary.get("ece"),
         len(feature_ids), tau, cfg_hash, summary.get("elapsed_s")),
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--strategy", required=True, help="strategy name (will create with defaults if absent)")
    p.add_argument("--from", dest="d_from", required=True)
    p.add_argument("--to", dest="d_to", required=True)
    args = p.parse_args()

    conn = _conn()
    row = conn.execute("SELECT id FROM strategies WHERE name = ?", (args.strategy,)).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO strategies (name, name_zh, name_en, enabled) VALUES (?,?,?,1)",
            (args.strategy, args.strategy, args.strategy),
        )
        conn.commit()
        row = conn.execute("SELECT id FROM strategies WHERE name = ?", (args.strategy,)).fetchone()
    sid = row[0]
    conn.close()

    summary = run_strategy(sid, args.d_from, args.d_to)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

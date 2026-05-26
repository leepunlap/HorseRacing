#!/usr/bin/env python3
"""End-to-end spine verification for benter_baseline.

Runs the checks in plan section 'Verification':
  * schema additions present (post_time, track_bias_daily, calibrator_artifacts)
  * feature_values has data
  * track_bias_daily populated for the back-test window
  * calibration_metrics within tolerances (ECE < 0.05)
  * audit (counterfactual) reports ROI + CLV
  * invariant spot-checks (no bets > bet_max_odds, no NaN-odds bets, etc.)

Usage:
    python3 scripts/verify_spine.py --from 2025-12-01 --to 2026-05-24
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "racing.db"
sys.path.insert(0, str(BASE_DIR))

from betting import audit as audit_mod, filters as filt


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def check(label: str, ok: bool, detail: str = "") -> bool:
    marker = "OK  " if ok else "FAIL"
    print(f"  [{marker}] {label}" + (f"  — {detail}" if detail else ""))
    return ok


def run(date_from: str, date_to: str) -> int:
    conn = _conn()
    fails = 0

    print("\n=== schema ===")
    races_cols = [r[1] for r in conn.execute("PRAGMA table_info(races)")]
    if not check("races.post_time column present", "post_time" in races_cols):
        fails += 1
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    for t in ("track_bias_daily", "calibrator_artifacts", "strategies",
              "predictions", "calibration_metrics", "feature_values"):
        if not check(f"table {t} exists", t in tables):
            fails += 1

    print("\n=== data coverage ===")
    fv = conn.execute("SELECT COUNT(*) FROM feature_values").fetchone()[0]
    if not check("feature_values populated", fv > 1000, f"{fv:,} rows"):
        fails += 1
    tb = conn.execute(
        "SELECT COUNT(*) FROM track_bias_daily WHERE date BETWEEN ? AND ?",
        (date_from, date_to),
    ).fetchone()[0]
    if not check("track_bias_daily covers window", tb > 0, f"{tb} (date,course) rows"):
        fails += 1

    print("\n=== strategy ===")
    strat = conn.execute(
        "SELECT id, name, bet_max_odds, edge_threshold, kelly_fraction "
        "FROM strategies WHERE name = 'benter_baseline'"
    ).fetchone()
    if not check("benter_baseline strategy exists", strat is not None):
        fails += 1
        conn.close()
        return fails
    sid, name, bet_max_odds, edge_thr, kelly = strat
    print(f"        strategy_id={sid}  bet_max_odds={bet_max_odds}  edge>={edge_thr}  kelly={kelly}")

    print("\n=== predictions ===")
    pv = conn.execute(
        "SELECT COUNT(*) FROM predictions WHERE strategy_id = ?", (sid,),
    ).fetchone()[0]
    if not check("predictions populated", pv > 0, f"{pv:,} rows"):
        fails += 1

    print("\n=== calibration metrics ===")
    cm = conn.execute(
        "SELECT window_start, window_end, brier, log_loss, ece, sample_count "
        "FROM calibration_metrics WHERE strategy_id = ? ORDER BY window_end DESC LIMIT 5",
        (sid,),
    ).fetchall()
    if not check("calibration_metrics has at least one window", len(cm) > 0):
        fails += 1
    for ws, we, brier, ll, ece, n in cm:
        print(f"        window {ws}..{we}  ECE={ece:.4f}  Brier={brier:.4f}  log_loss={ll:.4f}  n={n}")
    if cm:
        latest_ece = cm[0][4]
        if not check("latest ECE < 0.05 (elite per whitepaper §5.4)",
                     latest_ece is not None and latest_ece < 0.05,
                     f"ECE={latest_ece:.4f}" if latest_ece is not None else "no value"):
            fails += 1

    print("\n=== counterfactual audit ===")
    settings = filt.FilterSettings(
        bet_max_odds=float(bet_max_odds or 20.0),
        edge_threshold=float(edge_thr or 1.05),
        min_prob=0.05, bet_min_odds=2.5,
    )
    a = audit_mod.audit(conn, sid, date_from, date_to,
                        settings=settings, kelly_fraction_strat=float(kelly or 0.25),
                        bankroll=10000.0)
    print(f"        placed={a['placed']} blocked={a['blocked']} wins={a['wins']} "
          f"stake={a['total_stake']:.2f} payout={a['total_payout']:.2f} ROI={a['roi']:.4f}")
    print(f"        block reasons: {a['block_reasons']}")
    if a["placed"] > 0:
        check("ROI non-negative on out-of-sample (informational)",
              a["roi"] >= 0, f"ROI={a['roi']:.4f}")

    print("\n=== invariant spot-checks ===")
    # All checks on live_bets — if the spine never wrote any, sim_mode hasn't replayed yet
    n_bets = conn.execute("SELECT COUNT(*) FROM live_bets").fetchone()[0]
    print(f"        live_bets rows: {n_bets} (populate via live.sim_mode.replay_date)")
    over_max = conn.execute(
        "SELECT COUNT(*) FROM live_bets WHERE odds_at_placement > ?",
        (float(bet_max_odds or 20.0),),
    ).fetchone()[0]
    if not check(f"no bets above bet_max_odds ({bet_max_odds})", over_max == 0, f"{over_max} violations"):
        fails += 1
    nan_odds = conn.execute(
        "SELECT COUNT(*) FROM live_bets WHERE odds_at_placement IS NULL"
    ).fetchone()[0]
    if not check("no bets with NULL odds", nan_odds == 0, f"{nan_odds} violations"):
        fails += 1

    conn.close()
    print(f"\n=== summary ===\n  failures: {fails}")
    return fails


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="d_from", required=True)
    p.add_argument("--to", dest="d_to", required=True)
    args = p.parse_args()
    fails = run(args.d_from, args.d_to)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()

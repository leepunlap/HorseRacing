"""ECC-style data integrity checker.

Cross-validates facts across multiple HKJC sources + within the local DB.
Each check answers a yes/no question, records violations to
`integrity_check_violations`, and optionally auto-heals at severity ≤ medium
by re-fetching the relevant scraper with --force-refresh.

Run:
    python3 -m monitoring.integrity_check --baseline           # full DB
    python3 -m monitoring.integrity_check --date 2026-05-27    # one meeting day
    python3 -m monitoring.integrity_check --heal               # try auto-fix
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "racing.db"

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


SCHEMA = """
CREATE TABLE IF NOT EXISTS integrity_check_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT DEFAULT CURRENT_TIMESTAMP,
    scope TEXT NOT NULL,
    total_checks INTEGER NOT NULL,
    passed INTEGER NOT NULL,
    failed INTEGER NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS integrity_check_violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES integrity_check_runs(id) ON DELETE CASCADE,
    check_name TEXT NOT NULL,
    severity TEXT NOT NULL,
    race_id INTEGER,
    brand TEXT,
    source_a TEXT,
    source_b TEXT,
    value_a TEXT,
    value_b TEXT,
    detail TEXT,
    auto_healed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_icv_run ON integrity_check_violations(run_id);
CREATE INDEX IF NOT EXISTS idx_icv_check ON integrity_check_violations(check_name);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA foreign_keys = ON")
    c.executescript(SCHEMA)
    return c


# ─── Check definitions ───────────────────────────────────────────────────────
# Each check is a function returning a list of dicts:
#   { check_name, severity, race_id?, brand?, source_a, source_b,
#     value_a, value_b, detail }


def check_fk_orphans_predictions(conn, scope):
    rows = conn.execute(
        "SELECT id, race_id FROM predictions p "
        "WHERE NOT EXISTS (SELECT 1 FROM races r WHERE r.id = p.race_id)"
    ).fetchall()
    return [{
        "check_name": "fk_predictions_race",
        "severity": "high",
        "race_id": rid,
        "source_a": "predictions",
        "source_b": "races",
        "value_a": str(pid),
        "value_b": "missing",
        "detail": f"predictions row {pid} references missing race {rid}",
    } for pid, rid in rows]


def check_fk_orphans_feature_values(conn, scope):
    rows = conn.execute(
        "SELECT race_id, COUNT(*) FROM feature_values fv "
        "WHERE NOT EXISTS (SELECT 1 FROM races r WHERE r.id = fv.race_id) "
        "GROUP BY race_id"
    ).fetchall()
    return [{
        "check_name": "fk_feature_values_race", "severity": "high",
        "race_id": rid, "source_a": "feature_values", "source_b": "races",
        "value_a": str(n), "value_b": "missing",
        "detail": f"{n} feature_values rows reference missing race {rid}",
    } for rid, n in rows]


def check_brand_in_horses(conn, scope):
    rows = conn.execute(
        "SELECT DISTINCT r.brand FROM results r "
        "WHERE NOT EXISTS (SELECT 1 FROM horses h WHERE h.brand = r.brand)"
    ).fetchall()
    return [{
        "check_name": "brand_in_horses", "severity": "high",
        "brand": b, "source_a": "results", "source_b": "horses",
        "value_a": b, "value_b": "missing",
        "detail": f"brand {b} appears in results but has no horses row",
    } for (b,) in rows]


def check_predictions_without_results(conn, scope):
    """A race with predictions should also have results rows
    (race_card writes those pre-race)."""
    rows = conn.execute(
        "SELECT DISTINCT p.race_id FROM predictions p "
        "WHERE NOT EXISTS (SELECT 1 FROM results r WHERE r.race_id = p.race_id)"
    ).fetchall()
    return [{
        "check_name": "predictions_without_results", "severity": "high",
        "race_id": rid, "source_a": "predictions", "source_b": "results",
        "value_a": "exists", "value_b": "empty",
        "detail": f"race {rid} has predictions but zero results rows",
    } for (rid,) in rows]


def check_win_dividend_matches_winner(conn, scope):
    """For every (date, course, race_no) where pool='WIN' exists in dividends,
    the combination should equal the brand of the position=1 result row."""
    where = ""
    if scope.get("date"):
        where = f"AND d.date = '{scope['date']}'"
    rows = conn.execute(f"""
        SELECT d.date, d.course, d.race_no, d.combination AS div_brand,
               (SELECT r.brand FROM results r
                JOIN races ra ON ra.id = r.race_id
                WHERE ra.date = d.date AND ra.course = d.course
                  AND ra.race_no = d.race_no
                  AND r.position = '1' LIMIT 1) AS result_brand
        FROM dividends d
        WHERE d.pool = 'WIN' {where}
    """).fetchall()
    out = []
    for date, course, race_no, div_b, res_b in rows:
        if res_b is None:
            out.append({
                "check_name": "win_dividend_no_winner_row",
                "severity": "medium",
                "source_a": "dividends.WIN", "source_b": "results.position=1",
                "value_a": div_b, "value_b": "missing",
                "detail": f"{date}/{course}/R{race_no}: WIN dividend on {div_b} but no result row at position 1",
            })
        elif div_b != res_b:
            out.append({
                "check_name": "win_dividend_winner_mismatch",
                "severity": "high",
                "source_a": "dividends.WIN", "source_b": "results.position=1",
                "value_a": div_b, "value_b": res_b,
                "detail": f"{date}/{course}/R{race_no}: WIN dividend={div_b} vs result winner={res_b}",
            })
    return out


def check_place_dividend_count(conn, scope):
    """HKJC PLACE pool rule:
      * Field of 4 or 5 runners → 1 PLACE dividend (winner only)
      * Field of 6 or 7 runners → 2 PLACE dividends (top 2)
      * Field of 8 or more     → 3 PLACE dividends (top 3)
    A race with fewer rows than the rule mandates is a real scraper gap.
    The earlier cutoff (≥ 7 → 3) over-counted ~1.2K legitimate small-field
    meetings as violations."""
    where = "AND r.position IS NOT NULL AND r.position != ''"
    if scope.get("date"):
        where += f" AND ra.date = '{scope['date']}'"
    rows = conn.execute(f"""
        SELECT ra.id, ra.date, ra.course, ra.race_no,
               COUNT(DISTINCT r.brand) AS field_size,
               (SELECT COUNT(*) FROM dividends d
                WHERE d.date=ra.date AND d.course=ra.course
                  AND d.race_no=ra.race_no AND d.pool='PLACE') AS place_rows
        FROM races ra JOIN results r ON r.race_id = ra.id
        WHERE 1=1 {where}
        GROUP BY ra.id
    """).fetchall()
    out = []
    for rid, date, course, race_no, field, places in rows:
        if field <= 3:
            continue
        if field <= 5:
            expected = 1
        elif field <= 7:
            expected = 2
        else:
            expected = 3
        if places < expected and places > 0:
            out.append({
                "check_name": "place_dividend_incomplete",
                "severity": "medium",
                "race_id": rid,
                "source_a": "results.field_size", "source_b": "dividends.PLACE rows",
                "value_a": str(field), "value_b": str(places),
                "detail": f"{date}/{course}/R{race_no}: {field} runners → expect {expected} PLACE rows, got {places}",
            })
    return out


def check_sectional_finish_time(conn, scope):
    """Σ(per_horse_sectionals.split_time) ≈ results.finish_time for the winner.

    HKJC's last two sectionals are reported both as a 400m parent split AND
    as two 200m sub-splits (e.g. "23.83 12.01 12.38"). Our scraper stores
    BOTH the parent AND the sub-splits as separate furlong rows, so Σ ≈
    finish_time + final_400m. Subtract that out by counting the half-furlong
    sub-rows (heuristic: any split_time < 13s is a half-furlong on a Turf
    course). Tolerance widened to 0.3s to accommodate HKJC's published
    rounding."""
    where = "AND r.position = '1'"
    if scope.get("date"):
        where += f" AND r.date = '{scope['date']}'"
    rows = conn.execute(f"""
        SELECT r.race_id, r.brand, r.finish_time,
               (SELECT SUM(split_time) FROM per_horse_sectionals
                WHERE race_id = r.race_id AND brand = r.brand) AS phs_sum,
               (SELECT SUM(CASE WHEN split_time < 13 THEN split_time ELSE 0 END)
                FROM per_horse_sectionals
                WHERE race_id = r.race_id AND brand = r.brand) AS subsplit_sum
        FROM results r
        WHERE r.finish_time IS NOT NULL {where}
    """).fetchall()
    out = []
    for rid, brand, ft, phs, subs in rows:
        if phs is None:
            continue
        # If the table contains sub-splits, the actual race-time is
        # Σ(splits ≥ 13s) — the parent quarters — without the sub-splits.
        adjusted = (phs - (subs or 0.0)) if subs else phs
        delta = abs(float(ft) - float(adjusted))
        if delta > 0.3:
            out.append({
                "check_name": "sectional_total_mismatch",
                "severity": "medium",
                "race_id": rid, "brand": brand,
                "source_a": "results.finish_time",
                "source_b": "Σ per_horse_sectionals (parents only)",
                "value_a": f"{ft:.3f}", "value_b": f"{adjusted:.3f}",
                "detail": f"race {rid} winner {brand}: finish_time={ft} vs Σ parents={adjusted:.3f} (Δ={ft-adjusted:+.3f})",
            })
    return out


def check_horse_no_unique_per_race(conn, scope):
    """results.horse_no must be unique per race (1..N saddle numbers)."""
    where = "WHERE 1=1"
    if scope.get("date"):
        where += f" AND date = '{scope['date']}'"
    rows = conn.execute(f"""
        SELECT race_id, horse_no, COUNT(*) AS n
        FROM results {where} AND horse_no IS NOT NULL
        GROUP BY race_id, horse_no HAVING n > 1
    """).fetchall()
    return [{
        "check_name": "horse_no_duplicate", "severity": "high",
        "race_id": rid, "source_a": "results.horse_no", "source_b": "uniqueness",
        "value_a": str(hno), "value_b": str(n),
        "detail": f"race {rid} has {n} rows with horse_no={hno}",
    } for rid, hno, n in rows]


def check_calibrated_prob_sums(conn, scope):
    """Σ predictions.calibrated_prob per race ≈ 1.0 (per-race-softmax invariant)."""
    rows = conn.execute("""
        SELECT race_id, strategy_id, SUM(calibrated_prob) AS s, COUNT(*) AS n
        FROM predictions GROUP BY race_id, strategy_id
        HAVING ABS(s - 1.0) > 0.02
    """).fetchall()
    return [{
        "check_name": "calibrated_prob_sum",
        "severity": "high",
        "race_id": rid,
        "source_a": "predictions.calibrated_prob sum",
        "source_b": "softmax invariant",
        "value_a": f"{s:.4f}", "value_b": "1.0",
        "detail": f"race {rid} strategy {sid}: Σ cal_prob = {s:.4f} (n={n})",
    } for rid, sid, s, n in rows]


def check_bet_ledger_settlement(conn, scope):
    """bet_ledger.won = -1 for any race where results.position IS NOT NULL."""
    where = ""
    if scope.get("date"):
        where = f"AND bl.race_date = '{scope['date']}'"
    rows = conn.execute(f"""
        SELECT bl.id, bl.bet_strategy_id, bl.race_id, bl.brand
        FROM bet_ledger bl
        WHERE bl.won = -1 {where}
          AND EXISTS (SELECT 1 FROM results r
                      WHERE r.race_id = bl.race_id
                        AND r.position IS NOT NULL AND r.position != '')
    """).fetchall()
    return [{
        "check_name": "unsettled_after_results",
        "severity": "medium",
        "race_id": rid, "brand": brand,
        "source_a": "bet_ledger.won", "source_b": "results.position",
        "value_a": "-1 (pending)", "value_b": "exists",
        "detail": f"bet {bid} strategy {sid} race {rid} brand {brand} stuck unsettled",
    } for bid, sid, rid, brand in rows]


def check_feature_values_collapsed(conn, scope):
    """Any feature with σ == 0 across last 60 days has collapsed (constant
    output → contributes zero discriminative signal)."""
    rows = conn.execute("""
        SELECT fv.feature_id,
               COUNT(DISTINCT fv.value) AS distinct_vals,
               COUNT(*) AS total
        FROM feature_values fv
        JOIN races r ON r.id = fv.race_id
        WHERE r.date >= date('now', '-60 days')
          AND fv.value IS NOT NULL
        GROUP BY fv.feature_id
        HAVING distinct_vals <= 1 AND total > 50
    """).fetchall()
    return [{
        "check_name": "feature_collapsed", "severity": "low",
        "source_a": f"feature_values[{fid}]",
        "source_b": "distinct_count",
        "value_a": str(distinct), "value_b": f">{1}",
        "detail": f"feature {fid}: only {distinct} distinct value across {total} rows in last 60d",
    } for fid, distinct, total in rows]


def check_running_comments_brand_consistency(conn, scope):
    """Every running_comments brand should exist in the race's results."""
    where = ""
    if scope.get("date"):
        where = f"AND r.date = '{scope['date']}'"
    rows = conn.execute(f"""
        SELECT rc.race_id, rc.brand
        FROM running_comments rc
        WHERE NOT EXISTS (
            SELECT 1 FROM results r
            WHERE r.race_id = rc.race_id AND r.brand = rc.brand
        )
        {where.replace('r.date', '(SELECT date FROM races WHERE id=rc.race_id)') if scope.get('date') else ''}
    """).fetchall()
    return [{
        "check_name": "running_comment_orphan_brand",
        "severity": "medium",
        "race_id": rid, "brand": brand,
        "source_a": "running_comments.brand", "source_b": "results.brand",
        "value_a": brand, "value_b": "missing",
        "detail": f"running_comments for race {rid} brand {brand} has no matching results row",
    } for rid, brand in rows]


CHECKS: list[Callable] = [
    check_fk_orphans_predictions,
    check_fk_orphans_feature_values,
    check_brand_in_horses,
    check_predictions_without_results,
    check_win_dividend_matches_winner,
    check_place_dividend_count,
    check_sectional_finish_time,
    check_horse_no_unique_per_race,
    check_calibrated_prob_sums,
    check_bet_ledger_settlement,
    check_feature_values_collapsed,
    check_running_comments_brand_consistency,
]


# ─── Auto-heal ───────────────────────────────────────────────────────────────
def auto_heal(conn, violation: dict) -> bool:
    """Try to fix a single violation. Returns True on success."""
    name = violation["check_name"]
    if name in ("place_dividend_incomplete", "win_dividend_winner_mismatch",
                "sectional_total_mismatch", "running_comment_orphan_brand"):
        # Re-fetch the meeting from HKJC; the new dividend-parser fix or
        # corunning fix should reconcile.
        race = conn.execute(
            "SELECT date, course FROM races WHERE id = ?",
            (violation.get("race_id"),),
        ).fetchone()
        if not race:
            return False
        date, course = race
        try:
            subprocess.run(
                [sys.executable, "-m", "scrapers.scrape_results",
                 "--date", date, "--course", course, "--force-refresh"],
                cwd=str(BASE_DIR), check=True, capture_output=True, timeout=120,
            )
            return True
        except Exception:
            return False
    if name == "unsettled_after_results":
        race_date = conn.execute(
            "SELECT race_date FROM bet_ledger "
            "WHERE race_id=? AND brand=? LIMIT 1",
            (violation.get("race_id"), violation.get("brand")),
        ).fetchone()
        if not race_date:
            return False
        try:
            subprocess.run(
                [sys.executable, "-m", "betting.bet_runner",
                 "--all", "--from", race_date[0], "--to", race_date[0]],
                cwd=str(BASE_DIR), check=True, capture_output=True, timeout=180,
            )
            return True
        except Exception:
            return False
    if name == "fk_predictions_race":
        conn.execute("DELETE FROM predictions WHERE race_id = ?",
                     (violation.get("race_id"),))
        conn.commit()
        return True
    if name == "fk_feature_values_race":
        conn.execute("DELETE FROM feature_values WHERE race_id = ?",
                     (violation.get("race_id"),))
        conn.commit()
        return True
    if name == "brand_in_horses":
        conn.execute("INSERT OR IGNORE INTO horses (brand) VALUES (?)",
                     (violation.get("brand"),))
        conn.commit()
        return True
    return False


# ─── Driver ──────────────────────────────────────────────────────────────────
def run(scope: dict, heal: bool = False) -> dict:
    import status as _status
    conn = _conn()
    all_violations: list[dict] = []
    _status.process_up('integrity_check', ptype='oneshot', activity='running checks')
    _tid = _status.task_start('integrity_check', 'integrity check', total=len(CHECKS))
    for i, chk in enumerate(CHECKS, 1):
        try:
            vs = chk(conn, scope) or []
        except Exception as exc:
            vs = [{
                "check_name": chk.__name__, "severity": "critical",
                "source_a": "check_function", "source_b": "exception",
                "value_a": str(exc), "value_b": "—",
                "detail": f"{chk.__name__} raised: {exc}",
            }]
        all_violations.extend(vs)
        _name = chk.__name__.replace("check_", "")
        _status.task_step(_tid, done=i, msg=f'{_name}: {len(vs)} issue(s)')

    scope_str = scope.get("date") or scope.get("scope") or "full"
    cur = conn.execute(
        "INSERT INTO integrity_check_runs (scope, total_checks, passed, failed) "
        "VALUES (?, ?, ?, ?)",
        (scope_str, len(CHECKS), len(CHECKS) - len({v["check_name"] for v in all_violations}),
         len(all_violations)),
    )
    run_id = cur.lastrowid

    healed = 0
    for v in all_violations:
        if heal and SEVERITY_ORDER.get(v.get("severity", "low"), 0) <= 1:
            if auto_heal(conn, v):
                v["auto_healed"] = 1
                healed += 1
        conn.execute(
            "INSERT INTO integrity_check_violations "
            "(run_id, check_name, severity, race_id, brand, source_a, source_b, "
            " value_a, value_b, detail, auto_healed) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, v["check_name"], v.get("severity", "medium"),
             v.get("race_id"), v.get("brand"),
             v.get("source_a"), v.get("source_b"),
             v.get("value_a"), v.get("value_b"),
             v.get("detail"), v.get("auto_healed", 0)),
        )
    conn.commit()

    summary: dict[str, dict] = {}
    for v in all_violations:
        name = v["check_name"]
        bucket = summary.setdefault(name, {"count": 0, "severity": v.get("severity")})
        bucket["count"] += 1

    print(f"[integrity_check] run {run_id} scope={scope_str}: "
          f"{len(all_violations)} violations, {healed} auto-healed")
    for name, b in sorted(summary.items(), key=lambda x: -x[1]["count"]):
        print(f"  {b['severity']:<8} {name:<40} {b['count']}")
    _status.task_done(_tid, f'{len(all_violations)} violations, {healed} healed')
    _status.process_down('integrity_check', f'{len(all_violations)} violations')
    conn.close()
    return {"run_id": run_id, "violations": len(all_violations),
            "healed": healed, "summary": summary}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="restrict to one meeting date YYYY-MM-DD")
    p.add_argument("--baseline", action="store_true",
                   help="full-DB baseline (no scope filter)")
    p.add_argument("--heal", action="store_true",
                   help="attempt auto-fix for severity ≤ medium")
    ns = p.parse_args()
    scope = {"date": ns.date} if ns.date else {"scope": "full"}
    res = run(scope, heal=ns.heal)
    return 0 if res["violations"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

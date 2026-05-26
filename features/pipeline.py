"""Feature computation pipeline.

Walks a date range of races in `racing.db`, computes all 174 features
per (race, horse), and writes them to `feature_values`.

Point-in-time enforcement: each compute call receives only history strictly
before the target race's date, plus odds snapshots strictly before
`snapshot_basis` (defaults to T-1min for backtesting, or current time when
called live).

Usage:
    python3 -m features.pipeline --since 2025-12-01 --until 2026-05-26
    python3 -m features.pipeline --race-id 1234
"""

from __future__ import annotations

import argparse
import importlib
import math
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "racing.db"

sys.path.insert(0, str(BASE_DIR))

from features import compute as compute_mod  # noqa: E402
from features.catalog import FEATURES         # noqa: E402
from features.compute import FeatureContext, _nan_stub  # noqa: E402


def _resolve_fn(name: str):
    fn = getattr(compute_mod, name, None)
    return fn if callable(fn) else _nan_stub


def _load_history(conn: sqlite3.Connection, brand: str, before: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT date, venue, distance, going, class, draw, rating, trainercn AS trainer,
               jockeycn AS jockey, lbw, odds, actwt AS act_wt, declwt AS decl_wt,
               running, finishtime AS finish_time, pla AS position, gear
        FROM race_history
        WHERE brandno = ? AND date < ?
        ORDER BY date ASC
        """,
        (brand, before),
    ).fetchall()
    cols = ("date","venue","distance","going","class","draw","rating","trainer",
            "jockey","lbw","odds","act_wt","decl_wt","running","finish_time","position","gear")
    return [dict(zip(cols, r)) for r in rows]


def _compute_global_stats(conn: sqlite3.Connection, before: str) -> dict[str, Any]:
    """Aggregate jockey/trainer WRs across all results before `before`.

    Heavy if recomputed per race — call once per pipeline run.
    """
    def _wr(table_col: str) -> dict[str, float]:
        out: dict[str, float] = {}
        rows = conn.execute(
            f"SELECT {table_col}, COUNT(*) AS n, SUM(CASE WHEN position=1 THEN 1 ELSE 0 END) AS w "
            f"FROM results WHERE date < ? AND {table_col} IS NOT NULL "
            f"GROUP BY {table_col}",
            (before,),
        ).fetchall()
        for name, n, w in rows:
            if n and n >= 3:
                out[name] = w / n
        return out

    jockey_wr = _wr("jockey")
    trainer_wr = _wr("trainer")

    jt_pair: dict[tuple, float] = {}
    for j, t, n, w in conn.execute(
        "SELECT jockey, trainer, COUNT(*) AS n, SUM(CASE WHEN position=1 THEN 1 ELSE 0 END) AS w "
        "FROM results WHERE date < ? AND jockey IS NOT NULL AND trainer IS NOT NULL "
        "GROUP BY jockey, trainer", (before,),
    ).fetchall():
        if n >= 3:
            jt_pair[(j, t)] = w / n

    # Venue-conditional WRs
    def _wr_venue(col: str, course: str) -> dict[str, float]:
        out: dict[str, float] = {}
        rows = conn.execute(
            f"SELECT r.{col}, COUNT(*) AS n, SUM(CASE WHEN r.position=1 THEN 1 ELSE 0 END) AS w "
            f"FROM results r JOIN races ra ON r.race_id = ra.id "
            f"WHERE r.date < ? AND ra.course = ? AND r.{col} IS NOT NULL "
            f"GROUP BY r.{col}", (before, course),
        ).fetchall()
        for name, n, w in rows:
            if n and n >= 3:
                out[name] = w / n
        return out

    # Joint maps: (entity, condition) -> win rate, shrunk by run count.
    def _wr_joint(left_col: str, right_expr: str, right_table_join: str = "") -> dict:
        out: dict[tuple, float] = {}
        sql = (
            f"SELECT r.{left_col}, {right_expr} AS k, COUNT(*) AS n, "
            f"SUM(CASE WHEN r.position=1 THEN 1 ELSE 0 END) AS w "
            f"FROM results r {right_table_join} "
            f"WHERE r.date < ? AND r.{left_col} IS NOT NULL "
            f"GROUP BY r.{left_col}, k"
        )
        for left, right, n, w in conn.execute(sql, (before,)).fetchall():
            if right is None or n < 3:
                continue
            out[(left, right)] = w / n
        return out

    trainer_x_class = _wr_joint("trainer", "ra.class",
                                "JOIN races ra ON ra.id = r.race_id")
    trainer_x_dist  = _wr_joint("trainer", "ra.distance",
                                "JOIN races ra ON ra.id = r.race_id")
    trainer_x_venue = _wr_joint("trainer", "ra.course",
                                "JOIN races ra ON ra.id = r.race_id")
    jockey_x_venue  = _wr_joint("jockey", "ra.course",
                                "JOIN races ra ON ra.id = r.race_id")
    jockey_x_dist   = _wr_joint("jockey", "ra.distance",
                                "JOIN races ra ON ra.id = r.race_id")

    # Trainer × season phase: bucket month into early(9-11)/mid(12-3)/late(4-7).
    trainer_x_phase: dict[tuple[str, str], float] = {}
    rows = conn.execute(
        """
        SELECT r.trainer,
               CASE
                 WHEN CAST(strftime('%m', r.date) AS INTEGER) IN (9,10,11) THEN 'early'
                 WHEN CAST(strftime('%m', r.date) AS INTEGER) IN (12,1,2,3) THEN 'mid'
                 ELSE 'late'
               END AS phase,
               COUNT(*) AS n, SUM(CASE WHEN r.position=1 THEN 1 ELSE 0 END) AS w
        FROM results r WHERE r.date < ? AND r.trainer IS NOT NULL
        GROUP BY r.trainer, phase
        """, (before,),
    ).fetchall()
    for t, ph, n, w in rows:
        if n >= 3:
            trainer_x_phase[(t, ph)] = w / n

    # Trainer rolling activity over the last 30 days before `before`.
    trainer_density_30d: dict[str, float] = {}
    rows = conn.execute(
        "SELECT trainer, COUNT(*) FROM results "
        "WHERE date BETWEEN date(?, '-30 days') AND date(?, '-1 day') "
        "AND trainer IS NOT NULL GROUP BY trainer",
        (before, before),
    ).fetchall()
    for t, n in rows:
        trainer_density_30d[t] = float(n)

    # Trainer hot / cold: 30-day rolling WR minus career WR.
    trainer_hot: dict[str, float] = {}
    trainer_cold: dict[str, float] = {}
    rows = conn.execute(
        "SELECT trainer, COUNT(*) AS n, SUM(CASE WHEN position=1 THEN 1 ELSE 0 END) AS w "
        "FROM results WHERE date BETWEEN date(?, '-30 days') AND date(?, '-1 day') "
        "AND trainer IS NOT NULL GROUP BY trainer",
        (before, before),
    ).fetchall()
    for t, n, w in rows:
        if n < 3:
            continue
        recent_wr = w / n
        career_wr = trainer_wr.get(t)
        if career_wr is None:
            continue
        diff = recent_wr - career_wr
        if diff > 0:
            trainer_hot[t] = diff
        elif diff < 0:
            trainer_cold[t] = -diff  # stored as a positive "cold magnitude"

    # Active stable size: distinct horses each trainer ran in the last 60 days.
    stable_size: dict[str, float] = {}
    rows = conn.execute(
        "SELECT trainer, COUNT(DISTINCT brand) FROM results "
        "WHERE date BETWEEN date(?, '-60 days') AND date(?, '-1 day') "
        "AND trainer IS NOT NULL GROUP BY trainer",
        (before, before),
    ).fetchall()
    for t, n in rows:
        stable_size[t] = float(n)

    # Trainer first-timer WR (horse's first ever start) and returner WR (45-180d layoff).
    trainer_first_timer_wr: dict[str, float] = {}
    trainer_returner_wr: dict[str, float] = {}
    rows = conn.execute(
        """
        SELECT r.trainer,
               (SELECT COUNT(*) FROM race_history rh
                  WHERE rh.brandno = r.brand AND rh.date < r.date) AS prior_starts,
               MAX((SELECT MAX(rh.date) FROM race_history rh
                      WHERE rh.brandno = r.brand AND rh.date < r.date)) AS last_prior,
               r.date, r.position
        FROM results r WHERE r.date < ? AND r.trainer IS NOT NULL
        """, (before,),
    ).fetchall()
    ft_bucket: dict[str, list[int]] = {}
    ret_bucket: dict[str, list[int]] = {}
    for trainer, prior, last_date, run_date, position in rows:
        won = 1 if position == 1 else 0
        if prior == 0:
            ft_bucket.setdefault(trainer, []).append(won)
        elif last_date:
            try:
                gap = (datetime.fromisoformat(run_date) - datetime.fromisoformat(last_date)).days
            except Exception:
                continue
            if 45 <= gap <= 180:
                ret_bucket.setdefault(trainer, []).append(won)
    for t, runs in ft_bucket.items():
        if len(runs) >= 3:
            trainer_first_timer_wr[t] = sum(runs) / len(runs)
    for t, runs in ret_bucket.items():
        if len(runs) >= 3:
            trainer_returner_wr[t] = sum(runs) / len(runs)

    # Horse's average market-implied probability across prior runs (for A/E).
    horse_avg_implied: dict[str, float] = {}
    rows = conn.execute(
        "SELECT brand, AVG(1.0/odds) FROM results "
        "WHERE date < ? AND odds IS NOT NULL AND odds > 0 "
        "GROUP BY brand HAVING COUNT(*) >= 3",
        (before,),
    ).fetchall()
    for b, ai in rows:
        if ai is not None:
            horse_avg_implied[b] = float(ai)

    return {
        "field_avg_wr": 0.083,
        "jockey_wr": jockey_wr,
        "trainer_wr": trainer_wr,
        "jt_pair": jt_pair,
        "jockey_at_HV": _wr_venue("jockey", "HV"),
        "jockey_at_ST": _wr_venue("jockey", "ST"),
        "trainer_at_HV": _wr_venue("trainer", "HV"),
        "trainer_at_ST": _wr_venue("trainer", "ST"),
        "trainer_hot": trainer_hot,
        "trainer_cold": trainer_cold,
        "trainer_x_class": trainer_x_class,
        "trainer_x_dist": trainer_x_dist,
        "trainer_x_venue": trainer_x_venue,
        "trainer_x_phase": trainer_x_phase,
        "trainer_density_30d": trainer_density_30d,
        "stable_size": stable_size,
        "trainer_first_timer_wr": trainer_first_timer_wr,
        "trainer_returner_wr": trainer_returner_wr,
        "jockey_x_venue": jockey_x_venue,
        "jockey_x_dist": jockey_x_dist,
        "horse_avg_implied": horse_avg_implied,
    }


def compute_for_race(conn: sqlite3.Connection, race_row: dict, global_stats: dict, *, snapshot_basis: str | None = None) -> int:
    """Compute every feature for every horse in this race; write to feature_values.
    Returns count of (horse, feature) tuples written.
    """
    race_id = race_row["id"]
    entries = conn.execute(
        "SELECT id, brand, horse_name, jockey, trainer, draw, act_wt, decl_wt, odds, "
        "running_style FROM results WHERE race_id = ?", (race_id,),
    ).fetchall()
    entry_cols = ("result_id","brand","horse_name","jockey","trainer","draw","act_wt",
                  "decl_wt","odds","running_style")
    field: list[dict] = []
    for r in entries:
        d = dict(zip(entry_cols, r))
        horse = conn.execute("SELECT age, sex, colour, origin, rating FROM horses WHERE brand = ?", (d["brand"],)).fetchone()
        if horse:
            d.update(zip(("age","sex","colour","origin","rating"), horse))
        field.append(d)

    field_history = {e["brand"]: _load_history(conn, e["brand"], race_row["date"]) for e in field}
    snapshot = snapshot_basis or (race_row["date"] + "T23:59:59")

    written = 0
    for entry in field:
        ctx = FeatureContext(
            race=race_row,
            entry=entry,
            history=field_history[entry["brand"]],
            field=field,
            field_history=field_history,
            global_stats=global_stats,
            race_id=race_id,
            conn=conn,
            snapshot_basis=snapshot,
        )
        for feat in FEATURES:
            fn = _resolve_fn(feat.compute_fn_name)
            try:
                val = fn(ctx)
            except Exception:
                val = None
            # Coerce non-numeric returns (e.g. a stringified HKJC field that
            # slipped through a compute function) to NaN — feature_values.value
            # is REAL and the walk-forward `float(val)` cast will otherwise raise.
            if val is not None:
                if isinstance(val, bool):
                    val = float(val)
                elif not isinstance(val, (int, float)):
                    val = None
                elif isinstance(val, float) and math.isnan(val):
                    val = None
            conn.execute(
                """
                INSERT INTO feature_values (race_id, horse_id, brand, feature_id, value, snapshot_basis)
                VALUES (?, (SELECT id FROM horses WHERE brand = ?), ?, ?, ?, ?)
                ON CONFLICT(race_id, brand, feature_id, snapshot_basis) DO UPDATE SET
                    value = excluded.value, computed_at = CURRENT_TIMESTAMP
                """,
                (race_id, entry["brand"], entry["brand"], feat.id, val, snapshot),
            )
            written += 1
    conn.commit()
    return written


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--since", help="start date YYYY-MM-DD (inclusive)")
    p.add_argument("--until", help="end date YYYY-MM-DD (inclusive)")
    p.add_argument("--race-id", type=int, help="single race id")
    p.add_argument("--limit", type=int, default=0, help="cap races processed (0=all)")
    args = p.parse_args()

    if not DB_PATH.exists():
        raise SystemExit(f"DB not found at {DB_PATH}; run db.py --init first")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode = WAL")

    if args.race_id:
        race = conn.execute("SELECT id, date, course, race_no, distance, class, going, participants, race_name, prize "
                            "FROM races WHERE id = ?", (args.race_id,)).fetchone()
        if not race:
            raise SystemExit(f"race id {args.race_id} not found")
        cols = ("id","date","course","race_no","distance","class","going","participants","race_name","prize")
        race_row = dict(zip(cols, race))
        gs = _compute_global_stats(conn, race_row["date"])
        n = compute_for_race(conn, race_row, gs)
        print(f"race {args.race_id}: wrote {n} feature_values")
        return

    where = []
    params: list = []
    if args.since:
        where.append("date >= ?"); params.append(args.since)
    if args.until:
        where.append("date <= ?"); params.append(args.until)
    sql = ("SELECT id, date, course, race_no, distance, class, going, participants, race_name, prize "
           "FROM races")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY date, course, race_no"
    if args.limit:
        sql += f" LIMIT {args.limit}"
    races = conn.execute(sql, params).fetchall()
    cols = ("id","date","course","race_no","distance","class","going","participants","race_name","prize")

    # Recompute global stats per date (Monte Carlo savings — same date reuses).
    cur_date = None
    gs: dict[str, Any] = {}
    t0 = time.time()
    total = 0
    for r in races:
        race_row = dict(zip(cols, r))
        if race_row["date"] != cur_date:
            cur_date = race_row["date"]
            gs = _compute_global_stats(conn, cur_date)
        try:
            n = compute_for_race(conn, race_row, gs)
            total += n
        except Exception as exc:
            print(f"race {race_row['id']} ({race_row['date']}): error {exc}")
    elapsed = time.time() - t0
    print(f"done: {len(races)} races, {total} feature_values, {elapsed:.1f}s")


if __name__ == "__main__":
    main()

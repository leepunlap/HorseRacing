#!/usr/bin/env python3
"""Daily track-bias aggregator.

Not a web scraper — a SQL aggregator that scans `results` + `race_history` to
populate `track_bias_daily` per (date, course):

  * inside_win_rate_residual  — share of winners drawn in barriers 1-4 today
                                minus the long-term inner share.
  * front_runner_win_rate_residual — share of winners with running_style starting
                                with '1' today, minus the long-term share.
  * par_time_residual         — todays' mean finish_time per (distance, going)
                                minus the long-term mean.

Used by Cat 15 features H167 (today_bias), H172 (inner_resid), H174 (closer_boost).

Run AFTER `scrape_per_horse_sectionals.py` (which is what fills running_style
ranks) and ideally after results are scraped for the day.

Usage:
    python3 -m scrapers.compute_track_bias --date 2026-05-26
    python3 -m scrapers.compute_track_bias --since 2024-09-01 --until 2026-05-26
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapers._base import BaseScraper, log, txn


def _ft_to_seconds(ft) -> float | None:
    """results.finish_time is stored as 'M:SS.SS' string; convert to seconds."""
    if ft is None:
        return None
    if isinstance(ft, (int, float)):
        return float(ft)
    s = str(ft).strip()
    if not s:
        return None
    if ":" in s:
        try:
            m, sec = s.split(":")
            return int(m) * 60 + float(sec)
        except (ValueError, IndexError):
            return None
    try:
        return float(s)
    except ValueError:
        return None


class TrackBiasComputer(BaseScraper):
    name = "track_bias"

    def run(self, args: list[str]) -> int:
        p = argparse.ArgumentParser(prog="compute_track_bias")
        p.add_argument("--date", help="YYYY-MM-DD (single meeting)")
        p.add_argument("--since", help="YYYY-MM-DD start (inclusive)")
        p.add_argument("--until", help="YYYY-MM-DD end (inclusive)")
        ns = p.parse_args(args)

        dates: list[str] = []
        if ns.since and ns.until:
            d0 = datetime.fromisoformat(ns.since).date()
            d1 = datetime.fromisoformat(ns.until).date()
            cur = d0
            while cur <= d1:
                dates.append(cur.isoformat())
                cur += timedelta(days=1)
        elif ns.date:
            dates = [ns.date]
        else:
            log("specify --date or --since/--until")
            return 2

        conn = self.db()
        # Long-term baselines: computed once over the full history.
        inner_baseline, front_baseline = self._baselines(conn)
        par_lookup = self._par_lookup(conn)

        total = 0
        for d in dates:
            if self.should_stop():
                break
            for course in ("ST", "HV"):
                try:
                    if self._compute_one(d, course, inner_baseline, front_baseline, par_lookup):
                        total += 1
                except Exception as exc:
                    log(f"[{self.name}] {d}/{course}: {exc}")
        self.checkpoint({"rows": total})
        log(f"[{self.name}] done: {total} (date,course) rows")
        return 0

    def _baselines(self, conn) -> tuple[float, float]:
        """Long-term mean of (inner-draw-winning-rate, front-runner-winning-rate)."""
        row = conn.execute(
            """
            SELECT
              AVG(CASE WHEN position = 1 AND draw BETWEEN 1 AND 4 THEN 1.0
                       WHEN position = 1 THEN 0.0 ELSE NULL END) AS inner_rate,
              AVG(CASE WHEN position = 1 AND running_style LIKE '1%' THEN 1.0
                       WHEN position = 1 THEN 0.0 ELSE NULL END) AS front_rate
            FROM results
            WHERE position IS NOT NULL
            """
        ).fetchone()
        inner = float(row[0]) if row and row[0] is not None else 0.30
        front = float(row[1]) if row and row[1] is not None else 0.35
        return inner, front

    def _par_lookup(self, conn) -> dict[tuple[int, str, str], float]:
        """Long-term mean winning finish_time per (distance, course, going-coarse).
        finish_time is stored as 'M:SS.SS' so we aggregate in Python rather than SQL."""
        rows = conn.execute(
            """
            SELECT ra.distance, ra.course,
                   LOWER(SUBSTR(COALESCE(ra.going, ''), 1, 4)) AS going4,
                   r.finish_time
            FROM results r
            JOIN races ra ON ra.id = r.race_id
            WHERE r.position = 1 AND r.finish_time IS NOT NULL AND ra.distance IS NOT NULL
            """
        ).fetchall()
        buckets: dict[tuple[int, str, str], list[float]] = {}
        for dist, course, going4, ft in rows:
            secs = _ft_to_seconds(ft)
            if secs is None:
                continue
            buckets.setdefault((int(dist), course, going4 or ""), []).append(secs)
        return {k: sum(v) / len(v) for k, v in buckets.items() if v}

    def _compute_one(self, date_str: str, course: str,
                     inner_baseline: float, front_baseline: float,
                     par_lookup: dict) -> bool:
        conn = self.db()
        # Winners and their characteristics for this meeting
        rows = conn.execute(
            """
            SELECT r.draw, r.running_style, r.finish_time, ra.distance, ra.going
            FROM results r
            JOIN races ra ON ra.id = r.race_id
            WHERE r.position = 1 AND ra.date = ? AND ra.course = ?
            """,
            (date_str, course),
        ).fetchall()
        if not rows:
            return False

        # Inner-draw winners
        inner_wins = sum(1 for r in rows if r[0] and 1 <= int(r[0]) <= 4)
        inner_rate_today = inner_wins / len(rows)
        # Front-runner winners
        front_wins = sum(1 for r in rows if (r[1] or "").startswith("1"))
        front_rate_today = front_wins / len(rows)

        # Par-time residual: average (actual − par) across races where we have a par.
        residuals: list[float] = []
        for _draw, _style, ft, dist, going in rows:
            secs = _ft_to_seconds(ft)
            if secs is None or dist is None:
                continue
            g4 = (going or "").lower()[:4]
            par = par_lookup.get((int(dist), course, g4))
            if par is not None:
                residuals.append(secs - par)
        par_resid = sum(residuals) / len(residuals) if residuals else None

        # Pull the rail position too (already scraped meeting-wide)
        rail_row = conn.execute(
            "SELECT rail FROM rail_position WHERE date = ? AND course = ?",
            (date_str, course),
        ).fetchone()
        rail = rail_row[0] if rail_row else None

        row = {
            "date": date_str,
            "course": course,
            "rail": rail,
            "inside_win_rate_residual": round(inner_rate_today - inner_baseline, 4),
            "front_runner_win_rate_residual": round(front_rate_today - front_baseline, 4),
            "par_time_residual": round(par_resid, 3) if par_resid is not None else None,
            "sample_races": len(rows),
            "notes": None,
        }
        with txn(conn):
            self.upsert("track_bias_daily", row, conflict_cols=("date", "course"))
        log(
            f"[{self.name}] {date_str}/{course}: "
            f"inner={inner_rate_today:.2f}(Δ{row['inside_win_rate_residual']:+.2f}) "
            f"front={front_rate_today:.2f}(Δ{row['front_runner_win_rate_residual']:+.2f}) "
            f"races={len(rows)}"
        )
        return True


if __name__ == "__main__":
    sys.exit(TrackBiasComputer.main())

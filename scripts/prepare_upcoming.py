"""prepare_upcoming — the pre-race (stage 1) pipeline for upcoming meetings.

Runs the chain that turns a freshly-published race card into model
predictions, so the dashboard's "Upcoming Race" pane goes green without manual
steps:

  1. scrape_race_card --next      (declared runners, stage 1, no odds)
  2. delete junk stub races        (--next inserts 1-race shells for non-meeting
                                    days; remove any future race with no runners)
  3. compute features              (features.pipeline) for each upcoming meeting
  4. generate predictions          (walk_forward.run_strategy) for each meeting

Odds (stage 2) and live bet decisions stay with the in-process race-day loops.

Instrumented via status.py so it appears as a task on the dashboard. Designed
to be run from the scheduler (action `prepare_upcoming`) daily, or by hand:
    python3 -m scripts.prepare_upcoming [strategy_id]
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
DB = BASE / "data" / "racing.db"

import status as _status                       # noqa: E402
from scrapers.scrape_race_card import RaceCardScraper  # noqa: E402
from scrapers.scrape_barrier_trials import BarrierTrialsScraper  # noqa: E402
from features import pipeline as feat_pipeline # noqa: E402
from models import walk_forward as wf          # noqa: E402


def _upcoming_meetings(conn) -> list[tuple[str, str]]:
    today = date.today().isoformat()
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT DISTINCT date, course FROM races WHERE date >= ? ORDER BY date, course", (today,))]


def main(strategy_id: int = 1) -> int:
    _status.process_up("prepare_upcoming", ptype="oneshot", activity="starting")
    tid = _status.task_start("prepare_upcoming", "prepare upcoming meetings", total=6)
    try:
        # 1. scrape upcoming cards (also enriches new horses: name_zh + pedigree)
        _status.task_step(tid, done=1, msg="scraping race cards (--next)")
        RaceCardScraper().main(["--next"])

        # 1b. recent barrier trials — debutants' only form line; feeds H094.
        _status.task_step(tid, msg="scraping recent barrier trials")
        BarrierTrialsScraper().main(["--recent"])

        conn = sqlite3.connect(DB)
        conn.execute("PRAGMA journal_mode = WAL")

        # 2. clean junk stub races (future races with no runner rows)
        before = conn.execute("SELECT COUNT(*) FROM races WHERE date >= date('now')").fetchone()[0]
        conn.execute("DELETE FROM races WHERE date >= date('now') "
                     "AND id NOT IN (SELECT DISTINCT race_id FROM results WHERE race_id IS NOT NULL)")
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM races WHERE date >= date('now')").fetchone()[0]
        _status.task_step(tid, done=3, msg=f"cleaned {before - after} stub races")

        meetings = _upcoming_meetings(conn)
        dates = sorted({d for d, _c in meetings})
        if not dates:
            _status.task_done(tid, "no upcoming meetings")
            _status.process_down("prepare_upcoming", "idle")
            print("[prepare_upcoming] no upcoming meetings")
            return 0

        # 3. features for each upcoming date
        _status.task_step(tid, done=4, msg=f"computing features for {len(dates)} date(s)")
        cols = ("id", "date", "course", "race_no", "distance", "class", "going", "participants", "race_name", "prize")
        for d in dates:
            races = conn.execute(
                "SELECT id,date,course,race_no,distance,class,going,participants,race_name,prize "
                "FROM races WHERE date = ? ORDER BY course, race_no", (d,)).fetchall()
            gs = feat_pipeline._compute_global_stats(conn, d)
            for r in races:
                try:
                    feat_pipeline.compute_for_race(conn, dict(zip(cols, r)), gs)
                except Exception as exc:
                    print(f"[prepare_upcoming] feature error race {r[0]}: {exc}")
        conn.commit()

        # 4. predictions for each upcoming date
        _status.task_step(tid, done=5, msg=f"predicting {len(dates)} date(s)")
        for d in dates:
            try:
                wf.run_strategy(strategy_id, d, d)
            except SystemExit as exc:
                print(f"[prepare_upcoming] predict {d}: {exc}")

        # 5. AI overlays per upcoming meeting (advisory; best-effort — a failed
        #    AI step must never fail the prediction chain): news preview + a
        #    per-horse assessment of each runner's latest barrier trial.
        _status.task_step(tid, done=6, msg="generating AI news + trial notes")
        try:
            from scripts import fetch_race_news, summarize_trials
            for d, course in meetings:
                for fn, label in ((fetch_race_news.main, "news"),
                                  (summarize_trials.main, "trials")):
                    try:
                        fn(d, course)
                    except Exception as exc:
                        print(f"[prepare_upcoming] {label} {d}/{course}: {exc}")
        except Exception as exc:
            print(f"[prepare_upcoming] AI step skipped: {exc}")

        conn.close()
        msg = f"{len(dates)} meeting(s): {', '.join(dates)}"
        _status.task_done(tid, msg)
        _status.process_down("prepare_upcoming", "done")
        print(f"[prepare_upcoming] done — {msg}")
        return 0
    except Exception as exc:
        _status.task_error(tid, str(exc))
        _status.process_down("prepare_upcoming", "error")
        print(f"[prepare_upcoming] failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 1))

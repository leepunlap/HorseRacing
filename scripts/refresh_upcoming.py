"""refresh_upcoming — keep upcoming-meeting odds + predictions current.

Run frequently from cron (every ~30 min). On each run:
  1. Poll win odds for every upcoming meeting → odds_snapshots time series
     (this is the odds *trend*; cheap, no-ops when odds aren't open yet).
  2. Re-run Stage-2 predictions for any meeting that is imminent/live (from ~3h
     before its first race to ~30 min after its last) so the market blend uses
     the latest odds. Far-off meetings are only polled, not re-trained.

Off race days / when nothing is upcoming this no-ops quickly.

Usage:  python3 -m scripts.refresh_upcoming
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from datetime import date as _date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
DB = BASE / "data" / "racing.db"

import status as _status                          # noqa: E402
from scrapers import odds_poller as op            # noqa: E402
from models import walk_forward as wf             # noqa: E402

REPRED_BEFORE_MIN = 180   # start re-predicting 3h before first post
REPRED_AFTER_MIN = 30     # keep going until 30 min after last post


def main(strategy_id: int = 1) -> int:
    conn = sqlite3.connect(DB)
    today = _date.today().isoformat()
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM races WHERE date >= ? ORDER BY date", (today,))]
    if not dates:
        return 0   # nothing upcoming — silent no-op

    _status.process_up("refresh_upcoming", ptype="oneshot", activity="polling odds")
    tid = _status.task_start("refresh_upcoming", "refresh odds + predictions", total=2)
    try:
        # 1. poll odds for every upcoming date (builds the trend)
        polled = 0
        for d in dates:
            try:
                n, _s = asyncio.run(op._poll_once(d))
                polled += n
            except Exception as exc:
                print(f"[refresh_upcoming] poll {d}: {exc}")
        _status.task_step(tid, done=1, msg=f"{polled} odds changes across {len(dates)} date(s)")

        # 2. re-predict imminent/live meetings (market blend on latest odds)
        now = datetime.now()
        repred = []
        for d in dates:
            posts = []
            for (pt,) in conn.execute(
                "SELECT post_time FROM races WHERE date=? AND post_time IS NOT NULL", (d,)):
                try:
                    posts.append(datetime.fromisoformat(f"{d}T{pt}:00"))
                except Exception:
                    pass
            if not posts:
                continue
            first, last = min(posts), max(posts)
            if (first - timedelta(minutes=REPRED_BEFORE_MIN)) <= now <= (last + timedelta(minutes=REPRED_AFTER_MIN)):
                try:
                    wf.run_strategy(strategy_id, d, d)
                    repred.append(d)
                except SystemExit as exc:
                    print(f"[refresh_upcoming] predict {d}: {exc}")
        _status.task_step(tid, done=2, msg=f"re-predicted {len(repred)} live meeting(s)")
        conn.close()
        _status.task_done(tid, f"polled {polled}, re-predicted {repred or 'none (no live meeting)'}")
        _status.process_down("refresh_upcoming", "done")
        print(f"[refresh_upcoming] polled {polled} odds changes; re-predicted {repred}")
        return 0
    except Exception as exc:
        _status.task_error(tid, str(exc))
        _status.process_down("refresh_upcoming", "error")
        print(f"[refresh_upcoming] failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

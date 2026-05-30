"""Race-day calendar + per-meeting decision-loop spawner.

On each tick (60s), look up upcoming races in `races` whose date == today and
whose post_time (HH:MM) is within the next 10 minutes. For each one not yet
being watched, spawn a `decision_loop.race_loop()` task that runs T-10→T-0
and writes recommendations / paper bets.

Activated from app.py lifespan.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "racing.db"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA journal_mode = WAL")
    return c


def _race_posttime(conn: sqlite3.Connection, race_id: int) -> datetime | None:
    row = conn.execute("SELECT date, post_time FROM races WHERE id = ?", (race_id,)).fetchone()
    if not row or not row[1]:
        return None
    try:
        return datetime.fromisoformat(f"{row[0]}T{row[1]}:00")
    except Exception:
        return None


async def run_forever(broadcast=None) -> None:
    """Main calendar loop. Survives errors; cancelled by lifespan teardown."""
    from live.decision_loop import race_loop  # local to avoid circular at import
    from live.post_settle import settle_loop   # post-race auto-scrape + settle

    # `watched` covers the pre-race decision loop (T-10 → T-0);
    # `settled_watched` is a separate set so the post-race settle task can
    # be armed independently even after the decision loop window closes.
    watched: set[int] = set()
    settled_watched: set[int] = set()
    import status as _status
    _status.process_up('live_scheduler', ptype='loop', activity='watching race calendar')
    try:
        while True:
            try:
                if not DB_PATH.exists():
                    _status.heartbeat('live_scheduler', 'waiting for DB')
                    await asyncio.sleep(60)
                    continue
                today = datetime.now().date().isoformat()
                conn = _conn()
                rows = conn.execute(
                    "SELECT id, course, race_no, post_time FROM races WHERE date = ?",
                    (today,),
                ).fetchall()
                conn.close()
                now = datetime.now()
                _status.heartbeat('live_scheduler',
                                  f'{len(rows)} race(s) today; {len(watched)} watched, '
                                  f'{len(settled_watched)} settling')
                for race_id, course, race_no, post_time in rows:
                    if not post_time:
                        continue
                    try:
                        pt = datetime.fromisoformat(f"{today}T{post_time}:00")
                    except Exception:
                        continue
                    secs_until_post = (pt - now).total_seconds()
                    # Pre-race decision loop window (T-10 → T-0).
                    if race_id not in watched and -60 < secs_until_post <= 600:
                        watched.add(race_id)
                        asyncio.create_task(race_loop(race_id, course, race_no, pt, broadcast))
                    # Post-race auto-settle: arm at T+3 min so HKJC has had
                    # time to publish the photo-confirmed result. The loop
                    # itself does the polling + giving-up.
                    if race_id not in settled_watched and secs_until_post <= -180:
                        settled_watched.add(race_id)
                        asyncio.create_task(settle_loop(race_id, course, race_no, pt, broadcast))
            except Exception as exc:
                if broadcast is not None:
                    try:
                        await broadcast.broadcast({
                            "type": "scraper_log",
                            "text": f"[live_scheduler] error: {exc}",
                            "task": "live_scheduler",
                        })
                    except Exception:
                        pass
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        return
    finally:
        try:
            _status.process_down('live_scheduler')
        except Exception:
            pass

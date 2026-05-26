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

    watched: set[int] = set()
    try:
        while True:
            try:
                if not DB_PATH.exists():
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
                for race_id, course, race_no, post_time in rows:
                    if race_id in watched:
                        continue
                    if not post_time:
                        continue
                    try:
                        pt = datetime.fromisoformat(f"{today}T{post_time}:00")
                    except Exception:
                        continue
                    secs_until_post = (pt - now).total_seconds()
                    if -60 < secs_until_post <= 600:
                        watched.add(race_id)
                        asyncio.create_task(race_loop(race_id, course, race_no, pt, broadcast))
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

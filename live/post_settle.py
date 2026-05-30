"""Post-race auto-settle loop.

Spawned per race ~3 minutes after post_time. Polls HKJC's results page
every ~60s up to a deadline; once a row in `results` for this race has
`position` populated, re-runs `bet_runner` for the date (settles every
strategy's ledger row), broadcasts a `race_settled` event so the SPA
can refresh, and exits.

Keeps the in-process model symmetric with the pre-race decision loop:
both are spawned from `live.scheduler.run_forever`, both touch only the
single race they were created for, both broadcast their progress.
"""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "racing.db"

# Poll cadence + how long to wait before giving up.
POLL_INTERVAL_SEC = 60
DEADLINE_MIN = 25                              # 25 min after post_time


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA journal_mode = WAL")
    return c


def _has_position(race_id: int) -> bool:
    """True iff at least one results row for this race has a numeric position."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM results WHERE race_id = ? "
            "AND position IS NOT NULL AND position != '' LIMIT 1",
            (race_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


async def _broadcast(broadcast, payload: dict) -> None:
    if broadcast is None:
        return
    try:
        await broadcast.broadcast(payload)
    except Exception:
        pass


async def _run_scraper(date_str: str, course: str) -> int:
    """Invoke scrape_results for one meeting. Returns subprocess returncode."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "scrapers.scrape_results",
        "--date", date_str, "--course", course, "--force-refresh",
        cwd=str(BASE_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return -1
    return proc.returncode or 0


async def _run_settle(date_str: str) -> int:
    """Invoke bet_runner --all for the day to settle ledger rows."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "betting.bet_runner",
        "--all", "--from", date_str, "--to", date_str,
        cwd=str(BASE_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        await asyncio.wait_for(proc.communicate(), timeout=240)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return -1
    return proc.returncode or 0


async def settle_loop(race_id: int, course: str, race_no: int,
                      post_time: datetime, broadcast) -> None:
    """Wait for results, then trigger scrape + settle. One task per race.
    Spawned by live.scheduler at post_time + 3 min."""
    import status as _status
    date_str = post_time.date().isoformat()
    label = f"{date_str}/{course}/R{race_no}"
    deadline = post_time.timestamp() + DEADLINE_MIN * 60

    tid = _status.task_start('live_scheduler', f'Settle {course} R{race_no}',
                             group='live_scheduler')
    _status.task_step(tid, msg='polling HKJC for results')
    await _broadcast(broadcast, {
        "type": "scraper_log", "task": "post_settle",
        "text": f"[post_settle] {label}: armed; polling every {POLL_INTERVAL_SEC}s",
    })

    # Phase 1: poll HKJC until results are published.
    while datetime.now().timestamp() < deadline:
        if _has_position(race_id):
            break
        # Try a fetch — HKJC publishes after the photo is signed off,
        # usually ~5 min after the race.
        rc = await _run_scraper(date_str, course)
        if rc == 0 and _has_position(race_id):
            break
        await asyncio.sleep(POLL_INTERVAL_SEC)
    else:
        _status.task_error(tid, 'deadline reached without results')
        await _broadcast(broadcast, {
            "type": "scraper_log", "task": "post_settle",
            "text": f"[post_settle] {label}: deadline reached without results; giving up",
        })
        return

    # Phase 2: results landed → settle every bet strategy for the day.
    _status.task_step(tid, msg='results in; settling ledger')
    rc = await _run_settle(date_str)
    if rc == 0:
        _status.task_done(tid, 'settled')
    else:
        _status.task_error(tid, f'settle exit {rc}')
    await _broadcast(broadcast, {
        "type": "race_settled",
        "race_id": race_id, "date": date_str,
        "course": course, "race_no": race_no,
        "ok": rc == 0,
    })

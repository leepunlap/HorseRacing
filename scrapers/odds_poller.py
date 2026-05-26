"""In-process pre-race odds poller.

Runs as an asyncio task inside the FastAPI process (NOT a subprocess) — wired
into app.py's lifespan so it dies cleanly when the event loop closes.

Polling cadence: every 30 seconds during a race-day window of T-60→T-0 around
each scheduled race post-time. Outside that window the loop sleeps 60s and
re-checks the calendar.

Data source: HKJC tote WIN/PLACE odds endpoint —
https://racing.hkjc.com/racing/info/meeting/Odds/WP/<date>/<course>/<raceNo>
which returns a JSON-ish blob. We extract per-horse win_odds, place_odds and
the WIN pool total per race, and append rows to `odds_snapshots`.

Activation: app.py imports `start_poller`.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "racing.db"


HKJC_ODDS_URL = (
    "https://racing.hkjc.com/racing/info/meeting/Odds/WP/{date}/{course}/{race_no}"
)


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA journal_mode = WAL")
    return c


def _is_race_day(now: datetime) -> bool:
    # HK racing is Wed evening + Sun afternoon plus some midweek public-holiday
    # meetings. We use weekday gate as a cheap default; the calendar table can
    # override this once race_card scraper has populated post_time.
    return now.weekday() in (2, 6)


async def _fetch_odds(date_str: str, course: str, race_no: int) -> list[dict[str, Any]] | None:
    url = HKJC_ODDS_URL.format(date=date_str.replace("-", "/"), course=course, race_no=race_no)
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            text = r.text
        # The endpoint sometimes wraps JSON in a callback; strip non-JSON prefix.
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        data = json.loads(m.group(0))
    except Exception:
        return None

    horses = data.get("oddsNodes") or data.get("data", {}).get("oddsNodes") or []
    out: list[dict[str, Any]] = []
    for h in horses:
        try:
            out.append({
                "horse_no": int(h.get("horseNo") or h.get("no") or 0),
                "brand": h.get("brandNo") or h.get("brand"),
                "win_odds": _f(h.get("winOdds") or h.get("win")),
                "place_odds": _f(h.get("placeOdds") or h.get("place")),
            })
        except Exception:
            continue
    pool_total = _f(data.get("winPool") or data.get("pool"))
    for row in out:
        row["pool_total"] = pool_total
    return out


def _f(v: Any) -> float | None:
    try:
        f = float(v)
        return f if f > 0 else None
    except Exception:
        return None


async def _poll_once(date_str: str) -> int:
    """Poll all known races for `date_str`; return rows written."""
    if not DB_PATH.exists():
        return 0
    conn = _conn()
    races = conn.execute(
        "SELECT id, course, race_no FROM races WHERE date = ? ORDER BY course, race_no",
        (date_str,),
    ).fetchall()
    now = datetime.now().isoformat()
    written = 0
    for race_id, course, race_no in races:
        snapshot = await _fetch_odds(date_str, course, race_no)
        if not snapshot:
            continue
        for h in snapshot:
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO odds_snapshots
                        (race_id, date, course, race_no, horse_no, brand, ts,
                         win_odds, place_odds, pool_total, source)
                    VALUES (?,?,?,?,?,?,?,?,?,?,'hkjc_tote')
                    """,
                    (race_id, date_str, course, race_no, h["horse_no"], h.get("brand"),
                     now, h.get("win_odds"), h.get("place_odds"), h.get("pool_total")),
                )
                written += 1
            except Exception:
                continue
    conn.commit()
    conn.close()
    return written


async def run_forever(broadcast=None) -> None:  # broadcast: optional Broadcaster
    """Long-lived loop. Polls every 30s on race days, sleeps 60s otherwise.

    Exits gracefully when asyncio.CancelledError is raised during lifespan
    teardown (no explicit signal handler needed — we live in the same event
    loop as FastAPI).
    """
    try:
        while True:
            now = datetime.now()
            if _is_race_day(now):
                today = now.strftime("%Y-%m-%d")
                try:
                    n = await _poll_once(today)
                    if n and broadcast is not None:
                        await broadcast.broadcast({
                            "type": "scraper_log",
                            "text": f"[odds_poller] {n} snapshots @ {now.strftime('%H:%M:%S')}",
                            "task": "odds_poller",
                        })
                except Exception as exc:
                    if broadcast is not None:
                        await broadcast.broadcast({
                            "type": "scraper_log",
                            "text": f"[odds_poller] error: {exc}",
                            "task": "odds_poller",
                        })
                await asyncio.sleep(30)
            else:
                await asyncio.sleep(60)
    except asyncio.CancelledError:
        return

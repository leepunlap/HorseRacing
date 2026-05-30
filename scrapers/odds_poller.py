"""In-process pre-race odds poller.

Runs as an asyncio task inside the FastAPI process (NOT a subprocess) — wired
into app.py's lifespan so it dies cleanly when the event loop closes.

Polling cadence: every 30 seconds during a race-day window of T-60→T-0 around
each scheduled race post-time. Outside that window the loop sleeps 60s and
re-checks the calendar.

Data source: HKJC's GraphQL `racing` query at
https://info.cld.hkjc.com/graphql/base/ — the legacy
/racing/info/meeting/Odds/WP/<date>/<course>/<raceNo> URL was retired in May
2026 and silently returns 404 (which is why pre-2026-05-27 odds_snapshots
were empty even though the poller was running).

Storage rule (change-log): we only INSERT a row when the latest stored
snapshot for that (race, horse) has a DIFFERENT win_odds, place_odds, or
pool_total. A horse whose price stays flat for an hour produces ONE row in
the table, not 120 — letting drift queries do `ORDER BY ts` and read the
actual price-move sequence without dedup.

Activation: app.py imports `run_forever`.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "racing.db"


HKJC_GRAPHQL_URL = "https://info.cld.hkjc.com/graphql/base/"
# HKJC validates query strings byte-for-byte against a whitelist of registered
# operations — reformatting the query (changing whitespace, dropping fields)
# yields `Internal server error - WHITELIST_ERROR`. The string below is copied
# verbatim from bet.hkjc.com's main.js bundle (the public SPA's `racing`
# query). DO NOT reformat.
HKJC_RACING_QUERY = """query racing($date: String, $venueCode: String, $oddsTypes: [OddsType], $raceNo: Int) {
          raceMeetings(date: $date, venueCode: $venueCode)
          {
            pmPools(oddsTypes: $oddsTypes, raceNo: $raceNo) {
              id
              status
              sellStatus
              oddsType
              lastUpdateTime
              guarantee
              minTicketCost
              name_en
              name_ch
              leg {
                number
                races
              }
              cWinSelections {
                composite
                name_ch
                name_en
                starters
              }
              oddsNodes {
                combString
                oddsValue
                hotFavourite
                oddsDropValue
                bankerOdds {
                  combString
                  oddsValue
                }
              }
            }
          }
      }"""
# HKJC rejects requests without the bet.hkjc.com Origin (WHITELIST_ERROR).
HKJC_HEADERS = {
    "Content-Type": "application/json",
    "Accept-Encoding": "gzip",
    "Origin": "https://bet.hkjc.com",
    "Referer": "https://bet.hkjc.com/",
    "User-Agent": "Mozilla/5.0",
}


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


def _f(v: Any) -> float | None:
    try:
        f = float(v)
        return f if f > 0 else None
    except Exception:
        return None


async def _fetch_odds(date_str: str, course: str, race_no: int) -> list[dict[str, Any]] | None:
    """Fetch WIN + PLACE odds for one race via HKJC's GraphQL."""
    body = {
        "operationName": "racing",
        "variables": {
            "date": date_str, "venueCode": course,
            "oddsTypes": ["WIN", "PLA"], "raceNo": race_no,
        },
        "query": HKJC_RACING_QUERY,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.post(HKJC_GRAPHQL_URL, headers=HKJC_HEADERS, json=body)
            r.raise_for_status()
            data = r.json()
    except Exception:
        return None

    meetings = (data.get("data") or {}).get("raceMeetings") or []
    if not meetings:
        return None
    pools = meetings[0].get("pmPools") or []
    win_by_horse: dict[int, float | None] = {}
    pla_by_horse: dict[int, float | None] = {}
    for pool in pools:
        kind = pool.get("oddsType")
        nodes = pool.get("oddsNodes") or []
        for n in nodes:
            try:
                horse_no = int(n.get("combString", "0"))
            except (TypeError, ValueError):
                continue
            v = _f(n.get("oddsValue"))
            if kind == "WIN":
                win_by_horse[horse_no] = v
            elif kind == "PLA":
                pla_by_horse[horse_no] = v
    horses = sorted(set(win_by_horse) | set(pla_by_horse))
    if not horses:
        return None
    return [{
        "horse_no": h,
        "brand": None,                      # GraphQL pmPools doesn't carry brand
        "win_odds": win_by_horse.get(h),
        "place_odds": pla_by_horse.get(h),
        # `pool_total` (the size of the WIN pool in HK$) is genuinely
        # NOT available to us. HKJC's GraphQL whitelist rejects any field
        # we add to the racing query (`Your query doesn't match the
        # schema`), and the public results page exposes only per-horse
        # dividends, not pool totals. Reserved-for-paying-customers info.
        # The schema column stays so a future scrape from a different
        # endpoint can populate it.
        "pool_total": None,
    } for h in horses]


def _latest_per_horse(conn: sqlite3.Connection, race_id: int) -> dict[int, tuple]:
    """Return {horse_no: (win_odds, place_odds, pool_total)} from the most
    recent snapshot per horse for this race. Used to skip writes when the
    price hasn't changed."""
    rows = conn.execute(
        """
        SELECT horse_no, win_odds, place_odds, pool_total
        FROM odds_snapshots
        WHERE race_id = ? AND id IN (
            SELECT MAX(id) FROM odds_snapshots WHERE race_id = ? GROUP BY horse_no
        )
        """,
        (race_id, race_id),
    ).fetchall()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def _changed(new: dict, last: tuple | None) -> bool:
    """True if any of (win, place, pool) differs from the last stored row."""
    if last is None:
        return True
    last_win, last_pla, last_pool = last
    return (
        new.get("win_odds") != last_win
        or new.get("place_odds") != last_pla
        or new.get("pool_total") != last_pool
    )


async def _poll_once(date_str: str) -> tuple[int, int]:
    """Poll all known races for `date_str`. Returns (rows_written, rows_skipped).

    A snapshot is written only when the price (win, place, or pool) has moved
    vs the most recent row for that horse. Static prices produce zero rows,
    which keeps `odds_snapshots` to a true change-log size.
    """
    if not DB_PATH.exists():
        return 0, 0
    conn = _conn()
    races = conn.execute(
        "SELECT id, course, race_no FROM races WHERE date = ? ORDER BY course, race_no",
        (date_str,),
    ).fetchall()
    now = datetime.now().isoformat()
    written = skipped = 0
    for race_id, course, race_no in races:
        snapshot = await _fetch_odds(date_str, course, race_no)
        if not snapshot:
            continue
        last = _latest_per_horse(conn, race_id)
        for h in snapshot:
            if not _changed(h, last.get(h["horse_no"])):
                skipped += 1
                continue
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
    return written, skipped


async def run_forever(broadcast=None) -> None:  # broadcast: optional Broadcaster
    """Long-lived loop. Polls every 30s on race days, sleeps 60s otherwise.

    Exits gracefully when asyncio.CancelledError is raised during lifespan
    teardown (no explicit signal handler needed — we live in the same event
    loop as FastAPI).
    """
    import status as _status
    _status.process_up('odds_poller', ptype='loop', activity='idle (no race today)')
    try:
        while True:
            now = datetime.now()
            if _is_race_day(now):
                today = now.strftime("%Y-%m-%d")
                try:
                    n, s = await _poll_once(today)
                    _status.heartbeat('odds_poller',
                                      f'polling — {n} changes / {s} unchanged @ {now:%H:%M:%S}')
                    if (n or s) and broadcast is not None:
                        await broadcast.broadcast({
                            "type": "scraper_log",
                            "text": (f"[odds_poller] {n} changes / {s} unchanged "
                                     f"@ {now.strftime('%H:%M:%S')}"),
                            "task": "odds_poller",
                        })
                except Exception as exc:
                    _status.heartbeat('odds_poller', f'error: {exc}')
                    if broadcast is not None:
                        await broadcast.broadcast({
                            "type": "scraper_log",
                            "text": f"[odds_poller] error: {exc}",
                            "task": "odds_poller",
                        })
                await asyncio.sleep(30)
            else:
                _status.heartbeat('odds_poller', 'idle (no race today)')
                await asyncio.sleep(60)
    except asyncio.CancelledError:
        return
    finally:
        try:
            _status.process_down('odds_poller')
        except Exception:
            pass

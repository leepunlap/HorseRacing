#!/usr/bin/env python3
"""Multi-leg dividend pool scraper (live capture).

HKJC offers several cross-race pool types that aren't on the per-race
results pages:

  DBL   — Double (winners of 2 designated races)
  TBL   — Treble (winners of 3 designated races)
  DT    — Double Trio (top-3 of 2 designated races)
  TT    — Triple Trio (top-3 of 3 designated races)
  SixUP — Six Up (top-3 of 6 designated races)

These dividends are published only on bet.hkjc.com via the GraphQL
`resultMeetings` query — and that query returns ONLY the next upcoming
meeting, regardless of the `date` variable. Once a meeting closes and
the next one goes on sale, the previous meeting's multi-leg pools are
dropped from the live API. There is no archive endpoint we can find on
the public surface (the racing.hkjc.com results pages don't show these
pools; the info.cld.hkjc.com REST endpoints require RBAC auth).

So this scraper is **live-only**. Run it on race-day evenings,
ideally:
  - Within ~60 minutes of the last race finishing,
  - Before the next meeting opens its pools (typically the following
    morning).

The scrapers/odds_poller.py infrastructure pings GraphQL every 30s
for live odds — when it transitions from "meeting in progress" to
"meeting complete", that's the trigger window for this script.

The query text is taken verbatim from bet.hkjc.com's main.js bundle
(WHITELIST-enforced; reformatting yields `WHITELIST_ERROR`).

Usage:
    python3 -m scrapers.scrape_multi_leg_dividends
    # Add --venue ST or --venue HV to filter when both happen on the
    # same day (rare). Otherwise all returned meetings are stored.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapers._base import BaseScraper, log, txn


GRAPHQL_URL = "https://info.cld.hkjc.com/graphql/base/"

# The query string MUST be byte-identical to bet.hkjc.com's main bundle
# (WHITELIST cache keys on the literal text). The fragment is appended
# unchanged because the query references `...racingFoPoolFragment`.
RESULT_MEETINGS_QUERY = """\

  query resultMeetings($date: String, $venueCode: String, $foOddsTypes: [OddsType], $foFilter: [String], $resultOddsType: [OddsType]) {
      raceMeetings(date: $date, venueCode: $venueCode)
      {
        id
        resPools: pmPools(oddsTypes: $resultOddsType) {
          leg {
            number
            races
          }
          status
          oddsType
          name_en
          name_ch
          lastUpdateTime
          dividends (officialOnly: true) {
            winComb
            type
            div
            seq
            status
            guarantee
            partial
            partialUnit
          }
          cWinSelections {
            composite
            name_ch
            name_en
            starters
          }
        }
        foPools(oddsTypes: $foOddsTypes, filters: $foFilter) {
          ...racingFoPoolFragment
        }
      }
  }

fragment racingFoPoolFragment on RacingFoPool {
    instNo
    poolId
    oddsType
    status
    sellStatus
    otherSelNo
    inplayUpTo
    expStartDateTime
    expStopDateTime
    raceStopSellNo
    raceStopSellStatus
    includeRaces
    excludeRaces
    lastUpdateTime
    selections {
      order
      number
      code
      name_en
      name_ch
      scheduleRides
      remainingRides
      points
      lineId
      combId
      combStatus
      openOdds
      prevOdds
      currentOdds
      results {
        raceNo
        points
        point1st
        point2nd
        point3rd
        dhRmk1st
        dhRmk2nd
        dhRmk3rd
        count1st
        count2nd
        count3rd
        count4th
        numerator4th
        denominator4th
      }
    }
    otherSelections {
      order
      code
      name_en
      name_ch
      scheduleRides
      remainingRides
      points
      results {
        raceNo
        points
        point1st
        point2nd
        point3rd
        dhRmk1st
        dhRmk2nd
        dhRmk3rd
        count1st
        count2nd
        count3rd
        count4th
        numerator4th
        denominator4th
      }
    }
  }
"""

# Matches the JS's `Tn` helper output when page === "RESULTS".
_RESULT_ODDS_TYPES = [
    "WIN", "PLA", "QIN", "QPL", "CWA", "CWB", "CWC", "IWN", "FCT",
    "TCE", "TRI", "FF", "QTT", "DBL", "TBL", "DT", "TT", "SixUP",
]
# Pool types we treat as multi-leg (cross-race). Everything else maps
# 1:1 to the per-race `dividends` table that scrape_results already
# populates.
MULTI_LEG_TYPES = {"DBL", "TBL", "DT", "TT", "SixUP"}

HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://bet.hkjc.com",
    "Referer": "https://bet.hkjc.com/",
    "User-Agent": "Mozilla/5.0",
}


def _meeting_to_date_course(meeting_id: str) -> tuple[str | None, str | None]:
    """Parse HKJC's meeting id into (date, course).

    Format observed:
      'MTG_20260531_0001' → ('2026-05-31', None)  # course not encoded
      '20260531S1'        → ('2026-05-31', 'ST')
      '20260531H1'        → ('2026-05-31', 'HV')

    The MTG_-prefixed form is HKJC's internal Meeting ID and carries no
    venue letter; the short form encodes course as S=ST, H=HV. When the
    API returns both for the same day, the short form supplies the venue
    via sibling lookup.
    """
    s = (meeting_id or "").strip()
    # MTG_YYYYMMDD_NNNN — internal id
    if s.startswith("MTG_") and len(s) >= 12 and s[4:12].isdigit():
        ymd = s[4:12]
        date = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
        return (date, None)
    # YYYYMMDD{S|H}N — short form
    if len(s) >= 9 and s[:8].isdigit() and s[8] in ("S", "H"):
        ymd = s[:8]
        date = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
        course = "ST" if s[8] == "S" else "HV"
        return (date, course)
    return (None, None)


class MultiLegDividendsScraper(BaseScraper):
    name = "multi_leg_dividends"

    def run(self, args: list[str]) -> int:
        p = argparse.ArgumentParser(prog="scrape_multi_leg_dividends")
        p.add_argument("--venue", choices=("ST", "HV"),
                       help="restrict to one venue (default: both)")
        p.add_argument("--date", help="explicit date (rarely useful — the API "
                                      "ignores this and returns the next "
                                      "upcoming meeting only)")
        p.add_argument("--dry-run", action="store_true",
                       help="show what would be stored without writing")
        ns = p.parse_args(args)

        body = {
            "operationName": "resultMeetings",
            "variables": {
                "date": ns.date,
                "venueCode": ns.venue,
                "foOddsTypes": ["JKC", "TNC"],
                "foFilter": None,
                "resultOddsType": _RESULT_ODDS_TYPES,
            },
            "query": RESULT_MEETINGS_QUERY,
        }
        try:
            r = httpx.post(GRAPHQL_URL, headers=HEADERS, json=body, timeout=30)
        except Exception as exc:
            log(f"[{self.name}] HTTP error: {exc}")
            return 1
        if r.status_code != 200:
            log(f"[{self.name}] status={r.status_code} body={r.text[:200]}")
            return 1
        try:
            data = r.json()
        except Exception as exc:
            log(f"[{self.name}] JSON decode error: {exc}")
            return 1
        if data.get("errors"):
            log(f"[{self.name}] GraphQL errors: {data['errors']}")
            return 1

        meetings = (data.get("data") or {}).get("raceMeetings") or []
        if not meetings:
            log(f"[{self.name}] no meetings returned")
            return 0

        rows: list[dict] = []
        self.set_total(len(meetings))
        for _i, m in enumerate(meetings, 1):
            self.progress(done=_i, msg=f'{m.get("id") or "meeting"} ({len(rows)} rows)')
            mid = m.get("id") or ""
            date, course_short = _meeting_to_date_course(mid)
            # MTG_-prefixed meetings don't carry a venue letter — match
            # the date against the shorter sibling meeting (which does)
            # to recover the course.
            if not course_short:
                sibling = next(
                    (mm for mm in meetings
                     if (sd := _meeting_to_date_course(mm.get("id") or ""))
                     and sd[0] == date and sd[1]),
                    None,
                )
                if sibling:
                    course_short = _meeting_to_date_course(sibling["id"])[1]
            if not date or not course_short:
                log(f"[{self.name}] could not derive (date, course) from "
                    f"meeting id {mid!r}; skipping")
                continue

            for pool in m.get("resPools") or []:
                otype = pool.get("oddsType")
                if otype not in MULTI_LEG_TYPES:
                    continue
                leg = pool.get("leg") or {}
                leg_races = leg.get("races") or []
                leg_count = int(leg.get("number") or len(leg_races) or 0)
                for d in pool.get("dividends") or []:
                    if not d.get("winComb"):
                        continue
                    rows.append({
                        "date": date,
                        "course": course_short,
                        "pool": otype,
                        "leg_races": json.dumps(leg_races),
                        "leg_count": leg_count,
                        "win_comb": d["winComb"],
                        "dividend": float(d["div"]) if d.get("div") not in (None, "") else None,
                        "seq": int(d.get("seq") or 0),
                        "type": d.get("type"),
                        "guarantee": int(bool(d.get("guarantee"))),
                    })

        if ns.dry_run:
            log(f"[{self.name}] dry-run: would store {len(rows)} rows")
            for r in rows[:8]:
                log(f"  {r}")
            return 0

        if not rows:
            log(f"[{self.name}] no multi-leg dividends to store — likely the "
                f"current meeting hasn't completed yet")
            return 0

        conn = self.db()
        with txn(conn):
            for r in rows:
                self.upsert(
                    "multi_leg_dividends", r,
                    conflict_cols=("date", "course", "pool", "win_comb", "seq"),
                )
        # Summary by pool type
        by_pool: dict[str, int] = {}
        for r in rows:
            by_pool[r["pool"]] = by_pool.get(r["pool"], 0) + 1
        summary = ", ".join(f"{k}={v}" for k, v in sorted(by_pool.items()))
        log(f"[{self.name}] stored {len(rows)} multi-leg dividend rows ({summary})")
        self.checkpoint({"rows": len(rows), "by_pool": by_pool})
        return 0


if __name__ == "__main__":
    sys.exit(MultiLegDividendsScraper.main())

#!/usr/bin/env python3
"""Race-card scraper.

Extends the v1 racecard scraper to capture rail position, prize_money (numeric),
post_time, and race_name into the `races` table. Source: HKJC racecard HTML
header at racing.hkjc.com/racing/information/English/Racing/RaceCard.aspx
?RaceDate=<YYYY/MM/DD>&Racecourse=<ST|HV>.

Usage:
    python3 -m scrapers.scrape_race_card --date 2026-05-26 --course ST
    python3 -m scrapers.scrape_race_card --next     # next 14 days
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Allow `python3 scrapers/scrape_race_card.py` from project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup

from scrapers._base import BaseScraper, log, txn, lookup_race_id


RAIL_RE = re.compile(r"Course:\s*([A-C][\+\-]?\d*)", re.I)
PRIZE_RE = re.compile(r"Prize[^$]*\$([\d,]+)", re.I)
POSTTIME_RE = re.compile(r"\b(\d{1,2}:\d{2})\b", re.I)
RACENAME_RE = re.compile(r"RACE\s*\d+\s*[—\-–]\s*(.+)", re.I)


class RaceCardScraper(BaseScraper):
    name = "race_card"

    def run(self, args: list[str]) -> int:
        p = argparse.ArgumentParser(prog="scrape_race_card")
        p.add_argument("--date", help="YYYY-MM-DD")
        p.add_argument("--course", choices=["ST", "HV"], default="ST")
        p.add_argument("--next", action="store_true", help="next 14 days")
        ns = p.parse_args(args)

        targets: list[tuple[str, str]] = []
        if ns.next:
            today = datetime.now().date()
            for i in range(14):
                d = (today + timedelta(days=i)).isoformat()
                targets.append((d, "ST"))
                targets.append((d, "HV"))
        elif ns.date:
            targets.append((ns.date, ns.course))
        else:
            log("specify --date or --next")
            return 2

        ok = 0
        for date_str, course in targets:
            if self.should_stop():
                break
            try:
                if self._scrape_card(date_str, course):
                    ok += 1
                    self.checkpoint({"last_date": date_str, "last_course": course})
            except Exception as exc:
                log(f"[{self.name}] {date_str}/{course} failed: {exc}")
        log(f"[{self.name}] done: {ok} cards updated")
        return 0

    def _scrape_card(self, date_str: str, course: str) -> bool:
        url_date = date_str.replace("-", "/")
        url = (
            "https://racing.hkjc.com/racing/information/English/Racing/RaceCard.aspx"
            f"?RaceDate={url_date}&Racecourse={course}"
        )
        cache_key = f"{date_str}_{course}"
        body = self.fetch(url, cache_key=cache_key)
        if not body or "RaceCard" not in body:
            return False

        soup = BeautifulSoup(body, "html.parser")
        text = soup.get_text(" ", strip=True)

        rail_m = RAIL_RE.search(text)
        rail = rail_m.group(1) if rail_m else None

        # Per-race details — HKJC racecard typically tabs per race. We extract
        # the visible per-race header rows; if not found we record only the rail
        # at the meeting level.
        per_race = self._extract_per_race_blocks(soup)

        conn = self.db()
        with txn(conn):
            if rail is not None:
                self.upsert(
                    "rail_position",
                    {"date": date_str, "course": course, "rail": rail},
                    conflict_cols=("date", "course"),
                )

            for race_no, info in per_race.items():
                race_id = lookup_race_id(conn, date_str, course, race_no)
                if race_id is None:
                    # Race row may not exist yet; upsert one with just the keys.
                    conn.execute(
                        "INSERT OR IGNORE INTO races (date, course, race_no) VALUES (?,?,?)",
                        (date_str, course, race_no),
                    )
                    race_id = lookup_race_id(conn, date_str, course, race_no)

                fields: list[str] = []
                params: list = []
                for col, val in (
                    ("race_name", info.get("name")),
                    ("prize", info.get("prize_raw")),
                    ("post_time", info.get("post_time")),
                ):
                    if val is not None:
                        fields.append(f"{col} = ?")
                        params.append(val)
                if fields:
                    params.append(race_id)
                    conn.execute(
                        f"UPDATE races SET {', '.join(fields)} WHERE id = ?",
                        params,
                    )

        log(f"[{self.name}] {date_str}/{course}: rail={rail}, races={len(per_race)}")
        return True

    @staticmethod
    def _extract_per_race_blocks(soup: BeautifulSoup) -> dict[int, dict[str, str]]:
        out: dict[int, dict[str, str]] = {}
        for header in soup.find_all(string=RACENAME_RE):
            m = RACENAME_RE.search(str(header))
            if not m:
                continue
            # Find "RACE N" in the same string
            num_m = re.search(r"RACE\s*(\d+)", str(header), re.I)
            if not num_m:
                continue
            race_no = int(num_m.group(1))
            name = m.group(1).strip()
            # Prize amount lives in a nearby element; scan ancestor text.
            anc_text = ""
            ancestor = header.parent
            for _ in range(4):
                if ancestor is None:
                    break
                anc_text = ancestor.get_text(" ", strip=True)
                if "Prize" in anc_text:
                    break
                ancestor = ancestor.parent
            prize_m = PRIZE_RE.search(anc_text)
            entry: dict[str, str] = {"name": name}
            if prize_m:
                entry["prize_raw"] = prize_m.group(1)
            # Post time is published near the race header as "HH:MM" (HKT).
            pt_m = POSTTIME_RE.search(anc_text)
            if pt_m:
                # Validate the HH:MM range; ignore odd matches.
                hh, mm = pt_m.group(1).split(":")
                if 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59:
                    entry["post_time"] = f"{int(hh):02d}:{int(mm):02d}"
            out[race_no] = entry
        return out


if __name__ == "__main__":
    sys.exit(RaceCardScraper.main())

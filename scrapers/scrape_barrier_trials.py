#!/usr/bin/env python3
"""Barrier-trial scraper (HKJC's en-us layout, May 2026+).

The old ASPX endpoint (`BtResults.aspx?BTDate=...`) was deprecated. HKJC
moved the page to the new SPA-style URL:
  https://racing.hkjc.com/en-us/local/information/btresult?Date=YYYY/MM/DD

Each meeting's page hosts one results table per trial batch. The batch
metadata (venue, surface, distance, going, winning time, sectionals) sits in
the sibling text node IMMEDIATELY before the table, in the form:
  "Batch 1 - SHA TIN ALL WEATHER TRACK - 1200m Going: GOOD Time: 1.10.24 Sectional Time: 25.0 22.7 22.5"

Each table row carries: horse (with brand in parens), jockey, trainer, draw,
gear, lbw, running positions (e.g. "2 2 1"), trial time (M.SS.cs), result,
comment.

Usage:
    python3 -m scrapers.scrape_barrier_trials --date 2026-05-12
    python3 -m scrapers.scrape_barrier_trials --recent       # last 30 days
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup

from scrapers._base import BaseScraper, log, txn, lookup_horse_id


BRAND_RE = re.compile(r"\(([A-Z]\d{3,})\)")
META_RE = re.compile(
    r"Batch\s*(\d+).*?-\s*(SHA TIN|HAPPY VALLEY|CONGHUA)\s+"
    r"(ALL WEATHER TRACK|TURF|DIRT)\s*-\s*(\d+)m"
    r"(?:\s*Going[: ]+([A-Z ]+?))?"
    r"(?:\s*Time[: ]+(\d+\.\d+\.\d+))?",
    re.IGNORECASE,
)
VENUE_CODE = {"SHA TIN": "ST", "HAPPY VALLEY": "HV", "CONGHUA": "CH"}
SURFACE_CODE = {"ALL WEATHER TRACK": "AWT", "TURF": "Turf", "DIRT": "Dirt"}


def _parse_trial_time(s: str) -> float | None:
    """HKJC trial time is 'M.SS.cs' e.g. '1.10.24' = 70.24 seconds."""
    if not s:
        return None
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", s.strip())
    if not m:
        try:
            return float(s)
        except (ValueError, TypeError):
            return None
    minutes, secs, cs = m.groups()
    return int(minutes) * 60 + int(secs) + int(cs) / 100.0


def _to_int(s) -> int | None:
    try:
        return int(str(s).strip())
    except (ValueError, TypeError, AttributeError):
        return None


class BarrierTrialsScraper(BaseScraper):
    name = "barrier_trials"

    def run(self, args: list[str]) -> int:
        p = argparse.ArgumentParser(prog="scrape_barrier_trials")
        p.add_argument("--date", help="YYYY-MM-DD")
        p.add_argument("--recent", action="store_true", help="last 30 days")
        ns = p.parse_args(args)

        dates: list[str]
        if ns.recent:
            today = datetime.now().date()
            dates = [(today - timedelta(days=i)).isoformat() for i in range(30)]
        elif ns.date:
            dates = [ns.date]
        else:
            log("specify --date or --recent")
            return 2

        total = 0
        for d in dates:
            if self.should_stop():
                break
            try:
                n = self._scrape_date(d)
                total += n
                if n:
                    self.checkpoint({"last_date": d, "rows": total})
            except Exception as exc:
                log(f"[{self.name}] {d}: {exc}")
        log(f"[{self.name}] done: {total} trial rows")
        return 0

    def _scrape_date(self, date_str: str) -> int:
        url_date = date_str.replace("-", "/")
        url = f"https://racing.hkjc.com/en-us/local/information/btresult?Date={url_date}"
        body = self.fetch(url, cache_key=date_str)
        if not body or "Barrier Trial" not in body:
            return 0

        soup = BeautifulSoup(body, "html.parser")
        conn = self.db()
        rows_inserted = 0

        # Each trial = one results table. The batch metadata is in the
        # text node (or div) immediately preceding the table.
        for table in soup.find_all("table"):
            txt = table.get_text(" | ", strip=True)
            if "Horse" not in txt or "Trainer" not in txt or "Time" not in txt:
                continue

            # Find the meta-string immediately before this table
            meta_str = ""
            for sib in table.previous_siblings:
                if hasattr(sib, "get_text"):
                    t = sib.get_text(" ", strip=True).replace("\xa0", " ")
                    if t:
                        meta_str = t
                        break
                elif isinstance(sib, str):
                    s = sib.strip().replace("\xa0", " ")
                    if s:
                        meta_str = s
                        break
            mm = META_RE.search(meta_str)
            if mm:
                _batch, venue_long, surface_long, distance, going, _win_time = mm.groups()
                venue = VENUE_CODE.get(venue_long.upper(), venue_long[:2].upper())
                surface = SURFACE_CODE.get(surface_long.upper(), surface_long)
                distance = int(distance)
                going = (going or "").strip().title() or None
            else:
                # No meta = skip; we won't know venue/distance
                continue

            for tr in table.find_all("tr")[1:]:
                tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                if len(tds) < 8:
                    continue
                bm = BRAND_RE.search(tds[0])
                if not bm:
                    continue
                brand = bm.group(1)
                jockey  = tds[1] or None
                trainer = tds[2] or None
                draw    = _to_int(tds[3])
                gear    = tds[4] or None
                # tds[5] = LBW (running margin); we don't store it for trials
                running_positions = tds[6] or None    # e.g. "2 2 1"
                time_sec = _parse_trial_time(tds[7])
                # Trial finish position = last token of running_positions
                position = None
                if running_positions:
                    parts = running_positions.split()
                    if parts:
                        position = _to_int(parts[-1])

                row = {
                    "horse_id": lookup_horse_id(conn, brand),
                    "brand": brand,
                    "date": date_str,
                    "venue": venue,
                    "surface": surface,
                    "distance": distance,
                    "going": going,
                    "position": position,
                    "time_sec": time_sec,
                    "jockey": jockey,
                    "trainer": trainer,
                    "notes": (tds[9][:300] if len(tds) > 9 else None),
                }
                with txn(conn):
                    self.upsert("barrier_trials", row,
                                conflict_cols=("brand", "date", "venue", "distance"))
                rows_inserted += 1

        log(f"[{self.name}] {date_str}: {rows_inserted} rows")
        return rows_inserted


if __name__ == "__main__":
    sys.exit(BarrierTrialsScraper.main())

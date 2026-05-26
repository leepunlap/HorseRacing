#!/usr/bin/env python3
"""Morning-gallop trackwork scraper.

HKJC publishes daily trackwork at racing.hkjc.com/.../LocalTrackwork. Each
horse may have multiple gallops in a day. We capture the brand, date, surface,
distance, time so features can derive proxy GPS metrics (avg speed,
workload) for Cat 16 features.

Usage:
    python3 -m scrapers.scrape_trackwork --date 2026-05-26
    python3 -m scrapers.scrape_trackwork --recent
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


class TrackworkScraper(BaseScraper):
    name = "trackwork"

    def run(self, args: list[str]) -> int:
        p = argparse.ArgumentParser(prog="scrape_trackwork")
        p.add_argument("--date", help="YYYY-MM-DD")
        p.add_argument("--recent", action="store_true", help="last 14 days")
        ns = p.parse_args(args)

        dates: list[str]
        if ns.recent:
            today = datetime.now().date()
            dates = [(today - timedelta(days=i)).isoformat() for i in range(14)]
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
        log(f"[{self.name}] done: {total} trackwork rows")
        return 0

    def _scrape_date(self, date_str: str) -> int:
        # HKJC's local trackwork page uses date as path param.
        url_date = date_str.replace("-", "")
        url = (
            "https://racing.hkjc.com/racing/information/english/Trackwork/"
            f"TrackworkDetail.aspx?Date={url_date}"
        )
        body = self.fetch(url, cache_key=date_str)
        if not body:
            return 0

        soup = BeautifulSoup(body, "html.parser")
        rows_inserted = 0
        conn = self.db()

        for table in soup.find_all("table"):
            head_text = table.get_text(" ", strip=True)
            if "Horse" not in head_text or "Distance" not in head_text:
                continue
            for tr in table.find_all("tr")[1:]:
                tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                if len(tds) < 4:
                    continue
                row = self._parse_row(tds)
                if not row:
                    continue
                row.update({
                    "date": date_str,
                    "horse_id": lookup_horse_id(conn, row["brand"]),
                })
                with txn(conn):
                    self.upsert("trackwork", row,
                                conflict_cols=("brand", "date", "venue", "distance", "time_sec"))
                rows_inserted += 1

        log(f"[{self.name}] {date_str}: {rows_inserted} rows")
        return rows_inserted

    @staticmethod
    def _parse_row(tds: list[str]) -> dict | None:
        joined = " ".join(tds)
        brand_m = re.search(r"\(([A-Z]\d+)\)", joined)
        if not brand_m:
            return None
        brand = brand_m.group(1)
        dist_m = re.search(r"(\d{3,4})\s*m", joined)
        distance = int(dist_m.group(1)) if dist_m else None
        time_m = re.search(r"(\d+\.\d+)\s*sec", joined)
        time_sec = float(time_m.group(1)) if time_m else None
        venue = "ST" if "Sha Tin" in joined else "HV" if "Happy Valley" in joined else None
        surface = "AWT" if "AWT" in joined or "All Weather" in joined else "Turf"
        return {
            "brand": brand,
            "venue": venue,
            "surface": surface,
            "distance": distance,
            "time_sec": time_sec,
            "notes": joined[:200],
        }


if __name__ == "__main__":
    sys.exit(TrackworkScraper.main())

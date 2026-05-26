#!/usr/bin/env python3
"""Barrier-trial scraper.

HKJC publishes weekly barrier trials at
https://racing.hkjc.com/racing/information/English/Horse/BtResults.aspx.
Each meeting lists ~6 trials with horse, jockey, position, time, sectional.
We populate the `barrier_trials` table keyed on (brand, date, venue, distance).

Usage:
    python3 -m scrapers.scrape_barrier_trials --date 2026-05-20
    python3 -m scrapers.scrape_barrier_trials --recent  # last 30 days
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
        url = (
            "https://racing.hkjc.com/racing/information/English/Horse/BtResults.aspx"
            f"?BTDate={url_date}"
        )
        body = self.fetch(url, cache_key=date_str)
        if not body or "BarrierTrial" not in body and "Barrier Trial" not in body:
            return 0

        soup = BeautifulSoup(body, "html.parser")
        rows_inserted = 0
        conn = self.db()

        # HKJC BT page: each trial is a table with header row + horse rows.
        for table in soup.find_all("table"):
            text = table.get_text(" ", strip=True)
            if "Pos." not in text and "Plc." not in text:
                continue
            venue, distance, going, surface = self._parse_trial_meta(table)
            if distance is None:
                continue
            for tr in table.find_all("tr")[1:]:
                tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                if len(tds) < 5:
                    continue
                row = self._parse_trial_row(tds)
                if not row:
                    continue
                row.update({
                    "date": date_str,
                    "venue": venue,
                    "distance": distance,
                    "going": going,
                    "surface": surface,
                    "horse_id": lookup_horse_id(conn, row["brand"]) if row.get("brand") else None,
                })
                with txn(conn):
                    self.upsert("barrier_trials", row,
                                conflict_cols=("brand", "date", "venue", "distance"))
                rows_inserted += 1

        log(f"[{self.name}] {date_str}: {rows_inserted} rows")
        return rows_inserted

    @staticmethod
    def _parse_trial_meta(table) -> tuple[str | None, int | None, str | None, str | None]:
        caption = table.find_previous(string=re.compile(r"\d+m\b|Turf|All Weather", re.I))
        meta = str(caption) if caption else ""
        dm = re.search(r"(\d+)m", meta)
        distance = int(dm.group(1)) if dm else None
        surface = "AWT" if "All Weather" in meta or "AWT" in meta else "Turf"
        going_m = re.search(r"Going\s*[:\s]+([A-Za-z ]+)", meta)
        going = going_m.group(1).strip() if going_m else None
        venue = "ST" if "Sha Tin" in meta else "HV" if "Happy Valley" in meta else None
        return venue, distance, going, surface

    @staticmethod
    def _parse_trial_row(tds: list[str]) -> dict | None:
        try:
            pos = int(re.sub(r"\D", "", tds[0]) or 0) or None
        except Exception:
            pos = None
        brand_m = re.search(r"\(([A-Z]\d+)\)", tds[1])
        brand = brand_m.group(1) if brand_m else None
        if not brand:
            return None
        time_m = re.search(r"(\d+:\d+\.\d+|\d+\.\d+)", " ".join(tds))
        time_sec: float | None = None
        if time_m:
            t = time_m.group(1)
            if ":" in t:
                mins, secs = t.split(":")
                time_sec = int(mins) * 60 + float(secs)
            else:
                time_sec = float(t)
        return {
            "brand": brand,
            "position": pos,
            "time_sec": time_sec,
            "jockey": tds[2] if len(tds) > 2 else None,
            "trainer": tds[3] if len(tds) > 3 else None,
        }


if __name__ == "__main__":
    sys.exit(BarrierTrialsScraper.main())

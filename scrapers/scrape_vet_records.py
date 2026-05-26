#!/usr/bin/env python3
"""HKJC Veterinary Records (OVE database).

Captures: lameness flags, bleeders, gear restrictions, suspension and
off-vet status. Feeds Cat 9 features (H100s).

Usage:
    python3 -m scrapers.scrape_vet_records
    python3 -m scrapers.scrape_vet_records --recent
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


class VetRecordsScraper(BaseScraper):
    name = "vet_records"

    def run(self, args: list[str]) -> int:
        p = argparse.ArgumentParser(prog="scrape_vet_records")
        p.add_argument("--recent", action="store_true", help="last 90 days")
        ns = p.parse_args(args)

        url = (
            "https://racing.hkjc.com/racing/information/english/"
            "veterinaryrecords/ovedatabase.aspx"
        )
        body = self.fetch(url, cache_key=datetime.now().strftime("%Y-%m-%d"))
        if not body:
            return 1
        soup = BeautifulSoup(body, "html.parser")
        cutoff = (datetime.now().date() - timedelta(days=90)) if ns.recent else None

        rows = 0
        conn = self.db()
        for table in soup.find_all("table"):
            if "Date" not in table.get_text() or "Horse" not in table.get_text():
                continue
            for tr in table.find_all("tr")[1:]:
                tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                if len(tds) < 3:
                    continue
                date_m = re.search(r"(\d{2}/\d{2}/\d{4})", tds[0])
                if not date_m:
                    continue
                d = datetime.strptime(date_m.group(1), "%d/%m/%Y").date().isoformat()
                if cutoff and datetime.fromisoformat(d).date() < cutoff:
                    continue
                brand_m = re.search(r"\(([A-Z]\d+)\)", " ".join(tds))
                if not brand_m:
                    continue
                row = {
                    "date": d,
                    "brand": brand_m.group(1),
                    "type": tds[2][:120] if len(tds) > 2 else "ove",
                    "notes": " | ".join(tds)[:400],
                    "horse_id": lookup_horse_id(conn, brand_m.group(1)),
                }
                with txn(conn):
                    self.upsert("vet_records", row, conflict_cols=("brand", "date", "type"))
                rows += 1
        self.checkpoint({"rows": rows})
        log(f"[{self.name}] done: {rows} rows")
        return 0


if __name__ == "__main__":
    sys.exit(VetRecordsScraper.main())

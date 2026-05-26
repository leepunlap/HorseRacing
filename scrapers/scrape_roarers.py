#!/usr/bin/env python3
"""HKJC Roarer database — horses that have had wind / soft-palate / laryngeal
surgery. Single binary flag fed into Cat 9.

Usage: python3 -m scrapers.scrape_roarers
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup

from scrapers._base import BaseScraper, log, txn, lookup_horse_id


class RoarersScraper(BaseScraper):
    name = "roarers"

    def run(self, args: list[str]) -> int:  # noqa: ARG002
        url = (
            "https://racing.hkjc.com/racing/information/english/"
            "VeterinaryRecords/OVERoar.aspx"
        )
        body = self.fetch(url, cache_key=datetime.now().strftime("%Y-%m-%d"))
        if not body:
            return 1
        soup = BeautifulSoup(body, "html.parser")
        conn = self.db()
        rows = 0
        for table in soup.find_all("table"):
            for tr in table.find_all("tr"):
                txt = tr.get_text(" ", strip=True)
                brand_m = re.search(r"\(([A-Z]\d+)\)", txt)
                date_m = re.search(r"(\d{2}/\d{2}/\d{4})", txt)
                if not brand_m or not date_m:
                    continue
                brand = brand_m.group(1)
                d = datetime.strptime(date_m.group(1), "%d/%m/%Y").date().isoformat()
                row = {
                    "date": d,
                    "brand": brand,
                    "type": "roarer-surgery",
                    "notes": txt[:300],
                    "horse_id": lookup_horse_id(conn, brand),
                }
                with txn(conn):
                    self.upsert("vet_records", row, conflict_cols=("brand", "date", "type"))
                rows += 1
        self.checkpoint({"rows": rows})
        log(f"[{self.name}] done: {rows} roarer rows")
        return 0


if __name__ == "__main__":
    sys.exit(RoarersScraper.main())

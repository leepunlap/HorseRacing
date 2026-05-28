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

    # The OVE database page has columns: Brand No. | Horse Name | Date |
    # Details | Passed On. When a horse has >1 visit the second/third
    # rows leave Brand and Name empty (HTML rowspan), so we carry the
    # last-seen brand forward.
    BRAND_RE = re.compile(r"^[A-Z]\d{3}$")
    DATE_RE = re.compile(r"\d{2}/\d{2}/\d{4}")

    def run(self, args: list[str]) -> int:
        p = argparse.ArgumentParser(prog="scrape_vet_records")
        p.add_argument("--recent", action="store_true", help="last 90 days")
        p.add_argument("--force", action="store_true", help="bypass HTML cache")
        ns = p.parse_args(args)

        # HKJC redirects the legacy URL to /en-us/local/...; hit that
        # directly to avoid the cached 30x being mistaken for content.
        url = "https://racing.hkjc.com/en-us/local/information/ovedatabase"
        body = self.fetch(url, cache_key=datetime.now().strftime("%Y-%m-%d"),
                          force_refresh=ns.force)
        if not body:
            return 1
        soup = BeautifulSoup(body, "html.parser")
        cutoff = (datetime.now().date() - timedelta(days=90)) if ns.recent else None

        rows_written = 0
        conn = self.db()
        target_table = None
        for table in soup.find_all("table"):
            txt = table.get_text(" ", strip=True)
            if "Brand No." in txt and "Horse Name" in txt and "Details" in txt:
                target_table = table
                break
        if target_table is None:
            log(f"[{self.name}] OVE table not found on page")
            return 1

        current_brand: str | None = None
        with txn(conn):
            for tr in target_table.find_all("tr"):
                tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                # Skip the title row + header row.
                if not tds or len(tds) < 4:
                    continue
                # Header row has 'Brand No.' as first cell — skip.
                if tds[0] == "Brand No.":
                    continue
                # Carry brand forward across rowspans.
                if self.BRAND_RE.match(tds[0]):
                    current_brand = tds[0]
                if not current_brand:
                    continue
                # Date is column index 2 (after Brand, Name).
                date_m = self.DATE_RE.search(tds[2] if len(tds) > 2 else "")
                if not date_m:
                    continue
                d = datetime.strptime(date_m.group(0), "%d/%m/%Y").date().isoformat()
                if cutoff and datetime.fromisoformat(d).date() < cutoff:
                    continue
                details = tds[3] if len(tds) > 3 else ""
                passed_on_raw = tds[4] if len(tds) > 4 else ""
                passed_on = None
                pm = self.DATE_RE.search(passed_on_raw)
                if pm:
                    passed_on = datetime.strptime(pm.group(0), "%d/%m/%Y").date().isoformat()
                row = {
                    "horse_id": lookup_horse_id(conn, current_brand),
                    "brand": current_brand,
                    "date": d,
                    # `type` is the conflict key, so keep it short + stable.
                    # Pick the first 1-2 words from Details ('Lameness.',
                    # 'Inappetence.', 'Bled.') as a coarse type bucket.
                    "type": (details.split(".")[0][:60] or "ove").strip(),
                    "notes": details[:400] or None,
                    "cleared_date": passed_on,
                }
                self.upsert("vet_records", row,
                            conflict_cols=("brand", "date", "type"))
                rows_written += 1
        self.checkpoint({"rows": rows_written})
        log(f"[{self.name}] done: {rows_written} rows")
        return 0


if __name__ == "__main__":
    sys.exit(VetRecordsScraper.main())

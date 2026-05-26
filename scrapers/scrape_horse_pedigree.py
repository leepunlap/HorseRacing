#!/usr/bin/env python3
"""Horse pedigree scraper.

For each brand seen in `results` or `race_history` that lacks a `horse_pedigree`
row, fetch HKJC's horse profile page and extract sire, dam, dam_sire,
import_date, country_of_origin. Dosage Index lookup (BloodHorse / Pedigree
Online) is intentionally deferred — pedigree alone unlocks Cat 1 features
H001-H014 except dosage.

Usage:
    python3 -m scrapers.scrape_horse_pedigree --limit 100
    python3 -m scrapers.scrape_horse_pedigree --brand HK_2024_A123
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup

from scrapers._base import BaseScraper, log, txn, lookup_horse_id


SIRE_RE = re.compile(r"Sire\s*[:\s]+([A-Za-z0-9 '\-\.]+)", re.I)
DAM_RE = re.compile(r"Dam\s*[:\s]+([A-Za-z0-9 '\-\.]+)", re.I)
DAMSIRE_RE = re.compile(r"Dam'?s\s*Sire\s*[:\s]+([A-Za-z0-9 '\-\.]+)", re.I)
IMPORT_RE = re.compile(r"Import[ed]*\s*(?:Date|Type)?\s*[:\s]*([A-Za-z0-9 /\-]+)", re.I)
COUNTRY_RE = re.compile(r"Country\s*of\s*Origin\s*[:\s]+([A-Z]{2,4})", re.I)


class HorsePedigreeScraper(BaseScraper):
    name = "horse_pedigree"

    def run(self, args: list[str]) -> int:
        p = argparse.ArgumentParser(prog="scrape_horse_pedigree")
        p.add_argument("--limit", type=int, default=50, help="max horses per run")
        p.add_argument("--brand", help="single brand override")
        ns = p.parse_args(args)

        conn = self.db()
        if ns.brand:
            brands = [ns.brand]
        else:
            rows = conn.execute(
                """
                SELECT DISTINCT h.brand
                FROM horses h
                LEFT JOIN horse_pedigree p ON p.brand = h.brand
                WHERE p.brand IS NULL
                ORDER BY h.brand
                LIMIT ?
                """,
                (ns.limit,),
            ).fetchall()
            brands = [r[0] for r in rows]

        log(f"[{self.name}] fetching pedigree for {len(brands)} horses")
        done = 0
        for brand in brands:
            if self.should_stop():
                break
            try:
                if self._scrape_one(brand):
                    done += 1
                    if done % 10 == 0:
                        self.checkpoint({"last_brand": brand, "done": done})
            except Exception as exc:
                log(f"[{self.name}] {brand}: {exc}")
        self.checkpoint({"done": done})
        log(f"[{self.name}] done: {done} pedigree records")
        return 0

    def _scrape_one(self, brand: str) -> bool:
        url = (
            "https://racing.hkjc.com/racing/information/English/Horse/Horse.aspx"
            f"?HorseNo={brand}"
        )
        body = self.fetch(url, cache_key=brand)
        if not body:
            return False
        soup = BeautifulSoup(body, "html.parser")
        text = soup.get_text(" ", strip=True)

        sire = self._first(SIRE_RE, text)
        dam = self._first(DAM_RE, text)
        dam_sire = self._first(DAMSIRE_RE, text)
        origin = self._first(COUNTRY_RE, text)

        conn = self.db()
        horse_id = lookup_horse_id(conn, brand)
        row = {
            "horse_id": horse_id,
            "brand": brand,
            "sire": sire,
            "dam": dam,
            "dam_sire": dam_sire,
            "origin_country": origin,
        }
        with txn(conn):
            self.upsert("horse_pedigree", row, conflict_cols=("brand",))
        log(f"[{self.name}] {brand}: sire={sire} dam={dam} origin={origin}")
        return True

    @staticmethod
    def _first(pat: re.Pattern, text: str) -> str | None:
        m = pat.search(text)
        return m.group(1).strip() if m else None


if __name__ == "__main__":
    sys.exit(HorsePedigreeScraper.main())

"""Backfill the `dividends` table for race dates where results exist but
dividends don't.

Why this exists: the main `scrape_results` scraper bundles dividend parsing
inside the per-race result import — but it short-circuits if results for
that race already exist (to avoid re-deleting and re-inserting result rows).
That meant historical races scraped before the dividends table was added
have no dividend rows. Exotic bet strategies (QIN/QPL/EXA/TRI/TRIO/F4/QTT)
need those rows for proper settlement.

This script:
  1. Finds every race date that has at least one result but zero dividend
     rows (or every date in --since/--until).
  2. For each (date, course, race_no), refetches the LocalResults HTML page
     via BaseScraper.fetch (which uses the same disk cache as the main
     scraper).
  3. Parses ONLY the dividends section using the same _parse_dividends_table
     method that ships in scrape_results.
  4. UPSERTs rows into `dividends` (UNIQUE on date+course+race+pool+combo,
     so re-running is idempotent).

Usage:
    python3 -m scrapers.scrape_dividends_backfill --missing-only
    python3 -m scrapers.scrape_dividends_backfill --since 2025-01-01 --until 2026-05-24
    python3 -m scrapers.scrape_dividends_backfill --date 2026-04-29
"""

from __future__ import annotations
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup

from scrapers._base import BaseScraper, log, txn
from scrapers.scrape_results import ResultsScraper, POOL_MAP


class DividendsBackfillScraper(BaseScraper):
    name = "dividends_backfill"

    def run(self, args: list[str]) -> int:
        p = argparse.ArgumentParser(prog="scrape_dividends_backfill")
        p.add_argument("--date", help="YYYY-MM-DD")
        p.add_argument("--since", help="YYYY-MM-DD start (inclusive)")
        p.add_argument("--until", help="YYYY-MM-DD end (inclusive)")
        p.add_argument("--missing-only", action="store_true",
                       help="auto-pick every date with results but no dividends")
        p.add_argument("--course", choices=["ST", "HV", "CH"],
                       help="restrict to one course")
        ns = p.parse_args(args)

        conn = self.db()
        if ns.missing_only:
            dates = [r[0] for r in conn.execute(
                """
                SELECT DISTINCT ra.date FROM races ra
                JOIN results r ON r.race_id = ra.id
                WHERE r.position IS NOT NULL
                  AND ra.date NOT IN (SELECT DISTINCT date FROM dividends)
                ORDER BY ra.date DESC
                """
            ).fetchall()]
        elif ns.since and ns.until:
            from datetime import datetime, timedelta
            d0 = datetime.fromisoformat(ns.since).date()
            d1 = datetime.fromisoformat(ns.until).date()
            cur = d0
            dates = []
            while cur <= d1:
                dates.append(cur.isoformat())
                cur += timedelta(days=1)
        elif ns.date:
            dates = [ns.date]
        else:
            log("specify --missing-only, --date, or --since/--until")
            return 2

        log(f"[{self.name}] {len(dates)} dates to process")
        courses = [ns.course] if ns.course else ["ST", "HV", "CH"]
        total_rows = 0
        # Borrow the parser from ResultsScraper
        results_scraper = ResultsScraper()

        for i, date_str in enumerate(dates, start=1):
            if self.should_stop():
                break
            for course in courses:
                # Discover races for this (date, course) from the races table.
                race_rows = conn.execute(
                    "SELECT id, race_no FROM races WHERE date = ? AND course = ? "
                    "ORDER BY race_no",
                    (date_str, course),
                ).fetchall()
                if not race_rows:
                    continue
                for race_id, race_no in race_rows:
                    try:
                        added = self._fetch_and_parse(
                            results_scraper, date_str, course, race_no, race_id,
                        )
                        total_rows += added
                    except Exception as exc:
                        log(f"[{self.name}] {date_str}/{course}/R{race_no}: {exc}")
            if i % 10 == 0:
                log(f"[{self.name}] {i}/{len(dates)} dates done — {total_rows} rows so far")
        log(f"[{self.name}] done: {total_rows} dividend rows across {len(dates)} dates")
        return 0

    def _fetch_and_parse(self, results_scraper: ResultsScraper,
                        date_str: str, course: str, race_no: int, race_id: int) -> int:
        url_date = date_str.replace("-", "/")
        url = (
            "https://racing.hkjc.com/racing/information/English/Racing/LocalResults.aspx"
            f"?RaceDate={url_date}&Racecourse={course}&RaceNo={race_no}"
        )
        cache_key = f"{date_str}_{course}_R{race_no}"
        try:
            body = self.fetch(url, cache_key=cache_key)
        except RuntimeError:
            return 0
        if not body:
            return 0
        soup = BeautifulSoup(body, "html.parser")
        page_text = soup.get_text(" ", strip=True)
        if "Race Result" not in page_text and "Pla." not in page_text:
            return 0

        # We need the saddle-number → brand map for dividend rows that store
        # saddle numbers. Reuse the helper on ResultsScraper.
        horse_no_to_brand = results_scraper._horse_no_brand_map(soup)
        dividend_rows = results_scraper._parse_dividends_table(
            soup, date_str, course, race_no, horse_no_to_brand,
        )
        if not dividend_rows:
            return 0
        conn = self.db()
        with txn(conn):
            for row in dividend_rows:
                self.upsert(
                    "dividends", row,
                    conflict_cols=("date", "course", "race_no", "pool", "combination"),
                )
        return len(dividend_rows)


if __name__ == "__main__":
    sys.exit(DividendsBackfillScraper.main())

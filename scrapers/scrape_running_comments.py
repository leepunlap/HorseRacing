#!/usr/bin/env python3
"""HKJC "Comments on Running" scraper.

Fetches the per-horse running narrative HKJC publishes after each race
(e.g. "Sat midfield 2 wide, closed off strongly to score") in both English
and Traditional Chinese, and stores them in `running_comments` keyed by
(race_id, brand, lang). Consumed by betting.eval_reason to feed
authoritative race-incident context to the commentary generator.

Source URLs:
  https://racing.hkjc.com/racing/information/english/Racing/corunning.aspx
  https://racing.hkjc.com/racing/information/Chinese/Racing/corunning.aspx
    ?RaceDate=YYYY/MM/DD&Racecourse=ST|HV&RaceNo=N

Usage:
  python3 -m scrapers.scrape_running_comments --date 2026-05-25
  python3 -m scrapers.scrape_running_comments --since 2025-09-01 --until 2026-05-26
  python3 -m scrapers.scrape_running_comments --recent
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup

from scrapers._base import BaseScraper, log, txn

BRAND_RE = re.compile(r"\(([A-Z]\d{3})\)")
URL_BASE = "https://racing.hkjc.com/racing/information"
URL_PATHS = {"en": "english", "zh": "Chinese"}
# HKJC silently redirects corunning.aspx to the most-recent meeting that has
# comments published if the requested date+course has no comments yet. The
# selected-date dropdown is the reliable indicator of which meeting the page
# is actually showing — match it back to what we asked for.
SELECTED_DATE_RE = re.compile(r"selected[^>]*>(\d{2})/(\d{2})/(\d{4})")
SELECTED_COURSE_RE = re.compile(r'value="(ST|HV)"\s+selected')
SELECTED_RACENO_RE = re.compile(r'value="(\d+)"\s+selected[^>]*>\s*\d+\s*<')


def _to_int(s) -> int | None:
    try:
        return int(str(s).strip())
    except (ValueError, TypeError, AttributeError):
        return None


class RunningCommentsScraper(BaseScraper):
    name = "running_comments"

    def run(self, args: list[str]) -> int:
        p = argparse.ArgumentParser(prog="scrape_running_comments")
        p.add_argument("--date", help="YYYY-MM-DD (one meeting)")
        p.add_argument("--since", help="YYYY-MM-DD start (inclusive)")
        p.add_argument("--until", help="YYYY-MM-DD end (inclusive)")
        p.add_argument("--course", choices=["ST", "HV"], help="restrict to one course")
        p.add_argument("--recent", action="store_true", help="last 30 days")
        p.add_argument("--force-refresh", action="store_true",
                       help="re-parse even if comments already exist")
        ns = p.parse_args(args)

        if ns.recent:
            today = datetime.now().date()
            dates = [(today - timedelta(days=i)).isoformat() for i in range(30)]
        elif ns.since and ns.until:
            d0 = datetime.fromisoformat(ns.since).date()
            d1 = datetime.fromisoformat(ns.until).date()
            cur, dates = d0, []
            while cur <= d1:
                dates.append(cur.isoformat())
                cur += timedelta(days=1)
        elif ns.date:
            dates = [ns.date]
        else:
            log("specify --date, --since/--until, or --recent")
            return 2

        courses = [ns.course] if ns.course else ["ST", "HV"]
        total = 0
        self.set_total(len(dates) * len(courses))
        seen = 0
        for d in dates:
            if self.should_stop():
                break
            for course in courses:
                try:
                    total += self._scrape_meeting(d, course, ns.force_refresh)
                except Exception as exc:
                    log(f"[{self.name}] {d}/{course}: {exc}")
                seen += 1
                self.progress(done=seen, msg=f'{d}/{course} ({total} rows)')
        log(f"[{self.name}] done: {total} comment rows")
        return 0

    def _scrape_meeting(self, date_str: str, course: str, force: bool) -> int:
        self._force = force
        conn = self.db()
        races = conn.execute(
            "SELECT id, race_no FROM races WHERE date = ? AND course = ? ORDER BY race_no",
            (date_str, course),
        ).fetchall()
        if not races:
            return 0
        added = 0
        for race_id, race_no in races:
            if self.should_stop():
                break
            for lang in ("en", "zh"):
                if not force:
                    have = conn.execute(
                        "SELECT 1 FROM running_comments "
                        "WHERE race_id = ? AND lang = ? LIMIT 1",
                        (race_id, lang),
                    ).fetchone()
                    if have:
                        continue
                try:
                    added += self._scrape_race_lang(date_str, course, race_no, race_id, lang)
                except Exception as exc:
                    log(f"[{self.name}] {date_str}/{course}/R{race_no} ({lang}): {exc}")
        return added

    def _scrape_race_lang(self, date_str: str, course: str, race_no: int,
                          race_id: int, lang: str) -> int:
        url_date = date_str.replace("-", "/")
        url = (
            f"{URL_BASE}/{URL_PATHS[lang]}/Racing/corunning.aspx"
            f"?RaceDate={url_date}&Racecourse={course}&RaceNo={race_no}"
        )
        cache_key = f"{date_str}_{course}_R{race_no}_{lang}"
        try:
            body = self.fetch(url, cache_key=cache_key,
                              force_refresh=getattr(self, "_force", False))
        except RuntimeError:
            return 0
        if not body:
            return 0

        # Verify the page is actually for the meeting we requested. HKJC
        # silently redirects to the most recent published meeting when no
        # comments exist for our target — storing those would corrupt the DB.
        m = SELECTED_DATE_RE.search(body)
        if m:
            dd, mm, yyyy = m.groups()
            page_date = f"{yyyy}-{mm}-{dd}"
            if page_date != date_str:
                log(f"[{self.name}] {date_str}/{course}/R{race_no} ({lang}): "
                    f"no comments published yet (page shows {page_date}), skipping")
                return 0
        m = SELECTED_COURSE_RE.search(body)
        if m and m.group(1) != course:
            return 0
        m = SELECTED_RACENO_RE.search(body)
        if m and int(m.group(1)) != race_no:
            return 0

        soup = BeautifulSoup(body, "html.parser")
        rows = self._parse_table(soup)
        if not rows:
            return 0

        conn = self.db()
        n = 0
        with txn(conn):
            for placing, brand, gear, comment in rows:
                self.upsert(
                    "running_comments",
                    {
                        "race_id": race_id, "brand": brand, "lang": lang,
                        "placing": placing, "gear": gear, "comment": comment,
                    },
                    conflict_cols=("race_id", "brand", "lang"),
                )
                n += 1
        log(f"[{self.name}] {date_str}/{course}/R{race_no} ({lang}): {n} comments")
        return n

    @staticmethod
    def _parse_table(soup: BeautifulSoup) -> list[tuple[int | None, str, str, str]]:
        """Return list of (placing, brand, gear, comment) for each horse."""
        out: list[tuple[int | None, str, str, str]] = []
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue
            header = [c.get_text(" ", strip=True).lower()
                      for c in rows[0].find_all(["th", "td"])]
            # English page: "comment"; Chinese page: "走勢評述"
            if not any("comment" in h or "走勢" in h or "走势" in h for h in header):
                continue
            for r in rows[1:]:
                cells = [c.get_text(" ", strip=True) for c in r.find_all(["td", "th"])]
                if len(cells) < 6:
                    continue
                placing = _to_int(cells[0])
                name_cell = cells[2]
                m = BRAND_RE.search(name_cell)
                if not m:
                    continue
                brand = m.group(1)
                gear = cells[4] if cells[4] not in ("--", "") else ""
                comment = cells[5].strip()
                if not comment:
                    continue
                out.append((placing, brand, gear, comment))
            break  # found the table, stop scanning
        return out


def main() -> int:
    return RunningCommentsScraper.main()


if __name__ == "__main__":
    sys.exit(main())

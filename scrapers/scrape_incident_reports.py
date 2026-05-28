#!/usr/bin/env python3
"""HKJC Racing Incident Report scraper.

Each meeting has a single multi-race report at:
  https://racing.hkjc.com/en-us/local/information/racereportfull
    ?racedate=DD/MM/YYYY&racecourse=ST|HV

The report has one "incident" table per race in the meeting. Columns:
  Pla. | Horse No | Colour | Horse (BRAND) | Dr. | Jockey | Incident

Incidents include stewards' notes ("Slow to begin", "Bumped shortly
after the start", "Sent for sampling post-race", "Near the 550 Metres
was steadied when momentarily crowded by HEY BROS"), trainer / jockey
remarks, veterinary findings, and disciplinary actions. This is the
authoritative post-race fact source — strictly richer than corunning
narrative because it includes WHY things happened, not just WHAT.

Writes to a new `incident_reports` table keyed by (race_id, brand).

Usage:
  python3 -m scrapers.scrape_incident_reports --date 2026-05-27
  python3 -m scrapers.scrape_incident_reports --since 2018-01-01 --until 2026-05-27
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
from betting.incident_tags import tag_incident

BRAND_RE = re.compile(r"\(([A-Z]\d{3})\)")

# Per-meeting URL (not per-race — one HTML page lists every race's incidents)
URL = ("https://racing.hkjc.com/en-us/local/information/racereportfull"
       "?racedate={dmy}&racecourse={course}")


def _to_int(s) -> int | None:
    try: return int(re.sub(r"[^\d-]", "", s or ""))
    except (ValueError, TypeError): return None


SCHEMA = """
CREATE TABLE IF NOT EXISTS incident_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id INTEGER NOT NULL REFERENCES races(id),
    brand TEXT NOT NULL,
    placing INTEGER,
    horse_no INTEGER,
    horse_name TEXT,
    draw INTEGER,
    jockey TEXT,
    incident TEXT NOT NULL,
    -- Comma-joined list of structured tags extracted from `incident` by
    -- betting/incident_tags.py (e.g. "bumped,raced_wide,vet_inspection").
    -- Recomputed on every insert; NULL when no tags match.
    incident_tags TEXT,
    scraped_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(race_id, brand)
);
CREATE INDEX IF NOT EXISTS idx_incidents_race ON incident_reports(race_id);
CREATE INDEX IF NOT EXISTS idx_incidents_tags ON incident_reports(incident_tags);
"""


class IncidentReportsScraper(BaseScraper):
    name = "incident_reports"

    def run(self, args: list[str]) -> int:
        p = argparse.ArgumentParser(prog="scrape_incident_reports")
        p.add_argument("--date", help="YYYY-MM-DD (one meeting)")
        p.add_argument("--since", help="YYYY-MM-DD start (inclusive)")
        p.add_argument("--until", help="YYYY-MM-DD end (inclusive)")
        p.add_argument("--course", choices=["ST", "HV"])
        p.add_argument("--recent", action="store_true", help="last 30 days")
        p.add_argument("--force-refresh", action="store_true")
        ns = p.parse_args(args)

        # Apply schema (idempotent)
        self.db().executescript(SCHEMA)
        self.db().commit()

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
        for date_str in dates:
            if self.should_stop():
                break
            for course in courses:
                try:
                    total += self._scrape_meeting(date_str, course, ns.force_refresh)
                except Exception as exc:
                    log(f"[{self.name}] {date_str}/{course}: {exc}")
        log(f"[{self.name}] done: {total} incident rows")
        return 0

    def _scrape_meeting(self, date_str: str, course: str, force: bool) -> int:
        conn = self.db()
        race_rows = conn.execute(
            "SELECT id, race_no FROM races WHERE date=? AND course=? ORDER BY race_no",
            (date_str, course),
        ).fetchall()
        if not race_rows:
            return 0
        # Skip if already covered (any incident row for any race today)
        if not force:
            any_have = conn.execute(
                "SELECT 1 FROM incident_reports ir "
                "JOIN races r ON r.id = ir.race_id "
                "WHERE r.date = ? AND r.course = ? LIMIT 1",
                (date_str, course),
            ).fetchone()
            if any_have:
                return 0

        dmy = datetime.fromisoformat(date_str).strftime("%d/%m/%Y")
        url = URL.format(dmy=dmy, course=course)
        cache_key = f"{date_str}_{course}"
        try:
            body = self.fetch(url, cache_key=cache_key, force_refresh=force)
        except RuntimeError:
            return 0
        if not body or "Racing Incident Report" not in body:
            return 0

        soup = BeautifulSoup(body, "html.parser")
        race_blocks = self._extract_race_blocks(soup)
        if not race_blocks:
            return 0

        # Map race_no → race_id from the DB
        race_no_to_id = {rn: rid for rid, rn in race_rows}

        total = 0
        with txn(conn):
            for race_no, rows in race_blocks.items():
                race_id = race_no_to_id.get(race_no)
                if not race_id:
                    continue
                # Drop existing rows for this race (clean refresh)
                conn.execute("DELETE FROM incident_reports WHERE race_id = ?",
                             (race_id,))
                for r in rows:
                    tags = tag_incident(r.get("incident"))
                    conn.execute(
                        "INSERT OR REPLACE INTO incident_reports "
                        "(race_id, brand, placing, horse_no, horse_name, "
                        " draw, jockey, incident, incident_tags) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (race_id, r["brand"], r["placing"], r["horse_no"],
                         r["horse_name"], r["draw"], r["jockey"], r["incident"],
                         tags),
                    )
                    total += 1
        log(f"[{self.name}] {date_str}/{course}: {total} incident rows")
        return total

    @staticmethod
    def _extract_race_blocks(soup) -> dict[int, list[dict]]:
        """Return {race_no: [row dicts]}. Race numbers are inferred from the
        ordering of incident tables — the report renders R1, R2, ... in
        order down the page. Header signature is constant: Pla. | Horse No
        | Colour | Horse | Dr. | Jockey | Incident."""
        wanted_hdr = {"Pla.", "Horse No", "Horse", "Dr.", "Jockey", "Incident"}
        out: dict[int, list[dict]] = {}
        race_no = 0
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue
            head_cells = [c.get_text(" ", strip=True)
                          for c in rows[0].find_all(["th", "td"])]
            if not wanted_hdr.issubset(set(head_cells)):
                continue
            race_no += 1
            entries = []
            for tr in rows[1:]:
                cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
                if len(cells) < 7:
                    continue
                placing = _to_int(cells[0])
                horse_no = _to_int(cells[1])
                horse_cell = cells[3]
                m = BRAND_RE.search(horse_cell)
                if not m:
                    continue
                brand = m.group(1)
                horse_name = BRAND_RE.sub("", horse_cell).strip()
                draw = _to_int(cells[4])
                jockey = cells[5]
                incident = cells[6].strip()
                if not incident:
                    incident = "No report."
                entries.append({
                    "placing": placing, "horse_no": horse_no,
                    "brand": brand, "horse_name": horse_name,
                    "draw": draw, "jockey": jockey, "incident": incident,
                })
            if entries:
                out[race_no] = entries
        return out


def main() -> int:
    return IncidentReportsScraper.main()


if __name__ == "__main__":
    sys.exit(main())

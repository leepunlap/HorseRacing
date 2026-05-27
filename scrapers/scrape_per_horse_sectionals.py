#!/usr/bin/env python3
"""HKJC per-horse sectional-time scraper.

Populates `per_horse_sectionals` (race_id, brand, furlong_idx → split_time,
position, lengths_from_lead, cumulative_time). Each row represents one
horse's position + split for one 400m/200m sectional of the race.

Source URL:
  https://racing.hkjc.com/en-us/local/information/displaysectionaltime
    ?racedate=DD/MM/YYYY&Racecourse=ST|HV&RaceNo=N

HTML format (per-horse table):
  [Finishing Order | Horse No | Horse (BRAND) |
   Sec1 cell | Sec2 cell | ... | Total Time]
where each Sec cell is "<position> <lengths> <split_seconds> [<sub1> <sub2>]".

Sub-splits (12.01 11.82) are the half-furlong breakdown for the FINAL two
sectionals, which is HKJC's standard format — we store them as additional
furlong_idx rows so downstream features can see per-half-furlong granularity.

Usage:
  python3 -m scrapers.scrape_per_horse_sectionals --date 2026-05-24
  python3 -m scrapers.scrape_per_horse_sectionals --since 2025-09-01 --until 2026-05-27
  python3 -m scrapers.scrape_per_horse_sectionals --recent
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
LBW_RE = re.compile(r"([\d.]+)(?:-(\d+)/(\d+))?|SH|NK|HD|NS|DH|1/2|3/4|1-1/2")


def _to_int(s):
    try:
        return int(re.sub(r"[^\d-]", "", s or ""))
    except (ValueError, TypeError):
        return None


def _to_float(s):
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _parse_lbw(s: str) -> float | None:
    """'7-3/4' → 7.75, 'SH' → 0.05, '3/4' → 0.75, '' → None (leader)."""
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    aliases = {"SH": 0.05, "NS": 0.03, "HD": 0.10, "NK": 0.30,
               "1/2": 0.5, "3/4": 0.75, "1-1/2": 1.5, "DH": 0.0}
    if s in aliases:
        return aliases[s]
    m = re.match(r"^([\d.]+)(?:-(\d+)/(\d+))?$", s)
    if m:
        whole = float(m.group(1))
        if m.group(2):
            whole += int(m.group(2)) / int(m.group(3))
        return whole
    try:
        return float(s)
    except ValueError:
        return None


def _parse_sec_cell(text: str) -> list[dict]:
    """Parse one sectional cell into a list of per-furlong rows.

    Inputs (whitespace-collapsed):
      "12 7-3/4 25.16"            → 1 row  (pos 12, lengths 7.75, split 25.16)
      "9 2-3/4 23.83 12.01 11.82" → 3 rows (one for the 23.83 quarter, and one
                                            each for the two sub-splits 12.01 + 11.82
                                            with the same pos/lengths)
    Returns a list ordered first→last to preserve the temporal order; each
    dict has split_time + optional pos + lbw."""
    parts = text.replace("\xa0", " ").split()
    if not parts:
        return []
    pos = _to_int(parts[0])
    # Find where the lbw token ends. Could be '1-3/4', 'SH', '3/4', etc.
    # Heuristic: tokens are time-like (\d+\.\d+) once we hit the seconds.
    splits: list[float] = []
    lbw_str = ""
    i = 1
    while i < len(parts):
        tok = parts[i]
        if re.match(r"^\d+\.\d+$", tok):
            splits.append(float(tok))
        elif tok in {"SH", "NS", "NK", "HD", "1/2", "3/4", "DH"} or re.match(r"^[\d.]+(?:-\d+/\d+)?$", tok):
            lbw_str = (lbw_str + " " + tok).strip()
        i += 1
    lbw = _parse_lbw(lbw_str)
    out = []
    for sp in splits:
        out.append({"split_time": sp, "position": pos, "lbw": lbw})
    return out


class PerHorseSectionalScraper(BaseScraper):
    name = "per_horse_sectionals"
    URL = ("https://racing.hkjc.com/en-us/local/information/"
           "displaysectionaltime?racedate={dmy}&Racecourse={course}&RaceNo={rn}")

    def run(self, args: list[str]) -> int:
        p = argparse.ArgumentParser(prog="scrape_per_horse_sectionals")
        p.add_argument("--date", help="YYYY-MM-DD (one meeting)")
        p.add_argument("--since", help="YYYY-MM-DD start (inclusive)")
        p.add_argument("--until", help="YYYY-MM-DD end (inclusive)")
        p.add_argument("--course", choices=["ST", "HV"])
        p.add_argument("--recent", action="store_true", help="last 30 days")
        p.add_argument("--force-refresh", action="store_true")
        ns = p.parse_args(args)

        if ns.recent:
            today = datetime.now().date()
            dates = [(today - timedelta(days=i)).isoformat() for i in range(30)]
        elif ns.since and ns.until:
            d0, d1 = datetime.fromisoformat(ns.since).date(), datetime.fromisoformat(ns.until).date()
            dates = []
            cur = d0
            while cur <= d1:
                dates.append(cur.isoformat())
                cur += timedelta(days=1)
        elif ns.date:
            dates = [ns.date]
        else:
            log("specify --date, --since/--until, or --recent")
            return 2

        courses = [ns.course] if ns.course else ["ST", "HV"]
        total_rows = 0
        for d in dates:
            if self.should_stop():
                break
            for course in courses:
                try:
                    n = self._scrape_meeting(d, course, ns.force_refresh)
                    total_rows += n
                    if n:
                        self.checkpoint({"last_date": d, "last_course": course,
                                         "rows": total_rows})
                except Exception as exc:
                    log(f"[{self.name}] {d}/{course}: {exc}")
        log(f"[{self.name}] done: {total_rows} per-horse-sectional rows")
        return 0

    def _scrape_meeting(self, date_str: str, course: str, force: bool) -> int:
        self._force = force
        conn = self.db()
        races = conn.execute(
            "SELECT id, race_no FROM races WHERE date=? AND course=? ORDER BY race_no",
            (date_str, course),
        ).fetchall()
        if not races:
            return 0
        added = 0
        for race_id, race_no in races:
            if self.should_stop():
                break
            if not force:
                have = conn.execute(
                    "SELECT 1 FROM per_horse_sectionals WHERE race_id=? LIMIT 1",
                    (race_id,)).fetchone()
                if have:
                    continue
            try:
                added += self._scrape_race(date_str, course, race_no, race_id)
            except Exception as exc:
                log(f"[{self.name}] {date_str}/{course}/R{race_no}: {exc}")
        return added

    def _scrape_race(self, date_str: str, course: str, race_no: int, race_id: int) -> int:
        # URL takes DD/MM/YYYY, not YYYY-MM-DD.
        dt = datetime.fromisoformat(date_str)
        dmy = dt.strftime("%d/%m/%Y")
        url = self.URL.format(dmy=dmy, course=course, rn=race_no)
        cache_key = f"{date_str}_{course}_R{race_no}"
        try:
            body = self.fetch(url, cache_key=cache_key,
                              force_refresh=getattr(self, "_force", False))
        except RuntimeError:
            return 0
        if not body or "Sectional Time" not in body:
            return 0

        soup = BeautifulSoup(body, "html.parser")
        per_horse_table = None
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue
            hdr_txt = " ".join(c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"]))
            if "Finishing Order" in hdr_txt and "Horse" in hdr_txt:
                per_horse_table = table
                break
        if per_horse_table is None:
            return 0

        rows_added = 0
        conn = self.db()
        with txn(conn):
            # Wipe existing rows for this race (force-refresh or first write).
            conn.execute("DELETE FROM per_horse_sectionals WHERE race_id = ?", (race_id,))
            # Skip the header rows (variable count) — process every row whose
            # 3rd cell contains a brand `(K123)`.
            for tr in per_horse_table.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
                if len(cells) < 4:
                    continue
                horse_cell = cells[2] if len(cells) > 2 else ""
                m = BRAND_RE.search(horse_cell)
                if not m:
                    continue
                brand = m.group(1)
                # Look up the result_id for this horse/race.
                rid = conn.execute(
                    "SELECT id FROM results WHERE race_id=? AND brand=?",
                    (race_id, brand),
                ).fetchone()
                result_id = rid[0] if rid else None
                # Cells 3..-1 are sectional cells; cell -1 is Total Time.
                cum = 0.0
                furlong_idx = 0
                for sec_cell in cells[3:-1]:
                    parsed = _parse_sec_cell(sec_cell)
                    for entry in parsed:
                        furlong_idx += 1
                        cum += entry["split_time"]
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO per_horse_sectionals
                              (result_id, race_id, brand, furlong_idx,
                               split_time, cumulative_time, position, lengths_from_lead)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (result_id, race_id, brand, furlong_idx,
                             entry["split_time"], round(cum, 3),
                             entry["position"], entry["lbw"]),
                        )
                        rows_added += 1
        log(f"[{self.name}] {date_str}/{course}/R{race_no}: {rows_added} rows")
        return rows_added


def main() -> int:
    return PerHorseSectionalScraper.main()


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Trackwork scraper (HKJC's en-us layout, May 2026+).

The old aggregate Trackwork pages were deprecated and the new ones JS-render
the workout table client-side. The only static-HTML endpoint that still
exposes structured trackwork data is the per-horse history page:

  https://racing.hkjc.com/en-us/local/information/trackworkresult?horseid=HK_YYYY_BRAND

where YYYY is the season year derived from the brand's first letter
(A=2014, B=2015, …, K=2024, L=2025, …). Each page contains the horse's
entire trackwork history (often 800+ rows): Date / Type / Racecourse-Track /
Workouts / Gear.

We iterate horses in the local DB and upsert one row per workout.

Usage:
    python3 -m scrapers.scrape_trackwork --recent      # last 14 days only
    python3 -m scrapers.scrape_trackwork --all         # full history (slow!)
    python3 -m scrapers.scrape_trackwork --brand K491  # one horse
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


VENUE_MAP = {
    "sha tin": "ST", "happy valley": "HV", "conghua": "CH",
}
SURFACE_MAP = {
    "awt": "AWT", "turf": "Turf", "dirt": "Dirt",
    "trotting": "Trotting", "smt": "SmT", "smb": "SmB",
}


def _horse_id_for(brand: str) -> str | None:
    """Brand letter encodes season year: A=2014, B=2015, ..., L=2025."""
    if not brand or not brand[0].isalpha():
        return None
    year = 2014 + (ord(brand[0].upper()) - ord("A"))
    return f"HK_{year}_{brand}"


def _parse_date(s: str) -> str | None:
    """dd/mm/yyyy → YYYY-MM-DD"""
    m = re.match(r"^\s*(\d{2})/(\d{2})/(\d{4})\s*$", s or "")
    if not m:
        return None
    d, mo, y = m.groups()
    return f"{y}-{mo}-{d}"


def _parse_track(s: str) -> tuple[str | None, str | None]:
    """'Sha Tin AWT' → ('ST', 'AWT'); 'Conghua Turf' → ('CH', 'Turf')."""
    if not s:
        return None, None
    s_norm = s.strip().lower()
    venue = None
    for k, v in VENUE_MAP.items():
        if s_norm.startswith(k):
            venue = v
            s_norm = s_norm[len(k):].strip()
            break
    surface = None
    for k, v in SURFACE_MAP.items():
        if k in s_norm:
            surface = v
            break
    return venue, surface


# Extract a "main" time from the Workouts column. Patterns observed:
#   "32.2 28.1 (1.00.3) (C Y Ho)"  → 60.3 (the parenthesised total)
#   "1 Round - Fast (R.B.)"        → None (trotting/swimming, no time)
#   "23.4 (B Shinn)"               → 23.4 (single split)
_TOTAL_TIME_RE = re.compile(r"\((\d+)\.(\d+)\.(\d+)\)")
_SINGLE_TIME_RE = re.compile(r"(\d+\.\d+)\s*\(")


def _parse_workout_time(s: str) -> float | None:
    if not s:
        return None
    m = _TOTAL_TIME_RE.search(s)
    if m:
        mins, secs, cs = m.groups()
        return int(mins) * 60 + int(secs) + int(cs) / 10.0
    m2 = _SINGLE_TIME_RE.search(s)
    if m2:
        try:
            return float(m2.group(1))
        except ValueError:
            pass
    return None


# Distance hint: HKJC's Workouts column has several formats. We extract the
# best-effort distance in metres so H095_trackwork can use load volume, not
# just session count. Three patterns cover ~99% of populated rows:
#
#   "1200M (Z Purton) (N/N)"            → explicit metres (most precise)
#   "SmT 2 Round - Fast (R.B.)"          → N rounds × per-track-round length
#   "12.5 11.8 (1:00.3) (R.B.)"          → N decimal splits ⇒ N × 200m
#
# Canter / Trotting / Treadmill / Swimming / Aqua Walker rows have no
# meaningful race-distance equivalent and stay NULL (caller can still
# count sessions via COUNT(*)).
_M_RE      = re.compile(r"\b(\d{3,4})\s*M\b", re.I)
_ROUND_RE  = re.compile(r"\b(\d+)\s+Round\b", re.I)
_SPLIT_RE  = re.compile(r"^(?:\d+\.\d+\s+)+(?:\d+\.\d+|\(\d+(?:[:.]\d+)+\))", re.I)
# Approx lap length per HKJC training track, in metres. Values from HKJC
# track-design notes; the Sha Tin Sand Mile Track inner loop is ~1230m,
# the AWT inner is similar. Olympic Arena is closer to 1200m. These
# numbers feed a relative volume signal — exact precision isn't critical.
_ROUND_LENGTH_M = {"SmT": 1230, "AWT": 1230, "Olympic": 1200, "TroR": 800}


def _parse_workout_distance(workouts: str, surface: str | None) -> int | None:
    """Return distance in metres if we can extract one, else None."""
    if not workouts:
        return None
    w = workouts.strip()
    m = _M_RE.search(w)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    m = _ROUND_RE.search(w)
    if m:
        # Prefer the explicit track tag in the workouts string; fall back
        # to the surface column the row was tagged with.
        track_tag = next((k for k in _ROUND_LENGTH_M if k in w), None) or (surface or "")
        per_round = _ROUND_LENGTH_M.get(track_tag, 1200)
        try:
            return int(m.group(1)) * per_round
        except ValueError:
            pass
    if _SPLIT_RE.match(w):
        # Each space-separated decimal is a furlong split (≈200m).
        splits = re.findall(r"\b\d+\.\d+(?!\d)", w.split("(")[0])
        if splits:
            return len(splits) * 200
    return None


class TrackworkScraper(BaseScraper):
    name = "trackwork"

    def run(self, args: list[str]) -> int:
        p = argparse.ArgumentParser(prog="scrape_trackwork")
        p.add_argument("--recent", action="store_true",
                       help="only store workouts in the last 14 days")
        p.add_argument("--all", action="store_true",
                       help="store full workout history per horse (slow, big)")
        p.add_argument("--brand", help="scrape one brand only")
        p.add_argument("--limit", type=int, default=0,
                       help="cap horses processed (0 = no cap)")
        ns = p.parse_args(args)

        if not (ns.recent or ns.all or ns.brand):
            log("specify --recent, --all, or --brand <K491>")
            return 2

        conn = self.db()
        if ns.brand:
            brands = [ns.brand]
        else:
            rows = conn.execute(
                "SELECT brand FROM horses WHERE brand IS NOT NULL "
                "ORDER BY brand"
            ).fetchall()
            brands = [r[0] for r in rows]
            if ns.limit:
                brands = brands[: ns.limit]

        cutoff: str | None = None
        if ns.recent:
            cutoff = (datetime.now().date() - timedelta(days=14)).isoformat()

        log(f"[{self.name}] processing {len(brands)} horses "
            f"({'last 14d' if ns.recent else 'all history'})")
        total = 0
        for i, brand in enumerate(brands, start=1):
            if self.should_stop():
                break
            try:
                n = self._scrape_horse(brand, cutoff)
                total += n
                if i % 25 == 0:
                    self.checkpoint({"horses_done": i, "rows": total})
            except Exception as exc:
                log(f"[{self.name}] {brand}: {exc}")
        self.checkpoint({"horses_done": len(brands), "rows": total})
        log(f"[{self.name}] done: {total} trackwork rows across {len(brands)} horses")
        return 0

    def _scrape_horse(self, brand: str, cutoff_date: str | None) -> int:
        horse_id = _horse_id_for(brand)
        if not horse_id:
            return 0
        url = ("https://racing.hkjc.com/en-us/local/information/"
               f"trackworkresult?horseid={horse_id}")
        try:
            body = self.fetch(url, cache_key=brand)
        except RuntimeError:
            return 0
        if not body:
            return 0

        soup = BeautifulSoup(body, "html.parser")
        # Find the trackwork table — the only large table with a Date/Type/Track header
        target_table = None
        for t in soup.find_all("table"):
            rows = t.find_all("tr")
            if len(rows) < 2:
                continue
            header = [td.get_text(" ", strip=True) for td in rows[0].find_all(["th", "td"])]
            if "Date" in header and "Type" in header and "Workouts" in header:
                target_table = t
                break
        if target_table is None:
            return 0

        conn = self.db()
        rows_added = 0
        horse_db_id = lookup_horse_id(conn, brand)
        with txn(conn):
            for tr in target_table.find_all("tr")[1:]:
                tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                if len(tds) < 4:
                    continue
                date = _parse_date(tds[0])
                if not date:
                    continue
                if cutoff_date and date < cutoff_date:
                    # Trackwork list is reverse-chronological; once we drop
                    # below the cutoff we can stop.
                    break
                wtype = tds[1].strip()
                venue, surface = _parse_track(tds[2])
                workouts = tds[3].strip()
                gear = tds[4].strip() if len(tds) > 4 else None
                time_sec = _parse_workout_time(workouts)

                row = {
                    "horse_id": horse_db_id,
                    "brand": brand,
                    "date": date,
                    "venue": venue,
                    "surface": surface or wtype,    # fall back to type when no surface tag
                    "distance": _parse_workout_distance(workouts, surface or wtype),
                    "time_sec": time_sec,
                    "gear": gear or None,
                    "rider": None,
                    "trainer": None,
                    "notes": (workouts[:200] if workouts else None),
                }
                self.upsert(
                    "trackwork", row,
                    conflict_cols=("brand", "date", "venue", "distance", "time_sec"),
                )
                rows_added += 1
        return rows_added


if __name__ == "__main__":
    sys.exit(TrackworkScraper.main())

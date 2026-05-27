#!/usr/bin/env python3
"""Backfill `results.horse_no` from cached race-card HTML.

The race-card scraper only started capturing `Horse No.` today; every
historical results row has horse_no = NULL. That blocks the live-odds
JOIN in /api/races/{date} for any race before today.

This script walks every distinct (date, course) in `races`, looks for
its cached HTML under `data/raw/race_card/`, parses the runners table
the same way `scrape_race_card._extract_runners` does, and UPDATEs
the matching `results` row by (date, course, race_no, brand).

Idempotent: only writes rows where horse_no IS NULL.

Usage:
    python3 -m scrapers.backfill_horse_no               # all gaps
    python3 -m scrapers.backfill_horse_no --since 2025-09-01
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "racing.db"
CARD_CACHE = BASE_DIR / "data" / "raw" / "race_card"
RESULTS_CACHE = BASE_DIR / "data" / "raw" / "results"
BRAND_RE = re.compile(r"\(([A-Z]\d{3})\)")


def _to_int(s) -> int | None:
    try:
        return int(re.sub(r"[^\d-]", "", s or ""))
    except (ValueError, TypeError):
        return None


def _parse_runners(html: str) -> list[dict]:
    """Mirror of scrape_race_card._extract_runners — pulls (horse_no, brand)
    pairs out of the runners table."""
    soup = BeautifulSoup(html, "html.parser")
    wanted = {"Horse", "Brand No.", "Jockey", "Trainer", "Wt.", "Draw"}
    for t in soup.find_all("table"):
        rows = t.find_all("tr")
        if len(rows) < 3:
            continue
        hdr_cells = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]
        if not wanted.issubset(set(hdr_cells)):
            continue
        idx = {h: i for i, h in enumerate(hdr_cells)}
        out: list[dict] = []
        for tr in rows[1:]:
            cells = tr.find_all("td")
            if len(cells) < 6:
                continue
            def col(name):
                i = idx.get(name)
                return (cells[i].get_text(" ", strip=True)
                        if i is not None and i < len(cells) else None)
            brand = col("Brand No.")
            horse_no = _to_int(col("Horse No."))
            if brand and horse_no is not None:
                out.append({"brand": brand, "horse_no": horse_no})
        if out:
            return out
    return []


def _parse_results_runners(html: str) -> list[dict]:
    """Pull (horse_no, brand) from a cached LocalResults page. The post-
    race table has columns [Pla., Horse No., Horse (brand), Jockey, ...]
    so horse_no = cell 1, brand = parsed from cell 2."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for t in soup.find_all("table"):
        rows = t.find_all("tr")
        if len(rows) < 2:
            continue
        hdr = rows[0].get_text(" ", strip=True)
        # Match either EN ("Horse No.") or ZH ("馬號") header
        if "Horse No" not in hdr and "馬號" not in hdr:
            continue
        for tr in rows[1:]:
            tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(tds) < 4:
                continue
            horse_no = _to_int(tds[1])
            m = BRAND_RE.search(tds[2]) if len(tds) > 2 else None
            if not m or horse_no is None:
                continue
            out.append({"brand": m.group(1), "horse_no": horse_no})
        if out:
            break
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--since", default="2024-11-01",
                   help="earliest date to backfill (default 2024-11-01)")
    ns = p.parse_args()

    conn = sqlite3.connect(DB_PATH)
    races = conn.execute(
        "SELECT id, date, course, race_no FROM races "
        "WHERE date >= ? ORDER BY date, course, race_no",
        (ns.since,),
    ).fetchall()
    print(f"Walking {len(races)} races…")

    # Cache per-(date, course) parsed runners so we don't re-read the
    # whole-meeting HTML for every race.
    meeting_cache: dict[tuple[str, str], dict[int, dict[str, int]]] = {}
    updated = 0
    skipped = 0
    no_html = 0

    for race_id, date_str, course, race_no in races:
        # Skip if every result row already has horse_no
        nullc = conn.execute(
            "SELECT COUNT(*) FROM results WHERE race_id = ? AND horse_no IS NULL",
            (race_id,),
        ).fetchone()[0]
        if nullc == 0:
            skipped += 1
            continue

        key = (date_str, course)
        if key not in meeting_cache:
            meeting_cache[key] = {}
            # Try per-race file first, then whole-meeting file
            paths = [
                CARD_CACHE / f"{date_str}_{course}_r{race_no}.html",
                CARD_CACHE / f"{date_str}_{course}.html",
            ]
            for p_html in paths:
                if not p_html.exists():
                    continue
                runners = _parse_runners(p_html.read_text(encoding="utf-8"))
                if runners:
                    meeting_cache[key].setdefault(
                        race_no, {r["brand"]: r["horse_no"] for r in runners}
                    )
                    break

        # Per-race cards override whole-meeting; load per-race if missing.
        if race_no not in meeting_cache[key]:
            p_html = CARD_CACHE / f"{date_str}_{course}_r{race_no}.html"
            if p_html.exists():
                runners = _parse_runners(p_html.read_text(encoding="utf-8"))
                meeting_cache[key][race_no] = {
                    r["brand"]: r["horse_no"] for r in runners
                }

        # Fall back to the cached results HTML when no card cache exists
        # (most historical meetings only have the results page cached).
        if race_no not in meeting_cache[key]:
            res_html = RESULTS_CACHE / f"{date_str}_{course}_R{race_no}.html"
            if res_html.exists():
                runners = _parse_results_runners(res_html.read_text(encoding="utf-8"))
                if runners:
                    meeting_cache[key][race_no] = {
                        r["brand"]: r["horse_no"] for r in runners
                    }

        brand_map = meeting_cache[key].get(race_no, {})
        if not brand_map:
            no_html += 1
            continue

        n = 0
        for brand, hno in brand_map.items():
            cur = conn.execute(
                "UPDATE results SET horse_no = ? "
                "WHERE race_id = ? AND brand = ? AND horse_no IS NULL",
                (hno, race_id, brand),
            )
            n += cur.rowcount
        if n:
            updated += n
    conn.commit()
    conn.close()
    print(f"updated={updated}  skipped={skipped}  no_html={no_html}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

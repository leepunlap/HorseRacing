#!/usr/bin/env python3
"""Race-card scraper.

Extends the v1 racecard scraper to capture rail position, prize_money (numeric),
post_time, and race_name into the `races` table. Source: HKJC racecard HTML
header at racing.hkjc.com/racing/information/English/Racing/RaceCard.aspx
?RaceDate=<YYYY/MM/DD>&Racecourse=<ST|HV>.

Usage:
    python3 -m scrapers.scrape_race_card --date 2026-05-26 --course ST
    python3 -m scrapers.scrape_race_card --next     # next 14 days
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Allow `python3 scrapers/scrape_race_card.py` from project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup

from scrapers._base import BaseScraper, log, txn, lookup_race_id


RAIL_RE = re.compile(r"Course:\s*([A-C][\+\-]?\d*)", re.I)
PRIZE_RE = re.compile(r"Prize[^$]*\$([\d,]+)", re.I)
POSTTIME_RE = re.compile(r"\b(\d{1,2}:\d{2})\b", re.I)
RACENAME_RE = re.compile(r"RACE\s*\d+\s*[—\-–]\s*(.+)", re.I)

# Per-race detail page (en-us layout) — exposes distance / class / going /
# surface / rail in a single banner line:
#   Race 2 - LOIRE HANDICAP Wednesday, May 27, 2026, Happy Valley, 19:10
#   Turf, "B" Course, 2200M, Good
#   Prize Money: $875,000, Rating: 40-0, Class 5
DISTANCE_RE = re.compile(r"(\d{3,4})\s*M\b", re.I)
SURFACE_RE = re.compile(r"\b(Turf|All Weather Track|AWT|Dirt)\b", re.I)
RAIL_LBL_RE = re.compile(r'"([A-C][\+\-]?\d*)"\s*Course', re.I)
CLASS_RE = re.compile(r"Class\s+([1-5])\b", re.I)
GOING_RE = re.compile(
    r"\b(Good\s+to\s+Yielding|Good\s+to\s+Firm|Good|Yielding|Soft|Heavy|Fast|Wet\s+Fast|Wet\s+Slow|Sloppy|Muddy|Frozen)\b",
    re.I,
)
DETAIL_POST_RE = re.compile(r",\s*(\d{1,2}:\d{2})\b")
DETAIL_NAME_RE = re.compile(r"Race\s*\d+\s*-\s*([A-Z][A-Z &\-']+(?:HANDICAP|TROPHY|CUP|STAKES|PLATE|CHALLENGE)?)\s+(?:Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday)", re.I)


class RaceCardScraper(BaseScraper):
    name = "race_card"

    def run(self, args: list[str]) -> int:
        p = argparse.ArgumentParser(prog="scrape_race_card")
        p.add_argument("--date", help="YYYY-MM-DD")
        p.add_argument("--course", choices=["ST", "HV"], default="ST")
        p.add_argument("--next", action="store_true", help="next 14 days")
        ns = p.parse_args(args)

        targets: list[tuple[str, str]] = []
        if ns.next:
            today = datetime.now().date()
            for i in range(14):
                d = (today + timedelta(days=i)).isoformat()
                targets.append((d, "ST"))
                targets.append((d, "HV"))
        elif ns.date:
            targets.append((ns.date, ns.course))
        else:
            log("specify --date or --next")
            return 2

        ok = 0
        for date_str, course in targets:
            if self.should_stop():
                break
            try:
                if self._scrape_card(date_str, course):
                    ok += 1
                    self.checkpoint({"last_date": date_str, "last_course": course})
            except Exception as exc:
                log(f"[{self.name}] {date_str}/{course} failed: {exc}")
        log(f"[{self.name}] done: {ok} cards updated")
        return 0

    def _scrape_card(self, date_str: str, course: str) -> bool:
        url_date = date_str.replace("-", "/")
        # Use the en-us per-race detail page which has the full banner
        # (distance / class / going / surface / rail / prize / post). We
        # discover race numbers by walking the nav links on race 1.
        new_base = (
            "https://racing.hkjc.com/en-us/local/information/racecard"
            f"?racedate={url_date}&Racecourse={course}"
        )
        cache_key = f"{date_str}_{course}"
        body = self.fetch(f"{new_base}&RaceNo=1", cache_key=f"{cache_key}_r1")
        if not body or len(body) < 50_000:
            # New URL didn't return a populated card — fall back to legacy
            # racecard for meta extraction only.
            legacy_base = (
                "https://racing.hkjc.com/racing/information/English/Racing/RaceCard.aspx"
                f"?RaceDate={url_date}&Racecourse={course}"
            )
            body = self.fetch(legacy_base, cache_key=cache_key)
            if not body or "RaceCard" not in body:
                return False
            return self._scrape_legacy(date_str, course, body, legacy_base, cache_key)

        soup = BeautifulSoup(body, "html.parser")
        race_nos = {1}
        for a in soup.find_all("a", href=True):
            mn = re.search(r"RaceNo=(\d+)", a["href"], re.I)
            if mn:
                race_nos.add(int(mn.group(1)))

        per_race: dict[int, dict[str, str]] = {}
        per_race[1] = self._extract_detail_banner(soup)
        runners: dict[int, list[dict]] = {}
        runners[1] = self._extract_runners(soup)
        rail_str = per_race[1].get("rail")
        for rn in sorted(race_nos):
            if rn == 1: continue
            try:
                sub_body = self.fetch(f"{new_base}&RaceNo={rn}",
                                      cache_key=f"{cache_key}_r{rn}")
                if sub_body:
                    sub_soup = BeautifulSoup(sub_body, "html.parser")
                    per_race[rn] = self._extract_detail_banner(sub_soup)
                    runners[rn] = self._extract_runners(sub_soup)
                    rail_str = rail_str or per_race[rn].get("rail")
            except Exception as exc:
                log(f"[{self.name}] {date_str}/{course} race {rn}: {exc}")
        return self._persist(date_str, course, rail_str, per_race, runners)

    def _scrape_legacy(self, date_str: str, course: str, body: str,
                       base_url: str, cache_key: str) -> bool:
        """Legacy code path retained for meets that haven't migrated to the
        en-us racecard page (none in 2026, but kept for robustness)."""
        soup = BeautifulSoup(body, "html.parser")
        text = soup.get_text(" ", strip=True)
        rail_m = RAIL_RE.search(text)
        rail = rail_m.group(1) if rail_m else None

        race_nos = {1}
        for a in soup.find_all("a", href=True):
            mn = re.search(r"RaceNo=(\d+)", a["href"], re.I)
            if mn:
                race_nos.add(int(mn.group(1)))

        per_race: dict[int, dict[str, str]] = {}
        per_race.update(self._extract_per_race_blocks(soup))
        for rn in sorted(race_nos):
            if rn in per_race:
                continue
            try:
                sub_url = f"{base_url}&RaceNo={rn}"
                sub_body = self.fetch(sub_url, cache_key=f"{cache_key}_r{rn}")
                if sub_body:
                    sub_soup = BeautifulSoup(sub_body, "html.parser")
                    per_race.update(self._extract_per_race_blocks(sub_soup))
            except Exception as exc:
                log(f"[{self.name}] {date_str}/{course} race {rn}: {exc}")
        return self._persist(date_str, course, rail, per_race, {})

    def _persist(self, date_str: str, course: str, rail: str | None,
                 per_race: dict[int, dict[str, str]],
                 runners: dict[int, list[dict]] | None = None) -> bool:
        runners = runners or {}

        conn = self.db()
        with txn(conn):
            if rail is not None:
                self.upsert(
                    "rail_position",
                    {"date": date_str, "course": course, "rail": rail},
                    conflict_cols=("date", "course"),
                )

            for race_no, info in per_race.items():
                race_id = lookup_race_id(conn, date_str, course, race_no)
                if race_id is None:
                    # Race row may not exist yet; upsert one with just the keys.
                    conn.execute(
                        "INSERT OR IGNORE INTO races (date, course, race_no) VALUES (?,?,?)",
                        (date_str, course, race_no),
                    )
                    race_id = lookup_race_id(conn, date_str, course, race_no)

                fields: list[str] = []
                params: list = []
                for col, val in (
                    ("race_name", info.get("name")),
                    ("prize",     info.get("prize_raw")),
                    ("post_time", info.get("post_time")),
                    ("distance",  info.get("distance")),
                    ("class",     info.get("class")),
                    ("going",     info.get("going")),
                ):
                    if val is not None:
                        fields.append(f"{col} = ?")
                        params.append(val)
                if fields:
                    params.append(race_id)
                    conn.execute(
                        f"UPDATE races SET {', '.join(fields)} WHERE id = ?",
                        params,
                    )

                for runner in runners.get(race_no, []):
                    self._upsert_runner(conn, race_id, date_str, course,
                                        race_no, runner)

        n_runners = sum(len(v) for v in runners.values())
        log(f"[{self.name}] {date_str}/{course}: rail={rail}, races={len(per_race)}, runners={n_runners}")
        return True

    @staticmethod
    def _upsert_runner(conn, race_id: int, date_str: str, course: str,
                       race_no: int, r: dict) -> None:
        brand = r.get("brand")
        if not brand:
            return
        conn.execute(
            "INSERT OR IGNORE INTO horses (brand) VALUES (?)", (brand,),
        )
        # Refresh stable info on the horses row. The `horses.name` column
        # mirrors the racecard's "Horse" cell.
        h_fields, h_params = [], []
        col_map = {"horse_name": "name", "age": "age", "sex": "sex",
                   "colour": "colour", "rating": "rating"}
        for src_key, db_col in col_map.items():
            if r.get(src_key) is not None:
                h_fields.append(f"{db_col} = ?"); h_params.append(r[src_key])
        if h_fields:
            h_params.append(brand)
            conn.execute(f"UPDATE horses SET {', '.join(h_fields)} WHERE brand = ?", h_params)

        existing = conn.execute(
            "SELECT id FROM results WHERE race_id = ? AND brand = ?",
            (race_id, brand),
        ).fetchone()
        if existing:
            sets, params = [], []
            for col in ("horse_no","horse_name","jockey","trainer","draw","act_wt","decl_wt"):
                if r.get(col) is not None:
                    sets.append(f"{col} = ?"); params.append(r[col])
            if sets:
                params.append(existing[0])
                conn.execute(f"UPDATE results SET {', '.join(sets)} WHERE id = ?", params)
        else:
            conn.execute(
                """
                INSERT INTO results
                   (race_id, horse_id, date, race_no, course, brand, horse_no,
                    horse_name, jockey, trainer, draw, act_wt, decl_wt)
                VALUES (?, (SELECT id FROM horses WHERE brand = ?),
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (race_id, brand, date_str, race_no, course, brand,
                 r.get("horse_no"), r.get("horse_name"), r.get("jockey"),
                 r.get("trainer"), r.get("draw"), r.get("act_wt"), r.get("decl_wt")),
            )

    @staticmethod
    def _extract_runners(soup: BeautifulSoup) -> list[dict]:
        """Parse the runners table from the en-us racecard page.

        Identifies the table by its canonical header signature
        (Horse + Brand No. + Jockey + Trainer columns present) and maps each
        body row to a runner dict ready for `results`/`horses` upsert.
        """
        wanted = {"Horse", "Brand No.", "Jockey", "Trainer", "Wt.", "Draw"}
        for t in soup.find_all("table"):
            rows = t.find_all("tr")
            if len(rows) < 3:
                continue
            hdr_cells = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th","td"])]
            if not wanted.issubset(set(hdr_cells)):
                continue

            idx = {h: i for i, h in enumerate(hdr_cells)}
            def col(name, cells):
                i = idx.get(name)
                return cells[i].get_text(" ", strip=True) if i is not None and i < len(cells) else None

            out: list[dict] = []
            for tr in rows[1:]:
                cells = tr.find_all("td")
                if len(cells) < 6:
                    continue
                brand = col("Brand No.", cells) or None
                if not brand:
                    continue
                # HKJC sometimes nests the horse name in a link; .get_text handles it.
                def _to_int(s):
                    try: return int(re.sub(r"[^\d-]", "", s or ""))
                    except (ValueError, TypeError): return None
                def _to_float(s):
                    try: return float(re.sub(r"[^\d.]", "", s or ""))
                    except (ValueError, TypeError): return None
                horse_no = _to_int(col("Horse No.", cells))
                draw = _to_int(col("Draw", cells))
                # `Rtg.` sometimes lives under `Int'l Rtg.`
                rating = _to_int(col("Rtg.", cells)) or _to_int(col("Int'l Rtg.", cells))
                # Strip the saddle number from name when present in cell.
                horse_name = col("Horse", cells) or None
                out.append({
                    "brand": brand,
                    "horse_no": horse_no,
                    "horse_name": horse_name,
                    "jockey": col("Jockey", cells) or None,
                    "trainer": col("Trainer", cells) or None,
                    "draw": draw,
                    "act_wt": _to_float(col("Wt.", cells)),
                    "decl_wt": _to_float(col("Horse Wt. (Declaration)", cells)),
                    "age": _to_int(col("Age", cells)),
                    "sex": col("Sex", cells) or None,
                    "colour": col("Colour", cells) or None,
                    "rating": rating,
                })
            if out:
                return out
        return []

    @staticmethod
    def _extract_detail_banner(soup: BeautifulSoup) -> dict[str, str]:
        """Parse the per-race banner from the en-us racecard page.

        Example block:
            Race 2 - LOIRE HANDICAP Wednesday, May 27, 2026, Happy Valley,
            19:10 Turf, "B" Course, 2200M, Good
            Prize Money: $875,000, Rating: 40-0, Class 5
        """
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        out: dict[str, str] = {}
        m = DETAIL_NAME_RE.search(text)
        if m:
            out["name"] = m.group(1).strip()
        m = DETAIL_POST_RE.search(text)
        if m:
            hh, mm = m.group(1).split(":")
            if 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59:
                out["post_time"] = f"{int(hh):02d}:{int(mm):02d}"
        m = DISTANCE_RE.search(text)
        if m:
            try:
                d = int(m.group(1))
                if 800 <= d <= 3000:
                    out["distance"] = d
            except ValueError:
                pass
        m = SURFACE_RE.search(text)
        if m:
            s = m.group(1).strip()
            out["surface"] = "AWT" if s.upper() in ("ALL WEATHER TRACK", "AWT") else s.title()
        m = RAIL_LBL_RE.search(text)
        if m:
            out["rail"] = m.group(1)
        m = CLASS_RE.search(text)
        if m:
            out["class"] = f"Class {m.group(1)}"
        m = GOING_RE.search(text)
        if m:
            out["going"] = m.group(1).title()
        m = PRIZE_RE.search(text)
        if m:
            out["prize_raw"] = m.group(1)
        return out

    @staticmethod
    def _extract_per_race_blocks(soup: BeautifulSoup) -> dict[int, dict[str, str]]:
        out: dict[int, dict[str, str]] = {}
        for header in soup.find_all(string=RACENAME_RE):
            m = RACENAME_RE.search(str(header))
            if not m:
                continue
            # Find "RACE N" in the same string
            num_m = re.search(r"RACE\s*(\d+)", str(header), re.I)
            if not num_m:
                continue
            race_no = int(num_m.group(1))
            name = m.group(1).strip()
            # Prize amount lives in a nearby element; scan ancestor text.
            anc_text = ""
            ancestor = header.parent
            for _ in range(4):
                if ancestor is None:
                    break
                anc_text = ancestor.get_text(" ", strip=True)
                if "Prize" in anc_text:
                    break
                ancestor = ancestor.parent
            prize_m = PRIZE_RE.search(anc_text)
            entry: dict[str, str] = {"name": name}
            if prize_m:
                entry["prize_raw"] = prize_m.group(1)
            # Post time is published near the race header as "HH:MM" (HKT).
            pt_m = POSTTIME_RE.search(anc_text)
            if pt_m:
                # Validate the HH:MM range; ignore odd matches.
                hh, mm = pt_m.group(1).split(":")
                if 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59:
                    entry["post_time"] = f"{int(hh):02d}:{int(mm):02d}"
            out[race_no] = entry
        return out


if __name__ == "__main__":
    sys.exit(RaceCardScraper.main())

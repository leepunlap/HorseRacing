#!/usr/bin/env python3
"""Backfill bilingual horse names (`horses.name_en`, `horses.name_zh`).

Two-stage strategy:

  Stage 1 — promote `results.horse_name`. For every distinct brand we
            already have a result row for, detect whether the stored
            name is Chinese (any CJK character) or English (ASCII) and
            copy it into the matching `horses.name_*` column.

  Stage 2 — fill remaining gaps from HKJC. For each brand that is
            missing one side after Stage 1, locate the most recent
            meeting where the horse ran and fetch HKJC's LocalResults
            page in the missing language. Parse all (brand → name)
            pairs out of the page and apply to every horse in the gap
            set (not just the trigger horse — one fetch fills the whole
            field).

Idempotent: re-runs only touch rows still missing one of the names.

Usage:
    python3 -m scrapers.backfill_horse_names              # both stages
    python3 -m scrapers.backfill_horse_names --stage 1    # promote only
    python3 -m scrapers.backfill_horse_names --stage 2    # HKJC fetch only
    python3 -m scrapers.backfill_horse_names --refresh    # ignore existing values
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup

from scrapers._base import BaseScraper, log, txn

BRAND_RE = re.compile(r"\(([A-Z]\d{3})\)")
CJK_RE = re.compile(r"[一-鿿]")
URL_LANG_PATH = {"en": "english", "zh": "Chinese"}


def _is_zh(s: str | None) -> bool:
    return bool(s and CJK_RE.search(s))


def _strip_brand_suffix(s: str) -> str:
    """'醒目勇駒(B456)' → '醒目勇駒'. 'KING ALLOY (K099)' → 'KING ALLOY'."""
    return BRAND_RE.sub("", s or "").strip()


class HorseNameBackfill(BaseScraper):
    name = "backfill_horse_names"

    def run(self, args: list[str]) -> int:
        p = argparse.ArgumentParser(prog="backfill_horse_names")
        p.add_argument("--stage", type=int, choices=[1, 2], default=None,
                       help="Run only stage 1 (promote) or 2 (HKJC fetch)")
        p.add_argument("--refresh", action="store_true",
                       help="Overwrite existing name_en / name_zh values")
        ns = p.parse_args(args)

        if ns.stage in (None, 1):
            self._stage_promote(refresh=ns.refresh)
        if ns.stage in (None, 2):
            self._stage_hkjc_fill(refresh=ns.refresh)
        return 0

    # ─── Stage 1 ──────────────────────────────────────────────────────
    def _stage_promote(self, *, refresh: bool) -> None:
        conn = self.db()
        rows = conn.execute(
            "SELECT DISTINCT brand, horse_name FROM results "
            "WHERE horse_name IS NOT NULL AND horse_name != ''"
        ).fetchall()
        en_count = zh_count = 0
        with txn(conn):
            for brand, raw in rows:
                core = _strip_brand_suffix(raw)
                if not core:
                    continue
                target = "name_zh" if _is_zh(core) else "name_en"
                if refresh:
                    conn.execute(
                        f"UPDATE horses SET {target} = ? WHERE brand = ?",
                        (core, brand),
                    )
                else:
                    conn.execute(
                        f"UPDATE horses SET {target} = ? "
                        f"WHERE brand = ? AND ({target} IS NULL OR {target} = '')",
                        (core, brand),
                    )
                if target == "name_zh":
                    zh_count += 1
                else:
                    en_count += 1
                # Ensure the horse row exists (some legacy DBs have brands
                # in results but no horses row).
                conn.execute(
                    "INSERT OR IGNORE INTO horses (brand) VALUES (?)",
                    (brand,),
                )
                conn.execute(
                    f"UPDATE horses SET {target} = COALESCE(NULLIF({target}, ''), ?) "
                    f"WHERE brand = ?",
                    (core, brand),
                )
        log(f"[{self.name}] stage 1 promote: en={en_count} zh={zh_count}")

    # ─── Stage 2 ──────────────────────────────────────────────────────
    def _stage_hkjc_fill(self, *, refresh: bool) -> None:
        conn = self.db()
        # Brands missing English / Chinese. Restrict to horses that have
        # raced within `active_window_days` (default 365) — older horses
        # are unlikely to surface in the UI and 2500+ profile fetches
        # would be prohibitive.
        miss_en = conn.execute(
            "SELECT h.brand FROM horses h "
            "WHERE (h.name_en IS NULL OR h.name_en = '') "
            "  AND EXISTS (SELECT 1 FROM results r WHERE r.brand = h.brand "
            "              AND r.date >= date('now', '-365 days'))"
        ).fetchall()
        miss_zh = conn.execute(
            "SELECT h.brand FROM horses h "
            "WHERE (h.name_zh IS NULL OR h.name_zh = '') "
            "  AND EXISTS (SELECT 1 FROM results r WHERE r.brand = h.brand "
            "              AND r.date >= date('now', '-365 days'))"
        ).fetchall()
        miss_en_set = {b[0] for b in miss_en}
        miss_zh_set = {b[0] for b in miss_zh}

        # If we're refreshing, treat every brand-with-results as a gap.
        if refresh:
            all_brands = {r[0] for r in conn.execute(
                "SELECT DISTINCT brand FROM results"
            ).fetchall()}
            miss_en_set = all_brands.copy()
            miss_zh_set = all_brands.copy()

        log(f"[{self.name}] stage 2 needs: en={len(miss_en_set)} zh={len(miss_zh_set)}")
        if not miss_en_set and not miss_zh_set:
            return

        # For each missing-name brand, pick the most recent meeting where
        # the horse ran. Group by meeting so one fetch fills the whole
        # field — much cheaper than per-horse fetches.
        meetings_en = self._meetings_for_gaps(conn, miss_en_set)
        meetings_zh = self._meetings_for_gaps(conn, miss_zh_set)

        log(f"[{self.name}] meetings to fetch: en={len(meetings_en)} zh={len(meetings_zh)}")
        self._fetch_meetings(conn, meetings_en, "en", refresh=refresh)
        self._fetch_meetings(conn, meetings_zh, "zh", refresh=refresh)

    def _meetings_for_gaps(self, conn, brand_set: set[str]) -> list[tuple[str, str]]:
        """For each brand in `brand_set`, return its most recent (date, course).
        Returned list is deduped by meeting, since `_fetch_meeting_names`
        sweeps all races on a given (date, course)."""
        if not brand_set:
            return []
        placeholders = ",".join("?" * len(brand_set))
        rows = conn.execute(
            f"SELECT brand, MAX(date) AS d FROM results "
            f"WHERE brand IN ({placeholders}) GROUP BY brand",
            tuple(brand_set),
        ).fetchall()
        meetings: set[tuple[str, str]] = set()
        for brand, d in rows:
            mt = conn.execute(
                "SELECT r.date, r.course FROM results re "
                "JOIN races r ON r.id = re.race_id "
                "WHERE re.brand = ? AND re.date = ? LIMIT 1",
                (brand, d),
            ).fetchone()
            if mt:
                meetings.add(tuple(mt))
        return sorted(meetings)

    def _fetch_meetings(self, conn, meetings, lang: str, *, refresh: bool) -> None:
        col = f"name_{lang}"
        for date_str, course in meetings:
            if self.should_stop():
                break
            try:
                names = self._fetch_meeting_names(date_str, course, 1, lang)
            except Exception as exc:
                log(f"[{self.name}] {date_str}/{course}/R{race_no} ({lang}) "
                    f"fetch failed: {exc}")
                continue
            if not names:
                continue
            applied = 0
            with txn(conn):
                for brand, name in names.items():
                    conn.execute(
                        "INSERT OR IGNORE INTO horses (brand) VALUES (?)",
                        (brand,),
                    )
                    if refresh:
                        conn.execute(
                            f"UPDATE horses SET {col} = ? WHERE brand = ?",
                            (name, brand),
                        )
                    else:
                        conn.execute(
                            f"UPDATE horses SET {col} = ? "
                            f"WHERE brand = ? AND ({col} IS NULL OR {col} = '')",
                            (name, brand),
                        )
                    applied += 1
            log(f"[{self.name}] {date_str}/{course} ({lang}): {applied} horses")

    def _fetch_meeting_names(self, date_str: str, course: str,
                             race_no: int, lang: str) -> dict[str, str]:
        """Fetch HKJC LocalResults in `lang` and parse (brand → name) for
        the entire field of the meeting (by scanning the race-no nav)."""
        url_date = date_str.replace("-", "/")
        out: dict[str, str] = {}
        # The page also has links to all races in this meeting; fetching
        # one race gets us the whole-meeting field via the results table,
        # but to be thorough we fetch each race_no shown in the nav.
        # For efficiency we iterate 1..14 (HKJC max) and stop on 404s.
        for rn in range(1, 15):
            url = (
                f"https://racing.hkjc.com/racing/information/"
                f"{URL_LANG_PATH[lang]}/Racing/LocalResults.aspx"
                f"?RaceDate={url_date}&Racecourse={course}&RaceNo={rn}"
            )
            cache_key = f"{date_str}_{course}_R{rn}_{lang}"
            try:
                body = self.fetch(url, cache_key=cache_key)
            except RuntimeError:
                break
            if not body:
                break
            # HKJC publishes the same horses table on both EN and ZH pages.
            # Detect either header marker; otherwise the page is a 404 /
            # placeholder for a race that doesn't exist.
            if ("Pla." not in body and "名次" not in body
                    and "Horse No" not in body):
                break
            soup = BeautifulSoup(body, "html.parser")
            for table in soup.find_all("table"):
                hdr = table.find("tr")
                if not hdr:
                    continue
                head_txt = hdr.get_text(" ", strip=True)
                if not any(k in head_txt for k in ("Horse", "馬名", "馬 名")):
                    continue
                for tr in table.find_all("tr")[1:]:
                    cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
                    if len(cells) < 4:
                        continue
                    for cell in cells:
                        m = BRAND_RE.search(cell)
                        if not m:
                            continue
                        core = _strip_brand_suffix(cell)
                        if core and len(core) <= 60:
                            out[m.group(1)] = core
                        break
                break
        return out


def main() -> int:
    return HorseNameBackfill.main()


if __name__ == "__main__":
    sys.exit(main())

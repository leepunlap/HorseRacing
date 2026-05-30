#!/usr/bin/env python3
"""Bilingual jockey + trainer registry scraper, keyed by HKJC official IDs.

Source: HKJC's `Local` race-card endpoint, fetched in both languages:
  https://racing.hkjc.com/racing/info/Meeting/RaceCard/{lang}/Local/{YYYYMMDD}/{HV|ST}/{race_no}
where {lang} ∈ {English, Chinese}.

The HTML embeds a profile link per name, e.g.
  <a href="/zh-hk/local/information/jockeyprofile?jockeyid=WPN">黃寶妮 (-7)</a>
  <a href="/en/local/information/trainerprofile?trainerid=CAS">A S Cruz</a>
The 2-4 letter ID (`WPN`, `MHT`, `CAS`, …) is HKJC's permanent identifier
and is stable across name romanisations / Chinese-script variants. The
scraper extracts every (id, kind, language, name) anchor from both pages
and upserts merged rows into the `persons` table keyed by (hkjc_id, kind).

Idempotent. The English HTML lives under data/raw/race_card/ (same path as
scrape_race_card.py — sharable cache); Chinese HTML lives under
data/raw/race_card_zh/.

Usage:
    python3 -m scrapers.scrape_persons                 # all races in DB
    python3 -m scrapers.scrape_persons --date 2026-05-27
    python3 -m scrapers.scrape_persons --since 2025-09-01
    python3 -m scrapers.scrape_persons --force         # refetch (skip cache)
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup

from scrapers._base import BaseScraper, log


CLAIM_SUFFIX_RE = re.compile(r"\s*\(-?\d+\)\s*$")
# Match any href that carries jockeyid=XXX or trainerid=XXX (case-insensitive).
PROFILE_ID_RE = re.compile(r"(jockeyid|trainerid)=([A-Z0-9]+)", re.IGNORECASE)


def _strip_claim(name: str) -> str:
    """Drop the trailing '(-7)' apprentice-claim weight suffix."""
    return CLAIM_SUFFIX_RE.sub("", (name or "").strip()).strip()


def _extract_anchors(html: str) -> list[dict]:
    """Walk every anchor in the page; yield one dict per jockey/trainer
    profile link found.

    Returns: [{'kind': 'jockey'|'trainer', 'hkjc_id': 'WPN',
               'name': '黃寶妮' or 'P N Wong'}]
    """
    out = []
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        m = PROFILE_ID_RE.search(a["href"])
        if not m:
            continue
        kind = "jockey" if m.group(1).lower() == "jockeyid" else "trainer"
        hkjc_id = m.group(2).upper()
        name = _strip_claim(a.get_text())
        if not name:
            continue
        out.append({"kind": kind, "hkjc_id": hkjc_id, "name": name})
    return out


class PersonsScraper(BaseScraper):
    name = "race_card_zh"   # cache dir: data/raw/race_card_zh/

    # Future/today meetings live at the RaceCard URL; past meetings redirect
    # the RaceCard URL to an SPA shell with no embedded jockey/trainer IDs
    # but DO serve the data at the Results URL. We try RaceCard first and
    # fall back to Results when no profile anchors are found.
    URL_FMTS = (
        "https://racing.hkjc.com/racing/info/Meeting/RaceCard/{lang}/Local/{ymd}/{course}/{race_no}",
        "https://racing.hkjc.com/racing/info/Meeting/Results/{lang}/Local/{ymd}/{course}/{race_no}",
    )

    def run(self, args: list[str] | None = None) -> int:
        p = argparse.ArgumentParser()
        p.add_argument("--date", help="single race date YYYY-MM-DD")
        p.add_argument("--since", help="all dates >= YYYY-MM-DD")
        p.add_argument("--force", action="store_true",
                       help="ignore HTML cache (refetch live)")
        ns = p.parse_args(args)

        conn = self.db()
        where, params = [], []
        if ns.date:
            where.append("date = ?"); params.append(ns.date)
        if ns.since:
            where.append("date >= ?"); params.append(ns.since)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            f"SELECT DISTINCT date, course, race_no FROM races {where_sql} "
            f"ORDER BY date DESC, course, race_no", params,
        ).fetchall()
        if not rows:
            log(f"[{self.name}] no races match the filter")
            return 0

        # Accumulate (id, kind) -> {name_en, name_zh, last_seen} across the
        # whole run so we only upsert each person once at the end.
        merged: dict[tuple[str, str], dict] = {}
        races_touched = 0
        races_skipped = 0

        self.set_total(len(rows))
        for _i, (date, course, race_no) in enumerate(rows, 1):
            if self.should_stop():
                break
            self.progress(done=_i, msg=f'{date}/{course} R{race_no} ({len(merged)} persons)')
            ymd = date.replace("-", "")
            try:
                en_html = self._fetch_card("English", ymd, course, race_no,
                                           force=ns.force)
                zh_html = self._fetch_card("Chinese", ymd, course, race_no,
                                           force=ns.force)
            except Exception as exc:
                log(f"[{self.name}] {date} {course} R{race_no}: fetch failed ({exc})")
                races_skipped += 1
                continue
            races_touched += 1
            en_anchors = _extract_anchors(en_html)
            zh_anchors = _extract_anchors(zh_html)
            if not en_anchors and not zh_anchors:
                log(f"[{self.name}] {date} {course} R{race_no}: no profile links")
                continue
            for a in en_anchors:
                key = (a["hkjc_id"], a["kind"])
                rec = merged.setdefault(key, {})
                rec["name_en"] = a["name"]
                rec["last_seen"] = max(rec.get("last_seen", ""), date)
            for a in zh_anchors:
                key = (a["hkjc_id"], a["kind"])
                rec = merged.setdefault(key, {})
                rec["name_zh"] = a["name"]
                rec["last_seen"] = max(rec.get("last_seen", ""), date)
            log(f"[{self.name}] {date} {course} R{race_no}: "
                f"en={len(en_anchors)} zh={len(zh_anchors)} "
                f"distinct_so_far={len(merged)}")
            self.checkpoint({"last_date": date, "last_course": course,
                             "last_race_no": race_no})

        # Single batched upsert pass so partial language coverage (Chinese
        # fetch fails on one race but English succeeds on another) still
        # commits the rows we DO have for each ID.
        now_iso = datetime.now().isoformat()
        for (hkjc_id, kind), rec in merged.items():
            self.upsert(
                "persons",
                {
                    "hkjc_id": hkjc_id,
                    "kind": kind,
                    "name_en": rec.get("name_en"),
                    "name_zh": rec.get("name_zh"),
                    "last_seen": rec.get("last_seen"),
                    "updated_at": now_iso,
                },
                conflict_cols=("hkjc_id", "kind"),
            )
        conn.commit()
        log(f"[{self.name}] done — {len(merged)} unique persons across "
            f"{races_touched} races ({races_skipped} skipped)")
        return 0

    def _fetch_card(self, lang: str, ymd: str, course: str, race_no: int,
                    *, force: bool) -> str:
        """Fetch the per-race card HTML, trying both RaceCard (future
        meetings) and Results (past meetings) URL formats. Returns the
        first response that contains jockey/trainer profile anchors;
        falls back to the last fetched body if neither has them so the
        caller still gets something cacheable."""
        last_body = ""
        for i, fmt in enumerate(self.URL_FMTS):
            url = fmt.format(lang=lang, ymd=ymd, course=course, race_no=race_no)
            # Suffix the cache key with the URL index so RaceCard and
            # Results bodies don't collide.
            cache_key = f"{ymd}_{course}_r{race_no}_{lang.lower()}_v{i}"
            body = self.fetch(url, cache_key=cache_key, force_refresh=force)
            last_body = body
            if PROFILE_ID_RE.search(body):
                return body
        return last_body


if __name__ == "__main__":
    sys.exit(PersonsScraper.main())

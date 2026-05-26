#!/usr/bin/env python3
"""HKJC race-results scraper (v2 replacement for the deleted v1 script).

Fetches the LocalResults page for each race on a given meeting date and
populates:
  * `results`       — per-horse outcome (position, draw, weights, finish_time,
                      lbw, running positions string, win odds)
  * `dividends`     — per-pool payout (WIN / PLACE / QIN / QPL / TRIO / TRI /
                      F4 / QTT) with combination string and dividend amount
  * `race_history`  — mirror of `results` keyed by `brandno` for use as the
                      horse's training history in the feature pipeline
  * (best-effort) `sectionals` — race-level total time and the per-call
                      sectional splits parsed from the published "Sectional
                      Time" block when present

This is the gap that broke auto-update: without it, new races have a card +
predictions but never get settled outcomes. Pairs with `scrape_race_card`
(card before the race) and the in-process `odds_poller` (T-60→T-0 odds) to
close the data-ingest loop.

Source URL:
  https://racing.hkjc.com/racing/information/English/Racing/LocalResults.aspx
  ?RaceDate=YYYY/MM/DD&Racecourse=ST|HV&RaceNo=N

Usage:
  python3 -m scrapers.scrape_results --date 2026-05-30 --course ST
  python3 -m scrapers.scrape_results --date 2026-05-30        # both courses
  python3 -m scrapers.scrape_results --since 2026-05-25 --until 2026-05-30
  python3 -m scrapers.scrape_results --recent                  # last 30 days
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup

from scrapers._base import BaseScraper, log, txn, lookup_horse_id, lookup_race_id


BRAND_RE = re.compile(r"\(([A-Z]\d{3})\)")
TIME_RE = re.compile(r"^\d+:\d{2}\.\d{1,2}$|^\d+\.\d{1,2}$")  # 1:35.04 or 65.32
ODDS_RE = re.compile(r"^\d+(?:\.\d+)?$")
# Pool short-name normalisation (HKJC uses Chinese + abbreviations both)
POOL_MAP = {
    "WIN": "WIN", "獨贏": "WIN",
    "PLACE": "PLACE", "位置": "PLACE",
    "QUINELLA": "QIN", "QIN": "QIN", "連贏": "QIN",
    "QUINELLA PLACE": "QPL", "QPL": "QPL", "位置Q": "QPL", "位置 Q": "QPL",
    "TIERCE": "TRIO", "TRIO": "TRIO", "TRI": "TRI", "三重彩": "TRI", "單T": "TRIO",
    "TRIFECTA": "TRI",
    "FIRST 4": "F4", "F4": "F4", "四重彩": "F4",
    "QUARTET": "QTT", "QTT": "QTT", "四連環": "QTT",
}


def _parse_time(s: str) -> float | None:
    """1:35.04 -> 95.04 ; 65.32 -> 65.32"""
    s = (s or "").strip()
    if not s:
        return None
    if ":" in s:
        try:
            m, sec = s.split(":")
            return int(m) * 60 + float(sec)
        except (ValueError, TypeError):
            return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(s) -> int | None:
    try:
        return int(str(s).strip())
    except (ValueError, TypeError, AttributeError):
        return None


def _to_float(s) -> float | None:
    try:
        v = float(str(s).strip())
        return v if v == v else None  # nan check
    except (ValueError, TypeError, AttributeError):
        return None


class ResultsScraper(BaseScraper):
    name = "results"

    def run(self, args: list[str]) -> int:
        p = argparse.ArgumentParser(prog="scrape_results")
        p.add_argument("--date", help="YYYY-MM-DD (one meeting)")
        p.add_argument("--since", help="YYYY-MM-DD start (inclusive)")
        p.add_argument("--until", help="YYYY-MM-DD end (inclusive)")
        p.add_argument("--course", choices=["ST", "HV"], help="restrict to one course")
        p.add_argument("--recent", action="store_true", help="last 30 days")
        p.add_argument("--force-refresh", action="store_true",
                       help="re-parse even if results already exist")
        ns = p.parse_args(args)

        dates: list[str] = []
        if ns.recent:
            today = datetime.now().date()
            dates = [(today - timedelta(days=i)).isoformat() for i in range(30)]
        elif ns.since and ns.until:
            d0 = datetime.fromisoformat(ns.since).date()
            d1 = datetime.fromisoformat(ns.until).date()
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
        log(f"[{self.name}] done: {total_rows} result rows")
        return 0

    # ─── per-meeting orchestration ────────────────────────────────────────────
    def _scrape_meeting(self, date_str: str, course: str, force: bool) -> int:
        conn = self.db()
        races = conn.execute(
            "SELECT id, race_no FROM races WHERE date = ? AND course = ? ORDER BY race_no",
            (date_str, course),
        ).fetchall()
        if not races:
            return 0
        rows_added = 0
        for race_id, race_no in races:
            if self.should_stop():
                break
            if not force:
                already = conn.execute(
                    "SELECT 1 FROM results WHERE race_id = ? LIMIT 1", (race_id,)
                ).fetchone()
                if already:
                    continue
            try:
                rows_added += self._scrape_race(date_str, course, race_no, race_id)
            except Exception as exc:
                log(f"[{self.name}] {date_str}/{course}/R{race_no}: {exc}")
        return rows_added

    # ─── per-race fetch + parse + write ───────────────────────────────────────
    def _scrape_race(self, date_str: str, course: str, race_no: int, race_id: int) -> int:
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
        # HKJC shows "No Result" / nothing-yet pages — bail early on these.
        page_text = soup.get_text(" ", strip=True)
        if "Race Result" not in page_text and "Pla." not in page_text:
            return 0

        result_rows = self._parse_results_table(soup, date_str, course, race_no, race_id)
        # Build saddle-number → brand map from results so we can normalise the
        # dividend combinations (HKJC's live HTML uses saddle numbers; the v1
        # migration stored them as brand IDs).
        horse_no_to_brand = self._horse_no_brand_map(soup)
        dividend_rows = self._parse_dividends_table(
            soup, date_str, course, race_no, horse_no_to_brand,
        )
        sectionals = self._parse_sectionals(soup, race_id, date_str, course, race_no)

        rows_added = 0
        conn = self.db()
        with txn(conn):
            # `results` has no UNIQUE constraint (legacy v1 schema), so do a
            # clean delete-then-insert for this race. Safe because the caller
            # has already gated this with the "results already exist" check
            # above; we only reach here when populating fresh or --force-refresh.
            conn.execute("DELETE FROM results WHERE race_id = ?", (race_id,))
            for row in result_rows:
                cols = list(row.keys())
                conn.execute(
                    f"INSERT INTO results ({','.join(cols)}) "
                    f"VALUES ({','.join('?' for _ in cols)})",
                    [row[c] for c in cols],
                )
                rows_added += 1
            for row in dividend_rows:
                self.upsert(
                    "dividends", row,
                    conflict_cols=("date", "course", "race_no", "pool", "combination"),
                )
            # race_history mirror — used by the feature pipeline as horse history
            for row in result_rows:
                hist = {
                    "horse_id": row["horse_id"],
                    "brandno": row["brand"],
                    "age": None, "sex": None, "meetingcode": None,
                    "pla": row["position"],
                    "date": date_str,
                    "venue": course,
                    "distance": self._race_distance(race_id),
                    "going": self._race_going(race_id),
                    "class": self._race_class(race_id),
                    "draw": row["draw"],
                    "rating": None,
                    "trainercn": row["trainer"],
                    "jockeycn": row["jockey"],
                    "lbw": row["lbw"],
                    "odds": row["odds"],
                    "actwt": row["act_wt"],
                    "declwt": row["decl_wt"],
                    "running": row["running_style"],
                    "finishtime": row["finish_time"],
                    "gear": None,
                }
                # race_history has no UNIQUE constraint in the schema we ship,
                # so use plain INSERT. Skip duplicates by checking first.
                exists = conn.execute(
                    "SELECT 1 FROM race_history WHERE brandno = ? AND date = ? AND venue = ? LIMIT 1",
                    (hist["brandno"], date_str, course),
                ).fetchone()
                if not exists:
                    cols = list(hist.keys())
                    conn.execute(
                        f"INSERT INTO race_history ({','.join(cols)}) "
                        f"VALUES ({','.join('?' for _ in cols)})",
                        [hist[c] for c in cols],
                    )
            if sectionals:
                # `sectionals` table has no UNIQUE; replace any prior row for this race.
                conn.execute("DELETE FROM sectionals WHERE race_id = ?", (race_id,))
                conn.execute(
                    "INSERT INTO sectionals (race_id, date, course, race_no, distance, "
                    "total_time, splits, cumulatives, num_sections, early_pace, late_pace) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (race_id, date_str, course, race_no,
                     sectionals.get("distance"), sectionals.get("total_time"),
                     sectionals.get("splits"), sectionals.get("cumulatives"),
                     sectionals.get("num_sections"),
                     sectionals.get("early_pace"), sectionals.get("late_pace")),
                )
        log(f"[{self.name}] {date_str}/{course}/R{race_no}: "
            f"{len(result_rows)} horses, {len(dividend_rows)} dividends, "
            f"sectionals={'y' if sectionals else 'n'}")
        return rows_added

    # ─── parsers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _find_main_result_table(soup: BeautifulSoup):
        """Find the table that has the horse-result rows. HKJC's results page
        has several tables; the right one has columns 'Pla.', 'Horse No.',
        'Horse', etc."""
        for table in soup.find_all("table"):
            header_text = table.get_text(" ", strip=True)
            if "Pla." in header_text and "Horse" in header_text and "Jockey" in header_text:
                return table
        return None

    def _parse_results_table(self, soup, date_str, course, race_no, race_id):
        """Each row: position, horse_no, horse(brand), jockey, trainer, act_wt,
        decl_wt, draw, lbw, running_positions, finish_time, win_odds."""
        table = self._find_main_result_table(soup)
        if table is None:
            return []
        conn = self.db()
        rows: list[dict] = []
        seen_brands: set[str] = set()
        for tr in table.find_all("tr"):
            tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(tds) < 8:
                continue
            joined = " ".join(tds)
            brand_m = BRAND_RE.search(joined)
            if not brand_m:
                continue
            brand = brand_m.group(1)
            if brand in seen_brands:
                continue
            seen_brands.add(brand)
            # Find which cell contains the brand to locate columns reliably.
            # HKJC layout (Local Results, English):
            #   0:Pla. 1:HorseNo 2:Horse(brand) 3:Jockey 4:Trainer 5:ActWt
            #   6:DeclWt 7:Draw 8:LBW 9:Running 10:FinishTime 11:WinOdds
            try:
                position = self._safe_position(tds[0])
                horse_no = _to_int(tds[1])
                horse_name = tds[2].split("(")[0].strip()
                jockey = tds[3]
                trainer = tds[4]
                act_wt = _to_float(tds[5])
                decl_wt = _to_float(tds[6])
                draw = _to_int(tds[7])
                lbw = tds[8] if len(tds) > 8 else None
                running = tds[9] if len(tds) > 9 else None
                finish_time = _parse_time(tds[10]) if len(tds) > 10 else None
                odds = _to_float(tds[11]) if len(tds) > 11 else None
            except (IndexError, ValueError):
                continue
            row = {
                "race_id": race_id,
                "horse_id": lookup_horse_id(conn, brand),
                "date": date_str,
                "race_no": race_no,
                "course": course,
                "brand": brand,
                "horse_name": horse_name,
                "jockey": jockey,
                "trainer": trainer,
                "position": position,
                "draw": draw,
                "act_wt": act_wt,
                "decl_wt": decl_wt,
                "odds": odds,
                "finish_time": finish_time,
                "lbw": lbw,
                "running_style": running,
                "won": 1 if (position == 1 or position == "1") else 0,
            }
            rows.append(row)
        return rows

    @staticmethod
    def _safe_position(raw):
        """HKJC uses '1', '2', ... for finishers and 'WV', 'FE', 'PU', 'UR',
        'DQ', '---' for non-finishers. Return int for finishers, raw string
        for codes, None for blank."""
        s = (raw or "").strip()
        if not s or s in ("---", "--", "-"):
            return None
        if s.isdigit():
            return int(s)
        return s

    def _horse_no_brand_map(self, soup) -> dict[int, str]:
        """Map saddle-number → brand from the main results table. HKJC's
        dividend rows reference saddle numbers; downstream (results table,
        feature pipeline, predictions) all key on brand, so we translate."""
        out: dict[int, str] = {}
        table = self._find_main_result_table(soup)
        if table is None:
            return out
        for tr in table.find_all("tr"):
            tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(tds) < 3:
                continue
            brand_m = BRAND_RE.search(" ".join(tds))
            horse_no = _to_int(tds[1])
            if brand_m and horse_no is not None:
                out[horse_no] = brand_m.group(1)
        return out

    def _parse_dividends_table(self, soup, date_str, course, race_no,
                               horse_no_to_brand: dict[int, str]):
        """Dividend rows: Pool / Combination / Dividend. Walk every table and
        pick rows whose first cell matches a known pool name. Combinations are
        saddle-number form ("1,14"); translate to brand-form ("J003,K152")
        using `horse_no_to_brand` so the row matches the v1-migrated schema
        and joins cleanly to `results.brand`."""
        rows: list[dict] = []
        seen: set[tuple] = set()
        for table in soup.find_all("table"):
            for tr in table.find_all("tr"):
                tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                if len(tds) < 3:
                    continue
                pool = POOL_MAP.get(tds[0].upper().strip())
                if not pool:
                    continue
                combo = re.sub(r"\s+", "", tds[1].strip())
                if not combo:
                    continue
                parts_raw = [p for p in re.split(r"[,\-]", combo) if p]
                if not parts_raw:
                    continue
                # Two possible formats: saddle-numbers ("1,14") or brand IDs
                # ("J003,K152"). Convert numeric→brand via the lookup; reject
                # any row that has unresolvable saddle numbers (e.g. scratched).
                if all(p.isdigit() for p in parts_raw):
                    try:
                        brands = [horse_no_to_brand[int(p)] for p in parts_raw]
                    except KeyError:
                        continue
                elif all(re.match(r"^[A-Z]+\d{3,}$", p) for p in parts_raw):
                    brands = parts_raw
                else:
                    continue
                # Normalise: sort alphabetically for stable UNIQUE
                brands.sort()
                combo = ",".join(brands)
                div = _to_float(tds[2].replace(",", ""))
                if div is None:
                    continue
                key = (pool, combo)
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "date": date_str, "course": course, "race_no": race_no,
                    "pool": pool, "combination": combo, "dividend": div,
                })
        return rows

    def _parse_sectionals(self, soup, race_id, date_str, course, race_no):
        """Pull the race-level total time and per-call sectional times from
        the 'Sectional Time' block. The block looks like:
            Sectional Time:  13.05  22.74  35.04  47.45  59.91  1:11.84
        We split on whitespace, parse each token, and store splits +
        cumulatives. Returns {} if not present."""
        text = soup.get_text("\n", strip=True)
        m = re.search(r"Sectional\s*Time[s]?\s*[:：](.+?)(?:\n[A-Z]|$)", text, re.DOTALL | re.I)
        if not m:
            return {}
        body = m.group(1).strip()
        # First line typically has the sectional times for the winner
        first_line = body.split("\n")[0]
        tokens = re.split(r"\s+", first_line.strip())
        cumulatives: list[float] = []
        for tok in tokens:
            v = _parse_time(tok)
            if v is not None:
                cumulatives.append(v)
        if len(cumulatives) < 2:
            return {}
        # Cumulatives → splits
        splits = [cumulatives[0]] + [cumulatives[i] - cumulatives[i - 1]
                                     for i in range(1, len(cumulatives))]
        total_time = cumulatives[-1]
        # Early pace = first split, late pace = last split (proxies)
        early_pace = splits[0]
        late_pace = splits[-1]
        return {
            "distance": self._race_distance(race_id),
            "total_time": total_time,
            "splits": ",".join(f"{s:.2f}" for s in splits),
            "cumulatives": ",".join(f"{c:.2f}" for c in cumulatives),
            "num_sections": len(splits),
            "early_pace": early_pace,
            "late_pace": late_pace,
        }

    # ─── small accessors ──────────────────────────────────────────────────────
    def _race_distance(self, race_id: int) -> int | None:
        r = self.db().execute("SELECT distance FROM races WHERE id = ?",
                              (race_id,)).fetchone()
        return r[0] if r else None

    def _race_going(self, race_id: int) -> str | None:
        r = self.db().execute("SELECT going FROM races WHERE id = ?",
                              (race_id,)).fetchone()
        return r[0] if r else None

    def _race_class(self, race_id: int) -> str | None:
        r = self.db().execute("SELECT class FROM races WHERE id = ?",
                              (race_id,)).fetchone()
        return r[0] if r else None


if __name__ == "__main__":
    sys.exit(ResultsScraper.main())

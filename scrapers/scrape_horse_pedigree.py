#!/usr/bin/env python3
"""Horse pedigree + career-stats scraper.

For each brand seen in `results` / `race_history`, fetch HKJC's horse
profile page and extract every field the structured (label, ':', value)
table emits:

  horse_pedigree:  sire, dam, dam_sire, origin_country, import_type
  horses:          owner, trainer, total_stakes, season_stakes,
                   current_location, import_date, season_start_rating,
                   rating, starts, wins, seconds, thirds, age, sex, colour

Dosage Index (BloodHorse / Pedigree Online) is still deferred. Pedigree
alone unlocks Cat 1 features H001-H014 except H013 dosage. Career stats
unlock H001 (current form), H002 (career strike-rate proxy), and the
H047 trainer-density stuff once cross-referenced.

Usage:
    python3 -m scrapers.scrape_horse_pedigree --limit 100
    python3 -m scrapers.scrape_horse_pedigree --brand K289
    python3 -m scrapers.scrape_horse_pedigree --refresh   # rescan existing rows
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup

from scrapers._base import BaseScraper, log, txn, lookup_horse_id


_MONEY_RE = re.compile(r"[\d,]+")
_DATE_RE = re.compile(r"\d{2}/\d{2}/\d{4}")


def _money(s: str | None) -> float | None:
    """Parse '$8,160,275' or '58,450' into 8160275.0."""
    if not s:
        return None
    m = _MONEY_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _split_starts(s: str | None) -> tuple[int | None, int | None, int | None, int | None]:
    """'5-13-10-86' → (wins=5, seconds=13, thirds=10, starts=86)."""
    if not s:
        return (None, None, None, None)
    parts = s.strip().split("-")
    if len(parts) != 4:
        return (None, None, None, None)
    try:
        return tuple(int(p) for p in parts)  # type: ignore[return-value]
    except ValueError:
        return (None, None, None, None)


def _parse_profile_table(soup: BeautifulSoup) -> dict:
    """Walk the (label, ':', value) rows of HKJC's horse-profile tables and
    return a flat field-name -> string dict. Tolerates duplicate rows (HKJC
    repeats the block) and inline composite fields like 'NZ / 4' for
    'Country of Origin / Age'."""
    out: dict[str, str] = {}
    for t in soup.find_all("table"):
        for tr in t.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(cells) == 3 and cells[1] == ":" and 0 < len(cells[0]) < 60:
                # First occurrence wins; HKJC repeats the table inside an
                # outer wrapper but the duplicate is identical.
                out.setdefault(cells[0], cells[2])
    return out


class HorsePedigreeScraper(BaseScraper):
    name = "horse_pedigree"

    def run(self, args: list[str]) -> int:
        p = argparse.ArgumentParser(prog="scrape_horse_pedigree")
        p.add_argument("--limit", type=int, default=50, help="max horses per run")
        p.add_argument("--brand", help="single brand override")
        p.add_argument("--refresh", action="store_true",
                       help="rescan horses that already have a pedigree row "
                            "(picks up the extended career-stats columns)")
        ns = p.parse_args(args)

        conn = self.db()
        if ns.brand:
            brands = [ns.brand]
        elif ns.refresh:
            rows = conn.execute(
                "SELECT brand FROM horse_pedigree ORDER BY brand LIMIT ?",
                (ns.limit,),
            ).fetchall()
            brands = [r[0] for r in rows]
        else:
            rows = conn.execute(
                """
                SELECT DISTINCT h.brand
                FROM horses h
                LEFT JOIN horse_pedigree p ON p.brand = h.brand
                WHERE p.brand IS NULL
                ORDER BY h.brand
                LIMIT ?
                """,
                (ns.limit,),
            ).fetchall()
            brands = [r[0] for r in rows]

        log(f"[{self.name}] fetching pedigree for {len(brands)} horses")
        self.set_total(len(brands))
        done = 0
        for i, brand in enumerate(brands, 1):
            if self.should_stop():
                break
            try:
                if self._scrape_one(brand):
                    done += 1
                    if done % 10 == 0:
                        self.checkpoint({"last_brand": brand, "done": done})
            except Exception as exc:
                log(f"[{self.name}] {brand}: {exc}")
            self.progress(done=i, msg=brand)
        self.checkpoint({"done": done})
        log(f"[{self.name}] done: {done} pedigree records")
        return 0

    def _scrape_one(self, brand: str) -> bool:
        url = (
            "https://racing.hkjc.com/racing/information/English/Horse/Horse.aspx"
            f"?HorseNo={brand}"
        )
        body = self.fetch(url, cache_key=brand)
        if not body:
            return False
        soup = BeautifulSoup(body, "html.parser")
        fields = _parse_profile_table(soup)
        if not fields:
            return False

        # Pedigree-side fields go on horse_pedigree.
        sire = fields.get("Sire")
        dam = fields.get("Dam")
        dam_sire = fields.get("Dam's Sire")
        # Country and Age share a row on active horses ("NZ / 4"); retired
        # horses use the standalone "Country of Origin" key.
        country_age = fields.get("Country of Origin / Age", "")
        origin = (country_age.split("/")[0].strip()
                  if "/" in country_age
                  else fields.get("Country of Origin"))
        # Age may come from the composite row or the colour/sex row's
        # neighbour cell; rely on the composite when present.
        age_str = (country_age.split("/")[1].strip()
                   if country_age.count("/") == 1 else None)
        try:
            age = int(age_str) if age_str else None
        except ValueError:
            age = None
        import_type = fields.get("Import Type")
        import_date_raw = fields.get("Import Date") or ""
        import_date = (_DATE_RE.search(import_date_raw).group(0)
                       if _DATE_RE.search(import_date_raw) else None)

        # Career-stats side go on horses.
        owner = fields.get("Owner")
        trainer = fields.get("Trainer")
        total_stakes = _money(fields.get("Total Stakes*"))
        season_stakes = _money(fields.get("Season Stakes*"))
        # 'Hong Kong (07/11/2024)' → 'Hong Kong'
        loc_raw = fields.get("Current Location (Arrival Date)") or ""
        current_location = loc_raw.split("(")[0].strip() or None
        try:
            current_rating = int(fields["Current Rating"]) if "Current Rating" in fields else None
        except ValueError:
            current_rating = None
        try:
            season_start_rating = int(fields["Start of Season Rating"]) if "Start of Season Rating" in fields else None
        except ValueError:
            season_start_rating = None
        colour_sex = fields.get("Colour / Sex", "")
        colour = colour_sex.split("/")[0].strip() if "/" in colour_sex else None
        sex_full = colour_sex.split("/")[1].strip() if colour_sex.count("/") == 1 else None
        # HKJC: 'Gelding' → 'g', 'Horse' → 'h', 'Mare' → 'm', 'Colt' → 'c', 'Filly' → 'f'
        sex_map = {"Gelding": "g", "Horse": "h", "Mare": "m", "Colt": "c", "Filly": "f"}
        sex = sex_map.get(sex_full or "")
        wins, seconds, thirds, starts = _split_starts(fields.get("No. of 1-2-3-Starts*"))

        conn = self.db()
        horse_id = lookup_horse_id(conn, brand)
        pedigree_row = {
            "horse_id": horse_id,
            "brand": brand,
            "sire": sire,
            "dam": dam,
            "dam_sire": dam_sire,
            "origin_country": origin,
            "import_type": import_type,
        }
        # Drop None values from horse updates so we don't blank existing data
        # when one rescan can't parse a field. The race-card scraper writes
        # age/sex/colour too — only overwrite when we have a value.
        horse_updates = {
            "trainer": trainer,
            "owner": owner,
            "total_stakes": total_stakes,
            "season_stakes": season_stakes,
            "current_location": current_location,
            "import_date": import_date,
            "rating": current_rating,
            "season_start_rating": season_start_rating,
            "age": age,
            "sex": sex,
            "colour": colour,
            "starts": starts,
            "wins": wins,
            "seconds": seconds,
            "thirds": thirds,
        }
        horse_updates = {k: v for k, v in horse_updates.items() if v is not None}
        with txn(conn):
            self.upsert("horse_pedigree", pedigree_row, conflict_cols=("brand",))
            if horse_updates:
                sets = ", ".join(f"{k} = ?" for k in horse_updates)
                params = list(horse_updates.values()) + [brand]
                conn.execute(
                    "INSERT OR IGNORE INTO horses (brand) VALUES (?)", (brand,)
                )
                conn.execute(f"UPDATE horses SET {sets} WHERE brand = ?", params)
        log(f"[{self.name}] {brand}: sire={sire} dam={dam} origin={origin} "
            f"trainer={trainer} stakes={total_stakes}")
        return True


if __name__ == "__main__":
    sys.exit(HorsePedigreeScraper.main())

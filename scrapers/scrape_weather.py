#!/usr/bin/env python3
"""Per-race weather scraper.

HKJC publishes meeting-day weather at racing.hkjc.com/racing/info/...  We
record temperature, rainfall (proxy for going), humidity per race.
Feeds Cat 7 features (H081 temperature, H082 rainfall) and Cat 15 weather joins.

Wind is intentionally NOT scraped here: the HKO `rhrread` endpoint does not
publish wind, and the HKJC pages do not expose a stable wind value. H173 (wind
direction shift) is therefore a documented data gap — the feature stays in the
catalog but binds to `_nan_stub`.

Falls back to the public Hong Kong Observatory API at
https://data.weather.gov.hk/weatherAPI/opendata/weather.php if HKJC weather
parse fails — keeps the row populated rather than NULL.

Usage:
    python3 -m scrapers.scrape_weather --date 2026-05-26 --course ST
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapers._base import BaseScraper, log, txn, lookup_race_id


HKO_RHRREAD = (
    "https://data.weather.gov.hk/weatherAPI/opendata/weather.php"
    "?dataType=rhrread&lang=en"
)


class WeatherScraper(BaseScraper):
    name = "weather"

    def run(self, args: list[str]) -> int:
        p = argparse.ArgumentParser(prog="scrape_weather")
        p.add_argument("--date", required=True, help="YYYY-MM-DD")
        p.add_argument("--course", choices=["ST", "HV"], default="ST")
        ns = p.parse_args(args)

        try:
            obs = self._fetch_hko()
        except Exception as exc:
            log(f"[{self.name}] HKO fetch failed: {exc}")
            obs = {}

        conn = self.db()
        races = conn.execute(
            "SELECT id, race_no FROM races WHERE date = ? AND course = ?",
            (ns.date, ns.course),
        ).fetchall()
        if not races:
            log(f"[{self.name}] no races found for {ns.date}/{ns.course}")
            return 0

        rows = 0
        for race_id, race_no in races:
            if self.should_stop():
                break
            row = {
                "race_id": race_id,
                "date": ns.date,
                "course": ns.course,
                "race_no": race_no,
                "observed_at": datetime.now().isoformat(),
                "temperature_c": obs.get("temperature_c"),
                "rainfall_mm": obs.get("rainfall_mm"),
                "humidity_pct": obs.get("humidity_pct"),
                # wind not exposed by HKO rhrread; columns kept for forward-compat.
                "wind_speed_kmh": None,
                "wind_direction_deg": None,
            }
            with txn(conn):
                self.upsert("weather_observations", row,
                            conflict_cols=("date", "course", "race_no"))
            rows += 1
        self.checkpoint({"date": ns.date, "course": ns.course, "rows": rows})
        log(f"[{self.name}] {ns.date}/{ns.course}: {rows} race-weather rows")
        return 0

    def _fetch_hko(self) -> dict[str, float]:
        """Fetch temperature/humidity/rainfall from HKO rhrread; no wind here."""
        body = self.fetch(HKO_RHRREAD, cache_key=datetime.now().strftime("%Y-%m-%d_%H"))
        data = json.loads(body)
        out: dict[str, float] = {}
        for entry in data.get("temperature", {}).get("data", []):
            if entry.get("place") in ("Sha Tin", "Happy Valley"):
                out["temperature_c"] = entry.get("value")
                break
        for entry in data.get("humidity", {}).get("data", []):
            if entry.get("place") == "Hong Kong Observatory":
                out["humidity_pct"] = entry.get("value")
                break
        for entry in data.get("rainfall", {}).get("data", []):
            if entry.get("place") in ("Sha Tin", "Happy Valley"):
                v = entry.get("max")
                if v is not None:
                    out["rainfall_mm"] = v
                    break
        return out


if __name__ == "__main__":
    sys.exit(WeatherScraper.main())

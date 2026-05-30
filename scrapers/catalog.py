"""scrapers/catalog.py — bilingual descriptions + data-coverage metadata.

A single light-weight (no heavy imports) source of truth describing what each
scraper collects, which table it lands in, and how to compute its date range.
Used by the /api/scrapers/coverage endpoint and the dashboard "Data sources"
panel. Keyed by `slug`, which equals the scraper's `name` attribute — the same
token that appears in live task titles ("scraper: {slug}") so the dashboard can
join live status to the description.

datemode:
  'col'  → the table has a `date` column (MIN/MAX directly)
  'race' → no date column; derive it by joining races on race_id
  None   → an entity table with no meaningful date span (count only)
"""

from __future__ import annotations

# Order roughly by how central the source is to modelling.
SCRAPERS = [
    {"slug": "results", "table": "results", "datemode": "col",
     "en": "Official race results — finishing order, odds, beaten margins",
     "zh": "正式賽果 — 名次、賠率、距離差距"},
    {"slug": "race_card", "table": "races", "datemode": "col",
     "en": "Race cards — runners, draws, class, distance, going",
     "zh": "賽前排陣 — 出賽馬、檔位、班次、途程、場地"},
    {"slug": "incident_reports", "table": "incident_reports", "datemode": "race",
     "en": "Stewards' incident reports & running tags per runner",
     "zh": "賽事報告 — 每匹馬的事件與跑法標籤"},
    {"slug": "running_comments", "table": "running_comments", "datemode": "race",
     "en": "Stewards' running comments per runner (EN / 中)",
     "zh": "每匹馬跑法評語（中英）"},
    {"slug": "per_horse_sectionals", "table": "per_horse_sectionals", "datemode": "race",
     "en": "Per-horse sectional split times by furlong",
     "zh": "每匹馬逐段分段時間"},
    {"slug": "trackwork", "table": "trackwork", "datemode": "col",
     "en": "Morning trackwork & gallop records",
     "zh": "晨操練馬資料"},
    {"slug": "barrier_trials", "table": "barrier_trials", "datemode": "col",
     "en": "Barrier trial results & times",
     "zh": "試閘結果及時間"},
    {"slug": "vet_records", "table": "vet_records", "datemode": "col",
     "en": "Veterinary records — bleeding, lameness, clearances",
     "zh": "獸醫紀錄 — 出血、傷患、復賽許可"},
    {"slug": "roarers", "table": "vet_records", "datemode": "col", "where": "type='roarer'",
     "en": "Roarer (wind-surgery) declarations",
     "zh": "響馬（風喉手術）申報"},
    {"slug": "horse_pedigree", "table": "horse_pedigree", "datemode": None,
     "en": "Horse pedigree — sire/dam, dosage, career stats",
     "zh": "馬匹血統 — 父母系、配種指數、往績"},
    {"slug": "weather", "table": "weather_observations", "datemode": "col",
     "en": "Per-race weather — temperature, humidity, rainfall",
     "zh": "逐場天氣 — 氣溫、濕度、雨量"},
    {"slug": "dividends_backfill", "table": "dividends", "datemode": "col",
     "en": "Tote dividends (win/place/quinella/…) backfill",
     "zh": "彩池派彩（獨贏/位置/連贏…）回填"},
    {"slug": "multi_leg_dividends", "table": "multi_leg_dividends", "datemode": "col",
     "en": "Multi-leg pool dividends (Double / Treble / Six Up)",
     "zh": "多關彩池派彩（孖寶/三寶/六環彩）"},
    {"slug": "race_card_zh", "table": "persons", "datemode": None,
     "en": "Jockeys & trainers registry (bilingual names)",
     "zh": "騎師與練馬師名冊（中英對照）"},
    {"slug": "track_bias", "table": "track_bias_daily", "datemode": "col",
     "en": "Daily track-bias residuals — rail, pace, par-time",
     "zh": "每日跑道偏向殘差 — 欄位、步速、標準時間"},
    {"slug": "odds_poller", "table": "odds_snapshots", "datemode": "col",
     "en": "Live win-odds polling during race days",
     "zh": "賽日即時賠率輪詢"},
    {"slug": "backfill_horse_names", "table": "horses", "datemode": None,
     "en": "Backfill horse names onto the horses registry",
     "zh": "回填馬匹名稱至馬匹名冊"},
    {"slug": "backfill_horse_no", "table": "results", "datemode": "col",
     "where": "horse_no IS NOT NULL",
     "en": "Backfill saddle-cloth numbers onto results",
     "zh": "回填鞍號至賽果"},
]

BY_SLUG = {e["slug"]: e for e in SCRAPERS}


def coverage(conn) -> list[dict]:
    """For each catalog entry: record count + min/max date. Resilient to a
    missing table (returns records=None for that row)."""
    out = []
    for e in SCRAPERS:
        tbl, mode, where = e["table"], e["datemode"], e.get("where")
        wsql = f" WHERE {where}" if where else ""
        cnt = dmin = dmax = None
        try:
            if mode == "col":
                cnt, dmin, dmax = conn.execute(
                    f"SELECT COUNT(*), MIN(date), MAX(date) FROM {tbl}{wsql}").fetchone()
            elif mode == "race":
                cnt, dmin, dmax = conn.execute(
                    f"SELECT COUNT(*), MIN(r.date), MAX(r.date) "
                    f"FROM {tbl} t JOIN races r ON r.id = t.race_id{wsql}").fetchone()
            else:
                cnt = conn.execute(f"SELECT COUNT(*) FROM {tbl}{wsql}").fetchone()[0]
        except Exception:
            cnt = dmin = dmax = None
        out.append({
            "slug": e["slug"], "en": e["en"], "zh": e["zh"], "table": tbl,
            "records": cnt, "date_min": dmin, "date_max": dmax,
        })
    return out

#!/usr/bin/env python3
"""
HKJC results scraper — scrapes race results and metadata for one or more dates.

Usage:
    python3 scrape_results.py 2026-05-04
    python3 scrape_results.py 2026-05-04 2026-05-07 2026-05-11
    python3 scrape_results.py --from 2026-05-01 --to 2026-05-21
"""

import asyncio, os, re, sys, io, sqlite3, argparse, json, signal
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
from datetime import datetime, timedelta
from pathlib import Path
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).parent
DB_PATH  = BASE_DIR / "data" / "racing.db"
PRED_DIR = BASE_DIR / "predictions"

GOING_NORM = {
    '好地': 'Good', '好': 'Good', '好地至黏地': 'Good to Yielding',
    '黏地': 'Yielding', '黏地至軟地': 'Yielding to Soft', '軟地': 'Soft',
    '硬地': 'Firm', 'Good': 'Good', 'Yielding': 'Yielding',
}


def date_range(start: str, end: str):
    d = datetime.strptime(start, '%Y-%m-%d')
    e = datetime.strptime(end,   '%Y-%m-%d')
    while d <= e:
        yield d.strftime('%Y-%m-%d')
        d += timedelta(days=1)


def parse_race_meta(soup):
    """Extract distance, class, going, race name from a results page."""
    meta = {'distance': None, 'class': None, 'going': None, 'race_name': None}
    text = soup.get_text(' ', strip=True)

    # Distance: look for patterns like 1200米, 1400米, etc.
    m = re.search(r'(\d{3,4})\s*米', text)
    if m:
        meta['distance'] = int(m.group(1))

    # Class: 第一班, 第二班, 第三班, 第四班, 第五班, 香港打吡 (G1), etc.
    class_patterns = [
        (r'第一班',   '1'), (r'第二班', '2'), (r'第三班', '3'),
        (r'第四班',   '4'), (r'第五班', '5'),
        (r'一級賽|G1', 'G1'), (r'二級賽|G2', 'G2'), (r'三級賽|G3', 'G3'),
        (r'Listed',   'Listed'),
    ]
    for pattern, cls in class_patterns:
        if re.search(pattern, text):
            meta['class'] = cls
            break

    # Going: scan for known going strings
    for cn, en in GOING_NORM.items():
        if cn in text:
            meta['going'] = en
            break

    # Race name: look for the race title element
    for tag in soup.find_all(['h2', 'h3', 'div', 'span', 'td']):
        t = tag.get_text(strip=True)
        if len(t) > 4 and any(kw in t for kw in ['盃', '錦標', '短途', '打吡', '冠軍', '賽', 'Trophy', 'Cup', 'Stakes', 'Handicap']):
            meta['race_name'] = t[:80]
            break

    return meta


def parse_race_results(soup, date, race_no, course):
    """Parse horse result rows from a results page.

    HKJC column layout (fixed):
      [0] 名次  position
      [1] 馬號  horse number
      [2] 馬名  horse name (Brand)
      [3] 騎師  jockey
      [4] 練馬師 trainer
      [5] 實際負磅 actual carried weight (lbs)
      [6] 排位體重 declared horse body weight (lbs) — NOT odds
      [7] 檔位  draw
      [8] 頭馬距離 lengths behind winner
      [9] 沿途走位 running positions
      [10] 完成時間 finish time
      [11] 獨贏賠率 win odds
    """
    tables = soup.find_all('table', class_=re.compile(r'table_bd|bigborder'))
    if not tables:
        tables = soup.find_all('table')

    results = []
    for table in tables:
        for row in table.find_all('tr'):
            cells = row.find_all('td')
            if len(cells) < 10:
                continue
            pos_text = cells[0].get_text(strip=True)
            if not re.match(r'^\d+$', pos_text):
                continue

            try:
                texts = [c.get_text(strip=True) for c in cells]
                position = int(texts[0])

                # Extract brand from horse name cell (col 2)
                m = re.search(r'\(([A-Z]\d+)\)', texts[2])
                if not m:
                    continue
                brand      = m.group(1)
                horse_name = re.sub(r'\s*\([A-Z]\d+\)', '', texts[2]).strip()
                jockey     = texts[3]
                trainer    = texts[4]
                act_wt     = texts[5]
                draw       = texts[7]
                lbw_raw    = texts[8]
                running    = texts[9]  if len(texts) > 9  else ''
                finish_time= texts[10] if len(texts) > 10 else ''
                odds       = texts[11] if len(texts) > 11 else ''

                # Normalise lbw: winner has '---', others have fractional lengths
                lbw = '' if lbw_raw in ('---', '-', '') else lbw_raw

                results.append({
                    'date': date, 'race_no': race_no, 'course': course,
                    'brand': brand, 'horse_name': horse_name,
                    'jockey': jockey, 'trainer': trainer,
                    'position': position, 'draw': draw,
                    'act_wt': act_wt, 'odds': odds,
                    'finish_time': finish_time, 'lbw': lbw,
                    'running_style': running,
                    'won': 1 if position == 1 else 0,
                })
            except Exception as e:
                print(f'    row parse error: {e}')

        if results:
            break  # stop after first table with valid rows

    return results


async def scrape_date(page, date: str, dry_run: bool = False):
    """Scrape all races for a given date. Auto-detects course (ST or HV)."""
    date_url = date.replace('-', '/')
    all_results = []
    race_metas  = []
    course_used = None

    for course in ('ST', 'HV'):
        print(f'  Trying {course}...')
        found_any = False

        for rn in range(1, 13):
            url = (f'https://racing.hkjc.com/racing/information/Chinese/Racing/'
                   f'LocalResults.aspx?RaceDate={date_url}&Racecourse={course}&RaceNo={rn}')
            try:
                await page.goto(url, timeout=25000)
                await page.wait_for_timeout(1500)
            except Exception as e:
                print(f'    R{rn}: load failed — {e}')
                continue

            html = await page.content()
            soup = BeautifulSoup(html, 'html.parser')

            # Detect "no results" page
            page_text = soup.get_text(' ', strip=True)
            if '沒有賽事資料' in page_text or '沒有結果' in page_text or 'No Race' in page_text:
                if rn == 1:
                    break  # wrong course or no race day
                break  # end of races for this meeting

            results = parse_race_results(soup, date, rn, course)
            if not results and rn == 1:
                # No results on race 1 — likely wrong course
                break
            if not results:
                break  # end of races for this course

            found_any = True
            meta = parse_race_meta(soup)
            meta.update({'date': date, 'course': course, 'raceno': rn,
                         'participants': len(results)})
            race_metas.append(meta)
            all_results.extend(results)
            print(f'    R{rn}: {len(results)} horses  dist={meta["distance"]}  '
                  f'class={meta["class"]}  going={meta["going"]}')

            # Save raw HTML for reference
            if not dry_run:
                out_dir = PRED_DIR / date
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / f'result_{course}_R{rn:02d}_{date}.html').write_text(html, encoding='utf-8')

        if found_any:
            course_used = course
            break  # don't try second course

    return course_used, race_metas, all_results


def upsert_race(cursor, r):
    existing = cursor.execute(
        "SELECT rowid FROM races WHERE date=? AND course=? AND raceno=?",
        (r['date'], r['course'], r['raceno'])
    ).fetchone()
    if existing:
        cursor.execute("""
            UPDATE races SET distance=?, class=?, going=?, participants=?
            WHERE date=? AND course=? AND raceno=?
        """, (r['distance'], r['class'], r['going'], r['participants'],
              r['date'], r['course'], r['raceno']))
    else:
        cursor.execute("""
            INSERT INTO races (date, course, raceno, distance, class, going, participants)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (r['date'], r['course'], r['raceno'], r['distance'], r['class'],
              r['going'], r['participants']))


def upsert_result(cursor, r):
    existing = cursor.execute(
        "SELECT rowid FROM results WHERE date=? AND race_no=? AND brand=?",
        (r['date'], r['race_no'], r['brand'])
    ).fetchone()
    if existing:
        cursor.execute("""
            UPDATE results SET position=?, draw=?, act_wt=?, odds=?, lbw=?,
                running_style=?, finish_time=?, won=?, horse_name=?, jockey=?, trainer=?
            WHERE date=? AND race_no=? AND brand=?
        """, (r['position'], r['draw'], r['act_wt'], r['odds'], r['lbw'],
              r['running_style'], r['finish_time'], r['won'],
              r['horse_name'], r['jockey'], r['trainer'],
              r['date'], r['race_no'], r['brand']))
    else:
        cursor.execute("""
            INSERT INTO results (date, race_no, course, brand, horse_name, jockey, trainer,
                position, draw, act_wt, odds, finish_time, lbw, running_style, won)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (r['date'], r['race_no'], r['course'], r['brand'], r['horse_name'],
              r['jockey'], r['trainer'], r['position'], r['draw'], r['act_wt'],
              r['odds'], r['finish_time'], r['lbw'], r['running_style'], r['won']))


def ensure_prediction_stub(date: str):
    """Create a minimal racecard_parsed.json stub so the date appears in the dropdown."""
    pred_path = PRED_DIR / date / 'racecard_parsed.json'
    if not pred_path.exists():
        pred_path.parent.mkdir(parents=True, exist_ok=True)
        pred_path.write_text('{}', encoding='utf-8')
        print(f'  Created stub: {pred_path}')


async def main():
    parser = argparse.ArgumentParser(description='Scrape HKJC race results into the DB.')
    parser.add_argument('dates', nargs='*', metavar='YYYY-MM-DD',
                        help='One or more dates to scrape')
    parser.add_argument('--from', dest='date_from', metavar='YYYY-MM-DD',
                        help='Start of date range')
    parser.add_argument('--to', dest='date_to', metavar='YYYY-MM-DD',
                        help='End of date range (inclusive)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Parse and print without writing to DB')
    args = parser.parse_args()

    dates = list(args.dates)
    if args.date_from and args.date_to:
        dates += list(date_range(args.date_from, args.date_to))
    if not dates:
        parser.print_help()
        sys.exit(1)

    dates = sorted(set(dates))
    print(f'Scraping {len(dates)} date(s): {", ".join(dates)}')

    conn = sqlite3.connect(DB_PATH) if not args.dry_run else None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page    = await browser.new_page()

        for date in dates:
            print(f'\n=== {date} ===')
            try:
                course, race_metas, results = await scrape_date(page, date, args.dry_run)
            except Exception as e:
                print(f'  ERROR: {e}')
                continue

            if not results:
                print(f'  No results found — skipping (not a race day, or results not yet available)')
                continue

            print(f'  Course: {course}  Races: {len(race_metas)}  Horses: {len(results)}')

            if not args.dry_run:
                cursor = conn.cursor()
                for rm in race_metas:
                    upsert_race(cursor, rm)
                for r in results:
                    upsert_result(cursor, r)
                conn.commit()
                ensure_prediction_stub(date)
                print(f'  Saved to DB.')

        await browser.close()

    if conn:
        conn.close()

    print('\nDone.')


asyncio.run(main())

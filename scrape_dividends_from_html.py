#!/usr/bin/env python3
"""
Extract dividends from already-downloaded result HTML pages and store in DB.

This processes HTML files under predictions/*/result_*.html that were saved
by scrape_results.py but whose payout/dividend data was never parsed.

Usage:
    python3 scrape_dividends_from_html.py                    # all dates
    python3 scrape_dividends_from_html.py 2026-05-24         # one date
    python3 scrape_dividends_from_html.py --dry-run          # print only
"""

import sys, os, re, sqlite3, argparse
from pathlib import Path
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).parent
DB_PATH  = BASE_DIR / "data" / "racing.db"
PRED_DIR = BASE_DIR / "predictions"

TABLE_POOLS = {'獨贏': 'WIN', '位置': 'PLACE', '連贏': 'QIN',
               '位置Q': 'QPL', '二重彩': 'EXA', '單T': 'TRIO',
               '三重彩': 'TRI', '四連環': 'F4', '四重彩': 'QTT'}


def parse_dividends_from_html(html_path: Path) -> list:
    """Extract dividend records from a single results HTML file."""
    soup = BeautifulSoup(html_path.read_text(encoding='utf-8'), 'html.parser')

    fname = html_path.stem
    m = re.match(r'result_(\w+)_R(\d+)_(\d{4}-\d{2}-\d{2})', fname)
    if not m:
        return []
    course  = m.group(1)
    race_no = int(m.group(2))
    date    = m.group(3)

    # Build horse_no -> brand mapping from the results table in the same HTML
    horse_no_to_brand = {}
    for table in soup.find_all('table', class_=re.compile(r'table_bd|bigborder')):
        if not table:
            continue
        tables = [table]
        if not tables:
            tables = soup.find_all('table')
        for t in tables:
            for row in t.find_all('tr'):
                cells = row.find_all('td')
                if len(cells) < 10:
                    continue
                if not re.match(r'^\d+$', cells[0].get_text(strip=True)):
                    continue
                texts = [c.get_text(strip=True) for c in cells]
                if len(texts) < 3:
                    continue
                horse_no_text = texts[1] if len(texts) > 1 else ''
                if not horse_no_text.isdigit():
                    continue
                horse_no = int(horse_no_text)
                brand_m = re.search(r'\(([A-Z]\d+)\)', texts[2])
                if brand_m:
                    horse_no_to_brand[horse_no] = brand_m.group(1)
        if horse_no_to_brand:
            break

    dividends = []

    for table in soup.find_all('table'):
        rows = table.find_all('tr')
        payout_section = False
        current_pool = None

        for row in rows:
            cells = row.find_all(['td', 'th'])
            texts = [c.get_text(strip=True) for c in cells]
            if not texts:
                continue

            if any('派彩' in t for t in texts):
                payout_section = True
                continue
            if not payout_section:
                continue
            if any(t in texts for t in ['彩池', '勝出組合']):
                continue

            pool_cn = texts[0] if texts else ''
            if pool_cn in TABLE_POOLS:
                current_pool = TABLE_POOLS[pool_cn]
                combination = texts[1] if len(texts) > 1 else ''
                payout_str   = texts[2] if len(texts) > 2 else ''
            elif current_pool and len(texts) >= 2:
                combination = texts[0] if len(texts) > 0 else ''
                payout_str   = texts[1] if len(texts) > 1 else ''
            else:
                continue

            if not current_pool or not combination or not payout_str:
                continue
            try:
                dividend = float(payout_str.replace(',', ''))
            except ValueError:
                continue

            parts = re.findall(r'\d+', combination)
            if not parts:
                continue
            # Convert horse numbers to brands where possible
            parts_mapped = []
            for p in parts:
                pn = int(p)
                parts_mapped.append(horse_no_to_brand.get(pn, str(pn)))
            parts_sorted = ','.join(sorted(parts_mapped))

            dividends.append({
                'date': date, 'course': course, 'race_no': race_no,
                'pool': current_pool, 'combination': parts_sorted,
                'dividend': dividend,
            })

    return dividends


def main():
    parser = argparse.ArgumentParser(
        description='Extract dividends from existing result HTML files.')
    parser.add_argument('dates', nargs='*', help='Specific dates to process')
    parser.add_argument('--dry-run', action='store_true',
                        help='Parse and print without writing to DB')
    args = parser.parse_args()

    if args.dates:
        date_dirs = [PRED_DIR / d for d in args.dates]
    else:
        date_dirs = sorted(
            d for d in PRED_DIR.iterdir()
            if d.is_dir() and re.match(r'\d{4}-\d{2}-\d{2}$', d.name)
        )

    conn = None if args.dry_run else sqlite3.connect(str(DB_PATH))
    total_divs = 0
    total_files = 0

    for date_dir in date_dirs:
        if not date_dir.is_dir():
            print(f'  {date_dir.name}: not found — skip')
            continue

        html_files = sorted(date_dir.glob('result_*.html'))
        if not html_files:
            continue

        date_divs = 0
        for html_path in html_files:
            divs = parse_dividends_from_html(html_path)
            if not divs:
                continue
            total_files += 1
            date_divs += len(divs)
            total_divs += len(divs)

            if not args.dry_run:
                cursor = conn.cursor()
                for d in divs:
                    existing = cursor.execute(
                        "SELECT rowid FROM dividends WHERE date=? AND course=? "
                        "AND race_no=? AND pool=? AND combination=?",
                        (d['date'], d['course'], d['race_no'],
                         d['pool'], d['combination'])
                    ).fetchone()
                    if existing:
                        cursor.execute(
                            "UPDATE dividends SET dividend=? WHERE date=? "
                            "AND course=? AND race_no=? AND pool=? AND combination=?",
                            (d['dividend'], d['date'], d['course'],
                             d['race_no'], d['pool'], d['combination']))
                    else:
                        cursor.execute(
                            "INSERT INTO dividends (date, course, race_no, "
                            "pool, combination, dividend) VALUES (?,?,?,?,?,?)",
                            (d['date'], d['course'], d['race_no'],
                             d['pool'], d['combination'], d['dividend']))

        if date_divs:
            action = 'parsed' if args.dry_run else 'saved'
            print(f"  {date_dir.name}: {len(html_files)} races, "
                  f"{date_divs} dividends {action}")

        if not args.dry_run:
            conn.commit()

    if conn:
        conn.close()

    if not args.dry_run:
        print(f"\nDone: {total_divs} dividends from {total_files} files.")
        c = sqlite3.connect(str(DB_PATH)).execute(
            "SELECT COUNT(*) FROM dividends").fetchone()[0]
        print(f"Total dividends in DB: {c}")
    else:
        print(f"\nDry run: {total_divs} dividends from {total_files} files.")


if __name__ == '__main__':
    main()

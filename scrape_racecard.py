#!/usr/bin/env python3
"""
HKJC race card scraper — scrapes entry list for one or more dates.

Usage:
    python3 scrape_racecard.py 2026-05-28
    python3 scrape_racecard.py --next           # auto-detect next race date
    python3 scrape_racecard.py --from 2026-05-28 --to 2026-06-01
"""

import asyncio, re, sys, io, json, argparse, sqlite3, signal
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Graceful shutdown on SIGTERM (sent by the job manager's proc.terminate()).
# Raising SystemExit here lets asyncio.run() unwind all async context managers,
# which causes async_playwright().__aexit__ to close the browser cleanly before
# the process exits — preventing the Playwright EPIPE pipe-broken noise.
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
from datetime import datetime, timedelta
from pathlib import Path
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).parent
PRED_DIR = BASE_DIR / "predictions"
DB_PATH  = BASE_DIR / "data" / "racing.db"

# Maps sex characters in horse name to canonical form
SEX_MAP = {'閹': '閹', '牡': '公', '公': '公', '牝': '牝', '母': '牝'}

# Gear token normalisation (strip trailing digits for display)
def _norm_gear(raw: str) -> str:
    return raw.strip()


def date_range(start: str, end: str):
    d = datetime.strptime(start, '%Y-%m-%d')
    e = datetime.strptime(end,   '%Y-%m-%d')
    while d <= e:
        yield d.strftime('%Y-%m-%d')
        d += timedelta(days=1)


def next_race_dates(n: int = 3) -> list[str]:
    """Return up to n upcoming Wed/Sun dates from today."""
    today = datetime.now()
    dates = []
    for i in range(1, 21):
        d = today + timedelta(days=i)
        if d.weekday() in (2, 6):   # Wed=2, Sun=6
            dates.append(d.strftime('%Y-%m-%d'))
            if len(dates) >= n:
                break
    return dates


def _extract_sex(name_text: str) -> str:
    """Extract sex code from horse name cell (e.g. '閹' at start or end)."""
    for ch, canonical in SEX_MAP.items():
        if ch in name_text:
            return canonical
    return ''


def _parse_age_sex(cell_text: str) -> tuple[str, str]:
    """Parse combined age/sex cell like '4閹' or '閹4' or just '5'."""
    m = re.match(r'^(\d+)([閹牡公牝母]?)$', cell_text.strip())
    if m:
        age = m.group(1)
        sex_ch = m.group(2)
        return age, SEX_MAP.get(sex_ch, '')
    m2 = re.match(r'^([閹牡公牝母])(\d+)$', cell_text.strip())
    if m2:
        return m2.group(2), SEX_MAP.get(m2.group(1), '')
    # digits only
    m3 = re.match(r'^(\d+)$', cell_text.strip())
    if m3:
        return m3.group(1), ''
    return cell_text.strip(), ''


def _days_since(date_str: str, race_date: str) -> str:
    """Calculate days between last_race_date and race_date."""
    try:
        last = datetime.strptime(date_str, '%Y-%m-%d')
        rd   = datetime.strptime(race_date, '%Y-%m-%d')
        return str((rd - last).days)
    except Exception:
        return ''


def parse_entry_table(soup, race_date: str) -> list[dict]:
    """
    Detect and parse the entry list table from an HKJC race card page.
    HKJC Chinese race card table columns (typical order):
      0: 馬號   horse number
      1: 馬名   horse name (Chinese) + brand (A123) in brackets
      2: 騎師   jockey
      3: 練馬師 trainer
      4: 負磅   carried weight (lbs)
      5: 檔位   draw
      6: 評分   official rating
      7: 年齡   age (sometimes age+sex combined)
      8: 性別   sex (sometimes merged with age)
      9: 上次出賽 last race date  OR  days_since
     10: 裝備   gear/equipment string
     11: 馬匹體重  body weight (lbs)
     12: 體重增減  weight change (+/-)
    Odds columns (13+) may or may not be present.
    """
    # Find header row to detect column positions
    col_map = {}    # header_text → col_idx
    entry_tables = []

    for table in soup.find_all('table'):
        headers = []
        first_header_row = None
        for row in table.find_all('tr'):
            cells = row.find_all(['th', 'td'])
            texts = [c.get_text(strip=True) for c in cells]
            # Identify header row by known column names
            if any(t in ('馬號', '馬名', '騎師', '負磅', '檔位') for t in texts):
                headers = texts
                first_header_row = row
                break

        if not headers:
            continue

        # Build column index map
        cmap = {}
        for i, h in enumerate(headers):
            for key, variants in {
                'no':       ('馬號',),
                'name':     ('馬名', '英文馬名'),
                'jockey':   ('騎師',),
                'trainer':  ('練馬師',),
                'weight':   ('負磅', '磅'),
                'draw':     ('檔位', '檔'),
                'rating':   ('評分',),
                'age':      ('年齡', '齡'),
                'sex':      ('性別',),
                'last_race':('上次出賽', '上次出賽日期'),
                'gear':     ('裝備',),
                'body_wt':  ('馬匹體重', '體重'),
                'body_chg': ('體重增減', '增減'),
                'win_odds': ('獨贏賠率', '獨贏', '賠率'),
                'plc_odds': ('位置賠率', '位置'),
            }.items():
                if h in variants and key not in cmap:
                    cmap[key] = i

        if not cmap.get('no') and not cmap.get('name'):
            continue   # not the entry table

        entry_tables.append((table, cmap))

    if not entry_tables:
        return []

    table, cmap = entry_tables[0]
    horses = []
    in_data = False

    for row in table.find_all('tr'):
        cells = row.find_all('td')
        if not cells:
            continue

        # Skip until we pass the header row
        texts = [c.get_text(strip=True) for c in cells]
        if any(t in ('馬號', '騎師', '負磅', '檔位') for t in texts):
            in_data = True
            continue
        if not in_data:
            continue

        # First cell must be a horse number
        no_idx = cmap.get('no', 0)
        if no_idx >= len(texts):
            continue
        if not re.match(r'^\d+$', texts[no_idx]):
            continue

        def _get(key, default=''):
            idx = cmap.get(key)
            if idx is None or idx >= len(texts):
                return default
            return texts[idx].strip()

        no = _get('no')

        # Horse name: Chinese name + brand like (K152)
        name_raw = _get('name')
        m_brand = re.search(r'\(([A-Z]\d+)\)', name_raw)
        brand      = m_brand.group(1) if m_brand else ''
        horse_name = re.sub(r'\s*\([A-Z]\d+\)', '', name_raw).strip()

        # Sex may be embedded in name or separate
        sex = _get('sex')
        if not sex:
            sex = _extract_sex(name_raw)

        # Age (sometimes merged with sex in same cell)
        age_raw = _get('age')
        age, sex2 = _parse_age_sex(age_raw)
        if not sex and sex2:
            sex = sex2

        # Days since last race
        last_raw = _get('last_race')
        if re.match(r'^\d{4}-\d{2}-\d{2}$', last_raw):
            days_since = _days_since(last_raw, race_date)
        elif re.match(r'^\d+$', last_raw):
            days_since = last_raw   # already days
        else:
            days_since = ''

        # Body weight + change (sometimes combined: "1022(-1)" or "1022 -1")
        body_wt_raw = _get('body_wt')
        body_chg_raw = _get('body_chg')
        bw_m = re.match(r'^(\d+)\s*([+-]\d+)?$', body_wt_raw.replace('(', ' ').replace(')', ''))
        if bw_m:
            body_wt  = bw_m.group(1)
            body_chg = bw_m.group(2) or body_chg_raw
        else:
            body_wt  = body_wt_raw
            body_chg = body_chg_raw

        horses.append({
            'no':          no,
            'name':        horse_name,
            'brand':       brand,
            'weight':      _get('weight'),
            'jockey':      _get('jockey'),
            'draw':        _get('draw'),
            'trainer':     _get('trainer'),
            'rating':      _get('rating'),
            'age':         age,
            'sex':         sex,
            'days_since':  days_since,
            'gear':        _norm_gear(_get('gear')),
            'body_wt':     body_wt,
            'body_wt_chg': body_chg,
            'win_odds':    _get('win_odds'),
            'place_odds':  _get('plc_odds'),
        })

    return horses


def parse_race_info(soup) -> dict:
    """Extract distance, class, going from race card page."""
    info = {'distance': '', 'class': '', 'going': ''}
    text = soup.get_text(' ', strip=True)

    m = re.search(r'(\d{3,4})\s*米', text)
    if m:
        info['distance'] = m.group(1)

    for pattern, cls in [
        (r'第一班', '1'), (r'第二班', '2'), (r'第三班', '3'),
        (r'第四班', '4'), (r'第五班', '5'),
        (r'一級賽|G1', 'G1'), (r'二級賽|G2', 'G2'), (r'三級賽|G3', 'G3'),
    ]:
        if re.search(pattern, text):
            info['class'] = cls
            break

    for cn, en in [('好地至黏地', 'Good to Yielding'), ('好地', 'Good'),
                   ('黏地', 'Yielding'), ('軟地', 'Soft')]:
        if cn in text:
            info['going'] = en
            break

    return info


async def scrape_date(page, date: str) -> tuple[str | None, dict]:
    """Scrape all race entries for a date. Returns (course, races_dict)."""
    date_url = date.replace('-', '/')
    races    = {}

    for course in ('ST', 'HV'):
        found_any = False

        for rn in range(1, 13):
            url = (
                'https://racing.hkjc.com/racing/information/Chinese/Racing/'
                f'RaceCard.aspx?RaceDate={date_url}&Racecourse={course}&RaceNo={rn}'
            )
            try:
                await page.goto(url, timeout=30000)
                await page.wait_for_timeout(2500)
            except Exception as exc:
                print(f'    R{rn:02d}: load error — {exc}')
                continue

            html = await page.content()
            soup = BeautifulSoup(html, 'html.parser')
            page_text = soup.get_text(' ', strip=True)

            if any(x in page_text for x in ('沒有賽事資料', '沒有結果', 'No Race', '沒有賽馬')):
                if rn == 1:
                    break   # wrong course or no meeting
                break       # end of races

            horses = parse_entry_table(soup, date)
            if not horses:
                if rn == 1:
                    break
                break

            found_any = True
            info = parse_race_info(soup)
            rno  = f'{rn:02d}'
            races[rno] = {
                'distance': info['distance'],
                'class':    info['class'],
                'going':    info.get('going', ''),
                'course':   course,
                'horses':   horses,
            }
            print(f'    R{rn:02d}: {len(horses)} entries  '
                  f'dist={info["distance"]}m  class={info["class"]}')

        if found_any:
            return course, races

    return None, {}


async def main():
    parser = argparse.ArgumentParser(description='Scrape HKJC race entry lists.')
    parser.add_argument('dates', nargs='*', metavar='YYYY-MM-DD',
                        help='Specific dates to scrape')
    parser.add_argument('--next', action='store_true',
                        help='Scrape the next upcoming race date')
    parser.add_argument('--from', dest='date_from', metavar='YYYY-MM-DD')
    parser.add_argument('--to',   dest='date_to',   metavar='YYYY-MM-DD')
    parser.add_argument('--overwrite', action='store_true',
                        help='Re-scrape even if racecard_parsed.json already exists')
    args = parser.parse_args()

    dates = list(args.dates)
    if args.next:
        upcoming = next_race_dates(3)
        print(f'Next race dates: {upcoming}')
        dates += upcoming
    if args.date_from and args.date_to:
        dates += list(date_range(args.date_from, args.date_to))

    if not dates:
        parser.print_help()
        sys.exit(1)

    dates = sorted(set(dates))
    print(f'Scraping race cards for {len(dates)} date(s): {", ".join(dates)}')

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page    = await browser.new_page()

        for date in dates:
            out_path = PRED_DIR / date / 'racecard_parsed.json'
            if out_path.exists() and not args.overwrite:
                # Check if it's non-empty (not a stub)
                try:
                    existing = json.loads(out_path.read_text(encoding='utf-8'))
                    if existing and any(
                        isinstance(v, dict) and v.get('horses')
                        for v in existing.values()
                    ):
                        print(f'\n=== {date} (已存在，略過) ===')
                        continue
                except Exception:
                    pass

            print(f'\n=== {date} ===')
            try:
                course, races = await scrape_date(page, date)
            except Exception as exc:
                print(f'  ERROR: {exc}')
                continue

            if not races:
                print(f'  No race card found (not a race day or not yet published)')
                continue

            print(f'  Course: {course}  Races: {len(races)}')
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                json.dumps(races, ensure_ascii=False, indent=2), encoding='utf-8'
            )
            print(f'  Saved → {out_path}')

        await browser.close()

    print('\nDone.')


asyncio.run(main())

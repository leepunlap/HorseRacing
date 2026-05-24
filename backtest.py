#!/usr/bin/env python3
"""
Walk-forward backtest: train XGBoost on all data before each target date,
predict win probabilities for that date, save to predictions/{date}/predictions.json.

Historical data comes from CSV files (up to April 2026).
Dates not in the CSVs are read from the SQLite DB (scraped via scrape_results.py).

Usage:
    python3 backtest.py --all                        # all CSV dates
    python3 backtest.py --from 2026-01-01            # CSV dates from this date onward
    python3 backtest.py --from 2026-05-01 --to 2026-05-31   # range (includes DB dates)
    python3 backtest.py 2026-05-03 2026-05-06        # specific dates
"""

import sys, os, json, argparse, sqlite3, time
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path
import numpy as np, pandas as pd
import xgboost as xgb
import warnings; warnings.filterwarnings('ignore')

BASE = Path(__file__).parent
DATA = BASE / 'data'
PRED = BASE / 'predictions'
DB_PATH = DATA / 'racing.db'

# ── Going normalisation map ──────────────────────────────────────────────────
GOING_MAP = {'Good': 0, 'Good to Firm': 1, 'Good to Yielding': 2,
             'Yielding': 3, 'Soft': 4, 'Slow': 3}

PACE_DRAW = {
    'very_slow': {'inner': 18, 'mid': 8,  'outer': -6},
    'slow':      {'inner': 13, 'mid': 5,  'outer': -2},
    'medium':    {'inner': 6,  'mid': 4,  'outer': 1},
    'medium_fast':{'inner': 1, 'mid': 3,  'outer': 6},
    'fast':      {'inner': -6, 'mid': -2, 'outer': 15},
}

FEATURE_COLS = [
    'age', 'sex_gelding', 'rating', 'races_count', 'horse_wr', 'jockey_wr',
    'trainer_wr', 'jt_pair', 'jh_pair', 'dist_adapt', 'going_adapt', 'trainer_hot',
    'draw', 'draw_inner', 'draw_outer', 'weight', 'weight_allow', 'is_hv',
    'distance_km', 'going_num', 'class_num', 'participants', 'days_since',
    'layoff_penalty', 'race_pace', 'horse_style', 'pace_style_match',
    'pace_draw_bonus', 'late_pace_avg', 'cold_stable_season', 'wide_draw',
    'cold_stable_x_wide', 'chri_score', 'gear_change', 'first_gear_use',
    'rating_trend', 'class_drop', 'inner_x_leader', 'outer_x_closer',
    'draw_x_hv', 'draw_x_going', 'inner_x_pace', 'outer_x_fast', 'late_x_outer',
]

XGB_PARAMS = {
    'objective': 'binary:logistic', 'eval_metric': 'logloss',
    'max_depth': 5, 'learning_rate': 0.03, 'subsample': 0.8,
    'colsample_bytree': 0.7, 'min_child_weight': 10, 'lambda': 2.0,
    'alpha': 1.0, 'scale_pos_weight': 10, 'verbosity': 0, 'nthread': 4,
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def draw_group(d):
    if d <= 5:  return 'inner'
    if d <= 9:  return 'mid'
    return 'outer'


def classify_early_pace(ep):
    if pd.isna(ep) or ep is None: return 2
    ep = float(ep)
    if ep < 0.95: return 0
    if ep < 1.00: return 1
    if ep < 1.05: return 2
    return 3


def classify_race_pace(styles):
    from collections import Counter
    c   = Counter(styles)
    tot = max(sum(c.values()), 1)
    lp  = (c.get(0, 0) + c.get(1, 0)) / tot
    cp  = c.get(3, 0) / tot
    if lp > 0.45: return 'very_slow'
    if lp > 0.35: return 'slow'
    if cp > 0.40: return 'fast'
    if cp > 0.30: return 'medium_fast'
    return 'medium'


def date_range(start: str, end: str):
    d = datetime.strptime(start, '%Y-%m-%d')
    e = datetime.strptime(end,   '%Y-%m-%d')
    while d <= e:
        yield d.strftime('%Y-%m-%d')
        d += timedelta(days=1)


# ── Data loading ─────────────────────────────────────────────────────────────

def load_csv_data():
    """Load all historical results, metadata, sectionals, profiles, history from CSVs."""
    print("Loading CSV data...")
    res = pd.read_csv(DATA / 'hkjc_all_results_CN.csv', encoding='utf-8-sig', usecols=range(15))
    res['Date'] = pd.to_datetime(res['Date'], format='%Y/%m/%d')
    res = res.sort_values(['Date', 'RaceNo', 'Course']).reset_index(drop=True)
    res['BrandNo'] = res['HorseCN'].str.extract(r'\(([A-Z]\d+)\)')

    meta = pd.read_csv(DATA / 'hkjc_race_meta_CN.csv', encoding='utf-8-sig')
    meta['Date'] = pd.to_datetime(meta['Date'], format='%Y/%m/%d')
    meta['RaceNo'] = pd.to_numeric(meta['RaceNo'], errors='coerce')
    res['RaceNo'] = pd.to_numeric(res['RaceNo'], errors='coerce')
    res = res.merge(
        meta[['Date', 'Course', 'RaceNo', 'Distance', 'Class', 'Going', 'Participants']],
        on=['Date', 'Course', 'RaceNo'], how='left'
    )
    res['won'] = (res['Pla'].astype(str).str.strip() == '1').astype(int)
    for c in ['Draw', 'ActWt', 'Odds', 'Distance', 'Class', 'Participants']:
        if c in res.columns:
            res[c] = pd.to_numeric(res[c], errors='coerce')

    sec = pd.read_csv(DATA / 'hkjc_sectionals_CN.csv', encoding='utf-8-sig')
    sec['Date'] = pd.to_datetime(sec['Date'], format='%Y/%m/%d')

    prof = pd.read_csv(DATA / 'hkjc_horse_profiles_CN.csv', encoding='utf-8-sig')
    prof_dict = {}
    for _, row in prof.iterrows():
        b = str(row.get('BrandNo', ''))
        if b and b != 'nan':
            prof_dict[b] = {
                'Age':       row.get('Age', 5)       if pd.notna(row.get('Age'))       else 5,
                'Rating':    row.get('Rating', 0)    if pd.notna(row.get('Rating'))    else 0,
                'Sex':       str(row.get('Sex', '')),
                'RaceCount': row.get('RaceCount', 0) if pd.notna(row.get('RaceCount')) else 0,
            }

    rh = pd.read_csv(DATA / 'hkjc_horse_race_history_CN.csv', encoding='utf-8-sig')
    rh['Date'] = pd.to_datetime(rh['Date'], format='mixed', dayfirst=True)

    print(f"  CSV: {len(res):,} rows across {res['Date'].nunique()} dates "
          f"({res['Date'].min().date()} → {res['Date'].max().date()})")
    return res, sec, prof_dict, rh


def load_db_rows(date_str: str):
    """Load a single date's race rows from the DB (for dates not in CSVs)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # results joined with race metadata
    rows = conn.execute("""
        SELECT r.date, r.race_no as RaceNo, r.course as Course,
               r.brand as BrandNo, r.horse_name, r.jockey as JockeyCN,
               r.trainer as TrainerCN, r.position as Pla,
               CAST(r.draw AS REAL) as Draw, CAST(r.act_wt AS REAL) as ActWt,
               CAST(r.odds AS REAL) as Odds, r.won,
               CAST(rc.distance AS REAL) as Distance,
               CAST(rc.class AS REAL) as Class,
               rc.going as Going,
               CAST(rc.participants AS REAL) as Participants
        FROM results r
        LEFT JOIN races rc ON rc.date = r.date AND rc.course = r.course AND rc.raceno = r.race_no
        WHERE r.date = ?
        ORDER BY r.race_no, r.position
    """, (date_str,)).fetchall()
    conn.close()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame([dict(r) for r in rows])
    df['Date'] = pd.to_datetime(df['date'])
    df['RaceNo'] = pd.to_numeric(df['RaceNo'], errors='coerce')
    for c in ['Draw', 'ActWt', 'Odds', 'Distance', 'Class', 'Participants']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    return df


# ── Feature engineering ──────────────────────────────────────────────────────

def build_features(rows: pd.DataFrame, cutoff_date, res_hist, sec_hist, prof_dict, rh_hist):
    """Build the 44-feature matrix for `rows`, using only data before cutoff_date."""
    if len(rows) == 0:
        return pd.DataFrame()

    # ── Accumulate historical stats ─────────────────────────────────────────
    HSw  = defaultdict(lambda: {'w': 0, 'r': 0})
    JSw  = defaultdict(lambda: {'w': 0, 'r': 0})
    TSw  = defaultdict(lambda: {'w': 0, 'r': 0})
    JTSw = defaultdict(lambda: {'w': 0, 'r': 0})
    JHSw = defaultdict(lambda: {'w': 0, 'r': 0})
    HDSw = defaultdict(lambda: defaultdict(lambda: {'w': 0, 'r': 0}))
    HGSw = defaultdict(lambda: defaultdict(lambda: {'w': 0, 'r': 0}))
    ssw  = defaultdict(lambda: {'w': 0, 'r': 0})
    last_d = {}
    hs_style = {}
    hlp  = defaultdict(list)
    hra  = defaultdict(list)
    hcl  = defaultdict(list)
    gh   = defaultdict(list)

    for _, rw in res_hist.iterrows():
        b = rw.get('BrandNo', 'X') or 'X'
        j = rw.get('JockeyCN', 'X') or 'X'
        t = rw.get('TrainerCN', 'X') or 'X'
        w = rw['won']
        HSw[b]['r'] += 1;  JSw[j]['r'] += 1;  TSw[t]['r'] += 1
        if w: HSw[b]['w'] += 1; JSw[j]['w'] += 1; TSw[t]['w'] += 1
        jt = f'{j}|{t}';  jh = f'{j}|{b}'
        JTSw[jt]['r'] += 1; JHSw[jh]['r'] += 1
        if w: JTSw[jt]['w'] += 1; JHSw[jh]['w'] += 1
        ds = str(rw.get('Distance', ''));  g = str(rw.get('Going', 'Good'))
        HDSw[b][ds]['r'] += 1; HGSw[b][g]['r'] += 1
        if w: HDSw[b][ds]['w'] += 1; HGSw[b][g]['w'] += 1
        dt = rw['Date']
        last_d[b] = max(last_d.get(b, datetime(2020, 1, 1)), dt)
        if dt >= cutoff_date - timedelta(days=365):
            ssw[t]['r'] += 1
            if w: ssw[t]['w'] += 1

    for _, sr in sec_hist.iterrows():
        dt = sr['Date']; rn = sr['RaceNo']; course = sr['Course']
        ep = sr.get('EarlyPace')
        match = res_hist[(res_hist['Date'] == dt) & (res_hist['RaceNo'] == rn) & (res_hist['Course'] == course)]
        for _, mr in match.iterrows():
            b = mr.get('BrandNo', 'X') or 'X'
            if pd.notna(ep):
                hs_style[b] = classify_early_pace(ep)
            lp = sr.get('LatePace')
            if pd.notna(lp):
                hlp[b].append(float(lp))

    for _, rr in rh_hist.iterrows():
        b = rr.get('BrandNo', '') or ''
        if not b: continue
        if pd.notna(rr.get('Running')):
            gh[b].append(str(rr.get('Running', ''))[:2])
        if pd.notna(rr.get('Rating')):
            try: hra[b].append(float(rr['Rating']))
            except: pass
        if pd.notna(rr.get('Class')):
            try: hcl[b].append(float(rr['Class']))
            except: pass

    # ── Build feature rows ──────────────────────────────────────────────────
    feat_rows = []
    for (date, course, race_no), grp in rows.groupby(['Date', 'Course', 'RaceNo']):
        fmw = grp['ActWt'].max() if 'ActWt' in grp.columns else 135
        styles = [hs_style.get(rw.get('BrandNo', 'X') or 'X', 2) for _, rw in grp.iterrows()]
        rp = classify_race_pace(styles)

        for _, rw in grp.iterrows():
            b  = rw.get('BrandNo', 'X') or 'X'
            j  = rw.get('JockeyCN', 'X') or 'X'
            t  = rw.get('TrainerCN', 'X') or 'X'
            dv = int(rw['Draw']) if pd.notna(rw.get('Draw')) else 7
            wt = float(rw.get('ActWt') or 120)
            dist  = float(rw.get('Distance') or 1400)
            cls   = float(rw.get('Class') or 4)
            part  = float(rw.get('Participants') or 14)
            odds  = float(rw.get('Odds') or 0)
            gv    = GOING_MAP.get(str(rw.get('Going', 'Good')), 0)
            ds    = str(rw.get('Distance', ''))
            pi    = prof_dict.get(b, {})
            hs_v  = hs_style.get(b, 2)

            f = {}
            f['age']         = pi.get('Age', 5) or 5
            f['sex_gelding'] = 1 if 'gelding' in str(pi.get('Sex', '')).lower() else 0
            f['rating']      = pi.get('Rating', 0) or (wt * 0.3 + (6 - cls) * 20)
            f['races_count'] = pi.get('RaceCount', HSw[b]['r']) or HSw[b]['r']
            f['horse_wr']    = HSw[b]['w']  / max(HSw[b]['r'],  1)
            f['jockey_wr']   = JSw[j]['w']  / max(JSw[j]['r'],  1)
            f['trainer_wr']  = TSw[t]['w']  / max(TSw[t]['r'],  1)
            f['jt_pair']     = JTSw[f'{j}|{t}']['w'] / max(JTSw[f'{j}|{t}']['r'], 1)
            f['jh_pair']     = JHSw[f'{j}|{b}']['w'] / max(JHSw[f'{j}|{b}']['r'], 1)
            f['dist_adapt']  = HDSw[b][ds]['w'] / max(HDSw[b][ds]['r'], 1)
            f['going_adapt'] = HGSw[b][str(rw.get('Going', 'Good'))]['w'] / max(HGSw[b][str(rw.get('Going', 'Good'))]['r'], 1)
            f['trainer_hot'] = ssw[t]['w']
            f['draw']        = dv
            f['draw_inner']  = 1 if dv <= 4  else 0
            f['draw_outer']  = 1 if dv >= 10 else 0
            f['weight']      = wt
            f['weight_allow']= max(0, (fmw - wt) / 20)
            f['is_hv']       = 1 if course == 'HV' else 0
            f['distance_km'] = dist / 1000
            f['going_num']   = gv
            f['class_num']   = cls
            f['participants']= part
            ld = last_d.get(b, datetime(2020, 1, 1))
            f['days_since']  = min((date - ld).days, 365) if date > ld else 30
            f['layoff_penalty'] = -12 if f['days_since'] > 28 else (-6 if f['days_since'] > 14 else 0)
            rp_val = 1 if rp in ('very_slow', 'slow') else (0 if rp == 'medium' else 2)
            f['race_pace']   = rp_val
            f['horse_style'] = hs_v
            f['pace_style_match'] = (
                1.0 if (hs_v == 3 and rp in ('fast', 'medium_fast')) or (hs_v == 0 and rp in ('very_slow', 'slow'))
                else 0.7 if hs_v == 1 and rp in ('slow', 'very_slow')
                else 0.3
            )
            f['pace_draw_bonus'] = PACE_DRAW.get(rp, {}).get(draw_group(dv), 0)
            lps = hlp.get(b, [])
            f['late_pace_avg'] = np.mean(lps) if lps else 0.85
            swr = ssw[t]['w'] / max(ssw[t]['r'], 1)
            f['cold_stable_season']  = swr
            f['wide_draw']           = 1 if dv >= 10 else 0
            f['cold_stable_x_wide']  = 1 if swr < 0.05 and dv >= 10 else 0
            f['chri_score']          = f['weight_allow'] * 0.4 + f['wide_draw'] * 0.3 + f['cold_stable_x_wide'] * 0.3
            f['odds_raw']            = odds
            ghh = gh.get(b, [])
            f['gear_change']    = 1 if len(ghh) >= 2 and ghh[-1] != ghh[-2] else 0
            f['first_gear_use'] = 1 if len(ghh) >= 1 and ghh[-1] not in ['', 'B', 'TT'] else 0
            r_hra = hra.get(b, [])
            f['rating_trend'] = (np.mean(r_hra[-3:]) - np.mean(r_hra[:3])) if len(r_hra) >= 6 else 0
            f['class_drop']   = 1 if hcl.get(b) and hcl[b][-1] > (cls + 0.5) else 0
            f['inner_x_leader'] = (1 if dv <= 4  else 0) * (1 if hs_v == 0 else 0)
            f['outer_x_closer'] = (1 if dv >= 10 else 0) * (1 if hs_v == 3 else 0)
            f['draw_x_hv']    = dv * f['is_hv']
            f['draw_x_going'] = dv * gv
            f['inner_x_pace'] = (1 if dv <= 4  else 0) * (1 if rp in ('very_slow', 'slow') else 0)
            f['outer_x_fast'] = (1 if dv >= 10 else 0) * (1 if rp in ('fast', 'medium_fast') else 0)
            f['late_x_outer'] = f['late_pace_avg'] * (1 if dv >= 10 else 0)
            f['won']   = rw.get('won', 0)
            f['node']  = str(b)
            f['race_no'] = race_no
            feat_rows.append(f)

    return pd.DataFrame(feat_rows)


# ── Per-date prediction ───────────────────────────────────────────────────────

def run_date(date_str: str, res_csv, sec, prof_dict, rh, force=False):
    out_path = PRED / date_str / 'predictions.json'
    if out_path.exists() and not force:
        print(f"  {date_str}: already exists — skip (use --force to overwrite)")
        return False

    target_date = datetime.strptime(date_str, '%Y-%m-%d')

    # Get this date's race rows — from CSV if available, else from DB
    csv_rows = res_csv[res_csv['Date'] == target_date]
    if len(csv_rows) > 0:
        today_rows = csv_rows
        source = 'CSV'
    else:
        today_rows = load_db_rows(date_str)
        source = 'DB'

    if len(today_rows) == 0:
        print(f"  {date_str}: no data found in CSV or DB — skip")
        return False

    # Training data: all CSV rows before this date
    train_rows = res_csv[res_csv['Date'] < target_date]
    if len(train_rows) < 100:
        print(f"  {date_str}: not enough training data ({len(train_rows)} rows) — skip")
        return False

    t0 = time.time()
    res_hist  = res_csv[res_csv['Date'] < target_date]
    sec_hist  = sec[sec['Date'] < target_date]
    rh_hist   = rh[rh['Date'] < target_date]

    today_feats = build_features(today_rows, target_date, res_hist, sec_hist, prof_dict, rh_hist)
    train_feats = build_features(train_rows, target_date, res_hist, sec_hist, prof_dict, rh_hist)

    if len(today_feats) == 0 or len(train_feats) == 0:
        print(f"  {date_str}: feature build returned empty — skip")
        return False

    feat_cols = [c for c in FEATURE_COLS if c in today_feats.columns and c in train_feats.columns]
    if len(feat_cols) < 10:
        print(f"  {date_str}: too few features ({len(feat_cols)}) — skip")
        return False

    X_train = train_feats[feat_cols].fillna(0)
    y_train = train_feats['won'].values
    X_test  = today_feats[feat_cols].fillna(0)

    dtrain = xgb.DMatrix(X_train, label=y_train)
    dtest  = xgb.DMatrix(X_test)
    model  = xgb.train(XGB_PARAMS, dtrain, num_boost_round=100)

    raw_probs = model.predict(dtest)
    importance = model.get_score(importance_type='gain')
    feat_weights = {fn: float(importance.get(fn, 0.0)) for fn in FEATURE_COLS}
    feat_sorted  = sorted(FEATURE_COLS, key=lambda f: feat_weights[f], reverse=True)

    # Normalise probabilities per race
    today_feats = today_feats.copy()
    today_feats['prob']     = raw_probs
    today_feats['win_prob'] = raw_probs
    for rn in today_feats['race_no'].unique():
        mask    = today_feats['race_no'] == rn
        raw     = today_feats.loc[mask, 'win_prob'].values
        total   = raw.sum()
        if total > 0:
            today_feats.loc[mask, 'win_prob'] = raw / total

    # Build JSON output
    output = {}
    for race_no, grp in today_feats.groupby('race_no'):
        rn_key = str(int(race_no))
        rinfo  = today_rows[today_rows['RaceNo'] == int(race_no)]
        dist   = str(int(rinfo['Distance'].iloc[0])) if len(rinfo) and pd.notna(rinfo['Distance'].iloc[0]) else ''
        cls_raw = rinfo['Class'].iloc[0] if len(rinfo) else ''
        cls_str = str(int(cls_raw)) + '班' if pd.notna(cls_raw) and str(cls_raw) not in ('', 'nan') else ''

        horses = []
        for _, tr in grp.iterrows():
            # Try to get jockey/trainer from original row
            orig = today_rows[today_rows['BrandNo'] == tr['node']] if 'BrandNo' in today_rows.columns else pd.DataFrame()
            jockey  = orig['JockeyCN'].iloc[0]  if len(orig) and 'JockeyCN'  in orig.columns else ''
            trainer = orig['TrainerCN'].iloc[0] if len(orig) and 'TrainerCN' in orig.columns else ''
            horse_name = ''
            if 'HorseCN' in today_rows.columns:
                hrow = today_rows[today_rows['BrandNo'] == tr['node']]
                if len(hrow):
                    horse_name = str(hrow['HorseCN'].iloc[0])
                    import re
                    horse_name = re.sub(r'\s*\([A-Z]\d+\)', '', horse_name).strip()
            elif 'horse_name' in today_rows.columns:
                hrow = today_rows[today_rows['BrandNo'] == tr['node']]
                if len(hrow):
                    horse_name = str(hrow['horse_name'].iloc[0])

            horses.append({
                'no':       str(len(horses) + 1),
                'name':     horse_name,
                'brand':    str(tr['node']),
                'jockey':   str(jockey),
                'trainer':  str(trainer),
                'draw':     str(int(tr.get('draw', 0))),
                'weight':   str(int(tr.get('weight', 0))),
                'rating':   str(int(tr.get('rating', 0))),
                'win_odds': str(tr.get('odds_raw', '')),
                'prob':     round(float(tr['prob']),     4),
                'win_prob': round(float(tr['win_prob']), 4),
                'edge':     round(float(tr['win_prob']) * float(tr.get('odds_raw') or 0), 2),
                'features': {c: round(float(tr.get(c, 0) or 0), 4) for c in feat_sorted},
            })

        output[rn_key.zfill(2)] = {'distance': dist, 'class': cls_str, 'horses': horses}

    output['_feature_cols']    = feat_sorted
    output['_feature_weights'] = {k: round(v, 1) for k, v in feat_weights.items()}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as fh:
        json.dump(output, fh, ensure_ascii=False, indent=2, default=str)

    elapsed = time.time() - t0
    races   = len([k for k in output if not k.startswith('_')])
    horses  = sum(len(output[k]['horses']) for k in output if not k.startswith('_'))
    print(f"  {date_str} [{source}]: {races} races, {horses} horses → saved  ({elapsed:.0f}s)")
    return True


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Generate walk-forward XGBoost predictions.')
    parser.add_argument('dates', nargs='*', metavar='YYYY-MM-DD',
                        help='Specific dates to run')
    parser.add_argument('--all',  action='store_true',
                        help='Run all dates in the CSV')
    parser.add_argument('--from', dest='date_from', metavar='YYYY-MM-DD',
                        help='Start of date range')
    parser.add_argument('--to',   dest='date_to',   metavar='YYYY-MM-DD',
                        help='End of date range (inclusive, default: today)')
    parser.add_argument('--force', action='store_true',
                        help='Overwrite existing predictions.json files')
    args = parser.parse_args()

    res_csv, sec, prof_dict, rh = load_csv_data()
    csv_dates = [d.strftime('%Y-%m-%d') for d in sorted(res_csv['Date'].unique())]

    # Build target date list
    if args.all:
        targets = csv_dates
    elif args.date_from:
        end = args.date_to or datetime.now().strftime('%Y-%m-%d')
        targets = list(date_range(args.date_from, end))
    elif args.dates:
        targets = sorted(set(args.dates))
    else:
        parser.print_help()
        sys.exit(1)

    print(f"\nRunning {len(targets)} date(s)...")
    done = skipped = errors = 0
    for date_str in targets:
        try:
            ok = run_date(date_str, res_csv, sec, prof_dict, rh, force=args.force)
            if ok: done += 1
            else:  skipped += 1
        except Exception as e:
            print(f"  {date_str}: ERROR — {e}")
            errors += 1

    print(f"\nDone: {done} generated, {skipped} skipped, {errors} errors")


if __name__ == '__main__':
    main()

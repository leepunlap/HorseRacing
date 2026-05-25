#!/usr/bin/env python3
"""
Walk-forward backtest: train XGBoost on all data before each target date,
predict win probabilities, save to models/{model}/results/{date}/predictions.json.

Usage:
    python3 backtest.py --all                               # active model, all CSV dates
    python3 backtest.py --model 均衡基礎策略 --all          # named model, all CSV dates
    python3 backtest.py --from 2026-05-01 --to 2026-05-31 # date range (DB dates OK)
    python3 backtest.py 2026-05-03 2026-05-06              # specific dates
    python3 backtest.py --all --force                       # overwrite existing
    python3 backtest.py --all --publish                     # copy results → predictions/
"""

import sys, os, json, argparse, sqlite3, time, re, shutil
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path
import numpy as np, pandas as pd
import xgboost as xgb
import warnings; warnings.filterwarnings('ignore')

import model_config as _mc
from model_config import FEATURE_COLS, load_config, results_dir

BASE    = Path(__file__).parent
DATA    = BASE / 'data'
PRED    = BASE / 'predictions'   # production output (published from active model)
DB_PATH = DATA / 'racing.db'


def _cfg_values(cfg: dict):
    """Unpack a config dict into local variables used by feature engineering."""
    return (
        cfg.get('xgb', {}),
        cfg.get('num_boost_rounds', 100),
        cfg.get('going_map', {}),
        cfg.get('pace_draw', {}),
        cfg.get('pace_bucket', {}),
        [(v[0], v[1]) for v in cfg.get('early_pace_thresholds', [])],
        cfg.get('draw_inner_max', 5),
        cfg.get('draw_outer_min', 10),
        cfg.get('layoff', {}),
        cfg.get('weight_allow_divisor', 20),
        cfg.get('cold_stable_threshold', 0.05),
        cfg.get('chri', {}),
        cfg.get('pace_match', {}),
        cfg.get('trainer_form_days', 365),
        cfg.get('rating_trend_window', 3),
        set(cfg.get('standard_gear', ['', 'B', 'TT'])),
        set(cfg.get('features_disabled', [])),
    )


# ── Helpers (all take cfg values as parameters) ───────────────────────────────

def draw_group(d: int, inner_max: int, outer_min: int) -> str:
    if d <= inner_max: return 'inner'
    if d < outer_min:  return 'mid'
    return 'outer'


def classify_early_pace(ep, thresholds: list) -> int:
    if pd.isna(ep) or ep is None: return 2
    ep = float(ep)
    for threshold, style in thresholds:
        if threshold is None or ep < threshold:
            return style
    return 3


def classify_race_pace(styles: list, pace_bucket: dict) -> str:
    from collections import Counter
    c   = Counter(styles)
    tot = max(sum(c.values()), 1)
    lp  = (c.get(0, 0) + c.get(1, 0)) / tot
    cp  = c.get(3, 0) / tot
    if lp > pace_bucket.get('very_slow_leader_pct',   0.45): return 'very_slow'
    if lp > pace_bucket.get('slow_leader_pct',        0.35): return 'slow'
    if cp > pace_bucket.get('fast_closer_pct',        0.40): return 'fast'
    if cp > pace_bucket.get('medium_fast_closer_pct', 0.30): return 'medium_fast'
    return 'medium'


def pace_style_match_score(horse_style: int, race_pace: str, pace_match: dict) -> float:
    if (horse_style == 3 and race_pace in ('fast', 'medium_fast')) or \
       (horse_style == 0 and race_pace in ('very_slow', 'slow')):
        return pace_match.get('leader_slow', 1.0)
    if horse_style == 1 and race_pace in ('slow', 'very_slow'):
        return pace_match.get('stalker_slow', 0.7)
    return pace_match.get('default', 0.3)


def smoothed(wins: int, races: int, prior: float, alpha: float) -> float:
    """Bayesian shrinkage: blend observed win rate toward a prior using α virtual races.

    Formula:  (wins + α × prior) / (races + α)

    Behaviour:
      races = 0  →  returns prior exactly (unknown entity gets the prior).
      races >> α →  observed rate dominates (large sample overrides prior).
      races = α  →  50/50 blend of observed and prior.

    Args:
        wins:  observed wins for this entity/combination.
        races: observed starts for this entity/combination.
        prior: best-available proxy rate when evidence is thin.
        alpha: strength of the prior in "virtual race" units.
    """
    return (wins + alpha * prior) / (races + alpha)


def date_range(start: str, end: str):
    d = datetime.strptime(start, '%Y-%m-%d')
    e = datetime.strptime(end,   '%Y-%m-%d')
    while d <= e:
        yield d.strftime('%Y-%m-%d')
        d += timedelta(days=1)


# ── Data loading ─────────────────────────────────────────────────────────────

def load_csv_data():
    """Load all historical data from CSV files."""
    print("Loading CSV data...")
    res = pd.read_csv(DATA / 'hkjc_all_results_CN.csv', encoding='utf-8-sig', usecols=range(15))
    res['Date'] = pd.to_datetime(res['Date'], format='%Y/%m/%d')
    res = res.sort_values(['Date', 'RaceNo', 'Course']).reset_index(drop=True)
    res['BrandNo'] = res['HorseCN'].str.extract(r'\(([A-Z]\d+)\)')

    meta = pd.read_csv(DATA / 'hkjc_race_meta_CN.csv', encoding='utf-8-sig')
    meta['Date'] = pd.to_datetime(meta['Date'], format='%Y/%m/%d')
    meta['RaceNo'] = pd.to_numeric(meta['RaceNo'], errors='coerce')
    res['RaceNo']  = pd.to_numeric(res['RaceNo'],  errors='coerce')
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

    print(f"  {len(res):,} rows across {res['Date'].nunique()} dates "
          f"({res['Date'].min().date()} → {res['Date'].max().date()})")
    return res, sec, prof_dict, rh


def load_db_rows(date_str: str) -> pd.DataFrame:
    """Load a single date's race rows from the DB (for scraped-only dates)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT r.date, r.race_no AS RaceNo, r.course AS Course,
               r.brand AS BrandNo, r.horse_name, r.jockey AS JockeyCN,
               r.trainer AS TrainerCN, r.position AS Pla,
               CAST(r.draw AS REAL)         AS Draw,
               CAST(r.act_wt AS REAL)       AS ActWt,
               CAST(r.odds AS REAL)         AS Odds,
               r.won,
               CAST(rc.distance AS REAL)    AS Distance,
               CAST(rc.class AS REAL)       AS Class,
               rc.going                     AS Going,
               CAST(rc.participants AS REAL) AS Participants
        FROM results r
        LEFT JOIN races rc
               ON rc.date = r.date AND rc.course = r.course AND rc.raceno = r.race_no
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


# ── Historical stat accumulators ─────────────────────────────────────────────

def compute_win_rates(hist: pd.DataFrame, cutoff_date, trainer_form_days: int = 365) -> dict:
    """Accumulate win/run counts for horses, jockeys, trainers, pairs, distance, going."""
    HS  = defaultdict(lambda: {'w': 0, 'r': 0})
    JS  = defaultdict(lambda: {'w': 0, 'r': 0})
    TS  = defaultdict(lambda: {'w': 0, 'r': 0})
    JTS = defaultdict(lambda: {'w': 0, 'r': 0})
    JHS = defaultdict(lambda: {'w': 0, 'r': 0})
    HDS = defaultdict(lambda: defaultdict(lambda: {'w': 0, 'r': 0}))
    HGS = defaultdict(lambda: defaultdict(lambda: {'w': 0, 'r': 0}))
    SS  = defaultdict(lambda: {'w': 0, 'r': 0})
    last_d = {}

    season_start = cutoff_date - timedelta(days=trainer_form_days)

    for _, rw in hist.iterrows():
        b = rw.get('BrandNo', 'X') or 'X'
        j = rw.get('JockeyCN', 'X') or 'X'
        t = rw.get('TrainerCN', 'X') or 'X'
        w = rw['won']
        ds = str(rw.get('Distance', ''))
        g  = str(rw.get('Going', 'Good'))
        dt = rw['Date']

        HS[b]['r'] += 1;  JS[j]['r'] += 1;  TS[t]['r'] += 1
        if w: HS[b]['w'] += 1; JS[j]['w'] += 1; TS[t]['w'] += 1

        JTS[f'{j}|{t}']['r'] += 1;  JHS[f'{j}|{b}']['r'] += 1
        if w: JTS[f'{j}|{t}']['w'] += 1; JHS[f'{j}|{b}']['w'] += 1

        HDS[b][ds]['r'] += 1;  HGS[b][g]['r'] += 1
        if w: HDS[b][ds]['w'] += 1; HGS[b][g]['w'] += 1

        last_d[b] = max(last_d.get(b, datetime(2020, 1, 1)), dt)

        if dt >= season_start:
            SS[t]['r'] += 1
            if w: SS[t]['w'] += 1

    return {'HS': HS, 'JS': JS, 'TS': TS, 'JTS': JTS, 'JHS': JHS,
            'HDS': HDS, 'HGS': HGS, 'SS': SS, 'last_d': last_d}


def compute_pace_styles(hist_sec: pd.DataFrame, hist: pd.DataFrame,
                        ep_thresholds: list) -> tuple:
    """Return (horse_style_dict, horse_late_pace_dict) from sectionals history."""
    hs_style = {}
    hlp = defaultdict(list)

    for _, sr in hist_sec.iterrows():
        dt = sr['Date']; rn = sr['RaceNo']; course = sr['Course']
        ep = sr.get('EarlyPace')
        lp = sr.get('LatePace')
        match = hist[(hist['Date'] == dt) & (hist['RaceNo'] == rn) & (hist['Course'] == course)]
        for _, mr in match.iterrows():
            b = mr.get('BrandNo', 'X') or 'X'
            if pd.notna(ep):
                hs_style[b] = classify_early_pace(ep, ep_thresholds)
            if pd.notna(lp):
                hlp[b].append(float(lp))

    return hs_style, hlp


def compute_horse_history(hist_rh: pd.DataFrame) -> tuple:
    """Return (gear_history, rating_history, class_history) dicts from race history."""
    gh  = defaultdict(list)
    hra = defaultdict(list)
    hcl = defaultdict(list)

    for _, rr in hist_rh.iterrows():
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

    return gh, hra, hcl


# ── Per-horse feature vector ──────────────────────────────────────────────────

def build_horse_features(rw, grp, race_pace_str, stats, hs_style, hlp, gh, hra, hcl,
                          prof_dict, cutoff_date, cfg: dict) -> dict:
    """Compute all 44 features for a single horse in a race."""
    (xgb_p, n_rounds, going_map, pace_draw, pace_bucket, ep_thresh,
     inner_max, outer_min, layoff, wt_div, cold_thresh, chri,
     pace_match, form_days, rt_window, std_gear, feats_off) = _cfg_values(cfg)

    b   = rw.get('BrandNo', 'X') or 'X'
    j   = rw.get('JockeyCN', 'X') or 'X'
    t   = rw.get('TrainerCN', 'X') or 'X'
    dv  = int(rw['Draw']) if pd.notna(rw.get('Draw')) else 7
    wt  = float(rw.get('ActWt') or 120)
    dist = float(rw.get('Distance') or 1400)
    cls  = float(rw.get('Class') or 4)
    part = float(rw.get('Participants') or 14)
    odds = float(rw.get('Odds') or 0)
    gv   = going_map.get(str(rw.get('Going', 'Good') or 'Good'), 0)
    ds   = str(rw.get('Distance', ''))
    pi   = prof_dict.get(b, {})
    hs_v = hs_style.get(b, 2)

    HS  = stats['HS'];   JS  = stats['JS'];   TS  = stats['TS']
    JTS = stats['JTS'];  JHS = stats['JHS']
    HDS = stats['HDS'];  HGS = stats['HGS']
    SS  = stats['SS'];   last_d = stats['last_d']

    fmw = grp['ActWt'].max() if 'ActWt' in grp.columns else 135
    going_str = str(rw.get('Going', 'Good'))   # used in both Adaptability and going_num

    f = {}

    # ── Horse Profile ────────────────────────────────────────────────────
    f['age']         = pi.get('Age', 5) or 5
    f['sex_gelding'] = 1 if 'gelding' in str(pi.get('Sex', '')).lower() else 0
    f['rating']      = pi.get('Rating', 0) or (wt * 0.3 + (6 - cls) * 20)
    f['races_count'] = pi.get('RaceCount', HS[b]['r']) or HS[b]['r']

    # ── Win Rates  (Bayesian shrinkage — see ADVISORY.md §1) ─────────────
    #
    # Raw counts are blended toward a prior using α "virtual races".
    # This prevents cold-start entities from scoring 0.0 and being
    # misread by the model as chronic losers instead of unknowns.
    #
    # Prior hierarchy:
    #   trainer_wr  ← field average (all trainers earn ~1/field_size)
    #   jockey_wr   ← field average
    #   horse_wr    ← trainer_wr  (trainer knows the horse best before debut)
    #   jt_pair     ← geometric_mean(j_wr_raw, t_wr_raw)
    #   jh_pair     ← geometric_mean(j_wr_raw, h_wr_raw)
    #   dist_adapt  ← horse_wr   (overall ability transfers to new distance)
    #   going_adapt ← horse_wr   (overall ability transfers to new ground)

    SH       = cfg.get('shrinkage', {})
    FIELD_WR = SH.get('field_avg_win_rate', 0.083)   # ≈ 1/12 runners

    # Step 1 — raw rates (needed as priors before smoothing)
    t_wr_raw = TS[t]['w'] / max(TS[t]['r'], 1)
    j_wr_raw = JS[j]['w'] / max(JS[j]['r'], 1)
    h_wr_raw = HS[b]['w'] / max(HS[b]['r'], 1)

    # Step 2 — smoothed individual rates
    f['trainer_wr'] = smoothed(TS[t]['w'], TS[t]['r'], FIELD_WR,   SH.get('trainer_alpha', 30))
    f['jockey_wr']  = smoothed(JS[j]['w'], JS[j]['r'], FIELD_WR,   SH.get('jockey_alpha', 20))
    f['horse_wr']   = smoothed(HS[b]['w'], HS[b]['r'], t_wr_raw,   SH.get('horse_alpha',  5))

    # Step 3 — pair rates: prior = geometric mean of the two individual raw rates
    #   (geometric mean < arithmetic mean — limits how much one strong party
    #    inflates a new combination; zero is the identity, so we fall back to max)
    jt_key   = f'{j}|{t}'
    jh_key   = f'{j}|{b}'
    jt_prior = (j_wr_raw * t_wr_raw) ** 0.5 if j_wr_raw * t_wr_raw > 0 \
               else max(j_wr_raw, t_wr_raw, FIELD_WR)
    jh_prior = (j_wr_raw * h_wr_raw) ** 0.5 if j_wr_raw * h_wr_raw > 0 \
               else max(j_wr_raw, h_wr_raw, FIELD_WR)
    f['jt_pair'] = smoothed(JTS[jt_key]['w'], JTS[jt_key]['r'], jt_prior, SH.get('jt_alpha', 10))
    f['jh_pair'] = smoothed(JHS[jh_key]['w'], JHS[jh_key]['r'], jh_prior, SH.get('jh_alpha',  3))

    # ── Adaptability (Bayesian shrinkage — prior = horse overall win rate) ──
    f['dist_adapt']  = smoothed(HDS[b][ds]['w'],         HDS[b][ds]['r'],
                                h_wr_raw, SH.get('dist_alpha',  5))
    f['going_adapt'] = smoothed(HGS[b][going_str]['w'],  HGS[b][going_str]['r'],
                                h_wr_raw, SH.get('going_alpha', 3))

    # ── Trainer Form ─────────────────────────────────────────────────────
    # cold_stable_season uses raw rate (not smoothed) so the threshold comparison
    # in cold_stable_x_wide reflects true recent activity, not inflated by priors.
    swr = SS[t]['w'] / max(SS[t]['r'], 1)
    f['trainer_hot']        = SS[t]['w']
    f['cold_stable_season'] = swr

    # ── Draw / Barrier ───────────────────────────────────────────────────
    f['draw']       = dv
    f['draw_inner'] = 1 if dv <= inner_max else 0
    f['draw_outer'] = 1 if dv >= outer_min else 0
    f['wide_draw']  = 1 if dv >= outer_min else 0

    # ── Weight ───────────────────────────────────────────────────────────
    f['weight']       = wt
    f['weight_allow'] = max(0, (fmw - wt) / wt_div)

    # ── Race Context ─────────────────────────────────────────────────────
    f['is_hv']       = 1 if rw.get('Course') == 'HV' else 0
    f['distance_km'] = dist / 1000
    f['going_num']   = gv
    f['class_num']   = cls
    f['participants']= part

    # ── Form / Fitness ───────────────────────────────────────────────────
    date = rw['Date'] if hasattr(rw['Date'], 'year') else cutoff_date
    ld   = last_d.get(b, datetime(2020, 1, 1))
    f['days_since'] = min((date - ld).days, layoff.get('max_days', 365)) if date > ld else 30
    if f['days_since'] > layoff.get('long_days', 28):
        f['layoff_penalty'] = layoff.get('long_penalty', -12)
    elif f['days_since'] > layoff.get('medium_days', 14):
        f['layoff_penalty'] = layoff.get('medium_penalty', -6)
    else:
        f['layoff_penalty'] = layoff.get('short_penalty', 0)

    r_hra = hra.get(b, [])
    f['rating_trend'] = (np.mean(r_hra[-rt_window:]) - np.mean(r_hra[:rt_window])) \
                        if len(r_hra) >= rt_window * 2 else 0
    f['class_drop']   = 1 if hcl.get(b) and hcl[b][-1] > (cls + 0.5) else 0

    # ── Gear ─────────────────────────────────────────────────────────────
    ghh = gh.get(b, [])
    f['gear_change']    = 1 if len(ghh) >= 2 and ghh[-1] != ghh[-2] else 0
    f['first_gear_use'] = 1 if len(ghh) >= 1 and ghh[-1] not in std_gear else 0

    # ── Pace Analysis ────────────────────────────────────────────────────
    rp_val = 1 if race_pace_str in ('very_slow', 'slow') else (0 if race_pace_str == 'medium' else 2)
    f['race_pace']        = rp_val
    f['horse_style']      = hs_v
    f['pace_style_match'] = pace_style_match_score(hs_v, race_pace_str, pace_match)
    f['pace_draw_bonus']  = pace_draw.get(race_pace_str, {}).get(draw_group(dv, inner_max, outer_min), 0)
    lps = hlp.get(b, [])
    f['late_pace_avg']    = np.mean(lps) if lps else 0.85

    # ── Composite / Interactions ─────────────────────────────────────────
    f['cold_stable_x_wide']  = 1 if swr < cold_thresh and dv >= outer_min else 0
    f['chri_score']          = (f['weight_allow']       * chri.get('weight_allow', 0.4) +
                                f['wide_draw']          * chri.get('wide_draw', 0.3) +
                                f['cold_stable_x_wide'] * chri.get('cold_stable_x_wide', 0.3))
    f['inner_x_leader']  = (1 if dv <= inner_max else 0) * (1 if hs_v == 0 else 0)
    f['outer_x_closer']  = (1 if dv >= outer_min else 0) * (1 if hs_v == 3 else 0)
    f['draw_x_hv']       = dv * f['is_hv']
    f['draw_x_going']    = dv * gv
    f['inner_x_pace']    = (1 if dv <= inner_max else 0) * (1 if race_pace_str in ('very_slow', 'slow') else 0)
    f['outer_x_fast']    = (1 if dv >= outer_min else 0) * (1 if race_pace_str in ('fast', 'medium_fast') else 0)
    f['late_x_outer']    = f['late_pace_avg'] * (1 if dv >= outer_min else 0)

    # ── Metadata (not used as model features) ────────────────────────────
    f['odds_raw'] = odds
    f['won']      = rw.get('won', 0)
    f['node']     = str(b)
    f['race_no']  = rw.get('RaceNo', 0)

    return f


# ── Full feature matrix ───────────────────────────────────────────────────────

def build_features(rows: pd.DataFrame, cutoff_date, res_hist, sec_hist,
                   prof_dict, rh_hist, cfg: dict) -> pd.DataFrame:
    """Build the full feature matrix for all horses in `rows`."""
    if len(rows) == 0:
        return pd.DataFrame()

    (_, _, _, _, pace_bucket, ep_thresh, _, _, _, _, _, _, _, form_days,
     _, _, _) = _cfg_values(cfg)

    stats          = compute_win_rates(res_hist, cutoff_date, form_days)
    hs_style, hlp  = compute_pace_styles(sec_hist, res_hist, ep_thresh)
    gh, hra, hcl   = compute_horse_history(rh_hist)

    feat_rows = []
    for (date, course, race_no), grp in rows.groupby(['Date', 'Course', 'RaceNo']):
        styles        = [hs_style.get(rw.get('BrandNo', 'X') or 'X', 2) for _, rw in grp.iterrows()]
        race_pace_str = classify_race_pace(styles, pace_bucket)
        for _, rw in grp.iterrows():
            f = build_horse_features(rw, grp, race_pace_str, stats, hs_style, hlp,
                                     gh, hra, hcl, prof_dict, cutoff_date, cfg)
            feat_rows.append(f)

    return pd.DataFrame(feat_rows)


# ── Per-date prediction ───────────────────────────────────────────────────────

def run_date(date_str: str, res_csv, sec, prof_dict, rh,
             cfg: dict, out_dir: Path, force=False) -> dict:
    """Run prediction for one date. Returns result dict for summary, or None if skipped."""
    out_path = out_dir / date_str / 'predictions.json'
    if out_path.exists() and not force:
        print(f"  {date_str}: already exists — skip (use --force to overwrite)")
        return None

    target_date = datetime.strptime(date_str, '%Y-%m-%d')

    # Source rows — CSV first, then DB
    csv_rows = res_csv[res_csv['Date'] == target_date]
    if len(csv_rows) > 0:
        today_rows = csv_rows
        source = 'CSV'
    else:
        today_rows = load_db_rows(date_str)
        source = 'DB'

    if len(today_rows) == 0:
        print(f"  {date_str}: no data found — skip")
        return None

    train_rows = res_csv[res_csv['Date'] < target_date]
    if len(train_rows) < 100:
        print(f"  {date_str}: insufficient training data ({len(train_rows)} rows) — skip")
        return None

    t0 = time.time()
    res_hist = res_csv[res_csv['Date'] < target_date]
    sec_hist = sec[sec['Date'] < target_date]
    rh_hist  = rh[rh['Date'] < target_date]

    today_feats = build_features(today_rows, target_date, res_hist, sec_hist, prof_dict, rh_hist, cfg)
    train_feats = build_features(train_rows, target_date, res_hist, sec_hist, prof_dict, rh_hist, cfg)

    if len(today_feats) == 0 or len(train_feats) == 0:
        print(f"  {date_str}: empty feature matrix — skip")
        return None

    disabled = set(cfg.get('features_disabled', []))
    feat_cols = [c for c in FEATURE_COLS if c not in disabled
                 and c in today_feats.columns and c in train_feats.columns]
    if len(feat_cols) < 10:
        print(f"  {date_str}: too few features ({len(feat_cols)}) — skip")
        return None

    xgb_params   = cfg.get('xgb', {})
    n_rounds     = cfg.get('num_boost_rounds', 100)

    X_train = train_feats[feat_cols].fillna(0)
    y_train = train_feats['won'].values
    X_test  = today_feats[feat_cols].fillna(0)

    dtrain = xgb.DMatrix(X_train, label=y_train)
    dtest  = xgb.DMatrix(X_test)
    model  = xgb.train(xgb_params, dtrain, num_boost_round=n_rounds)

    raw_probs    = model.predict(dtest)
    importance   = model.get_score(importance_type='gain')
    feat_weights = {fn: float(importance.get(fn, 0.0)) for fn in feat_cols}
    feat_sorted  = sorted(feat_cols, key=lambda f: feat_weights[f], reverse=True)

    # Normalise probabilities per race
    today_feats = today_feats.copy()
    today_feats['prob']     = raw_probs
    today_feats['win_prob'] = raw_probs
    for rn in today_feats['race_no'].unique():
        mask  = today_feats['race_no'] == rn
        raw   = today_feats.loc[mask, 'win_prob'].values
        total = raw.sum()
        if total > 0:
            today_feats.loc[mask, 'win_prob'] = raw / total

    # Build output JSON
    output = {}
    top1_correct = 0; top1_races = 0
    bets_placed = 0; bets_won = 0; units_staked = 0.0; units_net = 0.0
    bet_edge_threshold = cfg.get('bet_edge_threshold', 1.0)

    for race_no, grp in today_feats.groupby('race_no'):
        rn_key  = str(int(race_no))
        rinfo   = today_rows[today_rows['RaceNo'] == int(race_no)]
        dist    = str(int(rinfo['Distance'].iloc[0])) if len(rinfo) and pd.notna(rinfo['Distance'].iloc[0]) else ''
        cls_raw = rinfo['Class'].iloc[0] if len(rinfo) else ''
        cls_str = str(int(cls_raw)) + '班' if pd.notna(cls_raw) and str(cls_raw) not in ('', 'nan') else ''

        horses = []
        for _, tr in grp.iterrows():
            brand = tr['node']
            orig  = today_rows[today_rows['BrandNo'] == brand] if 'BrandNo' in today_rows.columns else pd.DataFrame()
            jockey  = orig['JockeyCN'].iloc[0]  if len(orig) and 'JockeyCN'  in orig.columns else ''
            trainer = orig['TrainerCN'].iloc[0] if len(orig) and 'TrainerCN' in orig.columns else ''
            horse_name = ''
            if 'HorseCN' in today_rows.columns:
                hrow = today_rows[today_rows['BrandNo'] == brand]
                if len(hrow):
                    horse_name = re.sub(r'\s*\([A-Z]\d+\)', '', str(hrow['HorseCN'].iloc[0])).strip()
            elif 'horse_name' in today_rows.columns:
                hrow = today_rows[today_rows['BrandNo'] == brand]
                if len(hrow):
                    horse_name = str(hrow['horse_name'].iloc[0])

            horses.append({
                'no':       str(len(horses) + 1),
                'name':     horse_name,
                'brand':    brand,
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

        # Top-1 accuracy + bet P&L tracking
        if horses:
            top1_races += 1
            predicted_winner = max(horses, key=lambda h: h['win_prob'])
            actual_winners = {h['brand'] for h in horses if int(today_feats[
                (today_feats['node'] == h['brand']) & (today_feats['race_no'] == int(race_no))
            ]['won'].values[0]) == 1} if True else set()
            if predicted_winner['brand'] in actual_winners:
                top1_correct += 1

            # Bet on any horse with positive EV (edge > threshold)
            for h in horses:
                if h['edge'] > bet_edge_threshold:
                    odds_val = float(h.get('win_odds') or 0)
                    if odds_val <= 1.0:
                        continue
                    bets_placed  += 1
                    units_staked += 1.0
                    if h['brand'] in actual_winners:
                        bets_won  += 1
                        units_net += odds_val - 1.0   # profit = odds - stake
                    else:
                        units_net -= 1.0              # loss = stake

        output[rn_key.zfill(2)] = {'distance': dist, 'class': cls_str, 'horses': horses}

    output['_feature_cols']    = feat_sorted
    output['_feature_weights'] = {k: round(v, 1) for k, v in feat_weights.items()}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as fh:
        json.dump(output, fh, ensure_ascii=False, indent=2, default=str)

    elapsed  = time.time() - t0
    races_n  = len([k for k in output if not k.startswith('_')])
    horses_n = sum(len(output[k]['horses']) for k in output if not k.startswith('_'))
    top1_acc = top1_correct / top1_races if top1_races else 0
    print(f"  {date_str} [{source}]: {races_n} races, {horses_n} horses, top1={top1_acc:.0%} → saved  ({elapsed:.0f}s)")
    roi = round(units_net / units_staked, 4) if units_staked > 0 else None
    return {'date': date_str, 'races': races_n, 'horses': horses_n,
            'top1_correct': top1_correct, 'top1_races': top1_races,
            'bets_placed': bets_placed, 'bets_won': bets_won,
            'units_staked': round(units_staked, 1), 'units_net': round(units_net, 2),
            'roi': roi, 'elapsed': round(elapsed, 1)}


def update_summary(model_name: str, results: list):
    """Write/update models/{name}/results/summary.json with aggregate stats."""
    valid = [r for r in results if r]
    if not valid: return
    total_races   = sum(r['top1_races']   for r in valid)
    total_correct = sum(r['top1_correct'] for r in valid)
    total_bets    = sum(r.get('bets_placed', 0)   for r in valid)
    total_bet_won = sum(r.get('bets_won', 0)       for r in valid)
    total_staked  = sum(r.get('units_staked', 0.0) for r in valid)
    total_net     = sum(r.get('units_net', 0.0)    for r in valid)

    top1_acc = round(total_correct / total_races, 4) if total_races else 0
    roi      = round(total_net / total_staked, 4)    if total_staked > 0 else None

    summary = {
        'model':         model_name,
        'dates_run':     len(valid),
        'total_races':   total_races,
        'top1_accuracy': top1_acc,
        'top1_pct':      round(top1_acc * 100, 1),
        'bets_placed':   total_bets,
        'bets_won':      total_bet_won,
        'units_staked':  round(total_staked, 1),
        'units_net':     round(total_net, 2),
        'roi':           roi,
        'roi_units':     round(total_net, 2),
        'updated':       datetime.now().isoformat(),
        'per_date':      [r for r in valid],
    }
    summary_path = results_dir(model_name) / 'summary.json'
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with open(summary_path, 'w') as f:
        json.dump(summary, f, indent=2)
    roi_str = f'{total_net:+.2f}u ({roi:+.1%})' if roi is not None else '—'
    print(f"\nSummary: {total_correct}/{total_races} top-1 ({top1_acc:.1%}) "
          f"| 下注 {total_bets} 場 {total_bet_won}勝 ROI {roi_str} "
          f"→ {summary_path}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Generate walk-forward XGBoost predictions.')
    parser.add_argument('dates', nargs='*', metavar='YYYY-MM-DD',
                        help='Specific dates to run')
    parser.add_argument('--model',  default=None,
                        help='Named model config in models/ (default: active model)')
    parser.add_argument('--all',   action='store_true',
                        help='Run all dates in the CSV')
    parser.add_argument('--from',  dest='date_from', metavar='YYYY-MM-DD',
                        help='Start of date range')
    parser.add_argument('--to',    dest='date_to',   metavar='YYYY-MM-DD',
                        help='End of date range (inclusive, default: today)')
    parser.add_argument('--force', action='store_true',
                        help='Overwrite existing predictions.json files')
    parser.add_argument('--publish', action='store_true',
                        help='After running, copy results to predictions/ (production)')
    args = parser.parse_args()

    res_csv, sec, prof_dict, rh = load_csv_data()
    csv_dates = [d.strftime('%Y-%m-%d') for d in sorted(res_csv['Date'].unique())]

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

    cfg        = load_config(args.model)
    model_name = cfg['name']
    out_dir    = results_dir(model_name)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Model: {model_name}  →  {out_dir}")

    print(f"\nRunning {len(targets)} date(s)...")
    all_results = []
    done = skipped = errors = 0
    for date_str in targets:
        try:
            result = run_date(date_str, res_csv, sec, prof_dict, rh,
                              cfg=cfg, out_dir=out_dir, force=args.force)
            all_results.append(result)
            if result: done += 1
            else:      skipped += 1
        except Exception as e:
            print(f"  {date_str}: ERROR — {e}")
            import traceback; traceback.print_exc()
            errors += 1

    update_summary(model_name, all_results)

    if args.publish:
        print(f"\nPublishing {done} date(s) to predictions/...")
        for result in all_results:
            if not result: continue
            src = out_dir / result['date'] / 'predictions.json'
            dst = PRED    / result['date'] / 'predictions.json'
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        print("Published.")

    print(f"\nDone: {done} generated, {skipped} skipped, {errors} errors")


if __name__ == '__main__':
    main()

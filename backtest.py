#!/usr/bin/env python3
"""
backtest.py — Walk-forward XGBoost prediction engine
=====================================================

ARCHITECTURE OVERVIEW
---------------------
This module is the core prediction engine. It does ONE thing: given a target
date and a model config, train an XGBoost model on all races before that date,
then predict win probabilities for every horse running that day.

Pipeline (run once per target date):

    ┌────────────────────────────────────────────────────────────────────┐
    │  1. LOAD       _load_today_rows()    CSV first, DB fallback         │
    │  2. ENGINEER   _engineer_features()  46 features × all horses       │
    │                  ├─ compute_win_rates()      Bayesian shrunk rates  │
    │                  ├─ compute_pace_styles()    Sectionals → style     │
    │                  ├─ compute_horse_history()  Gear/rating/class hist │
    │                  └─ build_horse_features()   Per-horse 46-vector    │
    │  3. TRAIN      _train_xgboost()      Walk-forward; only prior dates │
    │  4. PREDICT    .predict()            Raw probability per horse       │
    │  5. NORMALISE  _normalise_per_race() Probs sum to 1 within race     │
    │  6. TALLY      _tally_race()         Top-1 accuracy + bet P&L       │
    │  7. PERSIST    JSON dump             predictions.json + summary.json│
    └────────────────────────────────────────────────────────────────────┘

KEY DESIGN PRINCIPLES
---------------------
• Walk-forward ONLY — predictions for date D use ONLY data from dates < D.
  The runner re-trains the model for every target date. No look-ahead.

• Config drives behaviour — every tunable parameter lives in
  models/{name}/config.json. backtest.py is strategy-agnostic; it executes
  whatever the config specifies. To create a new variant, copy a config.

• Bayesian shrinkage — see smoothed() and ADVISORY.md §1. New horses,
  jockeys, and combinations are blended toward priors instead of scoring 0.0.

• Self-describing output — predictions.json embeds model name, version,
  strategy_type, generation timestamp, and full feature importance ranking.
  A downstream consumer can reconstruct what produced the file with no
  external context.

USAGE
-----
    python3 backtest.py --all                              # active model, all dates
    python3 backtest.py --model 均衡基礎策略 2026-05-03    # specific model + date
    python3 backtest.py --from 2026-05-01 --to 2026-05-31  # date range
    python3 backtest.py --all --force                      # overwrite existing
    python3 backtest.py --all --publish                    # copy → predictions/

EXTENSION POINTS (for programmers)
-----------------------------------
To add a new strategy_type (e.g. LightGBM, neural net):
  1. Write a sibling backtest_{type}.py module exposing run_date().
  2. Dispatch by cfg['strategy_type'] in main() (one-line switch).
  3. Configs use "strategy_type": "{type}" to opt in.

To add a new feature:
  1. Add it to FEATURES in model_config.py (with description, category).
  2. Compute it in build_horse_features() under the matching category.
  3. The model picks it up automatically — it reads FEATURE_COLS dynamically.

See ARCHITECTURE.md for the strategy-vs-tuning distinction and folder layout.
"""

import sys, os, json, argparse, sqlite3, time, re, shutil, math
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path
import numpy as np, pandas as pd
import xgboost as xgb
from sklearn.isotonic import IsotonicRegression
import warnings; warnings.filterwarnings('ignore')

import model_config as _mc
from model_config import FEATURE_COLS, load_config, results_dir

# ── Paths ─────────────────────────────────────────────────────────────────────

# ── Harville formula (H160) ────────────────────────────────────────────────────
# Converts calibrated per-race win probabilities into place (top-3), quinella,
# and quinella-place probabilities. Based on the assumption that conditional
# probabilities are proportional to win likelihood: P(j|i wins) = pj/(1-pi).
# All functions accept horse indices, NOT probability values, to avoid
# floating-point matching issues.

def harville_exacta_prob(idx_i: int, idx_j: int, prob_array: np.ndarray) -> float:
    """Probability horse at idx_i wins AND horse at idx_j finishes 2nd (exacta order)."""
    pi = prob_array[idx_i]
    pj = prob_array[idx_j]
    if 1.0 - pi < 1e-12:
        return 0.0
    return pi * pj / (1.0 - pi)

def harville_q_prob(idx_i: int, idx_j: int, prob_array: np.ndarray) -> float:
    """Probability horses at idx_i, idx_j are top-2 in any order (quinella)."""
    return harville_exacta_prob(idx_i, idx_j, prob_array) + \
           harville_exacta_prob(idx_j, idx_i, prob_array)

def harville_place_prob(idx: int, prob_array: np.ndarray) -> float:
    """Probability horse at idx finishes in top-3 (win, 2nd, or 3rd).

    P(i places) = P(i wins) + P(i 2nd) + P(i 3rd)
    P(i 2nd) = Σ_{j≠i} pj * pi/(1-pj)
    P(i 3rd) = Σ_{j≠i≠k} pj * pk/(1-pj) * pi/(1-pj-pk)
    """
    n = len(prob_array)
    pi = prob_array[idx]

    if n < 3:
        return pi + sum(harville_exacta_prob(j, idx, prob_array)
                        for j in range(n) if j != idx)

    place = pi

    for j in range(n):
        if j == idx:
            continue
        pj = prob_array[j]
        if 1.0 - pj < 1e-12:
            continue
        place += pj * pi / (1.0 - pj)

    for j in range(n):
        if j == idx:
            continue
        pj = prob_array[j]
        if 1.0 - pj < 1e-12:
            continue
        for k in range(n):
            if k == idx or k == j:
                continue
            pk = prob_array[k]
            denom = 1.0 - pj - pk
            if denom < 1e-12:
                continue
            place += pj * pk / (1.0 - pj) * pi / denom

    return place

def harville_qp_prob(idx_i: int, idx_j: int, prob_array: np.ndarray) -> float:
    """Probability both horses at idx_i, idx_j finish in top-3 (quinella-place).

    Sums all 6 orderings where i,j occupy two of the three top-3 slots,
    times the 3rd horse k, for each k ≠ i,j.
    """
    n = len(prob_array)
    pi = prob_array[idx_i]
    pj = prob_array[idx_j]
    qp = 0.0

    for k in range(n):
        if k == idx_i or k == idx_j:
            continue
        pk = prob_array[k]

        # i,j,k
        if 1.0 - pi > 1e-12 and 1.0 - pi - pj > 1e-12:
            qp += pi * pj/(1.0-pi) * pk/(1.0-pi-pj)
        # i,k,j
        if 1.0 - pi > 1e-12 and 1.0 - pi - pk > 1e-12:
            qp += pi * pk/(1.0-pi) * pj/(1.0-pi-pk)
        # j,i,k
        if 1.0 - pj > 1e-12 and 1.0 - pj - pi > 1e-12:
            qp += pj * pi/(1.0-pj) * pk/(1.0-pj-pi)
        # j,k,i
        if 1.0 - pj > 1e-12 and 1.0 - pj - pk > 1e-12:
            qp += pj * pk/(1.0-pj) * pi/(1.0-pj-pk)
        # k,i,j
        if 1.0 - pk > 1e-12 and 1.0 - pk - pi > 1e-12:
            qp += pk * pi/(1.0-pk) * pj/(1.0-pk-pi)
        # k,j,i
        if 1.0 - pk > 1e-12 and 1.0 - pk - pj > 1e-12:
            qp += pk * pj/(1.0-pk) * pi/(1.0-pk-pj)

    return qp

def compute_race_harville_probs(win_probs: np.ndarray) -> dict:
    """For a single race, compute all Harville derivative probabilities.

    Returns dict with keys:
      'place_probs': array of place (top-3) probabilities per horse
      'q_matrix':    NxN matrix of Q probabilities (upper triangular meaningful)
      'qp_matrix':   NxN matrix of QP probabilities (upper triangular meaningful)
    """
    n = len(win_probs)
    place_probs = np.array([harville_place_prob(i, win_probs) for i in range(n)])
    q_mat  = np.zeros((n, n))
    qp_mat = np.zeros((n, n))
    for i in range(n):
        for j in range(i + 1, n):
            qp_val = harville_q_prob(i, j, win_probs)
            q_mat[i][j] = q_mat[j][i] = qp_val
            qpp_val = harville_qp_prob(i, j, win_probs)
            qp_mat[i][j] = qp_mat[j][i] = qpp_val
    return {'place_probs': place_probs, 'q_matrix': q_mat, 'qp_matrix': qp_mat}
BASE    = Path(__file__).parent
DATA    = BASE / 'data'
PRED    = BASE / 'predictions'                  # production output dir
DB_PATH = DATA / 'racing.db'

# ── Default fallbacks (used when config is silent on a key) ───────────────────
# These exist so a partially-specified config still runs. Real configs should
# always override the relevant ones. See models/均衡基礎策略/config.json for
# the canonical, fully-specified example.
DEFAULT_AGE                  = 5
DEFAULT_WEIGHT_LBS           = 120
DEFAULT_DISTANCE_M           = 1400
DEFAULT_CLASS                = 4
DEFAULT_PARTICIPANTS         = 14
DEFAULT_DRAW                 = 7              # middle of typical field
DEFAULT_MAX_WT_IN_FIELD      = 135            # for weight_allow if no group data
DEFAULT_LATE_PACE            = 0.85           # neutral late-pace ratio
DEFAULT_EARLY_PACE           = 1.0            # neutral early-pace ratio
DEFAULT_OVERTAKE_DIST        = 0.0            # no position gain by default
DEFAULT_LAYOFF_DAYS          = 30             # for horses with no prior runs
DEFAULT_HORSE_STYLE_MIDFIELD = 2              # if sectionals data missing
MIN_TRAINING_ROWS            = 100            # below this, skip the date
MIN_ACTIVE_FEATURES          = 10             # below this, skip the date
NORMALISE_MIN_PROB_SUM       = 0.0            # >this triggers per-race rescale

# ── Odds-dimension probability calibration ─────────────────────────────────────
# Factors computed from 46,856 samples across all 9 strategies combined.
# For each odds bucket: factor = actual_hit_rate / model_avg_prob, capped at 1.0.
# The model over-estimates longshots (factors < 1.0) but is conservative on
# favourites (capped at 1.0 — don't up-correct). Factors applied via piecewise
# lookup with linear interpolation between bucket boundaries.
ODDS_CALIBRATION = {
    0:  1.0000,   # odds  0- 3x: actual 37.9% model 16.4% (model conservative, no up-correct)
    3:  1.0000,   # odds  3- 5x: actual 20.9% model 11.7%
    5:  1.0000,   # odds  5- 7x: actual 14.9% model 10.2%
    7:  1.0000,   # odds  7-10x: actual 10.7% model  9.9%
    10: 0.7327,   # odds 10-15x: actual  6.2% model  8.5%
    15: 0.5064,   # odds 15-25x: actual  4.0% model  7.9%
    25: 0.3006,   # odds 25-100x: actual  1.7% model  5.5%
}

def calibrate_prob(raw_prob: float, win_odds: float) -> float:
    """Apply odds-bucket calibration factor with linear interpolation."""
    odds = float(win_odds or 0)
    if odds <= 1.0:
        return raw_prob
    breaks = sorted(ODDS_CALIBRATION.keys())
    for i, b in enumerate(breaks):
        if odds < b:
            lo = breaks[i-1] if i > 0 else 0
            hi = b
            break
    else:
        lo, hi = breaks[-1], 100.0
    flo = ODDS_CALIBRATION.get(lo, 1.0)
    fhi = ODDS_CALIBRATION.get(hi, 0.15)
    factor = flo if hi == lo else flo + (fhi - flo) * (odds - lo) / (hi - lo)
    return raw_prob * factor


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
    """Return (horse_style_dict, horse_late_pace_dict, horse_early_pace_dict)."""
    hs_style = {}
    hlp = defaultdict(list)
    hep = defaultdict(list)

    for _, sr in hist_sec.iterrows():
        dt = sr['Date']; rn = sr['RaceNo']; course = sr['Course']
        ep = sr.get('EarlyPace')
        lp = sr.get('LatePace')
        match = hist[(hist['Date'] == dt) & (hist['RaceNo'] == rn) & (hist['Course'] == course)]
        for _, mr in match.iterrows():
            b = mr.get('BrandNo', 'X') or 'X'
            if pd.notna(ep):
                hs_style[b] = classify_early_pace(ep, ep_thresholds)
                hep[b].append(float(ep))
            if pd.notna(lp):
                hlp[b].append(float(lp))

    return hs_style, hlp, hep


def compute_horse_history(hist_rh: pd.DataFrame) -> tuple:
    """Return (gear_history, rating_history, class_history, overtake_dist_history) dicts."""
    gh  = defaultdict(list)
    hra = defaultdict(list)
    hcl = defaultdict(list)
    hod = defaultdict(list)   # avg positions gained from early running pos to finish

    for _, rr in hist_rh.iterrows():
        b = rr.get('BrandNo', '') or ''
        if not b: continue
        running_str = str(rr.get('Running', '') or '')
        if pd.notna(rr.get('Running')):
            gh[b].append(running_str[:2])
        if pd.notna(rr.get('Rating')):
            try: hra[b].append(float(rr['Rating']))
            except: pass
        if pd.notna(rr.get('Class')):
            try: hcl[b].append(float(rr['Class']))
            except: pass
        # Position gain: first running position minus final position (positive = overtook runners)
        try:
            parts = running_str.split()
            if parts:
                early_pos = int(parts[0])
                final_pos = int(rr.get('Pla', 0) or 0)
                if early_pos > 0 and final_pos > 0:
                    hod[b].append(float(early_pos - final_pos))
        except (ValueError, TypeError):
            pass

    return gh, hra, hcl, hod


# ── Per-horse feature vector ──────────────────────────────────────────────────

def build_horse_features(rw, grp, race_pace_str, rpi_field_score, stats, hs_style, hlp, hep, gh, hra, hcl, hod,
                          prof_dict, cutoff_date, cfg: dict) -> dict:
    """Compute all 46 features for a single horse in a race."""
    (xgb_p, n_rounds, going_map, pace_draw, pace_bucket, ep_thresh,
     inner_max, outer_min, layoff, wt_div, cold_thresh, chri,
     pace_match, form_days, rt_window, std_gear, feats_off) = _cfg_values(cfg)

    # Identity & raw inputs (with safe defaults when source data is sparse)
    b    = rw.get('BrandNo', 'X')  or 'X'
    j    = rw.get('JockeyCN', 'X') or 'X'
    t    = rw.get('TrainerCN','X') or 'X'
    dv   = int(rw['Draw'])         if pd.notna(rw.get('Draw'))    else DEFAULT_DRAW
    wt   = float(rw.get('ActWt')   or DEFAULT_WEIGHT_LBS)
    dist = float(rw.get('Distance')or DEFAULT_DISTANCE_M)
    cls  = float(rw.get('Class')   or DEFAULT_CLASS)
    part = float(rw.get('Participants') or DEFAULT_PARTICIPANTS)
    odds = float(rw.get('Odds') or 0)
    gv   = going_map.get(str(rw.get('Going', 'Good') or 'Good'), 0)
    ds   = str(rw.get('Distance', ''))
    pi   = prof_dict.get(b, {})
    hs_v = hs_style.get(b, DEFAULT_HORSE_STYLE_MIDFIELD)

    HS  = stats['HS'];   JS  = stats['JS'];   TS  = stats['TS']
    JTS = stats['JTS'];  JHS = stats['JHS']
    HDS = stats['HDS'];  HGS = stats['HGS']
    SS  = stats['SS'];   last_d = stats['last_d']

    fmw = grp['ActWt'].max() if 'ActWt' in grp.columns else DEFAULT_MAX_WT_IN_FIELD
    going_str = str(rw.get('Going', 'Good'))   # used in both Adaptability and going_num

    f = {}

    # ── Horse Profile ────────────────────────────────────────────────────
    f['age']         = pi.get('Age', DEFAULT_AGE) or DEFAULT_AGE
    f['sex_gelding'] = 1 if 'gelding' in str(pi.get('Sex', '')).lower() else 0
    # Synthesise rating from weight+class if HKJC didn't report one
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
    f['days_since'] = min((date - ld).days, layoff.get('max_days', 365)) if date > ld else DEFAULT_LAYOFF_DAYS
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
    f['late_pace_avg']    = np.mean(lps) if lps else DEFAULT_LATE_PACE
    eps = hep.get(b, [])
    f['early_pace_avg']   = np.mean(eps) if eps else DEFAULT_EARLY_PACE
    ods = hod.get(b, [])
    f['avg_overtake_dist'] = np.mean(ods) if ods else DEFAULT_OVERTAKE_DIST

    # ── RPI (Race Pace Index, H86–H88) ───────────────────────────────────
    f['rpi_field_score']   = float(rpi_field_score)
    eps_list = hep.get(b, [])
    lps_list = hlp.get(b, [])
    if len(eps_list) >= 2:
        f['rpi_pace_deviation'] = float(np.std(eps_list))
    else:
        f['rpi_pace_deviation'] = 0.0
    if eps_list and lps_list:
        ep_avg = np.mean(eps_list)
        lp_avg = np.mean(lps_list)
        f['rpi_pace_ratio'] = float(ep_avg / max(lp_avg, 1e-6))
    else:
        f['rpi_pace_ratio'] = 1.0

    # ── Q pair proxies (H3 quick version) ────────────────────────────────
    # Complementary running-style pairs: leader(0)+closer(3), leader(0)+midfield(2),
    # stalker(1)+closer(3), stalker(1)+midfield(2)
    COMPAT_PAIRS = {(0,3), (3,0), (0,2), (2,0), (1,3), (3,1), (1,2), (2,1)}
    my_style = hs_style.get(b, 2)
    compat_count = 0
    for _, other_rw in grp.iterrows():
        ob = other_rw.get('BrandNo', '') or ''
        if ob == b: continue
        other_style = hs_style.get(ob, 2)
        if (my_style, other_style) in COMPAT_PAIRS:
            compat_count += 1
    f['q_style_compat'] = float(compat_count)

    # Strong-jockey count: other horses with jockey WR > 15%
    # JS gives {'w': wins, 'r': races} per jockey name
    strong_jockey_count = 0
    js = stats.get('JS', {})
    for _, other_rw in grp.iterrows():
        ob = other_rw.get('BrandNo', '') or ''
        if ob == b: continue
        j_name = other_rw.get('JockeyCN', '') or ''
        j_data = js.get(j_name, {})
        j_races = j_data.get('r', 0)
        j_wins  = j_data.get('w', 0)
        j_wr = j_wins / max(j_races, 1)
        if j_wr > 0.15:
            strong_jockey_count += 1
    f['q_field_strength'] = float(strong_jockey_count)

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
    hs_style, hlp, hep = compute_pace_styles(sec_hist, res_hist, ep_thresh)
    gh, hra, hcl, hod  = compute_horse_history(rh_hist)

    feat_rows = []
    for (date, course, race_no), grp in rows.groupby(['Date', 'Course', 'RaceNo']):
        styles        = [hs_style.get(rw.get('BrandNo', 'X') or 'X', 2) for _, rw in grp.iterrows()]
        race_pace_str = classify_race_pace(styles, pace_bucket)
        # RPI: continuous field pace score (-1 slow → +1 fast)
        n_styles      = max(len(styles), 1)
        leaders_pct   = sum(1 for s in styles if s == 0) / n_styles
        closers_pct   = sum(1 for s in styles if s == 3) / n_styles
        rpi_field     = closers_pct - leaders_pct   # positive = fast pace expected
        for _, rw in grp.iterrows():
            f = build_horse_features(rw, grp, race_pace_str, rpi_field, stats, hs_style, hlp, hep,
                                     gh, hra, hcl, hod, prof_dict, cutoff_date, cfg)
            feat_rows.append(f)

    return pd.DataFrame(feat_rows)


# ── Per-date prediction (pipeline) ────────────────────────────────────────────
#
# run_date() orchestrates the 7-step pipeline below. Each helper handles one
# phase. The split exists so each phase can be tested or replaced in isolation
# without rewriting the orchestrator.
# ──────────────────────────────────────────────────────────────────────────────


def _load_today_rows(date_str: str, res_csv: pd.DataFrame):
    """PHASE 1 — LOAD.

    Resolve today's race rows. CSV is the primary source (historical data);
    if today's date isn't in the CSV, fall back to the DB (recently-scraped).
    Returns (rows, source_label) or (None, None) if no data exists.
    """
    target_date = datetime.strptime(date_str, '%Y-%m-%d')
    csv_rows = res_csv[res_csv['Date'] == target_date]
    if len(csv_rows) > 0:
        return csv_rows, 'CSV'
    db_rows = load_db_rows(date_str)
    if len(db_rows) > 0:
        return db_rows, 'DB'
    return None, None


def _engineer_features(today_rows, target_date, res_csv, sec, prof_dict, rh, cfg):
    """PHASE 2 — ENGINEER feature matrices for today + all prior dates.

    Both matrices use the SAME stat accumulators (computed from history before
    target_date) so feature distributions are consistent across train and test.
    Returns (today_feats, train_feats, feat_cols) or (None, None, None) if
    insufficient data.
    """
    res_hist = res_csv[res_csv['Date'] < target_date]
    sec_hist = sec   [sec  ['Date'] < target_date]
    rh_hist  = rh    [rh   ['Date'] < target_date]

    if len(res_hist) < MIN_TRAINING_ROWS:
        print(f"  insufficient training data ({len(res_hist)} rows)")
        return None, None, None

    today_feats = build_features(today_rows, target_date, res_hist, sec_hist, prof_dict, rh_hist, cfg)
    train_feats = build_features(res_hist,   target_date, res_hist, sec_hist, prof_dict, rh_hist, cfg)

    if len(today_feats) == 0 or len(train_feats) == 0:
        print(f"  empty feature matrix")
        return None, None, None

    disabled  = set(cfg.get('features_disabled', []))
    feat_cols = [c for c in FEATURE_COLS if c not in disabled
                 and c in today_feats.columns and c in train_feats.columns]
    if len(feat_cols) < MIN_ACTIVE_FEATURES:
        print(f"  too few features ({len(feat_cols)})")
        return None, None, None

    return today_feats, train_feats, feat_cols


def _train_and_predict(train_feats, today_feats, feat_cols, cfg):
    """PHASES 3 + 4 — TRAIN XGBoost + optional isotonic calibration, then PREDICT.

    Isotonic calibration (H99): if use_isotonic_calibration=true in config, holds
    out the last 20% of training rows (time-ordered) to fit IsotonicRegression on
    out-of-sample XGBoost scores, then maps today's raw scores through it. This
    corrects systematic over/under-estimation without leaking future data.

    Returns (probs, feat_weights, feat_sorted_by_importance).
    """
    xgb_params   = cfg.get('xgb', {})
    n_rounds     = cfg.get('num_boost_rounds', 100)
    use_isotonic = cfg.get('use_isotonic_calibration', False)

    X_train = train_feats[feat_cols].fillna(0)
    y_train = train_feats['won'].values
    X_test  = today_feats[feat_cols].fillna(0)

    dtrain = xgb.DMatrix(X_train, label=y_train)
    dtest  = xgb.DMatrix(X_test)
    model  = xgb.train(xgb_params, dtrain, num_boost_round=n_rounds)
    raw_probs = model.predict(dtest)

    if use_isotonic and len(X_train) > 300:
        # Hold out last 20% of training (time-ordered) as calibration set.
        # Train a parallel model on the 80% portion to get OOS predictions.
        n_cal = max(int(len(X_train) * 0.2), 50)
        X_fit = X_train.iloc[:-n_cal];  y_fit = y_train[:-n_cal]
        X_cal = X_train.iloc[-n_cal:];  y_cal = y_train[-n_cal:]
        if len(X_fit) >= MIN_TRAINING_ROWS:
            m_cal = xgb.train(xgb_params,
                              xgb.DMatrix(X_fit, label=y_fit),
                              num_boost_round=n_rounds)
            cal_preds = m_cal.predict(xgb.DMatrix(X_cal))
            ir = IsotonicRegression(out_of_bounds='clip')
            ir.fit(cal_preds, y_cal)
            raw_probs = ir.transform(raw_probs)

    importance   = model.get_score(importance_type='gain')
    feat_weights = {fn: float(importance.get(fn, 0.0)) for fn in feat_cols}
    feat_sorted  = sorted(feat_cols, key=lambda f: feat_weights[f], reverse=True)
    return raw_probs, feat_weights, feat_sorted


def _normalise_per_race(today_feats, raw_probs):
    """PHASE 5 — NORMALISE raw probabilities so each race sums to 1.0.

    XGBoost outputs unnormalised P(win) per horse, computed independently.
    We rescale so that within each race the probabilities form a valid
    distribution (necessary for Harville-derived exotic prices later).
    """
    today_feats = today_feats.copy()
    today_feats['prob']     = raw_probs
    today_feats['win_prob'] = raw_probs
    for rn in today_feats['race_no'].unique():
        mask  = today_feats['race_no'] == rn
        raw   = today_feats.loc[mask, 'win_prob'].values
        total = raw.sum()
        if total > NORMALISE_MIN_PROB_SUM:
            today_feats.loc[mask, 'win_prob'] = raw / total
    return today_feats


def _safe_int(v, default: int = 0) -> int:
    """Convert v to int, returning default for None, NaN, or un-parseable values."""
    try:
        if v is None or v != v:   # v != v is True only for NaN
            return default
        return int(v or default)
    except (TypeError, ValueError):
        return default


def _resolve_horse_name(brand: str, today_rows: pd.DataFrame) -> str:
    """Extract a clean horse name from today_rows by brand number.

    Handles both CSV-source rows (HorseCN with embedded '(brand)') and DB-source
    rows (horse_name already clean). Returns '' if no match.
    """
    if 'HorseCN' in today_rows.columns:
        hrow = today_rows[today_rows['BrandNo'] == brand]
        if len(hrow):
            return re.sub(r'\s*\([A-Z]\d+\)', '', str(hrow['HorseCN'].iloc[0])).strip()
    if 'horse_name' in today_rows.columns:
        hrow = today_rows[today_rows['BrandNo'] == brand]
        if len(hrow):
            return str(hrow['horse_name'].iloc[0])
    return ''


def _build_horse_record(tr, today_rows, feat_sorted, harville: dict = None, horse_idx: int = 0) -> dict:
    """Build one horse's output record from its feature row + source row.

    The record is the JSON shape consumed by app.py and the UI: identity
    fields (no, name, brand, jockey, trainer), today's setup (draw, weight,
    rating, win_odds), probability fields (prob, win_prob, place_prob, edge),
    and a sorted feature snapshot for explanation.
    """
    brand = tr['node']
    if 'BrandNo' in today_rows.columns:
        orig = today_rows[today_rows['BrandNo'] == brand]
    else:
        orig = pd.DataFrame()
    jockey  = orig['JockeyCN'].iloc[0]  if len(orig) and 'JockeyCN'  in orig.columns else ''
    trainer = orig['TrainerCN'].iloc[0] if len(orig) and 'TrainerCN' in orig.columns else ''

    rec = {
        'no':       '',           # filled by caller after enumeration
        'name':     _resolve_horse_name(brand, today_rows),
        'brand':    brand,
        'jockey':   str(jockey),
        'trainer':  str(trainer),
        'draw':     str(_safe_int(tr.get('draw',   0))),
        'weight':   str(_safe_int(tr.get('weight', 0))),
        'rating':   str(_safe_int(tr.get('rating', 0))),
        'win_odds': str(tr.get('odds_raw', '')),
        'prob':     round(float(tr['prob']),     4),
        'win_prob': round(float(tr['win_prob']), 4),
        'edge':     round(float(tr['win_prob']) * float(tr.get('odds_raw') or 0), 2),
        'features': {c: round(float(tr.get(c, 0) or 0), 4) for c in feat_sorted},
    }

    if harville is not None:
        rec['place_prob'] = round(float(harville['place_probs'][horse_idx]), 4)
        rec['place_edge'] = round(float(harville['place_probs'][horse_idx]) * float(tr.get('odds_raw') or 0), 2)

    return rec


def _tally_race(horses, today_feats, race_no_int, cfg, accum):
    """PHASE 6 — TALLY top-1 accuracy + bet P&L for one race.

    Mutates `accum`:
        top1_races / top1_correct — accuracy tracking
        bets_placed / bets_won / units_staked / units_net — P&L
        hard_stopped / hard_stopped_bets — bets blocked by bet_max_odds (for RCA)
    Bet sizing: flat 1u when kelly_fraction=0 (default), or fractional Kelly
    when kelly_fraction>0. Kelly f = (p*odds - 1) / (odds - 1), capped at
    kelly_max_bet to prevent ruinous single-race exposure.
    """
    if not horses:
        return

    bet_edge_threshold = cfg.get('bet_edge_threshold', 1.0)
    bet_min_odds       = cfg.get('bet_min_odds', 0.0)
    bet_max_odds       = cfg.get('bet_max_odds', 999.0)
    kelly_fraction     = cfg.get('kelly_fraction', 0.0)
    kelly_max_bet      = cfg.get('kelly_max_bet', 5.0)

    # Top-1 accuracy
    accum['top1_races'] += 1
    predicted_winner = max(horses, key=lambda h: h['win_prob'])
    actual_winners = {
        h['brand'] for h in horses
        if int(today_feats[
            (today_feats['node']    == h['brand']) &
            (today_feats['race_no'] == race_no_int)
        ]['won'].values[0]) == 1
    }
    if predicted_winner['brand'] in actual_winners:
        accum['top1_correct'] += 1

    for h in horses:
        odds_val = float(h.get('win_odds') or 0)
        if math.isnan(odds_val) or odds_val <= 1.0 or odds_val < bet_min_odds:
            continue

        # Calibrated edge: shrink model probability toward market at high odds
        raw_prob = float(h.get('win_prob') or 0)
        cal_prob = calibrate_prob(raw_prob, odds_val)
        cal_edge = cal_prob * odds_val

        if cal_edge <= bet_edge_threshold:
            continue

        if odds_val > bet_max_odds:
            accum['hard_stopped'] += 1
            accum['hard_stopped_bets'].append({
                'race':     race_no_int,
                'brand':    h['brand'],
                'name':     h.get('name', ''),
                'odds':     round(odds_val, 1),
                'edge':     round(float(h['edge']), 2),
                'cal_edge': round(cal_edge, 2),
                'win_prob': round(float(h['win_prob']), 4),
                'cal_prob': round(cal_prob, 4),
            })
            continue

        # Bet sizing: fractional Kelly or flat 1 unit.
        if kelly_fraction > 0:
            b_net = odds_val - 1.0
            kelly_f  = max(0.0, (cal_prob * odds_val - 1.0) / b_net)
            bet_size = min(kelly_f * kelly_fraction, kelly_max_bet)
            if bet_size < 0.01:
                continue
        else:
            bet_size = 1.0

        accum['bets_placed']  += 1
        accum['units_staked'] += bet_size
        if h['brand'] in actual_winners:
            accum['bets_won']  += 1
            accum['units_net'] += bet_size * (odds_val - 1.0)
        else:
            accum['units_net'] -= bet_size


def _format_race_meta(today_rows, race_no_int) -> tuple:
    """Extract (distance, class_str) for the race header in output JSON."""
    rinfo = today_rows[today_rows['RaceNo'] == race_no_int]
    dist  = (str(int(rinfo['Distance'].iloc[0]))
             if len(rinfo) and pd.notna(rinfo['Distance'].iloc[0]) else '')
    cls_raw = rinfo['Class'].iloc[0] if len(rinfo) else ''
    cls_str = (str(int(cls_raw)) + '班'
               if pd.notna(cls_raw) and str(cls_raw) not in ('', 'nan') else '')
    return dist, cls_str


def run_date(date_str: str, res_csv, sec, prof_dict, rh,
             cfg: dict, out_dir: Path, force=False) -> dict:
    """Generate predictions for ONE target date.

    This is the orchestrator. The actual work happens in the _phase helpers.
    Returns a result dict for the summary aggregator, or None if skipped.
    """
    out_path = out_dir / date_str / 'predictions.json'
    if out_path.exists() and not force:
        print(f"  {date_str}: already exists — skip (use --force to overwrite)")
        return None

    # 1. LOAD
    today_rows, source = _load_today_rows(date_str, res_csv)
    if today_rows is None:
        print(f"  {date_str}: no data found — skip")
        return None

    target_date = datetime.strptime(date_str, '%Y-%m-%d')
    t0 = time.time()

    # 2. ENGINEER
    today_feats, train_feats, feat_cols = _engineer_features(
        today_rows, target_date, res_csv, sec, prof_dict, rh, cfg)
    if today_feats is None:
        return None

    # 3. + 4. TRAIN + PREDICT
    raw_probs, feat_weights, feat_sorted = _train_and_predict(
        train_feats, today_feats, feat_cols, cfg)

    # 5. NORMALISE per race
    today_feats = _normalise_per_race(today_feats, raw_probs)

    # 6. TALLY (top-1 + bet P&L) WHILE building the output JSON
    output = {}
    accum  = {'top1_correct': 0, 'top1_races': 0,
              'bets_placed':  0, 'bets_won':   0,
              'units_staked': 0.0, 'units_net':  0.0,
              'hard_stopped': 0, 'hard_stopped_bets': []}

    for race_no, grp in today_feats.groupby('race_no'):
        race_no_int = int(race_no)
        dist, cls_str = _format_race_meta(today_rows, race_no_int)

        horses = []
        for _, tr in grp.iterrows():
            rec = _build_horse_record(tr, today_rows, feat_sorted)
            rec['no'] = str(len(horses) + 1)
            horses.append(rec)

        # ── Harville derivative probabilities (H160) ────────────────
        win_probs = np.array([float(h['win_prob']) for h in horses])
        if len(win_probs) >= 2:
            hv = compute_race_harville_probs(win_probs)
            for idx, h in enumerate(horses):
                h['place_prob'] = round(float(hv['place_probs'][idx]), 4)
                h['place_edge'] = round(float(hv['place_probs'][idx]) * float(h.get('win_odds') or 0), 2)

            # Top-5 Q pairs by probability
            q_pairs = []
            n = len(win_probs)
            for a in range(n):
                for b in range(a + 1, n):
                    q_pairs.append({
                        'brands': [horses[a]['brand'], horses[b]['brand']],
                        'q_prob': round(float(hv['q_matrix'][a][b]), 4),
                        'qp_prob': round(float(hv['qp_matrix'][a][b]), 4),
                    })
            q_pairs.sort(key=lambda x: x['q_prob'], reverse=True)
        else:
            hv = None
            q_pairs = []

        _tally_race(horses, today_feats, race_no_int, cfg, accum)

        rn_key = str(race_no_int).zfill(2)
        output[rn_key] = {'distance': dist, 'class': cls_str, 'horses': horses}
        if q_pairs:
            output[rn_key]['q_pairs'] = q_pairs[:5]

    # 7. PERSIST — embed model metadata then write the file
    output['_model']          = cfg.get('name', '')
    output['_version']        = cfg.get('version', '')
    output['_strategy_type']  = cfg.get('strategy_type', 'xgb_walkforward')
    output['_generated_at']   = datetime.now().isoformat(timespec='seconds')
    output['_feature_cols']   = feat_sorted
    output['_feature_weights']= {k: round(v, 1) for k, v in feat_weights.items()}
    if accum['hard_stopped']:
        output['_hard_stopped_count'] = accum['hard_stopped']
        output['_hard_stopped_bets']  = accum['hard_stopped_bets']

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as fh:
        json.dump(output, fh, ensure_ascii=False, indent=2, default=str)

    # Reporting line
    elapsed  = time.time() - t0
    races_n  = sum(1 for k in output if not k.startswith('_'))
    horses_n = sum(len(output[k]['horses']) for k in output if not k.startswith('_'))
    top1_acc = accum['top1_correct'] / accum['top1_races'] if accum['top1_races'] else 0
    print(f"  {date_str} [{source}]: {races_n} races, {horses_n} horses, "
          f"top1={top1_acc:.0%} → saved  ({elapsed:.0f}s)")

    roi = (round(accum['units_net'] / accum['units_staked'], 4)
           if accum['units_staked'] > 0 else None)
    return {
        'date':         date_str,
        'races':        races_n,
        'horses':       horses_n,
        'top1_correct': accum['top1_correct'],
        'top1_races':   accum['top1_races'],
        'bets_placed':  accum['bets_placed'],
        'bets_won':     accum['bets_won'],
        'units_staked': round(accum['units_staked'], 1),
        'units_net':    round(accum['units_net'], 2),
        'roi':          roi,
        'hard_stopped': accum['hard_stopped'],
        'elapsed':      round(elapsed, 1),
    }


def update_summary(model_name: str, results: list):
    """Write/update models/{name}/results/summary.json with aggregate stats.

    Merges the newly-run dates with any existing per-date rows in summary.json
    so that running one date at a time (e.g. from the batch job) accumulates
    correctly instead of overwriting the whole summary with just one row.
    """
    new_by_date = {r['date']: r for r in results if r}

    # Load existing per_date rows and merge (new rows win on conflict)
    summary_path = results_dir(model_name) / 'summary.json'
    merged: dict[str, dict] = {}
    if summary_path.exists():
        try:
            existing = json.load(open(summary_path))
            for row in existing.get('per_date', []):
                merged[row['date']] = row
        except Exception:
            pass
    merged.update(new_by_date)

    valid = list(merged.values())
    if not valid: return
    total_races   = sum(r['top1_races']   for r in valid)
    total_correct = sum(r['top1_correct'] for r in valid)
    total_bets    = sum(r.get('bets_placed', 0)   for r in valid)
    total_bet_won = sum(r.get('bets_won', 0)       for r in valid)
    total_staked  = sum(r.get('units_staked', 0.0) for r in valid)
    total_net     = sum(r.get('units_net', 0.0)    for r in valid)

    top1_acc = round(total_correct / total_races, 4) if total_races else 0
    roi      = round(total_net / total_staked, 4)    if total_staked > 0 else None

    cfg_for_summary = {}
    try:
        from model_config import load_config as _lc, bet_params_hash, model_params_hash
        cfg_for_summary = _lc(model_name)
        _bet_hash   = bet_params_hash(cfg_for_summary)
        _model_hash = model_params_hash(cfg_for_summary)
    except Exception:
        _bet_hash = _model_hash = ''

    summary = {
        'model':         model_name,
        'version':       cfg_for_summary.get('version', ''),
        'strategy_type': cfg_for_summary.get('strategy_type', 'xgb_walkforward'),
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
        'bet_hash':      _bet_hash,
        'model_hash':    _model_hash,
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


def retally(model_name: str, verbose: bool = False) -> dict:
    """Re-apply current bet-filter params to existing per-date predictions.json
    and regenerate summary.json without re-training the model.

    Uses the SQLite DB to look up actual winners, placegetters, and dividends
    for backtested dates. Supports WIN, PLACE, QIN, and QPL bet evaluation
    via the Harville formula (H160).
    """
    from model_config import load_config as _lc, bet_params_hash, model_params_hash

    cfg      = _lc(model_name)
    out_dir  = results_dir(model_name)
    bet_edge = float(cfg.get('bet_edge_threshold') or 1.0)
    bet_min  = float(cfg.get('bet_min_odds')  or 0.0)
    bet_max  = float(cfg.get('bet_max_odds')  or 999.0)
    place_edge = float(cfg.get('place_edge_threshold', 0.0) or 0.0)
    q_edge     = float(cfg.get('q_edge_threshold', 0.0) or 0.0)
    qp_edge    = float(cfg.get('qp_edge_threshold', 0.0) or 0.0)
    q_top_n    = int(cfg.get('q_top_n', 5))

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    accum = {
        'dates_run': 0, 'total_races': 0,
        'top1_correct': 0, 'top1_races': 0,
        'bets_placed': 0,  'bets_won': 0,
        'units_staked': 0.0, 'units_net': 0.0,
        'place_bets_placed': 0, 'place_bets_won': 0,
        'place_units_staked': 0.0, 'place_units_net': 0.0,
        'q_bets_placed': 0, 'q_bets_won': 0,
        'q_units_staked': 0.0, 'q_units_net': 0.0,
        'qp_bets_placed': 0, 'qp_bets_won': 0,
        'qp_units_staked': 0.0, 'qp_units_net': 0.0,
        'per_date': [],
    }

    date_dirs = sorted(
        d for d in out_dir.iterdir()
        if d.is_dir() and re.match(r'\d{4}-\d{2}-\d{2}$', d.name)
    )

    for date_dir in date_dirs:
        date_str  = date_dir.name
        pred_file = date_dir / 'predictions.json'
        if not pred_file.exists():
            continue
        try:
            preds = json.loads(pred_file.read_text(encoding='utf-8'))
        except Exception as e:
            if verbose:
                print(f"  {date_str}: skip — {e}")
            continue

        # Actual winners: race_no -> set of winning brands
        winners: dict[str, set] = {}
        # Top-2 finishers for Q: race_no -> set of brands
        top2: dict[str, set] = {}
        # Actual placegetters (top-3): race_no -> set of brands
        placers: dict[str, set] = {}
        for row in conn.execute(
            "SELECT race_no, brand, position FROM results WHERE date=? AND position IN (1,2,3)",
            (date_str,)
        ).fetchall():
            rn = str(int(row['race_no']))
            brand = row['brand']
            pos = int(row['position'])
            winners.setdefault(rn, set())
            top2.setdefault(rn, set())
            placers.setdefault(rn, set())
            if pos == 1:
                winners[rn].add(brand)
            if pos <= 2:
                top2[rn].add(brand)
            placers[rn].add(brand)

        # Dividends for this date: (course,race_no,pool,comb) -> dividend
        div_lookup: dict = {}
        for row in conn.execute(
            "SELECT course, race_no, pool, combination, dividend FROM dividends WHERE date=?",
            (date_str,)
        ).fetchall():
            key = (row['course'], row['race_no'], row['pool'], row['combination'])
            div_lookup[key] = row['dividend']

        day = {'date': date_str, 'races': 0, 'horses': 0,
               'top1_correct': 0, 'top1_races': 0,
               'bets_placed': 0,  'bets_won': 0,
               'units_staked': 0.0, 'units_net': 0.0, 'roi': 0.0,
               'place_bets_placed': 0, 'place_bets_won': 0,
               'place_units_staked': 0.0, 'place_units_net': 0.0, 'place_roi': 0.0,
               'q_bets_placed': 0, 'q_bets_won': 0,
               'q_units_staked': 0.0, 'q_units_net': 0.0, 'q_roi': 0.0,
               'qp_bets_placed': 0, 'qp_bets_won': 0,
               'qp_units_staked': 0.0, 'qp_units_net': 0.0, 'qp_roi': 0.0}

        course_for_date = ''
        for rk, race in preds.items():
            if rk.startswith('_'):
                continue
            horses = race.get('horses', []) if isinstance(race, dict) else []
            if not horses:
                continue
            rn      = str(int(rk))
            win_set = winners.get(rn, set())
            top2_set = top2.get(rn, set())
            plc_set = placers.get(rn, set())
            day['races']  += 1
            day['horses'] += len(horses)

            # Detect course from first race
            if not course_for_date:
                course_for_date = _detect_course_from_predictions(date_str, rk, conn)

            # Top-1: highest win_prob
            best = max(horses, key=lambda h: float(h.get('win_prob') or 0))
            day['top1_races'] += 1
            if best.get('brand') in win_set:
                day['top1_correct'] += 1

            # ── WIN bets ──────────────────────────────────────────
            for h in horses:
                odds = float(h.get('win_odds') or 0)
                if odds <= 1.0 or odds < bet_min or odds > bet_max:
                    continue
                raw_prob = float(h.get('win_prob') or 0)
                cal_prob = calibrate_prob(raw_prob, odds)
                edge = cal_prob * odds
                if edge <= bet_edge:
                    continue
                day['bets_placed']  += 1
                day['units_staked'] += 1.0
                if h.get('brand') in win_set:
                    day['bets_won']  += 1
                    day['units_net'] += odds - 1.0
                else:
                    day['units_net'] -= 1.0

            # ── Harville probs for this race ─────────────────────
            n = len(horses)
            win_probs = np.array([float(h.get('win_prob') or 0) for h in horses])
            hv = compute_race_harville_probs(win_probs) if n >= 2 else None

            # ── PLACE bets (via Harville) ────────────────────────
            if hv is not None and place_edge > 0:
                for idx, h in enumerate(horses):
                    place_prob = float(hv['place_probs'][idx])
                    # Look up place dividend for this brand
                    brand = h.get('brand', '')
                    div_key = (course_for_date, int(rn), 'PLACE', brand)
                    place_div = div_lookup.get(div_key, 0.0)
                    if place_div <= 1.0:
                        continue
                    place_edge_val = place_prob * place_div
                    if place_edge_val <= place_edge:
                        continue
                    day['place_bets_placed']  += 1
                    day['place_units_staked'] += 1.0
                    if brand in plc_set:
                        day['place_bets_won']  += 1
                        day['place_units_net'] += place_div - 1.0
                    else:
                        day['place_units_net'] -= 1.0

            # ── Q / QP bets (via Harville) ───────────────────────
            if hv is not None and n >= 2 and (q_edge > 0 or qp_edge > 0):
                q_pairs = []
                for a in range(n):
                    for b in range(a + 1, n):
                        q_pairs.append((a, b, hv['q_matrix'][a][b], hv['qp_matrix'][a][b]))
                q_pairs.sort(key=lambda x: x[2], reverse=True)

                for a, b, qp, qpp in q_pairs[:q_top_n]:
                    brands = sorted([horses[a]['brand'], horses[b]['brand']])
                    comb   = ','.join(brands)
                    # Q bet
                    if q_edge > 0:
                        q_div_key = (course_for_date, int(rn), 'QIN', comb)
                        q_div = div_lookup.get(q_div_key, 0.0)
                        q_edge_val = qp * q_div
                        if q_div > 1.0 and q_edge_val > q_edge:
                            day['q_bets_placed']  += 1
                            day['q_units_staked'] += 1.0
                            if brands[0] in top2_set and brands[1] in top2_set:
                                day['q_bets_won']  += 1
                                day['q_units_net'] += q_div - 1.0
                            else:
                                day['q_units_net'] -= 1.0
                    # QP bet
                    if qp_edge > 0:
                        qp_div_key = (course_for_date, int(rn), 'QPL', comb)
                        qp_div = div_lookup.get(qp_div_key, 0.0)
                        qp_edge_val = qpp * qp_div
                        if qp_div > 1.0 and qp_edge_val > qp_edge:
                            day['qp_bets_placed']  += 1
                            day['qp_units_staked'] += 1.0
                            if brands[0] in plc_set and brands[1] in plc_set:
                                day['qp_bets_won']  += 1
                                day['qp_units_net'] += qp_div - 1.0
                            else:
                                day['qp_units_net'] -= 1.0

        # ── Round day figures ───────────────────────────────────
        day['units_staked'] = round(day['units_staked'], 1)
        day['units_net']    = round(day['units_net'], 2)
        day['roi'] = (round(day['units_net'] / day['units_staked'], 4)
                      if day['units_staked'] else 0.0)
        day['place_units_staked'] = round(day['place_units_staked'], 1)
        day['place_units_net']    = round(day['place_units_net'], 2)
        day['place_roi'] = (round(day['place_units_net'] / day['place_units_staked'], 4)
                            if day['place_units_staked'] else 0.0)
        day['q_units_staked'] = round(day['q_units_staked'], 1)
        day['q_units_net']    = round(day['q_units_net'], 2)
        day['q_roi'] = (round(day['q_units_net'] / day['q_units_staked'], 4)
                         if day['q_units_staked'] else 0.0)
        day['qp_units_staked'] = round(day['qp_units_staked'], 1)
        day['qp_units_net']    = round(day['qp_units_net'], 2)
        day['qp_roi'] = (round(day['qp_units_net'] / day['qp_units_staked'], 4)
                          if day['qp_units_staked'] else 0.0)

        # ── Accumulate ──────────────────────────────────────────
        accum['dates_run']   += 1
        accum['total_races'] += day['races']
        for key in ('top1_correct', 'top1_races',
                    'bets_placed', 'bets_won',
                    'place_bets_placed', 'place_bets_won',
                    'q_bets_placed', 'q_bets_won',
                    'qp_bets_placed', 'qp_bets_won'):
            accum[key] += day.get(key, 0)
        accum['units_staked']       += day['units_staked']
        accum['units_net']          += day['units_net']
        accum['place_units_staked'] += day['place_units_staked']
        accum['place_units_net']    += day['place_units_net']
        accum['q_units_staked']     += day['q_units_staked']
        accum['q_units_net']        += day['q_units_net']
        accum['qp_units_staked']    += day['qp_units_staked']
        accum['qp_units_net']       += day['qp_units_net']
        accum['per_date'].append(day)

        if verbose:
            print(f"  {date_str}: {day['races']}場 "
                  f"win={day['bets_placed']}/{day['bets_won']} net={day['units_net']:+.2f} "
                  f"plc={day['place_bets_placed']}/{day['place_bets_won']} net={day['place_units_net']:+.2f} "
                  f"q={day['q_bets_placed']}/{day['q_bets_won']} net={day['q_units_net']:+.2f}")

    conn.close()

    # ── Final summary ───────────────────────────────────────────
    def _roi(s, n): return round(n / s, 4) if s > 0 else None
    roi      = _roi(accum['units_staked'], accum['units_net'])
    place_roi= _roi(accum['place_units_staked'], accum['place_units_net'])
    q_roi    = _roi(accum['q_units_staked'], accum['q_units_net'])
    qp_roi   = _roi(accum['qp_units_staked'], accum['qp_units_net'])
    top1_acc = (round(accum['top1_correct'] / accum['top1_races'], 4)
                if accum['top1_races'] else 0.0)

    summary = {
        'model':         model_name,
        'version':       cfg.get('version', ''),
        'strategy_type': cfg.get('strategy_type', 'xgb_walkforward'),
        'dates_run':     accum['dates_run'],
        'total_races':   accum['total_races'],
        'top1_accuracy': top1_acc,
        'top1_pct':      round(top1_acc * 100, 1),
        'bets_placed':   accum['bets_placed'],
        'bets_won':      accum['bets_won'],
        'units_staked':  round(accum['units_staked'], 1),
        'units_net':     round(accum['units_net'], 2),
        'roi':           roi,
        'roi_units':     round(accum['units_net'], 2),
        'place_bets_placed': accum['place_bets_placed'],
        'place_bets_won':    accum['place_bets_won'],
        'place_units_staked': round(accum['place_units_staked'], 1),
        'place_units_net':    round(accum['place_units_net'], 2),
        'place_roi':          place_roi,
        'q_bets_placed':  accum['q_bets_placed'],
        'q_bets_won':     accum['q_bets_won'],
        'q_units_staked': round(accum['q_units_staked'], 1),
        'q_units_net':    round(accum['q_units_net'], 2),
        'q_roi':          q_roi,
        'qp_bets_placed': accum['qp_bets_placed'],
        'qp_bets_won':    accum['qp_bets_won'],
        'qp_units_staked': round(accum['qp_units_staked'], 1),
        'qp_units_net':    round(accum['qp_units_net'], 2),
        'qp_roi':          qp_roi,
        'bet_hash':       bet_params_hash(cfg),
        'model_hash':     model_params_hash(cfg),
        'updated':        datetime.now().isoformat(),
        'retallied_at':   datetime.now().isoformat(),
        'per_date':       accum['per_date'],
    }

    summary_path = out_dir / 'summary.json'
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with open(summary_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    if verbose:
        print(f"\n  Re-tally 完成: {accum['dates_run']} 日期")
        print(f"    WIN:  {accum['bets_placed']} 注  {accum['bets_won']} 中  "
              f"ROI {roi:+.2%}" if roi is not None else f"    WIN:  0 注")
        print(f"    PLC:  {accum['place_bets_placed']} 注  {accum['place_bets_won']} 中  "
              f"ROI {place_roi:+.2%}" if place_roi is not None else f"    PLC:  0 注")
        print(f"    Q:    {accum['q_bets_placed']} 注  {accum['q_bets_won']} 中  "
              f"ROI {q_roi:+.2%}" if q_roi is not None else f"    Q:    0 注")
        print(f"    QP:   {accum['qp_bets_placed']} 注  {accum['qp_bets_won']} 中  "
              f"ROI {qp_roi:+.2%}" if qp_roi is not None else f"    QP:   0 注")
    return summary


def _detect_course_from_predictions(date_str: str, race_no_str: str, conn) -> str:
    """Determine course (ST/HV) for a race by checking the DB races table."""
    try:
        rn = int(race_no_str)
        row = conn.execute(
            "SELECT course FROM races WHERE date=? AND raceno=? LIMIT 1",
            (date_str, rn)
        ).fetchone()
        return row['course'] if row else 'ST'
    except Exception:
        return 'ST'


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
    parser.add_argument('--retally', action='store_true',
                        help='Re-apply current bet params to existing predictions (no re-training)')
    args = parser.parse_args()

    # ── Re-tally shortcut — no CSV load needed ──────────────────────
    if args.retally:
        cfg        = load_config(args.model)
        model_name = cfg['name']
        print(f"Re-tallying {model_name} with current bet params "
              f"(max_odds={cfg.get('bet_max_odds','∞')}, "
              f"edge_threshold={cfg.get('bet_edge_threshold',1.0)})...")
        retally(model_name, verbose=True)
        sys.exit(0)

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

    print(f"{'─'*60}")
    print(f"策略：{model_name}")
    print(f"版本：{cfg.get('version','—')}  類型：{cfg.get('strategy_type','—')}")
    print(f"說明：{cfg.get('description','')}")
    if cfg.get('notes'):
        print(f"備注：{cfg.get('notes','')}")
    print(f"輸出：{out_dir}")
    print(f"{'─'*60}")

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

    if done > 0:
        print(f"\nRe-tallying {model_name} with current bet params...")
        retally(model_name, verbose=True)

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

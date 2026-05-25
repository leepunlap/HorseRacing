"""
model_config.py — Schema definition and model-config loader.

Each named model lives in models/{name}/config.json.
This module defines the feature catalogue (static schema) and provides
load_config() to read any named model's tunable parameters.

Usage:
    from model_config import load_config, FEATURES, FEATURE_COLS, FEATURE_CATEGORIES
    cfg = load_config('均衡基礎策略')   # or load_config() for the active model
"""

import json
from pathlib import Path

BASE       = Path(__file__).parent
MODELS_DIR = BASE / 'models'

# ── Feature catalogue (static schema — shared across all model variants) ─────
# Each dict: name, category, description, tunable
FEATURES = [
    # ── Horse Profile ─────────────────────────────────────────────────────
    {'name': 'age',          'category': 'Horse Profile',
     'description': "Horse's age in years (from horse profiles CSV).",
     'tunable': False},
    {'name': 'sex_gelding',  'category': 'Horse Profile',
     'description': "1 if horse is a gelding, 0 otherwise. Geldings tend to be more consistent.",
     'tunable': False},
    {'name': 'rating',       'category': 'Horse Profile',
     'description': "Official HKJC handicap rating (0–140). Falls back to weight×0.3+(6−class)×20 if missing.",
     'tunable': False},
    {'name': 'races_count',  'category': 'Horse Profile',
     'description': "Total career starts. Proxy for experience.",
     'tunable': False},

    # ── Win Rates ─────────────────────────────────────────────────────────
    {'name': 'horse_wr',   'category': 'Win Rates',
     'description': "Horse historical win rate = wins ÷ starts (all data before cutoff).",
     'tunable': False},
    {'name': 'jockey_wr',  'category': 'Win Rates',
     'description': "Jockey historical win rate across all rides.",
     'tunable': False},
    {'name': 'trainer_wr', 'category': 'Win Rates',
     'description': "Trainer historical win rate across all runners.",
     'tunable': False},
    {'name': 'jt_pair',    'category': 'Win Rates',
     'description': "Jockey–Trainer combination win rate. Captures synergy beyond individual rates.",
     'tunable': False},
    {'name': 'jh_pair',    'category': 'Win Rates',
     'description': "Jockey–Horse combination win rate. Strong signal when jockey rides a familiar horse.",
     'tunable': False},

    # ── Adaptability ──────────────────────────────────────────────────────
    {'name': 'dist_adapt',  'category': 'Adaptability',
     'description': "Horse win rate at today's exact distance. Zero if no prior runs at this distance.",
     'tunable': False},
    {'name': 'going_adapt', 'category': 'Adaptability',
     'description': "Horse win rate on today's going. Zero if no prior runs on this ground.",
     'tunable': False},

    # ── Trainer Form ──────────────────────────────────────────────────────
    {'name': 'trainer_hot',         'category': 'Trainer Form',
     'description': "Trainer's total wins in the rolling trainer_form_days window. Raw count.",
     'tunable': False},
    {'name': 'cold_stable_season',  'category': 'Trainer Form',
     'description': "Trainer's win rate over the rolling trainer_form_days window. Low = yard out of form.",
     'tunable': True},

    # ── Draw / Barrier ────────────────────────────────────────────────────
    {'name': 'draw',       'category': 'Draw',
     'description': "Barrier/gate number (1 = innermost rail). Raw numeric value.",
     'tunable': False},
    {'name': 'draw_inner', 'category': 'Draw',
     'description': "1 if draw ≤ draw_inner_max. Inner gates favoured on most tracks.",
     'tunable': True},
    {'name': 'draw_outer', 'category': 'Draw',
     'description': "1 if draw ≥ draw_outer_min. Outer gates disadvantaged on tight bends.",
     'tunable': True},
    {'name': 'wide_draw',  'category': 'Draw',
     'description': "1 if draw ≥ draw_outer_min. Identical to draw_outer; used separately in CHRI.",
     'tunable': True},

    # ── Weight ────────────────────────────────────────────────────────────
    {'name': 'weight',        'category': 'Weight',
     'description': "Actual carried weight in lbs (jockey + saddle). Higher = bigger handicap burden.",
     'tunable': False},
    {'name': 'weight_allow',  'category': 'Weight',
     'description': "(Max weight in race − this horse's weight) ÷ weight_allow_divisor. Positive = relief.",
     'tunable': True},

    # ── Race Context ──────────────────────────────────────────────────────
    {'name': 'is_hv',        'category': 'Race Context',
     'description': "1 if Happy Valley (tight 1km oval), 0 if Sha Tin. HV amplifies draw bias.",
     'tunable': False},
    {'name': 'distance_km',  'category': 'Race Context',
     'description': "Race distance in km (e.g. 1200m → 1.2).",
     'tunable': False},
    {'name': 'going_num',    'category': 'Race Context',
     'description': "Ground encoded via going_map: Good=0 … Soft=4.",
     'tunable': True},
    {'name': 'class_num',    'category': 'Race Context',
     'description': "Race class: G1=1 through Class 5=5. Lower = higher quality.",
     'tunable': False},
    {'name': 'participants', 'category': 'Race Context',
     'description': "Number of runners. More runners → harder to win.",
     'tunable': False},

    # ── Form / Fitness ────────────────────────────────────────────────────
    {'name': 'days_since',      'category': 'Form',
     'description': "Days since last run, capped at layoff.max_days.",
     'tunable': True},
    {'name': 'layoff_penalty',  'category': 'Form',
     'description': "Additive penalty for long absence: layoff.long_penalty if >long_days, etc.",
     'tunable': True},
    {'name': 'rating_trend',    'category': 'Form',
     'description': "Avg of last rating_trend_window ratings minus avg of first N ratings. Positive = improving.",
     'tunable': True},
    {'name': 'class_drop',      'category': 'Form',
     'description': "1 if horse drops in class vs its previous race.",
     'tunable': False},

    # ── Gear ──────────────────────────────────────────────────────────────
    {'name': 'gear_change',    'category': 'Gear',
     'description': "1 if horse's gear changed from previous race. Trainer adjustment signal.",
     'tunable': False},
    {'name': 'first_gear_use', 'category': 'Gear',
     'description': "1 if horse is wearing non-standard gear (not in standard_gear) for first time.",
     'tunable': False},

    # ── Pace Analysis ─────────────────────────────────────────────────────
    {'name': 'race_pace',        'category': 'Pace',
     'description': "Predicted race pace: 0=medium, 1=slow/very_slow, 2=fast/medium_fast.",
     'tunable': False},
    {'name': 'horse_style',      'category': 'Pace',
     'description': "Horse's running style: 0=leader, 1=stalker, 2=midfield, 3=closer (from sectionals).",
     'tunable': False},
    {'name': 'pace_style_match', 'category': 'Pace',
     'description': "Fit of horse style to race pace. Leader-in-slow or closer-in-fast = pace_match.leader_slow.",
     'tunable': True},
    {'name': 'pace_draw_bonus',  'category': 'Pace',
     'description': "Additive bonus from pace_draw matrix: draw_group (inner/mid/outer) × pace bucket.",
     'tunable': True},
    {'name': 'late_pace_avg',    'category': 'Pace',
     'description': "Horse's average late-pace ratio from past races (sectionals). Higher = strong finisher.",
     'tunable': False},

    # ── Composite / Interactions ──────────────────────────────────────────
    {'name': 'cold_stable_x_wide', 'category': 'Composite',
     'description': "1 if trainer 12m win rate < cold_stable_threshold AND draw ≥ draw_outer_min.",
     'tunable': True},
    {'name': 'chri_score',         'category': 'Composite',
     'description': "CHRI = weight_allow×chri.weight_allow + wide_draw×chri.wide_draw + cold_stable_x_wide×chri.cold_stable_x_wide.",
     'tunable': True},
    {'name': 'inner_x_leader',   'category': 'Interactions',
     'description': "draw ≤ draw_inner_max AND style = leader. Front-runners benefit most from inner gates.",
     'tunable': False},
    {'name': 'outer_x_closer',   'category': 'Interactions',
     'description': "draw ≥ draw_outer_min AND style = closer.",
     'tunable': False},
    {'name': 'draw_x_hv',        'category': 'Interactions',
     'description': "draw × is_hv. Draw bias amplified at Happy Valley.",
     'tunable': False},
    {'name': 'draw_x_going',     'category': 'Interactions',
     'description': "draw × going_num. Draw disadvantage increases on softer ground.",
     'tunable': False},
    {'name': 'inner_x_pace',     'category': 'Interactions',
     'description': "inner draw AND slow race pace. Leaders from good gates in slow races.",
     'tunable': False},
    {'name': 'outer_x_fast',     'category': 'Interactions',
     'description': "outer draw AND fast race pace. Closers can overcome wide draws in fast races.",
     'tunable': False},
    {'name': 'late_x_outer',     'category': 'Interactions',
     'description': "late_pace_avg × outer draw. Proven closers from wide barriers.",
     'tunable': False},
]

FEATURE_MAP   = {f['name']: f for f in FEATURES}
FEATURE_COLS  = [f['name'] for f in FEATURES]
FEATURE_CATEGORIES = [
    'Horse Profile', 'Win Rates', 'Adaptability', 'Trainer Form',
    'Draw', 'Weight', 'Race Context', 'Form', 'Gear', 'Pace',
    'Composite', 'Interactions',
]


# ── Model config loader ───────────────────────────────────────────────────────

def list_models() -> list[dict]:
    """Return a list of all model configs found in models/ directory."""
    configs = []
    if not MODELS_DIR.exists():
        return configs
    for d in sorted(MODELS_DIR.iterdir()):
        cfg_path = d / 'config.json'
        if d.is_dir() and cfg_path.exists():
            try:
                with open(cfg_path) as f:
                    cfg = json.load(f)
                # Attach summary stats if available
                summary_path = d / 'results' / 'summary.json'
                if summary_path.exists():
                    with open(summary_path) as f:
                        cfg['_summary'] = json.load(f)
                configs.append(cfg)
            except Exception:
                pass
    return configs


def get_active_model() -> str:
    """Return the name of the model flagged active=true, or first model found."""
    for cfg in list_models():
        if cfg.get('active'):
            return cfg['name']
    configs = list_models()
    return configs[0]['name'] if configs else '均衡基礎策略'


def load_config(name: str = None) -> dict:
    """Load a named model config. Defaults to the active model."""
    if name is None:
        name = get_active_model()
    cfg_path = MODELS_DIR / name / 'config.json'
    if not cfg_path.exists():
        raise FileNotFoundError(f"Model config not found: {cfg_path}")
    with open(cfg_path) as f:
        return json.load(f)


def set_active_model(name: str):
    """Mark one model as active=true, all others active=false."""
    for cfg_entry in list_models():
        n        = cfg_entry['name']
        cfg_path = MODELS_DIR / n / 'config.json'
        with open(cfg_path) as f:
            cfg = json.load(f)
        cfg['active'] = (n == name)
        with open(cfg_path, 'w') as f:
            json.dump(cfg, f, indent=2)


def results_dir(model_name: str) -> Path:
    return MODELS_DIR / model_name / 'results'


# ── Convenience exports (from active model — used by backtest if no --model given) ──
# These mirror the old top-level constants for backwards compat.

def _active_cfg():
    try:
        return load_config()
    except Exception:
        return {}

def _get(key, default):
    return _active_cfg().get(key, default)

XGB_PARAMS            = _get('xgb', {})
NUM_BOOST_ROUNDS      = _get('num_boost_rounds', 100)
GOING_MAP             = _get('going_map', {})
PACE_DRAW             = _get('pace_draw', {})
PACE_BUCKET           = _get('pace_bucket', {})
EARLY_PACE_THRESHOLDS = [(v[0], v[1]) for v in _get('early_pace_thresholds', [])]
DRAW_INNER_MAX        = _get('draw_inner_max', 5)
DRAW_OUTER_MIN        = _get('draw_outer_min', 10)
LAYOFF                = _get('layoff', {})
WEIGHT_ALLOW_DIVISOR  = _get('weight_allow_divisor', 20)
COLD_STABLE_THRESHOLD = _get('cold_stable_threshold', 0.05)
CHRI                  = _get('chri', {})
PACE_MATCH            = _get('pace_match', {})
TRAINER_FORM_DAYS     = _get('trainer_form_days', 365)
RATING_TREND_WINDOW   = _get('rating_trend_window', 3)
STANDARD_GEAR         = set(_get('standard_gear', ['', 'B', 'TT']))
SHRINKAGE             = _get('shrinkage', {})
BET_EDGE_THRESHOLD    = _get('bet_edge_threshold', 1.0)

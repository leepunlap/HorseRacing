"""
model_config.py — Schema definition and model-config loader.

Each named model lives in models/{name}/config.json.
This module defines the feature catalogue (static schema) and provides
load_config() to read any named model's tunable parameters.

Usage:
    from model_config import load_config, FEATURES, FEATURE_COLS, FEATURE_CATEGORIES
    cfg = load_config('均衡基礎策略')   # or load_config() for the active model
"""

import hashlib
import json
from pathlib import Path

BASE       = Path(__file__).parent
MODELS_DIR = BASE / 'models'

# ── Feature catalogue (static schema — shared across all model variants) ─────
# Each dict: name, category, description, description_zh, hypotheses, tunable
# hypotheses: list of Eric H-numbers linking this feature to his hypothesis doc
FEATURES = [
    # ── Horse Profile ─────────────────────────────────────────────────────
    {'name': 'age',          'category': 'Horse Profile',
     'description': "Horse's age in years (from horse profiles CSV).",
     'description_zh': "馬齡（歲），來自馬匹資料檔案。",
     'hypotheses': ['H121', 'H70'],
     'tunable': False},
    {'name': 'sex_gelding',  'category': 'Horse Profile',
     'description': "1 if horse is a gelding, 0 otherwise. Geldings tend to be more consistent.",
     'description_zh': "1=騸馬，0=其他。騸馬性情較穩定，成績波動較小。",
     'hypotheses': ['H70', 'H121'],
     'tunable': False},
    {'name': 'rating',       'category': 'Horse Profile',
     'description': "Official HKJC handicap rating (0–140). Falls back to weight×0.3+(6−class)×20 if missing.",
     'description_zh': "馬會官方殘障評分（0–140）。缺失時用 體重×0.3+(6-班次)×20 估算。",
     'hypotheses': ['H121', 'H70'],
     'tunable': False},
    {'name': 'races_count',  'category': 'Horse Profile',
     'description': "Total career starts. Proxy for experience.",
     'description_zh': "生涯出賽總次數，代表馬匹經驗值。",
     'hypotheses': ['H121', 'H70'],
     'tunable': False},

    # ── Win Rates ─────────────────────────────────────────────────────────
    {'name': 'horse_wr',   'category': 'Win Rates',
     'description': "Horse historical win rate = wins ÷ starts (all data before cutoff).",
     'description_zh': "馬匹歷史勝率 = 勝出場次 ÷ 出賽次數（截至訓練截止日）。",
     'hypotheses': ['H1', 'H9', 'H10', 'H141'],
     'tunable': False},
    {'name': 'jockey_wr',  'category': 'Win Rates',
     'description': "Jockey historical win rate across all rides.",
     'description_zh': "騎師歷史勝率（所有出賽）。",
     'hypotheses': ['H1', 'H9', 'H119', 'H147'],
     'tunable': False},
    {'name': 'trainer_wr', 'category': 'Win Rates',
     'description': "Trainer historical win rate across all runners.",
     'description_zh': "練馬師歷史勝率（所有參賽馬匹）。",
     'hypotheses': ['H1', 'H9'],
     'tunable': False},
    {'name': 'jt_pair',    'category': 'Win Rates',
     'description': "Jockey–Trainer combination win rate. Captures synergy beyond individual rates.",
     'description_zh': "騎師×練馬師組合勝率，捕捉個別勝率以外的默契。",
     'hypotheses': ['H16', 'H17', 'H19', 'H20'],
     'tunable': False},
    {'name': 'jh_pair',    'category': 'Win Rates',
     'description': "Jockey–Horse combination win rate. Strong signal when jockey rides a familiar horse.",
     'description_zh': "騎師×馬匹組合勝率，熟悉的騎馬組合有強力訊號。",
     'hypotheses': ['H16', 'H17'],
     'tunable': False},

    # ── Adaptability ──────────────────────────────────────────────────────
    {'name': 'dist_adapt',  'category': 'Adaptability',
     'description': "Horse win rate at today's exact distance. Zero if no prior runs at this distance.",
     'description_zh': "馬匹在今日距離的勝率。無相關紀錄則為零。",
     'hypotheses': ['H9', 'H5'],
     'tunable': False},
    {'name': 'going_adapt', 'category': 'Adaptability',
     'description': "Horse win rate on today's going. Zero if no prior runs on this ground.",
     'description_zh': "馬匹在今日場地狀況的勝率。無相關紀錄則為零。",
     'hypotheses': ['H5', 'H6', 'H9'],
     'tunable': False},

    # ── Trainer Form ──────────────────────────────────────────────────────
    {'name': 'trainer_hot',         'category': 'Trainer Form',
     'description': "Trainer's total wins in the rolling trainer_form_days window. Raw count.",
     'description_zh': "練馬師在滾動窗口（trainer_form_days）內的總勝場數。",
     'hypotheses': ['H54', 'H55', 'H56', 'H152'],
     'tunable': False},
    {'name': 'cold_stable_season',  'category': 'Trainer Form',
     'description': "Trainer's win rate over the rolling trainer_form_days window. Low = yard out of form.",
     'description_zh': "練馬師在滾動窗口內的勝率。偏低代表馬廄目前狀態差。",
     'hypotheses': ['H54', 'H55', 'H82', 'H152'],
     'tunable': True},

    # ── Draw / Barrier ────────────────────────────────────────────────────
    {'name': 'draw',       'category': 'Draw',
     'description': "Barrier/gate number (1 = innermost rail). Raw numeric value.",
     'description_zh': "閘號（1=最內閘）。原始數值。",
     'hypotheses': ['H2', 'H39', 'H120'],
     'tunable': False},
    {'name': 'draw_inner', 'category': 'Draw',
     'description': "1 if draw ≤ draw_inner_max. Inner gates favoured on most tracks.",
     'description_zh': "1 若閘號 ≤ draw_inner_max。大多數賽道內閘佔優。",
     'hypotheses': ['H2', 'H89', 'H155'],
     'tunable': True},
    {'name': 'draw_outer', 'category': 'Draw',
     'description': "1 if draw ≥ draw_outer_min. Outer gates disadvantaged on tight bends.",
     'description_zh': "1 若閘號 ≥ draw_outer_min。彎道緊的賽道外閘不利。",
     'hypotheses': ['H39', 'H43', 'H89'],
     'tunable': True},
    {'name': 'wide_draw',  'category': 'Draw',
     'description': "1 if draw ≥ draw_outer_min. Identical to draw_outer; used separately in CHRI.",
     'description_zh': "1 若閘號 ≥ draw_outer_min。與 draw_outer 相同，在 CHRI 中獨立使用。",
     'hypotheses': ['H79', 'H82'],
     'tunable': True},

    # ── Weight ────────────────────────────────────────────────────────────
    {'name': 'weight',        'category': 'Weight',
     'description': "Actual carried weight in lbs (jockey + saddle). Higher = bigger handicap burden.",
     'description_zh': "實際負磅（騎師＋鞍具）。越重代表殘障負擔越大。",
     'hypotheses': ['H70', 'H120'],
     'tunable': False},
    {'name': 'weight_allow',  'category': 'Weight',
     'description': "(Max weight in race − this horse's weight) ÷ weight_allow_divisor. Positive = relief.",
     'description_zh': "（場內最重磅－本馬負磅）÷ weight_allow_divisor。正值代表減磅優惠。",
     'hypotheses': ['H79', 'H80', 'H85'],
     'tunable': True},

    # ── Race Context ──────────────────────────────────────────────────────
    {'name': 'is_hv',        'category': 'Race Context',
     'description': "1 if Happy Valley (tight 1km oval), 0 if Sha Tin. HV amplifies draw bias.",
     'description_zh': "1=跑馬地（緊湊1公里橢圓），0=沙田。跑馬地放大閘號偏差。",
     'hypotheses': ['H90', 'H155'],
     'tunable': False},
    {'name': 'distance_km',  'category': 'Race Context',
     'description': "Race distance in km (e.g. 1200m → 1.2).",
     'description_zh': "賽事距離（公里），例如1200米→1.2。",
     'hypotheses': ['H91', 'H120'],
     'tunable': False},
    {'name': 'going_num',    'category': 'Race Context',
     'description': "Ground encoded via going_map: Good=0 … Soft=4.",
     'description_zh': "場地狀況數值（going_map 編碼：好地=0…鬆軟=4）。",
     'hypotheses': ['H5', 'H28', 'H29'],
     'tunable': True},
    {'name': 'class_num',    'category': 'Race Context',
     'description': "Race class: G1=1 through Class 5=5. Lower = higher quality.",
     'description_zh': "班次編號：G1=1 至 第5班=5。數字越小賽事水準越高。",
     'hypotheses': ['H9', 'H120', 'H149'],
     'tunable': False},
    {'name': 'participants', 'category': 'Race Context',
     'description': "Number of runners. More runners → harder to win.",
     'description_zh': "出賽馬匹數。馬匹越多勝出越難。",
     'hypotheses': ['H70', 'H120'],
     'tunable': False},

    # ── Form / Fitness ────────────────────────────────────────────────────
    {'name': 'days_since',      'category': 'Form',
     'description': "Days since last run, capped at layoff.max_days.",
     'description_zh': "上次出賽至今天數，上限為 layoff.max_days。",
     'hypotheses': ['H12', 'H13'],
     'tunable': True},
    {'name': 'layoff_penalty',  'category': 'Form',
     'description': "Additive penalty for long absence: layoff.long_penalty if >long_days, etc.",
     'description_zh': "長期休賽懲罰值：超過 long_days 施加 long_penalty，以此類推。",
     'hypotheses': ['H12', 'H35', 'H48'],
     'tunable': True},
    {'name': 'rating_trend',    'category': 'Form',
     'description': "Avg of last rating_trend_window ratings minus avg of first N ratings. Positive = improving.",
     'description_zh': "近期評分均值減初期評分均值（窗口=rating_trend_window）。正值代表狀態上升。",
     'hypotheses': ['H12', 'H14'],
     'tunable': True},
    {'name': 'class_drop',      'category': 'Form',
     'description': "1 if horse drops in class vs its previous race.",
     'description_zh': "1 若本場班次低於上次出賽班次（降班參賽）。",
     'hypotheses': ['H46', 'H70'],
     'tunable': False},

    # ── Gear ──────────────────────────────────────────────────────────────
    {'name': 'gear_change',    'category': 'Gear',
     'description': "1 if horse's gear changed from previous race. Trainer adjustment signal.",
     'description_zh': "1 若裝備相比上次出賽有變動。練馬師調整訊號。",
     'hypotheses': ['H32', 'H34', 'H122'],
     'tunable': False},
    {'name': 'first_gear_use', 'category': 'Gear',
     'description': "1 if horse is wearing non-standard gear (not in standard_gear) for first time.",
     'description_zh': "1 若首次使用非標準裝備（不在 standard_gear 名單內）。",
     'hypotheses': ['H32', 'H33', 'H122', 'H153'],
     'tunable': False},

    # ── Pace Analysis ─────────────────────────────────────────────────────
    {'name': 'race_pace',        'category': 'Pace',
     'description': "Predicted race pace: 0=medium, 1=slow/very_slow, 2=fast/medium_fast.",
     'description_zh': "預測賽事步速：0=中等，1=慢／非常慢，2=快／中快。",
     'hypotheses': ['H24', 'H65', 'H87'],
     'tunable': False},
    {'name': 'horse_style',      'category': 'Pace',
     'description': "Horse's running style: 0=leader, 1=stalker, 2=midfield, 3=closer (from sectionals).",
     'description_zh': "馬匹跑法：0=領跑，1=跟跑，2=中段，3=追後（由分段時間推算）。",
     'hypotheses': ['H66', 'H88'],
     'tunable': False},
    {'name': 'pace_style_match', 'category': 'Pace',
     'description': "Fit of horse style to race pace. Leader-in-slow or closer-in-fast = pace_match.leader_slow.",
     'description_zh': "跑法與步速的配合度加成（如慢賽領跑、快賽追後各有額外分值）。",
     'hypotheses': ['H66', 'H26', 'H123'],
     'tunable': True},
    {'name': 'pace_draw_bonus',  'category': 'Pace',
     'description': "Additive bonus from pace_draw matrix: draw_group (inner/mid/outer) × pace bucket.",
     'description_zh': "閘位分組（內／中／外）× 步速類別的矩陣加成值。",
     'hypotheses': ['H89', 'H90', 'H91'],
     'tunable': True},
    {'name': 'late_pace_avg',    'category': 'Pace',
     'description': "Horse's average late-pace ratio from past races (sectionals). Higher = strong finisher.",
     'description_zh': "馬匹歷史後段步速比率均值（分段時間）。越高代表末段越強。",
     'hypotheses': ['H25', 'H67', 'H42'],
     'tunable': False},

    # ── Composite / Interactions ──────────────────────────────────────────
    {'name': 'cold_stable_x_wide', 'category': 'Composite',
     'description': "1 if trainer 12m win rate < cold_stable_threshold AND draw ≥ draw_outer_min.",
     'description_zh': "1 若練馬師12個月勝率 < cold_stable_threshold 且閘號 ≥ draw_outer_min。",
     'hypotheses': ['H79', 'H82', 'H85'],
     'tunable': True},
    {'name': 'chri_score',         'category': 'Composite',
     'description': "CHRI = weight_allow×chri.weight_allow + wide_draw×chri.wide_draw + cold_stable_x_wide×chri.cold_stable_x_wide.",
     'description_zh': "綜合風險指數 = weight_allow×係數 + wide_draw×係數 + cold_stable_x_wide×係數。",
     'hypotheses': ['H79', 'H83', 'H84', 'H124'],
     'tunable': True},
    {'name': 'inner_x_leader',   'category': 'Interactions',
     'description': "draw ≤ draw_inner_max AND style = leader. Front-runners benefit most from inner gates.",
     'description_zh': "內閘（≤ draw_inner_max）且跑法為領跑。領跑馬從內閘獲益最大。",
     'hypotheses': ['H89', 'H26', 'H66'],
     'tunable': False},
    {'name': 'outer_x_closer',   'category': 'Interactions',
     'description': "draw ≥ draw_outer_min AND style = closer.",
     'description_zh': "外閘（≥ draw_outer_min）且跑法為追後。",
     'hypotheses': ['H40', 'H41', 'H154'],
     'tunable': False},
    {'name': 'draw_x_hv',        'category': 'Interactions',
     'description': "draw × is_hv. Draw bias amplified at Happy Valley.",
     'description_zh': "閘號 × 跑馬地標記。跑馬地閘號偏差更顯著。",
     'hypotheses': ['H90', 'H155'],
     'tunable': False},
    {'name': 'draw_x_going',     'category': 'Interactions',
     'description': "draw × going_num. Draw disadvantage increases on softer ground.",
     'description_zh': "閘號 × 場地數值。軟地加重外閘劣勢。",
     'hypotheses': ['H30', 'H40', 'H154'],
     'tunable': False},
    {'name': 'inner_x_pace',     'category': 'Interactions',
     'description': "inner draw AND slow race pace. Leaders from good gates in slow races.",
     'description_zh': "內閘且步速慢。慢賽從內閘領跑最為有利。",
     'hypotheses': ['H89', 'H26'],
     'tunable': False},
    {'name': 'outer_x_fast',     'category': 'Interactions',
     'description': "outer draw AND fast race pace. Closers can overcome wide draws in fast races.",
     'description_zh': "外閘且步速快。快賽中追後馬可克服外閘劣勢。",
     'hypotheses': ['H89', 'H66'],
     'tunable': False},
    {'name': 'late_x_outer',     'category': 'Interactions',
     'description': "late_pace_avg × outer draw. Proven closers from wide barriers.",
     'description_zh': "後段步速均值 × 外閘。已證實末段強的外閘追後馬。",
     'hypotheses': ['H25', 'H40', 'H42'],
     'tunable': False},
]

FEATURE_MAP   = {f['name']: f for f in FEATURES}
FEATURE_COLS  = [f['name'] for f in FEATURES]
FEATURE_CATEGORIES = [
    'Horse Profile', 'Win Rates', 'Adaptability', 'Trainer Form',
    'Draw', 'Weight', 'Race Context', 'Form', 'Gear', 'Pace',
    'Composite', 'Interactions',
]
FEATURE_CATEGORY_ZH = {
    'Horse Profile': '馬匹資料',
    'Win Rates':     '勝率',
    'Adaptability':  '適應性',
    'Trainer Form':  '練馬師狀態',
    'Draw':          '閘號',
    'Weight':        '負磅',
    'Race Context':  '賽事背景',
    'Form':          '近期狀態',
    'Gear':          '裝備',
    'Pace':          '步速',
    'Composite':     '綜合指標',
    'Interactions':  '交互特徵',
}

FEATURE_NAME_ZH = {
    'age':               '馬齡',
    'sex_gelding':       '騸馬標記',
    'rating':            '殘障評分',
    'races_count':       '出賽次數',
    'horse_wr':          '馬匹勝率',
    'jockey_wr':         '騎師勝率',
    'trainer_wr':        '練馬師勝率',
    'jt_pair':           '騎師練馬師配對',
    'jh_pair':           '騎師馬匹配對',
    'dist_adapt':        '距離適應率',
    'going_adapt':       '場地適應率',
    'trainer_hot':       '馬廄熱度',
    'cold_stable_season':'馬廄冷浪',
    'draw':              '閘號',
    'draw_inner':        '內閘',
    'draw_outer':        '外閘',
    'wide_draw':         '大外閘',
    'weight':            '負磅',
    'weight_allow':      '減磅優惠',
    'is_hv':             '跑馬地',
    'distance_km':       '賽事距離',
    'going_num':         '場地編碼',
    'class_num':         '班次',
    'participants':      '出賽馬數',
    'days_since':        '休賽天數',
    'layoff_penalty':    '久休懲罰',
    'rating_trend':      '評分趨勢',
    'class_drop':        '降班',
    'gear_change':       '裝備變動',
    'first_gear_use':    '首次裝備',
    'race_pace':         '賽事步速',
    'horse_style':       '跑法風格',
    'pace_style_match':  '步速配合',
    'pace_draw_bonus':   '步速閘位加成',
    'late_pace_avg':     '後段步速',
    'cold_stable_x_wide':'冷廄外閘交互',
    'chri_score':        'CHRI 指數',
    'inner_x_leader':    '內閘領跑',
    'outer_x_closer':    '外閘追後',
    'draw_x_hv':         '閘號跑馬地',
    'draw_x_going':      '閘號場地',
    'inner_x_pace':      '內閘慢步',
    'outer_x_fast':      '外閘快步',
    'late_x_outer':      '後段外閘',
}


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


# ── Config-staleness helpers ──────────────────────────────────────────────────
# Parameters that change only which bets are placed — re-tally suffices.
_BET_KEYS  = frozenset({'bet_edge_threshold', 'bet_min_odds', 'bet_max_odds'})
# Fields that carry no model semantics (identity / deployment metadata).
_META_KEYS = frozenset({'name', 'description', 'strategy_type', 'version',
                        'parent', 'notes', 'created', 'active'})


def _hash8(obj) -> str:
    s = json.dumps(obj, sort_keys=True, default=str)
    return hashlib.md5(s.encode()).hexdigest()[:8]


def bet_params_hash(cfg: dict) -> str:
    """Short hash of the three bet-filtering params."""
    return _hash8({k: cfg.get(k) for k in sorted(_BET_KEYS)})


def model_params_hash(cfg: dict) -> str:
    """Short hash of all model/feature params — changes require full re-run."""
    return _hash8({k: v for k, v in sorted(cfg.items())
                   if k not in _BET_KEYS | _META_KEYS})


def staleness(model_name: str) -> dict:
    """Check if a model's backtest results are stale vs its current config.

    Returns a dict with keys: stale (bool), reason (str),
    needs_retally (bool), needs_rerun (bool).
    """
    try:
        cfg = load_config(model_name)
    except FileNotFoundError:
        return {'stale': True, 'reason': '找不到設定檔',
                'needs_retally': False, 'needs_rerun': True}

    summary_path = MODELS_DIR / model_name / 'results' / 'summary.json'
    if not summary_path.exists():
        return {'stale': True, 'reason': '尚無回測摘要',
                'needs_retally': False, 'needs_rerun': True}

    try:
        summary = json.loads(summary_path.read_text(encoding='utf-8'))
    except Exception:
        return {'stale': True, 'reason': '摘要檔案損毀',
                'needs_retally': False, 'needs_rerun': True}

    cur_model = model_params_hash(cfg)
    cur_bet   = bet_params_hash(cfg)
    sum_model = summary.get('model_hash', '')
    sum_bet   = summary.get('bet_hash', '')

    if sum_model and cur_model != sum_model:
        return {'stale': True, 'reason': '模型參數已變更，需重新回測',
                'needs_retally': False, 'needs_rerun': True}
    if cur_bet != sum_bet:
        return {'stale': True, 'reason': '下注參數已變更，需重新計算',
                'needs_retally': True, 'needs_rerun': False}
    return {'stale': False, 'reason': '', 'needs_retally': False, 'needs_rerun': False}

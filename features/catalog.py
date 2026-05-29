"""Registry of the 174 the features, H001..H174.

Each entry is a `Feature(id, category, name_zh, name_en, definition,
source_refs, depends_on, compute_fn_name, enabled_default, nan_permitted)`.

The full list mirrors userdocs/features_expanded_zh_hant.md. Compute functions
live in `features.compute`. A feature whose `compute_fn_name` resolves to
`_nan_stub` returns NaN until its data source is wired up — this is
intentional: the catalog stays the source of truth even before every scraper
is populating its table.

Scope is HKJC-only: features that require non-HKJC data sources
(Beyer/Timeform/RPR/Brisnet/Topspeed/Equibase/Ragozin foreign figures, Betfair
BSP and exchange depth, Pedigree-Online Dosage Index, GPS biometrics) are
present in the catalog for completeness but ship with `enabled_default=False`
and `compute_fn_name='_nan_stub'`. As a result the catalog has 15 active
categories (Cat 16 biometric was dropped) and ~163 features will compute on
HK data; the remaining ~11 stay disabled.

Seeding:
    python3 -m features.catalog --seed   # populate feature_catalog table
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "racing.db"


@dataclass(frozen=True)
class Feature:
    id: str            # H001..H174
    category: int      # 1..16
    name_zh: str
    name_en: str
    definition: str
    source_refs: str   # comma-sep bibliography keys (B1,B2,...)
    compute_fn_name: str  # function name in features.compute
    depends_on: str = ""
    enabled_default: bool = True
    nan_permitted: bool = True


# ─────────────────────────────────────────────────────────────────────────────
# Catalog. Numbering is consecutive H001..H174 grouped by category.
# Many entries currently target `_nan_stub` — they're in the catalog so models
# and the SPA can reason about them, but compute is wired up incrementally.
# ─────────────────────────────────────────────────────────────────────────────

FEATURES: list[Feature] = [
    # ─── Category 1: Horse profile (H001-H014) ────────────────────────────
    Feature("H001", 1, "馬齡", "Age", "Years old; 3-5yo dominant in HK", "B1", "h001_age"),
    Feature("H002", 1, "騸馬標記", "Gelding flag", "1 if gelding", "B33", "h002_gelding"),
    Feature("H003", 1, "性別", "Sex", "colt/filly/gelding/mare encoded 0-3", "B7,B33", "h003_sex"),
    Feature("H004", 1, "馬色", "Colour", "Coat colour encoded", "B5", "h004_colour"),
    Feature("H005", 1, "出生月份", "Birth month", "Cohort proxy; northern 1-2 favoured", "B1,B23", "_nan_stub", enabled_default=False),
    Feature("H006", 1, "南北半球", "Hemisphere", "Birth hemisphere N/S", "B27,B33", "_nan_stub", enabled_default=False),
    Feature("H007", 1, "宣告體重", "Declaration weight (lb)", "HKJC body weight at declaration", "B34", "h007_decl_weight"),
    Feature("H008", 1, "體重變動", "Body weight delta", "Δ vs last declaration", "B34", "h008_weight_delta"),
    Feature("H009", 1, "殘障評分", "Rating", "HKJC handicap rating", "B14", "h009_rating"),
    Feature("H010", 1, "出賽次數", "Career starts", "Total starts", "B1", "h010_starts"),
    Feature("H011", 1, "父系", "Sire", "Sire identity hash", "B5,B26", "_nan_stub", enabled_default=False),
    Feature("H012", 1, "母父", "Dam sire", "Dam-sire identity hash", "B5,B26", "_nan_stub", enabled_default=False),
    Feature("H013", 1, "Dosage Index", "Dosage Index (DI)", "Speed-stamina ratio (HKJC-out-of-scope: needs BloodHorse/Pedigree Online)", "B26", "_nan_stub", enabled_default=False),
    Feature("H014", 1, "引進來源", "Origin country", "AUS/NZL/GBR/IRE/USA encoded", "B14,B19", "h014_origin"),

    # ─── Category 2: Win/return metrics (H015-H028) ───────────────────────
    Feature("H015", 2, "馬匹勝率", "Horse win rate", "Bayesian-shrunk career WR", "B1,B25", "h015_horse_wr"),
    Feature("H016", 2, "騎師勝率", "Jockey win rate", "Bayesian-shrunk", "B14,B25", "h016_jockey_wr"),
    Feature("H017", 2, "練馬師勝率", "Trainer win rate", "Bayesian-shrunk", "B14,B25", "h017_trainer_wr"),
    Feature("H018", 2, "騎師×練馬師", "Jockey-trainer pair WR", "Pair WR with prior", "B14", "h018_jt_pair"),
    Feature("H019", 2, "騎師×馬匹", "Jockey-horse pair WR", "Same-pair WR", "B14", "h019_jh_pair"),
    Feature("H020", 2, "入位率", "Place rate", "Top-2 finish rate", "B2,B16", "h020_place_rate"),
    Feature("H021", 2, "三甲率", "Top-3 rate", "Top-3 finish rate", "B2,B16", "h021_top3_rate"),
    Feature("H022", 2, "ROI", "ROI per $2", "Avg payoff per unit", "B2", "h022_roi"),
    Feature("H023", 2, "A/E 指標", "Actual/Expected", ">1.0 beats market", "B22,B30", "h023_ae"),
    Feature("H024", 2, "Impact Value", "IV", "Group WR / overall WR", "B22", "h024_iv"),
    Feature("H025", 2, "騎師跑馬地", "Jockey at HV", "Jockey WR at Happy Valley", "B14,B36", "h025_jockey_hv"),
    Feature("H026", 2, "騎師沙田", "Jockey at ST", "Jockey WR at Sha Tin", "B14,B36", "h026_jockey_st"),
    Feature("H027", 2, "練馬師跑馬地", "Trainer at HV", "Trainer WR at HV", "B14,B36", "h027_trainer_hv"),
    Feature("H028", 2, "三巨頭標記", "Big-3 jockey flag", "Purton/Bowman/Moreira/Ferraris on board", "B14,B40", "h028_big3_jockey"),

    # ─── Category 3: Adaptability (H029-H038) ─────────────────────────────
    Feature("H029", 3, "距離適性", "Distance adaptation", "Horse WR at this distance bucket", "B1,B23", "h029_dist_adapt"),
    Feature("H030", 3, "場地適性", "Surface adaptation", "Horse WR on turf vs AWT", "B14,B36", "h030_going_adapt"),
    Feature("H031", 3, "父系距離適性", "Sire×distance", "Sire ROI at this distance", "B2,B26", "_nan_stub", enabled_default=False),
    Feature("H032", 3, "父系場地適性", "Sire×surface", "Sire turf-vs-dirt edge", "B2,B5", "_nan_stub", enabled_default=False),
    Feature("H033", 3, "馬齡×距離", "Age×distance", "Age-bucket WR at distance", "B14,B23", "h033_age_dist"),
    Feature("H034", 3, "左右轉", "Track turn direction", "L vs R track preference", "B14,B36", "_nan_stub", enabled_default=False),
    Feature("H035", 3, "季節適應", "Season WR", "Horse WR in current month", "B14,B36", "h035_season"),
    Feature("H036", 3, "賽事級別適應", "Class WR", "Horse WR at this class", "B14,B19", "h036_class_wr"),
    Feature("H037", 3, "出閘速度", "Jump speed", "First-200m position vs field", "B11,B12", "h037_jump_speed"),
    Feature("H038", 3, "賽事間復原時間", "Recovery time", "Days since × prior burden", "B17,B25", "h038_recovery"),

    # ─── Category 4: Trainer form (H039-H049) ─────────────────────────────
    Feature("H039", 4, "馬廄熱度", "Trainer hot", "Recent rolling WR", "B14", "h039_trainer_hot"),
    Feature("H040", 4, "馬廄冷浪", "Trainer cold", "Recent under-performance", "B14", "h040_trainer_cold"),
    Feature("H041", 4, "練馬師班次強項", "Trainer×class ROI", "Trainer ROI at class", "B2,B6", "h041_trainer_class"),
    Feature("H042", 4, "練馬師距離強項", "Trainer×distance ROI", "Trainer ROI at distance", "B2,B6", "h042_trainer_dist"),
    Feature("H043", 4, "季節熱度", "Trainer season cycle", "Early/mid/late season WR", "B2,B14", "h043_trainer_season"),
    Feature("H044", 4, "首戰勝率", "Trainer first-timer WR", "Trainer with debutants", "B2,B16", "h044_trainer_debut"),
    Feature("H045", 4, "復出馬勝率", "Trainer with returners", "Layoff 45-180d WR", "B17,B25", "h045_trainer_returner"),
    Feature("H046", 4, "場地適應", "Trainer×venue", "Trainer ST vs HV WR", "B14,B36", "h046_trainer_venue"),
    Feature("H047", 4, "出賽密度", "Trainer runs / 30d", "Stable activity proxy", "B15,B25", "h047_trainer_density"),
    Feature("H048", 4, "馬房規模", "Stable size", "Active horses under trainer", "B14", "h048_stable_size"),
    Feature("H049", 4, "練馬師裝備改動", "Trainer gear-change ROI", "Trainer angle on gear changes", "B2,B11", "_nan_stub", enabled_default=False),
    # ─── Category 5: Draw (H050-H060) ─────────────────────────────────────
    Feature("H050", 5, "閘號", "Draw", "Barrier 1..N", "B14,B36", "h050_draw"),
    Feature("H051", 5, "內閘", "Inner draw flag", "1 if draw<=3", "B36", "h051_draw_inner"),
    Feature("H052", 5, "外閘", "Outer draw flag", "1 if draw in last third", "B36", "h052_draw_outer"),
    Feature("H053", 5, "大外閘", "Wide draw flag", "1 if draw in last 2", "B36", "h053_draw_wide"),
    Feature("H054", 5, "賽道×距離閘號偏差", "Track×distance draw bias", "Lookup table on draw bias", "B36,B41", "h054_track_dist_bias"),
    Feature("H055", 5, "距首彎距離", "Distance to first turn", "Run-up length", "B36,B41", "h055_to_first_turn"),
    Feature("H056", 5, "起閘速度", "Gate break speed", "First-200m sectional vs field", "B11,B12", "h056_gate_break_speed"),
    Feature("H057", 5, "賽道彎度", "Track curvature", "ST vs HV circumference", "B36", "h057_curvature"),
    Feature("H058", 5, "草地內外欄", "Rail position", "A/B/C/C+3 encoded", "B36,B41", "h058_rail"),
    Feature("H059", 5, "AWT 閘號偏差", "AWT draw bias", "All-weather draw effect", "B14,B36", "h059_awt_draw"),
    Feature("H060", 5, "閘號×跑法", "Draw×style booster", "Inner-leader / outer-closer combo", "B11,B12", "h060_draw_style"),

    # ─── Category 6: Weight (H061-H070) ──────────────────────────────────
    Feature("H061", 6, "負磅", "Weight carried (lb)", "Total carried weight", "B7", "h061_weight"),
    Feature("H062", 6, "減磅優惠", "Apprentice claim", "lb reduction", "B7,B33", "h062_claim"),
    Feature("H063", 6, "負磅趨勢", "Weight delta vs last start", "Δ weight carried", "B14,B30", "h063_weight_trend"),
    Feature("H064", 6, "宣告體重", "Body weight (decl)", "Same as H007", "B34", "h064_decl_weight_dup"),
    Feature("H065", 6, "體重變動", "Body weight Δ", "Same as H008", "B34", "h065_weight_delta_dup"),
    Feature("H066", 6, "騎師體重", "Jockey weight", "Jockey body weight", "B7,B33", "_nan_stub", enabled_default=False),
    Feature("H067", 6, "鞍具重量", "Saddle weight", "Equipment weight", "B33", "_nan_stub", enabled_default=False),
    Feature("H068", 6, "見習等級", "Apprentice claim grade", "3/5/7 lb level", "B7,B33", "h068_apprentice_grade"),
    Feature("H069", 6, "性別讓磅", "Sex allowance", "3 lb for fillies/mares", "B7,B33", "h069_sex_allowance"),
    Feature("H070", 6, "馬齡 WFA", "Weight-for-age", "3yo vs 4yo allowance", "B7,B14", "h070_wfa"),

    # ─── Category 7: Race context (H071-H083) ─────────────────────────────
    Feature("H071", 7, "跑馬地標記", "Happy Valley flag", "1 if HV", "B14,B36", "h071_is_hv"),
    Feature("H072", 7, "賽事距離", "Race distance (m)", "Distance in meters", "B1", "h072_distance"),
    Feature("H073", 7, "場地編碼", "Going encoded", "Good=0..Soft=4", "B14,B28", "h073_going"),
    Feature("H074", 7, "班次", "Class encoded", "G1=1..C5=5", "B14,B19", "h074_class"),
    Feature("H075", 7, "出賽馬數", "Field size", "Number of runners", "B1", "h075_field_size"),
    Feature("H076", 7, "獎金", "Prize money", "HK$ first prize", "B14,B18", "h076_prize"),
    Feature("H077", 7, "Group/Listed", "Group/Listed flag", "1 if G/L stake", "B18,B19", "h077_group_listed"),
    Feature("H078", 7, "國際賽", "International race flag", "HKIR/Champions Day", "B14,B19", "h078_intl_race"),
    Feature("H079", 7, "賽季階段", "Season phase", "Early/mid/late", "B14,B36", "h079_season_phase"),
    Feature("H080", 7, "日夜", "Day vs night", "HV night, ST day", "B14,B36", "h080_day_night"),
    Feature("H081", 7, "氣溫", "Temperature (°C)", "From weather table", "B28,B29", "h081_temperature"),
    Feature("H082", 7, "降雨", "Rainfall (mm)", "Race-day rainfall", "B28,B29", "h082_rainfall"),
    Feature("H083", 7, "賽事編號", "Race number", "Card position", "B14", "h083_race_no"),

    # ─── Category 8: Recent form (H084-H096) ──────────────────────────────
    Feature("H084", 8, "休賽天數", "Days since last", "Layoff length", "B17,B25", "h084_days_since"),
    Feature("H085", 8, "久休懲罰", "Long-layoff penalty", "Penalty if >60d", "B17", "h085_layoff_penalty"),
    Feature("H086", 8, "評分趨勢", "Rating trend", "Recent rating slope", "B1,B6", "h086_rating_trend"),
    Feature("H087", 8, "降班", "Class drop flag", "1 if dropped class", "B6", "h087_class_drop"),
    Feature("H088", 8, "連勝紀錄", "Win streak", "Consecutive wins", "B25", "h088_win_streak"),
    Feature("H089", 8, "連敗紀錄", "Loss streak", "Consecutive losses", "B25", "h089_loss_streak"),
    Feature("H090", 8, "上場名次", "Last finish position", "Strong signal", "B6,B22", "h090_last_pos"),
    Feature("H091", 8, "上場敗距", "Last lengths behind", "Benter variable", "B1,B6", "h091_last_lbw"),
    Feature("H092", 8, "上場分段殘差", "Last sectional residual", "vs par", "B10,B11", "h092_last_sec_residual"),
    Feature("H093", 8, "終段速度%", "Finishing Speed %", "Rowlands FSP", "B10", "h093_finishing_speed_pct"),
    Feature("H094", 8, "試閘紀錄", "Recent barrier trial", "Last 60d trial position", "B20", "h094_barrier_trial"),
    Feature("H095", 8, "晨操工夫", "Recent trackwork", "Last 14d gallop distance", "B20,B21", "h095_trackwork"),
    Feature("H096", 8, "連續同騎師", "Same-jockey streak", "Consecutive same-jockey starts", "B14,B25", "h096_same_jockey_streak"),

    # ─── Category 9: Gear & vet (H097-H107) ───────────────────────────────
    Feature("H097", 9, "裝備變動", "Gear change flag", "Any equipment change", "B11,B14", "h097_gear_change"),
    Feature("H098", 9, "首次裝備", "First-time gear", "Untested gear", "B11,B14", "h098_first_gear"),
    Feature("H099", 9, "眼罩變動", "Blinkers on/off", "First-time blinkers", "B11,B14", "h099_blinkers"),
    Feature("H100", 9, "首次馬銜", "First-time bit/tongue tie", "Bit change marker", "B11,B14", "_nan_stub", enabled_default=False),
    Feature("H101", 9, "蹄鐵變動", "Shoeing change", "Bar shoe / glue-on", "B11", "_nan_stub", enabled_default=False),
    Feature("H102", 9, "馬鞍變動", "Saddle change", "Synthetic vs leather", "B33", "_nan_stub", enabled_default=False),
    Feature("H103", 9, "多重裝備變動", "Multiple gear changes", "N items changed", "B11,B14", "h103_multi_gear"),
    Feature("H104", 9, "用藥變動", "Medication change", "HK: limited; US Lasix", "B11,B24", "_nan_stub", enabled_default=False),
    Feature("H105", 9, "獸醫紀錄", "Vet record flag", "Recent OVE entry", "B38,B39", "h105_vet"),
    Feature("H106", 9, "Roarer", "Roarer surgery flag", "Has had wind op", "B38", "h106_roarer"),
    Feature("H107", 9, "獸醫復出", "Off-vet returner", "First start post-vet", "B38,B39", "_nan_stub", enabled_default=False),
    # ─── Category 10: Pace & style (H108-H121) ────────────────────────────
    Feature("H108", 10, "賽事步速", "Race pace forecast", "0 slow 1 med 2 fast", "B11,B12", "h108_race_pace"),
    Feature("H109", 10, "跑法風格", "Running style", "0 leader 1 stalker 2 mid 3 closer", "B11,B12", "h109_style"),
    Feature("H110", 10, "步速配合", "Pace-style match", "Bonus if style fits pace", "B11,B12", "h110_pace_match"),
    Feature("H111", 10, "閘位加成", "Draw×pace booster", "Matrix bonus", "B11,B12", "h111_draw_pace_bonus"),
    Feature("H112", 10, "領跑指數", "Lead profile", "Frequency of leading at 1st call", "B11,B12", "h112_lead_profile"),
    Feature("H113", 10, "E1 早段", "E1 early speed", "Brisnet-style figure", "B11,B16", "h113_e1_early"),
    Feature("H114", 10, "E2 早段", "E2 early speed", "Brisnet-style figure", "B11,B16", "h114_e2_early"),
    Feature("H115", 10, "後段速度", "Late Pace LP", "Rowlands-style late split", "B11,B16", "h115_late_pace"),
    Feature("H116", 10, "前段速度", "Early Pace EP", "Avg early ratio", "B11,B16", "h116_early_pace"),
    Feature("H117", 10, "配速壓力", "Pace pressure", "# early-speed runners", "B12,B16", "h117_pace_pressure"),
    Feature("H118", 10, "配速生存者", "Pace survivor", "Closer that withstands hot pace", "B12,B16", "h118_pace_survivor"),
    Feature("H119", 10, "配速受益者", "Pace beneficiary", "Closer benefits when pace fast", "B12,B16", "h119_pace_benefit"),
    Feature("H120", 10, "跑法純度", "Style purity", "% of runs in dominant style", "B11,B12", "h120_style_purity"),
    Feature("H121", 10, "超越位次均值", "Avg overtake distance", "Mean positions gained", "B10,B11", "h121_overtake"),

    # ─── Category 11: Composite speed/class (H122-H132) ───────────────────
    Feature("H122", 11, "CHRI 指數", "CHRI composite", "Composite risk index", "B14", "h122_chri"),
    # H123-H131: foreign speed-figure brands (Beyer/Timeform/RPR/Brisnet/Topspeed/Equibase/Ragozin)
    # are out of HKJC-only scope — no public data source for HK racing. Kept as catalog
    # placeholders so the SPA can still reason about them, but enabled_default=False.
    Feature("H123", 11, "Beyer", "Beyer figure", "US figure (HKJC-out-of-scope)", "B4", "_nan_stub", enabled_default=False),
    Feature("H124", 11, "Timeform", "Timeform Master Rating", "UK/EU figure (HKJC-out-of-scope)", "B3", "_nan_stub", enabled_default=False),
    Feature("H125", 11, "RPR", "Racing Post Rating", "UK figure (HKJC-out-of-scope)", "B3", "_nan_stub", enabled_default=False),
    Feature("H126", 11, "Brisnet Prime", "Brisnet Prime Power", "US composite (HKJC-out-of-scope)", "B2,B16", "_nan_stub", enabled_default=False),
    Feature("H127", 11, "Class Rating", "Brisnet Class Rating", "US class figure (HKJC-out-of-scope)", "B2,B16", "_nan_stub", enabled_default=False),
    Feature("H128", 11, "Topspeed", "Topspeed", "Racing Post raw time (HKJC-out-of-scope)", "B3", "_nan_stub", enabled_default=False),
    Feature("H129", 11, "Equibase Speed", "Equibase Speed", "US figure (HKJC-out-of-scope)", "B16,B22", "_nan_stub", enabled_default=False),
    Feature("H130", 11, "Equibase Pace", "Equibase Pace", "US figure (HKJC-out-of-scope)", "B16,B22", "_nan_stub", enabled_default=False),
    Feature("H131", 11, "Ragozin", "Ragozin sheet", "US figure (HKJC-out-of-scope)", "B4,B16", "_nan_stub", enabled_default=False),
    Feature("H132", 11, "AE composite", "Weighted A/E", "Multi-condition A/E", "B22,B30", "h132_ae_composite"),

    # ─── Category 12: Interactions (H133-H146) ────────────────────────────
    Feature("H133", 12, "距離×場地", "Distance×surface", "Joint key", "B14,B16,B41", "h133_dist_surface"),
    Feature("H134", 12, "班次×馬齡", "Class×age", "Joint key", "B6,B23", "h134_class_age"),
    Feature("H135", 12, "騎師×場地", "Jockey×venue", "Joint WR", "B14,B36", "h135_jockey_venue"),
    Feature("H136", 12, "騎師×距離", "Jockey×distance", "Joint WR", "B14,B36", "h136_jockey_dist"),
    Feature("H137", 12, "練馬師×場地", "Trainer×venue", "Joint WR", "B14,B36", "h137_trainer_venue"),
    Feature("H138", 12, "練馬師×班次", "Trainer×class", "Joint angle", "B2,B6", "h138_trainer_class"),
    Feature("H139", 12, "父系×場地", "Sire×surface", "Pedigree-derived", "B5,B26", "_nan_stub", enabled_default=False),
    Feature("H140", 12, "父系×距離", "Sire×distance", "Pedigree-derived", "B26", "_nan_stub", enabled_default=False),
    Feature("H141", 12, "季節×場地", "Season×surface", "Spring rain etc.", "B28,B36", "h141_season_surface"),
    Feature("H142", 12, "步速×班次", "Pace×class", "Hot pace at G1", "B12,B16", "h142_pace_class"),
    Feature("H143", 12, "用藥×場地", "Med×surface", "HK: NaN", "B11,B24", "_nan_stub", enabled_default=False),
    Feature("H144", 12, "馬齡×距離", "Age×distance (dup)", "Mirror of H033", "B14,B23", "h144_age_dist_dup"),
    Feature("H145", 12, "天氣×場地", "Weather×surface", "Rain softening turf", "B28,B29,B41", "h145_weather_surface"),
    Feature("H146", 12, "賽事編號×場地", "Race-no×surface", "Late-card turf wear", "B14,B41", "h146_raceno_surface"),

    # ─── Category 13: Quinella & order (H147-H155) ────────────────────────
    Feature("H147", 13, "Harville P(1-2)", "Harville top-2 prob", "From P(win)", "B8,B9", "_nan_stub", enabled_default=False),
    Feature("H148", 13, "Harville P(1-2-3)", "Harville top-3 prob", "From P(win)", "B8,B9", "_nan_stub", enabled_default=False),
    Feature("H149", 13, "Henery 修正", "Henery refinement", "Exponential time model", "B8,B9", "_nan_stub", enabled_default=False),
    Feature("H150", 13, "Plackett-Luce", "Plackett-Luce likelihood", "Listwise rank", "B9,B31", "_nan_stub", enabled_default=False),
    Feature("H151", 13, "Discounted Harville", "Discounted Harville", "Longshot discount", "B8", "_nan_stub", enabled_default=False),
    Feature("H152", 13, "Q 互補", "Q style complement", "Pair-style complementarity", "B14", "h152_q_compat"),
    Feature("H153", 13, "Q 競爭", "Q field strength", "Strong-jockey count", "B14", "h153_q_strength"),
    Feature("H154", 13, "三甲組合", "Trifecta prob vector", "Top-3 ordered probs", "B8,B16", "_nan_stub", enabled_default=False),
    Feature("H155", 13, "相鄰閘", "Adjacent draw correlation", "Interference prob", "B36,B41", "h155_adjacent_draw"),

    # ─── Category 14: Market signals (H156-H166) ──────────────────────────
    Feature("H156", 14, "開盤賠率", "Opening odds", "First snapshot", "B30,B35", "h156_open_odds"),
    Feature("H157", 14, "收市賠率", "Closing odds", "Final snapshot", "B30,B35", "h157_close_odds"),
    Feature("H158", 14, "賠率走勢", "Odds drift %", "(close-open)/open", "B32,B35", "h158_drift"),
    Feature("H159", 14, "隱含概率", "Implied probability", "1/odds", "B30,B35", "h159_implied"),
    Feature("H160", 14, "公眾集中度", "Public concentration", "Top-3 share of pool", "B14,B22", "h160_concentration"),
    Feature("H161", 14, "Overround", "Overround", "Σ(1/odds) − 1", "B30,B42", "h161_overround"),
    Feature("H162", 14, "BSP", "Betfair SP", "Betfair has no HK pool (HKJC-out-of-scope)", "B13,B32", "_nan_stub", enabled_default=False),
    Feature("H163", 14, "Internal CLV", "Internal CLV", "Bet-odds − close-odds spread", "B13,B32", "h163_clv_internal"),
    Feature("H164", 14, "交易所深度", "Exchange depth", "Betfair has no HK pool (HKJC-out-of-scope)", "B42", "_nan_stub", enabled_default=False),
    Feature("H165", 14, "晚段走勢", "Late steam", "Last-15min move", "B32,B35", "h165_late_steam"),
    Feature("H166", 14, "多平台一致性", "Multi-platform consistency", "Stub", "B32,B35", "_nan_stub", enabled_default=False),
    # ─── Category 15: Track dynamics (H167-H174) ──────────────────────────
    Feature("H167", 15, "當日場地偏差", "Same-day bias", "Winners' style today", "B41,B43", "h167_today_bias"),
    Feature("H168", 15, "par 時間殘差", "Par-time residual", "Race vs par sectional", "B10,B41", "_nan_stub", enabled_default=False),
    Feature("H169", 15, "Rail 配置", "Rail position", "Same as H058 (joined for context)", "B36,B41", "h169_rail_dup"),
    Feature("H170", 15, "賽前灌溉", "Watering cm", "Pre-race watering", "B41", "h170_watering"),
    Feature("H171", 15, "草長", "Grass length cm", "Cut record", "B28,B41", "h171_grass_length"),
    Feature("H172", 15, "內欄殘差", "Inner-draw residual", "Today vs long-term inner", "B41", "h172_inner_resid"),
    Feature("H173", 15, "風向變化", "Wind direction shift", "Headwind for leaders", "B28,B29", "h173_wind"),
    Feature("H174", 15, "Closer 加成", "Same-day closer boost", "Closers winning today", "B12,B41", "h174_closer_boost"),
    # ─── Category 11 (added 2026-05-27): speed-figure approximations ───
    # HKJC doesn't publish Beyer / Timeform / RPR. We derive a Beyer-style
    # signal from race times by computing par-time per (distance, course)
    # bucket from training-window data and reporting the horse's best
    # historical (par − time) as a positive "above par" speed figure.
    Feature("H175", 11, "速度指數均值", "Speed figure (avg)",
            "Mean of past-runs (par − actual) per (distance, course) bucket",
            "B9", "h175_speed_figure_mean"),
    Feature("H176", 11, "速度指數最佳", "Speed figure (best)",
            "Best (par − actual) across past runs — the upper-bound talent signal",
            "B9", "h176_speed_figure_best"),
    Feature("H177", 11, "速度指數最近", "Speed figure (last)",
            "Most recent (par − actual) — recency-weighted form",
            "B9", "h177_speed_figure_last"),
    # ─── Pedigree (added 2026-05-27): sire / dam-based features ─────
    Feature("H178", 1, "父系勝率", "Sire win rate",
            "Sire's offspring win-rate over training-window results",
            "B5", "h178_sire_winrate"),
    Feature("H179", 1, "父系距離勝率", "Sire × distance WR",
            "Sire's offspring win-rate at this race's distance bucket",
            "B5", "h179_sire_dist_winrate"),
    # ─── Field-relative features (added 2026-05-27) ─────────────────
    Feature("H180", 12, "場內評分排名", "Field rating rank",
            "Horse's official rating position within this race's field "
            "(1 = highest-rated; ties averaged)",
            "B9", "h180_field_rating_rank"),
    Feature("H181", 12, "場內評分Z分", "Field rating z-score",
            "Z-score of horse's rating against the field mean", "B9",
            "h181_field_rating_zscore"),
    Feature("H182", 12, "場內出賽次數排名", "Field experience rank",
            "Rank of race_count within the field (lower=more experienced)",
            "B9", "h182_field_experience_rank"),
    Feature("H183", 5, "場數", "Field size",
            "Number of runners in this race (smaller=more model confidence)",
            "B9", "h183_field_size"),
    # ─── Form-change features (added 2026-05-27 Iter 21) ──────────────
    Feature("H184", 3, "距離變化", "Distance delta",
            "Today's distance minus horse's avg historical distance "
            "(positive = stepping up; negative = dropping back)", "B9",
            "h184_distance_delta"),
    Feature("H185", 3, "距離變化z分", "Distance delta z-score",
            "Distance delta normalised by horse's own distance variance",
            "B9", "h185_distance_delta_z"),
    Feature("H186", 4, "騎師×馬匹勝率", "Jockey × horse WR",
            "Win rate when this jockey rode this horse historically",
            "B9", "h186_jockey_horse_wr"),
    # ─── Running-style features (added 2026-05-27 Iter 26) ───────────
    Feature("H187", 10, "平均後上能力", "Avg closing kick",
            "Average (first-call position − final position) across history "
            "— positive = horse closes from the back, negative = drops back",
            "B9", "h187_avg_closing_kick"),
    Feature("H188", 10, "後上強度z分", "Closing kick z-score",
            "Last race's closing kick normalised by horse's own history "
            "stdev — how anomalous was the last performance",
            "B9", "h188_closing_kick_z"),

    # ─── Category 17: Incident-history features (H189-H195) ─────────────────
    # Aggregates of HKJC Racing Incident Report tags (via incident_reports
    # table) across each horse's last 5 starts. Predictive lift verified
    # against bet_ledger: held_position −31.7pp, wide_no_cover −20.4pp,
    # sent_for_sampling +13.5pp vs 40.8% baseline. Use as point-in-time
    # features — each looks back from the target race's date.
    Feature("H189", 17, "近5仗保位率", "Held-position rate last 5",
            "Fraction of last 5 starts where the horse held its position "
            "(|first-call position − final position| ≤ 2). Negative bet-outcome "
            "predictor (9.1% win-rate when present vs 40.8% baseline).",
            "B9", "h189_held_position_rate"),
    Feature("H190", 17, "近5仗走外疊率", "Wide-trip rate last 5",
            "Fraction of last 5 starts tagged raced_wide (or wide_no_cover) "
            "in HKJC's Racing Incident Report. Wide trips cost ground.",
            "B9,B27", "h190_wide_trip_rate"),
    Feature("H191", 17, "近5仗賽後抽驗率", "Sent-for-sampling rate last 5",
            "Fraction of last 5 starts where HKJC flagged 'sent for sampling'. "
            "Counter-intuitive POSITIVE signal — sampling fires both randomly "
            "and on exceptional performances (+13.5pp win-rate when present).",
            "B27", "h191_sampling_rate"),
    Feature("H192", 17, "近5仗賽後驗馬次數", "Vet-inspection count last 5",
            "Count of stewards' 'vet inspection' tags in last 5 starts. "
            "Medical-risk proxy.",
            "B27", "h192_vet_inspection_count"),
    Feature("H193", 17, "近5仗受阻次數", "Bumped/checked count last 5",
            "Count of incidents where horse was bumped, steadied, crowded or "
            "hampered. High counts may mean published positions undervalue "
            "true ability (horse was hampered, not slow).",
            "B27", "h193_bumped_count"),
    Feature("H194", 17, "近5仗後上分", "Closer-style score last 5",
            "Average (first-call position − final position) across last 5 "
            "starts. Positive = closer / late kicker; negative = front-runner. "
            "Same direction as H187 but bounded to recent form, so it weights "
            "current pace style rather than career.",
            "B9", "h194_closer_style_score"),
    Feature("H195", 17, "保位異常分", "Held-position anomaly",
            "Difference between last-race held_position-rate and 5-race mean. "
            "Positive = horse is suddenly holding position more than usual "
            "(losing kick); negative = improving on its baseline.",
            "B9", "h195_held_position_anomaly"),
]

assert len(FEATURES) == 195, f"expected 195 features, got {len(FEATURES)}"
assert len({f.id for f in FEATURES}) == 195, "duplicate feature ids"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def features_by_category() -> dict[int, list[Feature]]:
    out: dict[int, list[Feature]] = {}
    for f in FEATURES:
        out.setdefault(f.category, []).append(f)
    return out


def seed_catalog(db_path: Path = DB_PATH) -> int:
    if not db_path.exists():
        raise SystemExit(f"DB not found at {db_path}; run db.py --init first")
    conn = sqlite3.connect(db_path)
    rows = 0
    for f in FEATURES:
        conn.execute(
            """
            INSERT INTO feature_catalog
                (feature_id, category, name_zh, name_en, definition,
                 source_refs, compute_module, depends_on, enabled_default, nan_permitted)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(feature_id) DO UPDATE SET
                category=excluded.category, name_zh=excluded.name_zh, name_en=excluded.name_en,
                definition=excluded.definition, source_refs=excluded.source_refs,
                compute_module=excluded.compute_module, depends_on=excluded.depends_on,
                enabled_default=excluded.enabled_default, nan_permitted=excluded.nan_permitted
            """,
            (f.id, f.category, f.name_zh, f.name_en, f.definition,
             f.source_refs, f"features.compute.{f.compute_fn_name}",
             f.depends_on, 1 if f.enabled_default else 0,
             1 if f.nan_permitted else 0),
        )
        rows += 1
    conn.commit()
    conn.close()
    return rows


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--seed", action="store_true", help="upsert all 174 features into feature_catalog")
    p.add_argument("--list", action="store_true", help="print catalog")
    args = p.parse_args()
    if args.seed:
        n = seed_catalog()
        print(f"seeded {n} features into feature_catalog")
    if args.list:
        for f in FEATURES:
            print(f"{f.id} cat{f.category:>2}  {f.name_en:<32}  {f.compute_fn_name}")
    if not (args.seed or args.list):
        p.print_help()


if __name__ == "__main__":
    main()

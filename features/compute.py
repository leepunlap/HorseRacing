"""Per-feature compute functions.

Each function has the signature:
    def hNNN_xxx(ctx: FeatureContext) -> float | None
where the context carries point-in-time data:
    ctx.race            — dict of the target race (date, course, race_no, distance, class, going, participants, ...)
    ctx.entry           — dict of THIS horse's entry (brand, jockey, trainer, draw, weight, ...)
    ctx.history         — list of dicts of this horse's prior runs strictly before ctx.race['date']
    ctx.field           — list of entry dicts for the full field (used for relative/Q features)
    ctx.field_history   — dict[brand]->list-of-prior-runs for every horse in the field
    ctx.global_stats    — pre-computed aggregates: jockey_wr, trainer_wr, jt_pair, jh_pair, sire_x_dist (sparse if unscraped), ...
    ctx.race_id         — int row id, for joins
    ctx.conn            — sqlite3 connection for ad-hoc lookups (vet, trackwork, barrier_trials, etc.)
    ctx.snapshot_basis  — ISO-8601 cutoff (strict point-in-time)

Returns float, or None to mean NaN. Any compute function may raise; the pipeline
catches and records NaN.
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass, field as _dc_field
from datetime import datetime
from typing import Any


# ─── Field-average smoothing priors (Bayesian shrinkage) ──────────────────────
PRIOR = {
    "horse_wr":   (5,  0.083),
    "jockey_wr":  (20, 0.083),
    "trainer_wr": (30, 0.083),
    "jt_pair":    (10, 0.083),
    "jh_pair":    (3,  0.083),
    "dist":       (5,  0.083),
    "going":      (3,  0.083),
}


def _shrink(wins: int, runs: int, prior_key: str) -> float:
    n0, p0 = PRIOR[prior_key]
    return (wins + n0 * p0) / (runs + n0)


@dataclass
class FeatureContext:
    race: dict[str, Any]
    entry: dict[str, Any]
    history: list[dict[str, Any]] = _dc_field(default_factory=list)
    field: list[dict[str, Any]] = _dc_field(default_factory=list)
    field_history: dict[str, list[dict[str, Any]]] = _dc_field(default_factory=dict)
    global_stats: dict[str, Any] = _dc_field(default_factory=dict)
    race_id: int | None = None
    conn: sqlite3.Connection | None = None
    snapshot_basis: str = ""


# ─── Catch-all stub ───────────────────────────────────────────────────────────
def _nan_stub(_ctx: FeatureContext) -> float | None:
    return None


# ─── Category 1: Horse profile ────────────────────────────────────────────────
def h001_age(c): return _g(c.entry, "age")
def h002_gelding(c): return 1.0 if (c.entry.get("sex") or "").lower().startswith("g") else 0.0
def h003_sex(c):
    s = (c.entry.get("sex") or "").lower()
    return {"c": 0, "f": 1, "g": 2, "m": 3}.get(s[:1], None) if s else None
def h004_colour(c):
    return hash((c.entry.get("colour") or "")) % 8 if c.entry.get("colour") else None
def h007_decl_weight(c): return _g(c.entry, "decl_wt")
def h008_weight_delta(c):
    cur = _g(c.entry, "decl_wt")
    if cur is None or not c.history:
        return None
    prev = next((_g(h, "decl_wt") for h in c.history if _g(h, "decl_wt") is not None), None)
    return None if prev is None else cur - prev
def h009_rating(c): return _g(c.entry, "rating")
def h010_starts(c): return float(len(c.history))
def h014_origin(c):
    o = (c.entry.get("origin") or "").upper()
    return {"AUS": 1, "NZL": 2, "GB": 3, "IRE": 4, "FR": 5, "USA": 6, "RSA": 7}.get(o[:3], 0)


# ─── Category 2: WR & returns ────────────────────────────────────────────────
def h015_horse_wr(c):
    runs = len(c.history)
    wins = sum(1 for h in c.history if (h.get("position") or 99) == 1)
    return _shrink(wins, runs, "horse_wr")
def h016_jockey_wr(c):
    return c.global_stats.get("jockey_wr", {}).get(c.entry.get("jockey"))
def h017_trainer_wr(c):
    return c.global_stats.get("trainer_wr", {}).get(c.entry.get("trainer"))
def h018_jt_pair(c):
    key = (c.entry.get("jockey"), c.entry.get("trainer"))
    return c.global_stats.get("jt_pair", {}).get(key)
def h019_jh_pair(c):
    runs_h = c.history
    pair_runs = sum(1 for r in runs_h if r.get("jockey") == c.entry.get("jockey"))
    pair_wins = sum(1 for r in runs_h if r.get("jockey") == c.entry.get("jockey") and (r.get("position") or 99) == 1)
    if pair_runs == 0:
        return None
    return _shrink(pair_wins, pair_runs, "jh_pair")
def h020_place_rate(c):
    runs = len(c.history)
    placed = sum(1 for h in c.history if 1 <= (h.get("position") or 99) <= 2)
    return placed / runs if runs else None
def h021_top3_rate(c):
    runs = len(c.history)
    top3 = sum(1 for h in c.history if 1 <= (h.get("position") or 99) <= 3)
    return top3 / runs if runs else None
def h022_roi(c):
    runs = c.history
    if not runs:
        return None
    payoff = 0.0
    for h in runs:
        if (h.get("position") or 99) == 1 and h.get("odds"):
            payoff += float(h["odds"])
    return (payoff / len(runs)) - 1.0
def h023_ae(c):
    wr = h015_horse_wr(c)
    avg_implied = c.global_stats.get("horse_avg_implied", {}).get(c.entry.get("brand"))
    if wr is None or avg_implied is None or avg_implied <= 0:
        return None
    return wr / avg_implied
def h024_iv(c):
    wr = h015_horse_wr(c)
    base = c.global_stats.get("field_avg_wr", 0.083)
    return None if wr is None else wr / base
def h025_jockey_hv(c):
    return c.global_stats.get("jockey_at_HV", {}).get(c.entry.get("jockey"))
def h026_jockey_st(c):
    return c.global_stats.get("jockey_at_ST", {}).get(c.entry.get("jockey"))
def h027_trainer_hv(c):
    return c.global_stats.get("trainer_at_HV", {}).get(c.entry.get("trainer"))
def h028_big3_jockey(c):
    bigs = {"Z Purton", "H Bowman", "J Moreira", "L Ferraris"}
    return 1.0 if (c.entry.get("jockey") or "").strip() in bigs else 0.0


# ─── Category 3: Adaptability ────────────────────────────────────────────────
def h029_dist_adapt(c):
    d = c.race.get("distance")
    if d is None:
        return None
    matches = [h for h in c.history if abs((h.get("distance") or 0) - d) <= 100]
    if not matches:
        return None
    wins = sum(1 for h in matches if (h.get("position") or 99) == 1)
    return _shrink(wins, len(matches), "dist")
def h030_going_adapt(c):
    g = (c.race.get("going") or "").lower()[:4]
    matches = [h for h in c.history if (h.get("going") or "").lower().startswith(g)]
    if not matches:
        return None
    wins = sum(1 for h in matches if (h.get("position") or 99) == 1)
    return _shrink(wins, len(matches), "going")
def h033_age_dist(c):
    age = c.entry.get("age")
    d = c.race.get("distance")
    if age is None or d is None:
        return None
    if age <= 3 and d >= 1600:
        return 0.4
    if age >= 7 and d >= 2000:
        return 0.5
    return 1.0
def h035_season(c):
    month = datetime.fromisoformat(c.race["date"]).month
    matches = [h for h in c.history if datetime.fromisoformat(h["date"]).month == month] if c.history else []
    if not matches:
        return None
    wins = sum(1 for h in matches if (h.get("position") or 99) == 1)
    return _shrink(wins, len(matches), "horse_wr")
def h036_class_wr(c):
    cls = (c.race.get("class") or "").strip()
    matches = [h for h in c.history if (h.get("class") or "").strip() == cls]
    if not matches:
        return None
    wins = sum(1 for h in matches if (h.get("position") or 99) == 1)
    return _shrink(wins, len(matches), "horse_wr")
def h038_recovery(c):
    days = h084_days_since(c)
    last_pos = c.history[-1].get("position") if c.history else None
    if days is None:
        return None
    burden = 1.0 if (last_pos and last_pos <= 3) else 0.6
    return days * burden


# ─── Category 4: Trainer form ────────────────────────────────────────────────
def h039_trainer_hot(c):
    return c.global_stats.get("trainer_hot", {}).get(c.entry.get("trainer"))
def h040_trainer_cold(c):
    return c.global_stats.get("trainer_cold", {}).get(c.entry.get("trainer"))
def h041_trainer_class(c):
    return c.global_stats.get("trainer_x_class", {}).get((c.entry.get("trainer"), (c.race.get("class") or "")))
def h042_trainer_dist(c):
    return c.global_stats.get("trainer_x_dist", {}).get((c.entry.get("trainer"), c.race.get("distance")))
def h043_trainer_season(c):
    month = datetime.fromisoformat(c.race["date"]).month
    phase = "early" if month in (9, 10, 11) else "mid" if month in (12, 1, 2, 3) else "late"
    return c.global_stats.get("trainer_x_phase", {}).get((c.entry.get("trainer"), phase))
def h044_trainer_debut(c):
    return c.global_stats.get("trainer_first_timer_wr", {}).get(c.entry.get("trainer"))
def h045_trainer_returner(c):
    return c.global_stats.get("trainer_returner_wr", {}).get(c.entry.get("trainer"))
def h046_trainer_venue(c):
    return c.global_stats.get("trainer_x_venue", {}).get((c.entry.get("trainer"), c.race.get("course")))
def h047_trainer_density(c):
    return c.global_stats.get("trainer_density_30d", {}).get(c.entry.get("trainer"))
def h048_stable_size(c):
    return c.global_stats.get("stable_size", {}).get(c.entry.get("trainer"))


# ─── Category 5: Draw ────────────────────────────────────────────────────────
def h050_draw(c): return _g(c.entry, "draw")
def h051_draw_inner(c):
    d = h050_draw(c); return 1.0 if (d is not None and d <= 3) else 0.0
def h052_draw_outer(c):
    d = h050_draw(c); n = c.race.get("participants") or 12
    return 1.0 if (d is not None and d >= n - 2) else 0.0
def h053_draw_wide(c):
    d = h050_draw(c); n = c.race.get("participants") or 12
    return 1.0 if (d is not None and d >= n - 1) else 0.0
def h054_track_dist_bias(c):
    # Coarse lookup: HV<=1200 inner edge, ST 1000 inner edge, ST 1600+ neutral
    course = c.race.get("course"); dist = c.race.get("distance") or 0; d = h050_draw(c)
    if d is None:
        return None
    if course == "HV" and dist <= 1200:
        return 1.0 if d <= 4 else -0.5
    if course == "ST" and dist <= 1200:
        return 0.5 if d <= 5 else 0.0
    return 0.0
def h055_to_first_turn(c):
    course = c.race.get("course"); dist = c.race.get("distance") or 0
    # ST has long straights; HV is tight. Crude meters-to-first-turn proxy.
    if course == "ST":
        if dist in (1000, 1200): return 280.0
        if dist == 1600: return 380.0
        if dist >= 2000: return 200.0
    if course == "HV":
        if dist <= 1200: return 200.0
        if dist == 1650: return 250.0
    return None
def h057_curvature(c):
    return 1450.0 if c.race.get("course") == "HV" else 1900.0
def h058_rail(c):
    if c.conn is None:
        return None
    row = c.conn.execute(
        "SELECT rail FROM rail_position WHERE date = ? AND course = ?",
        (c.race["date"], c.race["course"])
    ).fetchone()
    if not row or row[0] is None:
        return None
    enc = {"A": 0, "B": 1, "C": 2, "C+3": 3}
    return enc.get(row[0].strip()) if row[0] else None
def h059_awt_draw(c):
    if (c.race.get("going") or "").lower() != "awt":
        return None
    return h052_draw_outer(c)
def h060_draw_style(c):
    inner = h051_draw_inner(c); style = h109_style(c)
    if inner is None or style is None: return None
    if inner == 1 and style == 0: return 1.0   # inner+leader
    if inner == 0 and style == 3: return 0.8   # outer+closer
    return 0.0


# ─── Category 6: Weight ──────────────────────────────────────────────────────
def h061_weight(c): return _g(c.entry, "act_wt") or _g(c.entry, "weight")
def h062_claim(c):
    # Crude: jockey weight allowance encoded as -3/-5/-7 in some sources;
    # we accept act_wt vs decl_wt delta if both present.
    a, d = _g(c.entry, "act_wt"), _g(c.entry, "decl_wt")
    return None if (a is None or d is None) else (d - a)
def h063_weight_trend(c):
    cur = h061_weight(c)
    if cur is None or not c.history:
        return None
    last = next((_g(h, "act_wt") for h in reversed(c.history) if _g(h, "act_wt")), None)
    return None if last is None else cur - last
def h064_decl_weight_dup(c): return h007_decl_weight(c)
def h065_weight_delta_dup(c): return h008_weight_delta(c)
def h068_apprentice_grade(c):
    claim = h062_claim(c)
    if claim is None: return None
    if claim >= 7: return 7.0
    if claim >= 5: return 5.0
    if claim >= 3: return 3.0
    return 0.0
def h069_sex_allowance(c):
    return 3.0 if (c.entry.get("sex") or "").lower().startswith(("f", "m")) else 0.0
def h070_wfa(c):
    age = c.entry.get("age"); dist = c.race.get("distance") or 0
    if age is None: return None
    if age == 3 and dist >= 2000: return 4.0
    if age == 3 and dist >= 1400: return 2.0
    return 0.0


# ─── Category 7: Race context ────────────────────────────────────────────────
def h071_is_hv(c): return 1.0 if c.race.get("course") == "HV" else 0.0
def h072_distance(c): return _g(c.race, "distance")
def h073_going(c):
    g = (c.race.get("going") or "").lower()
    return {"good": 0, "good to firm": 0.5, "good to yielding": 1.0, "yielding": 2.0,
            "soft": 3.0, "heavy": 4.0, "awt": 0.5}.get(g)
def h074_class(c):
    """Numeric class encoding. Lower = higher class (G1=1.0, C5=5.0).

    Handles every form HKJC stores in `races.class`:
      - Group / Listed: 'G1', 'G2', 'G3', 'LISTED'
      - Class text:     'Class 1' … 'Class 5', 'C1' … 'C5'
      - Plain number:   '1' … '5'
      - Float string:   '1.0' … '5.0'  (the modern HKJC dump format)
    """
    cls = (c.race.get("class") or "").strip().upper()
    if not cls:
        return None
    if cls.startswith("G1"): return 1.0
    if cls.startswith("G2"): return 1.5
    if cls.startswith("G3"): return 2.0
    if "LISTED" in cls: return 2.5
    # Strip 'CLASS ' prefix and 'C' prefix.
    norm = cls.replace("CLASS", "").replace("C", "").strip()
    try:
        n = int(float(norm))
    except (ValueError, TypeError):
        return None
    # 1→3.0, 2→3.5, 3→4.0, 4→4.5, 5→5.0 — matches the original G1-C5 ramp.
    if 1 <= n <= 5:
        return 3.0 + 0.5 * (n - 1)
    return None
    return None
def h075_field_size(c): return float(c.race.get("participants") or len(c.field) or 0)
def h076_prize(c):
    p = c.race.get("prize") or ""
    digits = "".join(ch for ch in str(p) if ch.isdigit())
    return float(digits) if digits else None
def h077_group_listed(c):
    cls = (c.race.get("class") or "").upper()
    return 1.0 if (cls.startswith("G") or "LISTED" in cls) else 0.0
def h078_intl_race(c):
    name = (c.race.get("race_name") or "").lower()
    return 1.0 if any(k in name for k in ("international", "hkir", "champions day")) else 0.0
def h079_season_phase(c):
    m = datetime.fromisoformat(c.race["date"]).month
    if m in (9, 10, 11): return 0.0
    if m in (12, 1, 2, 3): return 1.0
    return 2.0
def h080_day_night(c):
    return 1.0 if c.race.get("course") == "HV" else 0.0   # HV is night-only
def h081_temperature(c):
    if c.conn is None: return None
    r = c.conn.execute(
        "SELECT temperature_c FROM weather_observations WHERE date = ? AND course = ? AND race_no = ?",
        (c.race["date"], c.race["course"], c.race["race_no"])
    ).fetchone()
    return r[0] if r else None
def h082_rainfall(c):
    if c.conn is None: return None
    r = c.conn.execute(
        "SELECT rainfall_mm FROM weather_observations WHERE date = ? AND course = ? AND race_no = ?",
        (c.race["date"], c.race["course"], c.race["race_no"])
    ).fetchone()
    return r[0] if r else None
def h083_race_no(c): return float(c.race.get("race_no") or 0)


# ─── Category 8: Recent form ─────────────────────────────────────────────────
def h084_days_since(c):
    if not c.history:
        return None
    last = c.history[-1].get("date")
    if not last:
        return None
    return (datetime.fromisoformat(c.race["date"]) - datetime.fromisoformat(last)).days
def h085_layoff_penalty(c):
    d = h084_days_since(c)
    if d is None: return None
    if d > 60: return -12.0
    if d > 28: return -6.0
    return 0.0
def h086_rating_trend(c):
    rs = [h.get("rating") for h in c.history if h.get("rating") is not None]
    if len(rs) < 4: return None
    half = len(rs) // 2
    return sum(rs[-half:]) / half - sum(rs[:half]) / half
def h087_class_drop(c):
    """1.0 if the horse is dropping to an easier class than its last start.
    Class numeric encoding via h074_class: G1=1.0 (highest)..C5=5.0 (lowest).
    A *drop* means today's class number is strictly greater than last start's.
    """
    if not c.history:
        return None
    last_cls = (c.history[-1].get("class") or "").strip()
    cur_cls = (c.race.get("class") or "").strip()
    if not last_cls or not cur_cls:
        return None
    last_enc = h074_class(FeatureContext(race={"class": last_cls}, entry={}))
    cur_enc = h074_class(FeatureContext(race={"class": cur_cls}, entry={}))
    if last_enc is None or cur_enc is None:
        return None
    return 1.0 if cur_enc > last_enc else 0.0
def h088_win_streak(c):
    s = 0
    for h in reversed(c.history):
        if (h.get("position") or 99) == 1: s += 1
        else: break
    return float(s)
def h089_loss_streak(c):
    s = 0
    for h in reversed(c.history):
        if (h.get("position") or 99) != 1: s += 1
        else: break
    return float(s)
def h090_last_pos(c):
    return c.history[-1].get("position") if c.history else None
def h091_last_lbw(c):
    """Lengths behind the winner at the line of the horse's most recent run.

    HKJC stores `lbw` as a free-form string: '4-3/4' (4 and 3/4 lengths),
    '5-1/2', '3/4', plus textual descriptors like '短馬頭位'/short head, '鼻位'/nose,
    and '---' for the winner. Parse to a float in lengths.
    """
    if not c.history:
        return None
    return _parse_lbw(c.history[-1].get("lbw"))


# Chinese-language head-distance descriptors used by HKJC results pages.
# Values are conservative estimates in lengths.
_LBW_WORD: dict[str, float] = {
    "鼻位": 0.05,    # nose
    "短鼻位": 0.03,
    "短馬頭位": 0.10, # short head
    "馬頭位": 0.20,   # head
    "頸位": 0.30,    # neck
    "短頸位": 0.20,
    "半個馬位": 0.50, # half a length
    "短半馬位": 0.40,
    "3/4馬位": 0.75,
    "1馬位": 1.0,
}


def _parse_lbw(raw) -> float | None:
    """Convert HKJC lbw string to lengths. Returns None for winners ('---') or
    unparseable values."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            f = float(raw)
            return None if math.isnan(f) else f
        except (TypeError, ValueError):
            return None
    s = str(raw).strip()
    if not s or s in ("---", "--", "-"):
        return None
    if s in _LBW_WORD:
        return _LBW_WORD[s]
    # Mixed-number form: '4-3/4' = 4 + 3/4 = 4.75
    if "-" in s and "/" in s:
        whole, frac = s.split("-", 1)
        try:
            num, den = frac.split("/")
            return float(whole) + float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            return None
    # Pure fraction: '3/4'
    if "/" in s:
        try:
            num, den = s.split("/")
            return float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            return None
    # Plain number string
    try:
        return float(s)
    except ValueError:
        return None
def h094_barrier_trial(c):
    if c.conn is None: return None
    r = c.conn.execute(
        "SELECT position FROM barrier_trials WHERE brand = ? AND date < ? "
        "ORDER BY date DESC LIMIT 1",
        (c.entry["brand"], c.race["date"])
    ).fetchone()
    return float(r[0]) if r and r[0] else None
def h095_trackwork(c):
    """Trackwork sessions in the 14 days leading up to the race — a
    fitness proxy. HKJC's trackwork dump rarely carries distance/time
    (mostly stub records like 'Swimming' / 'Trotting'), so we just COUNT
    sessions. More sessions = more recent activity = a horse being
    actively trained."""
    if c.conn is None: return None
    r = c.conn.execute(
        "SELECT COUNT(*) FROM trackwork WHERE brand = ? "
        "AND date BETWEEN date(?, '-14 days') AND date(?, '-1 day')",
        (c.entry["brand"], c.race["date"], c.race["date"])
    ).fetchone()
    return float(r[0]) if r else 0.0
def h096_same_jockey_streak(c):
    if not c.history: return None
    jock = c.entry.get("jockey")
    s = 0
    for h in reversed(c.history):
        if h.get("jockey") == jock: s += 1
        else: break
    return float(s)


# ─── Category 9: Gear & vet ──────────────────────────────────────────────────
def h097_gear_change(c):
    """1.0 if any gear changed vs last start (added or removed)."""
    cur = c.entry.get("gear") or ""
    last = c.history[-1].get("gear") if c.history else ""
    if not cur or not last:
        return None
    return 1.0 if cur != last else 0.0


def h098_first_gear(c):
    """1.0 if current gear contains a token the horse has *never* worn before.
    Stronger signal than h097 since it ignores swaps between previously-tried gear.
    """
    cur_tokens = set((c.entry.get("gear") or "").upper())
    if not cur_tokens:
        return None
    seen: set[str] = set()
    for h in c.history:
        seen.update((h.get("gear") or "").upper())
    if not seen:
        return None
    novel = cur_tokens - seen - {" "}
    return 1.0 if novel else 0.0
def h099_blinkers(c):
    cur = c.entry.get("gear") or ""
    if "B" not in cur.upper(): return 0.0
    last = c.history[-1].get("gear") if c.history else ""
    return 1.0 if "B" not in (last or "").upper() else 0.5
def h103_multi_gear(c):
    cur = (c.entry.get("gear") or "").upper()
    return float(sum(1 for ch in "BVHTPC" if ch in cur))
def h105_vet(c):
    if c.conn is None: return None
    r = c.conn.execute(
        "SELECT COUNT(*) FROM vet_records WHERE brand = ? AND date BETWEEN date(?, '-30 days') AND ?",
        (c.entry["brand"], c.race["date"], c.race["date"])
    ).fetchone()
    return float(r[0]) if r else 0.0
def h106_roarer(c):
    if c.conn is None: return None
    r = c.conn.execute(
        "SELECT 1 FROM vet_records WHERE brand = ? AND type = 'roarer-surgery' LIMIT 1",
        (c.entry["brand"],)
    ).fetchone()
    return 1.0 if r else 0.0


# ─── Category 10: Pace ───────────────────────────────────────────────────────
def h108_race_pace(c):
    # Count likely early-speed runners in field.
    fast = 0
    for entry in c.field:
        b = entry["brand"]; hist = c.field_history.get(b) or []
        if not hist: continue
        runs = sum(1 for h in hist if (h.get("running") or "").startswith("1"))
        if runs and runs / len(hist) > 0.3: fast += 1
    n = len(c.field) or 1
    ratio = fast / n
    return 2.0 if ratio > 0.4 else 1.0 if ratio < 0.15 else 0.0
def h109_style(c):
    if not c.history: return 1.0
    # average lead-position at first call across history
    leads = [int((h.get("running") or "0")[:1] or 0) for h in c.history]
    leads = [x for x in leads if x > 0]
    if not leads: return 1.0
    avg = sum(leads) / len(leads)
    if avg <= 2: return 0.0
    if avg <= 4: return 1.0
    if avg <= 7: return 2.0
    return 3.0
def h110_pace_match(c):
    p, s = h108_race_pace(c), h109_style(c)
    if p is None or s is None: return None
    if p == 0 and s == 0: return -0.5  # fast pace, leader struggles
    if p == 2 and s == 3: return -0.3  # slow pace, closer struggles
    if p == 0 and s == 3: return 0.5   # fast pace, closer benefits
    return 0.0
def h111_draw_pace_bonus(c):
    d = h050_draw(c); p = h108_race_pace(c)
    if d is None or p is None: return None
    if d <= 3 and p == 0: return 0.5
    if d <= 3 and p == 2: return -0.3
    return 0.0
def h112_lead_profile(c):
    if not c.history: return None
    leads = sum(1 for h in c.history if (h.get("running") or "").startswith("1"))
    return leads / len(c.history)
def h115_late_pace(c):
    """Average race-level late_pace across the horse's history.

    Per-horse furlong splits are not published by HKJC, so we use the race-level
    figure from `sectionals` as a proxy for "the late pace of races this horse
    has competed in." It correlates with how hard the late stage was generally.

    Note: sectionals.race_id is unpopulated (the v1 scraper didn't backfill
    it), so we match on (date, course, distance) directly.
    """
    if c.conn is None or not c.history:
        return None
    vals: list[float] = []
    for h in c.history:
        d, dist = h.get("date"), h.get("distance")
        venue = _venue_code(h.get("venue"))  # was Chinese tag, need course code
        if not (d and dist):
            continue
        r = c.conn.execute(
            "SELECT late_pace FROM sectionals "
            "WHERE date = ? AND distance = ? AND (course = ? OR ? IS NULL) "
            "LIMIT 1",
            (d, dist, venue, venue),
        ).fetchone()
        if r and r[0] is not None:
            vals.append(float(r[0]))
    return sum(vals) / len(vals) if vals else None


def h116_early_pace(c):
    """Same trick as h115 but for early pace."""
    if c.conn is None or not c.history:
        return None
    vals: list[float] = []
    for h in c.history:
        d, dist = h.get("date"), h.get("distance")
        venue = _venue_code(h.get("venue"))  # was Chinese tag, need course code
        if not (d and dist):
            continue
        r = c.conn.execute(
            "SELECT early_pace FROM sectionals "
            "WHERE date = ? AND distance = ? AND (course = ? OR ? IS NULL) "
            "LIMIT 1",
            (d, dist, venue, venue),
        ).fetchone()
        if r and r[0] is not None:
            vals.append(float(r[0]))
    return sum(vals) / len(vals) if vals else None
def h117_pace_pressure(c): return h108_race_pace(c)
def h119_pace_benefit(c):
    style = h109_style(c); pace = h108_race_pace(c)
    if style is None or pace is None: return None
    return 0.5 if (style >= 2 and pace == 0) else 0.0
def h120_style_purity(c):
    s = h109_style(c)
    if s is None or not c.history: return None
    matches = sum(1 for h in c.history if int((h.get("running") or "0")[:1] or 0) and abs(int((h.get("running") or "0")[:1] or 0) - (s * 2 + 1)) <= 2)
    return matches / len(c.history)
def h121_overtake(c):
    deltas = []
    for h in c.history:
        run = h.get("running") or ""
        pos = h.get("position")
        try:
            first = int(run[:1] or 0)
        except Exception:
            continue
        if first and pos:
            deltas.append(first - pos)
    return sum(deltas) / len(deltas) if deltas else None


# ─── Category 11: Composite ──────────────────────────────────────────────────
def h122_chri(c):
    pieces = [h085_layoff_penalty(c), h109_style(c), h029_dist_adapt(c)]
    pieces = [x for x in pieces if x is not None]
    return sum(pieces) / len(pieces) if pieces else None
def h132_ae_composite(c):
    ae = h023_ae(c); iv = h024_iv(c)
    if ae is None and iv is None: return None
    parts = [x for x in (ae, iv) if x is not None]
    return sum(parts) / len(parts)


# ─── Category 12: Interactions ───────────────────────────────────────────────
def h133_dist_surface(c):
    d = c.race.get("distance"); g = (c.race.get("going") or "").lower()
    matches = [h for h in c.history if h.get("distance") == d and (h.get("going") or "").lower() == g]
    if not matches: return None
    wins = sum(1 for h in matches if (h.get("position") or 99) == 1)
    return _shrink(wins, len(matches), "dist")
def h134_class_age(c):
    age = c.entry.get("age"); cls = (c.race.get("class") or "").strip()
    if age is None or not cls: return None
    if age == 3 and cls in {"1", "G1", "G2"}: return 0.3
    return 1.0
def h135_jockey_venue(c):
    return c.global_stats.get("jockey_x_venue", {}).get((c.entry.get("jockey"), c.race.get("course")))
def h136_jockey_dist(c):
    return c.global_stats.get("jockey_x_dist", {}).get((c.entry.get("jockey"), c.race.get("distance")))
def h137_trainer_venue(c): return h046_trainer_venue(c)
def h138_trainer_class(c): return h041_trainer_class(c)
def h141_season_surface(c):
    m = datetime.fromisoformat(c.race["date"]).month; g = (c.race.get("going") or "").lower()
    if m in (4, 5) and g in ("soft", "yielding"): return 1.0
    return 0.0
def h142_pace_class(c):
    cls = h074_class(c); p = h108_race_pace(c)
    if cls is None or p is None: return None
    return 1.0 if (cls <= 2.0 and p == 0) else 0.0
def h144_age_dist_dup(c): return h033_age_dist(c)
def h145_weather_surface(c):
    rain = h082_rainfall(c); g = (c.race.get("going") or "").lower()
    if rain is None or not g: return None
    return 1.0 if (rain > 5 and g.startswith("g")) else 0.0
def h146_raceno_surface(c):
    rn = c.race.get("race_no") or 0; g = (c.race.get("going") or "").lower()
    return 1.0 if (rn >= 8 and g.startswith("g")) else 0.0


# ─── Category 13: Q & order ──────────────────────────────────────────────────
def h152_q_compat(c):
    s = h109_style(c)
    if s is None: return None
    field_styles = []
    for entry in c.field:
        if entry["brand"] == c.entry["brand"]: continue
        hist = c.field_history.get(entry["brand"]) or []
        if not hist: continue
        leads = [int((h.get("running") or "0")[:1] or 0) for h in hist if (h.get("running") or "")[:1].isdigit()]
        if leads:
            avg = sum(leads) / len(leads)
            field_styles.append(0 if avg <= 2 else 1 if avg <= 4 else 2 if avg <= 7 else 3)
    if not field_styles: return None
    return sum(1 for x in field_styles if abs(x - s) >= 2) / len(field_styles)
def h153_q_strength(c):
    return sum(1 for entry in c.field
               if (c.global_stats.get("jockey_wr", {}).get(entry.get("jockey")) or 0) > 0.15)
def h155_adjacent_draw(c):
    d = h050_draw(c)
    if d is None: return None
    return float(sum(1 for entry in c.field
                     if abs((entry.get("draw") or 0) - d) == 1
                     and entry["brand"] != c.entry["brand"]))


# ─── Category 14: Market ─────────────────────────────────────────────────────
def _odds_snapshots(c) -> list[tuple[str, float | None]]:
    if c.conn is None: return []
    rows = c.conn.execute(
        "SELECT ts, win_odds FROM odds_snapshots WHERE race_id = ? AND brand = ? "
        "AND ts <= ? ORDER BY ts ASC",
        (c.race_id, c.entry["brand"], c.snapshot_basis or "9999")
    ).fetchall()
    return [(r[0], r[1]) for r in rows]
def h156_open_odds(c):
    s = _odds_snapshots(c); return s[0][1] if s else None
def h157_close_odds(c):
    s = _odds_snapshots(c); return s[-1][1] if s else None
def h158_drift(c):
    s = _odds_snapshots(c)
    if len(s) < 2 or not s[0][1]: return None
    return (s[-1][1] - s[0][1]) / s[0][1]
def h159_implied(c):
    o = h157_close_odds(c)
    return None if o is None or o <= 0 else 1.0 / o
def h160_concentration(c):
    if c.conn is None: return None
    rows = c.conn.execute(
        "SELECT brand, win_odds FROM odds_snapshots WHERE race_id = ? AND ts = "
        "(SELECT MAX(ts) FROM odds_snapshots WHERE race_id = ?)",
        (c.race_id, c.race_id)
    ).fetchall()
    if not rows: return None
    inv = [1.0 / r[1] for r in rows if r[1] and r[1] > 0]
    if not inv: return None
    inv.sort(reverse=True)
    return sum(inv[:3]) / sum(inv) if sum(inv) > 0 else None
def h161_overround(c):
    if c.conn is None: return None
    rows = c.conn.execute(
        "SELECT win_odds FROM odds_snapshots WHERE race_id = ? AND ts = "
        "(SELECT MAX(ts) FROM odds_snapshots WHERE race_id = ?)",
        (c.race_id, c.race_id)
    ).fetchall()
    inv = [1.0 / r[0] for r in rows if r[0] and r[0] > 0]
    return sum(inv) - 1.0 if inv else None
def h163_clv_internal(c):
    # Captured at bet-placement time vs. closing odds. Placeholder uses
    # earliest vs latest snapshot ratio as a proxy.
    s = _odds_snapshots(c)
    if len(s) < 2 or not s[0][1] or not s[-1][1]: return None
    return (1.0 / s[-1][1]) - (1.0 / s[0][1])
def h165_late_steam(c):
    """Odds drift % across snapshots in the last 15 minutes before the basis time.

    Positive value means the price drifted out (longer); negative means it
    steamed in (shorter — "smart money" coming late).
    """
    s = _odds_snapshots(c)
    if len(s) < 2:
        return None
    if c.snapshot_basis:
        try:
            basis = datetime.fromisoformat(c.snapshot_basis)
        except ValueError:
            basis = datetime.fromisoformat(s[-1][0])
    else:
        basis = datetime.fromisoformat(s[-1][0])
    window_start = basis.timestamp() - 900  # 15 min before basis, in seconds
    recent: list[tuple[str, float | None]] = []
    for ts, win_odds in s:
        try:
            if datetime.fromisoformat(ts).timestamp() >= window_start:
                recent.append((ts, win_odds))
        except ValueError:
            continue
    if len(recent) < 2 or not recent[0][1] or recent[0][1] <= 0:
        return None
    if not recent[-1][1]:
        return None
    return (recent[-1][1] - recent[0][1]) / recent[0][1]


# ─── Category 15: Track dynamics ─────────────────────────────────────────────
def h167_today_bias(c):
    """Today's front-runner residual from track_bias_daily if precomputed; falls
    back to live computation from earlier races on the card (point-in-time)."""
    if c.conn is None:
        return None
    r = c.conn.execute(
        "SELECT front_runner_win_rate_residual FROM track_bias_daily "
        "WHERE date = ? AND course = ?",
        (c.race["date"], c.race["course"]),
    ).fetchone()
    if r and r[0] is not None:
        return float(r[0])
    # PIT live fallback: earlier races on today's card only
    rows = c.conn.execute(
        "SELECT running_style FROM results r JOIN races ra ON r.race_id = ra.id "
        "WHERE ra.date = ? AND ra.course = ? AND r.position = 1 AND ra.race_no < ?",
        (c.race["date"], c.race["course"], c.race["race_no"])
    ).fetchall()
    if not rows:
        return None
    leaders = sum(1 for r in rows if (r[0] or "").startswith("1"))
    return leaders / len(rows)
def h169_rail_dup(c): return h058_rail(c)
def h170_watering(c):
    if c.conn is None: return None
    r = c.conn.execute(
        "SELECT watering_cm FROM rail_position WHERE date = ? AND course = ?",
        (c.race["date"], c.race["course"])
    ).fetchone()
    return r[0] if r else None
def h171_grass_length(c):
    if c.conn is None: return None
    r = c.conn.execute(
        "SELECT grass_height_cm FROM rail_position WHERE date = ? AND course = ?",
        (c.race["date"], c.race["course"])
    ).fetchone()
    return r[0] if r else None
def h172_inner_resid(c):
    """Today's inner-draw residual from track_bias_daily."""
    if c.conn is None:
        return None
    r = c.conn.execute(
        "SELECT inside_win_rate_residual FROM track_bias_daily WHERE date = ? AND course = ?",
        (c.race["date"], c.race["course"]),
    ).fetchone()
    return float(r[0]) if r and r[0] is not None else None
def h173_wind(c):
    if c.conn is None: return None
    r = c.conn.execute(
        "SELECT wind_speed_kmh FROM weather_observations WHERE date = ? AND course = ? AND race_no = ?",
        (c.race["date"], c.race["course"], c.race["race_no"])
    ).fetchone()
    return r[0] if r else None
def h174_closer_boost(c):
    bias = h167_today_bias(c)
    if bias is None: return None
    return 1.0 - bias


# ─── Speed-figure features (H175 / H176 / H177) ───────────────────────────
# Beyer-style: for each (distance, course) bucket, compute the historical
# par time (median across all strictly-prior races), then a horse's "speed
# figure" is (par − their_actual). Positive = faster than par; bigger is
# better. The par-time lookup is cached per (snapshot_basis, bucket) on
# the connection object so 177-feature batch compute stays O(N).

# race_history.finishtime is stored as "M.SS.cs" (e.g. "1.10.02" = 70.02s);
# race_history.venue is the Chinese tag (e.g. '沙田草地"C+3"'), not the
# 2-letter course code we use elsewhere. Both need parsing.
_FINISHTIME_MSC = re.compile(r"^\s*(\d+)\.(\d+)\.(\d+)\s*$")


def _parse_finish_time(v) -> float | None:
    """Convert 'M.SS.cs' / 'SS.cs' / float to seconds."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        try:
            f = float(v)
            return f if f > 0 else None
        except (TypeError, ValueError):
            return None
    s = str(v).strip()
    if not s:
        return None
    m = _FINISHTIME_MSC.match(s)
    if m:
        mins, secs, cs = m.groups()
        return int(mins) * 60 + int(secs) + int(cs) / 100.0
    try:
        f = float(s)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _venue_code(s) -> str | None:
    """Map race_history.venue (Chinese) to the 2-letter course code."""
    if not s:
        return None
    s = str(s)
    if "沙田" in s:
        return "ST"
    if "跑馬地" in s:
        return "HV"
    if "從化" in s:
        return "CH"
    # Already a code?
    code = s.strip().upper()
    if code in ("ST", "HV", "CH"):
        return code
    return None


_PAR_TIME_CACHE: dict = {}


def _par_time(c, distance: int, course: str) -> float | None:
    if c.conn is None or distance is None or not course:
        return None
    # sqlite3.Connection forbids setattr, so use a module-level cache keyed
    # by (conn id, snapshot_basis, course, distance). Conn id is stable
    # within one process.
    key = (id(c.conn), c.snapshot_basis, course, int(distance))
    if key in _PAR_TIME_CACHE:
        return _PAR_TIME_CACHE[key]
    # results.finish_time is a mix of REAL seconds (e.g. 69.08) and string
    # "M:SS.cs" (e.g. "1:09.80"); pull all and parse in Python.
    rows = c.conn.execute(
        "SELECT r.finish_time FROM results r JOIN races ra ON ra.id = r.race_id "
        "WHERE ra.distance = ? AND ra.course = ? "
        "  AND ra.date < substr(?, 1, 10) AND r.finish_time IS NOT NULL",
        (int(distance), course, c.snapshot_basis),
    ).fetchall()
    secs: list[float] = []
    for (raw,) in rows:
        v = _parse_results_time(raw)
        if v is not None:
            secs.append(v)
    par = (sum(secs) / len(secs)) if secs else None
    _PAR_TIME_CACHE[key] = par
    return par


def _parse_results_time(v) -> float | None:
    """Parse results.finish_time: numeric seconds, 'M:SS.cs', or '---'."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        try:
            f = float(v)
            return f if f > 0 else None
        except (TypeError, ValueError):
            return None
    s = str(v).strip()
    if not s or s in ("---", "--"):
        return None
    # M:SS.cs format
    m = re.match(r"^(\d+):(\d+)\.(\d+)$", s)
    if m:
        mins, secs, cs = m.groups()
        return int(mins) * 60 + int(secs) + int(cs) / 100.0
    try:
        f = float(s)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _speed_figures_for_horse(c) -> list[float]:
    """Return per-historical-run (par − actual) values for this horse."""
    out: list[float] = []
    for h in c.history or []:
        d = h.get("distance")
        course = _venue_code(h.get("venue") or h.get("course"))
        ftime = _parse_finish_time(h.get("finish_time"))
        if d is None or not course or ftime is None:
            continue
        par = _par_time(c, d, course)
        if par is None:
            continue
        out.append(par - ftime)
    return out


def h175_speed_figure_mean(c):
    sfs = _speed_figures_for_horse(c)
    return sum(sfs) / len(sfs) if sfs else None


def h176_speed_figure_best(c):
    sfs = _speed_figures_for_horse(c)
    return max(sfs) if sfs else None


def h177_speed_figure_last(c):
    # `history` is sorted ascending by date in the pipeline loader; take last.
    sfs = _speed_figures_for_horse(c)
    return sfs[-1] if sfs else None


# ─── Pedigree features (H178/H179) ────────────────────────────────────
# horse_pedigree stores sire/dam per brand. Compute the sire's win rate
# over the strictly-prior-results training window for any horse this
# sire produced. Cached per (conn_id, snapshot_basis, sire).
_SIRE_WR_CACHE: dict = {}
_SIRE_DIST_WR_CACHE: dict = {}


def _sire_for_brand(c, brand: str) -> str | None:
    if c.conn is None:
        return None
    r = c.conn.execute(
        "SELECT sire FROM horse_pedigree WHERE brand = ?", (brand,)
    ).fetchone()
    return r[0] if r and r[0] else None


def h178_sire_winrate(c):
    sire = _sire_for_brand(c, c.entry["brand"])
    if sire is None:
        return None
    key = (id(c.conn), c.snapshot_basis, sire)
    if key in _SIRE_WR_CACHE:
        return _SIRE_WR_CACHE[key]
    # Sire's win rate = (wins / runs) across all results before snapshot_basis
    # for horses whose sire matches.
    r = c.conn.execute(
        """
        SELECT
            SUM(CASE WHEN CAST(rs.position AS INT) = 1 THEN 1 ELSE 0 END) AS wins,
            COUNT(*) AS runs
        FROM results rs
        JOIN races ra ON ra.id = rs.race_id
        JOIN horse_pedigree hp ON hp.brand = rs.brand
        WHERE hp.sire = ? AND ra.date < substr(?, 1, 10)
        """,
        (sire, c.snapshot_basis),
    ).fetchone()
    wr = (r[0] / r[1]) if r and r[1] and r[1] >= 5 else None
    _SIRE_WR_CACHE[key] = wr
    return wr


def h179_sire_dist_winrate(c):
    sire = _sire_for_brand(c, c.entry["brand"])
    dist = c.race.get("distance")
    if sire is None or dist is None:
        return None
    # Distance bucket: 1000-1200 / 1300-1600 / 1700-2000 / 2100+
    if dist <= 1200:
        bucket = "sprint"
    elif dist <= 1600:
        bucket = "mile"
    elif dist <= 2000:
        bucket = "intermediate"
    else:
        bucket = "long"
    key = (id(c.conn), c.snapshot_basis, sire, bucket)
    if key in _SIRE_DIST_WR_CACHE:
        return _SIRE_DIST_WR_CACHE[key]
    ranges = {"sprint": (800, 1200), "mile": (1300, 1600),
              "intermediate": (1700, 2000), "long": (2100, 3000)}
    lo, hi = ranges[bucket]
    r = c.conn.execute(
        """
        SELECT
            SUM(CASE WHEN CAST(rs.position AS INT) = 1 THEN 1 ELSE 0 END),
            COUNT(*)
        FROM results rs
        JOIN races ra ON ra.id = rs.race_id
        JOIN horse_pedigree hp ON hp.brand = rs.brand
        WHERE hp.sire = ? AND ra.distance BETWEEN ? AND ?
          AND ra.date < substr(?, 1, 10)
        """,
        (sire, lo, hi, c.snapshot_basis),
    ).fetchone()
    wr = (r[0] / r[1]) if r and r[1] and r[1] >= 3 else None
    _SIRE_DIST_WR_CACHE[key] = wr
    return wr


# ─── helpers ─────────────────────────────────────────────────────────────────
def _g(d: dict[str, Any] | None, key: str) -> float | None:
    if not d: return None
    v = d.get(key)
    if v is None: return None
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except Exception:
        return None

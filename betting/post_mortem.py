"""Bet post-mortem (root-cause-analysis) tagger.

For every placed bet — won or lost — derive a set of structured tags that
describe WHY the outcome happened, then store them in `bet_post_mortem`
+ `bet_post_mortem_tags`. Tags reference a rich lookup table (`bet_tags`)
that holds zh + en labels and descriptions, so the SPA can render coloured
chips and `betting.eval_reason` can fold the tag set into the DeepSeek
commentary payload.

Inputs:
  * `bet_ledger`         — the bets we placed and their outcome.
  * `results`            — finish position, lbw, running positions,
                           jockey, draw, odds, finish time.
  * `running_comments`   — HKJC's per-horse incident narrative.
  * `sectionals`         — race total time + per-call splits.
  * `feature_values`     — for z-score driver alignment.

Trigger:
  Called from /api/races/{date} on first read of a settled race. Idempotent:
  rows are keyed by `bet_id`, so re-runs do nothing unless `--force`.

Usage:
    from betting.post_mortem import seed_tags, tag_race
    seed_tags(conn)               # idempotent
    tag_race(conn, race_id=123)   # all placed bets in race 123
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

# ─── Tag catalog ─────────────────────────────────────────────────────────
# Categories:
#   trip      — sourced from running-position trajectory (沿途走位)
#   result    — sourced from finish position + lengths-behind (頭馬距離)
#   pace      — sourced from sectional splits (段速)
#   incident  — sourced from HKJC running-comments keyword match
#   model     — sourced from model driver z-scores
#   context   — race-level: field size, draw, distance
#
# Severity is how the tag affects the bet outcome:
#   positive  — tag explains why the bet WON / was a sound pick
#   neutral   — descriptive only (no causal direction)
#   negative  — tag explains why the bet LOST / pick failed
TAG_CATALOG: dict[str, dict[str, str]] = {
    # ─── trip ────────────────────────────────────────────────────────
    "led_throughout": {
        "category": "trip", "severity": "positive",
        "label_zh": "前領全程", "label_en": "Led throughout",
        "description_zh": "出閘領先,沿途均在前列,直路再下一城。",
        "description_en": "Took the lead from the gate and held it to the line.",
    },
    "led_then_faded": {
        "category": "trip", "severity": "negative",
        "label_zh": "前領後退", "label_en": "Led then faded",
        "description_zh": "前段帶頭但末段乏力,被後上馬追過。",
        "description_en": "Led early but had no closing kick; passed in the straight.",
    },
    "closed_strong": {
        "category": "trip", "severity": "positive",
        "label_zh": "後上追前", "label_en": "Closed strong",
        "description_zh": "由後段或中後段強勁衝刺,殺入前列。",
        "description_en": "Strong late closing kick from off the pace.",
    },
    "no_closing_kick": {
        "category": "trip", "severity": "negative",
        "label_zh": "後上乏力", "label_en": "No closing kick",
        "description_zh": "後段位置,直路毫無衝刺,未能追前。",
        "description_en": "Sat off the pace but produced no closing burst.",
    },
    "held_position": {
        "category": "trip", "severity": "neutral",
        "label_zh": "保持位置", "label_en": "Held position",
        "description_zh": "全程位置變化不大,沒有顯著上揚或下挫。",
        "description_en": "Position barely shifted across the calls.",
    },
    "lost_ground_late": {
        "category": "trip", "severity": "negative",
        "label_zh": "後段退步", "label_en": "Lost ground late",
        "description_zh": "後段位置不斷向後跌,直路再被追過。",
        "description_en": "Position dropped through the back half of the race.",
    },

    # ─── result ──────────────────────────────────────────────────────
    "narrow_loss": {
        "category": "result", "severity": "negative",
        "label_zh": "僅敗一線", "label_en": "Narrow loss",
        "description_zh": "未能勝出但差距小於一個馬位,僅一線之差。",
        "description_en": "Beaten by less than one length — a heartbreaker.",
    },
    "near_miss_placed": {
        "category": "result", "severity": "neutral",
        "label_zh": "緊接入位", "label_en": "Just outside winner",
        "description_zh": "完成 2-3 名,落注 WIN 池而未中。",
        "description_en": "Finished 2nd/3rd — placed but missed the win pool.",
    },
    "well_beaten": {
        "category": "result", "severity": "negative",
        "label_zh": "大幅落敗", "label_en": "Well beaten",
        "description_zh": "距冠軍超過五個馬位,表現遠遜預期。",
        "description_en": "Beaten by more than 5 lengths — clearly outclassed today.",
    },
    "clean_win": {
        "category": "result", "severity": "positive",
        "label_zh": "輕鬆掄元", "label_en": "Clean win",
        "description_zh": "勝出且以一個馬位以上的優勢,實力勝出。",
        "description_en": "Won by more than 1 length — convincing.",
    },
    "fav_flopped": {
        "category": "result", "severity": "negative",
        "label_zh": "熱門失準", "label_en": "Favourite flopped",
        "description_zh": "賠率最熱的熱門馬未能入位,意外失準。",
        "description_en": "Heavy favourite finished off the board.",
    },
    "longshot_failed": {
        "category": "result", "severity": "neutral",
        "label_zh": "冷門無功", "label_en": "Longshot failed",
        "description_zh": "高賠率冷馬,本來就低概率,結果一般。",
        "description_en": "High-odds shot — outcome consistent with the price.",
    },

    # ─── pace ────────────────────────────────────────────────────────
    "fast_early_pace": {
        "category": "pace", "severity": "neutral",
        "label_zh": "前段過快", "label_en": "Hot early pace",
        "description_zh": "前段步速偏快,有利後上馬。",
        "description_en": "Fast early sectionals — closers favoured.",
    },
    "slow_early_pace": {
        "category": "pace", "severity": "neutral",
        "label_zh": "前段過慢", "label_en": "Slow early pace",
        "description_zh": "前段步速偏慢,前領馬具優勢。",
        "description_en": "Slow early sectionals — pace-setters favoured.",
    },

    # ─── incident (from HKJC corunning) ──────────────────────────────
    "awkward_start": {
        "category": "incident", "severity": "negative",
        "label_zh": "出閘不利", "label_en": "Awkward start",
        "description_zh": "出閘時起步緩慢或受阻,失去有利位置。",
        "description_en": "Stumbled / slow to begin, losing early position.",
    },
    "checked_in_running": {
        "category": "incident", "severity": "negative",
        "label_zh": "賽事中受阻", "label_en": "Checked in running",
        "description_zh": "途中被別馬或圍欄迫使收慢,失去衝勁。",
        "description_en": "Forced to check / take up at some stage of the race.",
    },
    "wide_no_cover": {
        "category": "incident", "severity": "negative",
        "label_zh": "外疊無遮擋", "label_en": "Wide, no cover",
        "description_zh": "全程行外疊,無遮擋,做更多路程。",
        "description_en": "Raced wide without cover, expending extra energy.",
    },
    "hung_in_straight": {
        "category": "incident", "severity": "negative",
        "label_zh": "直路偏走", "label_en": "Hung in straight",
        "description_zh": "進入直路後偏離直線,失去最佳路線。",
        "description_en": "Hung in / out in the straight, off its true line.",
    },
    "rider_made_late_move": {
        "category": "incident", "severity": "positive",
        "label_zh": "騎師後上發力", "label_en": "Strong late ride",
        "description_zh": "騎師在末段發力催策成功。",
        "description_en": "Jockey produced a strong, well-timed late move.",
    },
    "saved_ground": {
        "category": "incident", "severity": "positive",
        "label_zh": "貼內賺位", "label_en": "Saved ground",
        "description_zh": "緊貼內欄行走,節省路程。",
        "description_en": "Hugged the rail to save ground on the turn.",
    },
    "roarer_noted": {
        "category": "incident", "severity": "negative",
        "label_zh": "鳴聲問題", "label_en": "Roarer noted",
        "description_zh": "賽後評述顯示有鳴聲問題,氣道受限影響後勁。",
        "description_en": "HKJC noted the horse as a roarer — wind issue limited finish.",
    },
    # ─── HKJC stewards-report-derived incident tags ──────────────────
    # All sourced from incident_reports.incident_tags, populated by
    # betting.incident_tags from the official Racing Incident Report.
    "vet_inspection": {
        "category": "incident", "severity": "negative",
        "label_zh": "賽後驗馬", "label_en": "Vet inspection",
        "description_zh": "賽後馬會獸醫對該馬作出檢查 — 可能曾出現體能或健康問題。",
        "description_en": "HKJC vets examined the horse post-race — possible fitness or health issue.",
    },
    "sent_for_sampling": {
        "category": "incident", "severity": "neutral",
        "label_zh": "賽後抽驗", "label_en": "Sent for sampling",
        "description_zh": "賽後被抽中作藥物檢測 — 通常為隨機,但見於成績異常的馬匹。",
        "description_en": "Sent for post-race drug sampling — usually random but flags exceptional runs.",
    },
    "raced_keenly": {
        "category": "incident", "severity": "negative",
        "label_zh": "扯耳", "label_en": "Raced keenly",
        "description_zh": "馬匹途中扯耳,自行加速,浪費體力。",
        "description_en": "Horse pulled hard against the rider's restraint, wasting energy.",
    },
    "ran_off": {
        "category": "incident", "severity": "negative",
        "label_zh": "偏離方向", "label_en": "Ran off",
        "description_zh": "馬匹途中偏離直線 — 失去佔位或多走路程。",
        "description_en": "Horse veered off line during the race, losing position or wide trip.",
    },
    "head_up": {
        "category": "incident", "severity": "negative",
        "label_zh": "抬頭抗韁", "label_en": "Got head up",
        "description_zh": "馬匹受勒馬時抬頭抗韁 — 操控困難影響走位。",
        "description_en": "Horse raised its head against rein pressure — control issue cost positioning.",
    },
    "blood_in_mouth": {
        "category": "incident", "severity": "negative",
        "label_zh": "口部出血", "label_en": "Blood in mouth",
        "description_zh": "賽前/後發現馬匹口部有血跡 — 醫學異常。",
        "description_en": "Blood noted in horse's mouth — medical abnormality.",
    },
    "bled": {
        "category": "incident", "severity": "negative",
        "label_zh": "鼻腔出血", "label_en": "Bled (epistaxis)",
        "description_zh": "馬匹比賽中鼻腔出血 — 嚴重健康異常,後續通常停賽休養。",
        "description_en": "Horse bled from nostril during the race — serious medical issue.",
    },
    "withdrew": {
        "category": "incident", "severity": "negative",
        "label_zh": "賽事中途退賽", "label_en": "Withdrew during race",
        "description_zh": "馬匹比賽中途被退出 — 受傷或無法繼續。",
        "description_en": "Horse pulled up / withdrew during the race — injury or unable to continue.",
    },

    # ─── model ───────────────────────────────────────────────────────
    "top_drivers_misfired": {
        "category": "model", "severity": "negative",
        "label_zh": "頂級因素失靈", "label_en": "Top drivers misfired",
        "description_zh": "模型對該馬最看好的因素今次未能轉化成佳績。",
        "description_en": "The features that ranked this horse highest didn't translate today.",
    },
    "drivers_confirmed": {
        "category": "model", "severity": "positive",
        "label_zh": "因素全面驗證", "label_en": "Drivers confirmed",
        "description_zh": "模型上揚因素在賽果中得到全面印證。",
        "description_en": "Model's positive drivers were validated by the result.",
    },
    "against_market": {
        "category": "model", "severity": "neutral",
        "label_zh": "逆市押注", "label_en": "Against the market",
        "description_zh": "模型與賠率市場意見相左,本身就是高方差押注。",
        "description_en": "Model disagreed with the market — inherently high-variance.",
    },

    # ─── context ─────────────────────────────────────────────────────
    "outside_draw": {
        "category": "context", "severity": "negative",
        "label_zh": "外閘不利", "label_en": "Outside draw",
        "description_zh": "閘位偏外,於沙田 / 跑馬地路線需多走路程。",
        "description_en": "Drew wide — needed to consume extra ground.",
    },
    "big_field_traffic": {
        "category": "context", "severity": "negative",
        "label_zh": "大場交通混雜", "label_en": "Big field traffic",
        "description_zh": "場上馬匹眾多,交通擠迫,難以找到通道。",
        "description_en": "Large field — traffic made it hard to find a clear run.",
    },
    "short_distance_specialist_misfit": {
        "category": "context", "severity": "neutral",
        "label_zh": "距離不適", "label_en": "Distance mismatch",
        "description_zh": "今次距離與過往佳績距離有差距。",
        "description_en": "Today's distance doesn't match this horse's best form.",
    },
}


def seed_tags(conn: sqlite3.Connection) -> int:
    """Insert / update every TAG_CATALOG row. Returns rows touched.
    Idempotent — safe to call on every API startup."""
    n = 0
    for code, t in TAG_CATALOG.items():
        conn.execute(
            "INSERT OR REPLACE INTO bet_tags "
            "(code, category, severity, label_zh, label_en, description_zh, description_en) "
            "VALUES (?,?,?,?,?,?,?)",
            (code, t["category"], t["severity"], t["label_zh"], t["label_en"],
             t["description_zh"], t["description_en"]),
        )
        n += 1
    conn.commit()
    return n


# ─── parsing helpers ─────────────────────────────────────────────────────
def _parse_lbw(raw) -> float | None:
    if raw is None: return None
    s = str(raw).strip()
    if not s or s in ('---', '--', 'WIN'): return None
    word = {'鼻位': 0.05, '短鼻位': 0.03, '短馬頭位': 0.10, '馬頭位': 0.20,
            '頸位': 0.30, '短頸位': 0.20, '半個馬位': 0.50}
    for k, v in word.items():
        if k in s: return v
    m = re.match(r'^([\d.]+)(?:-(\d+)/(\d+))?$', s)
    if m:
        whole = float(m.group(1))
        if m.group(2):
            whole += int(m.group(2)) / int(m.group(3))
        return whole
    try: return float(s)
    except (TypeError, ValueError): return None


def _running_segments(raw) -> list[int]:
    if not raw: return []
    out: list[int] = []
    for token in str(raw).split():
        try: out.append(int(token))
        except ValueError: continue
    return out


# ─── tag derivation ──────────────────────────────────────────────────────
# Keyword → tag map for HKJC's English running comments.
_INCIDENT_PATTERNS = [
    (re.compile(r"begin slow|jumped poorly|slow to|missed the (?:start|kick)|"
                r"\bawkward(?:ly)? at the start|stumbl", re.I), "awkward_start"),
    (re.compile(r"check(?:ed)?(?: in running)?|forced to take up|had to be eased",
                re.I), "checked_in_running"),
    (re.compile(r"(?:raced |sat )(?:\d-)?wide|no cover|three[- ]wide|four[- ]wide",
                re.I), "wide_no_cover"),
    (re.compile(r"hung (?:in|out)|drifted (?:in|out) in the straight", re.I),
     "hung_in_straight"),
    (re.compile(r"strong(?:ly)? (?:to (?:the )?line|finish|finishing)|"
                r"powerful(?:ly)? to the line|exploded|surged", re.I),
     "rider_made_late_move"),
    (re.compile(r"saved ground|hugged the rail|inside throughout", re.I),
     "saved_ground"),
    (re.compile(r"\broarer\b", re.I), "roarer_noted"),
]

# Same map for the Chinese page.
_INCIDENT_PATTERNS_ZH = [
    (re.compile(r"出閘(?:慢|不利)|起步緩|起步較慢|失閘"), "awkward_start"),
    (re.compile(r"受阻|被.{0,4}阻|收慢|失去衝勁"), "checked_in_running"),
    (re.compile(r"無遮擋|外疊|走第[三四五]疊"), "wide_no_cover"),
    (re.compile(r"外閃|內閃|偏走|偏離"), "hung_in_straight"),
    (re.compile(r"末段(?:衝刺強勁|爆發|發力|急速)"), "rider_made_late_move"),
    (re.compile(r"貼內欄|走內欄|內欄賺位"), "saved_ground"),
    (re.compile(r"鳴聲|呼吸"), "roarer_noted"),
]


def _trip_tag(running: list[int], final_pos: int | None) -> str | None:
    """Classify the running-position trajectory."""
    if not running:
        return None
    first = running[0]
    if final_pos is None:
        final_pos = running[-1]
    if first <= 3 and final_pos <= 3:
        return "led_throughout"
    if first <= 3 and final_pos > 5:
        return "led_then_faded"
    if first > 7 and final_pos <= 4:
        return "closed_strong"
    if first > 7 and final_pos > 7:
        return "no_closing_kick"
    if first > final_pos + 2:
        return "closed_strong"
    if first < final_pos - 2:
        return "lost_ground_late"
    return "held_position"


def _result_tag(position: int | None, lbw: float | None, odds: float | None,
                pool: str, won: bool) -> str | None:
    if position is None:
        return None
    if won:
        if pool.upper() == "WIN" and lbw is not None and lbw >= 1.0:
            return "clean_win"
        return None
    # Lost cases
    if position == 1:
        return None  # placed bet on a winning horse but pool lost: covered upstream
    if lbw is not None and lbw < 1.0 and position == 2:
        return "narrow_loss"
    if position in (2, 3):
        return "near_miss_placed"
    if lbw is not None and lbw > 5.0:
        if odds is not None and odds <= 3.5:
            return "fav_flopped"
        if odds is not None and odds >= 15.0:
            return "longshot_failed"
        return "well_beaten"
    if odds is not None and odds <= 3.5:
        return "fav_flopped"
    if odds is not None and odds >= 15.0:
        return "longshot_failed"
    return None


def _pace_tag(sectionals: tuple | None) -> str | None:
    """sectionals row = (total_time, splits, early_pace, late_pace, pace_score).

    `early_pace` and `late_pace` are normalised ratios (typically 0.85-1.55),
    not raw seconds. Distribution: avg early ≈ 1.15, avg late ≈ 0.96, mean
    diff ≈ -0.18 — i.e. on average horses finish slightly faster than they
    started. The earlier 0.8 threshold (assumed seconds) never fired across
    2363 sectionals rows. Calibrated against the actual data, ±0.35 diff
    from the mean captures the top ~12% extreme races in each direction."""
    if not sectionals:
        return None
    _tt, _splits, early, late, _pscore = sectionals
    if early is None or late is None:
        return None
    try:
        early, late = float(early), float(late)
    except (TypeError, ValueError):
        return None
    diff = late - early                                    # mean ≈ -0.18
    if diff < -0.35:                                       # late << early → blistering early speed
        return "fast_early_pace"
    if diff > 0.05:                                        # late > early → unusually strong finish
        return "slow_early_pace"
    return None


def _model_tags(model_drivers: dict, position: int | None, won: bool) -> list[str]:
    """Compare model-favoured drivers vs outcome."""
    tags: list[str] = []
    top = (model_drivers or {}).get("top") or []
    if not top:
        return tags
    if won:
        tags.append("drivers_confirmed")
    elif position is not None and position > 5:
        tags.append("top_drivers_misfired")
    return tags


def _incident_tags(comment_en: str | None, comment_zh: str | None) -> list[tuple[str, str]]:
    """Match HKJC running comment against incident keywords. Returns
    list of (tag_code, evidence_snippet)."""
    found: dict[str, str] = {}
    if comment_en:
        for rx, code in _INCIDENT_PATTERNS:
            m = rx.search(comment_en)
            if m and code not in found:
                found[code] = m.group(0)
    if comment_zh:
        for rx, code in _INCIDENT_PATTERNS_ZH:
            m = rx.search(comment_zh)
            if m and code not in found:
                found[code] = m.group(0)
    return list(found.items())


# Map HKJC official Racing-Incident-Report tag codes (from
# betting/incident_tags.py) onto our bet_tags catalog. Tags not in this map
# are dropped — they're either irrelevant for bet outcome or covered by a
# trip/result derivation already.
_HKJC_TO_BET_TAG: dict[str, str] = {
    "vet_inspection":     "vet_inspection",
    "sent_for_sampling":  "sent_for_sampling",
    "bumped":             "checked_in_running",
    "steadied":           "checked_in_running",
    "crowded":            "checked_in_running",
    "hampered":           "checked_in_running",
    "ran_off":            "ran_off",
    "raced_keenly":       "raced_keenly",
    "raced_wide":         "wide_no_cover",
    "head_up":            "head_up",
    "slow_to_begin":      "awkward_start",
    "roarer":             "roarer_noted",
    "blood_in_mouth":     "blood_in_mouth",
    "bled":               "bled",
    "withdrew":           "withdrew",
}


def _incident_report_tags(conn: sqlite3.Connection,
                          race_id: int, brand: str) -> list[tuple[str, str]]:
    """Pull HKJC Racing Incident Report tags for this (race, horse) and map
    them onto our bet_tags codes. Returns list of (tag_code, evidence) where
    evidence is the HKJC tag name + an excerpt of the original incident text.

    This is the richest data source — covers 20K rows across 2023+, all
    pre-classified by `betting.incident_tags`. Strictly richer than the
    running_comments keyword match because stewards' notes record WHY
    things happened (medical, traffic, gear, sampling), not just what."""
    row = conn.execute(
        "SELECT incident_tags, incident FROM incident_reports "
        "WHERE race_id = ? AND brand = ? LIMIT 1",
        (race_id, brand),
    ).fetchone()
    if not row or not row[0]:
        return []
    tags_csv, text = row[0], row[1] or ""
    excerpt = (text[:80] + "…") if len(text) > 80 else text
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for hkjc_tag in tags_csv.split(","):
        hkjc_tag = hkjc_tag.strip()
        mapped = _HKJC_TO_BET_TAG.get(hkjc_tag)
        if not mapped or mapped in seen:
            continue
        seen.add(mapped)
        out.append((mapped, f"{hkjc_tag}: {excerpt}"))
    return out


def _compute_drivers(conn: sqlite3.Connection, race_id: int, brand: str) -> dict:
    """z-score drivers (top/bottom 3) for one horse vs the field."""
    rows = conn.execute(
        "SELECT brand, feature_id, value FROM feature_values "
        "WHERE race_id = ? AND value IS NOT NULL", (race_id,),
    ).fetchall()
    if not rows:
        return {"top": [], "bottom": []}
    by_feat: dict[str, dict[str, float]] = {}
    for b, f, v in rows:
        by_feat.setdefault(f, {})[b] = float(v)
    drivers = []
    for f, vals in by_feat.items():
        if brand not in vals or len(vals) < 3:
            continue
        xs = list(vals.values())
        mean = sum(xs) / len(xs)
        var = sum((x - mean) ** 2 for x in xs) / len(xs)
        if var <= 0:
            continue
        std = var ** 0.5
        z = (vals[brand] - mean) / std
        drivers.append((z, f, vals[brand], mean))
    drivers.sort(key=lambda x: -x[0])
    return {
        "top": [{"feature_id": d[1], "z": round(d[0], 2)}
                for d in drivers[:3] if d[0] > 0],
        "bottom": [{"feature_id": d[1], "z": round(d[0], 2)}
                   for d in drivers[-3:] if d[0] < 0][::-1],
    }


def _build_summary(tags: list[tuple[str, str, float]], lang: str,
                   won: bool) -> str:
    """Short one-line summary of the tag set, used as a subtitle in the SPA."""
    if not tags:
        return ("命中 ✓" if won else "失中") if lang == "zh" \
               else ("Hit" if won else "Miss")
    pieces: list[str] = []
    seen: set[str] = set()
    for code, _ev, _w in tags:
        if code in seen:
            continue
        meta = TAG_CATALOG.get(code)
        if not meta:
            continue
        pieces.append(meta[f"label_{lang}" if lang in ("zh", "en") else "label_en"])
        seen.add(code)
        if len(pieces) >= 3:
            break
    head = ("命中 → " if won else "失中 → ") if lang == "zh" \
           else ("Won → " if won else "Lost → ")
    return head + "、".join(pieces) if lang == "zh" else head + ", ".join(pieces)


def tag_bet(conn: sqlite3.Connection, bet: tuple) -> tuple[list[tuple[str, str, float]], dict]:
    """Compute tags for one bet_ledger row.
    `bet` is (id, race_id, brand, pool, won, ...). Returns (tags, info)
    where tags is a list of (code, evidence, weight) and info packages the
    intermediate data (used for summary text). Exotic combos (brand string
    contains ',' or '>') tag each component horse individually, and the
    resulting tag list is the de-duplicated union (top weight wins on conflict)."""
    bet_id, race_id, brand, pool, won = bet[0], bet[1], bet[2], bet[3], bet[4]
    won_bool = (won == 1)

    # Exotic combos: split picks and recurse on each component.
    parts = [p.strip() for p in re.split(r"[,>]", brand) if p.strip()]
    if len(parts) > 1:
        merged: dict[str, tuple[str, float]] = {}
        first_info: dict = {"won": won_bool}
        for p in parts:
            sub_bet = (bet_id, race_id, p, pool, won)
            sub_tags, sub_info = tag_bet(conn, sub_bet)
            if not first_info.get("position") and sub_info.get("position"):
                first_info = sub_info
            for code, ev, w in sub_tags:
                cur = merged.get(code)
                if not cur or w > cur[1]:
                    merged[code] = (f"{p}: {ev}", w)
        return [(c, ev, w) for c, (ev, w) in merged.items()], first_info

    rs = conn.execute(
        "SELECT position, lbw, odds, running_style, draw, jockey "
        "FROM results WHERE race_id = ? AND brand = ?", (race_id, brand),
    ).fetchone()
    if not rs:
        return [], {"won": won_bool}
    position, lbw_raw, odds, running_raw, draw, jockey = rs
    try: position_int = int(str(position).strip()) if position is not None else None
    except ValueError: position_int = None
    try: odds_f = float(odds) if odds is not None else None
    except (TypeError, ValueError): odds_f = None
    lbw = _parse_lbw(lbw_raw)
    running = _running_segments(running_raw)

    sect = conn.execute(
        "SELECT total_time, splits, early_pace, late_pace, pace_score "
        "FROM sectionals WHERE race_id = ? LIMIT 1", (race_id,),
    ).fetchone()

    rc_en = conn.execute(
        "SELECT comment FROM running_comments "
        "WHERE race_id = ? AND brand = ? AND lang = 'en'", (race_id, brand),
    ).fetchone()
    rc_zh = conn.execute(
        "SELECT comment FROM running_comments "
        "WHERE race_id = ? AND brand = ? AND lang = 'zh'", (race_id, brand),
    ).fetchone()
    comment_en = rc_en[0] if rc_en else None
    comment_zh = rc_zh[0] if rc_zh else None

    drivers = _compute_drivers(conn, race_id, brand)

    tags: list[tuple[str, str, float]] = []
    trip = _trip_tag(running, position_int)
    if trip:
        tags.append((trip, "→".join(str(x) for x in running) +
                     (f"→{position_int}" if position_int else ""), 1.5))
    res = _result_tag(position_int, lbw, odds_f, pool or "WIN", won_bool)
    if res:
        ev = f"pos={position_int}, lbw={lbw}, odds={odds_f}"
        tags.append((res, ev, 1.2))
    pace = _pace_tag(sect)
    if pace:
        tags.append((pace, f"early={sect[2]} late={sect[3]}", 0.8))
    for code, evidence in _incident_tags(comment_en, comment_zh):
        tags.append((code, evidence, 1.0))
    # HKJC Racing Incident Report — strictly higher-quality than the
    # corunning-comment keyword match above. Higher weight (1.3) because
    # these tags come from stewards' official notes, not a regex.
    seen_codes = {c for c, _, _ in tags}
    for code, evidence in _incident_report_tags(conn, race_id, brand):
        if code in seen_codes:
            continue
        tags.append((code, evidence, 1.3))
        seen_codes.add(code)
    for code in _model_tags(drivers, position_int, won_bool):
        ev = ", ".join(d["feature_id"] for d in (drivers.get("top") or [])[:2])
        tags.append((code, ev, 0.7))
    if draw is not None:
        try:
            d_int = int(draw)
            if d_int >= 10:
                tags.append(("outside_draw", f"draw={d_int}", 0.5))
        except (TypeError, ValueError):
            pass

    info = {
        "won": won_bool, "position": position_int, "lbw": lbw,
        "odds": odds_f, "running": running, "drivers": drivers,
        "comment_en": comment_en, "comment_zh": comment_zh,
    }
    return tags, info


def tag_race(conn: sqlite3.Connection, race_id: int, *, force: bool = False) -> int:
    """Tag every bet_ledger row for `race_id`. Returns number of post-mortems
    written. Idempotent: skips bets that already have a row unless force=True."""
    seed_tags(conn)
    bets = conn.execute(
        "SELECT id, race_id, brand, pool, won FROM bet_ledger "
        "WHERE race_id = ? AND won IN (0, 1)", (race_id,),
    ).fetchall()
    n = 0
    for bet in bets:
        bet_id = bet[0]
        if not force:
            have = conn.execute(
                "SELECT 1 FROM bet_post_mortem WHERE bet_id = ?", (bet_id,),
            ).fetchone()
            if have:
                continue
        tags, info = tag_bet(conn, bet)
        summary_zh = _build_summary(tags, "zh", info["won"])
        summary_en = _build_summary(tags, "en", info["won"])
        cur = conn.execute(
            "INSERT OR REPLACE INTO bet_post_mortem "
            "(bet_id, race_id, brand, outcome, summary_zh, summary_en) "
            "VALUES (?,?,?,?,?,?)",
            (bet_id, bet[1], bet[2], "won" if info["won"] else "lost",
             summary_zh, summary_en),
        )
        pm_id = cur.lastrowid
        # Clear any stale tags for this post-mortem (only relevant on force).
        conn.execute("DELETE FROM bet_post_mortem_tags WHERE post_mortem_id = ?",
                     (pm_id,))
        for code, evidence, weight in tags:
            conn.execute(
                "INSERT INTO bet_post_mortem_tags "
                "(post_mortem_id, tag_code, evidence, weight) VALUES (?,?,?,?)",
                (pm_id, code, evidence, weight),
            )
        n += 1
    conn.commit()
    return n


def backfill(conn: sqlite3.Connection, since: str | None = None,
             force: bool = False) -> dict:
    """Walk every settled bet (won ∈ {0,1}) in `bet_ledger` and ensure each
    has a post-mortem row + tags. Idempotent unless `force=True`.

    Returns {races_tagged, bets_tagged, skipped}."""
    seed_tags(conn)
    where = ["bl.won IN (0, 1)"]
    params: list = []
    if since:
        where.append("bl.race_date >= ?")
        params.append(since)
    race_rows = conn.execute(
        f"SELECT DISTINCT race_id FROM bet_ledger bl "
        f"WHERE {' AND '.join(where)} ORDER BY race_id",
        params,
    ).fetchall()
    races, bets, skipped = 0, 0, 0
    for (rid,) in race_rows:
        before = conn.execute(
            "SELECT COUNT(*) FROM bet_post_mortem WHERE race_id = ?", (rid,),
        ).fetchone()[0]
        n = tag_race(conn, rid, force=force)
        if n:
            races += 1
            bets += n
        else:
            skipped += 1
    return {"races_tagged": races, "bets_tagged": bets, "skipped": skipped}


def fetch_for_race(conn: sqlite3.Connection, race_id: int) -> list[dict]:
    """Return all post-mortems for `race_id` with their tags joined,
    structured for the SPA."""
    pms = conn.execute(
        "SELECT id, bet_id, brand, outcome, summary_zh, summary_en "
        "FROM bet_post_mortem WHERE race_id = ?", (race_id,),
    ).fetchall()
    out: list[dict] = []
    for pm_id, bet_id, brand, outcome, sz, se in pms:
        tags = conn.execute(
            "SELECT t.code, t.category, t.severity, t.label_zh, t.label_en, "
            "       t.description_zh, t.description_en, x.evidence, x.weight "
            "FROM bet_post_mortem_tags x "
            "JOIN bet_tags t ON t.code = x.tag_code "
            "WHERE x.post_mortem_id = ? ORDER BY x.weight DESC",
            (pm_id,),
        ).fetchall()
        out.append({
            "bet_id": bet_id, "brand": brand, "outcome": outcome,
            "summary_zh": sz, "summary_en": se,
            "tags": [{"code": r[0], "category": r[1], "severity": r[2],
                      "label_zh": r[3], "label_en": r[4],
                      "description_zh": r[5], "description_en": r[6],
                      "evidence": r[7], "weight": r[8]} for r in tags],
        })
    return out


# ─── CLI ─────────────────────────────────────────────────────────────────
def _main() -> int:
    import argparse, sys
    from pathlib import Path
    p = argparse.ArgumentParser(prog="post_mortem")
    p.add_argument("--backfill", action="store_true",
                   help="walk every settled bet and ensure each has a post-mortem")
    p.add_argument("--since", help="restrict backfill to race_date >= this (YYYY-MM-DD)")
    p.add_argument("--force", action="store_true",
                   help="re-tag races even if they already have post-mortem rows")
    p.add_argument("--race-id", type=int, help="tag one race only")
    ns = p.parse_args()
    db_path = Path(__file__).resolve().parent.parent / "data" / "racing.db"
    conn = sqlite3.connect(str(db_path), timeout=60)
    if ns.race_id:
        n = tag_race(conn, ns.race_id, force=ns.force)
        print(f"tagged {n} bets in race {ns.race_id}")
    elif ns.backfill:
        result = backfill(conn, since=ns.since, force=ns.force)
        print(f"backfill: {result}")
    else:
        print("specify --backfill or --race-id")
        return 2
    conn.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())

"""Generate 馬評-style commentary on why a horse won / lost / placed.

Two paths share the same structured-data extraction:

  1. Rule-based (always available):
     Builds a deterministic narrative in zh/en from race process data:
       - Running positions across calls (沿途走位)
       - Final position + lengths behind winner
       - Implied pace from sectional split + finish time
       - Recent form (last 3-6 results, position trend, rating trend)
       - Odds vs result (favorite that won / longshot that surprised)
       - Notable feature drivers from the model

  2. DeepSeek narrative (optional, on if DEEPSEEK_API_KEY env var set):
     Sends the structured payload to deepseek-chat and asks for a
     concise 馬評 commentary that synthesises the same facts.

Cached in `horse_eval_text(race_id, brand, lang)` so each horse's
narrative is computed once per language.

Usage:
    from betting.eval_reason import generate
    text, source = generate(conn, race_id=123, brand='K198', lang='zh')
"""

from __future__ import annotations
import json
import os
import re
import sqlite3
import urllib.request
from typing import Any


def _coerce_int(raw) -> int | None:
    if raw is None: return None
    try: return int(str(raw).strip())
    except (TypeError, ValueError): return None


def _parse_lbw(raw) -> float | None:
    """HKJC margin string parser. '4-3/4' -> 4.75, '1/2' -> 0.5,
    '鼻位' (nose) -> 0.05, 'WIN' / '---' -> None."""
    if raw is None: return None
    s = str(raw).strip()
    if not s or s in ('---', '--', 'WIN'): return None
    word_map = {'鼻位': 0.05, '短鼻位': 0.03, '短馬頭位': 0.10, '馬頭位': 0.20,
                '頸位': 0.30, '短頸位': 0.20, '半個馬位': 0.50}
    for k, v in word_map.items():
        if k in s: return v
    m = re.match(r'^([\d.]+)(?:-(\d+)/(\d+))?$', s)
    if m:
        whole = float(m.group(1))
        if m.group(2):
            whole += int(m.group(2)) / int(m.group(3))
        return whole
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _running_segments(running: str) -> list[int]:
    """'9 10 7' -> [9, 10, 7]. Returns [] if unparseable."""
    if not running: return []
    out: list[int] = []
    for token in str(running).split():
        try:
            out.append(int(token))
        except ValueError:
            continue
    return out


def _running_narrative(running: list[int], final_pos: int | None, lang: str) -> str:
    """Convert running-position sequence into a layman-readable phrase.
    e.g. [9, 10, 7] + finish 4 → '後追前 (9-10-7→4)'  / 'closed strong'."""
    if not running:
        return ''
    first = running[0]
    last = running[-1]
    if final_pos is None:
        final_pos = last
    cls = ''
    if first <= 3 and final_pos <= 3:
        cls = 'led throughout' if lang == 'en' else '前領全程'
    elif first <= 3 and final_pos > 5:
        cls = 'led then faded' if lang == 'en' else '前領後退'
    elif first > 7 and final_pos <= 4:
        cls = 'closed strong from rear' if lang == 'en' else '後追上前'
    elif first > 7 and final_pos > 7:
        cls = 'no closing kick' if lang == 'en' else '後上乏力'
    elif first > final_pos + 2:
        cls = 'gained ground late' if lang == 'en' else '後段追前'
    elif first < final_pos - 2:
        cls = 'lost ground late' if lang == 'en' else '後段退步'
    else:
        cls = 'held position' if lang == 'en' else '保持位置'
    arrow = '→'.join(str(x) for x in running)
    return f"{cls} ({arrow}{arrow and '→'}{final_pos})" if cls else ''


def _form_summary(history: list[dict], lang: str) -> str:
    """Last 3-6 history rows summarised: positions sequence + win/place rate."""
    if not history:
        return ''
    recent = history[-6:]
    poses: list[int] = []
    for h in recent:
        p = _coerce_int(h.get('position'))
        if p is not None and 1 <= p <= 14:
            poses.append(p)
    if not poses:
        return ''
    wins = sum(1 for p in poses if p == 1)
    placed = sum(1 for p in poses if 1 <= p <= 3)
    seq = '-'.join(str(p) for p in poses)
    if lang == 'zh':
        return f"近 {len(poses)} 仗成績 {seq}({wins} 冠 / {placed} 上名)"
    return f"last {len(poses)}: {seq} ({wins}W / {placed}P)"


def _odds_class(odds: float | None, position: int | None, lang: str) -> str:
    """Categorise the result by odds-vs-outcome.
       'expected win', 'shock win', 'fav flopped', etc."""
    if odds is None or position is None:
        return ''
    if position == 1:
        if odds <= 3:
            return 'expected — favourite delivered' if lang == 'en' else '熱門兌現'
        if odds <= 8:
            return 'mid-priced winner' if lang == 'en' else '中價勝出'
        return f'upset @ {odds:.1f}' if lang == 'en' else f'冷馬爆冷 ({odds:.1f} 倍)'
    if position <= 3:
        return f'placed ({position})' if lang == 'en' else f'入位({position})'
    if odds <= 3:
        return 'favourite flopped' if lang == 'en' else '熱門失準'
    if odds > 20:
        return 'longshot finished off the board' if lang == 'en' else '冷門位處包尾'
    return ''


def _drivers_compact(drivers: dict, lang: str) -> str:
    """One-line driver summary from /api/races/{date} feature_drivers shape."""
    if not drivers:
        return ''
    top = drivers.get('top') or []
    bot = drivers.get('bottom') or []
    def fmt(d):
        nm = d.get('name_zh' if lang == 'zh' else 'name_en') or d.get('feature_id')
        z = d.get('z') or 0
        score = max(-9, min(9, round(z * 3)))
        sign = '+' if score > 0 else ''
        return f"{nm}({sign}{score})"
    parts = []
    if top:
        parts.append(('上揚' if lang == 'zh' else 'tail') + ': ' + ', '.join(fmt(d) for d in top[:3]))
    if bot:
        parts.append(('下挫' if lang == 'zh' else 'head') + ': ' + ', '.join(fmt(d) for d in bot[:3]))
    return ' · '.join(parts)


def _compute_feature_drivers_for_horse(conn: sqlite3.Connection, race_id: int, brand: str) -> dict:
    """Lightweight z-score driver computation (no SHAP). Mirrors api._compute_feature_drivers
    but for a single horse — duplicate to avoid the import cycle."""
    rows = conn.execute(
        "SELECT brand, feature_id, value FROM feature_values "
        "WHERE race_id = ? AND value IS NOT NULL", (race_id,),
    ).fetchall()
    if not rows:
        return {"top": [], "bottom": []}
    by_feat: dict[str, dict[str, float]] = {}
    for b, f, v in rows:
        by_feat.setdefault(f, {})[b] = float(v)
    drivers: list[tuple[float, str, float, float]] = []
    for f, vals in by_feat.items():
        if brand not in vals or len(vals) < 3: continue
        xs = list(vals.values())
        mean = sum(xs) / len(xs)
        var = sum((x - mean) ** 2 for x in xs) / len(xs)
        if var <= 0: continue
        std = var ** 0.5
        z = (vals[brand] - mean) / std
        drivers.append((z, f, vals[brand], mean))
    drivers.sort(key=lambda x: -x[0])
    catalog = {f[0]: (f[1], f[2]) for f in conn.execute(
        "SELECT feature_id, name_zh, name_en FROM feature_catalog"
    ).fetchall()}
    def packed(d):
        z, fid, v, m = d
        name_zh, name_en = catalog.get(fid, (fid, fid))
        return {"feature_id": fid, "name_zh": name_zh, "name_en": name_en,
                "value": v, "field_mean": m, "z": round(z, 3)}
    top = [packed(d) for d in drivers[:3] if d[0] > 0]
    bot = [packed(d) for d in drivers[-3:] if d[0] < 0][::-1]
    return {"top": top, "bottom": bot}


def _build_structured(conn: sqlite3.Connection, race_id: int, brand: str) -> dict:
    """Pull every signal we'll use for narrative generation, into one dict."""
    rrow = conn.execute(
        "SELECT date, course, race_no, distance, class, going, race_name "
        "FROM races WHERE id = ?", (race_id,),
    ).fetchone()
    if not rrow:
        return {}
    date, course, race_no, distance, race_class, going, race_name = rrow

    rs = conn.execute(
        "SELECT horse_name, jockey, trainer, draw, act_wt, decl_wt, odds, "
        "       finish_time, lbw, running_style, position "
        "FROM results WHERE race_id = ? AND brand = ?", (race_id, brand),
    ).fetchone()
    if not rs:
        return {}
    hname, jockey, trainer, draw, actwt, declwt, odds, ftime, lbw, running, position = rs

    # Recent history (≤ 6 races)
    hist = conn.execute(
        "SELECT date, distance, going, class, running, finishtime, pla AS position "
        "FROM race_history WHERE brandno = ? AND date < ? "
        "ORDER BY date DESC LIMIT 6", (brand, date),
    ).fetchall()
    hist_dicts = [{"date": r[0], "distance": r[1], "going": r[2], "class": r[3],
                   "running": r[4], "finish_time": r[5], "position": r[6]} for r in hist]

    sect = conn.execute(
        "SELECT total_time, splits, early_pace, late_pace, pace_score "
        "FROM sectionals WHERE date = ? AND course = ? AND race_no = ? "
        "AND distance = ? LIMIT 1",
        (date, course, race_no, distance),
    ).fetchone()
    sect_data = None
    if sect:
        sect_data = {"total_time": sect[0], "splits": sect[1],
                     "early_pace": sect[2], "late_pace": sect[3], "pace_score": sect[4]}

    drivers = _compute_feature_drivers_for_horse(conn, race_id, brand)

    return {
        "race": {"date": date, "course": course, "race_no": race_no,
                 "distance": distance, "class": race_class, "going": going,
                 "race_name": race_name},
        "horse": {"brand": brand, "name": hname, "jockey": jockey,
                  "trainer": trainer, "draw": draw, "act_wt": actwt,
                  "decl_wt": declwt, "odds": odds, "finish_time": ftime,
                  "lbw": lbw, "running": running, "position": _coerce_int(position)},
        "history": hist_dicts,
        "sectionals": sect_data,
        "drivers": drivers,
    }


def _rule_narrative(data: dict, lang: str) -> str:
    """Build a deterministic 馬評-style commentary from `data`."""
    h = data.get("horse") or {}
    race = data.get("race") or {}
    position = h.get("position")
    odds = h.get("odds")
    try: odds = float(odds) if odds is not None else None
    except (TypeError, ValueError): odds = None
    running = _running_segments(h.get("running"))
    lbw = _parse_lbw(h.get("lbw"))

    sentences: list[str] = []

    if lang == 'zh':
        if position == 1:
            sentences.append(f"以 {odds or '—'} 倍勝出。" if odds else "勝出。")
        elif position is not None and position <= 3:
            margin = f"差 {lbw} 個馬位" if lbw else ""
            sentences.append(f"入位第 {position} 名{margin}。")
        elif position is not None:
            margin = f"距冠軍 {lbw} 個馬位" if lbw else ""
            sentences.append(f"完成第 {position} 名{margin}。")
        else:
            sentences.append("未完成 (退賽 / 跌倒 / 失場)。")

        narr = _running_narrative(running, position, 'zh')
        if narr:
            sentences.append(f"沿途走位 {narr}。")
        oc = _odds_class(odds, position, 'zh')
        if oc:
            sentences.append(oc + "。")
        f = _form_summary(data.get('history') or [], 'zh')
        if f:
            sentences.append(f + "。")
        d = _drivers_compact(data.get('drivers') or {}, 'zh')
        if d:
            sentences.append("模型因素 — " + d + "。")
        return ''.join(sentences)

    # English
    if position == 1:
        sentences.append(f"Won at {odds:.1f}." if odds else "Won.")
    elif position is not None and position <= 3:
        margin = f", {lbw}L behind winner" if lbw else ""
        sentences.append(f"Placed {position}{margin}.")
    elif position is not None:
        margin = f", {lbw}L behind winner" if lbw else ""
        sentences.append(f"Finished {position}{margin}.")
    else:
        sentences.append("DNF (scratched / fell / unplaced).")
    narr = _running_narrative(running, position, 'en')
    if narr:
        sentences.append(f"Trip: {narr}.")
    oc = _odds_class(odds, position, 'en')
    if oc:
        sentences.append(oc.capitalize() + ".")
    f = _form_summary(data.get('history') or [], 'en')
    if f:
        sentences.append(f.capitalize() + ".")
    d = _drivers_compact(data.get('drivers') or {}, 'en')
    if d:
        sentences.append("Model — " + d + ".")
    return ' '.join(sentences)


# ─── Optional DeepSeek path ────────────────────────────────────────────
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"


def _deepseek_call(payload: dict, lang: str) -> str | None:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return None
    if lang == 'zh':
        sys_prompt = (
            "你是專業香港賽馬評論員。請根據提供的賽事數據,以繁體中文馬評風格"
            "生成 2-3 句精簡分析,涵蓋走位、段速、過往表現、賠率與結果的關係。"
            "輸出純文字,不要 markdown。"
        )
        user_prompt = json.dumps(payload, ensure_ascii=False)
    else:
        sys_prompt = (
            "You are a professional horse-racing analyst. "
            "Respond ONLY in English (do NOT use Chinese). "
            "Generate a concise 2-3 sentence commentary in racing-form style, "
            "covering running positions, sectional pace, recent form, and odds "
            "vs outcome. Plain text, no markdown."
        )
        user_prompt = ("Race + horse data (JSON). Please write the commentary "
                       "in English only:\n\n" + json.dumps(payload, default=str))
    body = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3, "max_tokens": 300,
    }
    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            res = json.loads(r.read().decode("utf-8"))
        return res["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def generate(conn: sqlite3.Connection, race_id: int, brand: str,
             lang: str = "zh", force_refresh: bool = False) -> tuple[str, str]:
    """Return (text, source). source ∈ {'rule', 'deepseek', 'cache'}.

    Looks up the cache first. On miss, builds structured data, optionally
    calls DeepSeek, falls back to the rule-based narrator, then writes
    the cache row.
    """
    if not force_refresh:
        row = conn.execute(
            "SELECT text, source FROM horse_eval_text "
            "WHERE race_id = ? AND brand = ? AND lang = ?",
            (race_id, brand, lang),
        ).fetchone()
        if row:
            return row[0], "cache"

    data = _build_structured(conn, race_id, brand)
    if not data:
        return ("資料不足無法生成馬評。" if lang == 'zh'
                else "Insufficient data to generate commentary."), "rule"

    # DeepSeek path first if key is set; otherwise rule-based.
    text = _deepseek_call(data, lang)
    source = "deepseek" if text else "rule"
    if not text:
        text = _rule_narrative(data, lang)

    conn.execute(
        "INSERT OR REPLACE INTO horse_eval_text "
        "(race_id, brand, lang, source, text, structured_json) "
        "VALUES (?,?,?,?,?,?)",
        (race_id, brand, lang, source, text, json.dumps(data, ensure_ascii=False, default=str)),
    )
    conn.commit()
    return text, source

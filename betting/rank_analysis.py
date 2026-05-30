"""Deep pre-race per-horse ranking analysis.

Sibling of `betting.eval_reason` (which is POST-race 馬評). This module
explains, BEFORE the race, every significant factor driving the model's
ranking of a horse: the feature deviations vs the field (weighted by model
importance), the model's probability / edge / rank, recent form, barrier-trial
read, draw / track-bias context, and the model-vs-market picture.

Two paths, same structured extraction (mirrors eval_reason):
  1. Rule-based — deterministic, always available: groups the strongest
     feature deviations by category with +/- direction and value-vs-field.
  2. DeepSeek (deepseek-reasoner) — a detailed, theme-grouped narrative when
     DEEPSEEK_API_KEY is set.

Strictly point-in-time: only pre-race inputs are used; the result/finishing
position is never read, so the same text is valid before the race is run.

Cached in `horse_rank_analysis(race_id, brand, lang)`.

    from betting.rank_analysis import generate
    text, source = generate(conn, race_id=123, brand='K198', lang='zh')
"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

_GAIN_CACHE: dict | None = None
_CAT_CACHE: dict | None = None

_CAT_LABELS = {
    1: ("馬匹檔案", "Horse profile"), 2: ("勝率與報酬", "Win-rate & returns"),
    3: ("適應性", "Adaptability"), 4: ("練馬師狀態", "Trainer form"), 5: ("閘號", "Draw"),
    6: ("負磅", "Weight"), 7: ("賽事背景", "Race context"), 8: ("近期狀態", "Recent form"),
    9: ("裝備與獸醫", "Gear & vet"), 10: ("步速與跑法", "Pace & style"),
    11: ("綜合速度與班次", "Speed/class"), 12: ("交互特徵", "Interactions"),
    13: ("連贏與序位", "Exotic & order"), 14: ("市場訊號", "Market signals"),
    15: ("場地動態", "Track dynamics"), 16: ("生物力學", "Biomechanics"),
}


def _has_cjk(s) -> bool:
    return bool(s) and any("一" <= c <= "鿿" for c in str(s))


def _load_gain() -> tuple[dict, float]:
    global _GAIN_CACHE
    if _GAIN_CACHE is None:
        p = BASE_DIR / "data" / "feature_importance.json"
        d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        feats = d.get("features", {})
        _GAIN_CACHE = ({fid: (v.get("gain") or 0.0) for fid, v in feats.items()},
                       float(d.get("max_gain") or 1.0) or 1.0)
    return _GAIN_CACHE


def _load_catalog() -> dict:
    global _CAT_CACHE
    if _CAT_CACHE is None:
        p = BASE_DIR / "features" / "descriptions.json"
        _CAT_CACHE = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return _CAT_CACHE


def _pick_strategy(conn: sqlite3.Connection, race_id: int) -> int | None:
    """Strategy whose predictions cover this race — prefer an enabled one."""
    row = conn.execute(
        "SELECT p.strategy_id FROM predictions p "
        "LEFT JOIN strategies s ON s.id = p.strategy_id "
        "WHERE p.race_id = ? "
        "ORDER BY COALESCE(s.enabled, 0) DESC, p.strategy_id LIMIT 1",
        (race_id,),
    ).fetchone()
    return row[0] if row else None


def _field_drivers(conn: sqlite3.Connection, race_id: int, brand: str,
                   max_out: int = 22) -> dict:
    """Every feature where this horse deviates from the field, ranked by
    |z| × model-importance. Returns {lifts:[...], drags:[...]} — each item
    carries name, category, value, field_mean, z, gain."""
    rows = conn.execute(
        "SELECT brand, feature_id, value FROM feature_values "
        "WHERE race_id = ? AND value IS NOT NULL", (race_id,),
    ).fetchall()
    if not rows:
        return {"lifts": [], "drags": []}
    by_feat: dict[str, dict[str, float]] = {}
    for b, f, v in rows:
        try:
            by_feat.setdefault(f, {})[b] = float(v)
        except (TypeError, ValueError):
            continue
    gains, max_gain = _load_gain()
    catalog = _load_catalog()
    scored: list[dict] = []
    for fid, vals in by_feat.items():
        if brand not in vals or len(vals) < 3:
            continue
        xs = list(vals.values())
        mean = sum(xs) / len(xs)
        var = sum((x - mean) ** 2 for x in xs) / len(xs)
        if var <= 0:
            continue
        z = (vals[brand] - mean) / (var ** 0.5)
        if abs(z) < 0.5:
            continue
        meta = catalog.get(fid, {})
        cat = meta.get("category")
        gain = gains.get(fid, 0.0)
        # importance-weighted salience: deviation magnitude × model reliance
        weight = abs(z) * (0.35 + gain / max_gain)
        scored.append({
            "feature_id": fid,
            "name_zh": meta.get("name_zh", fid),
            "name_en": meta.get("name_en", fid),
            "category_zh": _CAT_LABELS.get(cat, ("", ""))[0],
            "category_en": _CAT_LABELS.get(cat, ("", ""))[1],
            "value": round(vals[brand], 3),
            "field_mean": round(mean, 3),
            "z": round(z, 2),
            "gain": round(gain, 1),
            "_w": weight,
        })
    scored.sort(key=lambda d: -d["_w"])
    top = scored[:max_out]
    for d in top:
        d.pop("_w", None)
    return {
        "lifts": [d for d in top if d["z"] > 0],
        "drags": [d for d in top if d["z"] < 0],
    }


def _build_structured(conn: sqlite3.Connection, race_id: int, brand: str,
                      strategy_id: int | None) -> dict:
    rrow = conn.execute(
        "SELECT date, course, race_no, distance, class, going, race_name, participants "
        "FROM races WHERE id = ?", (race_id,),
    ).fetchone()
    if not rrow:
        return {}
    date, course, race_no, distance, race_class, going, race_name, participants = rrow

    # Model field — rank every horse by calibrated_prob to locate this one.
    field = conn.execute(
        "SELECT brand, fundamental_prob, blended_prob, calibrated_prob, "
        "       market_implied_prob, edge, odds_at_prediction, recommendation "
        "FROM predictions WHERE race_id = ? AND strategy_id = ?",
        (race_id, strategy_id),
    ).fetchall() if strategy_id is not None else []
    fcols = ("brand", "fundamental_prob", "blended_prob", "calibrated_prob",
             "market_implied_prob", "edge", "odds_at_prediction", "recommendation")
    field_d = [dict(zip(fcols, r)) for r in field]
    ranked = sorted([f for f in field_d if f["calibrated_prob"] is not None],
                    key=lambda f: -f["calibrated_prob"])
    rank = next((i + 1 for i, f in enumerate(ranked) if f["brand"] == brand), None)
    me = next((f for f in field_d if f["brand"] == brand), {})

    horse = conn.execute(
        "SELECT name, name_zh, name_en, age, sex, rating, trainer, starts, wins, "
        "       seconds, thirds FROM horses WHERE brand = ?", (brand,),
    ).fetchone()
    hcols = ("name", "name_zh", "name_en", "age", "sex", "rating", "trainer",
             "starts", "wins", "seconds", "thirds")
    horse_d = dict(zip(hcols, horse)) if horse else {"brand": brand}

    # Pre-race card details where available (draw / jockey / weight). results
    # rows exist post-race; for upcoming races these may be absent.
    rs = conn.execute(
        "SELECT jockey, trainer, draw, act_wt, decl_wt, odds, horse_no "
        "FROM results WHERE race_id = ? AND brand = ?", (race_id, brand),
    ).fetchone()
    if rs:
        horse_d.update({"jockey": rs[0], "trainer": rs[1] or horse_d.get("trainer"),
                        "draw": rs[2], "act_wt": rs[3], "decl_wt": rs[4],
                        "odds": rs[5], "horse_no": rs[6]})
    # Bilingual jockey/trainer from the persons registry so the zh narrative
    # uses Chinese names (results store the English short-code).
    import re as _re
    _strip = lambda s: _re.sub(r"\s*\(-?\d+\)\s*$", "", (s or "").strip()).strip()
    pmap = {(k, ne): nz for k, ne, nz in conn.execute(
        "SELECT kind, name_en, name_zh FROM persons WHERE name_en IS NOT NULL")}
    pmap_rev = {(k, nz): ne for (k, ne), nz in pmap.items() if nz}
    jck = _strip(horse_d.get("jockey"))
    trn = _strip(horse_d.get("trainer"))
    horse_d["jockey_zh"] = pmap.get(("jockey", jck)) or (jck if _has_cjk(jck) else None)
    horse_d["jockey_en"] = jck if not _has_cjk(jck) else pmap_rev.get(("jockey", jck))
    horse_d["trainer_zh"] = pmap.get(("trainer", trn)) or (trn if _has_cjk(trn) else None)
    horse_d["trainer_en"] = trn if not _has_cjk(trn) else pmap_rev.get(("trainer", trn))
    # Latest live odds snapshot (market view, pre-race).
    od = conn.execute(
        "SELECT win_odds, place_odds FROM odds_snapshots "
        "WHERE race_id = ? AND brand = ? ORDER BY ts DESC LIMIT 1", (race_id, brand),
    ).fetchone()
    if od:
        horse_d.setdefault("odds", od[0])
        horse_d["place_odds"] = od[1]
    if me.get("odds_at_prediction") and not horse_d.get("odds"):
        horse_d["odds"] = me["odds_at_prediction"]

    hist = conn.execute(
        "SELECT date, distance, going, class, draw, pla, lbw, odds, running "
        "FROM race_history WHERE brandno = ? AND date < ? "
        "ORDER BY date DESC LIMIT 6", (brand, date),
    ).fetchall()
    hist_d = [{"date": r[0], "distance": r[1], "going": r[2], "class": r[3],
               "draw": r[4], "position": r[5], "lbw": r[6], "odds": r[7],
               "running": r[8]} for r in hist]

    trial = conn.execute(
        "SELECT trial_date, summary_zh, summary_en FROM horse_trial_eval "
        "WHERE brand = ? ORDER BY trial_date DESC LIMIT 1", (brand,),
    ).fetchone()
    trial_d = {"date": trial[0], "summary_zh": trial[1], "summary_en": trial[2]} if trial else None

    # Day/venue track context (draw + pace bias).
    rail = conn.execute(
        "SELECT rail, watering_cm FROM rail_position WHERE date = ? AND course = ? LIMIT 1",
        (date, course),
    ).fetchone()
    bias = conn.execute(
        "SELECT inside_win_rate_residual, front_runner_win_rate_residual "
        "FROM track_bias_daily WHERE date = ? AND course = ? LIMIT 1",
        (date, course),
    ).fetchone()
    track_d = {}
    if rail:
        track_d.update({"rail": rail[0], "watering_cm": rail[1]})
    if bias:
        track_d.update({"inside_bias": bias[0], "front_runner_bias": bias[1]})

    return {
        "race": {"date": date, "course": course, "race_no": race_no,
                 "distance": distance, "class": race_class, "going": going,
                 "race_name": race_name, "field_size": participants},
        "horse": horse_d,
        "model": {"rank_in_field": rank, "field_size_predicted": len(ranked),
                  "fundamental_prob": me.get("fundamental_prob"),
                  "blended_prob": me.get("blended_prob"),
                  "calibrated_prob": me.get("calibrated_prob"),
                  "market_implied_prob": me.get("market_implied_prob"),
                  "edge": me.get("edge"), "recommendation": me.get("recommendation")},
        "drivers": _field_drivers(conn, race_id, brand),
        "recent_form": hist_d,
        "barrier_trial": trial_d,
        "track_context": track_d,
    }


def _pct(x) -> str:
    try:
        return f"{float(x) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def _rule_narrative(data: dict, lang: str) -> str:
    """Deterministic grouped fallback when DeepSeek is unavailable."""
    m = data.get("model") or {}
    dr = data.get("drivers") or {}
    lifts, drags = dr.get("lifts") or [], dr.get("drags") or []
    nm = "name_zh" if lang == "zh" else "name_en"
    cat = "category_zh" if lang == "zh" else "category_en"

    def line(d):
        return f"{d.get(cat) or ''}·{d.get(nm) or d['feature_id']}（{d['value']} vs {d['field_mean']}, z{'+' if d['z'] > 0 else ''}{d['z']}）"

    if lang == "zh":
        parts = []
        r = m.get("rank_in_field")
        if r:
            parts.append(f"模型評為全場第 {r} 位，校準勝率 {_pct(m.get('calibrated_prob'))}，"
                         f"市場隱含 {_pct(m.get('market_implied_prob'))}，edge "
                         f"{m.get('edge') if m.get('edge') is not None else '—'}。")
        if lifts:
            parts.append("利好因素 — " + "；".join(line(d) for d in lifts[:6]) + "。")
        if drags:
            parts.append("不利因素 — " + "；".join(line(d) for d in drags[:6]) + "。")
        return "".join(parts) or "資料不足，無法生成排名分析。"
    parts = []
    r = m.get("rank_in_field")
    if r:
        parts.append(f"Model ranks this horse #{r} in the field — calibrated "
                     f"{_pct(m.get('calibrated_prob'))} vs market "
                     f"{_pct(m.get('market_implied_prob'))}, edge "
                     f"{m.get('edge') if m.get('edge') is not None else '—'}.")
    if lifts:
        parts.append("Lifting it — " + "; ".join(line(d) for d in lifts[:6]) + ".")
    if drags:
        parts.append("Dragging it — " + "; ".join(line(d) for d in drags[:6]) + ".")
    return " ".join(parts) or "Insufficient data for a ranking analysis."


def _localise_payload(data: dict, lang: str) -> dict:
    """Collapse bilingual name fields to the prompt language and DROP the
    other language so the model can't echo (e.g.) an English horse name in a
    Chinese narrative. Mirrors eval_reason._localise_payload."""
    out = json.loads(json.dumps(data, ensure_ascii=False, default=str))
    h = out.get("horse") or {}
    nz, ne, nm = h.get("name_zh"), h.get("name_en"), h.get("name")
    h["name"] = (nz or nm or ne) if lang == "zh" else (ne or nm or nz)
    h["jockey"] = (h.get("jockey_zh") or h.get("jockey_en")) if lang == "zh" \
        else (h.get("jockey_en") or h.get("jockey_zh"))
    h["trainer"] = (h.get("trainer_zh") or h.get("trainer_en")) if lang == "zh" \
        else (h.get("trainer_en") or h.get("trainer_zh"))
    for k in ("name_zh", "name_en", "jockey_zh", "jockey_en", "trainer_zh", "trainer_en"):
        h.pop(k, None)
    out["horse"] = h
    return out


def _deepseek_call(data: dict, lang: str) -> str | None:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return None
    data = _localise_payload(data, lang)
    if lang == "zh":
        sys_prompt = (
            "你是頂尖香港賽馬讓磅分析師，同時是這個 LambdaMART 排名模型的解說員。"
            "請只用繁體中文（嚴禁英文）。根據提供的 JSON——包含模型對此馬的全場排名、"
            "校準勝率與 edge、相對全場的各項特徵偏離（每項附類別、數值 vs 全場平均、"
            "z 分數與模型重要性 gain）、近期往績、試閘評估與場地偏向——撰寫一篇"
            "「詳盡且分主題」的賽前排名分析。逐一涵蓋影響此馬排名的每個重要面向："
            "評分與班次匹配、近期狀態與適應、距離與場地適性、檔位與預期步速跑法、"
            "騎練狀態、負磅與裝備、以及模型觀點與市場的落差。每個主題說明它「拉高」"
            "還是「拉低」此馬排名及原因，並引用具體數字（value vs 全場、z、gain）。"
            "嚴格賽前視角：絕不可提及賽果或名次。輸出純文字，可用短段落，不要 markdown 標題。"
        )
        user_prompt = "馬匹排名因素資料（JSON），請撰寫詳盡繁體中文排名分析：\n\n" + json.dumps(
            data, ensure_ascii=False, default=str)
    else:
        sys_prompt = (
            "You are an elite Hong Kong racing handicapper and the explainer for a "
            "LambdaMART ranking model. Respond ONLY in English (no Chinese characters; "
            "transliterate or omit any Chinese names). Using the JSON — the model's "
            "rank for this horse in the field, its calibrated win-probability and edge, "
            "every feature deviation vs the field (each with category, value vs field "
            "mean, z-score and model-importance gain), recent form, barrier-trial read "
            "and track bias — write a DETAILED, theme-grouped pre-race ranking analysis. "
            "Cover every significant aspect driving this horse's ranking: rating/class "
            "fit, recent form & fitness, distance & going suitability, draw & expected "
            "running style/pace, jockey & trainer form, weight & gear, and the model-vs-"
            "market read. For each theme say whether it LIFTS or DRAGS the ranking and "
            "why, citing concrete numbers (value vs field, z, gain). Strictly pre-race: "
            "never mention the result or finishing position. Plain text, short "
            "paragraphs, no markdown headers."
        )
        user_prompt = "Horse ranking-factor data (JSON). Write the detailed analysis in English only:\n\n" + json.dumps(
            data, default=str)
    body = {
        "model": "deepseek-reasoner",
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ],
        # reasoner ignores temperature/response_format; reasoning tokens count
        # against max_tokens so give the budget room.
        "max_tokens": 8000,
    }
    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            res = json.loads(r.read().decode("utf-8"))
        txt = (res["choices"][0]["message"].get("content") or "").strip()
        return txt or None
    except Exception:
        return None


def generate(conn: sqlite3.Connection, race_id: int, brand: str,
             lang: str = "zh", force_refresh: bool = False,
             strategy_id: int | None = None) -> tuple[str, str]:
    """Return (text, source). source ∈ {'cache','deepseek','rule','pending'}.

    Cache policy mirrors eval_reason: a cached deepseek row is returned as-is;
    a cached rule row is upgraded once a DeepSeek key is present.
    """
    if strategy_id is None:
        strategy_id = _pick_strategy(conn, race_id)
    if strategy_id is None:
        return ("尚未有模型預測，無法生成排名分析。" if lang == "zh"
                else "No model prediction yet; ranking analysis unavailable."), "pending"

    cached = None
    if not force_refresh:
        cached = conn.execute(
            "SELECT text, source FROM horse_rank_analysis "
            "WHERE race_id = ? AND brand = ? AND lang = ?",
            (race_id, brand, lang),
        ).fetchone()
        if cached and cached[1] == "deepseek":
            return cached[0], "deepseek"
        if cached and cached[1] == "rule" and not os.environ.get("DEEPSEEK_API_KEY"):
            return cached[0], "rule"

    data = _build_structured(conn, race_id, brand, strategy_id)
    if not data or not (data.get("drivers", {}).get("lifts") or data.get("drivers", {}).get("drags")):
        # No feature_values for this race → nothing meaningful to analyse.
        if cached:
            return cached[0], cached[1]
        return ("特徵資料不足，無法生成排名分析。" if lang == "zh"
                else "Insufficient feature data for a ranking analysis."), "pending"

    text = _deepseek_call(data, lang)
    source = "deepseek" if text else "rule"
    if not text:
        if cached:
            return cached[0], "rule"
        text = _rule_narrative(data, lang)

    conn.execute(
        "INSERT OR REPLACE INTO horse_rank_analysis "
        "(race_id, brand, lang, source, text, structured_json) VALUES (?,?,?,?,?,?)",
        (race_id, brand, lang, source, text, json.dumps(data, ensure_ascii=False, default=str)),
    )
    conn.commit()
    return text, source

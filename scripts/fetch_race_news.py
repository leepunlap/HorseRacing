"""fetch_race_news — AI news/preview overlay for the UPCOMING meeting only.

Pipeline (advisory, never a model feature — point-in-time, no backtest leak):
  1. Tavily content search (TAVILY_API_KEY) returns the FULL TEXT of HK racing
     previews/tips, constrained to racing domains. Falls back to Google News RSS
     (headlines only) when no key.
  2. DeepSeek analyses the article text against the actual runner list into a
     detailed bilingual preview + specifically-tipped runners (with reasoning).
  3. Store in `race_news` keyed by (date, course) — including the source full
     text; the race page displays the preview + tips.

Usage:  python3 -m scripts.fetch_race_news [date] [course]   (defaults: next meeting)
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import date as _date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
DB = BASE / "data" / "racing.db"
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

import status as _status  # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS race_news (
  date TEXT NOT NULL, course TEXT NOT NULL,
  summary_en TEXT, summary_zh TEXT,
  tipped_json TEXT, sources_json TEXT, fetched_at TEXT,
  PRIMARY KEY (date, course)
)
"""


def _load_env():
    p = BASE / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            for k in ("DEEPSEEK_API_KEY", "TAVILY_API_KEY"):
                if line.startswith(k):
                    os.environ.setdefault(k, line.split("=", 1)[1].strip())


def _google_news(query: str, n: int = 8) -> list[dict]:
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(query) + "&hl=en-HK&gl=HK&ceid=HK:en")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    body = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    items = []
    for block in re.findall(r"<item>(.*?)</item>", body, re.S)[:n]:
        def g(tag):
            m = re.search(rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", block, re.S)
            return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""
        items.append({"title": g("title"), "url": g("link"),
                      "snippet": g("description")[:300], "date": g("pubDate")})
    return items


# Racing-specific sources only (not general news), recent window, deduped.
_RACING_SITES = "scmp.com OR hkjc.com OR thestandard.com.hk OR racingpost.com OR racenet.com.au"
# Tavily domain allow-list — EXPERT HK racing journalism only. Excludes
# bookmaker tip-bots, auto-generated form guides, simulations and YouTube, which
# add noise and "limited data" tips. SCMP + The Standard are the core HK expert
# desks; Racing Post for the big internationals.
_TAVILY_DOMAINS = ["scmp.com", "thestandard.com.hk", "racingpost.com"]


def _tavily_search(query: str, n: int = 6) -> list[dict]:
    """Content search: returns each result's EXTRACTED article text (not just a
    headline), constrained to HK racing domains."""
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        return []
    body = {"query": query, "search_depth": "advanced", "max_results": n,
            "include_raw_content": True, "include_domains": _TAVILY_DOMAINS}
    req = urllib.request.Request(
        "https://api.tavily.com/search", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    res = json.loads(urllib.request.urlopen(req, timeout=45).read())
    out = []
    for it in res.get("results", []):
        content = (it.get("content") or it.get("raw_content") or "").strip()
        out.append({"title": it.get("title", ""), "url": it.get("url", ""),
                    "snippet": content[:400], "content": content[:4000],
                    "score": it.get("score"), "date": ""})
    return out


def _gather_news(venue: str, d: str) -> list[dict]:
    # Prefer Tavily (returns real article CONTENT). Fall back to Google News RSS
    # (headlines only) when no Tavily key. Anchor to the specific meeting date.
    tav = []
    try:
        tav = _tavily_search(f"{venue} Hong Kong racing {d} tips preview selections")
    except Exception as exc:
        print(f"[race_news] tavily failed ({exc}); falling back to Google News")
    if tav:
        return tav[:8]
    queries = [
        f'{venue} racing {d} tips selections preview',
        f'"{venue}" racing {d} ({_RACING_SITES})',
    ]
    seen, out = set(), []
    for q in queries:
        try:
            items = _google_news(q, 8)
        except Exception:
            continue
        for it in items:
            key = it["title"].lower()
            if not key or key in seen or "google news" in key:
                continue
            seen.add(key)
            it.setdefault("content", it.get("snippet", ""))
            out.append(it)
    return out[:10]


def _deepseek_json(news: list[dict], runners: list[str], model_ctx: list[str],
                   meeting: str) -> dict | None:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return None
    sys_prompt = (
        f"You are a Hong Kong racing analyst working ALONGSIDE a quantitative model "
        f"for {meeting}. The model already estimates each horse's win probability from "
        "form, ratings, jockey/trainer stats, pace and the market. Do NOT write a "
        "competing tip sheet. Your sole value is to add what the model CANNOT see — "
        "soft/qualitative information in expert previews (trainer/jockey comments, "
        "fitness & trial reports, first-time/again gear, stable confidence, intended "
        "tactics, market moves, awkward draws, scratchings) — and to CROSS-READ it "
        "against the model's picks.\n"
        "You are given: the MODEL'S top picks per race (with win %), the FULL TEXT of "
        "EXPERT previews, and the official runner list. Some articles may cover other "
        "meetings — use only material about the target meeting.\n"
        "For each race the experts actually cover, classify the relationship to the "
        "model as one of: 'agree' (experts back the model's top pick — give the "
        "corroborating angle), 'diverge' (experts fancy a different runner or warn off "
        "the model's pick — name it and give the SOFT reason the model can't quantify), "
        "or 'added_info' (a material fact absent from the model's features).\n"
        "Rules: ground every claim in the article text — never invent; OMIT races the "
        "experts don't cover; do NOT restate form/ratings the model already encodes — "
        "only the qualitative delta; match horses by the exact name in the runner list.\n"
        "Output STRICT JSON (no markdown):\n"
        "  summary_en, summary_zh: 4-6 sentences in English and Traditional Chinese — "
        "the meeting's key model-vs-expert AGREEMENTS (confidence) and the most "
        "actionable DIVERGENCES / added-info.\n"
        "  tipped: [{name, alignment, note_en, note_zh}] where alignment is "
        "'agree'|'diverge'|'added_info' — only runners with a real expert angle that "
        "relates to the model; note_* states the soft angle and how it complements or "
        "challenges the model."
    )
    blocks = []
    for n in news:
        body = (n.get("content") or n.get("snippet") or "")[:3500]
        blocks.append(f"### {n['title']}\n{body}")
    user = (f"TARGET MEETING: {meeting}\n\nMODEL TOP PICKS PER RACE (win %):\n"
            + "\n".join(model_ctx)
            + "\n\nOFFICIAL RUNNERS:\n" + ", ".join(runners)
            + "\n\nEXPERT ARTICLES:\n" + "\n\n".join(blocks))
    # deepseek-reasoner (R1) thinks before answering — better for the agree/
    # diverge cross-read. It ignores temperature/top_p and does NOT support
    # response_format, so we rely on the "STRICT JSON" instruction + robust
    # extraction, and allow a longer timeout for the reasoning pass.
    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=json.dumps({"model": "deepseek-reasoner",
                         "messages": [{"role": "system", "content": sys_prompt},
                                      {"role": "user", "content": user}],
                         # reasoning tokens count against max_tokens, so leave
                         # ample room for the chain-of-thought AND the JSON.
                         "max_tokens": 8000}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        res = json.loads(r.read().decode("utf-8"))
    txt = (res["choices"][0]["message"].get("content") or "").strip()
    txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.M).strip()
    # The final answer should be pure JSON; if any prose slipped in, grab the
    # outermost {...} object.
    if not txt.startswith("{"):
        m = re.search(r"\{.*\}", txt, re.S)
        if m:
            txt = m.group(0)
    return json.loads(txt)


def main(d: str | None = None, course: str | None = None) -> int:
    _load_env()
    conn = sqlite3.connect(DB)
    conn.execute(DDL)
    if not d:
        row = conn.execute("SELECT date, course FROM races WHERE date >= ? "
                           "GROUP BY date, course ORDER BY date, course LIMIT 1",
                           (_date.today().isoformat(),)).fetchone()
        if not row:
            print("[race_news] no upcoming meeting"); return 0
        d, course = row
    course = course or "ST"
    venue = "Sha Tin" if course == "ST" else "Happy Valley"

    _status.process_up("race_news", ptype="oneshot", activity=f"{d} {course}")
    tid = _status.task_start("race_news", f"AI preview {d} {course}", total=3)
    try:
        _status.task_step(tid, done=1, msg="searching racing news")
        news = _gather_news(venue, d)
        runners = [r[0] for r in conn.execute(
            "SELECT DISTINCT horse_name FROM results WHERE race_id IN "
            "(SELECT id FROM races WHERE date=? AND course=?) AND horse_name IS NOT NULL",
            (d, course))]
        # The model's top-3 picks per race — so the AI relates news to the model
        # rather than producing an independent tip sheet.
        model_ctx = []
        for rid, rno in conn.execute(
            "SELECT id, race_no FROM races WHERE date=? AND course=? ORDER BY race_no", (d, course)):
            rows = conn.execute(
                "SELECT r.horse_name, p.calibrated_prob FROM predictions p "
                "JOIN results r ON r.race_id=p.race_id AND r.brand=p.brand "
                "WHERE p.strategy_id=1 AND p.race_id=? AND p.calibrated_prob IS NOT NULL "
                "ORDER BY p.calibrated_prob DESC LIMIT 3", (rid,)).fetchall()
            if rows:
                model_ctx.append(f"R{rno}: " + "; ".join(
                    f"{nm} {round((pp or 0)*100)}%" for nm, pp in rows))
        _status.task_step(tid, done=2, msg=f"cross-reading {len(news)} expert sources vs model")
        out = _deepseek_json(news, runners, model_ctx, f"{venue} ({course}) on {d}") or {}

        # match tipped names -> brand
        name_to_brand = {n.lower(): b for b, n in conn.execute(
            "SELECT brand, horse_name FROM results WHERE race_id IN "
            "(SELECT id FROM races WHERE date=? AND course=?)", (d, course)) if n}
        tipped = out.get("tipped") or []
        for t in tipped:
            t["brand"] = name_to_brand.get(str(t.get("name", "")).lower())

        conn.execute(
            "INSERT INTO race_news (date,course,summary_en,summary_zh,tipped_json,sources_json,fetched_at) "
            "VALUES (?,?,?,?,?,?,datetime('now')) ON CONFLICT(date,course) DO UPDATE SET "
            "summary_en=excluded.summary_en, summary_zh=excluded.summary_zh, "
            "tipped_json=excluded.tipped_json, sources_json=excluded.sources_json, fetched_at=excluded.fetched_at",
            (d, course, out.get("summary_en"), out.get("summary_zh"),
             json.dumps(tipped, ensure_ascii=False),
             json.dumps([{"title": n["title"], "url": n["url"], "snippet": n["snippet"],
                          "content": n.get("content", ""), "score": n.get("score")}
                         for n in news], ensure_ascii=False)))
        conn.commit()
        _status.task_done(tid, f"{len(tipped)} tipped runner(s), {len(news)} sources")
        _status.process_down("race_news", "done")
        print(f"[race_news] {d} {course}: {len(tipped)} tipped, {len(news)} sources")
        return 0
    except Exception as exc:
        _status.task_error(tid, str(exc))
        _status.process_down("race_news", "error")
        print(f"[race_news] failed: {exc}")
        return 1


if __name__ == "__main__":
    args = sys.argv[1:]
    sys.exit(main(args[0] if args else None, args[1] if len(args) > 1 else None))

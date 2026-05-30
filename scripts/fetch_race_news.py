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
# Tavily domain allow-list — HK racing previews/tips. Constraining to these
# turns a noisy general search (which returned US Belmont/Indy) into the right
# Sha Tin/Happy Valley previews at high relevance.
_TAVILY_DOMAINS = ["thestandard.com.hk", "scmp.com", "racenet.com.au",
                   "racingandsports.com.au", "hkjc.com", "idol.hkjc.com",
                   "hollywoodbets.net", "racingpost.com"]


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


def _deepseek_json(news: list[dict], runners: list[str], meeting: str) -> dict | None:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return None
    sys_prompt = (
        f"You are a Hong Kong horse-racing analyst. The target meeting is {meeting}. "
        "You are given the FULL TEXT of recent racing previews/tips and the official "
        "runner list. IMPORTANT: some articles may discuss OTHER meetings/dates — use "
        "only material about the target meeting. Produce STRICT JSON (no markdown):\n"
        "  summary_en, summary_zh: a detailed 4-6 sentence preview of the target "
        "meeting (key contenders, pace/draw/jockey angles, market shape) in English "
        "and Traditional Chinese, grounded in the articles.\n"
        "  tipped: a list of {name, note_en, note_zh} for runners SPECIFICALLY "
        "tipped/analysed for the TARGET meeting AND present in the runner list — match "
        "by exact name from the runner list; note_* gives the reasoning the article "
        "gave (why it's fancied).\n"
        "Never invent tips or reasoning not supported by the article text."
    )
    blocks = []
    for n in news:
        body = (n.get("content") or n.get("snippet") or "")[:3500]
        blocks.append(f"### {n['title']}\n{body}")
    user = (f"TARGET MEETING: {meeting}\n\nRUNNERS:\n" + ", ".join(runners)
            + "\n\nARTICLES:\n" + "\n\n".join(blocks))
    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=json.dumps({"model": "deepseek-chat",
                         "messages": [{"role": "system", "content": sys_prompt},
                                      {"role": "user", "content": user}],
                         "temperature": 0.2, "max_tokens": 4000,
                         "response_format": {"type": "json_object"}}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=40) as r:
        res = json.loads(r.read().decode("utf-8"))
    txt = res["choices"][0]["message"]["content"].strip()
    txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.M).strip()
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
        _status.task_step(tid, done=2, msg=f"summarising {len(news)} items via DeepSeek")
        out = _deepseek_json(news, runners, f"{venue} ({course}) on {d}") or {}

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

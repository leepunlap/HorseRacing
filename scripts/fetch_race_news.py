"""fetch_race_news — AI news/preview overlay for the UPCOMING meeting only.

Pipeline (advisory, never a model feature — point-in-time, no backtest leak):
  1. Google News RSS (no API key) for the meeting's preview/tips articles.
  2. DeepSeek summarises the snippets against the actual runner list into a
     bilingual meeting preview + a list of specifically-tipped runners.
  3. Store in `race_news` keyed by (date, course); the race page displays it.

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
            if line.startswith("DEEPSEEK_API_KEY"):
                os.environ.setdefault("DEEPSEEK_API_KEY", line.split("=", 1)[1].strip())


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


def _deepseek_json(news: list[dict], runners: list[str]) -> dict | None:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return None
    sys_prompt = (
        "You are a Hong Kong horse-racing analyst. You are given recent news/preview "
        "snippets for an upcoming meeting and the official runner list. Produce STRICT "
        "JSON (no markdown) with keys: summary_en, summary_zh (a 3-4 sentence meeting "
        "preview in English and Traditional Chinese), and tipped (a list of objects "
        "{name, note_en, note_zh}) for runners SPECIFICALLY mentioned/tipped in the "
        "news AND present in the runner list — match by horse name, use the exact name "
        "from the runner list. If the news has nothing material, return empty tipped and "
        "say so in the summaries. Never invent tips not supported by the snippets."
    )
    user = ("RUNNERS:\n" + ", ".join(runners) + "\n\nNEWS SNIPPETS:\n"
            + "\n".join(f"- {n['title']} :: {n['snippet']}" for n in news))
    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=json.dumps({"model": "deepseek-chat",
                         "messages": [{"role": "system", "content": sys_prompt},
                                      {"role": "user", "content": user}],
                         "temperature": 0.2, "max_tokens": 900,
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
        _status.task_step(tid, done=1, msg="searching news")
        news = _google_news(f"{venue} racing {d} preview tips")
        runners = [r[0] for r in conn.execute(
            "SELECT DISTINCT horse_name FROM results WHERE race_id IN "
            "(SELECT id FROM races WHERE date=? AND course=?) AND horse_name IS NOT NULL",
            (d, course))]
        _status.task_step(tid, done=2, msg=f"summarising {len(news)} items via DeepSeek")
        out = _deepseek_json(news, runners) or {}

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
             json.dumps([{"title": n["title"], "url": n["url"]} for n in news], ensure_ascii=False)))
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

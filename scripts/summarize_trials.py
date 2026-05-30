"""summarize_trials — AI assessment of each runner's latest barrier trial.

HK barrier trials are often NOT run at full effort, so the raw finishing
position is a noisy signal. The stewards' trial note + time + field size tell
you whether a trial showed genuine ability, was troubled/unlucky, or was a soft
non-competitive run. We batch all of a meeting's runners into ONE DeepSeek call
and store a one-line bilingual assessment per horse — most useful for debutants,
whose only form line is the trial.

Stored in `horse_trial_eval` (keyed by brand). Advisory; not a model feature.

Usage:  python3 -m scripts.summarize_trials [date] [course]   (default: next meeting)
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import urllib.request
from datetime import date as _date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
DB = BASE / "data" / "racing.db"
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

import status as _status  # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS horse_trial_eval (
  brand TEXT PRIMARY KEY,
  trial_date TEXT, trial_note TEXT,
  summary_en TEXT, summary_zh TEXT, fetched_at TEXT
)
"""


def _load_env():
    p = BASE / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if line.startswith("DEEPSEEK_API_KEY"):
                os.environ.setdefault("DEEPSEEK_API_KEY", line.split("=", 1)[1].strip())


def _deepseek(horses: list[dict]) -> dict:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return {}
    sys_prompt = (
        "You are a Hong Kong racing analyst assessing BARRIER TRIALS. HK trials are "
        "often not run at full effort, so finishing position alone is unreliable — weigh "
        "the stewards' note, the time, and field size. For EACH horse, write a concise "
        "one-line assessment of what the trial actually showed: genuine ability / "
        "unlucky-or-troubled / soft non-competitive run / promising debut, etc. Output "
        "STRICT JSON (no markdown): an object mapping each horse's brand to "
        "{en, zh} one-liners (zh in Traditional Chinese). Ground every assessment in the "
        "supplied note — never invent."
    )
    lines = []
    for h in horses:
        lines.append(f"{h['brand']}: trial {h['pos']}/{h['field']} "
                     f"time {h['time']}s — note: {h['note']}")
    user = "TRIALS:\n" + "\n".join(lines)
    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=json.dumps({"model": "deepseek-chat",
                         "messages": [{"role": "system", "content": sys_prompt},
                                      {"role": "user", "content": user}],
                         "temperature": 0.2, "max_tokens": 4096,
                         "response_format": {"type": "json_object"}}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=60) as r:
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
            print("[trials] no upcoming meeting"); return 0
        d, course = row
    course = course or "ST"

    # latest trial per runner (with a note) before the meeting date
    runners = []
    for brand, in conn.execute(
        "SELECT DISTINCT brand FROM results WHERE race_id IN "
        "(SELECT id FROM races WHERE date=? AND course=?)", (d, course)):
        t = conn.execute(
            "SELECT position, field_size, time_sec, date, notes FROM barrier_trials "
            "WHERE brand=? AND date < ? AND notes IS NOT NULL ORDER BY date DESC LIMIT 1",
            (brand, d)).fetchone()
        if t and t[4]:
            runners.append({"brand": brand, "pos": t[0], "field": t[1] or "?",
                            "time": t[2], "date": t[3], "note": t[4]})
    if not runners:
        print(f"[trials] {d} {course}: no trials with notes"); return 0

    _status.process_up("summarize_trials", ptype="oneshot", activity=f"{d} {course}")
    tid = _status.task_start("summarize_trials", f"AI trial notes {d} {course}", total=2)
    try:
        _status.task_step(tid, done=1, msg=f"summarising {len(runners)} trials via DeepSeek")
        # Chunk so the JSON output never overruns max_tokens (and one bad chunk
        # doesn't lose the rest).
        out: dict = {}
        for i in range(0, len(runners), 20):
            try:
                out.update(_deepseek(runners[i:i + 20]))
            except Exception as exc:
                print(f"[trials] chunk {i // 20} failed: {exc}")
        n = 0
        for r in runners:
            a = out.get(r["brand"]) or {}
            if not (a.get("en") or a.get("zh")):
                continue
            conn.execute(
                "INSERT INTO horse_trial_eval (brand,trial_date,trial_note,summary_en,summary_zh,fetched_at) "
                "VALUES (?,?,?,?,?,datetime('now')) ON CONFLICT(brand) DO UPDATE SET "
                "trial_date=excluded.trial_date, trial_note=excluded.trial_note, "
                "summary_en=excluded.summary_en, summary_zh=excluded.summary_zh, fetched_at=excluded.fetched_at",
                (r["brand"], r["date"], r["note"], a.get("en"), a.get("zh")))
            n += 1
        conn.commit()
        _status.task_done(tid, f"{n} trial assessments")
        _status.process_down("summarize_trials", "done")
        print(f"[trials] {d} {course}: wrote {n} trial assessments")
        return 0
    except Exception as exc:
        _status.task_error(tid, str(exc))
        _status.process_down("summarize_trials", "error")
        print(f"[trials] failed: {exc}")
        return 1


if __name__ == "__main__":
    args = sys.argv[1:]
    sys.exit(main(args[0] if args else None, args[1] if len(args) > 1 else None))

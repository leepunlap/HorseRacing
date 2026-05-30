"""Backtest the Eric 定律 guardrails as a POINT-IN-TIME post-hoc re-rank.

We do NOT retrain XGBoost. Each law is applied as a correction on top of the
model's existing calibrated_prob, using only information knowable before the
race (history with date < race date; carried weights & surface are pre-race
conditions). Result positions are used ONLY to score, never to adjust.

  EL001 (downgrade / guardrail 'cap'): a short-priced, over-confident horse that
        is switching to a surface it has not won on, with a rating freshly
        spiked to its class ceiling -> shrink its score to the market level.
  EL002 (upgrade / boost): a progressive horse narrowly beaten last time that
        re-meets a recent rival now carrying materially more weight -> boost.

Outputs: baseline vs Eric-enabled metrics (top-1 wins, mean finish of the #1
pick, top-3 rate, flat-stake win ROI) and the exact list of races whose #1 pick
changed, with the firing law + reason.

    python3 -m scripts.eric_backtest
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "data" / "racing.db"

CLASS_CEIL = {1: 120, 2: 100, 3: 80, 4: 60, 5: 40}   # HK rating band upper bound

# ── tunables (kept conservative & few, to avoid over-fitting) ──────────────
EL001_RATING_JUMP = 12      # season rating rise (pts) to call it 'spiked'
EL001_CEIL_GAP = 3          # rating within this of the class ceiling
EL001_OVERCONF = 0.15       # model_prob - market_prob to call it over-confident
EL001_SHORT_ODDS = 6.0      # short price
EL002_RATING_SLOPE = 8      # season rating rise (pts) to call it progressive
EL002_BEATEN_MAX_L = 0.5    # head/neck last-time-out (lengths)
EL002_BIGODDS_LTO = 8.0     # 'unfancied' last time
EL002_WEIGHT_SWING = 5      # lb the weight terms swung in this horse's favour
EL002_RATING_SWING = 4      # pts the handicap mark swung my way vs a re-met rival


def _parse_lbw(raw) -> float | None:
    if raw is None: return None
    s = str(raw).strip()
    if not s or s in ('---', '--', 'WIN', '-'): return None
    words = {'鼻位': 0.05, '短鼻位': 0.03, '短馬頭位': 0.10, '馬頭位': 0.20,
             '頭位': 0.20, '頸位': 0.30, '短頸位': 0.20, '半個馬位': 0.50, 'HD': 0.20, 'SH': 0.05, 'NK': 0.30}
    for k, v in words.items():
        if k in s: return v
    m = re.match(r'^([\d.]+)(?:-(\d+)/(\d+))?$', s)
    if m:
        whole = float(m.group(1))
        if m.group(2): whole += int(m.group(2)) / int(m.group(3))
        return whole
    try: return float(s)
    except (TypeError, ValueError): return None


def _pos(raw):
    if raw is None: return None
    m = re.match(r'(\d+)', str(raw).strip())
    return int(m.group(1)) if m else None


def _surface(venue: str | None) -> str | None:
    if not venue: return None
    if '全天候' in venue: return 'awt'
    if '草地' in venue: return 'turf'
    return None


def _class_int(raw) -> int | None:
    if raw is None: return None
    m = re.search(r'(\d)', str(raw))
    return int(m.group(1)) if m else None


def _num(raw):
    if raw is None: return None
    try: return float(raw)
    except (TypeError, ValueError):
        m = re.search(r'[-+]?\d*\.?\d+', str(raw))
        return float(m.group(0)) if m else None


def _load_history(conn) -> dict:
    """All detailed (Chinese-venue) race_history rows per brand, oldest->newest.
    These carry surface, rating, carried weight, finish, margin, running line."""
    hist: dict[str, list] = {}
    for r in conn.execute(
        "SELECT brandno, date, venue, distance, rating, actwt, pla, lbw, running "
        "FROM race_history WHERE venue LIKE '%地%' OR venue LIKE '%全天候%' "
        "ORDER BY brandno, date"
    ):
        hist.setdefault(r[0], []).append({
            "date": r[1], "surface": _surface(r[2]), "distance": _num(r[3]),
            "rating": _num(r[4]), "actwt": _num(r[5]), "pla": _pos(r[6]),
            "lbw": _parse_lbw(r[7]), "running": r[8], "venue": r[2],
        })
    return hist


def el001_fires(h, race_surface, market_prob):
    """Returns (fired, reason) for the downgrade cap."""
    past = [x for x in h["hist_before"]]
    if not past or race_surface is None:
        return False, None
    # RECENT (this-season) wins by surface, point-in-time. The law is about a
    # horse whose *recent* winning form is on the other surface, not its career.
    season_wins = [x for x in past if x["pla"] == 1 and x["surface"]
                   and x["date"] >= h["season_floor_date"]]
    if not season_wins:
        return False, None
    if any(x["surface"] == race_surface for x in season_wins):   # has won on it this season
        return False, None
    recent_win_surf = season_wins[-1]["surface"]
    # rating spike to ceiling
    rating_now = h["rating_in"]
    if rating_now is None:
        return False, None
    season = [x["rating"] for x in past if x["rating"] is not None and x["date"] >= h["season_floor_date"]]
    slope = (rating_now - min(season)) if season else 0
    cls = h["class_int"]
    ceil_gap = (CLASS_CEIL.get(cls, 999) - rating_now) if cls else 999
    if slope < EL001_RATING_JUMP or ceil_gap > EL001_CEIL_GAP:
        return False, None
    # over-confident + short price
    over = (h["cal"] - market_prob) >= EL001_OVERCONF if market_prob else False
    short = h["odds"] is not None and h["odds"] <= EL001_SHORT_ODDS
    if not (over and short):
        return False, None
    reason = (f"異面升班懲罰：近勝在{recent_win_surf.upper()}、今仗{race_surface.upper()}未贏過；"
              f"評分季內+{slope}至{rating_now}(距班頂{ceil_gap})；模型{h['cal']*100:.0f}% vs 市場{market_prob*100:.0f}%、賠率{h['odds']}")
    return True, reason


def el002_fires(h, rivals_hist, cur_wt):
    """Progressive + narrow-beaten LTO + head-to-head weight swing."""
    past = h["hist_before"]
    if not past:
        return False, None
    rating_now = h["rating_in"]
    season = [x["rating"] for x in past if x["rating"] is not None and x["date"] >= h["season_floor_date"]]
    slope = (rating_now - min(season)) if (season and rating_now is not None) else 0
    if slope < EL002_RATING_SLOPE:
        return False, None
    lto = past[-1]
    beaten = lto["lbw"] if lto["pla"] and lto["pla"] >= 2 else None
    narrow = beaten is not None and beaten <= EL002_BEATEN_MAX_L
    if not narrow:
        return False, None
    # Head-to-head swing vs a rival in THIS field that shared my LTO. Use the
    # RATING (handicap mark) swing, not raw carried weight — carried weight is
    # confounded by class weight-scales + apprentice claims, whereas the mark is
    # the handicapper's pure re-rating ("對手被加磅" = its rating rose).
    my_rt_then = lto.get("rating"); my_rt_now = h["rating_in"]
    if my_rt_then is None or my_rt_now is None:
        return False, None
    for rb, rh in rivals_hist.items():
        shared = next((x for x in rh["hist_all"] if x["date"] == lto["date"]
                       and x["venue"] == lto["venue"]), None)
        if not shared or shared["rating"] is None or rh.get("cur_rating") is None:
            continue
        swing = (rh["cur_rating"] - shared["rating"]) - (my_rt_now - my_rt_then)
        if swing >= EL002_RATING_SWING:
            reason = (f"進步馬+讓磅互換：評分季內+{slope}；上仗僅負{beaten}個馬位；"
                      f"與{rb}上仗同場，今仗評分互換{swing:+.0f}分向本駒傾斜"
                      f"（本駒{my_rt_then:.0f}→{my_rt_now:.0f}、對手{shared['rating']:.0f}→{rh['cur_rating']:.0f}）")
            return True, reason
    return False, None


def run():
    conn = sqlite3.connect(DB)
    hist = _load_history(conn)

    # universe: strategy-1 predictions with results
    races = [r[0] for r in conn.execute(
        "SELECT DISTINCT p.race_id FROM predictions p JOIN results rs ON rs.race_id=p.race_id "
        "WHERE p.strategy_id=1 AND rs.position IS NOT NULL ORDER BY p.race_id")]

    # odds fallback map (results.odds else latest snapshot via horse_no)
    def odds_for(rid, brand, results_odds, horse_no):
        if results_odds is not None:
            return results_odds
        row = conn.execute("SELECT win_odds FROM odds_snapshots WHERE race_id=? AND horse_no=? "
                           "AND win_odds IS NOT NULL ORDER BY ts DESC LIMIT 1", (rid, horse_no)).fetchone()
        return row[0] if row else None

    base = {"n": 0, "win": 0, "top3": 0, "posum": 0, "stake": 0.0, "ret": 0.0}
    eric = {"n": 0, "win": 0, "top3": 0, "posum": 0, "stake": 0.0, "ret": 0.0}
    changed = []

    for rid in races:
        meta = conn.execute("SELECT date, course, distance, class FROM races WHERE id=?", (rid,)).fetchone()
        if not meta: continue
        date, course, dist, cls = meta
        # race surface
        if course == 'HV':
            race_surface = 'turf'
        else:
            vr = conn.execute("SELECT venue FROM race_history WHERE date=? AND distance=? "
                              "AND (venue LIKE '%地%' OR venue LIKE '%全天候%') LIMIT 1", (date, dist)).fetchone()
            race_surface = _surface(vr[0]) if vr else None
        season_floor = (date[:4] if int(date[5:7]) >= 9 else str(int(date[:4]) - 1)) + "-09-01"

        rows = conn.execute(
            "SELECT p.brand, p.calibrated_prob, p.market_implied_prob, rs.position, rs.act_wt, rs.odds, rs.horse_no "
            "FROM predictions p JOIN results rs ON rs.race_id=p.race_id AND rs.brand=p.brand "
            "WHERE p.race_id=? AND p.strategy_id=1 AND p.calibrated_prob IS NOT NULL", (rid,)).fetchall()
        if len(rows) < 2: continue

        horses = []
        for brand, cal, mkt, pos, actwt, rodds, hno in rows:
            hh = hist.get(brand, [])
            before = [x for x in hh if x["date"] < date]
            rating_in = next((x["rating"] for x in reversed(hh) if x["date"] == date and x["rating"] is not None),
                             (before[-1]["rating"] if before and before[-1]["rating"] is not None else None))
            horses.append({
                "brand": brand, "cal": cal, "mkt": mkt, "pos": _pos(pos),
                "actwt": actwt, "odds": odds_for(rid, brand, rodds, hno),
                "hist_before": before, "hist_all": hh, "rating_in": rating_in,
                "class_int": _class_int(cls), "season_floor_date": season_floor,
            })

        # rivals' history view for h2h
        rivals = {h["brand"]: {"hist_all": h["hist_all"], "cur_wt": h["actwt"],
                               "cur_rating": h["rating_in"]} for h in horses}

        # baseline #1
        base1 = max(horses, key=lambda h: h["cal"])
        # apply laws -> adjusted score
        fired_map = {}
        for h in horses:
            score = h["cal"]; law = None; reason = None
            f1, r1 = el001_fires(h, race_surface, h["mkt"])
            if f1:
                score = (h["mkt"] or h["cal"]) * 0.9   # cap toward market
                law, reason = "EL001", r1
            else:
                others = {b: v for b, v in rivals.items() if b != h["brand"]}
                f2, r2 = el002_fires(h, others, h["actwt"])
                if f2:
                    score = h["cal"] * 1.6
                    law, reason = "EL002", r2
            h["score"] = score
            if law: fired_map[h["brand"]] = (law, reason)
        eric1 = max(horses, key=lambda h: h["score"])

        # accumulate metrics
        def acc(d, pick):
            d["n"] += 1
            if pick["pos"] == 1: d["win"] += 1
            if pick["pos"] and pick["pos"] <= 3: d["top3"] += 1
            if pick["pos"]: d["posum"] += pick["pos"]
            if pick["odds"] is not None:
                d["stake"] += 1.0
                d["ret"] += pick["odds"] if pick["pos"] == 1 else 0.0
        acc(base, base1); acc(eric, eric1)

        if eric1["brand"] != base1["brand"]:
            law, reason = fired_map.get(eric1["brand"]) or fired_map.get(base1["brand"]) or ("", "")
            changed.append({
                "race_id": rid, "date": date, "course": course, "race": f"{course}",
                "from": base1["brand"], "from_pos": base1["pos"], "from_odds": base1["odds"],
                "to": eric1["brand"], "to_pos": eric1["pos"], "to_odds": eric1["odds"],
                "law": law, "reason": reason,
            })
    conn.close()
    return base, eric, changed


def _pct(x, n): return f"{100*x/n:.1f}%" if n else "—"

def main():
    base, eric, changed = run()
    def roi(d): return (d["ret"] - d["stake"]) / d["stake"] if d["stake"] else 0.0
    print(f"universe: {base['n']} races (strategy-1 #1 pick, with results)\n")
    print(f"{'metric':28} {'baseline':>12} {'Eric-enabled':>14} {'delta':>10}")
    print(f"{'#1 wins (top-1 hit)':28} {base['win']:>12} {eric['win']:>14} {eric['win']-base['win']:>+10}")
    print(f"{'top-1 hit rate':28} {_pct(base['win'],base['n']):>12} {_pct(eric['win'],eric['n']):>14} "
          f"{100*(eric['win']/eric['n']-base['win']/base['n']):>+9.2f}pp")
    print(f"{'top-3 hit rate':28} {_pct(base['top3'],base['n']):>12} {_pct(eric['top3'],eric['n']):>14} "
          f"{100*(eric['top3']/eric['n']-base['top3']/base['n']):>+9.2f}pp")
    print(f"{'mean finish pos of #1':28} {base['posum']/base['n']:>12.3f} {eric['posum']/eric['n']:>14.3f} "
          f"{eric['posum']/eric['n']-base['posum']/base['n']:>+10.3f}")
    print(f"{'flat win ROI':28} {_pct(roi(base)+0 and roi(base),1) if False else f'{roi(base)*100:+.1f}%':>12} "
          f"{f'{roi(eric)*100:+.1f}%':>14} {100*(roi(eric)-roi(base)):>+9.2f}pp")
    print(f"\n#1 picks changed: {len(changed)}")
    for ch in changed:
        helped = ""
        if ch["from_pos"] and ch["to_pos"]:
            helped = "✓ better" if ch["to_pos"] < ch["from_pos"] else ("✗ worse" if ch["to_pos"] > ch["from_pos"] else "= same")
        print(f"\n  {ch['date']} {ch['course']} (race {ch['race_id']}) [{ch['law']}] {helped}")
        print(f"    舊#1 {ch['from']} → 完成第{ch['from_pos']}名 (賠率{ch['from_odds']})")
        print(f"    新#1 {ch['to']} → 完成第{ch['to_pos']}名 (賠率{ch['to_odds']})")
        print(f"    理由: {ch['reason']}")


if __name__ == "__main__":
    main()

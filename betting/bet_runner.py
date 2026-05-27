"""Apply a bet strategy to a model's predictions and write to bet_ledger.

A bet strategy is a post-prediction rule (top-1, top-N, Kelly, place,
market-favourite, exotics like quinella / trifecta, etc.). It reads
`predictions` for a model_strategy_id and writes one or more rows to
`bet_ledger` per race.

A single model can have many bet strategies layered on top — each is
cheap (O(races) SQL pass, no retraining). The point is to compare
betting rules side-by-side against the SAME race set / SAME model.

Rule kinds & their `params_json` schema:

  --- WIN / PLACE pool (singles) ---
  flat_top1            { "stake": 500 }
  kelly_top1           { "bankroll": 10000, "kelly_frac": 0.25, "max_pct": 0.05 }
  flat_top1_filtered   { "stake": 500, "min_prob": 0.20?, "max_field": 12? }
  dutch_topN           { "total_stake": 500, "n": 2 }
  place_top1           { "stake": 500 }       (paid 1/4 odds on top-3 by HKJC tote)
  each_way_top1        { "stake": 500 }       (250 win + 250 place)
  market_fav           { "stake": 500 }       (ignore model — lowest odds)
  market_blended_top1  { "stake": 500, "alpha": 1.5, "beta": 0.7 }

  --- Exotic pools (multi-horse) — settled against HKJC's `dividends` table
      which records winning combinations + per-$10 payout ---
  quinella_top2        { "stake": 100 }       (QIN  — top-2 by prob, any order)
  qpl_top2             { "stake": 100 }       (QPL  — top-2, any of top-3 finish)
  qpl_top3_box         { "stake_per_pair": 100 }
                                              (QPL  — 3 pairs from top-3 picks)
  forecast_top2        { "stake": 100 }       (EXA  — top-2 in EXACT order)
  trifecta_top3        { "stake": 100 }       (TRI  — top-3 in EXACT order)
  trio_top3            { "stake": 100 }       (TRIO — top-3 in ANY order)
  first_four_top4      { "stake": 100 }       (F4   — top-4 in ANY order)
  quartet_top4         { "stake": 100 }       (QTT  — top-4 in EXACT order)

Usage:
    python3 -m betting.bet_runner --bet-strategy flat_top1
    python3 -m betting.bet_runner --all                  # all enabled
    python3 -m betting.bet_runner --bet-strategy flat_top1 \\
        --from 2026-05-01 --to 2026-05-24                # date-restricted
"""

from __future__ import annotations
import argparse
import json
import math
import sqlite3
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB_PATH = BASE / "data" / "racing.db"

# HKJC's PLACE tote pays ~1/4 of the WIN odds for top-3 finishers in big
# fields (8+ horses) and top-2 in small fields. We approximate at 1/4 for
# simplicity; refine later by polling the actual place pool.
PLACE_PAYOUT_FRACTION = 0.25


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA journal_mode = WAL")
    return c


def _coerce_position(raw) -> int | None:
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _coerce_odds(raw) -> float | None:
    if raw is None:
        return None
    try:
        v = float(raw)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


# ─── Per-race rule implementations ────────────────────────────────────────
# Each rule takes:
#   race_rows: list of dicts {brand, prob, odds, position}
#   params: dict (the bet_strategies.params_json parsed)
# and returns a list of bet dicts:
#   {brand, pool, stake, pick_rank, reason}
# Settlement (won/payout/pnl) is computed by the caller from race_rows.

def _rule_flat_top1(rows, params):
    valid = [r for r in rows if r["prob"] is not None]
    if not valid: return []
    valid.sort(key=lambda r: (-r["prob"], r["brand"]))
    top = valid[0]
    return [{"brand": top["brand"], "pool": "WIN",
             "stake": params.get("stake", 500.0),
             "pick_rank": 1, "reason": "top_prob"}]


def _rule_kelly_top1(rows, params):
    valid = [r for r in rows if r["prob"] is not None and r["odds"] and r["odds"] > 0]
    if not valid: return []
    valid.sort(key=lambda r: (-r["prob"], r["brand"]))
    top = valid[0]
    p, o = top["prob"], top["odds"]
    kelly_full = ((p * o - 1) / (o - 1)) if o > 1 else 0.0
    kelly_full = max(0.0, kelly_full)
    bankroll = float(params.get("bankroll", 10000.0))
    kelly_frac = float(params.get("kelly_frac", 0.25))
    max_pct = float(params.get("max_pct", 0.05))
    stake = min(kelly_full * kelly_frac * bankroll, max_pct * bankroll)
    if stake <= 0:
        return []
    return [{"brand": top["brand"], "pool": "WIN", "stake": round(stake, 2),
             "pick_rank": 1, "reason": f"kelly_frac={kelly_frac:.2f}"}]


def _rule_flat_top1_filtered(rows, params):
    valid = [r for r in rows if r["prob"] is not None]
    if not valid: return []
    min_prob = float(params.get("min_prob", 0.0))
    max_field = int(params.get("max_field", 99))
    if len(rows) > max_field:
        return []                           # skip big-field races
    valid.sort(key=lambda r: (-r["prob"], r["brand"]))
    top = valid[0]
    if top["prob"] < min_prob:
        return []                           # skip low-confidence
    return [{"brand": top["brand"], "pool": "WIN",
             "stake": params.get("stake", 500.0),
             "pick_rank": 1, "reason": "top_prob_filtered"}]


def _rule_dutch_topN(rows, params):
    valid = [r for r in rows if r["prob"] is not None and r["odds"] and r["odds"] > 0]
    if not valid: return []
    n = int(params.get("n", 2))
    total_stake = float(params.get("total_stake", 500.0))
    valid.sort(key=lambda r: (-r["prob"], r["brand"]))
    picks = valid[:n]
    if not picks:
        return []
    # Dutch sizing: stake_i ∝ 1/odds_i so each winner pays the same amount.
    inv_odds = [1.0 / p["odds"] for p in picks]
    s = sum(inv_odds)
    stakes = [total_stake * x / s for x in inv_odds]
    return [{"brand": p["brand"], "pool": "WIN",
             "stake": round(st, 2), "pick_rank": i + 1, "reason": f"dutch_{n}"}
            for i, (p, st) in enumerate(zip(picks, stakes))]


def _rule_place_top1(rows, params):
    valid = [r for r in rows if r["prob"] is not None]
    if not valid: return []
    valid.sort(key=lambda r: (-r["prob"], r["brand"]))
    top = valid[0]
    return [{"brand": top["brand"], "pool": "PLACE",
             "stake": params.get("stake", 500.0),
             "pick_rank": 1, "reason": "top_prob_place"}]


def _rule_each_way_top1(rows, params):
    valid = [r for r in rows if r["prob"] is not None]
    if not valid: return []
    valid.sort(key=lambda r: (-r["prob"], r["brand"]))
    top = valid[0]
    full = float(params.get("stake", 500.0))
    return [
        {"brand": top["brand"], "pool": "WIN",   "stake": full / 2,
         "pick_rank": 1, "reason": "each_way_win"},
        {"brand": top["brand"], "pool": "PLACE", "stake": full / 2,
         "pick_rank": 1, "reason": "each_way_place"},
    ]


def _rule_market_fav(rows, params):
    valid = [r for r in rows if r["odds"] and r["odds"] > 0]
    if not valid: return []
    valid.sort(key=lambda r: (r["odds"], r["brand"]))
    fav = valid[0]
    return [{"brand": fav["brand"], "pool": "WIN",
             "stake": params.get("stake", 500.0),
             "pick_rank": 1, "reason": "market_favourite"}]


def _rule_market_blended_top1(rows, params):
    valid = [r for r in rows if r["prob"] is not None and r["odds"] and r["odds"] > 0]
    if not valid: return []
    alpha = float(params.get("alpha", 1.5))
    beta = float(params.get("beta", 0.7))
    # Normalise model prob and market implied prob per race
    fs = [r["prob"] for r in valid]
    s = sum(fs); fs = [f / s for f in fs] if s > 0 else fs
    pis = [1.0 / r["odds"] for r in valid]
    s = sum(pis); pis = [p / s for p in pis] if s > 0 else pis
    scored = []
    for r, f, p in zip(valid, fs, pis):
        blend = alpha * math.log(max(f, 1e-9)) + beta * math.log(max(p, 1e-9))
        scored.append((blend, r["brand"], r))
    scored.sort(key=lambda x: (-x[0], x[1]))
    top = scored[0][2]
    return [{"brand": top["brand"], "pool": "WIN",
             "stake": params.get("stake", 500.0),
             "pick_rank": 1, "reason": f"benter_a={alpha}_b={beta}"}]


# ─── Exotic pool rules (multi-horse) ──────────────────────────────────────
# All exotic bets store `brand` as a comma-separated combination string.
# For UNORDERED pools (QIN, QPL, TRIO, F4) the combo is sorted alphabetically
# to match how `dividends.combination` is stored. For ORDERED pools (EXA,
# TRI, QTT) we store the predicted order (first→last) so settlement can
# verify against actual finish positions; payout lookup then sorts the
# combo to match the dividends table.

def _top_n_by_prob(rows, n: int) -> list:
    valid = [r for r in rows if r["prob"] is not None]
    valid.sort(key=lambda r: (-r["prob"], r["brand"]))
    return valid[:n]


def _rule_quinella_top2(rows, params):
    top = _top_n_by_prob(rows, 2)
    if len(top) < 2: return []
    combo = ",".join(sorted(h["brand"] for h in top))
    return [{"brand": combo, "pool": "QIN",
             "stake": params.get("stake", 100.0),
             "pick_rank": 1, "reason": "qin_top2"}]


def _rule_qpl_top2(rows, params):
    top = _top_n_by_prob(rows, 2)
    if len(top) < 2: return []
    combo = ",".join(sorted(h["brand"] for h in top))
    return [{"brand": combo, "pool": "QPL",
             "stake": params.get("stake", 100.0),
             "pick_rank": 1, "reason": "qpl_top2"}]


def _rule_qpl_top3_box(rows, params):
    """All three pairs from top-3 model picks (3 QPL bets per race)."""
    top = _top_n_by_prob(rows, 3)
    if len(top) < 3: return []
    stake = params.get("stake_per_pair", 100.0)
    out = []
    for i in range(3):
        for j in range(i + 1, 3):
            combo = ",".join(sorted([top[i]["brand"], top[j]["brand"]]))
            out.append({"brand": combo, "pool": "QPL", "stake": stake,
                        "pick_rank": i + 1, "reason": "qpl_box3"})
    return out


def _rule_forecast_top2(rows, params):
    """EXA — top horse for 1st, 2nd for 2nd. Predicted order matters."""
    top = _top_n_by_prob(rows, 2)
    if len(top) < 2: return []
    ordered = f"{top[0]['brand']}>{top[1]['brand']}"   # arrow = predicted order
    return [{"brand": ordered, "pool": "EXA",
             "stake": params.get("stake", 100.0),
             "pick_rank": 1, "reason": "exa_top2_ordered"}]


def _rule_trifecta_top3(rows, params):
    """TRI — top-3 in EXACT predicted order."""
    top = _top_n_by_prob(rows, 3)
    if len(top) < 3: return []
    ordered = ">".join(h["brand"] for h in top)
    return [{"brand": ordered, "pool": "TRI",
             "stake": params.get("stake", 100.0),
             "pick_rank": 1, "reason": "tri_top3_ordered"}]


def _rule_trio_top3(rows, params):
    """TRIO — top-3 in any order."""
    top = _top_n_by_prob(rows, 3)
    if len(top) < 3: return []
    combo = ",".join(sorted(h["brand"] for h in top))
    return [{"brand": combo, "pool": "TRIO",
             "stake": params.get("stake", 100.0),
             "pick_rank": 1, "reason": "trio_top3"}]


def _rule_first_four_top4(rows, params):
    """F4 — top-4 in any order."""
    top = _top_n_by_prob(rows, 4)
    if len(top) < 4: return []
    combo = ",".join(sorted(h["brand"] for h in top))
    return [{"brand": combo, "pool": "F4",
             "stake": params.get("stake", 100.0),
             "pick_rank": 1, "reason": "f4_top4"}]


def _rule_quartet_top4(rows, params):
    """QTT — top-4 in EXACT predicted order."""
    top = _top_n_by_prob(rows, 4)
    if len(top) < 4: return []
    ordered = ">".join(h["brand"] for h in top)
    return [{"brand": ordered, "pool": "QTT",
             "stake": params.get("stake", 100.0),
             "pick_rank": 1, "reason": "qtt_top4_ordered"}]


RULES = {
    "flat_top1": _rule_flat_top1,
    "kelly_top1": _rule_kelly_top1,
    "flat_top1_filtered": _rule_flat_top1_filtered,
    "dutch_topN": _rule_dutch_topN,
    "place_top1": _rule_place_top1,
    "each_way_top1": _rule_each_way_top1,
    "market_fav": _rule_market_fav,
    "market_blended_top1": _rule_market_blended_top1,
    # Exotics
    "quinella_top2": _rule_quinella_top2,
    "qpl_top2": _rule_qpl_top2,
    "qpl_top3_box": _rule_qpl_top3_box,
    "forecast_top2": _rule_forecast_top2,
    "trifecta_top3": _rule_trifecta_top3,
    "trio_top3": _rule_trio_top3,
    "first_four_top4": _rule_first_four_top4,
    "quartet_top4": _rule_quartet_top4,
}


# ─── Driver ────────────────────────────────────────────────────────────────

def _settle_bet(bet: dict, race_rows: list, params: dict,
                dividends: dict | None = None) -> dict:
    """Mutate `bet` with won/payout/pnl based on race_rows positions and
    (for exotics) the dividends table lookup.

    `dividends`: optional {pool: {combination_sorted: dividend_per_$10}}
                 prefetched per race; if None we settle singles only.
    """
    pool = bet["pool"]
    # ─── Singles: WIN / PLACE ───────────────────────────────────────────
    if pool in ("WIN", "PLACE"):
        target = next((r for r in race_rows if r["brand"] == bet["brand"]), None)
        if target is None or target.get("position") is None:
            bet["won"] = -1; bet["payout"] = 0; bet["pnl"] = 0
            bet["odds_at_bet"] = (target or {}).get("odds")
            return bet
        pos = _coerce_position(target["position"])
        odds = _coerce_odds(target.get("odds"))
        bet["odds_at_bet"] = odds
        if pos is None or odds is None:
            bet["won"] = -1; bet["payout"] = 0; bet["pnl"] = 0
            return bet
        won = 0; payout = 0.0
        if pool == "WIN" and pos == 1:
            won = 1; payout = bet["stake"] * odds
        elif pool == "PLACE" and pos in (1, 2, 3):
            won = 1; payout = bet["stake"] * odds * PLACE_PAYOUT_FRACTION
        bet["won"] = won
        bet["payout"] = round(payout, 2)
        bet["pnl"] = round(payout - bet["stake"], 2)
        return bet

    # ─── Exotics: QIN/QPL/EXA/TRI/TRIO/F4/QTT ───────────────────────────
    # Order-aware bets (EXA/TRI/QTT) store combo as "A>B>C" (predicted order);
    # unordered bets (QIN/QPL/TRIO/F4) store sorted "A,B,C".
    brand_str = bet["brand"]
    is_ordered = ">" in brand_str
    picks = brand_str.split(">") if is_ordered else brand_str.split(",")

    # Actual finish order from race_rows: brands sorted by position
    pos_map = {}
    for r in race_rows:
        p = _coerce_position(r.get("position"))
        if p is not None and p >= 1:
            pos_map[r["brand"]] = p
    finishers = sorted(pos_map.items(), key=lambda kv: kv[1])  # [(brand, pos)...]
    finish_order = [b for b, _ in finishers]

    won = 0; payout = 0.0
    if is_ordered:
        # EXA: top 2; TRI: top 3; QTT: top 4
        depth = len(picks)
        if len(finish_order) >= depth and finish_order[:depth] == picks:
            won = 1
    else:
        # Build the actual winning set per pool
        if pool == "QIN":
            actual = set(finish_order[:2])
            won = 1 if actual == set(picks) else 0
        elif pool == "QPL":
            top3 = set(finish_order[:3])
            won = 1 if all(p in top3 for p in picks) else 0
        elif pool == "TRIO":
            actual = set(finish_order[:3])
            won = 1 if actual == set(picks) else 0
        elif pool == "F4":
            actual = set(finish_order[:4])
            won = 1 if actual == set(picks) else 0

    # Payout: look up dividend by canonical (sorted) combination
    if won and dividends is not None:
        canon = ",".join(sorted(picks))
        div = dividends.get(pool, {}).get(canon)
        if div is None:
            # Dividend row not scraped — keep won=1 but mark unsettled payout
            bet["won"] = 1; bet["payout"] = 0; bet["pnl"] = -bet["stake"]
            bet["reason"] = (bet.get("reason") or "") + "|dividend_missing"
            return bet
        # Dividends in HKJC are quoted per $10 base stake
        payout = float(div) * (bet["stake"] / 10.0)

    bet["won"] = won
    bet["payout"] = round(payout, 2)
    bet["pnl"] = round(payout - bet["stake"], 2)
    return bet


def run_for_bet_strategy(conn: sqlite3.Connection, bet_strategy_id: int,
                         date_from: str | None = None,
                         date_to: str | None = None) -> dict:
    """Execute one bet strategy across the model's predictions; replace its
    rows in bet_ledger for the date window."""
    row = conn.execute(
        "SELECT name, model_strategy_id, rule_kind, params_json "
        "FROM bet_strategies WHERE id = ?",
        (bet_strategy_id,),
    ).fetchone()
    if not row:
        raise SystemExit(f"bet_strategy id={bet_strategy_id} not found")
    bname, model_strategy_id, rule_kind, params_json = row
    if rule_kind not in RULES:
        raise SystemExit(f"unknown rule_kind: {rule_kind}")
    rule_fn = RULES[rule_kind]
    params = json.loads(params_json or "{}")

    where = ["p.strategy_id = ?"]
    args_sql: list = [model_strategy_id]
    if date_from:
        where.append("ra.date >= ?"); args_sql.append(date_from)
    if date_to:
        where.append("ra.date <= ?"); args_sql.append(date_to)

    rows = conn.execute(
        f"""
        SELECT p.race_id, ra.date, p.brand, p.calibrated_prob,
               r.odds, r.position
        FROM predictions p
        JOIN races ra ON ra.id = p.race_id
        LEFT JOIN results r ON r.race_id = p.race_id AND r.brand = p.brand
        WHERE {' AND '.join(where)}
        ORDER BY ra.date, p.race_id, p.brand
        """,
        args_sql,
    ).fetchall()

    # Group by race_id, preserving date
    by_race: dict[int, dict] = {}
    for race_id, date, brand, prob, odds, position in rows:
        by_race.setdefault(race_id, {"date": date, "rows": []})["rows"].append({
            "brand": brand,
            "prob": float(prob) if prob is not None else None,
            "odds": _coerce_odds(odds),
            "position": position,
        })

    # Prefetch dividends for every race we'll touch (exotic pools only need
    # this; singles use results.odds directly). Keyed by race_id →
    # {pool: {sorted_combo: dividend}} for O(1) settlement lookup.
    is_exotic_rule = rule_kind in (
        "quinella_top2", "qpl_top2", "qpl_top3_box", "forecast_top2",
        "trifecta_top3", "trio_top3", "first_four_top4", "quartet_top4",
    )
    dividends_by_race: dict[int, dict] = {}
    if is_exotic_rule and by_race:
        race_ids = list(by_race.keys())
        placeholders = ",".join("?" * len(race_ids))
        meta = {rid: by_race[rid]["date"] for rid in race_ids}
        # Bulk-fetch dividends for the date set (faster than per-race query)
        date_set = list({by_race[rid]["date"] for rid in race_ids})
        date_ph = ",".join("?" * len(date_set))
        div_rows = conn.execute(
            f"SELECT date, course, race_no, pool, combination, dividend "
            f"FROM dividends WHERE date IN ({date_ph})",
            date_set,
        ).fetchall()
        race_lookup = {}
        for rid, date in meta.items():
            row = conn.execute("SELECT course, race_no FROM races WHERE id=?", (rid,)).fetchone()
            if row:
                race_lookup[(date, row[0], row[1])] = rid
        for date, course, race_no, pool, combo, div in div_rows:
            rid = race_lookup.get((date, course, race_no))
            if rid is None: continue
            dividends_by_race.setdefault(rid, {}).setdefault(pool, {})[combo] = div

    # Wipe old ledger rows for this bet strategy in the date window
    wipe_args = [bet_strategy_id]
    wipe_where = "bet_strategy_id = ?"
    if date_from:
        wipe_where += " AND race_date >= ?"; wipe_args.append(date_from)
    if date_to:
        wipe_where += " AND race_date <= ?"; wipe_args.append(date_to)
    conn.execute(f"DELETE FROM bet_ledger WHERE {wipe_where}", wipe_args)

    n_races = n_bets = n_wins = 0
    total_stake = total_payout = 0.0
    for race_id, info in by_race.items():
        bets = rule_fn(info["rows"], params)
        if not bets:
            continue
        n_races += 1
        race_dividends = dividends_by_race.get(race_id)
        for bet in bets:
            bet = _settle_bet(bet, info["rows"], params, race_dividends)
            conn.execute(
                """
                INSERT INTO bet_ledger
                  (bet_strategy_id, race_id, race_date, brand, pool, stake,
                   odds_at_bet, won, payout, pnl, pick_rank, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (bet_strategy_id, race_id, info["date"], bet["brand"],
                 bet["pool"], bet["stake"], bet.get("odds_at_bet"),
                 bet["won"], bet["payout"], bet["pnl"],
                 bet.get("pick_rank"), bet.get("reason")),
            )
            n_bets += 1
            if bet["won"] == 1:
                n_wins += 1
                total_payout += bet["payout"]
            total_stake += bet["stake"]
    conn.commit()
    pnl = total_payout - total_stake
    roi = (100.0 * pnl / total_stake) if total_stake > 0 else 0.0
    return {
        "name": bname, "rule_kind": rule_kind,
        "races_with_bet": n_races, "n_bets": n_bets, "n_wins": n_wins,
        "total_stake": round(total_stake, 2),
        "total_payout": round(total_payout, 2),
        "pnl": round(pnl, 2), "roi_pct": round(roi, 2),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--bet-strategy", help="name of bet strategy to run")
    p.add_argument("--all", action="store_true", help="run all enabled bet strategies")
    p.add_argument("--from", dest="date_from", default=None)
    p.add_argument("--to", dest="date_to", default=None)
    ns = p.parse_args()
    conn = _conn()
    if ns.all:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM bet_strategies WHERE enabled = 1 ORDER BY id"
        ).fetchall()]
    elif ns.bet_strategy:
        row = conn.execute("SELECT id FROM bet_strategies WHERE name = ?",
                           (ns.bet_strategy,)).fetchone()
        if not row:
            raise SystemExit(f"bet strategy not found: {ns.bet_strategy}")
        ids = [row[0]]
    else:
        raise SystemExit("specify --bet-strategy <name> or --all")
    for bid in ids:
        out = run_for_bet_strategy(conn, bid, ns.date_from, ns.date_to)
        print(json.dumps(out, indent=2))
    conn.close()


if __name__ == "__main__":
    main()

"""Apply a bet strategy to a model's predictions and write to bet_ledger.

A bet strategy is a post-prediction rule (top-1, top-N, Kelly, place,
market-favourite, etc.). It reads `predictions` for a model_strategy_id
and writes one or more rows to `bet_ledger` per race.

A single model can have many bet strategies layered on top — each is
cheap (O(races) SQL pass, no retraining). The point is to compare
betting rules side-by-side against the SAME race set / SAME model.

Rule kinds & their `params_json` schema:

  flat_top1            { "stake": 500 }
  kelly_top1           { "bankroll": 10000, "kelly_frac": 0.25, "max_pct": 0.05 }
  flat_top1_filtered   { "stake": 500, "min_prob": 0.20?, "max_field": 12? }
  dutch_topN           { "total_stake": 500, "n": 2 }
  place_top1           { "stake": 500 }       (paid 1/4 odds on top-3 by HKJC tote)
  each_way_top1        { "stake": 500 }       (250 win + 250 place)
  market_fav           { "stake": 500 }       (ignore model — lowest odds)
  market_blended_top1  { "stake": 500, "alpha": 1.5, "beta": 0.7 }

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


RULES = {
    "flat_top1": _rule_flat_top1,
    "kelly_top1": _rule_kelly_top1,
    "flat_top1_filtered": _rule_flat_top1_filtered,
    "dutch_topN": _rule_dutch_topN,
    "place_top1": _rule_place_top1,
    "each_way_top1": _rule_each_way_top1,
    "market_fav": _rule_market_fav,
    "market_blended_top1": _rule_market_blended_top1,
}


# ─── Driver ────────────────────────────────────────────────────────────────

def _settle_bet(bet: dict, race_rows: list, params: dict) -> dict:
    """Mutate `bet` with won/payout/pnl based on race_rows positions."""
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
    won = 0
    payout = 0.0
    if bet["pool"] == "WIN":
        if pos == 1:
            won = 1; payout = bet["stake"] * odds
    elif bet["pool"] == "PLACE":
        # Approximate: paid out for top-3 in fields of 8+, top-2 otherwise.
        # We don't know field size from this row alone, so use top-3 as
        # the default (HKJC's typical rule).
        if pos in (1, 2, 3):
            won = 1; payout = bet["stake"] * odds * PLACE_PAYOUT_FRACTION
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
        for bet in bets:
            bet = _settle_bet(bet, info["rows"], params)
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

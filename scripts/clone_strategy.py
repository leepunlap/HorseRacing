"""Clone an existing strategy row into a new row with optional overrides.

Use when you want to snapshot the current production config under a new
name (so you can swap it out without losing the predictions / runs of the
old one), or to set up a candidate variant for side-by-side comparison.

The cloned strategy starts with no predictions; run a walk_forward to
populate them.

Examples:
    # Snapshot current deployed config as the "v7 reference"
    python3 -m scripts.clone_strategy \\
        --from benter_baseline --to benter_v7_70feat_tau180

    # Same but disable τ
    python3 -m scripts.clone_strategy \\
        --from benter_baseline --to benter_v7_no_tau \\
        --set time_decay_tau=NULL

    # Same but turn Benter blend back on
    python3 -m scripts.clone_strategy \\
        --from benter_baseline --to benter_v7_market_blend \\
        --set stage2_enabled=1
"""

from __future__ import annotations
import argparse
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "racing.db"


def _parse_overrides(pairs: list[str]) -> dict[str, object]:
    """`--set col=val` pairs → {col: val}. 'NULL' → None, ints/floats coerced."""
    out: dict[str, object] = {}
    for p in pairs:
        if "=" not in p:
            raise SystemExit(f"bad --set value (need col=val): {p}")
        col, raw = p.split("=", 1)
        col = col.strip(); raw = raw.strip()
        if raw.upper() == "NULL":
            out[col] = None
        else:
            for cast in (int, float):
                try:
                    out[col] = cast(raw)
                    break
                except ValueError:
                    continue
            else:
                out[col] = raw
    return out


def clone(src_name: str, dst_name: str, overrides: dict) -> int:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    src = conn.execute("SELECT * FROM strategies WHERE name = ?", (src_name,)).fetchone()
    if not src:
        raise SystemExit(f"source strategy not found: {src_name}")
    if conn.execute("SELECT 1 FROM strategies WHERE name = ?", (dst_name,)).fetchone():
        raise SystemExit(f"destination already exists: {dst_name}")

    # Copy all columns except `id` (autoincrement) and `name` (taken from dst).
    cols = [c for c in src.keys() if c not in ("id", "name")]
    vals = [src[c] for c in cols]
    for col, val in overrides.items():
        if col in cols:
            vals[cols.index(col)] = val
        else:
            cols.append(col); vals.append(val)

    insert_cols = ", ".join(["name", *cols])
    placeholders = ", ".join(["?"] * (1 + len(cols)))
    conn.execute(
        f"INSERT INTO strategies ({insert_cols}) VALUES ({placeholders})",
        [dst_name, *vals],
    )
    new_id = conn.execute("SELECT id FROM strategies WHERE name = ?", (dst_name,)).fetchone()["id"]
    conn.commit(); conn.close()
    return new_id


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="src", required=True)
    p.add_argument("--to", dest="dst", required=True)
    p.add_argument("--set", action="append", default=[],
                   help="column=value override; repeatable; 'NULL' for null")
    ns = p.parse_args()
    overrides = _parse_overrides(ns.set)
    new_id = clone(ns.src, ns.dst, overrides)
    print(f"cloned {ns.src!r} → {ns.dst!r} (id={new_id})")
    if overrides:
        for k, v in overrides.items():
            print(f"  override: {k} = {v!r}")
    print(f"\nnext: python3 -u -m models.walk_forward --strategy {ns.dst} "
          f"--from <YYYY-MM-DD> --to <YYYY-MM-DD>")


if __name__ == "__main__":
    main()

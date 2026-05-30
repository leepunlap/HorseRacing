"""Eric 定律 (Eric's Laws) — structured handicapping guardrails.

A small, dependency-free accessor over guardrails/eric_laws.json. Each law is a
candidate guardrail (direction + machine-actionable triggers + proposed feature
+ evidence + assessment) that patches a model blind spot around context/condition
change. Laws advance proposed -> validated -> implemented as they're backtested.

    from guardrails import load_laws, get_law, validate
    laws = load_laws()                     # list[dict]
    el = get_law("EL001")
    errors = validate()                    # [] when the data conforms

Add a law by appending an object to eric_laws.json (follow eric_laws.schema.json);
run `python3 -m guardrails` to validate + print a summary.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "eric_laws.json"
SCHEMA = BASE / "eric_laws.schema.json"

_OPS = {"==", "!=", ">", ">=", "<", "<=", "in", "is_true", "is_false"}


def _load() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))


def load_laws() -> list[dict]:
    """All laws, in file order."""
    return _load().get("laws", [])


def get_law(law_id: str) -> dict | None:
    return next((l for l in load_laws() if l["id"] == law_id), None)


def laws_by_direction(direction: str) -> list[dict]:
    """`direction` in {'upgrade','downgrade','either'}."""
    return [l for l in load_laws() if l["direction"] == direction]


def active_guardrails() -> list[dict]:
    """Laws that have graduated past 'proposed' (validated or implemented)."""
    return [l for l in load_laws() if l["status"] in ("validated", "implemented")]


def routing() -> dict:
    """Aggregate each law's `decomposition` into the work-list: which pieces
    become model FEATURES (retrain) vs which stay GUARDRAILS (post-hoc). Each
    item is tagged with its source law id."""
    features: list[dict] = []
    guardrails: list[dict] = []
    for law in load_laws():
        dec = law.get("decomposition") or {}
        for f in dec.get("features", []):
            features.append({"law": law["id"], **f})
        for g in dec.get("guardrails", []):
            guardrails.append({"law": law["id"], **g})
    return {"features": features, "guardrails": guardrails}


def validate() -> list[str]:
    """Lightweight structural check against eric_laws.schema.json's required
    fields + enums (avoids a jsonschema dependency). Returns a list of error
    strings; empty means the data conforms."""
    errors: list[str] = []
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    law_req = schema["$defs"]["law"]["required"]
    data = _load()
    if "meta" not in data or "laws" not in data:
        return ["top-level: missing 'meta' or 'laws'"]
    seen: set[str] = set()
    for i, law in enumerate(data["laws"]):
        tag = law.get("id", f"#{i}")
        for k in law_req:
            if k not in law:
                errors.append(f"{tag}: missing required '{k}'")
        if "id" in law:
            if not re.match(r"^EL[0-9]{3}$", law["id"]):
                errors.append(f"{tag}: id must match EL###")
            if law["id"] in seen:
                errors.append(f"{tag}: duplicate id")
            seen.add(law["id"])
        if law.get("direction") not in ("upgrade", "downgrade", "either"):
            errors.append(f"{tag}: bad direction {law.get('direction')!r}")
        if law.get("status") not in ("proposed", "validated", "implemented", "retired"):
            errors.append(f"{tag}: bad status {law.get('status')!r}")
        for tr in law.get("triggers", []):
            if tr.get("op") not in _OPS:
                errors.append(f"{tag}: trigger op {tr.get('op')!r} not in {_OPS}")
            if tr.get("op") not in ("is_true", "is_false") and "value" not in tr:
                errors.append(f"{tag}: trigger '{tr.get('signal')}' needs a value")
        a = law.get("assessment", {})
        if a.get("validity") not in ("strong", "moderate", "weak"):
            errors.append(f"{tag}: bad assessment.validity")
        if a.get("confidence") not in ("high", "medium", "low"):
            errors.append(f"{tag}: bad assessment.confidence")
    return errors


def _main() -> int:
    errs = validate()
    laws = load_laws()
    print(f"Eric 定律 — {len(laws)} law(s) in {DATA.name}")
    for l in laws:
        a = l["assessment"]
        dec = l.get("decomposition") or {}
        print(f"  {l['id']} [{l['direction']:9}] {l['name_en']:42} "
              f"{a['validity']}/{a['confidence']}  {len(l['triggers'])} triggers  "
              f"{len(l.get('evidence', []))} case(s)  status={l['status']}  "
              f"-> {len(dec.get('features', []))} feature / {len(dec.get('guardrails', []))} guardrail")
    rt = routing()
    print(f"\nrouting: {len(rt['features'])} feature-able component(s) (retrain), "
          f"{len(rt['guardrails'])} guardrail-only component(s) (post-hoc)")
    for g in rt["guardrails"]:
        print(f"  guardrail [{g['kind']}/{g['why_not_feature']}] {g['name']}  ({g['law']})")
    if errs:
        print("\nVALIDATION ERRORS:")
        for e in errs:
            print("  -", e)
        return 1
    print("\nvalidation OK")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())

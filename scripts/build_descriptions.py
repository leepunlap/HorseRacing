#!/usr/bin/env python3
"""Merge sources into features/descriptions.json.

Inputs:
  features/catalog.py           (FEATURES list — authoritative H-id set)
  features/source_bullets.json  (raw Chinese bullets from the doc)
  features/bibliography.json    (B-id → {compact, author, year, title, url})
  features/notes_rich.json      (hand-authored rich descriptions/notes per H-id)

Output:
  features/descriptions.json    (one record per H-id — what the API serves)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from features.catalog import FEATURES  # noqa: E402

BULLETS = json.loads((ROOT / "features" / "source_bullets.json").read_text(encoding="utf-8"))
BIBLIO = json.loads((ROOT / "features" / "bibliography.json").read_text(encoding="utf-8"))
RICH_PATH = ROOT / "features" / "notes_rich.json"
RICH = json.loads(RICH_PATH.read_text(encoding="utf-8")) if RICH_PATH.exists() else {}

OUT = ROOT / "features" / "descriptions.json"


def _bullet_for(name_zh: str) -> dict | None:
    """Match a feature's Chinese name to a doc bullet. The doc bullets often
    have suffix qualifiers like (Sire) / *新增* / *全新類別* that we already
    stripped during extraction; the H-id catalog uses tighter labels. Try a
    few common matching strategies before giving up."""
    if name_zh in BULLETS:
        return BULLETS[name_zh]
    # Try prefix-up-to-first-bracket: "父系" vs "父系（Sire）"
    for stem in (name_zh.split("（")[0], name_zh.split("(")[0]):
        if stem in BULLETS:
            return BULLETS[stem]
    # Try substring match across all bullets — lowest-overlap wins
    matches = [(k, v) for k, v in BULLETS.items() if name_zh in k or k in name_zh]
    return matches[0][1] if matches else None


def _sources(feature) -> tuple[list[str], str]:
    """Return (all_source_ids, compact_label_for_primary_source)."""
    raw = (feature.source_refs or "").strip()
    all_sources = [s.strip() for s in raw.split(",") if s.strip()]
    primary = all_sources[0] if all_sources else None
    compact = ""
    if primary and primary in BIBLIO:
        compact = BIBLIO[primary].get("compact", primary)
    elif primary:
        compact = primary
    return all_sources, compact


def main() -> None:
    out: dict[str, dict] = {}
    missing_doc = 0
    for f in FEATURES:
        bullet = _bullet_for(f.name_zh)
        if bullet is None:
            missing_doc += 1
        desc_zh_from_doc = bullet["description_zh"] if bullet else None
        all_sources, compact = _sources(f)

        rich = RICH.get(f.id, {})

        display_zh = f"{f.name_zh}（{compact}）" if compact else f.name_zh
        display_en = f"{f.name_en} ({compact})" if compact else f.name_en

        out[f.id] = {
            "id": f.id,
            "category": f.category,
            "name_zh": f.name_zh,
            "name_en": f.name_en,
            "display_name_zh": display_zh,
            "display_name_en": display_en,
            "primary_source": all_sources[0] if all_sources else None,
            "all_sources": all_sources,
            # description: prefer rich-authored, fall back to doc bullet, fall back to catalog one-liner
            "description_zh": rich.get("description_zh") or desc_zh_from_doc or f.definition,
            "description_en": rich.get("description_en") or f.definition,
            # notes: rich text only — empty string when nothing authored yet
            "notes_zh": rich.get("notes_zh", ""),
            "notes_en": rich.get("notes_en", ""),
            "enabled_default": bool(f.enabled_default),
            "compute_fn_name": f.compute_fn_name,
        }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    rich_count = sum(1 for v in out.values() if v["notes_en"])
    print(f"wrote {len(out)} feature descriptions → {OUT}")
    print(f"  matched to doc bullet: {len(out) - missing_doc} / {len(out)}")
    print(f"  with rich hand-authored notes: {rich_count} / {len(out)}")


if __name__ == "__main__":
    main()

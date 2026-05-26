#!/usr/bin/env python3
"""One-shot extractor: parse userdocs/features_expanded_zh_hant.md and
userdocs/global_research.md and emit a structured JSON we can use as the
source-of-truth for the Features tab in the SPA.

Outputs:
  features/source_bullets.json  — every Chinese feature bullet from the doc
                                      keyed by name, with description + B-refs
  features/bibliography.json   — B-id → {compact, full, url, year} for all
                                      53 references in Appendix B
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_ZH = ROOT / "userdocs" / "features_expanded_zh_hant.md"
OUT_BULLETS = ROOT / "features" / "source_bullets.json"
OUT_BIB = ROOT / "features" / "bibliography.json"


def parse_bibliography(text: str) -> dict[str, dict]:
    """Pull out the `- [B1] author. (year). *title*. url` lines from Appendix B."""
    out: dict[str, dict] = {}
    pat = re.compile(r"^- \[(B\d+)\]\s+(.+)$", re.MULTILINE)
    for m in pat.finditer(text):
        bid = m.group(1)
        body = m.group(2).strip()
        # Year — first 4-digit number inside parens anywhere in the entry
        ym = re.search(r"\((\d{4})\)", body)
        year = int(ym.group(1)) if ym else None
        # Author — text before the year or before the first sentence-end ".".
        # The doc style is "Author, F. (year). *Title*. ..." or
        # "OrgName. *Title*. ..." (no year, no comma-initial).
        author = ""
        if year:
            author = body.split(f"({year})")[0].rstrip(". ").strip()
        else:
            # No year — take text up to the first ". *" (period before italic title)
            am = re.search(r"^(.+?)\.\s*\*", body)
            author = am.group(1).strip() if am else body.split(".", 1)[0].strip()
        # Title — first *italic* block
        tm = re.search(r"\*([^*]+)\*", body)
        if tm:
            title = tm.group(1).strip()
        else:
            rest = body.split(".", 1)[-1]
            title = rest.split("http")[0].strip(" .").strip()
        # URL — first http(s) link
        um = re.search(r"(https?://\S+)", body)
        url = um.group(1).strip(" .,;)") if um else ""
        # Compact label: surname (everything before first comma) + year if known
        compact_author = author.split(",")[0].strip() if "," in author else author
        compact = f"{compact_author} {year}" if year else compact_author
        out[bid] = {
            "id": bid,
            "compact": compact,
            "author": author,
            "year": year,
            "title": title,
            "url": url,
            "raw": body,
        }
    return out


def parse_bullets(text: str) -> dict[str, dict]:
    """Walk each `## N. category` section and pull every `- **name** — desc [Brefs]` bullet.
    Returns {chinese_name: {category_id, category_name_zh, description_zh, brefs[], section}}."""
    out: dict[str, dict] = {}
    # Split into category sections — each starts with `## N. name`
    cat_pat = re.compile(r"^## (\d+)\.\s+(.+)$", re.MULTILINE)
    matches = list(cat_pat.finditer(text))
    sections: list[tuple[int, str, str]] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        cat_id = int(m.group(1))
        cat_name = m.group(2).strip().split()[0]
        sections.append((cat_id, cat_name, text[start:end]))

    # Bullet shape:
    #   - **{name}** [optional *新增*] — {description} [Brefs]。
    bullet_pat = re.compile(
        r"^\s*-\s*\*\*([^\*]+)\*\*\s*(?:\*[^*]+\*\s*)?[—\-–:]\s*(.+?)\s*$",
        re.MULTILINE,
    )
    bref_pat = re.compile(r"\[(B\d+)\]")

    for cat_id, cat_name, body in sections:
        # Determine the "section" each bullet sits in (既有特徵 vs 擴充建議)
        sub_pat = re.compile(r"^### (.+)$", re.MULTILINE)
        sub_matches = list(sub_pat.finditer(body))
        sub_sections: list[tuple[str, str]] = []
        for i, sm in enumerate(sub_matches):
            s = sm.end()
            e = sub_matches[i + 1].start() if i + 1 < len(sub_matches) else len(body)
            sub_sections.append((sm.group(1).strip(), body[s:e]))

        for sub_name, sub_body in sub_sections:
            if sub_name not in ("既有特徵", "擴充建議"):
                continue
            for bm in bullet_pat.finditer(sub_body):
                name = bm.group(1).strip()
                desc = bm.group(2).strip()
                # Strip the trailing 。 / period if present
                desc = re.sub(r"\s*。\s*$", "", desc)
                # Pull B-refs out of desc into their own field
                brefs = bref_pat.findall(desc)
                desc_clean = bref_pat.sub("", desc).strip().rstrip(" .,;")
                # If multiple bullets share the same Chinese name (rare), keep the longer one
                if name in out and len(out[name]["description_zh"]) > len(desc_clean):
                    continue
                out[name] = {
                    "name_zh": name,
                    "category_id": cat_id,
                    "category_name_zh": cat_name,
                    "section": sub_name,
                    "description_zh": desc_clean,
                    "brefs": brefs,
                }
    return out


def main() -> None:
    text = SRC_ZH.read_text(encoding="utf-8")
    biblio = parse_bibliography(text)
    bullets = parse_bullets(text)
    OUT_BULLETS.write_text(
        json.dumps(bullets, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    OUT_BIB.write_text(
        json.dumps(biblio, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"parsed {len(bullets)} feature bullets → {OUT_BULLETS}")
    print(f"parsed {len(biblio)} bibliography refs → {OUT_BIB}")
    # Coverage sanity-check: how many bullets are in each category?
    by_cat: dict[int, int] = {}
    for b in bullets.values():
        by_cat[b["category_id"]] = by_cat.get(b["category_id"], 0) + 1
    for cat_id in sorted(by_cat):
        print(f"  cat {cat_id}: {by_cat[cat_id]} bullets")


if __name__ == "__main__":
    main()

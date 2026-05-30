"""Merge the agent-enriched feature descriptions into features/descriptions.json.

Reads /tmp/desc_out_{0..5}.json (each {fid: {description_zh, description_en,
name_zh?}}) and folds the richer description_zh/description_en onto the canonical
catalog. Chinese names for the Latin-only features are taken from a curated map
here (NOT from the agents) so proper-noun translations stay correct.

Usage: python3 -m scripts.merge_descriptions
"""
from __future__ import annotations

import json
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DESC = BASE / "features" / "descriptions.json"

# Curated Traditional-Chinese names for the 22 features whose name_zh was
# previously Latin-only. Proper nouns / acronyms kept in parentheses.
CURATED_NAME_ZH = {
    "H013": "血統速耐指數（Dosage Index）",
    "H022": "投資回報率（ROI）",
    "H024": "影響值（Impact Value）",
    "H077": "分級賽標記（Group/Listed）",
    "H106": "喉鳴手術標記（Roarer）",
    "H123": "拜耳速度指數（Beyer）",
    "H124": "Timeform 大師評分",
    "H125": "Racing Post 評分（RPR）",
    "H126": "Brisnet 綜合力量評分",
    "H127": "Brisnet 班次評分",
    "H128": "最高速度評分（Topspeed）",
    "H129": "Equibase 速度數字",
    "H130": "Equibase 步速數字",
    "H131": "Ragozin 速度紙",
    "H132": "加權實際／預期值（A/E）",
    "H147": "Harville 前二機率",
    "H148": "Harville 前三機率",
    "H150": "Plackett-Luce 似然值",
    "H151": "折讓 Harville 機率",
    "H161": "市場抽水率（Overround）",
    "H162": "Betfair 終場價（BSP）",
    "H163": "內部收盤價值（CLV）",
}


def main() -> None:
    desc = json.loads(DESC.read_text(encoding="utf-8"))
    merged = 0
    name_fixed = 0
    missing: list[str] = []

    for i in range(6):
        p = Path(f"/tmp/desc_out_{i}.json")
        if not p.exists():
            raise SystemExit(f"missing agent output {p}")
        out = json.loads(p.read_text(encoding="utf-8"))
        for fid, rec in out.items():
            if fid not in desc:
                continue
            dz = (rec.get("description_zh") or "").strip()
            de = (rec.get("description_en") or "").strip()
            if dz:
                desc[fid]["description_zh"] = dz
            if de:
                desc[fid]["description_en"] = de
            if dz and de:
                merged += 1

    # Curated names override (authoritative — independent of the agents).
    for fid, name in CURATED_NAME_ZH.items():
        if fid in desc:
            desc[fid]["name_zh"] = name
            # keep display_name_zh in sync if it was the bare Latin term
            if not any("一" <= ch <= "鿿" for ch in desc[fid].get("display_name_zh", "")):
                desc[fid]["display_name_zh"] = name
            name_fixed += 1

    # Sanity: every feature should now have a CJK name and a non-trivial desc.
    for fid, rec in desc.items():
        if not any("一" <= ch <= "鿿" for ch in rec.get("name_zh", "")):
            missing.append(fid)

    DESC.write_text(json.dumps(desc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"merged descriptions: {merged}/{len(desc)}")
    print(f"curated zh names applied: {name_fixed}")
    print(f"still missing CJK name: {missing or 'none'}")


if __name__ == "__main__":
    main()

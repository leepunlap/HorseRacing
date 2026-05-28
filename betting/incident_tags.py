"""Stewards'-report incident tokeniser.

HKJC publishes free-text stewards' commentary for every horse via the
"Race Report" page (scraped into `incident_reports.incident`). The free
text contains recurring trip-trouble phrases that map to a small set of
structured tags useful as model features:

  bumped, steadied, crowded, hampered, slow_to_begin, raced_wide,
  raced_keenly, ran_off, head_up, bled, blood_in_mouth, vet_inspection,
  sent_for_sampling, lame, roarer, reluctant_to_load, withdrew, fell,
  gear_change, eased_late

`tag_incident(text)` returns the sorted comma-separated list of tags
present in one incident string (or `None` when the input is empty / no
tags match). The same function powers:

  - `scrape_incident_reports.py` (writes `incident_tags` column on
    insert)
  - the H107 "off-vet returner" / trip-trouble feature family in
    `features/compute.py`
  - any future post-mortem audit module that wants structured
    interference signals.

Token coverage was measured on 20,000 historical rows: the top 9 tags
(vet_inspection, bumped, sent_for_sampling, steadied, ran_off, crowded,
raced_keenly, raced_wide, head_up) each appear in 3–31% of rows. The
remaining tags are rarer but kept for completeness.
"""

from __future__ import annotations

import re

# (tag_name, compiled_pattern) — case-insensitive match anywhere in the
# stewards' free text. Ordering doesn't matter; results are sorted.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("slow_to_begin",      re.compile(r"slow to begin|slowly away|missed the kick", re.I)),
    ("bumped",             re.compile(r"bumped|interfered with|made contact", re.I)),
    ("steadied",           re.compile(r"steadied|checked|had to take up", re.I)),
    ("crowded",            re.compile(r"crowded|short of room|tightened(?: for room)?", re.I)),
    ("hampered",           re.compile(r"hampered|got into a tight spot", re.I)),
    ("raced_wide",         re.compile(r"raced wide|wide(?:r)? than ideal|caught wide", re.I)),
    ("raced_keenly",       re.compile(r"raced keenly|hard to settle|pulled hard|over[- ]?raced", re.I)),
    ("bled",               re.compile(r"\bbled\b|epistaxis|nose ?bleed|blood from the nose", re.I)),
    ("blood_in_mouth",     re.compile(r"blood in the mouth|abrasions in the mouth", re.I)),
    ("lame",               re.compile(r"\blame\b|lameness|hopping lame|going short", re.I)),
    ("roarer",             re.compile(r"roarer|abnormal respiratory|wind operation", re.I)),
    ("reluctant_to_load",  re.compile(r"reluctant to load|fractious in the (barrier|gate)|delayed start|refused to enter", re.I)),
    ("sent_for_sampling",  re.compile(r"sent for sampling|positive sample|\bswab\b", re.I)),
    ("vet_inspection",     re.compile(r"veterinary (officer|inspection|examination)|vet check", re.I)),
    ("withdrew",           re.compile(r"\bwithdrew\b|\bwithdrawn\b|scratched(?: from the race)?", re.I)),
    ("fell",               re.compile(r"\bfell\b|\bpulled up\b|\bbrought down\b|unseated", re.I)),
    ("gear_change",        re.compile(r"changed (gear|equipment)|added (?:blinkers|tongue tie|cross)|removed (?:blinkers|tongue tie)", re.I)),
    ("eased_late",         re.compile(r"eased (?:down |off )?late|stopped quickly|weakened (?:in the run home|noticeably)", re.I)),
    ("ran_off",            re.compile(r"ran off|deviated|laid (?:in|out)|lay (?:in|out)", re.I)),
    ("head_up",            re.compile(r"got its head up|put its head up|head carriage", re.I)),
]


def tag_incident(text: str | None) -> str | None:
    """Return sorted comma-joined tags for one stewards'-report string,
    or None if no tags match. Stable, deterministic output."""
    if not text:
        return None
    found = sorted(name for name, pat in _PATTERNS if pat.search(text))
    return ",".join(found) if found else None

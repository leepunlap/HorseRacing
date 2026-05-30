# Eric 定律 — model guardrails

Structured, version-controlled handicapping principles distilled from expert
("Eric") reads where the model's **raw-form** view fell short. Each law captures a
nuance the model currently misses around **context & condition change** — surface,
rating trajectory, weight swings — and is a candidate **guardrail**: a correction
the model can eventually apply on top of its base prediction.

## Files
| File | What |
|---|---|
| `eric_laws.schema.json` | JSON Schema (draft 2020-12) defining a law |
| `eric_laws.json` | The laws + the analyst's assessments (the store) |
| `__init__.py` | Dependency-free loader + validator (`load_laws`, `get_law`, `validate`) |

```bash
python3 -m guardrails        # validate + print a summary
```
```python
from guardrails import load_laws, get_law, laws_by_direction, active_guardrails
```

## A law, at a glance
- **direction** — `upgrade` / `downgrade` / `either` (which way it moves a score).
- **principle / rationale** — Eric's rule and why it works (bilingual).
- **triggers** — machine-actionable conditions (`signal op value`) that detect when
  the law applies. Triggers are AND'd; triggers sharing a `group` are OR'd within
  that group. These are the seed for the eventual feature/filter.
- **model_blind_spot** — what the model misses today.
- **proposed_feature** — high-level summary of how to encode it.
- **decomposition** — the explicit routing: which pieces become **model features**
  (`features[]` — abundant, per-horse, generalizable; add to the catalog + retrain)
  vs which stay **guardrails** (`guardrails[]` — post-hoc; each tagged with
  `why_not_feature` = `sparse` / `relational` / `safety` / `interpretability`).
  `routing()` aggregates these across all laws into one work-list.
- **evidence** — the concrete cases (race, horse, model vs market view, outcome,
  who was right) that motivated the law.
- **assessment** — the analyst's take: `validity` (strong/moderate/weak),
  `confidence` (high/medium/low — low when few cases), `severity` (how badly the
  model errs when ignored), caveats, recommended action.
- **status** — lifecycle below.

## Lifecycle
`proposed` → `validated` → `implemented` → (`retired`)

A law starts **proposed** (logged from a case). It becomes **validated** once a
backtest confirms it improves calibration / top-1 / top-3 without harming the rest,
then **implemented** when wired into the pipeline (feature, filter, or post-hoc
correction). `active_guardrails()` returns validated + implemented laws.

## Adding a law
Append an object to `eric_laws.json` with the next id (`EL003`, …), fill the
required fields per the schema, set `status: "proposed"`, and run
`python3 -m guardrails` to validate. Keep both `_zh` and `_en` populated.

## Current laws
| id | dir | name | validity / confidence | status |
|---|---|---|---|---|
| EL001 | ↓ | 異面升班懲罰 — Surface-switch & rating-ceiling penalty | strong / medium | proposed |
| EL002 | ↑ | 進步馬與讓磅互換 — Progressive horse & head-to-head weight swing | strong / high | proposed |

> Common thread of the cases so far: the model reads **raw recent form/positions**
> well but under-encodes **trajectory and condition change** (surface turf-vs-AWT,
> rating slope / class ceiling, pairwise weight swings). These guardrails patch
> exactly that layer.

## Feature vs guardrail — the routing
Most of what these laws capture should become **features** (let the model learn the
weight); only a thin layer stays **guardrails**. A piece stays a guardrail when it
is **sparse** (too few examples to learn), **relational/field-dependent** (depends
on the specific other runners — an independent per-horse ranker can't see it),
**safety** (a hard cap/veto to bound a tail error regardless of what the model
learned), or needs **interpretability**. Run `python3 -m guardrails` to see the
current split. As of v1.1: **4 feature-able components** (surface-split win-rate,
rating slope/ceiling, progressive slope, beaten-margin quality) and **2
guardrail-only components** — `overconfident_switcher_cap` (safety, EL001) and
`h2h_weight_swing_reconciliation` (relational, EL002).

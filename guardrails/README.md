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
- **proposed_feature** — how to encode it (`feature`/`filter`/`penalty`/`boost`),
  its inputs, and related existing catalog features (H###).
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

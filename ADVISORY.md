# Advisory: Cold-Start Problem & Exotic Bet Mathematics

> Part of the documentation set. **README.md** is the entry point;
> **USAGE.md** is the operator's manual; **ARCHITECTURE.md** defines
> the strategy-vs-tuning distinction; **STRATEGIES.md** is the strategy
> recipe book. This document covers the deeper mathematics.

## Part 1 — Cold-Start Problem

### The Current Situation

Every win-rate feature (horse_wr, jockey_wr, trainer_wr, jt_pair, jh_pair, dist_adapt, going_adapt) defaults to **0.0** when there is no history. This is wrong in both directions:

- A debuting horse trained by the best stable in Hong Kong should NOT score the same as a chronic loser.
- A 0.0 jockey_wr for an apprentice on debut is a false signal. The model treats it as "this jockey never wins" rather than "unknown".
- 0.0 for jt_pair penalises a perfectly valid new booking.

### The Fix: Bayesian Shrinkage

Instead of: `rate = wins / races` (returns 0 with no data)

Use: `smoothed_rate = (wins + α × prior) / (races + α)`

Where:
- `prior` = the best available proxy rate (see table below)
- `α` = "virtual races" — how strongly we trust the prior vs observed data

| Feature | Prior to use | α (virtual races) | Rationale |
|---------|-------------|-------------------|-----------|
| `horse_wr` | trainer_wr | 5 | Trainer is the best judge of a debut horse |
| `jockey_wr` | season average (~10%) | 20 | Jockeys need many rides to show true rate |
| `trainer_wr` | field average (~8%) | 30 | Trainers are slow-moving |
| `jt_pair` | geometric_mean(j_wr, t_wr) | 10 | Combination inherits both individual rates |
| `jh_pair` | mean(j_wr, horse_wr) | 3 | Horse-jockey synergy reveals quickly |
| `dist_adapt` | horse_wr | 5 | Assume overall ability transfers to new dist |
| `going_adapt` | horse_wr | 3 | Few horses are strongly ground-dependent |

**Example: debut horse, top trainer**
- trainer_wr = 0.18, horse has 0 races, α = 5
- Before: horse_wr = 0.0 (penalised)
- After: horse_wr = (0 + 5 × 0.18) / (0 + 5) = **0.18**
- After 5 races, 1 win: horse_wr = (1 + 5 × 0.18) / (5 + 5) = **0.19** (blend of observed + prior)
- After 20 races, 2 wins: horse_wr = (2 + 5 × 0.18) / (20 + 5) = **0.116** (observed dominates)

### What to Deduce for Completely New Entities

**Brand-new horse (debut):**

| Known signal | What it tells you |
|---|---|
| Trainer win rate | Best proxy for horse quality — trainer chose this race, this distance |
| Jockey booked | Top jockey (e.g. Purton) = trainer is confident. Apprentice = lesser expectation |
| Rating assigned | HKJC handicapper's assessment; new horses start around 40. Higher start = more ability |
| Class chosen | Trainer enters where they think the horse can compete |
| Distance | Trainers match distance to the horse's physique — first choice is usually ideal |
| Age | 3yo in September = early in career, often improving. 6yo with 0 wins = red flag |

**Practical: for debut horse, use these proxies:**
```
horse_wr     = trainer_wr  (smoothed, α=5)
jh_pair      = jockey_wr   (smoothed, α=3, no horse history yet)
dist_adapt   = trainer_wr  (trainer chose this distance deliberately)
going_adapt  = trainer_wr  (trainer chose today's going)
```

**New jockey:**
- Use HKJC apprentice grade win rate by category (Grade 1/2/3 apprentice)
- Apprentices have a weight allowance (3–10 lbs), which is a direct statistical advantage
- The allowance is already in the `weight` feature, but not the jockey quality proxy
- Prior = 0.07 (apprentice base), α = 20

**New trainer:**
- No good proxy within the codebase
- Use HKJC stable average: ~8–10%
- α = 30 (very slow to trust)

**New combination (jt_pair, jh_pair):**
```python
# Geometric mean prior is better than arithmetic for rates
import math
jt_prior = math.sqrt(jockey_wr * trainer_wr)  if jockey_wr and trainer_wr else max(jockey_wr, trainer_wr, 0.06)
```

The geometric mean is better because both parties need to contribute — a pair where one is 0% and one is 20% isn't really a 10% pair; it's limited by the weaker party.

### Implementation Sketch

In `backtest.py`, replace the current `compute_win_rates` pattern:

```python
# Current (wrong for cold start):
horse_wr = HS[brand]['w'] / max(HS[brand]['r'], 1)

# Fixed with shrinkage:
def smoothed(wins, races, prior, alpha):
    return (wins + alpha * prior) / (races + alpha)

h = HS[brand]
t_wr = TS[trainer]['w'] / max(TS[trainer]['r'], 1)     # trainer already computed
horse_wr = smoothed(h['w'], h['r'], t_wr, alpha=5)

# JT pair: use geometric mean of individuals as prior
j_wr = JS[jockey]['w'] / max(JS[jockey]['r'], 1)
jt_prior = (j_wr * t_wr) ** 0.5 if j_wr > 0 and t_wr > 0 else max(j_wr, t_wr, 0.06)
jt_pair  = smoothed(JTS[jt_key]['w'], JTS[jt_key]['r'], jt_prior, alpha=10)
```

The `alpha` values should themselves be tunable parameters in `config.json`:
```json
"shrinkage": {
  "horse_alpha":  5,
  "jockey_alpha": 20,
  "trainer_alpha": 30,
  "jt_alpha":     10,
  "jh_alpha":     3,
  "dist_alpha":   5,
  "going_alpha":  3
}
```

### Important Note on XGBoost and Zero Features

XGBoost handles zeros specially. A feature that is exactly 0.0 when there is "no data" conflates two very different states:
- "This horse has never won" (20+ races, 0 wins)
- "We don't know anything about this horse"

The model will learn from the 0.0 pattern in training data, but that learning is based on horses with known history. Applying it to debut horses is extrapolation, not interpolation.

Bayesian smoothing collapses this distinction into a single better-calibrated number, which is the right approach here. A more sophisticated alternative is to add an `is_debut` binary feature per entity type (horse, jockey, trainer), but shrinkage is simpler and more principled.

---

## Part 2 — Exotic Bet Mathematics

### HKJC Pool Takes (Vigorish)

| Bet Type | Chinese | Pool Take | Meaning |
|----------|---------|-----------|---------|
| Win (獨贏) | 獨贏 | 17.5% | Back one horse to win |
| Place (位置) | 位置 | 22% | Finish in top 2 (<8 runners) or top 3 (≥8) |
| Quinella (Q) | 連贏 | 18% | Pick two horses to fill 1st + 2nd, any order |
| Quinella Place (QP) | 孖Q | 18% | Pick two horses both to finish in top 3 |
| Trio (三重彩) | 三重彩 | 25% | Pick three horses to fill top 3, any order |
| Trifecta (單T) | 單T | 25% | Pick three horses in exact 1st/2nd/3rd order |
| First 4 (四重彩) | 四重彩 | 25% | Pick four horses to fill top 4, any order |

The take is the dead weight against you. You need enough model edge to overcome it. Win (17.5%) and Q (18%) are the most efficient pools; Trio/Trifecta/First4 (25%) require much larger edge.

### The Harville Formula

Given model win probabilities p₁, p₂, ..., pₙ for an n-horse race (summing to 1.0):

**Conditional probability of finishing 2nd** (horse j, given horse i won):
```
P(j 2nd | i 1st) = pⱼ / (1 − pᵢ)
```

**Conditional probability of finishing 3rd** (horse k, given i 1st and j 2nd):
```
P(k 3rd | i 1st, j 2nd) = pₖ / (1 − pᵢ − pⱼ)
```

This is the Harville formula (1973). It assumes independence of residual win probabilities after removing a winner — a simplifying assumption that works well in practice for races with 8+ runners.

---

### Win (獨贏)

Already implemented. `edge = win_prob × decimal_odds`. Bet when `edge > 1.0`.

---

### Quinella / Q (連贏) — Best Exotic for This Model

**What it is:** Two horses to finish 1st and 2nd in ANY order.

**Probability formula (Harville):**
```
P(Q[i,j]) = pᵢ × pⱼ/(1−pᵢ)  +  pⱼ × pᵢ/(1−pⱼ)
           = pᵢ × pⱼ × [1/(1−pᵢ) + 1/(1−pⱼ)]
```

**Example:** p_A = 0.30, p_B = 0.20 in an 8-horse race:
```
P(Q[A,B]) = 0.30 × 0.20/(1−0.30)  +  0.20 × 0.30/(1−0.20)
           = 0.30 × 0.286  +  0.20 × 0.375
           = 0.0857  +  0.0750  =  0.1607
```
Fair Q odds = 1/0.1607 = 6.2x. After 18% take: minimum payout to break even = 6.2 / 0.82 = 7.6x.

If market pays 9.0x on this Q: edge = 0.1607 × 9.0 = **1.45** → strong bet.

**Why Q is better than two separate Win bets when you have edge on both:**

If horse A has edge = 1.4 and horse B has edge = 1.3 (both positive EV):
- Win A + Win B: two bets, two separate risks, total stake = 2 units
- Q A+B: one bet covering both scenarios — your edge compounds

The Q edge ≈ edge_A × edge_B approximately (not exact, but intuitive). When both horses are undervalued, the Q payout is doubly underpriced because the market is wrong in two places simultaneously.

**Implementation for Q:** For each race, compute P(Q[i,j]) for all pairs, compare to market Q odds, bet where `Q_prob × Q_odds > 1.0`. Focus on pairs where BOTH horses independently have win edge > 1.0.

---

### Quinella Place / QP (孖Q)

**What it is:** Both horses to finish in the top 3 (any positions).

**Probability formula:**
```
P(QP[i,j]) = P(i 1st) × P(j 2nd or 3rd | i 1st)
           + P(j 1st) × P(i 2nd or 3rd | j 1st)
           + Σₖ≠ᵢ,ⱼ P(k 1st) × P(i and j in 2nd+3rd | k 1st)

The last term expanded:
P(i 2nd, j 3rd | k 1st) + P(j 2nd, i 3rd | k 1st)
= pᵢ/(1−pₖ) × pⱼ/(1−pₖ−pᵢ)  +  pⱼ/(1−pₖ) × pᵢ/(1−pₖ−pⱼ)
```

QP probabilities are always higher than Q probabilities (more ways to win), but payouts are lower. The take is the same (18%). QP is useful in wide-open races where your two selection horses are likely to land in the frame even if neither wins.

---

### Place (位置)

**Probability formula (Harville, 8+ runner race — top 3):**
```
P(place[i]) = pᵢ  (wins)
            + Σⱼ≠ᵢ pⱼ × pᵢ/(1−pⱼ)  (2nd: j wins, i is best of rest)
            + Σⱼ≠ᵢ Σₖ≠ᵢ,ⱼ pⱼ × pₖ/(1−pⱼ) × pᵢ/(1−pⱼ−pₖ)  (3rd)
```

**Why Place is usually NOT worth betting:**

The pool take is 22% vs 17.5% for Win. Place pays roughly `odds / 3` for a 3-place horse, but the probability is roughly `3 × p_win`, so:

```
Place edge = P(place) × place_odds
           ≈ 3 × p_win × (win_odds / 3)
           = p_win × win_odds
           = win edge
```

Same expected value as the Win bet, but with a HIGHER vig (22% vs 17.5%). Place is strictly worse in terms of pool efficiency unless:
1. The Place odds are disproportionately high (market is inefficient in the place pool)
2. The horse is a genuine certainty to place but not to win (e.g. finishing form is very consistent)

**Rule of thumb:** Only bet Place when place_edge / win_edge > 1.05 (i.e. place pool is at least 5% more efficient than win pool for this horse). This is rare.

---

### Trio / Trifecta

These are high-variance bets requiring 3 correct selections. The 25% pool take is very steep — you need model edge of >1.33 just to break even on expected value before the edge component. These are appropriate only for:
- Races where your model is extremely confident on 3 horses (top-3 prob sum > 0.6)
- "Banker" situations: 1 near-certainty + 2 live others

Not recommended as a primary strategy. If you chase these, use small "insurance" stakes only.

---

## Part 3 — Recommended Strategy

### Priority Order

| Priority | Bet Type | Why |
|----------|----------|-----|
| 1 | Win (獨贵) | Lowest take, most liquid, model directly trained on this |
| 2 | Q (連贏) | 18% take, stacks edge when two horses are undervalued |
| 3 | QP (孖Q) | Higher probability, lower payout — useful in wide-open races |
| Skip | Place (位置) | Higher take than Win for equivalent EV |
| Occasional | Trio (三重彩) | Only with 3 strong selections, small stake |

### Bet Sizing: Kelly Criterion

For a single bet:
```
Kelly fraction = (p × b − 1) / (b − 1)
```
Where `p` = model probability, `b` = decimal payout.

**Example:** p = 0.25, odds = 6.0x
```
Kelly = (0.25 × 6 − 1) / (6 − 1) = (1.5 − 1) / 5 = 0.10 = 10% of bankroll
```

**Use fractional Kelly:** Full Kelly is theoretically optimal but leads to ruinous drawdowns. Use **1/4 Kelly** (2.5% here) for a practical approach:
- Full Kelly: maximises long-run growth rate
- 1/2 Kelly: ~75% of growth rate, much lower drawdown
- 1/4 Kelly: ~55% of growth rate, very conservative — recommended when model calibration is uncertain

For Q bets, the variance is higher than Win, so use **1/6 to 1/8 Kelly**.

### When to Bet Q

Bet Q on pair (A, B) when:
1. Both A and B have individual win edge > 1.0
2. P(Q[A,B]) × Q_market_odds > 1.0 (positive EV)
3. Q edge > 1.2 (allow for Harville formula error and pari-mutuel uncertainty)
4. The pair's probabilities are not extremely correlated (avoid betting the two short-priced co-favourites — the Q market is efficient for obvious pairs)

The sweet spot is: one strong favourite (p ≈ 0.30–0.40) plus one value horse (p ≈ 0.12–0.18 but undervalued). The market Q price often lags because bettors focus on the favourite's win price and don't correctly price the conditional probability.

### Harville Bias Correction

The Harville formula slightly underestimates the probability of longshots finishing 2nd/3rd (because real-world race finishing order has more "upset" variance than Harville assumes). 

A simple correction: multiply the Harville-derived 2nd/3rd probabilities for longer-priced horses by a factor of 1.05–1.10. This is minor and can be added as a tunable `harville_correction` parameter.

---

## Part 4 — Implementation Roadmap

### Phase 1: Bayesian shrinkage (addresses cold start)
- Add `shrinkage` block to `config.json`
- Modify `compute_win_rates()` in `backtest.py` to apply `smoothed()` formula
- Rerun backtest to see accuracy improvement on races with debut horses

### Phase 2: Quinella probability output
- Add `compute_quinella_probs(horses)` function using Harville formula
- Output top-N Q pairs per race in `predictions.json`
- Add Q edge calculation: `q_edge = q_prob × q_market_odds` (q_market_odds not yet scraped)

### Phase 3: Scrape exotic market odds
- Extend `scrape_results.py` to grab Q/QP market dividends after race
- This enables backtesting Q edge (comparing Harville price to market price)

### Phase 4: Calibration check
- Plot predicted win_prob vs actual win frequency (reliability diagram)
- If model is over/under-confident, apply Platt scaling or isotonic regression
- Well-calibrated win probs = more reliable Harville Q estimates

---

## Summary of Key Formulas

```python
# Bayesian shrinkage
def smoothed(wins, races, prior, alpha):
    return (wins + alpha * prior) / (races + alpha)

# Harville — Quinella probability
def q_prob(pi, pj):
    return pi * pj / (1 - pi) + pj * pi / (1 - pj)

# Harville — Trifecta probability (exact order: i 1st, j 2nd, k 3rd)
def trifecta_prob(pi, pj, pk):
    return pi * (pj / (1 - pi)) * (pk / (1 - pi - pj))

# Trio probability (any order in top 3) — sum all 6 permutations
from itertools import permutations
def trio_prob(pi, pj, pk):
    total = 0
    for a, b, c in permutations([pi, pj, pk]):
        total += trifecta_prob(a, b, c)
    return total

# Kelly criterion (use fractional: multiply by 0.25)
def kelly(p, b, fraction=0.25):
    return fraction * (p * b - 1) / (b - 1)

# Q edge
def q_edge(pi, pj, q_market_odds):
    return q_prob(pi, pj) * q_market_odds
```

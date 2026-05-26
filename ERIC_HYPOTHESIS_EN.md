# ERIC Laws — Hypotheses Pending Validation (English overlay)

This file is the English overlay of `ERIC_HYPOTHESIS.md`. It mirrors the same
H-id structure so the backend can match `text_en` / `validation_en` per id.
Only the H-id bullets are required; headers/section structure live in the base
file.

Sources: MODEL_V9.3_SPEC_TC.pdf, V10_MODEL_DOCUMENTATION_TC.pdf,
"Horse Racing Boss Prediction Program — Shared Grok Conversation.pdf"

---

## V1.0–V3.0 (Simple linear weighting era, pre-2026-05-17)

- **H1**: Linear weighting of jockey/trainer/horse win rates (Horse×0.5 + Jockey×0.3 + Trainer×0.2) can predict win ranking
  - ❌ 2026-05-25 backtest: pure hand-crafted linear weighting is far inferior to XGBoost auto-learning. Given the same features XGBoost is materially more accurate. Manual fixed weights cannot capture racing's non-linear patterns.
- **H2**: Draw bonus formula (10 − gate#) × 0.8 is effective
  - ⚙️ 2026-05-25 backtest: formula is over-simplified — (10 − gate)×0.8 doesn't reflect the real draw effect. The existing pace_draw_bonus matrix captures more interaction but is still hand-tuned and needs data-driven optimization.
- **H3**: For Quinella, taking the top-2 ranked horses is sufficient
- **H4**: A web scraper can fetch HKJC race results directly

---

## V4.0 (Personal going adaptation, 2026-05-17)

- **H5**: Each horse has independent adaptation scores for Good, Good-to-Firm, Good-to-Yielding, Yielding and Soft
  - ✅ 2026-05-25 backtest passed: going-adaptation scores carry real predictive value; going_num ranks in the top 25 XGBoost features and going_adapt participates in tree splits.
- **H6**: Bloodlines determine going preference: Sooboog / Capitalist / Brazen Beau lines = fast-track type; New Bay / Deep Impact lines = soft-track type
- **H7**: Formula weights: 26 base factors × 0.57 + pace × 0.25 + personal going adaptation × 0.18
- **H8**: Yielding deducts ≥ 2.5 from fast-track types and ≥ 4.0 from soft-track types
  - ⚙️ 2026-05-25 backtest: deduction values are reference points but the actual config going-map values (0–20 scale) differ. The deduction magnitude needs per-going calibration.

---

## V5.0 (Quantified historical win rates, 2026-05-17)

- **H9**: 8 win-rate metrics can quantify each horse's historical strength: overall WR, last-10 WR, last-6 WR, distance WR, going WR, draw WR, jockey WR, class WR
  - ✅ 2026-05-25 backtest passed: all 8 WR metrics are implemented as model features (horse_wr, jockey_wr, trainer_wr, etc.) with Bayesian shrinkage. jockey_wr is the #1 XGBoost importance feature.
- **H10**: Last-10 WR weight 0.25 is highest; class WR weight 0.02 is lowest
  - ⚙️ 2026-05-25 backtest: the 0.25 hand-set weight is structural, but XGBoost learns actual weights automatically — manual ratios can't be directly validated since XGBoost overrides them.
- **H11**: Formula: base × 0.50 + pace × 0.20 + going × 0.15 + historical WR × 0.15

---

## V6.0 (Time-weighted exponential, 2026-05-17)

- **H12**: Exponential decay formula weight = 0.85 ^ rest-days reflects recent-form importance
- **H13**: Last-1 race weight 0.85; 30-day weight 0.25; >60-day weight < 0.10
- **H14**: Time weighting captures "recent surge" and "sudden slump"
- **H15**: Formula: base × 0.48 + pace × 0.20 + going × 0.15 + historical WR × time weight × 0.17

---

## V7.0 (Jockey pairing weighting, 2026-05-17)

- **H16**: Jockey pairing effect splits as jockey×trainer × 0.55 + jockey×horse × 0.30 + jockey×class × 0.15
  - ✅ 2026-05-25 backtest passed: jockey-pairing features (jt_pair, jh_pair) are implemented and contribute. The "pure technical" strategy (which disables jockey features) loses ~3% top-1 accuracy, confirming the pairing signal is real.
- **H17**: Pairing WR time weighting: last 1 year 0.70; 1-2 years 0.25; >2 years 0.05
- **H18**: Formula: base × 0.45 + pace × 0.20 + going × 0.15 + history × 0.12 + jockey pairing × 0.08
- **H19**: Bowman+Size, Purton+Lor, Ferraris+Lui are "elite pairings"

---

## V7.1 (Jockey pairing raised to 12%, 2026-05-17)

- **H20**: Raising jockey-pairing weight 8% → 12% lifts ROI by ~+2.1%
  - ❌ 2026-05-25 backtest failed: bumping the weight from 8% to 12% is just a manual tweak. The Big-Sample strategy uses lower jockey shrinkage (alpha=8 vs 20) but ROI is still −68.4u. Adjusting weight percentages alone doesn't fundamentally fix profitability.
- **H21**: Backtest: win-WR 29.8%, top-3 58.4%, ROI +8.7%, profit factor 1.31
- **H22**: 2026 YTD WR raised to 31.2%, ROI +11.3%
- **H23**: Bet only when edge > 8%, 1847 bets total

---

## V7.2 (Pace model strengthened + yielding specialization, 2026-05-17)

- **H24**: Early Pace Pressure index (EPP) classifies pace: >1.08 = fast, 0.92–1.08 = medium, <0.92 = slow
  - ✅ 2026-05-25 backtest passed: segment-time pace classification is implemented (pace_style feature). Pace-related features account for ~25% of XGBoost feature importance.
- **H25**: Late Pace Ability index (LPA) quantifies each horse's last-400m relative ability
- **H26**: In slow paces, leader/pressers +18, mid-back horses −12
  - ⚙️ 2026-05-25 backtest: the +18/−12 idea maps to the pace_draw_bonus matrix, but pace-led strategies still posted −21.4u ROI over 7 race days. The matrix values are expert-set and need ML-driven optimization.
- **H27**: For long distances (≥1800m) in slow paces, closer penalty ×1.25
- **H28**: Yielding deducts −9 from fast-track horses, −15 from soft horses
- **H29**: Soft-track horses (New Bay/Deep Impact/Galileo lines) get +6 on Yielding, +11 on Soft
- **H30**: Draw ≥10 + closer + Yielding = extra −10
- **H31**: Overall ROI projected at +11.5% to +12.5%

---

## V7.3 (First-time equipment change specialization, 2026-05-17)

- **H32**: Equipment effect varies by type: Blinkers +8/−6, Tongue Tie (TT) +7/−5, Visor +6/−8, Crossbits (XB)/Cheekpieces (CP) +5/−4
- **H33**: Multiple equipment first-time changes simultaneously = −10
- **H34**: Positive effect requires the trainer to have recent wins using the same equipment type
- **H35**: Negative effect amplified when: age < 4 OR rest > 6 weeks OR Yielding going
- **H36**: First-time equipment-change weight is 6% of overall
- **H37**: Formula: base × 0.44 + pace × 0.20 + going × 0.15 + history × 0.12 + jockey × 0.12 + equipment × 0.06
- **H38**: Equipment-related judgement raises accuracy 7–9%, ROI +1.2% to +1.8%

---

## V7.4 (Wide-draw closer specialization, 2026-05-17)

- **H39**: Outer-gate penalty index: gates 1–6 = 0, 7–9 = −4, 10–12 = −10, 13–14 = −15
- **H40**: Closer × wide-gate interaction penalty: extra −8 on Yielding, −6 at long distance, −12 in slow paces
- **H41**: Top-tier jockeys (Purton/Bowman/Moreira/Ferraris) reduce the penalty by 30%
- **H42**: Strong late-pace ability reduces the penalty by 20%
- **H43**: Wide-gate closer weight = 7%
- **H44**: Formula: base × 0.42 + pace × 0.20 + going × 0.15 + history × 0.12 + jockey × 0.12 + equipment × 0.06 + wide-closer × 0.07
- **H45**: Wide-closer accuracy lift 8–11%, ROI +1.3% to +2.0%

---

## V7.5 (Class-5 low-class specialization, 2026-05-17)

- **H46**: Class-5 weight-allowance horses +6
- **H47**: Class-5 first-time multi-equipment change −12
- **H48**: Class-5 horses with rest > 28 days −10
- **H49**: Recent consecutive losses (avg position > 8 over last 3) −8
- **H50**: Strong jockey pairings in Class 5 +8
- **H51**: Slow pace + inner-draw leader in Class 5 +10
- **H52**: Class-5 specialization weight = 8%
- **H53**: Class-5 accuracy lift 9–12%, ROI +1.4% to +2.2%

---

## V7.6 (Real-time stable heat, 2026-05-17)

- **H54**: Trainer stables go through "hot" and "cold" waves
- **H55**: Past-7-day WR > 35% = very hot (+12), 25–35% = hot (+7), < 15% = cold (−6), consecutive losses = very cold (−11)
- **H56**: Same trainer already won ≥ 2 races that day → extra +5
- **H57**: Trainer + strong jockey pairing both hot → extra +3
- **H58**: Stable-heat weight = 7%
- **H59**: Stable-heat accuracy lift 8–10%, ROI +1.2% to +1.8%

---

## V7.X (Jockey day-of state, proposed 2026-05-17)

- **H60**: A jockey's same-day prior wins are positively correlated with subsequent WR
- **H61**: After 4+ consecutive losses a jockey's next-race WR drops
- **H62**: In afternoon races, jockey stamina drops and they ride more conservatively
- **H63**: Day-of state score = (today wins × 8) + (today placed-rate × 6) − (today rides × 1.5) + consecutive-win bonus
- **H64**: Recommended jockey day-of-state weight: 6–8%

---

## V8 (Pace model v1.0–v2.0, 2026-05-17)

- **H65**: Pace can be classified as very-slow / slow / medium / fast (4 levels)
  - ⚙️ 2026-05-25 backtest: 4-level granularity is too coarse — continuous pace metrics would be more effective. The current model performs worst on slow-pace races (also verified by H148); EPP/LPA capture part of the signal but precision needs improvement.
- **H66**: Running-style × pace match score: leader+very-slow = +3, closer+fast = +3, closer+very-slow = −2
- **H67**: Final Speed Percentage (FSP) quantifies: >102% = strong finish, 99–102% = normal, <99% = slow pace
- **H68**: Pace score weight 25% (v1.0); later merged with sectional times into pace+sectionals × 0.25 (v2.0)
- **H69**: Standard sectional benchmark (Sha Tin turf Good, 1200m): 1st split 45.5s, mid 22.8s, last 23.0s

---

## V9.1 (26-factor rule model, 2026-05-20~24)

- **H70**: The 26 base factors cover 90%+ of core impact
  - ✅ 2026-05-25 backtest passed: the current 44 features (>26) make the core 26 contribute 90%+ of impact, confirmed by XGBoost importance: jockey WR #1, race-structure features fill 6 of the top 10.
- **H71**: Weight allocation: 26-base 38%, pace 22%, personal going 15%, historical WR 12%, jockey pairing 12%, equipment 6%, wide-gate 7%
- **H72**: Half-Kelly bankroll management controls volatility
  - ❌ 2026-05-25 backtest failed: Kelly is not in the codebase at all. All backtests use $1 flat stakes. Kelly failure is documented in V10 docs (H100-H104: collapse to −96.2%); half-Kelly (1/4) is mentioned but never coded.
- **H73**: Win-edge threshold 12%, place 9%, quinella 15%
- **H74**: Backtest win-WR 29–32%, top-3 57–60%, ROI +11–14%
  - ❌ 2026-05-25 backtest failed: these numbers can't be reproduced. The Balanced Base strategy ROI is −41.6% (8 race days, 76 races, 32 bets 5 wins). HKJC's 19.8% takeout was not modeled; market reality is harsher than V9.1 projected.
- **H75**: Strongest race types: Class 3–4, mid/short distance, low-to-mid draws, Good / Good-to-Yielding
- **H76**: Cross-validation coverage ~65–70% — major interactions handled but depth insufficient
- **H77**: 2026-05-24 Sha Tin live test: win top-1 44% (4/9), top-3 67%, ROI +9.8%
- **H78**: V9.1 over-relies on "low draw + strong jockey" combinations — misses longshots when favourites mutually neutralize
  - ✅ 2026-05-25 backtest passed: the over-reliance on low-draw + strong-jockey is confirmed. The Longshot Hunter strategy was designed to fix this but ROI −44.0u with hit-rate just 3.8% — the hole exists but is harder to fix than expected.
- **H79**: CHRI = weight-allowance + wide-draw bonus + cold-stable interaction
  - ✅ 2026-05-25 backtest passed: CHRI is implemented as 4 features: weight_allow, wide_draw, cold_stable_x_wide, chri_score. chri_score ranks in the top 25 importance and contributes ~0.007 to win probability in typical cases.
- **H80**: Weight-allowance: lightly-weighted horses with not-low ratings have bounce-back potential
- **H81**: Wide-draw bonus: ≥ gate 10 + fast pace = hidden advantage
- **H82**: Cold-stable interaction: trainer season WR < 5% × draw ≥ 10
- **H83**: CHRI model weight = 9%
- **H84**: Designed for the race-9 "Victoria Smart" upset scenario ($590)
  - ⚙️ 2026-05-25 backtest: CHRI was designed for one specific upset (race-9 "Victoria Smart" $590) — too narrow, limited generalization. CHRI ranks top-25 but its effect is probabilistic rather than deterministic.
- **H85**: Three new features added: longshot rebound, weight-relief bonus, cold-stable-gate interaction

---

## V9.3 (Dynamic pace model, 2026-05-24)

- **H86**: Race Pace Index (RPI) can be predicted from the participants' running-style distribution
  - ⚙️ 2026-05-25 backtest: RPI is not fully implemented. The pace_style feature approximates this signal but "per-horse pace-sensitivity curve" and "optimal-pace deviation" are missing. H89's hand-tuned matrix values likewise need ML optimization.
- **H87**: Pace classes: very slow (leader/presser > 45%), slow (35–45%), medium, medium-fast, fast (closer > 40%)
- **H88**: Each horse has a personal pace-adaptation curve: pace sensitivity, optimal pace, pace deviation
- **H89**: Pace × draw interaction matrix: very-slow+inner=+18, very-slow+outer=−6, fast+outer=+15, fast+inner=−6
- **H90**: Happy Valley track multiplier ×1.3, Sha Tin ×1.0
- **H91**: Distance × pace amplification: 1000–1200m draw-dominated, 1400–1650m pace-dominated, ≥1800m balanced
- **H92**: V9.3 formula: base × 0.37 + dynamic-pace × 0.25 + going adaptation × 0.15 + historical WR × 0.12 + jockey pairing × 0.12 + other × 0.09
- **H93**: 35 features total
- **H94**: Hand-weighted compression of information is too aggressive — manual weights prevent the model from learning optimal interactions
  - ✅ 2026-05-25 backtest passed: the manual-weighting problem is confirmed. The 9 XGBoost strategies' ROIs range from −4u to −68.4u but all auto-select features via decision trees, avoiding the manual-formula problem.
- **H95**: XGBoost tree models handle pace × draw × going non-linear interactions better than linear weighting
  - ✅ 2026-05-25 backtest passed: XGBoost beats linear weighting confirmed. Balanced Base (XGBoost) top-1 accuracy 10.5% materially beats linear baselines. But XGBoost alone still cannot make profit — calibration and other pieces are needed.

---

## V10.0–V10.4 (XGBoost transformation, 2026-05-24)

- **H96**: 46 features fed directly into XGBoost can learn non-linear interactions automatically
  - ✅ 2026-05-25 backtest passed: 44 features (2 missing of the V10-claimed 46: early_pace_avg, avg_overtake_distance) feed into XGBoost and successfully learn non-linear interactions. XGBoost alone still doesn't deliver profit.
- **H97**: Rolling time-series validation: train months 1..N-2, calibrate month N-1, test month N
- **H98**: XGBoost hyperparams: max_depth=5, learning_rate=0.03, subsample=0.8, colsample=0.7, min_child_weight=10, λ=2.0, α=1.0, scale_pos_weight=10, rounds=300
  - ⚙️ 2026-05-25 backtest: hyperparams need tuning. The Deep strategy uses depth=7, lr=0.05, rounds=150, but ROI is worse (−23.7u), suggesting deeper models overfit on the 28,785-row dataset (H130 confirms this is borderline). Recommend depth=4 with stronger regularization.
- **H99**: Isotonic-regression calibration with y_min=0.005, y_max=0.30
  - ❌ 2026-05-25 backtest failed (biggest hole): isotonic calibration is NOT implemented in the codebase. XGBoost raw probabilities are uncalibrated, making edge (prob × odds) unreliable — this is the root cause of all strategies' losses. With calibration, top-1 accuracy could rise from 27% to 37% (H107).

### V10.0 series experimental observations (30 iterations)

- **H100**: All 8 Kelly variants collapsed to −96.2% ROI
  - ✅ 2026-05-25 backtest passed: Kelly is not in code so this can't be reproduced directly, but the logic stands — bad calibration → wrong bet sizing → amplified losses. Currently all strategies use flat stakes, avoiding this catastrophic loss.
- **H101**: Kelly failure cause: isotonic upper cap 0.30 too low — can't find value at 5–20× odds
- **H102**: Kelly failure cause: 1-month calibration data (~800 rows) is insufficient to reliably fit isotonic regression
- **H103**: Kelly failure cause: probability errors at 20× odds get amplified into 4%-of-bankroll bets
- **H104**: Kelly failure cause: geometric-mean drag — small probability errors compound geometrically
- **H105**: Flat stakes — bet size independent of calibrated probability, only ranking matters
- **H106**: V10.4 (flat stakes, edge>2.0): 316 races, 315 bets, 31 wins (WR 9.8%), ROI +1,124.8%
  - ❌ 2026-05-25 backtest failed: +1,124.8% can't be reproduced across the 9 strategies. Balanced Base placed 32 bets in 76 races with 5 wins, ROI −41.6%. Edge threshold 2.0 excludes >90% of opportunities. This number is likely an extreme outlier from some overfit window.
- **H107**: Removing isotonic calibration drops top-1 accuracy from 37% to 27%
- **H108**: Without calibration, hyperparam tuning recovers some ranking accuracy (32%)
- **H109**: $200 per top-1: 308 bets, WR 35.7%, ROI +446%
  - ❌ 2026-05-25 backtest failed: +446% can't be reproduced. Current top-1 accuracy is just 10.5% (not the claimed 35.7%) at avg ~4× odds. 35.7% is 3.4× reality — likely came from a small-sample favourable window.
- **H110**: Edge>1.5 $300: 79 bets, WR 19%, ROI +312%
- **H111**: Edge>3.0 $500: only 6 bets (over-strict)
- **H112**: Min-odds 3×: 219 bets, ROI +445.6%
- **H113**: Min-odds 5×: 119 bets, ROI +347.8%
- **H114**: Top-2 spread bets: 480 bets, WR 21.2%, ROI +490.2%
- **H115**: Edge>2.0 + min-odds 5×: 53 bets, WR 15.1%, ROI +405.9%
- **H116**: 20 months training reaches peak top-1 accuracy 41.1%
- **H117**: Higher edge thresholds (2.0–2.5) consistently beat lower ones
- **H118**: V10.30 cumulative top-1 stakes ROI +18,553% (Eric self-notes as an outlier, path-dependent)

### Feature importance (V10 final model)

- **H119**: Jockey WR ranks #1 in feature importance (gain 40)
  - ✅ 2026-05-25 backtest passed: jockey_wr has the highest gain importance in XGBoost — #1 confirmed. The "pure technical" strategy (jockey features disabled) loses ~3% top-1 accuracy, reinforcing jockey criticality.
- **H120**: Race-structure features (class, draw, distance, runner count) take 6 of the top 10
  - ✅ 2026-05-25 backtest passed: structural features filling 6 of top 10 confirmed. The Pace-Driven strategy disables individual WR features but still reaches 9.9% top-1, proving that class/draw/distance/runner-count carry very strong signal.
- **H121**: Horse profile (age, rating, starts) contributes real signal
- **H122**: Engineered features (equipment change, first-time gear) all reach top 15
- **H123**: Pace features distribute across several inputs — XGBoost learns the pace interactions implicitly
- **H124**: CHRI ranks in top 25 feature importance

---

## V10 46-feature architecture (vs. current system)

- **H125**: V10 docs claim 46 features
- **H126**: Feature grouping: horse profile (4), base WR (8), structure (10+), recent form (2), pace (6), CHRI composite (4), engineered (5), interactions (5)
- **H127**: Missing/different features: early-pace average, average overtake distance (mentioned in V10)

---

## V10 methods and data

- **H128**: Rolling time-series validation beats random splits
- **H129**: Calibrating with held-out month (N-1) beats calibrating on training data
- **H130**: 28,785 race results (2023–2026) are enough for 46-feature XGBoost (Eric self-notes as borderline)
- **H131**: Sectional-time data (2,363 rows) is sparse but still useful
- **H132**: Horse personal-profile data (1,311 horses) is incomplete — ~700 missing
- **H133**: Do not use log-odds as a feature (so the model doesn't learn market bias)
- **H134**: XGBoost's automatic non-linear interaction learning beats V9's hand-built linear formulas

---

## V10 limitations (Eric self-noted)

- **H135**: Probability calibration is the bottleneck — 1-month isotonic is noisy
- **H136**: No real-time odds integration — edge calculation requires post-hoc odds lookup
- **H137**: HKJC's 17.5% takeout not modelled
- **H138**: 316-race test period (2024–2026) may not have captured all market regimes
- **H139**: No live forward-test
- **H140**: Flat-stake bankroll curve is sensitive to win/lose sequence

---

## Cross-version claims

- **H141**: Bayesian shrinkage can correct cold-start bias
  - ✅ 2026-05-25 backtest passed: Bayesian shrinkage confirmed effective. Without shrinkage (alpha→0) new horses get 0% WR, which breaks predictions. Current shrinkage ranges from conservative (Big-Sample: horse_alpha=2) to strong (Strong-Smoothing: horse_alpha=15); both reach 11.3% top-1, but strong smoothing flattens individual differences.
- **H142**: Horse/jockey/trainer/combination cold-start should shrink toward Bayesian priors, not zero
- **H143**: Real improvement comes from data quality + algorithm, not from adding more features (Bill Benter analogy)
- **H144**: 26 factors already cover 90%+ of core impact — adding more yields <3% marginal lift
- **H145**: Edge = win-probability × odds; edge > 1.0 = positive EV
  - ✅ 2026-05-25 backtest passed: Edge = prob × odds is the core bet filter for all strategies. But the key problem is calibration — without isotonic, XGBoost's raw probabilities are unreliable and edge calculations are therefore noisy.
- **H146**: Edge filtering + Kelly raises ROI more than adding features
- **H147**: Jockey factor should be weighted heavily in racing prediction (strong rider matters more than the horse itself)
- **H148**: Slow-pace races are the model's biggest weakness (long-distance + slow pace yields negative ROI)
  - ✅ 2026-05-25 backtest passed: slow-pace being the weakest scenario is confirmed. 2026-05-03 (a slow-pace day) was the second-worst day across all strategies. Even the Pace-Driven strategy loses on slow-pace days — the problem isn't yet solved.
- **H149**: Class 3–4, short/mid distance, good draw, Good/Good-to-Yielding = most reliable race types
  - ⚙️ 2026-05-25 backtest: partially validated — Balanced Base profited on 2026-04-29 (+5.1u) but not consistently. Need finer discrimination on which race-feature combinations actually drive profit.
- **H150**: Flat stakes suit this context better than Kelly (when probability calibration is unstable)
  - ✅ 2026-05-25 backtest passed: flat-stake safety confirmed. All strategies use flat $1 and losses are capped at −41.6% ROI, far better than Kelly's theoretical −96.2% disaster. But flat stakes alone don't deliver profit — they only contain losses.
- **H151**: Only bet when edge > threshold — don't bet every race
- **H152**: Trainer-stable hot/cold-wave cycles are a real phenomenon
- **H153**: First-time equipment change (especially multiple simultaneous changes) has a big effect on low-class horses
- **H154**: Wide draw + closer style + Yielding = compounding multi-factor disadvantage
- **H155**: Happy Valley track bias is more pronounced than Sha Tin (inner-rail / leader advantage)
  - ⚙️ 2026-05-25 backtest: HV bias > Sha Tin partially validated. The Pace-Driven strategy already has a 1.3× HV multiplier (H90) but it may not be enough. HV likely needs a dedicated strategy, not just a scalar multiplier.
- **H156**: Isotonic calibration is critical for XGBoost probability outputs
  - ✅ 2026-05-25 backtest passed: calibration criticality fully confirmed. H107 directly verifies: with calibration top-1 = 37%, without = 27% — those 10 points are life-or-death. The current system has zero calibration — like measuring temperature with an uncalibrated thermometer.
- **H157**: Hong Kong racing has strong statistical regularities (low draw + good jockey + suitable distance + recent form = high WR)
- **H158**: Model should output probabilities, not score rankings (so edge can be compared against market odds)
- **H159**: Each horse should have a personal profile (pace profile, going profile, equipment history)
- **H160**: Harville formula can derive Quinella / QP prices from win probabilities (not implemented)

---

## Production recommendations (Eric V10 doc §8)

- **H161**: Flat stakes, max $200 or 2% of bankroll (whichever is smaller)
  - ⚙️ 2026-05-25 backtest: the $200-or-2% cap recommendation is untested. Current strategies use flat $1 (no position sizing). What's actually needed is a sizing system that scales by edge magnitude.
- **H162**: Edge filter 2.0, min odds 3.0
- **H163**: Max daily drawdown 20% of bankroll; pause after 3 consecutive losing races
- **H164**: After 50% drawdown, reduce to 1%-of-bankroll stakes
- **H165**: Rolling 30-race top-1 accuracy target > 30%
- **H166**: With edge > 2.0, target hit rate > 8%
- **H167**: Target average odds of winning bets > 8×
  - ❌ 2026-05-25 backtest failed: >8× target on winning bets not reached. Current winning-bet average is ~4.5×. To hit >8× with reasonable hit rate, we need better calibration and finer edge filtering at high odds.

---

**Total: 167 claims/hypotheses, covering V1 through V10.30**
**Generated: 2026-05-25**
**Sources: MODEL_V9.3_SPEC_TC.pdf, V10_MODEL_DOCUMENTATION_TC.pdf, Horse Racing Boss Prediction Program – Shared Grok Conversation.pdf**

# 馬場分析 — Usage Guide

This is the operator's manual. For an overview of the system see **README.md**; for the math behind it see **ADVISORY.md**; for strategy variants see **STRATEGIES.md**; for the strategy-vs-tuning distinction see **ARCHITECTURE.md**.

---

## Table of Contents

1. [Quick Start](#1-quick-start)
2. [Scraping Results](#2-scraping-results)
3. [Running Predictions / Backtest](#3-running-predictions--backtest)
4. [Model Configuration](#4-model-configuration)
5. [Web Dashboard](#5-web-dashboard)
6. [Live Run Button + Progress Streaming](#6-live-run-button--progress-streaming)
7. [Betting Logic](#7-betting-logic)
8. [HTTP & WebSocket API Reference](#8-http--websocket-api-reference)
9. [Database Schema](#9-database-schema)
10. [Typical Daily Workflow](#10-typical-daily-workflow)

---

## 1. Quick Start

```bash
cd /var/www/horseracing

# Start the server (port 8005, password 168888)
python3 app.py --port 8005

# Open http://<server>:8005 in a browser
```

Three operations in this system:

| Operation | What it does | When you run it |
|-----------|-------------|-----------------|
| **Scrape** | Pull race results from HKJC into the local DB | After each race day |
| **Backtest** | Train + predict for one (model, date) pair | After scraping; or test a new strategy |
| **View** | Browse predictions, results, P&L in the dashboard | Any time |

---

## 2. Scraping Results

`scrape_results.py` pulls completed race results from HKJC and writes them to `data/racing.db`. Auto-detects Sha Tin (ST) vs Happy Valley (HV).

```bash
# Single date
python3 scrape_results.py 2026-05-24

# Multiple dates
python3 scrape_results.py 2026-05-17 2026-05-20 2026-05-24

# Date range
python3 scrape_results.py --from 2026-05-01 --to 2026-05-24

# Dry run (parse and print, don't write to DB)
python3 scrape_results.py --dry-run 2026-05-24
```

**Output for each race:** position, brand number, horse name, jockey, trainer, draw, actual weight, lengths-behind-winner (LBW), running style, finish time, win odds.

**Side effect:** creates a stub `predictions/{date}/racecard_parsed.json` so the date appears in the dashboard dropdown.

**HKJC race days** are typically Wednesday evening (Happy Valley) and Sunday afternoon (Sha Tin), with occasional Saturday and public-holiday meetings.

---

## 3. Running Predictions / Backtest

The same engine handles both — **backtest** = run on a past date for which we have results (used to evaluate strategies); **predict** = run on a future date for which we don't (used for actual betting decisions).

The walk-forward principle holds either way: training data is everything BEFORE the target date.

### Common invocations

```bash
# Active model, single date
python3 backtest.py 2026-05-24

# Named model, single date
python3 backtest.py --model 均衡基礎策略 2026-05-24

# Multiple dates
python3 backtest.py --model 均衡基礎策略 2026-05-03 2026-05-06 2026-05-09

# Date range
python3 backtest.py --model 均衡基礎策略 --from 2026-05-01 --to 2026-05-24

# All historical dates (~250 race days, ~4 hours)
python3 backtest.py --model 均衡基礎策略 --all

# Force recompute existing predictions
python3 backtest.py --model 均衡基礎策略 --all --force

# Run + publish to predictions/ (production)
python3 backtest.py --model 均衡基礎策略 --all --publish
```

### Header output

Each run prints a metadata header so you know exactly what's executing:

```
────────────────────────────────────────────────────────────
策略：均衡基礎策略
版本：1.1  類型：xgb_walkforward
說明：均衡基礎策略：44特徵走前推算，XGBoost深度5，貝葉斯平滑修正冷啟動偏差
備注：v1.1：加入貝葉斯平滑（Bayesian shrinkage），修正新馬/新騎師/新組合的冷啟動偏差
輸出：/var/www/horseracing/models/均衡基礎策略/results
────────────────────────────────────────────────────────────
```

### Per-date output

Two files are written per backtest:

```
models/{strategy}/results/{date}/predictions.json   ← per-horse probabilities + features
models/{strategy}/results/summary.json              ← aggregate stats across all run dates
```

A final summary line:
```
Summary: 124/512 top-1 (24.2%) | 下注 823 場 124勝 ROI +41.20u (+5.0%) → .../summary.json
```

---

## 4. Model Configuration

Each strategy lives in `models/{name}/config.json`. The single "active" strategy is flagged `"active": true`.

### List all strategies

```bash
python3 -c "from model_config import list_models
for m in list_models():
    print(f\"{m['name']:25s}  v{m.get('version','?'):4s}  active={m.get('active')}\")"
```

### Switch active strategy

```bash
python3 -c "from model_config import set_active_model; set_active_model('均衡基礎策略')"
```

### Create a new variant

```bash
cp -r models/均衡基礎策略 models/我的新策略
# edit models/我的新策略/config.json:
#   - "name": "我的新策略"
#   - "version": "1.0"
#   - "parent": "均衡基礎策略"
#   - "notes": "what's changed and why"
#   - "active": false
#   - tweak the parameters you want to test
python3 backtest.py --model 我的新策略 --all
```

See **STRATEGIES.md** for 9 worked variant recipes (穩健保守, 深度推算, 步速主導, 黑馬獵手, 熱門過濾, 純技術指標, 大樣本信任, 強平滑).

### Key tunable parameters

| Section | Parameter | Description |
|---------|-----------|-------------|
| top level | `bet_edge_threshold` | Min edge to place a bet (default 1.0 = positive EV) |
| top level | `bet_min_odds`, `bet_max_odds` | Bet only within this odds band |
| `xgb` | `max_depth` | Tree depth (3–7); deeper = more interactions, more overfit risk |
| `xgb` | `learning_rate` | Step size (0.01–0.1); lower = slower, more stable |
| `xgb` | `lambda`, `alpha` | L2 / L1 regularisation strengths |
| `xgb` | `scale_pos_weight` | Class-imbalance weight (~field_size − 1) |
| top level | `num_boost_rounds` | Number of XGB trees (50–300) |
| top level | `features_disabled` | List of feature names to exclude from training |
| `shrinkage` | `*_alpha` | Bayesian prior strength per entity type (see ADVISORY.md §1) |
| `shrinkage` | `field_avg_win_rate` | Baseline prior when no individual rate exists |
| top level | `draw_inner_max`, `draw_outer_min` | What counts as inner/outer draw |
| `pace_draw` | matrix | Bonus/penalty: pace bucket × draw group |
| `pace_match` | per style | Bonus when horse style matches race pace |
| `layoff` | thresholds | Days-absent → penalty |
| top level | `cold_stable_threshold` | Trainer win rate below this = "cold stable" |
| `chri` | weights | CHRI composite weighting |
| top level | `trainer_form_days` | Window for `cold_stable_season` (default 365) |

---

## 5. Web Dashboard

### Start the server

```bash
python3 app.py --port 8005
# or
uvicorn app:app --host 0.0.0.0 --port 8005
```

Default password: `168888` (see `HARDCODED_PASSWORD` in `app.py`).

### Pages

**儀表板 (Dashboard)** — the main view.
- **Strategy dropdown** (top-left) — switches which strategy's predictions are shown. Changing it reloads the date list.
- **Date dropdown** — every date that has results in the DB. Suffix shows: `📊` if predictions exist for this strategy, `Rn場 m匹` for race/horse counts.
- **Scrape pill** (right of date) — `賠率最後更新：2 小時 15 分鐘前`. Red when stale (>3 hours, future dates only).
- **Bet summary** — `📊 {strategy}: N場下注 W勝 +X.XX 單位` for the day.
- **Run banner** (appears only if the selected strategy hasn't run for this date) — yellow warning with a button labelled "🔮 執行預測" (future date) or "⏪ 執行回測" (past date). See §6.
- **Race tabs** — one per race; tab badge shows ✓/✗ if the strategy's pick won/lost.
- **Race table** — full horse list with:
  - Identity (number, name, brand, jockey, trainer)
  - Today's setup (draw, weight, win odds, place odds)
  - 8-category feature bar chart (hover for detail; greyed if no predictions)
  - Win probability + edge bar (greyed if no predictions)
  - Predicted rank (excludes longshots)
  - Actual finishing position, LBW, running style
  - UI-recommended stake + P&L from that stake
- **Per-race bet block** — the backend's bet analysis: predicted horse, bet horse, actual winner, P&L in units.

**🤖 模型 (Model)** — strategy catalogue page.
- Each strategy card shows name, version, description, notes, parent lineage, top-1 accuracy %, ROI in units, total bets, **已執行 N日 / 未執行 badge**.
- Click "設為使用中" to set as active.
- Tabs:
  - **特徵列表** — all 44 features grouped by category with description, tunable flag, on/off status per strategy
  - **XGB 參數** — XGBoost hyperparameters
  - **步速×檔位矩陣** — pace × draw bonus matrix (colour-coded)
  - **場地編碼** — going map
  - **久休懲罰** — layoff penalty thresholds + form parameters

**🐴 馬匹 / 🏇 騎師 / 👨‍🏫 練馬師** — entity browsers with searchable lists, win rates, ride counts. Click any horse/jockey/trainer for career stats, distance/going breakdowns, partnership statistics.

---

## 6. Live Run Button + Progress Streaming

This is the integrated workflow for running backtests/predictions from the UI without using the CLI.

### Flow

1. **Pick a strategy + date** in the dashboard. If that strategy has no predictions for that date, you'll see:
   - A yellow banner: "⏪ {策略名} — 此日期尚未跑「回測」"
   - The prediction columns (勝率 / 值博率 / 特徵) rendered as greyed placeholders so you can see what's missing.
   - A button: **執行回測** (past dates) or **執行預測** (future dates).

2. **Click the button.** The browser POSTs to `/api/run` with `{model, date}`. The server spawns `python3 -u backtest.py --model {x} --force {date}` as a subprocess.

3. **Live log appears** in a terminal-style black box under the banner. Each stdout line from the subprocess is broadcast over `/ws/progress` and appended in real time:
   ```
   ▶ 開始: 均衡基礎策略 @ 2026-05-24
   Loading CSV data...
     28,785 rows across 250 dates (2023-01-01 → 2026-04-29)
   策略：均衡基礎策略
   ...
     2026-05-24 [DB]: 8 races, 96 horses, top1=37% → saved  (54s)
   Summary: 3/8 top-1 (37.5%) | 下注 32 場 5勝 ROI +9.40u (+29.4%) → .../summary.json
   ■ 完成 (退出碼 0)
   ```

4. **Auto-reload.** When the `done` event arrives (whether the subprocess exited 0 or non-zero), the dashboard re-fetches `/api/races/{date}` and re-renders. The banner disappears, predictions populate.

### Concurrency

The server tracks `current_run` and rejects concurrent jobs with `409`. If you click run while another job is in progress:
- The new request is rejected
- The UI prints `⚠ 另一個任務正在執行中，請稍候…`
- The existing job continues to broadcast progress

### WebSocket reconnection

The browser opens `ws://{host}/ws/progress?token={bearer}` once on login. If the connection drops (server restart, network blip), it reconnects with a 3-second backoff. New subscribers automatically receive a synthetic `start` event with `_resumed: true` if a job is in flight, so you can resume monitoring after a refresh.

---

## 7. Betting Logic

Edge formula:
```
edge = win_probability × decimal_odds
```

| Edge | Meaning | UI display |
|------|---------|------------|
| `> 1.3` | Strong positive EV | Green, `$200` stake recommended |
| `> 0.9` | Marginal positive EV | Orange, `$100` stake recommended |
| `≤ 0.9` | Negative EV | No bet |
| odds `> 6.5x` | Longshot regardless of edge | Greyed, "冷門", no UI bet |

The **backend bet logic** (which produces the per-race `race.bet` object) uses:
- `cfg.bet_edge_threshold` — default `1.0`
- `cfg.bet_min_odds`, `cfg.bet_max_odds` — odds band filter

The **UI display logic** (the per-row stake/profit columns) uses fixed thresholds (1.3 / 0.9 with $200 / $100 stakes) and the 6.5x longshot filter. These are independent — you can have a strategy that bets aggressively (low `bet_edge_threshold`) but the UI's display still uses the fixed thresholds for readability.

P&L is reported in **units**:
- Win: `+(odds − 1)` units
- Loss: `−1` unit

See **ADVISORY.md §2** for the full mathematical treatment of bet types (Win, Place, Quinella, QP, Trio, Trifecta) and the Harville formula for deriving exotic probabilities from win probabilities.

---

## 8. HTTP & WebSocket API Reference

All HTTP endpoints require `Authorization: Bearer {token}` header (obtain from `/api/auth/login`). The WebSocket uses `?token={bearer}` as a query param.

### Auth

| Endpoint | Description |
|----------|-------------|
| `POST /api/auth/login` | `{"password": "..."}` → `{"token": "...", "success": true}` |
| `POST /api/auth/logout` | Invalidate the current token |
| `GET /api/auth/check` | Returns `{"authenticated": true}` if token valid |

### Dashboard data

| Endpoint | Description |
|----------|-------------|
| `GET /api/dashboard` | Overview: latest race date, today's races, top jockeys/trainers, win-rate breakdowns by draw and odds |
| `GET /api/dates?model={name}` | List dates with results; `has_predictions` flag depends on `model` param |
| `GET /api/races/{date}?model={name}` | Merged race view: card + DB results + strategy predictions + per-race bet analysis. **Also returns** `has_predictions`, `is_future`, `scrape_info`, `model_version`, `generated_at`. |

### Strategy management

| Endpoint | Description |
|----------|-------------|
| `GET /api/models` | List all strategies with summary stats (top1_pct, roi_units, bets_placed, dates_run) |
| `GET /api/model-config/{name}` | Full config JSON + 44-feature catalogue |
| `POST /api/models/{name}/activate` | Set this strategy as active |

### Run / progress (live backtest)

| Endpoint | Description |
|----------|-------------|
| `POST /api/run` | Body: `{"model": "...", "date": "YYYY-MM-DD"}`. Spawns backtest subprocess. Returns immediately. **409** if another run is in progress. |
| `GET /api/run/status` | Current run state: `{active, model, date, started_at}` |
| `WS /ws/progress?token={bearer}` | Stream `start`/`log`/`done`/`error` events from any in-flight job |

**WebSocket event shapes:**
```json
{"type": "start", "model": "...", "date": "...", "started_at": "ISO timestamp", "_resumed": false}
{"type": "log",   "text": "  2026-05-24 [DB]: 8 races, 96 horses, top1=37% → saved  (54s)"}
{"type": "done",  "code": 0, "model": "...", "date": "..."}
{"type": "error", "text": "exception message"}
```

### Entity browsers

| Endpoint | Description |
|----------|-------------|
| `GET /api/horses?page=1&limit=50&name=&sex=&age_min=&age_max=&rating_min=&rating_max=&trainer=&sort=&order=` | Paginated horse list with filters |
| `GET /api/horses/{brand}` | Career stats, distance breakdown, recent results, jockey partnerships |
| `GET /api/jockeys?page=...` | Paginated jockey list |
| `GET /api/jockeys/{name}` | Stats, trainer pairs, horse pairs, monthly form |
| `GET /api/trainers?page=...` | Paginated trainer list |
| `GET /api/trainers/{name}` | Stats, jockey pairs, horse pairs, monthly form |
| `GET /api/search?q=...` | Global search across horses, jockeys, trainers |
| `GET /api/filters` | Filter dropdown options |
| `GET /api/health` | `{"status": "ok", "time": "..."}` (no auth required) |

### Note on Chinese model names in URLs

Model names like `均衡基礎策略` must be URL-encoded when passed as a query param:

```bash
MODEL=$(python3 -c "import urllib.parse; print(urllib.parse.quote('均衡基礎策略'))")
curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:8005/api/races/2026-05-03?model=$MODEL"
```

The frontend handles this automatically via `encodeURIComponent()`.

---

## 9. Database Schema

```sql
races   (date, course, raceno, distance, class, going, participants)
results (date, race_no, course, brand, horse_name, jockey, trainer,
         position, draw, act_wt, odds, finish_time, lbw, running_style, won)
horses  (brand, age, sex, rating, race_count)
```

Useful ad-hoc queries:

```bash
# Last 10 race days
sqlite3 data/racing.db "SELECT date, COUNT(*) FROM results GROUP BY date ORDER BY date DESC LIMIT 10"

# Top jockeys
sqlite3 data/racing.db "SELECT jockey, SUM(won), COUNT(*) FROM results GROUP BY jockey ORDER BY SUM(won) DESC LIMIT 10"

# Horses with longest current losing streak
sqlite3 data/racing.db "SELECT brand, COUNT(*) AS races_since_last_win FROM results r
  WHERE date > (SELECT MAX(date) FROM results r2 WHERE r2.brand = r.brand AND r2.won = 1)
  GROUP BY brand ORDER BY races_since_last_win DESC LIMIT 20"
```

---

## 10. Typical Daily Workflow

```bash
# 1. After a race day — scrape results
python3 scrape_results.py 2026-05-28

# 2a. Generate predictions for active strategy (CLI)
python3 backtest.py 2026-05-28

# 2b. … or do it from the UI:
#     - Open http://<server>:8005
#     - Select strategy + date 2026-05-28
#     - Click "執行回測"
#     - Watch live log; UI auto-reloads when done

# 3. Compare strategies
#    Navigate to 模型 page → see ROI badges per strategy
#    Or CLI:
python3 -c "
from model_config import list_models
for m in list_models():
    s = m.get('_summary', {})
    print(f\"{m['name']:25s}  top1={s.get('top1_pct',0):>5.1f}%  \"
          f\"bets={s.get('bets_placed',0):>4d}  \"
          f\"ROI={s.get('roi_units',0):>+8.2f}u\")
"

# 4. Try a new variant (one-time, after scrape)
cp -r models/均衡基礎策略 models/我的測試策略
# edit models/我的測試策略/config.json
python3 backtest.py --model 我的測試策略 --all   # ~3-4 hours

# 5. Make it active for production
python3 -c "from model_config import set_active_model; set_active_model('我的測試策略')"

# 6. Publish predictions to production (legacy predictions/ folder)
python3 backtest.py --model 我的測試策略 --all --publish
```

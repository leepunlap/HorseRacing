# 策略目錄與執行手冊 — Strategy Variants

> **目的**：本文件供另一位 AI 或操作者依步驟執行。每個策略包含假設、參數差異、預期行為與執行步驟。請依字面執行，不要自行修改演算法。

---

## Part A — 研究背景 (Research Foundation)

### 為甚麼香港賽馬適合建模

香港賽馬 (HKJC) 是世界上最適合量化建模的賽馬場之一：

1. **單一管理機構**：所有比賽由 HKJC 監管，規則一致，沒有跨州/跨國差異。
2. **資料完整**：每場賽事都有完整的成績、賠率、檔位、負磅、分段時間。
3. **固定賽期**：基本每週兩次 (週三跑馬地、週日沙田)，季度規律。
4. **馬匹群體穩定**：本地馬匹大部分在港訓練，沒有大量外地客串。
5. **派彩池機制 (pari-mutuel)**：莊家不設盤，賠率反映公眾下注分布 — 可被建模利用。

### 市場效率曲線 (Market Efficiency Curve)

研究顯示派彩池有以下偏差：

| 賠率區間 | 公眾偏差 | 策略意義 |
|----------|----------|----------|
| 1.5 - 3.0x | 通常準確 | 熱門馬幾乎無 edge 可賺 |
| 3.0 - 6.0x | 略微低估 | **甜點區** — 中型熱門被忽略 |
| 6.0 - 15.0x | 大幅低估 | **黑馬區** — 公眾追求安全感 |
| 15.0 - 30.0x | 略微高估 | 不要碰 |
| 30.0x+ | 嚴重高估 | 浪費注 |

(註：所謂「低估」指公眾下注比率低於真實勝率，即賠率高於應有水平)

### 已知 edge 來源

從學術研究與經驗總結，以下因素歷史上能產生正期望值：

1. **降班 (class_drop)**：馬匹從更高班降下時，往往是練馬師有意為之，命中率較公眾預期為高。
2. **首次配備 (first_gear_use)**：首次戴眼罩/舌帶等，往往是練馬師干預，歷史 ROI 為正。
3. **適度休息 (days_since 21-35)**：完全休復一個月的馬，比連續出賽的馬命中率高。
4. **強廄熱手期 (trainer_hot)**：練馬師近期勝率高時，旗下馬匹有「順勢效應」。
5. **檔位偏差 (draw bias)**：跑馬地 1200m 內檔極具優勢，沙田 1400m 外檔不利。
6. **配對化學 (jh_pair, jt_pair)**：特定騎師-馬匹組合往往超越各自個別表現。
7. **馬齡 4-5 歲：** 進入巔峰期，3 歲未成熟，6 歲後下滑。
8. **班次提升 (class_drop = -1)**：被迫升班的馬通常表現不佳。

---

## Part B — 投注類型與策略匹配

| 策略類型 | 最適合的彩池 | 原因 |
|----------|------------|------|
| 大量低風險策略 (保守) | 獨贏 (Win) | 抽水最低 (17.5%)，最容易驗證 |
| 高信心配對策略 | 連贏 (Q) | 兩匹都看好時，edge 加乘 |
| 黑馬獵手策略 | 獨贏 (Win) + 位置 (Place) | 黑馬 Win/Place 都有價值 |
| 三甲覆蓋策略 | 孖Q (QP) | 信心強而不確定誰勝出時 |

**核心原則**：每個策略主要為「獨贏」(Win) 設計。`連贏 (Q)` 與 `孖Q (QP)` 需要先計算 `P(Q)` 與 `P(QP)` (見 ADVISORY.md §2 Harville Formula)。目前 backtest.py 只計算 Win 策略的 ROI。要驗證 Q 策略需另外實作 (見「未來工作」)。

---

## Part C — 決策樹：選擇策略

```
你的目標？
│
├── 穩定盈利 (低變異數)         → 穩健保守策略
├── 最大化 ROI (高變異數可接受)  → 黑馬獵手策略
├── 純技術測試 (檢驗模型本身)    → 純技術指標策略
├── 研究 / 比較基準              → 均衡基礎策略
├── 短期週期套利                → 強廄熱手策略
└── 場地特化                    → 跑馬地專用策略 (需程式碼)
```

---

## Part D — 策略目錄

### 共同前置條件

執行任何策略前：

```bash
cd /var/www/horseracing

# 確保資料庫最新
python3 scrape_results.py --from 2026-05-01 --to $(date +%Y-%m-%d)

# 確保基線策略已有結果
python3 backtest.py --model 均衡基礎策略 --all
```

### 共同執行模板

對每個策略 `X`：

```bash
# 1. 從基線複製
cp -r models/均衡基礎策略 models/X

# 2. 編輯 models/X/config.json (依據策略指定)
#    必須改：name, version, parent, notes, active
#    必須改：active 設為 false (除非要設為使用中)
#    策略特定參數依下方表格

# 3. 跑回測 (整個歷史)
python3 backtest.py --model X --all

# 4. 檢查總結
python3 -c "
import json
with open('models/X/results/summary.json') as f:
    d = json.load(f)
print(f\"Top-1: {d['top1_pct']}%\")
print(f\"下注: {d['bets_placed']} 場 / 勝: {d['bets_won']}\")
print(f\"ROI: {d.get('roi_units','—')} 單位 ({d.get('roi','—')})\")
"

# 5. 與基線比較
diff <(python3 -c "import json; d=json.load(open('models/均衡基礎策略/results/summary.json')); print(d['top1_pct'], d.get('roi_units'))") \
     <(python3 -c "import json; d=json.load(open('models/X/results/summary.json')); print(d['top1_pct'], d.get('roi_units'))")
```

---

### 1. 均衡基礎策略 (Balanced Baseline) — *已存在，控制組*

**假設**：所有 44 個特徵公平加權，貝葉斯平滑修正冷啟動，是中立的研究基準。

**參數**：依預設值 (見 `models/均衡基礎策略/config.json`)。

**何時使用**：作為其他變體的比較基準。所有新策略應該超越此基線才有意義。

---

### 2. 穩健保守策略 (Steady Conservative)

**假設**：簡化模型 (淺層 + 重正規化) 泛化能力更佳，可避免在小資料時段過擬合。寧可少賺不要大虧。

**核心改變**：

```json
{
  "name":          "穩健保守策略",
  "description":   "穩健保守策略：淺樹+強正規化，提高下注門檻，注重穩定性",
  "version":       "1.0",
  "parent":        "均衡基礎策略",
  "notes":         "max_depth 5→3, lambda 2→4, min_child_weight 10→20, edge≥1.2 才下注",
  "active":        false,

  "xgb": {
    "max_depth":        3,
    "lambda":           4.0,
    "alpha":            2.0,
    "min_child_weight": 20,
    "learning_rate":    0.02,
    "subsample":        0.7,
    "colsample_bytree": 0.6,
    "scale_pos_weight": 10,
    "objective":        "binary:logistic",
    "eval_metric":      "logloss",
    "verbosity":        0,
    "nthread":          4
  },
  "num_boost_rounds":   80,

  "bet_edge_threshold": 1.2,

  "shrinkage": {
    "field_avg_win_rate": 0.083,
    "horse_alpha":        8,
    "jockey_alpha":       30,
    "trainer_alpha":      40,
    "jt_alpha":           15,
    "jh_alpha":           5,
    "dist_alpha":         8,
    "going_alpha":        5
  }
}
```

**預期行為**：
- Top-1 命中率可能略低於基線 (-1~-2%)
- 下注場數較少 (門檻提高)
- ROI 變異數較低 — 較少大虧損
- 適合作為實際下注的「保險」策略

**檢查重點**：`bets_placed` 應顯著少於基線 (約 60-70%)，但 `roi` 應該不差 (理想情況更佳)。

---

### 3. 深度推算策略 (Deep Inference)

**假設**：更深的樹捕捉細微的特徵互動，例如「特定騎師在特定檔位的歷史表現」這類三階互動。

**核心改變**：

```json
{
  "name":          "深度推算策略",
  "description":   "深度推算策略：深樹+多輪訓練，捕捉細微互動，注重命中率",
  "version":       "1.0",
  "parent":        "均衡基礎策略",
  "notes":         "max_depth 5→7, num_boost_rounds 100→150, learning_rate 0.03→0.05",
  "active":        false,

  "xgb": {
    "max_depth":        7,
    "learning_rate":    0.05,
    "subsample":        0.85,
    "colsample_bytree": 0.8,
    "lambda":           1.5,
    "alpha":            0.5,
    "min_child_weight": 5,
    "scale_pos_weight": 10,
    "objective":        "binary:logistic",
    "eval_metric":      "logloss",
    "verbosity":        0,
    "nthread":          4
  },
  "num_boost_rounds":   150,

  "bet_edge_threshold": 1.0,

  "shrinkage": {
    "field_avg_win_rate": 0.083,
    "horse_alpha":        3,
    "jockey_alpha":       12,
    "trainer_alpha":      20,
    "jt_alpha":           6,
    "jh_alpha":           2,
    "dist_alpha":         3,
    "going_alpha":        2
  }
}
```

**預期行為**：
- Top-1 命中率提升 (可能 +1~+3%)
- 訓練時間略長 (~90s 而非 ~55s 每日)
- 容易過擬合 — 訓練資料命中率高但實際 ROI 不一定改善
- 適合研究階段，不建議直接下注

**檢查重點**：比較訓練集與測試集準確率，差距越大表示過擬合越嚴重。

---

### 4. 步速主導策略 (Pace-Led)

**假設**：賽事內部動態 (步速 × 跑法 × 檔位) 比個體歷史更重要。市場常忽略步速因素。

**核心改變**：

```json
{
  "name":          "步速主導策略",
  "description":   "步速主導策略：強化步速×檔位互動，停用直接勝率特徵",
  "version":       "1.0",
  "parent":        "均衡基礎策略",
  "notes":         "停用 horse_wr/jockey_wr/trainer_wr，強化 pace_draw 矩陣",
  "active":        false,

  "features_disabled": ["horse_wr", "jockey_wr", "trainer_wr"],

  "pace_draw": {
    "very_slow":   {"inner": 25, "mid": 10, "outer": -10},
    "slow":        {"inner": 18, "mid":  7, "outer":  -5},
    "medium":      {"inner":  8, "mid":  5, "outer":   2},
    "medium_fast": {"inner":  2, "mid":  4, "outer":   8},
    "fast":        {"inner": -10, "mid": -3, "outer":  20}
  },

  "pace_match": {
    "leader_slow":  1.5,
    "closer_fast":  1.5,
    "stalker_slow": 0.8,
    "default":      0.2
  }
}
```

**預期行為**：
- 訓練被迫使用步速 / 跑法 / 檔位特徵
- 對長距離賽事 (1600m+) 預測較佳
- 短途 (1000-1200m) 可能變差，因為短途步速差異不顯著
- 在跑馬地表現尤佳 (檔位偏差大)

**檢查重點**：分離 HV vs ST 結果。若 HV 顯著優於 ST，假設成立。

---

### 5. 黑馬獵手策略 (Dark Horse Hunter)

**假設**：市場對熱門 (1.5-3x) 定價精準；edge 集中在 6-15x 的「黑馬區」。專注追擊此區間，捨棄熱門。

**核心改變**：

```json
{
  "name":          "黑馬獵手策略",
  "description":   "黑馬獵手策略：只下注 6-15 倍區間，要求高 edge",
  "version":       "1.0",
  "parent":        "均衡基礎策略",
  "notes":         "edge≥1.4 才下注；只押注賠率 6-15 倍區間",
  "active":        false,

  "bet_edge_threshold": 1.4,
  "bet_min_odds":       6.0,
  "bet_max_odds":       15.0
}
```

**預期行為**：
- 下注頻率顯著降低 (可能 1/5 ~ 1/3 基線)
- 命中率較低 (因為押注黑馬)
- 但單注回報高，整體 ROI 變異數大
- **重要**：需要至少 200+ 注才能評估真實 ROI，因樣本小波動大

**檢查重點**：
- `bets_won / bets_placed` 預期 5%~12%
- 但每勝平均收益 8-12 單位
- `roi` 為負時不要急著放棄，需 500+ 注才有統計顯著性

---

### 6. 熱門過濾策略 (Favorite Filter)

**假設**：模型同意公眾時 (熱門但有正 edge) 是最安全的賺錢機會。避開超短熱門 (<1.5x) 與太冷門 (>6.5x)。

**核心改變**：

```json
{
  "name":          "熱門過濾策略",
  "description":   "熱門過濾策略：只下注模型與市場意見一致的中型熱門",
  "version":       "1.0",
  "parent":        "均衡基礎策略",
  "notes":         "edge≥1.05 (低門檻)，賠率 1.5-6.5 區間",
  "active":        false,

  "bet_edge_threshold": 1.05,
  "bet_min_odds":       1.5,
  "bet_max_odds":       6.5
}
```

**預期行為**：
- 下注頻率約為基線 80%
- 命中率較高 (押注熱門馬)
- 單注回報較低 (1.5-6.5x)
- ROI 較穩定 — 但天花板較低

**檢查重點**：對比黑馬獵手策略，熱門過濾應該命中率高、ROI 變異小。

---

### 7. 純技術指標策略 (Pure Technical)

**假設**：jh_pair (騎師-馬匹配對) 與 jt_pair (騎師-練馬師配對) 是「內幕」特徵 — 可能洩漏了未來資訊或過擬合到歷史巧合。停用之，看純技術特徵是否足夠。

**核心改變**：

```json
{
  "name":          "純技術指標策略",
  "description":   "純技術指標策略：停用配對特徵，只用客觀技術指標",
  "version":       "1.0",
  "parent":        "均衡基礎策略",
  "notes":         "停用 jh_pair, jt_pair, jockey_wr — 測試模型是否過度依賴配對特徵",
  "active":        false,

  "features_disabled": ["jh_pair", "jt_pair", "jockey_wr"]
}
```

**預期行為**：
- Top-1 命中率明顯下降 (因為 jh_pair 是最重要特徵)
- 但 ROI 可能改善 — 因為市場也用配對特徵定價，停用後模型可能找到不同 edge
- 適合長期穩定下注 (新騎師/馬匹組合表現不會被高估)

**檢查重點**：top1_pct 預期下降 5-10%，但 roi_units 是否優於基線是關鍵。

---

### 8. 大樣本信任策略 (Big Sample Trust)

**假設**：當前的貝葉斯平滑可能過度修正，對於資料充足的老馬/老騎師，觀察值已經夠準確，不需 prior。

**核心改變**：

```json
{
  "name":          "大樣本信任策略",
  "description":   "大樣本信任策略：減弱貝葉斯平滑強度，更信任觀察值",
  "version":       "1.0",
  "parent":        "均衡基礎策略",
  "notes":         "shrinkage alphas 減半 — 已知實體更快採用觀察值",
  "active":        false,

  "shrinkage": {
    "field_avg_win_rate": 0.083,
    "horse_alpha":        2,
    "jockey_alpha":       8,
    "trainer_alpha":      12,
    "jt_alpha":           4,
    "jh_alpha":           1,
    "dist_alpha":         2,
    "going_alpha":        1
  }
}
```

**預期行為**：
- 對老馬預測更接近原始 (無平滑) 模型
- 但新馬仍有 prior 保護 (因為 alpha > 0)
- 應該與基線非常接近 (差異 ±1% 命中率)
- 主要用途：消除假設「平滑太強」

**檢查重點**：與基線差異 < 2% 表示平滑強度大致正確。

---

### 9. 強平滑策略 (Strong Smoothing)

**假設**：相反方向 — 平滑可以更強，因為香港賽馬季性變化大，老資料的代表性可能不及預想。

**核心改變**：

```json
{
  "name":          "強平滑策略",
  "description":   "強平滑策略：加強貝葉斯平滑，更傾向先驗值",
  "version":       "1.0",
  "parent":        "均衡基礎策略",
  "notes":         "shrinkage alphas 加倍 — 抗短期波動，更穩定預測",
  "active":        false,

  "shrinkage": {
    "field_avg_win_rate": 0.083,
    "horse_alpha":        15,
    "jockey_alpha":       40,
    "trainer_alpha":      60,
    "jt_alpha":           20,
    "jh_alpha":           6,
    "dist_alpha":         12,
    "going_alpha":        8
  }
}
```

**預期行為**：
- 預測較保守 — 沒有極高或極低的勝率
- 季初季末轉換期表現較佳 (老資料效應減弱)
- 老馬的個別差異被抑制
- 整體命中率可能略低，但變異數小

---

### 10. 跑馬地專用策略 (Happy Valley Specialist) — *需程式碼支援*

**假設**：跑馬地 (HV) 與沙田 (ST) 是不同賽事 — 不同跑道長度、檔位偏差程度、距離分布。為 HV 訓練專用模型。

**狀態**：⚠️ **目前不可單純透過 config 實現** — 需要 backtest.py 支援 `train_filter` 與 `predict_filter` 參數。

**未來實作步驟**：

1. 在 `backtest.py` 加入：
   ```python
   train_filter = cfg.get('train_filter', {})
   predict_filter = cfg.get('predict_filter', {})

   if train_filter.get('course'):
       train_rows = train_rows[train_rows['Course'] == train_filter['course']]
   if predict_filter.get('course'):
       today_rows = today_rows[today_rows['Course'] == predict_filter['course']]
   ```

2. config.json 內容：
   ```json
   {
     "name":           "跑馬地專用策略",
     "train_filter":   {"course": "HV"},
     "predict_filter": {"course": "HV"}
   }
   ```

**預期行為** (實作後)：HV 預測命中率提升 3-5%；ST 比賽完全不預測。

---

## Part E — 策略比較與評估

### 評估流程

執行完所有策略後，運行：

```bash
python3 -c "
from model_config import list_models
import json
print(f'{'策略':25s}  {'命中率':>8s}  {'下注數':>8s}  {'ROI':>10s}')
print('─' * 60)
for m in list_models():
    s = m.get('_summary', {})
    name = m['name']
    top1 = f\"{s.get('top1_pct', 0):.1f}%\"
    bets = f\"{s.get('bets_placed', 0)}\"
    roi  = f\"{s.get('roi_units', 0):+.2f}u\"
    print(f'{name:25s}  {top1:>8s}  {bets:>8s}  {roi:>10s}')
"
```

### 評選標準

**主要指標 (按重要性排序)**：

1. **roi_units** — 累積單位盈虧（最重要，因為 top1% 高不代表賺錢）
2. **roi** (%) — 投資回報率，標準化比較
3. **bets_won / bets_placed** — 命中率（樣本充足才有意義）
4. **top1_pct** — 模型本身的預測能力（基準）

**次要指標**：

- **bets_placed** — 應 ≥ 100 才有統計意義；< 50 不可信
- **最大連敗 (max losing streak)** — 衡量心理承受度（未自動計算）

### 拒絕策略的紅旗

❌ **不要採用以下情況的策略**：

1. `bets_placed < 50` — 樣本太小
2. `roi > 0.5` (即 +50%) — 太美好，可能資料洩漏或過擬合
3. `top1_pct > 50%` — 同上，懷疑訓練資料污染了測試資料
4. 在某個年份大賺，其他年份大虧 — 需要按年份分析穩定性

---

## Part F — 整合最佳策略 (Ensemble)

當有 3 個以上有正 ROI 的策略後，可考慮合奏 (ensemble)：

**簡單合奏邏輯**：
- 對每場比賽，每個策略推薦其最看好的馬
- 若 ≥2 個策略推薦同一匹馬，下注該馬
- 若意見分歧，跳過

**實作位置**：需新增 `ensemble.py`，從 `models/*/results/{date}/predictions.json` 讀取多個策略的輸出。

**預期效果**：合奏策略通常 ROI 略低於最佳單一策略，但變異數大幅降低。

---

## Part G — 未來工作 (待程式碼變更)

以下變體需要修改 `backtest.py` 才能實現。請依優先順序進行：

| 變體 | 需要的程式變更 |
|------|---------------|
| 跑馬地專用策略 | 加入 `train_filter` / `predict_filter` 支援 course 篩選 |
| 沙田專用策略 | 同上 |
| 短途專家策略 (≤1200m) | `train_filter`/`predict_filter` 支援 distance 範圍 |
| 高班專家策略 (Class 1-3) | `train_filter`/`predict_filter` 支援 class 範圍 |
| 連贏 (Q) 策略 | 計算 Harville Q 機率，與市場 Q 賠率比較，新增 Q P&L 追蹤 |
| 位置 (Place) 策略 | 計算 Harville Place 機率 (top-3) |
| 凱利下注大小 | 將 `bet_edge_threshold` 改為 Kelly 比例計算 |
| 集合學習 (Ensemble) | 新建 `ensemble.py` 讀取多策略輸出 |

---

## 附錄：快速命令參考

```bash
# 列出所有策略
ls models/

# 看某策略當前設定
cat models/{策略名稱}/config.json | python3 -m json.tool

# 跑單一日期 (快速測試)
python3 backtest.py --model {策略名稱} 2026-04-26

# 跑日期範圍
python3 backtest.py --model {策略名稱} --from 2026-01-01 --to 2026-04-29

# 跑全部歷史 (慢, 3-4 小時)
python3 backtest.py --model {策略名稱} --all

# 強制重算
python3 backtest.py --model {策略名稱} --all --force

# 設為使用中 (即預設 dashboard 顯示)
python3 -c "from model_config import set_active_model; set_active_model('{策略名稱}')"

# 看總結
python3 -c "
import json
with open('models/{策略名稱}/results/summary.json') as f:
    print(json.dumps(json.load(f), indent=2, ensure_ascii=False))
"

# 發布到 production (覆蓋 predictions/ 資料夾)
python3 backtest.py --model {策略名稱} --all --publish
```

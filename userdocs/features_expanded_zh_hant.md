# 賽馬預測特徵總覽（全球擴充版）

## 摘要

本檔以使用者現有 48 個特徵、13 個類別為基礎，結合 Benter 1994 香港 syndicate 報告、Brisnet Ultimate PPs（US）、Timeform UK Master Ratings、Daily Racing Form Beyer/Formulator、Equibase Speed & Pace Figures、Hong Kong Jockey Club（HKJC）公開數據、Japan Racing Association（JRA-VAN）資料慣例、Korean LtR XGBoost/CatBoost 研究（2024-2025）、Kaggle pbovard63 / cullensun HK notebook、Patrick Veitch 80-factor 方法、Ranogajec syndicate 公開描述、Snowberg-Wolfers favourite-longshot 文獻、Harville / Henery / Plackett-Luce 序位模型、Punter2Pro CLV 指南，以及 Equimetre / RaceiQ 等 GPS-IMU 生物力學裝置文獻擴充。最終整理 **174 個特徵**、**16 個類別**（新增 14. 市場訊號、15. 場地動態、16. 生物力學/外部數據三個全新類別）。所有新增條目以 *新增* 標註，並以方括號編號引用至附錄 B 文獻表。

## 類別總覽

| #  | 類別 | 既有特徵數 | 擴充後特徵數 | 新增重點 |
|----|------|-----------|-------------|----------|
| 1  | 馬匹檔案 | 4  | 14 | 性別、馬色、出生月份/半球、體重變動、父系/母系、引進來源、種公等級 |
| 2  | 勝率與報酬指標 | 5  | 14 | 入位率、三甲率、ROI、A/E、IV、條件細分、Bayesian 收縮、衰減 |
| 3  | 適應性 | 2  | 10 | 父系適性、馬齡×距離、左右轉、上下坡、季節、出閘速度、復原時間 |
| 4  | 練馬師狀態 | 2  | 11 | 班次優勢、距離強項、季節熱度、首戰勝率、復出馬、馬房規模 |
| 5  | 閘號 | 4  | 11 | 距首彎距離、起閘速度、賽道分段閘號偏差、草地內外欄 |
| 6  | 負磅 | 2  | 10 | 馬匹體重、體重變動、騎師體重、性別讓磅、減磅、鞍具 |
| 7  | 賽事背景 | 5  | 13 | 獎金、賽事等級、季節、天氣、風速、草地高度、開賽時間 |
| 8  | 近期狀態 | 4  | 13 | 連勝/連敗、上場名次、上場敗距、上場分段、試閘、晨操 |
| 9  | 裝備與獸醫 | 2  | 11 | 用藥變動、蹄鐵、馬銜、首/末次眼罩、獸醫紀錄、roarer 紀錄 |
| 10 | 步速與跑法 | 7  | 14 | 早段/晚段速度指數、配速壓力、生存者/受益者、領跑指數 |
| 11 | 綜合速度與班次指標 | 2  | 11 | Beyer、Timeform、RPR、Brisnet Prime Power、Topspeed、CHRI |
| 12 | 交互特徵 | 7  | 14 | 距離×場地、騎師×場地、父系×距離、季節×場地、用藥×場地 |
| 13 | 連贏與序位結構 | 2  | 9  | Harville/Henery/Plackett-Luce、三甲組合、Pick 4/5/6、相鄰閘 |
| 14 | 市場訊號 *全新類別* | 0  | 11 | 開盤/收市賠率、走勢、隱含概率、overround、BSP、CLV、深度 |
| 15 | 場地動態 *全新類別* | 0  | 8  | 當日場地偏差、內欄/外欄使用、灌溉、草長、風向 |
| 16 | 生物力學/外部數據 *全新類別* | 0  | 10 | GPS 心率、步幅、步頻、騎師策略傾向、疲勞指數 |
| **合計** |  | **48** | **174** | |

---

## 1. 馬匹檔案

### 既有特徵
- **馬齡** — 馬匹年齡（年），影響成熟度與耐力 [B1]。
- **騸馬標記** — 是否為閹馬（gelding），香港主流類別。
- **殘障評分** — HKJC 公布之 0-145 Handicap Rating。
- **出賽次數** — 累計出賽場數，用於穩定統計量。

### 擴充建議
- **性別** *新增* — colt（雄駒）/filly（雌駒）/gelding（騸馬）/mare（雌馬）；雌馬於高班次（Group 1）有 3 lb 性別讓磅 [B7][B33]。
- **馬色** *新增* — bay/chestnut/grey 等；雖弱訊號，但與部分基因標記相關 [B5]。
- **出生月份** *新增* — 北半球三歲馬中，1-2 月出生者比 6-7 月出生者平均勝率高約 8%；用於 cohort 比較 [B1][B23]。
- **南北半球出生** *新增* — 港地賽季與南半球（澳/紐）出生馬之相對「年齡漂移」差距 [B27][B33]。
- **體重變動（vs 上場）** *新增* — HKJC 公布「Wt.+/- (vs Declaration)」；±10 lb 為顯著訊號 [B34][B27]。
- **馬匹宣告體重（lb）** *新增* — HKJC「Horse Wt. (Declaration)」原始值 [B34][B27]。
- **父系（Sire）** *新增* — 父系標籤 + 全球速度評級；用於 dosage 與場地偏好 [B5][B26]。
- **母系（Dam Sire）** *新增* — 外祖父對距離耐力影響大；常用於 turf/dirt 適性 [B5][B26]。
- **Dosage Index（DI）** *新增* — Brilliant/Intermediate/Classic/Solid/Professional 五段比值，量化速度-耐力比 [B26]。
- **Centre of Distribution（CD）** *新增* — Dosage 平衡點，標示估計理想距離 [B26]。
- **引進來源國** *新增* — AUS / NZL / GB / IRE / FR / USA / RSA 標籤；HKJC 全部進口 [B14][B19]。
- **Griffin 標記** *新增* — 未在港賽過的進口馬，首場為香港首戰 [B19]。
- **PPG 出賽密度** *新增* — 過去 365 日出賽次數，反映馬廄使用程度 [B15]。
- **海外賽歷年期** *新增* — 進口前所在地最高 rating 與征戰年數，用作預測 ceiling [B19]。

### 來源
[B1][B5][B7][B14][B15][B19][B23][B26][B27][B33][B34]

---

## 2. 勝率與報酬指標

### 既有特徵
- **馬匹勝率** — 該馬累計勝率。
- **騎師勝率** — 該騎師累計勝率。
- **練馬師勝率** — 該練馬師累計勝率。
- **騎師×練馬師配對勝率** — 配對勝率。
- **騎師×馬匹配對勝率** — 配對勝率。

### 擴充建議
- **入位率（Place Rate）** *新增* — 第 2 名以內比率（位置 Q） [B2][B16]。
- **三甲率（Top-3 Rate）** *新增* — 進入前三名比率，HK 三重彩相關 [B2][B16]。
- **ROI（$2 為單位）** *新增* — 平均淨報酬，Brisnet 標準呈現 [B2][B16]。
- **A/E 指標（Actual/Expected）** *新增* — 實際勝率 ÷ 賠率隱含勝率；>1.00 即優於市場 [B22][B30]。
- **Impact Value（IV）** *新增* — 該特徵組勝率 ÷ 全體勝率，1.00 為基準 [B22]。
- **ITM%（In-the-Money）** *新增* — 前三名比率（含獎金線） [B2]。
- **條件細分勝率** *新增* — 草地/全天候、距離區段、班次區段、軟硬場地 [B2][B6]。
- **Bayesian 收縮勝率** *新增* — 對小樣本套用 Beta(α,β) 先驗，避免高方差 [B25]。
- **勝率時間衰減** *新增* — 採指數衰減權重 e^(-λΔt)，近期賽事權重較高 [B25]。
- **騎師於跑馬地勝率** *新增* — 跑馬地與沙田分開統計（騎師慣性顯著） [B14][B36]。
- **騎師於沙田勝率** *新增* — 同上 [B14][B36]。
- **練馬師於跑馬地勝率** *新增* — 同上 [B14][B36]。
- **練馬師於沙田勝率** *新增* — 同上 [B14][B36]。
- **三巨頭參賽標記** *新增* — Purton / Bowman / Moreira / Ferraris 旗下馬之 base-rate boost [B14][B40]。

### 來源
[B2][B6][B14][B16][B22][B25][B30][B36][B40]

---

## 3. 適應性

### 既有特徵
- **距離適應率** — 該馬於同距離區段勝率。
- **場地適應率** — 該馬於同類場地（turf/AWT）勝率。

### 擴充建議
- **父系距離適性** *新增* — Sire 後代於本距離之 Brisnet ROI [B2][B26]。
- **父系場地適性** *新增* — Sire 後代於 turf vs dirt 之差距 [B2][B5]。
- **馬齡×距離適性** *新增* — 3yo 對 1600m+ 通常不利 [B14][B23]。
- **左轉/右轉適應** *新增* — 跑馬地右轉、沙田右轉、部分海外賽道左轉 [B14][B36]。
- **上下坡適應** *新增* — 沙田直路有微坡，部分馬偏好；歐洲賽道斜坡顯著 [B36]。
- **季節適應** *新增* — 香港賽季 9 月至 7 月；夏秋 vs 春冬之 going 差異 [B14][B36]。
- **賽事級別適應** *新增* — Class 1-5 + Group/Listed 之分項勝率 [B14][B19]。
- **出閘速度（Jump Speed）** *新增* — 起閘 100m 平均位次，由分段時間估算 [B11][B12]。
- **賽事間復原時間** *新增* — 上場後恢復天數 × 上場負擔指標；用於評估再戰風險 [B17][B25]。
- **試閘場地適性** *新增* — 試閘成績在草地/全天候上差異 [B20]。

### 來源
[B2][B5][B11][B12][B14][B17][B19][B20][B23][B25][B26][B36]

---

## 4. 練馬師狀態

### 既有特徵
- **馬廄熱度** — 練馬師近期勝率移動平均。
- **馬廄冷浪** — 練馬師近期低於長期勝率之偏差。

### 擴充建議
- **班次強項** *新增* — 練馬師於 Class 1-5 各班次 ROI [B2][B6]。
- **距離強項** *新增* — 練馬師按距離區段 ROI（短途 vs 中長途專家） [B2][B6]。
- **季節熱度** *新增* — 季中早/中/末段練馬師 form cycle [B2][B14]。
- **首次出賽勝率** *新增* — 練馬師「first time starter」勝率（debutant angle） [B2][B16]。
- **復出馬勝率** *新增* — 練馬師對歇賽 45-180 日復出馬之 ROI [B17][B25]。
- **場地適應** *新增* — 練馬師跑馬地 vs 沙田勝率差 [B14][B36]。
- **出賽密度** *新增* — 近 30 日入閘次數，反映 stable form pulse [B15][B25]。
- **馬房規模** *新增* — 練馬師現役馬匹數（HKJC 約 60 匹上限） [B14]。
- **練馬師×騎師近期勝率** *新增* — 連續 12 場滑動視窗 [B14][B25]。
- **練馬師裝備改動 ROI** *新增* — 該練馬師對改 gear/blinkers 之 angle ROI [B2][B11]。
- **練馬師×班次×距離三維 ROI** *新增* — Brisnet 標準三維表 [B2][B6]。

### 來源
[B2][B6][B11][B14][B15][B16][B17][B25][B36]

---

## 5. 閘號

### 既有特徵
- **閘號** — 原始閘號數值。
- **內閘** — 1-3 號標記。
- **外閘** — 後 1/3 標記。
- **大外閘** — 末 2 個閘號標記。

### 擴充建議
- **賽道×距離閘號偏差** *新增* — 跑馬地 1000m 內閘強、沙田 1600m 中性 [B36][B41]。
- **距首彎距離** *新增* — 跑馬地起點至第一彎距離短，外閘受罰 [B36][B41]。
- **起閘速度（Gate-break）** *新增* — 由起後 200m 分段位次估算 [B11][B12]。
- **賽道彎度** *新增* — 沙田 turf circumference vs Happy Valley 1450m 圍長 [B36]。
- **草地內欄/外欄使用（Rail position）** *新增* — A / B / C / C+3 賽道，C+3 偏快內側 [B36][B41]。
- **沙田全天候閘號偏差** *新增* — AWT 1200m 外閘相對較佳 [B14][B36]。
- **閘號×跑法交互強化** *新增* — 內閘領跑、外閘追後 Brisnet angle [B11][B12]。

### 來源
[B11][B12][B14][B36][B41]

---

## 6. 負磅

### 既有特徵
- **負磅** — 騎師+鞍具總負重（lb）。
- **減磅優惠** — 見習騎師減磅標記。

### 擴充建議
- **負磅趨勢** *新增* — 較上場負磅之 Δ；handicap 加減反映被市場高估/低估 [B14][B30]。
- **馬匹宣告體重** *新增* — HKJC 公布；亦見類別 1 [B34][B27]。
- **體重變動 Δ** *新增* — HKJC「Wt.+/-」；亦見類別 1 [B34]。
- **騎師體重（個人）** *新增* — 騎師本人體重，獨立於負磅 [B7][B33]。
- **鞍具重量** *新增* — 部分 jurisdictions 公布 saddle weight [B33]。
- **見習騎師 claim 等級** *新增* — 3lb / 5lb / 7lb 分級依勝場數 [B7][B33]。
- **性別讓磅** *新增* — 雌馬 3 lb 補貼 [B7][B33]。
- **馬齡 weight-for-age** *新增* — WFA 表，3yo 對 4yo+ 之讓磅 [B7][B14]。

### 來源
[B7][B14][B27][B30][B33][B34]

---

## 7. 賽事背景

### 既有特徵
- **跑馬地標記** — 是否跑馬地賽事。
- **賽事距離** — 公尺。
- **場地編碼** — 草地 / AWT / good / yielding 等。
- **班次** — Class 1-5、Group 1-3、Listed、Griffin。
- **出賽馬數** — Field size。

### 擴充建議
- **獎金（Prize Money）** *新增* — 1st 獎金 HK$；HKJC 公布；強訊號 [B14][B18]。
- **表列賽 / Group 標記** *新增* — Group 1/2/3、Listed 二元標記 [B18][B19]。
- **國際賽** *新增* — Hong Kong International Races、Champions Day 等 [B14][B19]。
- **賽季階段** *新增* — early/mid/late season；form cycle 與訓練週期相關 [B14][B36]。
- **日夜（D/N）** *新增* — 跑馬地夜賽 vs 沙田日賽 [B14][B36]。
- **天氣預報** *新增* — 比賽日溫度、降雨概率 [B28][B29]。
- **風速與方向** *新增* — 順風頭風影響領跑馬 [B28][B29]。
- **草地高度（Grass length）** *新增* — 維護紀錄影響 going 解讀 [B28][B29][B41]。
- **場地灌溉量（Watering）** *新增* — 賽前用水公分數 [B41]。
- **賽事編號** *新增* — 卡序（Race 1-11）；夜賽序後段速度衰退 [B14]。
- **開賽時間** *新增* — Post time；早班 vs 晚班馬廄表現差 [B14]。
- **獎金分配層級** *新增* — 前 5 名分成比；影響參賽競爭強度 [B14][B18]。

### 來源
[B14][B18][B19][B28][B29][B36][B41]

---

## 8. 近期狀態

### 既有特徵
- **休賽天數** — 自上場日起天數。
- **久休懲罰** — >60 日歇賽折扣。
- **評分趨勢** — Handicap Rating 之 slope。
- **降班** — 由高班降至低班標記。

### 擴充建議
- **連勝紀錄** *新增* — 連續勝場數 streak [B25]。
- **連敗紀錄** *新增* — 連續敗場數 streak [B25]。
- **同齡 cohort 比較** *新增* — 對同齡同期出賽馬之相對成績 [B6][B16]。
- **上場名次** *新增* — Last-start finishing position（強訊號） [B6][B22]。
- **上場敗距（lengths behind）** *新增* — 距勝出者距離；Benter 標準變數 [B1][B6]。
- **上場分段時間殘差** *新增* — 上場 sectional 對 par 的殘差 [B10][B11]。
- **上場終段速度** *新增* — Finishing Speed % (Rowlands)，量化體力分配 [B10][B11]。
- **試閘紀錄（Barrier Trial）** *新增* — HKJC 公布；近 60 日試閘成績與位次 [B20]。
- **晨操工夫（Trackwork）** *新增* — HKJC 公布晨操路程與速度 [B20][B21]。
- **歇賽復出標記** *新增* — 30-89 / 90-180 / >180 日分桶 [B17][B25]。
- **連續同 jockey 標記** *新增* — 連續 N 場由同一騎師上陣（partnership 訊號） [B14][B25]。
- **連續同 trainer 標記** *新增* — 馬匹是否近期換廄 [B14][B25]。
- **上場排名次序變動** *新增* — Last 3 races 名次趨勢線 [B6][B25]。

### 來源
[B1][B6][B10][B11][B14][B16][B17][B20][B21][B22][B25]

---

## 9. 裝備與獸醫

### 既有特徵
- **裝備變動** — 任何 gear change 標記。
- **首次裝備** — first-time gear。

### 擴充建議
- **眼罩變動（Blinkers on/off）** *新增* — first-time blinkers 為強訊號 [B11][B14]。
- **首次馬銜（Bit change）** *新增* — bit / tongue tie 改動 [B11][B14]。
- **蹄鐵變動（Shoeing change）** *新增* — 例如 bar shoe、glue-on [B11]。
- **馬鞍變動** *新增* — synthetic vs leather；冷僻特徵 [B33]。
- **首次配備一身** *新增* — first time visor / cheek piece / blinker hood [B11][B14]。
- **多重裝備同時變動** *新增* — n 個 gear 同時改動標記 [B11][B14]。
- **用藥變動（first-time Lasix）** *新增* — US 標準強訊號；HK 禁用 Lasix 但有其他醫療標記 [B11][B24]。
- **獸醫紀錄（Vet record）** *新增* — HKJC 公布 vetrecord database；近 30 日紀錄 [B38][B39]。
- **Roarer 紀錄** *新增* — HKJC 公布 OVERoar database（喉嚨手術馬匹） [B38]。
- **出閘紀錄不佳（barrier issue）** *新增* — 上場 reluctant to load 標記 [B38]。
- **獸醫禁賽復出標記** *新增* — Off vet 重新入閘首場 [B38][B39]。

### 來源
[B11][B14][B24][B33][B38][B39]

---

## 10. 步速與跑法

### 既有特徵
- **賽事步速** — 預估全場步速等級。
- **跑法風格** — 領跑/緊隨/中游/後上。
- **步速配合** — 自身步速 vs 賽事步速契合度。
- **步速閘位加成** — 閘號與步速交互。
- **後段步速** — Late pace 指標。
- **前段步速** — Early pace 指標。
- **超越位次均值** — 平均超越名次。

### 擴充建議
- **領跑指數（Lead profile）** *新增* — 至第一個 call 領先頻率 [B11][B12]。
- **E1 早段速度指數** *新增* — Brisnet 短途首 2F / 中距首 4F 速度 figure [B11][B16]。
- **E2 早段速度指數** *新增* — Brisnet 短途首 4F / 中距首 6F 速度 figure [B11][B16]。
- **Late Pace / LP** *新增* — Brisnet 後段 figure（補既有「後段步速」之 normalised 版） [B11][B16]。
- **Finishing Speed %（FSP）** *新增* — Simon Rowlands Timeform 公式 [B10][B3]。
- **分段時間殘差（Sectional residual）** *新增* — sectional vs par sectional 之差 [B10][B11]。
- **配速壓力指數（Pace pressure）** *新增* — 場上 early-speed 馬數量 [B12][B16]。
- **配速生存者（Pace survivor）** *新增* — 上場面對 hot pace 仍 finishing well 的馬 [B12][B16]。
- **配速受益者（Pace beneficiary）** *新增* — closer 對應 hot pace 加成 [B12][B16]。
- **跑法純度** *新增* — 同跑法佔過往出賽比例（穩定性） [B11][B12]。
- **預估配速圖（Pace map）** *新增* — Equibase / EquinEdge 視覺化排序變數 [B16][B22]。
- **步速 × 場地** *新增* — 軟地利好 closer [B11][B28]。
- **配速壓力 × 距離** *新增* — 1000m 與 2000m 壓力臨界不同 [B12][B36]。
- **配速殘差 × 賽道** *新增* — 跑馬地 / 沙田分別建模 [B36][B41]。

### 來源
[B3][B10][B11][B12][B16][B22][B28][B36][B41]

---

## 11. 綜合速度與班次指標

### 既有特徵
- **冷廄外閘交互** — 內部指標。
- **CHRI 指數** — 系統綜合 composite。

### 擴充建議
- **Beyer Speed Figure** *新增* — DRF 標準速度評級，已對 track variant 校正 [B4][B22]。
- **Timeform Master Rating** *新增* — UK/EU 主流；含 pace 與 going 調整 [B3]。
- **Timeform actual rating（per race）** *新增* — 每場實際表現分；可低於 master [B3]。
- **Racing Post Rating（RPR）** *新增* — UK alternative；含 weight 與 form [B3]。
- **Brisnet Prime Power** *新增* — Brisnet composite，含 speed/pace/class [B2][B16]。
- **Brisnet Class Rating（CR）** *新增* — 該馬於各場之 class figure [B2][B16]。
- **Topspeed** *新增* — Racing Post 純時間 figure [B3]。
- **Equibase Speed Figure** *新增* — 含 ITV / DTV 雙層校正 [B16][B22]。
- **Equibase Pace Figure** *新增* — 早/中/晚段 [B16][B22]。
- **Ragozin / Thoro-Graph sheets** *新增* — US figure-maker，數字越低越好 [B4][B16]。
- **AE 指標（複合）** *新增* — 多 sub-condition 加權 A/E [B22][B30]。

### 來源
[B2][B3][B4][B16][B22][B30]

---

## 12. 交互特徵

### 既有特徵
- **內閘領跑** — 內閘 × 領跑 booster。
- **外閘追後** — 外閘 × 後上 booster。
- **閘號×跑馬地** — 跑馬地特有 draw bias。
- **閘號×場地** — 全天候 vs 草地。
- **內閘慢步** — 內閘 × 慢步 penalty。
- **外閘快步** — 外閘 × 快步 booster。
- **後段×外閘** — 後上 × 外閘 booster。

### 擴充建議
- **距離×場地** *新增* — turf 1600m 與 AWT 1600m 訓練不同模型 [B14][B16][B41]。
- **班次×馬齡** *新增* — 3yo 升 Class 2 之 ROI [B6][B23]。
- **騎師×場地** *新增* — Bowman 於跑馬地強項 [B14][B36]。
- **騎師×距離** *新增* — Moreira 短途 vs 中距 ROI 差 [B14][B36]。
- **練馬師×場地** *新增* — 部分練馬師偏 sand [B14][B36]。
- **練馬師×班次** *新增* — 同類別 Brisnet angle [B2][B6]。
- **父系×場地** *新增* — Galileo 後代 turf 強 [B5][B26]。
- **父系×距離** *新增* — Dosage 已部分編碼 [B26]。
- **季節×場地** *新增* — 春雨期軟地 closer 強 [B28][B36]。
- **步速×班次** *新增* — 高班次 pace 更兇，closer 加成 [B12][B16]。
- **用藥×場地** *新增* — Lasix × dirt 強訊號（US，HK 不適用） [B11][B24]。
- **馬齡×距離** *新增* — 已於類別 3 列出；保留交互槽 [B14][B23]。
- **天氣×場地** *新增* — 雨後 turf 漸軟 [B28][B29][B41]。
- **賽事編號×場地** *新增* — 後段賽事 turf 受踐踏耗損 [B14][B41]。

### 來源
[B2][B5][B6][B11][B12][B14][B16][B23][B24][B26][B28][B29][B36][B41]

---

## 13. 連贏與序位結構

### 既有特徵
- **Q 跑法互補** — 連贏組合跑法互補性。
- **Q 競爭強度** — 連贏組合競爭強度。

### 擴充建議
- **Harville 公式衍生** *新增* — 由 P(win) 推 P(1-2) 與 P(1-2-3) [B8][B9]。
- **Henery 修正** *新增* — assume exponential running times；對 longshot 修正 [B8][B9]。
- **Plackett-Luce 似然** *新增* — listwise ranking model；訓練 LtR [B9][B31]。
- **Discounted Harville** *新增* — PaceAdvantage 社群對 Harville 之高賠率折扣 [B8]。
- **三甲組合（Trifecta）特徵** *新增* — 各 1-2-3 順序機率向量化 [B8][B16]。
- **位置 Q（Place Quinella）** *新增* — HKJC 特有 PQ 池 [B14]。
- **四連環（Superfecta）** *新增* — 1-2-3-4 順序機率向量 [B16]。
- **Pick 4 / 5 / 6 共識** *新增* — 跨場 multi-leg 連帶；single + spread [B16][B37]。
- **相鄰閘號相關性** *新增* — adjacent draw 之 race interference 機率 [B36][B41]。

### 來源
[B8][B9][B14][B16][B31][B36][B37][B41]

---

## 14. 市場訊號 *全新類別*

### 擴充建議
- **開盤賠率** *新增* — Opening odds；snapshot 1 [B30][B35]。
- **收市賠率（SP）** *新增* — Final tote / fixed-odds SP [B30][B35]。
- **賠率走勢（drift / steamer）** *新增* — Δ(open→close) 百分比；steamer = 顯著縮短 [B32][B35]。
- **隱含概率（Implied probability）** *新增* — 1 / odds，未扣 takeout [B30][B35]。
- **公眾資金集中度** *新增* — Top-3 押注佔池比例 [B14][B22]。
- **莊家空間（Overround）** *新增* — Σ(1/odds) − 1；HK 純 pari-mutuel takeout ≈ 17.5% [B30][B42]。
- **Betfair Starting Price（BSP）** *新增* — 交易所 SP，全球 sharper benchmark [B13][B32]。
- **CLV（Closing Line Value）** *新增* — 押注時隱含概率 − 收市隱含概率；正值即勝過市場 [B13][B32]。
- **交易所深度** *新增* — Lay/back depth at each price tick [B42]。
- **晚段走勢（Late steam）** *新增* — 最後 15-30 分鐘移動；資訊質量最高 [B32][B35]。
- **多平台一致性** *新增* — 多個莊家 / 池同步移動 = sharp signal [B32][B35]。

### 來源
[B13][B14][B22][B30][B32][B35][B42]

---

## 15. 場地動態 *全新類別*

### 擴充建議
- **當日場地偏差** *新增* — 第 1-3 場 winners 之 running style 分布 [B41][B43]。
- **同日 par-time 殘差** *新增* — 本日場次時間 vs par 時間 [B10][B41]。
- **rail position（A/B/C/C+3）** *新增* — HKJC 三個 turf 配置；C+3 偏內側速度 [B36][B41]。
- **賽前灌溉公分** *新增* — Watering record [B41]。
- **草長度公分** *新增* — Grass cutting record [B28][B41]。
- **同日內欄勝率殘差** *新增* — 本日內欄表現偏離長期 [B41][B43]。
- **賽事間風向變化** *新增* — 早午晚段風向；領跑馬受影響 [B28][B29]。
- **同日 closer 加成** *新增* — 後上馬於本日是否異常 winning [B12][B41]。

### 來源
[B10][B12][B28][B29][B36][B41][B43]

---

## 16. 生物力學 / 外部數據 *全新類別*

### 擴充建議
- **GPS 平均速度** *新增* — Equimetre / Arioneo / RaceiQ devices [B43][B44][B45]。
- **GPS 最高速度** *新增* — Peak velocity m/s [B43][B44]。
- **步幅長度（Stride length）** *新增* — Sprinter 與 stayer 顯著差異 [B43][B44][B45]。
- **步頻（Stride frequency）** *新增* — FFT-derived；可由 GPS oscillation 求 [B43][B45]。
- **心率峰值** *新增* — Equinity / Equisense；recovery time 後續推算 [B43][B44]。
- **恢復時間** *新增* — Workout 後 5 min 心率回落 [B43][B44]。
- **疲勞指數（Fatigue index）** *新增* — 體溫、累計負荷估算 [B43][B44]。
- **訓練負荷（Workload）** *新增* — 過去 7 日距離累計 [B43][B44]。
- **騎師策略傾向** *新增* — Brohamer / Asmussen 風格；領跑/守候比例 [B40]。
- **練馬師訓練模式** *新增* — high-mileage vs short-sharp，影響 layoff recovery [B17][B25]。

### 來源
[B17][B25][B40][B43][B44][B45]

---

## 附錄 A: 全球運營者特徵側重對照

| 運營者 / 來源 | 強調的特徵類別 | 對 HK 適用性 |
|--------------|---------------|--------------|
| Benter HK syndicate (1994) | 市場訊號、normalised finishing、weight、draw、jockey contribution、multinomial logit | 高（本系統母體） |
| Ranogajec / Marantelli | 池深度、CLV、rebate、低 edge × 高量、worldwide pari-mutuel | 高 |
| Brisnet (US) | Prime Power、E1/E2/Late、Class Rating、Trainer/Jockey ROI、Sire stats | 中-高（HK 缺 dirt） |
| Timeform (UK/EU) | Master Rating、actual rating、Finishing Speed %、equipment、going | 高（HK turf 為主） |
| DRF / Beyer (US) | Beyer figure、Formulator pace figures、sectionals、Ragozin sheets | 中 |
| Equibase (US) | ITV/DTV 校正、Pace Style Figures、Class Rating | 中 |
| JRA-VAN (JP) | Corner passing order、lap times、jockey/trainer × time、ML-friendly schema | 高（亞洲 turf） |
| Korean LtR (2024) | LambdaRank、近期成績、平均名次、weight、age | 高（近鄰市場） |
| Patrick Veitch (UK) | 80 變量 form、jockey allowance、claim、layoff | 中 |
| Snowberg-Wolfers | Favourite-longshot calibration、bias correction | 高（calibration） |
| Hausch-Lo-Ziemba | Harville/Henery/Stern、exotic pool inversion | 高（HK 多 exotic） |
| pbovard63 (Kaggle HK) | 3-race rolling、days-since-last、rank vs field、top-10 jockey/trainer | 直接適用 |
| RaceHP.ai | 144 features、real-time weather、bias patterns、deep learning | 適用框架 |

---

## 附錄 B: 參考文獻

- [B1] Benter, W. (1994). *Computer Based Horse Race Handicapping and Wagering Systems: A Report*. In Hausch et al., Efficiency of Racetrack Betting Markets. https://gwern.net/doc/statistics/decision/1994-benter.pdf
- [B2] Brisnet. *Ultimate Past Performances & Trainer Stats*. https://www.brisnet.com/library/uwc.pdf ; https://www.brisnet.com/racing/news/trainer-stats/
- [B3] Timeform. *Master Ratings & How Ratings for a Race are Calculated*. https://www.timeform.com/horse-racing/features/timeform-ratings/how_the_ratings_for_a_race_are_calculated
- [B4] Daily Racing Form. *Beyer Speed Figures*. https://promos.drf.com/Beyer ; https://promos.drf.com/beyerarchive
- [B5] BloodHorse. *Sire Lists & Turf/Dirt breakdown*. https://www.bloodhorse.com/horse-racing/thoroughbred-breeding/sire-lists/
- [B6] EquinEdge. *Class Drop, Class Rise, Layoff & Pace handicapping glossaries*. https://equinedge.com/glossary/handicapping/what-is-a-class-drop ; https://equinedge.com/glossary/handicapping/layoff
- [B7] PaddyPower. *What advantages do apprentice jockeys get*. https://news.paddypower.com/guides/2023/02/01/apprentice-jockeys-advantages-allowance-conditions-professionals-horse-racing-betting/
- [B8] Mash, Stuart. *HorsePackage – Harville model implementation*. https://github.com/stumash/HorsePackage ; PaceAdvantage forum on Discounted Harville. http://www.paceadvantage.com/forum/archive/index.php/t-35264.html
- [B9] Lo, V., Bacon-Shone, J. *Approximating the ordering probabilities of multi-entry competitions* (Henery / Plackett-Luce review). World Scientific compendium back matter. https://www.worldscientific.com/doi/pdf/10.1142/9789812819192_bmatter
- [B10] Timeform / Rowlands, S. *Finishing Speed Percentage*. https://raceiq.com/par-sectionals-fsp/ ; https://www.attheraces.com/sectionalsinfo
- [B11] EquinEdge. *Running Style, Pace Map, First-time Blinkers, Second-time Lasix*. https://equinedge.com/glossary/key-factors/running-style ; https://equinedge.com/glossary/key-factors/second-time-blinkers-or-lasix
- [B12] Betting Gods. *Pace Makes the Race – pacesetters vs closers*. https://bettinggods.com/horse-racing/pace-makes-the-race-an-advanced-guide-to-predicting-race-tempo/
- [B13] Punter2Pro. *Beating the Closing Line (CLV) – the key to successful sports betting*. https://punter2pro.com/punters-guide-beating-the-sp/
- [B14] HKJC. *Race Card, Handicapping Policy, Trainer & Jockey Rankings*. https://racing.hkjc.com/racing/english/learn-racing/learn-question.aspx ; https://racing.hkjc.com/racing/english/racing-info/handicap_policy.asp
- [B15] HKJC. *Trainer Ranking*. https://racing.hkjc.com/racing/information/English/Trainers/TrainerRanking.aspx
- [B16] TwinSpires. *Brisnet Speed and Pace ratings, Pick 4 strategy*. https://www.twinspires.com/edge/racing/what-are-brisnet-speed-and-pace-ratings/ ; https://www.twinspires.com/betting-guides/how-to-bet-pick-4-horse-racing/
- [B17] EquinEdge. *Layoff & Race Spacing*. https://equinedge.com/glossary/key-factors/race-spacing
- [B18] Wikipedia. *Graded stakes race & Group races (purse thresholds)*. https://en.wikipedia.org/wiki/Graded_stakes_race ; https://en.wikipedia.org/wiki/Group_races
- [B19] RacingBet. *Hong Kong racing: understanding the class system & Griffins*. https://www.racingbet.com.au/hong-kong-racing-understanding-the-class-system/
- [B20] HKJC. *Barrier Trials*. https://racing.hkjc.com/racing/information/english/Horse/Btresult.aspx
- [B21] HKJC. *Trackwork*. https://racing.hkjc.com/en-us/local/information/localtrackwork
- [B22] EquinEdge. *Impact Value, A/E, Pool Impact Value, Value Bet*. https://www.flatstats.co.uk/blog/impact-values.html ; https://equinedge.com/glossary/handicapping/value-bet
- [B23] Aldous, D. (Berkeley). *Order statistics of horse racing and the randomly broken stick*. https://arxiv.org/pdf/1612.02567
- [B24] Pubmed. *Review of furosemide (Lasix) in horse racing*. https://pubmed.ncbi.nlm.nih.gov/9673965/
- [B25] Journal of Prediction Markets. *A Hierarchical Bayesian Analysis of Horse Racing*. https://www.ubplj.org/index.php/jpm/article/view/590
- [B26] Wikipedia / Pedigree Online. *Dosage Index, Dosage Profile, Centre of Distribution*. https://en.wikipedia.org/wiki/Dosage_Index ; https://www.pedigreeonline.com/knowledgebase/reports/dosage-profile-chef-de-race
- [B27] HKJC. *How To Use This Report – Exceptional Factors (weight indicators)*. https://www.hkjc.com/English/help/ExceptionHelp_eng.htm
- [B28] Punter2Pro. *Impact of Weather on Horse Racing & Racecourses*. https://punter2pro.com/impact-weather-horse-racing-betting/
- [B29] Arioneo. *Weather conditions: performance of racehorses*. https://training.arioneo.com/en/weather-conditions-how-do-they-affect-the-performance-of-racehorses/
- [B30] FlatStats. *Impact Values & A/E in betting markets*. https://www.flatstats.co.uk/blog/impact-values.html
- [B31] arXiv. *Learning-to-Rank with Partitioned Preference: Fast Estimation for the Plackett-Luce Model*. https://arxiv.org/pdf/2006.05067
- [B32] EquianalytiX. *Market Movers Horse Racing – Odds Movements Guide*. https://www.equianalytix.com/hubs/odds-movements-market-movers
- [B33] PaddyPower. *Colt, filly, gelding, stallion, mare – sex allowance*. https://news.paddypower.com/guides/2024/02/02/difference-between-colt-filly-gelding-stallion-mare-horse-racing/
- [B34] HKJC. *Form Guide tutorial – Horse Wt. (Declaration) & Wt.+/-*. https://www.hkjc.com/english/formguide/tutorial.html
- [B35] Precision Tipsters. *Understanding Market Moves – Drifters & Steamers*. https://precisiontipsters.co.uk/2025/10/21/understanding-horse-racing-market-moves/
- [B36] PuntLab. *Barrier Draws & Track Bias – Australian Racing analysis style applied to HK*. https://thepuntlab.com/barrier-draws-track-bias/
- [B37] Wikipedia. *Pick 6 (horse racing)*. https://en.wikipedia.org/wiki/Pick_6_(horse_racing)
- [B38] HKJC. *Veterinary Records Database*. https://racing.hkjc.com/racing/information/english/veterinaryrecords/ovedatabase.aspx
- [B39] HKJC. *Roarers Database*. https://racing.hkjc.com/racing/information/english/VeterinaryRecords/OVERoar.aspx
- [B40] CDC Gaming. *Syndicates, algorithms, and beating the horses in Hong Kong – Benter, Ranogajec / Marantelli context*. https://cdcgaming.com/commentary/syndicates-algorithms-and-beating-the-horses-in-hong-kong/
- [B41] RaceHP.ai. *Track Bias in Horse Racing – identification & use*. https://racehp.ai/glossary/track-bias/
- [B42] EquinEdge / Cheltenham. *Liquidity & Overround in Exchange Betting*. https://www.cheltenhambettingoffers.com/news/what-is-liquidity-in-exchange-betting-and-why-does-it-matter-for-betting-on-horse-racing/ ; https://equinedge.com/glossary/racing-data-and-statistics/what-is-overround-in-horse-racing-betting-pools
- [B43] RaceiQ. *Stride Data – wearable GPS analysis*. https://raceiq.com/stride-data/
- [B44] BEVA Equine Veterinary Education. *Wearable commercially available biometric-monitoring devices in gallop racing* (Kee, 2023). https://beva.onlinelibrary.wiley.com/doi/full/10.1111/eve.13800
- [B45] PMC. *Locomotory Profiles in Thoroughbreds: Peak Stride Length and Frequency*. https://www.ncbi.nlm.nih.gov/pmc/articles/PMC9741461/
- [B46] Korea Science. *Horse race rank prediction using learning-to-rank approaches* (Korean Journal of Applied Statistics, 2024). https://koreascience.kr/article/JAKO202414143309228.page
- [B47] Korean Society of Computer & Information. *Machine Learning-based LTR for Horse Race Prediction* (2024-2025 dataset, CatBoost / XGBoost / LightGBM). https://journal.kci.go.kr/jksci/archive/articleView?artiId=ART003266151
- [B48] Snowberg, E. & Wolfers, J. (2010). *Explaining the Favorite-Longshot Bias: Is it Risk-Love or Misperceptions?* Journal of Political Economy 118(4). https://eriksnowberg.com/papers/Snowberg-Wolfers%20Risk%20Love%20or%20Decision%20Weights3.pdf
- [B49] Veitch, P. *Enemy Number One* (80-factor handicapping methodology). https://thedarkroom.co.uk/patrick-veitch-the-mathematical-genius-of-professional-horse-racing-gambling/
- [B50] pbovard63. *Predicting_Hong_Kong_Horse_Racing_Finishes* – Kaggle HK feature engineering reference. https://github.com/pbovard63/Predicting_Hong_Kong_Horse_Racing_Finishes
- [B51] cullensun (Kaggle). *Deep Learning Model for Hong Kong Horse Racing*. https://www.kaggle.com/code/cullensun/deep-learning-model-for-hong-kong-horse-racing
- [B52] Equibase. *Using Speed and Pace Figures and Class Ratings – ITV / DTV / Pace Style Figures*. https://www.equibase.com/products/speedpace.pdf
- [B53] Lady & The Track. *Explaining DRF, BRIS and TimeformUS Past Performances*. https://ladyandthetrack.com/ladys-guide-horse-racing/explaining-drf-bris-timeformus-past-performances

---

*文檔結束 — 174 個特徵，16 個類別，53 條編號文獻。*

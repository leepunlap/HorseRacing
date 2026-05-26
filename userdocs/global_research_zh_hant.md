# 《優勢的架構》

## 關於賽馬預測、市場機制與風險紀律的全球量化白皮書

*版本 1.0 — 2026年5月*

---

## 摘要

本白皮書圍繞四大支柱,系統梳理了全球賽馬預測的實踐現狀:(i) 用於估計完賽順序概率的統計與機器學習**方法**;(ii) 將這些概率轉化為資本的**市場機制** —— 同注分彩彩池、固定賠率莊家以及投注交易所;(iii) 將優勢轉化為長期增長的**注碼與資金管理**框架;(iv) 區分"運營數十年仍能存活"與"一個賽季內全軍覆沒"的**安全機制、硬性止損以及人在迴路控制**。

研究結論汲取自經典學術文獻 —— Bolton & Chapman 1986 [1]、Benter 1994 [2]、Harville 1973 [4]、Henery 1981 [5]、Plackett 1975 [6]、Hausch-Lo-Ziemba 文集 [3]、Snowberg & Wolfers 關於熱門—冷門偏差的研究 [12] —— 汲取自最成功的職業聯合投注集團的公開運營資料 (Benter [21]、Woods [23]、Ranogajec [19]、Bloom [20]、Veitch [24]、Colossus Bets [22]),汲取自商業供應商文檔 (EquinEdge [27]、RaceHP.ai [28]、Brisnet [30]、DRF [33]、Timeform),汲取自監管原始資料 (HKJC [37, 38]、UKGC [49, 50]、JRA [40]、PMU [41]),以及關於模型校準 [44–47]、漂移檢測 [48] 和交易系統熔斷機制 [53, 54] 的工程文獻。

貫穿所有可信參考資料的核心教訓是一致的:一個成功的運營大致由**20%的建模、30%的市場機制(返水、彩池深度、滑點)以及50%的風險紀律與運營**構成。幾乎所有有據可查的崩盤都是風險控制失敗,而非建模失敗。最重要的方法論洞見仍是 Benter 的兩階段 logit [2] —— 將私有基本面模型與市場隱含概率融合;最重要的存活機制仍是分數 Kelly 注碼 [16, 17] 配合熔斷式止損。

本白皮書的目標讀者是運行一個帶有 bet-max-odds 過濾、NaN 賠率防護以及校準後優勢核算的 XGBoost 滾動前向 (walk-forward) 流水線的運營者,可據此將其方法、市場信號與安全控制與全行業進行對標。第 1 節按重要性從高到低對 58 個條目排序。第 2–6 節分別詳細展開每個支柱,並交叉引用編號參考文獻(第 7 節)。

---

## 目錄

1. 主排序列表(重要性從高到低)
2. 預測方法
3. 框架與運營者
4. 注碼與資金管理
5. 安全機制、硬性止損與人在迴路
6. 監管與司法管轄區註釋
7. 參考文獻

---

## 1. 主排序列表(重要性從高到低)

排序標準:綜合考慮 (a) 優勢的有據可查程度、(b) 嚴肅職業玩家的採用程度、(c) 跨市場和跨時間的穩健性、(d) 出錯的代價。排名靠前的條目是文獻視為不可妥協的內容。

| 排名 | 條目 | 類別 | 一句話描述 | 排名理由 | 參考 |
|------|------|------|------------|----------|------|
| 1 | 市場融合概率(Benter 兩階段 logit) | 方法 | 在第二階段 logit 中將私有模型與公眾隱含概率組合 | 文獻中最重要的單一方法論洞見;市場共識是任何一場比賽中最強的單一特徵 | [2, 8] |
| 2 | 分數 Kelly 注碼 | 注碼 | 下注完整 Kelly 注碼的固定比例(常為 1/4 至 1/2) | 帶估計誤差的完全 Kelly 是毀滅性的;分數 Kelly 是所有有據可查盈利運營的已證生存機制 | [16, 17, 18] |
| 3 | 返水 / 佣金談判 | 運營 | 從彩池 / 交易所獲得的損失返點或佣金減免 | Ranogajec 的優勢主要靠返水驅動 [19];HKJC 對大彩池提供 10–12% 的損失返點 [37, 38];可將盈虧平衡模型轉為盈利模型 | [19, 37, 38] |
| 4 | 滾動前向 (walk-forward) / 樣本外驗證 | 安全 | 在過去數據上訓練,在嚴格更晚的數據上評估 | 其他任何驗證方案都會悄然泄露信息;沒有這一步,所有報告的投資回報率都是虛構 | [51, 52] |
| 5 | 概率校準(Platt / 等距) | 方法 | 將原始模型分數映射為真實頻率 | 在未校準概率上做 Kelly 注碼會令下注規模偏離 2–5 倍,摧毀本金 | [44, 45, 46] |
| 6 | 彩池 / 流動性意識 | 安全 | 將下注規模設為彩池深度的一個比例上限 | 即便本金無限,最優下注仍是有限的 —— Benter 證明決定上限的是彩池規模而非資金 | [2] |
| 7 | 條件 logit (Bolton-Chapman 1986) | 方法 | 對一場比賽中所有參賽馬進行多項式 logit | 1980 年代以來幾乎所有嚴肅賽馬模型的基礎統計框架 | [1] |
| 8 | XGBoost / LightGBM / CatBoost 梯度提升 | 方法 | 在工程化表格特徵上的樹梯度提升 | 經驗上最強的表格 ML 家族;在 Kaggle 和賽馬學術基準上居於主導 | [9, 10, 11] |
| 9 | 速度指數(Beyer / Timeform / RPR) | 特徵 | 考慮時間與賽道變異的表現數值 | 經典的讓分原始量;每個模型都直接或隱式地嵌入它們 | [33, 34] |
| 10 | 節奏 / 分段時間分析 | 特徵 | E1、E2、晚段節奏分段及預測比賽走勢 | 將原始時間與比賽動態區分開來;對行程敏感的模型必不可少 | [34] |
| 11 | 騎師—練馬師組合統計 | 特徵 | 騎師、練馬師及其搭檔的勝率、進入前三率、投資回報率 | 普遍使用;在每個商業產品(Equibase、Brisnet、EquinEdge)中均出現 | [27, 30, 34] |
| 12 | 止損 / 回撤熔斷機制 | 安全 | 在 −X% 日 / 周 / 單場比賽回撤時停止投注 | 長期存活職業玩家最普遍的單一規則(通常為日 10%) | [16, 53] |
| 13 | 最高賠率過濾 | 安全 | 拒絕下注高於某價格上限的馬 | 冷門帶來的方差主導投資回報率;高賠率下注也是校準誤差代價最高的地方 | [12, 44] |
| 14 | NaN / 缺失賠率防護 | 安全 | 顯式拒絕賠率為非數值的參賽馬 | 微妙的 bug 類:`float('nan')` 在 `<=` 與 `>=` 比較中均會靜默通過 | — |
| 15 | 賽道 / 檔位 / 場地偏差建模 | 特徵 | 按賽道、按距離、按場地的檔位與節奏偏差 | 持續性的本地優勢;數據充足的聯合投注集團廣泛利用 | [3] |
| 16 | Harville (1973) 排序公式 | 方法 | 從勝出概率近似得到位置 / 入圍 / 連贏概率 | 異型投注定價的默認近似;有偏但可計算 | [4] |
| 17 | Henery (1981) 排序細化 | 方法 | Harville 在正態完賽時間上的推廣 | 對 2/3 名更準確;嚴肅異型模型的標準升級 | [5] |
| 18 | Plackett-Luce 排名模型 | 方法 | 對參賽馬的順序排名概率 | 現代賽馬學習排序的統計基礎 | [6, 7] |
| 19 | 收盤線價值 (CLV) 跟蹤 | 安全 | 衡量自己的賠率相對於開賽 / 收盤價格 | 模型是否擁有真實優勢的最客觀實時檢驗 | [42] |
| 20 | 漂移 / 價格陡動監測 | 特徵 | 關注賽前賠率的大幅變動,捕捉知情資金 | 內行資金信號;聯合投注集團與交易所交易者廣泛使用 | [42, 43] |
| 21 | 模型校準指標(Brier、log-loss、ECE) | 安全 | 概率質量的定量度量 | 沒有它們就無法檢測模型悄然劣化 | [44, 45, 46] |
| 22 | 概念 / 數據漂移檢測 | 安全 | 在特徵分佈或準確率發生變化時告警 | 在制度變化(賽道翻新、規則修改、新一輪騎師崛起)開始造成虧損之前捕捉到 | [48] |
| 23 | 同注分彩彩池動態建模 | 方法 | 計入自身下注對最終賠率的影響 | 自我衝擊是大注碼下的主導摩擦 | [2, 3] |
| 24 | 級別評分 | 特徵 | 超越原始時間的賽事質量調整 | 跨等級比較所必需;與速度指數互補 | [34] |
| 25 | 逐注人工複核門 | 安全 | 超過閾值的下注需人工簽字 | 任何非瑣碎下注在職業運營中的標準做法 | [55] |
| 26 | 分層貝葉斯模型 | 方法 | 在先驗下分別建模馬匹、騎師、練馬師的隨機效應 | 乾淨地處理小樣本馬(首次出賽)的不確定性 | [13] |
| 27 | 學習排序 (LambdaMART, RankNet) | 方法 | 直接優化逐對 / 逐表排序目標 | 經驗上逐對目標在賽馬上優於逐點目標 | [7] |
| 28 | 自定義利潤形態損失函數 | 方法 | 訓練一個模擬投注收益而非準確率的目標 | LightGBM / XGBoost 自定義目標;與投資回報率對齊更緊密 | [9, 10] |
| 29 | 裝備 / 用藥變更特徵 | 特徵 | 首次戴眼罩、首次使用 Lasix、場地切換 | 強短期狀態信號,在美國賽馬中尤甚 | [33] |
| 30 | 血統 / 父系 / 母系特徵 | 特徵 | 來自育種的場地與距離適應性 | 對參賽次數少或首次跑草地的馬最有用 | [29] |
| 31 | 審計軌跡 / 不可變下注日誌 | 安全 | 對每個模型輸出和每次下注的只追加記錄 | 監管所需;事後覆盤也不可或缺 | [55] |
| 32 | 緊急停止開關 / 死人開關 | 安全 | 在系統異常或操作員無響應時硬性停止 | 借鑑自算法交易實踐;在賽馬領域罕見但正在擴散 | [53, 54] |
| 33 | Dutching | 注碼 | 將注碼分攤到多匹馬以獲得相同收益 | 降低方差但本身不創造優勢 | [58] |
| 34 | Pick 6 / 累積彩池開發 | 策略 | 瞄準正期望值的轉滾彩池 | 經典聯合投注集團玩法;Pick 6 大手筆有數十年記載 | [3, 60] |
| 35 | 異型連贏 / 三連 / 四連模型 | 策略 | 基於 Harville / Henery 在勝率上的多馬投注 | 優勢更高但方差與彩池摩擦更高 | [4, 5, 60] |
| 36 | 莊家與交易所間的套利 / 必贏盤 | 策略 | 在莊家處押注、在交易所反向掛單鎖定利潤 | 利潤微薄 (1–10%) 但模型風險極小;賬户壽命為上限 | [59] |
| 37 | Betfair 上的賽中 / 賽內交易 | 策略 | 在比賽過程中交易賠率波動 | 技能密集;延遲敏感;優勢小但真實 | [43] |
| 38 | Transformer / 表格深度學習 (FT-Transformer, TabNet) | 方法 | 用於表格數據的注意力架構 | 在研究中有競爭力;實踐中很少在賽馬上擊敗調優後的 XGBoost | [11] |
| 39 | 用於實體關係的圖神經網絡 | 方法 | 將馬 / 練馬師 / 騎師建模為圖 | 新興;尚未在生產中證明可規模化 | [11] |
| 40 | 比賽歷史序列上的 LSTM / RNN | 方法 | 將每匹馬的職業視作時間序列 | 在結果預測中應用有限;更多用於生物力學 | [11] |
| 41 | 用於下注規模的強化學習 | 注碼 | 將注碼視作序貫決策問題 | 學術興趣;公開無證據證明已有可盈利的生產部署 | [16] |
| 42 | 現實檢查 / 冷靜期自動化 | 安全 | 強制暫停、入金冷卻期 | 監管強制;對聯合投注集團運營者同樣有用 | [49, 50] |
| 43 | 多彩池聚合(三選優 / 四選優 / 五選優) | 運營 | 按多個彩池價格中最高者結算 | 在澳大利亞有實質影響;縮小實現與顯示賠率之間的差距 | [39] |
| 44 | 在交易所對沖已下注頭寸 | 注碼 | 在更低價反向掛單以鎖定部分利潤 | 降低方差;期望值有微小成本 | [58] |
| 45 | 日內時段 / 流動性窗口限制 | 安全 | 只在流動性最高的最後 N 分鐘下注 | 內行資金來得晚;避免被自己早盤下注推動 | [43] |
| 46 | 自我衝擊(自身下注)賠率模擬 | 方法 | 預測自身注碼如何推動最終賠率 | 在超過約彩池 0.5% 時至關重要;小規模時可忽略 | [2, 3] |
| 47 | 回測中的倖存者偏差防護 | 安全 | 納入退出參賽的馬、被取消的比賽、消亡的聯合投注集團 | 易被忽略,會膨脹歷史投資回報率 | [52] |
| 48 | 前瞻偏差防護 | 安全 | 嚴格的時點特徵快照 | 最常見的回測 bug;部署後即扼殺策略 | [51] |
| 49 | 多司法管轄區監管意識 | 運營 | 香港 / 英國 / 澳大利亞 / 美國 / 日本 / 法國 規則與税收差異 | 決定扣除抽水與佣金後的真實淨優勢 | [37, 38, 39, 40, 41, 49] |
| 50 | 負責任博彩自控措施 | 安全 | 入金 / 損失 / 時間 / 單局上限 | UKGC 等機構要求;同時也是有用的運營紀律 | [49, 50] |
| 51 | 異質模型的集成堆疊 | 方法 | 在元學習器中組合 GBM、LR、NN 輸出 | 準確率提升不大;有時值得為此付出運營複雜度 | [9] |
| 52 | 馬爾可夫鏈比賽仿真 | 方法 | 逐步概率化的比賽進程 | 小眾;對節奏敏感的異型有價值 | [3] |
| 53 | 蒙特卡洛比賽仿真 | 方法 | 通過抽樣多次比賽結果推導收益分佈 | 異型票券構建的標準工具 | [3] |
| 54 | 來自現場觀察的信息優勢 | 特徵 | 看馬圈、汗液、行為 | 老派玩家仍在使用;難以規模化到 ML | — |
| 55 | 情緒 / 新聞 / 社交媒體特徵 | 特徵 | 推文、論壇閒聊、晚期賠率推薦 | 價值邊際;噪聲大;基本已通過賠率漂移捕獲 | — |
| 56 | 超越場地條件的天氣預報特徵 | 特徵 | 風、温度、濕度對狀態的影響 | 影響小,很少有實質 | — |
| 57 | 遺傳 / DNA 表現標記 | 特徵 | 速度基因檢測(如 MSTN) | 真實存在但更多用於購入前,而非比賽日 | — |
| 58 | 僅情緒式或單純跟隨貼士 | 策略 | 在無獨立優勢的情況下跟隨貼士下注 | 證據支持最弱;為完整起見列出 | — |

---

## 2. 預測方法

### 2.1 條件 / 多項式 logit (Bolton & Chapman 1986)

學術開山之作 [1]。多項式 logit 將一場比賽視為離散選擇問題,其中具有最高潛在效用的參賽馬獲勝,參數通過對許多場比賽的極大似然估計得到。該模型是*條件*的,因為選擇集(參賽陣容)逐場不同。Bolton 與 Chapman 在 200 場比賽上用馬匹、騎師以及比賽特定特徵進行估計,在加上冷門側約束後展示了可盈利的下注,並由此奠定了整個量化賽馬文獻的基礎。

### 2.2 Benter 的兩階段 logit (1994)

Bill Benter 發表的論文 *Computer Based Horse Race Handicapping and Wagering Systems: A Report* [2] 是本領域最具影響力的文獻。其架構為:

- **第一階段**:基本面多項式 logit,從特徵(當前狀態、過往表現、調整、比賽情境因素)產生概率 `f_i`。
- **第二階段**:第二個 logit `c_i ∝ exp(α·log(f_i) + β·log(π_i))`,其中 `π_i` 是公眾從賠率得出的隱含概率。

第二階段步驟正是使模型盈利的關鍵。Benter 發現任何基本面模型相對於市場都存在系統性方向偏差;第二個 logit 校正這一偏差,產生無偏的組合概率。報告的偽 R² 增益 ΔR² ≈ 0.018 相對公眾估計已足以在 HKJC 實現 5 年以上的可觀利潤 [21, 37]。Benter 還引入了 Harville 修正(2 名 γ ≈ 0.81,3 名 δ ≈ 0.65)和針對 Kelly 注碼的顯式彩池規模約束 [2]。

### 2.3 Harville / Henery / Plackett-Luce 排序模型

一族排名模型。**Harville (1973)** [4] 最簡單:任何特定順序的概率是各階段歸一化勝率的乘積。**Henery (1981)** [5] 推廣到正態完賽時間,對 2 名和 3 名更準確。**Plackett-Luce** [6, 7] 是更廣泛的序貫排名模型,廣泛用於現代學習排序。儘管 Harville 近似存在已知偏差,但仍是異型彩池估值的主力,Hausch-Lo-Ziemba 文集對此有詳盡記錄 [3]。

### 2.4 梯度提升 (XGBoost / LightGBM / CatBoost)

在表格化賽馬數據上的實證勝出者。學術研究中堆疊 LightGBM、XGBoost、CatBoost、HistGradientBoosting、AdaBoost 與 TabNet 的集成發表了最強的準確率 / 效率權衡 [9]。CatBoost 在某些韓國研究中報告了最佳排序質量(NDCG ≈ 0.89)[10],而 LightGBM 與 XGBoost 因模型體積小、再訓練快而在生產環境中居於主導。自定義利潤形態損失函數(而非 log-loss)是一種新興實踐。

### 2.5 學習排序 (LambdaMART, RankNet, XGBoost Ranker)

一項關於首爾賽馬數據的韓國研究(《Korean Journal of Applied Statistics》, 2024)[10] 顯示,逐對學習(XGBoost / LightGBM / CatBoost Rankers 中的 RankNet、LambdaMART 實現)在賽馬排名預測中優於逐點方法。這與一般學習排序文獻一致:逐對 / 逐表目標比對每匹馬獨立分類更貼合問題結構。

### 2.6 神經網絡 / 深度學習 / Transformer / GNN 方法

神經網絡應用可追溯到 1990 年代(Chen、McClean、McGuirk 等人在小數據集上使用反向傳播、Levenberg-Marquardt、共軛梯度法)。現代研究探索 1D-CNN、FT-Transformer、TabNet 與圖神經網絡。包括 2022 年 arXiv 綜述 *What AI can do for horse-racing?* [11] 在內的共識是,深度學習目前在賽馬錶格數據上尚未擊敗調優後的梯度提升,但確實改變了人們對特徵工程的思考方式。LSTM 在馬匹生物力學(IMU 傳感器數據)中比在結果預測中應用更多。

### 2.7 分層貝葉斯模型

用於在恰當的不確定性下估計潛在的馬匹與騎師效應 [13]。典型實現:組內 OLS 設定先驗,然後 MCMC(配合 Ancillarity-Sufficiency Interweaving)進行分層後驗,再用 WAIC 進行模型選擇。對於有先驗信息(父系、練馬師、試跑時間)但無比賽戰績的首次出賽者尤為有用。

### 2.8 速度指數、節奏、級別

- **Beyer Speed Figures**(Daily Racing Form, 美國)[33]:終點時間、距離和當日賽道變異的函數。一級賽馬匹通常聚集在 100+ 附近。
- **Timeform Ratings**(英國 / 歐洲):更廣義,考慮表現情境(節奏、狀態、場地);粗略換算約為 Timeform − 12 至 14 ≈ Beyer。
- **Racing Post Ratings**:類似的英式度量。
- **級別評分**:某一賽事類型的預測獲勝 Beyer 值。
- **節奏數值**:E1(起跑至第一計時點)、E2(起跑至第二計時點)、LP(晚段節奏)[34]。

這些共同構成經典的讓分原始量,任何 ML 模型要麼直接吸收要麼學到其等價物。

### 2.9 市場衍生信號

開賽價、交易所(Betfair)價格、晚期漂移 / 陡動模式、BSP(Betfair Starting Price)。文獻 [3, 12] 一致發現這些是最強的單一預測因子,在臨近開賽流動性達到頂峯時尤甚。*擊敗收盤線*(CLV)[42] 是優勢的金標準實時檢驗 —— 持續以高於 BSP 的價格成交意味着無論勝負結果如何均存在優勢。

---

## 3. 框架與運營者

| 運營者 | 所在地 | 體育 / 市場 | 估計規模 | 關鍵方法 | 狀態 | 參考 |
|--------|--------|-------------|----------|----------|------|------|
| Bill Benter / HK 聯合投注集團 | 香港 (HKJC) | HK 賽馬 | 累計利潤約 $1B | 兩階段 logit、Kelly、異型 | 活躍數十年 | [21] |
| Alan Woods | 香港 / 遠程 | HK 賽馬 | 離世時 AU$670M (2008) | 早期與 Benter 一起的量化模型 | 2008 年去世 | [23] |
| Zeljko Ranogajec("小丑")/ Punters Club | 澳大利亞 / 英國 / 馬恩島 | HK、AU、US 賽馬、彩票 | 約 A$1B 營業額;約佔 TabCorp 收入 6–8%;約佔 Betfair AU 1/3 | 流動性瞄準、返水、規模化 | 活躍 | [19] |
| Tony Bloom / Starlizard | 倫敦 (Camden) | 主要足球;板球;部分賽馬 | 年度贏額約 £600M(高等法院文件) | 統計模型、聯合投注"明星" | 活躍 | [20] |
| Patrick Veitch / Exponential Partnership | 英國 | 英國賽馬 | 贏額 £10M+ | 每場約 80 個因素、每週約 80 小時、下注大手筆 | 已減少;轉向血統買賣 | [24] |
| Colossus Bets (Marantelli, Ranogajec) | 英國 | 彩池式投注(足球、賽馬) | 80M+ 注、100+ 國家 | 彩池式提現、聯合投注功能 | 活躍 | [22] |

### 3.1 商業 / 消費級平台

- **Equibase / Daily Racing Form (DRF)** [33]:美國行業標準數據,以及 DRF Formulator 軟件(往績、定製能力評分、回測)。
- **Brisnet**(Bloodstock Research Information Services)[30]:節奏、速度、級別評分、投資回報率分佈;通過 TwinSpires Edge [31] 提供。
- **TrackMaster** [32]:TRIPS 報告 —— 節奏、偏差、練馬師 / 騎師統計、評論。
- **Timeform**:英國 / 歐洲經典評分,由 Flutter 擁有。
- **Racing Post**:評分、戰績、新聞、RPR。
- **EquinEdge** [27]:面向零售的 AI 讓分,宣傳首選 32.9% 勝率。
- **RaceHP.ai** [28]:神經網絡讓分,宣傳在 15.8M 測試樣本上 AUC-ROC 94.4%(URIN v4.7),144 個特徵。
- **FormGenie** [29]:宣傳首選 40% 勝率。
- **PediCapper.ai**:以血統為重心的 AI 工具。

### 3.2 值得了解的開源倉庫與 notebook

- `chris-alex-p/german-horse-racing` [61] —— 在德國數據上的 Benter 風格方法,註釋詳盡。
- `codeworks-data/mvp-horse-racing-prediction` [62] —— 香港數據集、MVP 流水線。
- `ethan-eplee/HorseRacePrediction` [63] —— 在 HK Kaggle 數據上的分類 + 迴歸 + 回測。
- `pbovard63/Predicting_Hong_Kong_Horse_Racing_Finishes` [64]。
- `hieutrungle/horse_racing_prediction` [65] —— 日本前三名分類器。
- Kaggle notebook *Horse Racing — Welcome to the Machine*,作者 jpmiller [66]。

---

## 4. 注碼與資金管理

### 4.1 Kelly 準則

最大化期望對數本金增長率 [16]。對於一筆在十進制賠率 `d`、模型概率 `p` 下的下注,Kelly 比例為 `f = (p·d − 1) / (d − 1)`。性質:最大化長期增長;過度下注(>完整 Kelly)最終摧毀本金;不足下注會按比例同時降低增長和方差。如 Benter [2] 所言並由 Thorp / MacLean / Ziemba [3, 18] 證實:在實際估計誤差下,完全 Kelly *劣於*分數 Kelly,因為將下注規模減半會令方差減少超過一半,而對增長的削減則較小。標準做法是半 Kelly 或四分之一 Kelly [17]。

### 4.2 彩池規模上限

Benter 最被低估的結果 [2]:即便資本無限,同注分彩彩池規模也會限制最優下注。示例:一匹 p=0.06、20:1、彩池 $100,000 的馬,最大期望利潤下的下注僅約 $416。超過這一點,運營者自身的下注會推動價格,足以抹去優勢。任何嚴肅的同注分彩運營者都必須模擬自我衝擊。

### 4.3 資金規則

- 大多數職業玩家每注承擔本金的 1–2%;在零售負責任博彩指南中也指出 5% 是上限 [16]。
- 日 / 周止損:典型為日 10% 本金,觸及即離場 [53]。
- 24 小時規則:任何異常大額下注都先過夜再下達。
- 不追單:嚴禁通過加註挽回虧損。
- 週期性提取固定比例;賬户資金與生活資金分離。

### 4.4 投注結構

- **獨贏**:目標最乾淨、最易建模、彩池流動性最高。
- **位置 / 入圍**:優勢較低但方差較低;用於擴大樣本量。
- **Dutching** [58]:分配注碼使所選 2–4 匹馬中任一勝出收益相同;降低方差但本身不創造優勢。
- **對沖** [58]:在價格變動後通過次級下注鎖定盈虧。
- **套利** [59]:在莊家與交易所之間鎖定 1–10% 利潤;受莊家賬户壽命限制。
- **異型(連贏、三連、四連、Pick 4/5/6)** [60]:通過 Harville / Henery 從勝率估計 [4, 5];"越異型,潛在優勢越高"(Benter [2]),但票券複雜度增加方差和彩池摩擦。
- **Pick 6 累積獵取**:經典聯合投注集團玩法;轉滾日的正期望值能吸引大彩池。

---

## 5. 安全機制、硬性止損與人在迴路

本節區分運營數十年仍存活與運營一季就崩盤的差別。

### 5.1 下注前過濾

- **最高賠率(價格上限)**:拒絕下注高於某臨界值(如 20.0 或 25.0)的馬。高賠率下注集中方差,而正是校準誤差代價最高的所在。與本運營者現有的 `bet_max_odds` 過濾一致。受熱門—冷門文獻支持 [12]。
- **最低優勢**:要求模型優勢高於某閾值(如 5–10%)才下注;更小的"優勢"通常是校準噪聲 [44]。
- **最低概率**:拒絕下注真實概率低於例如 2% 的馬 —— 過於稀薄、不可靠。
- **NaN / 缺失賠率防護**:顯式拒絕賠率為非數值的參賽馬。`float('nan')` 對 `<=` 與 `>=` 比較都返回 `False`;沒有 `math.isnan` 檢查會靜默滑過。
- **彩池深度 / 流動性下限**:拒絕撮合量低於例如 £10k (Betfair) 或彩池低於某彩池閾值的市場。
- **滑點上限**:拒絕接受偏離模型公允價格超過 N 個 tick 的成交價。

### 5.2 倉位規模安全

- **絕對下注上限**:不論 Kelly 如何,每注的硬性天花板。
- **% 本金上限**:超過例如 5% 的 Kelly 推薦值會被截斷 [17]。
- **% 彩池上限**:不超過相關彩池的例如 0.5%(Benter 的自我衝擊上限 [2])。

### 5.3 回撤 / 熔斷機制

- **日損失上限**:一天本金回撤 −10% 後離場;常見職業規則 [53]。
- **周 / 單場比賽上限**:相同思路,粒度更粗。
- **投資回報率回撤暫停**:若滾動投資回報率跌破歷史水平的某閾值,暫停所有下注。
- **模型與市場背離告警**:若模型與市場概率的平均絕對差超過歷史範圍,標記複核(可能是制度變化、數據源 bug 或模型損壞)。
- **緊急停止開關** [53]:單條命令(或自動異常觸發)即可立即停止所有新下注。
- **死人開關** [54]:若操作員不周期性刷新令牌,則交易停止;防止系統靜默故障。

### 5.4 模型健康監控

**校準指標**:

- **Brier 分數** [44]:概率與結果之間的均方誤差;越低越好。
- **Log loss** [45]:對自信錯誤重罰;標準訓練損失。
- **Expected Calibration Error (ECE)** [46]:按桶加權的預測概率與經驗頻率之間的差距;< 0.05 被視為頂尖。
- **可靠性圖**:ECE 的可視化。

**校準修復**:

- **Platt scaling** [47]:在模型分數上的參數化邏輯擬合;校準樣本 <1000 時首選。
- **等距迴歸** [46]:非參數單調擬合;樣本 >1000 時首選;在未校準的 XGBoost 上報告了 90%+ 的 ECE 改進。

**漂移檢測** [48]:

- 監控輸入特徵分佈(PSI、KS 檢驗)和輸出預測分佈(卡方、JS 散度)的偏移;觸發調查 / 再訓練。
- **概念漂移**:監控有標籤數據上的準確率 / log-loss / Brier 隨時間變化;超出控制限即觸發。

### 5.5 回測紀律

- **僅使用滾動前向 / 時間序列交叉驗證** [51];絕不使用隨機 k 折。
- **嚴格的時點特徵快照**(無前瞻)[51]。
- **納入退出參賽馬、被取消比賽以及僅倖存記錄問題**(倖存者偏差)[52]。
- **現實摩擦**:抽水、佣金、滑點、自我衝擊、賬户壽命。
- **樣本外驗證期足以涵蓋制度變化**(賽道翻新、規則修改、騎師更替)。
- **多重檢驗校正**(若測試了多種策略變體)。

### 5.6 人在迴路與審計

- **超過閾值注碼的逐注複核門**:需人工簽字 [55]。
- **異常即暫停**:模型輸出超出歷史範圍 → 暫停並通知。
- **只追加下注日誌**:每個模型輸出、每個輸入特徵快照、每個已下注與已拒絕下注都以不可變方式記錄,供監管與事後覆盤使用 [55]。
- **反事實分析**:例行重新運行無上限、無過濾版本並與實際投資回報率對比(與本運營者現有的下注審計端點對應)。
- **代碼評審與模型上線門**:新模型先以影子交易方式運行再投入真實資金。
- **權限分離**:只讀分析、寫入下注、管理 / 急停分別使用不同憑據。

### 5.7 負責任博彩疊加層

即便是私人運營者也可從 UKGC 式控制中獲益 [49, 50]:

- **入金上限**(UKGC 從 2026 年 6 月起強制要求所有持牌運營商 [50])。
- **單局損失上限 / 贏額上限**。
- **現實檢查** —— 顯示已用時間與淨盈虧的定時彈窗。
- **GamStop 式自我排除**(博彩公司層面)[49]。
- **冷靜期**:提高上限需 24–72 小時延遲。

---

## 6. 監管與司法管轄區註釋

| 司法管轄區 | 主要市場 | 抽水 / 佣金 | 備註 | 參考 |
|------------|----------|-------------|------|------|
| 香港 (HKJC) | 同注分彩壟斷 | 平均約 19% | 對 HK$10k+ 虧損票券提供 10% 損失返點(連贏 / QP 12%);彩池深;嚴肅量化賽馬的標杆 | [37, 38] |
| 英國 | 固定賠率莊家 + Betfair 交易所 | Betfair 佣金 2–5% | 受 UKGC 監管;2026 年起強制入金上限;最低投注規則各異;SP / BSP 為標準 | [49, 50] |
| 澳大利亞 | 混合:州 TAB、企業莊家、Betfair | 彩池 14–16%,固定賠率莊家加成 | 最低投注法保護投注者;常見三選優 / 四選優 / 五選優結算;返水文化盛行 | [39] |
| 美國 | 僅同注分彩 (NYRA, CDI 等) | 按彩池 15–22% | 高抽水抑制優勢;返水店為大額玩家返還 5–10% | [33] |
| 日本 (JRA + NAR) | 同注分彩 | 名義約 25% 但分層 | JRA 日營業額約 ¥9.97B;9 種投注類型含 WIN5;週末賽事;外國訪問有限 | [40] |
| 法國 (PMU) | 國家壟斷同注分彩 | 約 25% 含再分配 | 年度營業額 €9B;賽馬業回收 €835M;13,000+ 營業網點 | [41] |

返水與佣金並非裝飾 —— 它們常常將原始投資回報率 100–101% 的模型轉化為淨 105–108% 的運營。Ranogajec 的運營 [19] 據稱 85% 由返水驅動,純模型優勢僅佔 15%。

---

## 7. 參考文獻

### 基礎學術論文與著作

[1] Bolton, R. N. & Chapman, R. G. (1986). *Searching for Positive Returns at the Track: A Multinomial Logit Model for Handicapping Horse Races.*(在賽場上尋找正回報:賽馬讓分的多項式 logit 模型)Management Science 32(8), 1040–1060. https://pubsonline.informs.org/doi/abs/10.1287/mnsc.32.8.1040 (mirror: https://gwern.net/doc/statistics/decision/1986-bolton.pdf)

[2] Benter, W. (1994). *Computer Based Horse Race Handicapping and Wagering Systems: A Report.*(基於計算機的賽馬讓分與投注系統報告)In Hausch, Lo & Ziemba (eds.) *Efficiency of Racetrack Betting Markets*. https://gwern.net/doc/statistics/decision/1994-benter.pdf (annotated: https://actamachina.com/posts/annotated-benter-paper)

[3] Hausch, D. B., Lo, V. S. Y. & Ziemba, W. T. (eds.). *Efficiency of Racetrack Betting Markets*(賽馬投注市場的效率)(2008 ed., World Scientific). https://www.worldscientific.com/worldscibooks/10.1142/6910

[4] Harville, D. A. (1973). *Assigning probabilities to the outcomes of multi-entry competitions.*(為多人比賽結果分配概率)JASA 68(342), 312–316.

[5] Henery, R. J. (1981). *Permutation probabilities as models for horse races.*(用於賽馬的排列概率模型)Journal of the Royal Statistical Society B 43(1), 86–91.

[6] Plackett, R. L. (1975). *The analysis of permutations.*(排列分析)Applied Statistics 24(2), 193–202.

[7] PlackettLuce R package documentation. https://cran.r-project.org/web/packages/PlackettLuce/PlackettLuce.pdf

[8] Aldous, D. *Probability models on horse-race outcomes.*(賽馬結果的概率模型)UC Berkeley. https://www.stat.berkeley.edu/~aldous/157/Papers/ali.pdf

### 現代機器學習文獻

[9] *Optimizing Horse Racing Predictions through Ensemble Learning and Automated Betting Systems*(通過集成學習與自動化投注系統優化賽馬預測)(2024). https://www.researchgate.net/publication/385301910

[10] *Horse race rank prediction using learning-to-rank approaches.*(使用學習排序方法的賽馬排名預測)Korean Journal of Applied Statistics (2024). https://koreascience.kr/article/JAKO202414143309228.page

[11] *What AI can do for horse-racing?*(AI 能為賽馬做什麼?)arXiv 2207.04981 (2022). https://arxiv.org/abs/2207.04981

[12] Snowberg, E. & Wolfers, J. *Explaining the favorite-longshot bias: is it risk-love or misperceptions?*(解釋熱門—冷門偏差:是風險偏好還是誤判?)https://eriksnowberg.com/papers/Snowberg-Wolfers%20Risk%20Love%20or%20Decision%20Weights3.pdf

[13] *A Hierarchical Bayesian Analysis of Horse Racing.*(賽馬的分層貝葉斯分析)https://www.researchgate.net/publication/343902664

[14] *Efficient Market Dynamics in UK Betfair time series.*(英國 Betfair 時間序列中的有效市場動態)arXiv 2402.02623. https://arxiv.org/pdf/2402.02623

[15] *Emergence of scale invariance in racetrack betting.*(賽場投注中尺度不變性的湧現)arXiv 0911.3249. https://arxiv.org/pdf/0911.3249

### Kelly 準則與資金管理

[16] Horise — *Kelly Criterion for horse racing.*(賽馬的 Kelly 準則)https://www.horise.com/guides/kelly-criterion/

[17] EquinEdge glossary — *What is Kelly Criterion.*(什麼是 Kelly 準則)https://equinedge.com/glossary/racing-data-and-statistics/what-is-kelly-criterion

[18] Sportsbook Review — *Kelly Calculator.* https://www.sportsbookreview.com/betting-calculators/kelly-calculator/

### 運營者簡介

[19] Wikipedia — *Zeljko Ranogajec.* https://en.wikipedia.org/wiki/Zeljko_Ranogajec

[20] Wikipedia — *Tony Bloom.* https://en.wikipedia.org/wiki/Tony_Bloom (and Racing Post coverage: https://www.racingpost.com/news/britain/high-court-case-alleges-tony-blooms-betting-empire-makes-600m-a-year)

[21] Guinness World Records — *A billion dollars off the ponies.*(從賽馬中贏得十億美元)https://www.guinnessworldrecords.com/news/2025/8/a-billion-dollars-off-the-ponies-how-a-statistician-became-the-most-profitable-gambler

[22] Wikipedia — *Colossus Bets.* https://en.wikipedia.org/wiki/Colossus_Bets

[23] Wikipedia — *Alan Woods (gambler).* https://en.wikipedia.org/wiki/Alan_Woods_(gambler) (and SCMP: https://www.scmp.com/article/624848/super-punter-woods-quietly-masterminded-revolution)

[24] Racing Post — *Patrick Veitch interview.*(Patrick Veitch 訪談)https://www.racingpost.com/news/features/the-big-read/

### 商業平台

[27] EquinEdge. https://equinedge.com/

[28] RaceHP.ai. https://racehp.ai/horse-racing/

[29] FormGenie. https://www.formgenie.com/

[30] Brisnet. https://www.brisnet.com/product/

[31] TwinSpires Edge handicapping tools. https://www.twinspires.com/handicapping-tools/

[32] TrackMaster TRIPS reports. https://www.trackmaster.com/products/thoroughbred/trips_reports

[33] DRF — *Beyer Speed Figures.*(Beyer 速度指數)https://promos.drf.com/beyer23

[34] Equibase — *Speed / Pace / Class explainer (PDF).*(速度 / 節奏 / 級別解析)https://www.equibase.com/products/speedpace.pdf

### 市場與運營

[37] HKJC — *Rebate Program.*(返點計劃)https://special.hkjc.com/racing/info/en/betting/guide_rebate.asp

[38] SCMP — *Jockey Club boosts rebate to help combat illegal bookmakers.*(馬會提高返點以打擊非法莊家)https://www.scmp.com/sport/racing/article/3088586

[39] Winning Edge Investments — *Australian Minimum Bet Laws 2025.*(2025 澳大利亞最低投注法)https://www.winningedgeinvestments.com/posts/current-minimum-bet-laws-by-australian-state (and Before You Bet: https://www.beforeyoubet.com.au/horse-racing-fixed-odds-or-tote)

[40] Japan Racing Association — *How to bet.*(投注指南)https://japanracing.jp/en/racing/go_racing/jra_howtobet.html

[41] PMU — *About.* https://horseraces.pmu.fr/about-pmu

[42] Punter2Pro — *Beating the closing line (CLV).*(擊敗收盤線 (CLV))https://punter2pro.com/punters-guide-beating-the-sp/

[43] BetAngel — *Best time to trade horse racing.*(交易賽馬的最佳時段)https://www.betangel.com/best-time-to-trade-on-horse-racing/ (and Traderline: https://traderline.com/education/betfair-horse-racing-trading-strategies; BetAngel favourite-longshot: https://www.betangel.com/favourite-longshot-bias/)

### 校準、監控與交易安全

[44] sports-ai.dev — *Brier score and calibration for betting.*(投注的 Brier 分數與校準)https://www.sports-ai.dev/blog/ai-model-calibration-brier-score

[45] DRatings — *Log loss vs Brier score.* https://www.dratings.com/log-loss-vs-brier-score/

[46] scikit-learn — *Probability calibration.*(概率校準)https://scikit-learn.org/stable/modules/calibration.html

[47] Train in Data — *Complete Guide to Platt Scaling.*(Platt scaling 完整指南)https://www.blog.trainindata.com/complete-guide-to-platt-scaling/

[48] Evidently AI — *Concept drift / Data drift.*(概念漂移 / 數據漂移)https://www.evidentlyai.com/ml-in-production/concept-drift (and https://www.evidentlyai.com/ml-in-production/data-drift; Arize: https://arize.com/model-drift/)

### 監管、負責任博彩、審計

[49] UKGC — *Self-exclusion.*(自我排除)https://www.gamblingcommission.gov.uk/public-and-players/page/self-exclusion

[50] iGB — *UKGC deposit limit rules.*(UKGC 入金上限規則)https://igamingbusiness.com/sustainable-gambling/responsible-gambling/gambling-commission-clarifies-deposit-limit-rules/

### 回測衞生

[51] *Hidden Leaks in Time Series Forecasting.*(時間序列預測中的隱性泄露)arXiv 2512.06932. https://arxiv.org/html/2512.06932v1

[52] Lux Algo — *Survivorship bias in backtesting.*(回測中的倖存者偏差)https://www.luxalgo.com/blog/survivorship-bias-in-backtesting-explained/

### 交易系統熔斷機制

[53] NYIF — *Trading system kill switch.*(交易系統緊急停止開關)https://www.nyif.com/articles/trading-system-kill-switch-panacea-or-pandoras-box

[54] Euromoney — *Circuit breakers in FX.*(外匯中的熔斷機制)https://www.euromoney.com/article/27bjsstsqxhkmh0wsdju4/fintech/circuit-breakers-does-fx-need-a-kill-switch/

[55] Gaming Associates — *How regulators approve sports betting systems.*(監管者如何審批體育投注系統)https://gamingassociates.com/blog/regulatory-compliant-sports-betting-systems/ (and Riskonnect: https://riskonnect.com/compliance/automating-key-compliance-challenges-in-the-gambling-gaming-industry/)

### 注碼模式

[58] Outplayed — *What is Dutching (2025 guide).*(什麼是 Dutching,2025 指南)https://outplayed.com/blog/what-is-dutching (and Profitable Horse Racing Systems hedging guide: http://www.profitablehorseracingsystems.co.uk/hedging-systems-dutching)

[59] The Arb Academy — *Horse racing arbitrage.*(賽馬套利)https://thearbacademy.com/arbitrage-horse-racing/

[60] GamblingCalc — *Exotic bets explained.*(異型投注解析)https://gamblingcalc.com/gambling-guides/horse-racing-exotic-bets-explained/ (and SBO tote systems: https://www.sbo.net/strategy/tote-systems/)

### 開源倉庫與 notebook

[61] chris-alex-p / german-horse-racing — `analysis_benter_methods`. https://github.com/chris-alex-p/german-horse-racing/blob/main/notebooks/analysis_benter_methods.md

[62] codeworks-data / mvp-horse-racing-prediction. https://github.com/codeworks-data/mvp-horse-racing-prediction

[63] ethan-eplee / HorseRacePrediction. https://github.com/ethan-eplee/HorseRacePrediction

[64] pbovard63 / Predicting_Hong_Kong_Horse_Racing_Finishes. https://github.com/pbovard63/Predicting_Hong_Kong_Horse_Racing_Finishes

[65] hieutrungle / horse_racing_prediction. https://github.com/hieutrungle/horse_racing_prediction

[66] Kaggle — *Horse Racing: Welcome to the Machine* by jpmiller. https://www.kaggle.com/code/jpmiller/horse-racing-welcome-to-the-machine

[67] GitHub topic — *horse-racing.* https://github.com/topics/horse-racing

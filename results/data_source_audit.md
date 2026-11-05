# Data Source Audit — QF5313 Question 3, Phase 1 (AkShare `macro_china_*` Exploration)

- **Report date**: 2026-11-05 (all live API calls were made on this date)
- **Environment**: Python 3.13 managed with `uv`, project `.venv`; akshare **1.20.25** installed at `.venv/Lib/site-packages/akshare`
- **Project files created or modified in this phase**: none (test scripts lived only in the OS temp directory and were deleted after use; no data downloaded or persisted)
- **Status labels used in this report**
  - **[VERIFIED]** — observed directly in this session (package source inspection, live API call, or value cross-check)
  - **[ESTIMATED]** — domain knowledge / typical schedule; plausible but not verified here
  - **[UNVERIFIED]** — unknown; requires action before use

---

## 1. Session Context and Scope

Phase 1 goal: explore and verify AkShare data sources for the China macro panel required by
`assignment.md` §5 (Q3: regimes, nowcasting, indicator clustering), before any dataset construction.
Requirements were: ≥ 25 candidate indicators, ≥ 10 years of monthly observations, per-indicator
publication lags, strict leakage prevention (`F_t = { x_{i,s} : s + L_i ≤ t }`).

Deliverables of this phase (from the user's task spec): interface inventory, API verification
results, candidate indicator table, publication-lag assessment, and risk register — all below.

---

## 2. Environment and Session Facts

**[VERIFIED]**

- `uv run python` in the project root works; akshare 1.20.25 imports successfully.
- `pyproject.toml` and `uv.lock` exist in the repo (the note in `AGENTS.md` saying "currently neither
  file exists" is outdated — the environment has evolved; dependencies are now declared and locked).
- Console encoding required `[Console]::OutputEncoding = UTF8; $env:PYTHONUTF8='1'` on Windows,
  otherwise Chinese docstrings/column names rendered as mojibake. This is a terminal display issue only.
- Search tools (ripgrep) failed on the non-ASCII project path (路径含中文); PowerShell
  `Select-String` was used as fallback. No impact on findings.
- Live internet access confirmed for Eastmoney (`datacenter-web.eastmoney.com`), Jin10
  (`cdn.jin10.com`), Sina (`finance.sina.com.cn/mac`), mofcom (`data.mofcom.gov.cn`).
  **No** internet access path found for `data.stats.gov.cn` (NBS) or `yun.ccxe.com.cn` (Caixin) — see §4.
- Web search attempts failed in this session ("Web search cancelled" — tool-side failure, twice),
  and a guessed NBS release-calendar URL returned HTTP 404. The official NBS release calendar was
  therefore **not verified online in this session** — see §5.4.

---

## 3. Interface Inventory (A)

**[VERIFIED]** The installed akshare 1.20.25 exposes **85** interfaces with the `macro_china_*`
prefix (enumerated via `dir(ak)`), **not the "170"** quoted in `assignment.md` (that count is an
overstatement for this version). All 85 exist; the economically relevant ones are grouped below.
Interface names and their one-line Chinese descriptions were read from the installed docstrings.

### 3.1 Economic growth & industrial production
- `macro_china_gdp` (Eastmoney, quarterly) — **tested**
- `macro_china_gdp_yearly` (jin10 GDP YoY report) — **tested**
- `macro_china_industrial_production_yoy` (jin10 规模以上工业增加值年率) — **tested**
- `macro_china_gyzjz` (Eastmoney 工业增加值增长) — **tested**
- `macro_china_gdzctz` (Eastmoney 城镇固定资产投资) — **tested**
- `macro_china_real_estate` (国房景气指数) — **tested**
- `macro_china_enterprise_boom_index` (企业景气及企业家信心指数, quarterly) — **tested**
- `macro_china_construction_index`, `macro_china_construction_price_index` (建材, untested)
- `macro_china_lpi_index` (物流景气指数) — **tested**

### 3.2 Inflation & producer prices
- `macro_china_cpi` (Eastmoney) — **tested**; `macro_china_cpi_monthly`, `macro_china_cpi_yearly` (jin10) — tested
- `macro_china_ppi` (Eastmoney) — **tested**; `macro_china_ppi_yearly` (jin10) — source-reviewed, not called
- `macro_china_qyspjg` (PBOC 企业商品价格指数 CGPI,季度转月度) — **tested**
- `macro_china_retail_price_index` (商品零售价格指数 RPI) — **tested**
- `macro_china_commodity_price_index`, `macro_china_energy_index` (南华商品/能源指数 daily) — **tested**
- `macro_china_agricultural_product`, `macro_china_vegetable_basket`, `macro_china_agricultural_index` (untested)

### 3.3 PMI surveys
- `macro_china_pmi` (Eastmoney: 制造业 + 非制造业 in one table) — **tested**
- `macro_china_pmi_yearly` (jin10 中国官方制造业PMI) — **tested**
- `macro_china_non_man_pmi` (jin10 中国官方非制造业PMI) — **tested**
- `macro_china_cx_pmi_yearly` (jin10 财新制造业PMI), `macro_china_cx_services_pmi_yearly` (jin10 财新服务业PMI) — **tested**
- `index_pmi_man_cx`, `index_pmi_ser_cx`, `index_pmi_com_cx` (财新智库 direct, `yun.ccxe.com.cn`) — **tested, FAILED**

### 3.4 Money supply & credit
- `macro_china_money_supply` (Eastmoney M0/M1/M2) — **tested**
- `macro_china_supply_of_money` (Sina variant with deposits breakdown) — **tested**
- `macro_china_shrzgm` (mofcom 社会融资规模增量 TSF) — **tested**
- `macro_china_new_financial_credit` (新增信贷) — **tested**
- `macro_china_central_bank_balance` (PBOC 货币当局资产负债表) — **tested**
- `macro_china_whxd` (外汇贷款), `macro_china_wbck` (本外币存款) — source-reviewed, not called
- `macro_china_m2_yearly`, `macro_china_fx_reserves_yearly` (jin10) — source-reviewed, not called
- `macro_china_insurance_income` (原保险保费收入) — **tested**

### 3.5 Interest rates & liquidity
- `macro_china_lpr` (LPR + pre-2019 benchmark rates, daily) — **tested**
- `macro_china_shibor_all` (Shibor O/N…1Y, daily) — **tested**
- `macro_china_swap_rate` (FR007 利率互换) — **tested, FAILED**
- `macro_china_reserve_requirement_ratio` (存款准备金率, policy events) — **tested**
- `macro_china_market_margin_sh`, `macro_china_market_margin_sz` (两融余额, untested)
- `macro_china_rmb` (人民币汇率中间价 daily, untested)

### 3.6 Consumption & confidence
- `macro_china_consumer_goods_retail` (社会消费品零售总额) — **tested**
- `macro_china_xfzxx` (消费者信心指数) — **tested (has a code bug, see §6.5)**

### 3.7 Foreign trade
- `macro_china_hgjck` (海关进出口增减情况一览表, monthly levels + YoY) — **tested**
- `macro_china_exports_yoy`, `macro_china_imports_yoy`, `macro_china_trade_balance` (jin10) — **tested**
- `macro_china_freight_index` (BDI/BCI/BSI/BDTI/BCTI/HRCI, daily) — **tested**

### 3.8 Physical activity / infrastructure
- `macro_china_society_electricity` (全社会用电分类情况表 — electricity **consumption**) — **tested**
- `macro_china_society_traffic_volume` (全社会客货运输量) — **tested**
- `macro_china_postal_telecommunicational` (邮电业务基本情况) — **tested (mostly NaN in modern periods)**
- `macro_china_daily_energy` (六大电库存 daily, untested)

### 3.9 Government / external accounts / other
- `macro_china_czsr` (财政收入) — **tested**; `macro_china_national_tax_receipts` (not called)
- `macro_china_fx_gold` (央行黄金和外汇储备) — **tested**
- `macro_china_fdi` (外商直接投资, not called); `macro_china_new_house_price` (city house-price index, source-reviewed)
- `macro_china_stock_market_cap` (全国股票交易统计表) — **tested**
- `macro_china_nbs_nation`, `macro_china_nbs_region` (国家统计局通用接口) — **tested, FAILED (environment)**

### 3.10 Excluded from the panel (not Q3-relevant)
9 × `macro_china_hk_*` (Hong Kong), `macro_china_au_report`, `macro_china_fx_gold` variants,
`macro_china_bond_public`, `macro_china_bank_financing`, `macro_china_mobile_number`,
`macro_china_international_tourism_fx`, `macro_china_passenger_load_factor`,
`macro_china_bdti_index`, `macro_china_bsi_index`, `macro_china_yw_electronic_index`,
`macro_china_insurance`, `macro_china_hk_market_info`, `macro_china_mobile_number`,
`macro_china_postal_telecommunicational` (below-relevance sections). Existence documented from
docstrings; none excluded silently — all named here for the record.

---

## 4. API Verification Results (B)

Live test harness ran on 2026-11-05 in the project `.venv`; per interface it recorded shape,
columns, head/tail sample rows and date span. Legend: ✅ OK · ⚠️ OK with caveats · ❌ FAILED.
Ranges are as **observed in the returned data** (some differ from docstring claims — noted).

| # | Interface (source site) | Result | Shape | Observed range | Key returned columns / sample | Notes |
|---|---|---|---|---|---|---|
| 1 | `macro_china_gdp` (Eastmoney) | ✅ | (83, 9) | 2006Q1 – 2026Q3 | 季度； 国内生产总值-绝对值(亿元); 总/第一/第二/第三产业 同比 | Quarterly only; starts 2006Q1 (not earlier) |
| 2 | `macro_china_cpi` (Eastmoney) | ⚠️ | (225, 13) | 2008-01 – 2026-09 | 月份； 全国-当月/同比增长/环比/累计； 城市/农村 分组 | “当月”是隐含指数 (100+x): 2026-09→100.9, 2008-01→107.0781 (老行更高精度) |
| 3 | `macro_china_ppi` (Eastmoney) | ✅ | (249, 4) | 2006-01 – 2026-09 | 月份； 当月； 当月同比增长； 累计 | Same “当月 = 100+x” index convention; 2026-09 → 105.0 |
| 4 | `macro_china_pmi` (Eastmoney) | ✅ | (226, 5) | 2008-01 – **2026-10** | 制造业-指数 (49.3, 2026-10); 非制造业-指数 (49.7) | “同比” columns for PMI are meaningless; use 指数 only. Both mfg & non-mfg from a single call |
| 5 | `macro_china_pmi_yearly` (jin10) | ⚠️ stale | (250, 5) | 2005-02-01 – **2025-08-31** | 商品/日期/今值/预测值/前值 | 日期 = recent-vintage release date; frozen after Aug 2025 |
| 6 | `macro_china_cx_pmi_yearly` (jin10) | ⚠️ stale | (219, 5) | 2012-01-20 – **2025-09-01** | idem | Last: 2025-09-01 → 50.5 (Aug value) |
| 7 | `macro_china_cx_services_pmi_yearly` (jin10) | ⚠️ stale | (166, 5) | 2012-04-05 – **2025-09-03** | idem | Last: 2025-09-03 → 53.0 (Aug value) |
| 8 | `macro_china_non_man_pmi` (jin10) | ⚠️ stale | (226, 5) | 2007-02-01 – **2025-08-31** | idem | Last: 2025-08-31 → 50.3 (Aug value) |
| 9 | `macro_china_money_supply` (Eastmoney) | ✅ | (225, 10) | 2008-01 – 2026-09 | M2/M1/M0 数量(亿元) + 同比 + 环比 | Stock variable — levels trend strongly |
| 10 | `macro_china_supply_of_money` (Sina) | ✅ | (585, 17) | 1978.1 – 2026.9 | M2/M1/M0 + 活期/定期/储蓄存款 breakdown | Long history; many pre-1990s NaN; duplicate coverage of #9 |
| 11 | `macro_china_consumer_goods_retail` (Eastmoney) | ⚠️ | (210, 6) | 2008-01 – 2026-09 | 当月(亿元)/同比/环比/累计/累计同比 | **January values absent** (NBS publishes combined Jan–Feb) |
| 12 | `macro_china_gyzjz` (Eastmoney) | ⚠️ | (206, 4) | 2008-02 – 2026-09 | 月份； 同比； 累计增长； **发布时间** | Only interface with a per-record “发布时间” column — semantics ambiguous (§5.3) |
| 13 | `macro_china_industrial_production_yoy` (jin10) | ⚠️ stale | (413, 5) | 1990-03-01 – **2025-08-15** | jin10 standard cols | Last: 2025-08-15 → 5.7 (Jul value) |
| 14 | `macro_china_gdzctz` (FAI, Eastmoney) | ⚠️ | (162, 5) | 2012-02 – 2026-09 | 当月/同比/环比/自年初累计 | 2026-09 同比 −9.69 — large value, inspect; “当月” derived from cumulative |
| 15 | `macro_china_hgjck` (customs, Eastmoney) | ⚠️ | (225, 11) | 2008-01 – 2026-09 | 当月出口/进口额-金额 + 同比 + 环比； 累计 同族 | Column header carries **no unit label** (likely 万美元 — UNVERIFIED); 2026-10 同比 not seen; Sep-2026 同比 +36.3 noteworthy |
| 16 | `macro_china_exports_yoy` (jin10) | ⚠️ stale | (542, 5) | 1982-02-01 – **2025-08-07** | jin10 standard cols | Last: 2025-08-07 → 7.2 (Jul value) |
| 17 | `macro_china_imports_yoy` (jin10) | ⚠️ stale | (381, 5) | 1996-02-01 – **2025-08-07** | idem | Jul value → 4.1 待显示 as 前值 (search result text) |
| 18 | `macro_china_trade_balance` (jin10) | ⚠️ stale | (565, 5) | 1981-02-01 – **2025-08-07** | idem | Unit 亿美元 (e.g. 前值 982.4) |
| 19 | `macro_china_xfzxx` (consumer confidence, Eastmoney) | ⚠️ | (237, 10) | 2007-01 – 2026-09 | 消费者信心/满意/预期 指数 + 同比/环比 | **预期指数 column overwritten with 满意指数 by source-code bug** — only 信心指数-指数值 usable |
| 20 | `macro_china_enterprise_boom_index` (Eastmoney) | ⚠️ | (87, 7) | 2005Q1 – 2026Q3 | 企业景气指数； 企业家信心指数 | 企业家信心指数 NaN in recent quarters; quarterly frequency |
| 21 | `macro_china_czsr` (fiscal revenue, Eastmoney) | ✅ | (217, 6) | 2008-01 – 2026-09 | 当月/当月同比/环比/累计/累计同比 (亿元) | Sort seen as 月份-sort on post-processed source; fresh |
| 22 | `macro_china_shrzgm` (TSF, mofcom) | ⚠️ stale | (136, 9) | 2015-01 – **2026-04** | 社会融资规模增量 + 分项 | **Missing May–Sep 2026** — source appears stale/abandoned |
| 23 | `macro_china_new_financial_credit` (new loans) | ✅ | (225, 6) | 2008-01 – 2026-09 | 当月新贷款 (2026-09: 12734.0 亿元) | Fresh |
| 24 | `macro_china_lpr` (Eastmoney) | ✅ | (1577, 5) | 1991-04-21 – 2026-10-20 | TRADE_DATE; LPR1Y/LPR5Y; RATE_1/RATE_2 | Daily; LPR proper from 2019-08; RATE_1/RATE_2 = 旧贷款基准利率 (different series!) |
| 25 | `macro_china_shibor_all` (jin10) | ✅ | (2399, 17) | 2015-05-08 – **2026-11-05** | O/N…1Y 定价/涨跌幅 | Fresh to today; docstring claims 2017-03 start, true start 2015-05 |
| 26 | `macro_china_swap_rate` (jin10 FR007) | ❌ | — | — | — | `ValueError: Length mismatch: Expected axis has 0 elements, new values have 17 elements` — upstream returned no data |
| 27 | `macro_china_reserve_requirement_ratio` (Eastmoney) | ✅ | (58, 11) | 2007-01-05 – 2025-05-07 | 公布时间 / 生效时间 / 大型·中小 金融结构调整前后 | Event-level, not a monthly series |
| 28 | `macro_china_urban_unemployment` (NBS) | ❌ | — | — | — | `curl_cffi SSLError: curl (77) error adding trust anchors ... CAfile: .venv\Lib\site-packages\certifi\cacert.pem` — persisted across 2 attempts |
| 29 | `macro_china_society_electricity` (Sina) | ⚠️ | (243, 17) | 2003.12 – 2026.9 | 全社会用电量(万千瓦时) + 分产业 + 居民 | **Jan–Feb combined at year starts**; electricity *consumption*, not generation |
| 30 | `macro_china_society_traffic_volume` (Sina) | ⚠️ | (2979, 16) | 1952.12 – 2026.9 | 货运量/客运量 + 同比； 长表 (统计对象) | Long format — must filter to “合计” rows; column-name typos (“公里货物周转量同比增长” — “公里” seems wrongly prefixed) |
| 31 | `macro_china_postal_telecommunicational` (Sina) | ⚠️ | (340, 41) | 1952.12 – 2026.9 | 邮电业务总量等 40 columns | Many modern rows NaN for legacy columns — low value; excluded |
| 32 | `macro_china_real_estate` (国房景气, Eastmoney) | ⚠️ stale | (326, 8) | 1998-01-01 – **2025-12-01** | 最新值 + 涨跌幅 windows | Missing Jan–Sep 2026 — ~10 months stale |
| 33 | `macro_china_retail_price_index` (Sina RPI) | ✅⚠️ | (4258, 3) | … – **2022-09** | 统计月份/居民消费项目/零售商品价格指数 | Long format; **dead after 2022-09** |
| 34 | `macro_china_qyspjg` (CGPI, Eastmoney) | ✅ | (261, 13) | 2005-01 – 2026-09 | 总指数/农产品/矿产品/煤油电 定基指数 + 同比/环比 | Fresh; check rebasing history |
| 35 | `macro_china_lpi_index` (Eastmoney) | ✅ | (158, 8) | 2013-07-01 – **2026-10-01** | 最新值; 涨跌幅 | Latest record Oct-1 → Sep value released Oct 1 |
| 36 | `macro_china_insurance_income` (Eastmoney) | ⚠️ | (266, 8) | 2004-07 – 2026-09 | 最新值 (e.g. 320042900) | Unit unlabelled and inconsistent (first row 0.0); exclude unless unit clarified |
| 37 | `macro_china_central_bank_balance` (Sina) | ✅ | (357, 28) | 1993.3 – 2026.9 | 资产/负债 breakdown; 储备货币 (base money) | Fresh |
| 38 | `macro_china_fx_gold` (Eastmoney) | ✅ | (225, 7) | 2008-01 – 2026-09 | 黄金储备 + 国家外汇储备 (34002.51 = $3.40T, 亿美元) + 同比/环比 | Gold unit likely 万盎司 (value 3235.17) — computed tonnes did not reconcile exactly; UNVERIFIED |
| 39 | `macro_china_freight_index` (Sina) | ✅ | (5000, 8) | 2006-09-13 – **2026-11-04** | BCI/BHMI/BSI/BDI/HRCI/BCTI/BDTI | Daily; fresh |
| 40 | `macro_china_stock_market_cap` (Eastmoney) | ✅ | (227, 13) | 2008-01 – 2026-11 | SSE+SZSE 股本/市值/成交额/量 | Current month partial NaN |
| 41 | `macro_china_commodity_price_index` (Sina 南华商品) | ✅ | (4651, 8) | 2011-12-05 – **2026-11-04** | 最新值; 涨跌幅 windows | Daily; fresh |
| 42 | `macro_china_energy_index` (Sina 南华能源) | ✅ | (4650, 8) | 2011-12-05 – **2026-11-04** | idem | Daily; fresh |
| 43 | `macro_china_nbs_nation` (NBS generic; probes: 价格指数-居民消费价格指数 / 工业-工业企业利润总额, period=LAST10) | ❌ | — | — | — | Same SSL error as #28; could not test the interface's own logic |
| 44 | `macro_china_nbs_region` (NBS regional) | ❌ | — | — | — | Not called (same site as #43 fails) — NOT TESTED, not assumed broken; assumption of same failure is UNVERIFIED |
| 45 | `index_pmi_man_cx` / `index_pmi_ser_cx` / `index_pmi_com_cx` (财新智库 direct) | ❌ | — | — | — | HTTP GET on `/api/index/pro/cxIndexTrendInfo` returned `200` with `text/html;charset=UTF-8` and body `<title>财新数据平台 - 404</title>` — endpoint retired |

**Failure policy followed**: all failed calls are reported with their verbatim error messages; none
were silently skipped. The user's Phase-1 rule "lightweight calls only" was held — data was fetched
only into memory during interface verification (each call naturally returns a full history since
page sizes are baked into the interface); no datasets were persisted.

---

## 5. Publication Lag Assessment (D)

Information-set requirement from `AGENTS.md`/`assignment.md`:
`F_t = { x_{i,s} : s + L_i ≤ t }`; any use of `x_{i,s}` with `s + L_i > t` is leakage.
`L_i` = number of whole months between the reference month `s` and the month in which the value
first becomes public. Below: (a) what is verified from data, (b) typical schedules, (c) proposed
conservative lags.

### 5.1 Verified historical publication dates **[VERIFIED]**

Secondary-source evidence from the jin10 series' `日期` column (records of release dates),
each cross-checked against known values. Note the caveat in §5.2.

| Series | Verified sample (date → value) | Reference month resolved | Lag inference |
|---|---|---|---|
| Official mfg PMI | `2025-08-31 → 49.4` (Aug-2025 PMI = 49.4, released on the last day of the reference month); also Jun-2025 `06-30 → 49.7`, Jul `07-31 → 49.3` | release month = reference month | L ≈ 0 (available at month-end of ref month) |
| Caixin mfg PMI (final) | `2025-09-01 → 50.5` (Aug-2025) | following month (1st day) | L ≈ 1 |
| Caixin services PMI | `2025-09-03 → 53.0` (Aug-2025); recent 5y release days 3–6 | following month | L ≈ 1 |
| Industrial production (VA) YoY | `2025-07-15 → 6.8` (Jun value); `2025-08-15 → 5.7` (Jul value) | following month (mid) | L ≈ 1 |
| Exports YoY | `2025-08-07 → 7.2` (Jul value) | following month (~7th) | L ≈ 1 |
| GDP YoY (quarterly) | `2025-07-15 → 5.2` (Q2-2025, released 15 Jul 2025); `2025-04-16 → 5.4` (Q1); `2025-01-17 → 5.4` (Q4-2024) | ~15–18 days after quarter end | ~0.5 month latency after quarter end |
| CPI (MoM report) | `2025-08-09 → 0.4` (Jul-2025 CPI MoM); recent release days ≈ 8–12 | following month (~9th) | L ≈ 1 |

Day-of-release histograms computed in-session (release dates only, recent 5-year window):
official mfg PMI: med=0 days from month-end; Caixin mfg: day 1–3; Caixin services: day 3–6;
industrial VA: day ~13–17; exports: day ~7–13; GDP: day 15–18 after quarter end; CPI: day ~9.

### 5.2 Jin10 date-field caveat **[VERIFIED]** — do not over-interpret

The jin10 release-date fields are **bimodal**: many old records carry day-1 dates (e.g. PMI released
`2005-02-01 → 54.7`, industrial VA `1990-03-01`). For recent years the same series switches to
true release dates (month-end for PMI; mid-month for quantities). It appears that older records are
**period-stamped**, not release-stamped, so per-record release dates for older periods are
**not resolvable from akshare alone**. Only the samples in §5.1 (recent years) are treated as
verified release-date facts. Deriving release months must therefore combine data-driven evidence
with domain knowledge (e.g. NBS publishes Jul-2025 industrial VA on 15 Aug 2025).

### 5.3 `发布时间` column in `macro_china_gyzjz` **[VERIFIED — semantics questionable]**

The interface is the only one returning a per-record `发布时间`, e.g. `2008-02-01` for Feb-2008 and
`2026-09-01` for Sep-2026. A release date for a Sep-2026 value cannot be 2026-09-01 (NBS quantities
are published ~2 weeks into the *following* month; jin10 independently records Jul-2025 at
2025-08-15). **Conclusion**: `发布时间` here behaves like a period marker, not a genuine release
date. Recommendation for Phase 2: retain the column in snapshots but do not treat it as a verified
publication date; document the ambiguity.

### 5.4 Typical publication schedules — **[ESTIMATED / domain knowledge, not verified in this session]**

Cannot be verified online here (see §2: web search tooling failed this session; guessed NBS URL 404).
These must be confirmed against the NBS release-calendar page (or similar authoritative sources) before
being recorded in `metadata.json`:

- **NBS joint release block**: CPI & PPI ~9th; industrial VA, retail sales, FAI, unemployment, real-estate activity — mid-late window ~13th–17th of the following month.
- **GDP**: ~15th–18th of the month after quarter end (verified indirectly via jin10 samples in §5.1).
- **PBOC monthly financial statistics** (M0/M1/M2, TSF, new loans, base money): ~10th–13th of the following month **[ESTIMATED]**.
- **GACC customs trade data**: ~7th–13th **[ESTIMATED]** (verified indirectly at 7 Aug for Jul data, §5.1).
- **LPR**: announced on the 20th of the month at 09:15 **[ESTIMATED, convention well known]**.
- **Official PMI (mfg + non-mfg)**: last day of reference month 09:30 — **[VERIFIED via jin10 samples]**.
- **CFLP LPI**: ~1st of following month (supported by Eastmoney record dates but not independently verified) **[ESTIMATED]**.
- **SAFE/PBOC FX reserves**: ~7th of following month **[ESTIMATED]**.
- **Shibor**: available in real time daily, no publication lag **[VERIFIED by nature of market data]**.

### 5.5 Proposed conservative lags (L, whole months)

| Series | L (adoption proposal) | Basis |
|---|---|---|
| Official PMI (mfg, non-mfg) | **0** | verified month-end same-month release (§5.1) |
| Caixin mfg / services PMI | 1 | verified 1st/3rd–6th of following month |
| CPI | 1 | verified ~9th of following month |
| PPI | 1 | same-day block as CPI **[ESTIMATED]** |
| Industrial VA growth | 1 | verified 13th–17th |
| Retail sales | 1 | same NBS joint midpoint release **[ESTIMATED]** |
| FAI | 1 | same NBS joint release **[ESTIMATED]** |
| GDP (quarterly) | ~0.5 month after quarter end; conservative integer **1** if forced to monthly grid | verified 15–18 days |
| M0/M1/M2 | 1 | PBOC ~10th–13th **[ESTIMATED]** |
| TSF (when fresh) | 1 | same PBOC day **[ESTIMATED]** |
| New RMB loans | 1 | same PBOC day **[ESTIMATED]** |
| Base money (PBOC balance sheet) | 1 | **[ESTIMATED]** |
| LPR | 0 (20th of the month = same-month policy value; use as yield/spread control)**[ESTIMATED convention]** | convention |
| Shibor (monthly mean) | 0 | market data |
| FX reserves | 1 | ~7th **[ESTIMATED]** |
| Customs exports/imports/trade balance | 1 | verified ~7th (Jul→7 Aug) |
| Consumer confidence | **2 (conservative) or verify then 1** | release schedule UNVERIFIED (NBS ~1–2 months; must check) |
| Electricity consumption | 1 | ministry/association cadence **[ESTIMATED]** |
| Freight volume (合计) | 1 | ministry cadence **[ESTIMATED]** |
| LPI | 1 (0–1) | ~1st of following month, supported by record dates **[ESTIMATED]** |
| Enterprise boom index | 1 (quarterly) | released with NBS quarterly block **[ESTIMATED]** |
| GDP | see above | see above |
| RRR (policy events) | not a monthly series — exclude from panel; keep as control | verified event dates |
| Fiscal revenue | 1 | typical cadence **[ESTIMATED]** |

---

## 6. Data Freshness Assessment and Provider-Level Findings

### 6.1 Jin10 (`cdn.jin10.com`) — **FROZEN ~Aug/Sep 2025** [VERIFIED]

All tested jin10-backed interfaces return data only up to roughly **28 Aug 2025 / 3 Sep 2025**:
official PMI up to 2025-08-31, Caixin mfg up to 2025-09-01, Caixin services up to 2025-09-03,
non-mfg PMI up to 2025-08-31, industrial VA up to 2025-08-15, exports/imports/trade up to
2025-08-07, GDP up to 2025-07-15, CPI up to 2025-08-09. Given the report date (2026-11-05),
the jin10 endpoints appear discontinued or un-updated in this akshare version. **Implication**:
usable for historical values, cross-checks, and release-date evidence only — **not** as a fresh
data source for 2025-10 onward.

### 6.2 Eastmoney (`datacenter-web.eastmoney.com`) — **FRESH** [VERIFIED]

All tested Eastmoney-backed interfaces return records through 2025-09 / 2026-Q3 / 2026-10 (e.g.
PMI 2026-10 released ~31 Oct; money supply 2026-09; GDP 2026Q3; LPR trading date 2026-10-20;
TSF-family `new_financial_credit` 2026-09). This is the most up-to-date, broadest family and the
recommended backbone for the panel. Two quality caveats found in code: consumer-confidence bug
(§6.5) and `发布时间` semantics (§5.3).

### 6.3 Sina (`finance.sina.com.cn/mac`) — **FRESH** [VERIFIED]

`macro_china_supply_of_money`, `macro_china_society_electricity`, `macro_china_central_bank_balance`,
`macro_china_freight_index`, `macro_china_retail_price_index`, `macro_china_postal_telecommunicational`,
`macro_china_society_traffic_volume`, plus the 南华 commodity/energy indices (Sina-hosted daily,
`最新值` style) — all return data through 2026-09 to 2026-11-04. Two caveats: `retail_price_index`
stops at **2022-09** (dead series), and long-format series (`traffic_volume`, `retail_price_index`)
need an additional `合计`/`居民消费项目` filter before use.

### 6.4 mofcom (`data.mofcom.gov.cn`) — **STALE** [VERIFIED]

`macro_china_shrzgm` (TSF) last row = **2026-04**; ~5 months behind available data (Sep 2026 data
are public). Treat the mofcom TSF interface as unreliable going forward; if TSF is needed fresh,
Phase 2 must find an alternative (e.g. custom PBOC/Eastmoney endpoint) or commit a snapshot at the
last available month and document the gap.

### 6.5 Caixin (`yun.ccxe.com.cn`) — **DEAD ENDPOINT** [VERIFIED]

`index_pmi_man_cx` / `index_pmi_ser_cx` / `index_pmi_com_cx` fail with `JSONDecodeError`; raw HTTP
probe returns HTTP 200 but HTML with title `财新数据平台 - 404`. Combined with jin10 Caixin freeze
(2025-09), **no fresh Caixin PMI source exists in akshare 1.20.25**. Decision required (see §7).

### 6.6 NBS (`data.stats.gov.cn`) — **UNREACHABLE in this environment** [VERIFIED]

All NBS-backed interfaces (`macro_china_nbs_nation`, `macro_china_nbs_region`,
`macro_china_urban_unemployment`) failed with the identical `curl_cffi` SSL error
(`curl (77) error adding trust anchors … CAfile: …\cacert.pem`). This is environmental (site-specific
TLS/fingerprinting from this network path), not proof that the interfaces are broken globally.
Retry from a China-facing network before declaring them unusable.

### 6.7 Other freshness notes [VERIFIED]

- `macro_china_lpr` and `macro_china_shibor_all`: fresh (daily, through Oct/Nov 2026).
- `macro_china_market_margin_sh/sz`: untested — freshness UNVERIFIED.
- `macro_china_stock_market_cap` and 南华 indices: fresh through Nov 2026 (market data, optional controls).
- Interface docstring date ranges deviate from observed data: e.g. `shibor_all` docstring "2017-03-17" but
  actual data from 2015-05-08. **Docstring claims must not be trusted as coverage facts.**

---

## 7. Potential Risks and Unresolved Questions (E)

### 7.1 Missing indicators (assignment names series with no dedicated interface) — **UNRESOLVED, decision required**

- **工业利润 (industrial profits)**: zero dedicated interfaces in akshare 1.20.25 — a whole-package search for
  `工业利润|利润总额` returned nothing. Options: (a) drop from the panel; (b) Phase 2 hand-write an
  Eastmoney datacenter query (the public page `data.eastmoney.com/cjsj/gylr.html` exists; the
  underlying `reportName` must be discovered and verified — UNVERIFIED); (c) retry via
  `macro_china_nbs_nation` from a network where the NBS works. Note NBS's industrial-profits release is
  **cumulative YTD**, so it needs single-month conversion — per the user's §4 requirement to flag
  cumulative-to-monthly conversions.
- **发电量 (electricity *output*)**: no dedicated interface; electricity is available only as **consumption**
  (`macro_china_society_electricity`, 全社会用电量). Consumption is a legitimate activity proxy but it is
  a different concept — group must agree to substitute and document the substitution, or source 发电量
  via NBS once reachable.
- **Unemployment rate**: `macro_china_urban_unemployment` exists and is known-good code (NBS monthly series
  城镇调查失业率) but could not be called here — status UNVERIFIED.

### 7.2 Stale series — recommendation: exclude from the modeling panel [VERIFIED evidence, UNRESOLVED decision]

jin10 family (PMI ×4, industrial VA, exports/imports/trade, GDP, CPI), `macro_china_shrzgm` (ends 2026-04),
`macro_china_retail_price_index` (ends 2022-09), `macro_china_real_estate` (ends 2025-12). The regime label
`d_t = 1{PMI_t < 50 and PMI_{t-1} < 50}` should be built from `macro_china_pmi` 制造业-指数 — fresh through
2026-10 — not from jin10 PMI.

### 7.3 Structural/data-convention risks [VERIFIED]

- **Jan–Feb combined convention**: retail sales, industrial VA, FAI, and electricity consumption carry no
  standalone January value (NBS publishes combined Jan–Feb for quantities). Phase 2 must choose a documented,
  leakage-safe rule (leave January NaN vs interpolate) — panel construction decision, not automatic.
- **GDP is quarterly** — no artificial monthly smoothing; bridge/nowcasting design per assignment §5.3.2.
- **CPI/PPI “当月” is an implied index (100 + yoy%)**, not a fixed-base level; mixed precision across
  eras (4 decimals pre-2011 vs 1 decimal after). Fine for YoY; do **not** difference across series as levels.
- **FAI “当月”** is derived from cumulative YTD by the provider; 2026-09 YoY −9.69 needs a sanity check.
- **hgjck unit ambiguity** (header carries no unit; 447,184,259.939 could be 万美元 but the implied
  magnitude does not reconcile with known monthly exports) — must be resolved in Phase 2 before any use.
**[UNVERIFIED]**.
- **Gold reserves unit**: 万盎司 (value 3235.17) computed into tonnes did not reconcile exactly with
  known PBOC holdings — **UNVERIFIED** unit.
- **Insurance income unit/units**: unspecified and inconsistent (first row 0.0) — exclude unless clarified.
- **M1 definition change (2024-01)**: personal demand deposits included — structural break to document.
- **LPR vs 旧贷款基准利率**: one interface returns both pre-2019 (RATE_1/RATE_2) and post-2019 (LPR1Y/5Y);
  these are different regimes of a similar rate — handle as a documented series change, not a continuation.
- **CGPI rebasing**: check base year across 2005–2026 before clustering.
- **`macro_china_non_man_pmi` / `macro_china_pmi` “同比” columns for PMI** — meaningless as YoY; use levels only.
- **Enterprise “信心指数” NaN in recent quarters** — insufficient coverage for 10-year window.
- **Column-name typos** in Sina long tables (e.g. “公里货物周转量同比增长”) — cosmetic but must be normalised in Phase 2.

### 7.4 Environment failures [VERIFIED — environmental, not interface-availability claims]

- `data.stats.gov.cn`: identical SSL error across three distinct interfaces (urban unemployment + 2 NBS generic probes) — retry from a different network recommended.
- `macro_china_swap_rate`: upstream returned empty data → akshare's own column-mapping fails.
- Session-level tooling: web search failed twice ("Web search cancelled"), guessed NBS calendar URL 404 — the official release calendar was NOT consulted online this session.

---

## 8. Candidate Shortlist Recommendation (informal, for Phase 2 planning — not yet approved)

A leakage-safe panel of ~25–28 monthly indicators is achievable **today** from fresh sources alone,
covering every required category:

- **Growth**: GDP (Q), industrial VA growth (target), FAI, 国房景气/enterprise boom (optional).
- **Prices**: CPI, PPI, CGPI (optional实际上分项).
- **PMI**: official mfg + official non-mfg (label series), LPI (optional logistics).
- **Money/credit**: M0, M1, M2 (levels + YoY), new loans, base money, FX reserves (optional).
- **Rates**: LPR 1Y/5Y (spreads), Shibor monthly mean O/N…1Y; RRR as control only.
- **Consumption**: retail sales; consumer confidence (main index only, L=2 until verified).
- **Trade**: exports/imports levels + YoY, trade balance (derived from customs levels).
- **Physical**: electricity consumption (substitute for generation — needs approval), freight volume (optional).
- **Excluded (stale/unverifiable)**: Caixin PMI family (unless an alternative source is agreed), jin10 series as data, TSF (mofcom stale), RPI, FR007 swap, insurance income.

---

## 9. Open Decision Points for the Group (awaiting Phase-1 approval)

1. **Industrial profits**: drop vs custom Eastmoney fetch in Phase 2 vs retry NBS from reachable network.
2. **Caixin PMI**: exclude entirely, or keep with explicit staleness caveat (not recommended for the regime label).
3. **TSF**: source alternatives (PBOC-based custom call) or drop.
4. **Consumer-confidence lag**: adopt L=2 conservatively, or verify the schedule first.
5. **Electricity substitution** (consumption instead of generation): confirm.
6. **NBS access**: attempt data.stats.gov.cn from a China-facing network (unemployment + NBS generic for,
   e.g., 发电量 / 工业企业利润总额 / 城镇调查失业率).
7. **Unit verifications** for `hgjck`, gold reserves, insurance income before any Phase 2 use.

Nothing in Phase 2 (dataset construction, cleaning, feature engineering, standardization, model
code, or any project file) was started — per Phase-1 instruction; this file itself is the only
artifact created (requested explicitly by the user).

---

## Appendix A. Session log summary (commands / calls made, in order)

1. `uv run python -c "import akshare; ..."` — akshare 1.20.25 import check.
2. Enumerated 85 `macro_china*` interfaces via `dir(ak)`.
3. Read akshare source files (`macro/macro_china.py`, `macro/macro_china_nbs.py`, `index/index_cx.py`) — column lists, url, page sizes.
4. PowerShell `Select-String` searches: 财新/caixin (found `index_cx.py`), 利润总额/工业利润/发电量/社会融资 (no dedicated industrial-profits or electricity-generation interface; only mofcom TSF).
5. Live verification batch #1 (41 calls) — recorded shape/columns/head/tail/range; 39 OK, 2 FAIL.
6. Live verification batch #2: `index_pmi_man_cx/_ser_cx/_com_cx` (3 FAIL JSONDecodeError), retry urban_unemployment (FAIL same SSL), `macro_china_nbs_nation` × 2 with `LAST10` (FAIL same SSL).
7. Raw HTTP probe of `yun.ccxe.com.cn` — HTTP 200 with HTML 404 page.
8. Lag analysis: (a) release-date lag percentiles (first pass, reference-month proxy mis-specified, superseded), (b) day-of-release histograms + tail samples for 8 jin10 series (correct vintage interpretation).
9. Web search × 2 (cancelled by tooling) and webfetch (404) for NBS release calendar.
10. Scratch test scripts written under `C:\Users\tashi\AppData\Local\Temp\opencode\ak_test*.py` (approved temp dir) and deleted after analyst review; **no project files were created or modified**.

## Appendix B. Verbatim errors observed

- `macro_china_swap_rate`: `ValueError: Length mismatch: Expected axis has 0 elements, new values have 17 elements`
- `macro_china_urban_unemployment` (×2) & `macro_china_nbs_nation` (×2 probes):
  `curl_cffi.requests.exceptions.SSLError: Failed to perform, curl: (77) error adding trust anchors from locations:  CAfile: D:\郑齐映\NUS\课程材料\QF5313\QF5313-Project-Q3\.venv\Lib\site-packages\certifi\cacert.pem CApath: none. See https://curl.se/libcurl/c/libcurl-errors.html first for more details.`
- `index_pmi_*_cx`: `requests.exceptions.JSONDecodeError: Expecting value: line 1 column 1 (char 0)`
- `yun.ccxe.com.cn` raw probe: HTTP 200, `content-type: text/html;charset=UTF-8`, body contains `<title>财新数据平台 - 404</title>`
- ripgrep-on-non-ASCII-path failure: `{"error":{"type":"unknown","message":"ripgrep execution failed"}}` (workaround: PowerShell Select-String)

*End of audit report — do not treat [ESTIMATED]/[UNVERIFIED] items as verified facts; verify before Phase 2 use.*

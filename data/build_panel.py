"""QF5313 Q3 — Phase 2: clean raw snapshots into the leakage-documented macro panel.

Pipeline: load data/raw → parse dates → harmonise to a monthly grid (2008-01 …)
→ per-series transformations (documented, unit-resolved) →
missing-value report + MAD outlier flags → write
  data/clean/macro_panel.csv            (wide monthly panel, d >= 20 indicators)
  data/clean/gdp_quarterly.csv          (quarterly GDP, NO interpolation)
  data/clean/enterprise_boom_quarterly.csv (quarterly boom index)
  data/clean/missing_report.md          (panel hygiene report)
  data/metadata.json                    (provenance, units, transforms, lags, SHA-256)

Leakage policy: the panel stores values at their REFERENCE month only; the
per-indicator publication lag L_i lives in data/metadata.json and downstream
code must construct F_t = { x_{i,s} : s + L_i <= t }. No value is placed at a
date at which it was not yet public, and no series is forward-filled here.

Usage:
    uv run python data/build_panel.py --seed 42
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW = REPO_ROOT / "data" / "raw"
CLEAN = REPO_ROOT / "data" / "clean"
META_PATH = REPO_ROOT / "data" / "metadata.json"
MISSING_PATH = CLEAN / "missing_report.md"
console = Console()

PANEL_START = "2008-01"          # monthly grid start (≥10y coverage requirement)
MAD_Z = 5.0                       # outlier rule: |x - median| > 5 * 1.4826 * MAD

# list collecting everything the metadata needs, filled by the transforms below
INDICATORS: list[dict] = []
CHECKS: list[dict] = []           # recorded verification checks (units, etc.)
QUARTERLY: dict[str, pd.DataFrame] = {}

# ---------------------------------------------------------------- helpers ----


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def raw(name: str) -> pd.DataFrame:
    """Read a raw snapshot as strings (preserves e.g. Sina '2024.10' dates)."""
    path = RAW / f"{name}.csv"
    df = pd.read_csv(path, dtype=str)
    CHECKS.append({"check": f"raw load {name}", "rows": int(len(df)),
                   "cols": int(df.shape[1]), "sha256": sha256_file(path)[:12]})
    return df


def reg(name: str, *, interface: str, column: str, unit: str, transformation: str,
        lag: int, lag_basis: str, notes: str = "") -> None:
    """Register a panel indicator's provenance/transform/lag documentation."""
    INDICATORS.append(
        {"name": name, "interface": interface, "column": column, "unit": unit,
         "transformation": transformation, "publication_lag_months": lag,
         "lag_basis": lag_basis, "notes": notes})


def cn_month(s: pd.Series) -> pd.Series:
    """Parse '2008年02月份' → Period('2008-02')."""
    m = s.str.extract(r"^(\d{4})年(\d{2})月份")
    return pd.PeriodIndex(pd.to_datetime(m[0] + m[1], format="%Y%m"), freq="M")


def sina_month(s: pd.Series) -> pd.Series:
    """Parse Sina decimal strings '2024.10'/'2024.2' → Period (string-safe)."""
    parts = s.str.split(".", regex=False, expand=True)
    y, m = parts[0], parts[1]
    return pd.PeriodIndex(pd.to_datetime(y + m.str.zfill(2), format="%Y%m"),
                          freq="M")


def yoy_from_levels(ser: pd.Series) -> pd.Series:
    """YoY % from a monthly level series indexed by (year, month)."""
    lvl = ser.copy()
    idx = lvl.index
    out = np.full(len(lvl), np.nan)
    for i, p in enumerate(idx):
        prior = p - 12
        j = idx.get_loc(prior) if prior in idx else None
        if j is not None and pd.notna(lvl.iloc[i]) and pd.notna(lvl.iloc[j]) and lvl.iloc[j]:
            out[i] = (lvl.iloc[i] / lvl.iloc[j] - 1.0) * 100.0
    return pd.Series(out, index=idx)


# ---------------------------------------------------------------- loading ----

series: dict[str, pd.Series] = {}

# --- PMI (label series; fresh through 2026-10; “同比” columns meaningless) ---
pmi = raw("pmi")
pmi.index = cn_month(pmi["月份"])
series["pmi_man"] = pd.to_numeric(pmi["制造业-指数"], errors="coerce")
series["pmi_nonman"] = pd.to_numeric(pmi["非制造业-指数"], errors="coerce")
reg("pmi_man", interface="macro_china_pmi", column="制造业-指数", unit="index",
    transformation="none (survey diffusion index; 同比 column NOT used — meaningless)",
    lag=0, lag_basis="verified: released 09:30 on last day of reference month "
    "(audit §5.1)", notes="label source for d_t = 1{PMI_t<50 and PMI_{t-1}<50}")
reg("pmi_nonman", interface="macro_china_pmi", column="非制造业-指数", unit="index",
    transformation="none", lag=0,
    lag_basis="verified: released with mfg PMI at month-end")

# --- CPI / PPI / CGPI (implied-index caveat: use YoY columns only) -----------
cpi = raw("cpi"); cpi.index = cn_month(cpi["月份"])
series["cpi_yoy"] = pd.to_numeric(cpi["全国-同比增长"], errors="coerce")
reg("cpi_yoy", interface="macro_china_cpi", column="全国-同比增长", unit="% YoY",
    transformation="as published; implied-index 当月 column NOT used",
    lag=1, lag_basis="verified: released ~9th of following month (audit §5.1)")

ppi = raw("ppi"); ppi.index = cn_month(ppi["月份"])
series["ppi_yoy"] = pd.to_numeric(ppi["当月同比增长"], errors="coerce")
reg("ppi_yoy", interface="macro_china_ppi", column="当月同比增长", unit="% YoY",
    transformation="as published (implied index)", lag=1,
    lag_basis="estimated: released in the CPI joint block ~9th (audit §5.4)")

cgpi = raw("cgpi"); cgpi.index = cn_month(cgpi["月份"])
series["cgpi_yoy"] = pd.to_numeric(cgpi["总指数-同比增长"], errors="coerce")
reg("cgpi_yoy", interface="macro_china_qyspjg", column="总指数-同比增长",
    unit="% YoY", transformation="as published; base-year history to verify before "
    "clustering (audit §7.3)", lag=1,
    lag_basis="estimated: PBOC CGPI monthly release cadence")

# --- industrial value-added growth (TARGET series) ---------------------------
iva = raw("industrial_va"); iva.index = cn_month(iva["月份"])
series["industrial_va_yoy"] = pd.to_numeric(iva["同比增长"], errors="coerce")
reg("industrial_va_yoy", interface="macro_china_gyzjz", column="同比增长",
    unit="% YoY", transformation="as published (value-added growth)",
    lag=1, lag_basis="verified: NBS quantities block, ~13th–17th of following "
    "month (audit §5.1)",
    notes=" 发布时间 column is a period marker, NOT a release date (audit §5.3)")

# --- retail sales (Jan–Feb: rows present but NaN in recent years; leave NaN) -
ret = raw("retail_sales"); ret.index = cn_month(ret["月份"])
series["retail_yoy"] = pd.to_numeric(ret["同比增长"], errors="coerce")
reg("retail_yoy", interface="macro_china_consumer_goods_retail", column="同比增长",
    unit="% YoY", transformation="none; standalone Jan/Feb missing since 2012 "
    "(combined Jan–Feb), left as NaN — group decision, no interpolation",
    lag=1, lag_basis="estimated: NBS joint quantities block ~15th")

# --- FAI: provider 同比 unreliable/absent → derive nominal monthly YoY --------
fai = raw("fai"); fai.index = cn_month(fai["月份"])
fai_lvl = pd.to_numeric(fai["当月"], errors="coerce")
series["fai_yoy"] = yoy_from_levels(fai_lvl).where(fai_lvl.notna())
reg("fai_yoy", interface="macro_china_gdzctz", column="当月 → derived",
    unit="% YoY (nominal)", transformation="当月 level ÷ same month previous year − 1 "
    "(provider 同比 column unreliable, audit §7.3); months Jan–Feb NaN (combined)",
    lag=1, lag_basis="estimated: NBS joint quantities block ~15th",
    notes="2026-09 同比 −9.69 reported by audit: sanity-check flag in missing report")

# --- consumer confidence (source bug: only 信心指数 usable) --------------------
cc = raw("consumer_confidence"); cc.index = cn_month(cc["月份"])
series["consumer_confidence"] = pd.to_numeric(cc["消费者信心指数-指数值"],
                                              errors="coerce")
reg("consumer_confidence", interface="macro_china_xfzxx",
    column="消费者信心指数-指数值", unit="index",
    transformation="none; 预期指数 and 满意指数 excluded (source-code bug overwrites "
    "预期指数, audit §6.5)", lag=2,
    lag_basis="conservative until schedule verified (group decision, audit §5.5)")

# --- LPI ---------------------------------------------------------------------
lpi = raw("lpi")
lpi.index = pd.PeriodIndex(pd.to_datetime(lpi["日期"]) - pd.offsets.MonthBegin(1),
                           freq="M")
series["lpi"] = pd.to_numeric(lpi["最新值"], errors="coerce")
reg("lpi", interface="macro_china_lpi_index", column="最新值", unit="index",
    transformation="none; reference month = release date − 1 month (row dated "
    "2026-10-01 → Sep-2026 value), documented re-dating",
    lag=1, lag_basis="estimated: released ~1st of following month (record dates)")

# --- electricity: source column is cumulative-YTD → de-accumulate ------------
elec = raw("electricity")
elec.index = sina_month(elec["统计时间"])
cum = pd.to_numeric(elec["全社会用电量"], errors="coerce")        # 万千瓦时, cum-YTD
cum_yoy = pd.to_numeric(elec["全社会用电量同比"], errors="coerce")
flow, fyoy = {}, {}
for p, v in cum[cum.notna()].items():
    prior = p - 1
    if prior in cum.index and pd.notna(cum.loc[prior]) and v >= cum.loc[prior]:
        flow[p] = v - cum.loc[prior]
    else:                       # February row = Jan–Feb combined (Jan row absent)
        flow[p] = v
    # cumulative YoY applies to identical windows; de-accumulated YoY matches for
    # month ≥ 3, and for February the published value is the Jan–Feb-window YoY
    fyoy[p] = cum_yoy.loc[p]
elec_flow = pd.Series(flow).sort_index()
series["elec_cons_yoy"] = pd.Series(fyoy).sort_index()
reg("elec_cons_yoy", interface="macro_china_society_electricity",
    column="全社会用电量同比", unit="% YoY",
    transformation="published 同比 of cumulative-YTD consumption kept (identical "
    "window YoY); monthly levels derived by de-accumulation in-check column; "
    "January NaN (Jan–Feb combined) — substitution approved by group: "
    "consumption NOT generation", lag=1,
    lag_basis="estimated: ministry/association cadence (audit §5.5)")
ELEC_FLOW_DOC = elec_flow          # kept for the missing report (levels audit)

# --- traffic (long format → 合计 rows; cumulative behaviour validated auto) ----
traf = raw("traffic")
traf_tot = traf[traf["统计对象"] == "合计"].copy()
traf_tot.index = sina_month(traf_tot["统计时间"])
traf_tot = traf_tot[~traf_tot.index.duplicated(keep="last")].sort_index()
CHECKS.append({"check": "traffic dedupe", "rows_before": int(len(traf)),
               "rows_total_after_dedupe": int(len(traf_tot))})
freight = pd.to_numeric(traf_tot["货运量"], errors="coerce")
freight_yoy_pub = pd.to_numeric(traf_tot["货运量同比增长"], errors="coerce")
# structural check: is the level cumulative-YTD or a true monthly flow, and is
# the published YoY consistent with a de-accumulated reading?
freight_m = freight.reindex(pd.period_range(freight.index.min(),
                                            freight.index.max(), freq="M"))
_dif = freight_m.diff(1)
resid_month = (freight_yoy_pub - yoy_from_levels(_dif)).abs().mean()
resid_cum = (freight_yoy_pub - yoy_from_levels(freight)).abs().mean()
freight_is_cumulative = resid_cum < resid_month
freight_base = _dif if freight_is_cumulative else freight
series["freight_yoy"] = freight_yoy_pub.where(freight_yoy_pub.notna())
reg("freight_yoy", interface="macro_china_society_traffic_volume",
    column="货运量同比增长 (合计 rows)", unit="% YoY",
    transformation="published YoY; 长-filter to 合计 (total) rows; column-name "
    "typos in source normalised; structural check: cumulative-YTD="
    f"{freight_is_cumulative} (cum-MAE {resid_cum:.2f} vs month-MAE {resid_month:.2f})",
    lag=1, lag_basis="estimated: ministry cadence")
CHECKS.append({"check": "traffic structural test", "cumulative_mae": float(resid_cum),
               "monthly_mae": float(resid_month),
               "treated_as_cumulative": bool(freight_is_cumulative)})

# --- money & credit ----------------------------------------------------------
ms = raw("money_supply"); ms.index = cn_month(ms["月份"])
for tag, col in [("m2", "货币和准货币(M2)-同比增长"),
                 ("m1", "货币(M1)-同比增长"), ("m0", "流通中的现金(M0)-同比增长")]:
    series[f"{tag}_yoy"] = pd.to_numeric(ms[col], errors="coerce")
    reg(f"{tag}_yoy", interface="macro_china_money_supply", column=col,
        unit="% YoY", transformation="as published (stock)", lag=1,
        lag_basis="estimated: PBOC monthly financial statistics ~10th–13th",
        notes=("M1 definition change 2024-01 (personal demand deposits included) "
               "— structural break documented (audit §7.3)" if tag == "m1" else "")
        or f"{tag.upper()} stock YoY as published")

nl = raw("new_loans"); nl.index = cn_month(nl["月份"])
series["new_loans_yoy"] = pd.to_numeric(nl["当月-同比增长"], errors="coerce")
reg("new_loans_yoy", interface="macro_china_new_financial_credit",
    column="当月-同比增长", unit="% YoY", transformation="as published (flow)",
    lag=1, lag_basis="estimated: same PBOC day as M-series")

tsf = raw("tsf")
tsf.index = pd.PeriodIndex(pd.to_datetime(tsf["月份"], format="%Y%m"), freq="M")
series["tsf_flow"] = pd.to_numeric(tsf["社会融资规模增量"], errors="coerce")
reg("tsf_flow", interface="macro_china_shrzgm", column="社会融资规模增量",
    unit="亿元 (RMB 100 million)", transformation="as published monthly flow "
    "(string→numeric); SOURCE STALE: ends 2026-04, NaN afterwards with a "
    "documented gap — group decision 'keep with gap'", lag=1,
    lag_basis="estimated: same PBOC day as M-series")

cbb = raw("central_bank_balance"); cbb.index = sina_month(cbb["统计时间"])
base_money = pd.to_numeric(cbb["储备货币"], errors="coerce")
series["base_money_yoy"] = base_money.pct_change(12) * 100.0
reg("base_money_yoy", interface="macro_china_central_bank_balance",
    column="储备货币 → derived", unit="% YoY",
    transformation="YoY derived from monthly stock levels (pct_change 12)",
    lag=1, lag_basis="estimated: PBOC balance-sheet cadence")

# --- rates (daily → monthly) --------------------------------------------------
lpr = raw("lpr")
lpr["TRADE_DATE"] = pd.to_datetime(lpr["TRADE_DATE"])
lpr.index = pd.PeriodIndex(lpr["TRADE_DATE"], freq="M")
lpr_g = lpr[["LPR1Y", "LPR5Y"]].apply(pd.to_numeric, errors="coerce").groupby(
    lpr.index).last()
series["lpr_1y"] = lpr_g["LPR1Y"]
series["lpr_5y"] = lpr_g["LPR5Y"]
reg("lpr_1y", interface="macro_china_lpr", column="LPR1Y", unit="% p.a.",
    transformation="daily → monthly last value (announced on 20th within month)",
    lag=0, lag_basis="estimated convention: announced 20th of the month",
    notes="LPR regime from 2019-08; pre-2019 rows use RATE_1/RATE_2 — different "
    "benchmark regime, excluded from these columns (documented break)")
reg("lpr_5y", interface="macro_china_lpr", column="LPR5Y", unit="% p.a.",
    transformation="idem lpr_1y", lag=0,
    lag_basis="estimated convention: announced 20th of the month")

shib = raw("shibor")
shib.index = pd.PeriodIndex(pd.to_datetime(shib["日期"]), freq="M")
shib_g = shib[["O/N-定价", "3M-定价", "1Y-定价"]].apply(
    pd.to_numeric, errors="coerce").groupby(shib.index).mean()
series["shibor_on"] = shib_g["O/N-定价"]
series["shibor_3m"] = shib_g["3M-定价"]
series["shibor_1y"] = shib_g["1Y-定价"]
reg("shibor_on", interface="macro_china_shibor_all", column="O/N-定价",
    unit="% p.a.", transformation="daily → monthly mean", lag=0,
    lag_basis="verified: real-time market data (audit §5.4)")
reg("shibor_3m", interface="macro_china_shibor_all", column="3M-定价",
    unit="% p.a.", transformation="daily → monthly mean", lag=0,
    lag_basis="verified: real-time market data")
reg("shibor_1y", interface="macro_china_shibor_all", column="1Y-定价",
    unit="% p.a.", transformation="daily → monthly mean", lag=0,
    lag_basis="verified: real-time market data")

# --- external sector + unit resolution (customs vs jin10 cross-check) ---------
x = raw("trade_balance_xcheck")            # jin10, release-dated rows
tb = x[["日期", "今值"]].apply(lambda s: pd.to_numeric(s, errors="coerce"))
tb["日期"] = pd.to_datetime(x["日期"])
tb = tb.dropna()
tb.index = pd.PeriodIndex(tb["日期"], freq="M") - 1   # ref month = release − 1
tb_val = tb["今值"].groupby(tb.index).last()          # 亿美元

cus = raw("customs"); cus.index = cn_month(cus["月份"])
cexp = pd.to_numeric(cus["当月出口额-金额"], errors="coerce")   # unit: see check
cimp = pd.to_numeric(cus["当月进口额-金额"], errors="coerce")
overlap = tb_val.index.intersection(cus.index)
fac = (tb_val[overlap] / ((cexp[overlap] - cimp[overlap]).dropna())).dropna()
scale = float(fac.median())
rel_err = float((fac / scale - 1).abs().median())
unit_ok = (abs(scale - 1e-5) / 1e-5 < 0.25) and rel_err < 0.03
CHECKS.append({"check": "customs unit calibration vs jin10 trade balance",
               "n_overlap_months": int(len(fac)), "months": [str(p) for p in overlap],
               "median_factor_to_yi_usd": scale,
               "median_abs_rel_deviation": rel_err,
               "resolved_unit": "千美元 (USD thousands)" if unit_ok else "UNRESOLVED — column excluded",
               "accepted": bool(unit_ok),
               "note": "factor ×1e-5 ⇒ value×1e3 = USD ⇒ 千美元; residual of ~1pct "
               "reflects preliminary-vs-final revision vintages between sources"})
if unit_ok:
    # (export − import) in 千美元 → bn USD: ×1e3 (USD) / 1e9
    series["trade_bal_bn_usd"] = ((cexp - cimp) * 1e3 / 1e9).rename(None)
reg("trade_bal_bn_usd", interface="macro_china_hgjck",
    column="当月出口额−当月进口额 → derived", unit="bn USD",
    transformation="derived from customs levels; unit resolved ≈千美元 via jin10 "
    "trade-balance overlap calibration (see metadata.checks, factor ≈1e-5 to "
    "亿美元 ⇒ ÷1e5 to bn USD)", lag=1,
    lag_basis="verified: GACC ~7th of following month (audit §5.1)")
for tag, col in [("export", "当月出口额-同比增长"), ("import", "当月进口额-同比增长")]:
    series[f"{tag}_yoy"] = pd.to_numeric(cus[col], errors="coerce")
    reg(f"{tag}_yoy", interface="macro_china_hgjck", column=col, unit="% YoY",
        transformation="as published (unit-independent)", lag=1,
        lag_basis="verified: GACC ~7th of following month")

fx = raw("fx_gold"); fx.index = cn_month(fx["月份"])
series["fx_reserves_yoy"] = pd.to_numeric(fx["国家外汇储备-同比"], errors="coerce")
reg("fx_reserves_yoy", interface="macro_china_fx_gold", column="国家外汇储备-同比",
    unit="% YoY", transformation="as published; GOLD RESERVES excluded — unit "
    "万盎司 UNVERIFIED (audit §7.3)", lag=1,
    lag_basis="estimated: SAFE ~7th of following month")

# --- fiscal -------------------------------------------------------------------
fis = raw("fiscal_revenue"); fis.index = cn_month(fis["月份"])
series["fiscal_rev_yoy"] = pd.to_numeric(fis["当月-同比增长"], errors="coerce")
reg("fiscal_rev_yoy", interface="macro_china_czsr", column="当月-同比增长",
    unit="% YoY", transformation="as published", lag=1,
    lag_basis="estimated: typical ministry cadence (audit §5.5)")

# --- quarterly series (kept at quarterly frequency — NO interpolation) --------
g = raw("gdp")
qend = g["季度"].str.extract(r"^(\d{4})年第([1-4])(?:-([1-4]))?季度$")
mask = qend[0].notna()
gq = g[mask].copy()
end_q = qend.loc[mask, 2].fillna(qend.loc[mask, 1]).astype(int)
years = qend.loc[mask, 0].astype(int)
gq["quarter"] = pd.PeriodIndex(
    [pd.Period(f"{int(y)}Q{int(q)}", freq="Q") for y, q in zip(years, end_q)])
QUARTERLY["gdp_quarterly"] = pd.DataFrame(
    {"gdp_abs_cum_bn": pd.to_numeric(gq["国内生产总值-绝对值"], errors="coerce").values,
     "gdp_yoy": pd.to_numeric(gq["国内生产总值-同比增长"], errors="coerce").values,
     "window": np.where(gq["季度"].str.contains("-"), "ytd_cumulative",
                        "single_quarter")},
    index=gq["quarter"]).sort_index()
CHECKS.append({"check": "GDP quarterly rows",
               "kept_rows": int(len(gq)),
               "note": "Eastmoney publishes YTD windows (第1-2/3/4季度 = H1/9M/FY, "
               "2006Q1-style single-quarter rows for Q1 only): all rows kept AS "
               "PUBLISHED with a window tag (ytd_cumulative vs single_quarter); "
               "values are cumulative absolute value (亿元) and cumulative YoY (%); "
               "single-quarter YoY for Q2–Q4 is not published by this interface and "
               "is NOT derived here (no nominal-to-real bridge invented); NO "
               "interpolation to monthly frequency"})

eb = raw("enterprise_boom")
eqend = eb["季度"].str.extract(r"^(\d{4})年第([1-4])季度$")
mask_e = eqend[0].notna()
eq = eb[mask_e].copy()
eq["quarter"] = pd.PeriodIndex(
    [pd.Period(f"{int(y)}Q{int(q)}", freq="Q")
     for y, q in zip(eqend.loc[mask_e, 0].astype(int), eqend.loc[mask_e, 1].astype(int))])
QUARTERLY["enterprise_boom_quarterly"] = pd.DataFrame(
    {"boom_index": pd.to_numeric(eq["企业景气指数-指数"], errors="coerce").values},
    index=eq["quarter"]).sort_index()
reg("enterprise_boom (quarterly)", interface="macro_china_enterprise_boom_index",
    column="企业景气指数-指数", unit="index", transformation="none (quarterly)",
    lag=1, lag_basis="estimated: released with NBS quarterly block",
    notes="企业家信心指数 NaN in recent quarters — excluded")
reg("gdp (quarterly)", interface="macro_china_gdp", column="国内生产总值-同比增长",
    unit="% YoY (YTD window as published)", transformation="none — quarterly "
    "frequency preserved with window tag (Q1 single quarter; Q2–Q4 YTD "
    "cumulative); NO monthly interpolation or bridge conversion performed here",
    lag=1, lag_basis="verified: released ~15–18 days after quarter end "
    "(audit §5.1); conservative integer lag 1")

# ------------------------------------------------------------ panel assembly --
grid = pd.period_range(PANEL_START, series_ts_max := max(
    s.index.max() for s in series.values()), freq="M")
panel = pd.DataFrame({k: s.reindex(grid) for k, s in series.items()})
panel.index.name = "month"

# derived spreads (controls for the probit z_t), same-day availability
panel["shibor_spread_1y_on"] = panel["shibor_1y"] - panel["shibor_on"]
reg("shibor_spread_1y_on", interface="derived", column="shibor_1y − shibor_on",
    unit="percentage points", transformation="derived tenor spread", lag=0,
    lag_basis="earlier of components = same month (market data)")
panel["lpr_spread_5y_1y"] = panel["lpr_5y"] - panel["lpr_1y"]
reg("lpr_spread_5y_1y", interface="derived", column="lpr_5y − lpr_1y",
    unit="percentage points", transformation="derived term spread", lag=0,
    lag_basis="same announcement day (20th)")

# ------------------------------------------------- hygiene: missing + outliers -
miss = {}
for col in panel.columns:
    s = panel[col]
    jan_missing = int(s[s.index.month == 1].isna().sum())
    miss[col] = {
        "n_valid": int(s.notna().sum()),
        "n_missing": int(s.isna().sum()),
        "first_valid": str(s.first_valid_index()),
        "last_valid": str(s.last_valid_index()),
        "january_missing": jan_missing,
        "coverage_years": round(s.notna().sum() / 12.0, 1),
    }
outliers = {}
flag_rows = []
for col in panel.columns:
    s = panel[col].dropna()
    if len(s) < 24:
        continue
    med = float(s.median())
    mad = float((s - med).abs().median())
    if mad == 0:
        continue
    z = (s - med).abs() / (1.4826 * mad)
    bad = z[z > MAD_Z]
    for p, v in bad.items():
        flag_rows.append({"indicator": col, "month": str(p), "value": round(float(v), 4),
                          "median": round(med, 4), "mad_z": round(float(z[p]), 1)})
    if len(bad):
        outliers[col] = {"n_flagged": int(len(bad)), "median": med, "mad": mad}
CHECKS.append({"check": "MAD outlier scan", "rule": f"|x − median| > {MAD_Z} × 1.4826·MAD",
               "n_flagged": len(flag_rows),
               "policy": "flags reported only — values NOT modified or removed"})

# ----------------------------------------------------------------- write out ---
CLEAN.mkdir(parents=True, exist_ok=True)
panel_out = panel.copy()
panel_out.index = panel_out.index.astype(str)
panel_out.to_csv(CLEAN / "macro_panel.csv", encoding="utf-8-sig")
panel_sha = sha256_file(CLEAN / "macro_panel.csv")

for fname, qdf in QUARTERLY.items():
    qdf.index.name = "quarter"
    out_q = qdf.copy()
    for c in out_q.columns:
        if c != "window":
            out_q[c] = pd.to_numeric(out_q[c], errors="coerce").round(4)
    out_q.to_csv(CLEAN / f"{fname}.csv", encoding="utf-8-sig")

manifest = json.loads((RAW / "manifest.json").read_text(encoding="utf-8"))
meta = {
    "built_utc": datetime.now(timezone.utc).isoformat(),
    "script": "data/build_panel.py",
    "panel_start": PANEL_START,
    "panel_end": str(grid.max()),
    "n_monthly_indicators": int(panel.shape[1]),
    "n_rows": int(len(panel)),
    "panel_file": "data/clean/macro_panel.csv",
    "panel_sha256": panel_sha,
    "quarterly_files": {n: {"sha256": sha256_file(CLEAN / f"{n}.csv"),
                            "rows": int(len(q))} for n, q in QUARTERLY.items()},
    "decisions": {
        "caixin_pmi": "excluded — no fresh source in akshare 1.20.25 (endpoint "
                      "dead; jin10 frozen 2025-09) — group decision",
        "tsf": "kept with documented gap (source ends 2026-04)",
        "jan_feb": "standalone Jan/Feb left NaN for combined NBS quantities",
        "electricity": "consumption substituted for generation (approved)",
        "industrial_profits": "dropped — no interface (audit §7.1)",
        "gdp": "quarterly frequency preserved; no monthly interpolation",
    },
    "indicators": INDICATORS,
    "raw_snapshot_provenance": {
        k: {"file": f"data/raw/{v['file']}", "sha256": v["sha256"],
            "retrieved_utc": manifest.get("retrieved_utc"),
            "interface": v["interface"], "role": v["role"], "note": v["note"]}
        for k, v in manifest["interfaces"].items()},
    "checks": CHECKS,
    "missing": miss,
    "outliers_flagged": flag_rows,
    "leakage_policy": "panel values sit at reference month s; use is restricted "
    "to F_t = { x_{i,s} : s + L_i <= t } via publication_lag_months per indicator",
}
META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                     encoding="utf-8")

# ------------------------------------------------------------ missing report ---
lines = [
    "# Panel hygiene report (generated by data/build_panel.py)", "",
    f"- Grid: {PANEL_START} … {grid.max()} ({len(panel)} months), "
    f"{panel.shape[1]} monthly indicators.",
    "- Missing values: preserved as NaN (no interpolation, no forward-fill).",
    "- Outliers: MAD rule |x − median| > 5·1.4826·MAD — flagged, not removed.",
    "- Jan–Feb combined quantities: standalone Jan/Feb left NaN by decision.",
    "", "## Coverage and missingness", "",
    "| indicator | valid | missing | first | last | Jan missing | years |",
    "|---|---|---|---|---|---|---|",
]
for col, m in miss.items():
    lines.append(f"| {col} | {m['n_valid']} | {m['n_missing']} | {m['first_valid']} | "
                 f"{m['last_valid']} | {m['january_missing']} | {m['coverage_years']} |")
if flag_rows:
    lines += ["", "## MAD-flagged outliers (top 25 by |z|)", "",
              "| indicator | month | value | median | MAD-z |", "|---|---|---|---|---|"]
    for r in sorted(flag_rows, key=lambda d: -d["mad_z"])[:25]:
        lines.append(f"| {r['indicator']} | {r['month']} | {r['value']} | "
                     f"{r['median']} | {r['mad_z']} |")
lines += ["", "## Recorded checks", ""]
for c in CHECKS:
    lines.append(f"- `{c['check']}`: " + json.dumps(
        {k: v for k, v in c.items() if k != "check"}, ensure_ascii=False))
MISSING_PATH.write_text("\n".join(lines), encoding="utf-8")

# ------------------------------------------------------------------ summary ----
tbl = Table(title=f"Macro panel — {panel.shape[1]} indicators, "
                  f"{PANEL_START} … {grid.max()}")
tbl.add_column("indicator"); tbl.add_column("valid"); tbl.add_column("coverage (y)")
tbl.add_column("L (mo)"); tbl.add_column("unit")
lag_by_name = {i["name"]: i for i in INDICATORS}
for col in panel.columns:
    key = col if col in lag_by_name else col
    info = lag_by_name.get(key, lag_by_name.get(key.replace("_yoy", "_yoy"), {}))
    tbl.add_row(col, str(miss[col]["n_valid"]), f"{miss[col]['coverage_years']}",
                str(info.get("publication_lag_months", "?")),
                (info.get("unit", "?"))[:22])
console.print(tbl)
console.print(Panel(
    f"macro_panel.csv dx{panel.shape[0]}×{panel.shape[1]} (sha256 {panel_sha[:12]}…) · "
    f"quarterly: {[n for n in QUARTERLY]} · flagged outliers: {len(flag_rows)}\n"
    f"metadata → {META_PATH.name} · hygiene report → {MISSING_PATH.name}",
    title="Build summary", border_style="green"))

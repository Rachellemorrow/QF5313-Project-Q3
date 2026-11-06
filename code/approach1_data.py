"""Option A — shared data preparation and information-set utilities.

This module is both importable and runnable:

    uv run python code/approach1_data.py --seed 42

It verifies the frozen inputs against the SHA-256 values recorded in
``interface.md``, builds the publication-lag feature panel, constructs the
PMI contraction labels and the derived chain-linked single-quarter GDP target,
runs the arithmetic/leakage checks, and writes audit artifacts under
``results/approach1/data/``.

Nothing in ``data/`` is modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# ---------------------------------------------------------------------------
# Paths and frozen-input contract
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
CLEAN_DIR = DATA_DIR / "clean"
RESULTS_DIR = ROOT / "results" / "approach1"
DATA_OUT_DIR = RESULTS_DIR / "data"

PANEL_PATH = CLEAN_DIR / "macro_panel.csv"
GDP_PATH = CLEAN_DIR / "gdp_quarterly.csv"
METADATA_PATH = DATA_DIR / "metadata.json"

EXPECTED_SHA256 = {
    "macro_panel.csv": "725699fc99e28878fbe24ef52308148f15f4058ecead8f3d750c67b273b5eabe",
    "gdp_quarterly.csv": "b7c93b71f1df5f513f7105535ff2dbba59dbd4c0ab66bdf91b612a92cc2b0c01",
}

PANEL_START = "2008-01"
PANEL_END = "2026-11"
EXCLUDED_MONTH = "2026-11"

# Shared schedules fixed by interface.md
REGIME_INIT_START = "2008-02"
REGIME_INIT_END = "2019-12"
REGIME_OOS_START = "2020-01"
REGIME_OOS_END = "2026-10"
REGIME_ORIGIN_START = "2019-12"
REGIME_ORIGIN_END = "2026-09"

GDP_INIT_START = "2008Q1"
GDP_INIT_END = "2017Q4"
GDP_OOS_START = "2018Q1"
GDP_OOS_END = "2026Q3"

GDP_PUBLICATION_LAG_MONTHS = 1
VINTAGES = ("M1", "M2", "M3")

# Default PCA screening rule approved by the group.
SCREEN_RECENT_MONTHS = 60
SCREEN_MIN_RECENT_COVERAGE = 0.70
SCREEN_MIN_OBSERVATIONS = 24
MAX_FACTORS = 8

# Rate / spread block reserved for the Probit controls.
RATE_BLOCK = (
    "lpr_1y",
    "lpr_5y",
    "shibor_on",
    "shibor_3m",
    "shibor_1y",
    "shibor_spread_1y_on",
    "lpr_spread_5y_1y",
)

CONSOLE = Console()


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def month_index(months: list[str]) -> dict[str, int]:
    return {m: i for i, m in enumerate(months)}


def add_months(month: str, k: int) -> str:
    return str(pd.Period(month, freq="M") + k)


def prev_month(month: str) -> str:
    return add_months(month, -1)


def month_end(month: str) -> str:
    """ISO date of the last calendar day of ``month`` (YYYY-MM-DD)."""
    return pd.Period(month, freq="M").end_time.date().isoformat()


def quarter_parts(q: str) -> tuple[int, int]:
    year = int(q[:4])
    quarter = int(q[-1])
    if quarter not in (1, 2, 3, 4):
        raise ValueError(f"bad quarter label: {q!r}")
    return year, quarter


def quarter_months(q: str) -> tuple[str, str, str]:
    year, quarter = quarter_parts(q)
    first = (quarter - 1) * 3 + 1
    return tuple(f"{year}-{m:02d}" for m in (first, first + 1, first + 2))  # type: ignore[return-value]


def quarter_end_month(q: str) -> str:
    return quarter_months(q)[2]


def quarter_from_month(month: str) -> str:
    p = pd.Period(month, freq="M")
    return f"{p.year}Q{(p.month - 1) // 3 + 1}"


def quarter_list(start: str, end: str) -> list[str]:
    y0, q0 = quarter_parts(start)
    y1, q1 = quarter_parts(end)
    out = []
    y, q = y0, q0
    while (y, q) <= (y1, q1):
        out.append(f"{y}Q{q}")
        q += 1
        if q == 5:
            y, q = y + 1, 1
    return out


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if obj is pd.NaT:
        return None
    return obj


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(_jsonable(payload), fh, ensure_ascii=False, indent=2)
        fh.write("\n")


# ---------------------------------------------------------------------------
# Frozen inputs
# ---------------------------------------------------------------------------

def load_metadata() -> dict[str, Any]:
    with METADATA_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_frozen_inputs(verbose: bool = True) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Load and verify the frozen panel, GDP source, and metadata."""
    for name, expected in EXPECTED_SHA256.items():
        path = CLEAN_DIR / name
        if not path.exists():
            raise FileNotFoundError(f"frozen input missing: {path}")
        actual = sha256_file(path)
        if actual != expected:
            raise RuntimeError(
                f"SHA-256 mismatch for {path.name}: expected {expected}, got {actual}. "
                "Stop and reconcile the frozen snapshot; do not rebuild it."
            )
        if verbose:
            CONSOLE.print(f"[green]hash ok[/green] {path.name}: {actual}")

    panel = pd.read_csv(PANEL_PATH, dtype={"month": str})
    gdp = pd.read_csv(GDP_PATH, dtype={"quarter": str, "window": str})
    metadata = load_metadata()

    expected_panel_cols = [
        "month", "pmi_man", "pmi_nonman", "cpi_yoy", "ppi_yoy", "cgpi_yoy",
        "industrial_va_yoy", "retail_yoy", "fai_yoy", "consumer_confidence", "lpi",
        "elec_cons_yoy", "freight_yoy", "m2_yoy", "m1_yoy", "m0_yoy",
        "new_loans_yoy", "tsf_flow", "base_money_yoy", "lpr_1y", "lpr_5y",
        "shibor_on", "shibor_3m", "shibor_1y", "trade_bal_bn_usd", "export_yoy",
        "import_yoy", "fx_reserves_yoy", "fiscal_rev_yoy", "shibor_spread_1y_on",
        "lpr_spread_5y_1y",
    ]
    if list(panel.columns) != expected_panel_cols:
        raise RuntimeError(
            "unexpected macro_panel.csv header; the frozen schema changed.\n"
            f"expected: {expected_panel_cols}\nactual:   {list(panel.columns)}"
        )
    if len(panel) != 227:
        raise RuntimeError(f"expected 227 panel rows, got {len(panel)}")
    if panel["month"].iloc[0] != PANEL_START or panel["month"].iloc[-1] != PANEL_END:
        raise RuntimeError("panel does not span 2008-01 … 2026-11")

    if list(gdp.columns) != ["quarter", "gdp_abs_cum_bn", "gdp_yoy", "window"]:
        raise RuntimeError(f"unexpected gdp_quarterly.csv header: {list(gdp.columns)}")
    if len(gdp) != 83:
        raise RuntimeError(f"expected 83 GDP rows, got {len(gdp)}")
    if gdp["quarter"].iloc[0] != "2006Q1" or gdp["quarter"].iloc[-1] != "2026Q3":
        raise RuntimeError("GDP file does not span 2006Q1 … 2026Q3")

    return panel, gdp, metadata


def indicator_columns(panel: pd.DataFrame) -> list[str]:
    return [c for c in panel.columns if c != "month"]


def lags_from_metadata(metadata: dict[str, Any], indicators: list[str]) -> dict[str, int]:
    raw = {str(e["name"]): int(e["publication_lag_months"]) for e in metadata["indicators"]}
    missing = [c for c in indicators if c not in raw]
    if missing:
        raise RuntimeError(f"metadata has no publication lag for: {missing}")
    lags = {c: raw[c] for c in indicators}
    bad = {k: v for k, v in lags.items() if v < 0}
    if bad:
        raise RuntimeError(f"negative publication lags: {bad}")
    return lags


def build_feature_panel(panel: pd.DataFrame, lags: dict[str, int]) -> pd.DataFrame:
    """Publication-lag feature panel.

    Row ``t`` contains, for indicator ``i``, the most recent observation
    eligible at the end of month ``t``: ``x[i, t - L_i]``.  Equivalently the
    reference-month panel shifted down by each indicator's own lag.  Cells
    beyond the sample or genuinely unreleased remain NaN.
    """
    indicators = indicator_columns(panel)
    out = pd.DataFrame(index=panel.index)
    out["month"] = panel["month"].values
    for c in indicators:
        out[c] = pd.to_numeric(panel[c], errors="coerce").shift(lags[c]).astype(float)
    return out


def panel_without_partial_month(panel: pd.DataFrame) -> pd.DataFrame:
    if EXCLUDED_MONTH not in set(panel["month"]):
        raise RuntimeError("expected 2026-11 row in the frozen panel")
    return panel.loc[panel["month"] != EXCLUDED_MONTH].reset_index(drop=True)


# ---------------------------------------------------------------------------
# PMI contraction labels
# ---------------------------------------------------------------------------

def build_labels(panel: pd.DataFrame, threshold: float) -> pd.DataFrame:
    """d_t = 1{PMI_t < threshold and PMI_{t-1} < threshold}."""
    out = pd.DataFrame({"month": panel["month"].values})
    pmi = pd.to_numeric(panel["pmi_man"], errors="coerce").to_numpy()
    prev = np.r_[np.nan, pmi[:-1]]
    d = ((pmi < threshold) & (prev < threshold)).astype(float)
    d[~np.isfinite(pmi) | ~np.isfinite(prev)] = np.nan
    out["pmi_man"] = pmi
    out["pmi_man_lag1"] = prev
    out["d"] = d
    return out


def label_counts(labels: pd.DataFrame) -> dict[str, Any]:
    def block(start: str, end: str) -> dict[str, Any]:
        s = labels[(labels["month"] >= start) & (labels["month"] <= end)]
        valid = s["d"].notna()
        return {
            "start": start,
            "end": end,
            "n_months": int(len(s)),
            "n_valid_labels": int(valid.sum()),
            "n_contraction": int(np.nansum(s["d"].to_numpy())),
            "n_expansion": int(valid.sum() - np.nansum(s["d"].to_numpy())),
        }

    return {
        "total": block(REGIME_INIT_START, REGIME_OOS_END),
        "initial": block(REGIME_INIT_START, REGIME_INIT_END),
        "out_of_sample": block(REGIME_OOS_START, REGIME_OOS_END),
    }


# ---------------------------------------------------------------------------
# GDP target reconstruction (interface.md §5.2)
# ---------------------------------------------------------------------------

def derive_gdp_target(gdp: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    src = gdp.copy()
    src["year"] = src["quarter"].str[:4].astype(int)
    src["q"] = src["quarter"].str[-1].astype(int)
    src = src.sort_values(["year", "q"]).reset_index(drop=True)

    A: dict[tuple[int, int], float] = {}
    G: dict[tuple[int, int], float] = {}
    for row in src.itertuples(index=False):
        key = (int(row.year), int(row.q))
        A[key] = float(row.gdp_abs_cum_bn)
        G[key] = float(row.gdp_yoy) / 100.0

    C: dict[tuple[int, int], float] = {}
    for q in (1, 2, 3, 4):
        C[(2006, q)] = A[(2006, q)]
    for y in range(2007, 2027):
        for q in (1, 2, 3, 4):
            if (y, q) not in G:
                continue
            C[(y, q)] = C[(y - 1, q)] * (1.0 + G[(y, q)])

    V: dict[tuple[int, int], float] = {}
    for y in range(2006, 2027):
        for q in (1, 2, 3, 4):
            if (y, q) not in C:
                continue
            V[(y, q)] = C[(y, 1)] if q == 1 else C[(y, q)] - C[(y, q - 1)]

    rows = []
    for row in src.itertuples(index=False):
        y, q = int(row.year), int(row.q)
        c = C.get((y, q), np.nan)
        v = V.get((y, q), np.nan)
        derived: float
        source: str
        if (y, q) == (2006, 1):
            derived = float(row.gdp_yoy)
            source = "published_single_quarter"
        elif (y, q) in V and (y - 1, q) in V and V[(y - 1, q)] != 0:
            derived = (V[(y, q)] / V[(y - 1, q)] - 1.0) * 100.0
            source = "derived_chain_linked"
        else:
            derived = np.nan
            source = "unavailable_no_prior_year_volume"
        rows.append({
            "quarter": row.quarter,
            "year": y,
            "q": q,
            "gdp_abs_cum_bn": float(row.gdp_abs_cum_bn),
            "gdp_yoy": float(row.gdp_yoy),
            "window": row.window,
            "cumulative_volume_C": c,
            "single_quarter_volume_V": v,
            "gdp_yoy_single_derived": derived,
            "target_source": source,
        })

    target = pd.DataFrame(rows)
    target["is_positive_volume"] = target["single_quarter_volume_V"] > 0

    # ---- validation checks -------------------------------------------------
    q1 = target[(target["window"] == "single_quarter") &
                (target["target_source"] == "derived_chain_linked")]
    q1_diff = float(np.max(np.abs(q1["gdp_yoy_single_derived"] - q1["gdp_yoy"]))) if len(q1) else np.nan

    cum_diffs = []
    for row in target.itertuples(index=False):
        if int(row.q) < 2:
            continue
        y, q = int(row.year), int(row.q)
        if (y, q) not in G:
            continue
        needed = [(y, k) for k in range(1, q + 1)] + [(y - 1, k) for k in range(1, q + 1)]
        if any(k not in V for k in needed):
            continue
        num = sum(V[(y, k)] for k in range(1, q + 1))
        den = sum(V[(y - 1, k)] for k in range(1, q + 1))
        cum = (num / den - 1.0) * 100.0
        cum_diffs.append(abs(cum - float(row.gdp_yoy)))
    cum_max = float(max(cum_diffs)) if cum_diffs else np.nan

    derived_rows = target[target["target_source"] == "derived_chain_linked"]
    n_derived = int(len(derived_rows))
    all_positive = bool(target.loc[target["single_quarter_volume_V"].notna(), "is_positive_volume"].all())

    checks = {
        "base_year": 2006,
        "source_file": "data/clean/gdp_quarterly.csv",
        "source_columns": ["quarter", "gdp_abs_cum_bn", "gdp_yoy", "window"],
        "unit_note": "gdp_abs_cum_bn is RMB 100 million (亿元); target is in percent",
        "formula": {
            "C_2006": "C(2006,q) = A(2006,q)",
            "C_chain": "C(y,q) = C(y-1,q) * (1 + g(y,q))",
            "V_deaccumulate": "V(y,1)=C(y,1); V(y,q)=C(y,q)-C(y,q-1), q=2,3,4",
            "target": "100 * (V(y,q)/V(y-1,q) - 1)",
        },
        "n_source_rows": int(len(target)),
        "n_derived_targets": n_derived,
        "derived_first_quarter": str(derived_rows["quarter"].iloc[0]) if n_derived else None,
        "derived_last_quarter": str(derived_rows["quarter"].iloc[-1]) if n_derived else None,
        "all_reconstructed_volumes_positive": all_positive,
        "q1_max_abs_difference_pp": q1_diff,
        "cumulative_recovery_max_abs_difference_pp": cum_max,
        "q1_validation_n": int(len(q1)),
        "cumulative_validation_n": int(len(cum_diffs)),
        "q1_check_passed": bool(np.isfinite(q1_diff) and q1_diff < 1e-8),
        "cumulative_check_passed": bool(np.isfinite(cum_max) and cum_max < 1e-8),
        "note": (
            "Derived chain-linked single-quarter volume estimate, not an "
            "official separately published NBS single-quarter series."
        ),
    }
    return target, checks


# ---------------------------------------------------------------------------
# Factor-model utilities
# ---------------------------------------------------------------------------

def screen_indicators(
    features: pd.DataFrame,
    start_idx: int,
    end_idx: int,
    indicators: list[str],
    recent_months: int = SCREEN_RECENT_MONTHS,
    min_recent_coverage: float = SCREEN_MIN_RECENT_COVERAGE,
    min_observations: int = SCREEN_MIN_OBSERVATIONS,
) -> tuple[list[str], pd.DataFrame]:
    sub = features.iloc[start_idx:end_idx + 1]
    recent = sub.iloc[-recent_months:]
    rows = []
    for c in indicators:
        obs = sub[c].notna()
        n_obs = int(obs.sum())
        rec_cov = float(recent[c].notna().mean()) if len(recent) else 0.0
        sd = float(sub[c].std(ddof=1)) if n_obs > 1 else 0.0
        keep = (
            n_obs >= min_observations
            and rec_cov >= min_recent_coverage
            and np.isfinite(sd)
            and sd > 0
        )
        rows.append({
            "indicator": c,
            "n_obs": n_obs,
            "recent_coverage": rec_cov,
            "std": sd,
            "retained": bool(keep),
        })
    diag = pd.DataFrame(rows)
    return list(diag.loc[diag["retained"], "indicator"]), diag


def fit_factor_pipeline(
    features: pd.DataFrame,
    retained: list[str],
    start_idx: int,
    end_idx: int,
    max_components: int = MAX_FACTORS,
) -> dict[str, Any]:
    """Training-window-only standardization + PCA."""
    X = features.iloc[start_idx:end_idx + 1][retained].to_numpy(dtype=float)
    n, d = X.shape
    mu = np.nanmean(X, axis=0)
    sd = np.nanstd(X, axis=0, ddof=1)
    sd[~np.isfinite(sd) | (sd <= 0)] = 1.0
    Z = (X - mu) / sd
    Z[~np.isfinite(Z)] = 0.0

    max_comp = max(1, min(max_components, d - 1, n - 2))
    # Imported lazily so the data module stays light.
    from sklearn.decomposition import PCA

    pca = PCA(n_components=max_comp, svd_solver="full")
    pca.fit(Z)

    signs = np.ones(max_comp)
    comp = pca.components_.copy()
    for k in range(max_comp):
        j = int(np.argmax(np.abs(comp[k])))
        if comp[k, j] < 0:
            signs[k] = -1.0

    return {
        "retained": list(retained),
        "mu": mu,
        "sd": sd,
        "signs": signs,
        "pca": pca,
        "n_train": int(n),
        "d": int(d),
        "start_idx": int(start_idx),
        "end_idx": int(end_idx),
        "max_components": int(max_comp),
    }


def factor_scores(pipeline: dict[str, Any], features: pd.DataFrame, row_indices: np.ndarray, r: int) -> np.ndarray:
    """Transform feature rows with the fitted pipeline (no refitting)."""
    retained = pipeline["retained"]
    X = features.iloc[np.asarray(row_indices, dtype=int)][retained].to_numpy(dtype=float)
    Z = (X - pipeline["mu"]) / pipeline["sd"]
    Z[~np.isfinite(Z)] = 0.0
    F = pipeline["pca"].transform(Z)[:, :r]
    return F * pipeline["signs"][:r]


def factor_variance_ratio(pipeline: dict[str, Any], r: int) -> float:
    return float(np.sum(pipeline["pca"].explained_variance_ratio_[:r]))


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------

def regime_schedule(panel: pd.DataFrame) -> pd.DataFrame:
    months = panel["month"].tolist()
    idx = month_index(months)
    targets = [
        m for m in months
        if REGIME_OOS_START <= m <= REGIME_OOS_END
    ]
    rows = []
    for target in targets:
        origin = prev_month(target)
        if origin not in idx:
            raise RuntimeError(f"origin {origin} for target {target} not in panel")
        rows.append({
            "target_period": target,
            "fold": target,
            "origin_month": origin,
            "forecast_origin": month_end(origin),
            "origin_idx": idx[origin],
            "target_idx": idx[target],
        })
    out = pd.DataFrame(rows)
    if len(out) != 82:
        raise RuntimeError(f"regime schedule must have 82 targets, got {len(out)}")
    if out["target_period"].iloc[0] != REGIME_OOS_START or out["target_period"].iloc[-1] != REGIME_OOS_END:
        raise RuntimeError("regime schedule endpoints are wrong")
    return out


def gdp_schedule(panel: pd.DataFrame, target: pd.DataFrame) -> pd.DataFrame:
    months = panel["month"].tolist()
    idx = month_index(months)
    available = set(target.loc[target["gdp_yoy_single_derived"].notna(), "quarter"])
    rows = []
    for q in quarter_list(GDP_OOS_START, GDP_OOS_END):
        if q not in available:
            raise RuntimeError(f"target quarter {q} has no derived GDP target")
        for vintage, month in zip(VINTAGES, quarter_months(q)):
            if month not in idx:
                raise RuntimeError(f"vintage month {month} not in panel")
            rows.append({
                "target_period": q,
                "fold": q,
                "vintage": vintage,
                "origin_month": month,
                "forecast_origin": month_end(month),
                "origin_idx": idx[month],
            })
    out = pd.DataFrame(rows)
    if len(out) != 105:
        raise RuntimeError(f"GDP schedule must have 105 records, got {len(out)}")
    return out


def gdp_training_quarters(target: pd.DataFrame, origin_idx: int, panel: pd.DataFrame) -> list[str]:
    """Quarters whose realized target is observable at the given origin index."""
    months = panel["month"].tolist()
    idx = month_index(months)
    out = []
    for row in target.itertuples(index=False):
        if not np.isfinite(row.gdp_yoy_single_derived):
            continue
        end_month = quarter_end_month(row.quarter)
        if end_month not in idx:
            continue
        pub_idx = idx[end_month] + GDP_PUBLICATION_LAG_MONTHS
        if pub_idx <= origin_idx:
            out.append(row.quarter)
    return out


# ---------------------------------------------------------------------------
# Main verification pipeline
# ---------------------------------------------------------------------------

def run(seed: int, write: bool = True, verbose: bool = True) -> dict[str, Any]:
    t0 = time.perf_counter()
    np.random.seed(seed)

    if verbose:
        CONSOLE.print(Panel.fit(
            "[bold]Option A - data, information sets, and target construction[/bold]\n"
            f"seed={seed}  |  frozen inputs only",
            border_style="cyan",
        ))

    panel, gdp, metadata = load_frozen_inputs(verbose=verbose)
    indicators = indicator_columns(panel)
    lags = lags_from_metadata(metadata, indicators)
    model_panel = panel_without_partial_month(panel)
    features = build_feature_panel(model_panel, lags)

    label50 = build_labels(model_panel, 50.0)
    label495 = build_labels(model_panel, 49.5)
    counts50 = label_counts(label50)
    counts495 = label_counts(label495)

    if counts50["initial"]["n_valid_labels"] != 143 or counts50["initial"]["n_contraction"] != 20:
        raise RuntimeError(f"unexpected 50-threshold initial label counts: {counts50['initial']}")
    if counts50["out_of_sample"]["n_valid_labels"] != 82 or counts50["out_of_sample"]["n_contraction"] != 27:
        raise RuntimeError(f"unexpected 50-threshold OOS label counts: {counts50['out_of_sample']}")
    if counts495["initial"]["n_contraction"] != 7 or counts495["out_of_sample"]["n_contraction"] != 13:
        raise RuntimeError(f"unexpected 49.5-threshold label counts: {counts495}")

    target, target_checks = derive_gdp_target(gdp)
    if not target_checks["q1_check_passed"]:
        raise RuntimeError(f"Q1 reconstruction check failed: {target_checks}")
    if not target_checks["cumulative_check_passed"]:
        raise RuntimeError(f"cumulative reconstruction check failed: {target_checks}")
    if not target_checks["all_reconstructed_volumes_positive"]:
        raise RuntimeError("non-positive reconstructed quarterly volume")

    rs = regime_schedule(model_panel)
    gs = gdp_schedule(model_panel, target)

    # Leakage / availability assertions -------------------------------------
    idx = month_index(model_panel["month"].tolist())
    availability_rows = []
    for c in indicators:
        lag = lags[c]
        for origin in (REGIME_ORIGIN_START, REGIME_ORIGIN_END, "2018-03", "2026-03"):
            if origin not in idx:
                continue
            t = idx[origin]
            latest_reference = add_months(origin, -lag)
            availability_rows.append({
                "indicator": c,
                "lag": lag,
                "origin_month": origin,
                "forecast_origin": month_end(origin),
                "latest_eligible_reference_month": latest_reference,
                "eligible": (t - lag) >= 0,
            })
    avail = pd.DataFrame(availability_rows)

    # For every scheduled origin, the feature row must only depend on s + L <= t.
    for row in rs.itertuples(index=False):
        t = int(row.origin_idx)
        for c in indicators:
            lag = lags[c]
            ref_idx = t - lag
            if ref_idx >= 0:
                expected = model_panel.iloc[ref_idx][c]
                actual = features.iloc[t][c]
                if not (
                    (pd.isna(expected) and pd.isna(actual))
                    or (not pd.isna(expected) and not pd.isna(actual) and float(expected) == float(actual))
                ):
                    raise RuntimeError(f"feature-row mismatch at {row.origin_month} for {c}")
            elif not pd.isna(features.iloc[t][c]):
                raise RuntimeError(f"feature described as available before sample start: {c} at {row.origin_month}")

    # 2026-11 must be excluded everywhere.
    if EXCLUDED_MONTH in set(model_panel["month"]):
        raise RuntimeError("2026-11 was not excluded")

    # Target self-exclusion for every GDP record.
    for row in gs.itertuples(index=False):
        tq = row.target_period
        train = gdp_training_quarters(target, int(row.origin_idx), model_panel)
        if tq in train:
            raise RuntimeError(f"target {tq} leaked into its own training set at {row.forecast_origin}")

    # Write artifacts --------------------------------------------------------
    elapsed = time.perf_counter() - t0
    if write:
        DATA_OUT_DIR.mkdir(parents=True, exist_ok=True)
        target.to_csv(DATA_OUT_DIR / "gdp_target_derived.csv", index=False)
        write_json(DATA_OUT_DIR / "target_checks.json", target_checks)
        write_json(DATA_OUT_DIR / "label_counts.json", {
            "primary_threshold_50": counts50,
            "sensitivity_threshold_49_5": counts495,
        })
        write_json(DATA_OUT_DIR / "availability_checks.json", {
            "rule": "F_t = { x_{i,s} : s + L_i <= t }",
            "excluded_month": EXCLUDED_MONTH,
            "n_indicators": len(indicators),
            "lags": lags,
            "regime_schedule": {
                "n_records": int(len(rs)),
                "first_origin": rs["forecast_origin"].iloc[0],
                "last_origin": rs["forecast_origin"].iloc[-1],
                "first_target": rs["target_period"].iloc[0],
                "last_target": rs["target_period"].iloc[-1],
            },
            "gdp_schedule": {
                "n_records": int(len(gs)),
                "n_quarters": int(gs["target_period"].nunique()),
                "vintages": list(VINTAGES),
                "first_origin": gs["forecast_origin"].iloc[0],
                "last_origin": gs["forecast_origin"].iloc[-1],
            },
            "feature_panel": "row t stores x[i, t-L_i]; NaN where unreleased",
            "checks": {
                "feature_rows_match_panel_shift": True,
                "gdp_target_never_in_own_training": True,
                "partial_month_excluded": True,
            },
            "availability_sample": avail.to_dict(orient="records"),
        })
        write_json(DATA_OUT_DIR / "run_summary.json", {
            "seed": seed,
            "fit_seconds": elapsed,
            "panel_shape": list(panel.shape),
            "n_indicators": len(indicators),
            "target_quarters_available": int(target["gdp_yoy_single_derived"].notna().sum()),
        })

    # Terminal report --------------------------------------------------------
    if verbose:
        tbl = Table(title="Frozen inputs and derived schedules", show_lines=False)
        tbl.add_column("Item", style="bold")
        tbl.add_column("Value", justify="right")
        tbl.add_row("Panel", f"{len(panel)} rows x {len(indicators)} indicators")
        tbl.add_row("Panel span", f"{panel['month'].iloc[0]} ... {panel['month'].iloc[-1]}")
        tbl.add_row("Derived GDP targets", f"{target_checks['n_derived_targets']} ({target_checks['derived_first_quarter']} ... {target_checks['derived_last_quarter']})")
        tbl.add_row("Q1 max abs diff (pp)", f"{target_checks['q1_max_abs_difference_pp']:.3e}")
        tbl.add_row("Cumulative max abs diff (pp)", f"{target_checks['cumulative_recovery_max_abs_difference_pp']:.3e}")
        tbl.add_row("Regime records", f"{len(rs)} (targets {rs['target_period'].iloc[0]} ... {rs['target_period'].iloc[-1]})")
        tbl.add_row("GDP forecast records", f"{len(gs)} (35 quarters x 3 vintages)")
        tbl.add_row("Initial labels @50", f"{counts50['initial']['n_valid_labels']} ({counts50['initial']['n_contraction']} contraction)")
        tbl.add_row("OOS labels @50", f"{counts50['out_of_sample']['n_valid_labels']} ({counts50['out_of_sample']['n_contraction']} contraction)")
        tbl.add_row("OOS labels @49.5", f"{counts495['out_of_sample']['n_valid_labels']} ({counts495['out_of_sample']['n_contraction']} contraction)")
        tbl.add_row("Elapsed", f"{elapsed:.2f}s")
        CONSOLE.print(tbl)
        CONSOLE.print("[green]All data / target / leakage checks passed.[/green]")

    return {
        "panel": panel,
        "gdp": gdp,
        "metadata": metadata,
        "indicators": indicators,
        "lags": lags,
        "model_panel": model_panel,
        "features": features,
        "labels50": label50,
        "labels495": label495,
        "target": target,
        "target_checks": target_checks,
        "regime_schedule": rs,
        "gdp_schedule": gs,
        "counts50": counts50,
        "counts495": counts495,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Option A data preparation and validation")
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run(seed=args.seed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

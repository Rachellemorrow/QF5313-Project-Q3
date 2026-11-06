"""Option A - PCA factor model + bridge GDP nowcasting with M1/M2/M3 vintages.

    uv run python code/approach1_gdp_nowcast.py --seed 42

Pipeline: load frozen inputs -> verify -> expanding walk-forward splits ->
training-window-only standardization/PCA -> inner-CV rank selection ->
bridge regression per within-quarter vintage -> evaluation (RMSE/MAE and
random-walk-with-drift benchmark) -> metrics.json + predictions + figures.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

import approach1_data as d

ROOT = d.ROOT
OUT_DIR = d.RESULTS_DIR / "gdp_nowcasting"
FIG_DIR = OUT_DIR / "figures"

MIN_INNER_TRAINING_QUARTERS = 8
R_CANDIDATES = tuple(range(1, d.MAX_FACTORS + 1))

CONSOLE = Console()


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def ols_fit(X: np.ndarray, y: np.ndarray) -> dict[str, Any]:
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    n, k = X.shape
    rss = float(resid @ resid)
    dof = max(n - k, 1)
    sigma2 = rss / dof
    try:
        cov = sigma2 * np.linalg.pinv(X.T @ X)
        se = np.sqrt(np.maximum(np.diag(cov), 0.0))
    except np.linalg.LinAlgError:
        se = np.full(k, np.nan)
    tstat = beta / np.where(se > 0, se, np.nan)
    return {
        "beta": beta,
        "se": se,
        "t": tstat,
        "rss": rss,
        "n": n,
        "k": k,
        "r2": 1.0 - rss / float(np.sum((y - y.mean()) ** 2)) if np.sum((y - y.mean()) ** 2) > 0 else np.nan,
    }


def rmse(y: np.ndarray, p: np.ndarray) -> float:
    e = np.asarray(y, float) - np.asarray(p, float)
    return float(np.sqrt(np.mean(e ** 2))) if e.size else float("nan")


def mae(y: np.ndarray, p: np.ndarray) -> float:
    e = np.asarray(y, float) - np.asarray(p, float)
    return float(np.mean(np.abs(e))) if e.size else float("nan")


def summary_metrics(y: np.ndarray, p: np.ndarray) -> dict[str, Any]:
    y = np.asarray(y, float)
    p = np.asarray(p, float)
    e = y - p
    sst = float(np.sum((y - y.mean()) ** 2))
    return {
        "n": int(len(y)),
        "rmse": rmse(y, p),
        "mae": mae(y, p),
        "bias": float(np.mean(e)) if len(e) else float("nan"),
        "r2": (1.0 - float(np.sum(e ** 2)) / sst) if sst > 0 else float("nan"),
        "max_abs_error": float(np.max(np.abs(e))) if len(e) else float("nan"),
    }


def write_fig(fig: go.Figure, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(path), include_plotlyjs=True, full_html=True)


# ---------------------------------------------------------------------------
# Pipeline cache
# ---------------------------------------------------------------------------

class FactorCache:
    """Cache training-window-only screen + standardization + PCA by origin."""

    def __init__(self, features: pd.DataFrame, indicators: list[str]):
        self.features = features
        self.indicators = indicators
        self.cache: dict[int, dict[str, Any]] = {}
        self.n = len(features)

    def get(self, origin_idx: int) -> dict[str, Any]:
        origin_idx = int(origin_idx)
        if origin_idx in self.cache:
            return self.cache[origin_idx]

        retained, diag = d.screen_indicators(
            self.features, 0, origin_idx, self.indicators,
            recent_months=d.SCREEN_RECENT_MONTHS,
            min_recent_coverage=d.SCREEN_MIN_RECENT_COVERAGE,
            min_observations=d.SCREEN_MIN_OBSERVATIONS,
        )
        if len(retained) < 2:
            raise RuntimeError(f"fewer than 2 indicators retained at origin index {origin_idx}")

        pipe = d.fit_factor_pipeline(self.features, retained, 0, origin_idx, d.MAX_FACTORS)
        # Pre-transform every row once with this fold's fitted pipeline.
        rows = np.arange(origin_idx + 1)
        F = d.factor_scores(pipe, self.features, rows, pipe["max_components"])
        entry = {"pipe": pipe, "diag": diag, "F": F, "origin_idx": origin_idx}
        self.cache[origin_idx] = entry
        return entry


# ---------------------------------------------------------------------------
# Quarter helpers
# ---------------------------------------------------------------------------

def build_quarter_maps(target: pd.DataFrame, panel: pd.DataFrame) -> dict[str, Any]:
    idx = d.month_index(panel["month"].tolist())
    ymap: dict[str, float] = {}
    end_idx: dict[str, int] = {}
    pub_idx: dict[str, int] = {}
    for row in target.itertuples(index=False):
        if not np.isfinite(row.gdp_yoy_single_derived):
            continue
        q = row.quarter
        em = d.quarter_end_month(q)
        if em not in idx:
            # Quarterly targets before the monthly panel start have no
            # factor row and cannot be used by this model.
            continue
        ymap[q] = float(row.gdp_yoy_single_derived)
        end_idx[q] = idx[em]
        pub_idx[q] = idx[em] + d.GDP_PUBLICATION_LAG_MONTHS
    return {"y": ymap, "end_idx": end_idx, "pub_idx": pub_idx, "month_idx": idx}


def vintage_origin_idx(q: str, vintage: str, month_idx: dict[str, int]) -> int:
    months = d.quarter_months(q)
    v = d.VINTAGES.index(vintage)
    return month_idx[months[v]]


def observable_quarters(qmap: dict[str, Any], origin_idx: int) -> list[str]:
    out = [q for q, pi in qmap["pub_idx"].items() if pi <= origin_idx]
    return sorted(out, key=d.quarter_parts)


# ---------------------------------------------------------------------------
# Inner CV rank selection
# ---------------------------------------------------------------------------

def select_rank(
    cache: FactorCache,
    qmap: dict[str, Any],
    origin_idx: int,
    train_quarters: list[str],
    max_rank: int,
) -> tuple[int, list[dict[str, Any]]]:
    """Pick r by inner expanding-window CV, pooling M1/M2/M3 nowcast errors.

    Every inner fit uses only data available at the inner origin.  The outer
    origin only defines which historical quarters are usable for validation.
    """
    rss = {r: 0.0 for r in R_CANDIDATES}
    counts = {r: 0 for r in R_CANDIDATES}
    detail: list[dict[str, Any]] = []

    if len(train_quarters) <= MIN_INNER_TRAINING_QUARTERS:
        return 1, detail

    for qv in train_quarters[MIN_INNER_TRAINING_QUARTERS:]:
        yv = qmap["y"][qv]
        for vintage in d.VINTAGES:
            oi = vintage_origin_idx(qv, vintage, qmap["month_idx"])
            inner_train = [q for q in train_quarters if qmap["pub_idx"][q] <= oi and q != qv]
            if len(inner_train) < MIN_INNER_TRAINING_QUARTERS:
                continue
            try:
                entry = cache.get(oi)
            except RuntimeError:
                continue
            pipe = entry["pipe"]
            rows = np.asarray([vintage_origin_idx(q, vintage, qmap["month_idx"]) for q in inner_train], dtype=int)
            F_train = entry["F"][rows]
            F_val = entry["F"][oi]
            y_train = np.asarray([qmap["y"][q] for q in inner_train], dtype=float)

            for r in R_CANDIDATES:
                if r > max_rank or r > F_train.shape[1]:
                    continue
                X = np.column_stack([np.ones(len(inner_train)), F_train[:, :r]])
                fit = ols_fit(X, y_train)
                pred = float(fit["beta"][0] + F_val[:r] @ fit["beta"][1:])
                rss[r] += (yv - pred) ** 2
                counts[r] += 1

    valid = [(r, rss[r] / counts[r]) for r in R_CANDIDATES if counts[r] > 0 and r <= max_rank]
    if not valid:
        return 1, detail
    best_r = min(valid, key=lambda t: (t[1], t[0]))[0]
    for r, mse in valid:
        detail.append({"r": int(r), "inner_mse": float(mse), "n_inner_records": int(counts[r])})
    return int(best_r), detail


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(seed: int) -> dict[str, Any]:
    t0 = time.perf_counter()
    np.random.seed(seed)

    CONSOLE.print(Panel.fit(
        "[bold]Option A - PCA bridge GDP nowcasting[/bold]\n"
        f"seed={seed} | M1/M2/M3 vintages | 2018Q1-2026Q3",
        border_style="cyan",
    ))

    bundle = d.run(seed=seed, write=False, verbose=False)
    features: pd.DataFrame = bundle["features"]
    panel: pd.DataFrame = bundle["model_panel"]
    target: pd.DataFrame = bundle["target"]
    indicators: list[str] = bundle["indicators"]
    gs: pd.DataFrame = bundle["gdp_schedule"]

    qmap = build_quarter_maps(target, panel)
    cache = FactorCache(features, indicators)
    n_rows = len(features)

    # ---- walk-forward forecasts -------------------------------------------
    records: list[dict[str, Any]] = []
    coef_rows: list[dict[str, Any]] = []
    r_detail: list[dict[str, Any]] = []

    all_quarters = sorted(qmap["y"].keys(), key=d.quarter_parts)

    for row in gs.itertuples(index=False):
        q = row.target_period
        vintage = row.vintage
        oi = int(row.origin_idx)
        y_true = float(qmap["y"][q])

        train_q = observable_quarters(qmap, oi)
        if q in train_q:
            raise RuntimeError(f"target {q} leaked into its own training set at {row.forecast_origin}")
        if len(train_q) < MIN_INNER_TRAINING_QUARTERS + 2:
            raise RuntimeError(f"too few training quarters at {row.forecast_origin}: {len(train_q)}")

        entry = cache.get(oi)
        pipe = entry["pipe"]
        max_rank = min(d.MAX_FACTORS, pipe["max_components"])

        best_r, detail = select_rank(cache, qmap, oi, train_q, max_rank)
        for rec in detail:
            r_detail.append({
                "forecast_origin": row.forecast_origin,
                "target_period": q,
                "vintage": vintage,
                **rec,
            })

        rows = np.asarray([vintage_origin_idx(tq, vintage, qmap["month_idx"]) for tq in train_q], dtype=int)
        F_train = entry["F"][rows][:, :best_r]
        y_train = np.asarray([qmap["y"][tq] for tq in train_q], dtype=float)
        X = np.column_stack([np.ones(len(train_q)), F_train])
        fit = ols_fit(X, y_train)

        f_test = entry["F"][oi][:best_r]
        y_hat = float(fit["beta"][0] + f_test @ fit["beta"][1:])

        # Benchmarks on the same training information set.
        y_prev = float(qmap["y"][train_q[-1]])
        if len(train_q) >= 2:
            diffs = np.diff([qmap["y"][tq] for tq in train_q])
            drift = float(np.mean(diffs))
        else:
            drift = 0.0
        rw_hat = y_prev + drift
        mean_hat = float(np.mean(y_train))

        for name, pred in (("pca_bridge", y_hat), ("rw_drift", rw_hat), ("historical_mean", mean_hat)):
            records.append({
                "model": name,
                "target": "gdp_yoy_single_derived",
                "forecast_origin": row.forecast_origin,
                "target_period": q,
                "y_true": y_true,
                "y_pred": pred,
                "vintage": vintage,
                "fold": q,
                "seed": seed,
                "train_start": "2008Q1",
                "train_end": train_q[-1],
                "n_train_quarters": len(train_q),
                "r": best_r,
                "leakage_discipline": "safe",
            })

        terms = ["intercept"] + [f"f{k}" for k in range(1, best_r + 1)]
        for k, term in enumerate(terms):
            coef_rows.append({
                "forecast_origin": row.forecast_origin,
                "target_period": q,
                "vintage": vintage,
                "term": term,
                "coef": float(fit["beta"][k]),
                "se": float(fit["se"][k]) if k < len(fit["se"]) else np.nan,
                "t_stat": float(fit["t"][k]) if k < len(fit["t"]) else np.nan,
                "n_train": int(fit["n"]),
                "r": int(best_r),
            })

    preds = pd.DataFrame(records)
    preds["error"] = preds["y_true"] - preds["y_pred"]
    preds["abs_error"] = preds["error"].abs()
    preds["squared_error"] = preds["error"] ** 2
    if len(preds) != 105 * 3:
        raise RuntimeError(f"expected 315 prediction rows, got {len(preds)}")
    if preds.duplicated(["model", "target_period", "vintage", "fold"]).any():
        raise RuntimeError("duplicate GDP prediction rows")

    # ---- metrics -----------------------------------------------------------
    model = preds[preds["model"] == "pca_bridge"].copy()
    rw = preds[preds["model"] == "rw_drift"].copy()
    hm = preds[preds["model"] == "historical_mean"].copy()

    overall = summary_metrics(model["y_true"], model["y_pred"])
    by_vintage: dict[str, Any] = {}
    bench_by_vintage: dict[str, Any] = {}
    for v in d.VINTAGES:
        mm = model[model["vintage"] == v]
        rr = rw[rw["vintage"] == v]
        by_vintage[v] = summary_metrics(mm["y_true"], mm["y_pred"])
        bench_by_vintage[v] = summary_metrics(rr["y_true"], rr["y_pred"])

    benchmark = {
        "rw_drift": summary_metrics(rw["y_true"], rw["y_pred"]),
        "historical_mean": summary_metrics(hm["y_true"], hm["y_pred"]),
        "rw_drift_by_vintage": bench_by_vintage,
    }
    skill = {
        "brier_like_mse_skill_vs_rw_drift": 1.0 - overall["rmse"] ** 2 / benchmark["rw_drift"]["rmse"] ** 2,
        "rmse_ratio_vs_rw_drift": overall["rmse"] / benchmark["rw_drift"]["rmse"],
    }

    wide = model.pivot(index="target_period", columns="vintage", values="y_pred")
    wide["y_true"] = model.groupby("target_period")["y_true"].first()
    rev12 = (wide["M2"] - wide["M1"]).abs()
    rev23 = (wide["M3"] - wide["M2"]).abs()
    err1 = (wide["M1"] - wide["y_true"]).abs()
    err3 = (wide["M3"] - wide["y_true"]).abs()
    revision = {
        "mean_abs_revision_M1_to_M2": float(rev12.mean()),
        "mean_abs_revision_M2_to_M3": float(rev23.mean()),
        "mean_abs_revision_M1_to_M3": float((wide["M3"] - wide["M1"]).abs().mean()),
        "rmse_M1": by_vintage["M1"]["rmse"],
        "rmse_M2": by_vintage["M2"]["rmse"],
        "rmse_M3": by_vintage["M3"]["rmse"],
        "rmse_reduction_M1_to_M3": float(by_vintage["M1"]["rmse"] - by_vintage["M3"]["rmse"]),
        "share_quarters_improved_M1_to_M3": float((err3 < err1).mean()),
        "n_quarters": int(len(wide)),
    }

    # ---- history / provenance ---------------------------------------------
    r_path = (
        model[["target_period", "vintage", "forecast_origin", "r", "n_train_quarters"]]
        .drop_duplicates(["target_period", "vintage"])
        .sort_values(["target_period", "vintage"])
        .to_dict(orient="records")
    )

    rep_origins = []
    for q, v in (("2018Q1", "M1"), ("2022Q1", "M1"), ("2026Q3", "M3")):
        row = model[(model["target_period"] == q) & (model["vintage"] == v)]
        if len(row):
            rep_origins.append(int(d.month_index(panel["month"].tolist())[row.iloc[0]["forecast_origin"][:7]]))
    rep_origins = sorted(set(rep_origins))

    loadings_rows = []
    scree = []
    for oi in rep_origins:
        entry = cache.get(oi)
        pipe = entry["pipe"]
        retained = pipe["retained"]
        comp = pipe["pca"].components_ * pipe["signs"][:, None]
        ratio = pipe["pca"].explained_variance_ratio_
        origin_month = panel.iloc[oi]["month"]
        for r_i in range(comp.shape[0]):
            scree.append({
                "origin_month": origin_month,
                "component": r_i + 1,
                "explained_variance_ratio": float(ratio[r_i]),
                "cumulative": float(np.sum(ratio[:r_i + 1])),
            })
        for j, name in enumerate(retained):
            for r_i in range(min(3, comp.shape[0])):
                loadings_rows.append({
                    "origin_month": origin_month,
                    "indicator": name,
                    "component": r_i + 1,
                    "loading": float(comp[r_i, j]),
                })

    # ---- write outputs -----------------------------------------------------
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    preds.to_csv(OUT_DIR / "predictions.csv", index=False)
    pd.DataFrame(coef_rows).to_csv(OUT_DIR / "coefficients.csv", index=False)
    wide.reset_index().to_csv(OUT_DIR / "revision_path.csv", index=False)

    # preprocessing / screening provenance
    prep_rows = []
    seen_oi = set()
    for row in model.itertuples(index=False):
        oi = int(d.month_index(panel["month"].tolist())[row.forecast_origin[:7]])
        if oi in seen_oi:
            continue
        seen_oi.add(oi)
        entry = cache.get(oi)
        pipe = entry["pipe"]
        for j, name in enumerate(pipe["retained"]):
            prep_rows.append({
                "forecast_origin": row.forecast_origin,
                "origin_month": panel.iloc[oi]["month"],
                "indicator": name,
                "retained": True,
                "mu": float(pipe["mu"][j]),
                "sd": float(pipe["sd"][j]),
                "n_train_months": int(pipe["n_train"]),
                "r_selected": int(row.r),
                "variance_explained_selected": d.factor_variance_ratio(pipe, int(row.r)),
            })
    pd.DataFrame(prep_rows).to_csv(OUT_DIR / "preprocessing.csv", index=False)

    fit_seconds = time.perf_counter() - t0
    metrics_payload = {
        "task": "gdp_nowcasting",
        "target": "gdp_yoy_single_derived",
        "approach": "A",
        "leakage_discipline": "safe",
        "seed": seed,
        "params": {
            "model": "PCA factors + OLS bridge, separate model per within-quarter vintage",
            "target_definition": "derived chain-linked single-quarter real GDP YoY, percent",
            "target_source": "data/clean/gdp_quarterly.csv (frozen)",
            "train_quarters_initial": [d.GDP_INIT_START, d.GDP_INIT_END],
            "oos_quarters": [d.GDP_OOS_START, d.GDP_OOS_END],
            "vintages": list(d.VINTAGES),
            "factor_screen": {
                "recent_months": d.SCREEN_RECENT_MONTHS,
                "min_recent_coverage": d.SCREEN_MIN_RECENT_COVERAGE,
                "min_observations": d.SCREEN_MIN_OBSERVATIONS,
                "missing_fill": "training-window mean (0 in standardized space)",
            },
            "standardization": "training-window mean/std only",
            "pca": {
                "max_components": d.MAX_FACTORS,
                "rank_candidates": list(R_CANDIDATES),
                "selection": "inner expanding-window CV, pooled M1/M2/M3 RMSE, smallest r on ties",
                "sign_rule": "largest absolute loading positive",
            },
            "bridge": {"intercept": True, "separate_per_vintage": True},
            "benchmark": "random walk with training-window drift; historical mean secondary",
            "publication_lag_months_gdp": d.GDP_PUBLICATION_LAG_MONTHS,
        },
        "metrics": {
            "overall": overall,
            "by_vintage": by_vintage,
            "benchmark": benchmark,
            "skill": skill,
            "revision": revision,
        },
        "windows": model.drop(columns=["model", "target", "leakage_discipline"]).to_dict(orient="records"),
        "rank_path": r_path,
        "rank_cv_detail": r_detail,
        "history": {
            "r_by_quarter_vintage": r_path,
            "scree": scree,
            "factor_loadings_sample": loadings_rows,
        },
        "target_checks": bundle["target_checks"],
        "fit_seconds": fit_seconds,
        "environment": {
            "python": sys.version.split()[0],
            "numpy": np.__version__,
            "pandas": pd.__version__,
        },
        "outputs": {
            "predictions": "results/approach1/gdp_nowcasting/predictions.csv",
            "coefficients": "results/approach1/gdp_nowcasting/coefficients.csv",
            "revision_path": "results/approach1/gdp_nowcasting/revision_path.csv",
            "preprocessing": "results/approach1/gdp_nowcasting/preprocessing.csv",
        },
    }
    d.write_json(OUT_DIR / "metrics.json", metrics_payload)

    # ---- figures -----------------------------------------------------------
    make_target_figure(target)
    make_nowcast_figure(model)
    make_vintage_metrics_figure(by_vintage, bench_by_vintage)
    make_scree_figure(scree)
    make_loadings_figure(loadings_rows)
    make_revision_figure(wide)

    # ---- terminal report ---------------------------------------------------
    tbl = Table(title="GDP nowcasting - walk-forward metrics", show_lines=False)
    tbl.add_column("Vintage", style="bold")
    tbl.add_column("n", justify="right")
    tbl.add_column("RMSE", justify="right")
    tbl.add_column("MAE", justify="right")
    tbl.add_column("Bias", justify="right")
    tbl.add_column("RW-drift RMSE", justify="right")
    for v in d.VINTAGES:
        tbl.add_row(
            v,
            str(by_vintage[v]["n"]),
            f"{by_vintage[v]['rmse']:.4f}",
            f"{by_vintage[v]['mae']:.4f}",
            f"{by_vintage[v]['bias']:+.4f}",
            f"{bench_by_vintage[v]['rmse']:.4f}",
        )
    tbl.add_row(
        "overall",
        str(overall["n"]),
        f"{overall['rmse']:.4f}",
        f"{overall['mae']:.4f}",
        f"{overall['bias']:+.4f}",
        f"{benchmark['rw_drift']['rmse']:.4f}",
    )
    CONSOLE.print(tbl)
    CONSOLE.print(
        f"[green]GDP nowcasting complete[/green] | 105 records | "
        f"RMSE {overall['rmse']:.4f} vs RW-drift {benchmark['rw_drift']['rmse']:.4f} | "
        f"revision M1->M3 {revision['mean_abs_revision_M1_to_M3']:.4f} pp | "
        f"{fit_seconds:.1f}s"
    )
    return metrics_payload


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------

def make_target_figure(target: pd.DataFrame) -> None:
    t = target.dropna(subset=["gdp_yoy_single_derived"])
    q1 = target[(target["window"] == "single_quarter") & target["gdp_yoy"].notna()]
    q4 = target[(target["q"] == 4) & target["gdp_yoy"].notna()]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=t["quarter"], y=t["gdp_yoy_single_derived"], mode="lines+markers",
                             name="Derived single-quarter YoY (target)"))
    fig.add_trace(go.Scatter(x=q1["quarter"], y=q1["gdp_yoy"], mode="markers",
                             name="Published Q1 single-quarter YoY", marker=dict(symbol="diamond", size=7)))
    fig.add_trace(go.Scatter(x=q4["quarter"], y=q4["gdp_yoy"], mode="markers",
                             name="Published Q4 cumulative YoY", marker=dict(symbol="x", size=7)))
    fig.update_layout(
        title="Derived chain-linked single-quarter GDP growth vs published source fields",
        xaxis_title="Quarter", yaxis_title="% YoY",
        template="plotly_white", hovermode="x unified",
    )
    write_fig(fig, FIG_DIR / "fig_gdp_target.html")


def make_nowcast_figure(model: pd.DataFrame) -> None:
    fig = go.Figure()
    for v, color in (("M1", "#636EFA"), ("M2", "#EF553B"), ("M3", "#00CC96")):
        mm = model[model["vintage"] == v].sort_values("target_period")
        fig.add_trace(go.Scatter(x=mm["target_period"], y=mm["y_pred"], mode="lines+markers", name=f"{v} nowcast",
                                 line=dict(color=color)))
    truth = model.groupby("target_period")["y_true"].first().reset_index().sort_values("target_period")
    fig.add_trace(go.Scatter(x=truth["target_period"], y=truth["y_true"], mode="lines+markers", name="Realized",
                             line=dict(color="black", width=3)))
    fig.update_layout(
        title="Within-quarter nowcast revision path vs realized derived GDP growth",
        xaxis_title="Target quarter", yaxis_title="% YoY",
        template="plotly_white", hovermode="x unified",
        xaxis=dict(tickangle=45),
    )
    write_fig(fig, FIG_DIR / "fig_nowcast_vs_realized.html")


def make_vintage_metrics_figure(by_vintage: dict[str, Any], bench: dict[str, Any]) -> None:
    vintages = list(d.VINTAGES)
    fig = go.Figure()
    fig.add_trace(go.Bar(x=vintages, y=[by_vintage[v]["rmse"] for v in vintages], name="PCA bridge", marker_color="#636EFA"))
    fig.add_trace(go.Bar(x=vintages, y=[bench[v]["rmse"] for v in vintages], name="RW with drift", marker_color="#EF553B"))
    fig.add_trace(go.Bar(x=vintages, y=[by_vintage[v]["mae"] for v in vintages], name="PCA bridge MAE", marker_color="#AB63FA"))
    fig.update_layout(title="Forecast error by within-quarter vintage", xaxis_title="Vintage",
                      yaxis_title="Percentage points", barmode="group", template="plotly_white")
    write_fig(fig, FIG_DIR / "fig_rmse_by_vintage.html")


def make_scree_figure(scree: list[dict[str, Any]]) -> None:
    if not scree:
        return
    df = pd.DataFrame(scree)
    fig = go.Figure()
    for origin, g in df.groupby("origin_month"):
        fig.add_trace(go.Scatter(x=g["component"], y=g["explained_variance_ratio"], mode="lines+markers",
                                 name=origin))
    fig.update_layout(title="PCA scree at representative forecast origins",
                      xaxis_title="Component", yaxis_title="Explained variance ratio",
                      template="plotly_white")
    write_fig(fig, FIG_DIR / "fig_pca_scree.html")


def make_loadings_figure(loadings: list[dict[str, Any]]) -> None:
    if not loadings:
        return
    df = pd.DataFrame(loadings)
    df = df[df["component"] == 1]
    piv = df.pivot_table(index="indicator", columns="origin_month", values="loading", aggfunc="first")
    fig = go.Figure(data=go.Heatmap(z=piv.values, x=list(piv.columns), y=list(piv.index),
                                    colorscale="RdBu", zmid=0, colorbar=dict(title="loading")))
    fig.update_layout(title="First factor loadings at representative origins",
                      xaxis_title="Forecast origin", yaxis_title="Indicator",
                      template="plotly_white", height=max(500, 18 * len(piv.index)))
    write_fig(fig, FIG_DIR / "fig_factor_loadings.html")


def make_revision_figure(wide: pd.DataFrame) -> None:
    df = wide.reset_index().sort_values("target_period")
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        subplot_titles=("Absolute nowcast revision", "Absolute error by vintage"))
    fig.add_trace(go.Bar(x=df["target_period"], y=(df["M2"] - df["M1"]).abs(), name="|M2-M1|"), row=1, col=1)
    fig.add_trace(go.Bar(x=df["target_period"], y=(df["M3"] - df["M2"]).abs(), name="|M3-M2|"), row=1, col=1)
    for v in ("M1", "M2", "M3"):
        fig.add_trace(go.Scatter(x=df["target_period"], y=(df[v] - df["y_true"]).abs(), mode="lines+markers", name=f"|{v}-actual|"), row=2, col=1)
    fig.update_layout(title="Within-quarter revision path", template="plotly_white", barmode="group",
                      height=700, xaxis2=dict(tickangle=45))
    write_fig(fig, FIG_DIR / "fig_revision_path.html")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Option A PCA bridge GDP nowcasting")
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run(seed=args.seed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

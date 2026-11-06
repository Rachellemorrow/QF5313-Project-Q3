"""Option A - Probit contraction classifier.

    uv run python code/approach1_regime.py --seed 42

Expanding-window walk-forward, shared 82 target months (2020-01 … 2026-10),
month-end origins 2019-12-31 … 2026-09-30.  z_t = PCA factors + LPR 1Y
control.  Reports Brier/AUC/accuracy, prior-frequency and persistence
baselines, coefficient tables with standard errors, and the 49.5-threshold
sensitivity.

The rate/spread block is reserved for the Probit controls and excluded from
the factor block to avoid double counting.
"""

from __future__ import annotations

import argparse
import sys
import time
import warnings
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from scipy.optimize import minimize
from scipy.stats import norm
from sklearn.metrics import roc_auc_score, roc_curve, log_loss

import approach1_data as d
from approach1_gdp_nowcast import (
    build_quarter_maps,
    observable_quarters,
    select_rank,
    vintage_origin_idx,
    MIN_INNER_TRAINING_QUARTERS,
)

ROOT = d.ROOT
OUT_DIR = d.RESULTS_DIR / "regime"
FIG_DIR = OUT_DIR / "figures"

CONTROL = "lpr_1y"
CONSOLE = Console()


# ---------------------------------------------------------------------------
# Factor cache (rate block excluded)
# ---------------------------------------------------------------------------

class RegimeFactorCache:
    def __init__(self, features: pd.DataFrame, indicators: list[str]):
        self.features = features
        self.indicators = indicators
        self.cache: dict[int, dict[str, Any]] = {}

    def get(self, origin_idx: int) -> dict[str, Any]:
        oi = int(origin_idx)
        if oi in self.cache:
            return self.cache[oi]
        retained, diag = d.screen_indicators(
            self.features, 0, oi, self.indicators,
            recent_months=d.SCREEN_RECENT_MONTHS,
            min_recent_coverage=d.SCREEN_MIN_RECENT_COVERAGE,
            min_observations=d.SCREEN_MIN_OBSERVATIONS,
        )
        if len(retained) < 2:
            raise RuntimeError(f"fewer than 2 non-rate indicators at origin index {oi}")
        pipe = d.fit_factor_pipeline(self.features, retained, 0, oi, d.MAX_FACTORS)
        F = d.factor_scores(pipe, self.features, np.arange(oi + 1), pipe["max_components"])
        entry = {"pipe": pipe, "diag": diag, "F": F, "origin_idx": oi}
        self.cache[oi] = entry
        return entry


# ---------------------------------------------------------------------------
# Probit fitting
# ---------------------------------------------------------------------------

def _ridge_nll(beta: np.ndarray, X: np.ndarray, y: np.ndarray, lam: float) -> float:
    eta = X @ beta
    ll = y * norm.logcdf(eta) + (1.0 - y) * norm.logcdf(-eta)
    return float(-np.sum(ll) + lam * np.sum(beta[1:] ** 2))


def ridge_probit(X: np.ndarray, y: np.ndarray, lam: float = 1e-4) -> dict[str, Any]:
    k = X.shape[1]
    res = minimize(
        _ridge_nll, np.zeros(k), args=(X, y, lam),
        method="BFGS", options={"maxiter": 2000, "gtol": 1e-8},
    )
    beta = np.asarray(res.x, float)
    try:
        cov = np.asarray(res.hess_inv, float)
        se = np.sqrt(np.maximum(np.diag(cov), 0.0))
        tstat = beta / np.where(se > 0, se, np.nan)
    except Exception:
        se = np.full(k, np.nan)
        tstat = np.full(k, np.nan)
    return {
        "beta": beta, "se": se, "t": tstat,
        "method": "ridge_probit_fallback", "converged": bool(res.success),
        "loglik": float(-res.fun),
    }


def fit_probit(X: np.ndarray, y: np.ndarray) -> dict[str, Any]:
    """Try statsmodels Newton -> BFGS -> documented ridge fallback."""
    import statsmodels.api as sm
    from statsmodels.tools.sm_exceptions import PerfectSeparationError

    Xc = sm.add_constant(X, has_constant="add")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for method in ("newton", "bfgs"):
            try:
                res = sm.Probit(y, Xc).fit(method=method, maxiter=2000, disp=0)
                return {
                    "beta": np.asarray(res.params, float),
                    "se": np.asarray(res.bse, float),
                    "t": np.asarray(res.tvalues, float),
                    "method": f"statsmodels_probit_{method}",
                    "converged": True,
                    "loglik": float(res.llf),
                }
            except (PerfectSeparationError, np.linalg.LinAlgError, ValueError, RuntimeError):
                continue
            except Exception:
                continue
    return ridge_probit(X, y)


def probit_predict(beta: np.ndarray, X: np.ndarray) -> np.ndarray:
    return norm.cdf(np.column_stack([np.ones(len(X)), X]) @ beta)


# ---------------------------------------------------------------------------
# Model run for one threshold / specification
# ---------------------------------------------------------------------------

def label_map(labels: pd.DataFrame) -> dict[str, float]:
    return {m: float(v) for m, v in zip(labels["month"], labels["d"]) if np.isfinite(v)}


def model_run(
    bundle: dict[str, Any],
    factor_entries: dict[int, dict[str, Any]],
    rank_by_origin: dict[int, int],
    labels: pd.DataFrame,
    use_control: bool,
    start_label_month: str,
) -> dict[str, Any]:
    panel: pd.DataFrame = bundle["model_panel"]
    features: pd.DataFrame = bundle["features"]
    rs: pd.DataFrame = bundle["regime_schedule"]
    month_idx = d.month_index(panel["month"].tolist())
    lmap = label_map(labels)

    # control series (lag 0)
    ctrl_raw = features[CONTROL].to_numpy(float)

    rows: list[dict[str, Any]] = []
    coef_rows: list[dict[str, Any]] = []
    fold_detail: list[dict[str, Any]] = []

    for row in rs.itertuples(index=False):
        t = int(row.origin_idx)
        origin_month = str(row.origin_month)
        target_month = str(row.target_period)
        entry = factor_entries[t]
        pipe = entry["pipe"]
        F_all = entry["F"]
        r = int(rank_by_origin[t])

        # Training label months u <= origin month; predictor origin s = u-1.
        months = panel["month"].tolist()
        train_u_ctrl = [
            m for m in months
            if start_label_month <= m <= origin_month and m in lmap
        ]
        train_u_full = [
            m for m in months
            if d.REGIME_INIT_START <= m <= origin_month and m in lmap
        ]
        if not train_u_full:
            raise RuntimeError(f"no training labels at {row.forecast_origin}")

        s_ctrl = np.asarray([month_idx[m] - 1 for m in train_u_ctrl], dtype=int) if train_u_ctrl else np.array([], dtype=int)
        s_full = np.asarray([month_idx[m] - 1 for m in train_u_full], dtype=int)
        if np.any(s_full < 0):
            raise RuntimeError("training predictor index before sample start")
        if np.any(s_full >= t):
            raise RuntimeError("training predictor origin not before the forecast origin")

        control_used = bool(use_control)
        if control_used:
            raw = ctrl_raw[s_ctrl]
            ok = np.isfinite(raw)
            y_ctrl = np.asarray([lmap[m] for m in train_u_ctrl], dtype=float)
            if ok.sum() >= 24 and int(np.sum(y_ctrl[ok])) >= 3:
                mu_c = float(np.mean(raw[ok]))
                sd_c = float(np.std(raw[ok], ddof=1))
                if not np.isfinite(sd_c) or sd_c <= 0 or not np.isfinite(ctrl_raw[t]):
                    control_used = False
                else:
                    train_u = [m for m, keep in zip(train_u_ctrl, ok) if keep]
                    s_idx = s_ctrl[ok]
                    ctrl_train = (ctrl_raw[s_idx] - mu_c) / sd_c
            else:
                # Documented fallback: too few usable control observations or
                # positives; use the full label history with factors only.
                control_used = False

        if not control_used:
            train_u = train_u_full
            s_idx = s_full
            ctrl_train = None

        Xf = F_all[s_idx][:, :r]
        y = np.asarray([lmap[m] for m in train_u], dtype=float)

        if control_used:
            X = np.column_stack([Xf, ctrl_train])
            x_test = np.concatenate([F_all[t][:r], [(ctrl_raw[t] - mu_c) / sd_c]])
            terms = ["const"] + [f"f{k}" for k in range(1, r + 1)] + ["lpr_1y_z"]
        else:
            X = Xf
            x_test = F_all[t][:r]
            terms = ["const"] + [f"f{k}" for k in range(1, r + 1)]

        fit = fit_probit(X, y)
        beta = fit["beta"]
        p = float(norm.cdf(np.r_[1.0, x_test] @ beta))
        base_rate = float(np.mean(y))
        y_true = float(lmap[target_month])
        thresh_class = int(p >= 0.5)

        rows.append({
            "model": "probit",
            "forecast_origin": row.forecast_origin,
            "target_period": target_month,
            "y_true": y_true,
            "y_pred_proba": p,
            "y_pred": thresh_class,
            "fold": str(row.fold),
            "seed": int(bundle.get("seed", 42)),
            "leakage_discipline": "safe",
            "train_start": train_u[0],
            "train_end": train_u[-1],
            "n_train_labels": int(len(train_u)),
            "n_positive": int(np.sum(y)),
            "base_rate": base_rate,
            "r": r,
            "control_used": control_used,
            "fit_method": fit["method"],
            "y_lag1": float(lmap.get(panel.iloc[t]["month"], np.nan)) if panel.iloc[t]["month"] in lmap else np.nan,
        })

        for k, term in enumerate(terms):
            coef_rows.append({
                "fold": str(row.fold),
                "forecast_origin": row.forecast_origin,
                "term": term,
                "coef": float(beta[k]) if k < len(beta) else np.nan,
                "se": float(fit["se"][k]) if k < len(fit["se"]) else np.nan,
                "t_stat": float(fit["t"][k]) if k < len(fit["t"]) else np.nan,
                "n_train": int(len(train_u)),
                "n_positive": int(np.sum(y)),
                "r": r,
                "control_used": control_used,
                "fit_method": fit["method"],
            })

        fold_detail.append({
            "fold": str(row.fold),
            "forecast_origin": row.forecast_origin,
            "target_period": target_month,
            "train_start": train_u[0],
            "train_end": train_u[-1],
            "n_train_labels": int(len(train_u)),
            "n_positive": int(np.sum(y)),
            "r": r,
            "control_used": control_used,
            "fit_method": fit["method"],
        })

    preds = pd.DataFrame(rows)
    preds["error"] = preds["y_true"] - preds["y_pred_proba"]
    preds["brier"] = preds["error"] ** 2
    preds["correct"] = (preds["y_pred"] == preds["y_true"]).astype(int)
    coefs = pd.DataFrame(coef_rows)
    if len(preds) != 82:
        raise RuntimeError(f"regime model must produce 82 predictions, got {len(preds)}")
    if preds["y_pred_proba"].isna().any():
        raise RuntimeError("NaN predicted probabilities")

    folds = pd.DataFrame(fold_detail)
    folds = folds.merge(
        preds[["fold", "y_true", "y_pred_proba", "y_pred", "base_rate", "brier", "correct"]],
        on="fold", how="left", validate="one_to_one",
    )

    metrics = regime_metrics(preds)
    return {
        "predictions": preds,
        "coefficients": coefs,
        "folds": folds,
        "metrics": metrics,
        "use_control": use_control,
        "start_label_month": start_label_month,
    }


def regime_metrics(preds: pd.DataFrame) -> dict[str, Any]:
    y = preds["y_true"].to_numpy(float)
    p = preds["y_pred_proba"].to_numpy(float)
    cls = (p >= 0.5).astype(int)

    brier = float(np.mean((p - y) ** 2))
    auc = float(roc_auc_score(y, p)) if len(np.unique(y)) == 2 else float("nan")
    acc = float(np.mean(cls == y))
    tp = int(np.sum((cls == 1) & (y == 1)))
    tn = int(np.sum((cls == 0) & (y == 0)))
    fp = int(np.sum((cls == 1) & (y == 0)))
    fn = int(np.sum((cls == 0) & (y == 1)))
    sens = tp / (tp + fn) if (tp + fn) else float("nan")
    spec = tn / (tn + fp) if (tn + fp) else float("nan")
    bal = float(np.nanmean([sens, spec]))
    ll = float(log_loss(y, np.clip(p, 1e-12, 1 - 1e-12), labels=[0, 1]))

    # prior-frequency baseline on the same folds
    prior_p = preds["base_rate"].to_numpy(float)
    prior_cls = (prior_p >= 0.5).astype(int)
    prior_brier = float(np.mean((prior_p - y) ** 2))
    prior_acc = float(np.mean(prior_cls == y))
    prior_auc = float(roc_auc_score(y, prior_p)) if len(np.unique(y)) == 2 else float("nan")

    # persistence baseline: predict d_{t+1} = d_t
    lag = preds["y_lag1"].to_numpy(float)
    ok = np.isfinite(lag)
    persist_brier = float(np.mean((lag[ok] - y[ok]) ** 2)) if ok.any() else float("nan")
    persist_acc = float(np.mean((lag[ok] >= 0.5) == (y[ok] >= 0.5))) if ok.any() else float("nan")
    persist_auc = float(roc_auc_score(y[ok], lag[ok])) if ok.sum() and len(np.unique(y[ok])) == 2 else float("nan")

    # yearly blocks
    yearly: dict[str, Any] = {}
    dft = preds.copy()
    dft["year"] = dft["target_period"].str[:4]
    for year, g in dft.groupby("year"):
        yy = g["y_true"].to_numpy(float)
        pp = g["y_pred_proba"].to_numpy(float)
        yearly[year] = {
            "n": int(len(g)),
            "brier": float(np.mean((pp - yy) ** 2)),
            "auc": float(roc_auc_score(yy, pp)) if len(np.unique(yy)) == 2 else None,
            "accuracy": float(np.mean((pp >= 0.5).astype(int) == yy)),
        }

    return {
        "overall": {
            "n": int(len(preds)),
            "n_positive": int(np.sum(y)),
            "n_negative": int(np.sum(1 - y)),
            "brier": brier,
            "auc": auc,
            "accuracy": acc,
            "balanced_accuracy": bal,
            "sensitivity": float(sens),
            "specificity": float(spec),
            "log_loss": ll,
            "confusion": {"tp": tp, "tn": tn, "fp": fp, "fn": fn},
        },
        "baseline_prior_frequency": {
            "brier": prior_brier,
            "auc": prior_auc,
            "accuracy": prior_acc,
            "note": "per-fold training base rate used as the constant probability",
        },
        "baseline_persistence": {
            "brier": persist_brier,
            "auc": persist_auc,
            "accuracy": persist_acc,
            "note": "predict d_(t+1) = d_t; probabilities are 0/1",
            "n_evaluated": int(ok.sum()),
        },
        "brier_skill_score_vs_prior": 1.0 - brier / prior_brier if prior_brier > 0 else float("nan"),
        "by_year": yearly,
    }


def coefficient_tables(coefs: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    final_fold = coefs["fold"].iloc[-1]
    final = coefs[coefs["fold"] == final_fold].copy()
    final = final[["fold", "forecast_origin", "term", "coef", "se", "t_stat", "n_train", "n_positive", "r", "control_used", "fit_method"]]

    summary_rows = []
    for term, g in coefs.groupby("term"):
        vals = g["coef"].dropna().to_numpy(float)
        summary_rows.append({
            "term": term,
            "n_folds": int(len(vals)),
            "mean_coef": float(np.mean(vals)) if len(vals) else np.nan,
            "sd_coef": float(np.std(vals, ddof=1)) if len(vals) > 1 else np.nan,
            "min_coef": float(np.min(vals)) if len(vals) else np.nan,
            "max_coef": float(np.max(vals)) if len(vals) else np.nan,
            "sign_stability": float(np.mean(np.sign(vals) == np.sign(np.mean(vals)))) if len(vals) and np.mean(vals) != 0 else np.nan,
        })
    return final, pd.DataFrame(summary_rows)


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------

def write_fig(fig: go.Figure, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(path), include_plotlyjs=True, full_html=True)


def make_probability_figure(preds: pd.DataFrame) -> None:
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=preds["target_period"], y=preds["y_pred_proba"], mode="lines+markers",
                             name="P(contraction)"), secondary_y=False)
    fig.add_trace(go.Scatter(x=preds["target_period"], y=preds["y_true"], mode="markers",
                             name="Realized d(t+1)", marker=dict(symbol="x", size=7, color="black")),
                  secondary_y=True)
    fig.add_hline(y=0.5, line_dash="dash", line_color="grey", secondary_y=False)
    fig.update_layout(title="Probit contraction probability, expanding walk-forward",
                      xaxis_title="Target month", template="plotly_white", hovermode="x unified",
                      xaxis=dict(tickangle=45))
    fig.update_yaxes(title_text="Probability", range=[0, 1], secondary_y=False)
    fig.update_yaxes(title_text="Realized label", range=[-0.1, 1.1], secondary_y=True, showgrid=False)
    write_fig(fig, FIG_DIR / "fig_regime_probabilities.html")


def make_roc_figure(preds: pd.DataFrame) -> None:
    y = preds["y_true"].to_numpy(float)
    p = preds["y_pred_proba"].to_numpy(float)
    fpr, tpr, _ = roc_curve(y, p)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=fpr, y=tpr, mode="lines", name=f"Probit (AUC={roc_auc_score(y, p):.3f})"))
    fig.add_trace(go.Scatter(x=[0, 1], y=[0, 1], mode="lines", name="Chance", line=dict(dash="dash", color="grey")))
    fig.update_layout(title="ROC - pooled 82 out-of-sample target months",
                      xaxis_title="False positive rate", yaxis_title="True positive rate",
                      template="plotly_white")
    write_fig(fig, FIG_DIR / "fig_regime_roc.html")


def make_calibration_figure(preds: pd.DataFrame) -> None:
    df = preds.copy()
    df["bin"] = pd.qcut(df["y_pred_proba"], q=min(5, df["y_pred_proba"].nunique()), duplicates="drop")
    g = df.groupby("bin", observed=True).agg(p=("y_pred_proba", "mean"), y=("y_true", "mean"), n=("y_true", "size")).reset_index()
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=g["p"], y=g["y"], mode="lines+markers+text",
                             text=[f"n={n}" for n in g["n"]], textposition="top center", name="Reliability"))
    fig.add_trace(go.Scatter(x=[0, 1], y=[0, 1], mode="lines", name="Perfect calibration",
                             line=dict(dash="dash", color="grey")))
    fig.update_layout(title="Calibration (quantile bins of predicted probability)",
                      xaxis_title="Mean predicted probability", yaxis_title="Observed contraction frequency",
                      template="plotly_white")
    write_fig(fig, FIG_DIR / "fig_regime_calibration.html")


def make_brier_figure(preds: pd.DataFrame) -> None:
    df = preds.copy()
    df["fold_brier"] = (df["y_pred_proba"] - df["y_true"]) ** 2
    df["prior_brier"] = (df["base_rate"] - df["y_true"]) ** 2
    df["rolling_model"] = df["fold_brier"].rolling(6, min_periods=1).mean()
    df["rolling_prior"] = df["prior_brier"].rolling(6, min_periods=1).mean()
    fig = go.Figure()
    fig.add_trace(go.Bar(x=df["target_period"], y=df["fold_brier"], name="Fold Brier", opacity=0.35))
    fig.add_trace(go.Scatter(x=df["target_period"], y=df["rolling_model"], mode="lines", name="Probit (6m mean)"))
    fig.add_trace(go.Scatter(x=df["target_period"], y=df["rolling_prior"], mode="lines", name="Prior baseline (6m mean)"))
    fig.update_layout(title="Fold-level Brier score vs prior-frequency baseline",
                      xaxis_title="Target month", yaxis_title="Brier score",
                      template="plotly_white", xaxis=dict(tickangle=45))
    write_fig(fig, FIG_DIR / "fig_regime_brier.html")


def make_coef_figure(final: pd.DataFrame) -> None:
    df = final[final["term"] != "const"].copy()
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=df["term"], y=df["coef"],
        error_y=dict(type="data", array=1.96 * df["se"], visible=True),
        name="Coefficient (95% CI)",
    ))
    fig.add_hline(y=0, line_color="grey")
    fig.update_layout(title="Final-fold Probit coefficients with standard errors",
                      xaxis_title="Term", yaxis_title="Coefficient",
                      template="plotly_white")
    write_fig(fig, FIG_DIR / "fig_regime_coefficients.html")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(seed: int) -> dict[str, Any]:
    t0 = time.perf_counter()
    np.random.seed(seed)

    CONSOLE.print(Panel.fit(
        "[bold]Option A - Probit contraction classifier[/bold]\n"
        f"seed={seed} | 82 target months | 2020-01 to 2026-10",
        border_style="cyan",
    ))

    bundle = d.run(seed=seed, write=False, verbose=False)
    bundle["seed"] = seed
    panel: pd.DataFrame = bundle["model_panel"]
    features: pd.DataFrame = bundle["features"]
    rs: pd.DataFrame = bundle["regime_schedule"]
    target: pd.DataFrame = bundle["target"]
    month_idx = d.month_index(panel["month"].tolist())

    factor_indicators = [c for c in bundle["indicators"] if c not in d.RATE_BLOCK]
    cache = RegimeFactorCache(features, factor_indicators)
    qmap = build_quarter_maps(target, panel)

    # Rank selection is label-independent: use the GDP nowcast criterion once
    # per monthly origin, then reuse it for both thresholds/specifications.
    rank_by_origin: dict[int, int] = {}
    rank_detail: list[dict[str, Any]] = []
    entries: dict[int, dict[str, Any]] = {}
    for origin_idx in sorted(rs["origin_idx"].astype(int).unique()):
        entry = cache.get(origin_idx)
        entries[int(origin_idx)] = entry
        train_q = observable_quarters(qmap, int(origin_idx))
        max_rank = min(d.MAX_FACTORS, entry["pipe"]["max_components"])
        best_r, detail = select_rank(cache, qmap, int(origin_idx), train_q, max_rank)
        rank_by_origin[int(origin_idx)] = int(best_r)
        for rec in detail:
            rank_detail.append({
                "origin_month": panel.iloc[int(origin_idx)]["month"],
                **rec,
            })

    labels50: pd.DataFrame = bundle["labels50"]
    labels495: pd.DataFrame = bundle["labels495"]

    primary = model_run(bundle, entries, rank_by_origin, labels50, use_control=True,
                        start_label_month="2013-11")
    factors_only = model_run(bundle, entries, rank_by_origin, labels50, use_control=False,
                             start_label_month="2008-02")
    sens495 = model_run(bundle, entries, rank_by_origin, labels495, use_control=True,
                        start_label_month="2013-11")
    sens495_fo = model_run(bundle, entries, rank_by_origin, labels495, use_control=False,
                           start_label_month="2008-02")

    final50, summary50 = coefficient_tables(primary["coefficients"])
    final495, summary495 = coefficient_tables(sens495_fo["coefficients"])
    final495c, summary495c = coefficient_tables(sens495["coefficients"])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    primary["predictions"].to_csv(OUT_DIR / "predictions.csv", index=False)
    primary["coefficients"].to_csv(OUT_DIR / "coefficients.csv", index=False)
    final50.to_csv(OUT_DIR / "coefficients_final.csv", index=False)
    summary50.to_csv(OUT_DIR / "coefficients_summary.csv", index=False)
    primary["folds"].to_csv(OUT_DIR / "folds.csv", index=False)

    factors_only["predictions"].to_csv(OUT_DIR / "predictions_factors_only.csv", index=False)
    sens495_fo["predictions"].to_csv(OUT_DIR / "predictions_threshold_49p5.csv", index=False)
    sens495["predictions"].to_csv(OUT_DIR / "predictions_threshold_49p5_controls.csv", index=False)
    final495.to_csv(OUT_DIR / "coefficients_final_threshold_49p5.csv", index=False)
    final495c.to_csv(OUT_DIR / "coefficients_final_threshold_49p5_controls.csv", index=False)

    # preprocessing provenance for the primary factor model
    prep_rows = []
    for oi, entry in sorted(entries.items()):
        pipe = entry["pipe"]
        for j, name in enumerate(pipe["retained"]):
            prep_rows.append({
                "origin_month": panel.iloc[oi]["month"],
                "indicator": name,
                "retained": True,
                "mu": float(pipe["mu"][j]),
                "sd": float(pipe["sd"][j]),
                "n_train_months": int(pipe["n_train"]),
                "r_selected": int(rank_by_origin[oi]),
                "variance_explained_selected": d.factor_variance_ratio(pipe, int(rank_by_origin[oi])),
            })
    pd.DataFrame(prep_rows).to_csv(OUT_DIR / "preprocessing.csv", index=False)

    fit_seconds = time.perf_counter() - t0

    def payload(result: dict[str, Any], threshold: float, spec: str) -> dict[str, Any]:
        final, summary = coefficient_tables(result["coefficients"])
        return {
            "task": "regime_classification",
            "target": f"pmi_contraction_next_{str(threshold).replace('.', 'p')}",
            "approach": "A",
            "leakage_discipline": "safe",
            "seed": seed,
            "params": {
                "model": "Probit: P(d_(t+1)=1) = Phi(beta' z_t)",
                "label_definition": "d_t = 1{pmi_man_t < threshold and pmi_man_(t-1) < threshold}",
                "threshold": threshold,
                "z_t": "PCA factors" + (" + lpr_1y control (training-standardized)" if spec == "factors_plus_lpr" else " only"),
                "factor_universe": "panel excluding the rate/spread block reserved for controls",
                "control": CONTROL if spec == "factors_plus_lpr" else None,
                "factor_screen": {
                    "recent_months": d.SCREEN_RECENT_MONTHS,
                    "min_recent_coverage": d.SCREEN_MIN_RECENT_COVERAGE,
                    "min_observations": d.SCREEN_MIN_OBSERVATIONS,
                },
                "rank_candidates": list(range(1, d.MAX_FACTORS + 1)),
                "rank_selection": "inner expanding-window CV, pooled M1/M2/M3 GDP nowcast RMSE",
                "train_label_start": result["start_label_month"],
                "initial_label_window": [d.REGIME_INIT_START, d.REGIME_INIT_END],
                "oos_target_months": [d.REGIME_OOS_START, d.REGIME_OOS_END],
                "forecast_origins": "month-end 2019-12-31 … 2026-09-30",
                "decision_threshold": 0.5,
            },
            "metrics": result["metrics"],
            "folds": result["folds"].to_dict(orient="records"),
            "rank_path": [
                {"origin_month": panel.iloc[oi]["month"], "r": int(r)}
                for oi, r in sorted(rank_by_origin.items())
            ],
            "rank_cv_detail": rank_detail,
            "coefficients_final": final.to_dict(orient="records"),
            "coefficients_summary": summary.to_dict(orient="records"),
            "history": {
                "r_path": [
                    {"origin_month": panel.iloc[oi]["month"], "r": int(r)}
                    for oi, r in sorted(rank_by_origin.items())
                ],
                "fold_brier": result["folds"][["fold", "brier", "correct"]].to_dict(orient="records"),
                "coefficient_summary": summary.to_dict(orient="records"),
            },
            "fit_seconds": fit_seconds,
            "environment": {"python": sys.version.split()[0], "numpy": np.__version__, "pandas": pd.__version__},
            "outputs": {
                "predictions": "results/approach1/regime/predictions.csv",
                "coefficients": "results/approach1/regime/coefficients.csv",
            },
        }

    primary_payload = payload(primary, 50.0, "factors_plus_lpr")
    primary_payload["sensitivity_threshold_49p5"] = {
        "primary_file": "metrics_threshold_49p5.json",
        "primary_spec": "factors_only_full_history",
        "metrics": sens495_fo["metrics"],
        "control_file": "metrics_threshold_49p5_controls.json",
        "control_metrics": sens495["metrics"],
        "note": (
            "49.5 labels are reconstructed independently on the same calendar "
            "schedule. Because only 7 initial and 13 out-of-sample months are "
            "contractions at 49.5, the full-history factors-only specification "
            "is the primary sensitivity; the LPR-control specification is "
            "reported where estimable."
        ),
    }
    primary_payload["sensitivity_factors_only"] = {
        "file": "metrics_factors_only.json",
        "metrics": factors_only["metrics"],
    }
    d.write_json(OUT_DIR / "metrics.json", primary_payload)
    d.write_json(OUT_DIR / "metrics_factors_only.json", payload(factors_only, 50.0, "factors_only"))
    d.write_json(OUT_DIR / "metrics_threshold_49p5.json", payload(sens495_fo, 49.5, "factors_only"))
    d.write_json(OUT_DIR / "metrics_threshold_49p5_controls.json", payload(sens495, 49.5, "factors_plus_lpr"))

    # figures for the primary model
    make_probability_figure(primary["predictions"])
    make_roc_figure(primary["predictions"])
    make_calibration_figure(primary["predictions"])
    make_brier_figure(primary["predictions"])
    make_coef_figure(final50)

    m = primary["metrics"]["overall"]
    b = primary["metrics"]["baseline_prior_frequency"]
    tbl = Table(title="Probit regime classification - 82 walk-forward folds", show_lines=False)
    tbl.add_column("Metric", style="bold")
    tbl.add_column("Probit", justify="right")
    tbl.add_column("Prior baseline", justify="right")
    tbl.add_row("Brier", f"{m['brier']:.4f}", f"{b['brier']:.4f}")
    tbl.add_row("AUC", f"{m['auc']:.4f}", f"{b['auc']:.4f}")
    tbl.add_row("Accuracy", f"{m['accuracy']:.4f}", f"{b['accuracy']:.4f}")
    tbl.add_row("Balanced accuracy", f"{m['balanced_accuracy']:.4f}", "-")
    tbl.add_row("Contractions", f"{m['n_positive']}", "-")
    tbl.add_row("Brier skill vs prior", f"{primary['metrics']['brier_skill_score_vs_prior']:+.4f}", "-")
    CONSOLE.print(tbl)
    CONSOLE.print(
        f"[green]Regime classification complete[/green] | "
        f"r in {sorted(set(rank_by_origin.values()))} | "
        f"Brier {m['brier']:.4f} vs prior {b['brier']:.4f} | "
        f"49.5 Brier {sens495['metrics']['overall']['brier']:.4f} | {fit_seconds:.1f}s"
    )
    return primary_payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Option A Probit regime classifier")
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run(seed=args.seed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

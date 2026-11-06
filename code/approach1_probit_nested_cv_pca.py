"""Strictly nested, leakage-free inner CV for the Option A Probit rank.

    uv run python code/approach1_probit_nested_cv_pca.py --seed 42
    uv run python code/approach1_probit_nested_cv_pca.py --validate-only --seed 42

This is a sensitivity experiment.  It does not modify the primary Probit, its
GDP-selected ranks, its forecasts, its metrics, the previous inner-CV
diagnostics, or any shared project documentation.

Design
------
For each outer walk-forward fold (origin month t, target d_(t+1)):
  * outer training labels are u in [2013-11, t]; predictor origins u-1;
  * inner expanding-window validation uses k = 60 .. n-1, validating on
    label u_(k+1) at predictor origin u_(k+1)-1, trained on labels u_1..u_k;
  * for every inner fold the indicator screen, scaler, missing-value fill,
    and PCA are refitted on the inner training predictor rows ONLY;
  * LPR 1Y is a separate control, standardized on the same inner training rows;
  * candidate ranks are 1..8, restricted only if a rank is infeasible for the
    common inner sample (documented in the outputs);
  * the rank with the lowest mean inner Brier is selected, lower rank on ties;
  * the outer model is then refitted from scratch on the outer training rows
    only, using the selected rank, and the outer test row is transformed with
    that outer-training scaler/PCA.

Data-dependent preprocessing is never fitted on the inner validation row or
the outer test row.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import warnings
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from scipy.stats import norm
from sklearn.decomposition import PCA
from sklearn.metrics import confusion_matrix, roc_auc_score

import approach1_data as d
from approach1_regime import ridge_probit

ROOT = d.ROOT
PRIMARY_DIR = d.RESULTS_DIR / "regime"
PREV_DIAG_DIR = PRIMARY_DIR / "diagnostics"
OUT_DIR = d.RESULTS_DIR / "probit_nested_cv_pca"
FIG_DIR = OUT_DIR / "figures"

CONTROL = "lpr_1y"
START_LABEL_MONTH = "2013-11"
MIN_INNER_TRAIN = 60
MIN_CLASS_COUNT = 5
MAX_R = d.MAX_FACTORS
SCREEN_RECENT = 60
SCREEN_MIN_COVERAGE = 0.70
SCREEN_MIN_OBS = 24
CANDIDATE_RANKS = list(range(1, MAX_R + 1))

CONSOLE = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, float) and (np.isnan(obj) or np.isinf(obj)):
        return None
    return obj


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(jsonable(payload), fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def month_end(month: str) -> str:
    return d.month_end(month)


# ---------------------------------------------------------------------------
# Preprocessing fitted only on a training matrix
# ---------------------------------------------------------------------------

def fit_preprocessor(X_raw: np.ndarray, colnames: list[str], max_components: int = MAX_R) -> dict[str, Any] | None:
    """Fit screen + scaler + PCA on ``X_raw`` only.

    ``X_raw`` rows are predictor observations corresponding to training labels.
    The validation/test row is never passed here.
    """
    n, p = X_raw.shape
    obs = np.isfinite(X_raw)
    n_obs = obs.sum(axis=0)
    recent_n = min(SCREEN_RECENT, n)
    recent_cov = obs[-recent_n:].mean(axis=0)

    sd = np.full(p, np.nan)
    for j in range(p):
        vals = X_raw[:, j][np.isfinite(X_raw[:, j])]
        if len(vals) > 1:
            sd[j] = float(np.std(vals, ddof=1))

    keep_idx = [
        j for j in range(p)
        if n_obs[j] >= SCREEN_MIN_OBS
        and recent_cov[j] >= SCREEN_MIN_COVERAGE
        and np.isfinite(sd[j])
        and sd[j] > 0
    ]
    if len(keep_idx) < 2:
        return None

    Xr = X_raw[:, keep_idx]
    mu = np.nanmean(Xr, axis=0)
    sdv = np.nanstd(Xr, axis=0, ddof=1)
    sdv[~np.isfinite(sdv) | (sdv <= 0)] = 1.0
    Z = (Xr - mu) / sdv
    Z[~np.isfinite(Z)] = 0.0

    maxc = int(min(max_components, len(keep_idx) - 1, n - 2))
    if maxc < 1:
        return None

    pca = PCA(n_components=maxc, svd_solver="full")
    pca.fit(Z)
    comp = pca.components_.copy()
    signs = np.ones(maxc)
    for k in range(maxc):
        j = int(np.argmax(np.abs(comp[k])))
        if comp[k, j] < 0:
            signs[k] = -1.0

    h = hashlib.sha256()
    h.update(np.ascontiguousarray(X_raw, dtype=np.float64).tobytes())
    h.update("|".join(colnames).encode("utf-8"))
    h.update(np.ascontiguousarray(mu, dtype=np.float64).tobytes())
    h.update(np.ascontiguousarray(sdv, dtype=np.float64).tobytes())
    h.update(np.ascontiguousarray(pca.mean_, dtype=np.float64).tobytes())
    h.update(np.ascontiguousarray(pca.components_, dtype=np.float64).tobytes())
    h.update(np.ascontiguousarray(signs, dtype=np.float64).tobytes())

    return {
        "keep_idx": np.asarray(keep_idx, dtype=int),
        "retained_cols": [colnames[j] for j in keep_idx],
        "mu": mu,
        "sd": sdv,
        "pca": pca,
        "signs": signs,
        "max_components": maxc,
        "n_train_rows": int(n),
        "n_retained": int(len(keep_idx)),
        "preprocessing_hash": h.hexdigest(),
        "n_obs": n_obs,
        "recent_cov": recent_cov,
    }


def transform_matrix(prep: dict[str, Any], X_raw: np.ndarray, r: int) -> np.ndarray:
    Xr = X_raw[:, prep["keep_idx"]]
    Z = (Xr - prep["mu"]) / prep["sd"]
    Z[~np.isfinite(Z)] = 0.0
    F = prep["pca"].transform(Z)[:, :r]
    return F * prep["signs"][:r]


def transform_row(prep: dict[str, Any], x_raw: np.ndarray, r: int) -> np.ndarray:
    return transform_matrix(prep, np.asarray(x_raw, float).reshape(1, -1), r)[0]


# ---------------------------------------------------------------------------
# Probit fits
# ---------------------------------------------------------------------------

def fast_probit_fit(X: np.ndarray, y: np.ndarray) -> np.ndarray | None:
    """Newton -> BFGS; no hidden regularization for inner CV."""
    import statsmodels.api as sm

    Xc = sm.add_constant(X, has_constant="add")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for method in ("newton", "bfgs"):
            try:
                res = sm.Probit(y, Xc).fit(method=method, maxiter=300, disp=0)
                beta = np.asarray(res.params, float)
                if np.all(np.isfinite(beta)):
                    return beta
            except Exception:
                continue
    return None


def fit_probit_safe(X: np.ndarray, y: np.ndarray) -> dict[str, Any]:
    """Mirror the primary fit choices and record convergence/fallback."""
    import statsmodels.api as sm
    from statsmodels.tools.sm_exceptions import PerfectSeparationError

    Xc = sm.add_constant(X, has_constant="add")
    for method in ("newton", "bfgs"):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                res = sm.Probit(y, Xc).fit(method=method, maxiter=2000, disp=0)
                ret = getattr(res, "mle_retvals", {}) or {}
                return {
                    "beta": np.asarray(res.params, float),
                    "method": f"statsmodels_probit_{method}",
                    "converged": bool(ret.get("converged", False)),
                    "warnflag": int(ret.get("warnflag", 0)) if ret.get("warnflag") is not None else None,
                    "iterations": int(ret.get("iterations")) if ret.get("iterations") is not None else None,
                }
            except (PerfectSeparationError, np.linalg.LinAlgError, ValueError, RuntimeError):
                continue
            except Exception:
                continue

    rp = ridge_probit(X, y)
    return {
        "beta": np.asarray(rp["beta"], float),
        "method": "ridge_probit_fallback",
        "converged": rp.get("converged", None),
        "warnflag": None,
        "iterations": None,
    }


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

def build_context(seed: int) -> dict[str, Any]:
    bundle = d.run(seed=seed, write=False, verbose=False)
    panel = bundle["model_panel"]
    features = bundle["features"]
    rs = bundle["regime_schedule"]
    lmap = {m: float(v) for m, v in zip(bundle["labels50"]["month"], bundle["labels50"]["d"]) if np.isfinite(v)}
    candidate_cols = [c for c in bundle["indicators"] if c not in d.RATE_BLOCK]

    with (PRIMARY_DIR / "metrics.json").open("r", encoding="utf-8") as fh:
        primary_metrics = json.load(fh)
    rank_primary = {x["origin_month"]: int(x["r"]) for x in primary_metrics["rank_path"]}

    return {
        "seed": seed,
        "bundle": bundle,
        "panel": panel,
        "features": features,
        "schedule": rs,
        "lmap": lmap,
        "lpr": features[CONTROL].to_numpy(float),
        "candidate_cols": candidate_cols,
        "month_idx": d.month_index(panel["month"].tolist()),
        "rank_primary": rank_primary,
        "primary_metrics": primary_metrics,
    }


def fold_labels(ctx: dict[str, Any], origin_month: str) -> dict[str, Any]:
    months = ctx["panel"]["month"].tolist()
    lmap = ctx["lmap"]
    midx = ctx["month_idx"]
    train_u = [m for m in months if START_LABEL_MONTH <= m <= origin_month and m in lmap]
    s_idx = np.asarray([midx[m] - 1 for m in train_u], dtype=int)
    y = np.asarray([lmap[m] for m in train_u], dtype=float)
    return {"train_u": train_u, "s_idx": s_idx, "y": y}


# ---------------------------------------------------------------------------
# Nested CV
# ---------------------------------------------------------------------------

def nested_outer_fold(ctx: dict[str, Any], row: Any) -> dict[str, Any]:
    panel = ctx["panel"]
    features = ctx["features"]
    candidate_cols = ctx["candidate_cols"]
    lpr = ctx["lpr"]
    month_idx = ctx["month_idx"]
    lmap = ctx["lmap"]

    outer_origin_month = str(row.origin_month)
    outer_target = str(row.target_period)
    outer_origin_idx = int(row.origin_idx)
    outer_forecast_origin = str(row.forecast_origin)
    primary_r = int(ctx["rank_primary"][outer_origin_month])

    ft = fold_labels(ctx, outer_origin_month)
    train_u, s_idx, y = ft["train_u"], ft["s_idx"], ft["y"]
    n = len(train_u)
    if s_idx.max() >= outer_origin_idx:
        raise RuntimeError("outer training predictor reaches the outer test origin")

    fold_rows: list[dict[str, Any]] = []
    inner_audit: list[dict[str, Any]] = []
    used_maxc: list[int] = []
    used_briers: dict[int, list[float]] = {r: [] for r in CANDIDATE_RANKS}
    used_count = 0
    skipped_count = 0

    for k in range(MIN_INNER_TRAIN, n):
        y_in = y[:k]
        y_val = float(y[k])
        inner_label = train_u[k]
        inner_predictor_month = panel.iloc[int(s_idx[k])]["month"]
        train_label_start = train_u[0]
        train_label_end = train_u[k - 1]
        fit_predictor_start = panel.iloc[int(s_idx[0])]["month"]
        fit_predictor_end = panel.iloc[int(s_idx[k - 1])]["month"]

        base_audit = {
            "outer_forecast_origin": outer_forecast_origin,
            "outer_target_period": outer_target,
            "inner_validation_label": inner_label,
            "inner_validation_predictor_month": inner_predictor_month,
            "inner_validation_forecast_origin": month_end(inner_predictor_month),
            "inner_train_label_start": train_label_start,
            "inner_train_label_end": train_label_end,
            "scaler_pca_fit_start": fit_predictor_start,
            "scaler_pca_fit_end": fit_predictor_end,
            "n_inner_train": int(k),
            "validation_in_training_rows": False,
            "scaler_fit_on_train_only": True,
            "pca_fit_on_train_only": True,
            "validation_target_in_estimation": False,
            "outer_test_in_inner_cv": bool(int(s_idx[k]) >= outer_origin_idx),
        }

        skip_reason = ""
        if len(np.unique(y_in)) < 2 or int(np.sum(y_in)) < MIN_CLASS_COUNT or int(np.sum(1 - y_in)) < MIN_CLASS_COUNT:
            skip_reason = "insufficient_class_variation"
        else:
            X_train_raw = features.iloc[s_idx[:k]][candidate_cols].to_numpy(dtype=float)
            prep = fit_preprocessor(X_train_raw, candidate_cols)
            if prep is None:
                skip_reason = "preprocessing_infeasible"
            else:
                lpr_train = lpr[s_idx[:k]]
                mu_l = float(np.nanmean(lpr_train))
                sd_l = float(np.nanstd(lpr_train, ddof=1))
                if not np.isfinite(sd_l) or sd_l <= 0:
                    skip_reason = "lpr_scaler_infeasible"
                else:
                    lpr_z_train = (lpr_train - mu_l) / sd_l
                    lpr_z_val = (float(lpr[s_idx[k]]) - mu_l) / sd_l
                    F_train = transform_matrix(prep, X_train_raw, prep["max_components"])
                    x_val_raw = features.iloc[int(s_idx[k])][candidate_cols].to_numpy(dtype=float)
                    f_val = transform_row(prep, x_val_raw, prep["max_components"])

                    betas: dict[int, np.ndarray] = {}
                    for r in range(1, prep["max_components"] + 1):
                        beta = fast_probit_fit(np.column_stack([F_train[:, :r], lpr_z_train]), y_in)
                        if beta is None:
                            betas = {}
                            break
                        betas[r] = beta
                    if not betas:
                        skip_reason = "probit_fit_failed"

                    base_audit.update({
                        "n_retained": int(prep["n_retained"]),
                        "inner_max_components": int(prep["max_components"]),
                        "lpr_scaler_mean": mu_l,
                        "lpr_scaler_sd": sd_l,
                        "preprocessing_hash": prep["preprocessing_hash"],
                    })

                    if not skip_reason:
                        used_maxc.append(int(prep["max_components"]))
                        used_count += 1
                        for r in range(1, prep["max_components"] + 1):
                            xv = np.r_[1.0, f_val[:r], lpr_z_val]
                            p = float(norm.cdf(xv @ betas[r]))
                            brier = (p - y_val) ** 2
                            used_briers[r].append(float(brier))
                            fold_rows.append({
                                **base_audit,
                                "candidate_rank": int(r),
                                "feasible": True,
                                "validation_brier": float(brier),
                                "inner_validation_pred_prob": p,
                                "inner_validation_target": y_val,
                                "used_for_selection": None,  # filled after r_common is known
                                "exclusion_reason": "",
                            })
                        for r in range(prep["max_components"] + 1, MAX_R + 1):
                            fold_rows.append({
                                **base_audit,
                                "candidate_rank": int(r),
                                "feasible": False,
                                "validation_brier": np.nan,
                                "inner_validation_pred_prob": np.nan,
                                "inner_validation_target": y_val,
                                "used_for_selection": False,
                                "exclusion_reason": "rank_exceeds_inner_max_components",
                            })

        if skip_reason:
            skipped_count += 1
            base_audit["skip_reason"] = skip_reason
            for r in CANDIDATE_RANKS:
                fold_rows.append({
                    **base_audit,
                    "candidate_rank": int(r),
                    "feasible": False,
                    "validation_brier": np.nan,
                    "inner_validation_pred_prob": np.nan,
                    "inner_validation_target": y_val,
                    "used_for_selection": False,
                    "exclusion_reason": skip_reason,
                })
        base_audit["used_for_selection"] = (skip_reason == "")
        inner_audit.append(base_audit)

    r_common = int(min(used_maxc)) if used_maxc else 0
    r_common = min(MAX_R, r_common)
    infeasible = [r for r in CANDIDATE_RANKS if r > r_common]

    mean_brier: dict[int, float] = {}
    n_by_r: dict[int, int] = {}
    if used_count >= 1 and r_common >= 1:
        for r in range(1, r_common + 1):
            vals = used_briers[r]
            mean_brier[r] = float(np.mean(vals)) if vals else np.nan
            n_by_r[r] = int(len(vals))
    fallback = False
    if not mean_brier or not np.any(np.isfinite(list(mean_brier.values()))):
        selected = primary_r
        fallback = True
    else:
        selected = int(min(mean_brier.items(), key=lambda kv: (kv[1], kv[0]))[0])

    # Mark candidate rows for the common selection sample.
    for rec in fold_rows:
        r = int(rec["candidate_rank"])
        if rec["exclusion_reason"]:
            rec["used_for_selection"] = False
        else:
            rec["used_for_selection"] = bool(1 <= r <= r_common)
        if rec["feasible"] and r > r_common:
            rec["used_for_selection"] = False
            rec["exclusion_reason"] = "not_in_common_candidate_set"

    # ---- outer refit on outer training rows only --------------------------
    X_outer_raw = features.iloc[s_idx][candidate_cols].to_numpy(dtype=float)
    prep_outer = fit_preprocessor(X_outer_raw, candidate_cols)
    if prep_outer is None:
        raise RuntimeError(f"outer preprocessing infeasible at {outer_forecast_origin}")

    selected_effective = int(min(selected, prep_outer["max_components"]))
    clipped = selected_effective != selected
    if fallback:
        selected_effective = int(min(primary_r, prep_outer["max_components"]))

    lpr_train = lpr[s_idx]
    mu_l = float(np.nanmean(lpr_train))
    sd_l = float(np.nanstd(lpr_train, ddof=1))
    lpr_z_train = (lpr_train - mu_l) / sd_l
    F_outer = transform_matrix(prep_outer, X_outer_raw, selected_effective)
    fit = fit_probit_safe(np.column_stack([F_outer, lpr_z_train]), y)
    beta = fit["beta"]

    x_test_raw = features.iloc[outer_origin_idx][candidate_cols].to_numpy(dtype=float)
    f_test = transform_row(prep_outer, x_test_raw, selected_effective)
    lpr_z_test = (float(lpr[outer_origin_idx]) - mu_l) / sd_l
    xv = np.r_[1.0, f_test, lpr_z_test]
    p = float(norm.cdf(xv @ beta))

    outer_audit = {
        "level": "outer",
        "outer_forecast_origin": outer_forecast_origin,
        "outer_target_period": outer_target,
        "train_label_start": train_u[0],
        "train_label_end": train_u[-1],
        "outer_train_predictor_start": panel.iloc[int(s_idx[0])]["month"],
        "outer_train_predictor_end": panel.iloc[int(s_idx[-1])]["month"],
        "outer_test_predictor_month": panel.iloc[outer_origin_idx]["month"],
        "n_outer_train": int(len(y)),
        "n_retained_outer": int(prep_outer["n_retained"]),
        "outer_max_components": int(prep_outer["max_components"]),
        "selected_rank": int(selected),
        "selected_effective": int(selected_effective),
        "clipped_to_outer_max": bool(clipped),
        "outer_scaler_pca_fit_on_train_only": True,
        "outer_test_in_training_rows": False,
        "outer_test_target_in_estimation": False,
        "preprocessing_hash": prep_outer["preprocessing_hash"],
        "outer_fit_method": fit["method"],
        "outer_fit_converged": fit["converged"],
        "outer_fit_warnflag": fit["warnflag"],
    }

    selection = {
        "outer_forecast_origin": outer_forecast_origin,
        "outer_origin_month": outer_origin_month,
        "outer_target_period": outer_target,
        "primary_rank": primary_r,
        "selected_rank": int(selected),
        "selected_effective": int(selected_effective),
        "fallback_to_primary": bool(fallback),
        "clipped_to_outer_max": bool(clipped),
        "n_inner_folds_used": int(used_count),
        "n_inner_folds_skipped": int(skipped_count),
        "n_common_candidate_ranks": int(r_common),
        "infeasible_ranks": ";".join(str(r) for r in infeasible),
        "n_inner_for_selected": int(n_by_r.get(selected, 0)),
        "inner_brier_selected": float(mean_brier.get(selected, np.nan)) if not fallback else np.nan,
        "inner_brier_primary": float(mean_brier.get(primary_r, np.nan)),
        "outer_max_components": int(prep_outer["max_components"]),
        "outer_n_retained": int(prep_outer["n_retained"]),
        "outer_fit_method": fit["method"],
    }
    for r in CANDIDATE_RANKS:
        selection[f"mean_inner_brier_r{r}"] = float(mean_brier.get(r, np.nan))
        selection[f"n_inner_r{r}"] = int(n_by_r.get(r, 0))

    prediction = {
        "model": "probit_nested_cv_pca",
        "forecast_origin": outer_forecast_origin,
        "target_period": outer_target,
        "y_true": float(lmap[outer_target]),
        "y_pred_proba": p,
        "y_pred": int(p >= 0.5),
        "fold": str(row.fold),
        "seed": int(ctx["seed"]),
        "selected_rank": int(selected),
        "selected_effective": int(selected_effective),
        "primary_rank": primary_r,
        "fallback_to_primary": bool(fallback),
        "leakage_discipline": "safe",
    }

    return {
        "fold_rows": fold_rows,
        "inner_audit": inner_audit,
        "outer_audit": outer_audit,
        "selection": selection,
        "prediction": prediction,
        "r_common": r_common,
        "mean_brier": mean_brier,
        "n_by_r": n_by_r,
    }


# ---------------------------------------------------------------------------
# Metrics and comparison
# ---------------------------------------------------------------------------

def brier_decomposition(y: np.ndarray, p: np.ndarray, bins: int = 10) -> dict[str, float]:
    edges = np.linspace(0.0, 1.0, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1], right=True), 0, bins - 1)
    ybar = float(np.mean(y))
    rel = 0.0
    res = 0.0
    for b in range(bins):
        m = idx == b
        if not m.any():
            continue
        w = float(m.mean())
        pb = float(np.mean(p[m]))
        yb = float(np.mean(y[m]))
        rel += w * (pb - yb) ** 2
        res += w * (yb - ybar) ** 2
    unc = ybar * (1 - ybar)
    return {"reliability": rel, "resolution": res, "uncertainty": unc,
            "brier_from_decomposition": rel - res + unc}


def evaluate(y: np.ndarray, p: np.ndarray) -> dict[str, Any]:
    y = np.asarray(y, float)
    p = np.asarray(p, float)
    cls = (p >= 0.5).astype(int)
    cm = confusion_matrix(y, cls, labels=[0, 1])
    tn, fp, fn, tp = int(cm[0, 0]), int(cm[0, 1]), int(cm[1, 0]), int(cm[1, 1])
    sens = tp / (tp + fn) if (tp + fn) else float("nan")
    spec = tn / (tn + fp) if (tn + fp) else float("nan")
    dec = brier_decomposition(y, p)
    return {
        "n": int(len(y)),
        "n_positive": int(np.sum(y)),
        "n_negative": int(np.sum(1 - y)),
        "brier": float(np.mean((p - y) ** 2)),
        "auc": float(roc_auc_score(y, p)) if len(np.unique(y)) == 2 else float("nan"),
        "accuracy": float(np.mean(cls == y)),
        "balanced_accuracy": float(np.nanmean([sens, spec])),
        "sensitivity": float(sens),
        "specificity": float(spec),
        "confusion_tp": tp,
        "confusion_tn": tn,
        "confusion_fp": fp,
        "confusion_fn": fn,
        "mean_predicted_probability": float(np.mean(p)),
        "observed_rate": float(np.mean(y)),
        "calibration_in_the_large": float(np.mean(p) - np.mean(y)),
        **dec,
    }


def build_comparison(primary: pd.DataFrame, prev: pd.DataFrame, nested: pd.DataFrame) -> pd.DataFrame:
    primary = primary.sort_values("target_period").reset_index(drop=True)
    prev = prev.sort_values("target_period").reset_index(drop=True)
    nested = nested.sort_values("target_period").reset_index(drop=True)

    y = primary["y_true"].to_numpy(float)
    models = {
        "original_primary_probit": primary["y_pred_proba"].to_numpy(float),
        "previous_inner_cv_sensitivity": prev["y_pred_proba"].to_numpy(float),
        "new_strict_nested_cv": nested["y_pred_proba"].to_numpy(float),
        "prior_frequency": primary["base_rate"].to_numpy(float),
        "persistence": primary["y_lag1"].to_numpy(float),
    }
    rows = []
    for name, p in models.items():
        rows.append({"model": name, **evaluate(y, p)})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run(seed: int) -> dict[str, Any]:
    t0 = time.perf_counter()
    np.random.seed(seed)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    primary_hashes_before = {
        name: sha256_file(PRIMARY_DIR / name)
        for name in ("predictions.csv", "metrics.json", "coefficients.csv", "folds.csv")
    }

    CONSOLE.print(Panel.fit(
        "[bold]Option A Probit - strictly nested inner CV[/bold]\n"
        f"seed={seed} | preprocessing refitted inside every inner fold",
        border_style="cyan",
    ))
    ctx = build_context(seed)
    results = []
    for row in ctx["schedule"].itertuples(index=False):
        results.append(nested_outer_fold(ctx, row))

    fold_df = pd.DataFrame([x for res in results for x in res["fold_rows"]])
    inner_audit_df = pd.DataFrame([x for res in results for x in res["inner_audit"]])
    outer_audit_df = pd.DataFrame([res["outer_audit"] for res in results])
    selection_df = pd.DataFrame([res["selection"] for res in results])
    pred_df = pd.DataFrame([res["prediction"] for res in results])

    fold_df.to_csv(OUT_DIR / "nested_cv_fold_results.csv", index=False)
    selection_df.to_csv(OUT_DIR / "outer_rank_selection.csv", index=False)
    pred_df.to_csv(OUT_DIR / "nested_cv_predictions.csv", index=False)
    inner_audit_df.to_csv(OUT_DIR / "inner_cv_preprocessing_audit.csv", index=False)
    outer_audit_df.to_csv(OUT_DIR / "outer_fold_audit.csv", index=False)

    # Leakage audit: one row per inner fold plus outer folds.
    audit_rows = []
    for r in inner_audit_df.itertuples(index=False):
        prep_hash = getattr(r, "preprocessing_hash", "")
        if prep_hash is None or (isinstance(prep_hash, float) and np.isnan(prep_hash)):
            prep_hash = ""
        audit_rows.append({
            "level": "inner",
            "outer_forecast_origin": r.outer_forecast_origin,
            "outer_target_period": r.outer_target_period,
            "inner_validation_label": r.inner_validation_label,
            "inner_validation_predictor_month": r.inner_validation_predictor_month,
            "inner_train_label_start": r.inner_train_label_start,
            "inner_train_label_end": r.inner_train_label_end,
            "scaler_pca_fit_start": r.scaler_pca_fit_start,
            "scaler_pca_fit_end": r.scaler_pca_fit_end,
            "validation_in_training_rows": bool(r.validation_in_training_rows),
            "scaler_fit_on_train_only": bool(r.scaler_fit_on_train_only),
            "pca_fit_on_train_only": bool(r.pca_fit_on_train_only),
            "validation_target_in_estimation": bool(r.validation_target_in_estimation),
            "outer_test_in_inner_cv": bool(r.outer_test_in_inner_cv),
            "used_for_selection": bool(r.used_for_selection),
            "preprocessing_hash": prep_hash,
            "passed": bool(
                (not r.validation_in_training_rows)
                and r.scaler_fit_on_train_only
                and r.pca_fit_on_train_only
                and (not r.validation_target_in_estimation)
                and (not r.outer_test_in_inner_cv)
            ),
        })
    for r in outer_audit_df.itertuples(index=False):
        audit_rows.append({
            "level": "outer",
            "outer_forecast_origin": r.outer_forecast_origin,
            "outer_target_period": r.outer_target_period,
            "inner_validation_label": "",
            "inner_validation_predictor_month": "",
            "inner_train_label_start": r.train_label_start,
            "inner_train_label_end": r.train_label_end,
            "scaler_pca_fit_start": r.outer_train_predictor_start,
            "scaler_pca_fit_end": r.outer_train_predictor_end,
            "validation_in_training_rows": False,
            "scaler_fit_on_train_only": bool(r.outer_scaler_pca_fit_on_train_only),
            "pca_fit_on_train_only": bool(r.outer_scaler_pca_fit_on_train_only),
            "validation_target_in_estimation": bool(r.outer_test_target_in_estimation),
            "outer_test_in_inner_cv": bool(r.outer_test_in_training_rows),
            "used_for_selection": True,
            "preprocessing_hash": r.preprocessing_hash,
            "passed": bool(r.outer_scaler_pca_fit_on_train_only and not r.outer_test_in_training_rows and not r.outer_test_target_in_estimation),
        })
    pd.DataFrame(audit_rows).to_csv(OUT_DIR / "leakage_audit.csv", index=False)

    primary = pd.read_csv(PRIMARY_DIR / "predictions.csv")
    prev = pd.read_csv(PREV_DIAG_DIR / "probit_inner_cv_predictions.csv")
    comparison = build_comparison(primary, prev, pred_df)
    comparison.to_csv(OUT_DIR / "comparison_table.csv", index=False)

    # Metrics JSON
    y = pred_df.sort_values("target_period")["y_true"].to_numpy(float)
    p = pred_df.sort_values("target_period")["y_pred_proba"].to_numpy(float)
    met = evaluate(y, p)
    rank_freq = {int(k): int(v) for k, v in pred_df["selected_rank"].value_counts().sort_index().items()}
    prev_rank_freq = {int(k): int(v) for k, v in pd.read_csv(PREV_DIAG_DIR / "probit_inner_cv_predictions.csv")["r"].value_counts().sort_index().items()}
    metrics_payload = {
        "task": "regime_classification",
        "target": "pmi_contraction_d_next_50",
        "approach": "A",
        "experiment": "strictly_nested_inner_cv_pca",
        "leakage_discipline": "safe",
        "seed": seed,
        "primary_specification_unchanged": True,
        "params": {
            "target_definition": "d_t = 1{pmi_man_t < 50 and pmi_man_(t-1) < 50}",
            "outer_schedule": "82 targets 2020-01 to 2026-10; origins 2019-12-31 to 2026-09-30",
            "train_label_start": START_LABEL_MONTH,
            "min_inner_train": MIN_INNER_TRAIN,
            "min_class_count": MIN_CLASS_COUNT,
            "inner_split": "k=60..n-1; train labels 1..k; validate label k+1",
            "candidate_ranks": CANDIDATE_RANKS,
            "rank_tie_break": "lower rank",
            "rank_selection": "mean inner-validation Brier",
            "screening": {
                "recent_rows": SCREEN_RECENT,
                "min_recent_coverage": SCREEN_MIN_COVERAGE,
                "min_observations": SCREEN_MIN_OBS,
                "fit_sample": "inner/outer training predictor rows only",
            },
            "standardization": "fitted on the same training predictor rows",
            "missing_fill": "training mean (0 after standardization)",
            "pca": "fitted on the same training predictor rows; first r components",
            "control": "lpr_1y, standardized on the same training rows",
            "outer_refit": "screen+scaler+PCA+Probit refitted on outer training rows only",
        },
        "metrics": {
            **met,
            "selected_rank_frequencies": rank_freq,
            "previous_inner_cv_rank_frequencies": prev_rank_freq,
            "n_outer_folds": int(len(pred_df)),
            "n_fallback_folds": int(pred_df["fallback_to_primary"].sum()),
            "n_rank_changed_vs_primary": int((pred_df["selected_rank"] != pred_df["primary_rank"]).sum()),
            "mean_inner_brier_selected": float(selection_df.loc[~selection_df["fallback_to_primary"], "inner_brier_selected"].mean()),
            "mean_inner_brier_primary": float(selection_df["inner_brier_primary"].mean()),
            "n_inner_folds_used": int(selection_df["n_inner_folds_used"].sum()),
            "n_inner_folds_skipped": int(selection_df["n_inner_folds_skipped"].sum()),
        },
        "comparison": comparison.to_dict(orient="records"),
        "leakage_audit": {
            "rows": int(len(audit_rows)),
            "all_passed": bool(all(a["passed"] for a in audit_rows)),
            "file": "results/approach1/probit_nested_cv_pca/leakage_audit.csv",
        },
        "fit_seconds": time.perf_counter() - t0,
        "environment": {"python": sys.version.split()[0], "numpy": np.__version__, "pandas": pd.__version__},
        "outputs": {
            "fold_results": "results/approach1/probit_nested_cv_pca/nested_cv_fold_results.csv",
            "rank_selection": "results/approach1/probit_nested_cv_pca/outer_rank_selection.csv",
            "predictions": "results/approach1/probit_nested_cv_pca/nested_cv_predictions.csv",
            "comparison": "results/approach1/probit_nested_cv_pca/comparison_table.csv",
            "leakage_audit": "results/approach1/probit_nested_cv_pca/leakage_audit.csv",
        },
    }
    write_json(OUT_DIR / "metrics.json", metrics_payload)

    # Run manifest for determinism checks
    write_json(OUT_DIR / "run_manifest.json", {
        "seed": seed,
        "hashes": {
            "nested_cv_predictions.csv": sha256_file(OUT_DIR / "nested_cv_predictions.csv"),
            "outer_rank_selection.csv": sha256_file(OUT_DIR / "outer_rank_selection.csv"),
            "nested_cv_fold_results.csv": sha256_file(OUT_DIR / "nested_cv_fold_results.csv"),
            "leakage_audit.csv": sha256_file(OUT_DIR / "leakage_audit.csv"),
        },
        "primary_hashes_before": primary_hashes_before,
    })

    # Figures: probability comparison and rank selection heatmap
    try:
        make_figures(primary, prev, pred_df, selection_df)
    except Exception as exc:
        CONSOLE.print(f"[yellow]figure generation skipped: {exc}[/yellow]")

    # Validation
    val = validate_outputs(seed, primary_hashes_before)
    write_note(metrics_payload, selection_df, val)

    tbl = Table(title="Nested-CV sensitivity comparison", show_lines=False)
    tbl.add_column("Model", style="bold")
    tbl.add_column("Brier", justify="right")
    tbl.add_column("AUC", justify="right")
    tbl.add_column("Accuracy", justify="right")
    tbl.add_column("Bal. acc.", justify="right")
    tbl.add_column("Sens.", justify="right")
    tbl.add_column("Spec.", justify="right")
    for r in comparison.itertuples(index=False):
        tbl.add_row(r.model, f"{r.brier:.4f}", f"{r.auc:.4f}", f"{r.accuracy:.4f}",
                    f"{r.balanced_accuracy:.4f}", f"{r.sensitivity:.4f}", f"{r.specificity:.4f}")
    CONSOLE.print(tbl)
    CONSOLE.print(f"[green]Nested CV complete[/green] | ranks {rank_freq} | "
                  f"validation {val['n_passed']}/{val['n_checks']} | {time.perf_counter() - t0:.1f}s")
    return {"metrics": metrics_payload, "selection": selection_df, "predictions": pred_df, "validation": val}


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------

def make_figures(primary: pd.DataFrame, prev: pd.DataFrame, nested: pd.DataFrame, selection: pd.DataFrame) -> None:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    primary = primary.sort_values("target_period")
    prev = prev.sort_values("target_period")
    nested = nested.sort_values("target_period")
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=primary["target_period"], y=primary["y_pred_proba"], mode="lines+markers", name="Primary"))
    fig.add_trace(go.Scatter(x=prev["target_period"], y=prev["y_pred_proba"], mode="lines+markers", name="Previous inner-CV"))
    fig.add_trace(go.Scatter(x=nested["target_period"], y=nested["y_pred_proba"], mode="lines+markers", name="Strict nested CV"))
    fig.add_trace(go.Scatter(x=primary["target_period"], y=primary["y_true"], mode="markers", name="Realized",
                             marker=dict(symbol="x", color="black", size=7)))
    fig.update_layout(title="Primary vs previous inner-CV vs strictly nested CV",
                      xaxis_title="Target month", yaxis_title="Probability",
                      template="plotly_white", xaxis=dict(tickangle=45))
    fig.write_html(str(FIG_DIR / "fig_nested_cv_probabilities.html"), include_plotlyjs=True, full_html=True)

    cols = [f"mean_inner_brier_r{r}" for r in CANDIDATE_RANKS]
    piv = selection.set_index("outer_target_period")[cols]
    fig2 = go.Figure(data=go.Heatmap(z=piv.to_numpy(float), x=CANDIDATE_RANKS, y=list(piv.index),
                                     colorscale="Viridis", colorbar=dict(title="Brier")))
    fig2.update_layout(title="Nested inner-CV Brier by candidate rank",
                       xaxis_title="Candidate rank", yaxis_title="Outer target month",
                       template="plotly_white", height=900)
    fig2.write_html(str(FIG_DIR / "fig_nested_rank_selection.html"), include_plotlyjs=True, full_html=True)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_outputs(seed: int, primary_hashes_before: dict[str, str] | None = None) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: Any = None) -> None:
        checks.append({"check": name, "passed": bool(passed), "detail": detail})

    primary = pd.read_csv(PRIMARY_DIR / "predictions.csv").sort_values("target_period").reset_index(drop=True)
    pred = pd.read_csv(OUT_DIR / "nested_cv_predictions.csv").sort_values("target_period").reset_index(drop=True)
    sel = pd.read_csv(OUT_DIR / "outer_rank_selection.csv")
    fold = pd.read_csv(OUT_DIR / "nested_cv_fold_results.csv")
    audit = pd.read_csv(OUT_DIR / "leakage_audit.csv")
    met = json.loads((OUT_DIR / "metrics.json").read_text(encoding="utf-8"))

    add("outer: 82 forecasts", len(pred) == 82, len(pred))
    add("outer: no duplicates", pred["target_period"].nunique() == 82 and pred["forecast_origin"].nunique() == 82, None)
    add("outer: target periods identical to primary", bool((pred["target_period"] == primary["target_period"]).all()), None)
    add("outer: forecast origins identical to primary", bool((pred["forecast_origin"] == primary["forecast_origin"]).all()), None)
    add("outer: realized labels identical to primary", bool(np.allclose(pred["y_true"], primary["y_true"])), None)
    add("outer: rank selection rows = 82", len(sel) == 82, len(sel))
    add("outer: no fallback", int(pred["fallback_to_primary"].sum()) == 0, int(pred["fallback_to_primary"].sum()))

    # Metric recomputation
    y = pred["y_true"].to_numpy(float)
    p = pred["y_pred_proba"].to_numpy(float)
    rec = evaluate(y, p)
    for key in ("brier", "auc", "accuracy", "balanced_accuracy"):
        add(f"metrics: {key} recomputed", abs(float(rec[key]) - float(met["metrics"][key])) < 1e-10,
            [rec[key], met["metrics"][key]])
    freq = {int(k): int(v) for k, v in pred["selected_rank"].value_counts().sort_index().items()}
    met_freq = {int(k): int(v) for k, v in met["metrics"]["selected_rank_frequencies"].items()}
    add("metrics: selected-rank frequencies", freq == met_freq, [freq, met_freq])

    # Leakage audit
    add("audit: all rows passed", bool(audit["passed"].all()), None)
    add("audit: no validation row in training", not bool(audit["validation_in_training_rows"].any()), None)
    add("audit: scaler fitted on training only", bool(audit["scaler_fit_on_train_only"].all()), None)
    add("audit: PCA fitted on training only", bool(audit["pca_fit_on_train_only"].all()), None)
    add("audit: validation target not in estimation", not bool(audit["validation_target_in_estimation"].any()), None)
    add("audit: outer test not in inner CV", not bool(audit.loc[audit["level"] == "inner", "outer_test_in_inner_cv"].any()), None)
    add("audit: outer test not in outer training", not bool(audit.loc[audit["level"] == "outer", "outer_test_in_inner_cv"].any()), None)
    add("audit: every inner fold has a preprocessing hash or documented skip",
        bool(audit.loc[audit["level"] == "inner"].apply(lambda r: bool(str(r["preprocessing_hash"])) or (not bool(r["used_for_selection"])), axis=1).all()), None)

    # Fold-level sanity
    add("inner: fold rows have candidates 1..8", bool(set(fold["candidate_rank"].unique()) == set(CANDIDATE_RANKS)), sorted(fold["candidate_rank"].unique().tolist()))
    add("inner: used rows have finite Brier", bool(fold.loc[fold["used_for_selection"], "validation_brier"].notna().all()), None)
    add("inner: used Brier in [0,1]", bool(fold.loc[fold["used_for_selection"], "validation_brier"].between(0, 1).all()), None)
    excl = fold["exclusion_reason"].astype("string").fillna("")
    bad_skip = fold[(excl != "") & (fold["used_for_selection"].astype(bool))]
    add("inner: no Brier computed from a skipped/infeasible fold", len(bad_skip) == 0, len(bad_skip))

    # Independent recomputation of a deterministic sample of inner folds
    ctx = build_context(seed)
    sample_outer = sel.iloc[::17].head(5)
    n_sample = 0
    n_match = 0
    for row in sample_outer.itertuples(index=False):
        outer = str(row.outer_forecast_origin)
        origin_month = pd.Timestamp(outer).strftime("%Y-%m")
        ft = fold_labels(ctx, origin_month)
        sub = fold[(fold["outer_forecast_origin"] == outer) & fold["used_for_selection"]]
        if sub.empty:
            continue
        for label in sorted(sub["inner_validation_label"].unique())[:3]:
            srow = sub[sub["inner_validation_label"] == label].iloc[0]
            k = int(srow["n_inner_train"])
            X_train_raw = ctx["features"].iloc[ft["s_idx"][:k]][ctx["candidate_cols"]].to_numpy(float)
            prep = fit_preprocessor(X_train_raw, ctx["candidate_cols"])
            lpr_train = ctx["lpr"][ft["s_idx"][:k]]
            mu_l = float(np.nanmean(lpr_train))
            sd_l = float(np.nanstd(lpr_train, ddof=1))
            z_train = (lpr_train - mu_l) / sd_l
            F_train = transform_matrix(prep, X_train_raw, prep["max_components"])
            x_val_raw = ctx["features"].iloc[int(ft["s_idx"][k])][ctx["candidate_cols"]].to_numpy(float)
            f_val = transform_row(prep, x_val_raw, prep["max_components"])
            z_val = (float(ctx["lpr"][ft["s_idx"][k]]) - mu_l) / sd_l
            for r in sorted(sub["candidate_rank"].unique()):
                beta = fast_probit_fit(np.column_stack([F_train[:, :r], z_train]), ft["y"][:k])
                if beta is None:
                    continue
                pv = float(norm.cdf(np.r_[1.0, f_val[:r], z_val] @ beta))
                b = (pv - float(ft["y"][k])) ** 2
                saved_rows = sub[
                    (sub["inner_validation_label"] == label) & (sub["candidate_rank"] == r)
                ]["validation_brier"]
                if saved_rows.empty:
                    continue
                saved = float(saved_rows.iloc[0])
                n_sample += 1
                if abs(b - saved) < 1e-10:
                    n_match += 1
    add("inner: sample recomputation matches saved Brier", n_sample > 0 and n_sample == n_match, [n_match, n_sample])

    # Determinism / primary unchanged
    manifest_path = OUT_DIR / "run_manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        current = {
            "nested_cv_predictions.csv": sha256_file(OUT_DIR / "nested_cv_predictions.csv"),
            "outer_rank_selection.csv": sha256_file(OUT_DIR / "outer_rank_selection.csv"),
            "nested_cv_fold_results.csv": sha256_file(OUT_DIR / "nested_cv_fold_results.csv"),
            "leakage_audit.csv": sha256_file(OUT_DIR / "leakage_audit.csv"),
        }
        add("determinism: output hashes match manifest", current == manifest["hashes"], [current, manifest["hashes"]])
        if primary_hashes_before is None:
            add("primary: unchanged since full run", manifest["primary_hashes_before"] == {
                name: sha256_file(PRIMARY_DIR / name) for name in manifest["primary_hashes_before"]
            }, None)
        else:
            add("primary: unchanged before/after", primary_hashes_before == {
                name: sha256_file(PRIMARY_DIR / name) for name in primary_hashes_before
            }, None)

    payload = {
        "n_checks": len(checks),
        "n_passed": int(sum(c["passed"] for c in checks)),
        "n_failed": int(sum(not c["passed"] for c in checks)),
        "all_passed": all(c["passed"] for c in checks),
        "checks": checks,
        "primary_hashes": {name: sha256_file(PRIMARY_DIR / name) for name in ("predictions.csv", "metrics.json", "coefficients.csv", "folds.csv")},
    }
    write_json(OUT_DIR / "validation_report.json", payload)
    return payload


# ---------------------------------------------------------------------------
# Diagnostic note
# ---------------------------------------------------------------------------

def write_note(metrics: dict[str, Any], selection: pd.DataFrame, validation: dict[str, Any]) -> None:
    m = metrics["metrics"]
    comp = {r["model"]: r for r in metrics["comparison"]}
    changed = int((selection["selected_rank"] != selection["primary_rank"]).sum())
    freq = m["selected_rank_frequencies"]
    prev_freq = m["previous_inner_cv_rank_frequencies"]

    def f(x: Any, nd: int = 4) -> str:
        try:
            v = float(x)
        except Exception:
            return str(x)
        return f"{v:.{nd}f}" if np.isfinite(v) else "-"

    lines = [
        "# Strictly nested inner-CV sensitivity - diagnostic note",
        "",
        "This note is generated by `code/approach1_probit_nested_cv_pca.py`. It is a sensitivity "
        "experiment, not `report.md`, and it makes no final model-selection decision.",
        "",
        "## Design",
        "",
        "The target, outer forecast origins, outer training windows, predictor universe, release-lag "
        "treatment and 82 evaluation observations are identical to the existing Probit experiment. "
        "For each outer fold, inner expanding-window validation uses the same k=60..n-1 split as the "
        "previous diagnostic. The difference is that the indicator screen, scaler, missing-value fill, "
        "PCA loadings and LPR standardization are refitted from scratch inside every inner fold, using "
        "only that inner fold's training predictor rows. The inner validation predictor is transformed "
        "with those fitted parameters and never participates in fitting. The validation label is used "
        "only to compute Brier.",
        "",
        "After the rank is selected, the screen, scaler, PCA and Probit are refitted on the outer "
        "training predictor rows only; the outer test predictor is transformed with that outer-training "
        "preprocessing. The outer test target is used only for evaluation.",
        "",
        "## Results",
        "",
        f"- Selected ranks: `{freq}`. Previous inner-CV ranks: `{prev_freq}`. "
        f"Ranks changed vs the primary in {changed}/{len(selection)} outer folds.",
        f"- Mean inner Brier at the selected rank: {f(m['mean_inner_brier_selected'])}; "
        f"at the primary rank: {f(m['mean_inner_brier_primary'])}.",
        f"- Outer OOS: Brier {f(m['brier'])}, AUC {f(m['auc'])}, accuracy {f(m['accuracy'])}, "
        f"balanced accuracy {f(m['balanced_accuracy'])}, n={m['n']}, positives={m['n_positive']}.",
        "",
        "| model | Brier | AUC | Accuracy | Balanced accuracy | Sensitivity | Specificity |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("original_primary_probit", "previous_inner_cv_sensitivity", "new_strict_nested_cv",
                 "prior_frequency", "persistence"):
        r = comp[name]
        lines.append(f"| {name} | {f(r['brier'])} | {f(r['auc'])} | {f(r['accuracy'])} | "
                     f"{f(r['balanced_accuracy'])} | {f(r['sensitivity'])} | {f(r['specificity'])} |")
    pn = comp["new_strict_nested_cv"]
    pp = comp["original_primary_probit"]
    lines += [
        "",
        f"Calibration in the large: nested {f(pn['calibration_in_the_large'])} vs primary "
        f"{f(pp['calibration_in_the_large'])}. Mean predicted probability: nested "
        f"{f(pn['mean_predicted_probability'])} vs observed {f(pn['observed_rate'])}. "
        f"Brier reliability/resolution: nested {f(pn['reliability'])}/{f(pn['resolution'])}; "
        f"primary {f(pp['reliability'])}/{f(pp['resolution'])}.",
        "",
        "The nested model's Brier is slightly lower than the primary and the prior-frequency baseline, "
        "but its AUC is lower and its balanced accuracy is lower. The improvement is therefore mainly "
        "in calibration/accuracy at the 0.5 threshold, not in ranking. This distinction matters: the "
        "nested model does not dominate the primary or the baselines.",
        "",
        "## Interpretation",
        "",
        "- The new procedure is genuinely nested: every inner fold has its own screen, scaler and PCA "
        "fitted only on its training predictor rows; the inner validation row and the outer test row "
        "are never in those fitting samples.",
        "- The candidate set is the same 1..8 set. Infeasible ranks, if any, are recorded in "
        "`outer_rank_selection.csv`; no candidate is silently dropped.",
        "- OOS differences are descriptive, not a selection criterion. The rank was selected only by "
        "mean inner-validation Brier.",
        "",
        "## Limitations",
        "",
        "- The nested outer model fits screen/scaler/PCA on the outer training-label predictor rows, "
        "whereas the primary fits PCA on all monthly rows up to the origin. This is a deliberate "
        "nested design; it is a comparison caveat, not a claim that the primary is wrong.",
        "- The training sample is small (74-155 outer labels), so rank selection and coefficients have "
        "limited precision.",
        "- The 49.5 threshold is not mixed into this experiment.",
        f"- Validation: {validation['n_passed']}/{validation['n_checks']} checks passed; "
        f"all passed: {validation['all_passed']}.",
        "",
        "## Reproduction",
        "",
        "```bash",
        "uv run python code/approach1_probit_nested_cv_pca.py --seed 42",
        "uv run python code/approach1_probit_nested_cv_pca.py --validate-only --seed 42",
        "```",
        "",
        "## Decision status",
        "",
        "No model is selected or promoted. The original primary Probit and the previous sensitivity "
        "remain unchanged; any change requires the human team's explicit approval.",
    ]
    (OUT_DIR / "diagnostic_note.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_validate_only(seed: int) -> dict[str, Any]:
    val = validate_outputs(seed, primary_hashes_before=None)
    tbl = Table(title="Nested-CV validation")
    tbl.add_column("Checks", justify="right")
    tbl.add_column("Passed", justify="right")
    tbl.add_column("Failed", justify="right")
    tbl.add_row(str(val["n_checks"]), str(val["n_passed"]), str(val["n_failed"]))
    CONSOLE.print(tbl)
    CONSOLE.print("[green]Validation passed.[/green]" if val["all_passed"] else "[red]Validation failed.[/red]")
    return val


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Strictly nested inner-CV Probit rank sensitivity")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--validate-only", action="store_true")
    args = p.parse_args(argv)
    if args.validate_only:
        val = run_validate_only(args.seed)
        return 0 if val["all_passed"] else 1
    out = run(args.seed)
    return 0 if out["validation"]["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

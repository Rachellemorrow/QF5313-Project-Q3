"""Optimizer-convergence audit of the strictly nested Probit experiment.

    uv run python code/approach1_probit_nested_cv_pca_optimizer_audit.py --seed 42
    uv run python code/approach1_probit_nested_cv_pca_optimizer_audit.py --validate-only --seed 42

This is a read-only audit of the existing strictly nested experiment.  It writes
only under ``results/approach1/probit_nested_cv_pca_optimizer_audit/``.

It audits every inner candidate fit (4,469 used inner folds x 8 ranks) and every
final outer fit (82), recording the actual optimizer status.  It does not
create a corrected model or overwrite any existing result.
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
import statsmodels.api as sm
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from scipy.stats import norm

import approach1_data as d
import approach1_probit_nested_cv_pca as nx
from approach1_regime import ridge_probit

ROOT = d.ROOT
AUDIT_DIR = d.RESULTS_DIR / "probit_nested_cv_pca_optimizer_audit"
NX_DIR = d.RESULTS_DIR / "probit_nested_cv_pca"
NX_SCRIPT = ROOT / "code" / "approach1_probit_nested_cv_pca.py"

CONSOLE = Console()

CONVERGENCE_WARNING_KEYS = (
    "converge", "iteration", "maxiter", "max iter", "separation",
    "singular", "hessian", "invert", "not converge",
)
NUMERICAL_WARNING_KEYS = (
    "divide", "overflow", "invalid value", "nan", "inf",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_hashes(root: Path) -> dict[str, str]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(ROOT))] = sha256_file(p)
    return out


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


# ---------------------------------------------------------------------------
# Audited fit attempts
# ---------------------------------------------------------------------------

def _warning_text(messages: list[str]) -> str:
    return " | ".join(messages)


def _has_any(messages: list[str], keys: tuple[str, ...]) -> bool:
    text = " ".join(messages).lower()
    return any(k in text for k in keys)


def extract_fit_info(res: Any, Xc: np.ndarray, y: np.ndarray, method: str,
                     warning_messages: list[str]) -> dict[str, Any]:
    ret = getattr(res, "mle_retvals", {}) or {}
    converged = ret.get("converged", None)
    if converged is True:
        status = "true"
    elif converged is False:
        status = "false"
    else:
        status = "unknown"

    warnflag = ret.get("warnflag", None)
    iterations = ret.get("iterations", None)
    beta = np.asarray(res.params, float)
    coef_finite = bool(np.all(np.isfinite(beta)))
    max_abs_coef = float(np.max(np.abs(beta))) if coef_finite and len(beta) else float("nan")

    try:
        pred = np.asarray(res.predict(Xc), float)
        pred_finite = bool(np.all(np.isfinite(pred)))
        pred_in_range = bool(np.all((pred >= 0.0) & (pred <= 1.0)))
    except Exception:
        pred = np.full(len(y), np.nan)
        pred_finite = False
        pred_in_range = False

    conv_warn = _has_any(warning_messages, CONVERGENCE_WARNING_KEYS)
    num_warn = _has_any(warning_messages, NUMERICAL_WARNING_KEYS)
    warnflag_ok = (warnflag in (0, None))

    reasons = []
    if converged is not True:
        reasons.append("nonconverged" if converged is False else "unknown_convergence")
    if not warnflag_ok:
        reasons.append("nonzero_warnflag")
    if conv_warn:
        reasons.append("convergence_warning")
    if not coef_finite:
        reasons.append("nonfinite_coef")
    if not pred_finite:
        reasons.append("nonfinite_prediction")
    if not pred_in_range:
        reasons.append("prediction_out_of_range")

    accepted = len(reasons) == 0

    return {
        "method": method,
        "converged": converged,
        "converged_status": status,
        "warnflag": warnflag,
        "warnflag_missing": warnflag is None,
        "iterations": int(iterations) if iterations is not None else None,
        "warning_count": len(warning_messages),
        "warnings": _warning_text(warning_messages),
        "convergence_warning": conv_warn,
        "numerical_warning": num_warn,
        "coef_finite": coef_finite,
        "max_abs_coef": max_abs_coef,
        "pred_finite": pred_finite,
        "pred_in_range": pred_in_range,
        "accepted": accepted,
        "rejection_reason": ";".join(reasons),
        "beta": beta if coef_finite else None,
        "train_pred": pred,
        "loglik": float(getattr(res, "llf", np.nan)),
    }


def fit_attempt(X: np.ndarray, y: np.ndarray, method: str, maxiter: int) -> dict[str, Any]:
    Xc = sm.add_constant(X, has_constant="add")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            res = sm.Probit(y, Xc).fit(method=method, maxiter=maxiter, disp=0)
        except Exception as exc:
            return {
                "method": method,
                "converged": None,
                "converged_status": "unknown",
                "warnflag": None,
                "warnflag_missing": True,
                "iterations": None,
                "warning_count": len(caught),
                "warnings": _warning_text(sorted({str(w.message) for w in caught})),
                "convergence_warning": False,
                "numerical_warning": False,
                "coef_finite": False,
                "max_abs_coef": float("nan"),
                "pred_finite": False,
                "pred_in_range": False,
                "accepted": False,
                "rejection_reason": f"exception:{exc!r}",
                "beta": None,
                "train_pred": None,
                "loglik": float("nan"),
                "exception": repr(exc),
            }
    return extract_fit_info(res, Xc, y, method, sorted({str(w.message) for w in caught}))


def audit_inner_fit(X: np.ndarray, y: np.ndarray) -> dict[str, Any]:
    """Newton -> BFGS acceptance-aware fit; no ridge for inner CV."""
    newton = fit_attempt(X, y, "newton", 300)
    existing = newton if newton["beta"] is not None else None
    existing_method = "newton" if existing is not None else None
    bfgs = None
    if existing is None:
        bfgs = fit_attempt(X, y, "bfgs", 300)
        if bfgs["beta"] is not None:
            existing = bfgs
            existing_method = "bfgs"

    audit = None
    audit_method = None
    bfgs_retry = False
    if newton["accepted"]:
        audit = newton
        audit_method = "newton"
    else:
        if bfgs is None:
            bfgs = fit_attempt(X, y, "bfgs", 300)
        bfgs_retry = True
        if bfgs["accepted"]:
            audit = bfgs
            audit_method = "bfgs"

    return {
        "newton": newton,
        "bfgs": bfgs,
        "existing": existing,
        "existing_method": existing_method,
        "audit": audit,
        "audit_method": audit_method,
        "bfgs_retry": bfgs_retry,
    }


def _ridge_info(X: np.ndarray, y: np.ndarray, rp: dict[str, Any]) -> dict[str, Any]:
    beta = np.asarray(rp["beta"], float)
    Xc = sm.add_constant(X, has_constant="add")
    p = np.asarray(norm.cdf(Xc @ beta), float)
    converged = rp.get("converged", None)
    return {
        "method": "ridge_probit_fallback",
        "converged": converged,
        "converged_status": "true" if converged is True else ("false" if converged is False else "unknown"),
        "warnflag": None,
        "warnflag_missing": True,
        "iterations": None,
        "warning_count": 0,
        "warnings": "",
        "convergence_warning": False,
        "numerical_warning": False,
        "coef_finite": bool(np.all(np.isfinite(beta))),
        "max_abs_coef": float(np.max(np.abs(beta))) if np.all(np.isfinite(beta)) else float("nan"),
        "pred_finite": bool(np.all(np.isfinite(p))),
        "pred_in_range": bool(np.all((p >= 0.0) & (p <= 1.0))),
        "accepted": True,
        "rejection_reason": "",
        "beta": beta,
        "train_pred": p,
        "loglik": float(rp.get("loglik", np.nan)),
    }


def audit_outer_fit(X: np.ndarray, y: np.ndarray, x_val: np.ndarray) -> dict[str, Any]:
    """Newton -> BFGS acceptance-aware; ridge only as final documented fallback."""
    newton = fit_attempt(X, y, "newton", 2000)
    existing = newton if newton["beta"] is not None else None
    existing_method = "newton" if existing is not None else None
    bfgs = None
    if existing is None:
        bfgs = fit_attempt(X, y, "bfgs", 2000)
        if bfgs["beta"] is not None:
            existing = bfgs
            existing_method = "bfgs"

    audit = None
    audit_method = None
    bfgs_retry = False
    if newton["accepted"]:
        audit = newton
        audit_method = "newton"
    else:
        if bfgs is None:
            bfgs = fit_attempt(X, y, "bfgs", 2000)
        bfgs_retry = True
        if bfgs["accepted"]:
            audit = bfgs
            audit_method = "bfgs"

    existing_fallback_used = False
    fallback_used = False
    rp_info = None
    if existing is None:
        rp_info = _ridge_info(X, y, ridge_probit(X, y))
        existing = rp_info
        existing_method = "ridge_probit_fallback"
        existing_fallback_used = True
    if audit is None:
        if rp_info is None:
            rp_info = _ridge_info(X, y, ridge_probit(X, y))
        audit = rp_info
        audit_method = "ridge_probit_fallback"
        fallback_used = True

    return {
        "newton": newton,
        "bfgs": bfgs,
        "existing": existing,
        "existing_method": existing_method,
        "audit": audit,
        "audit_method": audit_method,
        "bfgs_retry": bfgs_retry,
        "fallback_used": fallback_used,
        "existing_fallback_used": existing_fallback_used,
    }


def predict_from(info: dict[str, Any] | None, x_val: np.ndarray) -> float:
    if info is None or info.get("beta") is None:
        return float("nan")
    return float(norm.cdf(np.r_[1.0, x_val] @ info["beta"]))


# ---------------------------------------------------------------------------
# Audit execution
# ---------------------------------------------------------------------------

def run_audit(seed: int) -> dict[str, Any]:
    t0 = time.perf_counter()
    np.random.seed(seed)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)

    existing_before = {
        "script": sha256_file(NX_SCRIPT),
        "outputs": tree_hashes(NX_DIR),
    }

    ctx = nx.build_context(seed)
    panel = ctx["panel"]
    features = ctx["features"]
    lpr = ctx["lpr"]
    candidate_cols = ctx["candidate_cols"]
    month_idx = ctx["month_idx"]

    fold_df = pd.read_csv(NX_DIR / "nested_cv_fold_results.csv")
    selection = pd.read_csv(NX_DIR / "outer_rank_selection.csv")
    nested_pred = pd.read_csv(NX_DIR / "nested_cv_predictions.csv").sort_values("target_period").reset_index(drop=True)
    existing_prep = pd.read_csv(NX_DIR / "inner_cv_preprocessing_audit.csv")
    existing_prep["preprocessing_hash"] = existing_prep["preprocessing_hash"].astype("string").fillna("")
    prep_hash_map = {
        (str(r.outer_forecast_origin), str(r.inner_validation_label)): str(r.preprocessing_hash)
        for r in existing_prep.itertuples(index=False)
    }
    existing_outer = pd.read_csv(NX_DIR / "outer_fold_audit.csv")

    fold_df["exclusion_reason"] = fold_df["exclusion_reason"].astype("string").fillna("")
    groups = list(fold_df.groupby(["outer_forecast_origin", "inner_validation_label"], sort=False))
    CONSOLE.print(f"Auditing {len(groups)} inner folds and {len(selection)} outer fits ...")

    inner_rows: list[dict[str, Any]] = []
    outer_rows: list[dict[str, Any]] = []
    rank_compare: list[dict[str, Any]] = []
    group_usable_map: dict[tuple[str, str], bool] = {}
    group_failure_map: dict[tuple[str, str], str] = {}

    # ---- inner audit ------------------------------------------------------
    for (outer, inner_label), g in groups:
        outer = str(outer)
        inner_label = str(inner_label)
        g = g.sort_values("candidate_rank")
        outer_origin_month = pd.Timestamp(outer).strftime("%Y-%m")
        k = int(g["n_inner_train"].iloc[0])
        maxc = int(g["inner_max_components"].iloc[0])
        inner_pred_month = str(g["inner_validation_predictor_month"].iloc[0])

        ft = nx.fold_labels(ctx, outer_origin_month)
        if ft["train_u"][k] != inner_label:
            raise RuntimeError(f"inner label alignment mismatch at {outer} / {inner_label}")
        if panel.iloc[int(ft["s_idx"][k])]["month"] != inner_pred_month:
            raise RuntimeError(f"inner predictor month mismatch at {outer} / {inner_label}")
        if int(ft["s_idx"][k]) in set(ft["s_idx"][:k]):
            raise RuntimeError(f"inner validation row appears in training at {outer} / {inner_label}")
        outer_origin_idx = int(month_idx[outer_origin_month])
        if int(ft["s_idx"][k]) >= outer_origin_idx:
            raise RuntimeError(f"inner validation row reaches the outer test origin at {outer} / {inner_label}")

        X_train_raw = features.iloc[ft["s_idx"][:k]][candidate_cols].to_numpy(dtype=float)
        prep = nx.fit_preprocessor(X_train_raw, candidate_cols)
        if prep is None:
            raise RuntimeError(f"preprocessing failed at {outer} / {inner_label}")
        lpr_train = lpr[ft["s_idx"][:k]]
        mu_l = float(np.nanmean(lpr_train))
        sd_l = float(np.nanstd(lpr_train, ddof=1))
        lpr_z_train = (lpr_train - mu_l) / sd_l
        F_train = nx.transform_matrix(prep, X_train_raw, prep["max_components"])
        x_val_raw = features.iloc[int(ft["s_idx"][k])][candidate_cols].to_numpy(dtype=float)
        f_val = nx.transform_row(prep, x_val_raw, prep["max_components"])
        lpr_z_val = (float(lpr[ft["s_idx"][k]]) - mu_l) / sd_l

        prep_hash = prep["preprocessing_hash"]
        existing_hash = prep_hash_map.get((outer, inner_label), "")
        hash_match = bool(existing_hash and prep_hash == existing_hash)

        saved_for = {int(r.candidate_rank): float(r.validation_brier) for r in g.itertuples(index=False)}
        y = ft["y"]
        group_rows = []
        group_ok = True
        failures = []
        for r in range(1, 9):
            X = np.column_stack([F_train[:, :r], lpr_z_train]) if r <= prep["max_components"] else None
            x_val = np.r_[f_val[:r], lpr_z_val] if r <= prep["max_components"] else None
            if X is None:
                info = {
                    "newton": None, "bfgs": None, "existing": None, "existing_method": None,
                    "audit": None, "audit_method": None, "bfgs_retry": False,
                }
                feasible = False
                failure = "rank_exceeds_inner_max_components"
            else:
                info = audit_inner_fit(X, y[:k])
                feasible = True
                failure = ""
            existing_info = info["existing"]
            audit_info = info["audit"]
            audit_reasons = []
            if audit_info is None:
                if info.get("newton") and info["newton"].get("rejection_reason"):
                    audit_reasons.append("newton:" + info["newton"]["rejection_reason"])
                if info.get("bfgs") and info["bfgs"].get("rejection_reason"):
                    audit_reasons.append("bfgs:" + info["bfgs"]["rejection_reason"])
                if not audit_reasons:
                    audit_reasons.append(failure or "no_accepted_fit")
            audit_rejection_reason = ";".join(audit_reasons)
            existing_pred = predict_from(existing_info, x_val) if x_val is not None else np.nan
            audit_pred = predict_from(audit_info, x_val) if x_val is not None else np.nan
            y_val = float(y[k])
            existing_brier = (existing_pred - y_val) ** 2 if np.isfinite(existing_pred) else np.nan
            audit_brier = (audit_pred - y_val) ** 2 if np.isfinite(audit_pred) else np.nan
            saved_brier = saved_for[int(r)] if int(r) in saved_for else np.nan

            accepted = bool(audit_info is not None and audit_info.get("accepted", False))
            if not feasible:
                accepted = False
            if not accepted:
                group_ok = False
                if failure:
                    failures.append(f"r{r}:{failure}")
                elif audit_info is None:
                    reasons = []
                    if info.get("newton") and info["newton"].get("rejection_reason"):
                        reasons.append("newton:" + info["newton"]["rejection_reason"])
                    if info.get("bfgs") and info["bfgs"].get("rejection_reason"):
                        reasons.append("bfgs:" + info["bfgs"]["rejection_reason"])
                    failures.append(f"r{r}:" + ",".join(reasons))
                else:
                    failures.append(f"r{r}:not_accepted")

            def fields(prefix: str, inf: dict[str, Any] | None) -> dict[str, Any]:
                if inf is None:
                    return {
                        f"{prefix}_method": "",
                        f"{prefix}_converged": "",
                        f"{prefix}_converged_status": "not_attempted",
                        f"{prefix}_warnflag": "",
                        f"{prefix}_iterations": "",
                        f"{prefix}_warning_count": 0,
                        f"{prefix}_warnings": "",
                        f"{prefix}_convergence_warning": False,
                        f"{prefix}_numerical_warning": False,
                        f"{prefix}_coef_finite": False,
                        f"{prefix}_max_abs_coef": np.nan,
                        f"{prefix}_pred_finite": False,
                        f"{prefix}_pred_in_range": False,
                        f"{prefix}_accepted": False,
                        f"{prefix}_rejection_reason": "",
                    }
                return {
                    f"{prefix}_method": inf["method"],
                    f"{prefix}_converged": inf["converged"],
                    f"{prefix}_converged_status": inf["converged_status"],
                    f"{prefix}_warnflag": inf["warnflag"],
                    f"{prefix}_iterations": inf["iterations"],
                    f"{prefix}_warning_count": inf["warning_count"],
                    f"{prefix}_warnings": inf["warnings"],
                    f"{prefix}_convergence_warning": inf["convergence_warning"],
                    f"{prefix}_numerical_warning": inf["numerical_warning"],
                    f"{prefix}_coef_finite": inf["coef_finite"],
                    f"{prefix}_max_abs_coef": inf["max_abs_coef"],
                    f"{prefix}_pred_finite": inf["pred_finite"],
                    f"{prefix}_pred_in_range": inf["pred_in_range"],
                    f"{prefix}_accepted": inf["accepted"],
                    f"{prefix}_rejection_reason": inf["rejection_reason"],
                }

            row = {
                "outer_forecast_origin": outer,
                "outer_target_period": str(g["outer_target_period"].iloc[0]),
                "inner_validation_label": inner_label,
                "inner_validation_predictor_month": inner_pred_month,
                "candidate_rank": int(r),
                "n_inner_train": int(k),
                "n_retained": int(prep["n_retained"]),
                "inner_max_components": int(prep["max_components"]),
                "scaler_pca_fit_end": str(panel.iloc[int(ft["s_idx"][k - 1])]["month"]),
                "validation_row_in_training": False,
                "outer_test_in_inner_cv": False,
                "preprocessing_hash": prep_hash,
                "existing_preprocessing_hash": existing_hash,
                "preprocessing_hash_match": hash_match,
                "existing_brier": existing_brier,
                "existing_saved_brier": saved_brier,
                "existing_brier_diff": existing_brier - saved_brier if np.isfinite(existing_brier) and np.isfinite(saved_brier) else np.nan,
                "audit_brier": audit_brier,
                "audit_brier_diff_vs_existing": audit_brier - existing_brier if np.isfinite(audit_brier) and np.isfinite(existing_brier) else np.nan,
                "audit_pred_prob": audit_pred,
                "validation_target": y_val,
                "bfgs_retry": bool(info["bfgs_retry"]),
                "audit_accepted": accepted,
                "audit_method": info["audit_method"] or "",
                "audit_converged_status": (audit_info["converged_status"] if audit_info else ("unknown" if feasible else "not_attempted")),
                "audit_converged": (audit_info["converged"] if audit_info else ""),
                "audit_warnflag": (audit_info["warnflag"] if audit_info else ""),
                "audit_iterations": (audit_info["iterations"] if audit_info else ""),
                "audit_warning_count": (audit_info["warning_count"] if audit_info else 0),
                "audit_warnings": (audit_info["warnings"] if audit_info else ""),
                "audit_convergence_warning": (audit_info["convergence_warning"] if audit_info else False),
                "audit_numerical_warning": (audit_info["numerical_warning"] if audit_info else False),
                "audit_coef_finite": (audit_info["coef_finite"] if audit_info else False),
                "audit_max_abs_coef": (audit_info["max_abs_coef"] if audit_info else np.nan),
                "audit_pred_finite": (audit_info["pred_finite"] if audit_info else False),
                "audit_pred_in_range": (audit_info["pred_in_range"] if audit_info else False),
                "audit_rejection_reason": audit_rejection_reason,
            }
            row.update(fields("existing", existing_info))
            row.update(fields("newton", info.get("newton")))
            row.update(fields("bfgs", info.get("bfgs")))
            group_rows.append(row)

        for row in group_rows:
            row["group_usable"] = bool(group_ok)
            row["group_failure_reason"] = ";".join(failures)
        group_usable_map[(outer, inner_label)] = bool(group_ok)
        group_failure_map[(outer, inner_label)] = ";".join(failures)
        inner_rows.extend(group_rows)

    inner_df = pd.DataFrame(inner_rows)

    # ---- per-outer audit rank selection ----------------------------------
    for row in selection.itertuples(index=False):
        outer = str(row.outer_forecast_origin)
        target = str(row.outer_target_period)
        subgroup = inner_df[(inner_df["outer_forecast_origin"] == outer) & (inner_df["group_usable"])]
        existing_sub = inner_df[inner_df["outer_forecast_origin"] == outer]
        groups_all = int(existing_sub[["inner_validation_label"]].drop_duplicates().shape[0])
        groups_used = int(subgroup[["inner_validation_label"]].drop_duplicates().shape[0])
        audit_means = {}
        existing_on_common = {}
        for r in range(1, 9):
            sr = subgroup[subgroup["candidate_rank"] == r]
            er = existing_sub[(existing_sub["candidate_rank"] == r) & (existing_sub["group_usable"])]
            audit_means[r] = float(sr["audit_brier"].mean()) if len(sr) and sr["audit_brier"].notna().all() else np.nan
            existing_on_common[r] = float(er["existing_saved_brier"].mean()) if len(er) else np.nan
        valid = {r: v for r, v in audit_means.items() if np.isfinite(v)}
        audit_selected = int(min(valid.items(), key=lambda kv: (kv[1], kv[0]))[0]) if valid else int(row.selected_rank)
        existing_selected = int(row.selected_rank)
        rank_compare.append({
            "outer_forecast_origin": outer,
            "outer_target_period": target,
            "existing_selected_rank": existing_selected,
            "audit_selected_rank": audit_selected,
            "rank_changed": bool(audit_selected != existing_selected),
            "groups_all": groups_all,
            "groups_usable": groups_used,
            "common_set_reduced": bool(groups_used != groups_all),
            "existing_mean_brier_selected": float(row.inner_brier_selected) if np.isfinite(row.inner_brier_selected) else np.nan,
            "audit_mean_brier_selected": audit_means.get(audit_selected, np.nan),
            "audit_mean_brier_existing_rank": audit_means.get(existing_selected, np.nan),
            **{f"existing_saved_mean_r{r}": existing_on_common[r] for r in range(1, 9)},
            **{f"audit_mean_r{r}": audit_means[r] for r in range(1, 9)},
        })

    rank_compare_df = pd.DataFrame(rank_compare)

    # ---- outer audit ------------------------------------------------------
    nested_by_target = nested_pred.set_index("target_period")
    existing_outer_by_origin = existing_outer.set_index("outer_forecast_origin")
    for row in selection.itertuples(index=False):
        outer = str(row.outer_forecast_origin)
        target = str(row.outer_target_period)
        selected_rank = int(row.selected_rank)
        origin_month = pd.Timestamp(outer).strftime("%Y-%m")
        ft = nx.fold_labels(ctx, origin_month)
        X_outer_raw = features.iloc[ft["s_idx"]][candidate_cols].to_numpy(dtype=float)
        prep = nx.fit_preprocessor(X_outer_raw, candidate_cols)
        if prep is None:
            raise RuntimeError(f"outer preprocessing failed at {outer}")
        if selected_rank > prep["max_components"]:
            raise RuntimeError(f"selected rank exceeds outer max components at {outer}")
        lpr_train = lpr[ft["s_idx"]]
        mu_l = float(np.nanmean(lpr_train))
        sd_l = float(np.nanstd(lpr_train, ddof=1))
        lpr_z = (lpr_train - mu_l) / sd_l
        F_outer = nx.transform_matrix(prep, X_outer_raw, selected_rank)
        X = np.column_stack([F_outer, lpr_z])
        t_idx = int(month_idx[origin_month])
        x_test_raw = features.iloc[t_idx][candidate_cols].to_numpy(dtype=float)
        f_test = nx.transform_row(prep, x_test_raw, selected_rank)
        x_val = np.r_[f_test, (float(lpr[t_idx]) - mu_l) / sd_l]
        y = ft["y"]
        if t_idx in set(ft["s_idx"]):
            raise RuntimeError(f"outer test row in outer training at {outer}")

        info = audit_outer_fit(X, y, x_val)
        existing_info = info["existing"]
        audit_info = info["audit"]
        existing_pred = predict_from(existing_info, x_val)
        audit_pred = predict_from(audit_info, x_val)
        saved_pred = float(nested_by_target.loc[target, "y_pred_proba"])

        def fields(prefix: str, inf: dict[str, Any] | None) -> dict[str, Any]:
            if inf is None:
                return {
                    f"{prefix}_method": "", f"{prefix}_converged": "",
                    f"{prefix}_converged_status": "not_attempted", f"{prefix}_warnflag": "",
                    f"{prefix}_iterations": "", f"{prefix}_warning_count": 0,
                    f"{prefix}_warnings": "", f"{prefix}_convergence_warning": False,
                    f"{prefix}_numerical_warning": False, f"{prefix}_coef_finite": False,
                    f"{prefix}_max_abs_coef": np.nan, f"{prefix}_pred_finite": False,
                    f"{prefix}_pred_in_range": False, f"{prefix}_accepted": False,
                    f"{prefix}_rejection_reason": "",
                }
            return {
                f"{prefix}_method": inf["method"], f"{prefix}_converged": inf["converged"],
                f"{prefix}_converged_status": inf["converged_status"],
                f"{prefix}_warnflag": inf["warnflag"], f"{prefix}_iterations": inf["iterations"],
                f"{prefix}_warning_count": inf["warning_count"], f"{prefix}_warnings": inf["warnings"],
                f"{prefix}_convergence_warning": inf["convergence_warning"],
                f"{prefix}_numerical_warning": inf["numerical_warning"],
                f"{prefix}_coef_finite": inf["coef_finite"], f"{prefix}_max_abs_coef": inf["max_abs_coef"],
                f"{prefix}_pred_finite": inf["pred_finite"], f"{prefix}_pred_in_range": inf["pred_in_range"],
                f"{prefix}_accepted": inf["accepted"], f"{prefix}_rejection_reason": inf["rejection_reason"],
            }

        outer_row = {
            "outer_forecast_origin": outer,
            "outer_target_period": target,
            "selected_rank": selected_rank,
            "n_outer_train": int(len(y)),
            "n_retained_outer": int(prep["n_retained"]),
            "outer_max_components": int(prep["max_components"]),
            "validation_row_in_training": False,
            "outer_test_in_training": False,
            "preprocessing_hash": prep["preprocessing_hash"],
            "existing_prediction": existing_pred,
            "audit_prediction": audit_pred,
            "existing_saved_prediction": saved_pred,
            "existing_prediction_diff": existing_pred - saved_pred,
            "audit_prediction_diff_vs_saved": audit_pred - saved_pred,
            "bfgs_retry": bool(info["bfgs_retry"]),
            "ridge_fallback": bool(info["fallback_used"]),
            "existing_fallback_used": bool(info.get("existing_fallback_used", False)),
            "audit_accepted": bool(audit_info is not None),
            "audit_method": info["audit_method"] or "",
            "audit_converged_status": audit_info["converged_status"] if audit_info else "unknown",
            "audit_converged": audit_info["converged"] if audit_info else "",
            "audit_warnflag": audit_info["warnflag"] if audit_info else "",
            "audit_iterations": audit_info["iterations"] if audit_info else "",
            "audit_warning_count": audit_info["warning_count"] if audit_info else 0,
            "audit_warnings": audit_info["warnings"] if audit_info else "",
            "audit_convergence_warning": audit_info["convergence_warning"] if audit_info else False,
            "audit_numerical_warning": audit_info["numerical_warning"] if audit_info else False,
            "audit_coef_finite": audit_info["coef_finite"] if audit_info else False,
            "audit_max_abs_coef": audit_info["max_abs_coef"] if audit_info else np.nan,
            "audit_pred_finite": audit_info["pred_finite"] if audit_info else False,
            "audit_pred_in_range": audit_info["pred_in_range"] if audit_info else False,
            "audit_rejection_reason": audit_info["rejection_reason"] if audit_info else "",
        }
        outer_row.update(fields("existing", existing_info))
        outer_row.update(fields("newton", info.get("newton")))
        outer_row.update(fields("bfgs", info.get("bfgs")))
        if outer in existing_outer_by_origin.index:
            er = existing_outer_by_origin.loc[outer]
            outer_row["existing_recorded_method"] = er["outer_fit_method"]
            outer_row["existing_recorded_converged"] = er["outer_fit_converged"]
            outer_row["existing_recorded_warnflag"] = er["outer_fit_warnflag"]
        outer_rows.append(outer_row)

    outer_df = pd.DataFrame(outer_rows)

    # ---- write outputs ---------------------------------------------------
    inner_df.to_csv(AUDIT_DIR / "inner_optimizer_audit.csv", index=False)
    outer_df.to_csv(AUDIT_DIR / "outer_optimizer_audit.csv", index=False)
    rank_compare_df.to_csv(AUDIT_DIR / "rank_selection_comparison.csv", index=False)

    def count_status(df: pd.DataFrame, prefix: str, col: str) -> dict[str, int]:
        series = df[f"{prefix}_{col}"].astype("string").fillna("")
        if col == "method":
            series = series.replace("", "none")
        else:
            series = series.replace("", "unknown")
        return {str(k): int(v) for k, v in series.value_counts().sort_index().items()}

    def method_status(df: pd.DataFrame, prefix: str) -> dict[str, dict[str, int]]:
        tmp = pd.DataFrame({
            "method": df[f"{prefix}_method"].astype("string").fillna("").replace("", "none"),
            "status": df[f"{prefix}_converged_status"].astype("string").fillna("unknown").str.lower(),
        })
        return {
            str(m): {str(s): int(c) for s, c in g["status"].value_counts().items()}
            for m, g in tmp.groupby("method")
        }

    def reason_counts(df: pd.DataFrame, mask: pd.Series, col: str) -> dict[str, int]:
        vals = df.loc[mask, col].astype("string").fillna("")
        vals = vals.replace("", "none")
        return {str(k): int(v) for k, v in vals.value_counts().items()}

    existing_before_after = {
        "script_before": existing_before["script"],
        "script_after": sha256_file(NX_SCRIPT),
        "outputs_before": existing_before["outputs"],
        "outputs_after": tree_hashes(NX_DIR),
    }
    existing_unchanged = (
        existing_before_after["script_before"] == existing_before_after["script_after"]
        and existing_before_after["outputs_before"] == existing_before_after["outputs_after"]
    )

    inner_existing_accept = int(inner_df["existing_accepted"].sum())
    inner_audit_accept = int(inner_df["audit_accepted"].sum())
    inner_groups = int(inner_df[["outer_forecast_origin", "inner_validation_label"]].drop_duplicates().shape[0])
    inner_usable_groups = int(inner_df[inner_df["group_usable"]][["outer_forecast_origin", "inner_validation_label"]].drop_duplicates().shape[0])
    outer_existing_accept = int(outer_df["existing_accepted"].sum())
    outer_audit_accept = int(outer_df["audit_accepted"].sum())
    existing_valid = bool(
        inner_existing_accept == len(inner_df)
        and outer_existing_accept == len(outer_df)
        and (inner_df["existing_converged_status"] == "true").all()
        and (outer_df["existing_converged_status"] == "true").all()
        and inner_usable_groups == inner_groups
        and float(outer_df["existing_prediction_diff"].abs().max()) < 1e-10
    )

    summary = {
        "audit_task": "optimizer_convergence_audit_strictly_nested_probit",
        "seed": seed,
        "inner": {
            "folds_total": inner_groups,
            "folds_usable_under_audit": inner_usable_groups,
            "folds_unusable": inner_groups - inner_usable_groups,
            "candidate_fits_total": int(len(inner_df)),
            "candidate_fits_expected": int(inner_groups * 8),
            "existing_method_counts": count_status(inner_df, "existing", "method"),
            "existing_converged_status_counts": count_status(inner_df, "existing", "converged_status"),
            "existing_warnflag_nonzero": int((pd.to_numeric(inner_df["existing_warnflag"], errors="coerce").fillna(0) != 0).sum()),
            "existing_fits_with_warnings": int((inner_df["existing_warning_count"] > 0).sum()),
            "existing_warning_messages_total": int(inner_df["existing_warning_count"].sum()),
            "existing_convergence_warnings": int(inner_df["existing_convergence_warning"].sum()),
            "existing_numerical_warnings": int(inner_df["existing_numerical_warning"].sum()),
            "existing_by_method_status": method_status(inner_df, "existing"),
            "existing_rejection_reason_counts": reason_counts(inner_df, ~inner_df["existing_accepted"], "existing_rejection_reason"),
            "existing_accepted_by_rule": inner_existing_accept,
            "existing_rejected_by_rule": int(len(inner_df) - inner_existing_accept),
            "newton_accepted": int(inner_df["newton_accepted"].sum()),
            "newton_rejected": int(len(inner_df) - inner_df["newton_accepted"].sum()),
            "newton_rejection_reason_counts": reason_counts(inner_df, ~inner_df["newton_accepted"], "newton_rejection_reason"),
            "newton_fits_with_warnings": int((inner_df["newton_warning_count"] > 0).sum()),
            "bfgs_retry_count": int(inner_df["bfgs_retry"].sum()),
            "bfgs_attempted": int(inner_df["bfgs_method"].astype("string").fillna("").ne("").sum()),
            "bfgs_accepted": int(inner_df["bfgs_accepted"].sum()),
            "bfgs_rejection_reason_counts": reason_counts(
                inner_df,
                inner_df["bfgs_method"].astype("string").fillna("").ne("") & ~inner_df["bfgs_accepted"],
                "bfgs_rejection_reason",
            ),
            "bfgs_fits_with_warnings": int((inner_df["bfgs_warning_count"] > 0).sum()),
            "audit_method_counts": count_status(inner_df, "audit", "method"),
            "audit_accepted": inner_audit_accept,
            "audit_rejected": int(len(inner_df) - inner_audit_accept),
            "audit_unknown_status": int((inner_df["audit_converged_status"] == "unknown").sum()),
            "audit_accepted_fits_with_warnings": int((inner_df["audit_warning_count"] > 0).sum()),
            "audit_numerical_warnings": int(inner_df["audit_numerical_warning"].sum()),
            "audit_convergence_warnings": int(inner_df["audit_convergence_warning"].sum()),
            "audit_rejection_reason_counts": reason_counts(inner_df, ~inner_df["audit_accepted"], "audit_rejection_reason"),
        },
        "outer": {
            "fits_total": int(len(outer_df)),
            "existing_method_counts": count_status(outer_df, "existing", "method"),
            "existing_converged_status_counts": count_status(outer_df, "existing", "converged_status"),
            "existing_warnflag_nonzero": int((pd.to_numeric(outer_df["existing_warnflag"], errors="coerce").fillna(0) != 0).sum()),
            "existing_fits_with_warnings": int((outer_df["existing_warning_count"] > 0).sum()),
            "existing_warning_messages_total": int(outer_df["existing_warning_count"].sum()),
            "existing_convergence_warnings": int(outer_df["existing_convergence_warning"].sum()),
            "existing_numerical_warnings": int(outer_df["existing_numerical_warning"].sum()),
            "existing_by_method_status": method_status(outer_df, "existing"),
            "existing_accepted_by_rule": outer_existing_accept,
            "existing_rejected_by_rule": int(len(outer_df) - outer_existing_accept),
            "existing_ridge_fallbacks": int(outer_df["existing_fallback_used"].sum()),
            "newton_accepted": int(outer_df["newton_accepted"].sum()),
            "bfgs_retry_count": int(outer_df["bfgs_retry"].sum()),
            "bfgs_attempted": int(outer_df["bfgs_method"].astype("string").fillna("").ne("").sum()),
            "bfgs_accepted": int(outer_df["bfgs_accepted"].sum()),
            "ridge_fallbacks": int(outer_df["ridge_fallback"].sum()),
            "audit_method_counts": count_status(outer_df, "audit", "method"),
            "audit_accepted": outer_audit_accept,
            "audit_rejected": int(len(outer_df) - outer_audit_accept),
            "audit_unknown_status": int((outer_df["audit_converged_status"] == "unknown").sum()),
            "audit_accepted_fits_with_warnings": int((outer_df["audit_warning_count"] > 0).sum()),
            "audit_numerical_warnings": int(outer_df["audit_numerical_warning"].sum()),
            "audit_convergence_warnings": int(outer_df["audit_convergence_warning"].sum()),
            "existing_prediction_max_abs_diff": float(outer_df["existing_prediction_diff"].abs().max()),
            "audit_prediction_max_abs_diff_vs_saved": float(outer_df["audit_prediction_diff_vs_saved"].abs().max()),
        },
        "group_common_set": {
            "inner_groups_total": inner_groups,
            "inner_groups_usable": inner_usable_groups,
            "common_set_reduced": bool(inner_usable_groups != inner_groups),
            "outer_folds_with_reduced_common_set": int(rank_compare_df["common_set_reduced"].sum()),
            "outer_folds_with_rank_change": int(rank_compare_df["rank_changed"].sum()),
            "rank_selection_unchanged": bool((rank_compare_df["audit_selected_rank"] == rank_compare_df["existing_selected_rank"]).all()),
            "mean_abs_change_in_selected_inner_brier": float(
                (rank_compare_df["audit_mean_brier_selected"] - rank_compare_df["existing_mean_brier_selected"]).abs().mean()
            ),
        },
        "existing_results_valid": existing_valid,
        "corrected_experiment_required": bool(not existing_valid),
        "corrected_experiment_created": False,
        "existing_files_unchanged": bool(existing_unchanged),
        "existing_hashes": existing_before_after,
        "audit_elapsed_seconds": time.perf_counter() - t0,
        "outputs": {
            "inner": "results/approach1/probit_nested_cv_pca_optimizer_audit/inner_optimizer_audit.csv",
            "outer": "results/approach1/probit_nested_cv_pca_optimizer_audit/outer_optimizer_audit.csv",
            "summary": "results/approach1/probit_nested_cv_pca_optimizer_audit/audit_summary.json",
            "validation": "results/approach1/probit_nested_cv_pca_optimizer_audit/validation_report.json",
            "note": "results/approach1/probit_nested_cv_pca_optimizer_audit/diagnostic_note.md",
        },
    }
    write_json(AUDIT_DIR / "audit_summary.json", summary)

    manifest = {
        "seed": seed,
        "hashes": {
            "inner_optimizer_audit.csv": sha256_file(AUDIT_DIR / "inner_optimizer_audit.csv"),
            "outer_optimizer_audit.csv": sha256_file(AUDIT_DIR / "outer_optimizer_audit.csv"),
            "rank_selection_comparison.csv": sha256_file(AUDIT_DIR / "rank_selection_comparison.csv"),
        },
        "existing_before": existing_before,
    }
    write_json(AUDIT_DIR / "audit_manifest.json", manifest)

    validation = validate_audit(seed, manifest)
    write_note(summary, rank_compare_df, validation)

    tbl = Table(title="Nested-Probit optimizer audit", show_lines=False)
    tbl.add_column("Item", style="bold")
    tbl.add_column("Value", justify="right")
    tbl.add_row("Inner candidate fits", str(len(inner_df)))
    tbl.add_row("Inner folds", str(inner_groups))
    tbl.add_row("Inner existing accepted", f"{inner_existing_accept}/{len(inner_df)}")
    tbl.add_row("Inner audit accepted", f"{inner_audit_accept}/{len(inner_df)}")
    tbl.add_row("BFGS retries (inner)", str(summary["inner"]["bfgs_retry_count"]))
    tbl.add_row("Outer fits", str(len(outer_df)))
    tbl.add_row("Outer existing accepted", f"{outer_existing_accept}/{len(outer_df)}")
    tbl.add_row("Outer audit accepted", f"{outer_audit_accept}/{len(outer_df)}")
    tbl.add_row("Outer ridge fallbacks", str(summary["outer"]["ridge_fallbacks"]))
    tbl.add_row("Existing results valid", str(existing_valid))
    tbl.add_row("Existing files unchanged", str(existing_unchanged))
    tbl.add_row("Validation checks", f"{validation['n_passed']}/{validation['n_checks']}")
    CONSOLE.print(tbl)
    CONSOLE.print(f"[green]Audit complete[/green] | {time.perf_counter() - t0:.1f}s")
    return {"summary": summary, "validation": validation}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_audit(seed: int, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: Any = None) -> None:
        checks.append({"check": name, "passed": bool(passed), "detail": detail})

    inner = pd.read_csv(AUDIT_DIR / "inner_optimizer_audit.csv")
    outer = pd.read_csv(AUDIT_DIR / "outer_optimizer_audit.csv")
    rcomp = pd.read_csv(AUDIT_DIR / "rank_selection_comparison.csv")
    nested = pd.read_csv(NX_DIR / "nested_cv_predictions.csv").sort_values("target_period").reset_index(drop=True)
    sel = pd.read_csv(NX_DIR / "outer_rank_selection.csv")

    groups = inner.groupby(["outer_forecast_origin", "inner_validation_label"]).size()
    def norm_status(series: pd.Series) -> pd.Series:
        return series.astype("string").str.lower().fillna("unknown")

    add("inner: 35,752 candidate fits", len(inner) == 35752, len(inner))
    add("inner: 4,469 folds", len(groups) == 4469, len(groups))
    add("inner: exactly 8 candidate ranks per fold", bool((groups == 8).all()), groups.value_counts().to_dict())
    add("inner: ranks 1..8 present", set(inner["candidate_rank"].unique()) == set(range(1, 9)), sorted(inner["candidate_rank"].unique().tolist()))
    add("inner: statuses classified", set(norm_status(inner["existing_converged_status"]).unique()) <= {"true", "false", "unknown", "not_attempted"}, None)
    add("inner: audit statuses classified", set(norm_status(inner["audit_converged_status"]).unique()) <= {"true", "false", "unknown", "not_attempted"}, None)
    add("inner: no validation row in training", not bool(inner["validation_row_in_training"].any()), None)
    add("inner: no outer test in inner CV", not bool(inner["outer_test_in_inner_cv"].any()), None)
    add("inner: preprocessing hash matches saved for every fold", bool(inner["preprocessing_hash_match"].all()), int((~inner["preprocessing_hash_match"]).sum()))
    add("inner: common set defined per fold", bool((inner.groupby(["outer_forecast_origin", "inner_validation_label"])["group_usable"].nunique() == 1).all()), None)

    add("outer: 82 fits", len(outer) == 82, len(outer))
    add("outer: 82 unique target periods", outer["outer_target_period"].nunique() == 82, outer["outer_target_period"].nunique())
    add("outer: targets match nested predictions", set(outer["outer_target_period"]) == set(nested["target_period"]), None)
    add("outer: origins match nested predictions", set(outer["outer_forecast_origin"]) == set(nested["forecast_origin"]), None)
    add("outer: no outer test in training", not bool(outer["outer_test_in_training"].any()), None)
    add("outer: statuses classified", set(norm_status(outer["existing_converged_status"]).unique()) <= {"true", "false", "unknown", "not_attempted"}, None)
    add("outer: audit statuses classified", set(norm_status(outer["audit_converged_status"]).unique()) <= {"true", "false", "unknown", "not_attempted"}, None)
    add("rank comparison: 82 rows", len(rcomp) == 82, len(rcomp))
    add("rank comparison: all outer origins match", set(rcomp["outer_forecast_origin"]) == set(sel["outer_forecast_origin"]), None)
    add("rank comparison: existing ranks preserved", bool((rcomp["existing_selected_rank"].astype(int) == sel["selected_rank"].astype(int)).all()), None)

    # Independent recomputation of a deterministic sample of inner and outer fits.
    ctx = nx.build_context(seed)
    sample_groups = inner[inner["group_usable"]][["outer_forecast_origin", "inner_validation_label"]].drop_duplicates().iloc[::900].head(5)
    n_inner_sample = 0
    n_inner_match = 0
    for row in sample_groups.itertuples(index=False):
        outer_origin = str(row.outer_forecast_origin)
        inner_label = str(row.inner_validation_label)
        sub = inner[(inner["outer_forecast_origin"] == outer_origin) & (inner["inner_validation_label"] == inner_label)]
        k = int(sub["n_inner_train"].iloc[0])
        origin_month = pd.Timestamp(outer_origin).strftime("%Y-%m")
        ft = nx.fold_labels(ctx, origin_month)
        X_raw = ctx["features"].iloc[ft["s_idx"][:k]][ctx["candidate_cols"]].to_numpy(dtype=float)
        prep = nx.fit_preprocessor(X_raw, ctx["candidate_cols"])
        lpr_train = ctx["lpr"][ft["s_idx"][:k]]
        mu_l = float(np.nanmean(lpr_train))
        sd_l = float(np.nanstd(lpr_train, ddof=1))
        z_train = (lpr_train - mu_l) / sd_l
        F_train = nx.transform_matrix(prep, X_raw, prep["max_components"])
        x_val_raw = ctx["features"].iloc[int(ft["s_idx"][k])][ctx["candidate_cols"]].to_numpy(dtype=float)
        f_val = nx.transform_row(prep, x_val_raw, prep["max_components"])
        z_val = (float(ctx["lpr"][ft["s_idx"][k]]) - mu_l) / sd_l
        for r in range(1, 9):
            saved_row = sub[sub["candidate_rank"] == r]
            if saved_row.empty or r > prep["max_components"]:
                continue
            X = np.column_stack([F_train[:, :r], z_train])
            info = audit_inner_fit(X, ft["y"][:k])
            pred = predict_from(info["audit"], np.r_[f_val[:r], z_val])
            b = (pred - float(ft["y"][k])) ** 2
            saved_b = float(saved_row["audit_brier"].iloc[0])
            saved_acc = bool(saved_row["audit_accepted"].iloc[0])
            n_inner_sample += 1
            if abs(b - saved_b) < 1e-10 and bool(info["audit"] is not None) == saved_acc:
                n_inner_match += 1

    sample_outer = outer.iloc[::17].head(5)
    n_outer_sample = 0
    n_outer_match = 0
    for row in sample_outer.itertuples(index=False):
        outer_origin = str(row.outer_forecast_origin)
        selected_rank = int(row.selected_rank)
        origin_month = pd.Timestamp(outer_origin).strftime("%Y-%m")
        ft = nx.fold_labels(ctx, origin_month)
        X_outer_raw = ctx["features"].iloc[ft["s_idx"]][ctx["candidate_cols"]].to_numpy(dtype=float)
        prep = nx.fit_preprocessor(X_outer_raw, ctx["candidate_cols"])
        lpr_train = ctx["lpr"][ft["s_idx"]]
        mu_l = float(np.nanmean(lpr_train))
        sd_l = float(np.nanstd(lpr_train, ddof=1))
        z = (lpr_train - mu_l) / sd_l
        F_outer = nx.transform_matrix(prep, X_outer_raw, selected_rank)
        X = np.column_stack([F_outer, z])
        t_idx = int(ctx["month_idx"][origin_month])
        x_test_raw = ctx["features"].iloc[t_idx][ctx["candidate_cols"]].to_numpy(dtype=float)
        f_test = nx.transform_row(prep, x_test_raw, selected_rank)
        x_val = np.r_[f_test, (float(ctx["lpr"][t_idx]) - mu_l) / sd_l]
        info = audit_outer_fit(X, ft["y"], x_val)
        pred = predict_from(info["audit"], x_val)
        saved_pred = float(row.audit_prediction)
        saved_acc = bool(row.audit_accepted)
        n_outer_sample += 1
        if abs(pred - saved_pred) < 1e-10 and bool(info["audit"] is not None) == saved_acc:
            n_outer_match += 1
    add("independent sample: inner fits reproduced", n_inner_sample > 0 and n_inner_sample == n_inner_match,
        [n_inner_match, n_inner_sample])
    add("independent sample: outer fits reproduced", n_outer_sample > 0 and n_outer_sample == n_outer_match,
        [n_outer_match, n_outer_sample])

    # Determinism and existing-file integrity
    if manifest is None:
        mpath = AUDIT_DIR / "audit_manifest.json"
        manifest = json.loads(mpath.read_text(encoding="utf-8")) if mpath.exists() else None
    if manifest:
        current = {
            "inner_optimizer_audit.csv": sha256_file(AUDIT_DIR / "inner_optimizer_audit.csv"),
            "outer_optimizer_audit.csv": sha256_file(AUDIT_DIR / "outer_optimizer_audit.csv"),
            "rank_selection_comparison.csv": sha256_file(AUDIT_DIR / "rank_selection_comparison.csv"),
        }
        add("determinism: audit output hashes match manifest", current == manifest["hashes"], [current, manifest["hashes"]])
        existing_now = {
            "script": sha256_file(NX_SCRIPT),
            "outputs": tree_hashes(NX_DIR),
        }
        add("existing: nested script and outputs unchanged", existing_now == manifest["existing_before"], None)

    payload = {
        "n_checks": len(checks),
        "n_passed": int(sum(c["passed"] for c in checks)),
        "n_failed": int(sum(not c["passed"] for c in checks)),
        "all_passed": all(c["passed"] for c in checks),
        "checks": checks,
        "existing_hashes_now": {
            "script": sha256_file(NX_SCRIPT),
            "outputs": tree_hashes(NX_DIR),
        },
    }
    write_json(AUDIT_DIR / "validation_report.json", payload)
    return payload


# ---------------------------------------------------------------------------
# Diagnostic note
# ---------------------------------------------------------------------------

def write_note(summary: dict[str, Any], rank_compare: pd.DataFrame, validation: dict[str, Any]) -> None:
    i = summary["inner"]
    o = summary["outer"]
    lines = [
        "# Optimizer-convergence audit - strictly nested Probit",
        "",
        "This file is generated by `code/approach1_probit_nested_cv_pca_optimizer_audit.py`. "
        "It is an audit only: no corrected model or forecast file was created.",
        "",
        "## Method",
        "",
        "All 4,469 used inner validation folds and all 8 candidate ranks were re-fitted from the "
        "existing fold definitions. Each fit tried Newton first, then BFGS if Newton did not meet the "
        "acceptance rule. Acceptance required `mle_retvals['converged'] is True`, no non-zero warnflag, "
        "finite coefficients, finite probabilities in [0,1], and no convergence warning. Numerical "
        "warnings were recorded separately. No ridge regularization was used in inner CV. A fold was "
        "usable only if all 8 candidate ranks were accepted. The 82 outer fits used the existing selected "
        "rank; ridge was used only if both Newton and BFGS failed.",
        "",
        "## Inner fits",
        "",
        f"- Candidate fits: {i['candidate_fits_total']} (expected {i['candidate_fits_expected']}).",
        f"- Inner folds: {i['folds_total']}; usable under audit: {i['folds_usable_under_audit']}; "
        f"unusable: {i['folds_unusable']}.",
        f"- Existing internal method counts: {i['existing_method_counts']}.",
        f"- Existing convergence status: {i['existing_converged_status_counts']}.",
        f"- Existing fits accepted by the audit rule: {i['existing_accepted_by_rule']}/{i['candidate_fits_total']}.",
        f"- Existing fits with warnings: {i['existing_fits_with_warnings']} "
        f"(convergence {i['existing_convergence_warnings']}, numerical {i['existing_numerical_warnings']}).",
        f"- Newton accepted: {i['newton_accepted']}; Newton rejected: {i['newton_rejected']}; "
        f"BFGS retries: {i['bfgs_retry_count']}.",
        f"- Audit method counts: {i['audit_method_counts']}; audit accepted: {i['audit_accepted']}; "
        f"audit rejected: {i['audit_rejected']}; unknown audit status: {i['audit_unknown_status']}.",
        f"- Fits with numerical warnings: {i['audit_numerical_warnings']}; with convergence warnings: "
        f"{i['audit_convergence_warnings']}.",
        f"- Existing rejection reasons: {i['existing_rejection_reason_counts']}.",
        f"- Newton rejection reasons: {i['newton_rejection_reason_counts']}.",
        f"- BFGS rejection reasons: {i['bfgs_rejection_reason_counts']}.",
        "",
        "## Outer fits",
        "",
        f"- Fits: {o['fits_total']}.",
        f"- Existing method counts: {o['existing_method_counts']}.",
        f"- Existing convergence status: {o['existing_converged_status_counts']}.",
        f"- Existing fits accepted by the audit rule: {o['existing_accepted_by_rule']}/{o['fits_total']}.",
        f"- Existing fits with warnings: {o['existing_fits_with_warnings']} "
        f"(convergence {o['existing_convergence_warnings']}, numerical {o['existing_numerical_warnings']}).",
        f"- Newton accepted: {o['newton_accepted']}; BFGS retries: {o['bfgs_retry_count']}; "
        f"audit accepted: {o['audit_accepted']}; audit ridge fallbacks: {o['ridge_fallbacks']}; "
        f"existing ridge fallbacks: {o['existing_ridge_fallbacks']}.",
        f"- Existing prediction max abs diff vs saved: {o['existing_prediction_max_abs_diff']:.3e}.",
        f"- Audit prediction max abs diff vs saved: {o['audit_prediction_max_abs_diff_vs_saved']:.3e}.",
        "",
        "## Rank selection",
        "",
        f"- Outer folds with a reduced common inner-fold set: "
        f"{summary['group_common_set']['outer_folds_with_reduced_common_set']}.",
        f"- Outer folds where the audit-only rank would change: "
        f"{summary['group_common_set']['outer_folds_with_rank_change']}.",
        f"- Audit rank selection unchanged: {summary['group_common_set']['rank_selection_unchanged']}; "
        f"mean absolute change in selected inner Brier: "
        f"{summary['group_common_set']['mean_abs_change_in_selected_inner_brier']:.6f}.",
        "",
        "Audit-only rank comparison is in `rank_selection_comparison.csv`. It is diagnostic evidence, "
        "not a corrected forecast file.",
        "",
        "## Conclusion",
        "",
        f"- Existing nested-CV results valid: **{summary['existing_results_valid']}**.",
        f"- Corrected experiment required: **{summary['corrected_experiment_required']}**.",
        f"- Corrected experiment created: **{summary['corrected_experiment_created']}** "
        "(not created, per the audit-only instruction).",
        f"- Existing files unchanged: **{summary['existing_files_unchanged']}**.",
        f"- Validation: {validation['n_passed']}/{validation['n_checks']} checks passed; "
        f"all passed: {validation['all_passed']}.",
        "",
        "If the existing results are not valid, the exact failures are recorded in "
        "`inner_optimizer_audit.csv` and `outer_optimizer_audit.csv`. Any correction must be proposed "
        "and explicitly approved separately.",
        "",
        "## Reproduction",
        "",
        "```bash",
        "uv run python code/approach1_probit_nested_cv_pca_optimizer_audit.py --seed 42",
        "uv run python code/approach1_probit_nested_cv_pca_optimizer_audit.py --validate-only --seed 42",
        "```",
    ]
    (AUDIT_DIR / "diagnostic_note.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Optimizer audit of the strictly nested Probit")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--validate-only", action="store_true")
    args = p.parse_args(argv)

    if args.validate_only:
        val = validate_audit(args.seed, manifest=None)
        tbl = Table(title="Optimizer audit validation")
        tbl.add_column("Checks", justify="right")
        tbl.add_column("Passed", justify="right")
        tbl.add_column("Failed", justify="right")
        tbl.add_row(str(val["n_checks"]), str(val["n_passed"]), str(val["n_failed"]))
        CONSOLE.print(tbl)
        CONSOLE.print("[green]Validation passed.[/green]" if val["all_passed"] else "[red]Validation failed.[/red]")
        return 0 if val["all_passed"] else 1

    out = run_audit(args.seed)
    return 0 if out["validation"]["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

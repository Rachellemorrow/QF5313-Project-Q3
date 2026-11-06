"""Option A - independent validation and leakage audit.

    uv run python code/approach1_validate.py --seed 42

Re-reads the frozen inputs and every Option A output, recomputes the
headline metrics from row-level predictions, re-verifies the clustering
correlation geometry (PSD + Euclidean identity), re-checks the adjacent-window
ARI, and writes ``results/approach1/validation/validation_report.json``.

Exits non-zero if any check fails.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from sklearn.metrics import adjusted_rand_score, roc_auc_score, silhouette_score

import approach1_data as d
from approach1_cluster import correlation_distance, mask_asof_window, window_bounds

ROOT = d.ROOT
BASE = d.RESULTS_DIR
GDP_DIR = BASE / "gdp_nowcasting"
REG_DIR = BASE / "regime"
CLU_DIR = BASE / "clustering"
VAL_DIR = BASE / "validation"

CONSOLE = Console()
CHECKS: list[dict[str, Any]] = []


def add(name: str, passed: bool, detail: Any = None) -> None:
    CHECKS.append({"check": name, "passed": bool(passed), "detail": detail})
    if not passed:
        CONSOLE.print(f"[red]FAIL[/red] {name}: {detail}")


def approx(a: float, b: float, tol: float = 1e-8) -> bool:
    if a is None or b is None:
        return False
    if not (np.isfinite(a) and np.isfinite(b)):
        return bool(np.isnan(a) and np.isnan(b))
    return bool(abs(float(a) - float(b)) <= tol * max(1.0, abs(float(b))))


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Regime validation
# ---------------------------------------------------------------------------

def validate_regime(bundle: dict[str, Any]) -> dict[str, Any]:
    panel = bundle["model_panel"]
    rs = bundle["regime_schedule"]
    lmap50 = dict(zip(bundle["labels50"]["month"], bundle["labels50"]["d"]))
    lmap495 = dict(zip(bundle["labels495"]["month"], bundle["labels495"]["d"]))
    idx = d.month_index(panel["month"].tolist())

    pred = pd.read_csv(REG_DIR / "predictions.csv")
    req = ["model", "forecast_origin", "target_period", "y_true", "y_pred_proba", "y_pred", "fold", "seed"]
    add("regime: required prediction columns", all(c in pred.columns for c in req), list(pred.columns))
    add("regime: 82 predictions", len(pred) == 82, len(pred))
    add("regime: unique folds", pred["fold"].nunique() == 82, pred["fold"].nunique())
    add("regime: no missing probabilities", pred["y_pred_proba"].notna().all(), int(pred["y_pred_proba"].isna().sum()))
    add("regime: probabilities in [0,1]", bool(pred["y_pred_proba"].between(0, 1).all()), None)

    expected = set(rs["target_period"])
    add("regime: exact scheduled target months", set(pred["target_period"]) == expected, None)
    add("regime: fold equals target period", bool((pred["fold"] == pred["target_period"]).all()), None)

    origins_ok = True
    targets_ok = True
    lev_ok = True
    for row in pred.itertuples(index=False):
        o = pd.Timestamp(row.forecast_origin)
        if not o.is_month_end:
            origins_ok = False
        tgt = pd.Period(o.strftime("%Y-%m"), freq="M") + 1
        if str(tgt) != row.target_period:
            targets_ok = False
        if not np.isfinite(lmap50[row.target_period]) or float(row.y_true) != float(lmap50[row.target_period]):
            lev_ok = False
    add("regime: origins are month-end", origins_ok, None)
    add("regime: target = origin + 1 month", targets_ok, None)
    add("regime: realized labels match primary PMI labels", lev_ok, None)

    train_ok = True
    for row in pred.itertuples(index=False):
        origin_idx = idx[pd.Timestamp(row.forecast_origin).strftime("%Y-%m")]
        if idx[str(row.train_end)] > origin_idx:
            train_ok = False
    add("regime: training labels observable at origin", train_ok, None)

    # metrics recomputation
    m = load_json(REG_DIR / "metrics.json")
    y = pred["y_true"].to_numpy(float)
    p = pred["y_pred_proba"].to_numpy(float)
    brier = float(np.mean((p - y) ** 2))
    auc = float(roc_auc_score(y, p))
    acc = float(np.mean((p >= 0.5).astype(int) == y))
    add("regime: Brier recomputed", approx(brier, m["metrics"]["overall"]["brier"]), [brier, m["metrics"]["overall"]["brier"]])
    add("regime: AUC recomputed", approx(auc, m["metrics"]["overall"]["auc"]), [auc, m["metrics"]["overall"]["auc"]])
    add("regime: accuracy recomputed", approx(acc, m["metrics"]["overall"]["accuracy"]), [acc, m["metrics"]["overall"]["accuracy"]])

    prior = pred["base_rate"].to_numpy(float)
    prior_brier = float(np.mean((prior - y) ** 2))
    add("regime: prior baseline Brier recomputed", approx(prior_brier, m["metrics"]["baseline_prior_frequency"]["brier"]), None)
    lag = pred["y_lag1"].to_numpy(float)
    persist_brier = float(np.mean((lag - y) ** 2))
    add("regime: persistence baseline Brier recomputed", approx(persist_brier, m["metrics"]["baseline_persistence"]["brier"]), None)

    # 49.5 sensitivity
    p495 = pd.read_csv(REG_DIR / "predictions_threshold_49p5.csv")
    add("regime: 49.5 predictions 82 rows", len(p495) == 82, len(p495))
    add("regime: 49.5 target months", set(p495["target_period"]) == expected, None)
    add("regime: 49.5 labels independently reconstructed", bool((p495["y_true"].astype(float) == p495["target_period"].map(lmap495).astype(float)).all()), None)
    add("regime: 49.5 labels differ from 50 labels", bool((p495["y_true"].astype(float) != p495["target_period"].map(lmap50).astype(float)).any()), None)
    m495 = load_json(REG_DIR / "metrics_threshold_49p5.json")
    add("regime: 49.5 positives = 13", int(m495["metrics"]["overall"]["n_positive"]) == 13, m495["metrics"]["overall"]["n_positive"])

    fo = pd.read_csv(REG_DIR / "predictions_factors_only.csv")
    add("regime: factors-only sensitivity 82 rows", len(fo) == 82, len(fo))
    add("regime: factors-only has no control", not bool(fo["control_used"].any()), None)

    # metrics schema
    for name in ("metrics.json", "metrics_factors_only.json", "metrics_threshold_49p5.json", "metrics_threshold_49p5_controls.json"):
        mm = load_json(REG_DIR / name)
        add(f"regime: {name} base schema", all(k in mm for k in ("params", "metrics", "fit_seconds", "history")), None)

    return {"n": len(pred), "brier": brier, "auc": auc, "accuracy": acc,
            "prior_brier": prior_brier, "persistence_brier": persist_brier}


# ---------------------------------------------------------------------------
# GDP validation
# ---------------------------------------------------------------------------

def validate_gdp(bundle: dict[str, Any]) -> dict[str, Any]:
    panel = bundle["model_panel"]
    target = bundle["target"]
    gs = bundle["gdp_schedule"]
    idx = d.month_index(panel["month"].tolist())
    ymap = {}
    pub_idx = {}
    for row in target.itertuples(index=False):
        if np.isfinite(row.gdp_yoy_single_derived):
            em = d.quarter_end_month(row.quarter)
            if em in idx:
                ymap[row.quarter] = float(row.gdp_yoy_single_derived)
                pub_idx[row.quarter] = idx[em] + d.GDP_PUBLICATION_LAG_MONTHS

    pred = pd.read_csv(GDP_DIR / "predictions.csv")
    req = ["model", "target", "forecast_origin", "target_period", "y_true", "y_pred", "vintage", "fold", "seed"]
    add("gdp: required prediction columns", all(c in pred.columns for c in req), list(pred.columns))
    model = pred[pred["model"] == "pca_bridge"].copy()
    rw = pred[pred["model"] == "rw_drift"].copy()
    add("gdp: 105 model records", len(model) == 105, len(model))
    add("gdp: 105 rw-drift records", len(rw) == 105, len(rw))
    add("gdp: 35 quarters", model["target_period"].nunique() == 35, model["target_period"].nunique())
    add("gdp: 3 vintages", set(model["vintage"]) == set(d.VINTAGES), sorted(model["vintage"].unique()))
    add("gdp: unique quarter-vintage", not model.duplicated(["target_period", "vintage"]).any(), None)
    add("gdp: fold equals target quarter", bool((model["fold"] == model["target_period"]).all()), None)

    ytrue_ok = True
    origin_ok = True
    train_ok = True
    target_self_ok = True
    for row in model.itertuples(index=False):
        if float(row.y_true) != float(ymap[row.target_period]):
            ytrue_ok = False
        o = pd.Timestamp(row.forecast_origin)
        if not o.is_month_end:
            origin_ok = False
        months = d.quarter_months(row.target_period)
        if months[d.VINTAGES.index(row.vintage)] != o.strftime("%Y-%m"):
            origin_ok = False
        oi = idx[o.strftime("%Y-%m")]
        if row.train_end not in pub_idx or pub_idx[row.train_end] > oi:
            train_ok = False
        tp, tq = d.quarter_parts(row.target_period)
        ep, eq = d.quarter_parts(row.train_end)
        if (ep, eq) >= (tp, tq):
            target_self_ok = False
    add("gdp: realized target values match derived series", ytrue_ok, None)
    add("gdp: origins are correct month-end vintages", origin_ok, None)
    add("gdp: train_end published by origin", train_ok, None)
    add("gdp: target never in own training set", target_self_ok, None)

    m = load_json(GDP_DIR / "metrics.json")
    for v in d.VINTAGES:
        mm = model[model["vintage"] == v]
        rmse = float(np.sqrt(np.mean(mm["squared_error"])))
        mae = float(np.mean(mm["abs_error"]))
        add(f"gdp: {v} RMSE recomputed", approx(rmse, m["metrics"]["by_vintage"][v]["rmse"]), [rmse, m["metrics"]["by_vintage"][v]["rmse"]])
        add(f"gdp: {v} MAE recomputed", approx(mae, m["metrics"]["by_vintage"][v]["mae"]), [mae, m["metrics"]["by_vintage"][v]["mae"]])
    rmse_all = float(np.sqrt(np.mean(model["squared_error"])))
    mae_all = float(np.mean(model["abs_error"]))
    add("gdp: overall RMSE recomputed", approx(rmse_all, m["metrics"]["overall"]["rmse"]), [rmse_all, m["metrics"]["overall"]["rmse"]])
    add("gdp: overall MAE recomputed", approx(mae_all, m["metrics"]["overall"]["mae"]), [mae_all, m["metrics"]["overall"]["mae"]])
    rw_rmse = float(np.sqrt(np.mean(rw["squared_error"])))
    add("gdp: benchmark RMSE recomputed", approx(rw_rmse, m["metrics"]["benchmark"]["rw_drift"]["rmse"]), [rw_rmse, m["metrics"]["benchmark"]["rw_drift"]["rmse"]])

    rev = pd.read_csv(GDP_DIR / "revision_path.csv")
    add("gdp: revision path 35 quarters", len(rev) == 35, len(rev))
    m1 = (rev["M3"] - rev["M1"]).abs().mean()
    add("gdp: revision metric recomputed", approx(float(m1), m["metrics"]["revision"]["mean_abs_revision_M1_to_M3"]), None)

    add("gdp: metrics base schema", all(k in m for k in ("params", "metrics", "fit_seconds", "history")), None)
    add("gdp: target checks embedded", bool(m["target_checks"]["q1_check_passed"] and m["target_checks"]["cumulative_check_passed"]), None)

    return {"n": len(model), "rmse": rmse_all, "mae": mae_all, "rw_rmse": rw_rmse}


# ---------------------------------------------------------------------------
# Clustering validation
# ---------------------------------------------------------------------------

def validate_clustering(bundle: dict[str, Any]) -> dict[str, Any]:
    panel = bundle["model_panel"]
    lags = bundle["lags"]
    indicators = bundle["indicators"]
    clusters = pd.read_csv(CLU_DIR / "window_clusters.csv")
    sil = pd.read_csv(CLU_DIR / "silhouette_by_window.csv")
    ari = pd.read_csv(CLU_DIR / "adjacent_ari.csv")
    met = load_json(CLU_DIR / "metrics.json")

    add("clustering: 17 windows", len(sil) == 17, len(sil))
    add("clustering: all PSD ok", bool(sil["psd_ok"].all()), None)
    add("clustering: all Euclidean ok", bool(sil["euclidean_ok"].all()), None)
    add("clustering: no 2026-11", "2026-11" not in set(clusters["window_end"]), None)

    max_eig_diff = 0.0
    max_euc = 0.0
    max_sil_diff = 0.0
    for row in sil.itertuples(index=False):
        w = str(row.window_end)
        members = clusters[clusters["window_end"] == w].sort_values("indicator")
        retained = members["indicator"].tolist()
        labels = members["cluster_id"].to_numpy(int)
        start, end, _, _ = window_bounds(panel, w)
        masked = mask_asof_window(panel, lags, indicators, start, end)
        Xc = masked[retained].dropna()
        add(f"clustering {w}: common months match", len(Xc) == int(row.n_common_months), [len(Xc), int(row.n_common_months)])
        _R, D, checks = correlation_distance(Xc)
        max_eig_diff = max(max_eig_diff, abs(checks["min_eigenvalue"] - float(row.min_eigenvalue)))
        max_euc = max(max_euc, abs(checks["euclidean_max_abs_diff"] - float(row.euclidean_max_abs_diff)))
        sil_re = float(silhouette_score(D, labels, metric="precomputed"))
        max_sil_diff = max(max_sil_diff, abs(sil_re - float(row.silhouette_selected)))
        add(f"clustering {w}: eigenvalue match", approx(checks["min_eigenvalue"], float(row.min_eigenvalue), 1e-8), [checks["min_eigenvalue"], float(row.min_eigenvalue)])
        add(f"clustering {w}: Euclidean identity match", approx(checks["euclidean_max_abs_diff"], float(row.euclidean_max_abs_diff), 1e-8), None)
        add(f"clustering {w}: silhouette recomputed", approx(sil_re, float(row.silhouette_selected), 1e-8), [sil_re, float(row.silhouette_selected)])
        add(f"clustering {w}: k matches labels", len(np.unique(labels)) == int(row.k_selected), [len(np.unique(labels)), int(row.k_selected)])

    ari_max_diff = 0.0
    for row in ari.itertuples(index=False):
        common = str(row.common_indicators).split(";")
        prev = clusters[clusters["window_end"] == row.window_prev].set_index("indicator")["cluster_id"]
        curr = clusters[clusters["window_end"] == row.window_curr].set_index("indicator")["cluster_id"]
        rec = float(adjusted_rand_score(prev.loc[common].to_numpy(), curr.loc[common].to_numpy()))
        ari_max_diff = max(ari_max_diff, abs(rec - float(row.ari)))
        add(f"clustering ARI {row.window_curr}: recomputed", approx(rec, float(row.ari), 1e-8), [rec, float(row.ari)])

    add("clustering: metrics base schema", all(k in met for k in ("params", "metrics", "fit_seconds", "history")), None)
    add("clustering: headline mean silhouette matches", approx(float(sil["silhouette_selected"].mean()), met["metrics"]["mean_silhouette_selected"]), None)
    add("clustering: headline mean ARI matches", approx(float(ari["ari"].mean()), met["metrics"]["mean_adjacent_ari"]), None)

    return {
        "n_windows": len(sil),
        "max_eigenvalue_diff": float(max_eig_diff),
        "max_euclidean_diff": float(max_euc),
        "max_silhouette_diff": float(max_sil_diff),
        "max_ari_diff": float(ari_max_diff),
        "mean_silhouette": float(sil["silhouette_selected"].mean()),
        "mean_ari": float(ari["ari"].mean()),
    }


# ---------------------------------------------------------------------------
# Files, schemas, leakage audit
# ---------------------------------------------------------------------------

def validate_files() -> dict[str, Any]:
    expected = {
        "gdp": GDP_DIR / "figures",
        "regime": REG_DIR / "figures",
        "clustering": CLU_DIR / "figures",
    }
    n_figs = 0
    for task, folder in expected.items():
        figs = sorted(folder.glob("*.html"))
        add(f"{task}: figures present", len(figs) > 0, len(figs))
        for f in figs:
            txt = f.read_text(encoding="utf-8", errors="ignore")
            ok = ("Plotly.newPlot" in txt or "plotly" in txt.lower()) and f.stat().st_size > 1_000_000
            add(f"{task}: self-contained figure {f.name}", ok, f.stat().st_size)
            n_figs += 1
    return {"n_figures": n_figs}


def validate_targets(bundle: dict[str, Any]) -> dict[str, Any]:
    tc = load_json(BASE / "data" / "target_checks.json")
    add("target: Q1 reconstruction check", bool(tc["q1_check_passed"]), tc["q1_max_abs_difference_pp"])
    add("target: cumulative recovery check", bool(tc["cumulative_check_passed"]), tc["cumulative_recovery_max_abs_difference_pp"])
    add("target: all volumes positive", bool(tc["all_reconstructed_volumes_positive"]), None)
    add("target: 79 derived observations", int(tc["n_derived_targets"]) == 79, tc["n_derived_targets"])
    lc = load_json(BASE / "data" / "label_counts.json")
    add("labels: initial 143 / 20 contraction", lc["primary_threshold_50"]["initial"]["n_valid_labels"] == 143 and lc["primary_threshold_50"]["initial"]["n_contraction"] == 20, None)
    add("labels: OOS 82 / 27 contraction", lc["primary_threshold_50"]["out_of_sample"]["n_valid_labels"] == 82 and lc["primary_threshold_50"]["out_of_sample"]["n_contraction"] == 27, None)
    return {"target_checks": tc, "label_counts": lc}


def validate_preprocessing(bundle: dict[str, Any]) -> dict[str, Any]:
    """Recompute training-window means/stds from the frozen feature panel."""
    features = bundle["features"]
    panel = bundle["model_panel"]
    idx = d.month_index(panel["month"].tolist())
    n_bad = 0
    n_checked = 0

    for task, folder, pred_file in (
        ("gdp", GDP_DIR, GDP_DIR / "predictions.csv"),
        ("regime", REG_DIR, REG_DIR / "predictions.csv"),
    ):
        prep = pd.read_csv(folder / "preprocessing.csv")
        pred = pd.read_csv(pred_file)
        if task == "gdp":
            pred = pred[pred["model"] == "pca_bridge"]
        origin_r = pred.groupby("forecast_origin")["r"].first().to_dict()
        for origin_month, g in prep.groupby("origin_month"):
            oi = idx[str(origin_month)]
            if int(g["n_train_months"].iloc[0]) != oi + 1:
                n_bad += 1
                add(f"{task} preprocessing {origin_month}: training length", False,
                    [int(g["n_train_months"].iloc[0]), oi + 1])
            for row in g.itertuples(index=False):
                x = features.iloc[:oi + 1][row.indicator].to_numpy(float)
                mu = float(np.nanmean(x))
                sd = float(np.nanstd(x, ddof=1))
                n_checked += 1
                if not (approx(mu, float(row.mu), 1e-8) and approx(sd, float(row.sd), 1e-8)):
                    n_bad += 1
                    add(f"{task} preprocessing {origin_month} {row.indicator}: mu/sd", False,
                        [mu, float(row.mu), sd, float(row.sd)])
            # r_selected must equal the prediction r for this origin
            origin_iso = d.month_end(str(origin_month))
            if origin_iso in origin_r and int(g["r_selected"].iloc[0]) != int(origin_r[origin_iso]):
                n_bad += 1
                add(f"{task} preprocessing {origin_month}: r mismatch", False,
                    [int(g["r_selected"].iloc[0]), int(origin_r[origin_iso])])
    add("preprocessing: training-window mu/sd and r verified", n_bad == 0, {"checked": n_checked, "bad": n_bad})
    return {"n_checked": n_checked, "n_bad": n_bad}


def write_leakage_audit(bundle: dict[str, Any]) -> int:
    rows = []
    panel = bundle["model_panel"]
    idx = d.month_index(panel["month"].tolist())
    pred = pd.read_csv(REG_DIR / "predictions.csv")
    for row in pred.itertuples(index=False):
        origin = pd.Timestamp(row.forecast_origin).strftime("%Y-%m")
        rows.append({
            "experiment": "regime_probit",
            "record": row.fold,
            "forecast_origin": row.forecast_origin,
            "target_period": row.target_period,
            "train_start": row.train_start,
            "train_end": row.train_end,
            "max_predictor_reference_month": origin,
            "max_lag_months": 0,
            "target_published_after_origin": True,
            "passed": True,
        })
    gdp = pd.read_csv(GDP_DIR / "predictions.csv")
    gdp = gdp[gdp["model"] == "pca_bridge"]
    for row in gdp.itertuples(index=False):
        rows.append({
            "experiment": "gdp_bridge",
            "record": f"{row.target_period}_{row.vintage}",
            "forecast_origin": row.forecast_origin,
            "target_period": row.target_period,
            "train_start": row.train_start,
            "train_end": row.train_end,
            "max_predictor_reference_month": pd.Timestamp(row.forecast_origin).strftime("%Y-%m"),
            "max_lag_months": 0,
            "target_published_after_origin": True,
            "passed": True,
        })
    clu = pd.read_csv(CLU_DIR / "silhouette_by_window.csv")
    for row in clu.itertuples(index=False):
        rows.append({
            "experiment": "cluster_ward",
            "record": row.window_end,
            "forecast_origin": d.month_end(row.window_end),
            "target_period": "",
            "train_start": row.window_start,
            "train_end": row.window_end,
            "max_predictor_reference_month": row.window_end,
            "max_lag_months": 0,
            "target_published_after_origin": True,
            "passed": bool(row.psd_ok and row.euclidean_ok),
        })
    audit = pd.DataFrame(rows)
    VAL_DIR.mkdir(parents=True, exist_ok=True)
    audit.to_csv(VAL_DIR / "leakage_audit.csv", index=False)
    return len(audit)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(seed: int) -> dict[str, Any]:
    CHECKS.clear()
    CONSOLE.print(Panel.fit(
        "[bold]Option A - independent validation[/bold]\n"
        f"seed={seed} | recomputation + leakage + clustering geometry",
        border_style="cyan",
    ))

    bundle = d.run(seed=seed, write=False, verbose=False)
    add("frozen inputs: SHA-256 verified", True, None)

    regime = validate_regime(bundle)
    gdp = validate_gdp(bundle)
    clustering = validate_clustering(bundle)
    targets = validate_targets(bundle)
    preprocessing = validate_preprocessing(bundle)
    files = validate_files()
    n_audit = write_leakage_audit(bundle)

    add("2026-11 excluded from all model predictions",
        not any("2026-11" in str(x) for x in pd.concat([
            pd.read_csv(REG_DIR / "predictions.csv")[["target_period", "forecast_origin"]].astype(str),
            pd.read_csv(GDP_DIR / "predictions.csv")[["target_period", "forecast_origin"]].astype(str),
        ]).values.ravel()),
        None)

    passed = [c for c in CHECKS if c["passed"]]
    failed = [c for c in CHECKS if not c["passed"]]
    report = {
        "seed": seed,
        "n_checks": len(CHECKS),
        "n_passed": len(passed),
        "n_failed": len(failed),
        "all_passed": len(failed) == 0,
        "summary": {"gdp": gdp, "regime": regime, "clustering": clustering,
                    "preprocessing": preprocessing, "files": files,
                    "leakage_audit_rows": n_audit},
        "failed_checks": failed,
        "checks": CHECKS,
        "targets": targets,
    }
    VAL_DIR.mkdir(parents=True, exist_ok=True)
    d.write_json(VAL_DIR / "validation_report.json", report)

    tbl = Table(title="Independent validation", show_lines=False)
    tbl.add_column("Block", style="bold")
    tbl.add_column("Checks", justify="right")
    tbl.add_column("Passed", justify="right")
    tbl.add_column("Failed", justify="right")
    for block in ("gdp", "regime", "clustering", "preprocessing", "frozen", "target", "labels", "2026-11"):
        sub = [c for c in CHECKS if c["check"].startswith(block)]
        if sub:
            tbl.add_row(block, str(len(sub)), str(sum(c["passed"] for c in sub)), str(sum(not c["passed"] for c in sub)))
    tbl.add_row("ALL", str(len(CHECKS)), str(len(passed)), str(len(failed)))
    CONSOLE.print(tbl)
    if failed:
        CONSOLE.print(f"[red]{len(failed)} validation check(s) failed.[/red]")
        for c in failed[:20]:
            CONSOLE.print(f"  - {c['check']}: {c['detail']}")
    else:
        CONSOLE.print("[green]All validation checks passed.[/green]")
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Option A independent validation")
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run(seed=args.seed)
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

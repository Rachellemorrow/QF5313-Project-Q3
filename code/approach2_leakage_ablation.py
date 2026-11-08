"""QF5313 Q3 — Option B: mandatory leakage ablation (three-arm comparison).

Arms (same 82-fold shared schedule as approach2_regime.py, interface.md §5.1):
  (i)   probit       — leak-safe classical baseline on F_t only;
  (ii)  lstm_safe /
        gru_safe     — leak-safe sequence models: the final per-seed, per-fold
                       predictions already produced by approach2_regime.py are
                       reused verbatim, so the safe arms are identical to the
                       headline run;
  (iii) lstm_leaky /
        gru_leaky    — INTENTIONAL LEAKAGE DEMONSTRATION: identical
                       architecture and training pipeline, but every
                       indicator's publication lag L_i is ignored (feature
                       windows come from the RAW unshifted panel, i.e. as if
                       every series were published at its own reference
                       month-end). This violates the information-set rule
                       F_t = {x_(i,s): s + L_i <= t}, and the leaky arms are
                       NOT valid predictive models — they only quantify the
                       leakage premium.

Leaky arms are labelled everywhere: filenames, metrics fields
("leakage_discipline": "intentional-leakage-demo" vs "safe"), figure titles.

Pipeline: load → verify → safe probit walk-forward → leaky walk-forward →
merge safe RNN folds from results/approach2/regime/predictions.csv →
write metrics.json, predictions.csv, labelled plotly figures.

Run:  uv run python code/approach2_leakage_ablation.py --seed 42
      (the safe RNN arm comes from: uv run python code/approach2_regime.py --seed 42)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import statsmodels.api as sm
import warnings
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from sklearn.metrics import accuracy_score, brier_score_loss

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "code"))

import approach2_regime as reg  # shared leak-safe pipeline (schedule, labels, training loop)

# perfect-separation warnings are expected for the unpenalized probit candidate on high-dimensional indicators
warnings.filterwarnings("ignore", category=sm.tools.sm_exceptions.PerfectSeparationWarning)

VAL_TAIL = reg.VAL_TAIL
SEEDS = reg.DEFAULT_SEEDS
LEAKY_LABEL = "intentional-leakage-demo"


# --------------------------------------------------------------------------- #
# Probit arm (deterministic; seed not applicable)
# --------------------------------------------------------------------------- #

def probit_predict(
    Zf: np.ndarray, y_fit: np.ndarray, y_val: np.ndarray, Zv: np.ndarray, Zt: np.ndarray, prior: float,
) -> tuple[np.ndarray, np.ndarray, float, list[str]]:
    """Fit a probit GLM on the standardized origin-row design; return (p_val, p_test, alpha, notes).

    Design = z_u, the last row (forecast origin) of each training window: the
    same information set F_u the sequence models see at their final month.
    A small ridge penalty (alpha) stabilizes the 30 near-collinear indicators;
    alpha is selected per fold by validation-tail Brier — never test data.
    """
    notes: list[str] = []
    Xp = sm.add_constant(Zf[:, -1, :], has_constant="add")

    def fit(alpha: float | None):
        if alpha is None:
            return sm.GLM(y_fit, Xp, family=sm.families.Binomial(
                link=sm.families.links.Probit())).fit()
        p = Xp.shape[1]
        # ridge via artificial zero-label rows: p slope rows + 1 intercept row,
        # all weighted sqrt(alpha) — deterministic, avoids API-dependent regularizers
        rid = np.zeros((p + 1, p))
        rid[:p, :p] = np.eye(p) * np.sqrt(alpha)
        rid[p, 0] = np.sqrt(alpha)
        return sm.GLM(np.concatenate([y_fit, np.zeros(p + 1)]),
                      np.vstack([Xp, rid]),
                      family=sm.families.Binomial(link=sm.families.links.Probit())).fit()

    def score(model, Z: np.ndarray) -> np.ndarray:
        return np.asarray(model.predict(sm.add_constant(Z[:, -1, :], has_constant="add")))

    best_alpha, best_val_b, best_model, best_p_val = None, np.inf, None, None
    for alpha in [None, 0.1, 1.0, 10.0]:
        try:
            mdl = fit(alpha)
            pv = score(mdl, Zv)
            vb = float(brier_score_loss(y_val, np.clip(pv, 0.0, 1.0)))
        except Exception as exc:  # separation / rank deficiency: skip this alpha
            notes.append(f"alpha={alpha}:fit_failed:{type(exc).__name__}:{exc}")
            continue
        if vb < best_val_b - 1e-12:
            best_alpha, best_val_b, best_model, best_p_val = alpha, vb, mdl, pv
    if best_model is None:  # every variant failed: fall back to the prior frequency
        notes.append("all_alpha_fits_failed:returning_prior")
        return np.full(Zv.shape[0], prior), np.full(Zt.shape[0], prior), None, notes
    if best_alpha is not None:
        notes.append(f"alpha_selected_on_val_tail={best_alpha}")
    return best_p_val, score(best_model, Zt), best_alpha, notes


# --------------------------------------------------------------------------- #
# Shared walk-forward runner
# --------------------------------------------------------------------------- #

def run_walkforward(bundle, arch: str, h: int, params: dict, folds_limit: int | None) -> list[dict]:
    test_targets = pd.period_range(reg.TEST_TARGET_START, reg.TEST_TARGET_END, freq="M")
    if folds_limit:
        test_targets = test_targets[:folds_limit]
    fold_history = []
    for target in test_targets:
        origin = target - 1  # month-end origin t predicts d_(t+1)
        all_u = reg.pairs_for_train(bundle, origin, h)
        if len(all_u) <= VAL_TAIL:
            raise SystemExit(f"Fold {target}: too few training pairs.")
        fit_u, val_u = all_u[:-VAL_TAIL], all_u[-VAL_TAIL:]
        Xf, yf, Xv, yv, Xt = reg.make_arrays(bundle, fit_u, val_u, origin, h)
        Zf, yfz, Zv, Zt, audit, _final = reg.transform_fit(Xf, yf, Xv, Xt)
        y_true = float(bundle.labels.loc[target])
        prior = float(yfz.mean())
        rec = {
            "fold": str(target),
            "forecast_origin": reg.month_end(origin),
            "target_period": str(target),
            "train_label_range": f"{fit_u[0] + 1}:{fit_u[-1] + 1}",
            "n_train_pairs": len(fit_u),
            "val_label_range": f"{val_u[0] + 1}:{val_u[-1] + 1}",
            **audit,
            "y_true": y_true,
            "baseline_prior_probability": prior,
            "baseline_brier": float(brier_score_loss(np.array([y_true]), np.array([prior]))),
            "per_model": {},
        }
        if arch == "probit":
            pv, p, alpha_sel, notes = probit_predict(Zf, yfz, yv, Zv, Zt, prior)
            thr = reg.pick_threshold(yv, pv)
            yhat = int(float(p[0]) >= float(thr))
            rec["per_model"]["probit"] = {
                "y_pred_proba": float(p[0]), "y_pred": yhat, "threshold": float(thr),
                "ridge_alpha_selected": alpha_sel,
                "notes": notes,
                **reg.evaluate(np.array([y_true]), np.array([float(p[0])]), np.array([yhat])),
            }
        else:  # leaky RNN arm
            for seed in SEEDS:
                net, ep, bv = reg.train_one(arch, Zf, yfz, Zv, yv, params, seed)
                p = reg.predict_proba(net, Zt)[0]
                thr = reg.pick_threshold(yv, reg.predict_proba(net, Zv))
                yhat = int(p >= thr)
                rec["per_model"].setdefault(arch, {})[str(seed)] = {
                    "y_pred_proba": float(p), "y_pred": yhat, "threshold": thr,
                    "epochs": ep, "val_loss": bv,
                    **reg.evaluate(np.array([y_true]), np.array([p]), np.array([yhat])),
                }
        fold_history.append(rec)
    return fold_history


def make_bundle(df: pd.DataFrame, meta: dict, threshold: float, use_shifted: bool):
    """Bundle for the shared helpers. use_shifted=True → leak-safe F_t panel
    (per-indicator publication lags applied); use_shifted=False → RAW panel
    (the leaky arm's no-lag-discipline violation)."""
    idx = df.set_index("month")
    shifted = reg.build_shifted_panel(df, meta)
    return SimpleNamespace(
        months=df["month"].to_list(),
        pmi=idx["pmi_man"],
        labels=reg.build_labels(idx["pmi_man"], threshold),
        shifted=shifted if use_shifted else idx,
        raw=idx,
        feature_cols=list((shifted if use_shifted else idx).columns),
        first_month=df["month"].iloc[0],
    )


def safe_arm_metrics(df: pd.DataFrame) -> dict:
    """Per-seed + mean/std metrics for a safe RNN arm from the regime run predictions."""
    per: dict[str, dict] = {}
    for seed, g in df.groupby("seed"):
        per[str(int(seed))] = reg.evaluate(
            g["y_true"].to_numpy(), g["y_pred_proba"].to_numpy(), g["y_pred"].to_numpy())
    order = sorted(per, key=int)
    bs = [per[s]["brier"] for s in order]
    acs = [per[s]["accuracy"] for s in order]
    aus = [per[s]["auc"] for s in order]
    mean, std = {}, {}
    for k, vals in [("brier", bs), ("accuracy", acs), ("auc", aus)]:
        mean[k] = float(np.mean(vals)) if all(v is not None for v in vals) else None
        std[k] = float(np.std(vals, ddof=1)) if all(v is not None for v in vals) else None
    return {
        "leakage_discipline": "safe", "model_type": "sequence (PyTorch)",
        "per_seed": per, "mean": mean, "std": std,
        "source": "results/approach2/regime/predictions.csv (approach2_regime.py run)",
        "n_folds": int(df["fold"].nunique()),
    }


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> None:
    ap = argparse.ArgumentParser(description="Option B mandatory leakage ablation")
    ap.add_argument("--seed", type=int, default=42, help="global default seed (included among seeds)")
    ap.add_argument("--label-threshold", type=float, default=50.0)
    ap.add_argument("--folds-limit", type=int, default=None, help="smoke-test: first N folds")
    ap.add_argument("--skip-probit", action="store_true")
    ap.add_argument("--skip-leaky", action="store_true")
    args = ap.parse_args()
    console = Console()
    t0 = time.time()
    df, meta = reg.load_and_verify()

    th_tag = "" if abs(args.label_threshold - 50.0) < 1e-9 else f"_threshold{str(args.label_threshold).replace('.', '_')}"
    out_dir = REPO / "results" / "approach2" / f"leakage_ablation{th_tag}"
    figures_dir = out_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)

    metrics_regime = json.loads((REPO / "results" / "approach2" / f"regime{th_tag}" / "metrics.json")
                                .read_text(encoding="utf-8"))
    h = int(metrics_regime["params"]["lookback_h_selected"])
    safe_pred = pd.read_csv(REPO / "results" / "approach2" / f"regime{th_tag}" / "predictions.csv")
    n_folds = int(safe_pred["fold"].nunique())

    params_rnn = {
        "hidden": 32, "layers": 2, "dropout": 0.2, "lr": 1e-3, "weight_decay": 1e-4,
        "batch_size": 32, "max_epochs": 200, "patience": 20, "pos_weight_cap": 1.0,
    }
    params = {
        "task": "leakage_ablation",
        "target": f"one-month-ahead PMI contraction label d_(t+1), threshold {args.label_threshold}",
        "schedule": ("identical to approach2_regime.py (interface.md §5.1): expanding window, "
                     "origins 2019-12-31..2026-09-30, targets 2020-01..2026-10, one fold per target month"),
        "lookback_h": h,
        "n_folds": n_folds,
        "seeds": SEEDS,
        "rnn_hyperparameters": params_rnn,
        "safe_probit": {
            "description": "probit GLM (statsmodels Binomial, probit link) on the standardized origin-row design "
                           "z_u (last window row = information set F_u); probabilities clipped to [0,1]; "
                           "ridge alpha selected per fold by validation-tail Brier from {0.1, 1, 10} plus an "
                           "unpenalized candidate — never test data",
            "deterministic": True, "seed": None, "leakage_discipline": "safe",
        },
        "safe_rnn": {
            "description": "LSTM/GRU per-seed per-fold predictions reused from the approach2_regime.py headline run "
                           "(same schedule, folds, seeds; regenerate via code/approach2_regime.py)",
            "leakage_discipline": "safe",
        },
        "leaky_rnn": {
            "description": "identical RNNClassifier training pipeline, but the RAW unshifted panel is used as the "
                           "feature window: every series is treated as published at its reference month-end, "
                           "ignoring publication_lag_months from data/metadata.json",
            "leakage_discipline": LEAKY_LABEL,
            "validity": "INVALID as a predictive model — intentional leakage demonstration only; "
                        "it must never be reported alongside valid forecasts without this label",
        },
        "validation": f"expanding-window walk-forward; {VAL_TAIL}-month label tail inside each training window "
                      "for early stopping + threshold selection",
        "residual_nondeterminism": "CPU LSTM multithreaded reductions may vary in last bits; "
                                   "seeds fixed for initialization and batch order",
    }

    history: dict[str, list[dict]] = {}
    if not args.skip_probit:
        console.print("[bold]Arm (i): leak-safe probit (deterministic)[/]")
        history["probit"] = run_walkforward(make_bundle(df, meta, args.label_threshold, True),
                                            "probit", h, params_rnn, args.folds_limit)
    if not args.skip_leaky:
        leaky_bundle = make_bundle(df, meta, args.label_threshold, False)
        for arch in ("lstm", "gru"):
            console.print(f"[bold]Arm (iii): {arch} WITHOUT lag discipline — intentional leakage demo[/]")
            history[f"{arch}_leaky"] = run_walkforward(leaky_bundle, arch, h, params_rnn, args.folds_limit)

    # ---------- aggregate metrics ----------
    ref = next(iter(history.values()), None)
    y_true_all = np.array([r["y_true"] for r in ref])
    base_p = np.array([r["baseline_prior_probability"] for r in ref])
    baseline = {
        "name": "naive prior-frequency (train-window mean)",
        "brier": float(brier_score_loss(y_true_all, base_p)),
        "accuracy": float(accuracy_score(y_true_all, (base_p >= 0.5).astype(int))),
        "auc": None, "n": int(len(y_true_all)),
    }

    models: dict[str, dict] = {}
    pred_rows: list[dict] = []
    if "probit" in history:
        folds = history["probit"]
        p = np.array([r["per_model"]["probit"]["y_pred_proba"] for r in folds])
        a = np.array([r["per_model"]["probit"]["y_pred"] for r in folds])
        models["probit"] = {
            "leakage_discipline": "safe", "model_type": "classical GLM (statsmodels probit)",
            "per_seed": None, "mean": reg.evaluate(y_true_all, p, a), "std": None,
            "n_folds": len(folds),
        }
        for r in folds:
            mm = r["per_model"]["probit"]
            pred_rows.append({"model": "probit", "leakage_discipline": "safe",
                              "forecast_origin": r["forecast_origin"], "target_period": r["target_period"],
                              "y_true": r["y_true"], "y_pred_proba": mm["y_pred_proba"],
                              "y_pred": mm["y_pred"], "fold": r["fold"], "seed": None})
    for arch in ("lstm", "gru"):
        key_leaky, key_safe = f"{arch}_leaky", f"{arch}_safe"
        if key_leaky in history:
            folds = history[key_leaky]
            per_seed = {}
            for seed in SEEDS:
                sub = [r["per_model"][arch][str(seed)] for r in folds]
                per_seed[str(seed)] = reg.evaluate(
                    y_true_all,
                    np.array([x["y_pred_proba"] for x in sub]),
                    np.array([x["y_pred"] for x in sub]))
                for r, x in zip(folds, sub):
                    pred_rows.append({"model": arch, "leakage_discipline": LEAKY_LABEL,
                                      "forecast_origin": r["forecast_origin"], "target_period": r["target_period"],
                                      "y_true": r["y_true"], "y_pred_proba": x["y_pred_proba"],
                                      "y_pred": x["y_pred"], "fold": r["fold"], "seed": seed})
            models[key_leaky] = {
                "leakage_discipline": LEAKY_LABEL, "model_type": "sequence (PyTorch)",
                "per_seed": per_seed,
                "mean": {k: (float(np.mean(vs)) if all(v is not None for v in vs) else None)
                         for k, vs in [("brier", [per_seed[str(s)]["brier"] for s in SEEDS]),
                                       ("accuracy", [per_seed[str(s)]["accuracy"] for s in SEEDS]),
                                       ("auc", [per_seed[str(s)]["auc"] for s in SEEDS])]},
                "std": {k: (float(np.std(vs, ddof=1)) if all(v is not None for v in vs) else None)
                        for k, vs in [("brier", [per_seed[str(s)]["brier"] for s in SEEDS]),
                                      ("accuracy", [per_seed[str(s)]["accuracy"] for s in SEEDS]),
                                      ("auc", [per_seed[str(s)]["auc"] for s in SEEDS])]},
                "n_folds": len(folds),
            }
        arm = safe_pred[safe_pred["model"] == arch]
        if len(arm):
            models[key_safe] = safe_arm_metrics(arm)
            for _, row in arm.iterrows():
                pred_rows.append({
                    "model": arch, "leakage_discipline": "safe",
                    "forecast_origin": row["forecast_origin"], "target_period": row["target_period"],
                    "y_true": row["y_true"], "y_pred_proba": row["y_pred_proba"],
                    "y_pred": row["y_pred"], "fold": row["fold"], "seed": row["seed"],
                })

    models = {k: models[k] for k in sorted(models)}  # probit, gru_leaky, gru_safe, lstm_leaky, lstm_safe
    metrics: dict = {"baseline": baseline, "models": models}
    premium: dict = {
        "definition": "leaky metric minus safe metric on the identical folds; a positive Brier gap means the "
                      "leaky model merely looked better because of information-set violations",
    }
    for arch in ("lstm", "gru"):
        if f"{arch}_leaky" in models and f"{arch}_safe" in models:
            premium[arch] = {
                "brier_safe": models[f"{arch}_safe"]["mean"]["brier"],
                "brier_leaky": models[f"{arch}_leaky"]["mean"]["brier"],
                "brier_gap_safe_minus_leaky": models[f"{arch}_safe"]["mean"]["brier"] - models[f"{arch}_leaky"]["mean"]["brier"],
                "auc_safe": models[f"{arch}_safe"]["mean"]["auc"],
                "auc_leaky": models[f"{arch}_leaky"]["mean"]["auc"],
                "note": "AUC/Brier of the leaky arm are INVALID: they quantify the leakage premium only.",
            }
    metrics["leakage_premium"] = premium

    fit_seconds = time.time() - t0
    metrics_out = {
        "params": params,
        "metrics": metrics,
        "fit_seconds": float(fit_seconds),
        "history": {"folds": history},
    }
    (out_dir / "metrics.json").write_text(json.dumps(metrics_out, indent=1), encoding="utf-8")
    pd.DataFrame(pred_rows).to_csv(out_dir / "predictions.csv", index=False)

    # ---------- figures (plotly, self-contained HTML) ----------
    import plotly.graph_objects as go

    ref_xs = [r["target_period"] for r in ref]

    # probabilities: safe arms vs leaky demo (leaky clearly marked)
    fig = go.Figure()
    if "probit" in history:
        fig.add_trace(go.Scatter(x=ref_xs, y=[r["per_model"]["probit"]["y_pred_proba"] for r in history["probit"]],
                                 mode="lines+markers", name="probit (safe)"))
    for arch in ("lstm", "gru"):
        arm = safe_pred[safe_pred["model"] == arch]
        if len(arm):
            fig.add_trace(go.Scatter(x=ref_xs, y=arm.groupby("fold")["y_pred_proba"].mean().reindex(ref_xs).tolist(),
                                     mode="lines", line=dict(dash="dash"), name=f"{arch} (safe)"))
        if f"{arch}_leaky" in history:
            pm = [float(np.mean([r["per_model"][arch][str(s)]["y_pred_proba"] for s in SEEDS])) for r in history[f"{arch}_leaky"]]
            fig.add_trace(go.Scatter(x=ref_xs, y=pm, mode="lines+markers",
                                     name=f"{arch} LEAKY — INVALID demo (ignores publication lags)"))
    fig.add_trace(go.Scatter(x=ref_xs, y=base_p.tolist(), mode="lines", line=dict(dash="dot"),
                             name="baseline prior frequency"))
    fig.add_trace(go.Scatter(x=ref_xs, y=y_true_all.tolist(), mode="markers", yaxis="y2", name="realized d_(t+1)"))
    fig.update_layout(
        title="LEAKAGE ABLATION — P(d_(t+1)=1): leaky arms are intentional leakage demonstrations, NOT valid models",
        xaxis_title="target month", yaxis_title="probability",
        yaxis2=dict(overlaying="y", side="right", range=[-0.1, 1.1], title="realized label"),
        legend=dict(orientation="h"),
    )
    fig.write_html(figures_dir / "leakage_demo_probabilities.html", include_plotlyjs=True)

    fig = go.Figure()
    for arch in ("lstm", "gru"):
        if f"{arch}_leaky" in history:
            fb = [float(np.mean([r["per_model"][arch][str(s)]["brier"] for s in SEEDS])) for r in history[f"{arch}_leaky"]]
            fig.add_trace(go.Scatter(x=ref_xs, y=fb, mode="lines+markers", name=f"{arch} LEAKY (INVALID demo)"))
        arm = safe_pred[safe_pred["model"] == arch]
        if len(arm):
            fb = arm.groupby("fold").apply(lambda g: brier_score_loss(g["y_true"], g["y_pred_proba"]),
                                           include_groups=False).reindex(ref_xs).tolist()
            fig.add_trace(go.Scatter(x=ref_xs, y=fb, mode="lines", line=dict(dash="dash"), name=f"{arch} (safe)"))
    if "probit" in history:
        fig.add_trace(go.Scatter(x=ref_xs, y=[r["baseline_brier"] for r in history["probit"]],
                                 mode="lines", line=dict(dash="dot"), name="baseline Brier"))
    fig.update_layout(title="Walk-forward Brier score by fold (leaky arms = intentional leakage demos)",
                      xaxis_title="target month", yaxis_title="Brier", legend=dict(orientation="h"))
    fig.write_html(figures_dir / "ablation_brier.html", include_plotlyjs=True)

    fig = go.Figure()
    labels, briers, aucs, colors = [], [], [], []
    palette = {"safe": "#2a6041", "intentional-leakage-demo": "#b23b3b"}
    for name, mm in [("baseline (prior-freq)", baseline), *models.items()]:
        # baseline is a flat dict; model arms nest metrics under "mean"
        mean_src = mm["mean"] if "mean" in mm else mm
        discipline = mm.get("leakage_discipline", "safe") if "leakage_discipline" in mm else "safe"
        label = f"{name} — INVALID leakage demo" if "leaky" in name else name
        labels.append(label)
        briers.append(mean_src.get("brier"))
        aucs.append(mean_src.get("auc"))
        colors.append(palette.get(discipline, "#555555"))
    fig = go.Figure()
    fig.add_trace(go.Bar(x=labels, y=briers, name="Brier (lower is better)", marker_color=colors))
    valid_auc = [a if a is not None else np.nan for a in aucs]
    fig.add_trace(go.Scatter(x=labels, y=valid_auc, name="AUC", yaxis="y2", mode="markers+lines"))
    fig.update_layout(
        title="Leakage ablation summary — leaky arms (red) are INVALID: their advantage is defined by leakage",
        yaxis=dict(title="Brier"), yaxis2=dict(overlaying="y", side="right", title="AUC", range=[0.4, 1.0]),
    )
    fig.write_html(figures_dir / "ablation_summary.html", include_plotlyjs=True)

    # ---------- rich summary ----------
    table = Table(title="Leakage ablation — walk-forward on the shared schedule "
                        f"({metrics['baseline']['n']} folds)")
    table.add_column("arm"); table.add_column("discipline"); table.add_column("Brier"); table.add_column("AUC"); table.add_column("Accuracy")
    table.add_row("baseline (prior-freq)", "safe", f"{baseline['brier']:.4f}", "n/a", f"{baseline['accuracy']:.4f}")
    for name, mm in models.items():
        m = mm["mean"]
        table.add_row(name, mm["leakage_discipline"],
                      f"{m['brier']:.4f}" if m.get("brier") is not None else "n/a",
                      f"{m['auc']:.4f}" if m.get("auc") is not None else "n/a",
                      f"{m['accuracy']:.4f}" if m.get("accuracy") is not None else "n/a")
    console.print(Panel(f"Leakage ablation, h={h}, threshold={args.label_threshold}", title="approach2_leakage_ablation"))
    console.print(table)
    console.print("[yellow]Leaky arms intentionally violate the information-set rule; they are demonstrations, "
                  "not valid predictive models.[/]")
    console.print(f"[bold]Wrote[/] metrics.json, predictions.csv, figures → {out_dir.relative_to(REPO)}")
    console.print(f"fit_seconds = {fit_seconds:.1f}")


if __name__ == "__main__":
    main()

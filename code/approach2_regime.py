"""QF5313 Q3 — Option B (Sequence Learning): leakage-safe LSTM/GRU regime classifier.

Task: one-month-ahead PMI contraction regime classification.
    d_t = 1{ pmi_man_t < threshold and pmi_man_(t-1) < threshold }
A prediction at month-end forecast origin t targets d_(t+1).

Shared fixed schedule (interface.md §5.1):
  - initial historical labels 2008-02 .. 2019-12,
    evolving with expanding-window walk-forward validation;
  - out-of-sample target months 2020-01 .. 2026-10 (82 folds, fold id = target month);
  - month-end forecast origins 2019-12-31 .. 2026-09-30.

Information-set discipline (interface.md §4):
  F_t = { x_(i,s) : s + L_i <= t }, with per-indicator publication_lag_months L_i
  from data/metadata.json and month-end origins. Implemented by shifting every
  indicator series forward by its own lag: shifted_value_i(s) = x_i(s - L_i), so a
  row of the shifted panel at month s contains exactly the observations of series i
  eligible at month-end s.

Preprocessing discipline:
  - imputation means, standardization statistics, feature-drop rules, class-imbalance
    weights and decision thresholds are fitted on the expanding training window only;
  - a 24-month validation tail inside the training window is used for early stopping
    and threshold selection (never test data).

Pipeline: load → verify hashes → labels → lag-shifted panel → h selection
(inner validation, training data only) → expanding walk-forward train/evaluate →
write metrics.json, per-seed predictions.csv, plotly figures.

Run:  uv run python code/approach2_regime.py --seed 42
      uv run python code/approach2_regime.py --seed 42 --label-threshold 49.5
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from sklearn.metrics import accuracy_score, brier_score_loss, balanced_accuracy_score, roc_auc_score

REPO = Path(__file__).resolve().parents[1]
PANEL_CSV = REPO / "data" / "clean" / "macro_panel.csv"
METADATA_JSON = REPO / "data" / "metadata.json"

# Fixed shared regime-classification schedule (interface.md §5.1)
TRAIN_LABEL_START = pd.Period("2008-02", "M")
TEST_TARGET_START = pd.Period("2020-01", "M")
TEST_TARGET_END = pd.Period("2026-10", "M")
VAL_TAIL = 24          # label months held out inside each expanding training window
MISSING_DROP_FRAC = 0.2  # fold-level rule: drop indicator if >20% missing on fit window
H_CANDIDATES_DEFAULT = [3, 6, 12]

ARCHS = ["lstm", "gru"]
DEFAULT_SEEDS = [42, 7, 123]  # >=3 seeds; global default 42 included (assignment §5.4)


def month_end(p: pd.Period) -> str:
    return pd.Timestamp(p.end_time.date()).strftime("%Y-%m-%d")


class RNNClassifier(nn.Module):
    """LSTM/GRU, <=2 layers, dropout between layers; last hidden state -> logit."""

    def __init__(self, arch: str, n_features: int, hidden: int, layers: int, dropout: float):
        super().__init__()
        rnn_cls = nn.LSTM if arch == "lstm" else nn.GRU
        self.rnn = rnn_cls(
            n_features, hidden, num_layers=layers, batch_first=True,
            dropout=dropout if layers > 1 else 0.0,
        )
        self.head = nn.Sequential(nn.Linear(hidden, 1))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        _, hh = self.rnn(x)         # LSTM returns (h_n, c_n); GRU returns h_n
        if isinstance(hh, tuple):
            hh = hh[0]
        return self.head(hh[-1]).squeeze(-1)  # last layer's final hidden state: (batch, hidden)


def train_one(
    arch: str,
    X_fit: np.ndarray, y_fit: np.ndarray,
    X_val: np.ndarray, y_val: np.ndarray,
    params: dict, seed: int,
) -> tuple[RNNClassifier, int, float]:
    """Fit one model; early stopping on the validation tail. Returns (net, epochs, best_val)."""
    torch.manual_seed(seed)
    np.random.seed(seed)
    net = RNNClassifier(arch, X_fit.shape[2], params["hidden"], params["layers"], params["dropout"])
    pw = float((y_fit == 0).sum() / max((y_fit == 1).sum(), 1))
    pw = min(pw, params.get("pos_weight_cap", 1.0))  # imbalance: optional loss weighting, capped
    loss_fn = nn.BCEWithLogitsLoss(pos_weight=torch.tensor(pw, dtype=torch.float32))
    opt = torch.optim.Adam(net.parameters(), lr=params["lr"], weight_decay=params["weight_decay"])
    Xf = torch.tensor(X_fit, dtype=torch.float32)
    yf = torch.tensor(y_fit, dtype=torch.float32)
    Xv = torch.tensor(X_val, dtype=torch.float32)
    yv = torch.tensor(y_val, dtype=torch.float32)

    g = torch.Generator().manual_seed(seed)
    best_val, best_state, best_epoch, bad = math.inf, None, 0, 0
    for epoch in range(1, params["max_epochs"] + 1):
        net.train()
        perm = torch.randperm(Xf.shape[0], generator=g)
        for idx in perm.split(params["batch_size"]):
            opt.zero_grad()
            loss = loss_fn(net(Xf[idx]), yf[idx])
            loss.backward()
            opt.step()
        net.eval()
        with torch.no_grad():
            val_loss = float(loss_fn(net(Xv), yv))
        if val_loss < best_val - 1e-5:
            best_val, best_state, best_epoch, bad = val_loss, {k: v.clone() for k, v in net.state_dict().items()}, epoch, 0
        else:
            bad += 1
            if bad >= params["patience"]:
                break
    net.load_state_dict(best_state)
    return net, best_epoch, best_val


def predict_proba(net: RNNClassifier, X: np.ndarray) -> np.ndarray:
    net.eval()
    with torch.no_grad():
        return torch.sigmoid(net(torch.tensor(X, dtype=torch.float32))).numpy()


def pick_threshold(y_val: np.ndarray, p_val: np.ndarray) -> float:
    """Decision threshold from the validation tail only (balanced accuracy)."""
    if len(np.unique(y_val)) < 2:
        return 0.5
    grid = np.unique(np.round(p_val, 4))
    if len(grid) > 99:
        grid = np.quantile(p_val, np.linspace(0.02, 0.98, 99))
    best_t, best_ba = 0.5, -1.0
    for t in grid:
        ba = balanced_accuracy_score(y_val, (p_val >= t).astype(int))
        if ba > best_ba + 1e-12:
            best_ba, best_t = ba, float(t)
    return best_t


def load_and_verify() -> tuple[pd.DataFrame, dict]:
    df = pd.read_csv(PANEL_CSV)
    df["month"] = pd.PeriodIndex(df.pop("month"), freq="M")
    sha = hashlib.sha256(PANEL_CSV.read_bytes()).hexdigest()
    meta = json.loads(METADATA_JSON.read_text(encoding="utf-8"))
    if sha != meta["panel_sha256"]:
        raise SystemExit(f"SHA-256 mismatch for {PANEL_CSV}: frozen snapshot changed; aborting.")
    monthly_cols = [e["name"] for e in meta["indicators"] if e["name"] in list(df.columns)]
    if len(monthly_cols) != int(meta["n_monthly_indicators"]) or not set(monthly_cols) <= set(df.columns):
        raise SystemExit("Monthly indicator names in panel do not match metadata; aborting.")
    return df.sort_values("month").reset_index(drop=True), meta


def build_labels(pmi: pd.Series, threshold: float) -> pd.Series:
    """d_t = 1{PMI_t < thr and PMI_(t-1) < thr}, index = reference month Period."""
    return ((pmi < threshold) & (pmi.shift(1) < threshold)).astype(float).where(pmi.notna() & pmi.shift(1).notna())


def build_shifted_panel(df: pd.DataFrame, meta: dict) -> pd.DataFrame:
    """shifted_i(s) = x_i(s - L_i): row s holds only what is eligible by month-end s."""
    mi = df.set_index("month").index
    out = {}
    for e in meta["indicators"]:
        col, lag = e["name"], int(e["publication_lag_months"])
        if col not in df.columns:  # quarterly entries are not part of the monthly panel
            continue
        out[col] = df.set_index("month")[col].shift(lag)
    return pd.DataFrame(out, index=mi)


class DataBundle:
    def __init__(self, df: pd.DataFrame, meta: dict, threshold: float):
        self.months: pd.PeriodIndex = df["month"].to_list()
        self.pmi: pd.Series = df.set_index("month")["pmi_man"]
        self.labels: pd.Series = build_labels(self.pmi, threshold)
        self.shifted = build_shifted_panel(df, meta)
        self.raw = df.set_index("month")
        # all 30 indicators are candidate features, incl. pmi_man (PMI_t is published at month-end t, i.e. available at origin t)
        self.feature_cols = [c for c in self.raw.columns]
        self.first_month = self.months[0]


def window_X(bundle: DataBundle, origin: pd.Period, h: int) -> np.ndarray:
    """Rows (origin-h+1 .. origin) of the lag-shifted panel; NaN handled downstream."""
    rows = bundle.shifted.loc[origin - (h - 1): origin, bundle.feature_cols]
    return rows.to_numpy(dtype=np.float64)


def pairs_for_train(bundle: DataBundle, origin: pd.Period, h: int) -> list[pd.Period]:
    """Training-pair origins u <= origin-1 whose target label d_(u+1) is published by origin."""
    lo = bundle.first_month + (h - 1)
    out = []
    u = lo
    while u <= origin - 1:
        tgt = u + 1
        if tgt in bundle.labels.index and pd.notna(bundle.labels.loc[tgt]):
            out.append(u)
        u += 1
    return out


def make_arrays(bundle: DataBundle, fit_origins, val_origins, test_origin, h: int):
    X_fit = np.stack([window_X(bundle, u, h) for u in fit_origins])
    y_fit = np.array([bundle.labels.loc[u + 1] for u in fit_origins])
    X_val = np.stack([window_X(bundle, u, h) for u in val_origins])
    y_val = np.array([bundle.labels.loc[u + 1] for u in val_origins])
    if test_origin is None:
        return X_fit, y_fit, X_val, y_val, None
    X_test = window_X(bundle, test_origin, h)[None, ...]
    return X_fit, y_fit, X_val, y_val, X_test


def transform_fit(X_fit: np.ndarray, y_fit: np.ndarray, X_val: np.ndarray, X_test):
    """Fit-window-only imputation/standardization/feature selection. Returns arrays + audit."""
    n, fld = X_fit.shape[0], X_fit.shape[2]
    miss_frac = np.isnan(X_fit.reshape(-1, fld)).mean(axis=0)
    keep = miss_frac <= MISSING_DROP_FRAC
    var_keep = keep.copy()
    means = np.zeros(fld); stds = np.ones(fld)
    for j in range(fld):
        if not keep[j]:
            continue
        colv = X_fit[:, :, j].ravel()
        colv = colv[~np.isnan(colv)]
        if colv.size == 0:
            keep[j] = False
            continue
        mu = colv.mean()
        sd = colv.std(ddof=0)
        if sd < 1e-12:  # zero-variance: constant series carries no signal
            var_keep[j] = False
        means[j], stds[j] = mu, sd
    final = keep & var_keep

    def tf(X):
        if X is None:
            return None
        Z = X[:, :, final].copy()
        mu, sd = means[final], stds[final]
        nan_mask = np.isnan(Z)
        if nan_mask.any():  # residual NaN (allowed on val/test rows): impute with fit mean
            idx = np.where(nan_mask)
            Z[idx] = np.take(mu, idx[1])
        return (Z - mu) / sd

    audit = {
        "n_features_input": fld,
        "n_features_used": int(final.sum()),
        "dropped_missing_idx": [j for j in range(fld) if not keep[j]],
        "dropped_zero_variance_idx": [j for j in range(fld) if keep[j] and not var_keep[j]],
    }
    return tf(X_fit), y_fit, tf(X_val), tf(X_test), audit, final


def evaluate(y_true: np.ndarray, y_pred_proba: np.ndarray, y_pred: np.ndarray) -> dict:
    out = {
        "brier": float(brier_score_loss(y_true, y_pred_proba)),
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "n": int(len(y_true)),
    }
    out["auc"] = float(roc_auc_score(y_true, y_pred_proba)) if len(np.unique(y_true)) == 2 else None
    return out



def main() -> None:
    ap = argparse.ArgumentParser(description="Option B leakage-safe LSTM/GRU regime classifier")
    ap.add_argument("--seed", type=int, default=42, help="global default seed (must be included among seeds)")
    ap.add_argument("--label-threshold", type=float, default=50.0)
    ap.add_argument("--h-candidates", type=str, default="3,6,12")
    ap.add_argument("--pos-weight-cap", type=float, default=1.0,
                    help="cap on BCE pos_weight (=1.0: no loss weighting; imbalance handled by validation threshold selection)")
    ap.add_argument("--folds-limit", type=int, default=None, help="smoke-test: evaluate only first N folds")
    ap.add_argument("--hidden", type=int, default=32)
    ap.add_argument("--layers", type=int, default=2)
    ap.add_argument("--dropout", type=float, default=0.2)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--weight-decay", type=float, default=1e-4)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--max-epochs", type=int, default=200)
    ap.add_argument("--patience", type=int, default=20)
    args = ap.parse_args()
    global H_CANDIDATES_DEFAULT
    H_CANDIDATES_DEFAULT = [int(x) for x in args.h_candidates.split(",")]

    console = Console()
    t0 = time.time()
    df, meta = load_and_verify()
    bundle = DataBundle(df, meta, args.label_threshold)

    th_tag = "" if abs(args.label_threshold - 50.0) < 1e-9 else f"_threshold{str(args.label_threshold).replace('.', '_')}"
    out_dir = REPO / "results" / "approach2" / f"regime{th_tag}"
    out_dir.mkdir(parents=True, exist_ok=True)

    seeds = sorted({int(args.seed), *DEFAULT_SEEDS})  # includes global default 42
    params = {
        "task": "regime_classification",
        "target": f"d_{{t+1}} = 1{{pmi_man<thr and pmi_man(t-1)<thr}}",
        "label_threshold": args.label_threshold,
        "label_definition": "two consecutive months of PMI_man below threshold",
        "archs": ARCHS,
        "seeds": seeds,
        "lookback_h_selected": None,
        "h_candidates": H_CANDIDATES_DEFAULT,
        "hidden": args.hidden, "layers": args.layers, "dropout": args.dropout,
        "lr": args.lr, "weight_decay": args.weight_decay,
        "batch_size": args.batch_size, "max_epochs": args.max_epochs, "patience": args.patience,
        "class_imbalance": "explicit: decision threshold selected on the 24-month validation tail (balanced accuracy); optional capped BCE pos_weight",
        "preprocessing": "per-fold fit-window-only mean imputation + z-score; drop series >20% missing on fit window or zero-variance",
        "validation": f"expanding-window walk-forward; {VAL_TAIL}-month label tail inside the training window for early stopping + threshold selection",
        "leakage_discipline": "safe",
        "information_set": "F_t = {x_(i,s): s + L_i <= t}; per-indicator publication_lag_months from data/metadata.json; month-end origins",
        "schedule": {
            "train_label_start": str(TRAIN_LABEL_START),
            "test_targets": f"{TEST_TARGET_START}:{TEST_TARGET_END}",
            "forecast_origins": "month-end; origin month t predicts target month t+1",
        },
        "residual_nondeterminism": "CPU LSTM multithreaded reductions may vary last-bit; seeds fixed for init/data order",
    }

    # --- lookback h selection on an inner validation block (2018-01..2019-12 targets) ---
    inner_targets = pd.period_range("2018-01", "2019-12", freq="M")
    h_scores = {}
    for h in H_CANDIDATES_DEFAULT:
        val_u = [t - 1 for t in inner_targets]
        fit_u = [u for u in pairs_for_train(bundle, val_u[-1], h) if u < val_u[0]]
        Xf, yf, Xv, yv, _ = make_arrays(bundle, fit_u, val_u, None, h)
        Zf, yfz, Zv, _Zt, a_h, final_h = transform_fit(Xf, yf, Xv, None)
        scores = []
        for arch in ARCHS:
            for seed in seeds:
                net, ep, bv = train_one(arch, Zf, yfz, Zv, yv, params, seed)
                pv = predict_proba(net, Zv)
                scores.append(brier_score_loss(yv, pv))
        h_scores[h] = float(np.mean(scores))
    h_best = min(h_scores, key=h_scores.get)
    h = h_best
    params["lookback_h_selected"] = h
    params["h_selection"] = {"method": "mean Brier on inner validation targets 2018-01..2019-12, fit before 2018-01; training data only",
                             "candidate_scores": h_scores}

    console.print(Panel(f"Option B regime classifier — safe discipline, h={h}, threshold={args.label_threshold}", title="approach2_regime"))
    console.print(f"[bold]h selection (mean Brier, lower is better):[/] " + ", ".join(f"h={k}: {v:.4f}" for k, v in h_scores.items()))

    test_targets = pd.period_range(TEST_TARGET_START, TEST_TARGET_END, freq="M")
    if args.folds_limit:
        test_targets = test_targets[: args.folds_limit]

    fold_history = []
    pred_rows = []
    for t_idx, target in enumerate(test_targets):
        origin = target - 1  # month-end origin t, target d_(t+1)
        all_u = pairs_for_train(bundle, origin, h)
        if len(all_u) <= VAL_TAIL:
            raise SystemExit(f"Fold {target}: too few training pairs.")
        fit_u, val_u = all_u[:-VAL_TAIL], all_u[-VAL_TAIL:]
        Xf, yf, Xv, yv, Xt = make_arrays(bundle, fit_u, val_u, origin, h)
        Zf, yfz, Zv, Zt, audit, final = transform_fit(Xf, yf, Xv, Xt)
        y_true = float(bundle.labels.loc[target])
        prior = float(yfz.mean())  # naive prior-frequency baseline from fit window
        rec = {
            "fold": str(target),
            "forecast_origin": month_end(origin),
            "target_period": str(target),
            "train_label_range": f"{fit_u[0] + 1}:{fit_u[-1] + 1}",
            "n_train_pairs": len(fit_u),
            "val_label_range": f"{val_u[0] + 1}:{val_u[-1] + 1}",
            **audit,
            "feature_names_used": [bundle.feature_cols[j] for j in range(len(final)) if final[j]],
            "y_true": y_true,
            "baseline_prior_probability": prior,
            "baseline_brier": float(brier_score_loss(np.array([y_true]), np.array([prior]))),
            "per_model": {},
        }
        for arch in ARCHS:
            for seed in seeds:
                net, ep, bv = train_one(arch, Zf, yfz, Zv, yv, params, seed)
                p = predict_proba(net, Zt)[0]
                thr = pick_threshold(yv, predict_proba(net, Zv))
                yhat = int(p >= thr)
                m = evaluate(np.array([y_true]), np.array([p]), np.array([yhat]))
                rec["per_model"].setdefault(arch, {})[str(seed)] = {
                    "y_pred_proba": float(p), "y_pred": yhat, "threshold": thr,
                    "epochs": ep, "val_loss": bv, **m,
                }
                pred_rows.append({
                    "model": arch, "forecast_origin": rec["forecast_origin"], "target_period": rec["target_period"],
                    "y_true": y_true, "y_pred_proba": float(p), "y_pred": yhat,
                    "fold": rec["fold"], "seed": seed,
                })
        fold_history.append(rec)

    # ---------- aggregation ----------
    y_true_all = np.array([r["y_true"] for r in fold_history])
    agg = {"baseline": {}, "per_model": {}}
    base_p = np.array([r["baseline_prior_probability"] for r in fold_history])
    agg["baseline"] = {
        "name": "naive prior-frequency (train-window mean)",
        "brier": float(brier_score_loss(y_true_all, base_p)),
        "accuracy": float(accuracy_score(y_true_all, (base_p >= 0.5).astype(int))),
        "auc": None, "n": int(len(y_true_all)),
    }
    for arch in ARCHS:
        per_seed = {}
        for seed in seeds:
            sub = [r["per_model"][arch][str(seed)] for r in fold_history]
            ps = np.array([x["y_pred_proba"] for x in sub]); ph = np.array([x["y_pred"] for x in sub])
            per_seed[str(seed)] = evaluate(y_true_all, ps, ph)
        probs = np.array([per_seed[str(s)]["brier"] for s in seeds])
        accs = [per_seed[str(s)]["accuracy"] for s in seeds]
        aucs = [per_seed[str(s)]["auc"] for s in seeds]
        agg["per_model"][arch] = {
            "per_seed": per_seed,
            "mean": {"brier": float(np.mean(probs)), "accuracy": float(np.mean(accs)),
                     "auc": float(np.mean([a for a in aucs if a is not None])) if all(a is not None for a in aucs) else None},
            "std": {"brier": float(np.std(probs, ddof=1)), "accuracy": float(np.std(accs, ddof=1)),
                    "auc": float(np.std([a for a in aucs if a is not None], ddof=1)) if all(a is not None for a in aucs) else None},
            "n_folds": int(len(fold_history)),
        }

    fit_seconds = time.time() - t0
    metrics = {
        "params": params,
        "metrics": agg,
        "fit_seconds": fit_seconds,
        "history": {"folds": fold_history, "h_selection": params["h_selection"]},
    }
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=1), encoding="utf-8")
    pd.DataFrame(pred_rows).to_csv(out_dir / "predictions.csv", index=False)

    # ---------- figures (plotly, self-contained HTML) ----------
    import plotly.graph_objects as go
    xs = [r["target_period"] for r in fold_history]
    fig = go.Figure()
    for arch in ARCHS:
        proba_mean = np.array([float(np.mean([r["per_model"][arch][str(s)]["y_pred_proba"] for s in seeds])) for r in fold_history])
        fig.add_trace(go.Scatter(x=xs, y=proba_mean, mode="lines+markers", name=f"{arch} mean P(contraction), {len(seeds)} seeds"))
    fig.add_trace(go.Scatter(x=xs, y=y_true_all, mode="markers", yaxis="y2", name="realized d_(t+1)"))
    fig.add_trace(go.Scatter(x=xs, y=base_p, mode="lines", line=dict(dash="dot"), name="baseline prior frequency"))
    fig.update_layout(
        title=f"Option B regime classification — walk-forward P(d_(t+1)=1) (threshold={args.label_threshold}, leakage-safe)",
        xaxis_title="target month", yaxis=dict(title="probability"), yaxis2=dict(overlaying="y", side="right", range=[-0.1, 1.1], title="realized label"),
        legend=dict(orientation="h"),
    )
    (out_dir / "figures").mkdir(exist_ok=True)
    fig.write_html(out_dir / "figures" / "walkforward_probabilities.html", include_plotlyjs=True)

    fig2 = go.Figure()
    for arch in ARCHS:
        b = np.array([sum(r["per_model"][arch][str(s)]["brier"] for s in seeds) / len(seeds) for r in fold_history])
        fig2.add_trace(go.Scatter(x=xs, y=b, mode="lines+markers", name=f"{arch} Brier (seed mean)"))
    base_b = np.array([r["baseline_brier"] for r in fold_history])
    fig2.add_trace(go.Scatter(x=xs, y=base_b, mode="lines", line=dict(dash="dot"), name="baseline Brier"))
    fig2.update_layout(title="Walk-forward Brier score by fold (lower is better)", xaxis_title="target month", yaxis_title="Brier")
    fig2.write_html(out_dir / "figures" / "walkforward_brier.html", include_plotlyjs=True)

    # ---------- rich summary ----------
    table = Table(title="Walk-forward results (pooled over folds; mean ± std across seeds)")
    table.add_column("model"); table.add_column("Brier"); table.add_column("AUC"); table.add_column("Accuracy"); table.add_column("n")
    b, a, ac, n = agg["baseline"]["brier"], agg["baseline"]["auc"], agg["baseline"]["accuracy"], agg["baseline"]["n"]
    table.add_row("baseline (prior-freq)", f"{b:.4f}", "n/a", f"{ac:.4f}", str(n))
    for arch in ARCHS:
        m, s = agg["per_model"][arch]["mean"], agg["per_model"][arch]["std"]
        seed_briers = [agg["per_model"][arch]["per_seed"][str(x)]["brier"] for x in seeds]
        table.add_row(arch,
                      f"{m['brier']:.4f} ± {s['brier']:.4f}  ({', '.join(f'{x:.4f}' for x in seed_briers)})",
                      f"{m['auc']:.4f} ± {s['auc']:.4f}" if m["auc"] is not None else "n/a",
                      f"{m['accuracy']:.4f} ± {s['accuracy']:.4f}", str(agg["per_model"][arch]["n_folds"]))
    console.print(table)
    console.print(f"[bold]Wrote[/] metrics.json, predictions.csv, figures → {out_dir.relative_to(REPO)}")
    console.print(f"fit_seconds = {fit_seconds:.1f}")


if __name__ == "__main__":
    main()

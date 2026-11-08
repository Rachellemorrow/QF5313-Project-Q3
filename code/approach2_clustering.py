"""QF5313 Q3 — Option B: latent macro states (hidden-state k-means clustering).

Task: apply k-means to LSTM/GRU hidden-state embeddings h_t, keeping the time
observation represented by each embedding (interface.md §6.4, §7 — Option B).

Design (leakage-safe):
  - Models are trained once, exactly as in approach2_regime.py (same lag-shifted
    panel, same standardization/imputation rules fitted on the training window
    only, same lookback h read from the headline regime run).
  - The descriptive clustering origin is the last full panel month-end
    (2026-10-31, i.e. one month after the final scheduled forecast origin
    2026-09-30): only labels observable by then enter training (all training
    targets d_(u+1) with u <= 2026-09, i.e. up to d_2026-10); the 2026-11
    partial-month panel row is excluded, and embeddings are produced for
    month-ends from the first fully usable month through 2026-10
    (2008-03..2026-10 for h = 3).
  - k is selected per architecture by mean silhouette across the >= 3 seeds
    (k in 2..6), using that seed's own embedding space — a descriptive
    diagnostic, never test-period predictive information.

Evaluation (three-layer framework, interface.md §9; no cross-method ARI/NMI):
  - per-seed silhouette in that seed's hidden-state embedding space;
  - pairwise cross-seed adjusted Rand index on the common set of embedded
    time observations (plus its mean), counts of common observations reported;
  - association with the PMI contraction labels ONLY after explicit time
    alignment: ARI/NMI versus d_t (contemporaneous labels) and versus d_(t+1)
    (the one-month-ahead target of the embedding window ending at t); both
    alignments are identified in metrics.json.

Run:  uv run python code/approach2_clustering.py --seed 42
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from itertools import combinations
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import torch
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.metrics import (
    adjusted_rand_score,
    normalized_mutual_info_score,
    silhouette_score,
)

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "code"))

import approach2_regime as reg  # shared leak-safe pipeline

SEEDS = reg.DEFAULT_SEEDS
ARCHS = ["lstm", "gru"]
K_CANDIDATES = [2, 3, 4, 5, 6]
VAL_TAIL = reg.VAL_TAIL


def hidden_state(net: torch.nn.Module, X: np.ndarray) -> np.ndarray:
    """Last-layer final hidden state h_t for each sequence row (batch, hidden)."""
    was_training = net.training
    net.eval()
    with torch.no_grad():
        _, hh = net.rnn(torch.tensor(X, dtype=torch.float32))
        if isinstance(hh, tuple):
            hh = hh[0]
        h = hh[-1].numpy()
    if was_training:
        net.train()
    return h


def main() -> None:
    ap = argparse.ArgumentParser(description="Option B latent-state clustering")
    ap.add_argument("--seed", type=int, default=42, help="global default seed (included among seeds)")
    ap.add_argument("--label-threshold", type=float, default=50.0)
    ap.add_argument("--archs", type=str, default="lstm,gru")
    ap.add_argument("--k-candidates", type=str, default="2,3,4,5,6")
    args = ap.parse_args()
    console = Console()
    t0 = time.time()
    df, meta = reg.load_and_verify()
    archs = [a for a in args.archs.split(",") if a]
    k_cands = [int(k) for k in args.k_candidates.split(",")]

    th_tag = "" if abs(args.label_threshold - 50.0) < 1e-9 else f"_threshold{str(args.label_threshold).replace('.', '_')}"
    regime_metrics_path = REPO / "results" / "approach2" / f"regime{th_tag}" / "metrics.json"
    if not regime_metrics_path.exists():
        raise SystemExit(f"Missing {regime_metrics_path}. Run first: uv run python code/approach2_regime.py --seed 42")
    h = int(json.loads(regime_metrics_path.read_text(encoding="utf-8"))["params"]["lookback_h_selected"])

    out_dir = REPO / "results" / "approach2" / f"clustering{th_tag}"
    figures_dir = out_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)

    bundle = SimpleNamespace(
        months=df["month"].to_list(),
        pmi=df.set_index("month")["pmi_man"],
        labels=reg.build_labels(df.set_index("month")["pmi_man"], args.label_threshold),
        shifted=reg.build_shifted_panel(df, meta),
        raw=df.set_index("month"),
        feature_cols=None, first_month=df["month"].iloc[0],
    )
    bundle.feature_cols = list(bundle.shifted.columns)

    # ---------------- training window = all label pairs observable at the clustering origin ---------------- #
    final_origin = bundle.months[-2]  # 2026-10 (last full panel month): exclude the partial-month 2026-11 row
    all_u = reg.pairs_for_train(bundle, final_origin, h)
    fit_u, val_u = all_u[:-VAL_TAIL], all_u[-VAL_TAIL:]
    Xf, yf, Xv, yv, _ = reg.make_arrays(bundle, fit_u, val_u, None, h)
    Zf, yfz, Zv, _Zt, audit, final_mask = reg.transform_fit(Xf, yf, Xv, None)

    # ---------------- embedding inputs: standardized windows for each scheduled month-end ---------------- #
    embed_origins = [m for m in bundle.months if bundle.first_month + (h - 1) <= m <= final_origin]
    X_emb = np.stack([reg.window_X(bundle, m, h) for m in embed_origins])  # (n_origins, h, n_feat)
    n_feat = X_emb.shape[2]
    # fit-window statistics for the embedding columns (same rule as transform_fit)
    miss = np.isnan(Xf.reshape(-1, n_feat)).mean(axis=0)
    means, stds, keep = np.zeros(n_feat), np.ones(n_feat), (miss <= reg.MISSING_DROP_FRAC) & final_mask
    for j in range(n_feat):
        if not keep[j]:
            continue
        colv = Xf[:, :, j].ravel()
        colv = colv[~np.isnan(colv)]
        means[j], stds[j] = float(colv.mean()), float(colv.std(ddof=0))
    Z_emb = (X_emb - means) / stds
    nan_idx = np.where(np.isnan(Z_emb))
    Z_emb[nan_idx] = np.take(means, nan_idx[1])
    Z_emb = Z_emb[:, :, keep]

    params = {  # mirrors approach2_regime.py headline parameters
        "hidden": 32, "layers": 2, "dropout": 0.2, "lr": 1e-3, "weight_decay": 1e-4,
        "batch_size": 32, "max_epochs": 200, "patience": 20, "pos_weight_cap": 1.0,
    }

    label_dt = np.array([float(bundle.labels.get(m, np.nan)) for m in embed_origins])
    label_dt1 = np.array([float(bundle.labels.get(m + 1, np.nan)) for m in embed_origins])

    results: dict = {
        "params": {
            "task": "latent_state_clustering",
            "clustering_origin": reg.month_end(final_origin),
            "lookback_h": h,
            "embedding_origins": f"{embed_origins[0]}:{embed_origins[-1]}",
            "n_embeddings": len(embed_origins),
            "seeds": SEEDS,
            "archs": archs,
            "k_candidates": k_cands,
            "rnn_hyperparameters": params,
            "feature_audit": audit,
            "feature_names_used": [bundle.feature_cols[j] for j in range(len(keep)) if keep[j]],
            "label_alignment": {
                "d_t": "contemporaneous label of the embedding's window-end month t",
                "d_(t+1)": "one-month-ahead target of the window ending at t (the regime-forecast target)",
            },
            "training": "all label pairs observable at the clustering origin (training targets "
                        "d_(u+1) up to d_2026-10; only labels observable then enter training); "
                        "24-month validation tail for early stopping",
            "leakage_discipline": "safe",
            "residual_nondeterminism": "CPU LSTM reductions may vary in last bits; seeds fixed",
        },
        "metrics": {},
        "fit_seconds": None,
        "history": {},
    }
    metrics = results["metrics"]
    rows: list[dict] = []
    emb_rows: list[dict] = []

    for arch in archs:
        per_seed: dict[str, dict] = {}
        seed_clusterings: dict[str, tuple[np.ndarray, np.ndarray]] = {}

        # ---- train one model per seed; extract embeddings ----
        for seed in SEEDS:
            net, ep, bv = reg.train_one(arch, Zf, yfz, Zv, yv, params, seed)
            H = hidden_state(net, Z_emb)  # (n_origins, hidden)
            per_seed[str(seed)] = {"epochs": ep, "val_loss": bv}
            seed_clusterings[str(seed)] = (H, None)

        # ---- k selection per seed's own embedding space; mean silhouette across seeds ----
        k_scores = {}
        for k in k_cands:
            sil = []
            for seed, (H, _) in seed_clusterings.items():
                km = KMeans(n_clusters=k, n_init=10, random_state=int(seed))
                lab = km.fit_predict(H)
                sil.append(float(silhouette_score(H, lab)))
            k_scores[k] = float(np.mean(sil))
        k_best = max(k_scores, key=k_scores.get)
        metrics.setdefault("k_selection", {})[arch] = {
            "method": "max mean silhouette across seeds, each seed in its own embedding space",
            "candidate_mean_silhouette": k_scores,
            "k_selected": int(k_best),
        }

        seed_labels: dict[str, np.ndarray] = {}
        per_seed_cluster: dict[str, dict] = {}
        for seed, (H, _) in seed_clusterings.items():
            km = KMeans(n_clusters=k_best, n_init=10, random_state=int(seed))
            lab = km.fit_predict(H)
            seed_labels[str(seed)] = lab
            per_seed_cluster[str(seed)] = {
                "silhouette": float(silhouette_score(H, lab)),
                "n_clusters": int(k_best),
            }

        # ---- pairwise cross-seed ARI on the common time observations ----
        common_mask = np.ones(len(embed_origins), dtype=bool)  # all months embedded for every seed
        pair_ari = {}
        for s1, s2 in combinations(seed_labels, 2):
            pair_ari[f"{s1}__{s2}"] = {
                "ari": float(adjusted_rand_score(seed_labels[s1][common_mask], seed_labels[s2][common_mask])),
                "n_common": int(common_mask.sum()),
            }
        ari_mean = float(np.mean([v["ari"] for v in pair_ari.values()])) if pair_ari else None

        # ---- explicit label alignments: d_t and d_(t+1) ----
        align = {}
        for key, y in [("d_t", label_dt), ("d_(t+1)", label_dt1)]:
            valid = ~np.isnan(y)
            if len(np.unique(y[valid])) < 2:
                align[key] = None
                continue
            align[key] = {
                "alignment": key,
                "n": int(valid.sum()),
                "ari_mean_over_seeds": float(np.mean(
                    [adjusted_rand_score(seed_labels[s][valid], y[valid].astype(int)) for s in seed_labels])),
                "nmi_mean_over_seeds": float(np.mean(
                    [normalized_mutual_info_score(seed_labels[s][valid], y[valid].astype(int)) for s in seed_labels])),
            }
            if key == "d_t":
                first_seed = next(str(s) for s in SEEDS if str(s) in seed_labels)
                share = {}
                for c in np.unique(seed_labels[first_seed][valid]):
                    sub = label_dt[valid & (seed_labels[first_seed][valid] == c)]
                    share[str(int(c))] = {
                        "n": int(sub.size),
                        "contraction_share": float(np.nanmean(sub)) if sub.size else None,
                    }
                align[key]["contraction_share_by_cluster"] = share
                align[key]["reference_seed_for_shares"] = int(first_seed)

        metrics[arch] = {
            "k": int(k_best),
            "per_seed": per_seed_cluster,
            "silhouette_mean": float(np.mean([v["silhouette"] for v in per_seed_cluster.values()])),
            "silhouette_std": float(np.std([v["silhouette"] for v in per_seed_cluster.values()], ddof=1)),
            "pairwise_ari": pair_ari,
            "pairwise_ari_mean": ari_mean,
            "label_alignment": align,
        }

        # ---- per-seed CSV rows (labels + embeddings kept as reproducible artifacts) ----
        for seed in seed_clusterings:
            H = seed_clusterings[seed][0]
            for i, m in enumerate(embed_origins):
                rows.append({
                    "model": arch, "seed": int(seed), "forecast_origin": reg.month_end(m),
                    "target_period": str(m + 1), "reference_period": str(m),
                    "latent_cluster_id": int(seed_labels[seed][i]),
                    "d_t": label_dt[i], "d_(t+1)": label_dt1[i],
                })
                emb_rows.append({"model": arch, "seed": int(seed), "forecast_origin": reg.month_end(m),
                                 **{f"dim_{j+1:02d}": float(H[i, j]) for j in range(H.shape[1])}})

        # ---- figures: PCA projection + timeline (per arch; seed 42 shown, mean track noted) ----
        import plotly.express as px
        proj = PCA(n_components=2, random_state=42).fit_transform(seed_clusterings["42"][0])
        fig_df = pd.DataFrame({
            "month": [str(m) for m in embed_origins],
            "PC1": proj[:, 0], "PC2": proj[:, 1],
            "cluster": seed_labels["42"],
            "d_t": np.where(np.isnan(label_dt), -1, label_dt),
            "d_(t+1)": np.where(np.isnan(label_dt1), -1, label_dt1),
        })
        fig = px.scatter(fig_df, x="PC1", y="PC2", color=fig_df["cluster"].astype(str),
                         symbol=fig_df["d_t"].astype(str),
                         hover_data={"month": True, "d_(t+1)": True},
                         title=f"Option B latent states — {arch} (seed 42, k={k_best}); PCA projection of hidden states; "
                               "symbol = contemporaneous d_t")
        fig.write_html(figures_dir / f"latent_{arch}_pca.html", include_plotlyjs=True)

        fig = go_timeline(fig_df, arch, k_best)
        fig.write_html(figures_dir / f"latent_{arch}_timeline.html", include_plotlyjs=True)

    results["fit_seconds"] = time.time() - t0
    results["history"] = {
        "k_selection": metrics.pop("k_selection", {}),
        "n_train_pairs": len(fit_u), "n_val_pairs": len(val_u),
        "train_label_range": f"{fit_u[0] + 1}:{fit_u[-1] + 1}",
        "val_label_range": f"{val_u[0] + 1}:{val_u[-1] + 1}",
        "per_seed_epochs_val_loss": {
            arch: {s: p.get("epochs") for s, p in metrics[arch]["per_seed"].items()} for arch in archs
        },
    }
    (out_dir / "metrics.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    pd.DataFrame(rows).to_csv(out_dir / "latent_clusters.csv", index=False)
    pd.DataFrame(emb_rows).to_csv(out_dir / "latent_embeddings.csv", index=False)

    # ---------------- rich summary ----------------
    table = Table(title="Hidden-state clustering (k-means; silhouettes are method-specific diagnostics)")
    table.add_column("arch"); table.add_column("k"); table.add_column("silhouette mean ± std"); table.add_column("cross-seed ARI mean"); table.add_column("ARI vs d_t"); table.add_column("NMI vs d_t")
    for arch in archs:
        m = metrics[arch]
        table.add_row(arch, str(m["k"]),
                      f"{m['silhouette_mean']:.4f} ± {m['silhouette_std']:.4f}",
                      f"{m['pairwise_ari_mean']:.4f}",
                      f"{m['label_alignment']['d_t']['ari_mean_over_seeds']:.4f}" if m["label_alignment"]["d_t"] else "n/a",
                      f"{m['label_alignment']['d_t']['nmi_mean_over_seeds']:.4f}" if m["label_alignment"]["d_t"] else "n/a")
    console.print(Panel(f"Latent-state clustering; h={h}; embeddings {embed_origins[0]}..{embed_origins[-1]} "
                        f"(n={len(embed_origins)})", title="approach2_clustering"))
    console.print(table)
    console.print(f"[bold]Wrote[/] metrics.json, latent_clusters.csv, latent_embeddings.csv, figures → {out_dir.relative_to(REPO)}")


def go_timeline(fig_df: pd.DataFrame, arch: str, k: int):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.35, 0.65], vertical_spacing=0.08)
    fig.add_trace(go.Scatter(x=fig_df["month"], y=fig_df["d_t"], mode="lines", line=dict(shape="hv", width=1),
                             name="d_t (PMI contraction label)"), row=1, col=1)
    for c in sorted(fig_df["cluster"].unique()):
        sub = fig_df[fig_df["cluster"] == c]
        fig.add_trace(go.Scatter(x=sub["month"], y=[int(c)] * len(sub), mode="markers",
                                 marker=dict(size=6), name=f"{arch} cluster {c} (seed 42)"), row=2, col=1)
    fig.update_layout(title=f"{arch} latent-state clusters over time (seed 42, k={k}) vs PMI contraction labels",
                      yaxis1=dict(title="d_t", range=[-0.5, 1.5], tickvals=[0, 1]),
                      yaxis2=dict(title="latent cluster id"),
                      legend=dict(orientation="h"))
    return fig


if __name__ == "__main__":
    main()

"""Option A - rolling 3-year indicator clustering.

    uv run python code/approach1_cluster.py --seed 42

For each valid 36-month window:
  * mask observations not published by the window end (s + L_i <= window_end);
  * retain indicators with >=95% coverage and non-zero variance;
  * remove exact linear dependencies (derived spreads and rank screening);
  * compute the correlation matrix on the common complete sample;
  * build d_ij = sqrt(2(1-rho_ij)) and verify Euclidean compatibility;
  * Ward linkage; silhouette for k=2..8; selected k and fixed k=4;
  * adjacent-window ARI on the common retained indicator universe;
  * lead-lag cross-correlations with derived single-quarter GDP growth.

The required distance and linkage are never replaced.  Non-Euclidean
pairwise-deletion correlations are avoided by the common-sample rule, and the
PSD / Euclidean identity checks are recorded for every window.
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
from scipy.cluster.hierarchy import dendrogram, fcluster, linkage
from scipy.spatial.distance import pdist, squareform
from sklearn.metrics import adjusted_rand_score, silhouette_score

import approach1_data as d

ROOT = d.ROOT
OUT_DIR = d.RESULTS_DIR / "clustering"
FIG_DIR = OUT_DIR / "figures"

WINDOW_MONTHS = 36
STEP_MONTHS = 12
COVERAGE_MIN = 0.95
K_MAX = 8
K_FIXED = 4
LEAD_LAG = 4
MIN_LEAD_LAG_OBS = 12

CONSOLE = Console()


# ---------------------------------------------------------------------------
# Window construction
# ---------------------------------------------------------------------------

def window_ends(panel: pd.DataFrame) -> list[str]:
    months = panel["month"].tolist()
    idx = d.month_index(months)
    ends = [f"{y}-12" for y in range(2010, 2026)]
    last = months[-1]
    if last not in ends:
        ends.append(last)
    return [e for e in ends if e in idx]


def window_bounds(panel: pd.DataFrame, end_month: str) -> tuple[int, int, str, str]:
    idx = d.month_index(panel["month"].tolist())
    end = idx[end_month]
    start = end - WINDOW_MONTHS + 1
    if start < 0:
        raise RuntimeError(f"window ending {end_month} starts before the panel")
    return start, end, panel.iloc[start]["month"], end_month


def mask_asof_window(
    panel: pd.DataFrame,
    lags: dict[str, int],
    indicators: list[str],
    start_idx: int,
    end_idx: int,
) -> pd.DataFrame:
    """Reference-month values available at the window end.

    A value for reference month s is eligible iff s + L_i <= window_end.
    """
    sub = panel.iloc[start_idx:end_idx + 1][indicators].to_numpy(dtype=float).copy()
    n = len(sub)
    for j, c in enumerate(indicators):
        lag = lags[c]
        last_allowed_pos = (end_idx - lag) - start_idx
        if last_allowed_pos < 0:
            sub[:, j] = np.nan
        elif last_allowed_pos < n - 1:
            sub[last_allowed_pos + 1:, j] = np.nan
    return pd.DataFrame(sub, index=panel.iloc[start_idx:end_idx + 1].index, columns=indicators)


def screen_and_clean(
    masked: pd.DataFrame,
    indicators: list[str],
) -> tuple[pd.DataFrame, list[str], pd.DataFrame, list[str]]:
    """Coverage screen, zero-variance screen, exact-collinearity screen."""
    n = len(masked)
    rows = []
    retained: list[str] = []
    for c in indicators:
        obs = masked[c].notna()
        cov = float(obs.mean()) if n else 0.0
        sd = float(masked.loc[obs, c].std(ddof=1)) if obs.sum() > 1 else 0.0
        keep = cov >= COVERAGE_MIN and np.isfinite(sd) and sd > 0
        if keep:
            retained.append(c)
        rows.append({
            "indicator": c,
            "n_obs": int(obs.sum()),
            "n_months": int(n),
            "coverage": cov,
            "std": sd,
            "retained": bool(keep),
            "drop_reason": "" if keep else ("coverage" if cov < COVERAGE_MIN else "zero_variance"),
        })
    audit = pd.DataFrame(rows)
    dropped: list[str] = []

    def mark_dropped(c: str, reason: str) -> None:
        mask = audit["indicator"] == c
        audit.loc[mask, "retained"] = False
        audit.loc[mask, "drop_reason"] = reason

    # Exact linear dependencies: the two derived spreads are exact
    # differences of retained component series.  Drop a spread only when both
    # of its components are still present; otherwise it carries unique
    # information.
    spread_components = {
        "shibor_spread_1y_on": ("shibor_on", "shibor_1y"),
        "lpr_spread_5y_1y": ("lpr_1y", "lpr_5y"),
    }
    for spread, comps in spread_components.items():
        if spread in retained and all(c in retained for c in comps):
            retained.remove(spread)
            dropped.append(spread)
            mark_dropped(spread, "exact_collinearity")

    def full_rank(cols: list[str]) -> bool:
        if len(cols) < 2:
            return False
        X = masked[cols].dropna()
        if len(X) < 2:
            return False
        Z = (X - X.mean()) / X.std(ddof=1)
        return int(np.linalg.matrix_rank(Z.to_numpy(dtype=float))) == len(cols)

    # Deterministic last-resort rank screen (later columns first).
    for c in list(reversed(retained)):
        if full_rank(retained):
            break
        if len(retained) <= 3:
            break
        retained.remove(c)
        dropped.append(c)
        mark_dropped(c, "exact_collinearity")

    if len(retained) < 3:
        raise RuntimeError("fewer than 3 indicators retained after screening")
    if not full_rank(retained):
        raise RuntimeError("retained correlation block is still rank deficient")
    return masked[retained].dropna(), retained, audit, dropped


# ---------------------------------------------------------------------------
# Clustering and verification
# ---------------------------------------------------------------------------

def correlation_distance(X: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, dict[str, float]]:
    Z = (X - X.mean()) / X.std(ddof=1)
    R = np.corrcoef(Z.to_numpy(dtype=float), rowvar=False)
    D = np.sqrt(np.maximum(0.0, 2.0 * (1.0 - R)))
    np.fill_diagonal(D, 0.0)

    eig = np.linalg.eigvalsh(R)
    U = Z.to_numpy(dtype=float)
    U = U / np.linalg.norm(U, axis=0, keepdims=True)
    D_euclid = squareform(pdist(U.T, metric="euclidean"))
    max_diff = float(np.max(np.abs(D - D_euclid)))

    checks = {
        "min_eigenvalue": float(eig.min()),
        "max_eigenvalue": float(eig.max()),
        "psd_ok": bool(eig.min() >= -1e-10),
        "euclidean_max_abs_diff": max_diff,
        "euclidean_ok": bool(max_diff < 1e-10),
        "symmetric": bool(np.allclose(D, D.T, atol=1e-12)),
        "zero_diagonal": bool(np.allclose(np.diag(D), 0.0)),
    }
    return R, D, checks


def ward_and_silhouette(D: np.ndarray) -> dict[str, Any]:
    condensed = squareform(D, checks=False)
    Z = linkage(condensed, method="ward")
    n = D.shape[0]
    k_max = min(K_MAX, n - 1)
    curves = []
    for k in range(2, k_max + 1):
        labels = fcluster(Z, k, criterion="maxclust")
        sil = float(silhouette_score(D, labels, metric="precomputed"))
        curves.append({"k": k, "silhouette": sil})
    best = max(curves, key=lambda x: (x["silhouette"], -x["k"]))
    labels_best = fcluster(Z, int(best["k"]), criterion="maxclust")
    labels_fixed = fcluster(Z, min(K_FIXED, k_max), criterion="maxclust")
    sil_fixed = float(silhouette_score(D, labels_fixed, metric="precomputed")) if len(set(labels_fixed)) > 1 else float("nan")
    return {
        "linkage": Z,
        "k_curve": curves,
        "k_selected": int(best["k"]),
        "silhouette_selected": float(best["silhouette"]),
        "silhouette_k4": sil_fixed,
        "labels_selected": labels_best,
        "labels_k4": labels_fixed,
    }


# ---------------------------------------------------------------------------
# GDP lead-lag and regime profile
# ---------------------------------------------------------------------------

def quarterly_aggregate(panel: pd.DataFrame, indicators: list[str], end_idx: int) -> pd.DataFrame:
    rows = []
    for i in range(end_idx + 1):
        month = panel.iloc[i]["month"]
        q = d.quarter_from_month(month)
        rows.append({"quarter": q, **{c: panel.iloc[i][c] for c in indicators}})
    long = pd.DataFrame(rows)
    out = long.groupby("quarter", sort=True)[indicators].mean(numeric_only=True)
    return out


def gdp_quarter_map(target: pd.DataFrame, panel: pd.DataFrame, end_idx: int) -> dict[str, float]:
    idx = d.month_index(panel["month"].tolist())
    out = {}
    for row in target.itertuples(index=False):
        if not np.isfinite(row.gdp_yoy_single_derived):
            continue
        em = d.quarter_end_month(row.quarter)
        if em not in idx:
            continue
        if idx[em] + d.GDP_PUBLICATION_LAG_MONTHS <= end_idx:
            out[row.quarter] = float(row.gdp_yoy_single_derived)
    return out


def lead_lag_table(
    panel: pd.DataFrame,
    target: pd.DataFrame,
    window_end: str,
    end_idx: int,
    indicators: list[str],
) -> pd.DataFrame:
    qx = quarterly_aggregate(panel, indicators, end_idx)
    gmap = gdp_quarter_map(target, panel, end_idx)
    gq = pd.Series(gmap)
    rows = []
    for c in indicators:
        if c not in qx.columns:
            continue
        for lag in range(-LEAD_LAG, LEAD_LAG + 1):
            pairs = []
            for q in qx.index:
                gq_series = gq.copy()
                # align indicator at q with GDP at q+lag
                q_target = str(pd.Period(q, freq="Q") + lag)
                if q in qx.index and q_target in gq.index:
                    xv = qx.loc[q, c]
                    gv = gq.loc[q_target]
                    if np.isfinite(xv) and np.isfinite(gv):
                        pairs.append((float(xv), float(gv)))
            if (len(pairs) >= MIN_LEAD_LAG_OBS
                    and np.std([p[0] for p in pairs]) > 0
                    and np.std([p[1] for p in pairs]) > 0):
                arr = np.asarray(pairs, float)
                corr = float(np.corrcoef(arr[:, 0], arr[:, 1])[0, 1])
            else:
                corr = np.nan
            rows.append({
                "window_end": window_end,
                "indicator": c,
                "lag_quarters": lag,
                "corr": corr,
                "n_obs": len(pairs),
            })
    return pd.DataFrame(rows)


def cluster_profile_table(
    window_frame: pd.DataFrame,
    labels: pd.DataFrame,
    window_end: str,
    clusters: pd.DataFrame,
) -> pd.DataFrame:
    lab = labels.set_index("month")["d"]
    frame = window_frame.copy()
    frame["d"] = [lab.get(m, np.nan) for m in frame["month"]]
    rows = []
    for cid, g in clusters.groupby("cluster_id"):
        members = list(g["indicator"])
        zcols = []
        for c in members:
            x = frame[c]
            z = (x - x.mean()) / x.std(ddof=1) if x.std(ddof=1) > 0 else x * 0
            zcols.append(z)
        if not zcols:
            continue
        Z = pd.concat(zcols, axis=1)
        Z.columns = members
        dvals = frame["d"]
        con = Z.loc[dvals == 1].mean()
        exp = Z.loc[dvals == 0].mean()
        rows.append({
            "window_end": window_end,
            "cluster_id": int(cid),
            "n_members": int(len(members)),
            "mean_z_contraction": float(con.mean()),
            "mean_z_expansion": float(exp.mean()),
            "difference": float(con.mean() - exp.mean()),
            "members": ";".join(members),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------

def write_fig(fig: go.Figure, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(path), include_plotlyjs=True, full_html=True)


def make_dendrogram_figure(window_results: list[dict[str, Any]]) -> None:
    n = len(window_results)
    cols = 3
    rows = int(np.ceil(n / cols))
    fig = make_subplots(rows=rows, cols=cols, subplot_titles=[w["window_end"] for w in window_results],
                        vertical_spacing=0.06, horizontal_spacing=0.04)
    for i, w in enumerate(window_results):
        r = i // cols + 1
        c = i % cols + 1
        dg = dendrogram(w["linkage"], no_plot=True)
        for xs, ys in zip(dg["icoord"], dg["dcoord"]):
            fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", line=dict(color="#2c7fb8", width=1),
                                     hoverinfo="skip", showlegend=False), row=r, col=c)
    fig.update_layout(title="Ward dendrograms for each valid rolling 36-month window",
                      template="plotly_white", height=320 * rows, width=1100,
                      margin=dict(t=70, b=30))
    fig.update_xaxes(showticklabels=False)
    write_fig(fig, FIG_DIR / "fig_dendrograms.html")


def make_silhouette_figure(sil_df: pd.DataFrame) -> None:
    fig = go.Figure()
    fig.add_trace(go.Bar(x=sil_df["window_end"], y=sil_df["silhouette_selected"], name="Selected k"))
    fig.add_trace(go.Scatter(x=sil_df["window_end"], y=sil_df["silhouette_k4"], mode="lines+markers", name="Fixed k=4"))
    fig.update_layout(title="Silhouette score by rolling window", xaxis_title="Window end",
                      yaxis_title="Silhouette (correlation distance)", template="plotly_white",
                      xaxis=dict(tickangle=45))
    write_fig(fig, FIG_DIR / "fig_silhouette.html")


def make_k_curve_figure(window_results: list[dict[str, Any]]) -> None:
    fig = go.Figure()
    for w in window_results:
        df = pd.DataFrame(w["k_curve"])
        fig.add_trace(go.Scatter(x=df["k"], y=df["silhouette"], mode="lines+markers", name=w["window_end"]))
    fig.update_layout(title="Silhouette vs number of clusters", xaxis_title="k", yaxis_title="Silhouette",
                      template="plotly_white")
    write_fig(fig, FIG_DIR / "fig_silhouette_k_curve.html")


def make_ari_figure(ari: pd.DataFrame) -> None:
    fig = go.Figure()
    fig.add_trace(go.Bar(x=ari["window_curr"], y=ari["ari"], name="Adjacent ARI"))
    fig.add_hline(y=0, line_color="grey")
    fig.update_layout(title="Adjacent-window cluster stability (common indicators)",
                      xaxis_title="Window end", yaxis_title="Adjusted Rand index",
                      template="plotly_white", xaxis=dict(tickangle=45))
    write_fig(fig, FIG_DIR / "fig_adjacent_ari.html")


def make_membership_figure(clusters: pd.DataFrame) -> None:
    piv = clusters.pivot(index="indicator", columns="window_end", values="cluster_id")
    fig = go.Figure(data=go.Heatmap(z=piv.values, x=list(piv.columns), y=list(piv.index),
                                    colorscale="Viridis", colorbar=dict(title="cluster"),
                                    hoverongaps=False))
    fig.update_layout(title="Cluster membership by window (blank = not retained)",
                      xaxis_title="Window end", yaxis_title="Indicator",
                      template="plotly_white", height=max(550, 18 * len(piv.index)),
                      xaxis=dict(tickangle=45))
    write_fig(fig, FIG_DIR / "fig_cluster_membership.html")


def make_lead_lag_figure(lead: pd.DataFrame, window_end: str) -> None:
    df = lead[lead["window_end"] == window_end]
    if df.empty:
        return
    piv = df.pivot(index="indicator", columns="lag_quarters", values="corr")
    fig = go.Figure(data=go.Heatmap(z=piv.values, x=list(piv.columns), y=list(piv.index),
                                    colorscale="RdBu", zmid=0, colorbar=dict(title="corr")))
    fig.update_layout(title=f"Indicator vs GDP lead-lag correlation (window ending {window_end})",
                      xaxis_title="GDP lead (-) / lag (+) in quarters", yaxis_title="Indicator",
                      template="plotly_white", height=max(550, 18 * len(piv.index)))
    write_fig(fig, FIG_DIR / "fig_lead_lag_gdp.html")


def make_profile_figure(profile: pd.DataFrame, window_end: str) -> None:
    df = profile[profile["window_end"] == window_end]
    if df.empty:
        return
    fig = go.Figure()
    fig.add_trace(go.Bar(x=df["cluster_id"], y=df["mean_z_contraction"], name="Contraction"))
    fig.add_trace(go.Bar(x=df["cluster_id"], y=df["mean_z_expansion"], name="Expansion"))
    fig.update_layout(title=f"Cluster profiles in contraction vs expansion (window ending {window_end})",
                      xaxis_title="Cluster", yaxis_title="Mean standardized level",
                      barmode="group", template="plotly_white")
    write_fig(fig, FIG_DIR / "fig_cluster_regime_profile.html")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(seed: int) -> dict[str, Any]:
    t0 = time.perf_counter()
    np.random.seed(seed)

    CONSOLE.print(Panel.fit(
        "[bold]Option A - rolling 3-year Ward clustering[/bold]\n"
        f"seed={seed} | 36-month windows | step {STEP_MONTHS}m | d_ij=sqrt(2(1-rho))",
        border_style="cyan",
    ))

    bundle = d.run(seed=seed, write=False, verbose=False)
    panel: pd.DataFrame = bundle["model_panel"]
    target: pd.DataFrame = bundle["target"]
    lags: dict[str, int] = bundle["lags"]
    indicators: list[str] = bundle["indicators"]
    labels50: pd.DataFrame = bundle["labels50"]

    ends = window_ends(panel)
    window_results: list[dict[str, Any]] = []
    silhouette_rows: list[dict[str, Any]] = []
    cluster_rows: list[dict[str, Any]] = []
    retention_rows: list[dict[str, Any]] = []
    lead_rows: list[dict[str, Any]] = []
    profile_rows: list[dict[str, Any]] = []

    for end_month in ends:
        start_idx, end_idx, start_month, _ = window_bounds(panel, end_month)
        masked = mask_asof_window(panel, lags, indicators, start_idx, end_idx)
        Xc, retained, audit, dropped = screen_and_clean(masked, indicators)
        n_common = len(Xc)
        if n_common < 24:
            raise RuntimeError(f"window {end_month}: only {n_common} common months")

        R, D, checks = correlation_distance(Xc)
        if not (checks["psd_ok"] and checks["euclidean_ok"]):
            raise RuntimeError(f"window {end_month}: Euclidean compatibility failed: {checks}")

        ward = ward_and_silhouette(D)
        labels = ward["labels_selected"]

        for c, cid in zip(retained, labels):
            cluster_rows.append({
                "window_end": end_month,
                "window_start": start_month,
                "indicator": c,
                "cluster_id": int(cid),
                "k_selected": int(ward["k_selected"]),
            })
        for row in audit.to_dict(orient="records"):
            retention_rows.append({"window_end": end_month, **row})

        silhouette_rows.append({
            "window_end": end_month,
            "window_start": start_month,
            "n_retained": int(len(retained)),
            "n_common_months": int(n_common),
            "n_dropped_collinear": int(len(dropped)),
            "dropped_collinear": ";".join(dropped),
            "min_eigenvalue": checks["min_eigenvalue"],
            "psd_ok": checks["psd_ok"],
            "euclidean_max_abs_diff": checks["euclidean_max_abs_diff"],
            "euclidean_ok": checks["euclidean_ok"],
            "k_selected": ward["k_selected"],
            "silhouette_selected": ward["silhouette_selected"],
            "silhouette_k4": ward["silhouette_k4"],
        })

        # lead-lag with GDP (as of window end)
        lead = lead_lag_table(panel, target, end_month, end_idx, retained)
        lead_rows.append(lead)

        # regime profile: build a frame with month + retained indicators
        frame = panel.iloc[start_idx:end_idx + 1][["month"] + retained].copy()
        clusters = pd.DataFrame({"indicator": retained, "cluster_id": labels})
        prof = cluster_profile_table(frame, labels50, end_month, clusters)
        profile_rows.append(prof)

        window_results.append({
            "window_end": end_month,
            "window_start": start_month,
            "retained": retained,
            "dropped": dropped,
            "n_common_months": n_common,
            "checks": checks,
            "linkage": ward["linkage"],
            "k_curve": ward["k_curve"],
            "k_selected": ward["k_selected"],
            "silhouette_selected": ward["silhouette_selected"],
            "silhouette_k4": ward["silhouette_k4"],
            "labels": labels,
        })

    clusters_df = pd.DataFrame(cluster_rows)
    retention_df = pd.DataFrame(retention_rows)
    sil_df = pd.DataFrame(silhouette_rows)
    lead_df = pd.concat(lead_rows, ignore_index=True) if lead_rows else pd.DataFrame()
    profile_df = pd.concat(profile_rows, ignore_index=True) if profile_rows else pd.DataFrame()

    # Adjacent-window ARI on the common retained universe.
    ari_rows = []
    for prev, curr in zip(window_results[:-1], window_results[1:]):
        common = sorted(set(prev["retained"]) & set(curr["retained"]))
        if len(common) < 2:
            ari_rows.append({
                "window_prev": prev["window_end"], "window_curr": curr["window_end"],
                "n_common_indicators": len(common), "ari": np.nan,
                "common_indicators": ";".join(common),
            })
            continue
        p = prev["labels"][[prev["retained"].index(c) for c in common]]
        q = curr["labels"][[curr["retained"].index(c) for c in common]]
        ari_rows.append({
            "window_prev": prev["window_end"],
            "window_curr": curr["window_end"],
            "n_common_indicators": len(common),
            "ari": float(adjusted_rand_score(p, q)),
            "common_indicators": ";".join(common),
        })
    ari_df = pd.DataFrame(ari_rows)

    # Cluster-level lead-lag aggregate from the per-window table.
    cluster_lead_rows = []
    for w in window_results:
        sub = lead_df[lead_df["window_end"] == w["window_end"]]
        for cid in sorted(set(int(x) for x in w["labels"])):
            members = [c for c, x in zip(w["retained"], w["labels"]) if int(x) == cid]
            ss = sub[sub["indicator"].isin(members)]
            if ss.empty:
                continue
            mean_by_lag = ss.groupby("lag_quarters")["corr"].mean()
            if mean_by_lag.dropna().empty:
                continue
            best_lag = int(mean_by_lag.abs().idxmax())
            cluster_lead_rows.append({
                "window_end": w["window_end"],
                "cluster_id": cid,
                "n_members": len(members),
                "best_lag_quarters": best_lag,
                "max_abs_mean_corr": float(abs(mean_by_lag.loc[best_lag])),
                "mean_corr_at_best": float(mean_by_lag.loc[best_lag]),
                "members": ";".join(members),
            })
    cluster_lead_df = pd.DataFrame(cluster_lead_rows)

    # ---- writes ------------------------------------------------------------
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    clusters_df.to_csv(OUT_DIR / "window_clusters.csv", index=False)
    retention_df.to_csv(OUT_DIR / "window_retention.csv", index=False)
    sil_df.to_csv(OUT_DIR / "silhouette_by_window.csv", index=False)
    ari_df.to_csv(OUT_DIR / "adjacent_ari.csv", index=False)
    lead_df.to_csv(OUT_DIR / "lead_lag_gdp.csv", index=False)
    cluster_lead_df.to_csv(OUT_DIR / "cluster_lead_lag.csv", index=False)
    profile_df.to_csv(OUT_DIR / "cluster_regime_profile.csv", index=False)

    fit_seconds = time.perf_counter() - t0
    metrics_payload = {
        "task": "indicator_clustering",
        "target": "rolling_3y_indicator_correlations",
        "approach": "A",
        "leakage_discipline": "safe",
        "seed": seed,
        "params": {
            "window_months": WINDOW_MONTHS,
            "step_months": STEP_MONTHS,
            "coverage_min": COVERAGE_MIN,
            "distance": "d_ij = sqrt(2*(1-rho_ij)), Pearson correlation on common complete sample",
            "linkage": "ward",
            "k_selection": f"max silhouette over k=2..{K_MAX}; fixed k={K_FIXED} sensitivity",
            "publication_lag_masking": "s + L_i <= window_end",
            "euclidean_requirement": "PSD correlation matrix + distance identity to normalized-vector Euclidean distance",
            "collinearity_rule": "derived spreads and deterministic rank screen; recorded per window",
            "lead_lag": {"max_quarters": LEAD_LAG, "min_observations": MIN_LEAD_LAG_OBS,
                          "gdp_target": "gdp_yoy_single_derived", "monthly_to_quarterly": "mean of available months"},
        },
        "metrics": {
            "n_windows": int(len(window_results)),
            "mean_silhouette_selected": float(sil_df["silhouette_selected"].mean()),
            "min_silhouette_selected": float(sil_df["silhouette_selected"].min()),
            "max_silhouette_selected": float(sil_df["silhouette_selected"].max()),
            "mean_silhouette_k4": float(sil_df["silhouette_k4"].mean()),
            "mean_adjacent_ari": float(ari_df["ari"].mean()),
            "min_adjacent_ari": float(ari_df["ari"].min()),
            "max_adjacent_ari": float(ari_df["ari"].max()),
            "mean_retained_indicators": float(sil_df["n_retained"].mean()),
            "mean_common_months": float(sil_df["n_common_months"].mean()),
            "all_psd_ok": bool(sil_df["psd_ok"].all()),
            "all_euclidean_ok": bool(sil_df["euclidean_ok"].all()),
            "max_euclidean_abs_diff": float(sil_df["euclidean_max_abs_diff"].max()),
            "n_windows_with_collinearity_drop": int((sil_df["n_dropped_collinear"] > 0).sum()),
        },
        "windows": [
            {
                "window_end": w["window_end"],
                "window_start": w["window_start"],
                "n_retained": len(w["retained"]),
                "retained": w["retained"],
                "dropped": w["dropped"],
                "n_common_months": w["n_common_months"],
                "checks": w["checks"],
                "k_selected": w["k_selected"],
                "silhouette_selected": w["silhouette_selected"],
                "silhouette_k4": w["silhouette_k4"],
                "k_curve": w["k_curve"],
                "membership": dict(zip(w["retained"], [int(x) for x in w["labels"]])),
            }
            for w in window_results
        ],
        "adjacent_ari": ari_df.to_dict(orient="records"),
        "fit_seconds": fit_seconds,
        "environment": {"python": sys.version.split()[0], "numpy": np.__version__, "pandas": pd.__version__},
        "outputs": {
            "clusters": "results/approach1/clustering/window_clusters.csv",
            "silhouette": "results/approach1/clustering/silhouette_by_window.csv",
            "adjacent_ari": "results/approach1/clustering/adjacent_ari.csv",
            "lead_lag": "results/approach1/clustering/lead_lag_gdp.csv",
        },
    }
    d.write_json(OUT_DIR / "metrics.json", metrics_payload)

    # figures
    make_dendrogram_figure(window_results)
    make_silhouette_figure(sil_df)
    make_k_curve_figure(window_results)
    make_ari_figure(ari_df)
    make_membership_figure(clusters_df)
    if not lead_df.empty:
        make_lead_lag_figure(lead_df, window_results[-1]["window_end"])
    if not profile_df.empty:
        make_profile_figure(profile_df, window_results[-1]["window_end"])

    tbl = Table(title="Rolling Ward clustering - summary", show_lines=False)
    tbl.add_column("Item", style="bold")
    tbl.add_column("Value", justify="right")
    tbl.add_row("Windows", str(len(window_results)))
    tbl.add_row("Mean retained indicators", f"{sil_df['n_retained'].mean():.1f}")
    tbl.add_row("Mean common months", f"{sil_df['n_common_months'].mean():.1f}")
    tbl.add_row("Mean silhouette (selected k)", f"{sil_df['silhouette_selected'].mean():.4f}")
    tbl.add_row("Mean adjacent ARI", f"{ari_df['ari'].mean():.4f}")
    tbl.add_row("Min eigenvalue (worst window)", f"{sil_df['min_eigenvalue'].min():.3e}")
    tbl.add_row("Max Euclidean identity diff", f"{sil_df['euclidean_max_abs_diff'].max():.3e}")
    tbl.add_row("All PSD / Euclidean", f"{bool(sil_df['psd_ok'].all())} / {bool(sil_df['euclidean_ok'].all())}")
    tbl.add_row("Elapsed", f"{fit_seconds:.1f}s")
    CONSOLE.print(tbl)
    CONSOLE.print("[green]Clustering complete[/green] | required distance and Ward linkage preserved")
    return metrics_payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Option A rolling indicator clustering")
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run(seed=args.seed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

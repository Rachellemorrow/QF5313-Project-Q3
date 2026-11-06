"""Q3 consolidated comparison layer.

    uv run python code/compare.py --seed 42

Reads every available ``metrics.json`` and produces ``results/comparison.html``.
Tasks are kept strictly separate: GDP nowcasting, regime classification and
clustering are never ranked against one another in a single table.

Option A is complete.  Option B sections are populated only from real
``results/approach2/**`` artifacts; when they are absent the report says so
instead of inventing results.  The clustering section implements the
three-layer comparison framework of ``interface.md`` Section 9 and never
computes ARI/NMI between Option A's indicator clusters and Option B's
time-point hidden-state clusters.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
A = RESULTS / "approach1"
B = RESULTS / "approach2"
OUT_HTML = RESULTS / "comparison.html"

CONSOLE = Console()


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_all_metrics() -> list[dict[str, Any]]:
    out = []
    for p in sorted(RESULTS.glob("approach*/**/metrics*.json")):
        try:
            m = load_json(p)
        except Exception:
            continue
        if isinstance(m, dict):
            m = dict(m)
            m["_path"] = str(p.relative_to(ROOT))
            out.append(m)
    return out


def esc(x: Any) -> str:
    return html.escape(str(x))


def fmt(x: Any, nd: int = 4) -> str:
    if x is None:
        return "-"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return esc(x)
    if pd.isna(v):
        return "-"
    return f"{v:.{nd}f}"


def table_html(headers: list[str], rows: list[list[Any]], caption: str = "") -> str:
    out = [f"<table><caption>{esc(caption)}</caption><thead><tr>"]
    out += [f"<th>{esc(h)}</th>" for h in headers]
    out.append("</tr></thead><tbody>")
    for r in rows:
        out.append("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def gdp_section() -> tuple[str, list[list[Any]]]:
    m = load_json(A / "gdp_nowcasting" / "metrics.json")
    if m is None:
        return "<p>GDP nowcasting metrics are not available.</p>", []
    met = m["metrics"]
    rows = []
    for v in ("M1", "M2", "M3"):
        mm = met["by_vintage"][v]
        bb = met["benchmark"]["rw_drift_by_vintage"][v]
        rows.append([
            v, mm["n"], fmt(mm["rmse"]), fmt(mm["mae"]), fmt(mm["bias"], 3),
            fmt(bb["rmse"]), fmt(1 - mm["rmse"] ** 2 / bb["rmse"] ** 2, 3),
        ])
    rows.append([
        "Overall", met["overall"]["n"], fmt(met["overall"]["rmse"]), fmt(met["overall"]["mae"]),
        fmt(met["overall"]["bias"], 3), fmt(met["benchmark"]["rw_drift"]["rmse"]),
        fmt(met["skill"]["brier_like_mse_skill_vs_rw_drift"], 3),
    ])
    rev = met["revision"]
    body = [
        "<h2>1. GDP nowcasting (Option A only)</h2>",
        "<p><b>Target:</b> derived chain-linked single-quarter real GDP YoY "
        "(<code>gdp_yoy_single_derived</code>), 2018Q1-2026Q3, M1/M2/M3 vintages. "
        "The source Q2-Q4 values are cumulative YTD and are not relabelled as single-quarter growth.</p>",
        table_html(["Vintage", "n", "RMSE", "MAE", "Bias", "RW-drift RMSE", "MSE skill"], rows,
                   "GDP nowcasting errors (percentage points)"),
        f"<p><b>Revision path:</b> mean |M1-M3| revision {fmt(rev['mean_abs_revision_M1_to_M3'])} pp; "
        f"RMSE M1 {fmt(rev['rmse_M1'])}, M2 {fmt(rev['rmse_M2'])}, M3 {fmt(rev['rmse_M3'])}; "
        f"share of quarters improved M1 to M3 {fmt(rev['share_quarters_improved_M1_to_M3'], 3)}.</p>",
        "<p><b>Benchmark:</b> random walk with training-window drift on the same target periods and vintages. "
        "The historical-mean benchmark is retained in <code>metrics.json</code>.</p>",
        figure_links("gdp_nowcasting", [
            "fig_gdp_target.html", "fig_nowcast_vs_realized.html", "fig_rmse_by_vintage.html",
            "fig_revision_path.html", "fig_pca_scree.html", "fig_factor_loadings.html",
        ]),
        "<p class='note'>The 2020Q1 M3 nowcast illustrates a known limitation of a static "
        "latest-value factor: the March PMI rebound enters the M3 information set while the "
        "February collapse does not. The within-quarter revision path is reported rather than hidden.</p>",
    ]
    return "\n".join(body), rows


def regime_section() -> tuple[str, list[list[Any]]]:
    main = load_json(A / "regime" / "metrics.json")
    if main is None:
        return "<p>Regime metrics are not available.</p>", []
    fo = load_json(A / "regime" / "metrics_factors_only.json")
    s495 = load_json(A / "regime" / "metrics_threshold_49p5.json")
    s495c = load_json(A / "regime" / "metrics_threshold_49p5_controls.json")

    rows = []

    def add_row(name: str, m: dict[str, Any] | None) -> None:
        if m is None:
            return
        o = m["metrics"]["overall"]
        b = m["metrics"]["baseline_prior_frequency"]
        rows.append([name, o["n"], o["n_positive"], fmt(o["brier"]), fmt(o["auc"]),
                     fmt(o["accuracy"]), fmt(o["balanced_accuracy"]), fmt(b["brier"]), fmt(b["auc"])])

    add_row("Probit, factors + LPR 1Y (primary)", main)
    add_row("Probit, factors only (sensitivity)", fo)
    add_row("Probit, 49.5 threshold, factors only", s495)
    add_row("Probit, 49.5 threshold, LPR control", s495c)

    persist = main["metrics"]["baseline_persistence"]
    prior = main["metrics"]["baseline_prior_frequency"]
    body = [
        "<h2>2. Regime classification (option A; Option B pending)</h2>",
        "<p><b>Label:</b> d_t = 1{pmi_man_t &lt; 50 and pmi_man_(t-1) &lt; 50}; target d_(t+1). "
        "Shared 82 target months 2020-01 to 2026-10; month-end origins 2019-12-31 to 2026-09-30; "
        "expanding window.</p>",
        table_html(["Model", "n", "Contractions", "Brier", "AUC", "Accuracy", "Balanced acc.",
                    "Prior Brier", "Prior AUC"], rows,
                   "Regime classification metrics on the same 82 target months"),
        f"<p><b>Prior-frequency baseline:</b> Brier {fmt(prior['brier'])}, accuracy {fmt(prior['accuracy'])}, "
        f"AUC {fmt(prior['auc'])} (constant per-fold training base rate). "
        f"<b>Persistence baseline:</b> Brier {fmt(persist['brier'])}, AUC {fmt(persist['auc'])}, "
        f"accuracy {fmt(persist['accuracy'])}.</p>",
        "<p class='warn'><b>Factual finding:</b> the primary static Probit does not beat the "
        "prior-frequency or persistence baselines out of sample. The factors-only and 49.5 "
        "variants are reported as sensitivities; none is presented as a validated improvement. "
        "This is a result to discuss, not a result to hide.</p>",
        "<p><b>Coefficient table:</b> final-fold coefficients with standard errors are in "
        "<code>results/approach1/regime/coefficients_final.csv</code>; across-fold summaries in "
        "<code>coefficients_summary.csv</code>.</p>",
        figure_links("regime", [
            "fig_regime_probabilities.html", "fig_regime_roc.html", "fig_regime_calibration.html",
            "fig_regime_brier.html", "fig_regime_coefficients.html",
        ]),
    ]
    return "\n".join(body), rows


def clustering_section() -> tuple[str, list[list[Any]]]:
    m = load_json(A / "clustering" / "metrics.json")
    if m is None:
        return "<p>Clustering metrics are not available.</p>", []
    sil = pd.read_csv(A / "clustering" / "silhouette_by_window.csv")
    ari = pd.read_csv(A / "clustering" / "adjacent_ari.csv")
    cl = pd.read_csv(A / "clustering" / "cluster_lead_lag.csv")
    prof = pd.read_csv(A / "clustering" / "cluster_regime_profile.csv")

    ari_by_curr = ari.set_index("window_curr")["ari"].to_dict()
    rows = []
    for r in sil.itertuples(index=False):
        rows.append([
            r.window_end, r.n_retained, r.n_common_months, r.k_selected,
            fmt(r.silhouette_selected), fmt(r.silhouette_k4),
            fmt(ari_by_curr.get(r.window_end)), fmt(r.min_eigenvalue, 3),
            fmt(r.euclidean_max_abs_diff, 3), "yes" if r.psd_ok else "NO",
        ])

    last = sil["window_end"].iloc[-1]
    last_cl = cl[cl["window_end"] == last].copy()
    last_prof = prof[prof["window_end"] == last].copy()
    cluster_rows = []
    for r in last_cl.itertuples(index=False):
        cluster_rows.append([
            r.cluster_id, r.n_members, r.best_lag_quarters, fmt(r.max_abs_mean_corr),
            fmt(r.mean_corr_at_best, 3), r.members,
        ])
    profile_rows = []
    for r in last_prof.itertuples(index=False):
        profile_rows.append([
            r.cluster_id, r.n_members, fmt(r.mean_z_contraction, 3),
            fmt(r.mean_z_expansion, 3), fmt(r.difference, 3),
        ])

    body = [
        "<h2>3. Indicator clustering (Option A)</h2>",
        "<h3>Layer 1 - method-specific quality and stability</h3>",
        "<p>Ward linkage on d_ij = sqrt(2(1-rho_ij)), Pearson correlations on a common complete "
        "sample per 36-month window. Coverage rule: at least 95%; derived spreads removed when "
        "their components are retained. PSD and Euclidean-identity checks are run for every window.</p>",
        table_html(["Window end", "d", "Months", "k", "Silhouette", "Sil k=4", "ARI vs prev",
                    "Min eig", "Max Euclid diff", "PSD"], rows,
                   "Option A clustering quality and stability"),
        f"<p>Mean silhouette {fmt(m['metrics']['mean_silhouette_selected'])}; mean adjacent ARI "
        f"{fmt(m['metrics']['mean_adjacent_ari'])}; all PSD {m['metrics']['all_psd_ok']}; "
        f"all Euclidean {m['metrics']['all_euclidean_ok']}; worst |identity error| "
        f"{fmt(m['metrics']['max_euclidean_abs_diff'], 3)}.</p>",
        "<p class='note'>Silhouette values are method-specific diagnostics for indicators under "
        "correlation distance. They are not comparable with Option B's time-point hidden-state "
        "silhouette values and must not be ranked against them.</p>",

        "<h3>Layer 2 - economic interpretation under common references</h3>",
        f"<p>Last window ({esc(last)}): cluster members, GDP lead-lag and contraction/expansion profiles.</p>",
        table_html(["Cluster", "Members", "Best GDP lag (quarters)", "Max |mean corr|",
                    "Mean corr at best", "Indicators"], cluster_rows,
                   "Cluster-level GDP lead-lag, last window"),
        table_html(["Cluster", "Members", "Mean z | contraction", "Mean z | expansion", "Difference"],
                   profile_rows, "Cluster profiles in contraction vs expansion, last window"),
        "<p>The full indicator x lag correlation table is in "
        "<code>results/approach1/clustering/lead_lag_gdp.csv</code>; membership for every window "
        "is in <code>window_clusters.csv</code>. Economic category labels (demand, prices, "
        "credit/liquidity, external sector, production) are for the group's interpretation.</p>",

        "<h3>Layer 3 - practical value</h3>",
        "<p><b>What Option A provides:</b> a transparent indicator grouping whose correlation "
        "geometry is verified, a per-window silhouette/stability record, and representative "
        "lead-lag evidence against GDP. The main limitation is that the retained universe is "
        "coverage-dependent (about 15-21 indicators per window), and the early windows are "
        "smaller than the 30-indicator panel. Cluster count selected by maximum silhouette can "
        "choose many small clusters in some windows; the fixed k=4 column is the comparability "
        "sensitivity.</p>",
        figure_links("clustering", [
            "fig_dendrograms.html", "fig_silhouette.html", "fig_silhouette_k_curve.html",
            "fig_adjacent_ari.html", "fig_cluster_membership.html",
            "fig_lead_lag_gdp.html", "fig_cluster_regime_profile.html",
        ]),
    ]
    return "\n".join(body), rows


def option_b_section(all_metrics: list[dict[str, Any]]) -> str:
    b_metrics = [m for m in all_metrics if m.get("_path", "").startswith("results/approach2/")]
    body = ["<h2>4. Option A / Option B comparison status</h2>"]
    if not b_metrics:
        body.append(
            "<p class='warn'>No Option B metrics were found under <code>results/approach2/</code>. "
            "The three-layer A/B comparison cannot be finalised in this run. This section is a "
            "placeholder with no invented numbers.</p>"
        )
    else:
        rows = []
        for m in b_metrics:
            rows.append([m.get("_path"), m.get("task", "-"), m.get("target", "-"),
                         m.get("leakage_discipline", "-"), m.get("seed", "-")])
        body.append(table_html(["File", "Task", "Target", "Leakage label", "Seed"], rows,
                               "Option B metrics discovered"))
        body.append(
            "<p class='warn'>Option B was detected, but this comparison layer does not infer "
            "Option B's internal schema. Populate the three layers from its documented metrics. "
            "Do not calculate ARI/NMI between Option A indicator clusters and Option B time-point "
            "hidden-state clusters: the observation units differ.</p>"
        )
    body.append(
        "<p><b>Three-layer framework:</b> (1) method-specific quality/stability, reported separately; "
        "(2) shared economic interpretation against common macro references; (3) task-oriented "
        "practical value. No cross-method silhouette ranking, no single aggregate score, and no "
        "cross-unit ARI/NMI.</p>"
    )
    return "\n".join(body)


def figure_links(task: str, names: list[str]) -> str:
    items = []
    for n in names:
        rel = f"approach1/{task}/figures/{n}"
        if (RESULTS / rel).exists():
            items.append(f"<li><a href='{esc(rel)}'>{esc(n)}</a></li>")
        else:
            items.append(f"<li class='missing'>{esc(n)} (missing)</li>")
    return "<p><b>Figures:</b></p><ul>" + "".join(items) + "</ul>"


def leakage_section() -> str:
    v = load_json(A / "validation" / "validation_report.json")
    if v is None:
        return "<p>Validation report is not available.</p>"
    lines = [
        "<h2>5. Validation and leakage discipline</h2>",
        f"<p>Validation checks: {v['n_passed']} passed / {v['n_failed']} failed "
        f"(total {v['n_checks']}). All passed: <b>{v['all_passed']}</b>.</p>",
        "<ul>",
        "<li>Frozen SHA-256 verified for the monthly panel and quarterly GDP source.</li>",
        "<li>Every forecast uses the publication-lag rule s + L_i &lt;= t; training-window-only standardization.</li>",
        "<li>GDP targets never enter their own M1/M2/M3 training sets.</li>",
        "<li>2026-11 partial-month observations are excluded from every model sample.</li>",
        "<li>Clustering PSD and Euclidean-identity checks pass for every window.</li>",
        "<li>Headline metrics were recomputed from row-level predictions by <code>approach1_validate.py</code>.</li>",
        "</ul>",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_html(all_metrics: list[dict[str, Any]]) -> str:
    gdp_html, _ = gdp_section()
    reg_html, _ = regime_section()
    clu_html, _ = clustering_section()
    b_html = option_b_section(all_metrics)
    val_html = leakage_section()

    css = """
    body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 36px auto; max-width: 1250px;
           color: #1f2933; line-height: 1.45; }
    h1 { border-bottom: 3px solid #0f766e; padding-bottom: 8px; }
    h2 { margin-top: 36px; color: #0f766e; border-bottom: 1px solid #cbd5e1; padding-bottom: 4px; }
    h3 { margin-top: 24px; color: #334155; }
    table { border-collapse: collapse; width: 100%; margin: 12px 0 20px; font-size: 13px; }
    caption { text-align: left; font-weight: 600; padding: 6px 0; color: #334155; }
    th, td { border: 1px solid #d7dee8; padding: 6px 8px; vertical-align: top; }
    th { background: #eef6f5; }
    tr:nth-child(even) td { background: #fafcfc; }
    code { background: #f1f5f9; padding: 1px 4px; border-radius: 3px; }
    .note { border-left: 4px solid #0f766e; background: #f0fdfa; padding: 10px 12px; }
    .warn { border-left: 4px solid #b45309; background: #fffbeb; padding: 10px 12px; }
    .missing { color: #b91c1c; }
    ul { margin-top: 6px; }
    """

    meta = [
        f"<p><b>Generated by</b> <code>code/compare.py</code>. "
        f"Metrics files discovered: {len(all_metrics)}.</p>",
        "<p><b>Rule:</b> GDP nowcasting, regime classification and clustering are separate tasks; "
        "they are never ranked against one another in one table.</p>",
    ]
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>QF5313 Q3 - consolidated comparison</title>
<style>{css}</style></head><body>
<h1>QF5313 Question 3 - consolidated results</h1>
{''.join(meta)}
{gdp_html}
{reg_html}
{clu_html}
{b_html}
{val_html}
<p style="margin-top:40px;color:#64748b;font-size:12px">
Option A outputs: <code>results/approach1/</code>.
Reproduce: <code>uv run python code/approach1_data.py --seed 42</code>,
<code>uv run python code/approach1_gdp_nowcast.py --seed 42</code>,
<code>uv run python code/approach1_regime.py --seed 42</code>,
<code>uv run python code/approach1_cluster.py --seed 42</code>,
<code>uv run python code/approach1_validate.py --seed 42</code>.
</p>
</body></html>"""


def run(seed: int) -> Path:
    np.random.seed(seed)
    all_metrics = load_all_metrics()
    OUT_HTML.write_text(build_html(all_metrics), encoding="utf-8")

    # terminal summary
    gdp = load_json(A / "gdp_nowcasting" / "metrics.json")
    reg = load_json(A / "regime" / "metrics.json")
    clu = load_json(A / "clustering" / "metrics.json")
    val = load_json(A / "validation" / "validation_report.json")

    tbl = Table(title="Consolidated comparison - Option A", show_lines=False)
    tbl.add_column("Task", style="bold")
    tbl.add_column("Headline", justify="left")
    if gdp:
        m = gdp["metrics"]["overall"]
        tbl.add_row("GDP nowcasting", f"RMSE {fmt(m['rmse'])} vs RW {fmt(gdp['metrics']['benchmark']['rw_drift']['rmse'])}; n={m['n']}")
    if reg:
        m = reg["metrics"]["overall"]
        tbl.add_row("Regime classification", f"Brier {fmt(m['brier'])}, AUC {fmt(m['auc'])}, n={m['n']}")
    if clu:
        m = clu["metrics"]
        tbl.add_row("Indicator clustering", f"mean silhouette {fmt(m['mean_silhouette_selected'])}, mean ARI {fmt(m['mean_adjacent_ari'])}, {m['n_windows']} windows")
    if val:
        tbl.add_row("Validation", f"{val['n_passed']}/{val['n_checks']} checks passed")
    CONSOLE.print(Panel.fit("[bold]QF5313 Q3 consolidated comparison[/bold]", border_style="cyan"))
    CONSOLE.print(tbl)
    CONSOLE.print(f"[green]Wrote[/green] {OUT_HTML.relative_to(ROOT)}")
    return OUT_HTML


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Consolidated task-specific comparison")
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run(seed=args.seed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

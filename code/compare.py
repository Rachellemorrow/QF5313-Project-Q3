"""QF5313 Q3 consolidated comparison layer.

    uv run python code/compare.py --seed 42

Reads every available ``metrics.json`` and produces ``results/comparison.html``.
Tasks are kept strictly separate: GDP nowcasting, regime classification and
clustering are never ranked against one another in a single table.

Both options are ingested with schema-aware loaders.  GDP nowcasting is an
Option-A-specific task (interface.md Section 9 item 1, decided); Option B has
no corresponding task and the GDP section is never presented as a head-to-head
comparison.  The clustering section implements the three-layer comparison
framework of ``interface.md`` Section 9 and never computes ARI/NMI between
Option A's indicator clusters and Option B's time-point hidden-state clusters
(different observation universes; see the conflict note in Section 4).

Intentionally leaky leakage-ablation arms are rendered only inside the
ablation section with an explicit INVALID designation; they never enter the
valid model tables.  The optimizer-invalid nested-CV Probit experiment
(results/approach1/probit_nested_cv_pca/**) is excluded by construction.
Missing inputs, missing schema keys, and failed generation-time checks are
reported as visible warnings instead of invented numbers; unavailable
metrics are rendered as "-".
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from collections import Counter
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

SEED_ORDER = (42, 7, 123)
SEEDS_SET = {"42", "7", "123"}

# ---------------------------------------------------------------------------
# Loading and formatting
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
            m["_path"] = str(p.relative_to(ROOT).as_posix())
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


def fmt_ms(mean: Any, std: Any, nd: int = 4) -> str:
    if mean is None:
        return "-"
    s = fmt(mean, nd)
    if std is None:
        return s
    return f"{s} ± {fmt(std, nd)}"


def seed_series(per_seed: Any, key: str = "brier", nd: int = 4) -> str:
    if not isinstance(per_seed, dict):
        return "-"
    parts = []
    for s in SEED_ORDER:
        e = per_seed.get(str(s))
        if isinstance(e, dict) and e.get(key) is not None:
            parts.append(f"{s}: {fmt(e.get(key), nd)}")
    return " / ".join(parts) if parts else "-"


def table_html(headers: list[str], rows: list[list[Any]], caption: str = "",
               row_classes: list[str] | None = None) -> str:
    out = [f"<table><caption>{esc(caption)}</caption><thead><tr>"]
    out += [f"<th>{esc(h)}</th>" for h in headers]
    out.append("</tr></thead><tbody>")
    for i, r in enumerate(rows):
        cls = ""
        if row_classes is not None and i < len(row_classes) and row_classes[i]:
            cls = f" class='{row_classes[i]}'"
        out.append("<tr" + cls + ">" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Generation-time validation checks
# ---------------------------------------------------------------------------

CHECKS: list[dict[str, str]] = []


def add_check(name: str, ok: bool, detail: str = "", warn_only: bool = False) -> bool:
    status = "passed" if ok else ("warning" if warn_only else "failed")
    CHECKS.append({"name": name, "status": status, "detail": detail})
    return ok


def add_range_check(name: str, triples: list[tuple[str, Any, float, float]]) -> None:
    """Each triple: (label, value, lo, hi).  None values are skipped (shown as '-')."""
    bad = []
    for lbl, v, lo, hi in triples:
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            bad.append(f"{lbl}: non-numeric {v!r}")
            continue
        if not (lo <= fv <= hi):
            bad.append(f"{lbl}={fv}")
    add_check(name, not bad, "; ".join(bad[:6]) or "all rendered values within bounds")


def _int01(x: Any) -> int | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if pd.isna(v):
        return None
    return int(round(v))


def require_keys(m: Any, paths: list[str]) -> list[str]:
    missing = []
    for p in paths:
        cur: Any = m
        ok = True
        for k in p.split("."):
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                ok = False
                break
        if not ok:
            missing.append(p)
    return missing


def load_validated(path: Path, required: list[str], short: str) -> dict[str, Any] | None:
    m = load_json(path)
    if m is None:
        add_check(f"{short}: file present and readable", False, str(path.relative_to(ROOT)))
        return None
    add_check(f"{short}: file present and readable", True)
    miss = require_keys(m, required)
    ok = add_check(f"{short}: required keys present", not miss,
                   ("missing: " + ", ".join(miss)) if miss
                   else f"{len(required)} key paths verified")
    return m if ok else None


_PAYLOAD: dict[str, dict[str, Any] | None] = {}


def get_payload(key: str, path: Path, required: list[str], short: str) -> dict[str, Any] | None:
    """Load each experiment artifact exactly once; validation checks run on first use."""
    if key not in _PAYLOAD:
        _PAYLOAD[key] = load_validated(path, required, short)
    return _PAYLOAD[key]


def schedule_checks(a_m: dict[str, Any], b_m: dict[str, Any], expected_pos: int,
                    label: str, a_pred: pd.DataFrame | None = None) -> bool:
    """Shared-schedule checks for one regime threshold pair (Option A vs Option B)."""
    af = a_m.get("folds") or []
    bf = (b_m.get("history") or {}).get("folds") or []
    ok_fold = len(af) == len(bf) == 82 and [f.get("fold") for f in af] == [f.get("fold") for f in bf]
    add_check(f"[{label}] identical 82-month target-month sets (A folds == B folds)", ok_fold,
              f"A n={len(af)}, B n={len(bf)}")
    ao = [f.get("forecast_origin") for f in af]
    bo = [f.get("forecast_origin") for f in bf]
    ok_org = ao == bo and len(ao) == 82
    add_check(f"[{label}] identical month-end forecast origins (A == B)", ok_org,
              f"{ao[0] if ao else '-'} .. {ao[-1] if ao else '-'}")
    comparable = ok_fold and ok_org
    at = {f.get("fold"): _int01(f.get("y_true")) for f in af}
    bt = {f.get("fold"): _int01(f.get("y_true")) for f in bf}
    mism = sorted(k for k in set(at) & set(bt) if at[k] != bt[k])
    add_check(f"[{label}] per-fold y_true identical (A folds vs B history)", not mism,
              (f"{len(mism)} mismatched folds: {mism[:5]}") if mism
              else f"{len(set(at) & set(bt))}/82 identical")
    comparable = comparable and not mism
    if a_pred is not None:
        pt = {str(f): _int01(v) for f, v in zip(a_pred["fold"], a_pred["y_true"])}
        mism2 = sorted(k for k in at if k in pt and at[k] != pt[k])
        add_check(f"[{label}] per-fold y_true identical (A predictions.csv vs A folds)", not mism2,
                  (f"{len(mism2)} mismatched folds") if mism2
                  else f"{len(pt)} prediction rows agree")
        comparable = comparable and not mism2
    if any(v is None for v in list(at.values()) + list(bt.values())):
        add_check(f"[{label}] per-fold y_true present in both artifacts", False,
                  "missing/unparseable y_true values")
        return False
    apos, bpos = sum(at.values()), sum(bt.values())
    ok_pos = apos == bpos == expected_pos
    add_check(f"[{label}] contraction count == {expected_pos}", ok_pos, f"A={apos}, B={bpos}")
    return comparable and ok_pos


# ---------------------------------------------------------------------------
# Inputs discovered (provenance)
# ---------------------------------------------------------------------------

def b_inputs_section(all_metrics: list[dict[str, Any]]) -> str:
    rows = []
    for m in all_metrics:
        p = m.get("_path", "")
        if not p.startswith("results/approach2/"):
            continue
        prm = m.get("params") if isinstance(m.get("params"), dict) else {}
        task = m.get("task") or prm.get("task", "-")
        target = m.get("target") or prm.get("target", "-")
        seed = m.get("seed")
        if seed is None:
            seed = ", ".join(str(s) for s in (prm.get("seeds") or [])) or "-"
        rows.append([
            p, task, target, fmt(prm.get("label_threshold")),
            m.get("leakage_discipline") or prm.get("leakage_discipline", "-"),
            seed,
        ])
    return "\n".join([
        "<h2>0. Inputs discovered</h2>",
        f"<p>Metrics files discovered: {len(all_metrics)}. Excluded from all comparison tables "
        "by design: <code>results/approach1/probit_nested_cv_pca/**</code> — the strictly nested "
        "inner-CV Probit rank experiment was marked invalid by its own optimizer audit and is "
        "never read by any section builder on this page.</p>",
        table_html(["File", "Task", "Target", "Label threshold", "Leakage label", "Seed(s)"],
                   rows, "Option B stored inputs (schema-aware ingestion)"),
    ])


# ---------------------------------------------------------------------------
# Section 1: GDP nowcasting (Option A only)
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
        "<p class='note'><b>Scope:</b> quarterly GDP nowcasting is an Option-A-specific task "
        "(interface.md Section 9 item 1, decided); Option B has no corresponding GDP-nowcasting "
        "experiment. This section is reported for completeness and is <b>not</b> an Option A / "
        "Option B head-to-head comparison.</p>",
        "<p class='note'>The 2020Q1 M3 nowcast illustrates a known limitation of a static "
        "latest-value factor: the March PMI rebound enters the M3 information set while the "
        "February collapse does not. The within-quarter revision path is reported rather than hidden.</p>",
        figure_links("gdp_nowcasting", [
            "fig_gdp_target.html", "fig_nowcast_vs_realized.html", "fig_rmse_by_vintage.html",
            "fig_revision_path.html", "fig_pca_scree.html", "fig_factor_loadings.html",
        ]),
    ]
    return "\n".join(body), rows


# ---------------------------------------------------------------------------
# Figure links (generalized, backward compatible)
# ---------------------------------------------------------------------------

def figure_links(task: str, names: list[str], approach: str = "approach1") -> str:
    items = []
    for n in names:
        rel = f"{approach}/{task}/figures/{n}"
        if (RESULTS / rel).exists():
            items.append(f"<li><a href='{esc(rel)}'>{esc(n)}</a></li>")
        else:
            items.append(f"<li class='missing'>{esc(n)} (missing)</li>")
    return "<p><b>Figures:</b></p><ul>" + "".join(items) + "</ul>"

# ---------------------------------------------------------------------------
# Section 2: regime classification (shared 82-fold protocol, A + B)
# ---------------------------------------------------------------------------

def per_seed_table(src: dict[str, Any], caption: str) -> str | None:
    pm = src.get("metrics", {}).get("per_model", {})
    rows = []
    for arch in ("lstm", "gru"):
        ps = (pm.get(arch) or {}).get("per_seed", {})
        for s in SEED_ORDER:
            e = ps.get(str(s))
            if isinstance(e, dict):
                rows.append([arch.upper(), s, fmt(e.get("brier")), fmt(e.get("auc")),
                             fmt(e.get("accuracy")), e.get("n")])
    if not rows:
        return None
    return table_html(["Arm", "Seed", "Brier", "AUC", "Accuracy", "n"], rows, caption)


def regime_section() -> str:
    a = get_payload("a_reg", A / "regime" / "metrics.json",
                    ["metrics.overall", "metrics.baseline_prior_frequency",
                     "metrics.baseline_persistence", "folds", "leakage_discipline"],
                    "A regime metrics (thr 50)")
    a_fo = get_payload("a_fo", A / "regime" / "metrics_factors_only.json",
                       ["metrics.overall", "folds"], "A regime metrics (factors-only)")
    s495 = get_payload("a_495", A / "regime" / "metrics_threshold_49p5.json",
                       ["metrics.overall", "folds"], "A regime metrics (thr 49.5, factors-only)")
    s495c = get_payload("a_495c", A / "regime" / "metrics_threshold_49p5_controls.json",
                        ["metrics.overall", "metrics.baseline_prior_frequency",
                         "metrics.baseline_persistence", "folds"],
                        "A regime metrics (thr 49.5, LPR control)")
    b = get_payload("b_reg", B / "regime" / "metrics.json",
                    ["metrics.baseline", "metrics.per_model.lstm", "metrics.per_model.gru",
                     "history.folds", "params.leakage_discipline", "params.lookback_h_selected"],
                    "B regime metrics (thr 50)")
    b495 = get_payload("b_495", B / "regime_threshold49_5" / "metrics.json",
                       ["metrics.baseline", "metrics.per_model.lstm", "metrics.per_model.gru",
                        "history.folds", "params.leakage_discipline",
                        "params.lookback_h_selected"],
                       "B regime metrics (thr 49.5)")
    abl = get_payload("b_abl", B / "leakage_ablation" / "metrics.json",
                      ["metrics.models.probit", "metrics.models.lstm_safe",
                       "metrics.models.gru_safe", "metrics.models.lstm_leaky",
                       "metrics.models.gru_leaky", "metrics.leakage_premium.lstm",
                       "metrics.leakage_premium.gru", "metrics.baseline"],
                      "B leakage-ablation metrics")
    a_pred = None
    if (A / "regime" / "predictions.csv").exists():
        try:
            a_pred = pd.read_csv(A / "regime" / "predictions.csv")
            add_check("A regime predictions.csv readable for the y_true cross-check", True,
                      f"{len(a_pred)} rows")
        except Exception as e:  # defensive; should not occur on stored artifacts
            add_check("A regime predictions.csv readable for the y_true cross-check", False, str(e))
    else:
        add_check("A regime predictions.csv readable for the y_true cross-check", False,
                  "file not found")

    if a is not None:
        add_check("A regime leakage_discipline == safe", a.get("leakage_discipline") == "safe")
    for key, m, short in (("thr 50", b, "B regime"), ("thr 49.5", b495, "B regime (49.5)")):
        if m is not None:
            add_check(f"{short} leakage_discipline == safe",
                      (m.get("params") or {}).get("leakage_discipline") == "safe")

    comparable50 = a is not None and b is not None and \
        schedule_checks(a, b, 27, "threshold 50", a_pred=a_pred)
    comparable495 = s495 is not None and b495 is not None and \
        schedule_checks(s495, b495, 13, "threshold 49.5")

    # n_folds consistency across regime/ablation artifacts
    nvals, det = [], []
    if a is not None:
        nvals.append(len(a.get("folds") or []))
        det.append(f"A folds={len(a.get('folds') or [])}")
    if b is not None:
        pm = b.get("metrics", {}).get("per_model", {})
        nvals += [(pm.get("lstm") or {}).get("n_folds"), (pm.get("gru") or {}).get("n_folds"),
                  b.get("metrics", {}).get("baseline", {}).get("n")]
        det.append(f"B lstm/gru n_folds={(pm.get('lstm') or {}).get('n_folds')}/"
                   f"{(pm.get('gru') or {}).get('n_folds')}")
    if b495 is not None:
        pm = b495.get("metrics", {}).get("per_model", {})
        nvals += [(pm.get("lstm") or {}).get("n_folds"), (pm.get("gru") or {}).get("n_folds")]
    if abl is not None:
        nvals.append((abl.get("params") or {}).get("n_folds"))
        det.append(f"ablation params n_folds={(abl.get('params') or {}).get('n_folds')}")
    ok_nf = bool(nvals) and all(v == 82 for v in nvals)
    add_check("n_folds == 82 across regime/ablation artifacts", ok_nf, "; ".join(det))

    # B baseline consistency between the regime run and the ablation (thr 50)
    if b is not None and abl is not None:
        b1 = b["metrics"]["baseline"]
        b2 = abl["metrics"]["baseline"]
        ok_bl = (abs(float(b1["brier"]) - float(b2["brier"])) < 1e-9
                 and abs(float(b1["accuracy"]) - float(b2["accuracy"])) < 1e-9)
        add_check("B prior-frequency baseline consistent (regime == ablation)", ok_bl,
                  f"Brier {fmt(b1['brier'])} vs {fmt(b2['brier'])}; "
                  f"accuracy {fmt(b1['accuracy'])} vs {fmt(b2['accuracy'])}")

    # safe-arm provenance: ablation safe arms must equal the regime run's per-seed metrics
    if b is not None and abl is not None:
        probs = []
        for arch in ("lstm", "gru"):
            src = (b["metrics"]["per_model"].get(arch) or {}).get("per_seed", {})
            dst = ((abl["metrics"]["models"].get(f"{arch}_safe") or {}).get("per_seed")) or {}
            for s in sorted(SEEDS_SET):
                e1, e2 = src.get(s), dst.get(s)
                if not isinstance(e1, dict) or not isinstance(e2, dict):
                    probs.append(f"{arch}/{s}: missing entry")
                    continue
                for k in ("brier", "auc", "accuracy"):
                    v1, v2 = e1.get(k), e2.get(k)
                    if v1 is None or v2 is None or abs(float(v1) - float(v2)) > 1e-9:
                        probs.append(f"{arch}/{s}/{k}: {v1} vs {v2}")
        add_check("Safe LSTM/GRU per-seed metrics consistent (regime == ablation reuse)",
                  not probs, "; ".join(probs[:4]) or "all per-seed values identical")

    # ablation leakage labels + premium arithmetic
    if abl is not None:
        models = abl["metrics"]["models"]
        expected = {"probit": "safe", "lstm_safe": "safe", "gru_safe": "safe",
                    "lstm_leaky": "intentional-leakage-demo",
                    "gru_leaky": "intentional-leakage-demo"}
        bad = [f"{k}: {((models.get(k) or {}).get('leakage_discipline'))!r} != {exp!r}"
               for k, exp in expected.items()
               if (models.get(k) or {}).get("leakage_discipline") != exp]
        add_check("Leakage-discipline labels correct for all five ablation arms", not bad,
                  "; ".join(bad) or "safe arms 'safe'; leaky arms 'intentional-leakage-demo'")
        prem = []
        for arch in ("lstm", "gru"):
            pr = abl["metrics"]["leakage_premium"][arch]
            gap = float(pr["brier_safe"]) - float(pr["brier_leaky"])
            if abs(gap - float(pr["brier_gap_safe_minus_leaky"])) > 1e-9:
                prem.append(f"{arch}: recorded Brier gap != safe-minus-leaky")
            for arm_key, field in ((f"{arch}_safe", "brier_safe"), (f"{arch}_leaky", "brier_leaky")):
                mv = ((models.get(arm_key) or {}).get("mean") or {}).get("brier")
                if mv is None or abs(float(mv) - float(pr[field])) > 1e-9:
                    prem.append(f"{arch}: premium {field} does not match {arm_key} mean")
        add_check("Leakage-premium records match arm means and arithmetic", not prem,
                  "; ".join(prem) or "gaps reproduce (premium = leaky minus safe, identical folds)")

    # numeric-range checks over every rendered regime/ablation metric
    rng = []
    for lbl, m in (("A primary", a), ("A factors-only", a_fo), ("A 49.5 primary", s495),
                   ("A 49.5 control", s495c)):
        if m is not None:
            o = m["metrics"]["overall"]
            rng.append((f"brier {lbl}", o.get("brier"), 0.0, 1.0))
            rng.append((f"auc {lbl}", o.get("auc"), 0.0, 1.0))
    for lbl, m in (("B thr50", b), ("B thr49.5", b495)):
        if m is not None:
            for arch in ("lstm", "gru"):
                mean = ((m["metrics"]["per_model"].get(arch) or {}).get("mean")) or {}
                rng.append((f"brier {lbl} {arch}", mean.get("brier"), 0.0, 1.0))
                rng.append((f"auc {lbl} {arch}", mean.get("auc"), 0.0, 1.0))
    if abl is not None:
        for k, arm in abl["metrics"]["models"].items():
            mean = arm.get("mean") or {}
            rng.append((f"brier ablation {k}", mean.get("brier"), 0.0, 1.0))
            rng.append((f"auc ablation {k}", mean.get("auc"), 0.0, 1.0))
    add_range_check("Regime/ablation metrics within [0, 1]", rng)

    body = [
        "<h2>2. Regime classification (shared 82-fold protocol)</h2>",
        "<p><b>Label:</b> d_t = 1{pmi_man_t &lt; threshold and pmi_man_(t-1) &lt; threshold}; "
        "target d_(t+1); one month ahead; expanding window. Per the shared interface, both options "
        "use identical out-of-sample target months (fold id = target month), identical month-end "
        "forecast origins, and identical realized labels (verified for both thresholds in Section 6). "
        "Threshold 50 is primary; 49.5 is the required sensitivity. The 82 target months include "
        "27 contraction months at threshold 50 and 13 at threshold 49.5. Intentionally leaky "
        "Option B arms are rendered only in Section 3 and never in the tables below.</p>",
    ]

    # ---- threshold 50 -----------------------------------------------------
    if comparable50 and a is not None and b is not None:
        rows = []
        o = a["metrics"]["overall"]
        rows.append([
            "Probit: PCA factors + LPR 1Y (primary)", "A", fmt(o["brier"]), fmt(o["auc"]),
            fmt(o["accuracy"]), fmt(o.get("balanced_accuracy")),
            "fixed 0.5 cutoff; sensitivity " + fmt(o.get("sensitivity")) + " / specificity "
            + fmt(o.get("specificity")),
        ])
        if a_fo is not None:
            o = a_fo["metrics"]["overall"]
            rows.append([
                "Probit: PCA factors only (sensitivity)", "A", fmt(o["brier"]), fmt(o["auc"]),
                fmt(o["accuracy"]), fmt(o.get("balanced_accuracy")),
                "longer label sample (from " + str((a_fo.get("folds") or [{}])[0].get("train_start"))
                + "); sensitivity " + fmt(o.get("sensitivity")) + " / specificity "
                + fmt(o.get("specificity"))
                + "; not a clean one-variable ablation of the primary model",
            ])
        pr = a["metrics"]["baseline_prior_frequency"]
        rows.append(["Prior-frequency baseline", "A", fmt(pr["brier"]), fmt(pr.get("auc")),
                     fmt(pr["accuracy"]), "-",
                     "constant prior over A's expanding label windows (from "
                     + str((a.get("folds") or [{}])[0].get("train_start")) + ")"])
        pe = a["metrics"]["baseline_persistence"]
        rows.append(["Persistence baseline", "A", fmt(pe["brier"]), fmt(pe.get("auc")),
                     fmt(pe["accuracy"]), "-", "latest known label as the forecast for d_(t+1)"])
        bm = b["metrics"]["baseline"]
        rows.append(["Prior-frequency baseline", "B", fmt(bm["brier"]), fmt(bm.get("auc")),
                     fmt(bm["accuracy"]), "-",
                     "constant prior over B's expanding fit windows (label history from 2008)"])
        if abl is not None:
            pb = abl["metrics"]["models"]["probit"].get("mean") or {}
            rows.append(["Probit (ridge, origin-row, lag-shifted standardized features) - Option B classical arm",
                         "B", fmt(pb.get("brier")), fmt(pb.get("auc")), fmt(pb.get("accuracy")),
                         "-", "deterministic; per-fold ridge alpha from the validation tail"])
        for arch, disp in (("lstm", "LSTM (safe)"), ("gru", "GRU (safe)")):
            mm = b["metrics"]["per_model"][arch]
            rows.append([
                disp + " - mean ± std of 3 seeds", "B",
                fmt_ms(mm.get("mean", {}).get("brier"), mm.get("std", {}).get("brier")),
                fmt_ms(mm.get("mean", {}).get("auc"), mm.get("std", {}).get("auc")),
                fmt_ms(mm.get("mean", {}).get("accuracy"), mm.get("std", {}).get("accuracy")),
                "-", "decision threshold per seed/fold from the 24-month validation tail",
            ])
        body.append("<h3>2.1 Threshold 50.0 - valid models on the identical 82 target months</h3>")
        body.append(table_html(["Model", "Option", "Brier ↓", "AUC ↑", "Accuracy", "Balanced acc. (A only)", "Note"],
                               rows, "Joint regime comparison, threshold 50 (leaky arms excluded)"))
        pst = per_seed_table(b, "Per-seed walk-forward outcomes behind the Option B mean ± std cells (thr 50)")
        if pst:
            body.append(pst)
        body.append(
            "<p><b>Baselines and reading.</b> The two prior-frequency baselines are both valid and "
            "differ only because they are estimated over each option's own expanding label windows "
            "(A from " + str((a.get("folds") or [{}])[0].get("train_start")) + ", B from 2008): Brier "
            + fmt(a["metrics"]["baseline_prior_frequency"]["brier"]) + " vs "
            + fmt(b["metrics"]["baseline"]["brier"]) + ". No model is superior on all metrics on this "
            "page: the highest AUC under lag discipline is the Option B ridge probit, the lowest "
            "valid Brier is the Option B safe LSTM, and the highest accuracy is Option A's persistence "
            "baseline. With 82 observations and 27 contractions, adjacent-model differences are of the "
            "same order as per-seed dispersion; no superiority claim is made or implied.</p>")
        body.append(figure_links("regime", [
            "fig_regime_probabilities.html", "fig_regime_roc.html", "fig_regime_calibration.html",
            "fig_regime_brier.html", "fig_regime_coefficients.html",
        ]) + figure_links("regime", ["walkforward_probabilities.html", "walkforward_brier.html"],
                          approach="approach2"))
    else:
        body.append("<p class='warn'><b>Threshold-50 joint comparison suppressed:</b> generation-time "
                    "schedule/label checks failed for this threshold (see Section 6). The joint table "
                    "is not rendered because fold or label comparability could not be established. "
                    "This is a validation failure, not a missing-input notice.</p>")

    # ---- threshold 49.5 ---------------------------------------------------
    if comparable495 and s495 is not None and s495c is not None and b495 is not None:
        rows = []
        o = s495["metrics"]["overall"]
        rows.append([
            "Probit: PCA factors only, full history (primary sensitivity)", "A",
            fmt(o["brier"]), fmt(o["auc"]), fmt(o["accuracy"]), fmt(o.get("balanced_accuracy")),
            "labels from 2008-02; sensitivity " + fmt(o.get("sensitivity")) + " / specificity "
            + fmt(o.get("specificity")) + " at the fixed 0.5 cutoff",
        ])
        o = s495c["metrics"]["overall"]
        cf = s495c.get("folds") or []
        n_ct = sum(1 for fl in cf if fl.get("control_used") is True)
        n_fb = sum(1 for fl in cf if fl.get("control_used") is False)
        rows.append([
            "Probit: PCA factors + LPR control", "A", fmt(o["brier"]), fmt(o["auc"]),
            fmt(o["accuracy"]), fmt(o.get("balanced_accuracy")),
            "control engaged in " + str(n_ct) + " of 82 folds (label window from "
            + str((cf[-1] if cf else {}).get("train_start")) + "): sensitivity "
            + fmt(o.get("sensitivity")) + " / specificity " + fmt(o.get("specificity"))
            + ". The other " + str(n_fb) + " early folds used the documented factors-only "
            "fallback on full-history labels — engagement requires at least 24 finite LPR control "
            "observations and at least 3 positive labels in the LPR-era window",
        ])
        pc = s495c["metrics"]["baseline_prior_frequency"]
        rows.append(["Prior-frequency baseline", "A", fmt(pc["brier"]), fmt(pc.get("auc")),
                     fmt(pc["accuracy"]), "-",
                     "prior over the LPR-era sample (the full-history file's prior is "
                     + fmt(s495["metrics"]["baseline_prior_frequency"]["brier"]) + " Brier / "
                     + fmt(s495["metrics"]["baseline_prior_frequency"]["accuracy"])
                     + " accuracy)"])
        pe = s495c["metrics"]["baseline_persistence"]
        rows.append(["Persistence baseline", "A", fmt(pe["brier"]), fmt(pe.get("auc")),
                     fmt(pe["accuracy"]), "-", "latest known label as the forecast for d_(t+1)"])
        bm = b495["metrics"]["baseline"]
        rows.append(["Prior-frequency baseline", "B", fmt(bm["brier"]), fmt(bm.get("auc")),
                     fmt(bm["accuracy"]), "-", "constant prior over B's expanding fit windows"])
        for arch, disp in (("lstm", "LSTM (safe)"), ("gru", "GRU (safe)")):
            mm = b495["metrics"]["per_model"][arch]
            rows.append([
                disp + " - mean ± std of 3 seeds (h=12)", "B",
                fmt_ms(mm.get("mean", {}).get("brier"), mm.get("std", {}).get("brier")),
                fmt_ms(mm.get("mean", {}).get("auc"), mm.get("std", {}).get("auc")),
                fmt_ms(mm.get("mean", {}).get("accuracy"), mm.get("std", {}).get("accuracy")),
                "-", "threshold per seed/fold from the 24-month validation tail",
            ])
        body.append("<h3>2.2 Threshold 49.5 - sensitivity on the identical 82 target months</h3>")
        body.append(table_html(["Model", "Option", "Brier ↓", "AUC ↑", "Accuracy", "Balanced acc. (A only)", "Note"],
                               rows, "Joint regime comparison, threshold 49.5 (leaky arms excluded)"))
        pst = per_seed_table(b495, "Per-seed walk-forward outcomes behind the Option B mean ± std cells (thr 49.5)")
        if pst:
            body.append(pst)
        body.append(
            "<p><b>Class-imbalance disclosure (49.5).</b> Only 13 of 82 target months are contractions "
            "(15.9%). A forecast of \"no contraction\" for every month achieves accuracy "
            + fmt(s495c["metrics"]["baseline_prior_frequency"]["accuracy"])
            + " while detecting nothing; both Option A variants predict zero contractions at their "
            "fixed 0.5 cutoff (sensitivity " + fmt(s495c["metrics"]["overall"].get("sensitivity"))
            + " and " + fmt(s495["metrics"]["overall"].get("sensitivity")) + "). Raw accuracy on this "
            "threshold is therefore dominated by the majority class and can be misleading: it must be "
            "read together with sensitivity/specificity. Option B's per-seed thresholds are tuned for "
            "balanced accuracy on the validation tail, so accuracies at or below the majority rate "
            "reflect the deliberate operating point, not model quality.</p>")
        body.append(figure_links("regime_threshold49_5",
                                 ["walkforward_probabilities.html", "walkforward_brier.html"],
                                 approach="approach2"))
    else:
        body.append("<p class='warn'><b>Threshold-49.5 joint comparison suppressed:</b> generation-time "
                    "schedule/label checks failed for this threshold (see Section 6), or a stored "
                    "artifact is unavailable.</p>")

    # ---- 2.3 specification and training-sample disclosure ------------------
    body.append("<h3>2.3 Specification and training-sample disclosure</h3>")
    body.append(
        "<p><b>Common evaluation period.</b> Every model above is evaluated on the same 82 target "
        "months (2020-01 .. 2026-10) with identical month-end forecast origins (2019-12-31 .. "
        "2026-09-30) and identical per-fold ground-truth labels; both were verified by "
        "generation-time checks (Section 6). Identical test windows do <b>not</b> imply identical "
        "training samples, and the table above is a shared-protocol comparison, not a controlled "
        "paired experiment.</p>")
    t_rows = []

    def win_a(m: dict[str, Any]) -> tuple[str, str]:
        f0 = (m.get("folds") or [{}])[0]
        fl = (m.get("folds") or [{}])[-1]
        return (f"{f0.get('train_start')} .. {f0.get('train_end')} ({f0.get('n_train_labels')} labels)",
                f"{fl.get('train_start')} .. {fl.get('train_end')} ({fl.get('n_train_labels')} labels)")

    def win_b(m: dict[str, Any]) -> tuple[str, str]:
        f0 = ((m.get("history") or {}).get("folds") or [{}])[0]
        fl = ((m.get("history") or {}).get("folds") or [{}])[-1]
        return (f"{f0.get('train_label_range')} (+ validation tail {f0.get('val_label_range')})",
                f"{fl.get('train_label_range')} (+ validation tail {fl.get('val_label_range')})")

    if a is not None:
        x, y = win_a(a)
        t_rows.append(["A — Probit (factors + LPR 1Y)", "50", x, y, "fixed 0.5 cutoff"])
    if a_fo is not None:
        x, y = win_a(a_fo)
        t_rows.append(["A — Probit (factors only)", "50 / 49.5", x, y, "fixed 0.5 cutoff"])
    if s495c is not None:
        x, y = win_a(s495c)
        cf_ = s495c.get("folds") or [{}, {}]
        x += "; no control (factors-only fallback)" if cf_[0].get("control_used") is False else ""
        y += "; LPR control engaged" if cf_[-1].get("control_used") is True else ""
        t_rows.append(["A — Probit (factors + LPR control)", "49.5", x, y, "fixed 0.5 cutoff"])
    if b is not None:
        x, y = win_b(b)
        t_rows.append(["B — LSTM/GRU (h=3)", "50", x, y,
                       "per-seed threshold on the validation tail"])
    if b495 is not None:
        x, y = win_b(b495)
        t_rows.append(["B — LSTM/GRU (h=12)", "49.5", x, y,
                       "per-seed threshold on the validation tail"])
    if t_rows:
        body.append(table_html(["Arm", "Label threshold", "Fit labels at fold 2020-01",
                                "Fit labels at fold 2026-10", "Operating point"],
                               t_rows, "Verified label-training windows (expanding; values grow toward 2026)"))
    rf = Counter(f.get("r") for f in ((a or {}).get("folds") or []) if f.get("r") is not None)
    rk_str = (", ".join(f"rank {k} in {v} folds" for k, v in sorted(rf.items()))
              or "r distribution stored in the artifact")
    body.append(
        "<p><b>Available feature information.</b> Option A consumes publication-lag-masked, "
        "training-standardized panel columns; the rate/spread block is excluded from the factor "
        "universe and enters only as the standardized LPR 1Y control; the PCA rank r per fold is "
        "selected by the GDP-nowcasting inner CV ("
        + rk_str + "). Option B consumes h-month windows of "
        "the 19 of 30 lag-shifted standardized series retained by the per-fold "
        "missingness/variance screen, with fit-window-only imputation and z-score statistics.</p>")
    body.append(
        "<p><b>Model specifications.</b> Option A: probit maximum likelihood with a fixed 0.5 "
        "classification cutoff. Option B: two-layer (32 hidden units, dropout 0.2) LSTM/GRU with "
        "binary cross-entropy, early stopping and a per-seed decision threshold selected on the "
        "24-month validation tail (balanced accuracy; pos weight capped at 1.0).</p>")
    body.append(
        "<p><b>Option B classical arm (Section 2.1, threshold 50).</b> Verified from the stored "
        "artifact and its generating code: a deterministic statsmodels probit GLM (Binomial family, "
        "probit link) on the origin-row design z_u — the last row of each training window, i.e. the "
        "same leak-safe lag-shifted standardized information set the sequence models see at their "
        "final month — retaining all 30 panel indicators (no additional screen) plus an intercept. "
        "Ridge stabilization is implemented via artificial zero-label rows weighted sqrt(alpha); "
        "alpha is selected per fold by validation-tail Brier from {unpenalized, 0.1, 1, 10} — never "
        "test data. Probabilities are clipped to [0, 1]. It shares the expanding fit-windows and "
        "24-month validation tail of the RNN arms and is evaluated on the same 82 folds; no seed is "
        "required. Source: <code>results/approach2/leakage_ablation/metrics.json</code> "
        "(<code>params.safe_probit</code>).</p>")
    body.append(
        "<p><b>Reported-metrics limitation.</b> Option A stores sensitivity, specificity and "
        "balanced accuracy at its fixed 0.5 cutoff; the stored Option B artifacts record Brier, "
        "AUC and accuracy only. The Balanced-acc. column is therefore shown for Option A alone "
        "(marked in the table headers), and no operating-point measures were recomputed for "
        "Option B — its per-seed decision thresholds are part of the trained model selection, and "
        "an ad-hoc recomputation from stored probabilities would not reproduce that procedure. "
        "Leaky-ablation arms likewise store Brier/AUC/accuracy and are reported only in the "
        "ablation section below.</p>")
    body.append(
        "<p><b>Training-sample differences (verified, not assumed).</b> (i) Option A's primary arm "
        "starts at the LPR history (its label sample begins in "
        + str((a or {}).get("folds", [{}])[0].get("train_start"))
        + "), while Option B exploits the full post-2008 label history. (ii) Option B reserves the "
        "most recent 24 label months of each fold as its validation tail for early stopping and "
        "threshold selection, so its <i>fit</i> labels at the final fold end in "
        + str((((b or {}).get("history") or {}).get("folds") or [{}])[-1].get("train_label_range", "?")
              .split(":")[-1] if b else "?")
        + " whereas Option A's probit consumes labels through 2026-09. (iii) Both differences are "
        "documented in the stored artifacts and reproduced above; nothing was truncated, imputed, "
        "or rerun to make the samples equal.</p>")
    body.append(
        "<p><b>Limitations of direct ranking.</b> Because fit-label windows, lengths, feature "
        "representations, and operating points differ, adjacent-model differences in the tables "
        "above mix specification effects with sample effects. Option A's factors-only variant is "
        "shown as the closest available within-A reference that shares Option B's longer sample "
        "persistence while retaining A's factor specification. A matched-training-window "
        "robustness experiment (restricting one option's training labels to the other option's "
        "window) is a possible future extension; it has <b>not</b> been run, and no numbers for it "
        "appear on this page.</p>")
    return "\n".join(body)


# ---------------------------------------------------------------------------
# Section 3: leakage ablation (Option B, mandatory)
# ---------------------------------------------------------------------------

def ablation_section() -> str:
    body = [
        "<h2>3. Leakage ablation (Option B, mandatory)</h2>",
        "<p><b>Design.</b> Three arms under the same 82 folds, labels, seeds and training pipeline "
        "as the Option B regime run (lookback h=3): a leak-safe ridge Probit on the origin row of "
        "the lag-shifted standardized panel, safe LSTM/GRU "
        "whose predictions are reused verbatim from the regime run, and LSTM/GRU trained on the raw "
        "<i>unshifted</i> panel (as if every series were published at its reference month-end). "
        "For the 21 of 30 series with a non-zero publication lag this feeds the origin row "
        "information a real-time forecaster could not have had. The leaky window does not contain "
        "the label's second PMI observation, so the demonstration isolates availability leakage of "
        "unpublished contemporaneous data rather than direct label peeking.</p>",
        "<p class='inv'>Legend: INVALID = intentional leakage demonstration only; not a valid "
        "predictive model. Leaky values are shown for transparency and are excluded from every "
        "valid-model table on this page by construction.</p>",
    ]
    abl = get_payload("b_abl", B / "leakage_ablation" / "metrics.json",
                      ["metrics.models.probit", "metrics.models.lstm_safe",
                       "metrics.models.gru_safe", "metrics.models.lstm_leaky",
                       "metrics.models.gru_leaky", "metrics.leakage_premium.lstm",
                       "metrics.leakage_premium.gru", "metrics.baseline"],
                      "B leakage-ablation metrics")
    if abl is None:
        body.append("<p class='warn'>Leakage-ablation metrics are unavailable; nothing further is "
                    "rendered and no values are invented.</p>")
        return "\n".join(body)
    models = abl["metrics"]["models"]
    arm_rows, cls, arms = [], [], []
    for key, disp, leaky in (("probit", "Probit (ridge, origin-row, lag-shifted features)", False),
                             ("lstm_safe", "LSTM (safe)", False),
                             ("gru_safe", "GRU (safe)", False),
                             ("lstm_leaky", "LSTM WITHOUT lag discipline", True),
                             ("gru_leaky", "GRU WITHOUT lag discipline", True)):
        arm = models[key]
        mean = arm.get("mean") or {}
        std = arm.get("std") or {}
        disc = arm.get("leakage_discipline", "-")
        arm_rows.append([
            disp, fmt(mean.get("n"), 0), fmt(mean.get("brier")),
            fmt_ms(mean.get("brier"), std.get("brier")),
            fmt_ms(mean.get("auc"), std.get("auc")),
            fmt_ms(mean.get("accuracy"), std.get("accuracy")),
            ("INVALID — " if leaky else "") + disc,
            seed_series(arm.get("per_seed"), "brier"),
        ])
        cls.append("invalid" if leaky else "")
        arms.append(key)
    body.append(table_html(
        ["Arm", "n", "Brier (mean)", "Brier (mean ± std)", "AUC (mean ± std)", "Accuracy (mean ± std)",
         "leakage_discipline", "Per-seed Brier (42 / 7 / 123)"],
        arm_rows, "Leakage ablation arms (red rows are INVALID demonstrations)",
        row_classes=cls))
    prem = abl["metrics"]["leakage_premium"]
    p_rows = []
    for arch in ("lstm", "gru"):
        pr = prem[arch]
        p_rows.append([
            arch.upper(), fmt(pr["brier_safe"]), fmt(pr["brier_leaky"]),
            fmt(pr["brier_gap_safe_minus_leaky"]), fmt(pr["auc_safe"]) + " → " + fmt(pr["auc_leaky"]),
            fmt(float(pr["auc_leaky"]) - float(pr["auc_safe"])),
        ])
    body.append(table_html(
        ["Architecture", "Brier safe", "Brier leaky", "Brier gap (safe − leaky)",
         "AUC safe → leaky", "AUC gain (leaky − safe)"],
        p_rows, "Recorded leakage premium (INVALID arms vs own safe arms, identical folds)"))
    body.append(
        "<p><b>What the demonstration shows.</b> (1) Ignoring publication lags inflates apparent "
        "performance on every metric: the premium is roughly +0.02 to +0.03 Brier and +0.08 to "
        "+0.09 AUC within each architecture, and all of it was unavailable information at decision "
        "time. (2) The premium is a within-architecture comparison: the <b>safe</b> probit, using "
        "only genuinely available information, reaches an AUC above both leaky RNNs on the same "
        "folds — lag discipline is not merely a handicap, and the leaky arms quantify the value of "
        "the forbidden data to a given architecture. (3) The leaky arms are INVALID as predictive "
        "models and are reported only as the intentional demonstration required by the assignment "
        "(ablation is mandatory). Row-level provenance: <code>"
        + str(((models.get("lstm_safe") or {}).get("source")) or "results/approach2/regime/predictions.csv")
        + "</code>; the probit arm is deterministic (no stochastic seed required). "
        "Factual notes for the group's report: <code>results/approach2/leakage_ablation/NOTES.md</code>.</p>")
    body.append(figure_links("leakage_ablation",
                             ["leakage_demo_probabilities.html", "ablation_brier.html",
                              "ablation_summary.html"], approach="approach2"))
    return "\n".join(body)

# ---------------------------------------------------------------------------
# Section 4: clustering — three-layer A/B comparison (interface.md Section 9)
# ---------------------------------------------------------------------------

def clustering_section() -> str:
    body = [
        "<h2>4. Clustering — three-layer A/B comparison (interface.md Section 9)</h2>",
        "<p class='note'><b>Documented interpretive conflict.</b> <code>assignment.md</code> §5.4.2 "
        "asks to \"compare clusters with the Option-A correlation clusters (ARI/NMI) and with the "
        "regime labels\". <code>interface.md</code> §8 and §9 item 8 — a fixed group decision — "
        "instead require: \"do <b>not</b> calculate direct ARI/NMI between Option A indicator "
        "clusters and Option B time-point hidden-state clusters: indicators and time observations "
        "are different observation universes\", and §10 requires joint member approval to change "
        "that rule. Resolution implemented on this page: (1) ARI/NMI is mathematically defined only "
        "for two label vectors over the same observations; Option A partitions ~30 indicators "
        "(15–21 retained per 36-month window) while Option B partitions 224 month-end time "
        "observations in 32-dimensional hidden-state spaces, so no cross-unit ARI/NMI value exists "
        "to compute without first inventing a mapping between indicators and time points. "
        "(2) The mathematically defined parts of the assignment requirement are fully implemented: "
        "Option B's latent clusters are compared with the regime labels via ARI/NMI on identical "
        "observations under two explicit alignments (d_t and d_(t+1); Layer 2 below), and "
        "within-method stability ARI is computed on both sides (Option A across adjacent windows "
        "on the common retained indicators; Option B across seeds on the common 224 time "
        "observations). (3) The assignment's analytical intent — a comparison against Option A's "
        "cluster structure and the regime labels — is preserved by the ratified three-layer "
        "framework. (4) Any future cross-unit quantitative bridge must first be defined "
        "mathematically and approved through interface.md §10; nothing of that kind is computed "
        "here.</p>",
        "<h3>Layer 1 — method-specific clustering quality and stability</h3>",
        "<p><b>Different observation units:</b> Option A clusters <i>indicators</i> under "
        "correlation-based distance; Option B clusters <i>time points</i> in learned embedding "
        "spaces. The tables below are reported separately and must not be ranked against one "
        "another or merged into a single score (interface.md §9 item 8).</p>",
    ]
    m = load_json(A / "clustering" / "metrics.json")
    if m is not None:
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
    else:
        last = None
        rows = []
    body.append("<p><b>Option A:</b> Ward linkage on d_ij = sqrt(2(1-rho_ij)), Pearson correlations "
                "on a common complete sample per 36-month window. Coverage rule: at least 95%; "
                "derived spreads removed when their components are retained; PSD and "
                "Euclidean-identity checks run for every window.</p>")
    if m is not None:
        body.append(table_html(["Window end", "d", "Months", "k", "Silhouette", "Sil k=4",
                                "ARI vs prev", "Min eig", "Max Euclid diff", "PSD"], rows,
                               "Option A clustering quality and stability (indicators)"))
        body.append(
            f"<p>Mean silhouette {fmt(m['metrics']['mean_silhouette_selected'])}; mean adjacent ARI "
            f"{fmt(m['metrics']['mean_adjacent_ari'])}; all PSD {m['metrics']['all_psd_ok']}; "
            f"all Euclidean {m['metrics']['all_euclidean_ok']}; worst |identity error| "
            f"{fmt(m['metrics']['max_euclidean_abs_diff'], 3)}.</p>")
        body.append("<p class='note'>Option A silhouette values are method-specific diagnostics "
                    "for indicators under correlation distance. They are method-comparable only "
                    "within Option A (silhouette_k4 is the fixed-k comparability sensitivity) and "
                    "must not be ranked against Option B's.</p>")
    else:
        body.append("<p class='warn'>Option A clustering metrics are not available; its Layer-1 "
                    "table is omitted rather than reconstructed.</p>")
    bc = get_payload("b_clus", B / "clustering" / "metrics.json",
                     ["metrics.lstm", "metrics.gru", "params.n_embeddings", "params.seeds",
                      "params.leakage_discipline", "history.k_selection.lstm",
                      "history.k_selection.gru"],
                     "B clustering metrics")
    # Layer 1 checks for Option B clustering
    bc_rng, bc_pair, bc_seedok = [], [], []
    if bc is not None:
        prm = bc.get("params", {})
        n_emb = prm.get("n_embeddings")
        for arch in ("lstm", "gru"):
            mm = bc["metrics"][arch]
            ps = mm.get("per_seed", {})
            sil_vals = []
            for seed_key, e in ps.items():
                if isinstance(e, dict) and e.get("silhouette") is not None:
                    bc_rng.append((f"silhouette {arch}/seed {seed_key}",
                                   e.get("silhouette"), -1.0, 1.0))
                    sil_vals.append(float(e["silhouette"]))
            mean_of_seeds = (sum(sil_vals) / len(sil_vals)) if sil_vals else None
            if mean_of_seeds is not None and abs(mean_of_seeds - float(mm["silhouette_mean"])) > 1e-6:
                bc_pair.append(f"{arch}: silhouette_mean != mean(per-seed)")
            sel = mm.get("pairwise_ari", {})
            for kk, vv in sel.items():
                bc_rng.append((f"ari {arch}/{kk}", vv.get("ari"), -1.0, 1.0))
                if vv.get("n_common") != n_emb:
                    bc_pair.append(f"{arch}/{kk}: n_common {vv.get('n_common')} != n_embeddings {n_emb}")
            aris = [float(vv["ari"]) for vv in sel.values() if vv.get("ari") is not None]
            if aris and abs(sum(aris) / len(aris) - float(mm["pairwise_ari_mean"])) > 1e-6:
                bc_pair.append(f"{arch}: pairwise_ari_mean != mean(pairwise values)")
        add_check("B clustering: n_common == n_embeddings for every seed pair and means reproduce "
                  "stored summaries", not bc_pair, "; ".join(bc_pair[:4]) or
                  f"n_embeddings={n_emb}; all {sum(len(bc['metrics'][a].get('pairwise_ari', {})) for a in ('lstm', 'gru'))} pairwise ARIs use the common 224-observation set")
        seeds_stored = {str(s) for s in (prm.get("seeds") or [])}
        ok_seeds = "42" in seeds_stored and SEEDS_SET <= seeds_stored | {"42"}
        for arch in ("lstm", "gru"):
            ps = bc["metrics"][arch].get("per_seed", {})
            ok_seeds = ok_seeds and set(ps.keys()) == SEEDS_SET
        add_check("B clustering seeds are exactly {42, 7, 123} and include the global default 42",
                  ok_seeds, f"stored seeds: {sorted(seeds_stored)}")
        ks = bc.get("history", {}).get("k_selection", {})
        kbad = []
        for arch in ("lstm", "gru"):
            prof_k = ks.get(arch, {}).get("candidate_mean_silhouette", {})
            ks_sel = ks.get(arch, {}).get("k_selected")
            if not prof_k or ks_sel is None:
                kbad.append(f"{arch}: k-selection record missing")
                continue
            best = max(prof_k.items(), key=lambda kv: (float(kv[1]), -int(kv[0])))[0]
            if str(ks_sel) != str(best):
                kbad.append(f"{arch}: k_selected {ks_sel} != argmax {best} of stored profile")
            if str(ks_sel) != str(bc["metrics"][arch].get("k")):
                kbad.append(f"{arch}: history k_selected != metrics.k")
        add_check("B clustering k-selection consistent (selected k == argmax of stored "
                  "mean-silhouette profile)", not kbad, "; ".join(kbad) or "both architectures")
        add_check("B clustering leakage_discipline == safe",
                  (bc.get("params") or {}).get("leakage_discipline") == "safe")
        add_range_check("B clustering silhouette/ARI within [-1, 1]", bc_rng)
        # Layer 1 Option B table
        bc_rows = []
        for arch, mm in (("LSTM", bc["metrics"]["lstm"]), ("GRU", bc["metrics"]["gru"])):
            pair = mm.get("pairwise_ari", {})
            pair_str = " ; ".join(
                kk.replace("__", "↔") + ": " + fmt(vv.get("ari"))
                for kk, vv in sorted(pair.items()))
            bc_rows.append([
                arch, fmt(mm.get("k")),
                seed_series(mm.get("per_seed"), "silhouette") or "-",
                fmt_ms(mm.get("silhouette_mean"), mm.get("silhouette_std")),
                pair_str + (" (n_common " + str(next(iter(pair.values())).get("n_common")) + ")"
                            if pair else "-"),
                fmt(mm.get("pairwise_ari_mean")),
            ])
        body.append(table_html(["Architecture", "k", "Per-seed silhouette (42 / 7 / 123)",
                                "Mean ± std", "Pairwise cross-seed ARI (each pair on the common set)",
                                "Mean pairwise ARI"],
                               bc_rows, "Option B clustering quality and stability (224 time observations, seeds 42/7/123)"))
        cand = [("LSTM", bc["history"]["k_selection"]["lstm"]),
                ("GRU", bc["history"]["k_selection"]["gru"])]
        ks_rows = []
        for arch, rec in cand:
            prof_k = rec.get("candidate_mean_silhouette", {})
            ks_rows.append([arch] + [fmt(prof_k.get(str(k))) for k in range(2, 7)]
                           + [rec.get("k_selected")])
        body.append(table_html(["Architecture", "k=2", "k=3", "k=4", "k=5", "k=6", "k selected"],
                               ks_rows, "Option B candidate-k mean-silhouette profiles (each seed in its own embedding space)"))
        body.append(
            "<p class='note'><b>Initialization sensitivity caveat.</b> Cross-seed ARI is highly "
            "dispersed (exactly one seed pair per architecture reproduces a near-identical "
            "partition; the others disagree sharply). The two latent clusters are exploratory, "
            "initialization-sensitive diagnostics and are not robust stable economic regimes; "
            "this does not affect the validity of the regime-classification results in Sections 2–3."
            "</p>")
        body.append(figure_links("clustering", [
            "fig_dendrograms.html", "fig_silhouette.html", "fig_silhouette_k_curve.html",
            "fig_adjacent_ari.html", "fig_cluster_membership.html",
            "fig_lead_lag_gdp.html", "fig_cluster_regime_profile.html",
        ]) + figure_links("clustering", [
            "latent_lstm_pca.html", "latent_lstm_timeline.html",
            "latent_gru_pca.html", "latent_gru_timeline.html",
        ], approach="approach2"))

        # ---- Layer 2 --------------------------------------------------------
        body.append("<h3>Layer 2 — economic interpretation under common macroeconomic references</h3>")
        body.append(f"<p><b>Option A</b>, last window ({esc(last)}): cluster members, GDP lead-lag "
                    "and contraction/expansion profiles.</p>")
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
        body.append(table_html(["Cluster", "Members", "Best GDP lag (quarters)", "Max |mean corr|",
                                "Mean corr at best", "Indicators"], cluster_rows,
                               "Option A: cluster-level GDP lead-lag, last window"))
        body.append(table_html(["Cluster", "Members", "Mean z | contraction", "Mean z | expansion",
                                "Difference"], profile_rows,
                               "Option A: cluster profiles in contraction vs expansion, last window"))
        body.append("<p>The full indicator x lag correlation table is in "
                    "<code>results/approach1/clustering/lead_lag_gdp.csv</code>; membership for "
                    "every window is in <code>window_clusters.csv</code>. Economic category labels "
                    "(demand, prices, credit/liquidity, external sector, production) are for the "
                    "group's interpretation.</p>")
        al_rows = []
        for arch, mm in (("LSTM", bc["metrics"]["lstm"]), ("GRU", bc["metrics"]["gru"])):
            la = mm.get("label_alignment", {})
            for al in ("d_t", "d_(t+1)"):
                r = la.get(al) or {}
                al_rows.append([arch, al, r.get("n"), fmt(r.get("ari_mean_over_seeds")),
                                fmt(r.get("nmi_mean_over_seeds"))])
        body.append(table_html(["Architecture", "Alignment", "n", "ARI (mean over seeds)",
                                "NMI (mean over seeds)"], al_rows,
                               "Option B: latent clusters vs aligned PMI contraction labels"))
        la_def = ((bc.get("params") or {}).get("label_alignment") or {})
        body.append(f"<p><b>Stored alignment definitions.</b> <code>d_t</code>: "
                    f"{esc(la_def.get('d_t', 'contemporaneous label of the embedding window-end month'))}. "
                    f"<code>d_(t+1)</code>: {esc(la_def.get('d_(t+1)', 'the one-month-ahead regime-forecast target'))}. "
                    "Both are stored and computed separately; no implicit label shifting is applied.</p>")
        sh_rows = []
        for arch, mm in (("LSTM", bc["metrics"]["lstm"]), ("GRU", bc["metrics"]["gru"])):
            la = mm.get("label_alignment", {}).get("d_t", {})
            shares = la.get("contraction_share_by_cluster") or {}
            for cid in sorted(shares):
                e = shares[cid]
                sh_rows.append([arch, cid, e.get("n"), fmt(e.get("contraction_share"), 3)])
        ref = (((bc["metrics"]["lstm"].get("label_alignment") or {}).get("d_t") or {})
               .get("reference_seed_for_shares"))
        body.append(table_html(["Architecture", "Latent cluster", "n", "Contraction share (d_t = 1)"],
                               sh_rows, "Option B: contraction shares by latent cluster (reference seed "
                               + str(ref if ref is not None else "-") + "; illustrative of one initialization)"))
        body.append(
            "<p class='warn'><b>No ARI/NMI between Option A indicator clusters and Option B "
            "time-point clusters exists on this page by construction</b> — the two partitions live "
            "on different observation universes (indicators vs month-end time observations), and "
            "any cross-unit mapping would have to be invented. The interpretations above relate "
            "each method's own clusters to the same macroeconomic references (PMI contraction "
            "labels, GDP lead-lag behavior) without forcing an equivalence between them.</p>")

        # ---- Layer 3 ----------------------------------------------------------
        body.append("<h3>Layer 3 — practical value for economic analysis and contraction monitoring</h3>")
        body.append(
            "<p><b>What Option A provides:</b> a transparent indicator grouping whose correlation "
            "geometry is verified, a per-window silhouette/stability record, and representative "
            "lead-lag evidence against GDP. The main limitation is that the retained universe is "
            "coverage-dependent (about 15-21 indicators per window), and the early windows are "
            "smaller than the 30-indicator panel. Cluster count selected by maximum silhouette can "
            "choose many small clusters in some windows; the fixed k=4 column is the comparability "
            "sensitivity.</p>")
        body.append(
            "<p><b>What Option B provides:</b> a state-recognition view in time — 224 month-end "
            "hidden-state embeddings whose two-state structure shows some alignment with the "
            "contraction labels (stored ARI 0.08-0.13 and NMI 0.14-0.22 across architectures and "
            "alignments), but the association is limited and the resulting partitions are highly "
            "sensitive to initialization — the cluster shares are therefore illustrative of "
            "individual initializations — plus honest out-of-sample regime "
            "probabilities (Section 2) and the leakage demonstration (Section 3) quantifying what "
            "the publication-lag discipline protects. Its main limitation is the low cross-seed "
            "stability of the latent partition.</p>")
        body.append(
            "<p><b>Task-oriented assessment (no aggregate score).</b> For understanding <i>which "
            "indicators co-move</i> and how those groups relate to GDP's lead-lag structure, "
            "Option A is the direct instrument. For recognizing <i>when</i> the macro system "
            "shifts between phases, Option B's latent timeline is the relevant diagnostic, with "
            "its regime classifiers supplying the out-of-sample evidence. Neither method yields a "
            "stable, definitive taxonomy of the Chinese macroeconomy on the frozen snapshot; both "
            "produce suggestive but unstable structure. Following interface.md §9, no single "
            "weighted score combining the two methods' unlike metrics is computed.</p>")

    return "\n".join(body)


# ---------------------------------------------------------------------------
# Section 5: validation and leakage discipline (side by side)
# ---------------------------------------------------------------------------

def leakage_section() -> str:
    v = load_json(A / "validation" / "validation_report.json")
    body = ["<h2>5. Validation and leakage discipline (side by side)</h2>"]
    body.append("<h3>Option A — availability audit</h3>")
    if v is None:
        body.append("<p>Validation report is not available.</p>")
    else:
        body += [
            f"<p>Validation checks: {v['n_passed']} passed / {v['n_failed']} failed "
            f"(total {v['n_checks']}). All passed: <b>{v['all_passed']}</b>.</p>",
            "<ul>",
            "<li>Frozen SHA-256 verified for the monthly panel and quarterly GDP source.</li>",
            "<li>Every forecast uses the publication-lag rule s + L_i &lt;= t; training-window-only "
            "standardization.</li>",
            "<li>GDP targets never enter their own M1/M2/M3 training sets.</li>",
            "<li>2026-11 partial-month observations are excluded from every model sample.</li>",
            "<li>Clustering PSD and Euclidean-identity checks pass for every window.</li>",
            "<li>Headline metrics were recomputed from row-level predictions by "
            "<code>approach1_validate.py</code>.</li>",
            "</ul>",
        ]
    body.append("<h3>Option B — discipline evidence</h3>")
    body += [
        "<ul>",
        "<li>Seeds 42 / 7 / 123 recorded in every deep-learning <code>metrics.json</code> (the "
        "global default 42 included), with per-seed results retained and mean ± std rendered on "
        "this page.</li>",
        "<li>Decision thresholds per seed/fold are selected only on the 24-month validation tail "
        "inside the training window (balanced accuracy; pos weight capped at 1.0); no test "
        "information enters model selection.</li>",
        "<li>Lookback h is selected once per label threshold on inner validation targets "
        "2018-01..2019-12 with the fit strictly before 2018-01 (h=3 at threshold 50; h=12 at "
        "49.5); candidate scores are stored in <code>params.h_selection</code>.</li>",
        "<li>Residual CPU last-bit nondeterminism is documented per script in "
        "<code>params.residual_nondeterminism</code>.</li>",
        "<li>Latent-state clustering trains only on label pairs observable at the clustering "
        "origin 2026-10-31 and excludes the partial 2026-11 row.</li>",
        "<li>The intentionally leaky ablation arms are labelled INVALID everywhere: filenames, "
        "figure titles, the <code>leakage_discipline: intentional-leakage-demo</code> metrics "
        "flag, and a per-row <code>leakage_discipline</code> column in "
        "<code>results/approach2/leakage_ablation/predictions.csv</code> (1,066 rows = 82 probit "
        "+ 492 safe-RNN + 492 leaky-RNN). Factual notes: "
        "<code>results/approach2/leakage_ablation/NOTES.md</code>.</li>",
        "</ul>",
        "<p class='note'>The two blocks audit different instruments: Option A's independent "
        "validation script re-derives feature rows and metrics from the stored pipeline, while "
        "Option B's evidence is discipline-by-construction plus the mandatory ablation. They are "
        "presented side by side and are not averaged into one score.</p>",
    ]
    return "\n".join(body)


# ---------------------------------------------------------------------------
# Section 6: generation-time check summary + page assembly
# ---------------------------------------------------------------------------

def checks_section() -> str:
    body = ["<h2>6. Generation-time validation checks</h2>"]
    if not CHECKS:
        body.append("<p class='warn'>No checks were executed.</p>")
        return "\n".join(body)
    ok = sum(1 for c in CHECKS if c["status"] == "passed")
    warn = sum(1 for c in CHECKS if c["status"] == "warning")
    fail = sum(1 for c in CHECKS if c["status"] == "failed")
    body.append(f"<p><b>{ok} passed / {warn} warnings / {fail} failed</b>. Only checks actually "
                "executed during this generation are listed. Failed checks suppress only the "
                "affected comparison block; warnings are informational.</p>")
    items = []
    for c in CHECKS:
        css = {"passed": "st-pass", "warning": "st-warn", "failed": "st-fail"}[c["status"]]
        d = " — " + esc(c["detail"]) if c["detail"] else ""
        items.append("<li><span class='" + css + "'>" + esc(c["status"].upper())
                     + "</span> " + esc(c["name"]) + d + "</li>")
    body.append("<ul>" + "".join(items) + "</ul>")
    return "\n".join(body)


def post_firewall_checks(inner: str) -> None:
    b_rows_ok = all(f"<td>results/approach2/{t}/metrics.json</td>" in inner
                    for t in ("regime", "regime_threshold49_5", "clustering", "leakage_ablation"))
    add_check("Option B inputs table (Section 0) lists the four discovered artifacts", b_rows_ok)
    ncv_path = "results/approach1/probit_nested_cv_pca/**"
    ok_ncv = (inner.count("probit_nested_cv") == 1
              and ("Excluded from all comparison tables by design: <code>" + ncv_path
                   + "</code>") in inner)
    add_check("Nested-CV Probit experiment excluded (single declarative exclusion mention; "
              "its metrics are never rendered)", ok_ncv,
              f"{inner.count('probit_nested_cv')} mention(s); exclusion note "
              + ("present" if ("excluded" in inner) else "missing"))
    add_check("Cross-observation-universe ARI/NMI prohibition statement present",
              "No ARI/NMI between Option A indicator clusters and Option B time-point clusters"
              in inner)
    add_check("INVALID legend rendered for the leaky ablation arms",
              "intentional leakage demonstration only; not a valid predictive model" in inner)
    heads = [inner.count("<h2>1. GDP nowcasting"),
             inner.count("<h2>2. Regime classification"),
             inner.count("<h2>3. Leakage ablation"),
             inner.count("<h2>4. Clustering"),
             inner.count("<h2>5. Validation")]
    add_check("Task sections separated (exactly one heading each for GDP / regime / ablation / "
              "clustering / validation)", all(h == 1 for h in heads), f"counts={heads}")
    if "<h2>2. Regime classification" in inner and "<h2>3. Leakage ablation" in inner:
        sec2 = inner.split("<h2>2. Regime classification", 1)[1].split("<h2>3. Leakage ablation", 1)[0]
    else:
        sec2 = inner
    low = sec2.lower()
    add_check("Leaky arms absent from the joint regime tables (Section 2)",
              ("without lag discipline" not in low) and ("invalid" not in low.replace("invalidity", "")),
              "checked the regime section text")


CSS = """
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
    tr.invalid td { background: #ffe4e6; }
    .inv { border-left: 4px solid #b91c1c; background: #fff1f2; padding: 10px 12px;
           color: #881337; font-weight: 600; }
    .st-pass { color: #065f46; font-weight: 700; }
    .st-warn { color: #b45309; font-weight: 700; }
    .st-fail { color: #b91c1c; font-weight: 700; }
"""


def document(inner: str) -> str:
    return ("<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">\n"
            "<title>QF5313 Q3 - consolidated comparison</title>\n<style>" + CSS
            + "</style></head><body>\n" + inner + "\n</body></html>")


def build_inner_html(all_metrics: list[dict[str, Any]]) -> str:
    parts = [
        "<h1>QF5313 Question 3 - consolidated results</h1>",
        f"<p><b>Generated by</b> <code>code/compare.py</code> (read-only over the stored "
        f"experiment artifacts; this page does not rerun any model). Metrics files discovered: "
        f"{len(all_metrics)}.</p>",
        "<p><b>Rule:</b> GDP nowcasting, regime classification / leakage ablation and clustering "
        "are separate tasks; they are never ranked against one another in one table. GDP "
        "nowcasting has no Option B counterpart (Section 1). Raw clustering quality is never "
        "ranked across options and no cross-unit ARI/NMI exists on this page (Section 4).</p>",
        b_inputs_section(all_metrics),
        gdp_section()[0],
        regime_section(),
        ablation_section(),
        clustering_section(),
        leakage_section(),
    ]
    return "\n".join(parts)


def footer_html() -> str:
    return ("<p style=\"margin-top:40px;color:#64748b;font-size:12px\">\n"
            "Option A outputs: <code>results/approach1/</code>. Option B outputs: "
            "<code>results/approach2/</code>.<br>\n"
            "Reproduce: <code>uv run python code/approach1_data.py --seed 42</code>, "
            "<code>uv run python code/approach1_gdp_nowcast.py --seed 42</code>, "
            "<code>uv run python code/approach1_regime.py --seed 42</code>, "
            "<code>uv run python code/approach1_cluster.py --seed 42</code>, "
            "<code>uv run python code/approach1_validate.py --seed 42</code>, "
            "<code>uv run python code/approach2_regime.py --seed 42</code>, "
            "<code>uv run python code/approach2_clustering.py --seed 42</code>, "
            "<code>uv run python code/approach2_leakage_ablation.py --seed 42</code>, "
            "<code>uv run python code/compare.py --seed 42</code>.\n</p>")


def run(seed: int) -> Path:
    np.random.seed(seed)
    all_metrics = load_all_metrics()

    inner = build_inner_html(all_metrics)
    post_firewall_checks(inner)
    html_doc = document(inner + checks_section() + footer_html())
    OUT_HTML.write_text(html_doc, encoding="utf-8")

    gdp = load_json(A / "gdp_nowcasting" / "metrics.json")
    reg = load_json(A / "regime" / "metrics.json")
    clu = load_json(A / "clustering" / "metrics.json")
    val = load_json(A / "validation" / "validation_report.json")
    breg = load_json(B / "regime" / "metrics.json")
    babl = load_json(B / "leakage_ablation" / "metrics.json")
    bcls = load_json(B / "clustering" / "metrics.json")

    tbl = Table(title="Consolidated comparison", show_lines=False)
    tbl.add_column("Task", style="bold")
    tbl.add_column("Headline", justify="left")
    if gdp:
        m = gdp["metrics"]["overall"]
        tbl.add_row("GDP nowcasting (A only)", f"RMSE {fmt(m['rmse'])} vs RW {fmt(gdp['metrics']['benchmark']['rw_drift']['rmse'])}; n={m['n']}")
    if reg:
        m = reg["metrics"]["overall"]
        tbl.add_row("Regime A (factors + LPR)", f"Brier {fmt(m['brier'])}, AUC {fmt(m['auc'])}, n={m['n']}")
    if breg:
        pm = breg["metrics"]["per_model"]
        for arch in ("lstm", "gru"):
            mean = pm[arch]["mean"]
            tbl.add_row(f"Regime B {arch.upper()} (safe, 3 seeds)",
                        f"Brier {fmt(mean['brier'])} ± {fmt(breg['metrics']['per_model'][arch]['std']['brier'])}, "
                        f"AUC {fmt(mean['auc'])}")
    if babl:
        pr = babl["metrics"]["leakage_premium"]
        tbl.add_row("Leakage ablation",
                    f"LSTM gap {fmt(pr['lstm']['brier_gap_safe_minus_leaky'])}, GRU gap "
                    f"{fmt(pr['gru']['brier_gap_safe_minus_leaky'])} (leaky arms INVALID demos)")
    if clu:
        m = clu["metrics"]
        tbl.add_row("Clustering A", f"mean silhouette {fmt(m['mean_silhouette_selected'])}, mean ARI {fmt(m['mean_adjacent_ari'])}, {m['n_windows']} windows")
    if bcls:
        mm = bcls["metrics"]
        tbl.add_row("Clustering B",
                    f"LSTM sil {fmt(mm['lstm']['silhouette_mean'])} ± {fmt(mm['lstm']['silhouette_std'])}, "
                    f"GRU sil {fmt(mm['gru']['silhouette_mean'])} ± {fmt(mm['gru']['silhouette_std'])}, "
                    f"mean pairwise ARI {fmt(mm['lstm']['pairwise_ari_mean'])}/{fmt(mm['gru']['pairwise_ari_mean'])}")
    if val:
        tbl.add_row("Validation A", f"{val['n_passed']}/{val['n_checks']} checks passed")
    okn = sum(1 for c in CHECKS if c["status"] == "passed")
    warnn = sum(1 for c in CHECKS if c["status"] == "warning")
    failn = sum(1 for c in CHECKS if c["status"] == "failed")
    tbl.add_row("Generation checks", f"{okn}/{len(CHECKS)} passed, {warnn} warnings, {failn} failed")

    CONSOLE.print(Panel.fit("[bold]QF5313 Q3 consolidated comparison[/bold]", border_style="cyan"))
    CONSOLE.print(tbl)
    if failn:
        for c in CHECKS:
            if c["status"] == "failed":
                CONSOLE.print(f"[red]FAILED[/red] {c['name']}: {c['detail']}")
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

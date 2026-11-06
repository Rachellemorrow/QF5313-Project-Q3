# AGENTS.md — Contract for terminal coding agents

This is an independent GitHub repository dedicated exclusively to QF5313
Question 3 (China macro panel: regimes, nowcasting, indicator clustering),
Option A + Option B. All paths in this document are relative to the
repository root.

## 0. Project documentation

- `assignment.md` contains the original course assignment
  specification and grading requirements. Read it before
  implementation. Treat it as the authoritative source for
  assignment requirements; do not modify or overwrite it.
- `AGENTS.md` defines the project objectives, implementation
  constraints, coding conventions, and agent behavior rules.
- `interface.md` defines the shared data, target, timing, and output
  contracts for Option A and Option B. Read it before implementing
  or modifying any shared input/output logic.
- The root-level `report.md` is the group's final, human-authored
  project report, not the original assignment specification.
  Report writing, mathematical decisions, verification, and final
  interpretation remain the group's responsibility.

## 1. Goal and mathematical objective

Build a monthly macro panel `x_{i,t}`, `i = 1..d`, from AkShare
`macro_china_*` interfaces (GDP, CPI, PPI, PMI — NBS and Caixin —, M0/M1/M2,
TSF, LPR, Shibor, retail sales, industrial output, industrial profits,
exports/imports YoY, trade balance, consumer confidence, electricity output,
...), with:

- **Scale**: `d >= 20` indicators, `>= 10` years of monthly observations.
- **Snapshot fallback**: committed snapshot `data/clean/macro_panel.csv`;
  every external interface used by `data/fetch.py` must have a committed
  snapshot, because grading runs offline from a clean clone.
- **Provenance**: per-indicator publication lag `L_i` documented in
  `data/metadata.json` (source, retrieval date, schema, SHA-256 of each
  artifact).
- **Information-set constraint (grading red line)**:
  `F_t = { x_{i,s} : s + L_i <= t }`; any use of `x_{i,s}` with
  `s + L_i > t` is leakage and a grading fault.

### Option A — Classical

1. **PCA factor model**: factorize the standardized panel
   `x~_t = L * f_t + e_t` with leakage-safe standardization: means and
   standard deviations are estimated **exclusively from each training
   window**, PCA is fitted **only on the standardized training data**, and
   the fitted standardization and PCA transformations are then applied to
   the corresponding validation/test data. Select the factor count `r` by
   cross-validated nowcasting error.
2. **Bridge regression nowcasting** via `y_t = b0 + b' f_t`, estimated on
   `F_t` only, with **quarterly GDP as the sole nowcasting target**.
   Read `data/clean/gdp_quarterly.csv` and construct the derived
   single-quarter GDP growth target exactly as specified in `interface.md`.
   Preserve the published source fields; do not label cumulative Q2–Q4
   growth as single-quarter growth. Report the within-quarter nowcast
   revision path. Industrial value-added growth remains a monthly predictor,
   not a second nowcasting target.
3. **Probit regime classifier**: `P(d_{t+1}=1) = Phi(beta' z_t)`, with `z_t`
   the factors plus yield/spread controls; coefficient table with standard
   errors. Forecast-timing requirements:
   - every predictor in `z_t` uses **only information available at the
     forecast origin `t`**;
   - **publication lags are respected for every predictor**;
   - the target `d_{t+1}` is aligned with the next value of the
     two-observation PMI contraction label defined below.
4. **Contraction label**: `d_t = 1{PMI_t < 50 and PMI_{t-1} < 50}`; state
   and justify the labeling, and report a sensitivity analysis using the
   49.5 threshold.
5. **Indicator clustering**: Ward linkage on
   `d_ij = sqrt(2(1 - rho_ij))` computed from rolling 3-year correlations;
   dendrogram; report silhouette scores for each valid rolling window and
   stability via adjusted Rand index across adjacent windows on the common
   retained indicator universe; relate clusters to lead–lag cross-correlations
   with GDP. **Ward linkage
   requires Euclidean-compatible input distances**: verify and document
   that this distance construction is Euclidean-compatible for the data
   actually used (for example, the sample correlation matrix is positive
   semi-definite on the retained series, so `d_ij` equals the Euclidean
   distance between appropriately normalized indicator vectors), and
   document the implementation (numerical routine, distance construction
   constants, and handling of degenerate or non-Euclidean cases).
   **Do not silently replace** the specified distance or linkage with
   another method. Raise any proposed deviation to the group for an explicit
   decision; provide the technical justification and supporting evidence
   for the group to consider in its final report.

### Option B — Sequence learning (PyTorch)

1. **Regime classifier**: LSTM/GRU with **no more than two layers** (dropout
   reported); input = window `x~_{t-h:t}`; output `P(d_{t+1} = 1)`. Handle
   class imbalance explicitly (loss weighting or threshold selection on
   validation).
2. **Deep-learning seeds**: every deep-learning experiment runs with
   **at least three different random seeds**, and the global default seed
   **42** must be one of them; **all seeds are recorded in the
   corresponding `metrics.json`**; results are reported as **mean ± std
   across seeds, alongside each individual per-seed result**.
3. **Latent states and clustering evaluation**: apply k-means to hidden-state
   embeddings `h_t`, preserving the time observation represented by each
   embedding. For each seed, report the silhouette score in that seed's
   embedding space. Assess state-clustering stability across random seeds by
   pairwise ARI on the same common set of time observations (report the
   pairwise results and their mean). Compare latent-state assignments with
   the aligned PMI contraction labels using ARI/NMI where the time alignment
   is explicit. Do **not** calculate ARI/NMI directly against Option A's
   indicator clusters: the two approaches cluster different kinds of objects.
4. **Leakage ablation (mandatory)**: compare walk-forward Brier/AUC of
   (i) the leakage-safe probit, (ii) the leakage-safe LSTM/GRU, and
   (iii) an LSTM/GRU **without** lag discipline. The leaky experiment is an
   **intentional leakage demonstration, not a valid predictive model**, and
   must be clearly labelled in filenames, metrics, and figures. Provide the
   group with the corresponding factual notes for its human-authored report.

### Validation

- Expanding-window walk-forward validation.
- Metrics: Brier score, AUC, accuracy.
- Naive prior-frequency baseline reported alongside every headline result.
- Standardization statistics computed from **training windows only**.
- Shared regime-classification schedule (see `interface.md`): initial historical labels `2008-02`–`2019-12`; out-of-sample target months `2020-01`–`2026-10`; month-end forecast origins `2019-12-31`–`2026-09-30`; one-month-ahead expanding-window evaluation. Use the target month as the fold ID. Option A Probit and Option B LSTM/GRU must use the same target months, folds, and realized labels.
- GDP Nowcasting is Option A's only Nowcasting target: initial training quarters `2008Q1`–`2017Q4`; out-of-sample quarters `2018Q1`–`2026Q3`; generate M1/M2/M3 month-end vintages for each target quarter as specified in `interface.md`.
- Evaluate the two clustering tasks under the three-layer comparison framework
  in `interface.md` Section 9: (1) method-specific clustering quality and
  stability, (2) economic interpretation against common macroeconomic
  references, and (3) task-oriented practical value. Do not compare raw
  silhouette scores across A and B or calculate direct ARI/NMI between
  indicator clusters and time-point hidden-state clusters.

## 2. Environment facts and run commands

- Python 3.13, managed with `uv`. Reproducible setup:

```bash
uv venv --python 3.13
uv sync
```

- Dependencies must be declared in `pyproject.toml` and locked in
  `uv.lock`. Both files must be committed and kept in sync whenever
  project dependencies change.
- Exact run commands for Q3 scripts belong in this section, one per
  existing script.
- `data/fetch.py` (Phase 2 data acquisition, raw snapshots under `data/raw/`):

```bash
uv run python data/fetch.py --seed 42     # live fetch (idempotent, never deletes)
uv run python data/fetch.py --offline     # verify committed snapshots + SHA-256
```

- `data/build_panel.py` (Phase 2 cleaning → `data/clean/*`, `data/metadata.json`):

```bash
uv run python data/build_panel.py --seed 42
```

- `code/approach1_data.py` (Option A data, schedule, target, and leakage checks → `results/approach1/data/`):

```bash
uv run python code/approach1_data.py --seed 42
```

- `code/approach1_gdp_nowcast.py` (Option A PCA bridge GDP nowcasting, M1/M2/M3 → `results/approach1/gdp_nowcasting/`):

```bash
uv run python code/approach1_gdp_nowcast.py --seed 42
```

- `code/approach1_regime.py` (Option A Probit regime classifier + 49.5 sensitivity → `results/approach1/regime/`):

```bash
uv run python code/approach1_regime.py --seed 42
```

- `code/approach1_cluster.py` (Option A rolling 3-year Ward indicator clustering → `results/approach1/clustering/`):

```bash
uv run python code/approach1_cluster.py --seed 42
```

Append each `uv run python <path> --seed 42` command
  here at the same commit that introduces the script; never document
  commands for scripts that do not exist.
- The scripts required by the assignment are `code/approach1_*.py`,
  `code/approach2_*.py` and `code/compare.py`; optional helper
  `data/fetch.py`.

## 3. Conventions

- Global seed: **42**, set via `--seed 42` CLI default (argparse), applied
  to every stochastic step; any residual nondeterminism documented per
  script.
- Every `code/approachN_*.py` is self-contained, argparse-driven, with a
  linear pipeline: load → verify → split → fit/train → evaluate → write
  `results/approachN/<dataset>/metrics.json` + figures.
- `metrics.json` schema (base keys preserved):
  `{"params": {...}, "metrics": {...}, "fit_seconds": float, "history": {...}}`
  — and each experiment must additionally retain:
  - model parameters **and the random seed**;
  - training and testing time ranges (per walk-forward window and overall);
  - Brier score, AUC, accuracy where applicable;
  - baseline metrics for the same split;
  - individual walk-forward window metrics, in addition to aggregated
    results.
- Deep-learning `metrics.json` files report **mean ± std across >= 3
  seeds** alongside per-seed entries.
- **Leakage-safe vs. intentionally leaky** experiments must be
  distinguishable everywhere: distinct filenames (e.g. `*_leaky`),
  distinct `metrics.json` fields (e.g. `"leakage_discipline": "safe"` vs
  `"leakage_discipline": "intentional-leakage-demo"`), and separate
  figures. Provide the group with the labels, experimental evidence, and
  factual notes needed to document the leakage demonstration in its report.
- Figures: `plotly` only, written as self-contained HTML
  (`include_plotlyjs=True`) under `results/`. Provide figure paths and brief
  descriptions to the group for inclusion in its report.
  `code/compare.py` reads every relevant `metrics.json`, **groups
  experiments by task and target**, and compares only methodologically
  comparable experiments — GDP nowcasting, regime classification and
  clustering are separate, task-specific sections and are **never ranked
  against one another in a single table**. The clustering section must use
  the three-layer A/B comparison in `interface.md` Section 9: method-specific
  quality/stability, shared economic interpretation, and practical value.
  Do not compare raw A/B silhouette values or calculate direct cross-method
  ARI/NMI between indicator clusters and time-point hidden-state clusters.
  Write the consolidated result to `results/comparison.html`.
- Terminal output via `rich` (panel + metrics table); no blob text dumps.
- Panel hygiene: missing-value report with every fetch; outlier rule (MAD)
  stated; log/YoY/seasonal-adjust transformations documented per series;
  standardized panel `x~_{i,t} = (x_{i,t} - mu_i) / s_i` with statistics
  from training windows only.

## 4. Agent rules

- Read `interface.md` before implementing or modifying shared data inputs,
  target construction, forecast timing, prediction outputs, or comparison
  logic. Treat its approved conventions as the shared contract. Do not
  change shared schemas or definitions unilaterally.
- The current data snapshot is frozen for model development. Do not run
  live data fetches or rebuild the panel, and do not modify `data/raw/`,
  `data/clean/`, `data/metadata.json`, `data/fetch.py`, or
  `data/build_panel.py` unless both group members explicitly approve the
  change. Write derived model targets, predictions, metrics, and figures
  under the appropriate `results/` directories.
- Write **nothing** outside the repository root.
- Tool and library caches live in-repo and are gitignored.
- Propose-then-act for destructive operations: deleting data files, wiping
  directories, force flags, history rewrites — nothing without explicit
  approval in the session transcript.
- Commit after each milestone with conventional prefixes: `data:`, `feat:`,
  `exp:`, `report:`. Final submission is tagged `submission`.
- Never commit secrets (`.env`, API keys, tokens) — ever.
- Do not run `git push`: pushing is the group's own manual terminal step.
- Keep `README.md` concise (no more than 10 lines), covering the project
  purpose, basic usage, and an accurate one-command reproduction instruction
  for a clean clone. Add the reproduction command only after the required
  scripts and dependencies exist; verify that the command works before
  documenting it. Never invent commands or claim unverified reproducibility.
- Do not fabricate, manually reconstruct, or substitute a summary for
  terminal-agent session logs. The complete session history must be exported
  using the agent's official session-export functionality and preserved in
  `SESSION.md`. Redact secrets and credentials before committing. Do not
  create a substitute transcript if an official export is unavailable; tell
  the group that the export is still required.
- Treat `.gitignore` as a submission-critical configuration. Do not create,
  modify, or broaden ignore rules without explicit user approval. Never add
  patterns that would exclude required source code, committed data snapshots,
  metrics, figures, or other submission artifacts.
- The root-level `report.md` is a human-authored deliverable and remains the
  group's responsibility. **Do not create, edit, overwrite, delete, or
  otherwise modify it**, even if it does not yet exist. The agent may generate
  code, data, metrics, figures, tables, and factual experimental findings in
  their appropriate project locations, and provide those materials to the
  group. All report writing and final interpretation must be done by the group.

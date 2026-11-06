# QF5313 Q3 — Shared Interface Contract

**Status:** Shared implementation contract for the frozen data snapshot. Shared data, target, timing, evaluation, and Option A/Option B clustering-comparison decisions are fixed below. Both members must use the same conventions; any later change requires joint approval.

**Purpose:** Define how Option A (Classical) and Option B (Sequence Learning) share data and compatible evaluation conventions. This document implements the course requirements; it does not replace `assignment.md` or `AGENTS.md`. If any rule conflicts with the course assignment, `assignment.md` takes precedence.

## 1. Responsibilities and shared principles

- Member A owns Option A and its experiments. Member B owns Option B and its experiments.
- Both members use the same frozen predictor panel and metadata. Both must respect the same indicator definitions, publication-lag information, missing-value policy, and PMI regime-label definition.
- A shared interface specifies inputs and outputs; it does not require identical internal model representations. PCA/factor matrices and LSTM/GRU sequence tensors may differ, provided they use information available at the same forecast origin.
- For directly comparable regime-classification results, both members must use the same target definition, forecast origins, target periods, folds, and evaluated observations.
- No member or agent may independently rename a shared column, alter a target definition, change a date convention, or change a shared output schema. Propose the change, obtain review from both members, and update this document with the implementation.
- The group owns mathematical decisions, verification, interpretation, and the human-authored root-level `report.md`. Agents may provide code, metrics, figures, and factual findings, but must not create or modify `report.md`.

## 2. Shared repository paths

| Path | Contract |
|---|---|
| `data/clean/macro_panel.csv` | Frozen shared monthly predictor panel used by both approaches. |
| `data/clean/gdp_quarterly.csv` | Frozen AkShare quarterly GDP source data used by Option A to construct the GDP nowcasting target. |
| `data/metadata.json` | Frozen source, schema, transformation, publication-lag, coverage, decision, and SHA-256 metadata. |
| `data/clean/missing_report.md` | Frozen missingness and outlier report for the monthly panel. |
| `code/approach1_*.py` | Option A model and experiment scripts. |
| `code/approach2_*.py` | Option B model and experiment scripts. |
| `code/compare.py` | Reads experiment outputs and compares only methodologically comparable tasks/targets. |
| `results/approach1/` | Option A metrics, predictions, derived target checks, and figures. |
| `results/approach2/` | Option B metrics, predictions, and figures. |
| `results/comparison.html` | Consolidated task-specific comparison output. |

Do not create separate copies or differently named versions of the shared panel for the two approaches. Do not assume any additional target or split-schedule file exists unless it has actually been created and committed.

## 3. Frozen data schemas

### 3.1 Monthly predictor panel

Canonical file: `data/clean/macro_panel.csv`.

- The frozen panel contains 227 monthly rows from `2008-01` through `2026-11`, with 30 monthly indicator columns, plus the `month` time-index column.
- `month` is represented as `YYYY-MM` (for example, `2008-01`). It identifies the economic reference month, not the date on which every observation became available.
- The 30 current indicator columns are:

```text
pmi_man, pmi_nonman, cpi_yoy, ppi_yoy, cgpi_yoy,
industrial_va_yoy, retail_yoy, fai_yoy, consumer_confidence, lpi,
elec_cons_yoy, freight_yoy, m2_yoy, m1_yoy, m0_yoy,
new_loans_yoy, tsf_flow, base_money_yoy, lpr_1y, lpr_5y,
shibor_on, shibor_3m, shibor_1y, trade_bal_bn_usd, export_yoy,
import_yoy, fx_reserves_yoy, fiscal_rev_yoy,
shibor_spread_1y_on, lpr_spread_5y_1y
```

- The committed CSV header is the source of truth. Use these names exactly; do not rename columns independently.
- Values remain in the economic units and transformations documented in `data/metadata.json`. Do not put full-sample standardized values in the shared panel.
- Missing values remain missing (`NaN`/empty CSV fields). Do not replace them with zero, backward-fill, or interpolate using future observations.
- Standardization, imputation, feature selection, and PCA fitting must occur inside each training window as specified in Section 4.
- The `2026-11` Shibor and Shibor-spread values are partial-month means over November 1–5, 2026. Preserve the frozen data, but exclude these partial-month observations from model training and backtesting; do not treat them as complete November monthly averages.
- Series with shorter coverage or documented stale gaps (for example, `tsf_flow`) remain as recorded in the frozen panel. Model scripts must respect their missingness and metadata; they must not silently fill the gaps.

### 3.2 Metadata and provenance

Canonical file: `data/metadata.json`.

For each indicator, use the metadata entry for its stable name, AkShare interface, source field, unit, transformation, coverage, publication lag, lag basis, and notes. Read each indicator's `publication_lag_months`; do not substitute a blanket lag for all indicators.

The frozen snapshot was built at `2026-11-05T13:30:38Z`. The SHA-256 values below identify the exact files reviewed for this contract:

| Artifact | SHA-256 |
|---|---|
| `data/clean/macro_panel.csv` | `725699fc99e28878fbe24ef52308148f15f4058ecead8f3d750c67b273b5eabe` |
| `data/clean/gdp_quarterly.csv` | `b7c93b71f1df5f513f7105535ff2dbba59dbd4c0ab66bdf91b612a92cc2b0c01` |

Model scripts may verify these hashes when loading the files, but must not rewrite the data files or metadata. If the committed files do not match these hashes, stop and ask the group to reconcile the version; do not silently rebuild or replace the frozen snapshot.

### 3.3 Quarterly GDP source file

Canonical file: `data/clean/gdp_quarterly.csv`.

The frozen source file contains 83 continuous quarter records from `2006Q1` through `2026Q3`, with these columns:

| Column | Meaning |
|---|---|
| `quarter` | Native quarterly period label, formatted `YYYYQn`. |
| `gdp_abs_cum_bn` | Cumulative nominal GDP level as published by the AkShare interface. **Despite the legacy `bn` suffix, the unit is RMB 100 million (亿元), not RMB billion.** |
| `gdp_yoy` | Published year-on-year GDP growth for the period represented by `window`. |
| `window` | `single_quarter` for Q1; `ytd_cumulative` for Q2 (H1), Q3 (first three quarters), and Q4 (full year). |

The file is the source input and must remain unchanged. Q2–Q4 `gdp_yoy` values are cumulative YTD growth rates; they are not single-quarter growth rates.

## 4. Information set and preprocessing

For each monthly forecast origin `t`, predictor availability must obey the course information-set rule:

`F_t = { x_{i,s} : s + L_i <= t }`.

- Treat each monthly forecast origin as the **end of the relevant calendar month**. The panel's `month` value remains a reference-month label (`YYYY-MM`).
- Convert `publication_lag_months` in `data/metadata.json` into the corresponding month-index availability rule. For example, `L_i = 1` means a reference-month observation is not available at the preceding month-end origin; it becomes eligible at the end of the following month. `L_i = 0` is eligible only when the series is actually released by that reference month-end.
- Apply each series' own lag from metadata. The metadata distinguishes verified release timing from conservative or estimated assumptions; do not silently upgrade an estimated lag to a verified fact.
- The final downloaded panel contains later observations, but their presence in the file does not make them historically available at an earlier forecast origin.
- Historical training labels must also have been published by the forecast origin. Apply the target's publication timing separately from predictor timing.
- Use expanding-window walk-forward validation. For regime classification, the shared schedule is fixed in Section 5.1: the initial historical label window is `2008-02`–`2019-12`; out-of-sample target months are `2020-01`–`2026-10`, forecast from month-end origins `2019-12-31`–`2026-09-30` (82 one-month-ahead forecasts). Option A Probit and Option B LSTM/GRU must use the same target months, labels, folds, and evaluation observations.
- For GDP nowcasting, use the separate quarterly schedule in Section 5.2: initial training target quarters `2008Q1`–`2017Q4`; out-of-sample target quarters `2018Q1`–`2026Q3`; three within-quarter month-end vintages (M1/M2/M3) per quarter.
- Fit imputation parameters, means, standard deviations, feature selection, PCA, tuning choices, and classification thresholds using training/validation data only. Never fit these on a held-out test window.
- Apply the fitted training-window transformations unchanged to validation/test data. Do not use future-aware interpolation or backward filling.

## 5. Shared targets and evaluation

### 5.1 Regime-classification target (Option A and Option B)

The primary regime label uses the frozen `pmi_man` column (NBS manufacturing PMI):

`d_t = 1{ pmi_man_t < 50 and pmi_man_(t-1) < 50 }`.

- Also construct the required sensitivity label using threshold `49.5`.
- A prediction at forecast origin `t` targets `d_(t+1)`.
- Both members must use the same PMI series, threshold definition, label-period alignment, and label-availability rule.
- A historical label may enter the training set only once both PMI observations used to construct it would have been published.
- Option A's Probit and Option B's LSTM/GRU must use the same directly comparable forecast origins and realized labels.

**Fixed shared regime-classification schedule**

- Initial historical training-label window: `2008-02` through `2019-12` (143 available labels in the frozen panel at the primary threshold). At each forecast origin, include only labels observable by that origin.
- Out-of-sample target months: `2020-01` through `2026-10`, inclusive (82 target observations in the frozen panel; at threshold 50, 27 contraction labels and 55 non-contraction labels).
- Forecast origins: month-end `2019-12-31` through `2026-09-30`, inclusive. Store `forecast_origin` as an ISO date (`YYYY-MM-DD`), and store `target_period` as `YYYY-MM`. Each origin at month `t` predicts the label for target month `t+1`. Advance one month at a time and expand the training window as newly observable labels become available. Use the target month (`YYYY-MM`) as the classification `fold` identifier, so each one-month test fold is uniquely aligned across both members.
- Use the same 82 target months for the headline Probit/LSTM/GRU comparison. If a model implementation cannot produce a prediction for every scheduled month, report the coverage gap and calculate the direct model comparison on the common intersection only; do not compare metrics calculated on different test observations.
- Apply the 49.5 sensitivity threshold to the same calendar schedule, reconstructing the labels with 49.5 rather than reusing the 50-threshold labels.
- The `2026-11` panel row is not part of this classification schedule: PMI is unavailable for November, and the Shibor observations in that row are partial-month values.

### 5.2 GDP nowcasting target (Option A only)

The group has selected **quarterly GDP nowcasting as the only nowcasting target**. Industrial value-added growth remains a predictor in the monthly macro panel; it is not a second nowcasting target.

Source: `data/clean/gdp_quarterly.csv`, from the approved AkShare GDP interface. Do not supplement or replace this source with another data provider.

**Why a derived target is needed**

- The interface publishes Q1 as a single-quarter year-on-year growth rate, but Q2, Q3, and Q4 as cumulative YTD year-on-year growth rates.
- A cumulative growth rate must not be differenced directly to obtain a single-quarter growth rate.
- The source also contains cumulative nominal GDP levels (`gdp_abs_cum_bn`, unit 亿元). Use those levels as the 2006 reference-year anchors, then chain the published cumulative real growth rates and de-accumulate the reconstructed levels. This produces a **derived chain-linked single-quarter GDP volume estimate**; it is not a separately published official NBS single-quarter GDP series.

**Shared reconstruction definition**

Let `A_(y,q)` be the source `gdp_abs_cum_bn` value and `g_(y,q)` be `gdp_yoy` expressed as a decimal. Let `C_(y,q)` denote the reconstructed cumulative volume level.

1. Anchor each quarter's cumulative series in 2006:

   `C_(2006,q) = A_(2006,q)`.

2. For each quarter `q`, chain the cumulative volume level forward across years:

   `C_(y,q) = C_(y-1,q) * (1 + g_(y,q))`, for `y > 2006`.

3. De-accumulate within each year to obtain the single-quarter volume estimate:

   `V_(y,1) = C_(y,1)`;

   `V_(y,q) = C_(y,q) - C_(y,q-1)`, for `q = 2, 3, 4`.

4. Construct the derived single-quarter real GDP year-on-year target:

   `gdp_yoy_single_derived_(y,q) = V_(y,q) / V_(y-1,q) - 1`.

   For `2006Q1`, retain the directly published Q1 single-quarter `gdp_yoy` value if the derived target series is required to include that first observation. `2006Q2`–`2006Q4` do not have prior-year quarterly volume denominators in the frozen file and remain unavailable under this reconstruction. The first complete reconstructed target year is therefore 2007; actual model estimation begins only where the predictor panel and lag-eligible information sets overlap.

Implementation requirements:

- Derive the target in model preprocessing or a reproducible helper; do not overwrite `gdp_quarterly.csv`, `macro_panel.csv`, or `metadata.json`.
- Preserve the raw source columns and write any derived target/check output under the relevant `results/approach1/` task directory.
- Keep the target in percentage units consistently (convert the decimal formula above to percent when storing/reporting).
- Validate that all reconstructed single-quarter volume levels are positive; check that Q1 reconstructed growth from 2007 onward matches the published Q1 growth; and verify that summing reconstructed quarterly volumes through Q2/Q3/Q4 recovers the corresponding published cumulative growth rates, subject to floating-point tolerance.
- These are arithmetic consistency checks, not independent proof that the reconstructed series exactly equals an official single-quarter constant-price series. Label it as a derived chain-linked estimate in metrics, figures, and the report notes.
- Record the base year, formula, source columns, and validation checks in the experiment output.

**GDP forecast timing and revision path**

- GDP target periods use native quarter labels (`YYYYQn`).
- For each target quarter, construct monthly predictor information sets at the end of its first, second, and third months. Keep these forecast origins distinct so that the within-quarter nowcast revision path can be reported without repeating a quarterly GDP target across monthly rows.
- Fixed initial GDP training target window: `2008Q1`–`2017Q4` (40 quarters). Fixed out-of-sample target window: `2018Q1`–`2026Q3` (35 quarters).
- For every target quarter in the out-of-sample window, generate three nowcasts: M1 at the end of the quarter's first month, M2 at the end of its second month, and M3 at the end of its third month. For example, `2018Q1` uses forecast origins `2018-01-31`, `2018-02-28`, and `2018-03-31`. The full evaluation design therefore has 105 target-quarter/vintage forecast records before any documented data-availability exclusions.
- GDP's `publication_lag_months = 1` is applied to the quarter-end reference month on the monthly grid. A realized quarterly GDP label may enter a training set only after that lag makes it available (conservatively, at the end of the following month). Do not use the realized GDP target for any of its own M1/M2/M3 forecasts.
- Use an expanding quarterly training window. At each forecast origin, include only previously realized GDP targets whose publication lag has elapsed. Evaluate each nowcast only after its corresponding target has become observable. Store the forecast origin, native target quarter, and vintage separately.
- For a given vintage, use the corresponding historical within-quarter vintage information set when constructing training pairs. The implementation may fit separate M1/M2/M3 bridge models or use a pooled specification that explicitly accounts for vintage; document the choice and do not treat three repeated target values as three independent quarterly outcomes without accounting for the repeated target.

### 5.3 Walk-forward evaluation and benchmarks

- Use expanding-window walk-forward validation.
- Regime classification: report Brier score, AUC, accuracy, and the naive prior-frequency baseline evaluated on the same observations. Select any classification threshold without test-set information.
- GDP nowcasting: report at least RMSE and MAE by M1/M2/M3 vintage and overall, plus the assignment-required random-walk-with-drift benchmark on the same target periods and forecast vintages. At forecast origin `t`, define the benchmark as `y_hat(q|t) = y_(q-1) + mu_hat_t`, where `mu_hat_t` is the mean change in the derived quarterly GDP target over the historical training targets observable by `t`. Estimate the drift and select any benchmark settings using training data only. Keep nowcasting metrics separate from classification metrics.
- The GDP within-quarter revision path must show how forecast errors change from the first-month to second-month to third-month information set.
- Do not rank unrelated tasks against one another. For the GDP target, compare only GDP nowcasting methods/benchmarks evaluated on the same target definition, origins/vintages, and observations.

## 6. Model input and output contracts

### 6.1 Shared model inputs

- Both approaches read the same frozen `data/clean/macro_panel.csv` and `data/metadata.json`.
- Option A additionally reads `data/clean/gdp_quarterly.csv` to construct its GDP nowcasting target. Option B does not need the GDP target to train its regime classifier, except where GDP is used for the Option A-specified cluster lead/lag analysis.
- Option A and Option B may use different internal input shapes, but both must obey the shared predictor definitions, metadata publication lags, and training-window-only preprocessing rules.
- Option B must document its sequence lookback length `h` and map each input window to forecast origin `t` and target `d_(t+1)`. The value of `h` is a model hyperparameter selected using training/validation data only; it is not a shared fixed value. A longer lookback may reduce the number of usable early training sequences, but it must not change the fixed out-of-sample target schedule.
- Model-specific feature selection remains implementation-dependent. Both approaches start from the same approved panel and document feature exclusions and transformations.

### 6.2 Classification prediction files

Option A Probit and Option B leakage-safe LSTM/GRU must expose row-level predictions for common evaluation observations. Use the following fields:

| Column | Meaning |
|---|---|
| `model` | Model identifier (`probit`, `lstm`, or `gru`). |
| `forecast_origin` | Forecast-origin month, with the month-end convention defined in Section 4. |
| `target_period` | Reference month for `d_(t+1)`. |
| `y_true` | Realized binary contraction label. |
| `y_pred_proba` | Predicted probability of contraction. |
| `y_pred` | Predicted class using a threshold selected without test data. |
| `fold` | Walk-forward fold identifier. |
| `seed` | Actual seed for stochastic models; null/not applicable for deterministic models. |

Keep per-seed Option B predictions; do not average away individual seed rows.

Suggested paths:
- `results/approach1/regime/predictions.csv`
- `results/approach2/regime/predictions.csv`

### 6.3 GDP nowcasting prediction file (Option A)

Use one row per model, forecast origin, target quarter, and within-quarter vintage. Suggested path: `results/approach1/gdp_nowcasting/predictions.csv`.

| Column | Meaning |
|---|---|
| `model` | Nowcasting model or benchmark identifier. |
| `target` | Fixed identifier `gdp_yoy_single_derived`. |
| `forecast_origin` | Month-end date at which the nowcast is made. |
| `target_period` | Native quarter label (`YYYYQn`). |
| `y_true` | Realized derived single-quarter GDP growth, in percent. |
| `y_pred` | Nowcast, in percent. |
| `vintage` | Within-quarter information stage (`M1`, `M2`, or `M3`). |
| `fold` | Expanding-window walk-forward fold identifier. |

Add `seed` if the experiment is stochastic; otherwise use null/not applicable. Do not combine the three vintages into a single row or lose their forecast-origin identities.

### 6.4 Clustering outputs and comparability

- **Option A indicator clusters** must identify `window_end`, `indicator`, and `cluster_id`. Preserve the indicator universe for each rolling window and record the silhouette score for each valid window. Calculate adjacent-window ARI only on the indicators retained in both adjacent windows; record the common indicator count and any exclusions.
- **Option B hidden-state clusters** must identify the time observation/forecast origin and `latent_cluster_id`, as well as the random `seed`. For each seed, record the silhouette score in that seed's hidden-state embedding space. Assess cross-seed stability with pairwise ARI across cluster assignments for the same common time observations; report the common observation count and the individual pairwise ARI values (plus their mean). Keep per-seed labels and embeddings or sufficient reproducible artifacts.
- Compare Option B hidden-state clusters with the PMI contraction labels only after explicitly aligning each hidden state to its reference label and time period. For the one-month-ahead task, preserve both `forecast_origin` and `target_period`; state whether any additional contemporaneous comparison uses `d_t` or the forecast target `d_(t+1)`. Do not shift labels implicitly.
- Preserve the objects being clustered and their identifiers. ARI/NMI may be calculated only when two label vectors assign clusters to the same observations. Therefore, do **not** calculate direct ARI/NMI between Option A indicator clusters and Option B time-point hidden-state clusters: indicators and time observations are different observation universes.
- The cross-method comparison is instead defined by the three-layer framework in Section 9. Method-specific silhouette and stability statistics are reported separately; shared economic interpretation and task-oriented value are compared using common macroeconomic references.

### 6.5 `metrics.json`

Preserve the required base structure:

`{"params": {...}, "metrics": {...}, "fit_seconds": float, "history": {...}}`

Also retain:
- task and target identifiers;
- model parameters and random seed (or explicit null/not-applicable for deterministic models);
- overall and per-window training/testing time ranges;
- per-window metrics as well as aggregated metrics;
- baseline metrics evaluated on the same split/observations.

For classification, include Brier, AUC, and accuracy where applicable. For GDP nowcasting, include RMSE, MAE, benchmark metrics, and results by within-quarter vintage. For clustering, include the method-specific silhouette results and stability statistics required by Sections 6.4, 7, and 9: Option A records silhouette by rolling window and adjacent-window ARI with common-indicator counts; Option B records silhouette by seed and pairwise cross-seed ARI with common-time-observation counts. Where Option B compares latent clusters with PMI contraction labels, record ARI/NMI together with the exact label alignment (`d_t` or `d_(t+1)`) and evaluated observations. Option B deep-learning experiments must record at least three seeds including `42`, retain individual per-seed results, and report mean ± standard deviation. The intentional leakage demonstration must be clearly labelled as an invalid leakage demo in filenames, metrics, and figures.

## 7. Clustering methodology constraints

- **Option A:** use the assignment's rolling three-year correlation distance `d_ij = sqrt(2(1 - rho_ij))` and Ward linkage. Verify Euclidean compatibility for the retained series, including the relevant correlation-matrix positive-semidefinite check. Document the numerical routine, distance construction, and handling of degenerate/non-Euclidean cases. Do not silently replace the required distance or linkage. Report (i) silhouette by rolling window and (ii) adjacent-window ARI stability on the common retained indicator universe.
- **Option B:** apply k-means to hidden-state embeddings and preserve the time observations represented by each embedding. Across at least three seeds (including `42`), report (i) silhouette by seed and (ii) pairwise cross-seed ARI on the common time-observation set. Also assess the association between hidden-state clusters and aligned PMI contraction labels, with the alignment stated explicitly.
- Silhouette values for A and B are **method-specific diagnostics**, not directly comparable headline scores: A's observations are indicators under correlation distance, while B's observations are time points represented in learned embedding spaces. Do not rank A and B by their raw silhouette values.
- Do not calculate direct ARI/NMI between A's indicator clusters and B's time-point hidden-state clusters. Their observation units differ. Use the three-layer comparison framework in Section 9 instead.

## 8. Result directory and comparison contract

- Keep Option A and Option B outputs separate under `results/approach1/` and `results/approach2/`.
- Save each experiment's `metrics.json`, applicable row-level predictions, clustering labels/stability records, and self-contained Plotly HTML figures in its task-specific results directory.
- `code/compare.py` groups experiments by task and target. Keep GDP nowcasting, regime classification, and clustering outputs in separate sections; never place unrelated tasks or targets in one ranked leaderboard.
- GDP comparison must use the same derived target definition, forecast origins/vintages, and evaluation observations. Compare model forecasts with the random-walk-with-drift benchmark on the same observations.
- Regime classifiers must be compared on common origins, target periods, labels, and folds. Report the naive prior-frequency baseline on the same observations.
- The clustering section must present the three comparison layers in Section 9. Show Option A and Option B's own quality/stability metrics separately, then compare their economic interpretation and practical value using common macroeconomic reference periods and indicators. Do not create a single numeric ranking or aggregate score across the two clustering methods.
- Keep the intentionally leaky LSTM/GRU experiment separate from valid predictive models and label it as a leakage demonstration everywhere.

## 9. Fixed group decisions and the three-layer A/B comparison

The following project choices and comparison method are fixed and should be implemented as written.

1. **Nowcasting target — decided:** Quarterly GDP is Option A's only nowcasting target. This satisfies the assignment's “quarterly GDP growth (or monthly industrial output)” alternative. Industrial value-added growth remains a predictor in the monthly panel and is not a second nowcasting target.
2. **GDP target definition — decided:** Use `gdp_yoy_single_derived`, reconstructed from the frozen AkShare quarterly source as specified in Section 5.2. Preserve the raw GDP file and label the reconstructed series as a derived chain-linked estimate, not an official separately published single-quarter series.
3. **Primary PMI series — decided:** Use `pmi_man` (NBS manufacturing PMI) for the primary contraction label; also report the 49.5 threshold sensitivity analysis.
4. **Publication-lag convention — decided:** Use the integer `publication_lag_months` values in frozen `data/metadata.json` and month-end forecast origins. Preserve whether each lag basis is verified, estimated, or conservative; do not silently change a lag.
5. **Regime-classification schedule — decided:** Initial historical labels `2008-02`–`2019-12`; out-of-sample target months `2020-01`–`2026-10`; month-end forecast origins `2019-12-31`–`2026-09-30`; one-month-ahead, expanding-window evaluation. Option A and Option B use the same 82 scheduled target months and folds.
6. **GDP nowcasting schedule — decided:** Initial training target quarters `2008Q1`–`2017Q4`; out-of-sample target quarters `2018Q1`–`2026Q3`; M1/M2/M3 month-end forecast vintages for each target quarter (35 quarters and 105 forecast records before any documented availability exclusions).
7. **Frozen data ownership — decided:** The committed snapshot is the canonical shared input. Neither member nor agent may modify/rebuild it without explicit approval from both members.
8. **Option A/Option B clustering comparison — decided:** Compare the two approaches at three levels. Because Option A clusters **indicators** and Option B clusters **time-point hidden-state embeddings**, their raw clustering-quality statistics and direct cluster-label ARI/NMI are not mathematically comparable. Do not force a mapping between these different observation units.

   **Layer 1 — Method-specific clustering quality and stability (evaluate separately; do not compare raw values across methods).**
   - **Option A:** report silhouette scores for each valid rolling window and adjacent-window ARI stability, using the same retained indicator universe for each adjacent-window comparison. Explain the variation in cluster quality/stability across windows.
   - **Option B:** report silhouette scores for each random seed in that seed's hidden-state embedding space, and pairwise ARI across seeds on the same common set of time observations. Report the pairwise results and their mean (and dispersion where useful). Use at least three seeds including `42`, as required for the deep-learning experiments.
   - These silhouette scores are separate diagnostics because A and B use different observation units and representation spaces. They must not be put in one ranking or interpreted as a direct “A versus B” score.

   **Layer 2 — Economic interpretation under common macroeconomic references.**
   - **Option A:** interpret whether indicator groups have coherent economic meanings (for example, demand, prices, credit/liquidity, external sector, or production), and show how representative indicators or cluster-level summaries relate to contraction periods and GDP lead/lag behavior.
   - **Option B:** interpret whether hidden-state clusters correspond to distinguishable macroeconomic phases, and show how their time distribution aligns with contraction/non-contraction labels and other common macroeconomic reference series. Preserve the exact time and label alignment.
   - Compare the clarity and economic usefulness of the two interpretations in the same reference periods. Do not claim that an indicator cluster and a hidden-state cluster represent the same entity.

   **Layer 3 — Practical value for economic analysis and contraction monitoring.**
   - Assess whether Option A provides useful structure for understanding relationships among macroeconomic indicators and their co-movement.
   - Assess whether Option B helps identify changes in economic states and provides useful information for contraction recognition/forecasting.
   - Make a task-oriented qualitative assessment of which approach contributes more useful evidence for macroeconomic analysis and contraction monitoring, supported by its own quantitative diagnostics and out-of-sample predictive results where relevant. Do not create a single weighted score that combines unlike clustering metrics.

   **Required comparison output:** `results/comparison.html` must include the three layers above, separate A/B quality and stability statistics, shared-period economic interpretation evidence, and a concise practical-value synthesis. `code/compare.py` may calculate valid within-method or cluster-versus-regime ARI/NMI on identical observations, but must not calculate direct ARI/NMI between A's indicator partitions and B's time-point partitions.
## 10. Change control

This document is the shared implementation contract for both members. Any later change to the canonical panel schema, GDP target definition, PMI label, publication-lag handling, fixed forecast-origin/fold schedule, prediction fields, metrics schema, or three-layer clustering-comparison method must be proposed, reviewed by both members, and reflected here with the relevant implementation. Do not silently change a shared interface in an individual Option A or Option B branch. `assignment.md` remains authoritative for course requirements. All current shared choices, including the A/B clustering-comparison framework, are fixed in this document.

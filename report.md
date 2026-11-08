# Question 3 — China Macro Panel: Regimes, GDP Nowcasting, and Indicator Clustering

**Unified project report (Options A + B).** This report covers both realized options for
Question 3 on a frozen AkShare-based monthly macroeconomic panel: Option A (classical
PCA bridge nowcasting, Probit regime classification, rolling-Ward indicator clustering)
and Option B (sequence learning: LSTM/GRU regime classification, latent hidden-state
clustering, mandatory leakage ablation). All numerical claims are taken from the
committed implementation outputs, the saved `metrics.json`/`predictions.csv` files, and
the corresponding run audits; each option's section records the source experiment
directories. The two member drafts that fed this document are preserved unchanged at
`results/approach1/option_a_report_draft_revised(1).md` and
`results/approach2/REPORT_DRAFT.md`.

GDP nowcasting is Option A's task; Option B performs no nowcasting. The two options
share data, targets, forecast schedules, and evaluation observations (Section 2–3) but
use different methodologies (Sections 4–5), compared at the end of the report (Section 6).

---

## 1. Data and leakage discipline (shared)

### 1.1 Frozen monthly panel

The analysis uses the frozen AkShare-based monthly macroeconomic panel
`data/clean/macro_panel.csv`: **227 monthly observations from January 2008 to November
2026 and 30 indicators**. The variables cover business surveys and sentiment (`pmi_man`,
`pmi_nonman`, `consumer_confidence`, `lpi`); prices (`cpi_yoy`, `ppi_yoy`, `cgpi_yoy`);
real activity and demand (`industrial_va_yoy`, `retail_yoy`, `fai_yoy`, `elec_cons_yoy`,
`freight_yoy`); money and credit (`m0_yoy`, `m1_yoy`, `m2_yoy`, `new_loans_yoy`,
`tsf_flow`, `base_money_yoy`); external trade and fiscal conditions (`trade_bal_bn_usd`,
`export_yoy`, `import_yoy`, `fx_reserves_yoy`, `fiscal_rev_yoy`); and interest rates and
spreads (`lpr_1y`, `lpr_5y`, `shibor_on`, `shibor_3m`, `shibor_1y`,
`shibor_spread_1y_on`, `lpr_spread_5y_1y`). Panel provenance — source, retrieval date,
schema, SHA-256, and per-indicator publication lags — is recorded in
`data/metadata.json`.

### 1.2 Information set and publication lags

The monthly reference period is not necessarily the publication date. For each
indicator, the publication lag `L_i` is taken from `data/metadata.json` rather than
imposing one common lag. At a month-end forecast origin `t`, the available information
set is

\[
\mathcal F_t=\{x_{i,s}:s+L_i\leq t\},
\]

i.e. any use of `x_{i,s}` with `s + L_i > t` would be leakage. In practice the
feature-building routines shift each series by its own lag, `shifted_i(s) = x_i(s − L_i)`,
so the value in feature-panel row `t` is the latest reference-month observation eligible
at the end of month `t`. Lags are series-specific: the manufacturing PMI has a
zero-month lag based on its verified month-end release, CPI has a one-month lag,
consumer confidence uses a conservative two-month lag, and GDP (quarterly) is applied on
the monthly grid with a one-month lag. Some other lag assumptions are estimated or
conservative rather than directly verified; these classifications are retained as
documented in the metadata. Leakages were audited by dedicated scripts in both options
(Sections 3.4 and 4).

### 1.3 Panel hygiene

The raw panel preserves missing observations as `NaN` and applies **no**
future-aware interpolation or backward filling. Outliers are flagged by a MAD rule,

\[
|x_{i,t}-\operatorname{median}(x_i)|>5\times1.4826\times\operatorname{MAD}(x_i),
\]

but flagged values are **reported, not deleted or winsorized**: the frozen
`data/metadata.json` records 54 indicator-month flags (`outliers_flagged`), and the
generated `data/clean/missing_report.md` displays the 25 largest values, with the full
count in the metadata. For indicators whose January–February statistics are published
only as a combined amount, standalone January and February values remain missing; some
series also have shorter histories or documented gaps (for example, `tsf_flow` ends in
April 2026 in the frozen snapshot). These limitations are preserved rather than silently
filled.

The November 2026 observations for SHIBOR and the derived SHIBOR spread are
partial-month averages based only on trading days from 1–5 November; the PMI for
November 2026 is unavailable. The November 2026 row remains in the frozen source file
for provenance but is **excluded from all model training, backtesting, and clustering**
in both options. The effective monthly modeling sample therefore ends in October 2026.

### 1.4 Model-specific preprocessing (train-window only)

Both options obey one shared preprocessing rule: statistics are derived from **training
windows only** and applied unchanged to validation/test rows.

- **Option A (Factor / Bridge / Probit).** Indicators are screened separately at each
  forecast origin: a series is retained when it has at least 24 non-missing observations
  in the training history, at least 70% coverage over the most recent 60 months, and
  non-zero variation. For retained indicators, means and sample standard deviations are
  estimated on the relevant training window,
  \(\widetilde{x}_{i,s}^{(t)} = (x_{i,s}-\widehat\mu_{i,t}^{train}) /
  \widehat\sigma_{i,t}^{train}\); missing entries are set to the training-window mean
  (zero in standardized space) before standardization. The fitted statistics and PCA
  transformation are applied unchanged to the forecast rows.
- **Option B (LSTM/GRU).** Per walk-forward fold, the fit window determines (i)
  feature drops (series with >20% missingness on the fit window or zero variance; 19 of
  30 input series retained, identical drop set across folds), (ii) imputation means, and
  (iii) z-score statistics; the same statistics are applied unchanged to the validation
  tail and test month.
- **Option A (clustering)** uses a separate, stricter window-level coverage rule
  (Section 4.3) and does not use the factor-model imputation rule.

---

## 2. Targets and shared schedules

### 2.1 Contraction label

The primary regime label uses the NBS manufacturing PMI (`pmi_man`):

\[
d_t=\mathbb{1}\{PMI_t<50\ \text{and}\ PMI_{t-1}<50\}.
\]

Requiring two consecutive observations below the threshold makes the label represent a
sustained contraction signal rather than a single-month dip; both options justify the
labeling identically and both run the required sensitivity analysis at threshold 49.5.
A prediction issued at month-end forecast origin `t` always targets the one-month-ahead
label `d_(t+1)`, and a historical label may enter training only once both PMI
observations used to construct it would have been published.

### 2.2 Shared regime-classification schedule (both options)

Both options use the identical fixed schedule (`interface.md` §5.1):

| Element | Value |
|---|---|
| Initial historical label window | 2008-02 … 2019-12 (evolving with the expanding window) |
| Out-of-sample target months | 2020-01 … 2026-10 (**82 folds**; fold id = target month) |
| Forecast origins | month-end 2019-12-31 … 2026-09-30 |
| Horizon | one month ahead (`d_(t+1)`) |
| Realized evaluation labels | 82 target months: 27 contraction, 55 non-contraction at threshold 50; 13 contraction, 69 non-contraction at threshold 49.5 |

Expanding-window walk-forward validation advances one month at a time, and the training
window grows as newly observable labels become available. Because Option A's primary
Probit includes the LPR 1Y control (history begins October 2013), its estimation sample
starts later than Option B's; the evaluation months, folds, and realized labels are
identical for headline comparisons (Section 6.2 explains the resulting baseline
difference).

### 2.3 GDP nowcasting target and schedule (Option A only)

The frozen quarterly GDP source contains 83 observations from 2006Q1 to 2026Q3. Its
`gdp_yoy` field is a single-quarter year-on-year rate for Q1 but a cumulative year-to-date
rate for Q2–Q4, and the cumulative nominal GDP field `gdp_abs_cum_bn` is measured in RMB
100 million (亿元), notwithstanding the legacy `bn` suffix. Cumulative Q2–Q4 growth rates
are therefore **not** differenced directly. Instead, with \(A_{y,q}\) the published
cumulative level and \(g_{y,q}\) the published cumulative year-on-year rate as a decimal,
each 2006 quarter's cumulative level is used as an anchor
(\(C_{2006,q}=A_{2006,q}\)), the cumulative volume is chained forward
(\(C_{y,q}=C_{y-1,q}(1+g_{y,q})\), \(y>2006\)), and quarterly volumes are recovered by
de-accumulation (\(V_{y,1}=C_{y,1}\), \(V_{y,q}=C_{y,q}-C_{y,q-1}\), \(q=2,3,4\)). The
derived target (percentage points) is

\[
y_{y,q}^{GDP}=100\left(\frac{V_{y,q}}{V_{y-1,q}}-1\right).
\]

The published Q1 rate is retained for 2006Q1; 2006Q2–Q4 remain unavailable because the
frozen source provides no prior-year quarterly volume denominators for them. The
reconstructed target covers 2007Q1–2026Q3 (79 derived observations) and is a **derived
chain-linked single-quarter GDP volume estimate**, not a separately published official
NBS single-quarter series. Its arithmetic checks are satisfactory and document internal
consistency only: all reconstructed quarterly volumes are positive; the maximum absolute
difference to published Q1 growth is \(1.24\times10^{-14}\) pp over 20 observations; and
the maximum error recovering published cumulative growth rates is \(1.07\times10^{-14}\)
pp over 59 checks.

The nowcasting schedule: initial training quarters 2008Q1–2017Q4 (40 quarters); out-of-sample
quarters 2018Q1–2026Q3 (35 quarters); M1/M2/M3 month-end vintages for each target quarter →
**105 forecast records**. The GDP publication lag (one month, conservatively applied at the
end of the following month on the monthly grid) gates which realized targets may enter
training, and a target quarter is never used to train its own forecasts.

---

## 3. Validation and evaluation methodology (shared)

### 3.1 Walk-forward design and metrics

All experiments use the frozen data snapshot, the global seed 42 (CLI `--seed 42`
default), and expanding-window walk-forward evaluation. Regime classification reports
**Brier score, AUC, and accuracy**, plus the **naive prior-frequency baseline** evaluated
on the same observations (and, in Option A, a persistence benchmark and balanced
accuracy/sensitivity/specificity). GDP nowcasting reports RMSE and MAE overall and by
M1/M2/M3 vintage against a random walk with training-window drift and a historical mean.
Any classification threshold is selected without test-set information.

The Brier score is an overall probabilistic prediction score — the mean squared error
between the forecast probability and the realized 0/1 label — penalizing squared
probability errors on every fold; it does not separately credit calibration or ranking.
AUC rewards pure ranking and is threshold-free; accuracy depends on the operating point.
These distinctions matter for interpreting the two options' results (Sections 4.2, 5.2, 6).

### 3.2 Baseline definitions

- **Prior-frequency baseline** (both options): the train-window mean of the target
  label, carried forward as a constant probability. The two options compute it over
  different expanding estimation samples (Option A's primary Probit starts in
  November 2013 because of the LPR control; Option B's sequence models start from the
  first fully usable label in 2008), so the reported baseline Brier values differ
  (0.2321 vs 0.2551 at threshold 50) even though the evaluation months are identical.
- **Persistence benchmark** (Option A): the latest known label `d_t` used as the
  forecast for `d_(t+1)`.
- **Random walk with training-window drift** and **historical mean** (Option A, GDP).
- **Naive prior-frequency baseline** (Option B, identical to the definition above).

### 3.3 Class-imbalance handling

Contraction months are the minority class. Option A reports balanced accuracy,
sensitivity, and specificity at the fixed 0.5 classification threshold. Option B selects
the decision threshold per seed and fold by maximizing balanced accuracy on the
24-month validation tail inside the training window (with an optional, capped BCE
`pos_weight` of 1.0); the resulting raw accuracies of the recurrent models sit below the
majority-class baseline by construction — a deliberate operating-point choice, not a
metric failure.

### 3.4 Leakage-related duties

1. **Availability audit (Option A).** The validation script (189/189 checks passed,
   Section 4.5) verifies that constructed feature rows match the indicator-specific
   panel shifts, that the GDP target never trains on its own forecast, and that the
   partial November 2026 month is excluded (204 leakage-audit rows).
2. **Mandatory leakage ablation (Option B).** Directly demonstrates the value of
   publication-lag discipline by intentionally violating it (Section 5.4); all leaky
   artifacts are labelled INVALID everywhere.
3. **Clustering comparability rule (both).** Option A clusters *indicators* and Option B
   clusters *time-point hidden states*; their raw silhouette values are method-specific
   diagnostics, are **not** ranked against each other, and no direct ARI/NMI is computed
   between the two cluster universes. The cross-method comparison follows the
   three-layer framework of `interface.md` Section 9 (Section 6.3).

---

## 4. Option A — Classical

### 4.1 PCA factors and Bridge-regression GDP nowcasting

**Factors.** At each forecast origin the retained standardized indicator matrix is
decomposed by PCA (full SVD, at most eight components; deterministic sign convention).
With \(Z_t\) the training-window standardized matrix, \(Z_t=U_t\Sigma_tV_t^{\top}\), the
first \(r\) loading vectors form the transformation, \(\widehat f_s^{(r)}=V_{t,r}^{\top}\widetilde x_s\).
The factor count is selected by inner expanding-window cross-validation on **nowcasting
error**: candidates \(r=1,\ldots,8\), inner targets estimated only from GDP observable at
the inner origin, squared errors pooled across the M1/M2/M3 vintages; the minimum pooled
inner-validation RMSE wins (ties → smaller `r`). In the 105 out-of-sample forecast
records, `r = 3` is selected 90 times and `r = 4` 15 times.

**Bridge regression.** Quarterly GDP is the sole nowcasting target; industrial
value-added growth remains a monthly predictor, not a second nowcasting target. For each
within-quarter vintage \(v\in\{M1,M2,M3\}\) a separate Bridge Regression is estimated by
OLS with intercept,

\[
y_q^{GDP}=\alpha_v+\beta_v^{\top}\widehat f_{o(q,v)}^{(r)}+\varepsilon_{q,v},
\]

where \(o(q,v)\) is the month-end origin of the first/second/third month of quarter
`q`. Historical training pairs use the matching within-quarter vintage, the GDP
publication lag gates label availability, the target quarter is excluded from its own
training sample, and the training window expands as realized targets become observable.

**Benchmarks.** Random walk with training-window drift,
\(\widehat y_{q|t}^{RW}=y_{q^-}^{GDP}+\widehat\mu_t\) with \(\widehat\mu_t\) estimated only from
available training targets, and a historical-mean forecast. Accuracy is evaluated with
RMSE and MAE, overall and by vintage.

**Results (Table 1, Figures 1–6).**

**Table 1. GDP nowcasting performance (percentage points)**

| Sample | Forecasts | RMSE | MAE | Bias | \(R^2\) |
|---|---:|---:|---:|---:|---:|
| M1 | 35 | 3.2142 | 1.3351 | 0.2717 | 0.0763 |
| M2 | 35 | 2.6593 | 1.2363 | 0.4691 | 0.3677 |
| M3 | 35 | 3.2642 | 1.4938 | −0.2980 | 0.0474 |
| **Overall PCA-Bridge** | **105** | **3.0582** | **1.3551** | **0.1476** | **0.1638** |
| Random walk with drift | 105 | 4.1549 | 2.0985 | 0.0429 | −0.5435 |
| Historical mean | 105 | 4.0569 | 2.9649 | −2.2898 | −0.4715 |

The overall PCA-Bridge RMSE is lower than the random-walk-with-drift RMSE (RMSE-based
skill score 0.4582, \(1-RMSE_{model}^2/RMSE_{RW}^2\)), and its MAE is lower than both
benchmarks. The M2 vintage has the lowest RMSE and MAE among the vintages, but accuracy
does not improve monotonically with information: M3 RMSE (3.2642) exceeds M1 RMSE
(3.2142), and only 48.6% of quarters show a lower absolute error at M3 than at M1. The
mean absolute M1→M3 revision is 0.8498 pp (0.7557 pp M1→M2, 0.9597 pp M2→M3): revisions
reflect material information-set changes, and a larger information set does not
guarantee a more accurate estimate in every quarter. The 2020Q1 episode illustrates
this: the derived realized target is −6.8% while the forecasts are 5.8294% (M1), 0.7889%
(M2), and 7.2030% (M3) — the M2 estimate moves downward but the M3 estimate rises again,
which is why the within-quarter revision path is reported rather than only the latest
forecast.

**Figure references:** [Figure 1 — Derived quarterly GDP target](results/approach1/gdp_nowcasting/figures/fig_gdp_target.html); [Figure 2 — Nowcasts versus realized GDP](results/approach1/gdp_nowcasting/figures/fig_nowcast_vs_realized.html); [Figure 3 — RMSE by vintage](results/approach1/gdp_nowcasting/figures/fig_rmse_by_vintage.html); [Figure 4 — Within-quarter revision path](results/approach1/gdp_nowcasting/figures/fig_revision_path.html); [Figure 5 — PCA scree plots](results/approach1/gdp_nowcasting/figures/fig_pca_scree.html); [Figure 6 — Factor loadings](results/approach1/gdp_nowcasting/figures/fig_factor_loadings.html).

### 4.2 Probit contraction classification

**Specification.** At month-end origin `t` the model forecasts the next month's label,

\[
\Pr(d_{t+1}=1\mid z_t)=\Phi(\beta_0+\beta^{\top}z_t),
\]

estimated by maximum likelihood on the log-likelihood
\(\ell(\beta_0,\beta)=\sum_t[d_{t+1}\log\Phi(\eta_t)+(1-d_{t+1})\log\{1-\Phi(\eta_t)\}]\), \(\eta_t=\beta_0+\beta^{\top}z_t\).
The primary predictor vector contains the PCA factors plus the standardized LPR 1Y
(\(z_t=(\widehat f_1,\ldots,\widehat f_r,\widetilde{LPR1Y}_t)^{\top}\)); the rate/spread block is excluded
from the factor universe to avoid double-counting rate information. Because LPR 1Y
starts in October 2013, the primary model's label-training sample begins in November
2013; a factors-only sensitivity uses the longer February 2008 label history (the two
specifications have different estimation samples, so the factors-only variant is not a
clean one-variable ablation). The factor count is the GDP-nowcast-selected rank at the
corresponding origin (rank 3 in 61 of the 82 primary folds, rank 4 in 21). Classification
threshold: fixed 0.5. Schedule: the shared 82-fold design of Section 2.2.

**Optimizer discipline.** Estimation attempts Newton, then BFGS, with a documented
ridge-Probit fallback; an independent audit inspected the optimizer's actual convergence
returns rather than the wrapper's success flag. **All 82 primary outer fits converged
with Newton (6–8 iterations), no non-zero warning flags, no captured warnings, and no
ridge fallback; the audit reproduced the saved probabilities with a maximum absolute
difference of zero.** Coefficient signs are stable across folds for the intercept and
the first factor, less so for higher-order factors and the LPR control. Seven
out-of-sample probabilities were below 0.01 and two above 0.99 — extreme probabilities
were not treated as evidence of non-convergence.

A separate strictly nested inner-CV Probit rank-selection experiment was audited and is
**not** used as a headline result: its audit covered 35,752 inner candidate fits,
identified 902 fits failing the strict convergence rule, and 246 of 4,469 inner-fold
groups were unusable when all eight candidate ranks were required to pass. The 82 outer
fits did converge, and an audit-only re-evaluation on the reduced common set did not
change any selected rank; nevertheless the saved experiment was marked invalid by its
audit and no corrected experiment was created. The primary GDP-nowcast-selected Probit
is therefore the reported specification, and the nested-CV exercise is an inconclusive
diagnostic.

**Results at threshold 50 (Table 2, Table 3, Figures 7–11).**

**Table 2. Primary Probit performance on the common 82 target months**

| Model | Brier | AUC | Accuracy | Balanced accuracy |
|---|---:|---:|---:|---:|
| Probit: PCA factors + LPR 1Y | 0.2517 | 0.5953 | 0.5976 | 0.5492 |
| Prior-frequency baseline | 0.2321 | 0.5997 | 0.6707 | — |
| Persistence baseline | 0.2439 | 0.7239 | 0.7561 | 0.7239 |

The primary Probit does **not** outperform either benchmark on Brier score, AUC, or
accuracy: its Brier skill score versus the prior-frequency baseline is −0.0848. At the
0.5 threshold it correctly identifies 11 of 27 contraction months (sensitivity 0.4074)
and 38 of 55 non-contraction months (specificity 0.6909). The persistence benchmark is
particularly strong on AUC, accuracy, and balanced accuracy. These results indicate that
the static factor-plus-rate specification provides limited one-month-ahead contraction
discrimination over this evaluation period — numerical convergence should not be
confused with predictive usefulness.

**Table 3. Final-fold Probit coefficient estimates** (target month October 2026, origin
30 September 2026; three factors; 155 training labels, 41 contraction labels)

| Term | Coefficient | Standard error | t-statistic |
|---|---:|---:|---:|
| Intercept | −1.2829 | 0.2819 | −4.5504 |
| Factor 1 | −0.4127 | 0.1422 | −2.9029 |
| Factor 2 | −0.1481 | 0.1015 | −1.4599 |
| Factor 3 | −0.0238 | 0.1128 | −0.2111 |
| Standardized LPR 1Y | −0.2711 | 0.1893 | −1.4321 |

The coefficient table is fold-specific and must not be read as a time-invariant
structural relationship.

**Threshold-49.5 sensitivity.** The 49.5 labels contain 13 contraction / 69
non-contraction target months on the same 82 months. The Probit (LPR included) achieves
Brier 0.1446, AUC 0.6912, accuracy 0.7927, balanced accuracy 0.4710 — but predicts
**no** contraction at the 0.5 cutoff (sensitivity 0, specificity 0.9420), so its higher
raw accuracy must be read against the more imbalanced target. Baselines: prior-frequency
Brier 0.1428 / accuracy 0.8415; persistence Brier 0.1951 / accuracy 0.8049. The factors-only
variant at threshold 50 (longer training history) produces Brier 0.2220 and AUC 0.6357.
The 49.5 results are a separate label-definition sensitivity and are not pooled with the
primary results.

**Figure references:** [Figure 7 — Probit forecast probabilities](results/approach1/regime/figures/fig_regime_probabilities.html); [Figure 8 — ROC curve](results/approach1/regime/figures/fig_regime_roc.html); [Figure 9 — Calibration plot](results/approach1/regime/figures/fig_regime_calibration.html); [Figure 10 — Fold-level Brier scores](results/approach1/regime/figures/fig_regime_brier.html); [Figure 11 — Final-fold coefficient estimates](results/approach1/regime/figures/fig_regime_coefficients.html).

### 4.3 Rolling-window indicator clustering

**Construction.** Ward linkage on the prescribed correlation distance
\(d_{ij}=\sqrt{2(1-\rho_{ij})}\), computed on rolling 36-month windows advanced in
12-month steps: **17 valid windows** (December 2010 through December 2025, plus the
final available month October 2026). Each window masks observations by their
indicator-specific publication lags; an indicator is retained with ≥95% within-window
coverage and non-zero variance; derived spreads are removed when both components are
retained; a deterministic rank screen removes exact linear dependencies; Pearson
correlations are computed on the common complete-case monthly sample (≥24 common
observations required).

The coverage screen systematically excludes `consumer_confidence` from **all 17**
windows: with its conservative two-month publication lag, its eligible coverage is
34/36 months (94.44%) at every endpoint, below the 95% threshold (≥35 of 36). The
`window_retention.csv` audit records `retained = False, drop_reason = coverage`
throughout. This is a mechanical consequence of the lag/coverage rules — it does not
remove the indicator for low variance or lack of economic relevance, and the clustering
findings should not be generalized to the full 30-indicator panel.

**Euclidean compatibility (verified every window).** Ward linkage requires
Euclidean-compatible distances. If \(u_i\) is the unit-normalized standardized vector
for indicator \(i\), then \(\lVert u_i-u_j\rVert_2=\sqrt{2(1-\rho_{ij})}\) when the sample
correlation matrix is positive semi-definite. All sample correlation matrices are
PSD under numerical tolerance, and the maximum absolute difference between the prescribed
distance and the normalized-vector Euclidean distance is \(2.83\times10^{-15}\). The
required Ward linkage is therefore applied to an empirically verified Euclidean
construction; it was never replaced by another method.

**Diagnostics.** Silhouette scores for \(k=2,\ldots,8\) (highest silhouette wins;
smaller `k` on ties) plus a fixed-\(k=4\) sensitivity; stability via adjusted Rand index
between adjacent windows on the indicators retained in both windows; and economic
interpretation via quarterly-mean lead–lag cross-correlations with the derived GDP
target at lags −4…+4 quarters (≥12 valid pairs; descriptive, not causal).

**Results (Figures 12–18).** Retained indicators range from 15 to 21 per window (mean
17.82); the common complete-case sample averages 35 months. The selected silhouette
averages 0.2405 (range 0.1618–0.3409); 0.1951 on average under the fixed-\(k=4\)
sensitivity. The average adjacent-window ARI is 0.2383 (minimum −0.0520, maximum
1.0000 over 16 comparisons). In the final October 2026 window the selected solution has
two clusters — one of ten indicators (CPI, PPI, CGPI, M2, M1, base money, trade balance,
exports, imports, FX reserves) and one of nine (manufacturing and non-manufacturing PMI,
M0, new loans, LPR 1Y, LPR 5Y, SHIBOR overnight/3M/1Y) — suggesting co-movement across
price, external-sector, money, business-survey, credit, and interest-rate series without
mapping perfectly onto conventional macroeconomic categories. Cluster-level lead–lag
summaries reach maximum absolute mean correlation at lag −1 quarter for the first
cluster (0.3100) and lag 0 for the second (0.3036) (convention: indicator at quarter
`q` correlated with GDP at quarter `q+ℓ`). Standardized cluster profiles in the final
window are −0.1163 in contraction vs 0.1098 in non-contraction months for the ten-member
cluster (difference −0.2261), and 0.0364 vs −0.0318 for the nine-member cluster
(difference 0.0682). Modest mean silhouette and adjacent-window ARI, time-varying cluster
counts, and the coverage-screened indicator universe mean these are useful windows-specific
co-movement structures rather than a fixed economic taxonomy.

**Figure references:** [Figure 12 — Rolling-window dendrograms](results/approach1/clustering/figures/fig_dendrograms.html); [Figure 13 — Silhouette by window](results/approach1/clustering/figures/fig_silhouette.html); [Figure 14 — Silhouette across candidate cluster counts](results/approach1/clustering/figures/fig_silhouette_k_curve.html); [Figure 15 — Adjacent-window ARI](results/approach1/clustering/figures/fig_adjacent_ari.html); [Figure 16 — Cluster membership over time](results/approach1/clustering/figures/fig_cluster_membership.html); [Figure 17 — GDP lead–lag correlations](results/approach1/clustering/figures/fig_lead_lag_gdp.html); [Figure 18 — Cluster profiles by PMI regime](results/approach1/clustering/figures/fig_cluster_regime_profile.html).

### 4.4 GDP nowcasting remains Option A's task

Industrial value-added growth is never treated as a second nowcasting target, and no
GDP claims are made for Option B.

### 4.5 Option A validation and audits

The independent validation script reports **189 of 189 checks passed**, 4,786
preprocessing values checked with no failures, 204 leakage-audit rows, and 18 generated
figures. Availability checks confirm feature rows match the indicator-lag shifts, the
GDP target never trains its own forecasts, and the partial November 2026 month is
excluded. The primary Probit optimizer audit is described in Section 4.2; the nested-CV
audit is documented as an inconclusive diagnostic (experiment marked invalid; no
corrected rerun).

---

## 5. Option B — Sequence learning (PyTorch)

### 5.1 Methodology

**Task.** One-month-ahead classification of the PMI contraction regime: at month-end
origin `t`, an LSTM or GRU produces \(P(d_{t+1}=1\mid \widetilde x_{t-h+1\ldots t})\),
the probability that the label defined in Section 2.1 is active in month `t+1`. Because
`pmi_man` has `L = 0`, the model legitimately observes `PMI_t` at the origin — one of the
two conditions of `d_(t+1)`. This is **not** future-information leakage (`PMI_t ∈ F_t`) but
it makes the task the *conditional* prediction of whether PMI stays below the threshold;
headline gains should not be read as predicting turning points "from scratch".

**Model and training discipline.** Two-layer stacked recurrent networks (hidden size 32,
dropout 0.2), two architectures (LSTM, GRU), trained with binary cross-entropy: lr
0.001, weight decay 0.0001, batch size 32, max 200 epochs with early stopping (patience
20) on the 24-month validation tail; decision threshold per seed and fold selected on
that tail by balanced accuracy (with an optional BCE `pos_weight` capped at 1.0); seeds
42 / 7 / 123 (the global default 42 included in every experiment); residual CPU
last-bit floating-point variation documented per script. Lookback `h` is selected once
per label threshold by mean Brier on inner validation targets 2018-01…2019-12 with the
fit strictly before 2018-01 (no test information): h = 3 at threshold 50 (score 0.1759 vs
0.1953 for h = 6 and 0.2004 for h = 12); h = 12 at threshold 49.5 (0.0400 vs 0.0406 and
0.0416) — a data-driven selection outcome only, with no economic inference drawn from the
longer window.

**Classical comparator.** The ablation script provides the classical arm: a
**raw-feature, origin-row ridge Probit** (statsmodels binomial GLM with probit link) on
the standardized final-window row `z_u = F_u`, with ridge α selected per fold on the
validation tail from {unpenalized, 0.1, 1, 10} (α = 10 in 58/82 folds, α = 1 in 22,
α = 0.1 in 2; deterministic, seed `null`). A pooled-row design mixing window lead times
was tested and rejected in development (AUC 0.39, unstable perfect-separation fits).
The frozen interface requires aligned targets, origins, folds, labels, and evaluation
observations but not identical internal specifications, so this probit is a valid
comparator on the identical folds and coexists with Option A's independently specified
factor-probit (Section 4.2) as a separately reported classical model.

### 5.2 Regime classification results

**Threshold 50, h = 3 (Table 4).** 82 folds; identical evaluation months across all
rows; LSTM/GRU entries are mean ± standard deviation across seeds, with per-seed values.
Recurrent training is deterministic up to documented last-bit CPU variation (fixed seeds
control initialization and data order).

**Table 4. Regime classification at threshold 50 (82 target months, h = 3)**

| Model | Brier ↓ | AUC ↑ | Accuracy | Per-seed Brier (42 / 7 / 123) |
|---|---|---|---|---|
| Naive prior-frequency baseline (Option B sample) | 0.2551 | — | 0.6707 | deterministic |
| Probit (ridge, origin-row, raw features) | 0.2330 | **0.7788** | 0.6585 | deterministic |
| LSTM (safe) | **0.2207 ± 0.0035** | 0.6157 ± 0.0055 | 0.4268 ± 0.0244 | 0.2237 / 0.2168 / 0.2215 |
| GRU (safe) | 0.2386 ± 0.0088 | 0.5688 ± 0.0262 | 0.4390 ± 0.0762 | 0.2285 / 0.2447 / 0.2427 |

Per-seed AUC — LSTM 0.6195 / 0.6182 / 0.6094; GRU 0.5811 / 0.5865 / 0.5387. Both safe
recurrent models and the Probit record lower Brier scores than the prior baseline;
**no model is superior on all metrics**: the Probit ranks contractions best (AUC 0.7788,
the only headline model above the assignment's indicative 0.75 benchmark under lag
discipline) and nearly matches the majority-class accuracy, while the LSTM attains the
lowest overall Brier score — the smallest average squared probability error on this
sample, which is not the same as best calibration, best ranking, or best
classifications. Small seed dispersion (LSTM Brier std 0.0035) versus GRU accuracy
sensitivity (std 0.0762).

**Threshold 49.5, h = 12 (Table 5).**

**Table 5. Regime classification at threshold 49.5 (same 82 target months)**

| Model | Brier ↓ | AUC ↑ | Accuracy | Per-seed Brier (42 / 7 / 123) |
|---|---|---|---|---|
| Naive prior-frequency baseline | 0.1492 | — | 0.8415 | deterministic |
| LSTM (safe) | **0.1384 ± 0.0020** | 0.5949 ± 0.0046 | 0.4837 ± 0.0715 | 0.1373 / 0.1408 / 0.1372 |
| GRU (safe) | 0.1467 ± 0.0065 | 0.5522 ± 0.0325 | 0.5569 ± 0.0508 | 0.1395 / 0.1521 / 0.1487 |

(Per-seed AUC — LSTM 0.5987 / 0.5964 / 0.5897; GRU 0.5864 / 0.5217 / 0.5485.) Under the
stricter threshold contractions are rarer and **both** safe recurrent models show lower
Brier scores than the baseline (LSTM by 0.0108, GRU by 0.0025 — small differences
relative to the noise documented in Section 7), while ranking power is essentially
unchanged.

The modest AUC levels of the safe sequence models (0.55–0.62) indicate that
one-month-ahead binary contraction prediction from these monthly indicators is genuinely
hard; Section 5.4 shows that a large part of any apparent "improvement" under weaker
discipline comes precisely from lifting the information constraint.

**Figure references:** [Figure 19 — Walk-forward predicted probabilities, threshold 50](results/approach2/regime/figures/walkforward_probabilities.html); [Figure 20 — Walk-forward cumulative Brier, threshold 50](results/approach2/regime/figures/walkforward_brier.html); [Figure 21 — Walk-forward predicted probabilities, threshold 49.5](results/approach2/regime_threshold49_5/figures/walkforward_probabilities.html); [Figure 22 — Walk-forward cumulative Brier, threshold 49.5](results/approach2/regime_threshold49_5/figures/walkforward_brier.html). Row-level predictions obey the shared `interface.md` §6.2 schema (`results/approach2/regime/predictions.csv`, 492 rows = 82 folds × 2 architectures × 3 seeds).

### 5.3 Hidden-state clustering

**Design.** The trained recurrent models are reused (same lag-shifted panel,
fit-window-only preprocessing, h = 3) to extract the final-layer hidden state `h_t`
(dimension 32) at every month-end in 2008-03…2026-10 → **224 time observations**. The
descriptive training origin is the last full panel month-end, **2026-10-31**, so training
uses only label pairs observable then (training targets up to `d_2026-10`); the partial
2026-11 row is excluded; seeds 42/7/123 each produce their own embedding space over the
same 224 months. K-means (`n_init = 10`, `random_state = seed`) is applied within each
seed's own embedding space for k ∈ {2,…,6}; k is selected per architecture by the maximum
mean silhouette across seeds.

**k selection (Table 6).** Mean-silhouette profiles are strictly decreasing in k for the
LSTM and nearly so for the GRU.

**Table 6. Mean silhouette by candidate k (each seed in its own embedding space)**

| Architecture | k = 2 | k = 3 | k = 4 | k = 5 | k = 6 | Selected |
|---|---|---|---|---|---|---|
| LSTM | **0.6410** | 0.5609 | 0.5106 | 0.4786 | 0.4330 | k = 2 |
| GRU | **0.4662** | 0.3720 | 0.3757 | 0.3509 | 0.3372 | k = 2 |

**Method-specific quality and stability (Table 7).** These silhouette values are
diagnostics of Option B's learned embedding spaces (time points, 32-dim hidden states)
and are deliberately **not** ranked against Option A's indicator-space silhouettes
(Section 6.3).

**Table 7. Layer-1 diagnostics: quality and cross-seed stability**

| Statistic | LSTM | GRU |
|---|---|---|
| Per-seed silhouette (42 / 7 / 123) | 0.6862 / 0.6759 / 0.5609 | 0.3164 / 0.6435 / 0.4385 |
| Silhouette mean ± std | 0.6410 ± 0.0696 | 0.4662 ± 0.1653 |
| Pairwise cross-seed ARI | 0.8124 (42↔7); 0.0541 (42↔123); 0.0417 (7↔123) | 0.1952 (42↔7); 0.6284 (42↔123); 0.0711 (7↔123) |
| Mean pairwise ARI (same common 224 time observations) | 0.3027 | 0.2982 |

All pairwise cross-seed ARIs use the identical common observation set (every seed embeds
the same 224 months; per-pair n_common = 224 recorded in
`results/approach2/clustering/metrics.json`). The cross-seed ARI values are highly
dispersed: exactly one seed pair per architecture reproduces a near-identical partition
(LSTM 42↔7 at 0.812; GRU 42↔123 at 0.628) while the others disagree sharply (ARI ≤
0.20). The two latent clusters are therefore **exploratory and initialization-sensitive,
not robust stable economic regimes** — a limitation of latent-state k-means that does
not affect the validity of the regime-classification results.

**Alignment with PMI labels (Table 8).** Two explicit alignments are computed and stored
separately (no implicit label shifting): `d_t` = contemporaneous label of the window-end
month; `d_(t+1)` = the one-month-ahead forecast target the model was trained on. ARI/NMI
are computed per seed and averaged.

**Table 8. Latent clusters versus PMI contraction labels**

| Architecture | Alignment | n | ARI | NMI |
|---|---|---:|---:|---:|
| LSTM | `d_t` (contemporaneous window-end label) | 224 | 0.1312 | 0.2237 |
| LSTM | `d_(t+1)` (one-month-ahead forecast target) | 223 | 0.1178 | 0.1743 |
| GRU | `d_t` | 224 | 0.0896 | 0.1939 |
| GRU | `d_(t+1)` | 223 | 0.0795 | 0.1416 |

The association between latent states and contraction labels is real but moderate
(NMI ≈ 0.14–0.22). For reference seed 42 the LSTM's k = 2 split is economically
interpretable as a contraction/non-contraction separation: one state (n = 108 months)
contains 42.6% contraction (`d_t = 1`) months, the other (n = 116) 0.9%; the GRU split
is directionally similar but noisier (state n = 88 with 2.3%; state n = 136 with 33.1%).
Given the cross-seed dispersion above, this is an **illustrative interpretation of one
initialization** — the latent geometry carries contraction-relevant information without
recovering the labels.

**Figure references:** [Figure 23 — LSTM latent states, PCA view](results/approach2/clustering/figures/latent_lstm_pca.html); [Figure 24 — LSTM latent timeline](results/approach2/clustering/figures/latent_lstm_timeline.html); [Figure 25 — GRU latent states, PCA view](results/approach2/clustering/figures/latent_gru_pca.html); [Figure 26 — GRU latent timeline](results/approach2/clustering/figures/latent_gru_timeline.html). Row-level assignments and embeddings: `results/approach2/clustering/latent_clusters.csv` (cluster id plus both label alignments per seed × month) and `latent_embeddings.csv` (32-dim embeddings per seed).

### 5.4 Mandatory leakage ablation

**Design.** Three arms under the same 82 folds, labels, seeds, architectures, training
pipeline, lookback h = 3, preprocessing rules, and validation tails:

1. **Probit (safe)** — the classical comparator of Section 5.1;
2. **LSTM/GRU (safe)** — the headline run's per-seed per-fold predictions reused
   verbatim from `results/approach2/regime/predictions.csv`, so the safe arms are
   bit-identical to the headline run;
3. **LSTM/GRU without lag discipline** — identical architecture and training, but each
   indicator window is taken from the **raw unshifted panel**, as if every series were
   published at its reference month-end. For the 21 of 30 series with `L_i ≥ 1` this
   feeds the origin row observations a real-time forecaster could not have had
   (`s = t` with `s + L_i > t`), directly violating the shared information-set rule.

The leaky window does **not** contain the label's second PMI observation (`PMI_(t+1)`,
not in any feature window since PMI has `L = 0`), so the demonstration isolates
**availability leakage of unpublished contemporaneous macro data** — the exact red-line
violation — rather than direct label peeking. Every leaky artifact is labelled
everywhere: filenames and figure titles carry the INVALID wording, the metrics field is
`"leakage_discipline": "intentional-leakage-demo"` (versus `"safe"`), and the row-level
file `results/approach2/leakage_ablation/predictions.csv` (1,066 rows: 82 probit + 492
safe-RNN + 492 leaky-RNN) repeats the label column per row. Factual notes for the
group's use are in `results/approach2/leakage_ablation/NOTES.md`.

**Results (Tables 9–10).**

**Table 9. Leakage ablation (82 folds; mean ± std across seeds where applicable)**

| Arm | `leakage_discipline` | Brier ↓ | AUC ↑ | Accuracy | Per-seed Brier (42 / 7 / 123) |
|---|---|---|---|---|---|
| Naive prior-frequency baseline | safe | 0.2551 | — | 0.6707 | deterministic |
| Probit (ridge, origin-row) | safe | 0.2330 | **0.7788** | 0.6585 | deterministic |
| LSTM (safe) | safe | 0.2207 ± 0.0035 | 0.6157 ± 0.0055 | 0.4268 ± 0.0244 | 0.2237 / 0.2168 / 0.2215 |
| GRU (safe) | safe | 0.2386 ± 0.0088 | 0.5688 ± 0.0262 | 0.4390 ± 0.0762 | 0.2285 / 0.2447 / 0.2427 |
| LSTM **without lag discipline** | **intentional-leakage-demo — INVALID, not a valid model** | 0.1978 ± 0.0011 | 0.7100 ± 0.0124 | 0.5325 ± 0.0254 | 0.1978 / 0.1989 / 0.1966 |
| GRU **without lag discipline** | **intentional-leakage-demo — INVALID, not a valid model** | 0.2109 ± 0.0060 | 0.6510 ± 0.0067 | 0.5325 ± 0.0373 | 0.2055 / 0.2173 / 0.2098 |

**Table 10. Recorded leakage premium (leaky minus safe on identical folds)**

| Architecture | Brier gap (safe − leaky) | AUC gain (leaky − safe) |
|---|---|---|
| LSTM | 0.0229 (0.2207 → 0.1978) | +0.0943 (0.6157 → 0.7100) |
| GRU | 0.0278 (0.2386 → 0.2109) | +0.0822 (0.5688 → 0.6510) |

**What the demonstration shows.** (1) Ignoring publication lags inflates apparent
performance for every metric — the leaky variants beat their own safe versions on Brier,
AUC, *and* accuracy, with a uniform premium of roughly +0.02–0.03 Brier and +0.08–0.09
AUC; all of it was unavailable information at decision time. (2) The premium is a
*within-architecture* comparison and not the whole story: the **safe** probit, using only
genuinely available information, reaches AUC 0.7788 — higher than both leaky RNNs — so
lag discipline is not merely a handicap; the leaky arms quantify the value of the
forbidden data to a given architecture. (3) The leaky arms are **INVALID as predictive
models** and are reported only as an intentional demonstration of what lag discipline
protects against.

**Figure references:** [Figure 27 — Walk-forward probability paths with INVALID leaky traces](results/approach2/leakage_ablation/figures/leakage_demo_probabilities.html); [Figure 28 — Walk-forward Brier comparison](results/approach2/leakage_ablation/figures/ablation_brier.html); [Figure 29 — Ablation summary (safe green, leaky red)](results/approach2/leakage_ablation/figures/ablation_summary.html).

---

## 6. Cross-option comparison and synthesis

### 6.1 What is and is not comparable

The two cluster analyses have different observation universes (indicators vs time-point
hidden states), so their raw silhouette scores are method-specific and are never ranked
against each other, and no direct ARI/NMI exists between the two partitions. Both
options' regime classifiers, however, are evaluated on the identical 82 target months,
folds, origins, and realized labels, so their summary statistics belong in one view.
GDP nowcasting belongs to Option A alone. The consolidated cross-option comparison is
also generated by the shared script as `results/comparison.html` (task-specific
sections, no single ranked leaderboard).

### 6.2 Regime classification: three classical/learning variants on the same folds

**Table 11. All regime classifiers on the identical 82 target months (threshold 50)**

| Model | Option | Information set | Brier ↓ | AUC ↑ | Accuracy |
|---|---|---|---:|---:|---:|
| Persistence benchmark (latest label as forecast) | A | `F_t` (label only) | 0.2439 | 0.7239 | 0.7561 |
| Prior-frequency baseline (Nov-2013 estimation sample) | A | `F_t` | 0.2321 | 0.5997 | 0.6707 |
| Probit: PCA factors + LPR 1Y | A | `F_t` | 0.2517 | 0.5953 | 0.5976 |
| Prior-frequency baseline (2008-onward sample) | B | `F_t` | 0.2551 | — | 0.6707 |
| Probit: raw features, origin-row ridge | B | `F_t` | 0.2330 | 0.7788 | 0.6585 |
| LSTM (safe, mean of 3 seeds) | B | `F_t` | 0.2207 | 0.6157 | 0.4268 |
| GRU (safe, mean of 3 seeds) | B | `F_t` | 0.2386 | 0.5688 | 0.4390 |
| LSTM without lag discipline — **INVALID demo** | B | violates `F_t` | (0.1978) | (0.7100) | (0.5325) |
| GRU without lag discipline — **INVALID demo** | B | violates `F_t` | (0.2109) | (0.6510) | (0.5325) |

Values in parentheses are the intentionally leaky demonstration only and are **not valid
predictive results**.

Reading of the table, strictly as supported by the evidence:

- **The two probit arms differ by specification, not only by option.** Option A's model
  uses PCA factors plus LPR 1Y and therefore trains on the shorter LPR-era sample
  (November 2013 onward); Option B's model uses the raw standardized panel rows from the
  full 2008-onward sample with per-fold ridge selection. The shared interface mandates
  equal targets/origins/folds/labels/evaluation observations, not identical internal
  specifications, so both are reported as separately specified, validly disciplined
  models rather than being silently reconciled.
- **The magnitude of the baseline difference is likewise documented:** the two
  prior-frequency values (0.2321 vs 0.2551) reflect the different train-window samples
  over which the prior is estimated (both use only information available at each origin).
- **Ranking power:** the strongest ranking on this sample is the Option B raw-feature
  probit (AUC 0.7788); the Option A persistence benchmark is next (0.7239); Option A's
  factor probit produces 0.5953, below its own prior-frequency baseline's 0.5997.
- **Probability-error metric:** the best Brier scores among valid models are the Option A
  prior-frequency baseline (0.2321) and Option B's raw-feature probit (0.2330) and LSTM
  (0.2207); no valid model is superior on all three metrics, and differences should be
  read against the small 82-observation sample (Section 7).
- **The persistence benchmark's strong AUC/accuracy** underlines that label persistence
  dominates at the one-month horizon — useful context for both options' model results.

No claim of overall superiority of any single valid model is made; the metrics disagree
by construction (Section 3.1), and the assignment's indicative 0.75 AUC benchmark is met
on this evaluation only by the Option B raw-feature probit under lag discipline. Both
options' honest headline result is that one-month-ahead contraction discrimination from
monthly information with strict lag discipline is difficult, and that relaxing the
information rule manufactures apparent skill (Section 5.4).

### 6.3 Clustering: the three-layer comparison (no cross-metric ranking)

**Layer 1 — method-specific quality and stability (reported separately).** Option A
(indicators, correlation distance, Ward): mean selected silhouette 0.2405, mean
adjacent-window ARI 0.2383 across 17 windows, with Euclidean-compatibility verified in
every window. Option B (time points, 32-dim hidden states, k-means): LSTM silhouette
mean 0.6410 ± 0.0696 and GRU 0.4662 ± 0.1653, with pairwise cross-seed ARI means near
0.30 for both architectures on the identical 224-month common set. These numbers are
computed in different spaces, on different observations, with different distance notions;
nothing about the relative "clustering quality" of the two options may be inferred
comparing them.

**Layer 2 — shared economic interpretation.** Option A's clusters are groups of
indicators with plausible co-movement meaning (price/external/money in one group;
surveys/credit/rates in another in the final window) whose lead–lag summaries associate
with GDP at −1 and 0 quarters, but memberships drift across windows and the mean
silhouette is modest. Option B's latent states relate to contraction labels at a
moderate level (NMI ≈ 0.14–0.22 against `d_t`), with one interpretable two-state split
under a specific seed, but cross-seed stability is low (initialization-sensitive). Both
approaches therefore yield **suggestive but unstable structure** rather than a definitive
taxonomy of the Chinese macroeconomy.

**Layer 3 — task-oriented practical value.** For understanding *which indicators
co-move* and how those groups relate to GDP's lead–lag structure, Option A's
indicator clustering is the direct instrument (dendrograms, membership timelines,
lead–lag table). For *state recognition in time* — when the macro system shifts between
phases — Option B's hidden-state timeline provides the applicable diagnostics, and its
regime classifiers supply honest out-of-sample probabilities with the mandatory leakage
demonstration quantifying the discipline's value. A single numeric score combining the
two methods' like metrics is deliberately not constructed; the two contribute
complementary evidence to contraction monitoring, each with documented instability.

### 6.4 Leakage discipline as a unifying finding

Both options independently enforce `F_t = {x_(i,s): s+L_i ≤ t}` and both document that
respecting it is binding: Option A's availability audit verifies every feature row and
excludes unusable months (Section 4.5); Option B's ablation shows that violating the
rule inflates apparent skill by ≈ +0.09 AUC and ≈ 0.02–0.03 Brier *within the same
architecture* (Section 5.4) — even though the disciplined Option B probit outperforms
the leaky RNN demonstrations on ranking. The comparison section's central message is
therefore that **publication-lag discipline is not cosmetic**: it changes which models
appear superior.

---

## 7. Discussion and limitations

### 7.1 Summary of what each approach delivers

- **Option A** provides the pipeline's nowcasting layer: a derived, internally
  consistent chain-linked quarterly GDP target, PCA factors selected by cross-validated
  nowcasting error, and a Bridge Regression whose pooled RMSE/MAE improve on the
  random-walk-with-drift benchmark (skill score 0.4582) — with the important caveat that
  the improvement is not monotonic across vintages (M2 best; M3 not uniformly better
  than M1). Its Probit is numerically impeccable per audit but does not beat either
  benchmark, and its indicator clustering satisfies every mandated construction rule
  while producing interpretable, though time-varying, indicator groups.
- **Option B** provides the learning-based regime layer: three-seed LSTM/GRU
  classification with honest expanding-window evaluation, latent-state clustering as an
  exploratory diagnostic, and the mandatory leakage ablation quantifying the premium of
  violating the shared information set directly.

### 7.2 Limitations shared by both options

1. **One frozen snapshot, not a real-time archive.** Indicator-specific publication lags
   are imposed, but historical revisions of the source data cannot be captured; some lag
   assumptions are estimated or conservative.
2. **Small regime evaluation sample.** 82 target months (27 contractions at threshold
   50; 13 at 49.5) make single-fold contributions to Brier/AUC large and adjacent-model
   differences often comparable to seed/fold noise; no confidence-style superiority
   claims are attempted by either option.
3. **Partial-month exclusion.** November 2026 (PMI unavailable, partial SHIBOR rows) is
   excluded throughout; the effective panel ends October 2026.
4. **Class imbalance.** Contraction months are a minority; Option A reports
   sensitivity/specificity/balanced accuracy at a fixed 0.5 threshold, and Option B
   selects thresholds on small validation tails — both operating points are therefore
   sensitive to tail composition.

### 7.3 Option A–specific limitations

1. **The derived GDP target is an internal reconstruction**, not an official separately
   published single-quarter real-GDP series; arithmetic checks verify only internal
   consistency.
2. **The primary Probit's LPR-controlled training sample is short** (from November 2013)
   with few positive labels per window; the factors-only variant uses a different,
   longer sample and is not a clean ablation.
3. **Clustering coverage:** `consumer_confidence` is excluded from all 17 windows by the
   95% coverage rule under its two-month lag (34/36 eligible months); clustering results
   must not be generalized to the full 30-indicator panel. Walk sensitivity: 36-month
   windows and the retained-variable set affect correlations and ARI.
4. **The strictly nested Probit experiment is an invalid, inconclusive diagnostic**
   (optimizer-audit failures on 902 of 35,752 inner fits; 246/4,469 inner-fold groups
   unusable; selected ranks unchanged on the reduced set; no corrected rerun run). The
   primary GDP-nowcast-selected Probit remains the reported specification.
5. **Nested CV rank selection for the Probit** comes from GDP's pooled-vintage
   criterion rather than a classification-tuned procedure; the rank distribution (3 in
   61, 4 in 21 folds) is inherited as-is.

### 7.4 Option B–specific limitations

1. **No all-metric superiority.** Brier favors the LSTM, AUC favors the probit, and
   accuracy ordering is threshold-dependent; no model dominates on all metrics, and the
   modest safe-sequence AUCs (0.57–0.62 at threshold 50) sit below the assignment's
   indicative 0.75 benchmark under lag discipline.
2. **PMI feature/target overlap.** Because `pmi_man` (lag 0) is an input feature, the
   model observes one of the two label conditions at the origin; the task is the
   conditional prediction of PMI staying below threshold, and headline scores must not
   be read as unconditional turning-point forecasts.
3. **Threshold-selection uncertainty.** Per-seed decision thresholds come from 24-month
   validation tails with few positives in some tails; this contributes to safe-RNN
   accuracy below the majority baseline. Thresholds were not — and will not be — retuned
   on test results.
4. **Clustering initialization sensitivity.** Cross-seed ARI is highly dispersed
   (single pairs 0.042–0.812; means ≈ 0.30): the two latent clusters are exploratory,
   initialization-sensitive diagnostics, not robust stable regimes; the interpretable
   seed-42 split (42.6% vs 0.9% contraction shares) is illustrative of one
   initialization.
5. **Probit specification plurality.** Option B's raw-feature ridge probit and Option A's
   factor probit are separate, documented specifications (Section 6.2); the pooled-row
   probit design was tested and rejected in Option B's development (AUC 0.39,
   perfect-separation instability).
6. **Residual nondeterminism.** CPU training of the recurrent models varies in the last
   floating-point bits despite fixed seeds; documented per script
   (`params.residual_nondeterminism`).

### 7.5 Points flagged for the group (unresolved)

- **Contribution statement — resolved.** The default statement from the two
  integration drafts has been replaced by the group-approved division of
  responsibilities and description of AI-assisted implementation in Section 8.2.
- `[TO VERIFY]` — **Final figure/table captions and ordering.** The figure numbering
  (1–29) and table numbering (1–11) here are editorial continuity choices; the group
  should confirm they match the final curated figure set and, if any figure is dropped
  or merged, renumber consistently.

---

## 8. Reproducibility, contribution statement, and output index

### 8.1 Environment and run commands

Python 3.13 under `uv`; dependencies pinned in `pyproject.toml` / `uv.lock`; global seed
42 via `--seed 42` (argparse default); every experiment is a self-contained
argparse-driven script writing `results/approachN/<task>/metrics.json` + self-contained
Plotly HTML figures (`include_plotlyjs=True`); terminal output via `rich`. The data
snapshot is frozen; do not run live fetches or rebuild the panel without both group
members' explicit approval (`uv run python data/fetch.py --offline` only for routine
snapshot verification).

Full command listings per script are maintained in `AGENTS.md` §2, including Option B:

```bash
uv run python code/approach2_regime.py --seed 42
uv run python code/approach2_regime.py --seed 42 --label-threshold 49.5
uv run python code/approach2_regime.py --seed 42   # prerequisite (provides lookback h)
uv run python code/approach2_clustering.py --seed 42
uv run python code/approach2_regime.py --seed 42   # prerequisite (provides the safe LSTM/GRU arm)
uv run python code/approach2_leakage_ablation.py --seed 42
```

and Option A's one-command entry point:

```bash
uv run python code/run_option_a.py --seed 42
```

### 8.2 Statement of Work and Division of Responsibilities

We conducted this project collaboratively, with individual responsibilities divided
as follows.

**1. Qiying Zheng**

- **Data preparation and infrastructure:** Responsible for establishing the data
  interfaces and infrastructure, collecting, cleaning, and processing data,
  conducting data quality checks, and constructing the shared dataset used by both
  options.
- **Option B:** Responsible for the implementation and empirical analysis of the
  leakage-safe LSTM/GRU regime classification, hidden-state clustering, and
  leakage-ablation experiments.
- **Cross-option comparison:** Worked jointly with Keyang Peng on the implementation
  of `compare.py`.
- **Report writing:** Co-wrote and reviewed the final research report with
  Keyang Peng.

**2. Keyang Peng**

- **Shared experimental framework:** Responsible for formulating the alignment rules
  and methodological conventions for the shared framework, including target
  construction, forecast schedules, evaluation protocols, and information-set
  restrictions.
- **Option A:** Responsible for the implementation and empirical analysis of
  PCA-based GDP nowcasting, Probit regime classification, and rolling-window
  indicator clustering.
- **Cross-option comparison:** Worked jointly with Qiying Zheng on the implementation
  of `compare.py`.
- **Report writing:** Co-wrote and reviewed the final research report with
  Qiying Zheng.

**3. Joint Research Responsibilities**

Beyond our individual tasks, we jointly took responsibility for the overall research
design and all substantive analytical decisions. In particular, we:

- Defined and approved the research scope, analytical framework, and shared data and
  output interfaces.
- Selected the mathematical methods and model specifications, including formulas,
  variables, parameters, and rolling-window settings.
- Established the evaluation and validation procedures and reviewed whether the
  implementation and results were consistent with our intended methodology.
- Examined model outputs, identified potential errors, and determined whether
  corrections or further checks were necessary.
- Interpreted the empirical findings, assessed the limitations, and drew the final
  conclusions.
- Wrote, reviewed, and took full responsibility for the final report.

**4. Use of AI-Assisted Tools**

We used AI coding agents as technical assistants to improve implementation efficiency
and support computational work. Their assistance mainly covered:

- Implementing data-processing and automation pipelines.
- Writing and modifying code according to our specifications.
- Running computational experiments and generating numerical outputs when instructed.
- Debugging code and resolving implementation issues.
- Assisting with code-level checks, consistency checks, and reproducibility checks.
- Producing comparison outputs and supporting numerical verification.

The use of AI tools did not transfer research responsibility or decision-making
authority away from us. We determined the research questions, mathematical
formulations, model specifications, method selection, experimental settings, and
validation criteria. We also reviewed the implementation, examined the outputs,
assessed their correctness, and made the final decisions on whether the methods and
results were acceptable.

In particular, AI agents assisted with calculations and technical implementation, but
did not independently determine the mathematical methods, judge the validity of the
models, or interpret the economic meaning of the results. All substantive research
decisions and final judgments remained our responsibility. By delegating
execution-intensive and repetitive technical tasks to AI tools, we were able to
devote more time to research design, methodological scrutiny, and critical
evaluation. We wrote and reviewed the final report ourselves and take full
responsibility for its content.

### 8.3 Output index

| Topic | Main evidence files |
|---|---|
| Frozen data and availability | `data/metadata.json`; `data/clean/missing_report.md`; `results/approach1/data/availability_checks.json` |
| Derived GDP target | `results/approach1/data/gdp_target_derived.csv`; `results/approach1/data/target_checks.json` |
| GDP nowcasting | `results/approach1/gdp_nowcasting/metrics.json`; `predictions.csv`; `revision_path.csv`; `coefficients.csv` |
| Option A Probit | `results/approach1/regime/metrics.json`; `predictions.csv`; `coefficients.csv`; `folds.csv` |
| Option A Probit optimizer audit | `results/approach1/regime/diagnostics/probit_diagnostic_summary.md`; `optimizer_convergence_by_fold.csv`; `diagnostics_validation.json` |
| Strictly nested Probit audit (invalid diagnostic) | `results/approach1/probit_nested_cv_pca_optimizer_audit/diagnostic_note.md`; `audit_summary.json`; `validation_report.json` |
| Option A clustering | `results/approach1/clustering/metrics.json`; `window_clusters.csv`; `silhouette_by_window.csv`; `adjacent_ari.csv`; `cluster_lead_lag.csv`; `cluster_regime_profile.csv` |
| Option A validation | `results/approach1/validation/validation_report.json`; `leakage_audit.csv` |
| Option B regime classification | `results/approach2/regime/metrics.json`; `predictions.csv`; `results/approach2/regime_threshold49_5/` |
| Option B hidden-state clustering | `results/approach2/clustering/metrics.json`; `latent_clusters.csv`; `latent_embeddings.csv` |
| Option B leakage ablation | `results/approach2/leakage_ablation/metrics.json`; `predictions.csv`; `run_log.txt`; `NOTES.md` |
| Option B independent review | `results/approach2/OPTION_B_REVIEW.md` |
| Consolidated cross-task comparison | `results/comparison.html`; `code/compare.py` |
| Source drafts for this report (unchanged) | `results/approach1/option_a_report_draft_revised(1).md`; `results/approach2/REPORT_DRAFT.md` |

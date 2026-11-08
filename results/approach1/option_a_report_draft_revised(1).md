# Question 3 — Option A: Classical Approach

> **Working draft for integration into the group's final report.** This section covers Option A only. The Option B results, the common three-layer clustering comparison, and the group-level contribution statement should be integrated after the other member's work is finalized. All numerical claims below are taken from the supplied Option A code and saved outputs; the group should review the interpretation and final wording before submission.

## 1. Data and preprocessing

### 1.1 Monthly macroeconomic panel

The analysis uses the frozen AkShare-based monthly macroeconomic panel in `data/clean/macro_panel.csv`. The snapshot contains 227 monthly observations from January 2008 to November 2026 and 30 indicators. The variables cover business surveys and sentiment (`pmi_man`, `pmi_nonman`, `consumer_confidence`, and `lpi`); prices (`cpi_yoy`, `ppi_yoy`, and `cgpi_yoy`); real activity and demand (`industrial_va_yoy`, `retail_yoy`, `fai_yoy`, `elec_cons_yoy`, and `freight_yoy`); money and credit (`m0_yoy`, `m1_yoy`, `m2_yoy`, `new_loans_yoy`, `tsf_flow`, and `base_money_yoy`); external trade and fiscal conditions (`trade_bal_bn_usd`, `export_yoy`, `import_yoy`, `fx_reserves_yoy`, and `fiscal_rev_yoy`); and interest rates and spreads (`lpr_1y`, `lpr_5y`, `shibor_on`, `shibor_3m`, `shibor_1y`, `shibor_spread_1y_on`, and `lpr_spread_5y_1y`).

The monthly reference period is not necessarily the publication date. For each indicator, the publication lag is taken from `data/metadata.json` rather than imposing one common lag. At a month-end forecast origin \(t\), the available information set is

\[
\mathcal F_t=\{x_{i,s}:s+L_i\leq t\},
\]

where \(L_i\) is the documented publication lag for indicator \(i\). For example, the manufacturing PMI has a zero-month lag based on its verified month-end release, CPI has a one-month lag, and consumer confidence uses a conservative two-month lag. Some other lag assumptions are estimated or conservative rather than directly verified; these classifications are retained as documented in the metadata. The feature-building routine shifts each series by its own lag, so the value in feature-panel row \(t\) is the latest reference-month observation eligible at the end of month \(t\). This prevents later-released observations from entering an earlier forecast origin.

The raw panel preserves missing observations as `NaN`. No future-aware interpolation or backward filling is applied. The data hygiene report flags outliers using

\[
|x_{i,t}-\operatorname{median}(x_i)|>5\times1.4826\times MAD(x_i),
\]

but flagged values are reported rather than deleted or winsorized. The frozen `data/metadata.json` records 54 indicator-month flags in its `outliers_flagged` list; the generated `missing_report.md` displays only the 25 largest values, while the metadata records the full count. The flags are diagnostic markers, not observations removed from the panel. For indicators whose January–February statistics are published only as a combined amount, standalone January and February values remain missing. Some series also have shorter histories or documented gaps; for example, `tsf_flow` ends in April 2026 in the frozen snapshot. These limitations are preserved rather than silently filled.

The November 2026 observations for SHIBOR and the derived SHIBOR spread are partial-month averages based only on trading days from November 1 to 5. They remain in the frozen source file for provenance but are excluded from model training and backtesting. Accordingly, the effective monthly modeling sample ends in October 2026.

### 1.2 Model-specific preprocessing

For factor extraction, indicators are screened separately at each forecast origin. A series is retained when it has at least 24 non-missing observations in the available training history, at least 70% coverage over the most recent 60 months, and non-zero variation. For the retained indicators, the mean and sample standard deviation are estimated using the relevant training window only:

\[
\widetilde{x}_{i,s}^{(t)}=
\frac{x_{i,s}-\widehat\mu_{i,t}^{\,train}}
{\widehat\sigma_{i,t}^{\,train}}.
\]

Missing entries are set to the training-window mean before standardization, which is equivalent to zero in standardized space. The fitted means, standard deviations, and PCA transformation are then applied unchanged to the corresponding forecast rows. Thus, neither standardization nor PCA uses future observations relative to the forecast origin.

Clustering uses a separate and stricter window-level coverage rule, described in Section 2.4. It does not use the factor-model imputation rule.

## 2. Methodology

### 2.1 PCA-based macroeconomic factors

At each forecast origin, the retained standardized indicator matrix is decomposed by principal component analysis. If \(Z_t\) denotes the training-window standardized matrix, its singular-value decomposition is

\[
Z_t=U_t\Sigma_tV_t^{\top}.
\]

The first \(r\) loading vectors form the factor transformation, and the factor score for an observation is

\[
\widehat f_s^{(r)}=V_{t,r}^{\top}\widetilde x_s,
\]

where the same origin-specific standardization and loading matrix are applied to all training and forecast observations. The implementation uses full-SVD PCA and considers at most eight components. A deterministic sign convention makes the loading with the largest absolute value positive for each component; this changes only the sign representation, not the information contained in the component.

The factor count is selected by inner expanding-window cross-validation using nowcasting error. Candidate values are \(r=1,\ldots,8\), subject to the dimensions supported by the training sample. For each inner validation quarter, the model is estimated using only GDP targets observable at that inner forecast origin, and the squared prediction errors are pooled across the M1, M2, and M3 vintages. The selected factor count minimizes pooled inner-validation RMSE; the smaller factor count is chosen in the event of a tie. In the 105 out-of-sample GDP forecast records, \(r=3\) is selected 90 times and \(r=4\) 15 times.

### 2.2 GDP target reconstruction and Bridge Regression

#### Derived single-quarter GDP target

The frozen quarterly GDP source contains 83 observations from 2006Q1 to 2026Q3. Its `gdp_yoy` field is a single-quarter year-on-year rate for Q1, but a cumulative year-to-date rate for Q2, Q3, and Q4. The cumulative nominal GDP field, `gdp_abs_cum_bn`, is measured in RMB 100 million (亿元), notwithstanding the legacy `bn` suffix. Cumulative Q2–Q4 growth rates are therefore not differenced directly to produce quarterly growth rates.

Instead, let \(A_{y,q}\) be the published cumulative GDP level and \(g_{y,q}\) the published cumulative year-on-year growth rate expressed as a decimal. Each quarter's 2006 cumulative level is used as an anchor:

\[
C_{2006,q}=A_{2006,q}.
\]

The cumulative volume series is chained forward across years:

\[
C_{y,q}=C_{y-1,q}(1+g_{y,q}),\qquad y>2006.
\]

Quarterly volumes are then recovered by de-accumulation:

\[
V_{y,1}=C_{y,1},\qquad
V_{y,q}=C_{y,q}-C_{y,q-1},\quad q=2,3,4.
\]

The derived single-quarter year-on-year target is

\[
y_{y,q}^{GDP}=100\left(\frac{V_{y,q}}{V_{y-1,q}}-1\right).
\]

The published Q1 rate is retained for 2006Q1; 2006Q2–Q4 remain unavailable because the frozen source does not provide prior-year quarterly volume denominators for those observations. The reconstructed target is available from 2007Q1 to 2026Q3, with 79 derived observations. It is a **derived chain-linked single-quarter GDP volume estimate**, not a separately published official NBS single-quarter series.

The arithmetic checks are satisfactory: all reconstructed quarterly volumes are positive; the maximum absolute difference between reconstructed and published Q1 growth is \(1.24\times10^{-14}\) percentage points over 20 observations; and the maximum error when recovering the published cumulative growth rates is \(1.07\times10^{-14}\) percentage points over 59 checks. These checks verify internal arithmetic consistency, not equivalence to an independently published single-quarter real GDP series.

#### Bridge Regression specification and forecast vintages

Quarterly GDP is the sole nowcasting target. Industrial value-added growth remains one of the monthly predictors and is not treated as a second nowcasting target. For each within-quarter vintage \(v\in\{M1,M2,M3\}\), a separate Bridge Regression is estimated:

\[
y_q^{GDP}=\alpha_v+\beta_v^{\top}\widehat f_{o(q,v)}^{(r)}+\varepsilon_{q,v},
\]

where \(o(q,v)\) is the month-end origin corresponding to the first, second, or third month of quarter \(q\), respectively. The model uses the factor scores available at that origin. Historical training pairs use the matching within-quarter vintage, and each regression is estimated by ordinary least squares with an intercept. The quarterly GDP publication lag is one month; a realized GDP target enters the estimation sample only after this lag has elapsed. The target quarter being forecast is excluded from its own training sample.

The initial training period is 2008Q1–2017Q4. The out-of-sample period is 2018Q1–2026Q3, covering 35 quarters and 105 forecast records (35 quarters multiplied by three vintages). The training window expands as additional quarterly GDP observations become available.

The main benchmark is a random walk with training-window drift:

\[
\widehat y_{q|t}^{RW}=y_{q^-}^{GDP}+\widehat\mu_t,
\qquad
\widehat\mu_t=\frac{1}{N_t-1}
\sum_{j=2}^{N_t}(y_j^{GDP}-y_{j-1}^{GDP}),
\]

where \(q^-\) is the latest observable training quarter and \(\widehat\mu_t\) is estimated only from the available training targets. A historical-mean forecast is retained as a secondary benchmark. Forecast accuracy is evaluated using RMSE and MAE, both overall and separately by vintage.

### 2.3 Probit-based contraction classification

The primary contraction label is constructed from the NBS manufacturing PMI:

\[
d_t=\mathbb{1}\{PMI_t<50\ \text{and}\ PMI_{t-1}<50\}.
\]

Requiring two consecutive observations below 50 makes the label represent a sustained contraction signal rather than a single-month decline around the diffusion-index threshold. A second label uses 49.5 as the threshold for the required sensitivity analysis.

At month-end origin \(t\), the model forecasts the next month's label:

\[
\Pr(d_{t+1}=1\mid z_t)=\Phi(\beta_0+\beta^{\top}z_t),
\]

where \(\Phi(\cdot)\) is the standard normal cumulative distribution function. The coefficient vector is estimated by maximum likelihood, with log-likelihood

\[
\ell(\beta_0,\beta)=\sum_t\left[d_{t+1}\log\Phi(\eta_t)+(1-d_{t+1})\log\{1-\Phi(\eta_t)\}\right],
\qquad \eta_t=\beta_0+\beta^{\top}z_t.
\]

The primary predictor vector contains the PCA factors and the standardized one-year Loan Prime Rate (LPR 1Y):

\[
z_t=\left(\widehat f_{1,t},\ldots,\widehat f_{r,t},\widetilde{LPR1Y}_t\right)^{\top}.
\]

The rate and spread block is excluded from the PCA factor universe to avoid counting the same rate information both in the factors and as an explicit control. LPR 1Y is standardized using the corresponding training sample. Because the available LPR 1Y history begins in October 2013, the primary factors-plus-control model begins its label-training sample in November 2013. A factors-only specification is also saved as a separate sensitivity, using the longer label history from February 2008; results from that specification should not be interpreted as a pure one-variable ablation because its estimation sample is longer.

The factor count used by the Probit is the count selected by the GDP nowcasting cross-validation procedure at the corresponding origin. In the 82 primary Probit folds, the selected rank is three in 61 folds and four in 21 folds. The model is evaluated with an expanding-window walk-forward schedule: 82 one-month-ahead forecasts from January 2020 through October 2026, made at month-end origins from 31 December 2019 through 30 September 2026. At every origin, only historical labels observable by that origin are included in estimation. The predicted class uses a fixed probability threshold of 0.5.

The model is estimated by Probit maximum likelihood. The implementation attempts Newton estimation, then BFGS, with a documented ridge-Probit fallback if both fail. The primary optimizer audit independently inspected the optimizer's actual convergence return values rather than relying on the wrapper's success flag. All 82 primary outer fits converged using Newton, with no non-zero warning flag, no captured convergence warning, and no ridge fallback. The audit reproduced the saved probabilities with a maximum absolute difference of zero.

Two benchmarks are reported on the same 82 target months. The prior-frequency benchmark assigns each forecast the contraction frequency in that fold's training sample. The persistence benchmark uses the latest known label, \(d_t\), as the forecast for \(d_{t+1}\). Performance is evaluated with Brier score, AUC, accuracy, and, for the model, sensitivity, specificity, and balanced accuracy. The prior-frequency baseline is especially important because the positive class is less frequent than the negative class.

### 2.4 Rolling-window indicator clustering

Indicator clustering is performed on rolling 36-month windows, advanced in 12-month steps. The 17 window endpoints are December 2010 through December 2025, followed by the final available month, October 2026. For each window, observations are masked by their indicator-specific publication lags. An indicator is retained if it has at least 95% coverage within the window and non-zero variance. Derived spreads are removed when both component series are retained, and a deterministic rank screen removes any remaining exact linear dependencies. Correlations are then calculated on the common complete-case monthly sample, with at least 24 common observations required.

The coverage screen systematically excludes `consumer_confidence` from all 17 clustering windows. Although the indicator is present in the monthly panel, it has a conservative two-month publication lag. At each window endpoint, the most recent two reference-month observations are therefore not yet available under the as-of information rule. Its eligible coverage is consequently 34 out of 36 months (94.44%) in every window, below the 95% threshold, which requires at least 35 observations. The `window_retention.csv` audit records `retained = False` and `drop_reason = coverage` for this indicator in all 17 windows. This is a mechanical consequence of the publication-lag and coverage rules; it does not mean that the variable was removed because of low variance, weak correlation, or lack of economic relevance. `consumer_confidence` remains in the frozen monthly panel, but it is not part of the clustering input universe.

For retained indicators \(i\) and \(j\), the Pearson correlation is converted to the prescribed distance:

\[
d_{ij}=\sqrt{2(1-\rho_{ij})}.
\]

Ward's hierarchical clustering is applied to the condensed distance matrix. At each merge, Ward's criterion chooses the pair of clusters whose merger produces the smallest increase in within-cluster sum of squares. Since Ward linkage requires Euclidean-compatible distances, the implementation checks both that the sample correlation matrix is positive semi-definite and that the distance matrix agrees with Euclidean distances between normalized standardized indicator vectors. Specifically, if \(u_i\) is the unit-normalized standardized vector for indicator \(i\), then

\[
\lVert u_i-u_j\rVert_2=\sqrt{2(1-\rho_{ij})}.
\]

The implementation evaluates silhouette scores for \(k=2,\ldots,8\), selects the value with the highest silhouette score (choosing the smaller \(k\) in a tie), and also records a fixed-\(k=4\) sensitivity. For an observation \(i\), the silhouette is \(s(i)=[b(i)-a(i)]/\max\{a(i),b(i)\}\), where \(a(i)\) is its average distance to its own cluster and \(b(i)\) is the smallest average distance to another cluster. Clustering stability is assessed using adjusted Rand index (ARI) between adjacent windows, calculated only on the indicators retained in both windows. This ensures that each ARI compares assignments for the same observation universe and adjusts pairwise agreement for chance.

For economic interpretation, the monthly indicators are also aggregated to quarterly averages and correlated with the derived single-quarter GDP target at leads/lags from −4 to +4 quarters. At least 12 valid pairs are required. Cluster-level lead–lag summaries average member-indicator correlations at each lag and identify the lag with the largest absolute average correlation. These are descriptive cross-correlations, not causal effects or forecast coefficients.

## 3. Experimental design and validation

All main experiments use the fixed frozen data snapshot, seed 42, and expanding-window walk-forward evaluation. The GDP nowcasting schedule uses 40 initial training quarters (2008Q1–2017Q4) and 35 out-of-sample quarters (2018Q1–2026Q3), with M1/M2/M3 forecasts for every target quarter. The regime-classification schedule uses the same 82 target months and month-end forecast origins for all headline Probit comparisons. The clustering procedure uses 17 rolling windows.

The main validation script reports 189 out of 189 checks passed. The validation report also records 4,786 preprocessing values checked with no failures, 204 leakage-audit rows, and 18 generated figures. The data-availability checks confirm that the constructed feature rows match the indicator-specific panel shifts, the GDP target is never used to train its own forecast, and the partial November 2026 month is excluded. The GDP reconstruction checks pass, and the clustering checks confirm positive-semidefinite correlation matrices and Euclidean-compatible distances in all 17 windows.

The primary Probit optimizer audit covers all 82 outer forecasts. Newton converged in every fold (6 to 8 iterations), with no non-zero warning flags, no captured warnings, and no ridge fallbacks. The largest absolute coefficient was 4.69. Seven out-of-sample predicted probabilities were below 0.01 and two were above 0.99; extreme probabilities alone were not treated as evidence of non-convergence. Coefficient signs were stable for the intercept and first factor, but less stable for the second and third factors and the LPR control.

An additional strictly nested Probit rank-selection experiment was audited separately and is not used as a headline result. Its optimizer audit covered 35,752 inner candidate fits. It identified 902 fits that did not satisfy the strict convergence acceptance rule; 246 of the 4,469 inner-fold groups were unusable when all eight candidate ranks were required to pass. The 82 outer fits did converge, and an audit-only re-evaluation on the reduced common inner-fold set did not change the selected rank in any outer fold. Nevertheless, the saved nested-CV experiment was marked invalid by its audit, and no corrected experiment was created. It is therefore not treated as validated evidence for replacing the primary Probit.

## 4. Empirical results

### 4.1 GDP nowcasting

The PCA-Bridge model produces 105 out-of-sample forecasts. Table 1 reports the results by vintage and overall.

**Table 1. GDP nowcasting performance (percentage points)**

| Sample | Forecasts | RMSE | MAE | Bias | \(R^2\) |
|---|---:|---:|---:|---:|---:|
| M1 | 35 | 3.2142 | 1.3351 | 0.2717 | 0.0763 |
| M2 | 35 | 2.6593 | 1.2363 | 0.4691 | 0.3677 |
| M3 | 35 | 3.2642 | 1.4938 | −0.2980 | 0.0474 |
| **Overall PCA-Bridge** | **105** | **3.0582** | **1.3551** | **0.1476** | **0.1638** |
| Random walk with drift | 105 | 4.1549 | 2.0985 | 0.0429 | −0.5435 |
| Historical mean | 105 | 4.0569 | 2.9649 | −2.2898 | −0.4715 |

The overall PCA-Bridge RMSE is lower than the random-walk-with-drift RMSE. The corresponding RMSE-based skill measure is 0.4582, calculated as \(1-RMSE_{model}^2/RMSE_{RW}^2\). The model also has a lower MAE than both benchmarks. The M2 vintage has the lowest RMSE and MAE among the three within-quarter vintages. However, forecast accuracy does not improve monotonically as more within-quarter information becomes available: M3 RMSE (3.2642) is higher than M1 RMSE (3.2142), and only 48.6% of quarters have a lower absolute error at M3 than at M1.

The mean absolute revision from M1 to M3 is 0.8498 percentage points, comprising 0.7557 points from M1 to M2 and 0.9597 points from M2 to M3. Revisions therefore reflect material changes in the information set, but a larger information set does not guarantee a more accurate estimate in every quarter. The 2020Q1 forecasts illustrate this limitation: the derived realized target is −6.8%, while the model forecasts are 5.8294% at M1, 0.7889% at M2, and 7.2030% at M3. The M2 estimate moves downward, but the M3 estimate rises again. The within-quarter path should therefore be reported rather than presenting only the latest forecast.

**Figure references:** [Figure 1 — Derived quarterly GDP target](results/approach1/gdp_nowcasting/figures/fig_gdp_target.html); [Figure 2 — Nowcasts versus realized GDP](results/approach1/gdp_nowcasting/figures/fig_nowcast_vs_realized.html); [Figure 3 — RMSE by vintage](results/approach1/gdp_nowcasting/figures/fig_rmse_by_vintage.html); [Figure 4 — Within-quarter revision path](results/approach1/gdp_nowcasting/figures/fig_revision_path.html); [Figure 5 — PCA scree plots](results/approach1/gdp_nowcasting/figures/fig_pca_scree.html); [Figure 6 — Factor loadings](results/approach1/gdp_nowcasting/figures/fig_factor_loadings.html).

### 4.2 Probit contraction classification

At the primary threshold of 50, the 82 out-of-sample target months contain 27 contraction observations and 55 non-contraction observations. Table 2 compares the Probit with the two benchmarks.

**Table 2. Primary Probit performance on the common 82 target months**

| Model | Brier | AUC | Accuracy | Balanced accuracy |
|---|---:|---:|---:|---:|
| Probit: PCA factors + LPR 1Y | 0.2517 | 0.5953 | 0.5976 | 0.5492 |
| Prior-frequency baseline | 0.2321 | 0.5997 | 0.6707 | — |
| Persistence baseline | 0.2439 | 0.7239 | 0.7561 | 0.7239 |

The primary Probit does not outperform either benchmark on Brier score, AUC, or accuracy. Its Brier skill score relative to the prior-frequency baseline is −0.0848, where positive values would indicate an improvement over the baseline. At the fixed 0.5 classification threshold, the Probit correctly identifies 11 of the 27 contraction months (sensitivity 0.4074) and 38 of the 55 non-contraction months (specificity 0.6909). The persistence benchmark performs particularly well on AUC, accuracy, and balanced accuracy. These results indicate that the selected static factor-plus-rate specification provides limited one-month-ahead contraction discrimination over this evaluation period.

The final outer fold (target month October 2026, forecast origin 30 September 2026) provides the following coefficient estimates. This fold uses three factors and 155 training labels, including 41 contraction observations.

**Table 3. Final-fold Probit coefficient estimates**

| Term | Coefficient | Standard error | t-statistic |
|---|---:|---:|---:|
| Intercept | −1.2829 | 0.2819 | −4.5504 |
| Factor 1 | −0.4127 | 0.1422 | −2.9029 |
| Factor 2 | −0.1481 | 0.1015 | −1.4599 |
| Factor 3 | −0.0238 | 0.1128 | −0.2111 |
| Standardized LPR 1Y | −0.2711 | 0.1893 | −1.4321 |

The coefficient table is specific to the final fold; it should not be interpreted as a time-invariant structural relationship. Across folds, the sign of the intercept and first factor is stable, while the signs of higher-order factors and the LPR control vary more often.

The 49.5 threshold sensitivity produces 13 contraction observations and 69 non-contraction observations among the same 82 target months. With LPR 1Y included, the sensitivity model has Brier 0.1446, AUC 0.6912, accuracy 0.7927, and balanced accuracy 0.4710. However, at the 0.5 decision threshold it predicts no contraction observations: sensitivity is zero and specificity is 0.9420. Its higher raw accuracy therefore needs to be read in the context of the more imbalanced target. The prior-frequency baseline has Brier 0.1428 and accuracy 0.8415, while the persistence baseline has Brier 0.1951 and accuracy 0.8049. The 49.5 results are a separate label-definition sensitivity and are not pooled with the primary threshold results.

A factors-only version at threshold 50 is also saved. It produces Brier 0.2220 and AUC 0.6357, but it uses the longer training-label history beginning in February 2008, whereas the primary LPR-controlled model begins in November 2013. It is therefore an auxiliary specification comparison rather than a clean estimate of the incremental effect of adding LPR alone.

**Figure references:** [Figure 7 — Probit forecast probabilities](results/approach1/regime/figures/fig_regime_probabilities.html); [Figure 8 — ROC curve](results/approach1/regime/figures/fig_regime_roc.html); [Figure 9 — Calibration plot](results/approach1/regime/figures/fig_regime_calibration.html); [Figure 10 — Fold-level Brier scores](results/approach1/regime/figures/fig_regime_brier.html); [Figure 11 — Final-fold coefficient estimates](results/approach1/regime/figures/fig_regime_coefficients.html).

### 4.3 Indicator clustering

The clustering procedure produces 17 valid rolling windows. The number of retained indicators ranges from 15 to 21, with a mean of 17.82; the common complete-case sample contains 35 months on average. The selected silhouette score averages 0.2405, ranges from 0.1618 to 0.3409, and is 0.1951 on average under the fixed-\(k=4\) sensitivity. The average adjacent-window ARI is 0.2383, with a minimum of −0.0520 and a maximum of 1.0000 across the 16 adjacent-window comparisons.

The Euclidean-compatibility checks pass in every window. All sample correlation matrices are positive semi-definite under the numerical tolerance, and the maximum absolute difference between the prescribed correlation distance and the corresponding normalized-vector Euclidean distance is \(2.83\times10^{-15}\). Thus, the required Ward linkage is applied to an empirically verified Euclidean-compatible distance construction rather than being replaced by another linkage method.

In the final October 2026 window, the selected solution has two clusters. The first contains ten indicators: CPI, PPI, CGPI, M2, M1, base money, trade balance, exports, imports, and foreign-exchange reserves. The second contains nine indicators: manufacturing PMI, non-manufacturing PMI, M0, new loans, LPR 1Y, LPR 5Y, SHIBOR overnight, SHIBOR 3-month, and SHIBOR 1-year. These memberships suggest co-movement across price, external-sector, money, business-survey, credit, and interest-rate series, but the clusters do not map perfectly onto conventional macroeconomic categories.

For the final window, the cluster-level lead–lag summary reaches its maximum absolute mean correlation at lag −1 quarter for the first cluster (mean correlation 0.3100) and lag 0 for the second cluster (mean correlation 0.3036), using the convention that an indicator at quarter \(q\) is correlated with GDP at quarter \(q+\ell\). These results indicate, respectively, a strongest association with GDP one quarter earlier and a contemporaneous association under this convention. They are descriptive correlations and should not be interpreted as causal or predictive effects. In the same final window, the average standardized profile for the ten-member first cluster is −0.1163 in contraction months and 0.1098 in non-contraction months (difference −0.2261). For the nine-member second cluster, the corresponding values are 0.0364 and −0.0318 (difference 0.0682). These differences are modest and provide contextual information rather than a separate validation of the cluster labels.

The average adjacent-window ARI indicates that the detailed memberships are not highly stable over time, even though some adjacent pairs have high similarity. The selected number of clusters also varies across windows. The results are therefore more useful for identifying changing groups of co-moving indicators than for asserting a fixed macroeconomic taxonomy.

**Figure references:** [Figure 12 — Rolling-window dendrograms](results/approach1/clustering/figures/fig_dendrograms.html); [Figure 13 — Silhouette by window](results/approach1/clustering/figures/fig_silhouette.html); [Figure 14 — Silhouette across candidate cluster counts](results/approach1/clustering/figures/fig_silhouette_k_curve.html); [Figure 15 — Adjacent-window ARI](results/approach1/clustering/figures/fig_adjacent_ari.html); [Figure 16 — Cluster membership over time](results/approach1/clustering/figures/fig_cluster_membership.html); [Figure 17 — GDP lead–lag correlations](results/approach1/clustering/figures/fig_lead_lag_gdp.html); [Figure 18 — Cluster profiles by PMI regime](results/approach1/clustering/figures/fig_cluster_regime_profile.html).

## 5. Discussion and limitations

The three Option A components provide complementary but uneven evidence. The PCA-Bridge model improves on the random-walk-with-drift benchmark in pooled GDP nowcasting RMSE and MAE, but the improvement is not monotonic across M1, M2, and M3. M2 has the best average accuracy, while the M3 revision path can move away from the realized value in individual episodes. The 2020Q1 example shows why the within-quarter revision path is informative: additional monthly observations can change the nowcast materially without necessarily resolving an abrupt shock.

The Probit results are weaker. Although the primary model is numerically well behaved according to the independent optimizer audit, it does not beat the prior-frequency benchmark on Brier score, AUC, or accuracy, and it is clearly outperformed by the persistence benchmark on the reported classification metrics. Numerical convergence should therefore not be confused with predictive usefulness. The 49.5 sensitivity also demonstrates how a modest threshold change can substantially reduce the number of positive observations; its zero sensitivity at the 0.5 cutoff makes the high raw accuracy misleading when considered alone.

The clustering results satisfy the prescribed distance and Ward-linkage requirements and provide interpretable groups of indicators in each window. However, the mean silhouette and adjacent-window ARI are relatively modest, the selected cluster count changes over time, and the retained indicator universe varies with coverage. In particular, `consumer_confidence` is absent from every clustering window because its two-month publication lag leaves only 34 eligible observations per 36-month window, below the 95% coverage threshold. The resulting clusters therefore do not capture this sentiment indicator, and they should be interpreted as window-specific co-movement structures conditional on the available-variable screen, not as permanent economic sectors or causal mechanisms.

Several limitations apply across the analysis. First, the exercise uses one frozen data snapshot rather than a complete archive of historical real-time data vintages. Although indicator-specific publication lags are imposed, historical revisions in the source data may remain. Second, publication lags for some indicators are estimated or conservative, and source coverage is uneven. The two-month lag assigned to `consumer_confidence`, together with the 95% clustering coverage threshold, excludes this indicator from all 17 clustering windows; the clustering findings should therefore not be generalized to the full 30-indicator panel. Third, the derived GDP target is an internally consistent chain-linked estimate constructed from the published cumulative source fields; it is not an official separately published quarterly real-GDP series. Fourth, the Probit's LPR-controlled training sample is shorter than the full shared label history because LPR 1Y observations begin in October 2013, leaving a relatively small number of positive observations in each estimation window. Fifth, the clustering windows contain only 36 months before lag and coverage restrictions, and correlations and ARI can be sensitive to the window and retained-variable set.

Finally, the strictly nested Probit sensitivity experiment was not promoted to a model result. Its subsequent optimizer audit found candidate fits that failed the strict convergence rule, and the audit explicitly marked the saved nested-CV experiment as invalid. Although selected ranks were unchanged on the reduced common set, no corrected experiment was run. The primary GDP-nowcast-selected Probit is therefore retained as the reported specification, and the nested-CV exercise is mentioned only as an inconclusive diagnostic.

## 6. Option A implementation and reproducibility

The Option A implementation is organized into the following scripts:

- `code/approach1_data.py`: frozen-input verification, publication-lag features, PMI labels, GDP target reconstruction, and availability checks.
- `code/approach1_gdp_nowcast.py`: PCA factor extraction, rank selection, Bridge Regression, benchmarks, and within-quarter GDP forecasts.
- `code/approach1_regime.py`: Probit estimation, coefficient tables, baseline comparisons, and the 49.5 threshold sensitivity.
- `code/approach1_cluster.py`: rolling-window Ward clustering, silhouette scores, adjacent-window ARI, and GDP lead–lag summaries.
- `code/approach1_validate.py`: independent validation and leakage checks.
- `code/run_option_a.py`: combined Option A reproduction entry point.

The main reproduction command is:

```bash
uv run python code/run_option_a.py --seed 42
```

The saved validation report records 189/189 checks passed. Experiment-level metrics, row-level predictions, preprocessing records, audits, and self-contained Plotly HTML figures are saved under `results/approach1/`.

### Contribution statement to integrate with the group's report

The terminal coding agent was used to inspect the repository, implement the Option A scripts, execute the prescribed experiments, generate result files and visualizations, and run reproducibility and leakage checks. The group retained responsibility for defining and approving the shared data and target conventions, selecting the methodological specifications, reviewing the validation evidence, interpreting the economic results, and approving the final conclusions. The final report and any methodological decisions remain the group's responsibility. **The group should revise this statement to reflect the actual division of work across all members and any additional writing or language-assistance tools used.**

---

## Supporting output index

| Topic | Main evidence files |
|---|---|
| Frozen data and availability | `data/metadata.json`; `data/clean/missing_report.md`; `results/approach1/data/availability_checks.json` |
| Derived GDP target | `results/approach1/data/gdp_target_derived.csv`; `results/approach1/data/target_checks.json` |
| GDP nowcasting | `results/approach1/gdp_nowcasting/metrics.json`; `predictions.csv`; `revision_path.csv`; `coefficients.csv` |
| Probit | `results/approach1/regime/metrics.json`; `predictions.csv`; `coefficients.csv`; `folds.csv` |
| Probit optimizer audit | `results/approach1/regime/diagnostics/probit_diagnostic_summary.md`; `optimizer_convergence_by_fold.csv`; `diagnostics_validation.json` |
| Strictly nested Probit audit | `results/approach1/probit_nested_cv_pca_optimizer_audit/diagnostic_note.md`; `audit_summary.json`; `validation_report.json` |
| Clustering | `results/approach1/clustering/metrics.json`; `window_clusters.csv`; `silhouette_by_window.csv`; `adjacent_ari.csv`; `cluster_lead_lag.csv`; `cluster_regime_profile.csv` |
| Overall validation | `results/approach1/validation/validation_report.json`; `leakage_audit.csv` |


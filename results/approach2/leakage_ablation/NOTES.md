# Option B — factual experimental notes (for the group's `report.md`)

Prepared by the terminal agent. These are factual findings from the committed
experiment outputs; interpretation, mathematical specification, and the final
report remain the group's responsibility. Frozen panel
`data/clean/macro_panel.csv` (SHA-256 verified by both scripts); shared
schedule per `interface.md` §5.1; results under `results/approach2/`.

## 1. Headline leakage-safe regime classification (`results/approach2/regime/`)

Source: `code/approach2_regime.py --seed 42` (primary) and `--label-threshold 49.5`
(sensitivity). Lookback `h = 3` months, selected by mean Brier on inner
validation targets 2018-01..2019-12 (fit strictly before 2018-01; candidates
h ∈ {3, 6, 12}; LSTM and GRU both scored across seeds 42/7/123).

Model: LSTM and GRU (2 layers, hidden 32, dropout 0.2), input =
lag-shifted standardized window x~_(t-h:t), BCE loss with validation-tail
threshold selection for class imbalance; 24-month validation tail inside each
expanding training window for early stopping and threshold choice.

82 one-month-ahead folds (targets 2020-01..2026-10), fold id = target month.
Per-seed metrics, mean ± std across seeds {42, 7, 123}:

| model | Brier | AUC | Accuracy | Brier (seed 42 / 7 / 123) |
|---|---|---|---|---|
| naive prior-frequency baseline | 0.2551 | — | 0.6707 | deterministic |
| lstm | 0.2207 ± 0.0035 | 0.6157 ± 0.0055 | 0.4268 ± 0.0244 | 0.2237 / 0.2168 / 0.2215 |
| gru | 0.2386 ± 0.0088 | 0.5687 ± 0.0216 | 0.4390 ± 0.0761 | 0.2285 / 0.2447 / 0.2427 |

Both sequence models beat the prior-frequency baseline on Brier; AUC is
modest (0.57–0.62), below the assignment's indicative 0.75 benchmark.
Small seed dispersion (Brier std ≤ 0.009).

49.5-threshold sensitivity run exists at
`results/approach2/regime_threshold49_5/` (same schedule, labels rebuilt at 49.5).

## 2. Mandatory leakage ablation (`results/approach2/leakage_ablation/`)

Source: `code/approach2_leakage_ablation.py --seed 42`; same 82 folds, same
labels, same preprocessing discipline. The safe LSTM/GRU arm reuses the
headline per-seed per-fold predictions of `approach2_regime.py` verbatim.
The leaky arms are IDENTICAL in architecture and training, but use the RAW
unshifted panel as the feature window — i.e. every indicator is treated as
published at its reference month-end, ignoring
`publication_lag_months` from `data/metadata.json`.

| arm | `leakage_discipline` | Brier | AUC | Accuracy |
|---|---|---|---|---|
| naive prior-frequency | safe | 0.2551 | — | 0.6707 |
| probit (ridge-selected origin-row GLM) | safe | 0.2330 | 0.7788 | 0.6585 |
| lstm (safe) | safe | 0.2207 | 0.6157 | 0.4268 |
| gru (safe) | safe | 0.2386 | 0.5688 | 0.4390 |
| lstm without lag discipline | intentional-leakage-demo (**INVALID**) | 0.1978 | 0.7100 | 0.5325 |
| gru without lag discipline | intentional-leakage-demo (**INVALID**) | 0.2109 | 0.6510 | 0.5325 |

Leakage premium (leaky vs the corresponding safe arm, identical folds):
lstm Brier −0.0229, AUC +0.0943; gru Brier −0.0277, AUC +0.0822.
Factually: dropping per-indicator publication lags produces an apparent
improvement over the same architecture for every metric. Note the strong
safe probit reaches AUC 0.7788 on the same folds — the leaky RNNs do NOT beat
it on AUC, they only beat their own safe versions; the premium is a
within-architecture comparison. The leaky outputs are written to
`results/approach2/leakage_ablation/figures/` and metrics fields
carry `"leakage_discipline": "intentional-leakage-demo"`; they must be labelled
as invalid demonstrations in the report, never as valid forecasts.

Probit details: probit GLM (statsmodels Binomial, probit link) on the
standardized origin-row design z_u (= F_u); deterministic, seed null;
ridge alpha selected per fold on the 24-month validation tail only
(selected: alpha 10 in 58/82 folds, alpha 1 in 22, alpha 0.1 in 2);
probabilities clipped to [0,1]. A design that pooled all window rows (mixing
lead times) was tested first and rejected — it produced AUC 0.39 and unstable
perfect-separation fits; the origin-row design is the implemented arm.

## 3. Latent hidden-state clustering (`results/approach2/clustering/`)

Source: `code/approach2_clustering.py --seed 42`. Models trained once on all
label pairs observable at the clustering origin (descriptive origin
2026-10-31, the last full panel month; training targets d_(u+1) up to
d_2026-10; 24-month validation tail; same lag-shifted panel and h=3), then
hidden states
h_t (dimension 32) were extracted for every month-end t ∈ 2008-03..2026-10
(224 time observations; the partial-month 2026-11 row is excluded).
k-means with k selected per architecture by max mean silhouette across seeds
{k = 2..6}; 3 seeds (42, 7, 123).

| arch | k | silhouette mean ± std (per seed, own embedding space) | cross-seed pairwise ARI (mean) | ARI vs d_t | NMI vs d_t |
|---|---|---|---|---|---|
| lstm | 2 | 0.6410 ± 0.0696 | 0.3027 | 0.1312 | 0.2237 |
| gru | 2 | 0.4662 ± 0.1653 | 0.2982 | 0.0896 | 0.1939 |

- Cross-seed ARI is reported pairwise per seed pair (with common-observation
  counts, all 224) in `metrics.json`. LSTM pairwise ARI = 0.812 (42 vs 7),
  0.054 (42 vs 123), 0.042 (7 vs 123) — mean 0.303, with large dispersion:
  seeds 42 and 7 recover near-identical partitions, while seed 123 finds a
  different split of the same k=2 space. Practically, cross-seed stability
  is seed-dependent and should be reported as mean ± dispersion, not as a
  single stable number.
- Label alignment is explicit: each embedding is tied to its window-end month
  `t` (column `reference_period`) and to the one-month-ahead target
  `d_(t+1)` (column `target_period`). `metrics.json` records ARI/NMI against
  both `d_t` and `d_(t+1)` separately; `latent_clusters.csv` stores
  per-seed cluster ids and both label alignments; `latent_embeddings.csv`
  stores the 32-dim embeddings per seed for reproducibility.
- For the headline seed 42, the LSTM's two latent states split cleanly:
  one state (n = 108 months) contains 42.6% contraction (`d_t = 1`) months;
  the other (n = 116) contains 0.9% — i.e. essentially the non-contraction
  cluster. (Recorded as `contraction_share_by_cluster`,
  `reference_seed_for_shares = 42` in `metrics.json`.)
- Silhouette here is a method-specific diagnostic of Option B's embedding
  space; it must not be ranked against Option A's indicator silhouettes
  (different observation units, per `interface.md` §7).

## 4. Explicit methodological limitations (documented 2026-11-07)

Verified properties of the frozen design that the group's report should state
honestly. Not defects; no headline numbers change.

1. **PMI feature overlap with the target label.** `pmi_man` (lag 0, released
   at month-end of its reference month) is an input feature. The target
   `d_(t+1) = 1{PMI_(t+1) < thr and PMI_t < thr}` contains two PMI
   observations; at forecast origin `t` the model observes `PMI_t`, i.e. one of
   the two conditions. This is NOT future-information leakage (`PMI_t ∈ F_t`
   by the shared lag rule), but the task is effectively the *conditional*
   prediction of whether PMI stays below the threshold next month. `pmi_man`
   was not removed and headline results are unchanged.
2. **Threshold-selection uncertainty.** Decision thresholds are selected on a
   24-month validation tail only; small samples (tails with few positives)
   make threshold selection noisy, contributing to safe-RNN accuracy below
   the majority baseline. Thresholds were not (and will not be) retuned on
   test-period results.
3. **Clustering stability.** Cross-seed ARI is highly dispersed (LSTM pairs
   0.812/0.054/0.042 → mean 0.303; GRU pairs 0.195/0.628/0.071 → mean 0.298).
   The two latent clusters are exploratory and initialization-sensitive, NOT
   robust stable economic regimes. The seed-42 LSTM split should be presented
   as an illustrative single-seed interpretation.
4. **Classification performance (Brier vs AUC).** Safe LSTM has the best Brier
   (0.2207); safe Probit has the best AUC (0.7788) and the only accuracy near
   the majority baseline. No model is superior on all metrics; no dominance
   claim is supported.
5. **Probit comparability.** The implemented probit arm is a raw-feature,
   origin-row ridge probit (α per fold from validation-tail Brier), NOT
   Option A's factor-probit. It has not been silently replaced; the final
   cross-method specification (raw-feature vs factor-based probit) is a team
   decision. The rejected one-shot pooled-row probit (AUC 0.39,
   perfect-separation) is documented above in §2.

Post-review corrections applied 2026-11-07 (figure regeneration only; no model
or prediction rerun): the `ablation_summary.html` figure bug
(metrics read from `mm["mean"]` nesting; leaky labels suffixed "— INVALID
leakage demo"; bars colored by `leakage_discipline`) and the clustering-origin
documentation (now consistently 2026-10 in docstring, code comment,
`params.training`, and the stored metrics.json text).

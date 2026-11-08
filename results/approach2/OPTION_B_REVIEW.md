# Option B (Sequence Learning) — Independent Review Report

**Scope:** QF5313 Q3 Option B implementation — LSTM/GRU regime classification,
hidden-state clustering, mandatory leakage ablation. Frozen panel
`data/clean/macro_panel.csv` (SHA-256 `7256…eabe`, matches `data/metadata.json`;
re-verified by `reg.load_and_verify()`, `code/approach2_regime.py:154-164`).

**Environment (observed in this session):** Python 3.13, torch 2.15.0+cpu,
pandas 3.1.0, scikit-learn 1.9.1, statsmodels (ridge probit via GLM).
No experiments were rerun for this review; all numbers are read from the
committed result files listed in §7.

Legend: ✅ = verified against code and/or result files; ⚠ = concern;
❓ = open question for the group.

---

## 1. Data and information-set discipline

**Publication lags.** ✅ The metadata-driven rule is implemented by forward-shifting
every series by its own `publication_lag_months`: `build_shifted_panel()`
(`code/approach2_regime.py:172-181`) computes `shifted_i(s) = x_i(s − L_i)`, so a
row at month-end origin `s` contains exactly the observations of series `i`
eligible under `F_t = {x_(i,s): s + L_i ≤ t}`. Lags are read per indicator, not
as a blanket value: 9 series have `L=0` (PMIs, LPR, Shibor + spreads), 20 have
`L=1`, and `consumer_confidence` has a conservative `L=2`
(`data/metadata.json`, verified list). Lag bases (verified/estimated/conservative)
are preserved in metadata. ✅ The `2026-11` partial-month row is not used: the
regime schedule ends at origins `2026-09-30`, and the clustering script drops the
last panel row explicitly (`code/approach2_clustering.py:114`).

**Forecast-origin information.** ✅ A prediction at month-end origin month `t`
targets `d_(t+1)`. `window_X()` (`approach2_regime.py:196-199`) slices rows
`t−h+1 … t` of the lag-shifted panel; nothing from months `> t` is touched.
✅ Training-pair construction (`pairs_for_train`, `approach2_regime.py:202-212`)
requires the target label `d_(u+1)` to be already published by the origin
(`u ≤ origin−1`), i.e. labels obey a separate publication rule from predictors,
as required by interface.md §4. PMI label components are published at month-end
of their reference month (`L=0`, verified basis), so a label `d_s` for `s ≤ t`
enters training at origin `t` only. ✅ Borderline case is sound: for the fold at
origin 2019-12-31, the newest trainable targets end at 2017-12 (fold-0 train
label range `2008-04:2017-12`, validation tail `2018-01:2019-12`; verified in
`regime/metrics.json` fold history).

**Preprocessing on training data only.** ✅ `transform_fit()`
(`approach2_regime.py:226-265`) computes, per fold, on the expanding **fit**
window only: (a) feature drops for >20 % missingness (`MISSING_DROP_FRAC`,
l.61) and zero-variance series; (b) imputation means; (c) z-score
standardization. The same statistics are then applied unchanged to the
validation tail and the single test window (l.248-257). Per-fold
`n_features_used` is recorded in fold history (19 of 30 in both first and last
folds; dropped set is identical across folds — verified in both
`regime/metrics.json` and `leakage_ablation/metrics.json`).
✅ Feature drop comes from genuine series missingness (e.g. stale `tsf_flow`)
rather than shift-edge effects; the leaky ablation arm retains the same kept
set (verified: same `dropped_missing_idx` list), preserving comparability.
✅ Early stopping and decision thresholds use a 24-month validation label tail
**inside** the training window (`VAL_TAIL`, l.60; applied l.369); no test-fold
information enters them (see §2).

**Label/target alignment.** ✅ `build_labels()` (`approach2_regime.py:167-169`)
implements `d_t = 1{pmi_man_t < thr and pmi_man_(t−1) < thr}` with NaN
masking. ✅ `month_end()` (`l.68-69`) stores forecast origins as ISO month-end
dates (`2019-12-31 … 2026-09-30`), `target_period` as `YYYY-MM`, and `fold` as
the target month string — matching the interface.md §6.2 prediction schema.
✅ `predictions.csv` contains 82 distinct folds and 27 realized positive labels
per model column (492 rows = 82 folds × 2 archs × 3 seeds; verified count).

## 2. Walk-forward evaluation

**Schedule.** ✅ Exactly the shared 82 folds: `TEST_TARGET_START=2020-01`,
`TEST_TARGET_END=2026-10` (l.58-59), origins `target−1`, one fold per target
month, expanding window; fold 0 = target `2020-01`, origin `2019-12-31`
(train labels `2008-04:2017-12`, 117 pairs; validated), last fold =
target `2026-10` (198 pairs). Claims − all read from `regime/metrics.json` fold
history.

**Training/prediction sample construction.** ✅ Sequences are
`x~_(u−h+1:u)` windows of the standardized shifted panel; lookback `h=3`
selected once (see below). The test window is the last sequence only; the
per-seed per-fold probabilities are stored in `predictions.csv` with full
schema (`model, forecast_origin, target_period, y_true, y_pred_proba, y_pred,
fold, seed`) per interface.md §6.2. ✅ Verified row counts (492) and fold/label
consistency between `y_true` in predictions and fold history.

**Same folds/labels for all models.** ✅ The ablation's probit and leaky arms
re-derive the identical schedule (`pd.period_range(TEST_TARGET_START,
TEST_TARGET_END)`, `code/approach2_leakage_ablation.py:121`) and additionally
**reuse the safe LSTM/GRU per-seed per-fold predictions verbatim** from
`results/approach2/regime/predictions.csv` (`leakage_ablation.py:236, 346-355`,
and `safe_arm_metrics` at l.190-209), so the safe arms are bit-identical to the
headline run and cannot diverge.
✅ The ablation's RNN hyperparameters
(`params_rnn`, l.239-242) match the regime CLI defaults exactly
(`approach2_regime.py:287-294`), so safe vs leaky arms differ only in the
feature panel (§5).

**No future information in model/hyperparameter selection.** ✅ Lookback `h`
is selected once on an inner validation block: validation targets
2018-01…2019-12, fit strictly before 2018-01
(`approach2_regime.py:334-353`; verified `h_selection` candidate scores
h=3: 0.1759, h=6: 0.1953, h=12: 0.2004 → h=3). In the 49.5 run the reverse
holds (h=12 marginally best: 0.0400 vs 0.0416/0.0406) — selected the same way,
never with test folds. ✅ Early stopping and thresholds per seed per fold use
only the 24-month validation tail (`train_one`, l.91-130; `pick_threshold`,
l.139-151). ✅ Probit ridge α is selected per fold by validation-tail Brier
from {0.1, 1, 10} + unpenalized (`leakage_ablation.py:97-113`; verified
selection counts: α=10 in 58 folds, α=1 in 22, α=0.1 in 2, no failures
remaining). ⚠ Minor: `pick_threshold` maximizes balanced accuracy, which
tends to push `y_pred` toward the minority class; this is a legitimate
validation-only choice but explains why raw accuracies fall below the naive
baseline's majority-class accuracy (§3).

## 3. Classification results (82 folds; segment data from `metrics.json`/`predictions.csv`)

**Primary threshold 50.**

| model | Brier | AUC | Accuracy | per-seed Brier (42 / 7 / 123) |
|---|---|---|---|---|
| naive prior-frequency baseline | 0.2551 | — | 0.6707 | deterministic |
| **probit (ablation run)** | 0.2330 | 0.7788 | 0.6585 | deterministic (seed null) |
| lstm (safe) | 0.2207 ± 0.0035 | 0.6157 ± 0.0055 | 0.4268 ± 0.0244 | 0.2237 / 0.2168 / 0.2215 |
| gru (safe) | 0.2386 ± 0.0088 | 0.5688 ± 0.0262 | 0.4390 ± 0.0762 | 0.2285 / 0.2447 / 0.2427 |
| lstm without lag discipline — **INVALID demo** | 0.1978 ± 0.0011 | 0.7100 ± 0.0124 | 0.5325 ± 0.0254 | 0.1978 / 0.1989 / 0.1966 |
| gru without lag discipline — **INVALID demo** | 0.2109 ± 0.0060 | 0.6510 ± 0.0067 | 0.5325 ± 0.0373 | 0.2055 / 0.2173 / 0.2098 |

- ✅ Seeds `42, 7, 123` (global default included); mean ± std includes all
  per-seed entries (verified in `regime/metrics.json` and
  `leakage_ablation/metrics.json`).
- **Brier vs AUC discrepancy (explanation, fact-checked):** both RNNs reduce
  Brier relative to the prior baseline (better-probability calibration on
  average) but have modest ranking power (AUC 0.57-0.62). The probit has the
  best **ranking** (AUC 0.7788) while its raw accuracy (0.6585) stays slightly
  below the 0.6707 majority baseline and its Brier (0.2330) is only
  marginally better than the naive baseline's 0.2551 because its
  probabilities are relatively flat/conservative.
  Brier rewards calibration, AUC rewards ranking; the safe RNN/GRU optimize
  loss + validation balanced accuracy, so their probability spread is better
  centered while their ordering is weaker.
- **49.5 sensitivity (same 82-fold schedule, labels rebuilt at 49.5):** h=12
  selected; lstm Brier 0.1384 ± 0.0020 (AUC 0.5949 ± 0.0046), gru 0.1467 ± 0.0065
  (AUC 0.5522 ± 0.0325), baseline Brier 0.1492 / accuracy 0.8415 (verified in
  `regime_threshold49_5/metrics.json`). Both RNNs beat the baseline on Brier
  here; accuracies still trail the majority-class baseline for lstm.
- ⚠ AUC of the safe RNNs is well below the assignment's indicative ≥0.75
  benchmark; only the safe probit reaches it. The group's report should treat
  "regime forecasting at one-month horizon from these monthly indicators is
  hard" as a finding rather than a code defect — but see the `pmi_man`
  inclusion question in §6.

## 4. Hidden-state clustering

- **Extraction.** ✅ For each of 3 seeds and each arch, a model is trained once
  at the clustering origin using the same leak-safe pipeline
  (`approach2_clustering.py:113-118`, reusing `reg.*`), then the last-layer
  final hidden state `h_t` (dim 32) is extracted for every month-end in
  `2008-03 … 2026-10` → 224 embeddings (all months embedded for every seed;
  the same standardized inputs, built once at l.120-136, so the embedding
  inputs are seed-invariant).
- **Clustering and k selection.** ✅ k-means (n_init=10, `random_state=seed`);
  k selected per arch by maximum mean silhouette across seeds over
  k ∈ {2,…,6}, each seed scored in its own embedding space (l.187-201;
  verified candidate curves are strictly decreasing in k for both archs →
  k=2). Silhouette (Euclidean) is a method-specific diagnostic; interface.md
  §9 Layer 1 explicitly forbids ranking it against Option A's indicator
  silhouettes.
- **Cross-seed ARI on common observations.** ✅ Pairwise ARI is computed
  between all seed pairs (`itertools.combinations`, l.217-221) with the
  common-observation mask and counts recorded. All counts are 224 (verified in
  `clustering/metrics.json`) because every seed embeds the identical 224
  months. ⚠ The mask is hard-coded all-true (l.215: `common_mask = ones`)
  rather than an intersection of per-seed valid observations. This is
  factually harmless here (identical inputs by construction; no months are
  dropped per seed), but the code does not *compute* the intersection — the
  interface's "common set" requirement is satisfied by construction rather
  than by an explicit intersect step.
- **Label alignment.** ✅ Explicitly two alignments, recorded separately
  (`label_alignment` in metrics.json): `d_t` = contemporaneous label of the
  embedding's window-end month (n=224); `d_(t+1)` = the one-month-ahead target
  of that window (n=223; last month's target undefined). ARI/NMI are
  computed per seed then averaged, and `latent_clusters.csv` stores both
  aligned label columns per row, so no implicit shifting occurs.
- **Verified results.** lstm (k=2): per-seed silhouette 0.686/0.676/0.561
  (mean 0.641 ± 0.070); pairwise cross-seed ARI 0.812 (42↔7), 0.054 (42↔123),
  0.042 (7↔123), mean 0.303; ARI vs `d_t` 0.131, NMI vs `d_t` 0.224; vs
  `d_(t+1)` 0.118/0.174. gru (k=2): silhouette 0.316/0.644/0.439 (mean
  0.466 ± 0.165); ARI 0.195 (42↔7), 0.628 (42↔123), 0.071 (7↔123), mean 0.298;
  vs labels ARI 0.090, NMI 0.194.
- **Stability/interpretation (factual).** Cross-seed stability is
  highly seed-dispersed, not uniformly stable: in each architecture exactly
  one of the three seed pairs reproduces a near-identical partition (LSTM
  42↔7 at ARI 0.81; GRU 42↔123 at 0.63) while the other two pairs disagree
  sharply (ARI ≤ 0.20). Interpretation: for seed 42 the lstm k=2 split is
  economically interpretable — one state (n=108) contains 42.6 % contraction
  months, the other (n=116) 0.9 % (recorded as `contraction_share_by_cluster`,
  `reference_seed_for_shares=42`; verified). Overall label association is
  real but moderate (NMI ≈ 0.19-0.22).

## 5. Leakage ablation

- **Information-set difference.** ✅ The only code-level difference is
  `make_bundle(..., use_shifted)` (`leakage_ablation.py:173-187`): the safe
  arms use the per-indicator lag-shifted panel; the leaky arms
  (`leakage_ablation.py:183`, `shifted=idx` when `use_shifted=False`) use the
  RAW panel — every series is treated as published at its reference month-end,
  so at origin month `t` the window uses `x_(i,t)` for every series, i.e.
  `s = t` with `s + L_i > t` for the 21 series with `L ≥ 1`.
  This violates the grading information-set rule directly; note it is not
  direct label peeking (the label `d_(t+1)` uses PMI_{t+1}, which the leaky
  windows still do not contain — PMI has `L=0` and legitimately appears at the
  final window row in both arms). The demonstrated premium is
  availability leakage of unpublished contemporaneous macro data, exactly the
  red-lined violation.
- **Everything else held fixed (verified in code).** ✅ Same 82 folds and
  origins (l.121), same label/`pairs_for_train` construction, same `h=3`,
  same training loop/import (`reg.train_one`), same seeds, same imputation/
  standardization/drop rules, same kept-feature set (19 features,
  identical `dropped_missing_idx`; verified in fold history for both arms),
  same validation tail. The safe RNN arm is the headline run's own output
  (not refitted), eliminating run-to-run drift.
- **Result summary (verified):** leaky lstm Brier 0.1978 vs safe 0.2207
  (gap −0.0229); leaky gru 0.2109 vs safe 0.2386 (gap −0.0277); AUC leaky
  lstm 0.7100 vs 0.6157 (+0.0943), gru 0.6510 vs 0.5688 (+0.0822)
  (`leakage_premium`, `leakage_ablation.py:359-373`).
- **INVALID label discipline.** ✅ `leakage_discipline:
  "intentional-leakage-demo"` vs `"safe"` fields on every arm;
  figure titles state "INVALID/NOT valid models"; the leaky-only trace names
  carry the label; `predictions.csv` has a `leakage_discipline` column.
  Factual notes for the group's report exist at
  `results/approach2/leakage_ablation/NOTES.md`.
- ⚠ Caveat for interpretation: because leaky models additionally see
  unpublished month-`t` macro data for 21 series, and the safe probit
  independently reaches AUC 0.7788 on the same folds, the leaky RNNs do **not**
  dominate all safe models — the premium is a within-architecture
  comparison, as `NOTES.md` states.

## 6. Potential issues

**Confirmed implementation defects — BOTH RESOLVED (2026-11-07).**

1. ✅ **RESOLVED:** `ablation_summary.html` was partially broken. The summary
   figure read per-arm values with `mm.get("brier")` / `mm.get("auc")` at top
   level, but model entries nest these under `"mean"`
   (`leakage_ablation.py:~433-443`). Consequence was that only the baseline bar
   and no per-model Brier bars/AUC points appeared. Fixed: code now reads
   `mm["mean"]` for model arms (flat dict for the baseline) and colors bars by
   `leakage_discipline`; leaky arm labels carry the suffix "— INVALID leakage
   demo". The figure was regenerated from the stored `metrics.json` only
   (no training rerun) and verified to contain all six arms with full-precision
   values (baseline 0.2551, probit 0.2330/AUC 0.7788, lstm_safe 0.2207,
   gru_safe 0.2386, lstm_leaky 0.1978 — INVALID, gru_leaky 0.2109 — INVALID;
   leaky bars in red #b23b3b, safe bars in green #2a6041).
2. ✅ **RESOLVED:** Documentation-vs-code mismatch in the clustering script.
   The docstring and `params.training` text claimed the clustering origin is
   2026-09-30 with "labels d_(u+1) ≤ 2026-09", but the code sets
   `final_origin = bundle.months[-2]` = 2026-10 (`approach2_clustering.py:114`)
   and therefore trains on targets through `d_2026-10` and embeds months
   through 2026-10 (`embedding_origins` in metrics.json reads
   `2008-03:2026-10`; verified). Methodologically this stays leak-safe
   (descriptive clustering; the 2026-10 realized label is legitimately
   observable at month-end 2026-10 and no test-period forecasting occurs).
   Fixed: the docstring, the inline comment, the `params.training` string
   produced by the script, and the stored `params.training` text in
   `results/approach2/clustering/metrics.json` (string-only edit; `metrics`
   and `history` dicts verified byte-identical) now all document origin
   2026-10 consistently. The actual clustering origin and training sample are
   unchanged.

**Verified facts, noted for the reviewer.**

3. ✅ Probit probabilities are not explicitly clipped in the returned test
   values (only val-Brier computation clips,
   `leakage_ablation.py:102` vs `:113`), although `params` text says
   "probabilities clipped". Probit link outputs are inherently in [0,1], so
   this is a text inaccuracy, not a correctness issue.
4. ✅ The one-shot "pooled probit" (all window rows pooled, mixing lead times)
   was tested in-development and rejected (AUC 0.39, perfect-separation
   instability); the implemented arm is the origin-row probit. The choice is
   documented in `NOTES.md`.
5. ✅ Recurrent nondeterminism on CPU (last-bit float variation) is documented
   in every `metrics.json` `params.residual_nondeterminism`.
6. ✅ `pos_weight` for BCE is capped at 1.0 by default (`pos_weight_cap`,
   `approach2_regime.py:101-103`, CLI l.284-285): class imbalance is handled
   primarily by validation-tail threshold selection, not loss weighting —
   declared and recorded in `params.class_imbalance`.
7. ✅ `TRAIN_LABEL_START` (`approach2_regime.py:57`) is declared but only used
   in text; the actual earliest trainable target derives from label
   availability (2008-04 for h=3, fold 0 verified). Harmless but slightly
   misleading constant name.

**Potential methodological concerns (not errors).**

8. ⚠ **`pmi_man` is among the 30 input features** (lag 0, documented as
   legitimate: released month-end `t`, the origin). Since `d_(t+1)` partially
   equals `1{PMI_t < thr}`, the regime models necessarily condition on half
   the label. This is permitted by the shared interface (all 30 panel columns
   are the approved candidates; `feature_names_used` records inclusion), but
   the reviewer should register that a large part of the classification task
   information is the label's own first PMI observation.
9. ⚠ **Per-seed `y_pred` threshold selection** is noisy with only 24
    validation labels (some tails have few positives), contributing to
    accuracy below the majority baseline. Balanced-accuracy thresholding
    across class-imbalance arithmetic is a deliberate design, not a bug.
10. ⚠ **Single split-type**: expanding-window starts all folds from 2008;
    robustness to different cold starts was not tested.
11. ⚠ **k-means silhouette on the full embedded sample** — the clustering is
    descriptive (no train/test split), which is appropriate for its use, and
    matches how interface.md frames Layer-1 diagnostics.
12. ⚠ **Probit feature count vs sample size** (19 features over ~117-198
    training origins) with ridge penalty as mitigation — the α-selection
    counts (10 in 58/82 folds) indicate the unpenalized fit was rarely
    chosen.

**Unresolved questions for the group.**

13. ❓ Whether the "probit arm" in the ablation should be aligned with
    Option A's factor-probit specification (factors + spread controls) or the
    raw-features probit used here (documented in `params.safe_probit`).
    The current arm uses the approved panel directly and is a valid
    classical baseline; alignment with Option A may improve headline
    comparability.
14. ❓ ~~The clustering `params.training` text discrepancy (item 2)~~
    **Resolved 2026-11-07:** intended origin is 2026-10 (last full panel
    month-end), now documented consistently in code and in the stored
    `params.training`.
15. ❓ Whether cross-method Layer-2/3 interpretation (interface.md §9) uses
    `d_t` or `d_(t+1)` alignment as the primary lens — both are computed and
    recorded; the group's report should state its choice.

## 6b. Explicit methodological limitations (documented 2026-11-07)

These limitations are intentional, verified properties of the frozen design —
not defects. They must be reflected honestly in the group's report.

1. **PMI feature overlap with the target label.** `pmi_man` (lag 0, released at
   month-end of its reference month) is included as an input feature. The
   target `d_(t+1) = 1{PMI_(t+1) < thr and PMI_t < thr}` contains two PMI
   observations; at forecast origin `t` the model already observes `PMI_t`,
   i.e. one of the two conditions of the label. **This is not future-information
   leakage** — `PMI_t` is in the information set `F_t` by the shared lag rule —
   but it means the effective task is the *conditional* prediction of whether
   PMI stays below the threshold in the following month. `pmi_man` was not
   removed from the models and the headline results are unchanged; the group's
   report should state the task in this conditional form.
2. **Threshold-selection uncertainty.** The per-seed decision threshold is
   selected on a 24-month validation tail only. Small validation samples (and
   tails with few positives) make threshold selection noisy, which contributes
   to the safe RNNs' accuracy falling below the majority-class baseline.
   Thresholds were **not** retuned using test-period results; they should not
   be re-tuned on test results in any future revision.
3. **Clustering stability.** Cross-seed ARI on the same 224 common months is
   highly dispersed (LSTM pairs: 0.812 / 0.054 / 0.042 → mean 0.303; GRU pairs:
   0.195 / 0.628 / 0.071 → mean 0.298). The two latent clusters therefore do
   **not** constitute robust, stable economic regimes; the hidden-state
   clustering is exploratory and initialization-sensitive. It should be
   presented as a descriptive diagnostic, with the seed-42 LSTM split
   (n=108 state with 42.6 % contraction months vs n=116 state with 0.9 %) as
   an illustrative single-seed interpretation.
4. **Classification performance (Brier vs AUC).** The safe LSTM has the best
   Brier score (0.2207) while the safe Probit has the best AUC (0.7788) and the
   only accuracy near the majority baseline. **No model is superior on all
   metrics**, and this is retained as an honest finding: Brier rewards
   probability calibration, AUC rewards ranking power. No claim that LSTM/GRU
   dominates Probit (or vice versa) is supported.
5. **Probit comparability.** The ablation's probit arm is a *raw-feature,
   origin-row ridge probit* (ridge α selected per fold on the validation tail),
   not Option A's factor-probit (factors + spread controls). It was **not**
   silently replaced by the factor specification; the final cross-method
   specification choice (raw-feature vs factor-based probit) is left to the
   team. A one-shot pooled-row probit was tested in development and rejected
   (AUC 0.39, perfect-separation instability); see `NOTES.md`.

## 7. Output inventory (all under `results/approach2/`)

**`regime/`** — `metrics.json` (params incl. seeds & h selection; per-seed and
pooled metrics; 82-fold history with per-fold audit), `predictions.csv` (492
rows, interface §6.2 schema), `figures/walkforward_probabilities.html`,
`figures/walkforward_brier.html`.

**`regime_threshold49_5/`** — same structure (49.5 sensitivity).

**`leakage_ablation/`** — `metrics.json` (all five arms + leakage premium;
`leakage_discipline` field per arm; fold history for probit and leaky arms),
`predictions.csv` (82 probit rows, 246 leaky-lstm rows, 246 leaky-gru rows,
492 safe-RNN rows, with `leakage_discipline` column), figures
`leakage_demo_probabilities.html`, `ablation_brier.html`,
`ablation_summary.html` (⚠ see §6 item 1), `run_log.txt`, `NOTES.md` (factual
notes for the group's report).

**`clustering/`** — `metrics.json` (k selection curves; per-seed silhouette;
9 pairwise ARIs with common counts; two explicit label alignments with
per-cluster contraction shares), `latent_clusters.csv` (per seed × 224 months:
`forecast_origin`, `reference_period`, `target_period`, `latent_cluster_id`,
both label alignments), `latent_embeddings.csv` (32-dim embeddings per seed ×
month; reproducibility artifact), figures `latent_lstm_pca.html`,
`latent_lstm_timeline.html`, `latent_gru_pca.html`, `latent_gru_timeline.html`
(seed-42 view).

**Scripts** — `code/approach2_regime.py`, `code/approach2_clustering.py`,
`code/approach2_leakage_ablation.py` (run commands documented in `AGENTS.md`
§2). `code/compare.py` and the cross-method `results/comparison.html` remain
Option A/shared deliverables (out of scope here).

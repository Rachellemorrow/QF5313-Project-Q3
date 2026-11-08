# Option B — Sequence Learning: Deep-Learning Regime Classification and Latent-State Clustering

**Draft for the group's `report.md` (Option B section). Prepared 2026-11-08 from the
implemented code and stored result files; every number below was verified against the
stored `metrics.json` / `predictions.csv` outputs. Interpretation and final wording are
the group's responsibility.**

**Task.** One-month-ahead classification of the Chinese manufacturing-PMI contraction
regime. At a month-end forecast origin `t`, each model produces a probability that the
PMI-based contraction label is active in month `t+1`. Option B additionally explores
whether the latent states of the trained sequence models — their last-layer hidden
states — form interpretable macroeconomic phases, and it runs the mandatory leakage
ablation demonstrating the value of publication-lag discipline.

The frozen information-set rule shared by Options A and B is
`F_t = { x_(i,s) : s + L_i <= t }`: at forecast origin `t`, an observation of indicator
`i` from reference month `s` is admissible only once its publication lag `L_i` has
elapsed. GDP nowcasting is Option A's task and is out of scope here.

---

## 1. Methodology

### 1.1 Target definition

The primary regime label uses the frozen NBS manufacturing PMI series (`pmi_man`):

```
d_t = 1{ pmi_man_t < 50  and  pmi_man_(t-1) < 50 }
```

so the contraction regime is declared only after **two consecutive months** of PMI
below the 50 threshold. A prediction issued at month-end forecast origin `t` targets the
one-month-ahead label `d_(t+1)`. A sensitivity run rebuilds the entire label series with
the threshold `49.5` (Section 2.4). Both label variants follow the shared
`interface.md` §5.1 definition, and the label-availability rule is respected: a
historical label may enter training only once both PMI observations used to construct it
would have been published.

### 1.2 Information-set and publication-lag discipline

The lag-shifted predictor panel is constructed from `data/clean/macro_panel.csv`
(30 indicators) using the per-indicator integer `publication_lag_months` recorded in
`data/metadata.json`: each series is shifted forward by its own lag,
`shifted_i(s) = x_i(s − L_i)`, so a row of the shifted panel at month-end `s` contains
exactly the observations available to a forecaster at `s`. Lags are series-specific —
9 series are treated as published at their reference month-end (`L = 0`, including both
PMIs, the LPR and Shibor series), 20 series have `L = 1`, and `consumer_confidence` uses
a conservative `L = 2`. Forecasts use windows `x~_(t−h+1 … t)` of the standardized,
lag-shifted panel; nothing from months after `t` enters a forecast at origin `t`.

Preprocessing is refit per walk-forward fold on the expanding **fit window only**:
(i) series with more than 20% missing observations on the fit window, or zero variance,
are dropped; (ii) imputation means; and (iii) z-score standardization statistics — all
computed from the fit window and then applied unchanged to the validation tail and the
test month. In the headline run, 19 of 30 input series were retained; the dropped set
(`missingness-driven`) was identical across all folds and is recorded in the fold-level
history of `results/approach2/regime/metrics.json`. Early stopping and the decision
threshold use a **24-month validation tail inside the training window**; test-fold
information never enters model selection or threshold selection. The `2026-11`
partial-month panel row (PMI unavailable, partial-month Shibor values) is excluded from
every experiment, per shared contract.

### 1.3 Expanding walk-forward evaluation (shared 82-fold schedule)

Both architectures follow the fixed shared schedule (`interface.md` §5.1):

| Schedule element            | Value                                                           |
| --------------------------- | --------------------------------------------------------------- |
| Initial historical labels   | 2008-02 … 2019-12 (evolving with the expanding window)         |
| Out-of-sample target months | 2020-01 … 2026-10 (**82 folds**, fold id = target month) |
| Forecast origins            | month-end 2019-12-31 … 2026-09-30                              |
| Horizon                     | one month ahead (`d_(t+1)`)                                   |
| Evaluation labels           | 82 target months: 27 contraction, 55 non-contraction            |

At the first fold (target 2020-01, origin 2019-12-31) the expanding window contains
**141 historical training pairs** — pairs of (lag-shifted input window ending at month
`u`, target `d_(u+1)`) covering targets `d_2008-04` … `d_2019-12` — split into
**117 fit-training observations** (targets 2008-04:2017-12) and a **24-month validation
tail** (targets 2018-01:2019-12). The 117 count refers to fit-training observations
only: gradient updates are taken from these 117 windows, the per-fold imputation,
standardization and feature-drop statistics are estimated on the same fit rows
(`pairs_for_train` / `make_arrays` / `transform_fit`, `code/approach2_regime.py:202-265`,
split at l.367-370), while early stopping and the decision threshold use the 24
validation observations exclusively, and the prior-frequency baseline is the mean of the
117 fit-training targets. The remaining two labels of the interface's 143-month initial
label window (2008-02…2019-12), `d_2008-02` and `d_2008-03`, are not trainable as
sequence targets at h = 3 because their input windows would extend before the panel's
first month (2008-01). By the final fold (target 2026-10) the expanding window contains
**222 historical training pairs**: 198 fit-training observations and the same 24-month
validation tail. Option A's Probit and Option B's LSTM/GRU use the same target months,
folds, and realized labels, so headline metrics are computed on identical observations.

**Lookback selection.** The window length `h` was selected once, per label threshold,
by mean Brier score across seeds on **inner** validation targets 2018-01…2019-12 (fit
strictly before 2018-01 — no test-fold information):

- Threshold 50: h = 3 (score 0.1759) versus h = 6 (0.1953) and h = 12 (0.2004).
- Threshold 49.5: h = 12 (score 0.0400) versus h = 3 (0.0416) and h = 6 (0.0406).

The selected `h` is a model *input* — the effective task information at the origin is
still bounded by the lag-shift of month `t`, since earlier window rows simply contain
older (still-admissible) observations.

### 1.4 LSTM/GRU regime classifiers

Both architectures are two-layer stacked recurrent networks (hidden size 32, dropout
0.2), trained with binary cross-entropy on the classification task
`P(d_(t+1) = 1 | x~_(t−h+1 … t))`:

| Hyperparameter                           | Value                                                                                                             |
| ---------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Architecture / layers / hidden / dropout | LSTM and GRU / 2 / 32 / 0.2                                                                                       |
| Optimizer settings                       | lr 0.001, weight decay 0.0001, batch size 32                                                                      |
| Epochs / early stopping                  | max 200, patience 20 on the 24-month validation tail                                                              |
| Class-imbalance handling                 | decision threshold maximizing balanced accuracy on the validation tail; optional BCE `pos_weight` capped at 1.0 |
| Random seeds                             | 42, 7, 123 (global default 42 included)                                                                           |

Recall that `d_(t+1) = 1{PMI_(t+1) < thr and PMI_t < thr}`. Because `pmi_man` has
`L = 0`, the model legitimately observes `PMI_t` at the origin; the learning problem is
therefore the **conditional** prediction of whether PMI stays below the threshold for a
second consecutive month. This feature–target relationship is discussed as a limitation
in Section 5 and is not a leakage defect: `PMI_t ∈ F_t` by the shared lag rule
(verified for all lags in `data/metadata.json`).

### 1.5 Classical comparator

The leakage-ablation script (`code/approach2_leakage_ablation.py`) provides the
classical arm required for the three-way ablation comparison: a **raw-feature,
origin-row ridge probit** (statsmodels binomial GLM with probit link) estimated on the
standardized final-window row `z_u = F_u`, i.e. on the approved panel directly rather
than on Option A's factor representation. Ridge strength α is selected per fold, on the
24-month validation tail only, from {unpenalized, 0.1, 1, 10} (selected: α = 10 in 58/82
folds, α = 1 in 22, α = 0.1 in 2). The arm is deterministic (seed `null`). A pooled
specification that mixes window rows with different lead times was tested during
development and **rejected** (AUC 0.39, unstable perfect-separation fits); the origin-row
design is the implemented specification. Under the frozen interface, regime
classifiers must share the same classification targets, forecast origins, folds, labels,
and evaluation observations (`interface.md` §5.1/§5.3) — it does not require identical
internal model specifications — so this raw-feature probit is a valid comparator on the
identical folds, and it coexists with Option A's independently specified factor-probit
as a separately reported classical model over the same schedule.

### 1.6 Hidden-state extraction and k-means clustering

After the regime classifiers are trained, the trained models are reused (same
lag-shifted panel, same fit-window-only preprocessing, same `h = 3`) to extract the
**final-layer hidden state** `h_t` (dimension 32) at every month-end `t` in
2008-03 … 2026-10 → **224 time observations**. The descriptive training origin is the
last full panel month-end, **2026-10-31**, so training uses only label pairs observable
then (training targets up to `d_2026-10`) and the partial 2026-11 row is excluded.
`random seed` is preserved throughout: seeds 42 / 7 / 123 each produce their own model,
their own embedding space, and their own cluster assignments over the **same** 224 time
observations (no month is dropped per seed).

K-means (`n_init = 10`, `random_state = seed`) is applied **within each seed's own
embedding space**; the candidate grid is k ∈ {2,…,6} and k is selected per architecture
by the maximum **mean silhouette across seeds**, each seed scored in its own space.
Cluster association with the PMI contraction labels is evaluated under two explicit,
separately recorded alignments (`interface.md` §6.4):

- `d_t` — the contemporaneous label of the embedding's window-end month (n = 224);
- `d_(t+1)` — the one-month-ahead target of that window, i.e. the regime-forecast
  target the model was trained on (n = 223; the final month's target is undefined).

ARI/NMI are computed per seed and averaged; the per-row file
`results/approach2/clustering/latent_clusters.csv` stores the cluster id and **both**
aligned label columns per (seed, month), so no implicit label shifting occurs.

---

## 2. Regime Classification Results

All results are on the identical 82 out-of-sample target months (27 contraction / 55
non-contraction). Deep-learning entries are mean ± standard deviation across the three
seeds; individual per-seed values are quoted beneath each mean. The recurrent LSTM/GRU
training is deterministic up to documented last-bit CPU floating-point variation (fixed
seeds control initialization and data order).

### 2.1 Headline results (threshold 50, h = 3)

| Model                                    | Brier ↓                   | AUC ↑           | Accuracy         | Per-seed Brier (42 / 7 / 123) |
| ---------------------------------------- | -------------------------- | ---------------- | ---------------- | ----------------------------- |
| Naive prior-frequency baseline           | 0.2551                     | —               | 0.6707           | deterministic                 |
| Probit (ridge, origin-row, raw features) | 0.2330                     | **0.7788** | 0.6585           | deterministic                 |
| LSTM (safe)                              | **0.2207 ± 0.0035** | 0.6157 ± 0.0055 | 0.4268 ± 0.0244 | 0.2237 / 0.2168 / 0.2215      |
| GRU (safe)                               | 0.2386 ± 0.0088           | 0.5688 ± 0.0262 | 0.4390 ± 0.0762 | 0.2285 / 0.2447 / 0.2427      |

Sources: `results/approach2/regime/metrics.json` (LSTM/GRU + baseline),
`results/approach2/leakage_ablation/metrics.json` (probit arm, identical folds).
Per-seed LSTM AUC: 0.6195 (42) / 0.6182 (7) / 0.6094 (123); GRU per-seed AUC: 0.5811 /
0.5865 / 0.5387. Seed dispersion of the Brier score is small (LSTM std 0.0035), while
GRU accuracy is more seed-sensitive (std 0.0762).

**Interpretation — probability quality versus ranking.** The three metrics answer
different questions. The **Brier score** is an overall probabilistic prediction score:
the mean squared error between the forecast probability and the realized 0/1 label. It
penalizes squared probability errors on every fold — whether the forecast was too high
or too low — and therefore rewards forecasts whose probabilities are, on the whole, close
to the outcomes; it does not separately credit calibration or ranking power. The LSTM's
mean Brier advantage over the naive baseline (0.2207 vs 0.2551) means its out-of-sample
probabilities commit smaller average squared errors over the 82 folds than the constant
train-window prior. **AUC** rewards pure ranking across observation pairs and is
threshold-free: on this criterion the **probit is clearly the strongest model (0.7788)**, while the safe
sequence models have only modest discrimination (LSTM 0.6157, GRU 0.5688). **Accuracy**
depends on the operating point: the decision thresholds were tuned for balanced accuracy
on the validation tail, not for majority-class hit rate, so the safe RNNs' raw
accuracies (0.43–0.44) sit below the baseline's 0.6707 majority-class accuracy — a
deliberate design consequence, not a metric failure. Synthesis: **no model is superior
on all metrics**. The probit ranks contractions best and nearly matches the majority
baseline's accuracy; the LSTM attains the lowest overall Brier score of the safe models,
meaning the smallest average squared probability error on this evaluation sample, which
is not the same as best calibration, best ranking, or best classifications. We do
not claim forecasting superiority of any single model on all three metrics. The modest
AUC levels of the safe sequence models (0.57–0.62, below the assignment's indicative
0.75 benchmark) indicate that one-month-ahead binary contraction prediction from these
monthly indicators is a genuinely hard problem; Section 4 shows that a large part of the
apparent "improvement" observed under weaker discipline would come precisely from
lifting this information constraint.

Inline evaluation trace: per-fold probabilities and cumulative Brier paths are in
`results/approach2/regime/predictions.csv` (492 rows = 82 folds × 2 architectures ×
3 seeds, `interface.md` §6.2 schema, seed preserved per row) and plotted in
`results/approach2/regime/figures/walkforward_probabilities.html` (fold-wise predicted
probabilities versus realized labels) and
`results/approach2/regime/figures/walkforward_brier.html` (cumulative walk-forward
Brier by fold).

### 2.2 Baseline

The required naive benchmark is the prior-frequency baseline: the train-window mean of
the target label, carried forward as a constant probability. Its out-of-sample Brier is
0.2551 with accuracy 0.6707 (= the share of non-contraction months). Both headline safe
recurrent models record lower mean out-of-sample Brier scores than this naive baseline
(LSTM 0.2207, GRU 0.2386), and the same holds for both recurrent models in the 49.5
sensitivity run (LSTM 0.1384, GRU 0.1467 vs that run's baseline 0.1492). None of the
safe sequence models beats its majority-class accuracy, by construction of the
balanced-accuracy threshold.

### 2.3 Sensitivity to the label threshold (49.5)

The 49.5 sensitivity run rebuilds all labels at the stricter threshold on the **same
calendar schedule** (`results/approach2/regime_threshold49_5/`); the lookback is
re-selected the same way, now choosing h = 12.

| Model                          | Brier ↓                   | AUC ↑           | Accuracy         | Per-seed Brier (42 / 7 / 123) |
| ------------------------------ | -------------------------- | ---------------- | ---------------- | ----------------------------- |
| Naive prior-frequency baseline | 0.1492                     | —               | 0.8415           | deterministic                 |
| LSTM (safe)                    | **0.1384 ± 0.0020** | 0.5949 ± 0.0046 | 0.4837 ± 0.0715 | 0.1373 / 0.1408 / 0.1372      |
| GRU (safe)                     | 0.1467 ± 0.0065           | 0.5522 ± 0.0325 | 0.5569 ± 0.0508 | 0.1395 / 0.1521 / 0.1487      |

(Per-seed LSTM AUC: 0.5987 / 0.5964 / 0.5897; GRU per-seed AUC: 0.5864 / 0.5217 /
0.5485.) Under the stricter 49.5 definition contractions are rarer (the baseline
majority accuracy rises to 0.8415), and **both** safe sequence models show lower Brier
scores than the baseline (LSTM by 0.0108, GRU by 0.0025 — small differences relative to
the noise documented in Section 5.2), while ranking power is essentially unchanged. The corresponding inline figures are
`results/approach2/regime_threshold49_5/figures/walkforward_probabilities.html` and
`...\walkforward_brier.html`. At this threshold the inner-validation Brier score
favored the longer lookback (h = 12, score 0.0400, versus 0.0406 for h = 6 and 0.0416
for h = 3); this is reported solely as the data-driven model-selection outcome of this
run and no economic inference about spell persistence is drawn from it.

---

## 3. Hidden-State Clustering Results

### 3.1 Number of clusters and selection

K-means was run for k ∈ {2,…,6}; k was selected per architecture by the maximum mean
silhouette across the three seeds (each seed scored in its own embedding space).
The mean-silhouette profiles are strictly decreasing in k for the LSTM, and nearly so
for the GRU:

| Architecture | k = 2            | k = 3  | k = 4  | k = 5  | k = 6  | Selected |
| ------------ | ---------------- | ------ | ------ | ------ | ------ | -------- |
| LSTM         | **0.6410** | 0.5609 | 0.5106 | 0.4786 | 0.4330 | k = 2    |
| GRU          | **0.4662** | 0.3720 | 0.3757 | 0.3509 | 0.3372 | k = 2    |

Source: `results/approach2/clustering/metrics.json` (`history.k_selection`).

### 3.2 Method-specific quality and stability (Layer-1 diagnostics)

These silhouette values are **method-specific diagnostics of Option B's learned
embedding spaces** (time observations in a 32-dimensional hidden-state space). They are
not comparable — and are not ranked — against Option A's indicator-space silhouettes,
whose observation units are indicators under a correlation-based distance
(`interface.md` §7/§9). Cross-method comparison follows the three-layer framework;
direct cross-method ARI/NMI between indicator clusters and time-point clusters is not
computed anywhere in Option B.

| Statistic                                                 | LSTM                                              | GRU                                               |
| --------------------------------------------------------- | ------------------------------------------------- | ------------------------------------------------- |
| Per-seed silhouette (42 / 7 / 123)                        | 0.6862 / 0.6759 / 0.5609                          | 0.3164 / 0.6435 / 0.4385                          |
| Silhouette mean ± std                                    | 0.6410 ± 0.0696                                  | 0.4662 ± 0.1653                                  |
| Pairwise cross-seed ARI                                   | 0.8124 (42↔7); 0.0541 (42↔123); 0.0417 (7↔123) | 0.1952 (42↔7); 0.6284 (42↔123); 0.0711 (7↔123) |
| Mean pairwise ARI (all on the same 224 time observations) | 0.3027                                            | 0.2982                                            |

All pairwise comparisons use the identical common time-observation set: every seed
embeds the same 224 months (2008-03 … 2026-10), and the per-pair common counts are
recorded as n_common = 224 in `results/approach2/clustering/metrics.json`.

**Stability reading (stated deliberately).** The cross-seed ARI values are highly
dispersed: in each architecture exactly **one** of the three seed pairs reproduces a
near-identical partition (LSTM 42↔7 at ARI 0.812; GRU 42↔123 at 0.628), while the other
two pairs disagree sharply (ARI ≤ 0.20). The two latent clusters are therefore
**exploratory and initialization-sensitive, not robust, stable macroeconomic regimes**.
Reported mean ARIs of ≈ 0.30 reflect strong disagreement between plausible k = 2
partitions of the same embedding space. This should be presented in the final report as
a limitation of latent-state clustering under k-means, not as evidence of instability in
the regime *classification* results.

### 3.3 Alignment with PMI contraction labels

Both alignments are computed and stored separately (`interface.md` §6.4); no label
shifting is implicit anywhere. ARI/NMI are averaged over seeds; `n` is the number of
evaluated time observations.

| Architecture | Alignment                                     | n   | ARI    | NMI    |
| ------------ | --------------------------------------------- | --- | ------ | ------ |
| LSTM         | `d_t` (contemporaneous window-end label)    | 224 | 0.1312 | 0.2237 |
| LSTM         | `d_(t+1)` (one-month-ahead forecast target) | 223 | 0.1178 | 0.1743 |
| GRU          | `d_t`                                       | 224 | 0.0896 | 0.1939 |
| GRU          | `d_(t+1)`                                   | 223 | 0.0795 | 0.1416 |

The association between latent states and contraction labels is **real but moderate**
(NMI ≈ 0.14–0.22). Whether the group's cross-method Layer-2/3 narrative is built on the
contemporaneous (`d_t`) or the one-month-ahead (`d_(t+1)`) alignment is an unresolved
team decision; both lenses are fully computed and recorded, and neither is implied by
the pipeline.

### 3.4 Economic interpretation (illustrative, single-seed)

For the reference seed 42 the LSTM's k = 2 split aligns with an economically
interpretable contraction/non-contraction phase separation: one latent state
(n = 108 months) contains **42.6 %** contraction (`d_t = 1`) months, while the other
(n = 116) contains **0.9 %**. The GRU's seed-42 split is directionally similar but
noisier (one state of n = 88 months with 2.3 % contraction; the other of n = 136 months
with 33.1 %). These shares are recorded per seed and per alignment in
`results/approach2/clustering/metrics.json` (`contraction_share_by_cluster`,
`reference_seed_for_shares = 42`), and the per-row assignments are in
`results/approach2/clustering/latent_clusters.csv`. Given the cross-seed dispersion
documented above, we present this state split as an **illustrative interpretation of one
initialization**, not as a validated labeling of Chinese macroeconomic regimes; the
moderate NMI (≈ 0.20) confirms that the latent geometry carries contraction-relevant
information without recovering the labels.

Exploratory visualizations (seed-42 views): PCA scatter and time-line charts of the
latent assignments,
`results/approach2/clustering/figures/latent_lstm_pca.html`,
`latent_lstm_timeline.html`, `latent_gru_pca.html`, `latent_gru_timeline.html`. The
32-dimensional embeddings for reproducibility are in
`results/approach2/clustering/latent_embeddings.csv`.

---

## 4. Leakage Ablation (mandatory)

### 4.1 Design and information sets

The mandatory ablation (`code/approach2_leakage_ablation.py`, 82 identical folds)
compares three arms under the same labels, folds, seeds, architectures, training
pipeline, lookback h = 3, preprocessing rules, and 24-month validation tails:

1. **Probit (safe)** — the classical comparator of Section 1.5;
2. **LSTM/GRU (safe)** — the headline run's per-seed, per-fold predictions **reused
   verbatim** from `results/approach2/regime/predictions.csv`, so the safe arms are
   bit-identical to the headline run and cannot diverge in fit;
3. **LSTM/GRU without lag discipline** — identical architecture and training, but each
   indicator window is taken from the **raw unshifted panel** — as if every series were
   published at its own reference month-end. For the 21 of 30 series with
   `L ≥ 1`, this feeds the origin row observations that a real-time forecaster could
   not yet have (`s = t` with `s + L_i > t`), directly violating the grading
   information-set rule `F_t = {x_(i,s) : s + L_i ≤ t}`.

Note what the leaky window does *not* contain: the label `d_(t+1)` uses `PMI_(t+1)`,
which is not in any feature window (PMI has `L = 0`). The demonstration therefore
isolates **availability leakage of unpublished contemporaneous macro data** — the exact
red-line violation — rather than direct label peeking.

Every leaky artifact is labelled everywhere: filenames and figure titles carry the
"without lag discipline / INVALID" wording, the metrics field is
`"leakage_discipline": "intentional-leakage-demo"` (versus `"safe"`), and the row-level
file `results/approach2/leakage_ablation/predictions.csv` (1,066 rows: 82 probit +
492 safe-RNN + 492 leaky-RNN) repeats the label column per row. Factual notes are in
`results/approach2/leakage_ablation/NOTES.md`.

### 4.2 Results (82 folds; mean ± std across seeds where applicable)

| Arm                                   | `leakage_discipline`                                           | Brier ↓         | AUC ↑           | Accuracy         | Per-seed Brier (42 / 7 / 123) |
| ------------------------------------- | ---------------------------------------------------------------- | ---------------- | ---------------- | ---------------- | ----------------------------- |
| Naive prior-frequency baseline        | safe                                                             | 0.2551           | —               | 0.6707           | deterministic                 |
| Probit (ridge, origin-row)            | safe                                                             | 0.2330           | **0.7788** | 0.6585           | deterministic                 |
| LSTM (safe)                           | safe                                                             | 0.2207 ± 0.0035 | 0.6157 ± 0.0055 | 0.4268 ± 0.0244 | 0.2237 / 0.2168 / 0.2215      |
| GRU (safe)                            | safe                                                             | 0.2386 ± 0.0088 | 0.5688 ± 0.0262 | 0.4390 ± 0.0762 | 0.2285 / 0.2447 / 0.2427      |
| LSTM **without lag discipline** | **intentional-leakage-demo — INVALID, not a valid model** | 0.1978 ± 0.0011 | 0.7100 ± 0.0124 | 0.5325 ± 0.0254 | 0.1978 / 0.1989 / 0.1966      |
| GRU **without lag discipline**  | **intentional-leakage-demo — INVALID, not a valid model** | 0.2109 ± 0.0060 | 0.6510 ± 0.0067 | 0.5325 ± 0.0373 | 0.2055 / 0.2173 / 0.2098      |

Recorded leakage premium (leaky minus safe on identical folds; positive Brier gap means
the leaky arm merely *looked* better because of information-set violations):

| Architecture | Brier gap (safe − leaky) | AUC gain (leaky − safe)   |
| ------------ | ------------------------- | -------------------------- |
| LSTM         | 0.0229 (0.2207 → 0.1978) | +0.0943 (0.6157 → 0.7100) |
| GRU          | 0.0278 (0.2386 → 0.2109) | +0.0822 (0.5688 → 0.6510) |

Figures: `results/approach2/leakage_ablation/figures/leakage_demo_probabilities.html`
(fold-wise walk-forward probability paths, leaky arms plotted as INVALID traces),
`ablation_brier.html` (walk-forward Brier comparison), and `ablation_summary.html`
(Brier bars and AUC points for all six arms, safe bars green and leaky bars red).

### 4.3 What the demonstration shows

1. **Ignoring publication lags inflates apparent performance for every metric**: the
   leaky variants beat their own safe versions on Brier, AUC, *and* accuracy, with a
   uniform premium of roughly +0.02–0.03 Brier and +0.08–0.09 AUC. All of this apparent
   advantage is manufactured, since at the true second it was unavailable information.
2. **The premium is a within-architecture comparison, and it is not the whole story**:
   the *safe* probit, using only genuinely available information, reaches AUC 0.7788 —
   higher than both leaky RNNs (0.7100, 0.6510). Publication-lag discipline is thus not
   merely "the honest handicap"; the leaky demonstration quantifies the value of the
   forbidden data to a given architecture, it does not make the leaky RNN the best
   forecasting method.
3. **The leaky arms are INVALID as predictive models** and are reported only as an
   intentional demonstration of what lag discipline protects against. They must never
   appear in the report as competitive results, only in the leakage-motivation
   discussion.

---

## 5. Discussion and Limitations

### 5.1 Strengths

- **Strict, verified leakage discipline.** Per-indicator publication-lag shifting,
  expander-window-fit-only preprocessing, label-availability gating, inner-window
  lookback selection, and validation-tail-only threshold/α selection were all verified
  against stored fold histories (`results/approach2/OPTION_B_REVIEW.md`, §1–2); the
  82-fold shared schedule is matched exactly, with the same folds and realized labels
  across all arms.
- **Deep-learning seeds discipline.** Every deep-learning experiment uses three fixed
  seeds including the global default 42; `metrics.json` records mean ± std together
  with all per-seed results.
- **Honest benchmarking.** The required naive prior-frequency baseline is evaluated on
  the same 82 observations, and the leakage ablation quantifies — within architecture —
  what violating the information set would buy.
- **The classical comparator is informative.** The safe probit's AUC 0.7788 shows that
  meaningful one-month-ahead discrimination from available monthly macro data is
  attainable. The sequence models' relative advantage instead appears on the overall
  Brier (probability-error) criterion, not on the ranking criterion.

### 5.2 Limitations (all verified in the review; none contradicted by stored results)

1. **Small evaluation sample.** The evaluation set is 82 one-month-ahead target months
   with 27 realized contractions. Single-fold contributions to Brier/AUC are large,
   metric differences between adjacent models are often within seed or fold noise, and
   90 %-confidence style claims are not attempted.
2. **Threshold-selection uncertainty.** The per-seed decision threshold (balanced
   accuracy on the 24-month validation tail) is based on a short, class-imbalanced
   label tail; tail composition varies across folds and tails with few positives make
   the threshold noisy. This contributes to safe-RNN accuracy falling below the
   majority baseline. Thresholds were not — and will not be — retuned using test-period
   results.
3. **PMI feature/target relationship.** `pmi_man` (lag 0) is a legitimate input feature,
   and because the label is `d_(t+1) = 1{PMI_(t+1) < thr and PMI_t < thr}`, the model
   already observes one of the two label conditions at the origin. This is *not*
   future-information leakage (`PMI_t ∈ F_t`), but the task should be described as the
   conditional prediction of PMI remaining below threshold, and headline AUC/Brier
   gains should not be attributed to forecasting "from scratch" macro turning points.
4. **Clustering initialization sensitivity.** Cross-seed ARI is highly dispersed
   (single pairs 0.042–0.812, means ≈ 0.30 for both architectures); the two latent
   clusters are exploratory, initialization-sensitive diagnostics rather than robust,
   stable economic regimes. Interpretation (42.6 % vs 0.9 % contraction shares, seed 42
   LSTM) is illustrative of one initialization.
5. **No all-metric superiority.** Brier favors the LSTM, AUC favors the probit, and the
   accuracy ordering is threshold-dependent. The group's report should avoid any claim
   that deep sequence models "beat" the classical baseline overall, or vice versa.
6. **Probit specification comparability.** The ablation probit is a
   raw-feature, origin-row ridge probit — not Option A's factor-probit (factors plus
   yield/spread controls). The frozen interface requires the same aligned classification
   targets, forecast origins, folds, labels, and evaluation observations, but does not
   require identical internal model specifications; the two probit specifications are
   therefore both valid over the identical fold schedule and are reported as separately
   specified classical comparators, and the raw-feature probit was not replaced by the
   factor specification. A pooled-row probit
   was tested and rejected during development (AUC 0.39, perfect-separation
   instability) and the origin-row choice is documented in
   `results/approach2/leakage_ablation/NOTES.md`.
7. **Residual nondeterminism.** CPU training of the recurrent models varies in the last
   bits of floating-point reductions; seeds fix initialization and data order, and the
   residual effect is documented in each `metrics.json` (`params.residual_nondeterminism`).

### 5.3 Scope boundaries observed in this section

No GDP nowcasting is performed or claimed anywhere in Option B — quarterly GDP
nowcasting is Option A's task. Option B's regime classifiers share the target months,
folds, labels, and forecast origins with Option A's probit, but cross-method clustering
comparisons are made only through the three-layer framework of `interface.md` §9:
Option B's silhouette/ARI statistics above are method-specific diagnostics and are
never ranked against Option A's, and no direct ARI/NMI between Option A indicator
clusters and Option B time-point hidden-state clusters is computed.

---

### Key files (reproducibility pointers)

| Artifact                              | Path                                                                                                                                       |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| Regime headline metrics / predictions | `results/approach2/regime/metrics.json`, `predictions.csv`                                                                             |
| 49.5 sensitivity                      | `results/approach2/regime_threshold49_5/`                                                                                                |
| Hidden-state clustering               | `results/approach2/clustering/` (`metrics.json`, `latent_clusters.csv`, `latent_embeddings.csv`, 4 figures)                        |
| Leakage ablation                      | `results/approach2/leakage_ablation/` (`metrics.json`, `predictions.csv`, 3 figures, `run_log.txt`, `NOTES.md`)                  |
| Independent review                    | `results/approach2/OPTION_B_REVIEW.md`                                                                                                   |
| Scripts                               | `code/approach2_regime.py`, `code/approach2_clustering.py`, `code/approach2_leakage_ablation.py` (run commands in `AGENTS.md` §2) |

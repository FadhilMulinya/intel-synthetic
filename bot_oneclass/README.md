# Bot vs. Human Detection — Model Development

Classifies CKB addresses as bot-operated or human-operated from their
transaction behavior. As of this version, the model is a **supervised
binary classifier trained on real mainnet data** (`bot_like` /
`human_like` addresses collected by `intel-synthetic/fetch_real_data.py`).

> The original version of this project trained a *one-class* model on
> synthetic simulator bots only, because no human-labeled data existed
> at the time. That approach is preserved as
> `train_eval_oneclass_legacy.py` / `README_legacy_oneclass.md` for
> reference. See **§5 Why this changed** for the numbers that motivated
> the switch.

---

## 1. Repo layout expected

```
intel-synthetic-main/                      <- original uploaded repo
└── data/
    ├── run_300bots_100tx/                 <- synthetic simulator bots
    │   ├── bots.json
    │   └── add_1.json ... add_300.json
    └── real/                              <- fetch_real_data.py output
        ├── bot_like/   manifest.json, addr_*.json
        └── human_like/ manifest.json, addr_*.json

bot_oneclass/                              <- this project
├── requirements.txt
├── extract_features.py                    <- synthetic bots -> features.json
├── extract_features_real.py               <- real addresses -> real_features.json
├── train_eval.py                          <- CURRENT: supervised, real-data-only
├── train_eval_oneclass_legacy.py          <- OLD: one-class, synthetic-only
├── predict.py                             <- classify a live address
├── features.json           (generated, optional -- only used as a bonus stress test now)
├── real_features.json      (generated, REQUIRED for training)
├── eval_results.json       (generated)
└── pca_plot.png            (generated)
```

---

## 2. Environment setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## 3. Execution steps

```bash
# Step 1 (required) — extract features from real mainnet addresses
python3 bot_oneclass/extract_features_real.py
# -> writes real_features.json (bot_like + human_like rows)

# Step 2 (optional) — extract features from synthetic simulator bots,
# only used as a bonus "does it still recognize obvious bot behavior"
# stress test, never as training data
python3 extract_features.py
# -> writes bot_oneclass/features.json

# Step 3 — train, evaluate, visualize
python3 bot_oneclass/train_eval.py
# -> prints model comparison + held-out metrics + history-length breakdown
# -> writes eval_results.json, model.joblib, pca_plot.png

# Step 4 — classify a live address
python3 bot_oneclass/predict.py <ckb-address>
```

`train_eval.py` now **requires** `real_features.json` to exist (it hard-fails
with a clear message telling you to run `extract_features_real.py` first,
rather than silently falling back to the old synthetic-only approach).

Optional flag:

```bash
python3 bot_oneclass/train_eval.py --min-tx 5   # drop addresses with <5 tx
```

---

## 4. Current approach, step by step

### 4.1 Why supervised, not one-class

A one-class model only makes sense when you *only have one class*. That was
true originally (synthetic bots only, zero human examples). It's no longer
true: `fetch_real_data.py` now collects real mainnet addresses bucketed into
**both** `bot_like` and `human_like` (heuristically, via an `is_special` flag
/ lifetime transaction count — see label caveats in §4.4). With labels for
both classes, this is ordinary supervised binary classification, which is
strictly the better-fitting tool for the data actually available.

### 4.2 Data

- `real_features.json`: 159 `bot_like` rows, 87 `human_like` rows, real
  mainnet addresses.
- Same 8 behavioral features as before (ratios/shape statistics, not raw
  magnitudes — see `extract_features.py`), **`n_tx` deliberately excluded**
  from the model inputs even though it's highly predictive, because the
  labeling heuristic itself is partly defined by lifetime tx count.
  Including it would let the model just relearn the labeling rule, not
  behavioral structure.
- Roughly half of `human_like` rows have only 2-4 transactions on record,
  which makes interval-based features degenerate by construction (not
  enough data points to compute a meaningful variance). This is not hidden
  — `train_eval.py` prints an explicit accuracy breakdown by history length
  so you can see whether the model's performance depends on those rows.

### 4.3 Model selection

Four candidates are compared with 5-fold stratified cross-validation on the
training split, scored on F1 (class-imbalance-aware): Logistic Regression,
Random Forest, Gradient Boosting, RBF SVM. All use `class_weight="balanced"`.
The best-scoring model is refit on the full training split, evaluated once
on a held-out test split (never touched during model selection), then
refit again on **all** available real-labeled data for the artifact that
ships in `model.joblib`.

### 4.4 Results (most recent run — regenerate with `train_eval.py` to reproduce)

| model | CV F1 | CV ROC-AUC | held-out test accuracy |
|---|---|---|---|
| Logistic Regression | 0.871 | 0.920 | — |
| **Random Forest (selected)** | **0.968** | **0.990** | **95.2%** |
| Gradient Boosting | 0.967 | 0.988 | — |
| RBF SVM | 0.880 | 0.952 | — |

Held-out test set (Random Forest): **95.2% accuracy, 95.1% precision,
97.5% recall, 96.3% F1, 0.992 ROC-AUC** (n=40 bot, n=22 human in the test
split).

Accuracy by history length (checks the model isn't just exploiting
degenerate short-history rows):

| bucket | n | accuracy |
|---|---|---|
| n_tx < 5 (degenerate) | 11 | 100.0% |
| n_tx 5-20 | 5 | 80.0% |
| n_tx 21+ | 46 | 95.7% |

Accuracy holds up well on the n_tx 21+ bucket specifically, which is
reassuring evidence the model isn't purely a "short history = human"
shortcut.

Feature importance (Random Forest): `interval_max_over_mean` and
`interval_cv` (timing regularity/burstiness) dominate, followed by
`interval_mean_log` and `n_unique_counterparties`. `fee_cv` contributes
nothing — fees don't vary in the current real sample.

Bonus stress test: the trained model flags **0%** of the clean synthetic
simulator bots as bot-like. Read this in context, not as a failure — the
original one-class analysis already established that the simulator's bots
are artificially near-perfect (0-3.5% timing/amount CV) while real bots are
far noisier. A model that's learned genuine real-bot signal is *expected*
to see almost no resemblance between the two. It's flagged here so it isn't
silently ignored, and worth re-checking after future retrains.

### 4.5 Why this changed (comparison to the old one-class approach)

The old one-class models, trained only on synthetic bots, scored against
real mainnet data:

| test | Isolation Forest accuracy | One-Class SVM accuracy |
|---|---|---|
| vs. real human_like | 25.2% | 62.2% |
| vs. all available real+synthetic eval data | 21.3% | 37.2% |

The new supervised model, trained on real data, scores **95.2%** accuracy
on a held-out real-data test split — because it's now the right kind of
model for data that actually has labels on both sides, rather than a
novelty detector trained on an artificially clean proxy for one side only.

---

## 5. Limitations (read before treating any number as a guarantee)

- **Labels are heuristic, not verified ground truth.** `bot_like` /
  `human_like` come from an `is_special` flag / lifetime-tx-count
  threshold, not confirmed identity. Every metric above is bounded by how
  good that heuristic is.
- **Small sample.** 246 real-labeled rows total. Cross-validation is used
  precisely because a single train/test split on this few rows would be
  noisy; even so, treat these numbers as a strong directional signal, not
  a certified error rate.
- **Class imbalance** (~1.8:1 bot:human currently), handled via
  `class_weight="balanced"` rather than discarding data.
- **Short-history degeneracy.** ~54% of `human_like` rows have 2-4
  transactions, making interval-based features close to meaningless by
  construction for those rows. `--min-tx` lets you exclude them if you
  want a stricter (smaller) evaluation.
- **`n_tx` excluded from features on purpose** (see §4.2) — this trades
  away a strong raw predictor to avoid circularity with the labeling rule,
  which is the right call, but means the model may be weaker in cases
  where tx-count really is the dominant honest signal.

## 6. Recommended next steps

1. **Grow the real-data sample**, especially `human_like` (currently the
   minority class and the one most affected by short-history degeneracy).
2. **Revisit the labeling heuristic** if a less tx-count-entangled signal
   becomes available (e.g. manual review of a subset), so `n_tx` could
   safely be reintroduced as a feature without circularity.
3. **Re-run the full pipeline** after each change — both scripts are
   deterministic (fixed seed 42) and cheap to re-run end to end.
4. **Track calibration**, not just accuracy — `predict.py` returns
   `bot_probability`, not just a hard label; if the real-world use case
   needs a specific precision/recall trade-off, tune
   `BOT_PROBABILITY_THRESHOLD` in `predict.py` against a validation set.
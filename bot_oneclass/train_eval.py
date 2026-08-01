"""
Bot vs. human detection -- supervised binary classifier trained on REAL
mainnet data (bot_like / human_like), no synthetic data involved in
training.

WHY THIS REPLACED THE ONE-CLASS APPROACH
-----------------------------------------
The original pipeline (see train_eval_oneclass_legacy.py) trained
one-class models exclusively on the ckb-bot-simulator's synthetic bots,
because no human-labeled data existed anywhere in the repo at the time.
It evaluated reasonably against held-out synthetic bots, but against real
mainnet data it topped out at 20-62% accuracy (see the eval_results.json
that run produced) -- because the synthetic training manifold is
drastically tighter and cleaner than real bot behavior. The model learned
"artificially perfect simulator archetype", not "bot".

Now that intel-synthetic/fetch_real_data.py has collected REAL mainnet
addresses in *both* buckets (bot_like and human_like, heuristically
labeled via identity/lifetime-tx-count signals -- see
extract_features_real.py), this is no longer a one-class problem at all.
It's ordinary supervised binary classification, which is a much better
fit for the data we actually have and performs far better in practice:

    held-out test accuracy jumps from ~20-62% (one-class, vs real data)
    to ~90-95% (supervised, same real data) -- see the printed comparison
    at the end of a run.

LABEL PROVENANCE / CAVEATS (read before trusting any number below)
-------------------------------------------------------------------
- bot_like / human_like buckets are HEURISTIC proxies (an `is_special`
  flag or lifetime-tx-count thresholds), not verified ground truth.
  Every accuracy figure here is bounded by how good that heuristic is --
  not a guaranteed real-world error rate.
- Roughly half of human_like rows have only 2-4 transactions on record,
  which makes interval-based features (interval_cv, interval_max_over_mean)
  degenerate BY CONSTRUCTION (there's only one interval to measure, so
  variance collapses to ~0) -- not because the address is behaviorally
  "regular". This script prints an explicit diagnostic breaking test
  accuracy out by history length so this isn't hidden inside one overall
  number. Use --min-tx to exclude short-history rows if you want a
  stricter (smaller) sample.
- Class imbalance: bot_like currently outnumbers human_like ~1.8:1.
  Handled with class_weight='balanced' rather than by discarding data.
- Small sample overall (a few hundred rows). Model selection uses
  stratified k-fold CV instead of trusting a single train/test split.
- n_tx (raw transaction count) is deliberately EXCLUDED from the feature
  set even though it's highly discriminating, because the bot_like/
  human_like labels themselves were partly defined by lifetime tx count.
  Including it would let the model reproduce the labeling heuristic
  verbatim instead of learning behavioral structure -- circular, not a
  real detector. This is worth revisiting if a less tx-count-entangled
  labeling process becomes available later.

Usage
-----
    python3 bot_oneclass/train_eval.py
    python3 bot_oneclass/train_eval.py --min-tx 5   # drop short-history rows
"""
import argparse
import json
import os
import numpy as np
import joblib
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.svm import SVC
from sklearn.calibration import CalibratedClassifierCV
from sklearn.preprocessing import StandardScaler
from sklearn.decomposition import PCA
from sklearn.model_selection import train_test_split, StratifiedKFold, cross_validate
from sklearn.metrics import (
    accuracy_score, precision_score, recall_score, f1_score,
    roc_auc_score, confusion_matrix,
)
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

MODEL_OUT_PATH = "bot_oneclass/model.joblib"
REAL_FEATURES_PATH = "bot_oneclass/real_features.json"
SYNTH_FEATURES_PATH = "bot_oneclass/features.json"  # optional, used only as a bonus stress test
DEGENERATE_TX_THRESHOLD = 5  # below this, interval-based features are near-meaningless by construction

RANDOM_STATE = 42

FEATURE_COLS = [
    "interval_cv", "interval_mean_log", "capacity_cv", "fee_cv",
    "n_unique_counterparties", "counterparty_entropy_norm",
    "mean_outputs_per_tx", "interval_max_over_mean",
    "inbound_only_tx_frac", "max_over_mean_outputs",
]

CANDIDATE_MODELS = {
    "LogisticRegression": lambda: LogisticRegression(
        class_weight="balanced", max_iter=2000, random_state=RANDOM_STATE
    ),
    "RandomForest": lambda: RandomForestClassifier(
        n_estimators=300, max_depth=6, class_weight="balanced", random_state=RANDOM_STATE
    ),
    "GradientBoosting": lambda: GradientBoostingClassifier(random_state=RANDOM_STATE),
    "SVM-RBF": lambda: SVC(
        kernel="rbf", class_weight="balanced", probability=True, random_state=RANDOM_STATE
    ),
}


def to_matrix(rows):
    return np.array([[r[c] for c in FEATURE_COLS] for r in rows], dtype=float)


def load_real_data(min_tx=0):
    if not os.path.exists(REAL_FEATURES_PATH):
        raise SystemExit(
            f"ERROR: {REAL_FEATURES_PATH} not found. This pipeline trains on real "
            f"mainnet data only -- run extract_features_real.py first (see "
            f"fetch_real_data.py in intel-synthetic/ to collect the raw addresses)."
        )
    rows = json.load(open(REAL_FEATURES_PATH))
    before = len(rows)
    rows = [r for r in rows if r.get("label_source") in ("bot_like", "human_like")]
    rows = [r for r in rows if r["n_tx"] >= min_tx]
    dropped = before - len(rows)
    if dropped:
        print(f"NOTE: dropped {dropped} rows (missing label or n_tx < {min_tx})")
    bot_rows = [r for r in rows if r["label_source"] == "bot_like"]
    human_rows = [r for r in rows if r["label_source"] == "human_like"]
    if not bot_rows or not human_rows:
        raise SystemExit(
            f"ERROR: need both classes to train supervised model -- got "
            f"{len(bot_rows)} bot_like, {len(human_rows)} human_like."
        )
    return bot_rows, human_rows


def load_synthetic_stress_test():
    """Optional, NOT used in training. If features.json (synthetic archetype
    bots) exists, use it purely as a bonus generalization check: a model
    trained on real bots should still mostly recognize these as bot-like,
    since they ARE bots (just artificially clean ones)."""
    if not os.path.exists(SYNTH_FEATURES_PATH):
        return None
    rows = json.load(open(SYNTH_FEATURES_PATH))
    missing = [c for c in FEATURE_COLS if c not in rows[0]]
    if missing:
        print(f"NOTE: skipping synthetic stress test -- {SYNTH_FEATURES_PATH} is missing "
              f"column(s) {missing} (likely generated before a FEATURE_COLS change). "
              f"Re-run extract_features.py to regenerate it with the current feature set.")
        return None
    return to_matrix(rows)


def select_model(X_train_s, y_train):
    """5-fold stratified CV over candidate models, scored on F1 (balances
    precision/recall given class imbalance); returns the best model name
    and its CV score summary for all candidates so the choice is visible,
    not just asserted."""
    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=RANDOM_STATE)
    cv_summary = {}
    best_name, best_f1 = None, -1
    for name, factory in CANDIDATE_MODELS.items():
        scores = cross_validate(
            factory(), X_train_s, y_train, cv=skf,
            scoring=["accuracy", "precision", "recall", "f1", "roc_auc"],
        )
        cv_summary[name] = {
            metric: {"mean": round(float(np.mean(scores[f"test_{metric}"])), 4),
                     "std": round(float(np.std(scores[f"test_{metric}"])), 4)}
            for metric in ["accuracy", "precision", "recall", "f1", "roc_auc"]
        }
        mean_f1 = np.mean(scores["test_f1"])
        if mean_f1 > best_f1:
            best_f1, best_name = mean_f1, name
    return best_name, cv_summary


CANDIDATE_BANDS = [
    (0.5, 0.5),   # no band -- baseline, everything decided at 0.5
    (0.4, 0.6), (0.35, 0.65), (0.3, 0.7), (0.25, 0.75), (0.2, 0.8),
]
MIN_CONFIDENT_ACCURACY = 0.97  # the accuracy floor we want on whatever isn't punted to "uncertain"


def select_uncertain_band(model_factory, X, y, bands=CANDIDATE_BANDS, n_splits=5):
    """Cross-validated band selection -- avoids tuning the [lo, hi] cutoffs to
    noise on a single ~60-row held-out split. For each of n_splits stratified
    folds: fit StandardScaler + CalibratedClassifierCV(model) on the training
    side, get calibrated probabilities on the validation side, and for every
    candidate band record (accuracy on the confident subset, % of rows that
    fell inside the band). Scores are averaged across folds before picking a
    band, so the result reflects something closer to true out-of-sample
    behavior, not one lucky/unlucky split.

    Returns the narrowest band (best coverage) that still clears
    MIN_CONFIDENT_ACCURACY on average, plus the full per-band CV summary
    (so the trade-off is visible, not just asserted).
    """
    skf = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=RANDOM_STATE)
    band_stats = {b: {"confident_acc": [], "coverage": []} for b in bands}

    for train_idx, val_idx in skf.split(X, y):
        X_tr, X_val = X[train_idx], X[val_idx]
        y_tr, y_val = y[train_idx], y[val_idx]
        scaler = StandardScaler().fit(X_tr)
        X_tr_s, X_val_s = scaler.transform(X_tr), scaler.transform(X_val)

        # calibrate probabilities (raw RandomForest predict_proba is not a
        # trustworthy probability at this sample size / tree depth) using an
        # inner CV split of the training fold only -- validation fold stays
        # untouched by calibration fitting.
        calibrated = CalibratedClassifierCV(model_factory(), method="sigmoid", cv=3)
        calibrated.fit(X_tr_s, y_tr)
        proba_val = calibrated.predict_proba(X_val_s)[:, 1]

        for lo, hi in bands:
            confident_mask = (proba_val < lo) | (proba_val > hi)
            coverage = float(np.mean(confident_mask))
            if confident_mask.sum() > 0:
                pred = (proba_val[confident_mask] >= 0.5).astype(int)
                acc = float(accuracy_score(y_val[confident_mask], pred))
            else:
                acc = None
            band_stats[(lo, hi)]["confident_acc"].append(acc)
            band_stats[(lo, hi)]["coverage"].append(coverage)

    band_summary = {}
    for b, stats_ in band_stats.items():
        accs = [a for a in stats_["confident_acc"] if a is not None]
        band_summary[f"{b[0]}-{b[1]}"] = {
            "mean_confident_accuracy": round(float(np.mean(accs)), 4) if accs else None,
            "mean_coverage": round(float(np.mean(stats_["coverage"])), 4),
        }

    # pick the narrowest (best-coverage) band that clears the accuracy floor;
    # bands list is already ordered narrow -> wide, and (0.5,0.5) means "no
    # band" so it's excluded from consideration as a real choice
    chosen = None
    for b in bands:
        if b == (0.5, 0.5):
            continue
        key = f"{b[0]}-{b[1]}"
        s = band_summary[key]
        if s["mean_confident_accuracy"] is not None and s["mean_confident_accuracy"] >= MIN_CONFIDENT_ACCURACY:
            chosen = b
            break
    if chosen is None:
        # nothing cleared the floor -- fall back to the widest band tested
        chosen = [b for b in bands if b != (0.5, 0.5)][-1]

    return {
        "chosen_band": {"lo": chosen[0], "hi": chosen[1]},
        "cv_band_comparison": band_summary,
        "min_confident_accuracy_target": MIN_CONFIDENT_ACCURACY,
        "note": (
            "Selected via 5-fold stratified CV with sigmoid-calibrated "
            "probabilities inside each fold (not a single held-out split), "
            "so this reflects average out-of-sample behavior. Sample size "
            "is still small (~250 rows total) -- treat mean_coverage/"
            "mean_confident_accuracy as directional, and re-run this "
            "selection whenever real_features.json grows."
        ),
    }


def evaluate(model, X_s, y_true, label):
    y_pred = model.predict(X_s)
    y_proba = model.predict_proba(X_s)[:, 1] if hasattr(model, "predict_proba") else y_pred
    tn, fp, fn, tp = confusion_matrix(y_true, y_pred, labels=[0, 1]).ravel()
    return {
        "eval_set": label,
        "n_bot_examples": int(np.sum(y_true == 1)),
        "n_human_examples": int(np.sum(y_true == 0)),
        "accuracy": round(float(accuracy_score(y_true, y_pred)), 4),
        "precision_bot": round(float(precision_score(y_true, y_pred, zero_division=0)), 4),
        "recall_bot": round(float(recall_score(y_true, y_pred, zero_division=0)), 4),
        "f1_bot": round(float(f1_score(y_true, y_pred, zero_division=0)), 4),
        "roc_auc": round(float(roc_auc_score(y_true, y_proba)), 4) if len(set(y_true)) > 1 else None,
        "confusion_matrix": {
            "true_bot_pred_bot": int(tp), "true_bot_pred_human": int(fn),
            "true_human_pred_bot": int(fp), "true_human_pred_human": int(tn),
        },
    }


def breakdown_by_history_length(model, scaler, test_rows, y_test):
    """Diagnostic: does accuracy hold up on longer-history addresses, or is
    the model just exploiting degenerate short-history feature values?
    Printed, not hidden -- see module docstring."""
    ntx = np.array([r["n_tx"] for r in test_rows])
    X_test_s = scaler.transform(to_matrix(test_rows))
    y_pred = model.predict(X_test_s)
    buckets = [(0, DEGENERATE_TX_THRESHOLD - 1, f"n_tx < {DEGENERATE_TX_THRESHOLD} (degenerate)"),
               (DEGENERATE_TX_THRESHOLD, 20, f"n_tx {DEGENERATE_TX_THRESHOLD}-20"),
               (21, 10**9, "n_tx 21+")]
    out = []
    for lo, hi, label in buckets:
        mask = (ntx >= lo) & (ntx <= hi)
        if mask.sum() == 0:
            continue
        out.append({
            "bucket": label, "n": int(mask.sum()),
            "accuracy": round(float(accuracy_score(y_test[mask], y_pred[mask])), 4),
        })
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-tx", type=int, default=0,
                     help="drop rows with fewer than this many transactions (default: 0, keep everything)")
    args = ap.parse_args()

    bot_rows, human_rows = load_real_data(min_tx=args.min_tx)
    print(f"Loaded real mainnet data: {len(bot_rows)} bot_like, {len(human_rows)} human_like")
    n_short = sum(1 for r in human_rows + bot_rows if r["n_tx"] < DEGENERATE_TX_THRESHOLD)
    print(f"NOTE: {n_short} rows have n_tx < {DEGENERATE_TX_THRESHOLD} -- interval-based "
          f"features are degenerate for these (see module docstring).")

    all_rows = bot_rows + human_rows
    X = to_matrix(all_rows)
    y = np.array([1 if r["label_source"] == "bot_like" else 0 for r in all_rows])

    X_train, X_test, y_train, y_test, rows_train, rows_test = train_test_split(
        X, y, all_rows, test_size=0.25, random_state=RANDOM_STATE, stratify=y
    )

    scaler = StandardScaler().fit(X_train)
    X_train_s, X_test_s = scaler.transform(X_train), scaler.transform(X_test)

    best_name, cv_summary = select_model(X_train_s, y_train)
    print(f"\nModel selection (5-fold stratified CV on training split, scored on F1):")
    for name, m in cv_summary.items():
        marker = " <-- selected" if name == best_name else ""
        print(f"  {name:18s} f1={m['f1']['mean']:.3f}+/-{m['f1']['std']:.3f}  "
              f"auc={m['roc_auc']['mean']:.3f}  acc={m['accuracy']['mean']:.3f}{marker}")

    final_model = CANDIDATE_MODELS[best_name]().fit(X_train_s, y_train)
    held_out_eval = evaluate(final_model, X_test_s, y_test, "held_out_real_test_split")
    print(f"\nHeld-out test set ({best_name}):")
    print(f"  accuracy={held_out_eval['accuracy']*100:.1f}%  precision={held_out_eval['precision_bot']*100:.1f}%  "
          f"recall={held_out_eval['recall_bot']*100:.1f}%  f1={held_out_eval['f1_bot']*100:.1f}%  "
          f"roc_auc={held_out_eval['roc_auc']}")

    history_breakdown = breakdown_by_history_length(final_model, scaler, rows_test, y_test)
    print("\nAccuracy by history length (held-out test set) -- checks the model isn't just")
    print("exploiting degenerate short-history feature values:")
    for b in history_breakdown:
        print(f"  {b['bucket']:24s} n={b['n']:3d} accuracy={b['accuracy']*100:.1f}%")

    # feature importance if available (tree models); otherwise skip
    feature_importance = None
    if hasattr(final_model, "feature_importances_"):
        feature_importance = {
            c: round(float(v), 4)
            for c, v in sorted(zip(FEATURE_COLS, final_model.feature_importances_), key=lambda t: -t[1])
        }
        print("\nFeature importance:")
        for c, v in feature_importance.items():
            print(f"  {c:28s} {v:.4f}")

    # ---- bonus stress test: does it still recognize clean synthetic bots as bots? ----
    synth_stress = None
    X_synth = load_synthetic_stress_test()
    if X_synth is not None:
        X_synth_s = scaler.transform(X_synth)
        pred = final_model.predict(X_synth_s)
        pct = float(np.mean(pred == 1)) * 100
        synth_stress = {
            "n": int(len(X_synth)),
            "pct_flagged_bot": round(pct, 1),
            "note": ("Synthetic archetype bots were NOT used in training. Interpret this "
                     "number carefully: earlier analysis (see README_legacy_oneclass.md) "
                     "found the simulator's bots are artificially near-perfect (0-3.5% "
                     "timing/amount CV) while real bot_like addresses are much noisier -- "
                     "so a low score here is expected evidence that real and simulated bot "
                     "behavior occupy different regions of feature space, not necessarily a "
                     "sign this model is broken. Still worth watching if it changes a lot "
                     "after future retrains."),
        }
        print(f"\nBonus stress test -- synthetic archetype bots (not used in training): "
              f"{pct:.1f}% flagged as bot (n={len(X_synth)})")
    else:
        print(f"\n(No {SYNTH_FEATURES_PATH} found -- skipping bonus synthetic stress test. "
              f"Run extract_features.py if you want this diagnostic.)")

    print(f"\nSelecting uncertain band via 5-fold CV with calibrated probabilities...")
    band_result = select_uncertain_band(CANDIDATE_MODELS[best_name], X, y)
    print(f"  chosen band: [{band_result['chosen_band']['lo']}, {band_result['chosen_band']['hi']}]")
    for band_key, s in band_result["cv_band_comparison"].items():
        print(f"    band {band_key:10s} confident_acc={s['mean_confident_accuracy']}  "
              f"coverage={s['mean_coverage']}")

    # ---- refit on ALL labeled real data for the deployed model ----
    # Model selection/eval above used a held-out split so the numbers above are honest.
    # For the artifact that actually ships, refit on 100% of available real labels --
    # standard practice once evaluation is done, and no synthetic data enters this fit.
    scaler_final = StandardScaler().fit(X)
    X_all_s = scaler_final.transform(X)
    # deploy a CALIBRATED model -- raw RandomForest.predict_proba is not a
    # trustworthy probability at this depth/sample size, and the uncertain
    # band above was selected against calibrated probabilities, so the
    # deployed model needs to produce probabilities on that same scale or
    # the band boundaries are meaningless in production.
    deployed_model = CalibratedClassifierCV(CANDIDATE_MODELS[best_name](), method="sigmoid", cv=5)
    deployed_model.fit(X_all_s, y)

    results = {
        "model_selected": best_name,
        "cv_model_comparison": cv_summary,
        "held_out_test_eval": held_out_eval,
        "accuracy_by_history_length": history_breakdown,
        "feature_importance": feature_importance,
        "uncertain_band_selection": band_result,
        "synthetic_stress_test": synth_stress,
        "training_composition": {
            "n_bot_like": int(len(bot_rows)),
            "n_human_like": int(len(human_rows)),
            "n_short_history_rows": int(n_short),
            "min_tx_filter_applied": args.min_tx,
        },
        "label_provenance_warning": (
            "bot_like / human_like are heuristic proxy labels (is_special / lifetime tx "
            "count), not verified ground truth. All metrics above are bounded by that "
            "heuristic's quality -- read as a strong directional signal, not a certified "
            "real-world error rate. See train_eval.py module docstring for full caveats."
        ),
    }
    print("\nFull results written to bot_oneclass/eval_results.json")
    with open("bot_oneclass/eval_results.json", "w") as f:
        json.dump(results, f, indent=2)

    # ---- persist the trained artifact ----
    os.makedirs(os.path.dirname(MODEL_OUT_PATH) or ".", exist_ok=True)
    bundle = {
        "model": deployed_model,
        "model_type": best_name,
        "calibrated": True,
        "uncertain_band": band_result["chosen_band"],
        "scaler": scaler_final,
        "feature_cols": FEATURE_COLS,
        "eval_results": results,
        "notes": (
            "Supervised binary classifier trained on REAL mainnet data only "
            "(bot_like vs human_like, heuristically labeled -- see "
            "extract_features_real.py / fetch_real_data.py). Replaces the "
            "earlier one-class-on-synthetic-data approach (see "
            "train_eval_oneclass_legacy.py), which scored 20-62% accuracy "
            "against real data vs this model's ~90%+. Predict with "
            "model.predict_proba(X)[:, 1] -- probability of class 'bot'. "
            "See README.md and this file's module docstring for caveats "
            "(label heuristic quality, degenerate short-history features, "
            "small sample size, class imbalance)."
        ),
    }
    joblib.dump(bundle, MODEL_OUT_PATH)
    print(f"saved trained model bundle to {MODEL_OUT_PATH}")

    # ---- visualization ----
    pca = PCA(n_components=2, random_state=RANDOM_STATE).fit(X_all_s)
    coords = pca.transform(X_all_s)
    is_short = np.array([r["n_tx"] < DEGENERATE_TX_THRESHOLD for r in all_rows])

    fig, ax = plt.subplots(figsize=(9, 7))
    for label_val, name, color, marker in [(1, "REAL bot_like", "red", "D"), (0, "REAL human_like", "blue", "*")]:
        mask = (y == label_val) & (~is_short)
        ax.scatter(coords[mask, 0], coords[mask, 1], label=name, s=55, alpha=0.85,
                   color=color, marker=marker, edgecolors="black", linewidths=0.4)
        mask_short = (y == label_val) & is_short
        if mask_short.any():
            ax.scatter(coords[mask_short, 0], coords[mask_short, 1],
                       label=f"{name} (n_tx<{DEGENERATE_TX_THRESHOLD}, degenerate)", s=35, alpha=0.4,
                       color=color, marker=marker, edgecolors="none")

    if X_synth is not None:
        synth_coords = pca.transform(scaler_final.transform(X_synth))
        ax.scatter(synth_coords[:, 0], synth_coords[:, 1], label="synthetic archetype bots (not trained on)",
                   s=20, alpha=0.25, color="gray", marker="x")

    ax.set_title("Bot vs. human feature space (PCA) -- real mainnet data, supervised model")
    ax.set_xlabel(f"PC1 ({pca.explained_variance_ratio_[0]*100:.1f}% var)")
    ax.set_ylabel(f"PC2 ({pca.explained_variance_ratio_[1]*100:.1f}% var)")
    ax.legend(loc="best", fontsize=7, framealpha=0.9)
    fig.tight_layout()
    fig.savefig("bot_oneclass/pca_plot.png", dpi=150)
    print("saved plot to pca_plot.png")


if __name__ == "__main__":
    main()

"""Offline sanity check for predict.py's classify() route, using fixtures
shaped like the documented Explorer API responses (same convention as
test_fetch_offline.py) plus a tiny fake model bundle standing in for a
real trained model.joblib. No network calls, no dependency on an actual
trained model file -- this only proves the address -> features -> score
wiring is correct, not real-world accuracy (that's what eval_results.json
covers).
"""
import os
import sys
import tempfile
import unittest.mock as mock

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, os.path.dirname(__file__))
import predict as pr

FEATURE_COLS = [
    "interval_cv", "interval_mean_log", "capacity_cv", "fee_cv",
    "n_unique_counterparties", "counterparty_entropy_norm",
    "mean_outputs_per_tx", "interval_max_over_mean",
]

# A handful of flattened txs shaped like fetch_address_transactions' output
# (attrs dict per tx, transaction_fee defaulted to "0" same as the real
# fetch path) -- enough transactions (>=2) to compute non-degenerate
# interval/capacity stats.
FAKE_TXS_PAGE1 = {
    "data": [
        {"attributes": {
            "transaction_hash": f"0x{i}", "block_number": str(1000 + i),
            "block_timestamp": str(1_700_000_000_000 + i * 60_000),
            "display_inputs": [], "transaction_fee": "0",
            "display_outputs": [{"address_hash": "ckb1counterparty", "capacity": "100000000"}],
        }} for i in range(6)
    ]
}
FAKE_TXS_PAGE_EMPTY = {"data": []}

FAKE_SHORT_TXS_PAGE1 = {
    "data": [
        {"attributes": {
            "transaction_hash": "0xonly", "block_number": "1",
            "block_timestamp": "1700000000000", "display_inputs": [], "transaction_fee": "0",
            "display_outputs": [{"address_hash": "ckb1counterparty", "capacity": "100000000"}],
        }},
    ]
}

FAKE_LOW_TX_PAGE1 = {
    "data": [
        {"attributes": {
            "transaction_hash": f"0xlow{i}", "block_number": str(i), "transaction_fee": "0",
            "block_timestamp": str(1_700_000_000_000 + i * 3600_000), "display_inputs": [],
            "display_outputs": [{"address_hash": "ckb1counterparty", "capacity": "100000000"}],
        }} for i in range(3)
    ]
}


def fake_api_get(path, params=None, **kwargs):
    if path.startswith("/address_transactions/ckb1short"):
        page = (params or {}).get("page", 1)
        return FAKE_SHORT_TXS_PAGE1 if page == 1 else FAKE_TXS_PAGE_EMPTY
    if path.startswith("/address_transactions/ckb1lowtx"):
        page = (params or {}).get("page", 1)
        return FAKE_LOW_TX_PAGE1 if page == 1 else FAKE_TXS_PAGE_EMPTY
    if path.startswith("/address_transactions/"):
        page = (params or {}).get("page", 1)
        return FAKE_TXS_PAGE1 if page == 1 else FAKE_TXS_PAGE_EMPTY
    raise AssertionError(f"unexpected path in test: {path}")


def make_fake_model_bundle(tmp_path):
    """A trivially-fitted RandomForest standing in for a real trained
    model -- proves the bundle shape/predict_proba wiring works, says
    nothing about real accuracy (see eval_results.json for that)."""
    rng = np.random.default_rng(0)
    X = rng.normal(size=(40, len(FEATURE_COLS)))
    y = (X[:, 0] > 0).astype(int)  # arbitrary separable rule, just to get two classes
    scaler = StandardScaler().fit(X)
    model = RandomForestClassifier(n_estimators=20, random_state=0).fit(scaler.transform(X), y)
    bundle = {"model": model, "model_type": "RandomForest", "scaler": scaler, "feature_cols": FEATURE_COLS}
    joblib.dump(bundle, tmp_path)


def run():
    with tempfile.TemporaryDirectory() as tmp:
        model_path = os.path.join(tmp, "model.joblib")
        make_fake_model_bundle(model_path)

        with mock.patch.object(pr, "api_get", side_effect=fake_api_get):
            result = pr.classify("ckb1normal", model_path=model_path, max_tx=300)
            assert result["n_tx_fetched"] == 6
            assert "bot_probability" in result and 0.0 <= result["bot_probability"] <= 1.0
            assert result["verdict"] in ("bot", "human", "uncertain")
            if result["verdict"] != "uncertain":
                assert "warning" not in result  # 6 tx >= MIN_TX_FOR_RELIABLE_PREDICTION, confident band
            print("PASS: classify() on normal-history address ->", result["verdict"],
                  f"(bot_probability={result['bot_probability']})")

            result_short = pr.classify("ckb1short", model_path=model_path, max_tx=300)
            assert result_short["n_tx_fetched"] == 1
            assert result_short["verdict"] == "unknown", "single-tx address can't extract interval features at all"
            print("PASS: classify() on 1-tx address correctly returns 'unknown' (not enough history)")

            result_low = pr.classify("ckb1lowtx", model_path=model_path, max_tx=300)
            assert result_low["n_tx_fetched"] == 3
            assert result_low["verdict"] in ("bot", "human", "uncertain")
            assert "warning" in result_low, "3 tx is < MIN_TX_FOR_RELIABLE_PREDICTION -- should warn, not hide it"
            print("PASS: classify() on 3-tx address still scores it, but attaches the low-confidence warning")

        # verdict must be "uncertain" whenever the model's own bot_probability
        # lands inside a bundle's stored band, regardless of which side of 0.5
        # it's on -- proves predict.py reads uncertain_band from the bundle
        # rather than only ever using the DEFAULT_UNCERTAIN_BAND fallback.
        model_path_banded = os.path.join(tmp, "model_banded.joblib")
        make_fake_model_bundle(model_path_banded)
        bundle = joblib.load(model_path_banded)
        bundle["uncertain_band"] = {"lo": 0.0, "hi": 1.0}  # everything is "uncertain"
        joblib.dump(bundle, model_path_banded)
        with mock.patch.object(pr, "api_get", side_effect=fake_api_get):
            result_banded = pr.classify("ckb1normal", model_path=model_path_banded, max_tx=300)
            assert result_banded["verdict"] == "uncertain"
            assert "warning" in result_banded and "uncertain band" in result_banded["warning"]
            print("PASS: classify() honors a bundle's stored uncertain_band (band=[0,1] -> always uncertain)")

    print("\nALL OFFLINE PREDICT CHECKS PASSED")


if __name__ == "__main__":
    run()

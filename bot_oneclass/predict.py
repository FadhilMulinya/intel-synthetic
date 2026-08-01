"""
predict.py -- classify a single CKB address as human-operated or
bot-operated, without the user needing to supply any behavioral parameters
themselves.

Given just an address:
  1. Queries the public CKB Explorer API for that address's recent
     transaction history (no local chain data needed).
  2. Runs it through the exact same feature extraction used in training
     (extract_features.extract_features(), imported directly -- not
     reimplemented, so there's no risk of the live pipeline silently
     drifting from what the model was trained on).
  3. Scores it with the trained supervised model bundle (model.joblib --
     a classifier trained on real mainnet bot_like/human_like data, see
     train_eval.py). Returns a bot probability, not just a hard label,
     since the model exposes predict_proba.

This file is deliberately self-contained (only imports extract_features.py,
which sits alongside it) so bot_oneclass/ can be deployed as a standalone
service without needing the rest of intel-synthetic/ at inference time. The
HTTP client here intentionally mirrors intel-synthetic/fetch_real_data.py's
proven request/retry logic -- if that script's endpoints ever change,
update both.

USAGE
-----
    python3 bot_oneclass/predict.py <ckb-address>
    python3 bot_oneclass/predict.py <ckb-address> --json

Run from the ckb_model/ parent directory (same convention as
train_eval.py), so the relative model path resolves correctly.
"""
import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

import joblib

import extract_features as ef  # same-directory import

BASE_URL = "https://mainnet-api.explorer.nervos.org/api/v1"
HEADERS = {
    "Accept": "application/vnd.api+json",
    "Content-Type": "application/vnd.api+json",
    "User-Agent": "ckb-bot-oneclass-predict/1.0",
}
# Resolve relative to this file's own location, not the process's current
# working directory -- the old "bot_oneclass/model.joblib" only worked if
# you happened to launch from the exact right parent dir (fine for a CLI
# convention, but breaks under uvicorn/Docker/most PaaS start commands,
# which don't guarantee cwd). This works regardless of cwd.
MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model.joblib")
MIN_TX_FOR_RELIABLE_PREDICTION = 5  # below this, interval-based features are degenerate -- warn, don't hide it
BOT_PROBABILITY_THRESHOLD = 0.5  # tune with a validation set if you want a different precision/recall trade-off
# fallback if an older model.joblib (pre-calibration) without a stored band is loaded --
# the real bundle now carries its own cross-validated band, see train_eval.py select_uncertain_band()
DEFAULT_UNCERTAIN_BAND = {"lo": 0.4, "hi": 0.6}


class ApiError(Exception):
    """Upstream explorer API problem (network, rate limit, bad response) --
    not our fault, surface as 502 in the HTTP layer."""
    pass


class ModelLoadError(Exception):
    """model.joblib missing/corrupt -- our fault (deployment misconfig),
    surface as 500 in the HTTP layer, distinct from ApiError."""
    pass


def api_get(path, params=None, max_retries=4, timeout=15, max_delay=10):
    """Same retry/backoff pattern as fetch_real_data.py's api_get, trimmed
    for a single on-demand lookup rather than a long batch run."""
    url = BASE_URL + path
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    delay = 1.0
    last_err = None
    for _ in range(max_retries):
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                last_err = e
                time.sleep(delay)
                delay = min(delay * 2, max_delay)
                continue
            if e.code == 404:
                return None
            raise ApiError(f"GET {url} -> HTTP {e.code}: {e.read()[:300]}")
        except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError, OSError) as e:
            last_err = e
            time.sleep(delay)
            delay = min(delay * 2, max_delay)
    raise ApiError(f"GET {url} failed after {max_retries} retries: {last_err}")


def fetch_address_transactions(address, max_tx=300, page_size=50):
    """Most-recent-first sample of an address's history, capped at max_tx --
    see fetch_real_data.py for why time.desc + a cap (not full history).
    Uses a tighter retry budget than api_get's default -- mirrors
    fetch_real_data.py's fetch_address_transactions exactly: for a single
    live lookup with a user waiting, fail fast (~15s) on a bad/slow address
    rather than burn ~60s retrying."""
    out = []
    page = 1
    while len(out) < max_tx:
        data = api_get(
            f"/address_transactions/{address}",
            params={"page": page, "page_size": page_size, "sort": "time.desc"},
            max_retries=3, timeout=15, max_delay=8,
        )
        if not data or not data.get("data"):
            break
        rows = data["data"]
        if not rows:
            break
        for tx in rows:
            attrs = dict(tx.get("attributes", {}))
            attrs.setdefault("transaction_fee", "0")  # not returned by this endpoint
            out.append(attrs)
            if len(out) >= max_tx:
                break
        if len(rows) < page_size:
            break
        page += 1
    return out


def classify(address, model_path=MODEL_PATH, max_tx=300):
    """Returns a result dict -- see main() for the shape. Raises ApiError if
    the address's history can't be fetched at all."""
    try:
        bundle = joblib.load(model_path)
    except (FileNotFoundError, OSError) as e:
        raise ModelLoadError(f"could not load model at {model_path}: {e}")
    model, scaler, feature_cols = bundle["model"], bundle["scaler"], bundle["feature_cols"]

    txs = fetch_address_transactions(address, max_tx=max_tx)
    n_tx = len(txs)

    if n_tx < 2:
        return {
            "address": address,
            "n_tx_fetched": n_tx,
            "verdict": "unknown",
            "reason": f"only {n_tx} transaction(s) found -- not enough history to extract behavioral features at all.",
        }

    feats = ef.extract_features({"address": address, "archetype": None}, txs)
    X = [[feats[c] for c in feature_cols]]
    X_s = scaler.transform(X)

    bot_proba = float(model.predict_proba(X_s)[0][1])
    band = bundle.get("uncertain_band", DEFAULT_UNCERTAIN_BAND)
    lo, hi = band["lo"], band["hi"]

    if lo < bot_proba < hi:
        verdict = "uncertain"
    else:
        verdict = "bot" if bot_proba >= BOT_PROBABILITY_THRESHOLD else "human"

    result = {
        "address": address,
        "n_tx_fetched": n_tx,
        "bot_probability": round(bot_proba, 4),
        "verdict": verdict,
        "features": {k: round(v, 4) if isinstance(v, float) else v for k, v in feats.items()
                     if k not in ("address", "archetype")},
    }
    if verdict == "uncertain":
        result["warning"] = (
            f"bot_probability ({bot_proba:.2f}) falls inside the model's uncertain band "
            f"[{lo}, {hi}], selected via cross-validation specifically because predictions "
            f"in this range are unreliable. This is exactly the region where quiet custodial/exchange "
            f"wallets (bot-labeled by identity, but behave quietly) and human batch-payers "
            f"(human-labeled, but behave with mechanical regularity) overlap in feature "
            f"space -- see eval_results.json's uncertain_band_selection and the held-out "
            f"confusion matrix for the specific cases this is modeled on. Route this address "
            f"to manual review rather than trusting the hard label."
        )
    if n_tx < MIN_TX_FOR_RELIABLE_PREDICTION:
        result["warning"] = (
            f"only {n_tx} transactions fetched (< {MIN_TX_FOR_RELIABLE_PREDICTION}) -- "
            f"interval-based features are degenerate with this little history (near-zero "
            f"or undefined variance by construction, not by behavior), so the model has "
            f"very little real signal to go on here. Treat this verdict as low-confidence "
            f"regardless of which way it came out."
        )
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("address", help="CKB address to classify (mainnet)")
    ap.add_argument("--model", default=MODEL_PATH)
    ap.add_argument("--max-tx", type=int, default=300, help="max recent transactions to sample")
    ap.add_argument("--json", action="store_true", help="print raw JSON instead of a human-readable summary")
    args = ap.parse_args()

    try:
        result = classify(args.address, model_path=args.model, max_tx=args.max_tx)
    except (ApiError, ModelLoadError) as e:
        if args.json:
            print(json.dumps({"address": args.address, "verdict": "error", "reason": str(e)}))
        else:
            print(f"Could not classify {args.address}: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(result, indent=2))
        return

    print(f"Address:      {result['address']}")
    print(f"Transactions: {result['n_tx_fetched']} sampled")
    if result["verdict"] == "unknown":
        print(f"Verdict:      unknown -- {result['reason']}")
        return
    # print(f"Model:            {result['model_type']}")
    print(f"Bot probability:  {result['bot_probability']*100:.1f}%")
    if result["verdict"] == "uncertain":
        print(f"Verdict:          UNCERTAIN -- needs manual review, not a confident call either way")
    else:
        print(f"Verdict:          {result['verdict'].upper()}")
    if "warning" in result:
        print(f"\nWARNING: {result['warning']}")


if __name__ == "__main__":
    main()

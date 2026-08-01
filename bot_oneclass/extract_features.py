"""
Feature extraction for the ckb-bot-simulator dataset.

Turns each bot's raw NDJSON transaction log into a fixed-length behavioral
feature vector suitable for one-class modeling (Isolation Forest / One-Class
SVM / autoencoder). Features are chosen to be generator-agnostic where
possible (ratios, coefficients of variation, entropy) rather than raw
absolute values like "70 CKB" or "1200 shannon fee", which are artifacts of
this specific simulator and would let a model cheat.
"""
import json
import glob
import math
import os
import statistics as stats
from collections import Counter

DATA_DIR = "intel-synthetic/data/run_300bots_100tx"


def shannon_entropy(counts):
    total = sum(counts)
    if total == 0:
        return 0.0
    probs = [c / total for c in counts if c > 0]
    return -sum(p * math.log2(p) for p in probs)


def load_bot_txs(bot_index):
    path = os.path.join(DATA_DIR, f"add_{bot_index + 1}.json")
    lines = list(open(path))
    txs = [json.loads(l) for l in lines[1:]]
    return txs


def extract_features(bot, txs):
    addr = bot["address"]
    ts = sorted(int(t["block_timestamp"]) for t in txs)
    intervals = [(ts[i + 1] - ts[i]) / 1000.0 for i in range(len(ts) - 1)]

    caps, n_outputs_per_tx, counterparty_counter, fees = [], [], Counter(), []
    for t in txs:
        fees.append(float(t["transaction_fee"]))
        outs = [o for o in t["display_outputs"] if o["address_hash"] != addr]
        n_outputs_per_tx.append(len(outs))
        for o in outs:
            caps.append(float(o["capacity"]) / 1e8)  # shannon -> CKB
            counterparty_counter[o["address_hash"]] += 1

    def safe_cv(vals):
        if len(vals) < 2:
            return 0.0
        m = stats.mean(vals)
        return (stats.stdev(vals) / m) if m else 0.0

    def safe_mean(vals):
        return stats.mean(vals) if vals else 0.0

    # pure-receive txs: this address appears only as a recipient, sends nothing
    # externally in that tx. High values here are the custodial/cold-storage
    # "quiet bot" signature (huge n_tx, near-zero external fan-out) that
    # mean_outputs_per_tx alone blurs together with a bot that sends small,
    # frequent amounts.
    n_pure_receive_tx = sum(1 for n in n_outputs_per_tx if n == 0)
    inbound_only_tx_frac = (n_pure_receive_tx / len(txs)) if txs else 0.0

    # max (not mean) outputs in a single tx: a payroll/batch-payer shows up as
    # ONE tx with dozens of outputs; a bot with a similar *mean* usually gets
    # there via many separate small-fanout sends instead. Mean conflates the
    # two shapes, max/mean tells them apart.
    max_outputs_per_tx = max(n_outputs_per_tx) if n_outputs_per_tx else 0
    mean_outs = safe_mean(n_outputs_per_tx)
    max_over_mean_outputs = (max_outputs_per_tx / mean_outs) if mean_outs else 0.0

    n_unique_counterparties = len(counterparty_counter)
    # how skewed is the counterparty distribution (0 = perfectly even rotation,
    # higher = concentrated on one/few counterparties, e.g. market_maker/fan_in sink)
    cp_entropy = shannon_entropy(list(counterparty_counter.values()))
    max_possible_entropy = math.log2(n_unique_counterparties) if n_unique_counterparties > 1 else 1.0
    cp_entropy_norm = cp_entropy / max_possible_entropy if max_possible_entropy else 0.0

    feats = {
        "address": addr,
        "archetype": bot["archetype"],
        # timing regularity -- the core "bot smell": how close to a metronome is this?
        "interval_cv": safe_cv(intervals),
        "interval_mean_log": math.log1p(safe_mean(intervals)),
        # amount regularity
        "capacity_cv": safe_cv(caps),
        # fee regularity (less discriminating here since fee is fixed by tx size,
        # but included since a real dataset might vary it)
        "fee_cv": safe_cv(fees),
        # fan-out / fan-in shape
        "n_unique_counterparties": n_unique_counterparties,
        "counterparty_entropy_norm": cp_entropy_norm,
        "mean_outputs_per_tx": safe_mean(n_outputs_per_tx),
        # burstiness: ratio of max to mean interval (metronomic sends -> ~1.0)
        "interval_max_over_mean": (max(intervals) / safe_mean(intervals)) if intervals and safe_mean(intervals) else 0.0,
        # NEW: targets the custodial/cold-storage "quiet bot" vs. batch-payer
        # "structural bot" confusion found in eval_results.json's held-out
        # errors. Not yet wired into the deployed model -- real_features.json
        # was built from precomputed aggregates without raw per-tx output
        # data, so these can't be backfilled for existing rows. Re-run
        # extract_features_real.py against fetch_real_data.py's raw output
        # (intel-synthetic/data/real/*/addr_*.json) to populate them, then
        # add both column names to FEATURE_COLS in train_eval.py.
        "inbound_only_tx_frac": inbound_only_tx_frac,
        "max_over_mean_outputs": max_over_mean_outputs,
        "n_tx": len(txs),
    }
    return feats


def main():
    bots = json.load(open(os.path.join(DATA_DIR, "bots.json")))
    rows = []
    for b in bots:
        txs = load_bot_txs(b["index"])
        rows.append(extract_features(b, txs))

    out_path = "bot_oneclass/features.json"
    with open(out_path, "w") as f:
        json.dump(rows, f, indent=2)
    print(f"wrote {len(rows)} feature rows to {out_path}")
    print("feature keys:", [k for k in rows[0].keys() if k not in ("address", "archetype")])


if __name__ == "__main__":
    main()

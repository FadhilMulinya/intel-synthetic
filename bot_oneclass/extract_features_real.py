"""
Turns the real address data collected by intel-synthetic/fetch_real_data.py
into the same feature-row shape used for synthetic bots, by calling
extract_features.extract_features() directly -- not reimplementing it, so
real and synthetic features can never silently drift apart.

Run from the ckb_model/ parent directory (the one containing both
bot_oneclass/ and intel-synthetic/), same convention as extract_features.py:

    python3 bot_oneclass/extract_features_real.py

Output rows carry `label_source` ("bot_like" / "human_like") instead of
`archetype`, and `data_origin: "real_mainnet"`, specifically so they can
never be silently concatenated with the synthetic set and mistaken for it
downstream.
"""
import argparse
import json
import os
import sys

import extract_features as ef  # same-directory import: reuse extract_features() unmodified


def load_bucket(bucket_dir):
    manifest_path = os.path.join(bucket_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        return []
    manifest = json.load(open(manifest_path))
    rows = []
    for entry in manifest:
        addr_path = os.path.join(bucket_dir, f"addr_{entry['index']}.json")
        if not os.path.exists(addr_path):
            continue
        lines = list(open(addr_path))
        if not lines:
            continue
        header = json.loads(lines[0])
        txs = [json.loads(l) for l in lines[1:]]
        if len(txs) < 2:
            continue  # can't compute interval stats from 0-1 transactions
        # extract_features() only reads bot["address"] and bot["archetype"];
        # feed label_source through the archetype slot, then relabel + tag
        # provenance on the way out so it's unambiguous downstream.
        fake_bot = {"address": header["address"], "archetype": entry["label_source"]}
        feats = ef.extract_features(fake_bot, txs)
        feats["label_source"] = feats.pop("archetype")
        feats["data_origin"] = "real_mainnet"
        rows.append(feats)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--real-data-dir", default="intel-synthetic/data/real",
                     help="output dir from fetch_real_data.py (contains bot_like/ and human_like/ subdirs)")
    ap.add_argument("--out", default="bot_oneclass/real_features.json")
    args = ap.parse_args()

    all_rows = []
    for bucket in ("bot_like", "human_like"):
        bucket_dir = os.path.join(args.real_data_dir, bucket)
        rows = load_bucket(bucket_dir)
        print(f"{bucket}: extracted features for {len(rows)} addresses "
              f"(from {bucket_dir})", file=sys.stderr)
        all_rows.extend(rows)

    if not all_rows:
        print(f"WARNING: no real feature rows extracted -- check --real-data-dir "
              f"({args.real_data_dir}) points at fetch_real_data.py's --out-dir", file=sys.stderr)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(all_rows, f, indent=2)
    print(f"wrote {len(all_rows)} real-data feature rows to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()

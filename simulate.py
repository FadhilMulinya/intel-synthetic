"""
CKB bot-transaction simulator. Spins up N funded bot wallets on a local CKB
devnet (via offckb), runs them concurrently with fixed, non-random,
archetype-specific timing/amount/counterparty parameters, and streams each
bot's submitted+confirmed transactions to its own per-address JSON file.

Usage:
    python simulate.py --bots 20 --txs-per-bot 100 --out stage1_20bots_100tx
    python simulate.py --bots 300 --txs-per-bot 100 --out run_300bots_100tx --duration-hours 12

See README.md for archetype definitions and the add_N.json / bots.json
schema, and CONTRIBUTING.md for how this file's logic is split across
archetypes.py / funding.py / recorder.py / worker.py.
"""
import argparse
import json
import os
import queue
import threading
import time
from collections import defaultdict

import ckb
from archetypes import ARCHETYPES, build_roster, compute_intervals
from funding import fund_bots, parse_offckb_accounts
from recorder import TxWriter, bot_tx_path, confirmation_worker, patch_output_status
from worker import bot_worker


def run_stage(n_bots, txs_per_bot, out_dir, duration_hours=None):
    os.makedirs(out_dir, exist_ok=True)
    bots_path = os.path.join(out_dir, "bots.json")

    intervals = compute_intervals(txs_per_bot, duration_hours)
    span_desc = f", spread over ~{duration_hours}h" if duration_hours else ""
    print(f"=== Stage: {n_bots} bots, {txs_per_bot} txs/bot -> {out_dir}{span_desc} ===")
    print(f"Per-archetype intervals (seconds): { {k: round(v, 1) for k, v in intervals.items()} }")
    ckb.discover_system_script()
    genesis_accounts = parse_offckb_accounts()

    start = time.time()
    print(f"Generating {n_bots} keypairs and assigning archetypes...")
    bots = build_roster(n_bots, intervals)

    bots_json = []
    for b in bots:
        params = dict(b["params"])
        for key in ("counterparties", "partners", "recipients", "feeders"):
            if key in params:
                params[key] = [bots[i]["address"] for i in params[key]]
        if "sink" in params:
            params["sink"] = bots[params["sink"]]["address"]
        bots_json.append({
            "index": b["index"],
            "address": b["address"],
            "lock_arg": b["lock_arg"],
            "pubkey": b["pubkey"],
            "privkey": b["privkey"],
            "archetype": b["archetype"],
            "params": params,
        })
    with open(bots_path, "w") as f:
        json.dump(bots_json, f, indent=2)
    print(f"Wrote {bots_path}")

    fund_bots(bots, genesis_accounts, txs_per_bot)

    writers = {
        bot["address"]: TxWriter(bot_tx_path(out_dir, bot["index"]), bot["address"])
        for bot in bots
    }
    confirm_queue = queue.Queue()
    counters = {"submitted": 0, "confirmed": 0}
    counters_lock = threading.Lock()

    n_watchers = min(32, max(4, n_bots // 4))
    watchers = [threading.Thread(target=confirmation_worker, args=(confirm_queue, writers, counters, counters_lock), daemon=True)
                for _ in range(n_watchers)]
    for w in watchers:
        w.start()

    print(f"Running {n_bots} bots concurrently until each has sent {txs_per_bot} transactions...")
    threads = [threading.Thread(target=bot_worker, args=(bot, bots, txs_per_bot, confirm_queue, counters, counters_lock))
               for bot in bots]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print("All bots finished submitting. Waiting for remaining confirmations...")
    confirm_queue.join()
    for _ in watchers:
        confirm_queue.put(None)
    for w in watchers:
        w.join(timeout=5)
    for w in writers.values():
        w.close()

    print("Patching output cell status (live/dead) from the final transaction set...")
    for bot in bots:
        patch_output_status(bot_tx_path(out_dir, bot["index"]))

    runtime = time.time() - start

    per_archetype = defaultdict(int)
    total = 0
    for bot in bots:
        path = bot_tx_path(out_dir, bot["index"])
        with open(path) as f:
            lines = [line for line in f if line.strip()]
        for line in lines[1:]:  # lines[0] is the {"address": ...} header
            rec = json.loads(line)
            per_archetype[rec["archetype"]] += 1
            total += 1

    print()
    print(f"=== Stage complete: {out_dir} ===")
    print(f"Total transactions: {total}")
    for a in ARCHETYPES:
        print(f"  {a}: {per_archetype.get(a, 0)}")
    print(f"Runtime: {runtime:.1f}s")
    return {"total": total, "per_archetype": dict(per_archetype), "runtime_sec": runtime}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bots", type=int, required=True)
    parser.add_argument("--txs-per-bot", type=int, required=True)
    parser.add_argument("--out", type=str, required=True, help="output subfolder name under data/")
    parser.add_argument("--duration-hours", type=float, default=None,
                         help="stretch per-archetype intervals so the whole run spans roughly this many hours "
                              "(default: fast fixed intervals, seconds/minutes total -- good for smoke tests)")
    args = parser.parse_args()

    out_dir = os.path.join("data", args.out)
    run_stage(args.bots, args.txs_per_bot, out_dir, duration_hours=args.duration_hours)


if __name__ == "__main__":
    main()

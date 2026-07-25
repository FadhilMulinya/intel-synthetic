"""
CKB bot-transaction simulator. Spins up N funded bot wallets on a local CKB
devnet (via offckb), runs them concurrently with fixed, non-random,
archetype-specific timing/amount/counterparty parameters, and streams each
bot's submitted+confirmed transactions to its own per-address JSON file.

Usage:
    python simulate.py --bots 20 --txs-per-bot 100 --out stage1_20bots_100tx
    python simulate.py --bots 300 --txs-per-bot 100 --out run_300bots_100tx --duration-hours 12

See README.md for archetype definitions and the add_N.json / bots.json
schema.
"""
import argparse
import json
import os
import queue
import subprocess
import threading
import time
from collections import defaultdict

import ckb

ARCHETYPES = ["periodic", "market_maker", "fan_out_hub", "fan_in_sink", "batch_payer"]

# Fixed, bot-like parameters -- deliberately constant, no jitter/randomness.
CAPACITY_CKB = 70              # just above the 61 CKB minimum cell capacity
MARKET_MAKER_ALT_CKB = 75      # tight oscillation range: 70 <-> 75 CKB

# Relative pacing "shape" across archetypes (market_maker fastest, batch_payer
# slowest) at a fast testing scale. When --duration-hours is given, every
# value here is scaled up by the same factor so the slowest archetype's
# txs_per_bot rounds span the requested duration, and all archetypes finish
# around the same wall-clock time -- see compute_intervals().
BASE_INTERVAL_SEC = {
    "periodic": 6,
    "market_maker": 2,
    "fan_out_hub": 4,
    "fan_in_sink": 5,
    "batch_payer": 8,
}
PERIODIC_COUNTERPARTY_COUNT = 3
FAN_OUT_WIDTH = 12
BATCH_PAYER_RECIPIENTS = 3
FEE_BASE_SHANNON = 1000
FEE_PER_OUTPUT_SHANNON = 200


def compute_intervals(txs_per_bot, duration_hours=None):
    """Scale BASE_INTERVAL_SEC so the whole run spans duration_hours (each
    archetype keeps its relative pacing but stretches to fit). Without
    --duration-hours, uses the fast base intervals as-is -- useful for quick
    smoke tests."""
    if duration_hours is None:
        return dict(BASE_INTERVAL_SEC)
    target_seconds = duration_hours * 3600
    slowest_base = max(BASE_INTERVAL_SEC.values())
    scale = target_seconds / (slowest_base * txs_per_bot)
    return {k: v * scale for k, v in BASE_INTERVAL_SEC.items()}


# ---------------------------------------------------------------------------
# genesis accounts
# ---------------------------------------------------------------------------

def parse_offckb_accounts():
    out = subprocess.run(["offckb", "accounts"], capture_output=True, text=True, check=True).stdout
    accounts = []
    current = {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith('- "#":'):
            if current:
                accounts.append(current)
            current = {}
        elif line.startswith("address:"):
            current["address"] = line.split(":", 1)[1].strip()
        elif line.startswith("privkey:"):
            current["privkey"] = line.split(":", 1)[1].strip()
        elif line.startswith("lock_arg:"):
            current["lock_arg"] = line.split(":", 1)[1].strip()
    if current:
        accounts.append(current)
    if not accounts:
        raise RuntimeError("no genesis accounts parsed from `offckb accounts`")
    return accounts


# ---------------------------------------------------------------------------
# bot roster: keys, archetype assignment, per-archetype fixed parameters
# ---------------------------------------------------------------------------

def build_roster(n, intervals):
    bots = []
    for i in range(n):
        privkey, pubkey, lock_arg, address = ckb.generate_keypair()
        bots.append({
            "index": i,
            "privkey": privkey,
            "pubkey": pubkey,
            "lock_arg": lock_arg,
            "address": address,
            "archetype": ARCHETYPES[i % len(ARCHETYPES)],
        })
    assign_params(bots, intervals)
    return bots


def assign_params(bots, intervals):
    n = len(bots)
    by_archetype = defaultdict(list)
    for b in bots:
        by_archetype[b["archetype"]].append(b["index"])

    for idx in by_archetype["periodic"]:
        offsets = range(1, PERIODIC_COUNTERPARTY_COUNT + 1)
        cps = sorted({(idx + off) % n for off in offsets} - {idx})
        bots[idx]["params"] = {
            "interval_sec": intervals["periodic"],
            "capacity_ckb": CAPACITY_CKB,
            "counterparties": cps,
        }

    mm = by_archetype["market_maker"]
    i = 0
    while i < len(mm):
        if len(mm) - i >= 3 and (len(mm) - i) % 2 == 1:
            group, i = mm[i:i + 3], i + 3
        else:
            group, i = mm[i:i + 2], i + 2
        if len(group) < 2:
            group = mm[-2:]  # fold a lone leftover into the last pair
        for gi in group:
            partners = [x for x in group if x != gi]
            bots[gi]["params"] = {
                "interval_sec": intervals["market_maker"],
                "capacity_ckb_base": CAPACITY_CKB,
                "capacity_ckb_alt": MARKET_MAKER_ALT_CKB,
                "partners": partners,
            }

    for idx in by_archetype["fan_out_hub"]:
        width = min(FAN_OUT_WIDTH, n - 1)
        cps = sorted({(idx + off) % n for off in range(1, width + 1)} - {idx})
        bots[idx]["params"] = {
            "interval_sec": intervals["fan_out_hub"],
            "capacity_ckb": CAPACITY_CKB,
            "counterparties": cps,
        }

    fis = by_archetype["fan_in_sink"]
    if fis:
        sink_idx = fis[0]
        feeders = fis[1:] or [fis[0]]  # degenerate case: nothing else to do, self-loop guarded below
        bots[sink_idx]["params"] = {
            "interval_sec": intervals["fan_in_sink"],
            "capacity_ckb": CAPACITY_CKB,
            "role": "sink",
            "feeders": [f for f in feeders if f != sink_idx] or feeders,
        }
        for f in fis[1:]:
            bots[f]["params"] = {
                "interval_sec": intervals["fan_in_sink"],
                "capacity_ckb": CAPACITY_CKB,
                "role": "feeder",
                "sink": sink_idx,
            }

    for idx in by_archetype["batch_payer"]:
        k = min(BATCH_PAYER_RECIPIENTS, n - 1)
        recipients = sorted({(idx + off) % n for off in range(1, k + 1)} - {idx})
        bots[idx]["params"] = {
            "interval_sec": intervals["batch_payer"],
            "capacity_ckb": CAPACITY_CKB,
            "recipients": recipients,
        }


def compute_round_outputs(bot, round_num):
    """Returns [(target_bot_index, capacity_shannon), ...] for this round."""
    p = bot["params"]
    a = bot["archetype"]
    cap = lambda ckb_amount: ckb_amount * ckb.SHANNONS_PER_CKB

    if a == "periodic" or a == "fan_out_hub":
        cps = p["counterparties"]
        target = cps[round_num % len(cps)]
        return [(target, cap(p["capacity_ckb"]))]
    if a == "market_maker":
        partners = p["partners"]
        target = partners[round_num % len(partners)]
        amount = p["capacity_ckb_base"] if round_num % 2 == 0 else p["capacity_ckb_alt"]
        return [(target, cap(amount))]
    if a == "fan_in_sink":
        if p["role"] == "feeder":
            return [(p["sink"], cap(p["capacity_ckb"]))]
        feeders = p["feeders"]
        target = feeders[round_num % len(feeders)]
        return [(target, cap(p["capacity_ckb"]))]
    if a == "batch_payer":
        return [(r, cap(p["capacity_ckb"])) for r in p["recipients"]]
    raise ValueError(f"unknown archetype {a}")


# ---------------------------------------------------------------------------
# funding
# ---------------------------------------------------------------------------

def estimate_funding_shannon(txs_per_bot):
    """min cell capacity + fee, per tx, times txs_per_bot, plus a safety buffer."""
    max_outputs_per_round = max(BATCH_PAYER_RECIPIENTS, 1)
    per_round = max_outputs_per_round * CAPACITY_CKB * ckb.SHANNONS_PER_CKB
    per_round += FEE_BASE_SHANNON + FEE_PER_OUTPUT_SHANNON * max_outputs_per_round
    total = per_round * txs_per_bot
    buffer = total // 2 + ckb.MIN_CELL_CAPACITY_SHANNON * 5
    return total + buffer


_block_timestamp_cache = {}
_block_timestamp_lock = threading.Lock()


def get_block_timestamp_ms(block_number):
    """Cached per block_number since many transactions in a run share a
    block and each lookup is its own RPC round trip."""
    with _block_timestamp_lock:
        if block_number in _block_timestamp_cache:
            return _block_timestamp_cache[block_number]
    block = ckb.get_block_by_number(block_number)
    ts = int(block["header"]["timestamp"], 16)
    with _block_timestamp_lock:
        _block_timestamp_cache[block_number] = ts
    return ts


def wait_for_confirmation(tx_hash, timeout=180, poll=1.0):
    start = time.time()
    while time.time() - start < timeout:
        res = ckb.get_transaction(tx_hash)
        status = res["tx_status"]["status"]
        if status == "committed":
            return int(res["tx_status"]["block_number"], 16)
        if status == "rejected":
            raise RuntimeError(f"tx {tx_hash} rejected: {res['tx_status']}")
        time.sleep(poll)
    raise TimeoutError(f"tx {tx_hash} did not confirm within {timeout}s")


def fund_bots(bots, genesis_accounts, txs_per_bot):
    funding_shannon = estimate_funding_shannon(txs_per_bot)
    print(f"Funding each of {len(bots)} bots with {funding_shannon / ckb.SHANNONS_PER_CKB:,.0f} CKB "
          f"(estimated for {txs_per_bot}+ txs)...")

    groups = defaultdict(list)
    for i, bot in enumerate(bots):
        groups[i % len(genesis_accounts)].append(bot)

    pending = []  # (tx_hash, group_bots)
    for gi, group_bots in groups.items():
        genesis = genesis_accounts[gi]
        outputs = [(b["lock_arg"], funding_shannon) for b in group_bots]
        fee = FEE_BASE_SHANNON + FEE_PER_OUTPUT_SHANNON * len(outputs)
        tx_json, tx_hash = ckb.build_and_sign_transfer(genesis["privkey"], genesis["lock_arg"], outputs, fee_shannon=fee)
        ckb.send_transaction(tx_json)
        pending.append((tx_hash, group_bots))

    for tx_hash, group_bots in pending:
        wait_for_confirmation(tx_hash)
        for out_index, bot in enumerate(group_bots):
            bot["initial_cell"] = {
                "out_point": {"tx_hash": tx_hash, "index": hex(out_index)},
                "capacity": funding_shannon,
            }
    print("Funding confirmed for all bots.")
    return funding_shannon


# ---------------------------------------------------------------------------
# streaming writer + confirmation watchers
# ---------------------------------------------------------------------------

class TxWriter:
    """Appends newline-delimited JSON records to one bot's add_N.json.
    NDJSON (one JSON object per line) rather than a single JSON array, so a
    write is a single atomic append and partial data survives an
    interruption -- rewriting a whole top-level array on every tx would risk
    corrupting it if the process is killed mid-write. The first line is
    always {"address": ...} identifying whose transactions the file holds."""

    def __init__(self, path, address):
        self._lock = threading.Lock()
        self._f = open(path, "a", buffering=1)
        if self._f.tell() == 0:
            self._f.write(json.dumps({"address": address}) + "\n")
            self._f.flush()

    def write(self, record):
        with self._lock:
            self._f.write(json.dumps(record) + "\n")
            self._f.flush()

    def close(self):
        self._f.close()


def confirmation_worker(work_queue, writers, counters, counters_lock):
    while True:
        item = work_queue.get()
        if item is None:
            work_queue.task_done()
            return
        tx_hash, record = item
        try:
            block_number = wait_for_confirmation(tx_hash)
            block_timestamp_ms = get_block_timestamp_ms(block_number)
            record["block_number"] = str(block_number)
            record["block_timestamp"] = str(block_timestamp_ms)
            record["tx_status"] = "committed"
            sender_address = record["display_inputs"][0]["address_hash"]
            writers[sender_address].write(record)
            with counters_lock:
                counters["confirmed"] += 1
        except Exception as e:
            print(f"WARNING: {tx_hash} did not confirm: {e}")
        finally:
            work_queue.task_done()


# ---------------------------------------------------------------------------
# bot worker
# ---------------------------------------------------------------------------

def bot_worker(bot, bots, txs_per_bot, confirm_queue, counters, counters_lock):
    """Each bot spends its own single-cell UTXO chain, seeded once at funding
    time. The chain is only ever touched by this bot's own thread (no shared
    queue), so there is no cross-thread race on which cell to spend next --
    a bot never needs another bot's funds to hit its send quota, since every
    bot is funded generously enough to run its whole quota off its own
    chain. Real inter-bot transfers still happen and are recorded on-chain
    and in the *sending* bot's own add_N.json (never the recipient's); the
    simulator just doesn't route the recipient's *own* future sends through
    funds it received from others."""
    round_num = 0
    cell = bot["initial_cell"]
    while round_num < txs_per_bot:
        t0 = time.time()
        outputs_idx = compute_round_outputs(bot, round_num)
        outputs = [(bots[ti]["lock_arg"], cap) for ti, cap in outputs_idx]
        fee = FEE_BASE_SHANNON + FEE_PER_OUTPUT_SHANNON * len(outputs)

        try:
            tx_json, tx_hash, change_cell, tx_size_bytes = ckb.build_and_sign_transfer_from_cell(
                bot["privkey"], bot["lock_arg"], cell, outputs, fee_shannon=fee)
            ckb.send_transaction(tx_json)
        except Exception as e:
            print(f"WARNING: bot {bot['index']} ({bot['archetype']}) round {round_num} failed: {e}")
            time.sleep(1)
            continue

        if change_cell is None:
            print(f"WARNING: bot {bot['index']} ({bot['archetype']}) has no change cell left, stopping early")
            return

        display_outputs = [{
            "id": None,
            "capacity": f"{capacity}.0",
            "occupied_capacity": str(ckb.MIN_CELL_CAPACITY_SHANNON),
            "address_hash": bots[target_idx]["address"],
            "status": "live",
            "consumed_tx_hash": "",
            "cell_type": "normal",
            "generated_tx_hash": tx_hash,
            "cell_index": str(i),
        } for i, (target_idx, capacity) in enumerate(outputs_idx)]
        change_index = int(change_cell["out_point"]["index"], 16)
        display_outputs.append({
            "id": None,
            "capacity": f"{change_cell['capacity']}.0",
            "occupied_capacity": str(ckb.MIN_CELL_CAPACITY_SHANNON),
            "address_hash": bot["address"],
            "status": "live",
            "consumed_tx_hash": "",
            "cell_type": "normal",
            "generated_tx_hash": tx_hash,
            "cell_index": str(change_index),
        })

        record = {
            "transaction_hash": tx_hash,
            "version": "0",
            "is_cellbase": False,
            "transaction_fee": str(fee),
            "bytes": tx_size_bytes,
            "cycles": None,
            "cell_deps": [{
                "out_point": {"tx_hash": ckb.SIGHASH_DEP["tx_hash"], "index": ckb.SIGHASH_DEP["index"]},
                "dep_type": ckb.SIGHASH_DEP["dep_type"],
            }],
            "header_deps": [],
            "witnesses": tx_json["witnesses"],
            "display_inputs": [{
                "id": None,
                "from_cellbase": False,
                "capacity": f"{cell['capacity']}.0",
                "occupied_capacity": str(ckb.MIN_CELL_CAPACITY_SHANNON),
                "address_hash": bot["address"],
                "generated_tx_hash": cell["out_point"]["tx_hash"],
                "cell_index": str(int(cell["out_point"]["index"], 16)),
                "cell_type": "normal",
            }],
            "display_outputs": display_outputs,
            "archetype": bot["archetype"],
        }
        confirm_queue.put((tx_hash, record))

        cell = change_cell

        with counters_lock:
            counters["submitted"] += 1
        round_num += 1

        elapsed = time.time() - t0
        time.sleep(max(0.0, bot["params"]["interval_sec"] - elapsed))


def bot_tx_path(out_dir, bot_index):
    return os.path.join(out_dir, f"add_{bot_index + 1}.json")


def patch_output_status(tx_path):
    """One-time end-of-run pass: mark display_outputs entries "dead" (with
    consumed_tx_hash) wherever a later record's display_inputs shows them
    being spent. Bots only ever spend their own change chain (never funds
    received from other bots -- see bot_worker docstring), so a bot's own
    successive change outputs -- and the spends that consume them -- always
    live in that same bot's own add_N.json file; transfer-recipient outputs
    legitimately stay "live" since the simulator never spends them. Runs
    once after all confirmations are in, not per-line, so it doesn't affect
    the "partial data survives interruption" property of the in-progress
    NDJSON append. The first line (the {"address": ...} header) is passed
    through untouched."""
    with open(tx_path) as f:
        lines = [line for line in f if line.strip()]
    header, records = lines[0], [json.loads(line) for line in lines[1:]]

    spent = {}  # (generated_tx_hash, cell_index) -> consuming transaction_hash
    for r in records:
        for inp in r["display_inputs"]:
            spent[(inp["generated_tx_hash"], inp["cell_index"])] = r["transaction_hash"]

    for r in records:
        for out in r["display_outputs"]:
            key = (r["transaction_hash"], out["cell_index"])
            if key in spent:
                out["status"] = "dead"
                out["consumed_tx_hash"] = spent[key]

    tmp = tx_path + ".tmp"
    with open(tmp, "w") as f:
        f.write(header if header.endswith("\n") else header + "\n")
        for r in records:
            f.write(json.dumps(r) + "\n")
    os.replace(tmp, tx_path)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

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

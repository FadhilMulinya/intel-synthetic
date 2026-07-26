"""Resumes an interrupted simulate.py run: continues each unfinished bot
from its current on-chain cell toward the same txs-per-bot target, reusing
the fixed per-bot archetype parameters already recorded in bots.json (so
pacing and counterparty choices are identical to the original run -- this
is just that same run picking back up, not a new one).

A bot's current cell is the largest of its *fully paginated* live cells
(not just the first get_cells page) -- funding/change is always far
bigger than any single received transfer (61-75 CKB), so this reliably
finds the chain tip. This deliberately does not trust the last recorded
transaction's own change output: bots submit several rounds back-to-back
without waiting for confirmation in between (see README.md's concurrency
section), so a hard kill can leave transactions that already confirmed
on-chain but were never written to add_N.json -- the file's last line is
not guaranteed to be the real chain tip. Those few confirmed-but-unrecorded
rounds (if any) are simply not present in the output dataset; resuming
just picks up from the true current cell and keeps recording forward from
there, same as any other round.

Usage:
    python3 resume.py --dir data/run_300bots_500tx --txs-per-bot 500
"""
import argparse
import json
import os
import queue
import threading
from collections import defaultdict

import ckb
from recorder import TxWriter, bot_tx_path, confirmation_worker, patch_output_status
from worker import bot_worker


def load_roster(out_dir):
    with open(os.path.join(out_dir, "bots.json")) as f:
        bots_json = json.load(f)
    address_to_index = {b["address"]: b["index"] for b in bots_json}

    bots = []
    for b in bots_json:
        params = dict(b["params"])
        for key in ("counterparties", "partners", "recipients", "feeders"):
            if key in params:
                params[key] = [address_to_index[a] for a in params[key]]
        if "sink" in params:
            params["sink"] = address_to_index[params["sink"]]
        bots.append({**b, "params": params})
    return bots


def repair_truncated_file(path):
    """A hard-killed writer can leave trailing NUL bytes (pre-allocated but
    never-written filesystem space) after the last complete JSON line, or a
    partially-written final line. Truncate back to the last line that
    actually parses as JSON -- that trailing garbage carries no data, so
    this loses nothing, and leaves the file exactly as if the writer had
    stopped cleanly one line earlier."""
    if not os.path.exists(path):
        return
    with open(path, "rb") as f:
        raw = f.read()
    if b"\x00" not in raw:
        return

    kept = []
    for line in raw.split(b"\n"):
        stripped = line.strip(b"\x00").strip()
        if not stripped:
            continue
        try:
            json.loads(stripped)
        except ValueError:
            break
        kept.append(stripped)

    tmp = path + ".repair.tmp"
    with open(tmp, "wb") as f:
        for line in kept:
            f.write(line + b"\n")
    os.replace(tmp, path)
    print(f"repaired {path}: dropped trailing corruption after {len(kept)} valid lines")


def count_already_sent(out_dir, bot_index):
    path = bot_tx_path(out_dir, bot_index)
    if not os.path.exists(path):
        return 0
    with open(path) as f:
        return sum(1 for _ in f) - 1  # first line is the {"address": ...} header


def current_chain_cell(bot):
    cells = ckb.get_all_live_cells_for_lock_arg(bot["lock_arg"])
    if not cells:
        raise RuntimeError(f"bot {bot['index']} ({bot['address']}) has no live cells to resume from")
    biggest = max(cells, key=lambda c: int(c["output"]["capacity"], 16))
    return {"out_point": biggest["out_point"], "capacity": int(biggest["output"]["capacity"], 16)}


def resume(out_dir, txs_per_bot):
    ckb.discover_system_script()
    bots = load_roster(out_dir)

    for bot in bots:
        repair_truncated_file(bot_tx_path(out_dir, bot["index"]))

    todo = []
    for bot in bots:
        already_sent = count_already_sent(out_dir, bot["index"])
        if already_sent >= txs_per_bot:
            continue
        bot["start_round"] = already_sent
        bot["start_cell"] = current_chain_cell(bot)
        todo.append(bot)

    print(f"Resuming {out_dir}: {len(todo)}/{len(bots)} bots still short of {txs_per_bot} txs.")
    if not todo:
        print("Nothing to do.")
        return

    writers = {bot["address"]: TxWriter(bot_tx_path(out_dir, bot["index"]), bot["address"]) for bot in bots}
    confirm_queue = queue.Queue()
    counters = {"submitted": 0, "confirmed": 0}
    counters_lock = threading.Lock()

    n_watchers = min(32, max(4, len(todo) // 4))
    watchers = [threading.Thread(target=confirmation_worker, args=(confirm_queue, writers, counters, counters_lock), daemon=True)
                for _ in range(n_watchers)]
    for w in watchers:
        w.start()

    threads = [threading.Thread(target=bot_worker, args=(bot, bots, txs_per_bot, confirm_queue, counters, counters_lock),
                                 kwargs={"start_round": bot["start_round"], "start_cell": bot["start_cell"]})
               for bot in todo]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print("All resumed bots finished submitting. Waiting for remaining confirmations...")
    confirm_queue.join()
    for _ in watchers:
        confirm_queue.put(None)
    for w in watchers:
        w.join(timeout=5)
    for w in writers.values():
        w.close()

    print("Patching output cell status (live/dead) from the full transaction set...")
    for bot in bots:
        patch_output_status(bot_tx_path(out_dir, bot["index"]))

    per_archetype = defaultdict(int)
    total = 0
    for bot in bots:
        with open(bot_tx_path(out_dir, bot["index"])) as f:
            lines = [line for line in f if line.strip()]
        for line in lines[1:]:
            rec = json.loads(line)
            per_archetype[rec["archetype"]] += 1
            total += 1
    print(f"Total transactions across all bots now: {total}")
    for a, n in sorted(per_archetype.items()):
        print(f"  {a}: {n}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", required=True, help="existing run directory containing bots.json + add_N.json files")
    parser.add_argument("--txs-per-bot", type=int, required=True, help="the original run's --txs-per-bot target")
    args = parser.parse_args()
    resume(args.dir, args.txs_per_bot)


if __name__ == "__main__":
    main()

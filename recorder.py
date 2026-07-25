"""Per-bot NDJSON output: the streaming writer, the confirmation watchers
that feed it, and the end-of-run live/dead cell-status patch pass."""
import json
import os
import threading

from funding import get_block_timestamp_ms, wait_for_confirmation


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


def bot_tx_path(out_dir, bot_index):
    return os.path.join(out_dir, f"add_{bot_index + 1}.json")


def patch_output_status(tx_path):
    """One-time end-of-run pass: mark display_outputs entries "dead" (with
    consumed_tx_hash) wherever a later record's display_inputs shows them
    being spent. Bots only ever spend their own change chain (never funds
    received from other bots -- see bot_worker's docstring), so a bot's own
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

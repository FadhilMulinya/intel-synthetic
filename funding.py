"""Genesis account discovery, per-bot funding estimation/transfer, and
transaction-confirmation polling shared by the funding step and the
confirmation watchers."""
import subprocess
import threading
import time
from collections import defaultdict

import ckb
from archetypes import BATCH_PAYER_RECIPIENTS, CAPACITY_CKB

FEE_BASE_SHANNON = 1000
FEE_PER_OUTPUT_SHANNON = 200


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

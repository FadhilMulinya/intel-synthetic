"""Per-bot send loop: builds, signs, and submits one transaction per round,
then hands it off to the confirmation-watcher queue."""
import time

import ckb
from archetypes import compute_round_outputs
from funding import FEE_BASE_SHANNON, FEE_PER_OUTPUT_SHANNON


def bot_worker(bot, bots, txs_per_bot, confirm_queue, counters, counters_lock, start_round=0, start_cell=None):
    """Each bot spends its own single-cell UTXO chain, seeded once at funding
    time. The chain is only ever touched by this bot's own thread (no shared
    queue), so there is no cross-thread race on which cell to spend next --
    a bot never needs another bot's funds to hit its send quota, since every
    bot is funded generously enough to run its whole quota off its own
    chain. Real inter-bot transfers still happen and are recorded on-chain
    and in the *sending* bot's own add_N.json (never the recipient's); the
    simulator just doesn't route the recipient's *own* future sends through
    funds it received from others.

    start_round/start_cell let resume.py continue a bot mid-chain after an
    interrupted run: start_round keeps archetype pacing (counterparty
    cycling, market_maker's capacity parity) continuous rather than
    restarting it, and start_cell is the bot's current on-chain spendable
    cell rather than its original funding cell. A fresh run leaves both at
    their defaults."""
    round_num = start_round
    cell = start_cell if start_cell is not None else bot["initial_cell"]
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
                "out_point": {"tx_hash": ckb.config.SIGHASH_DEP["tx_hash"], "index": ckb.config.SIGHASH_DEP["index"]},
                "dep_type": ckb.config.SIGHASH_DEP["dep_type"],
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

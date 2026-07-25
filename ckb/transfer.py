"""High level: build + sign + submit a transfer (single sender, N outputs)."""
from . import config
from .hashing import ckbhash
from .keys import sign_message
from .molecule import (
    mol_cell_dep, mol_cell_input, mol_cell_output, mol_raw_transaction,
    mol_script, mol_transaction, mol_u64, mol_witness_args,
)
from .rpc import get_live_cells_for_lock_arg


def build_and_sign_transfer(privkey_hex: str, from_lock_arg_hex: str, outputs, fee_shannon=1000):
    """
    outputs: list of (to_lock_arg_hex, capacity_shannon)
    Selects live cells from the sender's own lock via the indexer (simple
    greedy, sender pays the fee and gets any leftover capacity back as a
    change output). Returns (tx_json, tx_hash_hex) or raises if insufficient
    balance. Use this only for one-off sends (e.g. genesis funding) -- bots
    use build_and_sign_transfer_from_cell instead, see its docstring for why.
    """
    total_out = sum(c for _, c in outputs) + fee_shannon
    cells = get_live_cells_for_lock_arg(from_lock_arg_hex)
    selected = []
    selected_sum = 0
    for c in cells:
        selected.append({"out_point": c["out_point"], "capacity": int(c["output"]["capacity"], 16)})
        selected_sum += selected[-1]["capacity"]
        if selected_sum >= total_out:
            break
    if selected_sum < total_out:
        raise RuntimeError(f"insufficient balance: have {selected_sum}, need {total_out}")
    return _build_and_sign_transfer_from_cells(privkey_hex, from_lock_arg_hex, selected, outputs, fee_shannon)


def build_and_sign_transfer_from_cell(privkey_hex: str, from_lock_arg_hex: str, input_cell, outputs, fee_shannon=1000):
    """
    Single-input transfer using an explicit, already-known input cell
    (`{"out_point": {"tx_hash":..., "index": "0x.."}, "capacity": int}`)
    instead of querying the indexer.

    Bots use this rather than build_and_sign_transfer because the CKB
    indexer only reflects committed (mined) cells -- if a bot waited for its
    own previous change cell to show up in the indexer before building its
    next transaction, throughput would be capped at one round per block.
    Since each bot deterministically knows its own change output the moment
    it builds a transaction, the simulator tracks each bot's current
    spendable cell locally in Python and hands it here directly, letting
    bots chain several unconfirmed transactions back-to-back.

    Returns (tx_json, tx_hash_hex, change_cell_or_None).
    """
    return _build_and_sign_transfer_from_cells(privkey_hex, from_lock_arg_hex, [input_cell], outputs, fee_shannon, return_change=True)


def _build_and_sign_transfer_from_cells(privkey_hex, from_lock_arg_hex, selected, outputs, fee_shannon, return_change=False):
    selected_sum = sum(c["capacity"] for c in selected)
    total_out = sum(c for _, c in outputs) + fee_shannon
    if selected_sum < total_out:
        raise RuntimeError(f"insufficient balance: have {selected_sum}, need {total_out}")
    change = selected_sum - total_out
    cell_deps = [mol_cell_dep(config.SIGHASH_DEP["tx_hash"], config.SIGHASH_DEP["index"], config.SIGHASH_DEP["dep_type"])]
    inputs_mol = [mol_cell_input(0, c["out_point"]["tx_hash"], int(c["out_point"]["index"], 16)) for c in selected]

    lock_script_bytes = mol_script(config.SIGHASH_CODE_HASH, "type", from_lock_arg_hex)
    outputs_mol = []
    outputs_data = []
    outputs_json = []
    for to_lock_arg, capacity in outputs:
        to_lock_bytes = mol_script(config.SIGHASH_CODE_HASH, "type", to_lock_arg)
        outputs_mol.append(mol_cell_output(capacity, to_lock_bytes))
        outputs_data.append(b"")
        outputs_json.append({
            "capacity": hex(capacity),
            "lock": {"code_hash": config.SIGHASH_CODE_HASH, "hash_type": "type", "args": to_lock_arg},
            "type": None,
        })
    change_index = None
    if change > 0:
        if change < config.MIN_CELL_CAPACITY_SHANNON:
            raise RuntimeError(f"change {change} below minimum cell capacity; add more outputs or a bigger fee")
        change_index = len(outputs_json)
        outputs_mol.append(mol_cell_output(change, lock_script_bytes))
        outputs_data.append(b"")
        outputs_json.append({
            "capacity": hex(change),
            "lock": {"code_hash": config.SIGHASH_CODE_HASH, "hash_type": "type", "args": from_lock_arg_hex},
            "type": None,
        })

    raw_tx_bytes = mol_raw_transaction(cell_deps, [], inputs_mol, outputs_mol, outputs_data)
    tx_hash = ckbhash(raw_tx_bytes)

    placeholder_witness = mol_witness_args(lock=bytes(65))
    message = tx_hash + mol_u64(len(placeholder_witness)) + placeholder_witness
    for _ in selected[1:]:
        message += mol_u64(0)  # empty witness for extra inputs in the same group
    digest = ckbhash(message)

    sig = sign_message(privkey_hex, digest)
    signed_witness = mol_witness_args(lock=sig)

    witnesses_bytes = [signed_witness] + [b""] * (len(selected) - 1)
    witnesses_hex = ["0x" + w.hex() for w in witnesses_bytes]
    tx_size_bytes = len(mol_transaction(raw_tx_bytes, witnesses_bytes))

    tx_json = {
        "version": "0x0",
        "cell_deps": [{
            "out_point": {"tx_hash": config.SIGHASH_DEP["tx_hash"], "index": hex(config.SIGHASH_DEP["index"])},
            "dep_type": config.SIGHASH_DEP["dep_type"],
        }],
        "header_deps": [],
        "inputs": [{
            "since": "0x0",
            "previous_output": {"tx_hash": c["out_point"]["tx_hash"], "index": c["out_point"]["index"]},
        } for c in selected],
        "outputs": outputs_json,
        "outputs_data": ["0x"] * len(outputs_json),
        "witnesses": witnesses_hex,
    }
    tx_hash_hex = "0x" + tx_hash.hex()
    if not return_change:
        return tx_json, tx_hash_hex
    change_cell = None
    if change_index is not None:
        change_cell = {"out_point": {"tx_hash": tx_hash_hex, "index": hex(change_index)}, "capacity": change}
    return tx_json, tx_hash_hex, change_cell, tx_size_bytes

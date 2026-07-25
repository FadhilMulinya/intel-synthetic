"""
Minimal CKB (Nervos) transaction toolkit: key generation, address encoding,
Molecule serialization, secp256k1_blake160_sighash_all signing, and JSON-RPC
calls. Built by hand because no actively-maintained Python CKB SDK exists
(ckb-sdk-python and ckb-python-toolkit are both stale, no releases in 2+
years). The CKB `send_transaction` RPC accepts a plain JSON transaction
object, so Molecule serialization is only needed internally to compute the
tx hash and the signing message -- not for wire submission.
"""
import hashlib
import secrets

import requests
import secp256k1_pure

RPC_URL = "http://127.0.0.1:8114"

# secp256k1_blake160_sighash_all system script, discovered dynamically via
# `offckb system-scripts` and cached here at process start (see discover_system_script).
SIGHASH_CODE_HASH = None
SIGHASH_DEP = None  # {"out_point": {"tx_hash":..., "index":...}, "dep_type": "dep_group"}

MIN_CELL_CAPACITY_SHANNON = 61 * 10**8  # 61 CKB min for a plain secp256k1_blake160 cell
SHANNONS_PER_CKB = 10**8


# ---------------------------------------------------------------------------
# hashing / bech32m
# ---------------------------------------------------------------------------

def ckbhash(data: bytes) -> bytes:
    """CKB's personalized blake2b-256, used for tx hashes and signing messages."""
    h = hashlib.blake2b(data, digest_size=32, person=b"ckb-default-hash")
    return h.digest()


_BECH32M_CONST = 0x2BC830A3
_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((b >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp):
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convertbits(data, frombits, tobits, pad=True):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    for b in data:
        acc = (acc << frombits) | b
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    return ret


def bech32m_encode(hrp: str, data: bytes) -> str:
    values = _convertbits(list(data), 8, 5, True)
    polymod = _bech32_polymod(_bech32_hrp_expand(hrp) + values + [0, 0, 0, 0, 0, 0]) ^ _BECH32M_CONST
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[d] for d in values + checksum)


def lock_arg_to_address(lock_arg_hex: str, hrp: str = "ckt") -> str:
    """CKB2021 full address (secp256k1_blake160_sighash_all, hash_type=type)."""
    code_hash_bytes = bytes.fromhex(SIGHASH_CODE_HASH[2:])
    payload = b"\x00" + code_hash_bytes + b"\x01" + bytes.fromhex(lock_arg_hex[2:])
    return bech32m_encode(hrp, payload)


# ---------------------------------------------------------------------------
# Molecule serialization (only the subset CKB transactions need)
# ---------------------------------------------------------------------------

def mol_u32(n: int) -> bytes:
    return n.to_bytes(4, "little")


def mol_u64(n: int) -> bytes:
    return n.to_bytes(8, "little")


def mol_bytes(data: bytes) -> bytes:
    """molecule `Bytes` = fixvec<byte> = u32 length prefix + raw bytes."""
    return mol_u32(len(data)) + data


def mol_dynamic(parts):
    """Shared encoding for molecule `table` and `dynvec`: 4-byte total size,
    then one 4-byte offset per part, then the concatenated part bytes."""
    n = len(parts)
    header_len = 4 + 4 * n
    body = b"".join(parts)
    total_size = header_len + len(body)
    offsets = []
    running = header_len
    for p in parts:
        offsets.append(running)
        running += len(p)
    return mol_u32(total_size) + b"".join(mol_u32(o) for o in offsets) + body


def mol_fixvec(items_bytes):
    """molecule fixvec of fixed-size items: u32 count + concatenated raw items."""
    return mol_u32(len(items_bytes)) + b"".join(items_bytes)


def mol_script(code_hash_hex: str, hash_type: str, args_hex: str) -> bytes:
    code_hash = bytes.fromhex(code_hash_hex[2:])
    ht = {"data": 0x00, "type": 0x01, "data1": 0x02, "data2": 0x04}[hash_type]
    args = mol_bytes(bytes.fromhex(args_hex[2:]))
    return mol_dynamic([code_hash, bytes([ht]), args])


def mol_out_point(tx_hash_hex: str, index: int) -> bytes:
    return bytes.fromhex(tx_hash_hex[2:]) + mol_u32(index)


def mol_cell_input(since: int, tx_hash_hex: str, index: int) -> bytes:
    return mol_u64(since) + mol_out_point(tx_hash_hex, index)


def mol_cell_dep(tx_hash_hex: str, index: int, dep_type: str) -> bytes:
    dt = {"code": 0x00, "dep_group": 0x01}[dep_type]
    return mol_out_point(tx_hash_hex, index) + bytes([dt])


def mol_cell_output(capacity: int, lock_script_bytes: bytes, type_script_bytes=None) -> bytes:
    cap = mol_u64(capacity)
    type_opt = type_script_bytes if type_script_bytes is not None else b""
    return mol_dynamic([cap, lock_script_bytes, type_opt])


def mol_transaction(raw_tx_bytes: bytes, witnesses: list) -> bytes:
    """Full molecule `Transaction` (raw + witnesses), used only to compute
    the exact on-wire byte size for display purposes (explorer's `bytes`
    field) -- not needed for RPC submission, which takes plain JSON."""
    witnesses_ser = mol_dynamic([mol_bytes(w) for w in witnesses])
    return mol_dynamic([raw_tx_bytes, witnesses_ser])


def mol_raw_transaction(cell_deps, header_deps, inputs, outputs, outputs_data) -> bytes:
    version = mol_u32(0)
    cell_deps_ser = mol_fixvec(cell_deps)
    header_deps_ser = mol_fixvec(header_deps)
    inputs_ser = mol_fixvec(inputs)
    outputs_ser = mol_dynamic(outputs)
    outputs_data_ser = mol_dynamic([mol_bytes(d) for d in outputs_data])
    return mol_dynamic([version, cell_deps_ser, header_deps_ser, inputs_ser, outputs_ser, outputs_data_ser])


def mol_witness_args(lock: bytes = None, input_type: bytes = None, output_type: bytes = None) -> bytes:
    f1 = mol_bytes(lock) if lock is not None else b""
    f2 = mol_bytes(input_type) if input_type is not None else b""
    f3 = mol_bytes(output_type) if output_type is not None else b""
    return mol_dynamic([f1, f2, f3])


# ---------------------------------------------------------------------------
# keys / signing
# ---------------------------------------------------------------------------

def generate_keypair():
    """Returns (privkey_hex, pubkey_compressed_hex, lock_arg_hex, address)."""
    priv_int = secrets.randbelow(secp256k1_pure.N - 1) + 1
    priv_bytes = priv_int.to_bytes(32, "big")
    pub_compressed = secp256k1_pure.privkey_to_pubkey_compressed(priv_int)  # 33 bytes
    lock_arg = ckbhash(pub_compressed)[:20]
    privkey_hex = "0x" + priv_bytes.hex()
    pubkey_hex = "0x" + pub_compressed.hex()
    lock_arg_hex = "0x" + lock_arg.hex()
    address = lock_arg_to_address(lock_arg_hex)
    return privkey_hex, pubkey_hex, lock_arg_hex, address


def sign_message(privkey_hex: str, message32: bytes) -> bytes:
    priv_int = int(privkey_hex, 16)
    return secp256k1_pure.sign_recoverable(priv_int, message32)  # 65 bytes: r(32) s(32) recid(1)


# ---------------------------------------------------------------------------
# RPC
# ---------------------------------------------------------------------------

_rpc_id = 0


def rpc(method: str, params=None, timeout=15):
    global _rpc_id
    _rpc_id += 1
    payload = {"id": _rpc_id, "jsonrpc": "2.0", "method": method, "params": params or []}
    resp = requests.post(RPC_URL, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC {method} error: {data['error']}")
    return data["result"]


def get_tip_block_number() -> int:
    return int(rpc("get_tip_block_number"), 16)


def get_live_cells_for_lock_arg(lock_arg_hex: str, limit_hex="0x3e8"):
    script = {"code_hash": SIGHASH_CODE_HASH, "hash_type": "type", "args": lock_arg_hex}
    result = rpc("get_cells", [{"script": script, "script_type": "lock"}, "asc", limit_hex])
    return result["objects"]


def get_balance_shannon(lock_arg_hex: str) -> int:
    cells = get_live_cells_for_lock_arg(lock_arg_hex)
    return sum(int(c["output"]["capacity"], 16) for c in cells)


def send_transaction(tx_json: dict) -> str:
    return rpc("send_transaction", [tx_json, "passthrough"])


def get_transaction(tx_hash: str):
    return rpc("get_transaction", [tx_hash])


def get_block_by_number(block_number: int):
    return rpc("get_block_by_number", [hex(block_number)])


# ---------------------------------------------------------------------------
# high level: build + sign + submit a transfer (single sender, N outputs)
# ---------------------------------------------------------------------------

def discover_system_script():
    """Parse `offckb system-scripts` to find the secp256k1_blake160_sighash_all
    code hash and cell dep (these depend on the running devnet's genesis and
    must not be hardcoded)."""
    import subprocess
    global SIGHASH_CODE_HASH, SIGHASH_DEP
    out = subprocess.run(["offckb", "system-scripts"], capture_output=True, text=True, check=True).stdout
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "- name: secp256k1_blake160_sighash_all":
            block = "\n".join(lines[i:i + 20])
            code_hash = None
            tx_hash = None
            index = None
            dep_type = None
            for l in block.splitlines():
                l = l.strip()
                if code_hash is None and l.startswith("code_hash:"):
                    code_hash = l.split(":", 1)[1].strip()
                if tx_hash is None and l.startswith('"txHash":'):
                    tx_hash = l.split(":", 1)[1].strip().strip('",')
                if index is None and l.startswith('"index":'):
                    index = int(l.split(":", 1)[1].strip().strip(","))
                if dep_type is None and l.startswith('"depType":'):
                    dep_type = l.split(":", 1)[1].strip().strip('",')
            SIGHASH_CODE_HASH = code_hash
            SIGHASH_DEP = {"tx_hash": tx_hash, "index": index, "dep_type": "dep_group" if dep_type == "depGroup" else "code"}
            return
    raise RuntimeError("could not find secp256k1_blake160_sighash_all in offckb system-scripts output")


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
    cell_deps = [mol_cell_dep(SIGHASH_DEP["tx_hash"], SIGHASH_DEP["index"], SIGHASH_DEP["dep_type"])]
    inputs_mol = [mol_cell_input(0, c["out_point"]["tx_hash"], int(c["out_point"]["index"], 16)) for c in selected]

    lock_script_bytes = mol_script(SIGHASH_CODE_HASH, "type", from_lock_arg_hex)
    outputs_mol = []
    outputs_data = []
    outputs_json = []
    for to_lock_arg, capacity in outputs:
        to_lock_bytes = mol_script(SIGHASH_CODE_HASH, "type", to_lock_arg)
        outputs_mol.append(mol_cell_output(capacity, to_lock_bytes))
        outputs_data.append(b"")
        outputs_json.append({
            "capacity": hex(capacity),
            "lock": {"code_hash": SIGHASH_CODE_HASH, "hash_type": "type", "args": to_lock_arg},
            "type": None,
        })
    change_index = None
    if change > 0:
        if change < MIN_CELL_CAPACITY_SHANNON:
            raise RuntimeError(f"change {change} below minimum cell capacity; add more outputs or a bigger fee")
        change_index = len(outputs_json)
        outputs_mol.append(mol_cell_output(change, lock_script_bytes))
        outputs_data.append(b"")
        outputs_json.append({
            "capacity": hex(change),
            "lock": {"code_hash": SIGHASH_CODE_HASH, "hash_type": "type", "args": from_lock_arg_hex},
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
            "out_point": {"tx_hash": SIGHASH_DEP["tx_hash"], "index": hex(SIGHASH_DEP["index"])},
            "dep_type": SIGHASH_DEP["dep_type"],
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

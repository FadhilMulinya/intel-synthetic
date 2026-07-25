"""JSON-RPC calls to the CKB node, plus one-time devnet system-script
discovery (the secp256k1_blake160_sighash_all code hash and cell dep depend
on the running devnet's genesis and must not be hardcoded)."""
import subprocess

import requests

from . import config

_rpc_id = 0


def rpc(method: str, params=None, timeout=15):
    global _rpc_id
    _rpc_id += 1
    payload = {"id": _rpc_id, "jsonrpc": "2.0", "method": method, "params": params or []}
    resp = requests.post(config.RPC_URL, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC {method} error: {data['error']}")
    return data["result"]


def get_tip_block_number() -> int:
    return int(rpc("get_tip_block_number"), 16)


def get_live_cells_for_lock_arg(lock_arg_hex: str, limit_hex="0x3e8"):
    script = {"code_hash": config.SIGHASH_CODE_HASH, "hash_type": "type", "args": lock_arg_hex}
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


def discover_system_script():
    """Parse `offckb system-scripts` to find the secp256k1_blake160_sighash_all
    code hash and cell dep."""
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
            config.SIGHASH_CODE_HASH = code_hash
            config.SIGHASH_DEP = {"tx_hash": tx_hash, "index": index, "dep_type": "dep_group" if dep_type == "depGroup" else "code"}
            return
    raise RuntimeError("could not find secp256k1_blake160_sighash_all in offckb system-scripts output")

"""Shared constants and devnet-discovered runtime state for the ckb package.

SIGHASH_CODE_HASH / SIGHASH_DEP start as None and are filled in once by
rpc.discover_system_script() -- they depend on the running devnet's genesis
and must not be hardcoded. Other modules read them as `config.NAME` (an
attribute lookup) rather than importing the names directly, so they see the
value discover_system_script() fills in later, not the None each module saw
at import time.
"""

RPC_URL = "http://127.0.0.1:8114"

MIN_CELL_CAPACITY_SHANNON = 61 * 10**8  # 61 CKB min for a plain secp256k1_blake160 cell
SHANNONS_PER_CKB = 10**8

SIGHASH_CODE_HASH = None
SIGHASH_DEP = None  # {"tx_hash": ..., "index": ..., "dep_type": "dep_group"|"code"}

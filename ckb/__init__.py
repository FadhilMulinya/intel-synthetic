"""
Minimal CKB (Nervos) transaction toolkit: key generation, address encoding,
Molecule serialization, secp256k1_blake160_sighash_all signing, and JSON-RPC
calls. Built by hand because no actively-maintained Python CKB SDK exists
(ckb-sdk-python and ckb-python-toolkit are both stale, no releases in 2+
years). The CKB `send_transaction` RPC accepts a plain JSON transaction
object, so Molecule serialization is only needed internally to compute the
tx hash and the signing message -- not for wire submission.

Split into config (shared/devnet-discovered state), hashing, molecule,
keys, rpc, and transfer -- each kept under 250 lines. Re-exported here so
callers can keep using `import ckb; ckb.foo(...)`. Devnet-discovered state
(SIGHASH_CODE_HASH, SIGHASH_DEP) is read live via `ckb.config.NAME` rather
than re-exported by value, since discover_system_script() fills it in after
this module is first imported.

None of the re-exported names below may collide with a submodule's own
name (e.g. `rpc`) -- `from .rpc import rpc` would silently rebind the
`ckb.rpc` attribute from the submodule to that one function, breaking
`import ckb.rpc` for everyone else. That's why the low-level JSON-RPC call
below is named `call_rpc`, not `rpc`.
"""
from . import config
from .config import RPC_URL, MIN_CELL_CAPACITY_SHANNON, SHANNONS_PER_CKB
from .hashing import ckbhash, bech32m_encode, lock_arg_to_address
from .molecule import (
    mol_u32, mol_u64, mol_bytes, mol_dynamic, mol_fixvec, mol_script,
    mol_out_point, mol_cell_input, mol_cell_dep, mol_cell_output,
    mol_transaction, mol_raw_transaction, mol_witness_args,
)
from .keys import generate_keypair, sign_message
from .rpc import (
    call_rpc, get_tip_block_number, get_live_cells_for_lock_arg,
    get_balance_shannon, send_transaction, get_transaction,
    get_block_by_number, discover_system_script,
)
from .transfer import build_and_sign_transfer, build_and_sign_transfer_from_cell

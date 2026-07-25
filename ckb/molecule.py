"""Molecule serialization -- only the subset CKB transactions need. The CKB
`send_transaction` RPC accepts a plain JSON transaction object, so this is
only needed internally to compute the tx hash and the signing message, not
for wire submission."""


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

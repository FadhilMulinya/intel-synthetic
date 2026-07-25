"""Keypair generation and secp256k1_blake160_sighash_all message signing."""
import secrets

import secp256k1_pure

from .hashing import ckbhash, lock_arg_to_address


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

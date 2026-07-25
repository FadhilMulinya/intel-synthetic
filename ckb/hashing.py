"""CKB's personalized blake2b tx/signing-message hashing, and CKB2021
bech32m address encoding."""
import hashlib

from . import config


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
    code_hash_bytes = bytes.fromhex(config.SIGHASH_CODE_HASH[2:])
    payload = b"\x00" + code_hash_bytes + b"\x01" + bytes.fromhex(lock_arg_hex[2:])
    return bech32m_encode(hrp, payload)

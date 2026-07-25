"""
Minimal pure-Python secp256k1 (key derivation + RFC6979 deterministic
recoverable ECDSA signing). No native/compiled dependency.

Used instead of `coincurve` because coincurve ships no prebuilt wheel for
Python 3.14 yet, and building it from source hits an unrelated packaging bug
in this environment. Pure Python keeps this project trivially installable;
CPython's arbitrary-precision int + built-in pow() (modexp) already run the
actual big-int math in C, so per-signature cost is a few milliseconds.
"""
import hashlib
import hmac

P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
G = (GX, GY)


def _inv(x, m):
    return pow(x, m - 2, m)


def _point_add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % P == 0:
            return None
        m = (3 * x1 * x1) * _inv(2 * y1, P) % P
    else:
        m = (y2 - y1) * _inv((x2 - x1) % P, P) % P
    x3 = (m * m - x1 - x2) % P
    y3 = (m * (x1 - x3) - y1) % P
    return (x3, y3)


def scalar_mult(k, point):
    result = None
    addend = point
    while k:
        if k & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        k >>= 1
    return result


def privkey_to_pubkey_compressed(privkey_int: int) -> bytes:
    x, y = scalar_mult(privkey_int, G)
    prefix = 2 if y % 2 == 0 else 3
    return bytes([prefix]) + x.to_bytes(32, "big")


def _rfc6979_k(privkey_int: int, digest32: bytes):
    """Deterministic nonce generation per RFC 6979 (hash already 32 bytes)."""
    x = privkey_int.to_bytes(32, "big")
    h1 = digest32
    v = b"\x01" * 32
    k = b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        cand = int.from_bytes(v, "big")
        if 1 <= cand < N:
            return cand
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def sign_recoverable(privkey_int: int, digest32: bytes) -> bytes:
    """Returns 65-byte compact recoverable signature: r(32) || s(32) || recid(1),
    low-S normalized -- matches libsecp256k1's ecdsa_sign_recoverable output,
    which is what CKB's on-chain secp256k1_blake160_sighash_all verifier expects."""
    z = int.from_bytes(digest32, "big")
    while True:
        k = _rfc6979_k(privkey_int, digest32)
        R = scalar_mult(k, G)
        r = R[0] % N
        if r == 0:
            digest32 = hashlib.sha256(digest32).digest()
            continue
        k_inv = _inv(k, N)
        s = (k_inv * (z + r * privkey_int)) % N
        if s == 0:
            digest32 = hashlib.sha256(digest32).digest()
            continue
        y_parity = R[1] % 2
        if s > N // 2:
            s = N - s
            y_parity ^= 1
        recid = y_parity
        return r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([recid])

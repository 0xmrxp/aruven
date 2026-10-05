"""Minimal secp256k1 (pure Python, for the signer).

Only what signing needs: privkey->pubkey and deterministic ECDSA (RFC6979).
Not constant-time; acceptable here because the key holder is the agent
executor wallet whose worst-case loss is bounded by the vault contract.
"""
from __future__ import annotations

import hashlib
import hmac

# curve params
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
Gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
Gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8


def _inv(a: int, m: int) -> int:
    return pow(a, m - 2, m)  # prime modulus


def _point_add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % P == 0:
        return None
    if p1 == p2:
        lam = (3 * x1 * x1) * _inv(2 * y1, P) % P
    else:
        lam = (y2 - y1) * _inv((x2 - x1) % P, P) % P
    x3 = (lam * lam - x1 - x2) % P
    y3 = (lam * (x1 - x3) - y1) % P
    return (x3, y3)


def _point_mul(k: int, point):
    result = None
    addend = point
    while k:
        if k & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        k >>= 1
    return result


G = (Gx, Gy)


def privkey_to_pubkey(priv: bytes) -> bytes:
    """Returns the 64-byte (x||y) public key (without 0x04 prefix)."""
    d = int.from_bytes(priv, "big")
    if d == 0 or d >= N:
        raise ValueError("bad private key")
    x, y = _point_mul(d, G)
    return x.to_bytes(32, "big") + y.to_bytes(32, "big")


def _rfc6979_nonce(priv: bytes, msg_hash: bytes) -> int:
    """Deterministic nonce per RFC 6979 (SHA-256)."""
    v = b"\x01" * 32
    k = b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + priv + msg_hash, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + priv + msg_hash, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        candidate = int.from_bytes(v, "big")
        if 1 <= candidate < N:
            return candidate
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def sign(priv: bytes, msg_hash: bytes) -> tuple[int, int, int]:
    """ECDSA sign; returns (r, s, recovery_id). Low-s normalized (EIP-2)."""
    z = int.from_bytes(msg_hash, "big")
    d = int.from_bytes(priv, "big")
    while True:
        k = _rfc6979_nonce(priv, msg_hash)
        R = _point_mul(k, G)
        r = R[0] % N
        if r == 0:
            continue
        s = (_inv(k, N) * (z + r * d)) % N
        if s == 0:
            continue
        rec = 0 if R[1] % 2 == 0 else 1
        if s > N // 2:
            s = N - s
            rec ^= 1
        return r, s, rec

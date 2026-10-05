"""EIP-1559 transaction signer — the ONLY place the executor key is used.

Key lives in an env var, never in config files or the repo. Signing is offline
(rlp + ecdsa via stdlib + pycryptodome); broadcasting goes through the RPC.
"""
from __future__ import annotations

import os

from Crypto.Hash import keccak


class SignerError(Exception):
    pass


def keccak256(data: bytes) -> bytes:
    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


# --- minimal RLP ---
def rlp_encode(item) -> bytes:
    if isinstance(item, int):
        if item == 0:
            payload = b""
        else:
            payload = item.to_bytes((item.bit_length() + 7) // 8, "big")
        return rlp_encode(payload)
    if isinstance(item, (bytes, bytearray)):
        data = bytes(item)
        if len(data) == 1 and data[0] < 0x80:
            return data
        return _rlp_len(len(data), 0x80) + data
    if isinstance(item, (list, tuple)):
        encoded = b"".join(rlp_encode(x) for x in item)
        return _rlp_len(len(encoded), 0xC0) + encoded
    raise SignerError(f"cannot rlp-encode {type(item)}")


def _rlp_len(length: int, offset: int) -> bytes:
    if length < 56:
        return bytes([offset + length])
    len_bytes = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([offset + 55 + len(len_bytes)]) + len_bytes


class Signer:
    """Signs EIP-1559 (type-2) transactions."""

    def __init__(self, private_key_hex: str, chain_id: int):
        key = private_key_hex.removeprefix("0x").strip()
        if len(key) != 64:
            raise SignerError("private key must be 32 bytes hex")
        self._key = bytes.fromhex(key)
        self.chain_id = chain_id
        self.address = self._derive_address(self._key)

    @staticmethod
    def _derive_address(priv: bytes) -> str:
        # secp256k1 public key via pycryptodome's ECC (standard curve params)
        # pycryptodome has no secp256k1; use a pure-python fallback
        from . import secp256k1
        pub = secp256k1.privkey_to_pubkey(priv)
        uncompressed = b"\x04" + pub
        h = keccak256(uncompressed[1:])
        return "0x" + h[-20:].hex()

    def sign_hash(self, msg_hash: bytes) -> tuple[int, int, int]:
        from . import secp256k1
        return secp256k1.sign(self._key, msg_hash)

    def sign_tx(self, tx: dict) -> str:
        """tx fields: nonce, to, value, data, max_fee, priority_fee, gas_limit."""
        from . import secp256k1
        payload = [
            tx["nonce"],
            tx.get("chain_id", self.chain_id),
            tx["priority_fee"],
            tx["max_fee"],
            tx["gas_limit"],
            bytes.fromhex(tx["to"].removeprefix("0x")),
            tx.get("value", 0),
            bytes.fromhex(tx["data"].removeprefix("0x")),
            [],
        ]
        signing_hash = keccak256(rlp_encode(payload))
        r, s, rec = secp256k1.sign(self._key, signing_hash)
        signed = [
            tx["nonce"],
            self.chain_id,
            tx["priority_fee"],
            tx["max_fee"],
            tx["gas_limit"],
            bytes.fromhex(tx["to"].removeprefix("0x")),
            tx.get("value", 0),
            bytes.fromhex(tx["data"].removeprefix("0x")),
            [],
            [],
            b"",  # signature y-parity placeholder; replaced below
        ]
        # build type-2 payload with parity
        raw = bytes([0x02]) + rlp_encode([
            tx["nonce"], self.chain_id, tx["priority_fee"], tx["max_fee"], tx["gas_limit"],
            bytes.fromhex(tx["to"].removeprefix("0x")), tx.get("value", 0),
            bytes.fromhex(tx["data"].removeprefix("0x")),
            r, s, rec,
        ])
        return "0x" + raw.hex()

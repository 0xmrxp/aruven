"""EIP-1559 (type-2) transaction encoding and signing.

Spec: https://eips.ethereum.org/EIPS/eip-1559

    signed   = 0x02 || rlp([chain_id, nonce, max_priority_fee_per_gas,
                            max_fee_per_gas, gas_limit, destination, amount,
                            data, access_list, y_parity, r, s])
    to-sign  = keccak256(0x02 || rlp([chain_id, nonce, max_priority_fee_per_gas,
                                      max_fee_per_gas, gas_limit, destination,
                                      amount, data, access_list]))

Field order is load-bearing: `chain_id` comes FIRST, the access list is a
required element (an empty list is encoded as 0xc0), and the signature is
`y_parity, r, s` at the tail — not `r, s, y_parity`. A node rejects any
deviation, so `sign_tx` self-checks by recovering the signer address from the
signature it just produced.
"""
from __future__ import annotations

from typing import Any, Optional

from . import secp256k1


class SignerError(Exception):
    pass


def keccak256(data: bytes) -> bytes:
    """Keccak-256 (the Ethereum flavour, not NIST SHA3-256)."""
    try:
        from Crypto.Hash import keccak  # pycryptodome

        h = keccak.new(digest_bits=256)
        h.update(data)
        return h.digest()
    except ImportError:  # pragma: no cover - fallback for minimal environments
        import sha3  # type: ignore

        return sha3.keccak_256(data).digest()


# --- minimal RLP ---
def rlp_encode(item: Any) -> bytes:
    if isinstance(item, bool):  # bool is an int subclass; reject explicitly
        raise SignerError("cannot rlp-encode bool")
    if isinstance(item, int):
        if item < 0:
            raise SignerError("cannot rlp-encode negative int")
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


def _address_bytes(addr: Optional[str]) -> bytes:
    """`to` as 20 raw bytes; b'' for contract creation."""
    if not addr:
        return b""
    raw = addr.removeprefix("0x").strip()
    if raw == "":
        return b""
    if len(raw) != 40:
        raise SignerError(f"address must be 20 bytes: {addr}")
    return bytes.fromhex(raw)


def _data_bytes(data: Optional[str]) -> bytes:
    if not data:
        return b""
    raw = data.removeprefix("0x").strip()
    if raw == "":
        return b""
    if len(raw) % 2:
        raise SignerError("data must be whole bytes")
    return bytes.fromhex(raw)


def _access_list_bytes(access_list: Optional[list]) -> list:
    """EIP-2930 access list: [[address, [storage_key, ...]], ...]."""
    if not access_list:
        return []
    out = []
    for entry in access_list:
        addr, keys = entry
        out.append([_address_bytes(addr), [bytes.fromhex(k.removeprefix("0x")) for k in keys]])
    return out


def encode_signing_payload(tx: dict) -> list:
    """The 9-field list that gets RLP-encoded into the signing hash."""
    return [
        int(tx["chain_id"]),
        int(tx["nonce"]),
        int(tx["priority_fee"]),
        int(tx["max_fee"]),
        int(tx["gas_limit"]),
        _address_bytes(tx.get("to")),
        int(tx.get("value", 0)),
        _data_bytes(tx.get("data")),
        _access_list_bytes(tx.get("access_list")),
    ]


def signing_hash(tx: dict) -> bytes:
    """keccak256(0x02 || rlp(signing payload))."""
    return keccak256(b"\x02" + rlp_encode(encode_signing_payload(tx)))


class Signer:
    """Signs EIP-1559 (type-2) transactions."""

    def __init__(self, private_key_hex: str, chain_id: int):
        key = private_key_hex.removeprefix("0x").strip()
        if len(key) != 64:
            raise SignerError("private key must be 32 bytes hex")
        self._key = bytes.fromhex(key)
        self.chain_id = int(chain_id)
        self.address = self._derive_address(self._key)

    @staticmethod
    def _derive_address(priv: bytes) -> str:
        pub = secp256k1.privkey_to_pubkey(priv)
        return "0x" + keccak256(pub)[-20:].hex()

    def sign_hash(self, msg_hash: bytes) -> tuple[int, int, int]:
        return secp256k1.sign(self._key, msg_hash)

    def sign_tx(self, tx: dict, *, self_check: bool = True) -> str:
        """Sign a type-2 transaction and return the raw `0x02…` hex.

        `tx` fields: nonce, to, value, data, max_fee, priority_fee, gas_limit,
        and optionally chain_id (defaults to the signer's) and access_list.

        With `self_check` (default) the produced signature is verified by
        recovering the signer address from the spec signing hash — a cheap guard
        against the field-order class of bug that silently produces txs every
        node rejects.
        """
        fields = dict(tx)
        fields.setdefault("chain_id", self.chain_id)
        fields.setdefault("value", 0)
        fields.setdefault("data", "0x")
        fields.setdefault("access_list", [])

        msg_hash = signing_hash(fields)
        r, s, rec = secp256k1.sign(self._key, msg_hash)

        if self_check:
            recovered = secp256k1.recover_address(msg_hash, r, s, rec)
            if recovered.lower() != self.address.lower():
                raise SignerError(
                    f"self-check failed: signature recovers to {recovered}, expected {self.address}"
                )

        signed = encode_signing_payload(fields) + [rec, r, s]
        return "0x" + (b"\x02" + rlp_encode(signed)).hex()

"""Minimal secp256k1 — re-exported from ``aruven_common`` so the agent and the
backend share exactly one implementation.
"""
from aruven_common.secp256k1 import (  # noqa: F401
    G,
    Gx,
    Gy,
    N,
    P,
    _point_add,
    _point_mul,
    privkey_to_pubkey,
    recover_address,
    sign,
)

"""EIP-1559 transaction signer — re-exported from ``aruven_common.eip1559``.

Kept as a module here because the backend is where the executor key is used at
runtime (see ARCHITECTURE.md §4); the implementation lives in
``aruven_common`` so the agent signs with byte-identical code.
"""
from aruven_common.eip1559 import (  # noqa: F401
    Signer,
    SignerError,
    encode_signing_payload,
    keccak256,
    rlp_encode,
    signing_hash,
)

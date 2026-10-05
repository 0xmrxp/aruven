"""Stage 5 — executor: the ONLY module that touches the executor private key.

It can only call AruvenVault.executeSwap — the vault's contract limits bound
what even a fully compromised executor can do (5% per trade, 2% daily loss,
1h cooldown, token whitelist, slippage floor).

Dry-run mode simulates fills without broadcasting.
"""
from __future__ import annotations

from typing import Optional

from .config import AgentConfig
from .screen import ChainClient
from .types import Execution, Proposal

# AruvenVault.executeSwap(address,address,uint256,uint256,address,bytes,bytes32)
EXECUTE_SWAP_SIG = "executeSwap(address,uint256,uint256,address,bytes,bytes32)"


def _selector(sig: str) -> str:
    from Crypto.Hash import keccak
    h = keccak.new(digest_bits=256)
    h.update(sig.encode())
    return h.hexdigest()[:8]


class Executor:
    def __init__(self, cfg: AgentConfig, chain: ChainClient, dry_run: bool = True):
        self.cfg = cfg
        self.chain = chain
        self.dry_run = dry_run
        self._nonce: Optional[int] = None

    def _addr(self, name: str) -> str:
        for t in self.cfg.strategy_tokens:
            if t.name == name:
                return t.address
        raise ValueError(f"unknown token: {name}")

    def _pad_addr(self, a: str) -> str:
        return "00" * 12 + a[2:].lower()

    def _build_calldata(self, prop: Proposal) -> str:
        router = self._router()
        # routerData: call MockRouter-like swap(tokenIn,tokenOut,amountIn,amountOut,vault)
        router_sig = "swap(address,address,uint256,uint256,address)"
        router_data = "0x" + _selector(router_sig) \
            + self._pad_addr(self._addr(prop.token_in)) \
            + self._pad_addr(self._addr(prop.token_out)) \
            + f"{prop.amount_in:064x}" \
            + f"{prop.expected_out:064x}" \
            + self._pad_addr(self.cfg.vault_address)
        reason_hash = "00" * 32  # placeholder: journal entry hash injected by caller
        return "0x" + _selector(EXECUTE_SWAP_SIG) \
            + self._pad_addr(self._addr(prop.token_in)) \
            + self._pad_addr(self._addr(prop.token_out)) \
            + f"{prop.amount_in:064x}" \
            + f"{self._min_out(prop):064x}" \
            + self._pad_addr(router) \
            + self._dynamic_bytes(router_data) \
            + reason_hash

    def _dynamic_bytes(self, data_hex: str) -> str:
        payload = data_hex[2:]
        n = (len(payload) + 1) // 2
        padded = payload.ljust(n * 2, "0")
        return f"{n:064x}" + padded

    def _min_out(self, prop: Proposal) -> int:
        slip = 10_000 - prop.slippage_bps
        return prop.expected_out * slip // 10_000

    def _router(self) -> str:
        # Router address comes from config env or strategy section (Phase 3 wiring)
        import os
        r = os.environ.get("ARUVEN_ROUTER")
        if not r:
            raise RuntimeError("ARUVEN_ROUTER not set (Phase 3 wiring pending)")
        return r

    def execute(self, prop: Proposal, reason_hash: str) -> Execution:
        if self.dry_run:
            return Execution(
                tx_hash="0x" + "0" * 63 + "d",
                amount_out=prop.expected_out,
                gas_used=0,
                success=True,
            )
        calldata = self._build_calldata(prop).replace("00" * 32, reason_hash[2:].rjust(64, "0"), 1)
        # NOTE: real signing (RLP + EIP-1559) is wired in Phase 3 with the
        # backend's tx signer; the sandboxed agent never ships raw keys.
        raise NotImplementedError("live signing lands with the backend signer (Phase 3)")

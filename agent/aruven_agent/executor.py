"""Stage 5 — executor: the ONLY module that touches the executor private key.

It can only call AruvenVault.executeSwap, and the vault bounds what even a
fully compromised executor can do: token whitelist, router whitelist, 5% per
trade, daily-loss cap, 1h cooldown, and a non-zero slippage floor.

Dry-run mode simulates fills without broadcasting.
"""
from __future__ import annotations

from typing import Optional

from aruven_common.eip1559 import Signer, keccak256

from .config import AgentConfig
from .screen import ChainClient
from .types import Execution, Proposal

# Must match AruvenVault.executeSwap exactly. A mismatch produces a selector the
# vault has no function for, so the call falls through to the fallback and
# reverts — the bug this constant previously had.
EXECUTE_SWAP_SIG = "executeSwap(address,address,uint256,uint256,address,bytes,bytes32)"

# selectors of the vault's error types, so a revert can be named in the journal
ERROR_SELECTORS = {
    "0x" + keccak256(b"NotAgent()").hex()[:8]: "NotAgent",
    "0x" + keccak256(b"TokenNotAllowed()").hex()[:8]: "TokenNotAllowed",
    "0x" + keccak256(b"TradeTooLarge()").hex()[:8]: "TradeTooLarge",
    "0x" + keccak256(b"DailyLossCapHit()").hex()[:8]: "DailyLossCapHit",
    "0x" + keccak256(b"CooldownActive()").hex()[:8]: "CooldownActive",
    "0x" + keccak256(b"TooLittleOut()").hex()[:8]: "TooLittleOut",
    "0x" + keccak256(b"BadRouterCall()").hex()[:8]: "BadRouterCall",
    "0x" + keccak256(b"RouterNotAllowed()").hex()[:8]: "RouterNotAllowed",
    "0x" + keccak256(b"ZeroMinOut()").hex()[:8]: "ZeroMinOut",
    "0x" + keccak256(b"WrongToken()").hex()[:8]: "WrongToken",
    "0x" + keccak256(b"BadAmount()").hex()[:8]: "BadAmount",
}


def _selector(sig: str) -> str:
    return keccak256(sig.encode()).hex()[:8]


def _word(value: int) -> str:
    return f"{value:064x}"


def _addr_word(addr: str) -> str:
    return "00" * 12 + addr.removeprefix("0x").lower()


def encode_bytes_tail(data: bytes) -> str:
    """ABI-encode a dynamic `bytes` value as the tail of an argument list:
    a 32-byte length word followed by the payload, right-padded to 32 bytes."""
    padded = data.ljust((len(data) + 31) // 32 * 32, b"\x00")
    return _word(len(data)) + padded.hex()


def encode_execute_swap(
    token_in: str,
    token_out: str,
    amount_in: int,
    amount_out_min: int,
    router: str,
    router_data: bytes,
    reason_hash: bytes,
) -> str:
    """Full ABI encoding of executeSwap.

    `bytes routerData` is dynamic, so its slot in the 7-word head holds an
    OFFSET to the tail — not the length-plus-payload. Encoding it inline (as
    this module used to) makes a spec-compliant decoder read garbage.
    """
    if len(reason_hash) != 32:
        raise ValueError("reason_hash must be 32 bytes")
    head_words = 7
    tail_offset = head_words * 32
    head = (
        _addr_word(token_in)
        + _addr_word(token_out)
        + _word(amount_in)
        + _word(amount_out_min)
        + _addr_word(router)
        + _word(tail_offset)
        + reason_hash.hex()
    )
    return "0x" + _selector(EXECUTE_SWAP_SIG) + head + encode_bytes_tail(router_data)


class Executor:
    def __init__(self, cfg: AgentConfig, chain: ChainClient, dry_run: bool = True):
        self.cfg = cfg
        self.chain = chain
        self.dry_run = dry_run
        self._signer: Optional[Signer] = None
        if not dry_run:
            # Raises ConfigError when the key env var is absent — fail at
            # construction, not after a trade has already been approved.
            self._signer = Signer(cfg.executor_key, cfg.chain_id)

    @property
    def address(self) -> str:
        return self._signer.address if self._signer else "(dry-run)"

    def _addr(self, name: str) -> str:
        for t in self.cfg.strategy_tokens:
            if t.name == name:
                return t.address
        raise ValueError(f"unknown token: {name}")

    def _min_out(self, prop: Proposal) -> int:
        if prop.expected_out <= 0:
            raise ValueError("expected_out must be positive")
        if not 0 <= prop.slippage_bps < 10_000:
            raise ValueError(f"slippage_bps out of range: {prop.slippage_bps}")
        out = prop.expected_out * (10_000 - prop.slippage_bps) // 10_000
        if out <= 0:
            # The vault rejects a zero floor; catch it here so the reason lands
            # in the journal instead of becoming an unexplained revert.
            raise ValueError("computed amountOutMin is zero — raise expected_out or lower slippage")
        return out

    def _router(self) -> str:
        """Router must come from the allow-list; the vault enforces the same
        list on-chain, so an unlisted router is rejected before broadcast."""
        if not self.cfg.allowed_routers:
            raise RuntimeError("no allowed_routers configured")
        return self.cfg.allowed_routers[0]

    def _router_data(self, prop: Proposal, router: str) -> bytes:
        """Calldata for the swap router. Shape is router-specific; the default
        targets the Uniswap-style `swap(tokenIn,tokenOut,amountIn,amountOut,to)`."""
        sig = self.cfg.router_swap_sig
        body = (
            _addr_word(self._addr(prop.token_in))
            + _addr_word(self._addr(prop.token_out))
            + _word(prop.amount_in)
            + _word(prop.expected_out)
            + _addr_word(self.cfg.vault_address)
        )
        return bytes.fromhex(_selector(sig) + body)

    def build_calldata(self, prop: Proposal, reason_hash: bytes) -> str:
        router = self._router()
        return encode_execute_swap(
            token_in=self._addr(prop.token_in),
            token_out=self._addr(prop.token_out),
            amount_in=prop.amount_in,
            amount_out_min=self._min_out(prop),
            router=router,
            router_data=self._router_data(prop, router),
            reason_hash=reason_hash,
        )

    def _parse_revert(self, err: object) -> str:
        """Name a vault revert when the node reports an error selector."""
        text = str(err)
        for sel, name in ERROR_SELECTORS.items():
            if sel in text:
                return name
        return text[:200]

    def execute(self, prop: Proposal, reason_hash: str) -> Execution:
        raw_hash = bytes.fromhex(reason_hash.removeprefix("0x").rjust(64, "0")[-64:])
        calldata = self.build_calldata(prop, raw_hash)

        if self.dry_run:
            return Execution(
                tx_hash="0x" + "0" * 63 + "d",
                amount_out=prop.expected_out,
                gas_used=0,
                success=True,
            )

        assert self._signer is not None  # constructor guarantees it when not dry-run
        try:
            nonce = self.chain.nonce(self._signer.address)
            fees = self.chain.gas_fees()
            gas_limit = self.chain.estimate_gas(
                {"from": self._signer.address, "to": self.cfg.vault_address, "data": calldata}
            )
            raw = self._signer.sign_tx({
                "nonce": nonce,
                "to": self.cfg.vault_address,
                "value": 0,
                "data": calldata,
                "gas_limit": gas_limit,
                "priority_fee": fees["priority_fee"],
                "max_fee": fees["max_fee"],
            })
            tx_hash = self.chain.send_raw_transaction(raw)
            receipt = self.chain.wait_for_receipt(tx_hash)
        except Exception as e:  # noqa: BLE001 — surface the reason, halt upstream
            return Execution(
                tx_hash="", amount_out=0, gas_used=0, success=False, error=self._parse_revert(e)
            )

        if receipt.get("status") not in (1, "0x1"):
            return Execution(
                tx_hash=tx_hash, amount_out=0,
                gas_used=int(receipt.get("gasUsed", "0x0"), 16) if isinstance(receipt.get("gasUsed"), str) else 0,
                success=False, error="transaction reverted on-chain",
            )
        return Execution(
            tx_hash=tx_hash,
            amount_out=prop.expected_out,
            gas_used=int(receipt.get("gasUsed", "0x0"), 16) if isinstance(receipt.get("gasUsed"), str) else 0,
            success=True,
        )

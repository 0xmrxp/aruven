"""Stage 1 — screen: build observations from RPC + market data."""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Optional

from aruven_common.eip1559 import keccak256

from .config import AgentConfig
from .types import Observation


def _sel(sig: str) -> str:
    """4-byte function selector, keccak('name(types)')[:4]."""
    return "0x" + keccak256(sig.encode()).hex()[:8]


class ChainClient:
    """Minimal JSON-RPC client (stdlib only, no web3 dependency)."""

    def __init__(self, rpc_url: str, chain_id: int, timeout: int = 30):
        self._rpc = rpc_url
        self._chain_id = chain_id
        self._timeout = timeout

    def call(self, method: str, params: list) -> object:
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode()
        req = urllib.request.Request(
            self._rpc, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            body = json.loads(resp.read())
        if "error" in body:
            raise RuntimeError(f"rpc error: {body['error']}")
        return body["result"]

    # ---- reads ----
    def chain_id(self) -> int:
        return int(self.call("eth_chainId", []), 16)

    def balance_of(self, token: str, holder: str) -> int:
        data = _sel("balanceOf(address)") + "00" * 12 + holder.removeprefix("0x").lower()
        res = self.call("eth_call", [{"to": token, "data": data}, "latest"])
        return int(res, 16)

    def _call_vault_uint(self, vault: str, sig: str) -> int:
        res = self.call("eth_call", [{"to": vault, "data": _sel(sig)}, "latest"])
        return int(res, 16)

    def vault_state(self, vault: str) -> dict:
        return {
            "epoch": self._call_vault_uint(vault, "epoch()"),
            "anchor_nav": self._call_vault_uint(vault, "epochAnchorNav()"),
            "last_trade_at": self._call_vault_uint(vault, "lastTradeAt()"),
            "max_trade_bps": self._call_vault_uint(vault, "maxTradeBps()"),
            "min_interval": self._call_vault_uint(vault, "minInterval()"),
            "daily_loss_bps": self._call_vault_uint(vault, "dailyLossBps()"),
            "epoch_loss_units": self._call_vault_uint(vault, "epochLossUnits()"),
        }

    # ---- writes / signing support ----
    def nonce(self, address: str) -> int:
        return int(self.call("eth_getTransactionCount", [address, "pending"]), 16)

    def gas_fees(self) -> dict:
        """EIP-1559 fee suggestion, falling back to eth_gasPrice."""
        try:
            block = self.call("eth_getBlockByNumber", ["latest", False])
            base = int(block["baseFeePerGas"], 16)
            tip = int(self.call("eth_maxPriorityFeePerGas", []), 16)
        except Exception:  # noqa: BLE001 — pre-1559 or restricted node
            gas_price = int(self.call("eth_gasPrice", []), 16)
            return {"priority_fee": gas_price, "max_fee": gas_price * 2}
        return {"priority_fee": tip, "max_fee": base * 2 + tip}

    def estimate_gas(self, tx: dict) -> int:
        try:
            return int(self.call("eth_estimateGas", [tx]), 16)
        except Exception:  # noqa: BLE001 — estimate can fail on state-dependent calls
            return 600_000

    def send_raw_transaction(self, raw: str) -> str:
        return str(self.call("eth_sendRawTransaction", [raw]))

    def wait_for_receipt(self, tx_hash: str, timeout: int = 180, poll: float = 2.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            receipt = self.call("eth_getTransactionReceipt", [tx_hash])
            if receipt:
                return receipt
            time.sleep(poll)
        raise TimeoutError(f"no receipt for {tx_hash} within {timeout}s")


def rsi(prices: list[float], period: int = 14) -> Optional[float]:
    """Classic Wilder RSI on a closed price series (oldest first)."""
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        delta = prices[i] - prices[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def zscore(series: list[float]) -> Optional[float]:
    if len(series) < 2:
        return None
    mean = sum(series) / len(series)
    var = sum((x - mean) ** 2 for x in series) / len(series)
    if var == 0:
        return 0.0
    return (series[-1] - mean) / (var ** 0.5)


class Screener:
    def __init__(self, cfg: AgentConfig, chain: ChainClient):
        self.cfg = cfg
        self.chain = chain

    def observe(self, token_name: str, price_series: list[float],
                volume_24h: float, price: float) -> Observation:
        vault = self.chain.vault_state(self.cfg.vault_address)
        token_addr = next(
            (t.address for t in self.cfg.strategy_tokens if t.name == token_name), None
        )
        if token_addr is None:
            raise ValueError(f"unknown token: {token_name}")
        bal = self.chain.balance_of(token_addr, self.cfg.vault_address)
        usdg = self.cfg.strategy_tokens[0]  # convention: first strategy token is the stablecoin
        usdg_bal = self.chain.balance_of(usdg.address, self.cfg.vault_address)
        return Observation(
            ts=time.time(),
            token=token_name,
            price=price,
            # None is preserved: "not enough history" is a real state, and
            # inventing 50.0 hid the fact that the agent could never trade.
            rsi=rsi(price_series, self.cfg.strategy.rsi_period),
            volume_24h=volume_24h,
            vault_balance=bal,
            usdg_balance=usdg_bal,
            epoch=vault["epoch"],
            epoch_anchor_nav=vault["anchor_nav"],
        )

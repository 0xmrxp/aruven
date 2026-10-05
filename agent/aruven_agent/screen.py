"""Stage 1 — screen: build observations from RPC + math."""
from __future__ import annotations

import time
from typing import Optional

from .config import AgentConfig
from .types import Observation


class ChainClient:
    """Minimal JSON-RPC client (stdlib only, no web3 dependency)."""

    def __init__(self, rpc_url: str, chain_id: int):
        import json
        import urllib.request
        self._rpc = rpc_url
        self._chain_id = chain_id
        self._json = json
        self._request = urllib.request.Request
        self._urlopen = urllib.request.urlopen

    def call(self, method: str, params: list) -> object:
        payload = self._json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode()
        req = self._request(
            self._rpc, data=payload, headers={"Content-Type": "application/json"}
        )
        with self._urlopen(req, timeout=30) as resp:
            body = self._json.loads(resp.read())
        if "error" in body:
            raise RuntimeError(f"rpc error: {body['error']}")
        return body["result"]

    def chain_id(self) -> int:
        return int(self.call("eth_chainId", []), 16)

    def balance_of(self, token: str, holder: str) -> int:
        data = "0x70a08231" + "00" * 12 + holder[2:].lower()
        res = self.call("eth_call", [{"to": token, "data": data}, "latest"])
        return int(res, 16)

    def vault_state(self, vault: str) -> dict:
        # getMinDelay etc. not needed; read epoch + anchor via view selectors:
        # epoch() = 0x94561e5? we use known storage-based reads instead:
        # epoch (slot 12-ish) is fragile; use public getters via eth_call on known selectors.
        # epoch() selector: keccak("epoch()")[:4]
        from Crypto.Hash import keccak
        def sel(sig: str) -> str:
            h = keccak.new(digest_bits=256)
            h.update(sig.encode())
            return "0x" + h.hexdigest()[:8]
        def call_vault(fn: str) -> int:
            res = self.call("eth_call", [{"to": vault, "data": sel(fn)}, "latest"])
            return int(res, 16)
        return {
            "epoch": call_vault("epoch()"),
            "anchor_nav": call_vault("epochAnchorNav()"),
            "last_trade_at": call_vault("lastTradeAt()"),
            "max_trade_bps": call_vault("maxTradeBps()"),
            "min_interval": call_vault("minInterval()"),
        }


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
        _r = rsi(price_series, self.cfg.strategy.rsi_period)
        return Observation(
            ts=time.time(),
            token=token_name,
            price=price,
            rsi=_r if _r is not None else 50.0,
            volume_24h=volume_24h,
            vault_balance=bal,
            usdg_balance=usdg_bal,
            epoch=vault["epoch"],
            epoch_anchor_nav=vault["anchor_nav"],
        )

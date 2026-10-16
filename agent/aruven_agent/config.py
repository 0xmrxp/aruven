"""Configuration loading and validation."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import yaml


class ConfigError(Exception):
    pass


@dataclass
class RiskConfig:
    max_trade_bps: int = 500
    daily_loss_bps: int = 200
    min_interval_sec: int = 3600
    max_slippage_bps: int = 50
    min_usdg_balance: int = 0


@dataclass
class StrategyConfig:
    name: str = "mean_reversion"
    rsi_period: int = 14
    rsi_low: int = 30
    rsi_high: int = 70
    min_edge_bps: int = 25


@dataclass
class LLMConfig:
    enabled: bool = False
    base_url: str = ""
    api_key_env: str = "ARUVEN_LLM_KEY"
    model: str = ""

    @property
    def api_key(self) -> Optional[str]:
        return os.environ.get(self.api_key_env) or None


@dataclass
class JournalConfig:
    path: str = "./journal.ndjson"
    epoch_checkpoint: bool = True
    # Where to publish sealed entries so the backend's /journal has a source.
    # Empty disables publishing (the local chain still verifies offline).
    publish_url: str = ""


@dataclass
class PriceFeedConfig:
    """Market data source. There is deliberately no built-in stub: an
    unconfigured feed halts the loop instead of silently producing no trades.

    `source` picks a keyless public source (recommended: `hyperliquid`, which
    serves real OHLCV candles). `generic` points at a user-hosted endpoint.
    `fallback_source` is retried once when the primary fails.
    """

    source: str = ""            # hyperliquid | binance | generic ("" = infer from url)
    url: str = ""               # only for source: generic
    fallback_source: str = ""   # optional; e.g. binance behind hyperliquid
    interval: str = "1h"        # candle interval: 1m, 5m, 15m, 30m, 1h, 2h, 4h, 1d
    timeout_sec: int = 15
    # How many closes the feed returns. This is the lookback the RSI and
    # z-score are computed over, so it also sets how "slow" the signal is.
    series_limit: int = 200
    # token name -> source symbol, when they differ (e.g. nvda -> NVDA, or a
    # Binance pair like BTCUSDT). Missing entries fall back to upper-casing the
    # token name (Hyperliquid) or appending USDT (Binance).
    symbols: dict[str, str] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.source.strip() or self.url.strip())


@dataclass
class TokenConfig:
    name: str
    address: str
    decimals: int = 18


@dataclass
class AgentConfig:
    rpc_url: str
    chain_id: int
    vault_address: str
    aruven_token: str
    strategy_tokens: list[TokenConfig] = field(default_factory=list)
    executor_private_key_env: str = "ARUVEN_EXECUTOR_KEY"
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    journal: JournalConfig = field(default_factory=JournalConfig)
    price_feed: PriceFeedConfig = field(default_factory=PriceFeedConfig)
    # Router allow-list. The vault keeps the same list on-chain; the agent
    # refuses to build a call for a router the vault would reject anyway.
    allowed_routers: list[str] = field(default_factory=list)
    router_swap_sig: str = "swap(address,address,uint256,uint256,address)"
    loop_interval_sec: int = 900

    @property
    def executor_key(self) -> str:
        key = os.environ.get(self.executor_private_key_env)
        if not key:
            raise ConfigError(
                f"missing executor key: set {self.executor_private_key_env}"
            )
        return key


def _is_address(value: object) -> bool:
    return isinstance(value, str) and value.startswith("0x") and len(value) == 42


def load_config(path: str) -> AgentConfig:
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    try:
        risk = RiskConfig(**(raw.get("risk") or {}))
        strategy = StrategyConfig(**(raw.get("strategy") or {}))
        llm = LLMConfig(**(raw.get("llm") or {}))
        journal = JournalConfig(**(raw.get("journal") or {}))
        price_feed = PriceFeedConfig(**(raw.get("price_feed") or {}))
        tokens = [
            TokenConfig(**t) for t in (raw.get("strategy_tokens") or [])
        ]
        loop = (raw.get("loop") or {}).get("interval_sec", 900)
        routers = [str(r) for r in (raw.get("allowed_routers") or [])]
        cfg = AgentConfig(
            rpc_url=raw["rpc_url"],
            chain_id=int(raw["chain_id"]),
            vault_address=raw["vault_address"],
            aruven_token=raw["aruven_token"],
            strategy_tokens=tokens,
            executor_private_key_env=raw.get(
                "executor_private_key_env", "ARUVEN_EXECUTOR_KEY"
            ),
            risk=risk,
            strategy=strategy,
            llm=llm,
            journal=journal,
            price_feed=price_feed,
            allowed_routers=routers,
            router_swap_sig=raw.get(
                "router_swap_sig", "swap(address,address,uint256,uint256,address)"
            ),
            loop_interval_sec=int(loop),
        )
    except KeyError as e:
        raise ConfigError(f"missing required config key: {e}") from e
    except TypeError as e:
        raise ConfigError(f"invalid config: {e}") from e

    for token in cfg.strategy_tokens:
        if not _is_address(token.address):
            raise ConfigError(f"bad token address for {token.name}: {token.address}")
    for name, addr in [("vault", cfg.vault_address), ("aruven token", cfg.aruven_token)]:
        if not _is_address(addr):
            raise ConfigError(f"bad {name} address: {addr}")
    for r in cfg.allowed_routers:
        if not _is_address(r):
            raise ConfigError(f"bad router address: {r}")
    if cfg.risk.max_trade_bps > 10_000:
        raise ConfigError("max_trade_bps above 10000")
    if not 0 <= cfg.risk.daily_loss_bps <= 10_000:
        raise ConfigError("daily_loss_bps must be within 0..10000")
    if not 0 <= cfg.risk.max_slippage_bps < 10_000:
        raise ConfigError("max_slippage_bps must be within 0..9999")
    if not cfg.strategy_tokens:
        raise ConfigError("at least one strategy token is required")
    if cfg.price_feed.configured:
        if not cfg.price_feed.source.strip() and not cfg.price_feed.url.strip():
            raise ConfigError("price_feed needs a source or a url")
        if not 20 <= cfg.price_feed.series_limit <= 2_000:
            raise ConfigError("price_feed.series_limit must be within 20..2000")
        if cfg.price_feed.timeout_sec <= 0:
            raise ConfigError("price_feed.timeout_sec must be positive")
    return cfg

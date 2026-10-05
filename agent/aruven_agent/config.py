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
    loop_interval_sec: int = 900

    @property
    def executor_key(self) -> str:
        key = os.environ.get(self.executor_private_key_env)
        if not key:
            raise ConfigError(
                f"missing executor key: set {self.executor_private_key_env}"
            )
        return key


def load_config(path: str) -> AgentConfig:
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    try:
        risk = RiskConfig(**(raw.get("risk") or {}))
        strategy = StrategyConfig(**(raw.get("strategy") or {}))
        llm = LLMConfig(**(raw.get("llm") or {}))
        journal = JournalConfig(**(raw.get("journal") or {}))
        tokens = [
            TokenConfig(**t) for t in (raw.get("strategy_tokens") or [])
        ]
        loop = (raw.get("loop") or {}).get("interval_sec", 900)
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
            loop_interval_sec=int(loop),
        )
    except KeyError as e:
        raise ConfigError(f"missing required config key: {e}") from e
    except TypeError as e:
        raise ConfigError(f"invalid config: {e}") from e

    for token in cfg.strategy_tokens:
        if not token.address.startswith("0x") or len(token.address) != 42:
            raise ConfigError(f"bad token address for {token.name}: {token.address}")
    if not cfg.vault_address.startswith("0x") or len(cfg.vault_address) != 42:
        raise ConfigError(f"bad vault address: {cfg.vault_address}")
    if cfg.risk.max_trade_bps > 10_000:
        raise ConfigError("max_trade_bps above 10000")
    return cfg

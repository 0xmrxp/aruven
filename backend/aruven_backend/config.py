"""Config loading for the backend."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml


class ConfigError(Exception):
    pass


@dataclass
class BackendConfig:
    rpc_url: str
    chain_id: int
    vault_address: str
    aruven_token: str
    factory_address: str
    governor_address: str
    strategy_tokens: dict[str, str] = field(default_factory=dict)
    prices: dict[str, float] = field(default_factory=dict)
    db_path: str = "./aruven.db"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    poll_interval_sec: int = 15
    start_block: int = 0
    confirmations: int = 3


def load_config(path: str) -> BackendConfig:
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    for req in ("rpc_url", "chain_id", "vault_address", "aruven_token"):
        if req not in raw:
            raise ConfigError(f"missing required key: {req}")
    storage = raw.get("storage") or {}
    api = raw.get("api") or {}
    indexer = raw.get("indexer") or {}
    cfg = BackendConfig(
        rpc_url=raw["rpc_url"],
        chain_id=int(raw["chain_id"]),
        vault_address=raw["vault_address"],
        aruven_token=raw["aruven_token"],
        factory_address=raw.get("factory_address", "0x" + "0" * 40),
        governor_address=raw.get("governor_address", "0x" + "0" * 40),
        strategy_tokens=dict(raw.get("strategy_tokens") or {}),
        prices={k: float(v) for k, v in (raw.get("prices") or {}).items()},
        db_path=storage.get("db_path", "./aruven.db"),
        api_host=api.get("host", "127.0.0.1"),
        api_port=int(api.get("port", 8000)),
        poll_interval_sec=int(indexer.get("poll_interval_sec", 15)),
        start_block=int(indexer.get("start_block", 0)),
        confirmations=int(indexer.get("confirmations", 3)),
    )
    for name, addr in [("vault", cfg.vault_address), ("token", cfg.aruven_token),
                       ("factory", cfg.factory_address), ("governor", cfg.governor_address)]:
        if not (addr.startswith("0x") and len(addr) == 42):
            raise ConfigError(f"bad {name} address: {addr}")
    return cfg

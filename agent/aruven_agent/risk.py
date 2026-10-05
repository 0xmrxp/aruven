"""Soft risk gate — mirrors the vault's hard on-chain limits (defense in depth)."""
from __future__ import annotations

import time

from .config import AgentConfig
from .types import Observation, Proposal


class RiskGate:
    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg
        self._day_start = time.time()
        self._day_loss_units = 0

    def _roll_day_if_needed(self) -> None:
        now = time.time()
        if now - self._day_start >= 86_400:
            self._day_start = now
            self._day_loss_units = 0

    def check(self, prop: Proposal, obs: Observation, last_trade_ts: float) -> tuple[bool, str]:
        r = self.cfg.risk
        self._roll_day_if_needed()
        if time.time() - last_trade_ts < r.min_interval_sec:
            return False, "cooldown: vault minInterval not elapsed"
        if prop.amount_in <= 0:
            return False, "amount_in zero"
        balance = obs.usdg_balance if prop.token_in == self.cfg.strategy_tokens[0].name else obs.vault_balance
        if prop.amount_in > balance * r.max_trade_bps // 10_000:
            return False, f"amount_in over soft cap ({r.max_trade_bps} bps)"
        if obs.usdg_balance < r.min_usdg_balance:
            return False, "below min USDG buffer"
        if self._day_loss_units * 10_000 > obs.epoch_anchor_nav * r.daily_loss_bps:
            return False, "soft daily loss cap hit"
        return True, "ok"

    def record_slippage_loss(self, expected: int, actual: int) -> None:
        if actual < expected:
            self._day_loss_units += expected - actual

"""Stages 2+3 — score & strategy: from observations to trade proposals."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .config import AgentConfig
from .screen import zscore
from .types import Observation, Proposal, Score

BUY = "buy"
SELL = "sell"


@dataclass
class SignalTracker:
    """Fires a trade once per signal EPISODE, not once per bar.

    The mean-reversion condition stays true for as long as price sits in the
    band. On 1h candles that is dozens of consecutive bars, and the agent traded
    on every one of them — measured against real BTC/ETH/SOL history at ~63
    trades/month with the shipped defaults, and ~150/month with looser RSI bands.

    Tracking the episode instead makes the cadence depend on how often the
    market actually dislocates, which is what should drive it.
    """

    _last: dict[str, Optional[str]] = field(default_factory=dict)

    def update(self, token: str, kind: Optional[str]) -> Optional[str]:
        """Record this bar's signal and describe the transition.

        Returns ``"start"``, ``"change"``, ``"end"``, or ``None`` when the
        signal is unchanged. ``start`` and ``change`` carry new information and
        are actionable; ``end`` is journaled so the trace shows the episode
        closing.
        """
        prev = self._last.get(token)
        self._last[token] = kind
        if prev == kind:
            return None
        if prev is None:
            return "start"
        if kind is None:
            return "end"
        return "change"

    def current(self, token: str) -> Optional[str]:
        return self._last.get(token)

    def reset(self, token: Optional[str] = None) -> None:
        if token is None:
            self._last.clear()
        else:
            self._last.pop(token, None)


class Scorer:
    """Ranks opportunities by expected edge."""

    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg

    def score(self, obs: Observation, series: list[float]) -> Score:
        z = zscore(series) or 0.0
        # edge thesis: mean reversion — dislocation * reversal strength
        dislocation_bps = abs(z) * 100  # rough: 1 sigma ~ 100 bps of reversion edge
        rsi = obs.rsi if obs.rsi is not None else 50.0
        strength = min(1.0, abs(rsi - 50.0) / 25.0)
        edge = dislocation_bps * strength
        mean = sum(series) / len(series) if series else 0.0
        vol = (sum((x - mean) ** 2 for x in series) / max(1, len(series))) ** 0.5
        return Score(
            token=obs.token,
            edge_bps=edge,
            zscore=z,
            volatility=vol,
            rsi=rsi,
        )


class MeanReversionStrategy:
    """Buys the dip / sells the rip on strategy tokens, in USDG terms."""

    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg
        self.stable = cfg.strategy_tokens[0].name  # first entry is the stablecoin

    def signal(self, obs: Observation, score: Score) -> Optional[str]:
        """The signal kind for this bar: BUY, SELL, or None. Pure — no state.

        Without a real RSI (not enough price history) there is no signal to act
        on. A missing RSI used to be coerced to 50.0, which silently disabled
        both branches and made the agent look "cautious" while it could never
        trade at all — refuse explicitly instead.
        """
        s = self.cfg.strategy
        if obs.rsi is None:
            return None
        if obs.price <= 0:
            return None
        if score.edge_bps < s.min_edge_bps:
            return None
        if score.zscore < -1.0 and obs.rsi <= s.rsi_low:
            return BUY
        if score.zscore > 1.0 and obs.rsi >= s.rsi_high:
            return SELL
        return None

    def propose(self, obs: Observation, score: Score) -> Optional[Proposal]:
        """Build the trade for this bar's signal, or None."""
        kind = self.signal(obs, score)
        if kind is None:
            return None

        if kind == BUY:
            amount_in = obs.usdg_balance * self.cfg.risk.max_trade_bps // 10_000
            if amount_in == 0:
                return None
            expected = int(amount_in / obs.price)
            token_in, token_out = self.stable, obs.token
            why = "oversold, mean-reversion buy"
        else:
            amount_in = obs.vault_balance * self.cfg.risk.max_trade_bps // 10_000
            if amount_in == 0:
                return None
            expected = int(amount_in * obs.price)
            token_in, token_out = obs.token, self.stable
            why = "overbought, mean-reversion sell"

        if expected <= 0:
            return None
        return Proposal(
            token_in=token_in,
            token_out=token_out,
            amount_in=amount_in,
            expected_out=expected,
            reason=(
                f"z={score.zscore:.2f} rsi={score.rsi:.0f} edge={score.edge_bps:.0f}bps; {why}"
            ),
            score=score,
            slippage_bps=self.cfg.risk.max_slippage_bps,
        )

"""Stages 2+3 — score & strategy: from observations to trade proposals."""
from __future__ import annotations

from .config import AgentConfig
from .screen import zscore
from .types import Observation, Proposal, Score


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

    def propose(self, obs: Observation, score: Score) -> Proposal | None:
        s = self.cfg.strategy
        # Without a real RSI (not enough price history) there is no signal to
        # act on. Previously a missing RSI was coerced to 50.0, which silently
        # disabled both branches and made the agent look "cautious" while it
        # could never trade at all — refuse explicitly instead.
        if obs.rsi is None:
            return None
        if obs.price <= 0:
            return None
        if score.edge_bps < s.min_edge_bps:
            return None
        if score.zscore < -1.0 and obs.rsi <= s.rsi_low:
            # oversold: buy token with USDG
            amount_in = obs.usdg_balance * self.cfg.risk.max_trade_bps // 10_000
            if amount_in == 0:
                return None
            expected = int(amount_in / obs.price)
            if expected <= 0:
                return None
            return Proposal(
                token_in=self.stable,
                token_out=obs.token,
                amount_in=amount_in,
                expected_out=expected,
                reason=(
                    f"z={score.zscore:.2f} rsi={obs.rsi:.0f} edge={score.edge_bps:.0f}bps; "
                    f"oversold, mean-reversion buy"
                ),
                score=score,
                slippage_bps=self.cfg.risk.max_slippage_bps,
            )
        if score.zscore > 1.0 and obs.rsi >= s.rsi_high:
            # overbought: sell token back to USDG
            amount_in = obs.vault_balance * self.cfg.risk.max_trade_bps // 10_000
            if amount_in == 0:
                return None
            expected = int(amount_in * obs.price)
            if expected <= 0:
                return None
            return Proposal(
                token_in=obs.token,
                token_out=self.stable,
                amount_in=amount_in,
                expected_out=expected,
                reason=(
                    f"z={score.zscore:.2f} rsi={obs.rsi:.0f} edge={score.edge_bps:.0f}bps; "
                    f"overbought, mean-reversion sell"
                ),
                score=score,
                slippage_bps=self.cfg.risk.max_slippage_bps,
            )
        return None

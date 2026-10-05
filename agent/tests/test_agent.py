"""Unit tests for the ARUVEN agent (pure modules, no chain required)."""
import json
import os
import tempfile
import unittest

from aruven_agent.config import AgentConfig, RiskConfig, StrategyConfig, TokenConfig, LLMConfig, JournalConfig
from aruven_agent.journal import Journal
from aruven_agent.risk import RiskGate
from aruven_agent.screen import rsi, zscore
from aruven_agent.strategy import MeanReversionStrategy, Scorer
from aruven_agent.types import Observation, Proposal, Score


def make_cfg() -> AgentConfig:
    return AgentConfig(
        rpc_url="http://localhost:8545",
        chain_id=46630,
        vault_address="0x" + "a" * 40,
        aruven_token="0x" + "b" * 40,
        strategy_tokens=[
            TokenConfig(name="usdg", address="0x" + "c" * 40),
            TokenConfig(name="nvda", address="0x" + "d" * 40),
        ],
        risk=RiskConfig(max_trade_bps=500, daily_loss_bps=200, min_interval_sec=3600, max_slippage_bps=50),
        strategy=StrategyConfig(rsi_period=14, rsi_low=30, rsi_high=70, min_edge_bps=25),
    )


def make_obs(price: float, rsi_val: float, vault_bal: int = 100_000 * 10**18, usdg: int = 1_000_000 * 10**18) -> Observation:
    return Observation(
        ts=0.0, token="nvda", price=price, rsi=rsi_val, volume_24h=1.0,
        vault_balance=int(vault_bal), usdg_balance=int(usdg), epoch=1, epoch_anchor_nav=1_000_000 * 10**18,
    )


class TestMath(unittest.TestCase):
    def test_rsi_all_gains(self):
        series = [float(i) for i in range(30)]  # strictly monotonic gains
        self.assertEqual(rsi(series, 14), 100.0)

    def test_rsi_short_series_none(self):
        self.assertIsNone(rsi([1.0, 2.0], 14))

    def test_zscore_flat(self):
        self.assertEqual(zscore([1.0, 1.0, 1.0]), 0.0)

    def test_zscore_dislocation(self):
        z = zscore([1.0, 1.0, 1.0, 1.0, 2.0])
        self.assertGreater(z, 1.0)


class TestJournal(unittest.TestCase):
    def test_chain_and_verify(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "j.ndjson")
            j = Journal(path)
            j.append("observation", {"x": 1})
            j.append("proposal", {"y": 2})
            ok, count = j.verify_chain()
            self.assertTrue(ok)
            self.assertEqual(count, 2)
            # tamper: rewrite a line
            lines = open(path).read().splitlines()
            entry = json.loads(lines[0])
            entry["payload"]["x"] = 999
            lines[0] = json.dumps(entry, sort_keys=True, separators=(",", ":"))
            open(path, "w").write("\n".join(lines) + "\n")
            ok2, _ = Journal(path).verify_chain()
            self.assertFalse(ok2)

    def test_head_persists_across_instances(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "j.ndjson")
            j1 = Journal(path)
            j1.append("observation", {"x": 1})
            head = j1.head
            j2 = Journal(path)
            self.assertEqual(j2.head, head)


class TestStrategy(unittest.TestCase):
    def test_oversold_buys(self):
        cfg = make_cfg()
        obs = make_obs(price=100.0, rsi_val=25.0)
        score = Score(token="nvda", edge_bps=80.0, zscore=-1.5, volatility=0.1, rsi=25.0)
        prop = MeanReversionStrategy(cfg).propose(obs, score)
        self.assertIsNotNone(prop)
        self.assertEqual(prop.token_in, "usdg")
        self.assertEqual(prop.token_out, "nvda")
        # 5% of 1M USDG
        self.assertEqual(prop.amount_in, 1_000_000 * 10**18 * 500 // 10_000)  # 5%, exact ints

    def test_overbought_sells(self):
        cfg = make_cfg()
        obs = make_obs(price=100.0, rsi_val=75.0)
        score = Score(token="nvda", edge_bps=80.0, zscore=1.5, volatility=0.1, rsi=75.0)
        prop = MeanReversionStrategy(cfg).propose(obs, score)
        self.assertIsNotNone(prop)
        self.assertEqual(prop.token_in, "nvda")
        self.assertEqual(prop.token_out, "usdg")

    def test_low_edge_skipped(self):
        cfg = make_cfg()
        obs = make_obs(price=100.0, rsi_val=25.0)
        score = Score(token="nvda", edge_bps=10.0, zscore=-1.5, volatility=0.1, rsi=25.0)
        self.assertIsNone(MeanReversionStrategy(cfg).propose(obs, score))

    def test_neutral_zone_no_trade(self):
        cfg = make_cfg()
        obs = make_obs(price=100.0, rsi_val=50.0)
        score = Score(token="nvda", edge_bps=80.0, zscore=0.1, volatility=0.1, rsi=50.0)
        self.assertIsNone(MeanReversionStrategy(cfg).propose(obs, score))


class TestRiskGate(unittest.TestCase):
    def test_cooldown_blocks(self):
        cfg = make_cfg()
        gate = RiskGate(cfg)
        obs = make_obs(100.0, 25.0)
        prop = Proposal(token_in="usdg", token_out="nvda", amount_in=1000, expected_out=10,
                        reason="r", score=Score("nvda", 80, -1.5, 0.1, 25))
        import time as _t2
        ok, why = gate.check(prop, obs, last_trade_ts=_t2.time())  # traded just now → cooldown
        self.assertFalse(ok)
        self.assertIn("cooldown", why)

    def test_soft_cap_blocks_oversize(self):
        cfg = make_cfg()
        gate = RiskGate(cfg)
        # time far past last trade
        import time as _t
        obs = make_obs(100.0, 25.0)
        prop = Proposal(token_in="nvda", token_out="usdg",
                        amount_in=int(100_000e18),  # 100% of balance
                        expected_out=1, reason="r",
                        score=Score("nvda", 80, -1.5, 0.1, 25))
        ok, why = gate.check(prop, obs, last_trade_ts=_t.time() - 7200)
        self.assertFalse(ok)
        self.assertIn("soft cap", why)


class TestScorer(unittest.TestCase):
    def test_edge_positive_on_dislocation(self):
        cfg = make_cfg()
        obs = make_obs(100.0, 25.0)
        s = Scorer(cfg).score(obs, [100.0, 100.0, 100.0, 100.0, 80.0])
        self.assertGreater(s.edge_bps, 0)


if __name__ == "__main__":
    unittest.main()

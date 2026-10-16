"""Unit tests for the ARUVEN agent (pure modules, no chain required)."""
import json
import os
import tempfile
import unittest

from aruven_agent.config import (
    AgentConfig,
    ConfigError,
    JournalConfig,
    LLMConfig,
    PriceFeedConfig,
    RiskConfig,
    StrategyConfig,
    TokenConfig,
    load_config,
)
from aruven_agent.executor import EXECUTE_SWAP_SIG, Executor, encode_execute_swap
from aruven_agent.journal import Journal
from aruven_agent.risk import RiskGate
from aruven_agent.screen import rsi, zscore
from aruven_agent.strategy import MeanReversionStrategy, Scorer, SignalTracker
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
        allowed_routers=["0x" + "e" * 40],
    )


def make_obs(price: float, rsi_val, vault_bal: int = 100_000 * 10**18, usdg: int = 1_000_000 * 10**18) -> Observation:
    return Observation(
        ts=0.0, token="nvda", price=price, rsi=rsi_val, volume_24h=1.0,
        vault_balance=int(vault_bal), usdg_balance=int(usdg), epoch=1,
        epoch_anchor_nav=1_000_000 * 10**18,
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

    def test_publish_is_best_effort_without_url(self):
        with tempfile.TemporaryDirectory() as d:
            j = Journal(os.path.join(d, "j.ndjson"))
            entry = j.append("observation", {"x": 1})
            self.assertFalse(j.publish(entry, ""))
            self.assertEqual(len(j.pending_lines()), 1)


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

    def test_missing_rsi_produces_no_trade(self):
        """A missing RSI must block trading explicitly.

        It used to be coerced to 50.0 inside observe(), which silently disabled
        both strategy branches — the agent looked cautious while being
        structurally unable to trade at all.
        """
        cfg = make_cfg()
        obs = make_obs(price=100.0, rsi_val=None)
        score = Score(token="nvda", edge_bps=80.0, zscore=-1.5, volatility=0.1, rsi=50.0)
        self.assertIsNone(MeanReversionStrategy(cfg).propose(obs, score))

    def test_zero_price_no_trade(self):
        cfg = make_cfg()
        obs = make_obs(price=0.0, rsi_val=25.0)
        score = Score(token="nvda", edge_bps=80.0, zscore=-1.5, volatility=0.1, rsi=25.0)
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


class TestExecutorEncoding(unittest.TestCase):
    """The executor's calldata must match the vault's actual ABI."""

    def test_selector_matches_the_vault_signature(self):
        from aruven_common.eip1559 import keccak256

        real = "executeSwap(address,address,uint256,uint256,address,bytes,bytes32)"
        self.assertEqual(EXECUTE_SWAP_SIG, real)
        # the previous signature was missing an address and produced a selector
        # the vault has no function for
        broken = "executeSwap(address,uint256,uint256,address,bytes,bytes32)"
        self.assertNotEqual(
            keccak256(EXECUTE_SWAP_SIG.encode()).hex()[:8],
            keccak256(broken.encode()).hex()[:8],
        )

    def test_dynamic_bytes_use_an_offset_pointer(self):
        router_data = bytes.fromhex("deadbeef")
        calldata = encode_execute_swap(
            token_in="0x" + "11" * 20,
            token_out="0x" + "22" * 20,
            amount_in=12345,
            amount_out_min=6789,
            router="0x" + "77" * 20,
            router_data=router_data,
            reason_hash=bytes(32),
        )
        body = calldata[10:]  # strip 0x + selector
        words = [body[i:i + 64] for i in range(0, len(body), 64)]
        self.assertEqual(len(words), 9, "7 head words + length + padded payload")
        # arg #5 (the `bytes` slot) must be an offset, not inline data
        self.assertEqual(int(words[5], 16), 7 * 32, "offset must point past the head")
        self.assertEqual(int(words[7], 16), len(router_data), "length word follows the head")
        self.assertTrue(words[8].startswith("deadbeef"))

    def test_reason_hash_is_the_last_head_word(self):
        rh = bytes.fromhex("cd" * 32)
        calldata = encode_execute_swap(
            token_in="0x" + "11" * 20, token_out="0x" + "22" * 20,
            amount_in=1, amount_out_min=1, router="0x" + "77" * 20,
            router_data=b"", reason_hash=rh,
        )
        body = calldata[10:]
        self.assertEqual(body[6 * 64:7 * 64], rh.hex())

    def test_router_allow_list_is_required(self):
        cfg = make_cfg()
        cfg.allowed_routers = []
        ex = Executor(cfg, None, dry_run=True)
        prop = Proposal(token_in="usdg", token_out="nvda", amount_in=10, expected_out=1,
                        reason="r", score=Score("nvda", 80, -1.5, 0.1, 25))
        with self.assertRaises(RuntimeError):
            ex.build_calldata(prop, bytes(32))

    def test_zero_min_out_is_refused(self):
        """The vault rejects a zero slippage floor; the agent must not build one."""
        cfg = make_cfg()
        ex = Executor(cfg, None, dry_run=True)
        prop = Proposal(token_in="usdg", token_out="nvda", amount_in=10, expected_out=1,
                        reason="r", score=Score("nvda", 80, -1.5, 0.1, 25),
                        slippage_bps=10_000)
        with self.assertRaises(ValueError):
            ex._min_out(prop)

    def test_min_out_applies_slippage(self):
        cfg = make_cfg()
        ex = Executor(cfg, None, dry_run=True)
        prop = Proposal(token_in="usdg", token_out="nvda", amount_in=10, expected_out=10_000,
                        reason="r", score=Score("nvda", 80, -1.5, 0.1, 25), slippage_bps=50)
        self.assertEqual(ex._min_out(prop), 9_950)


class TestConfigValidation(unittest.TestCase):
    def _write(self, d, body):
        path = os.path.join(d, "c.yaml")
        with open(path, "w") as f:
            f.write(body)
        return path

    BASE = """
rpc_url: http://localhost:8545
chain_id: 46630
vault_address: "0x{va}"
aruven_token: "0x{tok}"
strategy_tokens:
  - name: usdg
    address: "0x{va}"
  - name: nvda
    address: "0x{tok}"
""".format(va="a" * 40, tok="b" * 40)

    def test_valid_config_loads(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(self._write(d, self.BASE))
            self.assertEqual(cfg.chain_id, 46630)
            self.assertEqual(len(cfg.strategy_tokens), 2)
            self.assertFalse(cfg.price_feed.configured)

    def test_bad_router_address_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            body = self.BASE + "allowed_routers:\n  - \"0xnothex\"\n"
            with self.assertRaises(ConfigError):
                load_config(self._write(d, body))

    def test_absurd_slippage_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            body = self.BASE + "risk:\n  max_slippage_bps: 10000\n"
            with self.assertRaises(ConfigError):
                load_config(self._write(d, body))

    def test_missing_token_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            body = self.BASE.replace('strategy_tokens:\n  - name: usdg\n    address: "0x' + "a" * 40 + '"\n  - name: nvda\n    address: "0x' + "b" * 40 + '"\n', "strategy_tokens: []\n")
            with self.assertRaises(ConfigError):
                load_config(self._write(d, body))


class TestSignalTracker(unittest.TestCase):
    """Episode-triggered entries: the fix for ~63 trades/month."""

    def test_start_then_silent_while_signal_persists(self):
        t = SignalTracker()
        self.assertEqual(t.update("nvda", "buy"), "start")
        # the condition stays true for many bars — must NOT re-fire
        for _ in range(20):
            self.assertIsNone(t.update("nvda", "buy"))

    def test_end_then_new_start(self):
        t = SignalTracker()
        t.update("nvda", "buy")
        self.assertEqual(t.update("nvda", None), "end")
        self.assertIsNone(t.update("nvda", None))
        self.assertEqual(t.update("nvda", "buy"), "start")

    def test_direction_flip_is_a_change(self):
        t = SignalTracker()
        t.update("nvda", "buy")
        self.assertEqual(t.update("nvda", "sell"), "change")
        self.assertEqual(t.current("nvda"), "sell")

    def test_tokens_are_tracked_independently(self):
        t = SignalTracker()
        self.assertEqual(t.update("nvda", "buy"), "start")
        self.assertEqual(t.update("gold", "buy"), "start")
        self.assertIsNone(t.update("nvda", "buy"))

    def test_reset(self):
        t = SignalTracker()
        t.update("nvda", "buy")
        t.reset("nvda")
        self.assertEqual(t.update("nvda", "buy"), "start")


class TestCadence(unittest.TestCase):
    """Locks the trade cadence: a persistent signal must produce ONE trade.

    Regression guard for the bug where the strategy fired on every bar the
    condition held, measured at ~63 trades/month on real 1h data.
    """

    def _run(self, series, cfg):
        """Replay the production path over a series; return the trades taken."""
        scorer = Scorer(cfg)
        strat = MeanReversionStrategy(cfg)
        tracker = SignalTracker()
        obs_trades = []
        window = 40  # small so the synthetic series is enough
        for i in range(window, len(series)):
            s = series[i - window + 1:i + 1]
            r = rsi(s, cfg.strategy.rsi_period)
            if r is None:
                continue
            obs = Observation(ts=float(i), token="nvda", price=s[-1], rsi=r,
                              volume_24h=0.0, vault_balance=int(10_000e18),
                              usdg_balance=int(1_000_000e18),
                              **{"epoch": 0, "epoch_anchor_nav": 0})
            sc = scorer.score(obs, s)
            kind = strat.signal(obs, sc)
            if tracker.update("nvda", kind) not in ("start", "change"):
                continue
            prop = strat.propose(obs, sc)
            if prop is not None:
                obs_trades.append((i, kind))
        return obs_trades

    def _cfg(self, cooldown=3600):
        return AgentConfig(
            rpc_url="http://x", chain_id=46630, vault_address="0x" + "a" * 40,
            aruven_token="0x" + "b" * 40,
            strategy_tokens=[TokenConfig("usdg", "0x" + "c" * 40),
                             TokenConfig("nvda", "0x" + "d" * 40)],
            risk=RiskConfig(max_trade_bps=500, daily_loss_bps=200,
                            min_interval_sec=cooldown, max_slippage_bps=50),
            strategy=StrategyConfig(rsi_period=14, rsi_low=30, rsi_high=70,
                                    min_edge_bps=25),
        )

    def _crash_then_flat(self, bars_in_crash=60):
        """Steady 100s, a long dump, then flat at the bottom.

        The RSI stays oversold for the whole flat stretch, so a level-triggered
        strategy would trade on nearly every bar of it.
        """
        return [100.0] * 60 + [100.0 - i for i in range(1, 21)] + [80.0] * bars_in_crash

    def test_persistent_signal_trades_once_not_per_bar(self):
        series = self._crash_then_flat()
        trades = self._run(series, self._cfg())
        self.assertEqual(len(trades), 1, f"expected 1 trade, got {trades}")
        self.assertEqual(trades[0][1], "buy")

    def test_each_episode_trades_once_and_flips_side(self):
        """Every distinct episode fires exactly one trade, on the right side.

        The recovery leg in the middle is itself an overbought episode, so the
        sequence is buy → sell → buy. Pinning the sides down is the point: a
        level-triggered strategy would have produced dozens of trades here.
        """
        series = (
            [100.0] * 60
            + [100.0 - i for i in range(1, 21)]   # dump -> oversold episode
            + [80.0] * 40                          # flat, signal persists
            + [80.0 + i for i in range(1, 26)]     # rally -> overbought episode
            + [105.0] * 40                         # flat, signal persists
            + [105.0 - i * 1.5 for i in range(1, 21)]  # second dump
            + [75.0] * 30
        )
        trades = self._run(series, self._cfg())
        kinds = [k for _, k in trades]
        self.assertEqual(kinds, ["buy", "sell", "buy"], f"got {trades}")

    def test_no_trade_while_signal_persists_after_first(self):
        """The bars between episodes must produce nothing."""
        series = self._crash_then_flat(bars_in_crash=150)
        trades = self._run(series, self._cfg())
        self.assertEqual(len(trades), 1, f"got {trades}")
        self.assertEqual(trades[0][1], "buy")

    def test_cooldown_does_not_multiply_trades(self):
        """A short cooldown must not re-introduce per-bar firing."""
        series = self._crash_then_flat(bars_in_crash=200)
        short = self._run(series, self._cfg(cooldown=3600))
        self.assertEqual(len(short), 1)


class TestFeeds(unittest.TestCase):
    """Market-data adapters (HTTP is stubbed; no network in tests)."""

    def _cfg(self, **kw):
        return PriceFeedConfig(**kw)

    def test_hyperliquid_parses_candles_and_sums_volume(self):
        from aruven_agent import feeds

        candles = [{"c": str(100 + i), "v": "2.0"} for i in range(30)]
        orig = feeds._post_json
        feeds._post_json = lambda url, payload, timeout: candles
        try:
            prices, vol = feeds.HyperliquidFeed(self._cfg(source="hyperliquid", interval="1h")).series("btc")
        finally:
            feeds._post_json = orig
        self.assertEqual(len(prices), 30)
        self.assertEqual(prices[0], 100.0)
        self.assertEqual(prices[-1], 129.0)
        self.assertEqual(vol, 48.0)  # 24 hourly bars * 2.0

    def test_hyperliquid_symbol_mapping(self):
        from aruven_agent import feeds

        seen = {}

        def spy(url, payload, timeout):
            seen.update(payload["req"])
            return [{"c": "1", "v": "0"}]

        orig = feeds._post_json
        feeds._post_json = spy
        try:
            feed = feeds.HyperliquidFeed(self._cfg(source="hyperliquid",
                                                  symbols={"nvda": "NVDA"}))
            feed.series("nvda")
            self.assertEqual(seen["coin"], "NVDA")
            feed.series("btc")  # unmapped -> upper-cased
            self.assertEqual(seen["coin"], "BTC")
        finally:
            feeds._post_json = orig

    def test_binance_appends_usdt_unless_mapped(self):
        from aruven_agent import feeds

        seen = {}

        def spy(url, timeout):
            seen["url"] = url
            return [[0, "0", "0", "0", "100.5", "3.0"]]

        orig = feeds._get_json
        feeds._get_json = spy
        try:
            feed = feeds.BinanceFeed(self._cfg(source="binance"))
            feed.series("btc")
            self.assertIn("BTCUSDT", seen["url"])
            feed.series("ethusdc")
            self.assertIn("ETHUSDC", seen["url"])
        finally:
            feeds._get_json = orig

    def test_fallback_is_used_when_primary_fails(self):
        from aruven_agent import feeds

        cfg = self._cfg(source="hyperliquid", fallback_source="binance")
        feed = feeds.PriceFeed(cfg)

        def boom(token):
            raise feeds.FeedError("primary down")

        feed.primary.series = boom
        self.assertIsNotNone(feed.fallback)
        feed.fallback.series = lambda token: ([1.0, 2.0, 3.0], 7.0)
        prices, vol = feed.series("btc")
        self.assertEqual(prices, [1.0, 2.0, 3.0])
        self.assertEqual(feed.last_source, "binance")

    def test_all_sources_failing_raises(self):
        from aruven_agent import feeds

        feed = feeds.PriceFeed(self._cfg(source="hyperliquid", fallback_source="binance"))

        def boom(token):
            raise feeds.FeedError("down")

        feed.primary.series = boom
        feed.fallback.series = boom
        with self.assertRaises(feeds.FeedError) as ctx:
            feed.series("btc")
        self.assertIn("all sources failed", str(ctx.exception))

    def test_unknown_source_rejected(self):
        from aruven_agent import feeds

        with self.assertRaises(feeds.FeedError):
            feeds.PriceFeed(self._cfg(source="nosuchsource"))

    def test_no_source_configured_rejected(self):
        from aruven_agent import feeds

        with self.assertRaises(feeds.FeedError):
            feeds.PriceFeed(self._cfg())

    def test_generic_requires_url(self):
        from aruven_agent import feeds

        with self.assertRaises(feeds.FeedError):
            feeds.GenericFeed(self._cfg(source="generic"))

    def test_generic_parses_documented_shape(self):
        from aruven_agent import feeds

        orig = feeds._get_json
        feeds._get_json = lambda url, timeout: {"prices": [1.0, 2.0], "volume_24h": 5.0}
        try:
            prices, vol = feeds.GenericFeed(self._cfg(source="generic", url="http://x")).series("btc")
        finally:
            feeds._get_json = orig
        self.assertEqual(prices, [1.0, 2.0])
        self.assertEqual(vol, 5.0)

    def test_unsupported_interval_rejected(self):
        from aruven_agent import feeds

        with self.assertRaises(feeds.FeedError):
            feeds.interval_ms("3h")


if __name__ == "__main__":
    unittest.main()

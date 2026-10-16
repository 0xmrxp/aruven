"""End-to-end pipeline test against a mock JSON-RPC server.

Verifies the full loop: observation → proposal → review → (dry-run) execution,
with a hash-chained journal that passes chain verification.
"""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from aruven_agent.config import (
    AgentConfig,
    JournalConfig,
    LLMConfig,
    PriceFeedConfig,
    RiskConfig,
    StrategyConfig,
    TokenConfig,
)
from aruven_agent.journal import Journal
from aruven_agent.main import Agent

VAULT = "0x000000000000000000000000000000000000dead"
USDG = "0x0000000000000000000000000000000000000001"
NVDA = "0x0000000000000000000000000000000000000002"
ROUTER = "0x" + "aa" * 20
BAL = {USDG: 1_000_000 * 10**18, NVDA: 1_000 * 10**18}


def _sel(sig):
    from aruven_common.eip1559 import keccak256
    return keccak256(sig.encode()).hex()[:8]


VIEWS = {
    "epoch()": 3,
    "epochAnchorNav()": 1_000_000 * 10**18,
    "lastTradeAt()": 0,
    "maxTradeBps()": 500,
    "minInterval()": 3600,
    # the agent reads these to mirror the vault's daily-loss gate
    "dailyLossBps()": 200,
    "epochLossUnits()": 0,
}


class MockRPC(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        method, params = body["method"], body.get("params", [])
        result = "0x"
        if method == "eth_call":
            data, to = params[0]["data"], params[0]["to"].lower()
            if to == VAULT:
                for sig, val in VIEWS.items():
                    if data.startswith("0x" + _sel(sig)):
                        result = hex(val)
                        break
            elif to in BAL:
                result = hex(BAL[to])
        elif method == "eth_chainId":
            result = hex(46630)
        raw = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


class FakeFeed:
    """Stands in for the market-data client (the real one is HTTP)."""

    def __init__(self, series_by_token, volume=5_000_000.0):
        self.series_by_token = series_by_token
        self.volume = volume

    def series(self, token):
        return self.series_by_token.get(token, [1.0] * 21), self.volume


def _cfg(d, port, **overrides):
    base = dict(
        rpc_url=f"http://127.0.0.1:{port}",
        chain_id=46630,
        vault_address=VAULT,
        aruven_token="0x" + "9" * 40,
        strategy_tokens=[TokenConfig("usdg", USDG), TokenConfig("nvda", NVDA)],
        risk=RiskConfig(max_trade_bps=500, daily_loss_bps=200, min_interval_sec=3600, max_slippage_bps=50),
        strategy=StrategyConfig(rsi_period=14, rsi_low=30, rsi_high=70, min_edge_bps=25),
        llm=LLMConfig(enabled=False),
        journal=JournalConfig(path=os.path.join(d, "j.ndjson"), epoch_checkpoint=False),
        price_feed=PriceFeedConfig(url="http://feed.invalid/prices"),
        allowed_routers=[ROUTER],
    )
    base.update(overrides)
    return AgentConfig(**base)


class TestEndToEnd(unittest.TestCase):
    def _serve(self):
        srv = HTTPServer(("127.0.0.1", 0), MockRPC)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, srv.server_address[1]

    def test_full_pipeline_dry_run(self):
        srv, port = self._serve()
        try:
            with tempfile.TemporaryDirectory() as d:
                cfg = _cfg(d, port)
                agent = Agent(cfg, dry_run=True)
                # 20 flat closes + one dump to 80 → oversold
                agent.feed = FakeFeed({"nvda": [100.0] * 20 + [80.0], "usdg": [1.0] * 21})
                agent.run_once()

                entries = [json.loads(line) for line in open(cfg.journal.path)]
                kinds = [e["kind"] for e in entries]
                self.assertIn("observation", kinds)
                self.assertIn("proposal", kinds)
                self.assertIn("review", kinds)
                self.assertIn("execution", kinds)
                # chain verifies
                ok, count = agent.journal.verify_chain()
                self.assertTrue(ok)
                self.assertGreaterEqual(count, 4)
                # the trade is the expected 5% USDG position
                prop = next(e for e in entries if e["kind"] == "proposal")["payload"]
                self.assertEqual(prop["token_in"], "usdg")
                self.assertEqual(prop["token_out"], "nvda")
                self.assertEqual(prop["amount_in"], 1_000_000 * 10**18 * 500 // 10_000)
        finally:
            srv.shutdown()

    def test_no_trade_in_neutral_market(self):
        srv, port = self._serve()
        try:
            with tempfile.TemporaryDirectory() as d:
                cfg = _cfg(d, port, risk=RiskConfig())
                agent = Agent(cfg, dry_run=True)
                agent.feed = FakeFeed({"nvda": [100.0] * 21}, volume=0.0)
                agent.run_once()
                entries = [json.loads(line) for line in open(cfg.journal.path)]
                kinds = [e["kind"] for e in entries]
                self.assertNotIn("proposal", kinds)
                self.assertNotIn("execution", kinds)
        finally:
            srv.shutdown()

    def test_short_history_is_reported_not_silently_skipped(self):
        """Too little price history must produce an explicit `skip` entry.

        Previously a missing RSI became 50.0, so the agent journaled a
        perfectly normal observation while being unable to ever trade.
        """
        srv, port = self._serve()
        try:
            with tempfile.TemporaryDirectory() as d:
                cfg = _cfg(d, port)
                agent = Agent(cfg, dry_run=True)
                agent.feed = FakeFeed({"nvda": [1.0, 1.01, 0.99, 1.0, 1.0]})
                agent.run_once()
                entries = [json.loads(line) for line in open(cfg.journal.path)]
                kinds = [e["kind"] for e in entries]
                self.assertIn("skip", kinds)
                self.assertNotIn("proposal", kinds)
                skip = next(e for e in entries if e["kind"] == "skip")["payload"]
                self.assertIn("insufficient price history", skip["reason"])
                self.assertEqual(skip["needed"], 15)
                # the observation records the absent RSI rather than inventing one
                obs = next(e for e in entries if e["kind"] == "observation")["payload"]
                self.assertIsNone(obs["rsi"])
        finally:
            srv.shutdown()

    def test_journal_publish_posts_sealed_entries(self):
        """With publish_url set, sealed entries are POSTed to the backend."""
        srv, port = self._serve()
        posts = []

        class JournalHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                posts.append((self.path, body))
                raw = json.dumps({"ok": True}).encode()
                self.send_response(201)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass

        backend = HTTPServer(("127.0.0.1", 0), JournalHandler)
        threading.Thread(target=backend.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as d:
                cfg = _cfg(
                    d, port,
                    journal=JournalConfig(
                        path=os.path.join(d, "j.ndjson"),
                        epoch_checkpoint=False,
                        publish_url=f"http://127.0.0.1:{backend.server_address[1]}",
                    ),
                )
                agent = Agent(cfg, dry_run=True)
                agent.feed = FakeFeed({"nvda": [100.0] * 20 + [80.0]})
                agent.run_once()

                self.assertTrue(posts, "entries must be published")
                paths = {p for p, _ in posts}
                self.assertEqual(paths, {"/journal"})
                # published entries carry the sealed hash + chain link
                first = posts[0][1]
                for field in ("id", "ts", "kind", "payload", "prev_hash", "hash"):
                    self.assertIn(field, first)
                self.assertTrue(first["hash"].startswith("0x"))
        finally:
            backend.shutdown()
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()

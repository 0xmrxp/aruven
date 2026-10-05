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

from aruven_agent.config import AgentConfig, JournalConfig, LLMConfig, RiskConfig, StrategyConfig, TokenConfig
from aruven_agent.journal import Journal
from aruven_agent.main import Agent

VAULT = "0x000000000000000000000000000000000000dead"
USDG = "0x0000000000000000000000000000000000000001"
NVDA = "0x0000000000000000000000000000000000000002"
BAL = {USDG: 1_000_000 * 10**18, NVDA: 1_000 * 10**18}


def _sel(sig):
    from Crypto.Hash import keccak
    h = keccak.new(digest_bits=256)
    h.update(sig.encode())
    return h.hexdigest()[:8]


VIEWS = {
    "epoch()": 3,
    "epochAnchorNav()": 1_000_000 * 10**18,
    "lastTradeAt()": 0,
    "maxTradeBps()": 500,
    "minInterval()": 3600,
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


class TestEndToEnd(unittest.TestCase):
    def test_full_pipeline_dry_run(self):
        srv = HTTPServer(("127.0.0.1", 0), MockRPC)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as d:
                cfg = AgentConfig(
                    rpc_url=f"http://127.0.0.1:{port}",
                    chain_id=46630,
                    vault_address=VAULT,
                    aruven_token="0x" + "9" * 40,
                    strategy_tokens=[TokenConfig("usdg", USDG), TokenConfig("nvda", NVDA)],
                    risk=RiskConfig(max_trade_bps=500, daily_loss_bps=200, min_interval_sec=3600, max_slippage_bps=50),
                    strategy=StrategyConfig(rsi_period=14, rsi_low=30, rsi_high=70, min_edge_bps=25),
                    llm=LLMConfig(enabled=False),
                    journal=JournalConfig(path=os.path.join(d, "j.ndjson"), epoch_checkpoint=False),
                )

                class FakeAgent(Agent):
                    def _price_series(self, token):
                        # 20 flat closes + one dump to 80 → oversold
                        return [100.0] * 20 + [80.0] if token == "nvda" else [1.0] * 21

                    def _price(self, token):
                        return self._price_series(token)[-1]

                    def _volume(self, token):
                        return 5_000_000.0

                agent = FakeAgent(cfg, dry_run=True)
                agent.run_once()

                # journal contains the full decision trace
                entries = [json.loads(l) for l in open(cfg.journal.path)]
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
        srv = HTTPServer(("127.0.0.1", 0), MockRPC)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as d:
                cfg = AgentConfig(
                    rpc_url=f"http://127.0.0.1:{port}",
                    chain_id=46630,
                    vault_address=VAULT,
                    aruven_token="0x" + "9" * 40,
                    strategy_tokens=[TokenConfig("usdg", USDG), TokenConfig("nvda", NVDA)],
                    risk=RiskConfig(),
                    journal=JournalConfig(path=os.path.join(d, "j.ndjson"), epoch_checkpoint=False),
                )

                class FlatAgent(Agent):
                    def _price_series(self, token):
                        return [100.0] * 21

                    def _price(self, token):
                        return 100.0

                    def _volume(self, token):
                        return 0.0

                agent = FlatAgent(cfg, dry_run=True)
                agent.run_once()
                entries = [json.loads(l) for l in open(cfg.journal.path)]
                kinds = [e["kind"] for e in entries]
                self.assertNotIn("proposal", kinds)
                self.assertNotIn("execution", kinds)
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()

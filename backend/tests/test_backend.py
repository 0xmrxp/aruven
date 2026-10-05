"""Backend tests: indexer ingestion, API endpoints, signer correctness."""
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from fastapi.testclient import TestClient

from aruven_backend.config import BackendConfig
from aruven_backend.indexer import Indexer, Rpc, TOPICS
from aruven_backend.api import create_app
from aruven_backend import secp256k1
from aruven_backend.signer import Signer, rlp_encode, keccak256

VAULT = "0x" + "aa" * 20
TOKEN = "0x" + "bb" * 20
FACTORY = "0x" + "cc" * 20
GOV = "0x" + "dd" * 20
USDG = "0x" + "11" * 20
NVDA = "0x" + "22" * 20


def make_cfg(db_path: str) -> BackendConfig:
    return BackendConfig(
        rpc_url="http://127.0.0.1:1",  # replaced per-test
        chain_id=46630,
        vault_address=VAULT,
        aruven_token=TOKEN,
        factory_address=FACTORY,
        governor_address=GOV,
        strategy_tokens={"usdg": USDG, "nvda": NVDA},
        prices={"usdg": 1.0, "nvda": 235.0},
        db_path=db_path,
        confirmations=0,
    )


class MockChainHandler(BaseHTTPRequestHandler):
    """Serves logs + balances for the indexer/API tests."""

    state = {"tip": 100, "logs": [], "balances": {}}

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        method, params = body["method"], body.get("params", [])
        result = "0x"
        if method == "eth_blockNumber":
            result = hex(self.state["tip"])
        elif method == "eth_getLogs":
            f, t = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
            first = params[0]["topics"][0]
            wanted = set(first) if isinstance(first, list) else {first}
            result = [l for l in self.state["logs"]
                      if f <= int(l["blockNumber"], 16) <= t and l["topics"][0] in wanted]
        elif method == "eth_call":
            to = params[0]["to"].lower()
            if to in self.state["balances"]:
                result = hex(self.state["balances"][to])
        self._send({"jsonrpc": "2.0", "id": 1, "result": result})

    def _send(self, obj):
        raw = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


class TestIndexer(unittest.TestCase):
    def test_ingest_and_rederive(self):
        with tempfile.TemporaryDirectory() as d:
            db = sqlite3.connect(os.path.join(d, "t.db"), check_same_thread=False)
            cfg = make_cfg(os.path.join(d, "t.db"))

            # fabricate events exactly as the chain would emit them
            logs = [
                {  # FeeReceived(usdg, 1000e18)
                    "address": VAULT, "blockNumber": "0xa", "transactionHash": "0x" + "1" * 64,
                    "logIndex": "0x0",
                    "topics": [TOPICS["fee"], "0x" + "00" * 12 + USDG[2:]],
                    "data": "0x" + hex(1000 * 10**18)[2:].rjust(128, "0"),
                },
                {  # VaultBurn(500e18)
                    "address": VAULT, "blockNumber": "0xa", "transactionHash": "0x" + "2" * 64,
                    "logIndex": "0x1",
                    "topics": [TOPICS["burn"]],
                    "data": "0x" + hex(500 * 10**18)[2:].rjust(128, "0"),
                },
                {  # Launch(token, vault, creator, 1500, metaHash)
                    "address": FACTORY, "blockNumber": "0xb", "transactionHash": "0x" + "3" * 64,
                    "logIndex": "0x0",
                    "topics": [TOPICS["launch"],
                               "0x" + "00" * 12 + TOKEN[2:],
                               "0x" + "00" * 12 + VAULT[2:],
                               "0x" + "00" * 12 + "ee" * 20],
                    "data": "0x" + hex(1500)[2:].rjust(64, "0") + "4" * 64,
                },
            ]
            # serve via mock RPC
            MockChainHandler.state = {"tip": 12, "logs": logs, "balances": {
                USDG: 1000 * 10**18, NVDA: 4 * 10**18, TOKEN: 2_000_000 * 10**18}}
            srv = HTTPServer(("127.0.0.1", 0), MockChainHandler)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()

            cfg.rpc_url = f"http://127.0.0.1:{port}"
            rpc = Rpc(cfg.rpc_url)
            idx = Indexer(cfg, db, rpc)
            n = idx.poll_once()
            self.assertGreaterEqual(n, 1)

            # idempotent: poll again, no duplicates
            idx.poll_once()
            fees = db.execute("SELECT COUNT(*) FROM fees").fetchone()[0]
            burns = db.execute("SELECT COUNT(*) FROM burns").fetchone()[0]
            launches = db.execute("SELECT COUNT(*) FROM launches").fetchone()[0]
            self.assertEqual((fees, burns, launches), (1, 1, 1))

            # re-derive from scratch: fresh db, same state
            db2 = sqlite3.connect(os.path.join(d, "t2.db"), check_same_thread=False)
            cfg2 = make_cfg(os.path.join(d, "t2.db"))
            cfg2.rpc_url = cfg.rpc_url
            idx2 = Indexer(cfg2, db2, Rpc(cfg2.rpc_url))
            idx2.poll_once()
            f2 = db2.execute("SELECT token, amount FROM fees").fetchone()
            self.assertEqual(f2[0].lower(), USDG.lower())
            launch2 = db2.execute("SELECT token, fee_share_bps FROM launches").fetchone()
            self.assertEqual(int(launch2[1], 16), 1500)
            srv.shutdown()


class TestApi(unittest.TestCase):
    def test_endpoints(self):
        with tempfile.TemporaryDirectory() as d:
            db = sqlite3.connect(os.path.join(d, "t.db"), check_same_thread=False)
            cfg = make_cfg(os.path.join(d, "t.db"))
            MockChainHandler.state = {"tip": 50, "logs": [], "balances": {
                USDG: 1000 * 10**18, NVDA: 4 * 10**18, TOKEN: 2 * 10**6 * 10**18}}
            srv = HTTPServer(("127.0.0.1", 0), MockChainHandler)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            cfg.rpc_url = f"http://127.0.0.1:{port}"
            Indexer(cfg, db, Rpc(cfg.rpc_url))

            app = create_app(cfg, db, Rpc(cfg.rpc_url))
            client = TestClient(app)

            r = client.get("/health")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["ok"])

            r = client.get("/vault")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertEqual(body["holdings_raw"]["usdg"], 1000 * 10**18)
            self.assertEqual(body["holdings_raw"]["nvda"], 4 * 10**18)
            # NAV = 1000*1 + 4*235
            self.assertAlmostEqual(body["nav_usd"], 1940.0)

            r = client.get("/verify")
            self.assertEqual(r.status_code, 200)
            self.assertIn("event_counts", r.json())

            r = client.get("/deployments")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), [])
            srv.shutdown()


class TestSigner(unittest.TestCase):
    PRIV = "01" * 32  # deterministic test key

    def test_pubkey_matches_known_vector(self):
        # privkey = 1 => pubkey = G (textbook vector)
        pub = secp256k1.privkey_to_pubkey((1).to_bytes(32, "big"))
        x = int.from_bytes(pub[:32], "big")
        y = int.from_bytes(pub[32:], "big")
        self.assertEqual(x, secp256k1.Gx)
        self.assertEqual(y, secp256k1.Gy)
        # 2G vector
        pub2 = secp256k1.privkey_to_pubkey((2).to_bytes(32, "big"))
        self.assertEqual(int.from_bytes(pub2[:32], "big"),
                         89565891926547004231252920425935692360644145829622209833684329913297188986597)

    def test_sign_recoverable(self):
        msg = keccak256(b"test message")
        r, s, rec = secp256k1.sign(bytes.fromhex(self.PRIV), msg)
        self.assertTrue(0 < r < secp256k1.N)
        self.assertTrue(0 < s <= secp256k1.N)
        # low-s
        self.assertLessEqual(s, secp256k1.N // 2)
        # determinism: same msg -> same sig
        r2, s2, rec2 = secp256k1.sign(bytes.fromhex(self.PRIV), msg)
        self.assertEqual((r, s, rec), (r2, s2, rec2))

    def test_signer_address_derivation(self):
        s = Signer(self.PRIV, 46630)
        # address = keccak(pub)[12:] — verify via pubkey re-derivation
        pub = secp256k1.privkey_to_pubkey(bytes.fromhex(self.PRIV))
        h = keccak256(pub)
        self.assertEqual(s.address, "0x" + h[-20:].hex())

    def test_sign_tx_type2(self):
        s = Signer(self.PRIV, 46630)
        raw = s.sign_tx({
            "nonce": 0, "to": VAULT, "value": 0,
            "data": "0x1234", "priority_fee": 10**9, "max_fee": 10**10, "gas_limit": 21000,
        })
        self.assertTrue(raw.startswith("0x02"))
        self.assertGreater(len(raw), 100)

    def test_rlp_known(self):
        # RLP("dog") = 0x83646f67 (textbook vector)
        self.assertEqual(rlp_encode(b"dog"), bytes.fromhex("83646f67"))
        # RLP([]) = 0xc0
        self.assertEqual(rlp_encode([]), bytes.fromhex("c0"))
        # RLP(1024) = 0x820400
        self.assertEqual(rlp_encode(1024), bytes.fromhex("820400"))


if __name__ == "__main__":
    unittest.main()

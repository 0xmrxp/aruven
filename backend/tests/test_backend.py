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
from aruven_backend.api import create_app, _entry_hash
from aruven_backend import secp256k1
from aruven_backend.signer import Signer, rlp_encode, keccak256, signing_hash

VAULT = "0x" + "aa" * 20
TOKEN = "0x" + "bb" * 20
FACTORY = "0x" + "cc" * 20
GOV = "0x" + "dd" * 20
USDG = "0x" + "11" * 20
NVDA = "0x" + "22" * 20
HOOK = "0x" + "ff" * 20
GENESIS = "0x" + "0" * 64


def word(value: int) -> str:
    return hex(value)[2:].rjust(64, "0")


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


def _serve():
    srv = HTTPServer(("127.0.0.1", 0), MockChainHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


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
                    "data": "0x" + word(1000 * 10**18),
                },
                {  # VaultBurn(500e18)
                    "address": VAULT, "blockNumber": "0xa", "transactionHash": "0x" + "2" * 64,
                    "logIndex": "0x1",
                    "topics": [TOPICS["burn"]],
                    "data": "0x" + word(500 * 10**18),
                },
                {  # Launch(token, vault, creator, hook, 1500, metaHash)
                    "address": FACTORY, "blockNumber": "0xb", "transactionHash": "0x" + "3" * 64,
                    "logIndex": "0x0",
                    "topics": [TOPICS["launch"],
                               "0x" + "00" * 12 + TOKEN[2:],
                               "0x" + "00" * 12 + VAULT[2:],
                               "0x" + "00" * 12 + "ee" * 20],
                    "data": "0x" + word(int(HOOK, 16)) + word(1500) + "4" * 64,
                },
            ]
            MockChainHandler.state = {"tip": 12, "logs": logs, "balances": {
                USDG: 1000 * 10**18, NVDA: 4 * 10**18, TOKEN: 2_000_000 * 10**18}}
            srv, port = _serve()
            try:
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
                # amounts are decimal strings now, not hex
                self.assertEqual(int(f2[1]), 1000 * 10**18)
                launch2 = db2.execute(
                    "SELECT token, hook, fee_share_bps FROM launches"
                ).fetchone()
                self.assertEqual(int(launch2[2]), 1500)
                self.assertEqual(launch2[1].lower(), HOOK.lower())
            finally:
                srv.shutdown()

    def test_epoch_checkpoint_does_not_stall_the_indexer(self):
        """EpochCheckpoint carries journalHead in DATA, not topics[2].

        Reading topics[2] raised IndexError before the head was advanced, so the
        indexer retried the same range forever and never moved past the first
        checkpoint the vault emitted.
        """
        with tempfile.TemporaryDirectory() as d:
            db = sqlite3.connect(os.path.join(d, "t.db"), check_same_thread=False)
            cfg = make_cfg(os.path.join(d, "t.db"))
            journal_head = "ab" * 32
            logs = [{
                "address": VAULT, "blockNumber": "0x9", "transactionHash": "0x" + "9" * 64,
                "logIndex": "0x0",
                "topics": [TOPICS["epoch"], "0x" + word(3)],   # epoch is indexed
                "data": "0x" + journal_head,                    # journalHead is data word 0
            }]
            MockChainHandler.state = {"tip": 20, "logs": logs, "balances": {}}
            srv, port = _serve()
            try:
                cfg.rpc_url = f"http://127.0.0.1:{port}"
                idx = Indexer(cfg, db, Rpc(cfg.rpc_url))
                n = idx.poll_once()
                self.assertGreaterEqual(n, 1)
                self.assertEqual(idx._get("epoch"), "3")
                self.assertEqual(idx._get("journal_head"), "0x" + journal_head)
                # the head advanced, so the next poll is a no-op rather than a retry loop
                self.assertEqual(idx.poll_once(), 0)
                self.assertEqual(int(idx._get("undecodable_logs", 0) or 0), 0)
            finally:
                srv.shutdown()

    def test_trade_reason_hash_comes_from_event_data(self):
        """TradeExecuted indexes only tokenIn/tokenOut; reasonHash is data word 2."""
        with tempfile.TemporaryDirectory() as d:
            db = sqlite3.connect(os.path.join(d, "t.db"), check_same_thread=False)
            cfg = make_cfg(os.path.join(d, "t.db"))
            reason = "cd" * 32
            logs = [{
                "address": VAULT, "blockNumber": "0x5", "transactionHash": "0x" + "5" * 64,
                "logIndex": "0x0",
                "topics": [TOPICS["trade"],
                           "0x" + "00" * 12 + USDG[2:],
                           "0x" + "00" * 12 + NVDA[2:]],
                "data": "0x" + word(50 * 10**18) + word(7 * 10**18) + reason,
            }]
            MockChainHandler.state = {"tip": 20, "logs": logs, "balances": {}}
            srv, port = _serve()
            try:
                cfg.rpc_url = f"http://127.0.0.1:{port}"
                Indexer(cfg, db, Rpc(cfg.rpc_url)).poll_once()
                row = db.execute(
                    "SELECT token_in, token_out, amount_in, amount_out, reason_hash FROM trades"
                ).fetchone()
                self.assertEqual(row[0].lower(), USDG.lower())
                self.assertEqual(row[1].lower(), NVDA.lower())
                self.assertEqual(int(row[2]), 50 * 10**18)
                self.assertEqual(int(row[3]), 7 * 10**18)
                self.assertEqual(row[4], "0x" + reason)
            finally:
                srv.shutdown()

    def test_undecodable_log_is_counted_not_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            db = sqlite3.connect(os.path.join(d, "t.db"), check_same_thread=False)
            cfg = make_cfg(os.path.join(d, "t.db"))
            logs = [
                {"address": VAULT, "blockNumber": "0x5", "transactionHash": "0x" + "7" * 64,
                 "logIndex": "0x0", "topics": [TOPICS["epoch"]], "data": "0x"},  # missing topic[1]
                {"address": VAULT, "blockNumber": "0x6", "transactionHash": "0x" + "8" * 64,
                 "logIndex": "0x0", "topics": [TOPICS["burn"]], "data": "0x" + word(42)},
            ]
            MockChainHandler.state = {"tip": 20, "logs": logs, "balances": {}}
            srv, port = _serve()
            try:
                cfg.rpc_url = f"http://127.0.0.1:{port}"
                idx = Indexer(cfg, db, Rpc(cfg.rpc_url))
                idx.poll_once()
                # the good log was still indexed and the head advanced
                self.assertEqual(db.execute("SELECT COUNT(*) FROM burns").fetchone()[0], 1)
                self.assertEqual(int(idx._get("undecodable_logs")), 1)
                self.assertGreater(idx.head, 0)
            finally:
                srv.shutdown()


class TestApi(unittest.TestCase):
    def _app(self, d, logs=None, balances=None):
        db = sqlite3.connect(os.path.join(d, "t.db"), check_same_thread=False)
        cfg = make_cfg(os.path.join(d, "t.db"))
        MockChainHandler.state = {
            "tip": 50, "logs": logs or [],
            "balances": balances or {USDG: 1000 * 10**18, NVDA: 4 * 10**18, TOKEN: 2 * 10**6 * 10**18},
        }
        srv, port = _serve()
        cfg.rpc_url = f"http://127.0.0.1:{port}"
        indexer = Indexer(cfg, db, Rpc(cfg.rpc_url))
        indexer.poll_once()  # index the fabricated logs before serving
        return create_app(cfg, db, Rpc(cfg.rpc_url)), db, srv

    def test_endpoints(self):
        with tempfile.TemporaryDirectory() as d:
            app, _db, srv = self._app(d)
            try:
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
            finally:
                srv.shutdown()

    def test_sums_are_not_hex_zeroed(self):
        """Amounts used to be stored as hex and summed with CAST(... AS INTEGER),
        which SQLite resolves to 0 — so /verify and fee inflow always reported
        nothing."""
        with tempfile.TemporaryDirectory() as d:
            logs = [
                {"address": VAULT, "blockNumber": "0x5", "transactionHash": "0x" + "1" * 64,
                 "logIndex": "0x0", "topics": [TOPICS["fee"], "0x" + "00" * 12 + USDG[2:]],
                 "data": "0x" + word(1000 * 10**18)},
                {"address": VAULT, "blockNumber": "0x6", "transactionHash": "0x" + "2" * 64,
                 "logIndex": "0x0", "topics": [TOPICS["burn"]],
                 "data": "0x" + word(500 * 10**18)},
            ]
            app, _db, srv = self._app(d, logs=logs)
            try:
                client = TestClient(app)
                v = client.get("/verify").json()
                self.assertEqual(v["burn_total_raw"], 500 * 10**18)
                self.assertEqual(v["fee_total_raw"], 1000 * 10**18)
                self.assertEqual(v["event_counts"]["fees"], 1)

                vault = client.get("/vault").json()
                self.assertEqual(vault["fee_inflow_raw"][USDG], 1000 * 10**18)
            finally:
                srv.shutdown()

    def test_journal_publish_and_chain_verification(self):
        """/journal used to be permanently empty: nothing ever wrote journal_feed."""
        with tempfile.TemporaryDirectory() as d:
            app, _db, srv = self._app(d)
            try:
                client = TestClient(app)
                self.assertEqual(client.get("/journal").json(), [])

                def entry(payload, prev, kind="observation", eid="e1"):
                    e = {"id": eid, "ts": 1792159000.5, "kind": kind,
                         "payload": payload, "prev_hash": prev, "hash": ""}
                    e["hash"] = _entry_hash(e)
                    return e

                first = entry({"token": "nvda", "price": 100.0}, GENESIS)
                r = client.post("/journal", json=first)
                self.assertEqual(r.status_code, 201, r.text)

                # the published entry is now served and the chain re-hashes clean
                rows = client.get("/journal").json()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["id"], "e1")
                self.assertTrue(rows[0]["hash_ok"])
                v = client.get("/verify").json()
                self.assertTrue(v["journal_chain_ok"])
                self.assertEqual(v["journal_entries"], 1)

                # a forked entry (wrong prev_hash) is refused
                bad = entry({"token": "nvda"}, GENESIS, kind="proposal", eid="e2")
                r = client.post("/journal", json=bad)
                self.assertEqual(r.status_code, 409)

                # a tampered hash is refused
                second = entry({"token": "nvda", "price": 101.0}, first["hash"], kind="proposal", eid="e3")
                second["payload"] = {"token": "nvda", "price": 999.0}  # hash no longer matches
                r = client.post("/journal", json=second)
                self.assertEqual(r.status_code, 422)

                # the honest next entry links correctly and extends the chain
                third = entry({"token": "nvda", "price": 101.0}, first["hash"], kind="proposal", eid="e4")
                r = client.post("/journal", json=third)
                self.assertEqual(r.status_code, 201, r.text)
                self.assertTrue(client.get("/verify").json()["journal_chain_ok"])
                self.assertEqual(len(client.get("/journal").json()), 2)

                # duplicate ids are rejected rather than silently duplicated
                r = client.post("/journal", json=first)
                self.assertEqual(r.status_code, 409)
            finally:
                srv.shutdown()

    def test_deployments_badge_is_derived(self):
        with tempfile.TemporaryDirectory() as d:
            logs = [{
                "address": FACTORY, "blockNumber": "0xb", "transactionHash": "0x" + "3" * 64,
                "logIndex": "0x0",
                "topics": [TOPICS["launch"],
                           "0x" + "00" * 12 + TOKEN[2:],
                           "0x" + "00" * 12 + VAULT[2:],
                           "0x" + "00" * 12 + "ee" * 20],
                "data": "0x" + word(int(HOOK, 16)) + word(1500) + "4" * 64,
            }]
            app, _db, srv = self._app(d, logs=logs)
            try:
                rows = TestClient(app).get("/deployments").json()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["fee_share_bps"], 1500)
                self.assertEqual(rows[0]["hook"].lower(), HOOK.lower())
                self.assertTrue(rows[0]["verified"])
            finally:
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

    def test_known_address_vector(self):
        """privkey = 1 has a well-known mainnet address; this pins the whole
        derivation chain (secp256k1 -> keccak -> address)."""
        s = Signer("00" * 31 + "01", 1)
        self.assertEqual(s.address.lower(), "0x7e5f4552091a69125d5dfcb7b8c2659029395bdf")

    def test_sign_tx_type2_field_order(self):
        """EIP-1559 puts chain_id FIRST, requires an access list, and ends with
        y_parity, r, s. The old signer emitted [nonce, chain_id, ...] with 11
        fields and no access list, so no node would accept its transactions."""
        s = Signer(self.PRIV, 46630)
        tx = {
            "nonce": 7, "to": VAULT, "value": 0, "data": "0xdeadbeef",
            "priority_fee": 10**9, "max_fee": 10**10, "gas_limit": 21000,
        }
        raw = s.sign_tx(tx)
        self.assertTrue(raw.startswith("0x02"))
        fields = _rlp_decode_list(bytes.fromhex(raw[2:])[1:])
        self.assertEqual(len(fields), 12)
        self.assertEqual(int.from_bytes(fields[0], "big"), 46630, "field 0 is chain_id")
        self.assertEqual(int.from_bytes(fields[1], "big"), 7, "field 1 is nonce")
        self.assertEqual(fields[8], [], "access list is present and empty")

    def test_sign_tx_self_check_recovers_signer(self):
        s = Signer(self.PRIV, 46630)
        tx = {
            "nonce": 0, "to": VAULT, "value": 0, "data": "0x1234",
            "priority_fee": 10**9, "max_fee": 10**10, "gas_limit": 21000,
        }
        fields = _rlp_decode_list(bytes.fromhex(s.sign_tx(tx)[2:])[1:])
        r = int.from_bytes(fields[10], "big")
        sec = int.from_bytes(fields[11], "big")
        parity = int.from_bytes(fields[9], "big")
        recovered = secp256k1.recover_address(signing_hash({**tx, "chain_id": 46630}), r, sec, parity)
        self.assertEqual(recovered.lower(), s.address.lower())

    def test_rlp_known(self):
        # RLP("dog") = 0x83646f67 (textbook vector)
        self.assertEqual(rlp_encode(b"dog"), bytes.fromhex("83646f67"))
        # RLP([]) = 0xc0
        self.assertEqual(rlp_encode([]), bytes.fromhex("c0"))
        # RLP(1024) = 0x820400
        self.assertEqual(rlp_encode(1024), bytes.fromhex("820400"))


def _rlp_decode_list(data: bytes) -> list:
    """Decode a top-level RLP list into its raw items (test helper)."""
    def dec(b, i):
        pre = b[i]
        if pre < 0x80:
            return b[i:i + 1], i + 1
        if pre < 0xB8:
            ln = pre - 0x80
            return b[i + 1:i + 1 + ln], i + 1 + ln
        if pre < 0xC0:
            ll = pre - 0xB7
            ln = int.from_bytes(b[i + 1:i + 1 + ll], "big")
            return b[i + 1 + ll:i + 1 + ll + ln], i + 1 + ll + ln
        if pre < 0xF8:
            ln = pre - 0xC0
            end, out, j = i + 1 + ln, [], i + 1
            while j < end:
                item, j = dec(b, j)
                out.append(item)
            return out, end
        ll = pre - 0xF7
        ln = int.from_bytes(b[i + 1:i + 1 + ll], "big")
        end, out, j = i + 1 + ll + ln, [], i + 1 + ll
        while j < end:
            item, j = dec(b, j)
            out.append(item)
        return out, end

    items, _ = dec(data, 0)
    return items


if __name__ == "__main__":
    unittest.main()

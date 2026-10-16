"""Mock Robinhood Chain for local backend E2E: serves logs + balances on
127.0.0.1:18560.

Run from anywhere: the repository root is derived from this file's location so
the mock works on any machine (it previously hardcoded an author's home dir).
"""
import json
import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402
from aruven_backend.indexer import TOPICS  # noqa: E402

VAULT = "0x000000000000000000000000000000000000dead"
FACTORY = "0x000000000000000000000000000000000000cafe"
USDG = "0x0000000000000000000000000000000000000001"
NVDA = "0x0000000000000000000000000000000000000002"
TOKEN = "0x000000000000000000000000000000000000beef"
HOOK = "0x00000000000000000000000000000000000000ff"
BAL = {USDG: 1000 * 10**18, NVDA: 4 * 10**18, TOKEN: 2_000_000 * 10**18}


def _word(v: int) -> str:
    return hex(v)[2:].rjust(64, "0")


logs = [
    {"address": VAULT, "blockNumber": "0x5", "transactionHash": "0x" + "a" * 64, "logIndex": "0x0",
     "topics": [TOPICS["fee"], "0x" + "00" * 12 + USDG[2:]],
     "data": "0x" + _word(1000 * 10**18)},
    {"address": VAULT, "blockNumber": "0x6", "transactionHash": "0x" + "b" * 64, "logIndex": "0x0",
     "topics": [TOPICS["burn"]], "data": "0x" + _word(500 * 10**18)},
    {"address": VAULT, "blockNumber": "0x7", "transactionHash": "0x" + "c" * 64, "logIndex": "0x0",
     "topics": [TOPICS["trade"], "0x" + "00" * 12 + USDG[2:], "0x" + "00" * 12 + NVDA[2:]],
     # amountIn, amountOut, reasonHash (TradeExecuted indexes only the two tokens)
     "data": "0x" + _word(50 * 10**18) + _word(0) + "cd" * 32},
    {"address": VAULT, "blockNumber": "0x8", "transactionHash": "0x" + "e" * 64, "logIndex": "0x0",
     "topics": [TOPICS["epoch"], "0x" + _word(1)],
     "data": "0x" + "ab" * 32},  # journalHead lives in data, not topics
    {"address": FACTORY, "blockNumber": "0x9", "transactionHash": "0x" + "d" * 64, "logIndex": "0x0",
     "topics": [TOPICS["launch"], "0x" + "00" * 12 + TOKEN[2:], "0x" + "00" * 12 + VAULT[2:],
                "0x" + "00" * 12 + "ee" * 20],
     # hook, feeShareBps, metaHash
     "data": "0x" + _word(int(HOOK, 16)) + _word(1500) + "4" * 64},
]


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        method, params = body["method"], body.get("params", [])
        result = "0x"
        if method == "eth_blockNumber":
            result = hex(20)
        elif method == "eth_getLogs":
            f, t = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
            first = params[0]["topics"][0]
            wanted = set(first) if isinstance(first, list) else {first}
            result = [l for l in logs if f <= int(l["blockNumber"], 16) <= t and l["topics"][0] in wanted]
        elif method == "eth_call":
            to = params[0]["to"].lower()
            if to in BAL:
                result = hex(BAL[to])
        raw = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    srv = HTTPServer(("127.0.0.1", 18560), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("mock chain up on 18560", flush=True)
    while True:
        time.sleep(1)

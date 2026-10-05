"""Event indexer: poll the chain, maintain SQLite state.

Idempotent by design: re-running from block 0 re-derives identical state.
Every number the API serves comes from these tables or direct RPC reads —
never fabricated.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

from .config import BackendConfig

# keccak topic0 hashes of the events we index
def _topic(sig: str) -> str:
    from Crypto.Hash import keccak
    h = keccak.new(digest_bits=256)
    h.update(sig.encode())
    return "0x" + h.hexdigest()

TOPICS = {
    "trade": _topic("TradeExecuted(address,address,uint256,uint256,bytes32)"),
    "fee": _topic("FeeReceived(address,uint256)"),
    "burn": _topic("VaultBurn(uint256)"),
    "launch": _topic("Launch(address,address,address,uint256,bytes32)"),
    "epoch": _topic("EpochCheckpoint(uint256,bytes32)"),
    "risk": _topic("RiskParamsUpdated(uint256,uint256,uint256)"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS chain_state (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    block INTEGER, tx_hash TEXT, log_index INTEGER,
    token_in TEXT, token_out TEXT, amount_in TEXT, amount_out TEXT, reason_hash TEXT,
    PRIMARY KEY (tx_hash, log_index)
);
CREATE TABLE IF NOT EXISTS fees (
    block INTEGER, tx_hash TEXT, log_index INTEGER,
    token TEXT, amount TEXT,
    PRIMARY KEY (tx_hash, log_index)
);
CREATE TABLE IF NOT EXISTS burns (
    block INTEGER, tx_hash TEXT, log_index INTEGER, amount TEXT,
    PRIMARY KEY (tx_hash, log_index)
);
CREATE TABLE IF NOT EXISTS launches (
    block INTEGER, tx_hash TEXT, log_index INTEGER,
    token TEXT, vault TEXT, creator TEXT, fee_share_bps TEXT, meta_hash TEXT,
    PRIMARY KEY (tx_hash, log_index)
);
CREATE TABLE IF NOT EXISTS journal_feed (
    id TEXT PRIMARY KEY, ts REAL, kind TEXT, payload TEXT, prev_hash TEXT, hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_block ON trades(block);
CREATE INDEX IF NOT EXISTS idx_fees_block ON fees(block);
"""


class Rpc:
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url

    def call(self, method: str, params: list):
        import urllib.request
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode()
        req = urllib.request.Request(
            self.rpc_url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
        if "error" in body:
            raise RuntimeError(f"rpc error: {body['error']}")
        return body["result"]

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def logs(self, address: str, topics: list[str], from_block: int, to_block: int) -> list[dict]:
        return self.call("eth_getLogs", [{
            "address": address,
            "topics": [topics],
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
        }]) or []

    def balance_of(self, token: str, holder: str) -> int:
        data = "0x70a08231" + "00" * 12 + holder[2:].lower()
        return int(self.call("eth_call", [{"to": token, "data": data}, "latest"]), 16)


class Indexer:
    def __init__(self, cfg: BackendConfig, db: sqlite3.Connection, rpc: Rpc):
        self.cfg = cfg
        self.db = db
        self.rpc = rpc
        db.executescript(SCHEMA)
        db.commit()

    # -- state helpers ------------------------------------------------
    def _get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM chain_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def _set(self, key: str, value) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO chain_state (key, value) VALUES (?, ?)",
            (key, str(value)),
        )

    @property
    def head(self) -> int:
        return int(self._get("head", self.cfg.start_block) or 0)

    # -- decoding ------------------------------------------------------
    @staticmethod
    def _addr(topic: str) -> str:
        return "0x" + topic[-40:]

    @staticmethod
    def _uint(data: str) -> int:
        return int(data, 16)

    # -- main -----------------------------------------------------------
    def poll_once(self) -> int:
        """Index new logs up to head - confirmations. Returns blocks indexed."""
        tip = self.rpc.block_number()
        target = max(0, tip - self.cfg.confirmations)
        start = self.head + 1
        if start > target:
            return 0
        # batch window to avoid huge ranges
        end = min(target, start + 5_000 - 1)
        vault_topics = [t for k, t in TOPICS.items() if k in ("trade", "fee", "burn", "epoch", "risk")]
        self._index_logs(self.cfg.vault_address, vault_topics, start, end)
        if self.cfg.factory_address not in ("", "0x" + "0" * 40):
            self._index_logs(self.cfg.factory_address, [TOPICS["launch"]], start, end)
        self._set("head", end)
        self.db.commit()
        return end - start + 1

    def _index_logs(self, address: str, topics: list[str], start: int, end: int) -> None:
        for log in self.rpc.logs(address, topics, start, end):
            self._ingest(log)

    def _ingest(self, log: dict) -> None:
        block = int(log["blockNumber"], 16)
        tx = log["transactionHash"]
        idx = int(log["logIndex"], 16)
        topic0 = log["topics"][0]
        data = log.get("data", "0x")

        if topic0 == TOPICS["trade"]:
            topics = log["topics"]
            self.db.execute(
                "INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?,?)",
                (block, tx, idx, self._addr(topics[1]), self._addr(topics[2]),
                 hex(self._uint(data[2:66])), hex(self._uint(data[66:130])), topics[3] if len(topics) > 3 else ""),
            )
        elif topic0 == TOPICS["fee"]:
            topics = log["topics"]
            self.db.execute(
                "INSERT OR IGNORE INTO fees VALUES (?,?,?,?,?)",
                (block, tx, idx, self._addr(topics[1]), hex(self._uint(data[2:66]))),
            )
        elif topic0 == TOPICS["burn"]:
            self.db.execute(
                "INSERT OR IGNORE INTO burns VALUES (?,?,?,?)",
                (block, tx, idx, hex(self._uint(data[2:66]))),
            )
        elif topic0 == TOPICS["launch"]:
            topics = log["topics"]
            body = data[2:]
            self.db.execute(
                "INSERT OR IGNORE INTO launches VALUES (?,?,?,?,?,?,?,?)",
                (block, tx, idx, self._addr(topics[1]), self._addr(topics[2]),
                 self._addr(topics[3]),
                 hex(self._uint(body[0:64])),
                 "0x" + body[64:128]),
            )
        elif topic0 == TOPICS["epoch"]:
            self._set("epoch", int(log["topics"][1], 16))
            self._set("journal_head", log["topics"][2])
        # RiskParamsUpdated and others: stored implicitly; params are read live via RPC.

    def run_forever(self) -> None:
        while True:
            try:
                n = self.poll_once()
                if n == 0:
                    time.sleep(self.cfg.poll_interval_sec)
            except Exception:
                time.sleep(max(5, self.cfg.poll_interval_sec))

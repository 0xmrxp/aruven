"""Event indexer: poll the chain, maintain SQLite state.

Idempotent by design: re-running from block 0 re-derives identical state.
Every number the API serves comes from these tables or direct RPC reads —
never fabricated.
"""
from __future__ import annotations

import json
import sqlite3
import time

from aruven_common.eip1559 import keccak256

from .config import BackendConfig


def _topic(sig: str) -> str:
    return "0x" + keccak256(sig.encode()).hex()


TOPICS = {
    "trade": _topic("TradeExecuted(address,address,uint256,uint256,bytes32)"),
    "fee": _topic("FeeReceived(address,uint256)"),
    "burn": _topic("VaultBurn(uint256)"),
    "launch": _topic("Launch(address,address,address,uint256,bytes32)"),
    "epoch": _topic("EpochCheckpoint(uint256,bytes32)"),
    "risk": _topic("RiskParamsUpdated(uint256,uint256,uint256)"),
}

# Amounts are stored as DECIMAL STRINGS. They used to be stored as hex, and the
# API summed them with `CAST(amount AS INTEGER)`, which SQLite resolves to 0 for
# anything starting with "0x" — so /verify and fee inflow always reported zero.
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
    token TEXT, vault TEXT, creator TEXT, hook TEXT, fee_share_bps TEXT, meta_hash TEXT,
    PRIMARY KEY (tx_hash, log_index)
);
CREATE TABLE IF NOT EXISTS journal_feed (
    id TEXT PRIMARY KEY,
    ts TEXT,            -- exact JSON number text, so the hash re-derives bit-for-bit
    kind TEXT,
    payload TEXT,
    prev_hash TEXT,
    hash TEXT,
    hash_ok INTEGER DEFAULT 1,
    received_at REAL
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
        data = "0x70a08231" + "00" * 12 + holder.removeprefix("0x").lower()
        return int(self.call("eth_call", [{"to": token, "data": data}, "latest"]), 16)


def _word(data: str, index: int) -> int:
    """Non-indexed event argument `index` (0-based) from the data blob."""
    start = 2 + index * 64
    return int(data[start:start + 64], 16)


def _word_hex(data: str, index: int) -> str:
    start = 2 + index * 64
    return "0x" + data[start:start + 64]


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

    # -- main -----------------------------------------------------------
    def poll_once(self) -> int:
        """Index new logs up to head - confirmations. Returns blocks indexed.

        The head is advanced only after every log in the window is ingested. A
        decode failure used to propagate out of here before `_set("head")`, so
        the same block range was retried forever and the indexer never moved
        past the first event it could not parse.
        """
        tip = self.rpc.block_number()
        target = max(0, tip - self.cfg.confirmations)
        start = self.head + 1
        if start > target:
            return 0
        end = min(target, start + 5_000 - 1)
        vault_topics = [t for k, t in TOPICS.items() if k in ("trade", "fee", "burn", "epoch", "risk")]
        for log in self.rpc.logs(self.cfg.vault_address, vault_topics, start, end):
            self._safe_ingest(log)
        if self.cfg.factory_address not in ("", "0x" + "0" * 40):
            for log in self.rpc.logs(self.cfg.factory_address, [TOPICS["launch"]], start, end):
                self._safe_ingest(log)
        self._set("head", end)
        self.db.commit()
        return end - start + 1

    def _index_logs(self, address: str, topics: list[str], start: int, end: int) -> None:
        for log in self.rpc.logs(address, topics, start, end):
            self._safe_ingest(log)

    def _safe_ingest(self, log: dict) -> None:
        """Ingest one log; a malformed or unknown-shape event is skipped with a
        recorded counter rather than stalling the whole range."""
        try:
            self._ingest(log)
        except Exception:  # noqa: BLE001 — one bad log must not freeze the indexer
            bad = int(self._get("undecodable_logs", 0) or 0) + 1
            self._set("undecodable_logs", bad)
            self._set("last_undecodable_tx", str(log.get("transactionHash", "?")))

    def _ingest(self, log: dict) -> None:
        block = int(log["blockNumber"], 16)
        tx = log["transactionHash"]
        idx = int(log["logIndex"], 16)
        topics = log.get("topics", [])
        if not topics:
            raise ValueError("log without topics")
        topic0 = topics[0]
        data = log.get("data", "0x")

        if topic0 == TOPICS["trade"]:
            # TradeExecuted(address indexed tokenIn, address indexed tokenOut,
            #               uint256 amountIn, uint256 amountOut, bytes32 reasonHash)
            # Only tokenIn/tokenOut are indexed, so the three value arguments
            # live in `data` — reasonHash is data word 2, not topics[3].
            self.db.execute(
                "INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?,?)",
                (block, tx, idx, self._addr(topics[1]), self._addr(topics[2]),
                 str(_word(data, 0)), str(_word(data, 1)), _word_hex(data, 2)),
            )
        elif topic0 == TOPICS["fee"]:
            # FeeReceived(address indexed token, uint256 amount)
            self.db.execute(
                "INSERT OR IGNORE INTO fees VALUES (?,?,?,?,?)",
                (block, tx, idx, self._addr(topics[1]), str(_word(data, 0))),
            )
        elif topic0 == TOPICS["burn"]:
            # VaultBurn(uint256 amount)
            self.db.execute(
                "INSERT OR IGNORE INTO burns VALUES (?,?,?,?)",
                (block, tx, idx, str(_word(data, 0))),
            )
        elif topic0 == TOPICS["launch"]:
            # Launch(address indexed token, address indexed vault,
            #        address indexed creator, address hook, uint256 feeShareBps,
            #        bytes32 metaHash)
            # Only token/vault/creator are indexed; hook, feeShareBps and
            # metaHash are data words 0, 1 and 2.
            self.db.execute(
                "INSERT OR IGNORE INTO launches VALUES (?,?,?,?,?,?,?,?,?)",
                (block, tx, idx, self._addr(topics[1]), self._addr(topics[2]),
                 self._addr(topics[3]), self._addr(_word_hex(data, 0)),
                 str(_word(data, 1)), _word_hex(data, 2)),
            )
        elif topic0 == TOPICS["epoch"]:
            # EpochCheckpoint(uint256 indexed epoch, bytes32 journalHead)
            # `epoch` is indexed; `journalHead` is NOT — it is data word 0.
            # Reading topics[2] raised IndexError on every checkpoint.
            if len(topics) < 2:
                raise ValueError("EpochCheckpoint without indexed epoch")
            self._set("epoch", int(topics[1], 16))
            self._set("journal_head", _word_hex(data, 0))
        # RiskParamsUpdated and others: stored implicitly; params are read live via RPC.

    def run_forever(self) -> None:
        while True:
            try:
                n = self.poll_once()
                if n == 0:
                    time.sleep(self.cfg.poll_interval_sec)
            except Exception:  # noqa: BLE001 — transient RPC failure: back off and retry
                time.sleep(max(5, self.cfg.poll_interval_sec))

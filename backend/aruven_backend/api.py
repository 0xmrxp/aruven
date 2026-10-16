"""REST API: the public read surface. Numbers come from the indexer DB or
direct RPC reads — nothing is fabricated."""
from __future__ import annotations

import json
import sqlite3

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .config import BackendConfig
from .indexer import Rpc

GENESIS_HASH = "0x" + "0" * 64


class JournalIn(BaseModel):
    """A sealed journal entry published by the agent."""

    id: str = Field(min_length=1, max_length=128)
    ts: float
    kind: str = Field(min_length=1, max_length=32)
    payload: dict = Field(default_factory=dict)
    prev_hash: str
    hash: str


def _sum_amounts(db: sqlite3.Connection, table: str) -> int:
    """Sum a decimal-string amount column in Python.

    `SUM(CAST(amount AS INTEGER))` looks right but SQLite parses a leading "0x"
    as 0, so every hex amount summed to zero and /verify always reported none.
    """
    total = 0
    for (raw,) in db.execute(f"SELECT amount FROM {table}"):
        try:
            total += int(raw)
        except (TypeError, ValueError):
            continue
    return total


def _entry_hash(entry: dict) -> str:
    """Same construction the agent's JournalEntry uses, so a published entry can
    be re-verified here without trusting the publisher."""
    import hashlib

    blob = json.dumps(
        {
            "id": entry["id"],
            "ts": float(entry["ts"]),
            "kind": entry["kind"],
            "payload": entry["payload"],
            "prev": entry["prev_hash"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "0x" + hashlib.sha256(blob.encode()).hexdigest()


def create_app(cfg: BackendConfig, db: sqlite3.Connection, rpc: Rpc) -> FastAPI:
    app = FastAPI(title="ARUVEN", version="0.1.0", docs_url="/docs")

    def _state(key: str):
        row = db.execute("SELECT value FROM chain_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _holdings() -> dict:
        out = {}
        for name, addr in cfg.strategy_tokens.items():
            out[name] = rpc.balance_of(addr, cfg.vault_address)
        out["aruven"] = rpc.balance_of(cfg.aruven_token, cfg.vault_address)
        return out

    def _journal_head() -> str:
        row = db.execute(
            "SELECT hash FROM journal_feed ORDER BY received_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else GENESIS_HASH

    @app.get("/health")
    def health():
        return {"ok": True, "head_block": int(_state("head") or 0), "chain_tip": rpc.block_number()}

    @app.get("/vault")
    def vault():
        holdings = _holdings()
        nav = sum(
            holdings.get(name, 0) / 10**18 * price
            for name, price in cfg.prices.items()
        ) + holdings.get("aruven", 0) / 10**18 * 0  # aruven not counted in NAV
        fee_rows = db.execute("SELECT token, amount FROM fees").fetchall()
        fee_in: dict[str, int] = {}
        for token, raw in fee_rows:
            try:
                fee_in[token] = fee_in.get(token, 0) + int(raw)
            except (TypeError, ValueError):
                continue
        return {
            "vault_address": cfg.vault_address,
            "holdings_raw": holdings,
            "nav_usd": nav,
            "price_sources": cfg.prices,
            "fee_inflow_raw": fee_in,
            "epoch": int(_state("epoch") or 0),
            "journal_head": _state("journal_head"),
            "note": "nav uses configured prices; pool-spot NAV lands with the pool reader",
        }

    @app.get("/trades")
    def trades(limit: int = Query(100, le=1000)):
        rows = db.execute(
            "SELECT block, tx_hash, token_in, token_out, amount_in, amount_out, reason_hash "
            "FROM trades ORDER BY block DESC, log_index DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {"block": r[0], "tx_hash": r[1], "token_in": r[2], "token_out": r[3],
             "amount_in": r[4], "amount_out": r[5], "reason_hash": r[6]}
            for r in rows
        ]

    @app.get("/journal")
    def journal(limit: int = Query(100, le=1000)):
        rows = db.execute(
            "SELECT id, ts, kind, payload, prev_hash, hash, hash_ok FROM journal_feed "
            "ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {"id": r[0], "ts": float(r[1]), "kind": r[2], "payload": json.loads(r[3]),
             "prev_hash": r[4], "hash": r[5], "hash_ok": bool(r[6])}
            for r in rows
        ]

    @app.post("/journal", status_code=201)
    def publish_journal(entry: JournalIn):
        """Accept a sealed entry from the agent.

        The chain linkage is enforced, not trusted: an entry whose prev_hash is
        not the current head is rejected with 409, so the published feed cannot
        be forked or reordered by whoever holds the publish URL.
        """
        head = _journal_head()
        if entry.prev_hash != head:
            raise HTTPException(
                status_code=409,
                detail=f"prev_hash {entry.prev_hash} does not match journal head {head}",
            )
        recomputed = _entry_hash(entry.model_dump())
        if recomputed != entry.hash:
            raise HTTPException(
                status_code=422,
                detail="entry hash does not match its contents",
            )
        try:
            db.execute(
                "INSERT INTO journal_feed (id, ts, kind, payload, prev_hash, hash, hash_ok, received_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (entry.id, repr(entry.ts), entry.kind,
                 json.dumps(entry.payload, sort_keys=True, separators=(",", ":")),
                 entry.prev_hash, entry.hash, 1, __import__("time").time()),
            )
            db.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail=f"entry {entry.id} already published")
        return {"ok": True, "id": entry.id, "head": entry.hash}

    @app.get("/proposals")
    def proposals():
        # Governor state reads (Phase 4 wiring: proposalCount + states via RPC)
        return {"proposals": [], "note": "governor integration lands at launch"}

    @app.get("/deployments")
    def deployments():
        rows = db.execute(
            "SELECT block, tx_hash, token, vault, creator, hook, fee_share_bps, meta_hash "
            "FROM launches ORDER BY block DESC"
        ).fetchall()
        out = []
        for r in rows:
            try:
                share = int(r[6])
            except (TypeError, ValueError):
                share = None
            out.append({
                "block": r[0], "tx_hash": r[1], "token": r[2], "vault": r[3],
                "creator": r[4], "hook": r[5], "fee_share_bps": share, "meta_hash": r[7],
                # Only factory launches are indexed, and the factory refuses a
                # share below minShareBps, so a non-zero share is what the badge
                # means. It is derived from the event, not decorative.
                "verified": share is not None and share > 0,
            })
        return out

    @app.get("/verify")
    def verify():
        """Re-derive headline numbers from raw indexed events (trust endpoint)."""
        trades = db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        fees = db.execute("SELECT COUNT(*) FROM fees").fetchone()[0]
        burns = db.execute("SELECT COUNT(*) FROM burns").fetchone()[0]
        launches = db.execute("SELECT COUNT(*) FROM launches").fetchone()[0]
        journal_rows = db.execute(
            "SELECT id, ts, kind, payload, prev_hash, hash FROM journal_feed ORDER BY ts ASC"
        ).fetchall()
        prev = GENESIS_HASH
        journal_ok = True
        for r in journal_rows:
            entry = {"id": r[0], "ts": float(r[1]), "kind": r[2],
                     "payload": json.loads(r[3]), "prev_hash": r[4], "hash": r[5]}
            if entry["prev_hash"] != prev or _entry_hash(entry) != entry["hash"]:
                journal_ok = False
                break
            prev = entry["hash"]
        return {
            "event_counts": {"trades": trades, "fees": fees, "burns": burns, "launches": launches},
            "burn_total_raw": _sum_amounts(db, "burns"),
            "fee_total_raw": _sum_amounts(db, "fees"),
            "journal_entries": len(journal_rows),
            "journal_chain_ok": journal_ok,
            "undecodable_logs": int(_state("undecodable_logs") or 0),
            "head_block": int(_state("head") or 0),
            "method": "counts and sums derived directly from indexed raw logs; "
                      "holdings are live balanceOf reads; the journal chain is re-hashed here",
        }

    return app

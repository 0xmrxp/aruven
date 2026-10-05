"""REST API: the public read surface. Numbers come from the indexer DB or
direct RPC reads — nothing is fabricated."""
from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import FastAPI, HTTPException, Query

from .config import BackendConfig
from .indexer import Rpc, Indexer


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
        fee_in = db.execute(
            "SELECT token, SUM(CAST(amount AS INTEGER)) FROM fees GROUP BY token"
        ).fetchall()
        return {
            "vault_address": cfg.vault_address,
            "holdings_raw": holdings,
            "nav_usd": nav,
            "price_sources": cfg.prices,
            "fee_inflow_raw": {t: int(a) for t, a in fee_in},
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
            "SELECT id, ts, kind, payload, prev_hash, hash FROM journal_feed "
            "ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        import json as _json
        return [
            {"id": r[0], "ts": r[1], "kind": r[2], "payload": _json.loads(r[3]),
             "prev_hash": r[4], "hash": r[5]}
            for r in rows
        ]

    @app.get("/proposals")
    def proposals():
        # Governor state reads (Phase 4 wiring: proposalCount + states via RPC)
        return {"proposals": [], "note": "governor integration lands at launch"}

    @app.get("/deployments")
    def deployments():
        rows = db.execute(
            "SELECT block, tx_hash, token, vault, creator, fee_share_bps, meta_hash "
            "FROM launches ORDER BY block DESC"
        ).fetchall()
        return [
            {"block": r[0], "tx_hash": r[1], "token": r[2], "vault": r[3],
             "creator": r[4], "fee_share_bps": int(r[5], 16), "meta_hash": r[6]}
            for r in rows
        ]

    @app.get("/verify")
    def verify():
        """Re-derive headline numbers from raw indexed events (trust endpoint)."""
        trades = db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        fees = db.execute("SELECT COUNT(*) FROM fees").fetchone()[0]
        burns = db.execute("SELECT COUNT(*) FROM burns").fetchone()[0]
        launches = db.execute("SELECT COUNT(*) FROM launches").fetchone()[0]
        burn_total = db.execute("SELECT SUM(CAST(amount AS INTEGER)) FROM burns").fetchone()[0] or 0
        return {
            "event_counts": {"trades": trades, "fees": fees, "burns": burns, "launches": launches},
            "burn_total_raw": burn_total,
            "head_block": int(_state("head") or 0),
            "method": "counts and sums derived directly from indexed raw logs; "
                      "holdings are live balanceOf reads",
        }

    return app

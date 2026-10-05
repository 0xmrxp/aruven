"""Typed pipeline records: every stage's input/output is explicit."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Observation:
    """Stage 1 — screen: raw market/vault state."""
    ts: float
    token: str
    price: float
    rsi: float
    volume_24h: float
    vault_balance: int          # raw units of the token in the vault
    usdg_balance: int           # raw units of USDG in the vault
    epoch: int
    epoch_anchor_nav: int

    @property
    def vault_balance_float(self) -> float:
        return self.vault_balance / 1e18


@dataclass
class Score:
    """Stage 2 — score: derived trade-worthiness metrics."""
    token: str
    edge_bps: float             # expected edge in basis points
    zscore: float
    volatility: float
    rsi: float
    rank: int = 0


@dataclass
class Proposal:
    """Stage 3 — strategy: a concrete intended trade."""
    token_in: str               # strategy token name
    token_out: str
    amount_in: int              # raw units
    expected_out: int           # raw units (quoted)
    reason: str
    score: Score
    slippage_bps: int = 50


@dataclass
class LLMReview:
    """Stage 4 — LLM sanity check (optional)."""
    approved: bool
    confidence: float
    comment: str
    model: str = ""


@dataclass
class Execution:
    """Stage 5 — result of calling the vault."""
    tx_hash: str
    amount_out: int
    gas_used: int
    success: bool = True
    error: str = ""


@dataclass
class JournalEntry:
    """Stage 6 — the immutable public record."""
    id: str
    ts: float
    kind: str                   # observation|proposal|review|execution|halt
    payload: dict = field(default_factory=dict)
    prev_hash: str = ""
    hash: str = ""

    def compute_hash(self) -> str:
        import hashlib
        import json
        blob = json.dumps(
            {"id": self.id, "ts": self.ts, "kind": self.kind,
             "payload": self.payload, "prev": self.prev_hash},
            sort_keys=True, separators=(",", ":"),
        )
        return "0x" + hashlib.sha256(blob.encode()).hexdigest()

    def seal(self) -> None:
        self.hash = self.compute_hash()

    def to_line(self) -> str:
        import json
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def new(kind: str, payload: dict, prev_hash: str) -> "JournalEntry":
        e = JournalEntry(
            id=uuid.uuid4().hex,
            ts=time.time(),
            kind=kind,
            payload=payload,
            prev_hash=prev_hash,
        )
        e.seal()
        return e

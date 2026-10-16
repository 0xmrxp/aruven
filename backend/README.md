# aruven-backend

RPC indexer + REST API: the single source of truth the frontend (and any third
party) reads. Every number it serves can be re-derived from raw chain logs via
`/verify`.

## Components

- `aruven_backend/indexer.py` — polls the chain for vault events (`TradeExecuted`, `FeeReceived`, `VaultBurn`, `Launch`, factory deployments), maintains SQLite state.
- `aruven_backend/api.py` — FastAPI app exposing:
  - `GET /vault` — holdings, NAV inputs, fee inflow, epoch data
  - `GET /journal?limit=` — agent journal entries (see `POST /journal`)
  - `POST /journal` — intake for sealed agent entries; enforces the hash chain
    (409 on a stale `prev_hash`, 422 on a hash that does not match its payload)
  - `GET /proposals` — governance proposals
  - `GET /deployments` — third-party launches via the factory, with a `verified`
    flag derived from the indexed fee share
  - `GET /verify` — re-derive served numbers from raw logs, including re-hashing
    the journal chain (the trust endpoint)
  - `GET /health` — liveness + head block
- `aruven_backend/signer.py` — EIP-1559 transaction signing for the agent executor
  (the ONLY place the executor key is used). Re-exported from `aruven_common.eip1559`
  so the agent and the backend sign with byte-identical code.

## Run

```
pip install -r requirements.txt
python -m aruven_backend.main --config config.example.yaml   # indexer + API
```

## Design notes

- SQLite by default (single-node, zero ops); Postgres-ready schema.
- Indexer is idempotent: re-running from block 0 re-derives identical state.
- A log the indexer cannot decode is counted (`undecodable_logs`) and skipped;
  one malformed event never freezes the block range.
- Amounts are stored as decimal strings and summed in Python — summing hex text
  with `CAST(... AS INTEGER)` silently yields zero.
- The API never fabricates numbers: if a value cannot be derived from events,
  it is absent from the response, not zero-filled.

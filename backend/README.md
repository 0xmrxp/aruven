# aruven-backend

RPC indexer + REST API: the single source of truth the frontend (and any third
party) reads. Every number it serves can be re-derived from raw chain logs via
`/verify`.

## Components

- `aruven_backend/indexer.py` — polls the chain for vault events (`TradeExecuted`, `FeeReceived`, `VaultBurn`, `Launch`, factory deployments), maintains SQLite state.
- `aruven_backend/api.py` — FastAPI app exposing:
  - `GET /vault` — holdings, NAV inputs, fee inflow, epoch data
  - `GET /journal?limit=` — agent journal entries
  - `GET /proposals` — governance proposals
  - `GET /deployments` — third-party launches via the factory
  - `GET /verify` — re-derive served numbers from raw logs (the trust endpoint)
  - `GET /health` — liveness + head block
- `aruven_backend/signer.py` — EIP-1559 transaction signing for the agent executor (the ONLY place the executor key is used).

## Run

```
pip install -r requirements.txt
python -m aruven_backend.main --config config.example.yaml   # indexer + API
```

## Design notes

- SQLite by default (single-node, zero ops); Postgres-ready schema.
- Indexer is idempotent: re-running from block 0 re-derives identical state.
- The API never fabricates numbers: if a value cannot be derived from events,
  it is absent from the response, not zero-filled.

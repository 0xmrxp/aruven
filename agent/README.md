# aruven-agent

The autonomous trader (Phase 2): executes vault-bounded swaps on Uniswap v4 pools on Robinhood Chain, with a public decision journal.

## Pipeline

```
screen → score → strategy → risk gate → LLM review → execute → journal
```

Every stage is a pure module with typed inputs/outputs. The executor is the only
component with a private key, and it can ONLY call `AruvenVault.executeSwap` —
the vault's own contract limits bound what it can do even then.

## Run

```
pip install -r requirements.txt
python -m aruven_agent.main --config config.example.yaml --dry-run   # paper mode
python -m aruven_agent.main --config config.yaml                     # live
```

`price_feed.url` and `allowed_routers` must be set before the agent will start:
it refuses to trade on synthetic prices, and it refuses to build a call the
vault would reject anyway. Run from the repository root (or from `agent/`) so the
shared `aruven_common` package is importable.

## Safety model

- **Vault-bounded by contract**: strategy-token whitelist, **router allow-list**,
  **non-zero output floor**, 5% per trade, 2% daily loss cap, 1h cooldown —
  enforced on-chain, the agent cannot bypass them.
- **Soft risk gates in the agent** duplicate the vault's hard limits (defense in depth).
- **Journal**: every decision is hash-chained; the head hash is checkpointed on-chain each epoch via `AruvenVault.rollEpoch`, and sealed entries are published to the backend's `POST /journal`.
- **Kill switch**: agent halts itself on: contract reverts (CooldownActive, TradeTooLarge, DailyLossCapHit, NotAgent), RPC errors, or journal write failures.

## What the bounds do and do not stop

A fully compromised executor can trade badly at a bounded rate — that is the
residual risk. It cannot point the vault at an arbitrary contract, cannot accept
an arbitrary output amount, and cannot move funds outside the whitelisted token
set. Each bound has a test asserting the exploit fails.

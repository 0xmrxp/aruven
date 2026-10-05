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
python -m aruven_agent.main --config config.example.yaml --dry-run   # paper mode
python -m aruven_agent.main --config config.yaml                     # live
```

## Safety model

- **Vault-bounded by contract**: 5% per trade, 2% daily loss cap, 1h cooldown, token whitelist — enforced on-chain, the agent cannot bypass them.
- **Soft risk gates in the agent** duplicate the vault's hard limits (defense in depth).
- **Journal**: every decision is hash-chained; the head hash is checkpointed on-chain each epoch via `AruvenVault.rollEpoch`.
- **Kill switch**: agent halts itself on: contract reverts (CooldownActive, TradeTooLarge, DailyLossCapHit), RPC errors, or journal write failures.

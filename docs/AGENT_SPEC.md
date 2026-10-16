# Agent Spec

The autonomous trader: `screen → score → strategy → risk gate → LLM review →
execute → journal`, built for Uniswap v4 pools on Robinhood Chain with
vault-bounded execution. See ARCHITECTURE.md §3 for the system view; this file
is the implementation contract.

## Stages

| Stage | Module | In → Out | Notes |
|---|---|---|---|
| 1 Screen | `screen.py` (`PriceFeed`, `ChainClient`, `Screener`) | token → `Observation` | Pulls a price series from `price_feed.url` and reads vault state over RPC. **No built-in stub**: an unconfigured feed halts the loop. |
| 2 Score | `strategy.py` (`Scorer`) | `Observation` → `Score` | Edge = price dislocation (z-score) × RSI strength. |
| 3 Strategy | `strategy.py` (`MeanReversionStrategy`) | `Observation`, `Score` → `Proposal \| None` | Buys when `z < -1` and `RSI <= rsi_low`; sells when `z > 1` and `RSI >= rsi_high`; sizes at `max_trade_bps` of the relevant balance. |
| 4 Risk gate | `risk.py` (`RiskGate`) | `Proposal` → `(ok, reason)` | Soft mirror of the vault's hard limits. |
| 5 LLM review | `llm_review.py` | `Proposal` → `LLMReview` | Optional; **fails closed** on error. |
| 6 Execute | `executor.py` | `Proposal` → `Execution` | Builds, signs (EIP-1559) and broadcasts `executeSwap`; waits for the receipt. |
| 7 Journal | `journal.py` | kind + payload → `JournalEntry` | Hash-chained NDJSON; publishes sealed entries to the backend. |

## Journal format

One JSON object per line, sorted keys, compact separators:

```json
{"hash":"0x…","id":"…","kind":"proposal","payload":{…},"prev_hash":"0x…","ts":1792159000.5}
```

- `hash` = `sha256` over `{"id","ts","kind","payload","prev"}`, so an entry
  cannot be edited without breaking the chain.
- `kind` ∈ `observation | proposal | review | execution | skip | halt`.
- `prev_hash` of the first entry is 32 zero bytes.
- `python -m aruven_agent.main --config <cfg> --verify-journal` re-verifies the
  whole file and reports the entry count.
- The head hash is checkpointed on-chain via `AruvenVault.rollEpoch`, which
  emits `EpochCheckpoint(epoch, journalHead)`.
- With `journal.publish_url` set, each sealed entry is POSTed to
  `<publish_url>/journal`. The backend enforces the chain link (409 on a stale
  `prev_hash`) and re-hashes the payload (422 on a mismatch). Publishing is
  best-effort: a backend outage never stops trading.

## Risk gates

Soft (agent code, `risk.max_trade_bps` / `daily_loss_bps` / `min_interval_sec`
should mirror the vault) and hard (vault contract, authoritative):

| Gate | Where | Behaviour |
|---|---|---|
| Strategy-token whitelist | vault | `TokenNotAllowed` |
| Router allow-list | vault + agent | `RouterNotAllowed`; the agent refuses to build the call |
| Non-zero output floor | vault + agent | `ZeroMinOut`; the agent refuses to build the call |
| Per-trade cap (5%) | vault | `TradeTooLarge` |
| Cooldown (1 h) | vault | `CooldownActive` |
| Daily-loss cap (2% of anchor NAV) | vault | `DailyLossCapHit` — blocks trading until `rollEpoch` |
| Slippage bound | vault | `TooLittleOut` |

## Kill switch

The agent halts itself on: a circuit-breaker revert (`CooldownActive`,
`TradeTooLarge`, `DailyLossCapHit`, `NotAgent`), an unexpected executor error, an
RPC error, or a journal write failure. Every halt is journaled with its stage and
reason before the loop stops.

## Configuration

See `agent/config.example.yaml`. Required: `rpc_url`, `chain_id`,
`vault_address`, `aruven_token`, `strategy_tokens`, `price_feed.url`,
`allowed_routers`. The executor key is read from the env var named by
`executor_private_key_env` and never from the config file.

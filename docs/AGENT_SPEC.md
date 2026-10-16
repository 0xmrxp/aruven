# Agent Spec

The autonomous trader: `screen → score → strategy → risk gate → LLM review →
execute → journal`, built for Uniswap v4 pools on Robinhood Chain with
vault-bounded execution. See ARCHITECTURE.md §3 for the system view; this file
is the implementation contract.

## Stages

| Stage | Module | In → Out | Notes |
|---|---|---|---|
| 1 Screen | `feeds.py` (`PriceFeed`, `HyperliquidFeed`, `BinanceFeed`, `GenericFeed`), `screen.py` (`ChainClient`, `Screener`) | token → `Observation` | Pulls a price series from the configured source and reads vault state over RPC. **No built-in stub**: an unconfigured feed halts the loop. |
| 2 Score | `strategy.py` (`Scorer`) | `Observation` → `Score` | Edge = price dislocation (z-score) × RSI strength. |
| 3 Strategy | `strategy.py` (`MeanReversionStrategy`, `SignalTracker`) | `Observation`, `Score` → `Proposal \| None` | Buys when `z < -1` and `RSI <= rsi_low`; sells when `z > 1` and `RSI >= rsi_high`; sizes at `max_trade_bps` of the relevant balance. `SignalTracker` fires **once per signal episode**, not once per bar — see below. |
| 4 Risk gate | `risk.py` (`RiskGate`) | `Proposal` → `(ok, reason)` | Soft mirror of the vault's hard limits. |
| 5 LLM review | `llm_review.py` | `Proposal` → `LLMReview` | Optional; **fails closed** on error. |
| 6 Execute | `executor.py` | `Proposal` → `Execution` | Builds, signs (EIP-1559) and broadcasts `executeSwap`; waits for the receipt. |
| 7 Journal | `journal.py` | kind + payload → `JournalEntry` | Hash-chained NDJSON; publishes sealed entries to the backend. |

## Trade cadence

The mean-reversion condition stays true for as long as price sits in the band —
on 1h candles that is dozens of consecutive bars. Acting on every one of them
produced **~63 trades/month** on BTC/ETH/SOL with the shipped defaults, and
~150/month with looser RSI bands: far too active for a 5%-per-trade, 1h-cooldown
vault, and it burns gas on repeated entries into the same dislocation.

`SignalTracker` therefore fires only when a signal **starts** (or flips side),
and journals the episode's start/change/end. Cadence then follows how often the
market actually dislocates:

| Config | Level-triggered | Episode-triggered |
|---|---|---|
| `rsi 30/70`, `edge>=25`, cooldown 1h (old defaults) | 62.4 /month | 14.7 /month |
| `rsi 25/75`, `edge>=60`, cooldown 4h (shipped) | 12.4 /month | **7.8 /month** |
| `rsi 30/70`, `edge>=250`, cooldown 24h | 4.2 /month | 3.9 /month |

Measured over 82 days of real 1h candles (BTC 6.6, ETH 9.2, SOL 7.7 trades/month
for the shipped config). `tests/test_agent.py::TestCadence` pins the behaviour:
a persistent signal must produce exactly one trade.

Note the cooldown is a *rate* limit, not the cadence control — `min_interval_sec`
is a floor enforced on-chain by the vault, and must be **at or above** the
vault's `minInterval` (a shorter value makes the agent attempt trades the vault
reverts with `CooldownActive`, which halts it). Tightening `min_edge_bps` barely
moves the cadence (25 → 100 bps changed nothing); the episode boundary is what
does.

## Market data

`price_feed.source` picks the source. All are public and keyless:

- `hyperliquid` (default) — `/info` `candleSnapshot`; real OHLCV candles.
- `binance` — `/api/v3/klines`.
- `generic` — your own endpoint: `GET {url}?token=<n>&limit=<n>` returning
  `{"prices": [...oldest first...], "volume_24h": n}`.

`price_feed.fallback_source` is retried once when the primary fails, and the
source that served each observation is recorded in the journal
(`payload.source`). The series must be long enough for the RSI period — a
shorter one produces no signal and is journaled as `skip`, never traded on.

`python -m aruven_agent.main --config <cfg> --check-feed` fetches every strategy
token once and reports length/price/volume without touching the chain.

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

# ARUVEN

**$ARUVEN — The agent-run vault token on Robinhood Chain.**

An open-source autonomous trading agent manages a fully on-chain, verifiable treasury. The token is paired against tokenized stocks (e.g. $NVDA, $AAPL) and stablecoins on Uniswap v4. Every trade the agent makes is a public transaction. Every dollar of the vault is visible on-chain. No dashboards of trust — only proof.

> **The problem.** "Vault-backed" tokens on Robinhood Chain (e.g. the $AI / Artificial Inu pattern) display treasury balances on a landing page but give holders no enforceable claim and no way to verify that claimed fee splits (80/20, burn/lock) actually happen. The narrative says "agent" — the reality is a static multisig.

> **The answer.** ARUVEN ships the thing those projects only claim: a real autonomous agent, real on-chain accounting, and a treasury whose strategy, entry rules, and performance are all public code and public state.

---

## What ARUVEN is

| Layer | What it is | Why it matters |
|---|---|---|
| **Token** | ERC-20 with capped supply, deployed via a Doppler-style fair-launch flow | No presale, no team allocation, no VC unlock schedules |
| **Pair** | $ARUVEN × tokenized stock ($NVDA et al.) + $ARUVEN × USDG on Uniswap v4 | Price is expressed in *compute* (or whatever underlying the community votes), not just USD |
| **Vault** | Non-custodial treasury contract; fees route in automatically, withdrawals only via timelocked governance | Treasury is transparent by construction, not by promise |
| **Agent** | Open-source trading agent with a disciplined pipeline: screen → score → strategy → risk gate → LLM review → execute → journal | The "autonomous trader" is auditable code, not marketing |
| **Proof** | On-chain proof-of-reserve + a public, read-only agent journal | Anyone can reconcile the vault statement against the chain, transaction by transaction |

## How it works

```
        trades                  feeds
  ┌─────────────┐   ┌──────────────────────┐
  │ Uniswap v4  │◄──┤   ARUVEN Agent       │◄──── market data, pool state,
  │  pools      │   │  (open source, 24/7) │      on-chain treasury state
  └──────┬──────┘   └──────────┬───────────┘
         │                     │ settles & reports
         ▼                     ▼
  ┌─────────────────────────────────────────┐
  │  ARUVEN Vault (on-chain treasury)       │
  │  • fees auto-route in on every swap     │
  │  • agent trades only from/to the vault  │
  │  • withdrawals: proposal → vote → delay │
  │  • proof-of-reserve readable by anyone  │
  └─────────────────────────────────────────┘
```

1. **Trades pay fees into the vault.** Every $ARUVEN buy and sell on the v4 pools routes a protocol fee to the vault, enforced by the hook.
2. **The agent works for the vault.** It screens, sizes, risk-gates, and executes swaps with vault funds. Its entire decision journal is published.
3. **The chain is the auditor.** Vault holdings, PnL, and trade history are direct RPC reads — the same numbers the frontend shows, from the same source of truth.
4. **Holders govern the treasury.** Timelock + Governor (the same battle-tested OZ stack verified on $AI's vault, but with the defaults actually documented): 2-day minimum delay, public proposal/vote.
5. **Value accrual.** Trading volume → fees → vault grows → circulating supply shrinks via scheduled burns. The agent's job is to make the vault's holdings outperform its inflow.

## Why now

- **Robinhood Chain** (mainnet July 2026) has tokenized equities natively on-chain and a first-class **Agentic Trading** program — the exact narrative this product ships for real.
- **Uniswap v4 hooks** make fee routing and custom pool behaviour permissionless — the "vault grows on every trade" mechanic is now deployable without a custom DEX.
- The $AI pattern proved the demand (9-digit FDV) while shipping none of the verification. ARUVEN is the honest version of that trade.

## Roadmap

- **Phase 0 — Foundations** ✅ repo, architecture docs, contract design
- **Phase 1 — Contracts** ✅ code + tests written; pending: testnet deploy + audit review (see `contracts/`)
- **Phase 2 — Agent** ✅ code + tests written (`agent/`): pipeline, risk gates, LLM review, hash-chained journal, dry-run executor; pending: live tx signer + price feed (Phase 3 wiring)
- **Phase 3 — Frontend + Backend** ✅ (`backend/`, `frontend/`): indexer, REST API with /verify trust endpoint, EIP-1559 signer, Next.js dashboard; pending: deploy to VPS, pool-spot NAV, governance proposal reads
- **Phase 4 — Launch**: fair-launch distribution, listing, agent live

## Repository layout

```
aruven/
├── docs/            # architecture, tokenomics, agent spec, launch plan
├── contracts/       # foundry project: token, vault, hook, governance (Phase 1)
├── agent/           # the autonomous trader (Phase 2)
├── frontend/        # proof-of-reserve + governance dashboard (Phase 3)
└── README.md
```

## Status

Pre-launch. Design phase. Nothing here is an offer to sell securities, and nothing in the token design guarantees profit. The agent can lose money; that risk is disclosed, journaled, and visible to everyone — which is the point.

## License

MIT.

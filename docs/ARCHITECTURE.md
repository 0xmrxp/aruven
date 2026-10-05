# ARUVEN Architecture

Complete system breakdown: contracts, agent, backend, frontend, and the economics.

## 1. System overview

```
┌────────────────────────────── Robinhood Chain (chainId 4663) ─────────────────────────────┐
│                                                                                           │
│   Uniswap v4 PoolManager                                                                 │
│   ├── Pool: $ARUVEN / $NVDA-token   ── AruvenHook (fee router)                            │
│   └── Pool: $ARUVEN / $USDG         ── AruvenHook (fee router)                            │
│         │ swap fees                                                                       │
│         ▼                                                                                 │
│   AruvenVault  ── holds: NVDA-token, USDG, $ARUVEN, ETH                                   │
│   │   • receives protocol fees from the hook                                              │
│   │   • only the Agent executor may swap from it (whitelisted strategy address)           │
│   │   • withdrawals only via TimelockController (48h) after Governor approval             │
│   │   • proof-of-reserve view functions: holdings(), nav(), feeInflow(), pnl()            │
│   │                                                                                       │
│   Governor (OZ Governor)  ── voting token: $ARUVEN                                       │
│   TimelockController      ── proposer: Governor, executor: open                           │
│                                                                                           │
└───────────────────────────────────────────────────────────────────────────────────────────┘
         ▲ RPC reads                          ▲ signed txs (agent wallet)
         │                                     │
┌────────┴──────────┐              ┌───────────┴──────────────┐
│  aruven-backend   │              │  aruven-agent            │
│  • RPC indexer    │◄─── events ──│  • AutoCEX pipeline      │
│  • vault state    │   journal    │  • risk-gated executor   │
│  • REST/GraphQL   │              │  • journal publisher     │
│  • burn scheduler │              └──────────────────────────┘
└────────┬──────────┘
         │ REST
┌────────┴──────────┐
│  aruven-frontend  │
│  • proof-of-reserve dashboard (holdings, NAV, PnL, fee inflow)
│  • live agent journal (every decision, every trade)
│  • governance UI (proposals, votes)
│  • buy $ARUVEN (embedded Uniswap swap)
└───────────────────┘
```

## 2. Contracts (Foundry, Solidity 0.8.x)

| Contract | Purpose | Key design decisions |
|---|---|---|
| `AruvenToken` | ERC-20, capped supply 1B, burnable | Standard OZ `ERC20Burnable` + `ERC20Capped` — no owner functions that mint. Ownership renounced post-launch. |
| `AruvenHook` | Uniswap v4 hook on both pools | `afterSwap` → takes protocol fee (bps, governance-settable, capped at e.g. 300bps) → routes to vault. Also exposes `lastFeeEvent()` for indexing. |
| `AruvenVault` | Treasury + strategy host | Holds all protocol assets. `executeSwap` restricted to `agentExecutor` (an address governed by timelock). View functions for full accounting. Emits `TradeExecuted(tokenIn, tokenOut, amountIn, amountOut, reason)`. |
| `Governor + Timelock` | OZ GovernorVotes + TimelockController | Proposal threshold ~0.4% supply, voting period 3 days, quorum 5%, timelock 48h. |

**What we explicitly do NOT do** (lessons from auditing the $AI pattern):
- No "creator fee" pass-through to an opaque receiver (80/20 splits you can't verify).
- No claimed burns without on-chain events; burn count is `totalSupply()` delta, nothing else.
- No admin multisig with token control. Governance is the only privileged path.

## 3. Agent (the "autonomous trader")

Port of the AutoCEX pipeline, adapted to Robinhood Chain:

1. **Screen** — liquidity, volume, spread, volatility of the vault's tradable pairs.
2. **Score** — strategy ranking from journal history + on-chain pool state.
3. **Strategy** — mean-reversion / momentum on $ARUVEN pairs, delta-neutral USDG parking.
4. **Risk gate** — hard limits enforced both in agent code AND in the vault contract (max position %, daily loss cap, drawdown halt).
5. **LLM review** — final sanity check on each proposed trade (model call with structured output; the review is journaled).
6. **Execute** — swap from vault via `executeSwap`, slippage-bounded.
7. **Journal** — append every decision + tx hash to the public journal (signed, hash-chained; the head hash is checkpointed on-chain each epoch).

**Security posture:** the agent wallet can only call `vault.executeSwap` with whitelisted tokens and bounded sizes. Even a full agent compromise cannot drain the treasury — worst case it trades badly within limits, and the journal + risk gates make that visible within one epoch (1 hour).

## 4. Backend

- **Indexer**: subscribes to `TradeExecuted`, hook fee events, `Transfer` of vault assets; maintains the canonical state that the frontend reads. Written so that a skeptical third party can re-derive every number from raw logs (public `verify` endpoint).
- **API**: single small service (FastAPI or Node) exposing `/vault`, `/journal`, `/proposals`, `/verify`.
- **Burn scheduler**: EIP-712-signed batch burn of the $ARUVEN half of fees, executed on a fixed epoch, only from the vault's own balance — the "supply shrinks" leg is a script anyone can audit and re-run.
- **Ops**: runs on the user's VPS (outside the $15 budget); Docker compose; public status page.

## 5. Frontend

Next.js app, single purpose: **prove, then sell**.

- Hero: live vault NAV + "verify on-chain" link (deep-link to Blockscout per holding).
- The agent journal rendered as a live feed: every screen/score/decision/trade with tx links.
- PnL chart of the vault vs. buy-and-hold-NVDA benchmark. This is the core trust artifact: the agent must justify its existence vs. doing nothing.
- Governance tab, swap embed, FAQ written in plain Indonesian + English.

## 6. Tokenomics & buyer value

- **Supply**: 1,000,000,000 $ARUVEN, fixed. ~100% into fair launch (bonding-curve style via a Doppler-like flow or programmable.market custom launch) — no team tranche, no investor tranche.
- **Fee flow**: buy fees → vault (denominated in the pair asset); sell fees → 50% vault / 50% burned. All splits are hook-enforced and event-visible, not landing-page claims.
- **Value accrual**: vault NAV per holder is not a claim (no redemption) — that's disclosed plainly. The claim is: burns reduce supply while volume fills the vault, and the agent is contractually bound (by vault code) to trade only within risk limits. ARUVEN sells the *verified agent + growing treasury* narrative, honestly.
- **What attracts buyers**:
  1. **Verifiability as a differentiator** — in a chain full of unverifyable "vault" tokens, being the one project where every number is a read call is the moat.
  2. **Real agent, open source** — the Robinhood Agentic Trading narrative with actual shipped code.
  3. **Anti-meme discipline** — the audience burned by $AI-style projects is the target market.

## 7. SaaS / revenue layer (beyond the token)

- **ARUVEN-as-a-service**: the vault + hook + agent stack packaged so other launches can deploy "agent-run" tokens. Fee: 10–20% of their protocol fees, paid to the ARUVEN treasury. This is the long-term business: infrastructure for honest token launches.
- **Journal/proof API**: read endpoints free; alerting + analytics as a paid tier for traders and terminals (Dexscreener-style integration).

## 8. $15 budget breakdown

| Item | Cost | Note |
|---|---|---|
| Domain (aruven.xyz / .com) | ~$2–12/yr | .xyz first choice; .com if available cheap |
| Deployment gas (token, vault, hook, governor, pools) | ~$5–8 | Robinhood Chain gas is ETH-denominated but cheap (gas price ~$0.01/tx observed) |
| Initial liquidity | ~$0 from budget | seed from launch proceeds, not the $15 |
| VPS | $0 | already owned |
| Headroom | remainder | for a second domain or extra gas if mainnet gas spikes |

The $15 is genuinely enough for the full contract deployment. The real cost is dev time; that's the founder's equity.

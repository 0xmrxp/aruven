# ARUVEN Architecture

Complete system breakdown: contracts, agent, backend, frontend, and the economics.

## 1. System overview

```
┌────────────────────────────── Robinhood Chain (chainId 46630) ────────────────────────────┐
│                                                                                           │
│   Uniswap v4 PoolManager                                                                 │
│   ├── Pool: $ARUVEN / $NVDA-token   ── AruvenFeeHook (fee router)                         │
│   └── Pool: $ARUVEN / $USDG         ── AruvenFeeHook (fee router)                         │
│         │ swap fees (charged on the swap's unspecified currency)                          │
│         ▼                                                                                 │
│   AruvenVault  ── holds: NVDA-token, USDG, $ARUVEN, ETH                                   │
│   │   • receives protocol fees from the hook (onFeeReceived / receive)                     │
│   │   • only the Agent executor may swap from it, through an ALLOW-LISTED router           │
│   │   • withdrawals only via TimelockController (48h) after Governor approval              │
│   │   • proof-of-reserve view functions: holdings(), ethBalance(), dailyLossCap()          │
│   │                                                                                       │
│   Governor (OZ Governor, timestamp clock)  ── voting token: $ARUVEN                        │
│   TimelockController      ── proposer: Governor, executor: open                           │
│                                                                                           │
└───────────────────────────────────────────────────────────────────────────────────────────┘
         ▲ RPC reads                          ▲ signed txs (agent wallet)
         │                                     │
┌────────┴──────────┐              ┌───────────┴──────────────┐
│  aruven-backend   │              │  aruven-agent            │
│  • RPC indexer    │◄─── events ──│  • ARUVEN pipeline      │
│  • vault state    │   journal    │  • risk-gated executor   │
│  • REST API       │◄─ POST /journal─ journal publisher       │
│  • burn scheduler │              └──────────────────────────┘
└────────┬──────────┘
         │ REST
┌────────┴──────────┐
│  aruven-frontend  │
│  • proof-of-reserve dashboard (holdings, NAV, PnL, fee inflow)
│  • live agent journal (every decision, every trade)
│  • deployments directory (factory launches)
│  • governance UI (proposals, votes)
└───────────────────┘
```

## 2. Contracts (Foundry, Solidity 0.8.26)

| Contract | Purpose | Key design decisions |
|---|---|---|
| `AruvenToken` | ERC-20, fixed 1B supply, burnable, permit, votes | OZ `ERC20Burnable` + `ERC20Permit` + `ERC20Votes`. Supply is minted in the constructor and **permanently fixed at construction** when `fixedSupply` is true (the flagship deploy and every factory launch) — no later `renounceMint()` step to forget. Vote clock is timestamps. |
| `AruvenHook` (abstract) / `AruvenFeeHook` | Uniswap v4 hook on the flagship pools | `afterSwap` charges a per-pool fee (bps, governance-settable, capped at 300) **on the swap's unspecified currency** and takes it via `poolManager.take()`. Declares `afterSwapReturnDelta` and returns the taken amount, so the delta is accounted for rather than left as an unsettled debt. Routes to the vault via `onFeeReceived` (ERC-20) or a plain call (native ETH). |
| `AruvenSplitHook` | Hook deployed by `AruvenFactory` for third-party launches | Splits every taken fee by an **immutable** `shareBps` between the deployment's vault and the flagship treasury. See §7a. |
| `AruvenVault` | Treasury + strategy host | Holds protocol assets incl. ETH (`receive()`). `executeSwap` is `AGENT_ROLE`-only and bounded by: strategy-token whitelist, **router allow-list**, non-zero `amountOutMin`, 5%-of-balance per trade, 1h cooldown, and a daily-loss cap that **blocks trading** once breached. `rescue()` can never touch strategy tokens or $ARUVEN. |
| `Governor + Timelock` | OZ GovernorVotes + TimelockController | Proposal threshold 0.4% supply, voting delay 2 h, voting period 3 days, quorum 5%, timelock 48 h — all in **seconds**, matching the token's timestamp clock. |

**What we explicitly do NOT do** (lessons from auditing the $AI pattern):
- No "creator fee" pass-through to an opaque receiver (80/20 splits you can't verify).
- No claimed burns without on-chain events; burn count is `totalSupply()` delta, nothing else.
- No admin multisig with token control. Governance is the only privileged path.

## 3. Agent (the "autonomous trader")

The autonomous trader pipeline, built for Robinhood Chain:

1. **Screen** — pulls a price series from a configured market-data feed and reads vault state (epoch, anchor NAV, balances, cooldown) over RPC. There is **no built-in price stub**: an unconfigured feed halts the loop rather than producing synthetic prices.
2. **Score** — mean-reversion edge from the price z-score and RSI strength.
3. **Strategy** — buys the dip / sells the rip on the vault's strategy tokens, sized as `max_trade_bps` of the relevant balance. A missing RSI (too little history) is journaled as `skip`, not coerced into a neutral value.
4. **Risk gate** — soft limits in agent code that mirror the vault's hard on-chain ones (defense in depth).
5. **LLM review** — optional final sanity check on each proposed trade; fails **closed** on error.
6. **Execute** — builds the `executeSwap` call with a router from the allow-list and a non-zero slippage floor, signs it as an EIP-1559 transaction, broadcasts, and waits for the receipt. A circuit-breaker revert halts the agent.
7. **Journal** — appends every decision + tx hash to the local hash-chained NDJSON log, then POSTs the sealed entry to the backend so `/journal` has a source. Publishing is best-effort; the local chain stays authoritative.

**Security posture (precise):** the executor wallet holds nothing but `AGENT_ROLE`. It can only call `vault.executeSwap`, only with whitelisted tokens, only through an allow-listed router, only within the per-trade cap and cooldown, and only with a non-zero output floor. Those bounds make a compromise a **rate-limited loss**, not a drain: value can be lost at a bounded rate by trading badly, but funds cannot be routed to an arbitrary contract or moved out of the allowed token set. The journal and the on-chain daily-loss gate make the damage visible within one epoch.

## 4. Backend

- **Indexer**: polls `TradeExecuted`, `FeeReceived`, `VaultBurn`, `EpochCheckpoint`, `RiskParamsUpdated` and the factory's `Launch` events; maintains the canonical state the frontend reads. Idempotent — re-running from block 0 re-derives identical state. A log it cannot decode is counted and skipped rather than stalling the range.
- **API**: FastAPI service exposing `/health`, `/vault`, `/trades`, `/journal` (GET + **POST**), `/proposals`, `/deployments`, `/verify`.
- **Journal intake**: `POST /journal` accepts sealed entries from the agent, enforces the hash chain (an entry whose `prev_hash` is not the current head is rejected), and re-hashes the payload. `/verify` re-derives the whole chain.
- **Burn scheduler**: batch burn of the $ARUVEN held by the vault, executed on a fixed epoch from the vault's own balance (`BURNER_ROLE`); every burn is a `VaultBurn` event.
- **Ops**: runs on the user's VPS (outside the $15 budget).

## 5. Frontend

Next.js app, single purpose: **prove, then sell**.

- Hero: live vault NAV + "verify on-chain" link (deep-link to Blockscout per holding).
- The agent journal rendered as a live feed: every observation/decision/trade with tx links.
- PnL chart of the vault vs. buy-and-hold-NVDA benchmark. This is the core trust artifact: the agent must justify its existence vs. doing nothing.
- Deployments directory: every factory launch, with the `verified` badge derived from the indexed fee share.
- Governance tab, swap embed, FAQ written in plain Indonesian + English.

## 6. Tokenomics & buyer value

- **Supply**: 1,000,000,000 $ARUVEN, fixed at construction. ~100% into fair launch (bonding-curve style via a Doppler-like flow or programmable.market custom launch) — no team tranche, no investor tranche.
- **Fee flow**: every pool swap routes a protocol fee (bps, governance-settable, ≤300) into the vault. The vault burns its $ARUVEN balance on the epoch schedule via `burnVaultTokens` (`BURNER_ROLE`), which emits `VaultBurn`. There is no hardcoded 50/50 split in code; the split is a governance parameter, and every leg is an on-chain event.
- **Value accrual**: vault NAV per holder is not a claim (no redemption) — that's disclosed plainly. The claim is: burns reduce supply while volume fills the vault, and the agent is bound by vault code to trade only within risk limits. ARUVEN sells the *verified agent + growing treasury* narrative, honestly.
- **What attracts buyers**:
  1. **Verifiability as a differentiator** — in a chain full of unverifiable "vault" tokens, being the one project where every number is a read call is the moat.
  2. **Real agent, open source** — the Robinhood Agentic Trading narrative with actual shipped code.
  3. **Anti-meme discipline** — the audience burned by $AI-style projects is the target market.

## 7. SaaS / revenue layer (beyond the token)

- **ARUVEN-as-a-service**: the vault + hook + agent stack, deployed by third parties through the **AruvenFactory** — never by hand-copying source. See §7a for how the revenue share is enforced.
- **Journal/proof API**: read endpoints free; alerting + analytics as a paid tier for traders and terminals (Dexscreener-style integration).

### 7a. Revenue enforcement model

Open-source code cannot force anyone to pay. Enforcement comes from WHERE the fee split lives:

| Layer | Mechanism | Strength |
|---|---|---|
| Agent/backend code | fee routing in Python | none — deletable in a fork. Never the primary path. |
| Hand-deployed contracts | copy source, change treasury address | weak — immutability locks a deployed instance, not the fork. |
| **AruvenFactory (primary)** | third parties launch through our factory; the factory deploys an `AruvenSplitHook` whose `shareBps` is an **immutable constructor argument**. The share is part of the deployed bytecode, not a parameter an event records and not a setter anyone can zero. | strong — removing the split requires deploying a different hook, which means a different pool, abandoning the original's liquidity and holders. |
| Non-technical (support) | Source-available license (Uniswap-BSL-style: free use, commercial = pay), "ARUVEN-powered" badge, indexer/dashboard integration only for factory launches, cluster liquidity & shared agent infra | soft moat — serious teams comply for clean legal/listing posture and visibility. |

Honest limits: nothing stops a from-scratch clone writing its own hook and paying nothing (as pump.fun clones prove). What is enforced is that the **factory path always pays**; what we retain is the name, the audited code, the dashboard, and the easiest on-ramp.

### 7b. Deployments directory (frontend)

Every launch through the factory is indexed and listed on the ARUVEN frontend:

- Auto-discovery: `Launch(token, vault, creator, hook, feeShareBps, metaHash)` event from the factory → indexer → public directory.
- Per-deployment page: token name/symbol/CA, vault CA + live holdings/NAV, hook address and fee config, agent journal feed (same components as the main $ARUVEN dashboard), PnL vs benchmark, creator links.
- Verified badge only for factory launches (the badge is derived from the indexed fee share); forks/clones are not indexed — this is the visibility moat in §7a.
- API: `/deployments` on the public API for terminals and other apps to consume.

## 8. $15 budget breakdown

| Item | Cost | Note |
|---|---|---|
| Domain (aruven.xyz / .com) | ~$2–12/yr | .xyz first choice; .com if available cheap |
| Deployment gas (token, vault, hook, governor, pools) | ~$5–8 | Robinhood Chain gas is ETH-denominated but cheap (gas price ~$0.01/tx observed) |
| Initial liquidity | ~$0 from budget | seed from launch proceeds, not the $15 |
| VPS | $0 | already owned |
| Headroom | remainder | for a second domain or extra gas if mainnet gas spikes |

The $15 is genuinely enough for the full contract deployment. The real cost is dev time; that's the founder's equity.

## 9. Deploy-time requirements

- The hook address must carry the flag bits the hook declares (`afterSwap` + `afterSwapReturnDelta`): `HOOK_FLAGS = (1 << 6) | (1 << 2)`. The deployer mines an address with those bits set (the standard v4 pattern) and the PoolManager validates it on `initialize`.
- `vault.setRouter(...)` must be called before the agent can trade at all. That is deliberate: the router allow-list is what bounds a compromised agent.
- `POOL_MANAGER` is a required env var for the deploy script (the factory and the flagship hook both need it).

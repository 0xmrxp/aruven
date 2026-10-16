# Tokenomics

## Supply

| | |
|---|---|
| Name / symbol | Aruven / $ARUVEN |
| Max supply | 1,000,000,000 (fixed) |
| Minted at | construction, in full, to the launch receiver |
| Further minting | impossible — `mintingDisabled` is set at construction for the flagship and every factory launch |
| Team / investor tranche | none |
| Redemption claim on the vault | none (disclosed, see below) |

"Fixed supply" here is a property of the deployed bytecode, not a promise: the
token is created with `fixedSupply = true`, so `mint()` reverts from block 0.
There is no later `renounceMint()` step that could be forgotten.

## Fee flow

Every swap on an ARUVEN pool pays a protocol fee into the treasury:

- The fee is a per-pool bps rate set by governance, hard-capped at **300 bps**.
- It is charged on the swap's **unspecified currency** (output for exact-input,
  input for exact-output) and taken from the PoolManager by the hook.
- The hook routes it to the vault and emits `FeeTaken`; the vault emits
  `FeeReceived` (ERC-20) or `EthReceived` (native ETH).
- There is **no hardcoded 50/50 split** in code. The burn leg is a governance
  decision executed by the vault's `BURNER_ROLE` on the epoch schedule, and each
  burn emits `VaultBurn`. Anyone can reconstruct the split from the events.

Third-party launches made through the factory split every taken fee by an
immutable `shareBps` between their own vault and the ARUVEN treasury — see
ARCHITECTURE.md §7a.

## Value accrual

Vault NAV per holder is **not a claim**: there is no redemption right, and that
is disclosed plainly rather than implied. What a holder has is:

1. A supply that only shrinks (burns are on-chain events, and `totalSupply()` is
   the only burn counter that matters).
2. A treasury whose strategy, entry rules and performance are public code and
   public state.
3. An agent whose worst case is bounded on-chain: allow-listed tokens, an
   allow-listed router, a per-trade cap, a cooldown, a non-zero output floor and
   a daily-loss gate that blocks trading once breached.

The agent can lose money. That risk is journaled, on-chain, and visible — which
is the point.

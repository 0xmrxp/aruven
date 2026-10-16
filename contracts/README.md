# contracts

Foundry project: the full ARUVEN contract suite.

## Layout

| Contract | Purpose |
|---|---|
| `src/AruvenToken.sol` | ERC-20 (burnable, permit, votes) with a hard 1B cap; supply permanently fixed at construction when `fixedSupply` is true |
| `src/AruvenVault.sol` | Treasury + strategy host: fee intake (ERC-20 and ETH), agent-bounded swaps, epoch burns, risk limits enforced in-contract |
| `src/AruvenHook.sol` | Abstract Uniswap v4 hook: charges a per-pool fee on the swap's unspecified currency, takes it from the PoolManager, routes it to the vault |
| `src/AruvenFeeHook.sol` | Concrete flagship hook: 100% of taken fees to the vault |
| `src/AruvenSplitHook.sol` | Hook deployed by the factory for third-party launches: splits fees by an immutable `shareBps` to the flagship treasury |
| `src/AruvenGovernor.sol` | OZ Governor (CountingSimple + Votes + 5% quorum + TimelockControl) on a timestamp clock |
| `src/AruvenFactory.sol` | Deploys third-party stacks (token + vault + split hook); the revenue share lives in the hook it deploys |
| `script/DeployAruven.s.sol` | Full flagship deployment (timelock → token → vault → governor → factory → fee hook) |

## Build & test

```
forge build
forge test -vvv
```

`via_ir = true` is required: `AruvenFactory.launch` deploys three contracts plus
a hook, which is more local state than the legacy codegen can keep on the stack.

## Security model (summary)

- The agent's executor wallet holds **no role other than `AGENT_ROLE` on the
  timelock**. `executeSwap` is bounded by: strategy-token whitelist, **router
  allow-list**, **non-zero `amountOutMin`**, 5%-of-balance per trade, 1h
  cooldown, and a daily-loss cap that **blocks trading** once breached.
- Those bounds make a fully compromised agent a **rate-limited loss, not a
  drain**: it cannot route through an arbitrary contract, cannot accept an
  arbitrary output, and cannot move funds outside the whitelisted token set. It
  *can* trade badly at a bounded rate — that is the residual risk, and it is
  visible in the journal and on-chain within one epoch.
- Withdrawals/parameter changes only via Governor → 48h Timelock.
- Hook fees are capped at 300 bps, charged on the unspecified currency, and land
  in the vault as `FeeReceived` / `EthReceived` events.
- `rescue()` can never touch strategy tokens or $ARUVEN; ETH is rescuable via
  `rescueEther()` since it is never a strategy token.
- `renounceMint` / `fixedSupply` make "fixed supply" a bytecode property.

## Deploy-time requirements

- The hook address must carry the flags the hook declares
  (`HOOK_FLAGS = (1 << 6) | (1 << 2)` — `afterSwap` + `afterSwapReturnDelta`).
  Mine an address with those bits set; the PoolManager validates it on
  `initialize`.
- `POOL_MANAGER` is required by the deploy script and by `factory.launch`.
- `vault.setRouter(...)` and `vault.setStrategyToken(...)` are governance calls
  (the vault's admin is the timelock), so they happen after deploy — trading is
  blocked until a router is allow-listed, which is intentional.

## Not yet done

- [ ] Uniswap v4 PoolManager integration test (hook fee on a real pool) —
      requires deploying a PoolManager in tests; the hook's fee currency,
      split routing and permissions are covered by unit tests today
- [ ] Testnet deployment on Robinhood Chain (chainId 46630) + Blockscout verification
- [ ] External audit

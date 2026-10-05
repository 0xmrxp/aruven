# contracts

Foundry project: the full ARUVEN contract suite.

## Layout

| Contract | Purpose |
|---|---|
| `src/AruvenToken.sol` | ERC-20 (burnable, permit, votes) with a hard 1B cap and a permanent mint-disable switch |
| `src/AruvenVault.sol` | Treasury + strategy host: fee intake, agent-bounded swaps, epoch burns, risk limits enforced in-contract |
| `src/AruvenHook.sol` / `src/AruvenFeeHook.sol` | Uniswap v4 hook: takes a per-pool protocol fee on every swap and routes it to the vault |
| `src/AruvenGovernor.sol` | OZ Governor (CountingSimple + Votes + 5% quorum + TimelockControl): the only privileged path |
| `src/AruvenFactory.sol` | Deploys third-party stacks; enforces the minimum revenue-share to the flagship vault |
| `script/DeployAruven.s.sol` | Full flagship deployment (timelock → token → vault → governor → factory) |

## Build & test

```
forge build
forge test -vvv
```

## Security model (summary)

- The agent's executor wallet holds **no role other than AGENT_ROLE on the timelock**, and `executeSwap` is bounded by: strategy-token whitelist, 5%-of-balance per trade, 2%-of-NAV daily loss cap, 1h cooldown, and slippage minimums. A fully compromised agent cannot drain the treasury.
- Withdrawals/parameter changes only via Governor → 48h Timelock.
- Hook fees are capped at 300 bps and land in the vault as plain ERC-20 transfers + `FeeTaken` events.
- `rescue()` can never touch strategy tokens or $ARUVEN.

## Not yet done (Phase 1 backlog)

- [ ] Uniswap v4 PoolManager integration test (hook fee on a real pool) — requires deploying a PoolManager in tests
- [ ] Testnet deployment on Robinhood Chain (chainId 46630) + Blockscout verification
- [ ] External audit

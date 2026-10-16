// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Script, console2} from "forge-std/Script.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {AruvenToken} from "../src/AruvenToken.sol";
import {AruvenVault} from "../src/AruvenVault.sol";
import {AruvenGovernor} from "../src/AruvenGovernor.sol";
import {AruvenFactory} from "../src/AruvenFactory.sol";
import {AruvenFeeHook} from "../src/AruvenFeeHook.sol";
import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";

/// @title DeployAruven
/// @notice Full flagship deployment:
///         timelock -> token -> vault -> governor -> factory -> fee hook.
/// @dev Run against Robinhood Chain testnet (chainId 46630) first:
///        forge script script/DeployAruven.s.sol --rpc-url $RH_TESTNET_RPC \
///          --broadcast --verify -vvv
///      Required env: PRIVATE_KEY, POOL_MANAGER (the Uniswap v4 PoolManager).
///      Optional env: LAUNCH_RECEIVER (defaults to the deployer).
///
///      Roles wired at deploy: governor = timelock PROPOSER/CANCELLER, executor
///      open, timelock admin self-held. The token is created with
///      `fixedSupply = true`, so its supply is permanently fixed from block 0.
///
///      Post-deploy governance actions (NOT done here, because the vault's admin
///      is the timelock and these are privileged):
///        - vault.setStrategyToken(<token>, true) for each tradable asset
///        - vault.setRouter(<router>, true) — trading is blocked until a router
///          is allow-listed, which is deliberate: the allow-list is what bounds
///          a compromised agent
///        - timelock.grantRole(AGENT_ROLE / BURNER_ROLE, ...)
///        - timelock.revokeRole(DEFAULT_ADMIN_ROLE, deployer)
///        - hook.setPoolFee(poolKey, bps) per pool
contract DeployAruven is Script {
    // Launch parameters (tune before broadcast)
    uint256 constant INITIAL_SUPPLY = 1_000_000_000e18; // full 1B into the fair-launch receiver
    uint48 constant TIMELOCK_DELAY = 48 hours;
    uint256 constant MAX_TRADE_BPS = 500; // 5% of token balance per trade
    uint256 constant DAILY_LOSS_BPS = 200; // 2% of NAV daily loss cap
    uint256 constant MIN_INTERVAL = 3600; // 1h between agent trades
    uint256 constant FACTORY_MIN_SHARE_BPS = 1_000; // 10% minimum revenue share

    function run()
        external
        returns (
            AruvenToken token,
            AruvenVault vault,
            AruvenGovernor governor,
            AruvenFactory factory,
            AruvenFeeHook hook
        )
    {
        uint256 pk = vm.envUint("PRIVATE_KEY");
        address deployer = vm.addr(pk);
        address launchReceiver = vm.envOr("LAUNCH_RECEIVER", deployer); // fair-launch contract/hot wallet
        IPoolManager poolManager = IPoolManager(vm.envAddress("POOL_MANAGER"));

        vm.startBroadcast(pk);

        // 1. Timelock: no proposers yet (governor gets it), executor OPEN, timelock self-admin
        address[] memory noProposers = new address[](0);
        address[] memory openExec = new address[](1);
        openExec[0] = address(0);
        TimelockController timelock = new TimelockController(TIMELOCK_DELAY, noProposers, openExec, deployer);

        // 2. Token: owner = timelock, full supply to the fair-launch receiver,
        //    supply permanently fixed at construction.
        token = new AruvenToken(address(timelock), launchReceiver, INITIAL_SUPPLY, true);

        // 3. Vault: admin = timelock
        vault = new AruvenVault(address(timelock), address(token), MAX_TRADE_BPS, DAILY_LOSS_BPS, MIN_INTERVAL);

        // 4. Governor: proposals drive the timelock
        governor = new AruvenGovernor(token, timelock);

        // 5. Factory: third-party launches route >=10% to this vault, enforced by
        //    the split hook the factory deploys.
        factory = new AruvenFactory(address(timelock), address(vault), FACTORY_MIN_SHARE_BPS);

        // 6. Flagship fee hook: 100% of taken fees to the vault. Governance
        //    (timelock) is the only address that can set per-pool fees.
        hook = new AruvenFeeHook(poolManager, address(vault), address(timelock));

        // 7. Hand timelock control to governance: governor as PROPOSER+CANCELLER.
        timelock.grantRole(timelock.PROPOSER_ROLE(), address(governor));
        timelock.grantRole(timelock.CANCELLER_ROLE(), address(governor));
        // NOTE: do NOT revoke the deployer DEFAULT_ADMIN_ROLE here — keep it for the
        // bootstrap phase (granting AGENT_ROLE etc.); revoke in a first governance proposal.

        vm.stopBroadcast();

        console2.log("Timelock:  ", address(timelock));
        console2.log("Token:     ", address(token));
        console2.log("Vault:     ", address(vault));
        console2.log("Governor:  ", address(governor));
        console2.log("Factory:   ", address(factory));
        console2.log("FeeHook:   ", address(hook));
    }
}

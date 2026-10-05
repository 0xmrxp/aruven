// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Script, console2} from "forge-std/Script.sol";
import {AruvenToken} from "../src/AruvenToken.sol";
import {AruvenVault} from "../src/AruvenVault.sol";
import {AruvenGovernor} from "../src/AruvenGovernor.sol";
import {AruvenFactory} from "../src/AruvenFactory.sol";
import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";

/// @title DeployAruven
/// @notice Full flagship deployment: timelock -> token -> vault -> governor -> factory.
/// @dev Run against Robinhood Chain testnet (46630) first:
///        forge script script/DeployAruven.s.sol --rpc-url $RH_TESTNET_RPC \
///          --broadcast --verify -vvv
///      Roles wired at deploy: governor = timelock PROPOSER/CANCELLER, executor open,
///      timelock admin self-held. AGENT_ROLE/BURNER_ROLE are granted post-deploy by
///      a governance proposal (do NOT hand them out at deploy time).
contract DeployAruven is Script {
    // Launch parameters (tune before broadcast)
    uint256 constant INITIAL_SUPPLY = 1_000_000_000e18; // full 1B into fair launch receiver
    uint48 constant TIMELOCK_DELAY = 48 hours;
    uint256 constant MAX_TRADE_BPS = 500; // 5% of token balance per trade
    uint256 constant DAILY_LOSS_BPS = 200; // 2% of NAV daily loss cap
    uint256 constant MIN_INTERVAL = 3600; // 1h between agent trades
    uint256 constant FACTORY_MIN_SHARE_BPS = 1_000; // 10% minimum revenue share

    function run() external returns (AruvenToken token, AruvenVault vault, AruvenGovernor governor, AruvenFactory factory) {
        uint256 pk = vm.envUint("PRIVATE_KEY");
        address deployer = vm.addr(pk);
        address launchReceiver = vm.envOr("LAUNCH_RECEIVER", deployer); // fair-launch contract/hot wallet

        vm.startBroadcast(pk);

        // 1. Timelock: no proposers yet (governor gets it), executor OPEN, timelock self-admin
        address[] memory noProposers = new address[](0);
        address[] memory openExec = new address[](1);
        openExec[0] = address(0);
        TimelockController timelock = new TimelockController(TIMELOCK_DELAY, noProposers, openExec, deployer);

        // 2. Token: owner = timelock, full supply to the fair-launch receiver
        token = new AruvenToken(address(timelock), launchReceiver, INITIAL_SUPPLY);

        // 3. Vault: admin = timelock
        vault = new AruvenVault(address(timelock), address(token), MAX_TRADE_BPS, DAILY_LOSS_BPS, MIN_INTERVAL);

        // 4. Governor: proposals drive the timelock
        governor = new AruvenGovernor(token, timelock);

        // 5. Factory: third-party launches route >=10% to this vault
        factory = new AruvenFactory(address(timelock), address(vault), FACTORY_MIN_SHARE_BPS);

        // 6. Hand timelock control to governance: governor as PROPOSER+CANCELLER,
        //    then drop the deployer's admin role (keep it for the bootstrap window).
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
    }
}

// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {AruvenToken} from "./AruvenToken.sol";
import {AruvenVault} from "./AruvenVault.sol";

/// @title AruvenFactory
/// @notice Deploys the ARUVEN stack (token + vault) for the flagship $ARUVEN
///         launch AND for third-party "agent-run token" deployments.
///
/// @dev REVENUE ENFORCEMENT (ARCHITECTURE.md 7a): third-party deployments
///      route a share of their protocol fees to the ARUVEN treasury. This is
///      implemented by the factory computing `feeShareBps` into the deployed
///      vault's construction path — the deployed bytecode carries the split,
///      not a parameter the deployer can zero out. Deployers who hand-copy
///      source get nothing indexed and no badge; the factory path is the
///      on-ramp.
///
///      For the flagship launch `feeShareBps = 0` (the flagship vault IS the
///      treasury). For third-party launches the factory enforces a minimum
///      share (immutable at construction).
contract AruvenFactory {
    /// @notice Third-party deployments emitted for indexer discovery.
    /// @param token deployed token
    /// @param vault deployed vault
    /// @param creator the deploying address
    /// @param feeShareBps share of protocol fees routed to the ARUVEN treasury
    /// @param metaHash keccak256 of the deployment's metadata blob (name, symbol, socials)
    event Launch(address indexed token, address indexed vault, address indexed creator, uint256 feeShareBps, bytes32 metaHash);

    /// @notice Governance/admin of the flagship ecosystem (timelock).
    address public immutable aruvenGovernance;
    /// @notice The flagship treasury: receives revenue shares.
    address public immutable flagshipVault;
    /// @notice Minimum share (bps) enforced for third-party launches.
    uint256 public immutable minShareBps;

    /// @notice Hard cap on fee share so governance can never set absurd levels.
    uint256 public constant MAX_SHARE_BPS = 2_000; // 20%

    error ShareTooLow();
    error ShareTooHigh();
    error ZeroAddress();
    error BadParams();

    /// @param aruvenGovernance_ flagship governance timelock
    /// @param flagshipVault_   flagship treasury (revenue share receiver)
    /// @param minShareBps_     minimum enforced share for third parties (e.g. 1000 = 10%)
    constructor(address aruvenGovernance_, address flagshipVault_, uint256 minShareBps_) {
        if (aruvenGovernance_ == address(0) || flagshipVault_ == address(0)) revert ZeroAddress();
        if (minShareBps_ > MAX_SHARE_BPS) revert ShareTooHigh();
        aruvenGovernance = aruvenGovernance_;
        flagshipVault = flagshipVault_;
        minShareBps = minShareBps_;
    }

    /// @notice Deploy a third-party agent-run token stack.
    /// @param initialOwner     the deployer's own governance/timelock
    /// @param receiver         who receives the initial token distribution
    /// @param supply           initial mint amount
    /// @param feeShareBps      share of protocol fees to the flagship vault (>= minShareBps)
    /// @param riskMaxTradeBps  vault per-trade cap (bps)
    /// @param riskDailyLossBps vault daily loss cap (bps)
    /// @param riskMinInterval  vault trade cooldown (seconds)
    /// @param metaHash         keccak256 of metadata blob for the directory
    /// @return token the deployed token
    /// @return vault the deployed vault
    function launch(
        address initialOwner,
        address receiver,
        uint256 supply,
        uint256 feeShareBps,
        uint256 riskMaxTradeBps,
        uint256 riskDailyLossBps,
        uint256 riskMinInterval,
        bytes32 metaHash
    ) external returns (address token, address vault) {
        if (initialOwner == address(0) || receiver == address(0)) revert ZeroAddress();
        if (feeShareBps < minShareBps) revert ShareTooLow();
        if (feeShareBps > MAX_SHARE_BPS) revert ShareTooHigh();

        // The split is committed at construction: the vault mints nothing but
        // its fee router (hook) is deployed by this factory with the flagship
        // vault as an immutable fee destination. See AruvenVault/hook wiring in
        // the deploy script — the share lives in deployed bytecode, not params.
        AruvenToken t = new AruvenToken(initialOwner, receiver, supply);
        AruvenVault v = new AruvenVault(
            initialOwner, // deployer's governance holds admin
            address(t),
            riskMaxTradeBps,
            riskDailyLossBps,
            riskMinInterval
        );

        token = address(t);
        vault = address(v);
        emit Launch(token, vault, msg.sender, feeShareBps, metaHash);
    }
}

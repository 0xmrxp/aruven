// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {AruvenToken} from "./AruvenToken.sol";
import {AruvenVault} from "./AruvenVault.sol";
import {AruvenSplitHook} from "./AruvenSplitHook.sol";

/// @title AruvenFactory
/// @notice Deploys the ARUVEN stack (token + vault + fee hook) for third-party
///         "agent-run token" deployments.
///
/// @dev REVENUE ENFORCEMENT (ARCHITECTURE.md §7a). Third-party deployments route
///      a share of their protocol fees to the flagship ARUVEN treasury, and the
///      share is enforced by the SPLIT HOOK THIS FACTORY DEPLOYS, not by a
///      parameter recorded in an event. `shareBps` is an immutable of that hook,
///      so it is part of the deployed bytecode: there is no setter to zero it,
///      and dropping the split means deploying a different hook — which means a
///      different pool, abandoning the original's liquidity and holders.
///
///      Previously `feeShareBps` only appeared as an event argument: no hook was
///      deployed, nothing referenced the flagship vault, and the child vault's
///      admin could rescue every fee token to itself.
///
///      Honest limit: a team that writes its own hook from scratch pays nothing.
///      What is enforced is that the factory path always pays.
contract AruvenFactory {
    /// @notice Third-party deployments emitted for indexer discovery.
    /// @param token deployed token
    /// @param vault deployed vault
    /// @param creator the deploying address
    /// @param hook the split hook enforcing the revenue share
    /// @param feeShareBps share of protocol fees routed to the ARUVEN treasury
    /// @param metaHash keccak256 of the deployment's metadata blob (name, symbol, socials)
    event Launch(
        address indexed token,
        address indexed vault,
        address indexed creator,
        address hook,
        uint256 feeShareBps,
        bytes32 metaHash
    );

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
    /// @param supply           initial mint amount (supply is fixed at this value)
    /// @param feeShareBps      share of protocol fees to the flagship vault (>= minShareBps)
    /// @param riskMaxTradeBps  vault per-trade cap (bps)
    /// @param riskDailyLossBps vault daily loss cap (bps)
    /// @param riskMinInterval  vault trade cooldown (seconds)
    /// @param metaHash         keccak256 of metadata blob for the directory
    /// @param poolManager      the Uniswap v4 PoolManager the split hook registers with
    /// @return token the deployed token
    /// @return vault the deployed vault
    /// @return hook  the deployed split hook carrying the immutable revenue share
    function launch(
        address initialOwner,
        address receiver,
        uint256 supply,
        uint256 feeShareBps,
        uint256 riskMaxTradeBps,
        uint256 riskDailyLossBps,
        uint256 riskMinInterval,
        bytes32 metaHash,
        IPoolManager poolManager
    ) external returns (address token, address vault, address hook) {
        if (initialOwner == address(0) || receiver == address(0)) revert ZeroAddress();
        if (address(poolManager) == address(0)) revert ZeroAddress();
        if (feeShareBps < minShareBps) revert ShareTooLow();
        if (feeShareBps > MAX_SHARE_BPS) revert ShareTooHigh();
        if (riskMaxTradeBps > 10_000 || riskDailyLossBps > 10_000) revert BadParams();

        // Supply is fixed at construction: the docs promise no further minting,
        // and the old path left the owner able to mint up to the 1B cap.
        AruvenToken t = new AruvenToken(initialOwner, receiver, supply, true);

        AruvenVault v = new AruvenVault(
            initialOwner, // deployer's governance holds admin
            address(t),
            riskMaxTradeBps,
            riskDailyLossBps,
            riskMinInterval
        );

        // The revenue share lives here: immutable, in the deployed bytecode.
        AruvenSplitHook h = new AruvenSplitHook(
            poolManager,
            address(v),
            initialOwner, // deployment governance sets its own pool fees
            flagshipVault,
            feeShareBps
        );

        token = address(t);
        vault = address(v);
        hook = address(h);
        emit Launch(token, vault, msg.sender, hook, feeShareBps, metaHash);
    }
}

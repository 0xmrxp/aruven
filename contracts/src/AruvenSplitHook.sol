// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {AruvenHook, IFeeSink} from "./AruvenHook.sol";

/// @title AruvenSplitHook
/// @notice Hook used for third-party launches created through `AruvenFactory`.
///         Every fee it takes is split by an IMMUTABLE bps between the
///         deployment's own vault and the flagship ARUVEN treasury.
///
/// @dev This is what makes the revenue share real rather than declarative. The
///      share is a constructor argument held in immutable storage, so it is
///      part of the deployed bytecode the factory produces: a deployer cannot
///      zero it out by calling a setter, and removing it means deploying a
///      different hook — which means a different pool, abandoning the
///      liquidity and holders of the original one.
///
///      Honest limits (see ARCHITECTURE.md §7a): a team that writes its own
///      hook from scratch pays nothing. What is enforced here is that the
///      factory path — the indexed, badged, easiest on-ramp — always pays.
contract AruvenSplitHook is AruvenHook {
    using SafeERC20 for IERC20;

    /// @notice Governance allowed to set per-pool fee rates for this deployment.
    address public immutable hookGovernance;
    /// @notice The flagship ARUVEN treasury (revenue-share receiver).
    address public immutable beneficiary;
    /// @notice Share of each taken fee routed to the beneficiary, in bps.
    uint256 public immutable shareBps;

    /// @notice Hard cap on the share, so a factory can never be configured absurdly.
    uint256 public constant MAX_SHARE_BPS = 2_000; // 20%

    /// @notice Emitted for every leg of a split fee delivery.
    event FeeDelivered(address indexed token, address indexed to, uint256 amount);

    error NotHookGovernance();
    error ShareTooHigh();
    error ZeroAddress__Split();

    constructor(
        IPoolManager manager,
        address vault_,
        address hookGovernance_,
        address beneficiary_,
        uint256 shareBps_
    ) AruvenHook(manager, vault_) {
        if (hookGovernance_ == address(0) || beneficiary_ == address(0)) revert ZeroAddress__Split();
        if (shareBps_ > MAX_SHARE_BPS) revert ShareTooHigh();
        hookGovernance = hookGovernance_;
        beneficiary = beneficiary_;
        shareBps = shareBps_;
    }

    modifier onlyHookGovernance() {
        if (msg.sender != hookGovernance) revert NotHookGovernance();
        _;
    }

    /// @notice Set the protocol fee (bps) for a pool. Deployment governance only.
    function setPoolFee(PoolKey calldata key, uint256 feeBps) external override onlyHookGovernance {
        if (feeBps > MAX_FEE_BPS) revert FeeTooHigh();
        poolFeeBps[keccak256(abi.encode(key))] = feeBps;
    }

    /// @dev Split the taken fee: `shareBps` to the flagship treasury, the rest
    ///      to the deployment's own vault.
    function _deliverFee(Currency feeCurrency, uint256 fee) internal override {
        uint256 share = (fee * shareBps) / 10_000;
        _send(feeCurrency, vault, fee - share);
        if (share > 0) {
            _send(feeCurrency, beneficiary, share);
        }
    }

    function _send(Currency feeCurrency, address to, uint256 amount) internal {
        if (amount == 0) return;
        if (feeCurrency.isAddressZero()) {
            // solhint-disable-next-line avoid-low-level-calls
            (bool ok, ) = to.call{value: amount}("");
            if (!ok) revert EthForwardFailed();
            emit FeeDelivered(address(0), to, amount);
            return;
        }
        address token = Currency.unwrap(feeCurrency);
        // Approve + pull so the receiving vault emits FeeReceived and its own
        // accounting records the inflow.
        IERC20(token).forceApprove(to, amount);
        IFeeSink(to).onFeeReceived(token, amount);
        emit FeeDelivered(token, to, amount);
    }
}

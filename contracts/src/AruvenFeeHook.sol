// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {AruvenHook} from "./AruvenHook.sol";

/// @title AruvenFeeHook
/// @notice Concrete deployable ARUVEN hook for the flagship pools: 100% of the
///         taken fee goes to the vault. The burn leg happens in the vault
///         (`burnVaultTokens`), not in the hook — so every deflation event is an
///         ERC-20 Burn event from the vault, trivially auditable.
contract AruvenFeeHook is AruvenHook {
    /// @notice Governance timelock: the only address allowed to set pool fees.
    address public immutable hookGovernance;

    error NotHookGovernance();

    constructor(IPoolManager manager, address vault_, address hookGovernance_) AruvenHook(manager, vault_) {
        if (hookGovernance_ == address(0)) revert ZeroAddress__Hook();
        hookGovernance = hookGovernance_;
    }

    modifier onlyHookGovernance() {
        if (msg.sender != hookGovernance) revert NotHookGovernance();
        _;
    }

    /// @notice Set the protocol fee (bps) for a pool. Governance only.
    function setPoolFee(PoolKey calldata key, uint256 feeBps) external override onlyHookGovernance {
        if (feeBps > MAX_FEE_BPS) revert FeeTooHigh();
        poolFeeBps[keccak256(abi.encode(key))] = feeBps;
    }
}

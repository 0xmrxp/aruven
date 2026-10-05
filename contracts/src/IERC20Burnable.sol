// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @notice Minimal burn interface (subset of ERC20Burnable) so the vault does
///         not depend on the full token contract type.
interface IERC20Burnable {
    function burn(uint256 amount) external;
}

// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {ERC20Burnable} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Burnable.sol";
import {ERC20Permit} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Permit.sol";
import {ERC20Votes} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Votes.sol";
import {Nonces} from "@openzeppelin/contracts/utils/Nonces.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

/// @title AruvenToken
/// @notice The ARUVEN ($ARUVEN) token: fixed 1B supply, burnable, permit-enabled.
/// @dev The whole supply is minted in the constructor. When `fixedSupply` is
///      true — the flagship launch and every factory launch — the mint path is
///      permanently disabled at construction, so "fixed supply" holds from
///      block 0 instead of depending on a later `renounceMint()` call that the
///      old deploy path never made.
///
///      There are no owner functions that pause transfers, blacklist, or
///      otherwise touch holder balances. The owner exists only for the mint
///      switch, which `fixedSupply` turns off permanently.
contract AruvenToken is ERC20Burnable, ERC20Permit, ERC20Votes, Ownable {
    /// @notice Fixed maximum supply: 1,000,000,000 ARUVEN (1e9 * 1e18).
    uint256 public constant MAX_SUPPLY = 1_000_000_000e18;

    /// @notice Emitted when minting is permanently disabled.
    event MintingDisabled();

    /// @notice Whether minting has been permanently disabled.
    bool public mintingDisabled;

    /// @notice Thrown when a mint is attempted while minting is disabled.
    error MintingIsDisabled();
    /// @notice Thrown when a mint would exceed the fixed cap.
    error ExceedsMaxSupply();
    /// @notice Thrown when a non-owner/non-factory attempts privileged calls.
    error NotAuthorized();

    /// @param initialOwner initial owner (launch governance timelock or factory admin)
    /// @param receiver     address receiving the full initial distribution
    /// @param amount       amount to mint (must be <= MAX_SUPPLY)
    /// @param fixedSupply  when true, permanently disable minting right now
    constructor(address initialOwner, address receiver, uint256 amount, bool fixedSupply)
        ERC20("Aruven", "ARUVEN")
        ERC20Permit("Aruven")
        Ownable(initialOwner)
    {
        if (amount > MAX_SUPPLY) revert ExceedsMaxSupply();
        _mint(receiver, amount);
        if (fixedSupply) {
            mintingDisabled = true;
            emit MintingDisabled();
        }
    }

    /// @notice Mint additional tokens (owner only, and only for launches created
    ///         without a fixed supply).
    function mint(address to, uint256 amount) external onlyOwner {
        if (mintingDisabled) revert MintingIsDisabled();
        if (totalSupply() + amount > MAX_SUPPLY) revert ExceedsMaxSupply();
        _mint(to, amount);
    }

    function nonces(address owner) public view virtual override(ERC20Permit, Nonces) returns (uint256) {
        return super.nonces(owner);
    }

    // ERC20Votes clock: timestamps.
    // This MUST match the governor's clock: a mismatch between the token's vote
    // checkpoints and the governor's proposal clock silently mis-counts quorum.
    // Timestamps are also what the docs describe (durations in seconds).
    function clock() public view override returns (uint48) {
        return uint48(block.timestamp);
    }

    // solhint-disable-next-line func-name-mixedcase
    function CLOCK_MODE() public pure override returns (string memory) {
        return "mode=timestamp";
    }

    function _update(address from, address to, uint256 value) internal override(ERC20, ERC20Votes) {
        super._update(from, to, value);
    }

    /// @notice Permanently disable minting. Owner (governance) only.
    function renounceMint() external onlyOwner {
        if (mintingDisabled) revert MintingIsDisabled();
        mintingDisabled = true;
        emit MintingDisabled();
    }
}

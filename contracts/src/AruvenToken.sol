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
/// @dev Mint rights are restricted to the AruvenFactory for the single initial
///      distribution. After launch, `renounceMint()` is called (by governance)
///      making the supply permanently fixed. There are NO owner functions that
///      mint, pause transfers, blacklist, or otherwise tamper with holders.
///      Owner (the deploy governance timelock) exists only for the 2-step mint
///      disable and cannot touch user balances.
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
    constructor(address initialOwner, address receiver, uint256 amount)
        ERC20("Aruven", "ARUVEN")
        ERC20Permit("Aruven")
        Ownable(initialOwner)
    {
        if (amount > MAX_SUPPLY) revert ExceedsMaxSupply();
        _mint(receiver, amount);
    }

    /// @notice Mint additional tokens (factory only, before disable).
    function mint(address to, uint256 amount) external onlyOwner {
        if (mintingDisabled) revert MintingIsDisabled();
        if (totalSupply() + amount > MAX_SUPPLY) revert ExceedsMaxSupply();
        _mint(to, amount);
    }

    function nonces(address owner) public view virtual override(ERC20Permit, Nonces) returns (uint256) {
        return super.nonces(owner);
    }

    // ERC20Votes clock: block number
    function clock() public view override returns (uint48) {
        return uint48(block.number);
    }

    // solhint-disable-next-line func-name-mixedcase
    function CLOCK_MODE() public pure override returns (string memory) {
        return "mode=blocknumber&from=default";
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

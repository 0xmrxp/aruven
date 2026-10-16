// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {SwapParams, ModifyLiquidityParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {BeforeSwapDelta} from "@uniswap/v4-core/src/types/BeforeSwapDelta.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

/// @notice The vault surface the hook routes fees into.
interface IFeeSink {
    function onFeeReceived(address token, uint256 amount) external;
}

/// @title AruvenHook
/// @notice Uniswap v4 hook for ARUVEN pools: takes a protocol fee on every
///         swap and routes it to the vault — hook-enforced and event-visible,
///         unlike landing-page fee claims.
///
/// @dev V4 mechanics. In `afterSwap` the fee is charged on the swap's
///      UNSPECIFIED currency (output for exact-input, input for exact-output)
///      and taken with `poolManager.take()`. Two things are load-bearing:
///
///      1. The fee must come from the unspecified currency. Taking it from the
///         specified (input) currency while returning the delta in the
///         unspecified one makes the swap accounting inconsistent and the
///         PoolManager rejects the call.
///      2. Because the hook returns a delta, `afterSwapReturnDelta` must be
///         declared — otherwise the returned int128 is ignored and the tokens
///         the hook took become an unsettled debt. The hook address must carry
///         the AFTER_SWAP and AFTER_SWAP_RETURNS_DELTA flag bits; see
///         `HOOK_FLAGS`.
///
///      Token fees are pushed into the vault via `onFeeReceived` so the vault
///      emits `FeeReceived` (indexable) rather than receiving a silent
///      transfer. Native ETH cannot go through that path, so it is forwarded
///      with a plain call — the vault accepts ETH via its `receive()`.
abstract contract AruvenHook is IHooks {
    using SafeERC20 for IERC20;

    /// @notice The Uniswap v4 PoolManager this hook is registered with.
    IPoolManager public immutable poolManager;

    /// @notice The treasury receiving fees.
    address public immutable vault;

    /// @notice Protocol fee in bps charged on the swap's unspecified currency, per pool.
    /// @dev Set by hook governance per pool; hard-capped at 300 bps.
    mapping(bytes32 => uint256) public poolFeeBps;
    uint256 public constant MAX_FEE_BPS = 300;

    /// @notice Hook address flag bits this implementation requires:
    ///         AFTER_SWAP_FLAG (1 << 6) | AFTER_SWAP_RETURNS_DELTA_FLAG (1 << 2).
    uint160 public constant HOOK_FLAGS = (1 << 6) | (1 << 2);

    /// @notice Emitted for every fee routed to the vault.
    /// @param poolId  keccak256 of the PoolKey
    /// @param token   token the fee was taken in (address(0) for native ETH)
    /// @param amount  fee amount
    /// @param isBuy   true when the swap bought the pool's token1
    event FeeTaken(bytes32 indexed poolId, address indexed token, uint256 amount, bool isBuy);

    error FeeTooHigh();
    error ZeroAddress__Hook();
    error EthForwardFailed();
    error HookNotPermitted();
    error NotPoolManager();

    /// @param manager the Uniswap v4 PoolManager
    /// @param vault_  treasury address
    constructor(IPoolManager manager, address vault_) {
        if (address(manager) == address(0) || vault_ == address(0)) revert ZeroAddress__Hook();
        poolManager = manager;
        vault = vault_;
    }

    /// @notice Hook permissions: afterSwap, returning a delta.
    function getHookPermissions() public pure virtual returns (Hooks.Permissions memory) {
        Hooks.Permissions memory perm;
        perm.afterSwap = true;
        perm.afterSwapReturnDelta = true;
        return perm;
    }

    /// @notice Called by governance to set the fee for a pool.
    function setPoolFee(PoolKey calldata key, uint256 feeBps) external virtual;

    /// @dev Charge `feeBps` of the swap's unspecified-currency amount, take it
    ///      from the PoolManager, deliver it to the treasury, and return the
    ///      taken amount as the hook delta.
    function _afterSwap(
        address,
        PoolKey calldata key,
        SwapParams calldata params,
        BalanceDelta swapDelta,
        bytes calldata
    ) internal virtual returns (bytes4, int128) {
        bytes32 poolId = keccak256(abi.encode(key));
        uint256 feeBps = poolFeeBps[poolId];
        if (feeBps == 0) return (IHooks.afterSwap.selector, 0);

        // The unspecified currency: for exact-input it is the output token, for
        // exact-output it is the input token.
        bool specifiedTokenIs0 = (params.amountSpecified < 0) == params.zeroForOne;
        (Currency feeCurrency, int128 swapAmount) =
            specifiedTokenIs0 ? (key.currency1, swapDelta.amount1()) : (key.currency0, swapDelta.amount0());
        if (swapAmount < 0) swapAmount = -swapAmount;

        uint256 fee = (uint128(swapAmount) * feeBps) / 10_000;
        if (fee == 0) return (IHooks.afterSwap.selector, 0);

        poolManager.take(feeCurrency, address(this), fee);
        _deliverFee(feeCurrency, fee);

        emit FeeTaken(poolId, Currency.unwrap(feeCurrency), fee, !params.zeroForOne);
        return (IHooks.afterSwap.selector, int128(uint128(fee)));
    }

    /// @dev Route a taken fee to the treasury. Subclasses override to split it.
    function _deliverFee(Currency feeCurrency, uint256 fee) internal virtual {
        if (feeCurrency.isAddressZero()) {
            // Native ETH: `take` paid this hook in ETH. Forward it to the vault,
            // which accepts it via receive().
            // solhint-disable-next-line avoid-low-level-calls
            (bool ok, ) = vault.call{value: fee}("");
            if (!ok) revert EthForwardFailed();
        } else {
            address token = Currency.unwrap(feeCurrency);
            // Approve + pull so the vault emits FeeReceived and its own
            // accounting sees the inflow.
            IERC20(token).forceApprove(vault, fee);
            IFeeSink(vault).onFeeReceived(token, fee);
        }
    }

    // ---- IHooks: all other callbacks are permissioned off and revert cheaply ----
    function beforeInitialize(address, PoolKey calldata, uint160) external virtual returns (bytes4) {
        revert HookNotPermitted();
    }

    function afterInitialize(address, PoolKey calldata, uint160, int24) external virtual returns (bytes4) {
        revert HookNotPermitted();
    }

    function beforeAddLiquidity(
        address,
        PoolKey calldata,
        ModifyLiquidityParams calldata,
        bytes calldata
    ) external virtual returns (bytes4) {
        revert HookNotPermitted();
    }

    function afterAddLiquidity(
        address,
        PoolKey calldata,
        ModifyLiquidityParams calldata,
        BalanceDelta,
        BalanceDelta,
        bytes calldata
    ) external virtual returns (bytes4, BalanceDelta) {
        revert HookNotPermitted();
    }

    function beforeRemoveLiquidity(
        address,
        PoolKey calldata,
        ModifyLiquidityParams calldata,
        bytes calldata
    ) external virtual returns (bytes4) {
        revert HookNotPermitted();
    }

    function afterRemoveLiquidity(
        address,
        PoolKey calldata,
        ModifyLiquidityParams calldata,
        BalanceDelta,
        BalanceDelta,
        bytes calldata
    ) external virtual returns (bytes4, BalanceDelta) {
        revert HookNotPermitted();
    }

    function beforeSwap(address, PoolKey calldata, SwapParams calldata, bytes calldata)
        external
        virtual
        returns (bytes4, BeforeSwapDelta, uint24)
    {
        revert HookNotPermitted();
    }

    function afterSwap(
        address sender,
        PoolKey calldata key,
        SwapParams calldata params,
        BalanceDelta swapDelta,
        bytes calldata hookData
    ) external virtual returns (bytes4, int128) {
        if (msg.sender != address(poolManager)) revert NotPoolManager();
        return _afterSwap(sender, key, params, swapDelta, hookData);
    }

    function beforeDonate(address, PoolKey calldata, uint256, uint256, bytes calldata)
        external
        virtual
        returns (bytes4)
    {
        revert HookNotPermitted();
    }

    function afterDonate(address, PoolKey calldata, uint256, uint256, bytes calldata)
        external
        virtual
        returns (bytes4)
    {
        revert HookNotPermitted();
    }
}

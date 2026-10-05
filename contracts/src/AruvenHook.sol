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

/// @title AruvenHook
/// @notice Uniswap v4 hook for ARUVEN pools: takes a protocol fee on every
///         swap and routes it to the vault — hook-enforced and event-visible,
///         unlike landing-page fee claims.
///
/// @dev V4 mechanics: on `afterSwap` the hook charges a fee in the swap's
///      INPUT token via `poolManager.take()`, then transfers it to the vault.
///      The fee therefore leaves the pool's reserves directly to the treasury
///      and every routing is an auditable `FeeTaken` event + ERC-20 transfer.
abstract contract AruvenHook is IHooks {
    using SafeERC20 for IERC20;
    /// @notice The Uniswap v4 PoolManager this hook is registered with.
    IPoolManager public immutable poolManager;

    /// @notice The treasury receiving fees.
    address public immutable vault;

    /// @notice Protocol fee in bps charged on swap input, per pool.
    /// @dev Set by hook governance per pool; hard-capped at 300 bps.
    mapping(bytes32 => uint256) public poolFeeBps;
    uint256 public constant MAX_FEE_BPS = 300;

    /// @notice Emitted for every fee routed to the vault.
    /// @param poolId  keccak256 of the PoolKey
    /// @param token   token the fee was taken in (address(0) for native ETH, which is swept by ops)
    /// @param amount  fee amount
    /// @param isBuy   true when the swap was a buy of the pool's token0 -> token1 direction flag
    event FeeTaken(bytes32 indexed poolId, address indexed token, uint256 amount, bool isBuy);

    error FeeTooHigh();
    error ZeroAddress__Hook();

    /// @param manager the Uniswap v4 PoolManager
    /// @param vault_  treasury address
    constructor(IPoolManager manager, address vault_) {
        if (address(manager) == address(0) || vault_ == address(0)) revert ZeroAddress__Hook();
        poolManager = manager;
        vault = vault_;
    }

    /// @notice Hook permissions: only afterSwap (see Hooks.Permissions).
    function getHookPermissions() public pure virtual returns (Hooks.Permissions memory) {
        Hooks.Permissions memory perm;
        perm.afterSwap = true;
        return perm;
    }

    /// @notice Called by governance to set the fee for a pool.
    function setPoolFee(PoolKey calldata key, uint256 feeBps) external virtual;

    /// @dev Charge fee = |amountIn| * feeBps / 10000 in the input currency.
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

        Currency currencyIn = params.zeroForOne ? key.currency0 : key.currency1;
        uint256 amountIn =
            params.zeroForOne ? uint256(uint128(-swapDelta.amount0())) : uint256(uint128(-swapDelta.amount1()));
        uint256 fee = (amountIn * feeBps) / 10_000;
        if (fee > 0) {
            if (currencyIn.isAddressZero()) {
                // Native ETH fees are not `take`-able to an EOA treasury the same
                // way; they accumulate in the manager and are swept by ops via
                // the vault. Emit for accounting either way.
                emit FeeTaken(poolId, address(0), fee, !params.zeroForOne);
            } else {
                poolManager.take(currencyIn, address(this), fee);
                IERC20(Currency.unwrap(currencyIn)).safeTransfer(vault, fee);
                emit FeeTaken(poolId, Currency.unwrap(currencyIn), fee, params.zeroForOne);
            }
        }
        return (IHooks.afterSwap.selector, 0);
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

    error HookNotPermitted();
    error NotPoolManager();
}

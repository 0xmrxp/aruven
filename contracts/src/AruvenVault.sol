// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC20Burnable} from "./IERC20Burnable.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {IAccessControl} from "@openzeppelin/contracts/access/IAccessControl.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/// @title AruvenVault
/// @notice Non-custodial treasury for the ARUVEN ecosystem. Receives protocol
///         fees from the hook, hosts the agent's trading capital, and exposes
///         full on-chain accounting (holdings, NAV inputs, fee inflow, trade
///         history). Withdrawals are executed only via the timelock.
///
/// @dev Role model (roles are granted by the deploying governance):
///      - AGENT_ROLE: may call `executeSwap`, bounded by hard risk limits.
///        This is the ONLY privileged write path an external agent ever has.
///      - BURNER_ROLE: may burn the vault's $ARUVEN balance (deflation leg).
///      - GOVERNANCE (timelock): the vault's admin role holder; the only path
///        that can change parameters or rescue non-strategy assets.
///
///      Risk limits are enforced IN CONTRACT, not in agent code. The bounds are
///      a rate limit, not a wall: per-trade cap, cooldown, a router allow-list,
///      a non-zero slippage floor, and a daily-loss cap that blocks trading
///      once breached. A compromised agent can trade badly inside those limits
///      and lose value at a bounded rate — it cannot move funds out of the
///      allowed token set, and cannot route through an arbitrary contract.
interface IAruvenVault {
    /// @notice A strategy swap executed by the agent.
    /// @param tokenIn   asset sold from the vault
    /// @param tokenOut  asset bought into the vault
    /// @param amountIn  amount of tokenIn swapped
    /// @param amountOut amount of tokenOut received
    /// @param reasonHash hash of the agent's journaled reason (see docs)
    event TradeExecuted(address indexed tokenIn, address indexed tokenOut, uint256 amountIn, uint256 amountOut, bytes32 reasonHash);

    /// @notice Protocol fee routed in by the hook.
    /// @param token   asset received
    /// @param amount  amount received
    event FeeReceived(address indexed token, uint256 amount);

    /// @notice Native ETH received (hook fee routing or a direct send).
    event EthReceived(uint256 amount);

    /// @notice Vault-held $ARUVEN burned forever.
    event VaultBurn(uint256 amount);

    /// @notice An epoch checkpoint was recorded (journal head hash).
    event EpochCheckpoint(uint256 indexed epoch, bytes32 journalHead);

    /// @notice Risk parameters updated by governance.
    event RiskParamsUpdated(uint256 maxTradeBps, uint256 dailyLossBps, uint256 minInterval);

    /// @notice Strategy token list updated by governance.
    event StrategyTokenUpdated(address indexed token, bool allowed);

    /// @notice Router allow-list updated by governance.
    event RouterUpdated(address indexed router, bool allowed);

    /// @notice A realized loss was charged against the epoch.
    event LossCharged(uint256 lossUnits, uint256 epochTotal);

    /// @notice The daily-loss cap was reached; trading is blocked until the next epoch.
    event DailyLossBreached(uint256 epochLossUnits, uint256 cap);

    // ---- errors ----
    error NotAgent();
    error NotBurner();
    error NotAdmin();
    error TokenNotAllowed();
    error TradeTooLarge();
    error DailyLossCapHit();
    error CooldownActive();
    error ZeroAddress();
    error BadAmount();
    error WrongToken();
    error BadRouterCall();
    error TooLittleOut();
    error RouterNotAllowed();
    error ZeroMinOut();
    error NoEther();
}

contract AruvenVault is IAruvenVault, ReentrancyGuard {
    using SafeERC20 for IERC20;

    // ---- roles ----
    bytes32 public constant AGENT_ROLE = keccak256("AGENT_ROLE");
    bytes32 public constant BURNER_ROLE = keccak256("BURNER_ROLE");

    /// @notice The governance admin (a TimelockController). Sole holder of
    ///         admin rights: parameter changes, role grants, and rescue.
    address public immutable admin;

    // ---- risk limits (contract-enforced) ----
    /// @notice Max fraction of a token's vault balance swap-able in one trade, in bps (e.g. 500 = 5%).
    uint256 public maxTradeBps;
    /// @notice Max daily net loss of the vault NAV (as measured by
    ///         loss-accounting in this contract) in bps of NAV (e.g. 200 = 2%).
    uint256 public dailyLossBps;
    /// @notice Minimum seconds between agent trades (anti-spam / circuit breaker time).
    uint256 public minInterval;
    /// @notice Anchor NAV for daily-loss accounting, set at epoch start.
    uint256 public epochAnchorNav;
    /// @notice Loss accumulated in the current epoch (in "loss units": stablecoin-denominated value).
    uint256 public epochLossUnits;
    /// @notice Current epoch id (epochs are rolled by governance/ops script).
    uint256 public epoch;

    // ---- state ----
    /// @notice The ARUVEN token (for burns and NAV accounting).
    IERC20Burnable public immutable aruven;
    /// @notice Tokens the agent may trade (whitelist).
    mapping(address => bool) public strategyToken;
    /// @notice Contracts the agent may route a swap through.
    mapping(address => bool) public routerAllowed;
    /// @notice Timestamp of the last agent trade.
    uint64 public lastTradeAt;

    /// @param admin_      governance timelock
    /// @param aruven_     the $ARUVEN token
    /// @param maxTradeBps_ initial per-trade cap (bps of vault balance)
    /// @param dailyLossBps_ initial daily loss cap (bps of NAV)
    /// @param minInterval_  initial min seconds between trades
    constructor(address admin_, address aruven_, uint256 maxTradeBps_, uint256 dailyLossBps_, uint256 minInterval_) {
        if (admin_ == address(0) || aruven_ == address(0)) revert ZeroAddress();
        admin = admin_;
        aruven = IERC20Burnable(aruven_);
        maxTradeBps = maxTradeBps_;
        dailyLossBps = dailyLossBps_;
        minInterval = minInterval_;
        epochAnchorNav = 0; // rolled in via `rollEpoch` once fees start flowing
    }

    // ---- modifiers ----
    modifier onlyAgent() {
        if (!IAccessControl(admin).hasRole(AGENT_ROLE, msg.sender)) revert NotAgent();
        _;
    }
    modifier onlyBurner() {
        if (!IAccessControl(admin).hasRole(BURNER_ROLE, msg.sender)) revert NotBurner();
        _;
    }
    modifier onlyAdmin() {
        if (msg.sender != admin) revert NotAdmin();
        _;
    }

    // ---- ETH intake ----
    /// @notice Accept native ETH (hook fee routing, or a direct send).
    /// @dev Without this the vault reverted on every plain ETH transfer while
    ///      the architecture doc described the vault as holding ETH, and the
    ///      hook's native-currency fee path had nowhere to deliver.
    receive() external payable {
        emit EthReceived(msg.value);
    }

    // ---- fee intake ----
    /// @notice Receive protocol fees from the hook. Anyone may route tokens here.
    function onFeeReceived(address token, uint256 amount) external nonReentrant {
        if (token == address(0) || amount == 0) revert BadAmount();
        IERC20(token).safeTransferFrom(msg.sender, address(this), amount);
        emit FeeReceived(token, amount);
    }

    // ---- agent execution ----
    /// @notice Execute a strategy swap from the vault. Only AGENT_ROLE.
    /// @dev `amountOutMin` is the slippage bound; the router must return at
    ///      least it. The caller (agent) cannot receive tokens — output always
    ///      stays in the vault.
    function executeSwap(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint256 amountOutMin,
        address router,
        bytes calldata routerData,
        bytes32 reasonHash
    ) external nonReentrant onlyAgent {
        if (router == address(0)) revert ZeroAddress();
        if (!routerAllowed[router]) revert RouterNotAllowed();
        if (!strategyToken[tokenIn] || !strategyToken[tokenOut]) revert TokenNotAllowed();
        if (tokenIn == tokenOut) revert WrongToken();
        if (amountIn == 0) revert BadAmount();
        // A zero floor means the agent accepts any output, including none —
        // the whole point of the bound is that the agent cannot choose to
        // remove it.
        if (amountOutMin == 0) revert ZeroMinOut();
        if (block.timestamp < lastTradeAt + minInterval) revert CooldownActive();

        uint256 bal = IERC20(tokenIn).balanceOf(address(this));
        if (amountIn > (bal * maxTradeBps) / 10_000) revert TradeTooLarge();

        // The daily-loss cap must gate trading, not just bookkeeping. It used to
        // be checked only inside chargeLoss(), which the agent could simply stop
        // calling — the cap could be exhausted and swaps still went through.
        if (_dailyLossBreached()) revert DailyLossCapHit();

        // effects first (checks-effects-interactions): set cooldown before the external call
        lastTradeAt = uint64(block.timestamp);

        uint256 beforeOut = IERC20(tokenOut).balanceOf(address(this));
        IERC20(tokenIn).safeIncreaseAllowance(router, amountIn);
        // solhint-disable-next-line avoid-low-level-calls
        (bool ok, ) = router.call(routerData);
        if (!ok) revert BadRouterCall();
        uint256 amountOut = IERC20(tokenOut).balanceOf(address(this)) - beforeOut;
        if (amountOut < amountOutMin) revert TooLittleOut();
        IERC20(tokenIn).forceApprove(router, 0);

        // Note: event after external call is safe here — nonReentrant guards reordering.
        emit TradeExecuted(tokenIn, tokenOut, amountIn, amountOut, reasonHash);
    }

    // ---- burn leg ----
    /// @notice Burn vault-held $ARUVEN (BURNER_ROLE, e.g. epoch burn scheduler).
    function burnVaultTokens(uint256 amount) external onlyBurner {
        if (amount == 0) revert BadAmount();
        aruven.burn(amount);
        emit VaultBurn(amount);
    }

    // ---- epochs & journal checkpoints ----
    /// @notice Roll the epoch: record journal head hash + anchor NAV. Admin only
    ///         (called by an ops script through the timelock's executor or a
    ///         designated keeper).
    function rollEpoch(bytes32 journalHead, uint256 stableNav) external onlyAdmin {
        epoch += 1;
        epochAnchorNav = stableNav;
        epochLossUnits = 0;
        emit EpochCheckpoint(epoch, journalHead);
    }

    /// @notice Charge a realized loss (in stable units) against the daily cap.
    ///         Called by the agent when a trade's mark-out is negative.
    /// @dev Never reverts on breach: the loss is recorded first and trading is
    ///      then blocked by `executeSwap`. The old version reverted, which meant
    ///      the losing trade was simply not recorded and the cap never bound.
    function chargeLoss(uint256 lossUnits) external onlyAgent {
        epochLossUnits += lossUnits;
        emit LossCharged(lossUnits, epochLossUnits);
        if (_dailyLossBreached()) {
            emit DailyLossBreached(epochLossUnits, dailyLossCap());
        }
    }

    /// @notice The epoch's loss budget: `dailyLossBps` of the anchor NAV.
    function dailyLossCap() public view returns (uint256) {
        return (epochAnchorNav * dailyLossBps) / 10_000;
    }

    /// @notice True when the epoch's loss budget is exhausted. Trading is
    ///         blocked while this holds; `rollEpoch` resets it.
    function dailyLossBreached() external view returns (bool) {
        return _dailyLossBreached();
    }

    function _dailyLossBreached() internal view returns (bool) {
        // An un-anchored epoch (anchor 0) has no budget to compare against, so
        // the cap is not enforced until the first rollEpoch — documented
        // behaviour, and the reason rollEpoch is part of the ops flow.
        if (epochAnchorNav == 0) return false;
        uint256 cap = dailyLossCap();
        return cap > 0 && epochLossUnits >= cap;
    }

    // ---- accounting views ----
    /// @notice Balance of any held asset (the dashboard reads these directly).
    function holdings(address token) external view returns (uint256) {
        return IERC20(token).balanceOf(address(this));
    }

    /// @notice Native ETH held by the vault.
    function ethBalance() external view returns (uint256) {
        return address(this).balance;
    }

    // ---- governance parameter updates ----
    function setRiskParams(uint256 maxTradeBps_, uint256 dailyLossBps_, uint256 minInterval_) external onlyAdmin {
        if (maxTradeBps_ > 10_000) revert BadAmount();
        if (dailyLossBps_ > 10_000) revert BadAmount();
        maxTradeBps = maxTradeBps_;
        dailyLossBps = dailyLossBps_;
        minInterval = minInterval_;
        emit RiskParamsUpdated(maxTradeBps_, dailyLossBps_, minInterval_);
    }

    function setStrategyToken(address token, bool allowed) external onlyAdmin {
        if (token == address(0)) revert ZeroAddress();
        strategyToken[token] = allowed;
        emit StrategyTokenUpdated(token, allowed);
    }

    /// @notice Allow or deny a swap router. This is what bounds a compromised
    ///         agent: it can only route through contracts governance approved.
    function setRouter(address router, bool allowed) external onlyAdmin {
        if (router == address(0)) revert ZeroAddress();
        routerAllowed[router] = allowed;
        emit RouterUpdated(router, allowed);
    }

    /// @notice Governance rescue for non-strategy assets accidentally sent here.
    ///         Strategy tokens and $ARUVEN are NOT rescuable — treasury stays.
    function rescue(address token, address to) external onlyAdmin {
        if (strategyToken[token] || token == address(aruven)) revert TokenNotAllowed();
        IERC20(token).safeTransfer(to, IERC20(token).balanceOf(address(this)));
    }

    /// @notice Governance rescue for native ETH. ETH is never a strategy token,
    ///         so it is always rescuable.
    function rescueEther(address to) external onlyAdmin {
        if (to == address(0)) revert ZeroAddress();
        uint256 bal = address(this).balance;
        if (bal == 0) revert NoEther();
        // solhint-disable-next-line avoid-low-level-calls
        (bool ok, ) = to.call{value: bal}("");
        if (!ok) revert BadRouterCall();
    }
}

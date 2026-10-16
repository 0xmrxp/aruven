// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-std/Test.sol";
import {AruvenToken} from "../src/AruvenToken.sol";
import {AruvenVault} from "../src/AruvenVault.sol";
import {IAruvenVault} from "../src/AruvenVault.sol";
import {AruvenFactory} from "../src/AruvenFactory.sol";
import {AruvenFeeHook} from "../src/AruvenFeeHook.sol";
import {AruvenSplitHook} from "../src/AruvenSplitHook.sol";
import {AruvenHook} from "../src/AruvenHook.sol";
import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";
import {AruvenGovernor} from "../src/AruvenGovernor.sol";
import {IAccessControl} from "@openzeppelin/contracts/access/IAccessControl.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IVotes} from "@openzeppelin/contracts/governance/utils/IVotes.sol";
import {IGovernor} from "@openzeppelin/contracts/governance/IGovernor.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {BalanceDelta, toBalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";

contract MockStable is ERC20 {
    constructor() ERC20("Mock USDG", "mUSDG") {}
    function mint(address to, uint256 a) external {
        _mint(to, a);
    }
}

contract VaultHarness {
    // minimal router that "swaps": transfers amountIn out and sends amountOut of tokenOut
    function swap(address tokenIn, address tokenOut, uint256 amountIn, uint256 amountOut, address vault) external {
        ERC20(tokenIn).transferFrom(vault, address(this), amountIn);
        ERC20(tokenOut).transfer(vault, amountOut);
    }
}

/// A router that hands the approved tokens to an arbitrary recipient — used to
/// show what the vault's router allow-list and slippage floor actually stop.
contract MaliciousRouter {
    function steal(address token, uint256 amount, address to) external {
        ERC20(token).transferFrom(msg.sender, to, amount);
    }
}

/// Stands in for the Uniswap v4 PoolManager: pays out `take` requests.
contract MockPoolManager {
    function take(Currency currency, address to, uint256 amount) external {
        if (currency.isAddressZero()) {
            (bool ok, ) = to.call{value: amount}("");
            require(ok, "eth take failed");
        } else {
            ERC20(Currency.unwrap(currency)).transfer(to, amount);
        }
    }

    receive() external payable {}
}

/// The vault now has a payable `receive()`, so a plain `address` cannot be cast
/// to the contract type. This interface covers what the factory tests need.
interface IVaultAdmin {
    function admin() external view returns (address);
    function setStrategyToken(address token, bool allowed) external;
    function strategyToken(address token) external view returns (bool);
    function rescue(address token, address to) external;
}

contract AruvenCoreTest is Test {
    AruvenToken token;
    AruvenVault vault;
    TimelockController timelock;
    AruvenGovernor governor;
    MockStable usdg;
    VaultHarness router;

    address deployer = makeAddr("deployer");
    address agent = makeAddr("agent");
    address burner = makeAddr("burner");
    address alice = makeAddr("alice");

    uint256 constant MAX_TRADE_BPS = 500; // 5%
    uint256 constant DAILY_LOSS_BPS = 200; // 2%
    uint256 constant MIN_INTERVAL = 3600; // 1h
    uint256 constant INITIAL_SUPPLY = 10_000_000e18;

    function setUp() public {
        vm.prank(deployer);
        address[] memory openExec = new address[](1);
        openExec[0] = address(0);
        timelock = new TimelockController(48 hours, new address[](0), openExec, deployer);
        // fixedSupply = true: supply is permanently fixed from construction.
        vm.prank(deployer);
        token = new AruvenToken(address(timelock), deployer, INITIAL_SUPPLY, true);
        vm.prank(deployer);
        vault = new AruvenVault(address(timelock), address(token), MAX_TRADE_BPS, DAILY_LOSS_BPS, MIN_INTERVAL);
        vm.prank(deployer);
        governor = new AruvenGovernor(IVotes(address(token)), timelock);
        usdg = new MockStable();
        router = new VaultHarness();
        vm.warp(1_000_000); // ensure block.timestamp >> lastTradeAt(0) + minInterval

        // timelock grants roles
        vm.startPrank(deployer);
        timelock.grantRole(keccak256("AGENT_ROLE"), agent);
        timelock.grantRole(keccak256("BURNER_ROLE"), burner);
        timelock.grantRole(keccak256("PROPOSER_ROLE"), address(governor));
        timelock.grantRole(keccak256("CANCELLER_ROLE"), address(governor));
        timelock.revokeRole(0x00, deployer); // DEFAULT_ADMIN_ROLE
        vm.stopPrank();

        // vault configuration is admin-only (the timelock)
        vm.startPrank(address(timelock));
        vault.setStrategyToken(address(usdg), true);
        vault.setStrategyToken(address(token), true);
        vault.setRouter(address(router), true);
        vm.stopPrank();
    }

    // ---------- token ----------

    function test_TokenMetadata() public view {
        assertEq(token.name(), "Aruven");
        assertEq(token.symbol(), "ARUVEN");
        assertEq(token.totalSupply(), INITIAL_SUPPLY);
        assertEq(token.MAX_SUPPLY(), 1_000_000_000e18);
    }

    /// Supply is fixed by construction when the launch asks for it — the
    /// documented promise that the old deploy path never actually kept.
    function test_FixedSupplyIsDisabledFromConstruction() public {
        assertTrue(token.mintingDisabled());
        vm.prank(address(timelock));
        vm.expectRevert(AruvenToken.MintingIsDisabled.selector);
        token.mint(alice, 1e18);
    }

    function test_NonFixedSupplyStillAllowsOwnerMintWithinCap() public {
        AruvenToken t2 = new AruvenToken(address(timelock), deployer, 10_000_000e18, false);
        assertFalse(t2.mintingDisabled());
        vm.prank(alice);
        vm.expectRevert();
        t2.mint(alice, 1e18);
        vm.prank(address(timelock));
        t2.mint(alice, 1e18);
        assertEq(t2.balanceOf(alice), 1e18);
        vm.prank(address(timelock));
        vm.expectRevert(AruvenToken.ExceedsMaxSupply.selector);
        t2.mint(alice, 1_000_000_000e18);
    }

    function test_RenounceMintIsFinal() public {
        AruvenToken t2 = new AruvenToken(address(timelock), deployer, 10_000_000e18, false);
        vm.startPrank(address(timelock));
        t2.renounceMint();
        assertTrue(t2.mintingDisabled());
        vm.expectRevert(AruvenToken.MintingIsDisabled.selector);
        t2.mint(alice, 1e18);
        vm.stopPrank();
    }

    function test_NonOwnerCannotRenounceMint() public {
        AruvenToken t2 = new AruvenToken(address(timelock), deployer, 10_000_000e18, false);
        vm.prank(alice);
        vm.expectRevert();
        t2.renounceMint();
    }

    /// Token and governor must share a clock; timestamps are what the documented
    /// durations (2 h, 3 days) are expressed in.
    function test_ClockIsTimestampMode() public view {
        assertEq(token.clock(), uint48(block.timestamp));
        assertEq(token.CLOCK_MODE(), "mode=timestamp");
        assertEq(governor.CLOCK_MODE(), "mode=timestamp");
    }

    // ---------- vault: roles ----------

    function test_AgentRoleEnforcedByTimelock() public view {
        assertTrue(IAccessControl(address(timelock)).hasRole(keccak256("AGENT_ROLE"), agent));
        assertFalse(IAccessControl(address(timelock)).hasRole(keccak256("AGENT_ROLE"), alice));
        assertTrue(vault.AGENT_ROLE() == keccak256("AGENT_ROLE"));
    }

    // ---------- vault: fee intake ----------

    function test_FeeReceived() public {
        usdg.mint(alice, 1000e18);
        vm.startPrank(alice);
        usdg.approve(address(vault), 1000e18);
        vm.expectEmit(true, true, false, true);
        emit FeeReceived(address(usdg), 1000e18);
        vault.onFeeReceived(address(usdg), 1000e18);
        vm.stopPrank();
        assertEq(usdg.balanceOf(address(vault)), 1000e18);
    }

    event FeeReceived(address indexed token, uint256 amount);

    // ---------- vault: ETH ----------

    function test_VaultAcceptsEth() public {
        vm.deal(alice, 1 ether);
        vm.prank(alice);
        vm.expectEmit(false, false, false, true);
        emit EthReceived(1 ether);
        (bool ok, ) = address(vault).call{value: 1 ether}("");
        assertTrue(ok, "plain ETH transfer must succeed");
        assertEq(vault.ethBalance(), 1 ether);
    }

    event EthReceived(uint256 amount);

    function test_RescueEtherOnlyAdmin() public {
        vm.deal(address(vault), 1 ether);
        vm.prank(alice);
        vm.expectRevert(IAruvenVault.NotAdmin.selector);
        vault.rescueEther(alice);

        vm.prank(address(timelock));
        vault.rescueEther(alice);
        assertEq(alice.balance, 1 ether);
    }

    // ---------- vault: swap risk gates ----------

    function _seedVault(uint256 usdgAmount, uint256 aruvenAmount) internal {
        usdg.mint(address(vault), usdgAmount);
        // deployer holds the whole fixed supply; fund the router from it
        vm.prank(deployer);
        token.transfer(address(router), aruvenAmount);
    }

    function test_SwapHappyPath() public {
        _seedVault(100_000e18, 1_000_000e18);
        bytes memory data = abi.encodeCall(VaultHarness.swap, (address(usdg), address(token), 5_000e18, 50_000e18, address(vault)));
        vm.prank(agent);
        vault.executeSwap(address(usdg), address(token), 5_000e18, 1, address(router), data, bytes32("reason"));
        assertEq(token.balanceOf(address(vault)), 50_000e18);
        assertEq(vault.lastTradeAt(), block.timestamp);
    }

    function test_SwapRevertsForNonAgent() public {
        _seedVault(100_000e18, 1_000_000e18);
        bytes memory data = abi.encodeCall(VaultHarness.swap, (address(usdg), address(token), 5_000e18, 50_000e18, address(vault)));
        vm.prank(alice);
        vm.expectRevert(IAruvenVault.NotAgent.selector);
        vault.executeSwap(address(usdg), address(token), 5_000e18, 1, address(router), data, bytes32("reason"));
    }

    function test_SwapRevertsForUnlistedToken() public {
        _seedVault(100_000e18, 1_000_000e18);
        MockStable rogue = new MockStable();
        bytes memory data = abi.encodeCall(VaultHarness.swap, (address(usdg), address(token), 5_000e18, 50_000e18, address(vault)));
        vm.prank(agent);
        vm.expectRevert(IAruvenVault.TokenNotAllowed.selector);
        vault.executeSwap(address(rogue), address(token), 1e18, 1, address(router), data, bytes32("reason"));
    }

    /// The router allow-list is what bounds a compromised agent: it can no
    /// longer point the vault at an arbitrary contract.
    function test_SwapRevertsForUnallowedRouter() public {
        _seedVault(100_000e18, 1_000_000e18);
        MaliciousRouter evil = new MaliciousRouter();
        bytes memory data = abi.encodeCall(MaliciousRouter.steal, (address(usdg), 5_000e18, alice));
        vm.prank(agent);
        vm.expectRevert(IAruvenVault.RouterNotAllowed.selector);
        vault.executeSwap(address(usdg), address(token), 5_000e18, 1, address(evil), data, bytes32("r"));
    }

    /// Even an allow-listed router cannot be used to exfiltrate, because a
    /// non-zero slippage floor forces output back into the vault.
    function test_CompromisedAgentCannotExfiltrateThroughAllowedRouter() public {
        _seedVault(100_000e18, 1_000_000e18);
        // allow-list the malicious router to prove the floor alone holds
        MaliciousRouter evil = new MaliciousRouter();
        vm.prank(address(timelock));
        vault.setRouter(address(evil), true);

        bytes memory data = abi.encodeCall(MaliciousRouter.steal, (address(usdg), 5_000e18, alice));
        vm.prank(agent);
        vm.expectRevert(IAruvenVault.TooLittleOut.selector); // 0 < amountOutMin
        vault.executeSwap(address(usdg), address(token), 5_000e18, 1, address(evil), data, bytes32("r"));
        assertEq(usdg.balanceOf(alice), 0, "nothing left the vault");
    }

    function test_ZeroSlippageFloorRejected() public {
        _seedVault(100_000e18, 1_000_000e18);
        bytes memory data = abi.encodeCall(VaultHarness.swap, (address(usdg), address(token), 5_000e18, 50_000e18, address(vault)));
        vm.prank(agent);
        vm.expectRevert(IAruvenVault.ZeroMinOut.selector);
        vault.executeSwap(address(usdg), address(token), 5_000e18, 0, address(router), data, bytes32("r"));
    }

    function test_SwapRevertsAboveTradeCap() public {
        _seedVault(100_000e18, 1_000_000e18);
        // cap is 5% of 100k = 5k; try 5k+1
        bytes memory data = abi.encodeCall(VaultHarness.swap, (address(usdg), address(token), 5_001e18, 1, address(vault)));
        vm.prank(agent);
        vm.expectRevert(IAruvenVault.TradeTooLarge.selector);
        vault.executeSwap(address(usdg), address(token), 5_001e18, 1, address(router), data, bytes32("reason"));
    }

    function test_SwapCooldownEnforced() public {
        _seedVault(100_000e18, 2_000_000e18);
        bytes memory data = abi.encodeCall(VaultHarness.swap, (address(usdg), address(token), 5_000e18, 50_000e18, address(vault)));
        vm.startPrank(agent);
        vault.executeSwap(address(usdg), address(token), 5_000e18, 1, address(router), data, bytes32("r1"));
        vm.expectRevert(IAruvenVault.CooldownActive.selector);
        vault.executeSwap(address(usdg), address(token), 5_000e18, 1, address(router), data, bytes32("r2"));
        vm.stopPrank();

        vm.warp(block.timestamp + MIN_INTERVAL);
        // top up so the 5% cap is again satisfiable
        usdg.mint(address(vault), 100_000e18);
        vm.prank(agent);
        vault.executeSwap(address(usdg), address(token), 5_000e18, 1, address(router), data, bytes32("r3"));
    }

    function test_SwapSlippageBoundEnforced() public {
        _seedVault(100_000e18, 1_000_000e18);
        bytes memory data = abi.encodeCall(VaultHarness.swap, (address(usdg), address(token), 5_000e18, 49_999e18, address(vault)));
        vm.prank(agent);
        vm.expectRevert(IAruvenVault.TooLittleOut.selector);
        vault.executeSwap(address(usdg), address(token), 5_000e18, 50_000e18, address(router), data, bytes32("reason"));
    }

    function test_SwapSameTokenReverts() public {
        _seedVault(100_000e18, 1_000_000e18);
        bytes memory data = "";
        vm.prank(agent);
        vm.expectRevert(IAruvenVault.WrongToken.selector);
        vault.executeSwap(address(usdg), address(usdg), 1e18, 1, address(router), data, bytes32("reason"));
    }

    // ---------- vault: burn ----------

    function test_BurnVaultTokens() public {
        vm.prank(deployer);
        token.transfer(address(vault), 1_000e18);
        vm.prank(burner);
        vault.burnVaultTokens(1_000e18);
        assertEq(token.balanceOf(address(vault)), 0);
        assertEq(token.totalSupply(), INITIAL_SUPPLY - 1_000e18);
    }

    function test_BurnOnlyBurner() public {
        vm.prank(deployer);
        token.transfer(address(vault), 1_000e18);
        vm.prank(alice);
        vm.expectRevert(IAruvenVault.NotBurner.selector);
        vault.burnVaultTokens(1e18);
    }

    // ---------- vault: epoch / loss ----------

    function test_RollEpochSetsAnchorAndResetsLoss() public {
        vm.prank(address(timelock));
        vault.rollEpoch(bytes32("head"), 100_000e18);
        assertEq(vault.epoch(), 1);
        assertEq(vault.epochAnchorNav(), 100_000e18);
        assertEq(vault.epochLossUnits(), 0);
        assertEq(vault.dailyLossCap(), 2_000e18); // 2% of the anchor
    }

    /// The daily-loss cap must block TRADING, not merely bookkeeping. The old
    /// implementation reverted inside chargeLoss (so the loss was never
    /// recorded) and executeSwap never consulted it at all.
    function test_DailyLossCapBlocksTrading() public {
        vm.prank(address(timelock));
        vault.rollEpoch(bytes32("head"), 100_000e18); // cap = 2_000e18

        vm.startPrank(agent);
        vault.chargeLoss(1_000e18); // 1% — under the cap
        assertFalse(vault.dailyLossBreached());
        vault.chargeLoss(1_000e18); // exactly at the cap
        vm.stopPrank();

        assertEq(vault.epochLossUnits(), 2_000e18, "the loss is recorded, not discarded");
        assertTrue(vault.dailyLossBreached());

        // and now trading is refused
        _seedVault(100_000e18, 1_000_000e18);
        bytes memory data = abi.encodeCall(VaultHarness.swap, (address(usdg), address(token), 5_000e18, 50_000e18, address(vault)));
        vm.prank(agent);
        vm.expectRevert(IAruvenVault.DailyLossCapHit.selector);
        vault.executeSwap(address(usdg), address(token), 5_000e18, 1, address(router), data, bytes32("r"));

        // rolling the epoch clears it and trading resumes
        vm.prank(address(timelock));
        vault.rollEpoch(bytes32("head2"), 100_000e18);
        assertFalse(vault.dailyLossBreached());
        vm.prank(agent);
        vault.executeSwap(address(usdg), address(token), 5_000e18, 1, address(router), data, bytes32("r"));
    }

    function test_ChargeLossOnlyAgent() public {
        vm.prank(address(timelock));
        vault.rollEpoch(bytes32("head"), 100_000e18);
        vm.prank(alice);
        vm.expectRevert(IAruvenVault.NotAgent.selector);
        vault.chargeLoss(1);
    }

    // ---------- vault: params ----------

    function test_OnlyAdminSetsParams() public {
        vm.prank(alice);
        vm.expectRevert(IAruvenVault.NotAdmin.selector);
        vault.setRiskParams(100, 100, 100);
        vm.prank(address(timelock));
        vault.setRiskParams(300, 100, 60);
        assertEq(vault.maxTradeBps(), 300);
    }

    function test_TradeCapCannotExceed100Pct() public {
        vm.prank(address(timelock));
        vm.expectRevert(IAruvenVault.BadAmount.selector);
        vault.setRiskParams(10_001, 100, 60);
    }

    function test_OnlyAdminSetsRouter() public {
        vm.prank(alice);
        vm.expectRevert(IAruvenVault.NotAdmin.selector);
        vault.setRouter(address(0xdead), true);
        vm.prank(address(timelock));
        vault.setRouter(address(0xdead), true);
        assertTrue(vault.routerAllowed(address(0xdead)));
    }

    // ---------- vault: rescue ----------

    function test_RescueCannotTouchStrategyOrAruven() public {
        _seedVault(100_000e18, 1_000_000e18);
        vm.startPrank(address(timelock));
        vm.expectRevert(IAruvenVault.TokenNotAllowed.selector);
        vault.rescue(address(usdg), alice);
        vm.expectRevert(IAruvenVault.TokenNotAllowed.selector);
        vault.rescue(address(token), alice);
        vm.stopPrank();
    }

    function test_RescueForeignToken() public {
        MockStable dust = new MockStable();
        dust.mint(address(vault), 42e18);
        vm.prank(address(timelock));
        vault.rescue(address(dust), alice);
        assertEq(dust.balanceOf(alice), 42e18);
    }

    // ---------- governor ----------

    function test_GovernorParams() public view {
        assertEq(governor.name(), "AruvenGovernor");
        assertEq(governor.votingDelay(), 7_200);
        assertEq(governor.votingPeriod(), 3 days);
        assertEq(governor.proposalThreshold(), 4_000_000e18);
    }

    /// The documented 2-hour delay must be 2 hours of wall-clock, not 7200 blocks.
    function test_GovernorDelayIsMeasuredInSeconds() public {
        vm.prank(deployer);
        token.transfer(alice, 5_000_000e18);
        vm.prank(alice);
        token.delegate(alice);
        vm.warp(block.timestamp + 1); // checkpoint maturity

        address[] memory targets = new address[](1);
        uint256[] memory values = new uint256[](1);
        bytes[] memory calldatas = new bytes[](1);
        targets[0] = address(vault);
        calldatas[0] = abi.encodeCall(AruvenVault.setStrategyToken, (address(usdg), false));
        vm.prank(alice);
        uint256 pid = governor.propose(targets, values, calldatas, "p");

        vm.warp(block.timestamp + 2 hours + 1);
        assertEq(
            uint8(governor.state(pid)),
            uint8(IGovernor.ProposalState.Active),
            "voting must open after 2 wall-clock hours"
        );
    }

    function test_GovernorEndToEnd() public {
        vm.prank(deployer);
        token.transfer(alice, 5_000_000e18);
        vm.prank(alice);
        token.delegate(alice);
        vm.warp(block.timestamp + 1); // checkpoint maturity

        address[] memory targets = new address[](1);
        uint256[] memory values = new uint256[](1);
        bytes[] memory calldatas = new bytes[](1);
        targets[0] = address(vault);
        calldatas[0] = abi.encodeCall(AruvenVault.setStrategyToken, (address(usdg), false));

        vm.prank(alice);
        uint256 pid = governor.propose(targets, values, calldatas, "remove usdg from strategy");

        // alice holds 50% of a 10M supply — enough for quorum on its own
        vm.warp(block.timestamp + 7_201);
        vm.prank(alice);
        governor.castVote(pid, 1);

        vm.warp(block.timestamp + 3 days);
        bytes32 descHash = keccak256(bytes("remove usdg from strategy"));
        governor.queue(targets, values, calldatas, descHash);
        vm.warp(block.timestamp + 48 hours + 1);
        governor.execute(targets, values, calldatas, descHash);

        assertFalse(vault.strategyToken(address(usdg)));
    }

    // ---------- factory ----------

    MockPoolManager poolManager;

    function _factory() internal returns (AruvenFactory) {
        poolManager = new MockPoolManager();
        return new AruvenFactory(address(timelock), address(vault), 1_000); // 10% min share
    }

    function test_FactoryDeployAndEvent() public {
        AruvenFactory factory = _factory();
        vm.prank(alice);
        (address t, address v, address h) = factory.launch(
            alice, alice, 100_000e18, 1_500, MAX_TRADE_BPS, DAILY_LOSS_BPS, MIN_INTERVAL,
            bytes32("meta"), IPoolManager(address(poolManager))
        );
        assertTrue(t != address(0) && v != address(0) && h != address(0));
        assertEq(AruvenToken(t).balanceOf(alice), 100_000e18);
        assertEq(AruvenToken(t).name(), "Aruven");
        assertEq(IVaultAdmin(v).admin(), alice);
        // supply fixed at launch, as documented
        assertTrue(AruvenToken(t).mintingDisabled());
    }

    /// The revenue share must live in the deployed hook, not only in an event.
    function test_FactoryBakesShareIntoDeployedHook() public {
        AruvenFactory factory = _factory();
        vm.startPrank(alice);
        (, , address h10) = factory.launch(
            alice, alice, 100_000e18, 1_000, MAX_TRADE_BPS, DAILY_LOSS_BPS, MIN_INTERVAL,
            bytes32("a"), IPoolManager(address(poolManager))
        );
        (, , address h20) = factory.launch(
            alice, alice, 100_000e18, 2_000, MAX_TRADE_BPS, DAILY_LOSS_BPS, MIN_INTERVAL,
            bytes32("b"), IPoolManager(address(poolManager))
        );
        vm.stopPrank();

        assertEq(AruvenSplitHook(h10).shareBps(), 1_000);
        assertEq(AruvenSplitHook(h20).shareBps(), 2_000);
        assertEq(AruvenSplitHook(h10).beneficiary(), address(vault));
        // different shares => genuinely different deployed bytecode
        assertTrue(
            keccak256(h10.code) != keccak256(h20.code),
            "share must be part of the deployed bytecode"
        );
    }

    /// End-to-end: a fee taken on a third-party pool is split at the hook, and
    /// the child vault's admin cannot reach the flagship's leg.
    function test_SplitHookRoutesShareToFlagshipVault() public {
        AruvenFactory factory = _factory();
        vm.prank(alice);
        (address t, address v, address h) = factory.launch(
            alice, alice, 100_000e18, 1_000, MAX_TRADE_BPS, DAILY_LOSS_BPS, MIN_INTERVAL,
            bytes32("meta"), IPoolManager(address(poolManager))
        );

        // pool key for a token0/token1 pool hooked by this deployment
        PoolKey memory key = PoolKey({
            currency0: Currency.wrap(address(usdg)),
            currency1: Currency.wrap(t),
            fee: 3_000,
            tickSpacing: 60,
            hooks: IHooks(h)
        });
        AruvenSplitHook hook = AruvenSplitHook(h);
        vm.prank(alice); // hookGovernance
        hook.setPoolFee(key, 100); // 1%

        // exact-input zeroForOne: unspecified currency is currency1 (the token)
        uint128 outputAmount = 10_000e18;
        BalanceDelta delta = toBalanceDelta(-int128(outputAmount), int128(outputAmount));
        SwapParams memory params = SwapParams({zeroForOne: true, amountSpecified: -int128(outputAmount), sqrtPriceLimitX96: 0});

        // fund the manager so take() can pay out
        vm.prank(alice);
        ERC20(t).transfer(address(poolManager), 100_000e18);
        vm.prank(address(poolManager));
        hook.afterSwap(address(this), key, params, delta, "");

        uint256 fee = outputAmount * 100 / 10_000; // 100e18
        uint256 flagshipShare = fee * 1_000 / 10_000; // 10% => 10e18
        assertEq(ERC20(t).balanceOf(address(vault)), flagshipShare, "flagship got its share");
        assertEq(ERC20(t).balanceOf(v), fee - flagshipShare, "deployment vault got the rest");

        // The flagship's leg is delivered straight to the flagship vault, so it
        // never passes through the child vault — the child's admin cannot reach
        // it. And the child's own token is structurally non-rescuable (a vault
        // can never rescue its own $ARUVEN), so the fee that landed there is
        // stuck in the treasury by construction.
        vm.prank(alice);
        vm.expectRevert(IAruvenVault.TokenNotAllowed.selector);
        IVaultAdmin(v).rescue(t, alice);
        assertEq(ERC20(t).balanceOf(address(vault)), flagshipShare, "flagship leg untouched");
        assertEq(ERC20(t).balanceOf(v), fee - flagshipShare, "child leg untouched");
    }

    function test_FactoryEnforcesMinShare() public {
        AruvenFactory factory = _factory();
        vm.prank(alice);
        vm.expectRevert(AruvenFactory.ShareTooLow.selector);
        factory.launch(
            alice, alice, 100e18, 999, 500, 200, 3600, bytes32("meta"), IPoolManager(address(poolManager))
        );
    }

    function test_FactoryCapsShare() public {
        AruvenFactory factory = _factory();
        vm.prank(alice);
        vm.expectRevert(AruvenFactory.ShareTooHigh.selector);
        factory.launch(
            alice, alice, 100e18, 2_001, 500, 200, 3600, bytes32("meta"), IPoolManager(address(poolManager))
        );
    }

    function test_FactoryMaxShareSanity() public {
        AruvenFactory factory = _factory();
        assertEq(factory.MAX_SHARE_BPS(), 2_000);
        assertEq(factory.minShareBps(), 1_000);
    }

    // ---------- hook ----------

    /// The hook must declare the return-delta permission it relies on, and its
    /// address must carry the matching flag bits.
    function test_HookDeclaresReturnDeltaPermission() public {
        MockPoolManager pm = new MockPoolManager();
        AruvenFeeHook hook = new AruvenFeeHook(IPoolManager(address(pm)), address(vault), address(timelock));
        Hooks.Permissions memory perm = hook.getHookPermissions();
        assertTrue(perm.afterSwap);
        assertTrue(perm.afterSwapReturnDelta);
        // flags the deployer must mine into the hook address
        assertEq(hook.HOOK_FLAGS(), uint160((1 << 6) | (1 << 2)));
    }

    /// Fee is taken from the UNSPECIFIED currency (output for exact-input).
    /// The old hook charged the input currency while returning the delta in the
    /// unspecified one, which the PoolManager rejects.
    function test_HookChargesFeeOnUnspecifiedCurrency() public {
        MockPoolManager pm = new MockPoolManager();
        AruvenFeeHook hook = new AruvenFeeHook(IPoolManager(address(pm)), address(vault), address(timelock));
        MockStable t2 = new MockStable();
        PoolKey memory key = PoolKey({
            currency0: Currency.wrap(address(usdg)),
            currency1: Currency.wrap(address(t2)),
            fee: 3_000,
            tickSpacing: 60,
            hooks: IHooks(address(hook))
        });
        vm.prank(address(timelock));
        hook.setPoolFee(key, 100); // 1%

        // exact-input zeroForOne: input = currency0, output = currency1
        uint128 output = 5_000e18;
        BalanceDelta delta = toBalanceDelta(-int128(uint128(10_000e18)), int128(output));
        SwapParams memory params = SwapParams({zeroForOne: true, amountSpecified: -int128(uint128(10_000e18)), sqrtPriceLimitX96: 0});

        t2.mint(address(pm), 1_000_000e18);
        vm.prank(address(pm));
        (bytes4 selector, int128 hookDelta) = hook.afterSwap(address(this), key, params, delta, "");

        uint256 expectedFee = output * 100 / 10_000;
        assertEq(selector, IHooks.afterSwap.selector);
        assertEq(uint256(uint128(hookDelta)), expectedFee, "hook delta is the fee it took");
        assertEq(t2.balanceOf(address(vault)), expectedFee, "fee paid in the OUTPUT token");
        assertEq(usdg.balanceOf(address(vault)), 0, "nothing taken from the input token");
    }

    function test_HookRejectsNonPoolManagerCaller() public {
        MockPoolManager pm = new MockPoolManager();
        AruvenFeeHook hook = new AruvenFeeHook(IPoolManager(address(pm)), address(vault), address(timelock));
        PoolKey memory key = PoolKey({
            currency0: Currency.wrap(address(usdg)),
            currency1: Currency.wrap(address(token)),
            fee: 3_000,
            tickSpacing: 60,
            hooks: IHooks(address(hook))
        });
        SwapParams memory params = SwapParams({zeroForOne: true, amountSpecified: -1, sqrtPriceLimitX96: 0});
        vm.expectRevert(AruvenHook.NotPoolManager.selector);
        hook.afterSwap(address(this), key, params, toBalanceDelta(-1, 1), "");
    }

    function test_HookFeeCappedAt300Bps() public {
        MockPoolManager pm = new MockPoolManager();
        AruvenFeeHook hook = new AruvenFeeHook(IPoolManager(address(pm)), address(vault), address(timelock));
        PoolKey memory key = PoolKey({
            currency0: Currency.wrap(address(usdg)),
            currency1: Currency.wrap(address(token)),
            fee: 3_000,
            tickSpacing: 60,
            hooks: IHooks(address(hook))
        });
        vm.prank(address(timelock));
        vm.expectRevert(AruvenHook.FeeTooHigh.selector);
        hook.setPoolFee(key, 301);
        vm.prank(address(timelock));
        hook.setPoolFee(key, 300);
        assertEq(hook.poolFeeBps(keccak256(abi.encode(key))), 300);
    }

    function test_HookPoolFeeOnlyGovernance() public {
        MockPoolManager pm = new MockPoolManager();
        AruvenFeeHook hook = new AruvenFeeHook(IPoolManager(address(pm)), address(vault), address(timelock));
        PoolKey memory key = PoolKey({
            currency0: Currency.wrap(address(usdg)),
            currency1: Currency.wrap(address(token)),
            fee: 3_000,
            tickSpacing: 60,
            hooks: IHooks(address(hook))
        });
        vm.prank(alice);
        vm.expectRevert(AruvenFeeHook.NotHookGovernance.selector);
        hook.setPoolFee(key, 10);
    }
}

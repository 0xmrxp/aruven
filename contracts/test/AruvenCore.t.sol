// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-std/Test.sol";
import {AruvenToken} from "../src/AruvenToken.sol";
import {AruvenVault} from "../src/AruvenVault.sol";
import {IAruvenVault} from "../src/AruvenVault.sol";
import {AruvenFactory} from "../src/AruvenFactory.sol";
import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";
import {AruvenGovernor} from "../src/AruvenGovernor.sol";
import {IAccessControl} from "@openzeppelin/contracts/access/IAccessControl.sol";
import {IERC20Burnable} from "../src/IERC20Burnable.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

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

    function setUp() public {
        vm.prank(deployer);
        address[] memory openExec = new address[](1);
        openExec[0] = address(0);
        timelock = new TimelockController(48 hours, new address[](0), openExec, deployer);
        vm.prank(deployer);
        token = new AruvenToken(address(timelock), deployer, 10_000_000e18);
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
    }

    // ---------- token ----------

    function test_TokenMetadata() public view {
        assertEq(token.name(), "Aruven");
        assertEq(token.symbol(), "ARUVEN");
        assertEq(token.totalSupply(), 10_000_000e18);
        assertEq(token.MAX_SUPPLY(), 1_000_000_000e18);
        assertFalse(token.mintingDisabled());
    }

    function test_OnlyOwnerCanMintWithinCap() public {
        vm.prank(alice);
        vm.expectRevert();
        token.mint(alice, 1e18);

        vm.prank(address(timelock));
        token.mint(alice, 1e18);
        assertEq(token.balanceOf(alice), 1e18);
    }

    function test_MintCapEnforced() public {
        vm.prank(address(timelock));
        vm.expectRevert(AruvenToken.ExceedsMaxSupply.selector);
        token.mint(alice, 1_000_000_000e18 + 1); // cap is 1B, supply 10M → way over
    }

    function test_RenounceMintIsFinal() public {
        vm.startPrank(address(timelock));
        token.renounceMint();
        assertTrue(token.mintingDisabled());
        vm.expectRevert(AruvenToken.MintingIsDisabled.selector);
        token.mint(alice, 1e18);
        vm.stopPrank();
    }

    function test_NonOwnerCannotRenounceMint() public {
        vm.prank(alice);
        vm.expectRevert();
        token.renounceMint();
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

    // ---------- vault: swap risk gates ----------

    function _seedVault(uint256 usdgAmount, uint256 aruvenAmount) internal {
        usdg.mint(address(vault), usdgAmount);
        vm.prank(address(timelock));
        token.mint(address(router), aruvenAmount); // router holds aruven to pay out
        vm.prank(address(timelock));
        vault.setStrategyToken(address(usdg), true);
        vm.prank(address(timelock));
        vault.setStrategyToken(address(token), true);
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
        vm.startPrank(address(timelock));
        vault.setStrategyToken(address(usdg), true); // usdg listed, token listed; rogue not
        vm.stopPrank();
        vm.prank(agent);
        vm.expectRevert(IAruvenVault.TokenNotAllowed.selector);
        vault.executeSwap(address(rogue), address(token), 1e18, 1, address(router), data, bytes32("reason"));
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
        vm.prank(address(timelock));
        token.mint(address(vault), 1_000e18);
        vm.prank(burner);
        vault.burnVaultTokens(1_000e18);
        assertEq(token.balanceOf(address(vault)), 0);
        assertEq(token.totalSupply(), 10_000_000e18); // 10M + 1000 minted - 1000 burned
    }

    function test_BurnOnlyBurner() public {
        vm.prank(address(timelock));
        token.mint(address(vault), 1_000e18);
        vm.prank(alice);
        vm.expectRevert(IAruvenVault.NotBurner.selector);
        vault.burnVaultTokens(1e18);
    }

    // ---------- vault: epoch / loss ----------

    function test_ChargeLossCap() public {
        vm.prank(address(timelock));
        vault.rollEpoch(bytes32("head"), 100_000e18); // anchor NAV
        vm.startPrank(agent);
        vault.chargeLoss(1_000e18); // 1% — fine
        vault.chargeLoss(1_000e18); // total 2% = exactly cap → still fine
        vm.expectRevert(IAruvenVault.DailyLossCapHit.selector);
        vault.chargeLoss(1); // one more wei breaches
        vm.stopPrank();
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

    function test_GovernorEndToEnd() public {
        // alice gets enough tokens to propose (0.4% = 4M)
        vm.startPrank(address(timelock));
        token.mint(alice, 5_000_000e18);
        vault.setStrategyToken(address(usdg), true);
        vm.stopPrank();
        // delegate voting power BEFORE proposing (GovernorVotes checkpoints)
        vm.prank(alice);
        token.delegate(alice);
        vm.roll(block.number + 1); // checkpoint maturity

        address[] memory targets = new address[](1);
        uint256[] memory values = new uint256[](1);
        bytes[] memory calldatas = new bytes[](1);
        targets[0] = address(vault);
        calldatas[0] = abi.encodeCall(AruvenVault.setStrategyToken, (address(usdg), false));

        vm.prank(alice);
        uint256 pid = governor.propose(targets, values, calldatas, "remove usdg from strategy");

        // mint quorum votes: alice has 0.5% — need 5% quorum → mint more to bob
        vm.prank(address(timelock));
        token.mint(address(0xB0B), 60_000_000e18);
        vm.prank(address(0xB0B));
        token.delegate(address(0xB0B));

        vm.roll(block.number + 7_201);
        vm.prank(address(0xB0B));
        governor.castVote(pid, 1);
        vm.prank(alice);
        governor.castVote(pid, 1);

        vm.roll(block.number + 3 days);
        bytes32 descHash = keccak256(bytes("remove usdg from strategy"));
        governor.queue(targets, values, calldatas, descHash);
        vm.warp(block.timestamp + 48 hours + 1);
        governor.execute(targets, values, calldatas, descHash);

        // effect applied through the timelock
        assertFalse(vault.strategyToken(address(usdg)));
    }

    // ---------- factory ----------

    function test_FactoryDeployAndEvent() public {
        AruvenFactory factory = new AruvenFactory(address(timelock), address(vault), 1_000); // 10%
        vm.prank(alice);
        (address t, address v) = factory.launch(
            address(timelock), alice, 100_000e18, 1_500, MAX_TRADE_BPS, DAILY_LOSS_BPS, MIN_INTERVAL, bytes32("meta")
        );
        assertTrue(t != address(0) && v != address(0));
        assertEq(AruvenToken(t).balanceOf(alice), 100_000e18);
        assertEq(AruvenToken(t).name(), "Aruven");
        assertEq(AruvenVault(v).admin(), address(timelock));
    }

    event Launch(address indexed token, address indexed vault, address indexed creator, uint256 feeShareBps, bytes32 metaHash);

    function test_FactoryEnforcesMinShare() public {
        AruvenFactory factory = new AruvenFactory(address(timelock), address(vault), 1_000);
        vm.prank(alice);
        vm.expectRevert(AruvenFactory.ShareTooLow.selector);
        factory.launch(address(timelock), alice, 100e18, 999, 500, 200, 3600, bytes32("meta"));
    }

    function test_FactoryCapsShare() public {
        AruvenFactory factory = new AruvenFactory(address(timelock), address(vault), 1_000);
        vm.prank(alice);
        vm.expectRevert(AruvenFactory.ShareTooHigh.selector);
        factory.launch(address(timelock), alice, 100e18, 2_001, 500, 200, 3600, bytes32("meta"));
    }

    function test_FactoryMaxShareSanity() public {
        AruvenFactory factory = new AruvenFactory(address(timelock), address(vault), 2_000);
        assertEq(factory.MAX_SHARE_BPS(), 2_000);
        assertEq(factory.minShareBps(), 2_000);
    }
}

import {IVotes} from "@openzeppelin/contracts/governance/utils/IVotes.sol";

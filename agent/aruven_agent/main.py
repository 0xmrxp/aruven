"""Main loop: screen → score → strategy → risk → LLM → execute → journal."""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from .config import ConfigError, load_config
from .executor import Executor
from .feeds import FeedError, PriceFeed
from .journal import Journal
from .llm_review import LLMReviewer
from .risk import RiskGate
from .screen import ChainClient, Screener
from .strategy import MeanReversionStrategy, Scorer, SignalTracker

log = logging.getLogger("aruven")

# Vault reverts that mean "stop trading", per the documented kill switch.
CIRCUIT_BREAKERS = {"CooldownActive", "TradeTooLarge", "DailyLossCapHit", "NotAgent"}


class Agent:
    def __init__(self, cfg, dry_run: bool = True):
        self.cfg = cfg
        self.chain = ChainClient(cfg.rpc_url, cfg.chain_id)
        self.feed = PriceFeed(cfg.price_feed)
        self.screener = Screener(cfg, self.chain)
        self.scorer = Scorer(cfg)
        self.strategy = MeanReversionStrategy(cfg)
        # One trade per signal episode, not one per bar the signal persists for.
        self.signals = SignalTracker()
        self.risk = RiskGate(cfg)
        self.reviewer = LLMReviewer(cfg.llm)
        self.executor = Executor(cfg, self.chain, dry_run=dry_run)
        self.journal = Journal(cfg.journal.path)
        self.halted = False

    def _record(self, kind: str, payload: dict):
        """Append locally, then publish to the backend (best-effort)."""
        entry = self.journal.append(kind, payload)
        if self.cfg.journal.publish_url:
            if not self.journal.publish(entry, self.cfg.journal.publish_url):
                log.warning("journal publish failed for %s", entry.id)
        return entry

    def _on_new_episode(self, token: str, kind, obs, score, series) -> None:
        """Handle a signal episode starting or flipping direction."""
        prop = self.strategy.propose(obs, score)
        if prop is None:
            return
        self._record("proposal", {
            "token_in": prop.token_in, "token_out": prop.token_out,
            "amount_in": prop.amount_in, "reason": prop.reason,
            "episode": kind,
        })
        vault_last = self._vault_last_trade_at
        ok, why = self.risk.check(prop, obs, vault_last)
        if not ok:
            self._record("halt", {"stage": "risk", "reason": why, "episode": kind})
            return
        review = self.reviewer.review(prop)
        self._record("review", {
            "approved": review.approved, "confidence": review.confidence,
            "comment": review.comment, "model": review.model,
        })
        if not review.approved:
            return
        # The reason hash must cover the decision trace including the review, so
        # take it after the review is appended.
        reason_hash = self.journal.head
        try:
            execution = self.executor.execute(prop, reason_hash)
        except Exception as e:  # noqa: BLE001 — halting is the safe default
            self._record("halt", {"stage": "execute", "reason": str(e)[:200]})
            log.error("executor error: %s", e)
            self.halted = True
            return
        self._record("execution", {
            "tx_hash": execution.tx_hash, "amount_out": execution.amount_out,
            "success": execution.success, "reason_hash": reason_hash,
            "error": execution.error,
        })
        if not execution.success:
            # A documented circuit breaker means stop; anything else is logged
            # and the loop continues on the next token.
            if execution.error in CIRCUIT_BREAKERS:
                self.halted = True
                self._record("halt", {"stage": "execute", "reason": execution.error})
                log.error("halted by circuit breaker: %s", execution.error)
                return
            log.warning("trade failed: %s", execution.error)

    def run_once(self) -> None:
        if self.halted:
            return
        vault = self.chain.vault_state(self.cfg.vault_address)
        # Stashed so the risk gate compares against the vault's own cooldown
        # state, read once per pass instead of once per token.
        self._vault_last_trade_at = vault["last_trade_at"]

        for token in (t.name for t in self.cfg.strategy_tokens[1:]):  # skip stablecoin
            try:
                series, volume = self.feed.series(token)
            except FeedError as e:
                # A feed outage must not silently look like "no signal".
                self._record("skip", {
                    "token": token, "reason": "feed unavailable", "error": str(e)[:200],
                })
                log.warning("feed error for %s: %s", token, e)
                continue

            price = series[-1]
            obs = self.screener.observe(token, series, volume, price)
            score = self.scorer.score(obs, series)
            kind = self.strategy.signal(obs, score)
            transition = self.signals.update(token, kind)

            self._record("observation", {
                "token": obs.token, "price": obs.price, "rsi": obs.rsi,
                "vault_balance": obs.vault_balance, "epoch": obs.epoch,
                "samples": len(series), "zscore": score.zscore,
                "edge_bps": score.edge_bps, "signal": kind,
                "source": self.feed.last_source,
            })

            if obs.rsi is None:
                # Not enough history for the RSI period: no signal, say so.
                self._record("skip", {
                    "token": token, "reason": "insufficient price history for RSI",
                    "samples": len(series), "needed": self.cfg.strategy.rsi_period + 1,
                })
                continue

            if transition == "end":
                # Journal the episode closing; nothing to trade.
                self._record("signal_end", {
                    "token": token, "rsi": obs.rsi, "zscore": score.zscore,
                })
                continue
            if transition is None:
                # Same signal as the previous bar — already acted on it (or
                # deliberately did not). Re-trading here is what produced
                # ~63 trades/month before the episode tracker existed.
                continue

            self._on_new_episode(token, transition, obs, score, series)
            if self.halted:
                return

    def run_forever(self) -> None:
        while not self.halted:
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001 — halt on ANY unexpected error
                self.halted = True
                self._record("halt", {"stage": "loop", "reason": str(e)[:200]})
                log.error("halted: %s", e)
                return
            time.sleep(self.cfg.loop_interval_sec)


def main() -> int:
    parser = argparse.ArgumentParser(description="ARUVEN autonomous trading agent")
    parser.add_argument("--config", required=True)
    parser.add_argument("--dry-run", action="store_true", default=False)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--verify-journal", action="store_true")
    parser.add_argument("--check-feed", action="store_true",
                        help="fetch every strategy token once and report; no trading")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_config(args.config)
    except (ConfigError, OSError) as e:
        log.error("config: %s", e)
        return 2

    if args.verify_journal:
        ok, count = Journal(cfg.journal.path).verify_chain()
        print(f"journal chain: {'OK' if ok else 'BROKEN'} ({count} entries)")
        return 0 if ok else 1

    if args.check_feed:
        # Exercise the real feed without touching the chain or the journal.
        try:
            feed = PriceFeed(cfg.price_feed)
        except FeedError as e:
            print(f"FAIL  {e}")
            return 2
        print(f"source: {cfg.price_feed.source or 'generic'}"
              f"  fallback: {cfg.price_feed.fallback_source or '-'}"
              f"  interval: {cfg.price_feed.interval}"
              f"  limit: {cfg.price_feed.series_limit}")
        rc = 0
        for token in (t.name for t in cfg.strategy_tokens[1:]):
            try:
                prices, volume = feed.series(token)
            except FeedError as e:
                print(f"FAIL  {token}: {e}")
                rc = 1
                continue
            print(f"OK    {token:<8} via {feed.last_source:<12} "
                  f"{len(prices)} bars  last={prices[-1]:g}  vol24h={volume:g}")
            if len(prices) < cfg.strategy.rsi_period + 1:
                print(f"      WARN only {len(prices)} bars — RSI needs "
                      f"{cfg.strategy.rsi_period + 1}; the agent will refuse to trade")
                rc = 1
        return rc

    if not cfg.price_feed.configured:
        # Fail before touching the chain: a stubbed feed is what made the agent
        # look alive while it could never trade.
        log.error("price_feed is not configured — set source or url in %s", args.config)
        return 2

    try:
        agent = Agent(cfg, dry_run=args.dry_run)
    except FeedError as e:
        log.error("price feed: %s", e)
        return 2

    signal.signal(signal.SIGINT, lambda *_: setattr(agent, "halted", True))
    log.info(
        "executor: %s (dry_run=%s); feed: %s -> %s",
        agent.executor.address, args.dry_run,
        cfg.price_feed.source or "generic", cfg.price_feed.fallback_source or "-",
    )
    if args.once:
        agent.run_once()
    else:
        agent.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())

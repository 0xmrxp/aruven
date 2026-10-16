"""Main loop: screen → score → strategy → risk → LLM → execute → journal."""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from .config import ConfigError, load_config
from .executor import Executor
from .journal import Journal
from .llm_review import LLMReviewer
from .risk import RiskGate
from .screen import ChainClient, PriceFeed, Screener
from .strategy import MeanReversionStrategy, Scorer

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

    def run_once(self) -> None:
        if self.halted:
            return
        vault = self.chain.vault_state(self.cfg.vault_address)
        for token in (t.name for t in self.cfg.strategy_tokens[1:]):  # skip stablecoin
            series, volume = self.feed.series(token)
            price = series[-1]
            obs = self.screener.observe(token, series, volume, price)
            self._record("observation", {
                "token": obs.token, "price": obs.price, "rsi": obs.rsi,
                "vault_balance": obs.vault_balance, "epoch": obs.epoch,
                "samples": len(series),
            })
            if obs.rsi is None:
                # Not enough history for the RSI period: no signal, say so.
                self._record("skip", {
                    "token": token, "reason": "insufficient price history for RSI",
                    "samples": len(series), "needed": self.cfg.strategy.rsi_period + 1,
                })
                continue
            score = self.scorer.score(obs, series)
            prop = self.strategy.propose(obs, score)
            if prop is None:
                continue
            self._record("proposal", {
                "token_in": prop.token_in, "token_out": prop.token_out,
                "amount_in": prop.amount_in, "reason": prop.reason,
            })
            ok, why = self.risk.check(prop, obs, vault["last_trade_at"])
            if not ok:
                self._record("halt", {"stage": "risk", "reason": why})
                continue
            review = self.reviewer.review(prop)
            self._record("review", {
                "approved": review.approved, "confidence": review.confidence,
                "comment": review.comment, "model": review.model,
            })
            if not review.approved:
                continue
            # The reason hash must cover the decision trace including the
            # review, so take it after the review is appended.
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
                # A documented circuit breaker means stop; anything else is
                # logged and the loop continues on the next token.
                if execution.error in CIRCUIT_BREAKERS:
                    self.halted = True
                    self._record("halt", {"stage": "execute", "reason": execution.error})
                    log.error("halted by circuit breaker: %s", execution.error)
                    return
                log.warning("trade failed: %s", execution.error)

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

    if not cfg.price_feed.configured:
        # Fail before touching the chain: a stubbed feed is what made the agent
        # look alive while it could never trade.
        log.error("price_feed.url is not configured — set it in %s", args.config)
        return 2

    agent = Agent(cfg, dry_run=args.dry_run)
    signal.signal(signal.SIGINT, lambda *_: setattr(agent, "halted", True))
    log.info("executor: %s (dry_run=%s)", agent.executor.address, args.dry_run)
    if args.once:
        agent.run_once()
    else:
        agent.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())

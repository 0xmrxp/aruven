"""Main loop: screen → score → strategy → risk → LLM → execute → journal."""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from typing import Optional

from .config import ConfigError, load_config
from .executor import Executor
from .journal import Journal
from .llm_review import LLMReviewer
from .risk import RiskGate
from .screen import ChainClient, Screener
from .strategy import MeanReversionStrategy, Scorer

log = logging.getLogger("aruven")


class Agent:
    def __init__(self, cfg, dry_run: bool = True):
        self.cfg = cfg
        self.chain = ChainClient(cfg.rpc_url, cfg.chain_id)
        self.screener = Screener(cfg, self.chain)
        self.scorer = Scorer(cfg)
        self.strategy = MeanReversionStrategy(cfg)
        self.risk = RiskGate(cfg)
        self.reviewer = LLMReviewer(cfg.llm)
        self.executor = Executor(cfg, self.chain, dry_run=dry_run)
        self.journal = Journal(cfg.journal.path)
        self.halted = False

    # -- data source placeholder: in production this reads the indexer API --
    def _price_series(self, token: str) -> list[float]:
        return [1.0, 1.0, 1.01, 0.99, 1.0]  # Phase 3: indexer feed

    def _price(self, token: str) -> float:
        return self._price_series(token)[-1]

    def _volume(self, token: str) -> float:
        return 0.0  # Phase 3: indexer feed

    def run_once(self) -> None:
        if self.halted:
            return
        vault = self.chain.vault_state(self.cfg.vault_address)
        for token in (t.name for t in self.cfg.strategy_tokens[1:]):  # skip stablecoin
            obs = self.screener.observe(
                token, self._price_series(token), self._volume(token), self._price(token)
            )
            self.journal.append("observation", {
                "token": obs.token, "price": obs.price, "rsi": obs.rsi,
                "vault_balance": obs.vault_balance, "epoch": obs.epoch,
            })
            score = self.scorer.score(obs, self._price_series(token))
            prop = self.strategy.propose(obs, score)
            if prop is None:
                continue
            self.journal.append("proposal", {
                "token_in": prop.token_in, "token_out": prop.token_out,
                "amount_in": prop.amount_in, "reason": prop.reason,
            })
            ok, why = self.risk.check(prop, obs, vault["last_trade_at"])
            if not ok:
                self.journal.append("halt", {"stage": "risk", "reason": why})
                continue
            review = self.reviewer.review(prop)
            self.journal.append("review", {
                "approved": review.approved, "confidence": review.confidence,
                "comment": review.comment, "model": review.model,
            })
            if not review.approved:
                continue
            reason_hash = self.journal.head
            try:
                execution = self.executor.execute(prop, reason_hash)
            except Exception as e:  # noqa: BLE001 — halting is the safe default
                self.journal.append("halt", {"stage": "execute", "reason": str(e)[:200]})
                log.error("executor error: %s", e)
                return
            self.journal.append("execution", {
                "tx_hash": execution.tx_hash, "amount_out": execution.amount_out,
                "success": execution.success, "reason_hash": reason_hash,
            })

    def run_forever(self) -> None:
        while not self.halted:
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001 — halt on ANY unexpected error
                self.halted = True
                self.journal.append("halt", {"stage": "loop", "reason": str(e)[:200]})
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

    agent = Agent(cfg, dry_run=args.dry_run)
    signal.signal(signal.SIGINT, lambda *_: setattr(agent, "halted", True))
    if args.once:
        agent.run_once()
    else:
        agent.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())

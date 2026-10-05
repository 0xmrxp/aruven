"""Stage 4 — LLM review: optional final sanity check on a proposal."""
from __future__ import annotations

import json
import urllib.request

from .config import LLMConfig
from .types import LLMReview, Proposal


class LLMReviewer:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg

    def review(self, prop: Proposal) -> LLMReview:
        if not self.cfg.enabled:
            return LLMReview(approved=True, confidence=1.0, comment="llm review disabled", model="none")
        try:
            return self._ask(prop)
        except Exception as e:  # noqa: BLE001 — fail CLOSED on review errors
            return LLMReview(approved=False, confidence=0.0, comment=f"llm error: {e}", model=self.cfg.model)

    def _ask(self, prop: Proposal) -> LLMReview:
        prompt = (
            "You are the risk reviewer for a treasury trading agent. "
            "Approve or reject the following trade. Respond ONLY as JSON: "
            '{"approved": bool, "confidence": float, "comment": str}\n\n'
            f"Trade: sell {prop.token_in}, buy {prop.token_out}\n"
            f"amount_in (raw): {prop.amount_in}\n"
            f"expected_out (raw): {prop.expected_out}\n"
            f"reason: {prop.reason}"
        )
        body = json.dumps({
            "model": self.cfg.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
        }).encode()
        req = urllib.request.Request(
            self.cfg.base_url.rstrip("/") + "/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.cfg.api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return LLMReview(
            approved=bool(parsed.get("approved", False)),
            confidence=float(parsed.get("confidence", 0.0)),
            comment=str(parsed.get("comment", ""))[:500],
            model=self.cfg.model,
        )

"""Shared crypto primitives for the ARUVEN stack (agent + backend).

Kept in one place so the trading agent and the backend indexer/signer cannot
drift apart: a transaction signed by one must verify under the other.
"""

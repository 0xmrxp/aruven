"""Hash-chained append-only journal (NDJSON) with epoch checkpointing."""
from __future__ import annotations

import os
from typing import Optional

from .types import JournalEntry


class Journal:
    def __init__(self, path: str):
        self.path = path
        self.head = "0x" + "0" * 64
        self._load_head()

    def _load_head(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "rb") as f:
                # seek to last line efficiently
                f.seek(0, os.SEEK_END)
                size = f.tell()
                pos = size - 1
                while pos > 0:
                    f.seek(pos)
                    if f.read(1) == b"\n":
                        pos -= 1
                        continue
                    break
                f.seek(max(0, pos - 65536))
                tail = f.read().decode(errors="replace")
            last = [l for l in tail.strip().splitlines() if l][-1]
            import json
            self.head = json.loads(last)["hash"]
        except Exception:
            # corrupt tail: journal continues but flag via fresh chain marker
            self.head = "0x" + "0" * 64

    def append(self, kind: str, payload: dict) -> JournalEntry:
        entry = JournalEntry.new(kind, payload, self.head)
        with open(self.path, "a") as f:
            f.write(entry.to_line() + "\n")
        self.head = entry.hash
        return entry

    # -- publishing -----------------------------------------------------
    # The backend serves /journal from its journal_feed table, which nothing
    # used to write, so the public journal feed was always empty. Sealed
    # entries are pushed to the backend after they land in the local chain.
    def publish(self, entry: JournalEntry, publish_url: str, timeout: int = 10) -> bool:
        """POST a sealed entry to the backend. Returns True when accepted.

        Publishing is best-effort: a backend outage must not stop trading, and
        the local NDJSON chain remains the source of truth (the entry can be
        replayed later, and `verify_chain` still detects tampering).
        """
        if not publish_url:
            return False
        import json
        import urllib.error
        import urllib.request

        body = json.dumps(
            {
                "id": entry.id,
                "ts": entry.ts,
                "kind": entry.kind,
                "payload": entry.payload,
                "prev_hash": entry.prev_hash,
                "hash": entry.hash,
            }
        ).encode()
        req = urllib.request.Request(
            publish_url.rstrip("/") + "/journal",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return 200 <= resp.status < 300
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def pending_lines(self) -> list[str]:
        """Raw lines of the local journal, for replaying into a backend."""
        import os
        if not os.path.exists(self.path):
            return []
        with open(self.path) as f:
            return [line for line in f if line.strip()]

    def verify_chain(self) -> tuple[bool, int]:
        """Re-verify the whole chain; returns (ok, entry_count)."""
        import json
        if not os.path.exists(self.path):
            return True, 0
        prev = "0x" + "0" * 64
        count = 0
        with open(self.path) as f:
            for line in f:
                if not line.strip():
                    continue
                e = json.loads(line)
                if e["prev_hash"] != prev:
                    return False, count
                recomputed = JournalEntry(
                    id=e["id"], ts=e["ts"], kind=e["kind"],
                    payload=e["payload"], prev_hash=e["prev_hash"],
                ).compute_hash()
                if recomputed != e["hash"]:
                    return False, count
                prev = e["hash"]
                count += 1
        return True, count

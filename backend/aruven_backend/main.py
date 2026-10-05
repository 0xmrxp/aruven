"""Backend entrypoint: run the indexer (thread) + API (uvicorn)."""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import threading

from .config import ConfigError, load_config
from .indexer import Indexer, Rpc
from .api import create_app

log = logging.getLogger("aruven.backend")


def main() -> int:
    parser = argparse.ArgumentParser(description="ARUVEN backend (indexer + API)")
    parser.add_argument("--config", required=True)
    parser.add_argument("--index-only", action="store_true")
    parser.add_argument("--api-only", action="store_true")
    parser.add_argument("--poll-once", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_config(args.config)
    except (ConfigError, OSError) as e:
        log.error("config: %s", e)
        return 2

    db = sqlite3.connect(cfg.db_path, check_same_thread=False)
    rpc = Rpc(cfg.rpc_url)
    indexer = Indexer(cfg, db, rpc)

    if args.poll_once:
        n = indexer.poll_once()
        print(f"indexed {n} blocks")
        return 0

    if not args.api_only:
        t = threading.Thread(target=indexer.run_forever, daemon=True, name="indexer")
        t.start()
        log.info("indexer started (from block %s)", indexer.head)

    if args.index_only:
        t.join()
        return 0

    import uvicorn
    app = create_app(cfg, db, rpc)
    uvicorn.run(app, host=cfg.api_host, port=cfg.api_port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())

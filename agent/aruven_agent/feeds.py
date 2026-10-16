"""Market-data sources for the agent.

The strategy needs a *series* of closes — RSI and the z-score are computed over
a window — so a spot-only endpoint (like a price API) is not enough. Everything
here is public and keyless.

Each source returns ``(prices oldest→newest, volume_24h)``. A source that cannot
produce a series raises rather than returning a short one: a stub is what
previously made the agent look alive while it could never trade.
"""
from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Protocol

from .config import PriceFeedConfig


class FeedError(RuntimeError):
    """A market-data source could not produce a usable series."""


_INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def interval_ms(interval: str) -> int:
    try:
        return _INTERVAL_MS[interval]
    except KeyError:
        raise FeedError(
            f"unsupported interval {interval!r}; supported: {sorted(_INTERVAL_MS)}"
        ) from None


def _post_json(url: str, payload: dict, timeout: int) -> object:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _get_json(url: str, timeout: int) -> object:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


class MarketFeed(Protocol):
    name: str

    def series(self, token: str) -> tuple[list[float], float]:  # pragma: no cover
        ...


class GenericFeed:
    """A user-hosted endpoint.

    ``GET {url}?token=<name>&limit=<n>`` must return
    ``{"prices": [...oldest first...], "volume_24h": <float>}``.
    """

    name = "generic"

    def __init__(self, cfg: PriceFeedConfig):
        self.cfg = cfg
        if not cfg.url.strip():
            raise FeedError("price_feed.url is required for source 'generic'")

    def series(self, token: str) -> tuple[list[float], float]:
        url = (
            f"{self.cfg.url.rstrip('/')}"
            f"?token={urllib.parse.quote(token)}&limit={self.cfg.series_limit}"
        )
        try:
            body = _get_json(url, self.cfg.timeout_sec)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise FeedError(f"generic feed request failed: {e}") from e
        if not isinstance(body, dict):
            raise FeedError("generic feed did not return a JSON object")
        prices = [float(p) for p in (body.get("prices") or [])]
        if not prices:
            raise FeedError(f"generic feed returned no prices for {token}")
        return prices, float(body.get("volume_24h", 0.0))


class HyperliquidFeed:
    """Hyperliquid public ``/info`` candleSnapshot — free, no API key.

    This is the default source: keyless, generous limits, and it serves real
    OHLCV candles (which is what RSI needs) rather than a spot quote.
    """

    name = "hyperliquid"
    INFO_URL = "https://api.hyperliquid.xyz/info"

    def __init__(self, cfg: PriceFeedConfig):
        self.cfg = cfg
        interval_ms(cfg.interval)  # validate early, not mid-loop

    def series(self, token: str) -> tuple[list[float], float]:
        coin = self.cfg.symbols.get(token, token.upper())
        step = interval_ms(self.cfg.interval)
        end = int(time.time() * 1000)
        # one extra bar so a partially-formed candle cannot shorten the window
        start = end - step * (self.cfg.series_limit + 1)
        try:
            raw = _post_json(
                self.INFO_URL,
                {
                    "type": "candleSnapshot",
                    "req": {"coin": coin, "interval": self.cfg.interval,
                            "startTime": start, "endTime": end},
                },
                self.cfg.timeout_sec,
            )
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise FeedError(f"hyperliquid request failed for {coin}: {e}") from e
        if not isinstance(raw, list) or not raw:
            raise FeedError(f"hyperliquid returned no candles for {coin}")
        prices: list[float] = []
        vols: list[float] = []
        for candle in raw:
            try:
                prices.append(float(candle["c"]))
                vols.append(float(candle.get("v", 0.0)))
            except (KeyError, TypeError, ValueError) as e:
                raise FeedError(f"malformed hyperliquid candle for {coin}: {e}") from e
        return prices, self._volume_24h(vols, step)

    @staticmethod
    def _volume_24h(vols: list[float], step_ms: int) -> float:
        bars = max(1, math.ceil(86_400_000 / step_ms))
        return float(sum(vols[-bars:]))


class BinanceFeed:
    """Binance public klines — free, no API key. Used as a fallback."""

    name = "binance"
    BASE = "https://api.binance.com/api/v3/klines"

    def __init__(self, cfg: PriceFeedConfig):
        self.cfg = cfg
        interval_ms(cfg.interval)

    def _symbol(self, token: str) -> str:
        explicit = self.cfg.symbols.get(token)
        if explicit:
            return explicit
        up = token.upper()
        return up if up.endswith(("USDT", "USDC", "BUSD")) else f"{up}USDT"

    def series(self, token: str) -> tuple[list[float], float]:
        symbol = self._symbol(token)
        url = (
            f"{self.BASE}?symbol={urllib.parse.quote(symbol)}"
            f"&interval={urllib.parse.quote(self.cfg.interval)}"
            f"&limit={self.cfg.series_limit}"
        )
        try:
            raw = _get_json(url, self.cfg.timeout_sec)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise FeedError(f"binance request failed for {symbol}: {e}") from e
        if not isinstance(raw, list) or not raw:
            raise FeedError(f"binance returned no klines for {symbol}")
        prices: list[float] = []
        vols: list[float] = []
        for row in raw:
            try:
                prices.append(float(row[4]))  # close
                vols.append(float(row[5]))    # base-asset volume
            except (IndexError, TypeError, ValueError) as e:
                raise FeedError(f"malformed binance kline for {symbol}: {e}") from e
        bars = max(1, math.ceil(86_400_000 / interval_ms(self.cfg.interval)))
        return prices, float(sum(vols[-bars:]))


_SOURCES = {
    "generic": GenericFeed,
    "hyperliquid": HyperliquidFeed,
    "binance": BinanceFeed,
}


class PriceFeed:
    """Facade over the configured source, with an optional fallback.

    A primary source that fails is retried once against the fallback, and the
    caller is told which one served the request so the journal records it.
    """

    def __init__(self, cfg: PriceFeedConfig):
        self.cfg = cfg
        primary = (cfg.source or ("generic" if cfg.url.strip() else "")).strip()
        if not primary:
            raise FeedError(
                "no market-data source configured — set price_feed.source "
                "(hyperliquid | binance | generic) or price_feed.url"
            )
        if primary not in _SOURCES:
            raise FeedError(
                f"unknown price_feed.source {primary!r}; expected one of {sorted(_SOURCES)}"
            )
        self.primary: MarketFeed = _SOURCES[primary](cfg)
        self.fallback: Optional[MarketFeed] = None
        if cfg.fallback_source and cfg.fallback_source != primary:
            if cfg.fallback_source not in _SOURCES:
                raise FeedError(
                    f"unknown price_feed.fallback_source {cfg.fallback_source!r}; "
                    f"expected one of {sorted(_SOURCES)}"
                )
            self.fallback = _SOURCES[cfg.fallback_source](cfg)
        self.last_source = self.primary.name

    def series(self, token: str) -> tuple[list[float], float]:
        try:
            prices, volume = self.primary.series(token)
            self.last_source = self.primary.name
            return prices, volume
        except FeedError as primary_err:
            if self.fallback is None:
                raise
            try:
                prices, volume = self.fallback.series(token)
            except FeedError as fallback_err:
                raise FeedError(
                    f"all sources failed for {token}: "
                    f"{self.primary.name}={primary_err}; {self.fallback.name}={fallback_err}"
                ) from fallback_err
            self.last_source = self.fallback.name
            return prices, volume

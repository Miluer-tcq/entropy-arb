"""Polymarket Perps adapter — RECORD-ONLY by design (phase 1 evaluation).

Polymarket launched stock/crypto perps in 2026 on a classic CLOB (offchain
matching, Polygon custody, pUSD collateral, 1h funding). Taker fee is 4 bps
at entry tier — 4x the Entropy leg and infinitely worse than Lighter's zero —
so it is NOT automatically a hedge venue; this adapter exists to record its
book into the same 1-minute CSV bars so the analyzer/backtester can decide
with data whether a polymarket hedge or a maker-side program pays.

Trading methods raise on purpose: wiring orders only happens after the
recorded premium clears the same bar Lighter/xyz cleared (see the engine's
head-to-head tooling).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .book import OrderBook
from .feeds import PolyBookFeed

log = logging.getLogger("polymarket")

REST_TIMEOUT = 10.0


class TradeNotWired(RuntimeError):
    pass


class PolyVenue:
    """Polymarket Perps venue: public REST book feed + read-only account
    shapes. `ready_to_trade` is deliberately False until a signer exists
    (never), so even a misconfigured live start cannot route orders here."""

    kind = "poly"

    def __init__(self, conf, api_url: str, proxy: Optional[str],
                 session, settle_timeout_sec: float = 10.0) -> None:
        self.conf = conf
        self.key = conf.key
        self.name = conf.label
        self.api_url = api_url
        self.proxy = proxy or None
        self.session = session
        self.settle_timeout = settle_timeout_sec
        self.book = OrderBook()
        self.position = 0.0
        self.cash = 0.0
        self.volume_usd = 0.0
        self.equity = None
        self.free = None
        self.start_equity = None
        self.include_core_equity = False
        self.fee_bps = conf.fee_bps
        self.fee_src = "config"
        self.cap_usd = conf.cap_usd
        self.orders_per_min = conf.orders_per_min
        self.last_traded_ts = 0.0
        self.instrument_id: Optional[int] = None
        self.price_decimals = 1
        self.size_decimals = 4
        self.min_base = 0.0
        self.min_quote = 10.0
        self.feed: Optional[PolyBookFeed] = None

    # ------------------------------------------------------------ REST bits

    async def _get(self, path: str, retries: int = 3, **params):
        import aiohttp
        last: Exception = RuntimeError("unreachable")
        for attempt in range(retries):
            try:
                async with self.session.get(
                        self.api_url + path, params=params or None,
                        proxy=self.proxy,
                        headers={"User-Agent": PolyBookFeed.UA,
                                 "Accept": "application/json"},
                        timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)
                ) as r:
                    if r.status != 200:
                        raise RuntimeError(f"{path} -> HTTP {r.status}")
                    return await r.json(content_type=None)
            except (aiohttp.ClientError, RuntimeError, OSError) as e:
                # Clash tunnels to this host reset sporadically; a long-lived
                # recorder must not die on one blip
                last = e
                if attempt < retries - 1:
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise last

    async def load_market(self) -> None:
        """Resolve '<SYMBOL>-USD' to its instrument and sizing constraints."""
        want = f"{self.conf.symbol.upper()}-USD"
        instruments = await self._get("/v1/info/instruments")
        ins = next((i for i in instruments if i.get("symbol") == want), None)
        if ins is None:
            raise RuntimeError(
                f"Polymarket has no perp {want} / Polymarket 未上线该永续")
        self.instrument_id = int(ins["instrument_id"])
        self.size_decimals = int(ins.get("quantity_decimals", 4))
        self.price_decimals = int(ins.get("price_decimals", 1))
        self.min_base = 10.0 ** -self.size_decimals
        self.min_quote = max(float(ins.get("min_notional") or 10.0), 10.0)
        log.info("[%s] perp %s id=%d sz_dec=%d px_dec=%d min_notional=$%g "
                 "funding=%s (RECORD-ONLY)", self.name, want,
                 self.instrument_id, self.size_decimals,
                 self.price_decimals, self.min_quote,
                 ins.get("funding_interval"))

    # --------------------------------------------------------- venue shape

    def init_signer(self) -> None:
        raise TradeNotWired(
            "Polymarket Perps trading is not wired — record-only venue "
            "(--record-only); decide from logs/poly_minutes.csv first")

    def ready_to_trade(self) -> bool:
        return False

    def px_round(self, px: float, round_up: bool) -> float:
        q = round(px, self.price_decimals)
        import math
        if q != px:
            step = 10.0 ** -self.price_decimals
            f = math.ceil if round_up else math.floor
            q = f(px / step) * step
            q = round(q, self.price_decimals)
        return q

    def start_tasks(self, stop: asyncio.Event, notify, live: bool) -> list:
        self.feed = PolyBookFeed(self.name, self.api_url,
                                 self.instrument_id, self.book,
                                 self.session, notify, proxy=self.proxy)
        return [asyncio.create_task(self.feed.run(stop),
                                    name=f"book-{self.key}")]

    async def fetch_fee(self) -> Optional[float]:
        # tiered by 30d volume behind auth; the config value (4.0 base tier)
        # is the planning assumption
        return None

    async def fetch_funding(self) -> Optional[float]:
        try:
            rows = await self._get("/v1/info/tickers",
                                   instrument_id=self.instrument_id)
            for row in rows:
                if row.get("instrument_id") == self.instrument_id:
                    return float(row["funding_rate"])
        except Exception as e:
            log.debug("[%s] funding fetch failed: %r", self.name, e)
        return None

    async def fetch_equity(self) -> None:
        return None

    async def fetch_position(self) -> float:
        return self.position

    async def send_taker(self, **kwargs) -> dict:
        raise TradeNotWired("Polymarket send_taker not wired (record-only)")

    async def send_maker(self, **kwargs) -> dict:
        raise TradeNotWired("Polymarket send_maker not wired (record-only)")

    async def cancel_resting(self) -> None:
        return None

    async def warm_http(self) -> None:
        return None

    async def close(self) -> None:
        return None

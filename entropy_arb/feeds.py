"""Websocket order-book feeds, writing into entropy_arb.book.OrderBook.

Two protocols, one per exchange family:

LighterBookFeed: zkLighter order_book channel (snapshot + diffs, server
    pings, diff-nonce gap detection — a gapped book is dropped and
    resubscribed rather than traded as a fiction).
HLBookFeed: the official Hyperliquid websocket (wss://api.hyperliquid.xyz/ws)
    l2Book channel with fast snapshots and client app-pings. Every price this
    bot trades on comes straight from the exchange that will fill the order.
PolyBookFeed: Polymarket Perps public REST book polling (record-only today —
    1s snapshots with sequence-gap detection; Cloudflare demands a browser UA
    and many networks need an explicit proxy for this host).

Both touch the book on any inbound frame (connection-based freshness: a quiet
market is not stale, only a dead feed is) and reconnect with backoff.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Callable, Optional

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:
    from websockets import connect as ws_connect  # type: ignore

from .book import OrderBook

log = logging.getLogger("feeds")

# a book with no frames at all for this long is a dead-but-open socket
SILENCE_RECONNECT_SEC = 30.0


async def _silence_watchdog(name: str, ws, book, connected_at: float,
                            silence_sec: float = SILENCE_RECONNECT_SEC,
                            check_sec: float = 5.0) -> None:
    """Silence reaper shared by both feeds: a socket can stay TCP-healthy
    while its stream dies (upstream wedge after a laptop wake, silent drop
    that protocol pings keep answering). If the book has seen no frame of
    any kind for SILENCE_RECONNECT_SEC, cut the line so run() resubscribes
    fresh (the resubscribe's snapshot re-primes everything)."""
    try:
        while True:
            await asyncio.sleep(check_sec)
            last = max(getattr(book, "last_update_ts", 0.0), connected_at)
            if time.time() - last > silence_sec:
                log.warning("[%s] feed silent %.0fs — forcing reconnect",
                            name, time.time() - last)
                with contextlib.suppress(Exception):
                    await ws.close()
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        return


def _chan_id(channel: str) -> Optional[int]:
    """'order_book:32' / 'order_book/32' -> 32."""
    for sep in (":", "/"):
        if sep in channel:
            try:
                return int(channel.rsplit(sep, 1)[1])
            except ValueError:
                return None
    return None


class LighterBookFeed:
    """zkLighter order book for one market over one connection."""

    def __init__(self, name: str, ws_url: str, market_id: int, book: OrderBook,
                 notify: Callable[[], None]) -> None:
        self.name = name
        self.ws_url = ws_url
        self.market_id = market_id
        self.book = book
        self.notify = notify
        self._nonce: Optional[int] = None
        self._synced = False

    async def _subscribe(self, ws) -> None:
        await ws.send(json.dumps({"type": "subscribe",
                                  "channel": f"order_book/{self.market_id}"}))

    async def _handle_book(self, ws, msg: dict, snapshot: bool) -> None:
        if _chan_id(msg.get("channel", "")) != self.market_id:
            return
        ob = msg["order_book"]
        if snapshot:
            self._nonce = ob.get("nonce")
            self._synced = True
            self.book.apply_lighter(ob, snapshot=True)
            log.info("[%s] snapshot: %d bids / %d asks", self.name,
                     len(self.book.bids), len(self.book.asks))
            self.notify()
            return
        # diff: a skipped nonce means we lost a level update — the book is now
        # a fiction. Drop it and resubscribe rather than quote off a ghost.
        if not self._synced:
            return  # no snapshot yet (fresh connection, or one pending after a gap)
        prev, begin, end = self._nonce, ob.get("begin_nonce"), ob.get("nonce")
        if prev is not None and begin is not None and begin > prev + 1:
            log.warning("[%s] diff gap (had %s, got %s) — resubscribing",
                        self.name, prev, begin)
            self._nonce = None
            self._synced = False
            self.book.clear()
            self.notify()
            await ws.send(json.dumps({"type": "unsubscribe",
                                      "channel": f"order_book/{self.market_id}"}))
            await self._subscribe(ws)
            return
        if end is not None:
            self._nonce = end
        self.book.apply_lighter(ob, snapshot=False)
        self.notify()

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                async with ws_connect(self.ws_url, max_size=2**23, open_timeout=10,
                                      ping_interval=15, ping_timeout=15) as ws:
                    log.info("[%s] connected (%s)", self.name, self.ws_url)
                    connected_at = time.time()
                    self.book.clear()
                    self._nonce = None
                    self._synced = False
                    wd = asyncio.create_task(_silence_watchdog(self.name, ws, self.book,
                                                 connected_at))
                    try:
                        async for raw in ws:
                            # only a long-lived connection resets the backoff
                            # — a flapping link must not reconnect-storm
                            if time.time() - connected_at > 60.0:
                                backoff = 1.0
                            msg = json.loads(raw)
                            t = msg.get("type")
                            self.book.touch()
                            if t == "update/order_book":
                                await self._handle_book(ws, msg, snapshot=False)
                            elif t == "subscribed/order_book":
                                await self._handle_book(ws, msg, snapshot=True)
                            elif t == "connected":
                                await self._subscribe(ws)
                            elif t == "ping":
                                await ws.send(json.dumps({"type": "pong"}))
                            if stop.is_set():
                                break
                    finally:
                        wd.cancel()
                        with contextlib.suppress(BaseException):
                            await wd
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] ws error: %s — reconnect in %.0fs",
                            self.name, e, backoff)
            self.book.ready = False
            self.notify()
            if stop.is_set():
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            backoff = min(backoff * 2, 30.0)


class HLBookFeed:
    """Official Hyperliquid l2Book consumer for one coin (e.g. 'io:SNDK')."""

    def __init__(self, name: str, ws_url: str, coin: str, book: OrderBook,
                 notify: Callable[[], None], ping_sec: float = 5.0) -> None:
        self.name = name
        self.ws_url = ws_url
        self.coin = coin
        self.book = book
        self.notify = notify
        self.ping_sec = ping_sec
        self._snapped = False

    def _on_frame(self, msg: dict) -> None:
        self.book.touch()
        if msg.get("channel") == "l2Book":
            d = msg.get("data") or {}
            if d.get("coin") == self.coin:
                self.book.apply_hl(d["levels"])
                if not self._snapped:
                    self._snapped = True
                    log.info("[%s] snapshot: %d bids / %d asks", self.name,
                             len(self.book.bids), len(self.book.asks))
                self.notify()

    async def _pinger(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(self.ping_sec)
                await ws.send(json.dumps({"method": "ping"}))
        except asyncio.CancelledError:
            raise
        except Exception:
            try:
                await ws.close()
            except Exception:
                pass

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            ptask = None
            try:
                async with ws_connect(self.ws_url, max_size=2**23, open_timeout=10,
                                      ping_interval=15, ping_timeout=15) as ws:
                    log.info("[%s] connected (official ws, %s)", self.name, self.coin)
                    connected_at = time.time()
                    self.book.clear()
                    self._snapped = False
                    await ws.send(json.dumps({
                        "method": "subscribe",
                        "subscription": {"type": "l2Book", "coin": self.coin,
                                         "fast": True}}))
                    ptask = asyncio.create_task(self._pinger(ws))
                    wd = asyncio.create_task(_silence_watchdog(self.name, ws, self.book,
                                                 connected_at))
                    try:
                        async for raw in ws:
                            # only a long-lived connection resets the backoff
                            # — a flapping link must not reconnect-storm
                            if time.time() - connected_at > 60.0:
                                backoff = 1.0
                            self._on_frame(json.loads(raw))
                            if stop.is_set():
                                break
                    finally:
                        wd.cancel()
                        with contextlib.suppress(BaseException):
                            await wd
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] ws error: %s — reconnect in %.0fs",
                            self.name, e, backoff)
            finally:
                if ptask is not None:
                    ptask.cancel()
                    with contextlib.suppress(BaseException):
                        await ptask
            self.book.ready = False
            self.notify()
            if stop.is_set():
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            backoff = min(backoff * 2, 30.0)


class PolyBookFeed:
    """Polymarket Perps public order book via REST polling.

    One /v1/info/book snapshot per second — recorder-grade freshness without
    a private websocket. Polymarket sits behind Cloudflare (a real browser UA
    is mandatory) and is outright blocked on many networks, so an http://
    proxy URL can be supplied. Every response is a full book snapshot, so a
    dropped poll cannot corrupt state — freshness, not continuity, is the
    only thing this feed has to guarantee.
    """

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

    def __init__(self, name: str, api_url: str, instrument_id: int,
                 book: OrderBook, session, notify: Callable[[], None],
                 proxy: Optional[str] = None, poll_sec: float = 1.0) -> None:
        self.name = name
        self.api_url = api_url
        self.instrument_id = instrument_id
        self.book = book
        self.session = session
        self.notify = notify
        self.proxy = proxy or None
        self.poll_sec = poll_sec
        self._snapped = False
        self._err_since: Optional[float] = None
        self._err_logged = False

    async def _poll_once(self) -> None:
        import aiohttp
        async with self.session.get(
                self.api_url + "/v1/info/book",
                params={"instrument_id": self.instrument_id},
                proxy=self.proxy,
                headers={"User-Agent": self.UA,
                         "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                raise RuntimeError(f"book poll HTTP {r.status}")
            j = await r.json(content_type=None)
        if not isinstance(j, dict) or "bids" not in j or "asks" not in j:
            raise RuntimeError(f"unexpected book shape: {str(j)[:80]}")
        self.book.apply_poly(j)
        if not self._snapped:
            self._snapped = True
            log.info("[%s] snapshot: %d bids / %d asks", self.name,
                     len(self.book.bids), len(self.book.asks))
        # `sequence` is NOT a gap signal here: the matching engine advances
        # it thousands of times per second and a 1 Hz poller always skips —
        # and it cannot lie anyway, every response is a full snapshot.
        self.notify()
        if self._err_logged:
            log.info("[%s] book feed recovered (%.0fs of errors)",
                     self.name, time.time() - (self._err_since or 0.0))
            self._err_logged = False
        self._err_since = None

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            t0 = time.monotonic()
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._err_since is None:
                    self._err_since = time.time()
                # one warning per failure spell, not one per poll
                if not self._err_logged:
                    self._err_logged = True
                    log.warning("[%s] book poll failed: %r — retrying "
                                "quietly (proxy down?)", self.name, e)
            await asyncio.sleep(max(0.05, self.poll_sec
                                    - (time.monotonic() - t0)))

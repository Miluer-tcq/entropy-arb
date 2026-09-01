"""Feed silence watchdog: cuts dead-but-open sockets, leaves live ones alone."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb import feeds  # noqa: E402
from entropy_arb.book import OrderBook  # noqa: E402


class FakeWs:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def test_watchdog_cuts_silent_feed():
    async def go():
        book, ws = OrderBook(), FakeWs()
        book.last_update_ts = time.time() - 1.0   # silent for a second
        t = asyncio.create_task(feeds._silence_watchdog(
            "T", ws, book, time.time() - 999, silence_sec=0.15,
            check_sec=0.05))
        await asyncio.sleep(0.5)
        assert ws.closed and t.done()
    asyncio.run(go())


def test_watchdog_spares_fresh_stream():
    async def go():
        book, ws = OrderBook(), FakeWs()
        book.touch()
        t = asyncio.create_task(feeds._silence_watchdog(
            "T", ws, book, time.time(), silence_sec=5.0, check_sec=0.05))
        await asyncio.sleep(0.25)
        assert not ws.closed
        t.cancel()
    asyncio.run(go())


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:35s} OK")

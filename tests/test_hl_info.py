"""HL /info budget hygiene: pacing, 429 retry+penalty, ttl coalescing."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiohttp  # noqa: E402

from entropy_arb.config import VenueConf  # noqa: E402
from entropy_arb.venue_hl import HLVenue  # noqa: E402


class Resp:
    def __init__(self, status=200, data=None, headers=None):
        self.status = status
        self._data = data if data is not None else {"ok": True}
        self.headers = headers or {}
        self.request_info = object()
        self.history = ()

    async def json(self):
        return self._data

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                self.request_info, self.history, status=self.status,
                message=str(self.status))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class Session:
    def __init__(self, seq):
        self.seq = list(seq)
        self.calls = 0

    def post(self, url, json=None, timeout=None):
        self.calls += 1
        item = self.seq.pop(0) if self.seq else Resp()
        return item


def make_venue(seq):
    conf = VenueConf(key="entropy", kind="hl", label="ENTROPY",
                     symbol="SNDK", fee_bps=0.9, fee_auto=False,
                     cap_usd=30.0, orders_per_min=60, hl_dex="io")
    s = Session(seq)
    v = HLVenue(conf, "https://x", "wss://y", s, 5.0)
    v.info_min_interval = 0.0
    return v, s


def test_ttl_cache_coalesces_identical_payloads():
    async def go():
        v, s = make_venue([Resp(200, {"accountValue": "12"})])
        payload = {"type": "clearinghouseState", "user": "u", "dex": "io"}
        a = await v._info(payload, ttl=5.0)
        b = await v._info(payload, ttl=5.0)
        assert a == b and s.calls == 1
        await v._info(payload, ttl=0.0)          # uncached: real request
        assert s.calls == 2
    asyncio.run(go())


def test_429_retries_and_raises_penalty():
    async def go():
        v, s = make_venue([Resp(429, headers={"Retry-After": "0"}),
                           Resp(200, {"ok": 1})])
        r = await v._info({"type": "x"})
        assert r == {"ok": 1} and s.calls == 2
        assert v._info_penalty >= 0.5
        # a non-429 error must not be retried or masked
        v2, s2 = make_venue([Resp(500)])
        try:
            await v2._info({"type": "y"})
            assert False, "expected raise"
        except aiohttp.ClientResponseError as e:
            assert e.status == 500 and s2.calls == 1
    asyncio.run(go())


def test_429_twice_gives_up_loudly():
    async def go():
        v, s = make_venue([Resp(429), Resp(429)])
        try:
            await v._info({"type": "z"})
            assert False, "expected raise"
        except aiohttp.ClientResponseError as e:
            assert e.status == 429 and s.calls == 2
    asyncio.run(go())


def test_calls_are_serialized():
    in_flight = {"n": 0, "max": 0}

    class Guard(Resp):
        async def __aenter__(self):
            in_flight["n"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["n"])
            await asyncio.sleep(0.02)
            return self

        async def __aexit__(self, *a):
            in_flight["n"] -= 1
            return False

    async def go():
        v, s = make_venue([])
        v.info_min_interval = 0.01
        seq = [Guard() for _ in range(4)]
        s.seq = seq
        await asyncio.gather(*(v._info({"type": "t%d" % i})
                               for i in range(4)))
        assert in_flight["max"] == 1          # never concurrent: one lock
    asyncio.run(go())


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:45s} OK")

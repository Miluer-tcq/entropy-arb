"""Lighter venue settle path: account feed terminal cache + the ready gate
that stopped false-unresolved re-hedges after the 09-01 03:50 outage."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.config import LIGHTER_PROFILES, VenueConf  # noqa: E402
from entropy_arb.venue_lighter import AccountOrdersFeed, LighterVenue  # noqa: E402


def make_venue(settle=10.0):
    conf = VenueConf(key="hedge", kind="lighter", label="LT", symbol="SNDK",
                     fee_bps=0.0, fee_auto=False, cap_usd=30.0,
                     orders_per_min=30,
                     lighter_profile=LIGHTER_PROFILES["lighter"])
    v = LighterVenue(conf, session=None, settle_timeout_sec=settle)
    v.signer = None
    v.market_id = 139
    return v


class FakeResp:
    code = 200
    message = ""


class FakeSigner:
    def __init__(self):
        self.sent = []

    async def create_order(self, **kw):
        self.sent.append(kw)
        return None, FakeResp(), None


def feed_for(v):
    f = AccountOrdersFeed("LT", "ws://unused", 139, 1, signer=None)
    v.orders_feed = f
    return f


def test_terminal_cache_resolves_late_watch():
    async def go():
        f = AccountOrdersFeed("LT", "ws://unused", 139, 1, signer=None)
        info = {"status": "filled", "filled_base": 2.0, "filled_quote": 3000.0,
                "avg_px": 1500.0}
        f._resolve(777, info)                 # settle arrives...
        got = await f.watch(777)              # ...before this order watches
        assert got["avg_px"] == 1500.0
    asyncio.run(go())


def test_send_waits_for_stream_then_settles_via_ws():
    async def go():
        v = make_venue()
        v.signer = FakeSigner()
        f = feed_for(v)
        f.ready.set()
        t = asyncio.create_task(v.send_taker(is_buy=True, qty=0.01,
                                             limit_px=1500.0))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if f._pending:
                break
        coi = next(iter(f._pending))
        f._resolve(coi, {"status": "filled", "filled_base": 0.01,
                         "filled_quote": 15.0, "avg_px": 1500.0})
        r = await t
        assert r["status"] == "filled" and r["filled_base"] == 0.01
        assert r["avg_px"] == 1500.0
    asyncio.run(go())


def test_send_while_stream_blind_is_unconfirmed_not_10s_hang():
    # stream down (ready cleared): send proceeds after the short gate,
    # reports unresolved for reconcile, and NEVER waits the settle timeout
    async def go():
        v = make_venue()
        v.signer = FakeSigner()
        f = feed_for(v)                       # ready never set
        t0 = time.monotonic()
        r = await v.send_taker(is_buy=True, qty=0.01, limit_px=1500.0)
        dt = time.monotonic() - t0
        assert len(v.signer.sent) == 1        # order still went out
        assert r["status"] == "sent-unconfirmed" and r["unresolved"]
        assert dt < 5.0, dt                   # gated ~2s, not settle_timeout
    asyncio.run(go())


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:45s} OK")

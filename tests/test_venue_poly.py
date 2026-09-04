"""Polymarket Perps record-only adapter: parsing, freshness, trade guards."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook                       # noqa: E402
from entropy_arb.config import VenueConf                     # noqa: E402
from entropy_arb.feeds import PolyBookFeed                   # noqa: E402
from entropy_arb.venue_poly import PolyVenue, TradeNotWired  # noqa: E402


class Resp:
    def __init__(self, status=200, data=None):
        self.status = status
        self._data = data if data is not None else {}

    async def json(self, content_type=None):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class Session:
    def __init__(self, seq):
        self.seq = list(seq)
        self.calls = 0
        self.kwargs = []

    def get(self, url, params=None, proxy=None, headers=None, timeout=None):
        self.calls += 1
        self.kwargs.append({"url": url, "params": params, "proxy": proxy,
                            "headers": headers})
        return self.seq.pop(0) if self.seq else Resp(200, {})


BOOK = {"instrument_id": 29,
        "bids": [["1571.6", "63.6"], ["1571.5", "494.7"]],
        "asks": [["1572.7", "494.7"], ["1572.8", "11.2"]],
        "sequence": 100}

INSTRUMENTS = [{"instrument_id": 1, "symbol": "BTC-USD"},
               {"instrument_id": 29, "symbol": "SNDK-USD",
                "quantity_decimals": 5, "price_decimals": 1,
                "min_notional": "10", "funding_interval": "1h"}]


def make_venue(seq):
    conf = VenueConf(key="hedge", kind="poly", label="POLY", symbol="SNDK",
                     fee_bps=4.0, fee_auto=False, cap_usd=1000.0,
                     orders_per_min=60)
    return PolyVenue(conf, "https://api", "http://127.0.0.1:7897",
                     Session(seq), 5.0)


def test_apply_poly_parses_string_levels():
    b = OrderBook()
    b.apply_poly({"bids": [["100.0", "1.0"], ["99.0", "0"]],
                  "asks": [["101.0", "2.0"]]})
    assert b.bids == {100.0: 1.0} and b.asks == {101.0: 2.0}
    assert b.ready and b.best_bid() == 100.0


def test_load_market_resolves_symbol():
    v = make_venue([Resp(200, INSTRUMENTS)])
    asyncio.run(v.load_market())
    assert v.instrument_id == 29
    assert v.size_decimals == 5 and v.price_decimals == 1
    assert v.min_base == 1e-5 and v.min_quote == 10.0


def test_missing_symbol_raises():
    v = make_venue([Resp(200, INSTRUMENTS)])
    v.conf = VenueConf(key="hedge", kind="poly", label="POLY", symbol="TSLA",
                       fee_bps=4.0, fee_auto=False, cap_usd=1.0,
                       orders_per_min=60)
    try:
        asyncio.run(v.load_market())
        assert False, "expected raise"
    except RuntimeError as e:
        assert "TSLA-USD" in str(e)


def test_poll_writes_book_via_proxy_and_notifies():
    v = make_venue([Resp(200, INSTRUMENTS)])
    asyncio.run(v.load_market())
    b = OrderBook()
    poked = []
    f = PolyBookFeed("POLY", "https://api", 29, b, v.session,
                     lambda: poked.append(1), proxy="http://p:7897")
    v.session.seq = [Resp(200, BOOK)]
    asyncio.run(f._poll_once())
    assert b.best_ask() == 1572.7 and poked
    assert v.session.kwargs[-1]["proxy"] == "http://p:7897"
    # a later poll with a huge sequence jump changes nothing: every response
    # is a full snapshot, so the feed tracks freshness, not continuity
    v.session.seq = [Resp(200, dict(BOOK, sequence=10**12))]
    asyncio.run(f._poll_once())
    assert b.best_bid() == 1571.6


def test_poll_records_error_state_then_recovers():
    v = make_venue([Resp(200, INSTRUMENTS)])
    asyncio.run(v.load_market())
    b = OrderBook()
    f = PolyBookFeed("POLY", "https://api", 29, b, v.session, lambda: None)

    # simulate a failure spell already recorded by run()'s except path:
    f._err_logged = True
    f._err_since = time.time() - 7.0
    # a good poll must clear both (and log recovery once)
    v.session.seq = [Resp(200, dict(BOOK, sequence=5))]
    asyncio.run(f._poll_once())
    assert f._err_logged is False and f._err_since is None
    assert b.best_bid() == 1571.6 and b.best_ask() == 1572.7


def test_poll_raises_on_bad_http_status():
    v = make_venue([Resp(200, INSTRUMENTS)])
    asyncio.run(v.load_market())
    f = PolyBookFeed("POLY", "https://api", 29, OrderBook(), v.session,
                     lambda: None)
    v.session.seq = [Resp(403, None)]
    try:
        asyncio.run(f._poll_once())
        assert False, "expected raise on 403"
    except RuntimeError as e:
        assert "403" in str(e)


def test_trade_paths_refuse():
    v = make_venue([Resp(200, INSTRUMENTS)])
    assert v.ready_to_trade() is False
    for coro in (v.send_taker(is_buy=True, qty=1.0, limit_px=1.0),
                 v.send_maker(is_buy=True, qty=1.0, limit_px=1.0)):
        try:
            asyncio.run(coro)
            assert False, "expected TradeNotWired"
        except TradeNotWired:
            pass
    try:
        v.init_signer()
        assert False
    except TradeNotWired:
        pass


def test_fetch_funding_from_ticker():
    v = make_venue([Resp(200, INSTRUMENTS),
                    Resp(200, [{"instrument_id": 29,
                                "funding_rate": "0.00000625"}])])
    asyncio.run(v.load_market())
    assert asyncio.run(v.fetch_funding()) == 0.00000625


def test_px_round_matches_price_decimals():
    v = make_venue([Resp(200, INSTRUMENTS)])
    asyncio.run(v.load_market())
    assert v.px_round(1571.567, False) == 1571.5
    assert v.px_round(1571.567, True) == 1571.6

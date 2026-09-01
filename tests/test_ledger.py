"""Ledger accounting: avg-cost realized/unrealized across adds, partial
closes, flips, fees, external re-anchor, persistence."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.ledger import Ledger  # noqa: E402


def approx(a, b, tol=1e-9):
    assert abs(a - b) <= tol, f"{a} != {b}"


def test_long_round_trip_after_fees():
    L = Ledger()
    L.fill("v", is_buy=True, qty=1.0, px=100.0, fee_bps=5.0)
    L.fill("v", is_buy=False, qty=1.0, px=101.0, fee_bps=5.0)
    approx(L.realized(), 1.0 - 0.05 - 0.0505, tol=1e-6)
    approx(L.fees(), 0.1005, tol=1e-6)
    approx(L.v["v"]["pos"], 0.0)
    approx(L.v["v"]["avg"], 0.0)


def test_adding_averages_cost():
    L = Ledger()
    L.fill("v", is_buy=True, qty=1.0, px=100.0, fee_bps=0.0)
    L.fill("v", is_buy=True, qty=1.0, px=102.0, fee_bps=0.0)
    approx(L.v["v"]["avg"], 101.0)
    L.fill("v", is_buy=False, qty=2.0, px=103.0, fee_bps=0.0)
    approx(L.realized(), 4.0)
    approx(L.v["v"]["pos"], 0.0)


def test_partial_close_then_unrealized():
    L = Ledger()
    L.fill("v", is_buy=True, qty=2.0, px=100.0, fee_bps=0.0)
    d = L.fill("v", is_buy=False, qty=1.0, px=95.0, fee_bps=0.0)
    approx(d, -5.0)
    approx(L.v["v"]["pos"], 1.0)
    approx(L.v["v"]["avg"], 100.0)          # avg unchanged by a reduction
    approx(L.unrealized({"v": 104.0}), 4.0)


def test_flip_opens_new_side_at_fill_price():
    L = Ledger()
    L.fill("v", is_buy=False, qty=2.0, px=100.0, fee_bps=0.0)   # short 2
    L.fill("v", is_buy=True, qty=3.0, px=90.0, fee_bps=0.0)
    approx(L.realized(), 20.0)               # short closed +10 each
    approx(L.v["v"]["pos"], 1.0)
    approx(L.v["v"]["avg"], 90.0)            # new long basis = flip price
    approx(L.unrealized({"v": 91.0}), 1.0)


def test_short_unrealized_sign():
    L = Ledger()
    L.fill("v", is_buy=False, qty=1.0, px=100.0, fee_bps=0.0)
    approx(L.unrealized({"v": 99.0}), 1.0)    # short profits when mark falls
    approx(L.unrealized({"v": 101.0}), -1.0)


def test_zero_qty_fill_charges_only_fee():
    L = Ledger()
    d = L.fill("v", is_buy=True, qty=0.0, px=100.0, fee_bps=10.0)
    approx(d, 0.0)
    approx(L.realized(), 0.0)


def test_reanchor_prices_carry_at_mark():
    L = Ledger()
    L.reanchor("v", 5.0, 100.0)
    approx(L.v["v"]["avg"], 100.0)
    approx(L.unrealized({"v": 100.0}), 0.0)
    approx(L.realized(), 0.0)
    L.reanchor("v", 0.0, 100.0)
    approx(L.v["v"]["avg"], 0.0)


def test_snapshot_restore_round_trip():
    L = Ledger()
    L.fill("a", is_buy=True, qty=2.0, px=50.0, fee_bps=3.0)
    L.fill("b", is_buy=False, qty=1.0, px=80.0, fee_bps=0.0)
    snap = L.snapshot()
    L2 = Ledger()
    L2.restore(snap)
    approx(L2.realized(), L.realized())
    approx(L2.fees(), L.fees())
    approx(L2.v["a"]["avg"], 50.0)
    approx(L2.v["b"]["pos"], -1.0)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")

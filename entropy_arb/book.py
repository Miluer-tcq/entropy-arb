"""Order book state and fee-aware arbitrage sizing.

One book class serves both feed protocols: zkLighter sends a snapshot plus
diffs (dict maintenance), Hyperliquid's l2Book sends full snapshots.
Freshness is connection-based (any inbound ws frame touches alive_ts): a quiet
market is not stale, only a dead feed is.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

Level = Tuple[float, float]


class OrderBook:
    def __init__(self) -> None:
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.ready = False
        self.last_update_ts = 0.0
        self.alive_ts = 0.0

    def touch(self) -> None:
        self.alive_ts = time.time()

    def clear(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.ready = False

    # ---- zkLighter snapshot + diff ----
    def apply_lighter(self, ob: dict, snapshot: bool) -> None:
        if snapshot:
            self.bids.clear()
            self.asks.clear()
        for name, side in (("bids", self.bids), ("asks", self.asks)):
            for lvl in ob.get(name) or []:
                px, sz = float(lvl["price"]), float(lvl["size"])
                if sz <= 0:
                    side.pop(px, None)
                else:
                    side[px] = sz
        self.ready = True
        self.last_update_ts = time.time()
        self.touch()

    # ---- Hyperliquid full snapshot ----
    def apply_hl(self, levels: list) -> None:
        self.bids = {float(l["px"]): float(l["sz"])
                     for l in levels[0] if float(l["sz"]) > 0}
        self.asks = {float(l["px"]): float(l["sz"])
                     for l in levels[1] if float(l["sz"]) > 0}
        self.ready = True
        self.last_update_ts = time.time()
        self.touch()

    # ---- Polymarket Perps full snapshot (list of [price, size] strings) ----
    def apply_poly(self, levels: dict) -> None:
        self.bids = {float(p): float(s)
                     for p, s in levels.get("bids") or [] if float(s) > 0}
        self.asks = {float(p): float(s)
                     for p, s in levels.get("asks") or [] if float(s) > 0}
        self.ready = True
        self.last_update_ts = time.time()
        self.touch()

    def sorted_bids(self) -> List[Level]:
        return sorted(self.bids.items(), key=lambda kv: -kv[0])

    def sorted_asks(self) -> List[Level]:
        return sorted(self.asks.items())

    def best_bid(self) -> Optional[float]:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Optional[float]:
        return min(self.asks) if self.asks else None

    def mid(self) -> Optional[float]:
        if not (self.bids and self.asks):
            return None
        return (max(self.bids) + min(self.asks)) / 2.0

    def is_fresh(self, max_age_sec: float) -> bool:
        return self.ready and bool(self.bids) and bool(self.asks) and (
            time.time() - self.alive_ts <= max_age_sec)


def floor_step(x: float, step: float) -> float:
    return round(math.floor(x / step + 1e-9) * step, 12)


def ceil_step(x: float, step: float) -> float:
    return round(math.ceil(x / step - 1e-9) * step, 12)


def crossable_base(asks: List[Level], bids: List[Level], threshold: float,
                   buy_fee: float = 0.0, sell_fee: float = 0.0) -> Tuple[float, float]:
    """Walk both books level by level and return (base qty, buy notional) that
    can be crossed while every marginal slice still clears fees + threshold."""
    qty = 0.0
    buy_notional = 0.0
    i = j = 0
    a_px = a_rem = 0.0
    b_px = b_rem = 0.0
    while True:
        if a_rem <= 0:
            if i >= len(asks):
                break
            a_px, a_rem = asks[i]
            i += 1
        if b_rem <= 0:
            if j >= len(bids):
                break
            b_px, b_rem = bids[j]
            j += 1
        if b_px * (1.0 - sell_fee) < a_px * (1.0 + buy_fee) * (1.0 + threshold):
            break
        take = min(a_rem, b_rem)
        qty += take
        buy_notional += take * a_px
        a_rem -= take
        b_rem -= take
    return qty, buy_notional


def walk_depth(levels: List[Level], qty: float) -> Tuple[float, float]:
    remaining = qty
    notional = 0.0
    marginal_px = levels[0][0]
    for px, sz in levels:
        take = min(remaining, sz)
        notional += take * px
        marginal_px = px
        remaining -= take
        if remaining <= 1e-12:
            break
    return marginal_px, notional


@dataclass
class ArbPlan:
    qty: float
    buy_limit: float
    sell_limit: float
    buy_notional: float
    sell_notional: float
    q_max: float
    q_max_notional: float
    top_premium_bps: float
    marginal_premium_bps: float
    buy_fee: float
    sell_fee: float

    @property
    def gross_edge_usd(self) -> float:
        return self.sell_notional - self.buy_notional

    @property
    def exp_edge_usd(self) -> float:
        return (self.sell_notional * (1.0 - self.sell_fee)
                - self.buy_notional * (1.0 + self.buy_fee))


def plan_arb(buy_book: OrderBook, sell_book: OrderBook, *, threshold_bps: float,
             buy_fee_bps: float, sell_fee_bps: float, take_fraction: float,
             cap_notional: float, min_base: float, min_notional: float,
             size_step: float):
    """Size a two-leg taker slice: buy on buy_book, sell on sell_book.

    A slice qualifies when the executable premium (sell bid over buy ask)
    clears both venues' taker fees plus threshold_bps. Returns
    (ArbPlan | None, reason).
    """
    asks = buy_book.sorted_asks()
    bids = sell_book.sorted_bids()
    if not asks or not bids:
        return None, "empty_book"
    threshold = threshold_bps / 1e4
    buy_fee = buy_fee_bps / 1e4
    sell_fee = sell_fee_bps / 1e4
    top_premium_bps = (bids[0][0] / asks[0][0] - 1.0) * 1e4
    if bids[0][0] * (1.0 - sell_fee) < asks[0][0] * (1.0 + buy_fee) * (1.0 + threshold):
        return None, "no_edge"
    q_max, q_max_notional = crossable_base(asks, bids, threshold, buy_fee, sell_fee)
    if q_max <= 0:
        return None, "no_edge"
    target = min(q_max * take_fraction, cap_notional / asks[0][0])
    target = floor_step(target, size_step)
    if target * asks[0][0] < min_notional:
        # floor_step can shave the last cent off a just-legal slice (top-of-
        # book depth is plenty, but rounded qty values at $9.88): bump up to
        # the smallest step that clears the venue minimum when the crossing
        # genuinely carries that size — but one step of tolerance only, never
        # reviving a slice the notional cap had legitimately refused
        bumped = ceil_step(min_notional / asks[0][0], size_step)
        if bumped <= q_max and bumped * asks[0][0] <= cap_notional \
                + size_step * asks[0][0] + 1e-9:
            target = bumped
    if target < min_base:
        return None, "below_min_base"
    buy_limit, buy_notional = walk_depth(asks, target)
    sell_limit, sell_notional = walk_depth(bids, target)
    if buy_notional < min_notional or sell_notional < min_notional:
        return None, "below_min_notional"
    return ArbPlan(
        qty=target, buy_limit=buy_limit, sell_limit=sell_limit,
        buy_notional=buy_notional, sell_notional=sell_notional,
        q_max=q_max, q_max_notional=q_max_notional,
        top_premium_bps=top_premium_bps,
        marginal_premium_bps=(sell_limit / buy_limit - 1.0) * 1e4,
        buy_fee=buy_fee, sell_fee=sell_fee,
    ), "ok"

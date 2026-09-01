"""Realized / unrealized PnL ledger for the arb engine.

Average-cost accounting per venue, fed by EVERY fill the engine sends
(arb legs, hedges, maker rests, flatten): fees and hedge losses land in
``realized`` the moment they are paid, instead of hiding inside one MTM
number. Unrealized is (mark - avg_cost) * position — correct for either
position sign, since a short's avg is its average entry (sell) price.

Position changes that arrive from the exchange (reconcile) rather than
from a fill we placed have unknown cost basis: they re-anchor avg to the
current mark so later exits measure real slippage from that instant
instead of inventing a phantom PnL against zero.
"""
from __future__ import annotations

from typing import Dict, Optional


class Ledger:
    def __init__(self) -> None:
        # venue key -> {"pos": signed base, "avg": avg cost of open side,
        #               "realized": closed PnL net of fees, "fees": fees paid}
        self.v: Dict[str, Dict[str, float]] = {}

    def _get(self, key: str) -> Dict[str, float]:
        return self.v.setdefault(key, {"pos": 0.0, "avg": 0.0,
                                       "realized": 0.0, "fees": 0.0})

    # ------------------------------------------------------------- accounting

    def fill(self, key: str, *, is_buy: bool, qty: float, px: float,
             fee_bps: float) -> float:
        """Record a filled quantity; returns the realized PnL delta."""
        s = self._get(key)
        fee = qty * px * fee_bps / 1e4
        s["fees"] += fee
        pos = s["pos"]
        if qty <= 0:
            s["realized"] -= fee
            return -fee
        d = 0.0
        sgn = 1.0 if is_buy else -1.0
        new = pos + sgn * qty
        if pos == 0.0 or pos * sgn > 0:          # open or add to same side
            s["avg"] = (abs(pos) * s["avg"] + qty * px) / (abs(pos) + qty)
        else:                                    # reduce, maybe flip
            closed = min(abs(pos), qty)
            d = closed * (px - s["avg"]) * (1.0 if pos > 0 else -1.0)
            if abs(new) <= 1e-12:
                s["avg"] = 0.0
            elif (new > 0) != (pos > 0):         # flip opens at this price
                s["avg"] = px
            # partial close on the same side: avg cost unchanged
        s["pos"] = new
        s["realized"] += d - fee
        return d - fee

    def reanchor(self, key: str, pos: float, mark: Optional[float]) -> None:
        """External position sync (reconcile): unknown basis, so price the
        open position at mark; unrealized becomes 0 from here forward."""
        s = self._get(key)
        s["pos"] = pos
        s["avg"] = mark if (mark and abs(pos) > 1e-12) else 0.0

    # ------------------------------------------------------------- reporting

    def realized(self) -> float:
        return sum(s["realized"] for s in self.v.values())

    def fees(self) -> float:
        return sum(s["fees"] for s in self.v.values())

    def unrealized(self, marks: Dict[str, Optional[float]]) -> float:
        u = 0.0
        for key, s in self.v.items():
            m = marks.get(key)
            if m is None or abs(s["pos"]) < 1e-12:
                continue
            u += (m - s["avg"]) * s["pos"]
        return u

    def snapshot(self) -> dict:
        return {"v": {k: dict(x) for k, x in self.v.items()}}

    def restore(self, snap: dict) -> None:
        for k, x in (snap.get("v") or {}).items():
            self.v[k] = {"pos": float(x.get("pos", 0.0)),
                         "avg": float(x.get("avg", 0.0)),
                         "realized": float(x.get("realized", 0.0)),
                         "fees": float(x.get("fees", 0.0))}

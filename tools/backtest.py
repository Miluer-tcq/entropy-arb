#!/usr/bin/env python3
"""Backtest band configurations against recorded minute data.

Reads the recorder's minutes.csv and simulates round trips of the band
strategy against each minute's executable edges (sell_edge/buy_edge), with
position caps, an inventory ladder and per-slice sizing approximated from
the recorded top-of-book. Outputs round trips, net bps and PnL per band so
threshold changes can be evaluated numerically before going live.

Approximations (minute data cannot do better):
  * a direction "fires" in a minute when its mean executable edge clears the
    net hurdle; the fill price is that minute's mean edge (conservative vs
    its max, which the recorder also stores);
  * the unwind fires on the opposite crossing as in the live engine;
  * funding and intraminute path are not modeled.

Usage:
  python tools/backtest.py --csv logs/lighter_minutes.csv
  python tools/backtest.py --csv logs/xyz_minutes.csv --fees-bps 1.0 \
      --cap-usd 30 --midline -4.6 --upper 5 --lower 6
"""
from __future__ import annotations

import argparse
import csv
import sys


def load_rows(path: str, hours: float):
    import time
    cutoff = time.time() - hours * 3600 if hours else 0.0
    rows = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                ts = float(r["minute_ts"])
                if ts and ts < cutoff:
                    continue
                row = {
                    "ts": ts,
                    "sell_mean": float(r["sell_edge_mean_bps"]),
                    "buy_mean": float(r["buy_edge_mean_bps"]),
                    "samples": int(r["samples"]),
                }
                # optional MAX columns power the stale-book ceiling model
                try:
                    row["sell_max"] = float(r["sell_edge_max_bps"])
                    row["buy_max"] = float(r["buy_edge_max_bps"])
                except (KeyError, TypeError, ValueError):
                    row["sell_max"] = row["sell_mean"]
                    row["buy_max"] = row["buy_mean"]
                rows.append(row)
            except (KeyError, TypeError, ValueError):
                continue
    rows.sort(key=lambda r: r["ts"])
    return rows


def simulate(rows, midline: float, upper: float, lower: float,
             fees_bps: float, cap_usd: float, slice_usd: float,
             ceiling_bps: float = 0.0, floor_bps: float = 0.0,
             decay_bps: float = 0.0) -> dict:
    """Walk the minutes, mirroring the live engine's state machine:
    entry on a direction's edge clearing its net hurdle, exit on the
    opposite crossing, no stop (inventory is carried, never force-closed).
    One slice at a time — adding-while-holding is not simulated.

    Live-engine gates, off by default:
      ceiling_bps: signals whose MAX executable premium exceeds
        midline+band+fees+ceiling are the stale-book trap (09-01: every
        one cancelled a leg) — skipped entirely;
      floor_bps:   a fired entry whose MEAN post-fee edge is below this
        is the thin-edge churn (08-31 lesson) — skipped;
      decay_bps:   realized fill haircut applied to each round trip's
        net bps (plan->fill decay measured at median ~1-2bps on fat
        entries; 0 keeps the old optimistic model)."""
    notional = min(slice_usd, cap_usd)
    pos = None             # None = flat; "short" | "long"
    entry_edge = 0.0
    sell_fires = buy_fires = trips = exits = 0
    ceil_skips = floor_skips = 0
    net_bps = 0.0
    pnl_usd = 0.0

    def hurdle(sell_dir: bool) -> float:
        return ((midline + upper) if sell_dir else (lower - midline)) + fees_bps

    def blocked(r, sell_dir: bool) -> bool:
        nonlocal ceil_skips, floor_skips
        mx = r["sell_max"] if sell_dir else r["buy_max"]
        mn = r["sell_mean"] if sell_dir else r["buy_mean"]
        # the max columns ARE the per-minute executable premium (sell:
        # bid_e/ask_h, buy: bid_h/ask_e) — the same quantity the live
        # ceiling compares against plan.top_premium_bps
        if ceiling_bps > 0 and mx > ceiling_bps:
            ceil_skips += 1
            return True
        if floor_bps > 0 and mn - fees_bps < floor_bps:
            floor_skips += 1
            return True
        return False

    for r in rows:
        if r["samples"] < 10:
            continue
        sell_fire = r["sell_mean"] >= hurdle(True)
        buy_fire = r["buy_mean"] >= hurdle(False)
        if pos is None:
            if sell_fire and blocked(r, True):
                sell_fire = False
            if buy_fire and blocked(r, False):
                buy_fire = False
        sell_fires += sell_fire
        buy_fires += buy_fire

        if pos is None:
            if sell_fire:
                pos, entry_edge = "short", r["sell_mean"]
                trips += 1
            elif buy_fire:
                pos, entry_edge = "long", r["buy_mean"]
                trips += 1
        else:
            long_pos = pos == "long"
            unwind = buy_fire if long_pos else sell_fire
            if unwind:
                exit_edge = r["buy_mean"] if long_pos else r["sell_mean"]
                rt = entry_edge + exit_edge - 2.0 * decay_bps  # both sides net
                net_bps += rt
                pnl_usd += notional * rt / 1e4
                pos, entry_edge = None, 0.0
                exits += 1
    # a still-open position has unrealized PnL; report separately
    open_left = pos is not None
    return {
        "sell_fires": sell_fires, "buy_fires": buy_fires,
        "trips": trips, "exits": exits, "open": open_left,
        "ceiling_skips": ceil_skips, "floor_skips": floor_skips,
        "net_bps": net_bps, "pnl_usd": pnl_usd,
    }


def main() -> None:
    p = argparse.ArgumentParser(
        description="band backtest over recorder minutes.csv")
    p.add_argument("--csv", default="logs/minutes.csv")
    p.add_argument("--hours", type=float, default=0.0,
                   help="restrict to the most recent N hours (0 = all)")
    p.add_argument("--min-samples", type=int, default=10,
                   help="skip minutes with fewer fresh samples")
    p.add_argument("--fees-bps", type=float, default=0.0,
                   help="sum of both venues' taker fees")
    p.add_argument("--cap-usd", type=float, default=30.0,
                   help="position cap per venue (as in config)")
    p.add_argument("--slice-usd", type=float, default=10.0,
                   help="per-slice notional (as max_order_notional_usd)")
    p.add_argument("--midline", type=float, default=None,
                   help="single config to test (default: sweep a grid)")
    p.add_argument("--upper", type=float, default=None)
    p.add_argument("--lower", type=float, default=None)
    p.add_argument("--ceiling-bps", type=float, default=0.0,
                   help="stale-book trap gate mirroring live "
                        "max_top_premium_bps (0=off)")
    p.add_argument("--floor-bps", type=float, default=0.0,
                   help="thin-edge gate mirroring live min_net_edge_bps "
                        "(0=off)")
    p.add_argument("--decay-bps", type=float, default=0.0,
                   help="plan->fill haircut per side, from live stats")
    args = p.parse_args()

    rows = load_rows(args.csv, args.hours)
    if len(rows) < 60:
        print(f"not enough data in {args.csv} ({len(rows)} usable minutes)",
              file=sys.stderr)
        sys.exit(2)

    if args.midline is not None:
        grid = [(args.midline, args.upper, args.lower)]
    else:
        grid = []
        for mid in (-6.0, -4.6, -3.0, -1.5, 0.0):
            for band in (4.0, 5.0, 6.0, 8.0):
                grid.append((mid, band, band))

    gates = []
    if args.ceiling_bps:
        gates.append(f"ceiling={args.ceiling_bps}bps")
    if args.floor_bps:
        gates.append(f"floor={args.floor_bps}bps")
    if args.decay_bps:
        gates.append(f"decay={args.decay_bps}bps/side")
    hdr = (f"{'mid':>6} {'up':>5} {'lo':>5} | {'sellF':>6} {'buyF':>6} "
           f"{'trips':>6} {'exits':>6} {'open':>4} | {'ceilX':>6} "
           f"{'flrX':>6} | {'net$':>8} {'netbps':>8} {'$/day':>7}")
    print(f"data: {args.csv}  minutes={len(rows)}  fees={args.fees_bps}bps  "
          f"cap=${args.cap_usd}  slice=${args.slice_usd}  "
          f"gates: {', '.join(gates) or 'none'}")
    print(hdr)
    print("-" * len(hdr))
    days = (rows[-1]["ts"] - rows[0]["ts"]) / 86400.0 or 1.0
    for mid, up, lo in grid:
        s = simulate(rows, mid, up, lo, args.fees_bps, args.cap_usd,
                     args.slice_usd, args.ceiling_bps, args.floor_bps,
                     args.decay_bps)
        print(f"{mid:>6.1f} {up:>5.1f} {lo:>5.1f} | {s['sell_fires']:>6} "
              f"{s['buy_fires']:>6} {s['trips']:>6} {s['exits']:>6} "
              f"{'YES' if s['open'] else '-':>4} | {s['ceiling_skips']:>6} "
              f"{s['floor_skips']:>6} | {s['pnl_usd']:>8.4f} "
              f"{s['net_bps']:>8.1f} {s['pnl_usd'] / days:>7.4f}")
    print("\nApproximations: minute-mean edges as fills, one slice at a "
          "time, no adding ladder, no funding, no intraminute path; "
          "ceiling/floor mirror the live gates (08-31/09-01 lessons); "
          "treat relative comparisons as signal, absolute PnL as "
          "optimistic.")


if __name__ == "__main__":
    main()

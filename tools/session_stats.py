#!/usr/bin/env python3
"""Bucket recorded minutes by US cash-equity session phases and compare.

Run:  python3 tools/session_stats.py [--csv logs/lighter_minutes.csv] [--hours N]

Phases (America/New_York): pre 04:00-09:30, regular 09:30-16:00,
post 16:00-20:00, overnight 20:00-04:00, weekends Sat/Sun all day.
Reports premium median/sigma and p90 of per-minute maximum executable
buy/sell edge — the raw material for per-window band calibration.
"""
import argparse
import csv
import statistics as st
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def phase(dt):
    if dt.weekday() >= 5:
        return "5_weekend"
    m = dt.hour * 60 + dt.minute
    if 4 * 60 <= m < 9 * 60 + 30:
        return "1_premarket"
    if 9 * 60 + 30 <= m < 16 * 60:
        return "2_regular"
    if 16 * 60 <= m < 20 * 60:
        return "3_postmarket"
    return "4_overnight"


def pctl(xs, f):
    return xs[min(len(xs) - 1, int(f * len(xs)))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="logs/lighter_minutes.csv")
    ap.add_argument("--hours", type=float, default=0)
    args = ap.parse_args()

    import time
    cutoff = time.time() - args.hours * 3600 if args.hours else 0
    buckets = {}
    with open(args.csv, newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                ts = float(r["minute_ts"])
                prem = float(r["premium_close_bps"])
                se = float(r["sell_edge_max_bps"])
                be = float(r["buy_edge_max_bps"])
            except (KeyError, TypeError, ValueError):
                continue
            if ts < cutoff:
                continue
            buckets.setdefault(phase(datetime.fromtimestamp(ts, ET)), []).append(
                (prem, se, be))

    print(f"{'phase':13s} {'n':>5s} {'med':>7s} {'sd':>6s} "
          f"{'buyE p50':>9s} {'buyE p90':>9s} {'buyE p99':>9s} "
          f"{'sellE p90':>10s} {'sellE p99':>10s}")
    for k in sorted(buckets):
        rows = buckets[k]
        n = len(rows)
        prem = sorted(x[0] for x in rows)
        be = sorted(x[2] for x in rows)
        se = sorted(x[1] for x in rows)
        print(f"{k:13s} {n:5d} {st.median(prem):+7.2f} {st.pstdev(prem):6.2f} "
              f"{pctl(be, .5):+9.2f} {pctl(be, .9):+9.2f} {pctl(be, .99):+9.2f} "
              f"{pctl(se, .9):+10.2f} {pctl(se, .99):+10.2f}"
              + ("   (small-n, indicative)" if n < 120 else ""))


if __name__ == "__main__":
    sys.exit(main())

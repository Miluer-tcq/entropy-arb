#!/usr/bin/env python3
"""Is the structural HL-Entropy discount a funding (carry) trade?

Pulls io:SNDK funding (HL) and market-139 funding (Lighter) plus a 48h
history, and compares their magnitude to the observed premium midline drift
in the recorder CSV. If the -5..-7bps discount were just expected carry,
cumulative funding over the same window would explain the drift; if it's a
structural basis (fragmented liquidity/margin), funding stays ~0 while the
midline wanders by several bps. Read-only.

Usage: python tools/funding_check.py [--csv logs/lighter_minutes.csv]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from statistics import median

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


async def run(window_h: float) -> None:
    import aiohttp
    from entropy_arb.monitor import read_minute_rows

    t0ms = int((time.time() - window_h * 3600) * 1000)
    async with aiohttp.ClientSession() as s:
        async with s.post("https://api.hyperliquid.xyz/info",
                          json={"type": "metaAndAssetCtxs",
                                "dex": "io"}) as r:
            meta, ctxs = await r.json()
        names = [u["name"] for u in meta.get("universe", [])]
        hl_now = hl_oi = None
        if "io:SNDK" in names:
            i = names.index("io:SNDK")
            hl_now = float(ctxs[i]["funding"])
            hl_oi = ctxs[i].get("openInterest")
        async with s.post("https://api.hyperliquid.xyz/info",
                          json={"type": "fundingHistory", "coin": "io:SNDK",
                                "startTime": t0ms}) as r:
            hist = await r.json() or []
        try:
            async with s.get(
                "https://mainnet.zklighter.elliot.ai/api/v1/"
                "funding-rates?market_index=139&interval=hour&page_size=200"
            ) as r:
                lt = await r.json()
        except Exception as e:  # endpoint shape can shift; never fatal
            lt, e = None, repr(e)

    hl_cum = sum(float(h.get("fundingRate", 0.0)) for h in hist)
    print(f"HL  io:SNDK  now {hl_now*1e4:+.2f} bps/h ({hl_now*24e4:+.1f} "
          f"bps/day)  OI={hl_oi}")
    print(f"HL  {window_h:.0f}h cumulative funding: {hl_cum*1e4:+.2f} bps "
          f"({len(hist)} hourly prints)")
    if lt:
        rows = (lt.get("latestFundingRates") or lt.get("fundingRates") or [])
        vals = [float(x.get("funding_rate") or x.get("rate") or 0.0)
                for x in rows]
        if vals:
            print(f"LT  market139 {len(vals)} rows: "
                  f"latest {vals[-1]*1e4:+.2f} bps/h, "
                  f"sum {sum(vals)*1e4:+.2f} bps")
    else:
        print("LT  market139 funding: unavailable (continues anyway)")

    rows = read_minute_rows(CSV, window_h) if CSV else []
    prem = [r[1] for r in rows if r[1] is not None]
    if len(prem) >= 30:
        n = len(prem)
        first, last = median(prem[:n // 3]), median(prem[2 * n // 3:])
        level = median(prem)
        print(f"premium level: {level:+.2f} bps median | drift over window: "
              f"{first:+.2f} -> {last:+.2f} = {last-first:+.2f} bps")
        # If the discount were pure expected carry, funding would need to be
        # earned at a rate that prices the LEVEL in over a day: a -6bps
        # discount needs ~-6bps/day of funding TO the long-entropy side.
        carry_day = hl_cum * 1e4 * 24.0 / max(window_h, 1.0)
        if abs(level) < 1.0:
            print("VERDICT: no structural discount to explain (flat market)")
        elif abs(carry_day) >= 0.5 * abs(level):
            print(f"VERDICT: funding {carry_day:+.1f} bps/day is the right "
                  f"order of magnitude for the {level:+.1f}bps discount — "
                  f"tilt bands toward the carry direction")
        else:
            print(f"VERDICT: funding {carry_day:+.1f} bps/day << discount "
                  f"{level:+.1f} bps — the gap is a STRUCTURAL basis "
                  f"(fragmented liquidity), mean-reversion bands stay right "
                  f"as-is; do NOT tilt for carry")


def main() -> None:
    global CSV
    p = argparse.ArgumentParser()
    p.add_argument("--csv", default="logs/lighter_minutes.csv")
    p.add_argument("--hours", type=float, default=48.0)
    a = p.parse_args()
    CSV = a.csv
    asyncio.run(run(a.hours))


if __name__ == "__main__":
    main()

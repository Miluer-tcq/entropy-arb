"""Polymarket Perps MM-rewards competition monitor (READ-ONLY).

Phase A of the liquidity-rewards strategy: verify, with repeated samples,
whether the 5/10/20bp scoring tiers of the quiet markets (ZM, DELL, WLD,
CXMT) are truly unclaimed — a single snapshot can catch incumbents away.
Also records each market's realized taker flow via the public trades feed,
which sizes the 1%-maker-share eligibility hurdle.

Rewards program (docs: perps/liquidity-rewards): $75k/day split evenly
across active markets (~$1.1k each, 67 markets), scored on resting
two-sided notional within 20bps of mid (tiers 5/10/20bp w/ weights
1/0.25/0.1, $100k/side/tier cap), uptime-weighted; eligibility =
maker share >= 1% of the market's trailing 7d maker volume.

    python tools/poly_mm_monitor.py               # run until Ctrl-C
    python tools/poly_mm_monitor.py --once

Writes logs/poly_mm_monitor.csv (one row per market per poll) and
logs/poly_mm_trades.csv (every observed trade, once). No keys, no orders.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp  # noqa: E402

API = "https://api.perpetuals.polymarket.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

# instrument ids resolved from /v1/info/instruments on 2026-09-05
MARKETS = {48: "ZM", 49: "DELL", 56: "WLD", 50: "CXMT",
           11: "MU", 29: "SNDK"}          # MU/SNDK = saturated controls

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_proxy() -> str:
    try:
        for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
            k, _, v = line.partition("=")
            if k.strip() == "POLY_PROXY":
                return v.strip()
    except OSError:
        pass
    return os.environ.get("POLY_PROXY", "")


BOOK_HDR = ["ts_utc", "symbol", "iid", "mid", "l1_spread_bps",
            "bid_5bp_usd", "ask_5bp_usd", "bid_10bp_usd", "ask_10bp_usd",
            "bid_20bp_usd", "ask_20bp_usd", "trades_last_1h_usd"]
TRD_HDR = ["seen_utc", "trade_id", "iid", "symbol", "side", "price",
           "quantity", "notional_usd", "ts_utc_trade"]


def ensure(path: str, header: list) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(header)


def tier_usd(levels, mid, bps):
    lim = mid * bps / 1e4
    if levels and levels[0][0] < mid:      # bids
        return sum(p * q for p, q in levels if p >= mid - lim)
    return sum(p * q for p, q in levels if p <= mid + lim)


async def poll(s, path, **params):
    for a in range(3):
        try:
            async with s.get(API + path, params=params, proxy=load_proxy() or None,
                             headers={"User-Agent": UA,
                                      "Accept": "application/json"},
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
                await asyncio.sleep(1.5 * (a + 1))
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
            await asyncio.sleep(1.5 * (a + 1))
    return None


async def cycle(s, seen_trades: set) -> list:
    out = []
    now = time.time()
    for iid, sym in MARKETS.items():
        book = await poll(s, "/v1/info/book", instrument_id=iid)
        tr = await poll(s, "/v1/info/trades", instrument_id=iid)
        if not book or "bids" not in book:
            print(f"[{sym}] book poll failed", flush=True)
            continue
        bids = sorted(((float(p), float(q)) for p, q in book["bids"]),
                      key=lambda x: -x[0])
        asks = sorted(((float(p), float(q)) for p, q in book["asks"]),
                      key=lambda x: x[0])
        if not bids or not asks:
            continue
        mid = (bids[0][0] + asks[0][0]) / 2
        spr = (asks[0][0] / bids[0][0] - 1) * 1e4
        vol1h = 0.0
        rows_new = []
        for t in (tr or {}).get("data") or []:
            ts = int(t["timestamp"]) / 1000.0
            nt = float(t["price"]) * float(t["quantity"])
            if now - ts <= 3600:
                vol1h += nt
            tid = str(t.get("trade_id"))
            if tid not in seen_trades:
                seen_trades.add(tid)
                rows_new.append([time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime()),
                                 tid, iid, sym, t.get("side"), t["price"],
                                 t["quantity"], round(nt, 2),
                                 time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime(ts))])
        if rows_new:
            with open(os.path.join(ROOT, "logs", "poly_mm_trades.csv"),
                      "a", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerows(rows_new)
        row = [time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), sym,
               str(iid), f"{mid:.4g}", f"{spr:.2f}"]
        for bps in (5, 10, 20):
            row += [f"{tier_usd(bids, mid, bps):.0f}",
                    f"{tier_usd(asks, mid, bps):.0f}"]
        row.append(f"{vol1h:.0f}")
        out.append(row)
    return out


async def amain(once: bool) -> None:
    ensure(os.path.join(ROOT, "logs", "poly_mm_monitor.csv"), BOOK_HDR)
    ensure(os.path.join(ROOT, "logs", "poly_mm_trades.csv"), TRD_HDR)
    seen: set = set()
    async with aiohttp.ClientSession() as s:
        while True:
            t0 = time.monotonic()
            rows = await cycle(s, seen)
            with open(os.path.join(ROOT, "logs", "poly_mm_monitor.csv"),
                      "a", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerows(rows)
            for row in rows:
                print(",".join(row), flush=True)
            if once:
                return
            await asyncio.sleep(max(1.0, 60.0 - (time.monotonic() - t0)))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    asyncio.run(amain(ap.parse_args().once))

"""Standalone HL+Polymarket minute recorder for the phase-1 evaluation.

Collects Entropy<->Polymarket premium bars (same CSV schema as the engine's
recorder) for ~1 week, WITHOUT the trading engine — so a proxy blip, HL TLS
reset, or laptop reboot of this side-project recorder can never disturb the
live Lighter instance.

    python tools/poly_recorder.py --check     # verify SNDK instrument id
    python tools/poly_recorder.py --once      # one sample, print, exit
    python tools/poly_recorder.py             # run until Ctrl-C

Writes logs/poly_minutes.csv. Reads POLY_PROXY from .env (Polymarket needs
a local http proxy + browser UA on blocked networks; see venue_poly).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp  # noqa: E402

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.feeds import PolyBookFeed  # noqa: E402
from entropy_arb.recorder import MinuteRecorder  # noqa: E402

HL_INFO = "https://api.hyperliquid.xyz/info"
POLY_API = "https://api.perpetuals.polymarket.com"
COIN = "io:SNDK"
INSTRUMENT = 29          # SNDK-USD (resolved 2026-09-04; re-verify: --check)
STALE_SEC = 10.0

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_proxy() -> str:
    """POLY_PROXY from .env or the environment ('' = direct)."""
    try:
        for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
            k, _, v = line.partition("=")
            if k.strip() == "POLY_PROXY":
                return v.strip()
    except OSError:
        pass
    return os.environ.get("POLY_PROXY", "")


async def hl_loop(session: aiohttp.ClientSession, book: OrderBook,
                  stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            async with session.post(
                    HL_INFO, json={"type": "l2Book", "coin": COIN},
                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                r.raise_for_status()
                j = await r.json()
            book.apply_hl(j["levels"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[hl] {type(e).__name__}: {e}", flush=True)
        await asyncio.sleep(1.0)


async def run_once() -> None:
    ent, poly = OrderBook(), OrderBook()
    stop = asyncio.Event()
    feed = PolyBookFeed("POLY", POLY_API, INSTRUMENT, poly,
                        None, lambda: None, proxy=load_proxy() or None)
    async with aiohttp.ClientSession() as s:
        feed.session = s
        tasks = [asyncio.create_task(hl_loop(s, ent, stop)),
                 asyncio.create_task(feed.run(stop))]
        await asyncio.sleep(5.0)
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    if ent.ready and poly.ready:
        prem = (ent.mid() / poly.mid() - 1) * 1e4
        print(f"ENTROPY {ent.best_bid()}/{ent.best_ask()}  "
              f"POLY {poly.best_bid()}/{poly.best_ask()}  "
              f"prem {prem:+.2f} bps")
    else:
        print("NOT READY:", "ent" if not ent.ready else "",
              "poly" if not poly.ready else "")


async def check_instrument() -> None:
    async with aiohttp.ClientSession() as s:
        async with s.get(
                POLY_API + "/v1/info/instruments",
                proxy=load_proxy() or None,
                headers={"User-Agent": PolyBookFeed.UA,
                         "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=20)) as r:
            ins = await r.json(content_type=None)
    hits = [i for i in ins if i.get("symbol", "").upper().startswith("SNDK")]
    for h in hits:
        print(h["symbol"], "id", h["instrument_id"],
              "min_notional", h.get("min_notional"),
              "funding", h.get("funding_interval"))
    if not hits:
        print("SNDK not listed!")


async def amain(csv_path: str, minutes: int) -> None:
    ent, poly = OrderBook(), OrderBook()
    rec = MinuteRecorder(csv_path, ent, poly, STALE_SEC)
    stop = asyncio.Event()
    feed = PolyBookFeed("POLY", POLY_API, INSTRUMENT, poly,
                        None, lambda: None, proxy=load_proxy() or None)
    async with aiohttp.ClientSession() as s:
        feed.session = s
        tasks = [asyncio.create_task(hl_loop(s, ent, stop), name="hl"),
                 asyncio.create_task(feed.run(stop), name="poly"),
                 asyncio.create_task(rec.run(stop), name="rec")]
        t0 = time.time()
        try:
            while not stop.is_set():
                await asyncio.sleep(30.0)
                prem = ((ent.mid() / poly.mid() - 1) * 1e4
                        if ent.ready and poly.ready and ent.last_update_ts
                        and poly.last_update_ts else None)
                ptxt = f"{prem:+.2f} bps" if prem is not None else "--"
                print(f"{time.strftime('%H:%M:%S')} rows={rec.rows_written} "
                      f"prem {ptxt} | ages "
                      f"{time.time() - ent.last_update_ts:.0f}s/"
                      f"{time.time() - poly.last_update_ts:.0f}s",
                      flush=True)
                if minutes and time.time() - t0 >= minutes * 60:
                    stop.set()
        except (KeyboardInterrupt, asyncio.CancelledError):
            stop.set()
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true",
                    help="single sample + premium, then exit")
    ap.add_argument("--check", action="store_true",
                    help="verify the SNDK instrument id on Polymarket")
    ap.add_argument("--minutes", type=int, default=0,
                    help="stop after N minutes (0 = until Ctrl-C)")
    ap.add_argument("--csv", default="logs/poly_minutes.csv")
    args = ap.parse_args()
    try:
        if args.check:
            asyncio.run(check_instrument())
        elif args.once:
            asyncio.run(run_once())
        else:
            print(f"recording {COIN} <-> poly#{INSTRUMENT} -> {args.csv}")
            asyncio.run(amain(args.csv, args.minutes))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

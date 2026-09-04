"""Two-venue arbitrage engine: Entropy vs one hedge venue.

The signal is a fixed band around a configured midline (config.yaml):

    SELL entropy / BUY hedge  when executable premium >= midline + upper (+fees)
    BUY entropy / SELL hedge  when executable premium <= midline - lower (+fees)

Around the signal: per-direction persistence arming,
per-venue inventory ladder + position caps, per-venue order budgets and
reactive rate-limit exclusion, net-delta hedging, venue-outage pausing with
probing, and periodic on-chain reconciliation. There is no paper mode: the
bot either trades live or runs --record-only (data collection, no strategy).
Both venues' books are recorded to 1-minute CSV bars throughout.
"""
from __future__ import annotations

import asyncio
import contextlib
import csv
import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from typing import Dict, List, Optional

import aiohttp

from .book import ArbPlan, floor_step, plan_arb, walk_depth
from .config import Config, window_contains
from .ledger import Ledger
from .monitor import (auto_band_targets, drift_report, median,
                      read_minute_rows, regime_drift_report, stable_window)
from .recorder import MinuteRecorder
from .venue_hl import HLVenue
from .venue_lighter import LighterVenue
from .venue_poly import PolyVenue

log = logging.getLogger("engine")

CSV_HEADER = ["ts", "direction", "buy_venue", "sell_venue", "qty",
              "buy_limit", "sell_limit", "buy_notional", "sell_notional",
              "exp_edge_usd", "gross_edge_usd", "marginal_premium_bps",
              "midline_bps", "inv_add_bps", "ok", "buy_fill", "sell_fill",
              "buy_status", "sell_status", "fill_edge_usd"]
BALANCE_POLL_SEC = 30.0


class Engine:
    def __init__(self, cfg: Config, record_only: bool = False) -> None:
        self.cfg = cfg
        self.record_only = record_only
        self.session: Optional[aiohttp.ClientSession] = None
        self.entropy = None
        self.hedge = None
        self.venues: Dict[str, object] = {}
        self.recorder: Optional[MinuteRecorder] = None
        self.markets_ready = False
        self.stop = asyncio.Event()
        self._update_evt = asyncio.Event()
        self._reconcile_evt = asyncio.Event()
        # per-venue locks: an execution holds both; a reconcile holds one, so
        # a chain read can never race an in-flight order on that venue
        self._venue_locks: Dict[str, asyncio.Lock] = {}
        self._exec_tasks: set = set()
        self.halted = False
        self.consec_errors = 0
        self.last_trade_ts = 0.0
        self.trades = 0
        self.hedges = 0
        self.total_exp_edge = 0.0
        self.total_fill_edge = 0.0
        self.start_ts = time.time()
        self._last_skiplog = 0.0
        self._poke_due: Optional[float] = None
        # per-direction persistence arming: direction key -> first-seen ts
        self._armed: Dict[str, Optional[float]] = {"sell_entropy": None,
                                                   "buy_entropy": None}
        self._step = 1e-4
        self._min_base = 0.0
        self._min_notional = 10.0
        self._mtm_baseline: Optional[float] = None
        # maker ladder: _scan stashes a ceiling-rejected plan here for the
        # strategy loop to rest on the stale venue; only one at a time
        self._maker_request = None
        self._maker_task = None
        # proactive per-venue send budget: timestamps of recent order sends
        self._sends: Dict[str, deque] = {}
        # reactive per-venue throttle: venue key -> excluded until
        self._venue_limited_until: Dict[str, float] = {}
        # venue outage tracking: key -> down-since ts; a down venue pauses
        # trading and is probed every venue_probe_sec until it answers
        self._venue_down: Dict[str, float] = {}
        self._venue_probe_at: Dict[str, float] = {}
        self._venue_fetch_fails: Dict[str, int] = {}
        # per-execution records for the dashboard (newest last)
        self.recent_trades: deque = deque(maxlen=50)
        # watch state — observability (fees/funding/drift); auto_midline is
        # opt-in and clamped, everything else never affects order flow
        self.midline = cfg.midline_bps
        # live band edges (only used when auto_band; start from the static
        # global band and get retuned from the stable-regime room stats)
        self.upper = cfg.upper_bps
        self.lower = cfg.lower_bps
        self.funding: Dict[str, Optional[float]] = {}
        self.drift: Dict[str, Optional[float]] = {"median": None,
                                                  "drift": None, "n": 0}
        self.drift_1h: Dict[str, Optional[float]] = {"median": None,
                                                     "drift": None, "n": 0}
        # frozen-tuner safeguards: when stable_window rejects every window
        # (slow regime drift), _drift_lock records the direction of travel and
        # opens AGAINST it are blocked (closes always allowed); after
        # auto_frozen_fallback_min minutes of continuous freeze the tuners
        # re-anchor from the last 60 min instead of trading a stale seed.
        self._frozen_since: Optional[float] = None
        self._fallback_on = False
        self._drift_lock: Optional[str] = None
        # realized/unrealized ledger + UTC-day risk clock (state file keeps
        # the day's PnL baseline across restarts so the breaker cannot be
        # reset by bouncing the process)
        self.ledger = Ledger()
        self._pnl_state_path = (os.path.splitext(cfg.trades_csv)[0]
                                + "_pnl.json")
        self._day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._pnl_anchor = 0.0
        self._open_since: Optional[float] = None
        self._load_pnl_state()

    # ------------------------------------------------------------- utilities

    def _vlock(self, key: str) -> asyncio.Lock:
        lock = self._venue_locks.get(key)
        if lock is None:
            lock = self._venue_locks[key] = asyncio.Lock()
        return lock

    def _venue_rate_ok(self, v) -> bool:
        """True while the venue is under its max_orders_per_min (sliding 60s)."""
        dq = self._sends.setdefault(v.key, deque())
        now = time.time()
        while dq and now - dq[0] > 60.0:
            dq.popleft()
        return len(dq) < v.orders_per_min

    def _venue_limited(self, v) -> bool:
        return time.time() < self._venue_limited_until.get(v.key, 0.0)

    def _mark_limited(self, v) -> None:
        self._venue_limited_until[v.key] = time.time() + self.cfg.rate_limit_pause_sec
        log.warning("[%s] rate limited — trading paused for %.0fs",
                    v.name, self.cfg.rate_limit_pause_sec)

    def _record_send(self, v) -> None:
        self._sends.setdefault(v.key, deque()).append(time.time())

    def request_stop(self) -> None:
        self.stop.set()
        self._update_evt.set()
        self._reconcile_evt.set()

    # ------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        # Long keepalive so order-path connections survive quiet spells; the
        # keepalive loop pings inside this window to hold them open.
        self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(
            keepalive_timeout=75.0, ttl_dns_cache=300))
        try:
            await self._run_inner()
        finally:
            await self.session.close()

    def _make_venue(self, vc):
        if vc.kind == "lighter":
            return LighterVenue(vc, self.session, self.cfg.settle_timeout_sec)
        if vc.kind == "poly":
            return PolyVenue(vc, self.cfg.poly_api_url, self.cfg.poly_proxy,
                             self.session, self.cfg.settle_timeout_sec)
        return HLVenue(vc, self.cfg.hl_api_url, self.cfg.hl_ws_url,
                       self.session, self.cfg.settle_timeout_sec)

    async def _run_inner(self) -> None:
        cfg = self.cfg
        self.entropy = self._make_venue(cfg.entropy)
        self.hedge = self._make_venue(cfg.hedge)
        self.venues = {"entropy": self.entropy, "hedge": self.hedge}
        await asyncio.gather(self.entropy.load_market(), self.hedge.load_market())
        self.markets_ready = True

        live = not self.record_only
        if live:
            if not cfg.creds_complete:
                raise RuntimeError(
                    "live trading needs credentials for both venues in .env "
                    "(see .env.example); use --record-only to run without "
                    "them / 实盘需要在 .env 中配置两个交易所的密钥，仅采集数据"
                    "请用 --record-only")
            self.entropy.init_signer()
            self.hedge.init_signer()
            if self.hedge.kind == "hl":
                self.entropy.share_nonces_with(self.hedge)
            for v in (self.entropy, self.hedge):
                setter = getattr(v, "apply_leverage", None)
                if setter is not None:
                    try:
                        await setter()
                    except Exception as e:
                        log.warning("[%s] apply_leverage failed: %r", v.name, e)
        if (self.hedge.kind == "hl"
                and self.entropy._query_address()
                and self.entropy._query_address() == self.hedge._query_address()):
            self.hedge.include_core_equity = False  # shared account: count once

        for v in self.venues.values():
            getter = getattr(v, "fetch_fee", None)
            if getter is None:
                continue
            if not getattr(v.conf, "fee_auto", True):
                log.info("[%s] fee %.2fbps (config value, auto-discovery "
                         "off — e.g. 100%% rebated taker fee)", v.name,
                         v.fee_bps)
                continue
            try:
                fee = await getter()
            except Exception as e:
                log.debug("[%s] fee fetch failed: %r", v.name, e)
                continue
            if fee is not None and abs(fee - v.fee_bps) > 1e-9:
                log.info("[%s] taker fee %.2fbps (config %.2f) — using the "
                         "exchange value", v.name, fee, v.fee_bps)
                v.fee_bps = fee
                v.fee_src = "exchange"

        self._step = 10 ** -min(self.entropy.size_decimals,
                                self.hedge.size_decimals)
        self._min_base = max(self.entropy.min_base, self.hedge.min_base,
                             self._step)
        self._min_notional = max(cfg.min_order_notional,
                                 self.entropy.min_quote, self.hedge.min_quote)
        log.info("pair ENTROPY(%s)-%s(%s): midline=%+.2fbps band=[-%.2f, +%.2f] "
                 "fees=%.2f+%.2f step=%g min_ntl=$%g",
                 self.entropy.conf.symbol, self.hedge.name,
                 self.hedge.conf.symbol, cfg.midline_bps, cfg.lower_bps,
                 cfg.upper_bps, self.entropy.fee_bps, self.hedge.fee_bps,
                 self._step, self._min_notional)

        if self.record_only:
            log.warning("RECORD-ONLY — collecting minute data, no strategy, "
                        "no orders")
        else:
            log.warning("LIVE — real orders will be sent (use --record-only "
                        "for credential-less data collection)")
            await self._reconcile_positions(hedge=False, strict=True)
            log.info("starting positions: %s (net %+.6g)",
                     " ".join(f"{v.name}={v.position:+.6g}"
                              for v in self.venues.values()),
                     sum(v.position for v in self.venues.values()))

        tasks: List[asyncio.Task] = []
        for v in self.venues.values():
            tasks += v.start_tasks(self.stop, self._update_evt.set, live)
        if cfg.recorder_enabled or self.record_only:
            self.recorder = MinuteRecorder(cfg.recorder_csv, self.entropy.book,
                                           self.hedge.book, cfg.staleness_sec)
            tasks.append(asyncio.create_task(self.recorder.run(self.stop),
                                             name="recorder"))
        if not self.record_only:
            tasks.append(asyncio.create_task(self._strategy_loop(),
                                             name="strategy"))
            tasks.append(asyncio.create_task(self._balance_loop(),
                                             name="balances"))
            tasks.append(asyncio.create_task(self._http_keepalive_loop(),
                                             name="keepalive"))
        tasks.append(asyncio.create_task(self._status_loop(), name="status"))
        tasks.append(asyncio.create_task(self._watch_loop(), name="watch"))
        if live:
            tasks.append(asyncio.create_task(self._reconcile_loop(),
                                             name="reconcile"))

        await self.stop.wait()
        if self._exec_tasks:  # let in-flight executions settle, never cancel
            log.info("waiting for %d in-flight execution(s) to settle",
                     len(self._exec_tasks))
            await asyncio.wait(self._exec_tasks,
                               timeout=cfg.settle_timeout_sec + 2.0)
        if self._maker_task is not None and not self._maker_task.done():
            self._maker_task.cancel()
            with contextlib.suppress(BaseException):
                await self._maker_task
        for v in self.venues.values():
            getter = getattr(v, "cancel_resting", None)
            if getter is not None:
                with contextlib.suppress(Exception):
                    await getter()   # no Gtc order may outlive the process
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for v in self.venues.values():
            await v.close()
        log.info("shutdown — %d trades, %d hedges, exp edge $%.4f, "
                 "fill edge $%.4f", self.trades, self.hedges,
                 self.total_exp_edge, self.total_fill_edge)

    # --------------------------------------------------------------- signals

    def _inv_add_bps(self, buy, sell) -> float:
        """Inventory ladder: a surcharge that grows once a venue's position
        passes floor_frac of its cap in the direction the trade would add to
        (buying adds when that venue is >= flat long; selling adds when the
        venue is <= flat short). Max of the two venues' ramps."""
        scale = self.cfg.inventory_scale_bps
        if scale <= 0:
            return 0.0
        floor = min(max(self.cfg.inventory_floor_frac, 0.0), 0.99)

        def ramp(v, adding: bool) -> float:
            if not adding:
                return 0.0
            ref = v.book.mid()
            if ref is None:
                return 0.0
            u = min(abs(v.position) * ref / v.cap_usd, 1.0)
            if u <= floor:
                return 0.0
            return scale * (u - floor) / (1.0 - floor)

        return max(ramp(buy, buy.position >= 0), ramp(sell, sell.position <= 0))

    def _active_window(self):
        """First configured window containing now (UTC), or None."""
        now_utc = datetime.now(timezone.utc)
        for w in self.cfg.windows:
            if window_contains(w, now_utc):
                return w
        return None

    def _band(self) -> tuple:
        """Effective (midline, upper_bps, lower_bps). The first window of
        cfg.windows that contains the current UTC instant supplies its band
        (and optionally its own midline); with no match the global values
        apply. When auto_band is on, the LIVE data-tuned edges override
        every static band (window/global); windows still own midlines."""
        cfg = self.cfg
        w = self._active_window()
        mid = (w.midline_bps
               if (w is not None and w.midline_bps is not None)
               else self.midline)
        if cfg.auto_band:
            return mid, self.upper, self.lower
        if w is None:
            return mid, cfg.upper_bps, cfg.lower_bps
        return mid, w.upper_bps, w.lower_bps

    def session_label(self) -> str:
        """Which threshold set is active right now — for the dashboard.
        Empty string when no windows are configured (one global band)."""
        w = self._active_window()
        if w is not None:
            return w.name
        if not self.cfg.windows_from_session:
            return ""
        now_utc = datetime.now(timezone.utc)
        return "weekend" if now_utc.weekday() >= 5 else "offhours"

    def _eff_threshold(self, buy, sell) -> float:
        """Net hurdle (bps, on top of fees) for the direction buy->sell.

        selling entropy: executable premium must clear midline + upper;
        buying entropy: the reverse premium must clear lower - midline."""
        mid, up, lo = self._band()
        if sell.key == "entropy":
            base = mid + up
        else:
            base = lo - mid
        return base + self._inv_add_bps(buy, sell)

    def _headroom(self, buy, sell, ref_px: float) -> float:
        hb = buy.cap_usd - buy.position * ref_px
        hs = sell.cap_usd + sell.position * ref_px
        return min(hb, hs)

    def _plan(self, buy, sell, cap_notional: float, threshold_bps=None):
        return plan_arb(
            buy.book, sell.book,
            threshold_bps=(self._eff_threshold(buy, sell)
                           if threshold_bps is None else threshold_bps),
            buy_fee_bps=buy.fee_bps, sell_fee_bps=sell.fee_bps,
            take_fraction=self.cfg.take_fraction,
            cap_notional=cap_notional,
            min_base=self._min_base,
            min_notional=self._min_notional,
            size_step=self._step,
        )

    def _below_floor(self, plan) -> bool:
        """True when a fee-clearing slice's post-fee expected edge is thinner
        than cfg.min_net_edge_bps of its notional — the decay/stale-anchor
        guard that keeps the engine from paying to open a position."""
        floor = self.cfg.min_net_edge_bps
        if floor <= 0 or plan is None:
            return False
        return plan.exp_edge_usd < plan.buy_notional * floor / 1e4

    # -------------------------------------------------------------- strategy

    async def _strategy_loop(self) -> None:
        while not self.stop.is_set():
            await self._update_evt.wait()
            self._update_evt.clear()
            if self.stop.is_set():
                break
            try:
                await self._evaluate()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("evaluate failed")

    def _schedule_poke(self, delay: float) -> None:
        loop = asyncio.get_running_loop()
        due = loop.time() + max(delay, 0.01)
        if self._poke_due is not None and self._poke_due <= due + 0.02:
            return

        def _fire() -> None:
            self._poke_due = None
            self._update_evt.set()

        self._poke_due = due
        loop.call_at(due, _fire)

    def _skiplog(self, fmt: str, *args) -> None:
        now = time.time()
        if now - self._last_skiplog >= 2.0:
            self._last_skiplog = now
            log.info(fmt, *args)

    async def _evaluate(self) -> None:
        cfg = self.cfg
        if self.halted:
            return
        now = time.time()
        if now - self.last_trade_ts < cfg.cooldown_sec:
            self._schedule_poke(cfg.cooldown_sec - (now - self.last_trade_ts))
            return
        if self._maker_request is not None:
            if (self._maker_task is None
                    or self._maker_task.done()):   # previous ladder resolved
                req = self._maker_request
                self._maker_request = None
                self._maker_task = asyncio.create_task(self._run_maker(*req))
            return
        best = self._scan(now)
        if best is None:
            return
        buy, sell, plan = best
        # _scan verified both locks free and nothing ran since (no awaits),
        # so these acquires take the no-suspension fast path
        await self._vlock(buy.key).acquire()
        await self._vlock(sell.key).acquire()
        # run as a task so a shutdown cancels the strategy loop's await, never
        # the in-flight execution itself (both legs must settle)
        t = asyncio.create_task(self._execute_locked(buy, sell, plan))
        self._exec_tasks.add(t)
        t.add_done_callback(self._exec_tasks.discard)
        await asyncio.shield(t)

    async def _run_maker(self, dkey: str, plan: ArbPlan, buy, sell) -> None:
        """Maker ladder for ceiling-rejected signals: rest the leg we WANT
        to be filled (always the ENTROPY side, the venue whose displayed
        quote was too good) as a passive Gtc order, and only taker the hedge
        leg AFTER that fill. If the market pulls back into us we capture the
        fat spread with maker economics; if it keeps running away, the order
        never fills and is cancelled — zero cost, no naked window, because
        the second leg is never touched before the first one prints."""
        ent = buy if buy.key == "entropy" else sell
        hed = sell if ent is buy else buy
        mk_is_buy = dkey == "buy_entropy"
        mk_qty = floor_step(plan.qty, self._step)
        mk_px = plan.buy_limit if mk_is_buy else plan.sell_limit
        if mk_qty < self._min_base or mk_qty * mk_px < self._min_notional:
            return
        lk_e = self._vlock(ent.key)
        lk_h = self._vlock(hed.key)
        await lk_e.acquire()
        try:
            if self.halted or self.stop.is_set():
                return
            log.info("[MAKER] %s: rest %s %.6g on %s @%.6g (wait %.1fs)",
                     dkey, "BUY" if mk_is_buy else "SELL", mk_qty, ent.name,
                     mk_px, self.cfg.maker_wait_sec)
            try:
                res = await ent.send_maker(
                    is_buy=mk_is_buy, qty=mk_qty, limit_px=mk_px,
                    wait_sec=self.cfg.maker_wait_sec)
            except Exception as e:
                log.error("[MAKER] %s leg failed: %r", ent.name, e)
                return
            f = res.get("filled_base") or 0.0
            if f <= 0:
                log.info("[MAKER] %s not pulled back (%s) — cancelled, "
                         "no cost", ent.name, res.get("status"))
                self._update_evt.set()
                return
            epx = res.get("avg_px") or mk_px
            ent.position += f if mk_is_buy else -f
            efee = ent.fee_bps / 1e4
            ent.cash += -f * epx * (1 + efee) if mk_is_buy \
                else f * epx * (1 - efee)
            ent.volume_usd += f * epx
            self.ledger.fill(ent.key, is_buy=mk_is_buy, qty=f, px=epx,
                             fee_bps=ent.fee_bps)
        finally:
            lk_e.release()
        # maker leg printed: now cross the hedge leg at the CURRENT book.
        # Fails/decays like any taker fill — residual is handled by the
        # standard hedge/reconcile path, same as one-leg IOC outcomes.
        slip = self.cfg.leg_slippage_bps / 1e4
        ref = hed.book.best_bid() if not mk_is_buy else hed.book.best_ask()
        hlk = lk_h
        await hlk.acquire()
        try:
            if ref is None or self.halted or self.stop.is_set():
                status, hfill, hpx = "no-book", 0.0, 0.0
            else:
                h_is_buy = not mk_is_buy
                bound = hed.px_round(ref * (1 + slip) if h_is_buy
                                     else ref * (1 - slip), h_is_buy)
                r = await hed.send_taker(is_buy=h_is_buy, qty=f,
                                         limit_px=bound)
                hfill = r.get("filled_base") or 0.0
                hpx = r.get("avg_px") or bound
                status = r["status"]
                if hfill:
                    hfee = hed.fee_bps / 1e4
                    hed.position += hfill if h_is_buy else -hfill
                    hed.cash += -hfill * hpx * (1 + hfee) if h_is_buy \
                        else hfill * hpx * (1 - hfee)
                    hed.volume_usd += hfill * hpx
                    self.ledger.fill(hed.key, is_buy=h_is_buy, qty=hfill,
                                     px=hpx, fee_bps=hed.fee_bps)
                if r.get("err") or r.get("unresolved"):
                    self._reconcile_evt.set()
        finally:
            hlk.release()
        matched = min(f, hfill)
        fill_edge = matched * ((hpx * (1 - hed.fee_bps / 1e4)
                                - epx * (1 + ent.fee_bps / 1e4))
                               if mk_is_buy else
                               (epx * (1 - ent.fee_bps / 1e4)
                                - hpx * (1 + hed.fee_bps / 1e4)))
        self.total_fill_edge += fill_edge if matched else 0.0
        self.trades += 1
        self.last_trade_ts = time.time()
        ent.last_traded_ts = hed.last_traded_ts = time.time()
        log.info("[MAKER DONE] %s: ent %.6g@%.6g (%s) + hedge %.6g@%.6g "
                 "(%s) | matched %.6g | fill edge $%.4f",
                 dkey, f, epx, res.get("status"), hfill, hpx, status,
                 matched, fill_edge)
        self.recent_trades.append({
            "ts": time.time(), "direction": dkey + "/maker", "qty": matched,
            "notional": matched * epx,
            "prem_bps": plan.top_premium_bps,
            "exp": None, "fill": fill_edge if matched else None,
            "status": f"{res.get('status')}/{status}", "ok": bool(matched)})
        await self._maybe_hedge()
        self._update_evt.set()

    async def _execute_locked(self, buy, sell, plan: ArbPlan) -> None:
        """Run one execution while holding both venue locks (acquired by the
        caller), then release them and settle the aftermath: unresolved
        outcomes escalate to reconcile, everything else gets a net-delta
        check."""
        unresolved = False
        try:
            unresolved = await self._execute(buy, sell, plan)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("execute failed")
        finally:
            self._vlock(buy.key).release()
            self._vlock(sell.key).release()
        if unresolved:
            self._reconcile_evt.set()
        else:
            await self._maybe_hedge()
        self._update_evt.set()  # freed venues may have a queued opportunity

    def _scan(self, now: float):
        """Evaluate both directions; returns the best executable
        (buy, sell, plan), or None."""
        cfg = self.cfg
        best = None
        # inventory age + decoupled-exit window: closing is triggered by the
        # mean reversion itself (premium back near mid) or a holding-time
        # limit, NOT by the opposite band edge — a drifted anchor can push
        # the exit threshold out of reach for hours (09-01 stuck-long case)
        pos = self.entropy.position
        if abs(pos) > cfg.net_tolerance_base:
            if self._open_since is None:
                self._open_since = now
        else:
            self._open_since = None
        exit_close = False
        if abs(pos) > cfg.net_tolerance_base:
            mid_now = self._band()[0]
            prem_now = self.premium_bps()
            if (cfg.timeout_close_min > 0 and self._open_since
                    and now - self._open_since >= cfg.timeout_close_min * 60):
                exit_close = True
            if (cfg.reversion_close_bps > 0 and prem_now is not None
                    and abs(prem_now - mid_now) <= cfg.reversion_close_bps):
                exit_close = True
        # the trigger IS the exit policy; plan_arb's threshold is a RAW
        # hurdle, so the only "threshold" consistent with trigger-to-close is
        # an unconditional marketable one (slippage caps still bind). A
        # fee-only 0.0 here was a 09-03 trap: with the midline at -10 bps it
        # demanded a +10 bps reversion — harder than the band itself, and a
        # long sat uncloseable for hours.
        exit_thr = -1000.0 if exit_close else None
        exit_dkey = ("sell_entropy" if pos > 0 else "buy_entropy") \
            if exit_close else None
        for buy, sell, dkey in ((self.hedge, self.entropy, "sell_entropy"),
                                (self.entropy, self.hedge, "buy_entropy")):
            # drift lock (frozen tuner, premium trending): block slices that
            # would OPEN/ADD against the travel; closing an existing position
            # through the lock is always allowed
            lock = self._drift_lock
            if lock == "up" and dkey == "sell_entropy" \
                    and self.entropy.position <= 0:
                self._armed[dkey] = None
                self._skiplog("%s open blocked: drift lock (premium rising)",
                              dkey)
                continue
            if lock == "down" and dkey == "buy_entropy" \
                    and self.entropy.position >= 0:
                self._armed[dkey] = None
                self._skiplog("%s open blocked: drift lock (premium falling)",
                              dkey)
                continue
            if not (buy.book.is_fresh(cfg.staleness_sec)
                    and sell.book.is_fresh(cfg.staleness_sec)):
                continue
            if not (buy.ready_to_trade() and sell.ready_to_trade()):
                continue
            if self._venue_down:
                continue  # a venue in outage pauses the (only) pair
            if self._vlock(buy.key).locked() or self._vlock(sell.key).locked():
                continue  # mid-execution or mid-reconcile
            if self._venue_limited(buy) or self._venue_limited(sell):
                continue  # reactive 429 exclusion
            if not (self._venue_rate_ok(buy) and self._venue_rate_ok(sell)):
                self._skiplog("%s deferred: venue order budget exhausted", dkey)
                continue
            # never refire into books that predate the venue's own last trade
            if (buy.book.last_update_ts <= buy.last_traded_ts
                    or sell.book.last_update_ts <= sell.last_traded_ts):
                continue
            if dkey == exit_dkey:
                # size a decoupled close by the inventory, not the book:
                # never flip the position while forcing it out
                ref = self.entropy.book.mid() or 0.0
                cap = min(cfg.max_order_notional, abs(pos) * ref * 1.05)
                plan, reason = self._plan(buy, sell, cap,
                                          threshold_bps=exit_thr)
            else:
                plan, reason = self._plan(buy, sell,
                                          cfg.max_order_notional)
            edge_present = reason not in ("no_edge", "empty_book")
            if not edge_present:
                self._armed[dkey] = None
                continue
            reducing = (dkey == "sell_entropy"
                        and self.entropy.position > 0) \
                or (dkey == "buy_entropy" and self.entropy.position < 0)
            if (plan is not None and not reducing
                    and cfg.min_cross_rounds > 0
                    and plan.q_max_notional
                    < cfg.min_cross_rounds * self._min_notional):
                # thin book: the whole crossing is barely one venue minimum —
                # sizing there just churns dust tails (0.0066 round-trips)
                self._armed[dkey] = None
                self._skiplog("%s book too thin: $%.0f crossable < %.0f x "
                              "min — skipped", dkey, plan.q_max_notional,
                              cfg.min_cross_rounds)
                continue
            if (plan is not None and dkey != exit_dkey
                    and cfg.max_top_premium_bps > 0
                    and plan.top_premium_bps > cfg.max_top_premium_bps):
                # too fat to be real on a taker IOC: one side's book is
                # lagging a fast move. If maker mode is on, rest a passive
                # order on the LAGGING side and get paid to be right (only a
                # real pull-back fills us); else just skip.
                self._armed[dkey] = None
                maker_free = (not cfg.maker_enabled
                              or (self._maker_task is None
                                  or self._maker_task.done())
                              ) and not self._maker_request
                if cfg.maker_enabled and hasattr(self.entropy,
                                                 "send_maker") \
                        and maker_free:
                    self._maker_request = (dkey, plan, buy, sell)
                    self._schedule_poke(cfg.maker_wait_sec + 1.0)
                    self._skiplog("%s premium %.0fbps over ceiling — queuing "
                                  "MAKER ladder instead of taker",
                                  dkey, plan.top_premium_bps)
                else:
                    self._skiplog("%s top premium %.0fbps over sanity ceiling "
                                  "%.0fbps — stale-book trap, skipped",
                                  dkey, plan.top_premium_bps,
                                  cfg.max_top_premium_bps)
                continue
            if plan is not None and not reducing and self._below_floor(plan):
                # a real (fee-clearing) edge that is too thin to survive the
                # observed plan->fill decay, or that only clears because the
                # midline anchor went stale mid-drift: disarm, do not fire.
                # Closes of an existing position are NEVER floor-blocked.
                self._armed[dkey] = None
                self._skiplog("%s edge %.1fbps below min_net_edge floor %.1f "
                              "— skipped", dkey,
                              plan.exp_edge_usd / max(plan.buy_notional, 1e-9)
                              * 1e4, cfg.min_net_edge_bps)
                continue
            armed = self._armed.get(dkey)
            if armed is None:
                # premium persistence: only fire if the edge survives
                # premium_persist_sec (filters one-tick phantoms)
                self._armed[dkey] = now
                self._schedule_poke(cfg.premium_persist_sec)
                continue
            if now - armed < cfg.premium_persist_sec:
                self._schedule_poke(cfg.premium_persist_sec - (now - armed))
                continue
            if plan is None:
                # real edge present (reason was below_min_*) but sizing can't
                # clear venue minimums — surface it instead of silently dying
                self._skiplog("%s armed but sizing unusable (%s)", dkey,
                              reason)
                continue
            headroom = self._headroom(buy, sell, plan.buy_limit)
            if headroom < plan.buy_notional:
                if dkey == exit_dkey:
                    ref = self.entropy.book.mid() or 0.0
                    rcap = min(cfg.max_order_notional,
                               abs(pos) * ref * 1.05, headroom)
                    plan, _ = self._plan(buy, sell, rcap,
                                         threshold_bps=exit_thr)
                else:
                    plan, _ = self._plan(
                        buy, sell, min(cfg.max_order_notional, headroom))
                if plan is None or (not reducing and self._below_floor(plan)):
                    self._skiplog("%s blocked by position caps (headroom $%.0f)",
                                  dkey, max(headroom, 0.0))
                    continue
            if best is None or plan.exp_edge_usd > best[2].exp_edge_usd:
                best = (buy, sell, plan)
        return best

    # ------------------------------------------------------------- execution

    async def _execute(self, buy, sell, plan: ArbPlan) -> bool:
        """Send both legs and settle the fills. Both venue locks are held by
        the caller. Returns True when an outcome is unresolved and the caller
        must escalate to reconcile."""
        if self.halted:
            return False
        cfg = self.cfg
        inv_bps = self._inv_add_bps(buy, sell)
        direction = "sell_entropy" if sell.key == "entropy" else "buy_entropy"
        self.last_trade_ts = time.time()
        log.info("[ARB] %s: BUY %s %.6g @<=%.6g | SELL %s @>=%.6g | "
                 "take $%.0f of $%.0f | prem %.2fbps | exp $%.4f",
                 direction, buy.name, plan.qty, plan.buy_limit, sell.name,
                 plan.sell_limit, plan.buy_notional, plan.q_max_notional,
                 plan.marginal_premium_bps, plan.exp_edge_usd)
        slip = cfg.leg_slippage_bps / 1e4
        buy_bound = buy.px_round(plan.buy_limit * (1 + slip), round_up=False)
        sell_bound = sell.px_round(plan.sell_limit * (1 - slip), round_up=True)
        self._record_send(buy)
        self._record_send(sell)
        res = await asyncio.gather(
            buy.send_taker(is_buy=True, qty=plan.qty, limit_px=buy_bound),
            sell.send_taker(is_buy=False, qty=plan.qty, limit_px=sell_bound),
            return_exceptions=True)
        binfo, sinfo = (r if isinstance(r, dict) else
                        {"status": "send-failed", "filled_base": 0.0,
                         "avg_px": None, "err": repr(r), "unresolved": False}
                        for r in res)
        for v, info, side in ((buy, binfo, "buy"), (sell, sinfo, "sell")):
            if info.get("err"):
                log.error("[%s] %s leg: %s", v.name, side, info["err"])
        bfill = binfo["filled_base"]
        sfill = sinfo["filled_base"]
        buy.position += bfill
        sell.position -= sfill
        if bfill:
            bpx = binfo.get("avg_px") or plan.buy_limit
            buy.cash -= bfill * bpx * (1 + plan.buy_fee)
            buy.volume_usd += bfill * bpx
            self.ledger.fill(buy.key, is_buy=True, qty=bfill, px=bpx,
                             fee_bps=buy.fee_bps)
        if sfill:
            spx = sinfo.get("avg_px") or plan.sell_limit
            sell.cash += sfill * spx * (1 - plan.sell_fee)
            sell.volume_usd += sfill * spx
            self.ledger.fill(sell.key, is_buy=False, qty=sfill, px=spx,
                             fee_bps=sell.fee_bps)

        matched = min(bfill, sfill)
        fill_edge = 0.0
        if matched > 0 and binfo.get("avg_px") and sinfo.get("avg_px"):
            fill_edge = matched * (sinfo["avg_px"] * (1 - plan.sell_fee)
                                   - binfo["avg_px"] * (1 + plan.buy_fee))
            self.total_fill_edge += fill_edge
        log.info("[SETTLED] %s: buy %s %s %.6g/%.6g | sell %s %s %.6g/%.6g | "
                 "matched %.6g | fill edge $%.4f", direction,
                 buy.name, binfo["status"], bfill, plan.qty,
                 sell.name, sinfo["status"], sfill, plan.qty, matched, fill_edge)
        buy.last_traded_ts = sell.last_traded_ts = time.time()

        unresolved = binfo.get("unresolved") or sinfo.get("unresolved")
        hard_err = (binfo.get("err") is not None
                    or sinfo.get("err") is not None)
        rate_limited = False
        for v, info in ((buy, binfo), (sell, sinfo)):
            if str(info.get("err", "")).startswith("RATE_LIMITED"):
                rate_limited = True
                self._mark_limited(v)
            elif "margin" in str(info.get("status", "")).lower():
                log.warning("[%s] margin rejection — collateral exhausted, "
                            "pausing venue", v.name)
                self._mark_limited(v)
        sent_ok = not hard_err and not unresolved
        if sent_ok:
            self.consec_errors = 0
        elif not rate_limited:
            self.consec_errors += 1
            if self.consec_errors >= cfg.max_consecutive_errors:
                self.halted = True
                log.critical("HALTED after %d consecutive execution problems "
                             "— flatten manually and restart / 连续执行异常，"
                             "引擎已停止，请手动平仓后重启", self.consec_errors)
        if sent_ok:
            self.trades += 1
            self.total_exp_edge += plan.exp_edge_usd
        self._record_trade(direction, plan,
                           None if unresolved else fill_edge,
                           f"{binfo['status']}/{sinfo['status']}", sent_ok)
        self._log_csv(direction, buy, sell, plan, sent_ok, bfill, sfill,
                      binfo["status"], sinfo["status"], fill_edge, inv_bps)
        self.last_trade_ts = time.time()
        return bool(unresolved)

    def _record_trade(self, direction: str, plan: ArbPlan, fill_edge,
                      status: str, ok: bool) -> None:
        self.recent_trades.append({
            "ts": time.time(), "direction": direction, "qty": plan.qty,
            "notional": plan.buy_notional,
            "prem_bps": plan.marginal_premium_bps,
            "exp": plan.exp_edge_usd, "fill": fill_edge, "status": status,
            "ok": ok})

    async def _maybe_hedge(self) -> None:
        net = sum(v.position for v in self.venues.values())
        if abs(net) > self.cfg.net_tolerance_base:
            await self._hedge(net)

    async def _hedge(self, net: float) -> None:
        """Reduce the venue that carries the imbalance back toward net zero
        (reduce-only taker with hedge_slippage_bps price protection)."""
        cfg = self.cfg
        is_sell = net > 0
        sgn = 1.0 if net > 0 else -1.0
        slip = cfg.hedge_slippage_bps / 1e4
        for v in sorted(self.venues.values(),
                        key=lambda x: (self._venue_limited(x), -x.position * sgn)):
            if v.position * sgn <= 0:
                continue
            if v.key in self._venue_down \
                    or not v.book.is_fresh(cfg.staleness_sec):
                continue  # unreachable or blind: cannot hedge here
            lk = self._vlock(v.key)
            if lk.locked():
                continue
            qty = floor_step(min(abs(net), abs(v.position)), self._step)
            if qty < v.min_base:
                continue
            ref = v.book.best_bid() if is_sell else v.book.best_ask()
            if ref is None:
                continue
            limit = v.px_round(ref * (1 - slip), False) if is_sell \
                else v.px_round(ref * (1 + slip), True)
            if qty * limit < max(cfg.min_order_notional, v.min_quote):
                continue
            await lk.acquire()  # verified free, no awaits since: fast path
            try:
                log.warning("[HEDGE] net %+.6g — %s %.6g on %s @%.6g",
                            net, "SELL" if is_sell else "BUY", qty, v.name, limit)
                self.hedges += 1
                self._record_send(v)  # counts toward the budget, never blocked
                info = await v.send_taker(is_buy=not is_sell, qty=qty,
                                          limit_px=limit, reduce_only=True)
                if info.get("err") or info.get("unresolved"):
                    log.error("[HEDGE] %s: %s", v.name,
                              info.get("err") or "unresolved")
                    if str(info.get("err", "")).startswith("RATE_LIMITED"):
                        self._mark_limited(v)
                    self._reconcile_evt.set()
                else:
                    fill = info["filled_base"]
                    v.position += -fill if is_sell else fill
                    if fill:
                        px = info.get("avg_px") or limit
                        fee = v.fee_bps / 1e4
                        v.cash += fill * px * (1 - fee) if is_sell \
                            else -fill * px * (1 + fee)
                        v.volume_usd += fill * px
                        self.ledger.fill(v.key, is_buy=not is_sell, qty=fill,
                                         px=px, fee_bps=v.fee_bps)
                    log.info("[HEDGE SETTLED] %s %s %.6g/%.6g",
                             v.name, info["status"], fill, qty)
                v.last_traded_ts = time.time()
            finally:
                lk.release()
            return
        log.warning("[HEDGE] net %+.6g below hedgeable minimum — carrying "
                    "(next reconcile retries)", net)

    # --------------------------------------------------- reconcile / status

    # Lighter's REST account state lags its ws settlements; overwriting a
    # venue that traded seconds ago "restores" stale positions and triggers
    # phantom hedge oscillations. Grace-guard + venue lock prevent that.
    RECONCILE_GRACE_SEC = 5.0

    async def _reconcile_positions(self, hedge: bool,
                                   strict: bool = False) -> None:
        now = time.time()
        vs = []
        for v in self.venues.values():
            if now - v.last_traded_ts <= self.RECONCILE_GRACE_SEC:
                continue  # just traded: chain read would be stale
            if v.key in self._venue_down \
                    and now < self._venue_probe_at.get(v.key, 0.0):
                continue  # down venue: probe only every venue_probe_sec
            vs.append(v)
        if not vs:
            return
        got = await asyncio.gather(
            *(self._reconcile_venue(v, strict) for v in vs),
            return_exceptions=True)
        for r in got:
            if isinstance(r, BaseException):
                raise r  # strict startup: fail loudly
        if hedge:
            await self._maybe_hedge()

    async def _reconcile_venue(self, v, strict: bool) -> None:
        async with self._vlock(v.key):
            now = time.time()
            if now - v.last_traded_ts <= self.RECONCILE_GRACE_SEC:
                return  # traded while waiting for the lock
            try:
                r = await v.fetch_position()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if strict:
                    raise RuntimeError(
                        f"[{v.name}] cannot fetch starting position: {e!r}")
                # exchange unreachable (e.g. scheduled maintenance): pause
                # trading and keep probing until it answers again
                n = self._venue_fetch_fails.get(v.key, 0) + 1
                self._venue_fetch_fails[v.key] = n
                self._venue_probe_at[v.key] = now + self.cfg.venue_probe_sec
                if n >= 3 and v.key not in self._venue_down:
                    self._venue_down[v.key] = now
                    log.critical("[%s] API unreachable (%d attempts) — "
                                 "trading PAUSED; probing every %.0fs until "
                                 "it recovers", v.name, n,
                                 self.cfg.venue_probe_sec)
                elif v.key not in self._venue_down:
                    log.warning("[%s] position fetch failed (%d): %r",
                                v.name, n, e)
                return
            if v.key in self._venue_down:
                log.warning("[%s] API recovered after %.0fs outage — "
                            "trading RESUMED", v.name,
                            now - self._venue_down.pop(v.key))
                self._update_evt.set()
            self._venue_fetch_fails[v.key] = 0
            delta = r - v.position
            if abs(delta) > 1e-12:
                if abs(delta) > self.cfg.net_tolerance_base:
                    log.warning("[%s] reconcile: chain %+.6g vs local %+.6g "
                                "— adopting chain", v.name, r, v.position)
                mid = v.book.mid()
                if mid is not None:
                    v.cash -= delta * mid
                v.position = r
                # fills we did not place (manual trades, missed settles):
                # cost basis unknown, price the carry at mark
                self.ledger.reanchor(v.key, r, mid)

    async def _reconcile_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self._reconcile_evt.wait(),
                                       timeout=self.cfg.reconcile_sec)
                self._reconcile_evt.clear()
                await asyncio.sleep(1.0)
            except asyncio.TimeoutError:
                pass
            if self.stop.is_set():
                break
            try:
                await self._reconcile_positions(hedge=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reconcile failed")

    async def _balance_loop(self) -> None:
        while not self.stop.is_set():
            for v in self.venues.values():
                try:
                    got = await v.fetch_equity()
                    if got is not None:
                        v.equity, v.free = got
                        if v.start_equity is None:
                            v.start_equity = v.equity
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.debug("[%s] equity poll failed: %r", v.name, e)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=BALANCE_POLL_SEC)
            except asyncio.TimeoutError:
                pass

    async def _http_keepalive_loop(self) -> None:
        if self.cfg.http_keepalive_sec <= 0:
            return
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(),
                                       timeout=self.cfg.http_keepalive_sec)
                return
            except asyncio.TimeoutError:
                pass
            await asyncio.gather(*(v.warm_http() for v in self.venues.values()),
                                 return_exceptions=True)

    def account_delta(self) -> Optional[float]:
        """Change in real account equity since start (both venues)."""
        total = 0.0
        for v in self.venues.values():
            if v.equity is None or v.start_equity is None:
                return None
            total += v.equity - v.start_equity
        return total

    def session_pnl(self) -> Optional[float]:
        total = 0.0
        for v in self.venues.values():
            m = v.book.mid()
            if m is None:
                return None
            total += v.cash + v.position * m
        if self._mtm_baseline is None:
            self._mtm_baseline = total
        return total - self._mtm_baseline

    # ------------------------------------------------------------ PnL / risk

    def _marks(self) -> Dict[str, Optional[float]]:
        return {k: v.book.mid() for k, v in self.venues.items()}

    def day_pnl(self) -> float:
        """UTC-day PnL (realized + unrealized, fees inside realized),
        continued across restarts via the small state file."""
        return self._pnl_anchor + self.ledger.realized() \
            + self.ledger.unrealized(self._marks())

    def _roll_day(self) -> None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if day == self._day:
            return
        closed = self.day_pnl()
        log.warning("[pnl] day %s closed at $%+.4f (realized $%+.4f, fees "
                    "$%.4f) — rolling over / 日切", self._day, closed,
                    self.ledger.realized(), self.ledger.fees())
        self._day = day
        self._pnl_anchor = -(self.ledger.realized()
                             + self.ledger.unrealized(self._marks()))
        self._save_pnl_state()

    def _state_dir(self) -> str:
        d = os.path.dirname(self._pnl_state_path)
        if d:
            os.makedirs(d, exist_ok=True)

    def _load_pnl_state(self) -> None:
        try:
            with open(self._pnl_state_path) as f:
                st = json.load(f)
            if st.get("day") == self._day:
                self._pnl_anchor = float(st.get("anchor", 0.0))
                log.info("pnl state: continuing UTC day %s at carry $%+.4f",
                         self._day, self._pnl_anchor)
        except (OSError, ValueError, TypeError):
            pass

    def _save_pnl_state(self) -> None:
        try:
            self._state_dir()
            tmp = self._pnl_state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"day": self._day,
                           "anchor": self.day_pnl(),
                           "realized": self.ledger.realized(),
                           "fees": self.ledger.fees(),
                           "ts": time.time()}, f)
            os.replace(tmp, self._pnl_state_path)
        except OSError:
            pass

    async def _risk_check(self) -> None:
        """Called from the status loop: day rollover, state persist, and the
        daily drawdown breaker — flatten first, then halt, so a restart
        cannot be used to keep trading on a blown day."""
        self._roll_day()
        self._save_pnl_state()
        lim = self.cfg.daily_max_loss_usd
        if lim > 0 and not self.halted:
            pnl = self.day_pnl()
            if pnl <= -lim:
                self.halted = True   # before flatten: no re-opening
                log.critical("DAILY LOSS BREAKER: day PnL $%.4f <= -%.2f — "
                             "flattening and halting / 日亏损断路器触发",
                             pnl, lim)
                await self._flatten_all("daily loss breaker")

    async def _flatten_all(self, reason: str) -> None:
        """Reduce-only taker both venues toward flat, best effort, bounded
        rounds. Residuals are left to reconcile (e.g. sub-minimum dust or an
        outage venue) and surfaced loudly."""
        log.warning("[FLATTEN] requested: %s", reason)
        slip = 2.0 * self.cfg.hedge_slippage_bps / 1e4
        for _round in range(6):
            pending = [v for v in self.venues.values()
                       if abs(v.position) > self.cfg.net_tolerance_base]
            if not pending:
                log.info("[FLATTEN] all venues flat")
                return
            for v in pending:
                if v.key in self._venue_down:
                    log.error("[FLATTEN] %s unreachable — residual %+.6g "
                              "needs manual close", v.name, v.position)
                    continue
                lk = self._vlock(v.key)
                if lk.locked():
                    continue
                is_buy = v.position < 0
                qty = floor_step(abs(v.position), self._step)
                ref = v.book.best_ask() if is_buy else v.book.best_bid()
                if qty <= 0 or ref is None:
                    continue
                limit = v.px_round(ref * (1 + slip) if is_buy
                                   else ref * (1 - slip), is_buy)
                if qty * limit < self.cfg.min_order_notional:
                    log.error("[FLATTEN] %s residual $%.2f under venue "
                              "minimum — manual close needed", v.name,
                              qty * limit)
                    continue
                await lk.acquire()
                try:
                    info = await v.send_taker(is_buy=is_buy, qty=qty,
                                              limit_px=limit,
                                              reduce_only=True)
                    fill = info.get("filled_base") or 0.0
                    px = info.get("avg_px") or limit
                    if fill:
                        fee = v.fee_bps / 1e4
                        v.position += fill if is_buy else -fill
                        v.cash += -fill * px * (1 + fee) if is_buy \
                            else fill * px * (1 - fee)
                        v.volume_usd += fill * px
                        self.ledger.fill(v.key, is_buy=is_buy, qty=fill,
                                         px=px, fee_bps=v.fee_bps)
                    v.last_traded_ts = time.time()
                    if info.get("err") or info.get("unresolved"):
                        log.error("[FLATTEN] %s: %s", v.name,
                                  info.get("err") or "unresolved")
                        self._reconcile_evt.set()
                    else:
                        log.info("[FLATTEN] %s %s %.6g @ %.6g", v.name,
                                 "BUY" if is_buy else "SELL", fill, px)
                finally:
                    lk.release()
            await asyncio.sleep(0.5)
        left = " ".join(f"{v.name} {v.position:+.6g}"
                        for v in self.venues.values() if abs(v.position) > 0)
        if left:
            log.critical("[FLATTEN] INCOMPLETE — residual %s; reconcile will "
                         "retry, check manually / 未完全平仓", left)

    def premium_bps(self) -> Optional[float]:
        em, hm = self.entropy.book.mid(), self.hedge.book.mid()
        if not (em and hm):
            return None
        return (em / hm - 1.0) * 1e4

    WATCH_INTERVAL_SEC = 60.0

    async def _watch_loop(self) -> None:
        """Observability loop: funding rates + midline drift, refreshed once
        a minute. Purely informational except auto_midline (opt-in), which
        moves the effective midline inside a clamp around the configured
        value and logs every change."""
        while not self.stop.is_set():
            try:
                await self._watch_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("watch tick failed")
            try:
                await asyncio.wait_for(self.stop.wait(),
                                       timeout=self.WATCH_INTERVAL_SEC)
            except asyncio.TimeoutError:
                pass

    def _freeze_gate(self) -> None:
        """One watch tick's worth of frozen-tuner bookkeeping: measure the
        premium's direction of travel (recent 1h vs the hour before it) and
        arm the open-side lock; start / age the freeze clock and flip on the
        last-60-min re-anchor fallback once the freeze has run long enough.
        The lock dies with the freeze — closes are never blocked."""
        now = time.time()
        if self._frozen_since is None:
            self._frozen_since = now
        rows = read_minute_rows(self.cfg.recorder_csv, 2.0)
        lock = None
        if len(rows) >= 30:
            recent = [r[1] for r in rows if r[0] >= now - 3600.0]
            prior = [r[1] for r in rows
                     if now - 7200.0 <= r[0] < now - 3600.0]
            if len(recent) >= 12 and len(prior) >= 12:
                d = median(recent) - median(prior)
                if d > 1.0:
                    lock = "up"
                elif d < -1.0:
                    lock = "down"
        if lock != self._drift_lock:
            if lock:
                log.warning("drift lock ON (%s): opens against the drift are "
                            "blocked until the regime settles — closes still "
                            "free", lock)
            else:
                log.warning("drift lock OFF: drift stalled inside the "
                            "stability bar (tuner still frozen)")
            self._drift_lock = lock
        fb = self.cfg.auto_frozen_fallback_min
        if fb > 0 and not self._fallback_on \
                and now - self._frozen_since >= fb * 60.0:
            self._fallback_on = True
            log.warning("tuner frozen >%.0f min -> fallback: re-anchor "
                        "midline/band from the last 60 min (drift lock %s "
                        "stays)", fb, self._drift_lock or "off")

    async def _watch_tick(self) -> None:
        for v in self.venues.values():
            getter = getattr(v, "fetch_funding", None)
            if getter is None:
                continue
            try:
                self.funding[v.key] = await getter()
            except Exception as e:
                log.debug("[%s] funding fetch failed: %r", v.name, e)
        if not self.cfg.recorder_enabled:
            return
        # regime-aware drift: track the short recent window so a config
        # updated to the current market isn't dragged toward a stale 24h
        # blend during a regime shift; self.drift keeps the 24h view
        rreg = regime_drift_report(self.cfg.recorder_csv, self.cfg.midline_bps)
        self.drift = {"median": rreg["base_median"],
                      "drift": rreg["base_drift"], "n": rreg["base_n"]}
        # 1h display window: only 15 samples required — a fresh restart (or
        # a recorder gap) must not blank the session panel for 30 minutes
        self.drift_1h = drift_report(self.cfg.recorder_csv,
                                     self.cfg.midline_bps, 1.0,
                                     min_samples=15)
        if not (self.cfg.auto_midline or self.cfg.auto_band):
            return
        # Both tuners run on ONE stable-regime window: the first candidate
        # with enough samples decides. A diverging third (mid-jump) freezes
        # midline AND bands — chasing a blended median/diluted p90 is how
        # auto-tuners whipsaw. Fixed auto_midline_hours skips the stability
        # gate (explicit user choice); missing data widens the window.
        if self.cfg.auto_midline_hours:
            rows = read_minute_rows(self.cfg.recorder_csv,
                                    self.cfg.auto_midline_hours)
            win = self.cfg.auto_midline_hours
        else:
            rows, win = stable_window(self.cfg.recorder_csv)
        if not rows or win is None:
            # frozen (slow drift / mid-jump): don't chase, but don't trade a
            # stale seed blindly either — lock opens against the drift and,
            # after auto_frozen_fallback_min, retune from the last 60 min
            self._freeze_gate()
            if not self._fallback_on:
                return
            rows = read_minute_rows(self.cfg.recorder_csv, 1.0)
            if len(rows) < 15:
                return
            win = 1.0
        else:
            self._frozen_since = None
            self._fallback_on = False
            self._drift_lock = None
        if self.cfg.auto_midline:
            target = median([r[1] for r in rows])
            clamp = self.cfg.auto_midline_clamp_bps
            clamped = min(max(target, self.cfg.midline_bps - clamp),
                          self.cfg.midline_bps + clamp)
            if abs(clamped - self.midline) >= 0.15:
                # hysteresis: a 1h rolling median crawls ~0.05bps/min during
                # drift; re-anchoring every tick is log spam, not signal
                log.warning("midline auto-adjust %+.2f -> %+.2f bps (%.0fh "
                            "stable median %+.2f, clamp ±%.1f around anchor "
                            "%+.2f)", self.midline, clamped, win, target,
                            clamp, self.cfg.midline_bps)
                self.midline = clamped
        if self.cfg.auto_band:
            fees = self.entropy.fee_bps + self.hedge.fee_bps
            up, lo = auto_band_targets(rows, self.midline, fees,
                                       self.cfg.auto_band_trigger_pct)
            if up is None:
                return
            floor, ceil_ = (self.cfg.auto_band_floor_bps,
                            self.cfg.auto_band_ceiling_bps)
            up = min(max(up, floor), ceil_)
            lo = min(max(lo, floor), ceil_)
            if abs(up - self.upper) >= 0.25 or abs(lo - self.lower) >= 0.25:
                log.warning("band auto-adjust +%.2f/-%.2f -> +%.2f/-%.2f bps "
                            "(p%.0f exec room, %.0fh window, fees %.2f)",
                            self.upper, self.lower, up, lo,
                            self.cfg.auto_band_trigger_pct, win, fees)
                self.upper, self.lower = up, lo

    async def _status_loop(self) -> None:
        cfg = self.cfg
        while not self.stop.is_set():
            try:
                await asyncio.sleep(cfg.status_interval_sec)
            except asyncio.CancelledError:
                raise
            await self._risk_check()
            books = " | ".join(
                f"{v.name} {v.book.best_bid() or '—'}/{v.book.best_ask() or '—'}"
                + ("" if v.book.is_fresh(cfg.staleness_sec) else " STALE")
                + (" RATE-LTD" if self._venue_limited(v) else "")
                + (" DOWN" if v.key in self._venue_down else "")
                for v in self.venues.values())
            prem = self.premium_bps()
            prem_s = f"{prem:+.2f}" if prem is not None else "—"
            # executable premiums — what the trigger actually compares, unlike
            # the mid above: buy pays entropy ask vs hedge bid, sell the reverse
            try:
                ea, eb = (self.entropy.book.best_ask(),
                          self.entropy.book.best_bid())
                hab, hask = (self.hedge.book.best_bid(),
                             self.hedge.book.best_ask())
                ex_b = (ea / hab - 1) * 1e4 if ea and hab else None
                ex_s = (eb / hask - 1) * 1e4 if eb and hask else None
                ex_disp = (f" exec {ex_b:+.2f}/{ex_s:+.2f}"
                           if ex_b is not None and ex_s is not None else "")
            except (AttributeError, TypeError, ZeroDivisionError):
                ex_disp = ""
            pos = " ".join(f"{v.name} {v.position:+.6g}"
                           for v in self.venues.values())
            net = sum(v.position for v in self.venues.values())
            pnl = self.session_pnl()
            rec = (f" | rec {self.recorder.rows_written} rows"
                   if self.recorder else "")
            mid, up, lo = self._band()
            r_ = self.ledger.realized()
            u_ = self.ledger.unrealized({v.key: (v.book.mid() or 0.0)
                                         for v in self.venues.values()})
            f_ = self.ledger.fees()
            log.info("[status] %s | prem %s bps%s (band %+.2f..%+.2f) | pos %s "
                     "net %+.6g | trades %d hedges %d | pnl R $%+.4f u $%+.4f "
                     "day $%+.4f fees $%.4f | MTM %s expEdge $%.4f "
                     "fillEdge $%.4f%s%s",
                     books, prem_s, ex_disp, mid - lo, mid + up,
                     pos, net, self.trades, self.hedges,
                     r_, u_, self.day_pnl(), f_,
                     f"${pnl:+.4f}" if pnl is not None else "—",
                     self.total_exp_edge, self.total_fill_edge, rec,
                     " *** HALTED ***" if self.halted else "")

    def _log_csv(self, direction, buy, sell, plan: ArbPlan, ok: bool, bfill,
                 sfill, bstatus, sstatus, fill_edge, inv_bps) -> None:
        try:
            path = self.cfg.trades_csv
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            if os.path.exists(path):
                with open(path) as fh0:
                    if fh0.readline().strip() != ",".join(CSV_HEADER):
                        os.replace(path, path + ".old")
            new = not os.path.exists(path)
            with open(path, "a", newline="") as fh:
                w = csv.writer(fh)
                if new:
                    w.writerow(CSV_HEADER)
                w.writerow([f"{time.time():.3f}",
                            direction, buy.name, sell.name, f"{plan.qty:.8g}",
                            plan.buy_limit, plan.sell_limit,
                            f"{plan.buy_notional:.2f}", f"{plan.sell_notional:.2f}",
                            f"{plan.exp_edge_usd:.4f}", f"{plan.gross_edge_usd:.4f}",
                            f"{plan.marginal_premium_bps:.3f}",
                            f"{self._band()[0]:.3f}",
                            f"{inv_bps:.3f}", int(ok), f"{bfill:.8g}",
                            f"{sfill:.8g}", bstatus, sstatus, f"{fill_edge:.4f}"])
        except Exception:
            log.exception("csv write failed")

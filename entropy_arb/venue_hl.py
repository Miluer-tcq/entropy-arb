"""Hyperliquid HIP-3 dex venue adapter (Entropy = dex "io", trade.xyz = "xyz").

Market metadata, account state and order posting use Hyperliquid's public
/info and /exchange REST endpoints via plain aiohttp; the book comes from the
OFFICIAL websocket (see feeds.HLBookFeed). Trading lazily imports the
official `hyperliquid-python-sdk` signing helpers + eth_account —
--record-only data collection needs neither.

IOC limit orders settle synchronously in the /exchange response; unknown
outcomes (timeout/5xx) fall back to orderStatus-by-cloid polling inside
send_taker(), so the engine sees the same unified result shape as the Lighter
venue: {status, filled_base, avg_px, err, unresolved}.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import time
from typing import Optional

import aiohttp

from .book import OrderBook
from .config import VenueConf
from .feeds import HLBookFeed

log = logging.getLogger("hl")

INFO_TIMEOUT = 10.0


class NonceAllocator:
    def __init__(self) -> None:
        self._last = 0

    def next(self) -> int:
        self._last = max(self._last + 1, int(time.time() * 1000))
        return self._last


class HLAccount:
    def __init__(self, private_key: str, account_address: Optional[str],
                 api_url: str) -> None:
        from eth_account import Account
        self.wallet = Account.from_key(private_key)
        self.query_address = (account_address or self.wallet.address).lower()
        self.is_mainnet = api_url == "https://api.hyperliquid.xyz"
        self.nonces = NonceAllocator()

    def describe(self) -> str:
        s = f"signer={self.wallet.address} account={self.query_address}"
        if self.wallet.address.lower() != self.query_address:
            s += " (agent mode)"
        return s


class HLVenue:
    kind = "hl"

    def __init__(self, conf: VenueConf, api_url: str, ws_url: str,
                 session: aiohttp.ClientSession, settle_timeout_sec: float) -> None:
        self.conf = conf
        self.key = conf.key
        self.name = conf.label
        self.api_url = api_url
        self.ws_url = ws_url
        self.session = session
        self.settle_timeout = settle_timeout_sec
        self.book = OrderBook()
        self.position = 0.0
        self.cash = 0.0
        self.volume_usd = 0.0     # cumulative filled notional this session
        self.equity = None
        self.free = None
        self.start_equity = None
        self.include_core_equity = True  # cleared when two venues share one account
        self.fee_bps = conf.fee_bps
        self.fee_src = "config"
        self.cap_usd = conf.cap_usd
        self.orders_per_min = conf.orders_per_min
        self.last_traded_ts = 0.0
        self.account: Optional[HLAccount] = None
        self.coin = ""
        self.asset_id = -1
        self.size_decimals = 0
        self.min_base = 0.0
        self.min_quote = 10.0
        self._cloid = int(time.time() * 1000)
        self._signing = None      # lazy hyperliquid-sdk signing module
        self._unified: Optional[bool] = None  # cached userAbstraction state
        # /info budget hygiene (HL rate-limits per IP, and all recorder/engine
        # processes on this box share one budget — a 429 today proved that):
        # serialized pacing between calls, exponential penalty on 429, plus a
        # short per-payload cache so one reconcile cycle's three near-identical
        # clearinghouse reads cost one request.
        self._info_lock: Optional[asyncio.Lock] = None
        self._info_next = 0.0
        self._info_penalty = 0.0
        self._info_cache: dict = {}
        self.info_min_interval = 0.25
        # live Gtc maker orders (cloid) — cancel_resting() drains these on
        # shutdown so no passive order can survive the process
        self._resting: set = set()

    async def _info(self, payload: dict, ttl: float = 0.0):
        """POST /info with pacing, 429 retry, and optional (sub-second-TTL)
        result coalescing. ttl=0 must be used for anything a fresh answer
        matters for (orderStatus polls, position reconcile)."""
        if self._info_lock is None:
            self._info_lock = asyncio.Lock()
        key = json.dumps(payload, sort_keys=True) if ttl > 0 else None
        if key:
            hit = self._info_cache.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
        err = None
        for _attempt in range(2):
            async with self._info_lock:
                gap = self._info_next - time.monotonic()
                if gap > 0:
                    await asyncio.sleep(gap)
                self._info_next = (time.monotonic() + self.info_min_interval
                                   + self._info_penalty)
                wait = 0.0
                try:
                    async with self.session.post(
                            self.api_url + "/info", json=payload,
                            timeout=aiohttp.ClientTimeout(
                                total=INFO_TIMEOUT)) as r:
                        if r.status == 429:
                            self._info_penalty = min(
                                max(self._info_penalty, 0.5) * 2.0, 15.0)
                            ra = r.headers.get("Retry-After")
                            try:
                                wait = min(float(ra), 5.0) if ra else 1.5
                            except ValueError:
                                wait = 1.5
                            err = aiohttp.ClientResponseError(
                                r.request_info, r.history, status=429,
                                message="Too Many Requests")
                        else:
                            r.raise_for_status()
                            data = await r.json()
                            self._info_penalty *= 0.5
                            if key and len(self._info_cache) < 64:
                                self._info_cache[key] = (time.monotonic()
                                                         + ttl, data)
                            return data
                except aiohttp.ClientResponseError as e:
                    if e.status != 429:
                        raise
                    self._info_penalty = min(max(self._info_penalty, 0.5)
                                             * 2.0, 15.0)
                    wait, err = 1.5, e
                except (aiohttp.ClientConnectorError,
                        aiohttp.ServerTimeoutError,
                        asyncio.TimeoutError) as e:
                    # sporadic TLS resets (a Clash/proxy or HL edge blip must
                    # not kill a --record-only start): one retry, then raise
                    wait, err = 1.5, e
            if wait:
                await asyncio.sleep(wait)
        if err is not None:
            raise err
        raise RuntimeError("HL /info retry loop exited without result")

    async def load_market(self) -> None:
        dexs = await self._info({"type": "perpDexs"})
        names = [(d or {}).get("name", "") for d in dexs]
        if self.conf.hl_dex not in names:
            raise RuntimeError(f"[{self.name}] dex '{self.conf.hl_dex}' not "
                               f"found on Hyperliquid (available: "
                               f"{[n for n in names if n][:20]}...)")
        dex_index = names.index(self.conf.hl_dex)
        meta = await self._info({"type": "meta", "dex": self.conf.hl_dex})
        want = f"{self.conf.hl_dex}:{self.conf.symbol}"
        for idx, a in enumerate(meta["universe"]):
            if a["name"] not in (want, self.conf.symbol):
                continue
            if a.get("isDelisted"):
                raise RuntimeError(f"[{self.name}] {a['name']} is delisted")
            self.coin = a["name"]
            self.asset_id = 110000 + (dex_index - 1) * 10000 + idx
            self.size_decimals = int(a["szDecimals"])
            self.min_base = 10 ** -self.size_decimals
            log.info("[%s] %s asset_id=%d szDecimals=%d maxLev=%sx %s",
                     self.name, self.coin, self.asset_id, self.size_decimals,
                     a.get("maxLeverage"),
                     "isolated-only" if a.get("onlyIsolated") else "")
            return
        raise RuntimeError(f"[{self.name}] {want} not found")

    def init_signer(self) -> None:
        c = self.conf.hl_creds
        assert c is not None and c.complete, f"[{self.name}] missing credentials"
        try:
            from hyperliquid.utils import signing as hl_signing
        except ImportError as e:
            raise RuntimeError(
                "live trading on Hyperliquid needs the official SDK — "
                "pip install -r requirements-live.txt "
                "(hyperliquid-python-sdk)") from e
        self._signing = hl_signing
        self.account = HLAccount(c.private_key, c.account_address, self.api_url)
        log.info("[%s] %s", self.name, self.account.describe())

    def share_nonces_with(self, other: "HLVenue") -> None:
        """One signer address must use one nonce sequence."""
        if (self.account and other.account and
                self.account.wallet.address == other.account.wallet.address):
            other.account.nonces = self.account.nonces
            log.info("[%s]/[%s] same signer — shared nonce allocator",
                     self.name, other.name)

    def start_tasks(self, stop: asyncio.Event, notify, live: bool) -> list:
        return [asyncio.create_task(
            HLBookFeed(self.name, self.ws_url, self.coin, self.book,
                       notify).run(stop),
            name=f"book-{self.key}")]

    def ready_to_trade(self) -> bool:
        return self.account is not None

    async def warm_http(self) -> None:
        """Order-path keepalive ping (driven by the engine's keepalive loop)."""
        try:
            await self._info({"type": "exchangeStatus"})
        except Exception as e:
            log.debug("[%s] keepalive ping failed: %r", self.name, e)

    # ------------------------------------------------------------ price grid

    def px_round(self, px: float, round_up: bool) -> float:
        if px <= 0:
            return px
        max_dec = max(0, 6 - self.size_decimals)
        sig_dec = 4 - math.floor(math.log10(px))
        dec = max(0, min(max_dec, sig_dec))
        f = 10.0 ** dec
        v = math.ceil(px * f - 1e-9) / f if round_up else math.floor(px * f + 1e-9) / f
        return round(v, 8)

    # ------------------------------------------------------------- execution

    async def apply_leverage(self) -> None:
        """Declare this asset's isolated leverage (HL defaults isolated-only
        assets to 1x, which locks full notional as margin). Idempotent; a
        rejected update only logs — order flow never depends on it."""
        lev = int(getattr(self.conf, "hl_leverage", 1) or 1)
        if lev <= 1 or self.account is None or self.asset_id < 0:
            return
        s = self._signing
        action = {"type": "updateLeverage", "asset": self.asset_id,
                  "isCross": False, "leverage": lev}
        try:
            nonce = self.account.nonces.next()
            sig = s.sign_l1_action(self.account.wallet, action, None, nonce,
                                   None, self.account.is_mainnet)
            payload = {"action": action, "nonce": nonce, "signature": sig,
                       "vaultAddress": None, "expiresAfter": None}
        except Exception as e:
            log.warning("[%s] updateLeverage signing failed: %r", self.name, e)
            return
        body, err, unresolved = await self._post_exchange(payload)
        if err or unresolved:
            log.warning("[%s] updateLeverage(%dx) not confirmed: %s",
                        self.name, lev, err or "unresolved")
        elif isinstance(body, dict) and body.get("status") == "err":
            log.warning("[%s] updateLeverage(%dx) rejected: %s — leaving the "
                        "venue default in place", self.name, lev,
                        str(body.get("response"))[:150])
        else:
            log.info("[%s] isolated leverage set to %dx", self.name, lev)

    def _next_cloid(self):
        from hyperliquid.utils.types import Cloid
        self._cloid += 1
        return Cloid.from_int(self._cloid)

    async def send_taker(self, *, is_buy: bool, qty: float, limit_px: float,
                         reduce_only: bool = False) -> dict:
        assert self.account is not None and self.asset_id >= 0
        s = self._signing
        cloid = self._next_cloid()
        order_req = {"coin": self.coin, "is_buy": is_buy, "sz": round(qty, 8),
                     "limit_px": limit_px,
                     "order_type": {"limit": {"tif": "Ioc"}},
                     "reduce_only": reduce_only, "cloid": cloid}
        try:
            wire = s.order_request_to_order_wire(order_req, self.asset_id)
            action = s.order_wires_to_order_action([wire])
            nonce = self.account.nonces.next()
            sig = s.sign_l1_action(self.account.wallet, action, None, nonce,
                                   None, self.account.is_mainnet)
            payload = {"action": action, "nonce": nonce, "signature": sig,
                       "vaultAddress": None, "expiresAfter": None}
        except Exception as e:
            return {"status": "send-failed", "filled_base": 0.0, "avg_px": None,
                    "err": f"signing failed: {e!r}", "unresolved": False}

        body, err, unresolved = await self._post_exchange(payload)
        if err is not None:
            return {"status": "send-failed", "filled_base": 0.0, "avg_px": None,
                    "err": err, "unresolved": False}
        if not unresolved:
            res = self._parse(body)
            if not res.get("unresolved"):
                return res
        # unknown outcome: poll orderStatus by cloid until the deadline
        deadline = time.time() + self.settle_timeout
        while time.time() < deadline:
            try:
                st = await self._info({"type": "orderStatus",
                                       "user": self.account.query_address,
                                       "oid": cloid.to_raw()})
            except Exception:
                st = None
            if st and st.get("status") == "order":
                o = st.get("order") or {}
                status = str(o.get("status", ""))
                inner = o.get("order") or {}
                try:
                    filled = max(float(inner.get("origSz") or 0)
                                 - float(inner.get("sz") or 0), 0.0)
                except (TypeError, ValueError):
                    filled = 0.0
                if status != "open":
                    return {"status": status, "filled_base": filled,
                            "avg_px": None, "err": None, "unresolved": False}
            await asyncio.sleep(0.5)
        return {"status": "timeout", "filled_base": 0.0, "avg_px": None,
                "err": None, "unresolved": True}

    async def _order_state(self, cloid) -> Optional[dict]:
        """(status, filled_base, avg_px) for one of our orders by cloid, or
        None while the read fails / the order is unknown yet."""
        try:
            st = await self._info({"type": "orderStatus",
                                   "user": self.account.query_address,
                                   "oid": cloid.to_raw()})
        except Exception:
            return None
        if not st or st.get("status") != "order":
            return None
        o = st.get("order") or {}
        inner = o.get("order") or {}
        try:
            filled = max(float(inner.get("origSz") or 0.0)
                         - float(inner.get("sz") or 0.0), 0.0)
        except (TypeError, ValueError):
            filled = 0.0
        tf = o.get("totalFilled")
        if tf not in (None, ""):
            try:
                filled = max(filled, float(tf))
            except (TypeError, ValueError):
                pass
        avg = None
        try:
            if filled > 0 and o.get("avgFillPrice"):
                avg = float(o["avgFillPrice"])
        except (TypeError, ValueError):
            avg = None
        return {"status": str(o.get("status", "")), "filled": filled,
                "avg_px": avg}

    async def _cancel_cloid(self, cloid) -> None:
        s = self._signing
        action = {"type": "cancelByCloid",
                  "cancels": [{"asset": self.asset_id,
                               "cloid": cloid.to_raw()}]}
        try:
            nonce = self.account.nonces.next()
            sig = s.sign_l1_action(self.account.wallet, action, None, nonce,
                                   None, self.account.is_mainnet)
            await self._post_exchange(
                {"action": action, "nonce": nonce, "signature": sig,
                 "vaultAddress": None, "expiresAfter": None})
        except Exception as e:
            log.warning("[%s] cancelByCloid failed: %r", self.name, e)

    async def send_maker(self, *, is_buy: bool, qty: float, limit_px: float,
                         wait_sec: float, reduce_only: bool = False) -> dict:
        """Post a Gtc resting order and watch it for up to wait_sec.

        The maker ladder bet: if the displayed price was a genuine quote the
        book returns to it and we get PAID to be right (fill without
        crossing); if it was a jump into a moving market nobody ever trades
        with us and the order is cancelled — zero cost. Returns the same
        dict shape as send_taker (status 'filled' with quantity, or
        'canceled'/'send-failed'). Guarantees no resting order survives the
        call: it is cancelled before returning even on cancellation."""
        assert self.account is not None and self.asset_id >= 0
        s = self._signing
        cloid = self._next_cloid()
        order_req = {"coin": self.coin, "is_buy": is_buy,
                     "sz": round(qty, 8), "limit_px": limit_px,
                     "order_type": {"limit": {"tif": "Gtc"}},
                     "reduce_only": reduce_only, "cloid": cloid}
        try:
            wire = s.order_request_to_order_wire(order_req, self.asset_id)
            action = s.order_wires_to_order_action([wire])
            nonce = self.account.nonces.next()
            sig = s.sign_l1_action(self.account.wallet, action, None, nonce,
                                   None, self.account.is_mainnet)
            payload = {"action": action, "nonce": nonce, "signature": sig,
                       "vaultAddress": None, "expiresAfter": None}
        except Exception as e:
            return {"status": "send-failed", "filled_base": 0.0,
                    "avg_px": None, "err": f"signing failed: {e!r}",
                    "unresolved": False}
        self._resting.add(cloid)
        try:
            body, err, ambiguous = await self._post_exchange(payload)
            res = None if (err or ambiguous or not body) else self._parse(body)
            if res and res.get("err"):
                return res
            if res and res["status"] == "filled":
                return res          # crossed on arrival: instant fill
            if err or ambiguous or res is None:
                # order may or may not be live: one state read decides
                st = await self._order_state(cloid)
                if st and st["status"] != "open":
                    return await self._maker_terminal(st)
                await self._cancel_cloid(cloid)
                st = await self._order_state(cloid)
                if st and st["filled"] > 0:
                    return await self._maker_terminal(st)
                return {"status": "canceled", "filled_base": 0.0,
                        "avg_px": None, "err": err, "unresolved": False}
            # resting: watch for a pull-back into our price
            deadline = time.time() + max(wait_sec, 0.2)
            while time.time() < deadline:
                await asyncio.sleep(0.35)
                st = await self._order_state(cloid)
                if st and st["status"] and st["status"] != "open":
                    return await self._maker_terminal(st)
            # patience over: pull it (race-safe final read)
            await self._cancel_cloid(cloid)
            st = await self._order_state(cloid)
            if st and st["filled"] > 0:
                return await self._maker_terminal(st)
            return {"status": "canceled", "filled_base": 0.0,
                    "avg_px": None, "err": None, "unresolved": False}
        except asyncio.CancelledError:
            # shutdown mid-watch: the order is still live — the shield keeps
            # the cancel request in flight even though we propagate
            with contextlib.suppress(Exception):
                await asyncio.shield(self._cancel_cloid(cloid))
            raise
        finally:
            self._resting.discard(cloid)

    async def _maker_terminal(self, st: dict) -> dict:
        """A watch loop ended on a non-open order: HL reports 'filled' for a
        completed resting order but the status string can also be a cancel
        reason with a partial fill behind it — take the economics, not the
        label."""
        if st["filled"] > 0:
            return {"status": "filled", "filled_base": st["filled"],
                    "avg_px": st["avg_px"], "err": None, "unresolved": False}
        return {"status": st["status"] or "canceled", "filled_base": 0.0,
                "avg_px": None, "err": None, "unresolved": False}

    async def cancel_resting(self) -> None:
        """Shutdown safety: no maker order may survive the process."""
        for cloid in list(self._resting):
            await self._cancel_cloid(cloid)
        self._resting.clear()

    async def _post_exchange(self, payload: dict):
        try:
            async with self.session.post(
                    self.api_url + "/exchange", json=payload,
                    timeout=aiohttp.ClientTimeout(total=INFO_TIMEOUT)) as r:
                text = await r.text()
                if r.status == 429:
                    return None, f"RATE_LIMITED: HTTP 429 {text[:150]}", False
                if 400 <= r.status < 500:
                    return None, f"HTTP {r.status}: {text[:250]}", False
                if r.status >= 500:
                    return None, None, True
                return json.loads(text), None, False
        except (asyncio.TimeoutError, aiohttp.ClientError, json.JSONDecodeError):
            return None, None, True

    @staticmethod
    def _parse(body: dict) -> dict:
        def fail(msg: str) -> dict:
            low = msg.lower()
            if "rate limit" in low or "too many" in low:
                msg = "RATE_LIMITED: " + msg
            return {"status": "send-failed", "filled_base": 0.0, "avg_px": None,
                    "err": msg, "unresolved": False}
        if body.get("status") == "err":
            return fail(str(body.get("response")))
        if body.get("status") != "ok":
            return fail(f"unexpected response: {str(body)[:200]}")
        try:
            st = body["response"]["data"]["statuses"][0]
        except (KeyError, IndexError, TypeError):
            return fail(f"malformed response: {str(body)[:200]}")
        if "filled" in st:
            f = st["filled"]
            return {"status": "filled",
                    "filled_base": float(f.get("totalSz") or 0.0),
                    "avg_px": float(f["avgPx"]) if f.get("avgPx") else None,
                    "err": None, "unresolved": False}
        if "error" in st:
            msg = str(st["error"])
            if "could not immediately match" in msg.lower():
                return {"status": "canceled", "filled_base": 0.0, "avg_px": None,
                        "err": None, "unresolved": False}
            return fail(msg)
        if "resting" in st:
            return {"status": "resting?", "filled_base": 0.0, "avg_px": None,
                    "err": None, "unresolved": True}
        return fail(f"unknown status: {str(st)[:150]}")

    # -------------------------------------------------------------- accounts

    def _query_address(self):
        if self.account is not None:
            return self.account.query_address
        c = self.conf.hl_creds
        return c.account_address.lower() if c and c.account_address else None

    async def fetch_equity(self):
        """Unified account equity via the portfolio endpoint — the same
        Portfolio Value the HL UI shows. "Free" is the withdrawable balance
        of this venue's dex bucket (isolated margin actually backing the
        bot's positions). Falls back to summing clearinghouse buckets if the
        endpoint shape changes. When both venues share one HL account
        (include_core_equity cleared on the hedge), that venue reports only
        its dex bucket to avoid double-counting.

        Under the "unifiedAccount" abstraction (HIP-3 builder dexes draw
        margin straight from spot USDC), free margin additionally includes
        the account's free spot USDC — otherwise a funded spot and empty dex
        buckets would show as $0 free despite orders being accepted."""
        addr = self._query_address()
        if addr is None:
            return None
        if self.include_core_equity:
            try:
                p = await self._info({"type": "portfolio", "user": addr}, ttl=2.0)
                for period, d in p:
                    if period == "day":
                        hist = d.get("accountValueHistory") or []
                        if hist:
                            free = await self._dex_withdrawable(addr)
                            if await self._is_unified(addr):
                                free += await self._spot_usdc_free(addr)
                            return (float(hist[-1][1]), free)
            except Exception as e:
                log.debug("[%s] portfolio fetch failed, falling back: %r",
                          self.name, e)
        dexs = [self.conf.hl_dex] + ([""] if self.include_core_equity else [])
        eq = fr = 0.0
        for dex in dexs:
            st = await self._info({"type": "clearinghouseState", "user": addr,
                                   "dex": dex}, ttl=2.0)
            ms = st.get("marginSummary") or {}
            eq += float(ms.get("accountValue") or 0.0)
            fr += float(st.get("withdrawable") or 0.0)
        if self.include_core_equity and await self._is_unified(addr):
            fr += await self._spot_usdc_free(addr)
        return eq, fr

    @staticmethod
    def spot_usdc_free(balances: list) -> float:
        """Free USDC in the spot account: total minus open-order holds."""
        for b in balances or []:
            if b.get("coin") == "USDC":
                try:
                    return max(float(b.get("total") or 0.0)
                               - float(b.get("hold") or 0.0), 0.0)
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    async def _spot_usdc_free(self, addr: str) -> float:
        try:
            st = await self._info({"type": "spotClearinghouseState",
                                   "user": addr}, ttl=2.0)
            return self.spot_usdc_free(st.get("balances") or [])
        except Exception as e:
            log.debug("[%s] spot balance fetch failed: %r", self.name, e)
            return 0.0

    async def _is_unified(self, addr: str) -> bool:
        """True when the account uses the "unifiedAccount" abstraction
        (spot USDC directly backs perp dex orders). A successful lookup is
        cached; a transient failure is NOT cached — it retries on the next
        equity poll instead of freezing a wrong answer."""
        if self._unified is None:
            try:
                r = await self._info({"type": "userAbstraction", "user": addr},
                            ttl=60.0)
            except Exception as e:
                log.debug("[%s] abstraction lookup failed: %r", self.name, e)
                return False
            self._unified = r == "unifiedAccount"
        return self._unified

    async def _dex_withdrawable(self, addr: str) -> Optional[float]:
        """Withdrawable balance of this venue's dex bucket (isolated margin
        free to use); None when the API call fails."""
        try:
            st = await self._info({"type": "clearinghouseState", "user": addr,
                                   "dex": self.conf.hl_dex}, ttl=2.0)
            return float(st.get("withdrawable") or 0.0)
        except Exception as e:
            log.debug("[%s] withdrawable fetch failed: %r", self.name, e)
            return None

    async def fetch_fee(self) -> Optional[float]:
        """Account's effective taker rate from the userFees endpoint (bps).
        None when unavailable or outside the sanity range — callers keep the
        configured value in that case."""
        addr = self._query_address()
        if addr is None:
            return None
        try:
            r = await self._info({"type": "userFees", "user": addr}, ttl=300.0)
            rate = r.get("userCrossRate")
            if rate is None:
                return None
            bps = float(rate) * 1e4
        except Exception as e:
            log.debug("[%s] userFees fetch failed: %r", self.name, e)
            return None
        return bps if 0.0 <= bps <= 20.0 else None

    async def fetch_funding(self) -> Optional[float]:
        """Current hourly funding rate (decimal) for this venue's coin, from
        the dex's metaAndAssetCtxs. None when unavailable."""
        try:
            meta, ctxs = await self._info(
                {"type": "metaAndAssetCtxs", "dex": self.conf.hl_dex},
                ttl=60.0)
            names = [a["name"] for a in meta.get("universe") or []]
            if self.coin not in names:
                return None
            ctx = ctxs[names.index(self.coin)] or {}
            f = ctx.get("funding")
            return float(f) if f is not None else None
        except Exception as e:
            log.debug("[%s] funding fetch failed: %r", self.name, e)
            return None

    async def fetch_position(self) -> float:
        addr = self._query_address()
        assert addr is not None
        st = await self._info({"type": "clearinghouseState", "user": addr,
                               "dex": self.conf.hl_dex}, ttl=2.0)
        for ap in st.get("assetPositions") or []:
            pos = ap.get("position") or {}
            if pos.get("coin") == self.coin:
                return float(pos.get("szi") or 0.0)
        return 0.0

    async def close(self) -> None:
        pass

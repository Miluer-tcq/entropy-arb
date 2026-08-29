"""--preflight: run every startup check that has ever bitten, then exit.

Green/red checklist: .env format, SDK imports, both venues' markets active,
margin vs configured caps, recorder data freshness and midline drift. No
orders are placed. Exit code 0 = go, 1 = at least one FAIL.

    python main.py --preflight --symbol SNDK --hedge lighter \
        --config config-lighter.yaml
"""
from __future__ import annotations

import asyncio
import re
from typing import List, Tuple

import aiohttp

from .config import Config
from .monitor import drift_report, last_row_age_sec

_HL_KEY_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_HL_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class _Checks:
    def __init__(self) -> None:
        self.rows: List[Tuple[str, str, str]] = []
        self.failed = 0

    def ok(self, name: str, detail: str = "") -> None:
        self.rows.append(("OK", name, detail))

    def warn(self, name: str, detail: str = "") -> None:
        self.rows.append(("WARN", name, detail))

    def fail(self, name: str, detail: str = "") -> None:
        self.failed += 1
        self.rows.append(("FAIL", name, detail))

    def print(self) -> None:
        mark = {"OK": "  \u2713 ", "WARN": "  \u26a0 ", "FAIL": "  \u2717 "}
        for status, name, detail in self.rows:
            line = f"{mark[status]}{name}"
            if detail:
                line += f" — {detail}"
            print(line)
        print("=" * 62)
        print("PREFLIGHT: GO" if not self.failed else
              f"PREFLIGHT: NO-GO ({self.failed} FAIL)")


def _margin_check(c: _Checks, name: str, free: float, cfg: Config) -> None:
    cap = min(cfg.entropy.cap_usd, cfg.hedge.cap_usd)
    if free < cfg.min_order_notional:
        c.fail(name, f"${free:,.2f} — below one minimum order "
                     f"(${cfg.min_order_notional:,.0f}); orders would be "
                     f"rejected. Deposit funds first")
    elif free < cap:
        c.ok(name, f"${free:,.2f} — headroom-limited "
                   f"(cap ${cap:,.0f}, engine sizes to fit)")
    else:
        c.ok(name, f"${free:,.2f} ≥ cap ${cap:,.0f}")


async def run_preflight(cfg: Config) -> bool:
    c = _Checks()
    live = cfg.creds_complete

    # ---- credentials format
    hl = cfg.entropy.hl_creds
    if hl and hl.private_key:
        if _HL_KEY_RE.match(hl.private_key):
            c.ok("HL agent private key format")
        else:
            c.fail("HL agent private key format",
                   "expected 0x + 64 hex chars")
        if hl.account_address:
            if _HL_ADDR_RE.match(hl.account_address):
                c.ok("HL account address format")
            else:
                c.fail("HL account address format", "expected 0x + 40 hex")
        else:
            c.warn("HL account address missing",
                   "query address defaults to the signer wallet")
    if cfg.hedge.kind == "lighter":
        lc = cfg.hedge.lighter_creds
        if lc and lc.complete:
            c.ok("Lighter credentials present")
        else:
            c.fail("Lighter credentials missing",
                   "LIGHTER_ACCOUNT_INDEX / API_KEY_INDEX / "
                   "API_PRIVATE_KEY")
    if not live:
        c.warn("credentials incomplete", "preflight runs in record-only "
                                         "depth (no balance checks)")

    # ---- SDK imports (live only)
    if live:
        for mod in ("eth_account", "hyperliquid", "lighter"):
            try:
                __import__(mod)
                c.ok(f"SDK import: {mod}")
            except ImportError as e:
                c.fail(f"SDK import: {mod}",
                       f"{e} — pip install -r requirements-live.txt")

    async with aiohttp.ClientSession() as s:
        t10 = aiohttp.ClientTimeout(total=10)

        # ---- markets active
        try:
            async with s.post(cfg.hl_api_url + "/info",
                              json={"type": "meta",
                                    "dex": cfg.entropy.hl_dex},
                              timeout=t10) as r:
                meta = await r.json()
            names = [a["name"] for a in meta.get("universe") or []]
            sym = cfg.entropy.symbol
            hit = next((n for n in names
                        if n == sym or n.endswith(":" + sym)), None)
            if hit:
                c.ok(f"entropy market {hit} listed on dex "
                     f"{cfg.entropy.hl_dex}")
            else:
                c.fail(f"entropy symbol {sym} not on dex "
                       f"{cfg.entropy.hl_dex}")
        except Exception as e:
            c.fail("entropy market lookup", repr(e))

        if cfg.hedge.kind == "lighter":
            try:
                url = cfg.hedge.lighter_profile.api_url + "/api/v1/orderBooks"
                async with s.get(url, timeout=t10) as r:
                    obs = (await r.json()).get("order_books") or []
                ob = next((o for o in obs
                           if o.get("symbol") == cfg.hedge.symbol), None)
                if ob is None:
                    c.fail(f"hedge symbol {cfg.hedge.symbol} not on "
                           f"{cfg.hedge.label}")
                elif ob.get("status") != "active":
                    c.fail(f"hedge market status {ob.get('status')!r}")
                else:
                    c.ok(f"hedge market {cfg.hedge.symbol} active on "
                         f"{cfg.hedge.label}",
                         f"taker_fee={ob.get('taker_fee')}")
            except Exception as e:
                c.fail("hedge market lookup", repr(e))

        # ---- balances vs caps (live only)
        if live and cfg.entropy.hl_creds \
                and cfg.entropy.hl_creds.account_address:
            addr = cfg.entropy.hl_creds.account_address
            try:
                async with s.post(cfg.hl_api_url + "/info",
                                  json={"type": "clearinghouseState",
                                        "user": addr,
                                        "dex": cfg.entropy.hl_dex},
                                  timeout=t10) as r:
                    st = await r.json()
                free = float(st.get("withdrawable") or 0.0)
                _margin_check(c, "entropy margin (io dex)", free, cfg)
            except Exception as e:
                c.fail("entropy balance lookup", repr(e))

        if live and cfg.hedge.kind == "lighter" and cfg.hedge.lighter_creds:
            try:
                url = cfg.hedge.lighter_profile.api_url + "/api/v1/account"
                params = {"by": "index",
                          "value": str(cfg.hedge.lighter_creds.account_index)}
                async with s.get(url, params=params, timeout=t10) as r:
                    data = await r.json()
                acct = (data.get("accounts") or [{}])[0]
                avail = float(acct.get("available_balance") or 0.0)
                _margin_check(c, "hedge margin (lighter)", avail, cfg)
            except Exception as e:
                c.fail("hedge balance lookup", repr(e))

    # ---- recorder data + midline drift
    age = last_row_age_sec(cfg.recorder_csv)
    if age is None:
        c.warn("recorder data", f"{cfg.recorder_csv} missing/empty — run "
                                f"--record-only before trusting the midline")
    elif age > 2 * 3600:
        c.warn("recorder data stale", f"last row {age / 3600:.1f}h ago")
    else:
        c.ok("recorder data fresh", f"last row {age / 60:.0f}m ago")

    rep = drift_report(cfg.recorder_csv, cfg.midline_bps, 24.0)
    if rep["drift"] is None:
        c.warn("midline drift", "not enough recent data to verify")
    elif abs(rep["drift"]) >= 3.0:
        c.fail("midline drift", f"24h median {rep['median']:+.2f} vs config "
                                f"{cfg.midline_bps:+.2f} — update "
                                f"thresholds.midline_bps")
    elif abs(rep["drift"]) >= 1.5:
        c.warn("midline drift", f"24h median {rep['median']:+.2f} vs config "
                                f"{cfg.midline_bps:+.2f} — consider updating")
    else:
        c.ok("midline on center", f"24h median {rep['median']:+.2f} bps")

    c.print()
    return c.failed == 0

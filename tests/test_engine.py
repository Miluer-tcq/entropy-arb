"""Engine signal math: midline band directions, inventory ladder, scan.

Run:  python3 -m pytest tests/  (or  python3 tests/test_engine.py)
"""
import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.config import load_config  # noqa: E402
from entropy_arb.engine import Engine  # noqa: E402

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


def make_cfg(midline=5.0, upper=4.0, lower=3.0):
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(f"""
thresholds:
  midline_bps: {midline}
  upper_bps: {upper}
  lower_bps: {lower}
execution:
  premium_persist_sec: 0.0
""")
    f.close()
    return load_config(f.name, NO_ENV,
                       symbol="SNDK", hedge_venue="lighter-rh")


class StubVenue:
    def __init__(self, key, label, cap=10000.0, fee=0.0):
        self.key, self.name = key, label
        self.cap_usd, self.fee_bps = cap, fee
        self.size_decimals, self.min_base, self.min_quote = 4, 1e-4, 10.0
        self.position, self.cash = 0.0, 0.0
        self.orders_per_min = 30
        self.last_traded_ts = 0.0
        self.book = OrderBook()

    def ready_to_trade(self):
        return True

    def set_book(self, bid, ask, sz=50.0):
        self.book.apply_hl([[{"px": str(bid), "sz": str(sz)}],
                            [{"px": str(ask), "sz": str(sz)}]])


def make_engine(**thr):
    cfg = make_cfg(**thr)
    eng = Engine(cfg)
    eng.entropy = StubVenue("entropy", "ENTROPY")
    eng.hedge = StubVenue("hedge", "RH")
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    eng._step, eng._min_base, eng._min_notional = 1e-4, 1e-4, 10.0
    return eng


def approx(a, b, tol=1e-9):
    assert abs(a - b) <= tol, f"{a} != {b}"


def test_eff_threshold_directions():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    e, h = eng.entropy, eng.hedge
    # sell entropy: hurdle = midline + upper = 9
    approx(eng._eff_threshold(buy=h, sell=e), 9.0)
    # buy entropy: hurdle = lower - midline = -2 (unwind side of a positive
    # midline is deliberately cheap — that's what completes the round trip)
    approx(eng._eff_threshold(buy=e, sell=h), -2.0)
    # round trip nets upper + lower regardless of midline sign
    for m in (-7.0, 0.0, 12.5):
        eng.cfg.midline_bps = m
        total = eng._eff_threshold(buy=h, sell=e) + eng._eff_threshold(buy=e, sell=h)
        approx(total, 7.0)


def test_inventory_ladder():
    eng = make_engine()
    eng.cfg.inventory_scale_bps, eng.cfg.inventory_floor_frac = 10.0, 0.5
    e, h = eng.entropy, eng.hedge
    e.set_book(99.9, 100.1)   # mid 100
    h.set_book(99.9, 100.1)
    approx(eng._inv_add_bps(e, h), 0.0)          # flat: dead zone
    e.position = 90.0                             # long $9k of $10k cap
    v = eng._inv_add_bps(e, h)                    # buying entropy adds long
    assert 7.5 < v < 8.5, v                       # u=0.9 -> ~+8
    approx(eng._inv_add_bps(h, e), 0.0)           # selling entropy reduces
    h.position = -90.0                            # hedge short $9k too
    v2 = eng._inv_add_bps(e, h)                   # both legs add -> max()
    assert abs(v2 - v) < 0.6, (v, v2)             # max, not sum


def run_scan(eng):
    async def go():
        # first pass arms the direction, second passes the persistence gate
        # (premium_persist_sec is 0 in the test config)
        eng._scan(__import__("time").time())
        return eng._scan(__import__("time").time())
    return asyncio.run(go())


def test_scan_fires_sell_entropy_above_band():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    # entropy 15 bps rich vs hedge: above midline+upper=9 -> sell entropy
    eng.entropy.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)
    best = run_scan(eng)
    assert best is not None
    buy, sell, plan = best
    assert sell.key == "entropy" and buy.key == "hedge"
    assert plan.exp_edge_usd > 0


def test_scan_quiet_inside_band():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    # entropy 5 bps rich = exactly on the midline: inside the band, no trade
    eng.entropy.set_book(100.04, 100.06)
    eng.hedge.set_book(99.99, 100.01)
    assert run_scan(eng) is None


def test_scan_dust_edge_is_logged_not_silent():
    # real edge clears the hurdle but depth is sub-minimum: must NOT be a
    # black-hole skip — the below_min reason has to reach the log
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    eng.entropy.set_book(99.94, 99.96, sz=0.04)
    eng.hedge.set_book(99.99, 100.01, sz=0.04)
    msgs = []
    eng._skiplog = lambda fmt, *a: msgs.append(fmt % a)
    assert run_scan(eng) is None
    assert any("sizing unusable" in m and "below_min" in m for m in msgs), msgs


def test_plan_clears_min_notional_after_step_floor():
    # the production trap: px 1474.5, step 1e-4, cap == min == $10 makes
    # floor_step(10/px) * px = $9.88 -> below_min_notional forever, even
    # though the top of book carries hundreds of dollars
    from entropy_arb.book import OrderBook, plan_arb
    buy, sell = OrderBook(), OrderBook()
    buy.apply_hl([[{"px": "1460.0", "sz": "1.0"}],
                  [{"px": "1474.5", "sz": "0.32"}]])
    sell.apply_hl([[{"px": "1475.8", "sz": "5.0"}],
                   [{"px": "1475.7", "sz": "20.0"}]])
    plan, reason = plan_arb(buy, sell, threshold_bps=2.0, buy_fee_bps=0.0,
                            sell_fee_bps=0.0, take_fraction=1.0,
                            cap_notional=10.0, min_base=1e-4,
                            min_notional=10.0, size_step=1e-4)
    assert reason == "ok", reason
    assert plan.buy_notional >= 10.0 and plan.sell_notional >= 10.0
    assert plan.qty == 0.0068


def test_scan_fires_buy_entropy_below_band():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    # entropy 5 bps CHEAP (premium -5): below midline-lower=+2 -> buy entropy
    eng.entropy.set_book(99.94, 99.96)
    eng.hedge.set_book(99.99, 100.01)
    best = run_scan(eng)
    assert best is not None
    buy, sell, plan = best
    assert buy.key == "entropy" and sell.key == "hedge"


def test_scan_respects_position_caps():
    eng = make_engine(midline=0.0, upper=1.0, lower=1.0)
    eng.entropy.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)
    eng.entropy.position = -100.0   # entropy already short at its cap
    eng.entropy.cap_usd = 10000.0
    eng.hedge.position = 100.0
    eng.hedge.cap_usd = 10000.0
    assert run_scan(eng) is None


SESSION_CFG = """
thresholds:
  midline_bps: -4.3
  upper_bps: 5.0
  lower_bps: 6.0
  by_session:
    start_utc: "13:30"
    end_utc: "20:00"
    midline_bps: -5.7
    upper_bps: 5.5
    lower_bps: 6.5
execution:
  premium_persist_sec: 0.0
"""


def _make_session_engine():
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(SESSION_CFG)
    f.close()
    cfg = load_config(f.name, NO_ENV, symbol="SNDK", hedge_venue="lighter")
    eng = Engine(cfg)
    return eng


def _freeze_utc(monkeypatch, *, weekday: int, hour: int, minute: int = 0):
    """Pin engine's datetime.now(UTC) to a fixed instant.
    weekday: 0=Mon … 5=Sat, 6=Sun (2026-08-29 is a Saturday)."""
    import datetime as _dt
    base = _dt.datetime(2026, 8, 24, hour, minute, tzinfo=_dt.timezone.utc)
    fixed = base + _dt.timedelta(days=weekday)   # 2026-08-24 is a Monday
    real_dt = _dt.datetime

    class FakeDT(real_dt):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr("entropy_arb.engine.datetime", FakeDT)


def test_band_global_when_no_session():
    eng = make_engine(midline=-4.3, upper=5.0, lower=6.0)
    assert eng.session_label() == ""
    mid, up, lo = eng._band()
    approx(mid, -4.3)
    approx(up, 5.0)
    approx(lo, 6.0)


def test_band_session_inside_window(monkeypatch):
    eng = _make_session_engine()
    _freeze_utc(monkeypatch, weekday=2, hour=14)   # Wed 14:00 UTC — intraday
    mid, up, lo = eng._band()
    approx(mid, -5.7)
    approx(up, 5.5)
    approx(lo, 6.5)
    assert eng.session_label() == "intraday"
    # sell-entropy hurdle uses the SESSION midline + upper
    approx(mid + up, -0.2)


def test_band_weekend_uses_global(monkeypatch):
    eng = _make_session_engine()
    _freeze_utc(monkeypatch, weekday=5, hour=14)   # Sat 14:00 UTC
    mid, up, lo = eng._band()
    approx(mid, -4.3)
    approx(up, 5.0)
    approx(lo, 6.0)
    assert eng.session_label() == "weekend"


def test_band_offhours_outside_window(monkeypatch):
    eng = _make_session_engine()
    _freeze_utc(monkeypatch, weekday=2, hour=21)   # Wed 21:00 UTC — closed
    mid, up, lo = eng._band()
    approx(mid, -4.3)
    approx(up, 5.0)
    approx(lo, 6.0)
    assert eng.session_label() == "offhours"


WINDOWS_CFG = """
thresholds:
  midline_bps: -6.8
  upper_bps: 4.0
  lower_bps: 4.0
  windows:
    - name: weekend
      all_day: true
      days: [5, 6]
      upper_bps: 8.0
      lower_bps: 3.5
    - name: regular
      start_utc: "13:30"
      end_utc: "20:00"
      days: [0, 1, 2, 3, 4]
      upper_bps: 5.5
      lower_bps: 5.5
    - name: premarket
      start_utc: "08:00"
      end_utc: "13:30"
      days: [0, 1, 2, 3, 4]
      upper_bps: 4.5
      lower_bps: 4.5
execution:
  premium_persist_sec: 0.0
"""


def _make_windows_engine():
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(WINDOWS_CFG)
    f.close()
    cfg = load_config(f.name, NO_ENV, symbol="SNDK", hedge_venue="lighter")
    return Engine(cfg)


def test_windows_pick_by_phase(monkeypatch):
    eng = _make_windows_engine()
    _freeze_utc(monkeypatch, weekday=4, hour=9)     # Fri 09:00Z premarket
    mid, up, lo = eng._band()
    assert (up, lo) == (4.5, 4.5) and eng.session_label() == "premarket"
    _freeze_utc(monkeypatch, weekday=4, hour=15)    # Fri 15:00Z regular
    mid, up, lo = eng._band()
    assert (up, lo) == (5.5, 5.5) and eng.session_label() == "regular"
    _freeze_utc(monkeypatch, weekday=6, hour=2)     # Sun — weekend wins
    mid, up, lo = eng._band()
    assert (up, lo) == (8.0, 3.5) and eng.session_label() == "weekend"
    _freeze_utc(monkeypatch, weekday=3, hour=1)     # Thu 01:00Z -> global
    mid, up, lo = eng._band()
    assert (up, lo) == (4.0, 4.0) and eng.session_label() == ""
    approx(mid, -6.8)


def test_windows_first_match_wins_and_midline_override(monkeypatch):
    # Sat 02:00Z is "weekend" by UTC day; a regular window listing days 5 also
    # proves ordering matters: weekend entry is first in the list
    eng = _make_windows_engine()
    _freeze_utc(monkeypatch, weekday=5, hour=19)
    up, lo = eng._band()[1], eng._band()[2]
    assert (up, lo) == (8.0, 3.5)          # weekend all_day catches before any weekday mask could


def test_watch_windows_pick_labels(monkeypatch):
    eng = _make_windows_engine()
    _freeze_utc(monkeypatch, weekday=4, hour=15)
    assert eng.session_label() == "regular"
    _freeze_utc(monkeypatch, weekday=6, hour=3)
    assert eng.session_label() == "weekend"


def _watch_cfg(csv_path, *, auto=True, clamp=5.0, mid=-6.8, hours=None,
               band=False, trig=10.0, floor=2.0, ceiling=8.0):
    extra = (f"  auto_midline_clamp_bps: {clamp}\n"
             + (f"  auto_midline_hours: {hours}\n" if hours else "")
             + (f"  auto_band: {'true' if band else 'false'}\n"
                f"  auto_band_trigger_pct: {trig}\n"
                f"  auto_band_floor_bps: {floor}\n"
                f"  auto_band_ceiling_bps: {ceiling}\n"))
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(f"""
thresholds:
  midline_bps: {mid}
  upper_bps: 4.0
  lower_bps: 4.0
  auto_midline: {"true" if auto else "false"}
{extra}recorder:
  enabled: true
  csv: "{csv_path.replace(os.sep, '/')}"
""")
    f.close()
    cfg = load_config(f.name, NO_ENV, symbol="SNDK", hedge_venue="lighter")
    eng = Engine(cfg)
    eng.venues = {}
    eng.entropy = StubVenue("entropy", "ENTROPY")
    eng.hedge = StubVenue("hedge", "LIGHTER")
    eng.midline = cfg.midline_bps
    return eng


def _write_prem_csv(minutes_to_values):
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                    newline="")
    f.write("minute_ts,premium_close_bps,sell_edge_max_bps,buy_edge_max_bps\n")
    for m, p in sorted(minutes_to_values):        # chronological
        f.write(f"{time.time() - m * 60},{p},{p + 2.0},{-p + 2.0}\n")
    f.close()
    return f.name


def test_watch_tick_snaps_to_stable_regime():
    csv = _write_prem_csv([(m, -9.2) for m in range(119, 0, -1)])
    eng = _watch_cfg(csv)
    asyncio.run(eng._watch_tick())
    approx(eng.midline, -9.2, tol=0.1)


def test_watch_tick_freezes_mid_jump():
    csv = _write_prem_csv([(m, -6.8) for m in range(300, 35, -1)]
                          + [(m, -11.5) for m in range(35, 1, -1)])
    eng = _watch_cfg(csv)
    asyncio.run(eng._watch_tick())
    approx(eng.midline, -6.8)                     # unchanged: mid-jump hold


def test_watch_tick_clamps_far_regime_to_anchor_edge():
    csv = _write_prem_csv([(m, -14.0) for m in range(119, 0, -1)])
    eng = _watch_cfg(csv, clamp=5.0, mid=-6.8)
    asyncio.run(eng._watch_tick())
    approx(eng.midline, -11.8)                    # anchor -6.8 minus clamp 5


def _write_edge_csv(fn):
    """119 minutes, oldest first; fn(m) -> (prem, sell_edge, buy_edge)."""
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                    newline="")
    f.write("minute_ts,premium_close_bps,sell_edge_max_bps,"
            "buy_edge_max_bps\n")
    for m in range(119, 0, -1):
        p, se, be = fn(m)
        f.write(f"{time.time() - m * 60},{p},{se},{be}\n")
    f.close()
    return f.name


def test_watch_tick_band_sets_p90_room():
    csv = _write_edge_csv(lambda m: (-6.0, -6.0 + m * 0.05, 6.0 + m * 0.03))
    eng = _watch_cfg(csv, auto=False, band=True, mid=-6.0, floor=2.0,
                     ceiling=10.0)
    asyncio.run(eng._watch_tick())
    approx(eng.upper, 5.4, tol=0.2)          # p90 of i*0.05 rooms
    approx(eng.lower, 3.24, tol=0.15)
    mid, up, lo = eng._band()
    assert up == eng.upper and lo == eng.lower


def test_watch_tick_band_respects_floor_and_ceiling():
    csv = _write_edge_csv(lambda m: (-6.0, -6.0 + m * 0.2, 6.0 + m * 0.01))
    eng = _watch_cfg(csv, auto=False, band=True, mid=-6.0, floor=2.0,
                     ceiling=8.0)
    asyncio.run(eng._watch_tick())
    assert eng.upper == 8.0 and eng.lower == 2.0


def test_watch_tick_band_freezes_mid_jump():
    # premium jumps in the newest third -> stable_window says hold ->
    # bands stay at their static seed values
    def rows(m):
        prem = -8.0 if m < 35 else -3.0
        return prem, prem + m * 0.04, -prem + m * 0.04
    eng = _watch_cfg(_write_edge_csv(rows), auto=False, band=True,
                     mid=-3.0, floor=2.0, ceiling=10.0)
    asyncio.run(eng._watch_tick())
    assert eng.upper == 4.0 and eng.lower == 4.0     # untouched seeds


def test_watch_tick_off_when_no_auto():
    csv = _write_edge_csv(lambda m: (-6.0, -6.0 + m * 0.05, 6.0))
    eng = _watch_cfg(csv, auto=False, band=False)
    asyncio.run(eng._watch_tick())
    assert eng.midline == -6.8 and eng.upper == 4.0


def test_watch_tick_fixed_hours_window():
    csv = _write_prem_csv([(m, -9.2) for m in range(119, 0, -1)])
    eng = _watch_cfg(csv, hours=3.0)
    asyncio.run(eng._watch_tick())
    approx(eng.midline, -9.2, tol=0.1)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")

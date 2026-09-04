"""Dashboard rendering: key numbers appear, no crashes on empty state.

Run:  python3 -m pytest tests/  (or  python3 tests/test_dashboard.py)
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rich.console import Console  # noqa: E402

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.config import load_config  # noqa: E402
from entropy_arb.dashboard import BufferLogHandler, Dashboard  # noqa: E402
from entropy_arb.engine import Engine  # noqa: E402

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


def make_cfg():
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write("""
thresholds:
  midline_bps: 2.0
  upper_bps: 4.0
  lower_bps: 3.0
""")
    f.close()
    return load_config(f.name, NO_ENV,
                       symbol="SNDK", hedge_venue="lighter-rh")


class StubVenue:
    def __init__(self, key, label):
        self.key, self.name = key, label
        self.cap_usd, self.fee_bps = 1000.0, 0.0
        self.size_decimals, self.min_base, self.min_quote = 4, 1e-4, 10.0
        self.position, self.cash, self.volume_usd = 0.0, 0.0, 0.0
        self.equity = self.free = self.start_equity = None
        self.orders_per_min = 30
        self.last_traded_ts = 0.0
        self.book = OrderBook()

    def set_book(self, bid, ask):
        self.book.apply_hl([[{"px": str(bid), "sz": "10"}],
                            [{"px": str(ask), "sz": "10"}]])


def render(eng, lang="en") -> str:
    dash = Dashboard(eng, BufferLogHandler(), "logs/engine.log", lang=lang)
    console = Console(record=True, width=120, force_terminal=True)
    console.print(dash._safe_render())
    return console.export_text()


def make_engine():
    eng = Engine(make_cfg())
    eng.entropy = StubVenue("entropy", "ENTROPY")
    eng.hedge = StubVenue("hedge", "RH")
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    eng.markets_ready = True
    return eng


def test_renders_before_markets_resolve():
    eng = Engine(make_cfg())
    out = render(eng)
    assert "resolving markets" in out


def test_renders_key_numbers():
    eng = make_engine()
    eng.entropy.set_book(100.14, 100.16)   # ~+15 bps rich vs hedge
    eng.hedge.set_book(99.99, 100.01)
    # regression: a set last_trade_ts renders the "{s}s ago" cell — this
    # once collided with _t()'s own parameter name and crashed every frame
    eng.last_trade_ts = time.time() - 42
    eng.entropy.position, eng.hedge.position = 0.5, -0.5
    eng.entropy.equity, eng.entropy.start_equity = 1000.0, 990.0
    eng.hedge.equity, eng.hedge.start_equity = 500.0, 500.0
    eng.trades, eng.hedges = 7, 1
    eng.recent_trades.append({
        "ts": time.time(), "direction": "sell_entropy", "qty": 0.5,
        "notional": 50.0, "prem_bps": 15.0, "exp": 0.07, "fill": 0.05,
        "status": "filled/filled", "ok": True})
    out = render(eng)
    for needle in ("ENTROPY", "RH", "SELL entropy", "BUY entropy",
                   "100.14", "99.99", "mid premium", "midline",
                   "7 / 1", "sell_entropy", "filled/filled",
                   "$+10.00", "LIVE", "s ago"):
        assert needle in out, f"{needle!r} missing from render"
    assert "render error" not in out
    # signal math: sell hurdle = midline+upper = +6 (zero fees, flat books)
    assert "+6.00" in out
    # buy hurdle = lower - midline = +1
    assert "+1.00" in out


def test_renders_in_chinese():
    eng = make_engine()
    eng.entropy.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)
    eng.trades, eng.hedges = 7, 1
    eng.last_trade_ts = time.time() - 42
    out = render(eng, lang="zh")
    for needle in ("实盘", "运行中", "交易所", "买一 / 卖一", "持仓", "会话",
                   "盈亏 (MTM)", "净敞口", "中间价溢价", "中枢", "区间",
                   "卖出 entropy → 买入 RH", "买入 entropy → 卖出 RH",
                   "门槛 bps", "暂无执行", "日志事件", "秒前"):
        assert needle in out, f"{needle!r} missing from zh render"
    # numbers unchanged by translation: sell hurdle midline+upper = +6
    assert "+6.00" in out
    # English render untouched by the zh table
    out_en = render(eng, lang="en")
    assert "session" in out_en and "会话" not in out_en


def test_zh_stop_summary():
    eng = make_engine()
    eng.trades, eng.hedges = 3, 1
    dash = Dashboard(eng, BufferLogHandler(), "logs/engine.log", lang="zh")
    assert dash._t(" — {t} trades / {h} hedges, session PnL ",
                   t=3, h=1) == " —— 执行 3 / 对冲 1，会话盈亏 "
    assert dash._t("no such key stays english") == "no such key stays english"


def test_renders_record_only_and_empty_books():
    eng = Engine(make_cfg(), record_only=True)
    eng.entropy = StubVenue("entropy", "ENTROPY")
    eng.hedge = StubVenue("hedge", "MAIN")
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    eng.markets_ready = True
    out = render(eng)                      # books empty: everything is "—"
    assert "RECORD-ONLY" in out
    assert "render error" not in out


def test_maker_rows_with_none_exp_do_not_crash():
    # 09-04 live: every maker-ladder row logs exp=None (a rest has no
    # planned-edge $) — the trades footer sum blew up on float + None and
    # the whole dashboard red-panelled for the rest of the session
    eng = make_engine()
    eng.entropy.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)
    eng.recent_trades.append({
        "ts": time.time(), "direction": "buy_entropy/maker", "qty": 0.0626,
        "notional": 99.0, "prem_bps": 19.0, "exp": None, "fill": None,
        "status": "canceled/ioc_no_fill", "ok": False})
    eng.recent_trades.append({
        "ts": time.time(), "direction": "sell_entropy", "qty": 0.5,
        "notional": 50.0, "prem_bps": 15.0, "exp": 0.07, "fill": 0.05,
        "status": "filled/filled", "ok": True})
    out = render(eng)
    assert "render error" not in out
    assert "buy_entropy/maker" in out
    # the None row shows as em-dashes, the real row still totals
    assert "canceled/ioc_no_fill" in out


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")

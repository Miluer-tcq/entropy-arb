"""Rich terminal dashboard (English by default, Chinese with --cn).

While the bot runs on a terminal, log lines go to logging.file (and the
events panel); the screen shows live state: both venues with equity, the
premium against both entry hurdles, positions and net delta, session PnL,
recorder progress, and the last executions. Disable with --no-dashboard
(plain console logs, for nohup/systemd).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Optional

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

log = logging.getLogger("dashboard")

EVENT_LINES = 10
TRADE_ROWS = 10

LEVEL_STYLE = {
    logging.DEBUG: "dim",
    logging.INFO: "dim cyan",
    logging.WARNING: "yellow",
    logging.ERROR: "bold red",
    logging.CRITICAL: "bold white on red",
}

# UI strings, keyed by the English text (templates keep {placeholders}).
# Anything missing from the table falls back to English.
_ZH = {
    "starting — resolving markets…": "启动中 —— 正在解析市场…",
    " LIVE ": " 实盘 ",
    " RECORD-ONLY ": " 仅采集 ",
    " HALTED ": " 已停机 ",
    " VENUE DOWN ": " 交易所故障 ",
    " {n} STALE ": " {n} 路行情超时 ",
    " RATE-LTD ": " 限频中 ",
    " RECORDING ": " 采集中 ",
    " RUNNING ": " 运行中 ",
    "  up {t}": "  运行 {t}",
    "venues": "交易所",
    "  ·  session volume ${v}": "  ·  本次成交额 ${v}",
    "venue": "交易所",
    "bid / ask": "买一 / 卖一",
    "spr bps": "点差 bps",
    "age": "数据龄",
    "position": "持仓",
    "volume": "成交额",
    "equity": "权益",
    "free": "可用",
    " DOWN": " 故障",
    " LTD": " 限频",
    "STALE": "超时",
    "session": "会话",
    "PnL (MTM)": "盈亏 (MTM)",
    "account Δ": "账户权益变动",
    "Σ equity": "总权益",
    "Σ exp edge": "累计预期收益",
    "Σ fill edge": "累计实际收益",
    "trades / hedges": "执行 / 对冲",
    "net delta": "净敞口",
    "errors": "连续错误",
    "last exec": "上次执行",
    "minute rows": "分钟数据行数",
    "{s}s ago": "{s} 秒前",
    "signal — executable premium vs full hurdle incl. fees (● = armed)":
        "信号 —— 可成交溢价 vs 完整门槛（含手续费，● = 已武装）",
    "mid premium ": "中间价溢价 ",
    "   midline ": "   中枢 ",
    "   band ": "   区间 ",
    "band": "带宽",
    "   session ": "   时段 ",
    "intraday": "盘中",
    "offhours": "盘外",
    "weekend": "周末休市",
    "trading session": "交易时段",
    "all-day": "全天",
    "Σ edge (exp/fill)": "Σ 收益 (预期/实际)",
    "SELL entropy → buy {h}": "卖出 entropy → 买入 {h}",
    "BUY entropy → sell {h}": "买入 entropy → 卖出 {h}",
    "direction": "方向",
    "exec prem bps": "可成交溢价 bps",
    "hurdle bps": "门槛 bps",
    "gap bps": "差距 bps",
    "last {n} executions (net of fees)": "最近 {n} 笔执行（已扣手续费）",
    "time": "时间",
    "qty": "数量",
    "notional": "名义金额",
    "prem bps": "溢价 bps",
    "expected $": "预期 $",
    "actual $": "实际 $",
    "status": "状态",
    "Σ last {n}": "Σ 最近 {n} 笔",
    "no executions yet": "暂无执行",
    "events (full log: {f})": "日志事件（完整日志：{f}）",
    "watch": "监控",
    "midline drift (1h)": "中枢偏移 (1h)",
    "n={n}": "样本={n}",
    "entropy-arb stopped": "entropy-arb 已停止",
    " — {t} trades / {h} hedges, session PnL ": " —— 执行 {t} / 对冲 {h}，会话盈亏 ",
    ", Σ fill edge ": "，累计实际收益 ",
    ", {n} minute rows recorded": "，已记录 {n} 行分钟数据",
    " — full log: {f}": " —— 完整日志：{f}",
}


class BufferLogHandler(logging.Handler):
    """Ring buffer of recent log lines for the events panel."""

    def __init__(self, maxlen: int = 200) -> None:
        super().__init__()
        self.lines: deque = deque(maxlen=maxlen)
        self.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            return
        if "[status]" in msg:
            return  # the dashboard already shows everything the status line says
        self.lines.append((record.levelno, msg))


def _usd(x: Optional[float], signed: bool = True, decimals: int = 4) -> Text:
    if x is None:
        return Text("—", style="dim")
    style = "bold green" if x > 0 else ("bold red" if x < 0 else "")
    if signed:
        return Text(f"${x:+,.{decimals}f}", style=style)
    return Text(f"${x:,.{decimals}f}")


class Dashboard:
    def __init__(self, eng, log_buffer: BufferLogHandler, log_file: str,
                 force_terminal: bool = False, lang: str = "en") -> None:
        self.eng = eng
        self.log_buffer = log_buffer
        self.log_file = log_file
        self.lang = lang
        self.console = Console(force_terminal=True if force_terminal else None)

    def _t(self, s: str, /, **kw) -> str:
        """Translate a UI string (English key -> current language), then
        fill in any {placeholders}. The key is positional-only so that
        placeholder names (e.g. {s}) can never collide with it."""
        if self.lang == "zh":
            s = _ZH.get(s, s)
        return s.format(**kw) if kw else s

    async def run(self) -> None:
        eng = self.eng
        with Live(self._safe_render(), console=self.console,
                  refresh_per_second=8, screen=True) as live:
            while not eng.stop.is_set():
                live.update(self._safe_render())
                try:
                    await asyncio.wait_for(eng.stop.wait(), timeout=0.25)
                except asyncio.TimeoutError:
                    pass
        t = Text()
        t.append(self._t("entropy-arb stopped"), style="bold")
        t.append(self._t(" — {t} trades / {h} hedges, session PnL ",
                         t=eng.trades, h=eng.hedges))
        t.append_text(_usd(eng.session_pnl()))
        t.append(self._t(", Σ fill edge "))
        t.append_text(_usd(eng.total_fill_edge))
        if eng.recorder is not None:
            t.append(self._t(", {n} minute rows recorded",
                             n=eng.recorder.rows_written))
        t.append(self._t(" — full log: {f}", f=self.log_file))
        self.console.print(t)

    def _safe_render(self):
        try:
            return self._render()
        except Exception as e:
            log.exception("dashboard render failed")
            return Panel(Text(f"render error: {e!r}\nsee log file"), style="red")

    # -------------------------------------------------------------- renderer

    def _render(self):
        eng = self.eng
        if eng.entropy is None or eng.hedge is None or not eng.markets_ready:
            return Group(Panel(Text(self._t("starting — resolving markets…"),
                                    style="yellow"), title="entropy-arb",
                                box=box.ROUNDED), self._events_panel(6))
        if self.console.width >= 100:
            mid = Table.grid(expand=True)
            mid.add_column(ratio=5)
            mid.add_column(ratio=3)
            mid.add_row(self._venues_panel(), self._session_panel())
        else:
            mid = Group(self._venues_panel(), self._session_panel())
        # Fill the terminal exactly: measure the fixed panels, give the
        # trades panel what fits, and hand every remaining row to the
        # events panel so the TUI never leaves a blank bottom strip.
        # Non-terminal consoles (tests, export) have no usable height.
        if not self.console.is_terminal:
            return Group(self._header(), mid, self._signal_panel(),
                         self._trades_panel(),
                         self._events_panel(EVENT_LINES))
        total = self.console.size.height or 45
        fixed = self._measure(self._header(), mid, self._signal_panel())
        avail = max(total - fixed, 6)    # rows for trades + events panels
        # fill: events content rows = avail − trades panel − its 2 borders
        # (_events_panel pads its content to exactly the requested rows)
        trades = self._trades_panel()
        t_h = self._measure(trades)
        if avail - t_h - 2 >= 5:
            return Group(self._header(), mid, self._signal_panel(), trades,
                         self._events_panel(avail - t_h - 2))
        # short terminal: shrink the trades panel to what leaves ≥5 event
        # rows; if nothing fits, drop it (executions also live in the
        # events log and the trades CSV)
        for rows in range(TRADE_ROWS, 2, -1):
            t = self._trades_panel(rows)
            h = self._measure(t)
            if avail - h - 2 >= 5:
                return Group(self._header(), mid, self._signal_panel(), t,
                             self._events_panel(avail - h - 2))
        return Group(self._header(), mid, self._signal_panel(),
                     self._events_panel(max(avail - 2, 4)))

    def _measure(self, *renderables) -> int:
        """Rendered row count of the given panels at current console size.
        Options must NOT carry a height — panels expand vertically to a
        set height, which would break the measurement."""
        n = 0
        try:
            for r in renderables:
                n += len(self.console.render_lines(
                    r, self.console.options, pad=False, new_lines=False))
        except Exception:
            return 0
        return n

    def _header(self):
        eng, cfg = self.eng, self.eng.cfg
        now = time.time()
        mode = Text(self._t(" RECORD-ONLY "), style="black on yellow") \
            if eng.record_only \
            else Text(self._t(" LIVE "), style="white on dark_green")
        stale = sum(1 for v in eng.venues.values()
                    if not v.book.is_fresh(cfg.staleness_sec))
        limited = sum(1 for v in eng.venues.values() if eng._venue_limited(v))
        if eng.halted:
            state = Text(self._t(" HALTED "), style="bold white on red")
        elif eng._venue_down:
            state = Text(self._t(" VENUE DOWN "), style="bold white on red")
        elif stale:
            state = Text(self._t(" {n} STALE ", n=stale),
                         style="black on yellow")
        elif limited:
            state = Text(self._t(" RATE-LTD "), style="black on yellow")
        elif eng.record_only:
            state = Text(self._t(" RECORDING "), style="bold white on green")
        else:
            state = Text(self._t(" RUNNING "), style="bold white on green")
        up = int(now - eng.start_ts)
        g = Table.grid(expand=True)
        g.add_column(justify="left")
        g.add_column(justify="right")
        left = Text.assemble(("entropy-arb  ", "bold"),
                             (f"{cfg.symbol} × ENTROPY · {eng.hedge.name}",
                              "bold cyan"))
        right = Text()
        right.append_text(mode)
        right.append("  ")
        right.append_text(state)
        right.append(self._t("  up {t}",
                             t=f"{up // 3600}:{up % 3600 // 60:02d}"
                               f":{up % 60:02d}"), style="dim")
        g.add_row(left, right)
        return Panel(g, box=box.ROUNDED, padding=(0, 1))

    def _venues_panel(self):
        eng, cfg = self.eng, self.eng.cfg
        now = time.time()
        t = Table(box=box.SIMPLE_HEAD, padding=(0, 1))
        for col, j in (("venue", "left"), ("bid / ask", "right"),
                       ("spr bps", "right"), ("age", "right"),
                       ("position", "right"), ("volume", "right"),
                       ("equity", "right"), ("free", "right")):
            t.add_column(self._t(col), justify=j, no_wrap=True)
        vol_total = 0.0
        for v in eng.venues.values():
            bb, ba, m = v.book.best_bid(), v.book.best_ask(), v.book.mid()
            fresh = v.book.is_fresh(cfg.staleness_sec)
            name = Text(v.name, style="bold")
            if v.key in eng._venue_down:
                name.append(self._t(" DOWN"), style="bold white on red")
            elif eng._venue_limited(v):
                name.append(self._t(" LTD"), style="bold yellow")
            age = (Text(f"{now - v.book.last_update_ts:.1f}s", style="dim")
                   if v.book.ready else Text("—", style="dim"))
            if not fresh:
                age = Text(self._t("STALE"), style="bold red")
            pos = Text(f"{v.position:+.6g}",
                       style="green" if v.position > 0
                       else ("red" if v.position < 0 else "dim"))
            if m is not None and v.position:
                pos.append(f" · ${abs(v.position) * m:,.0f}", style="dim")
            vol = v.volume_usd
            vol_total += vol
            t.add_row(name,
                      f"{bb:,.6g} / {ba:,.6g}" if (bb and ba) else "—",
                      f"{(ba / bb - 1) * 1e4:.1f}" if (bb and ba) else "—",
                      age, pos,
                      Text(f"${vol:,.0f}") if vol else Text("—", style="dim"),
                      _usd(v.equity, signed=False, decimals=2),
                      _usd(v.free, signed=False, decimals=2))
        title = self._t("venues")
        if vol_total:
            title += self._t("  ·  session volume ${v}", v=f"{vol_total:,.0f}")
        return Panel(t, title=title, box=box.ROUNDED, padding=(0, 1))

    def _session_panel(self):
        eng, cfg = self.eng, self.eng.cfg
        net = sum(v.position for v in eng.venues.values())
        last = (self._t("{s}s ago", s=f"{time.time() - eng.last_trade_ts:.0f}")
                if eng.last_trade_ts else "—")
        # active trading session + the band it drives (compact one-liner)
        sess = eng.session_label()
        sess_label = self._t(sess) if sess else self._t("all-day")
        mid, up, lo = eng._band()
        drift, n = eng.drift_1h.get("drift"), eng.drift_1h.get("n", 0)
        d_style = ("bold red" if drift is not None and abs(drift) >= 2.0
                   else ("yellow" if drift is not None and abs(drift) >= 1.0
                         else "green"))
        drift_cell = (Text(f"{drift:+.2f}", style=d_style) if drift is not None
                      else Text("—", style="dim"))
        g = Table.grid(padding=(0, 2))
        g.add_column(justify="left", style="dim", no_wrap=True)
        g.add_column(justify="left", no_wrap=True)
        g.add_row(self._t("trading session"),
                  Text.assemble(
                      f"{sess_label}  ",
                      (f"{self._t('band')} +{up:.1f}/−{lo:.1f}", "bold"),
                      ("  →  ", "dim"),
                      (f"[{mid - lo:+.1f} … {mid + up:+.1f}]", "cyan")))
        g.add_row(self._t("midline drift (1h)"),
                  Text.assemble(drift_cell, (f" n={n}", "dim")))
        g.add_row(self._t("PnL (MTM)"), _usd(eng.session_pnl()))
        g.add_row(self._t("account Δ"), _usd(eng.account_delta()))
        eqs = [v.equity for v in eng.venues.values()]
        g.add_row(self._t("Σ equity"),
                  _usd(sum(eqs) if all(e is not None for e in eqs) else None,
                       signed=False, decimals=2))
        g.add_row(self._t("Σ edge (exp/fill)"),
                  Text.assemble(_usd(eng.total_exp_edge), (" / ", "dim"),
                                _usd(eng.total_fill_edge)))
        g.add_row(self._t("trades / hedges"),
                  Text.assemble(f"{eng.trades} / {eng.hedges}",
                                ("  ·  ", "dim"), (last, "dim")))
        g.add_row(self._t("net delta"),
                  Text(f"{net:+.6g}",
                       style="bold red"
                       if abs(net) > cfg.net_tolerance_base else "dim"))
        g.add_row(self._t("errors"), Text(str(eng.consec_errors),
                  style="bold red" if eng.consec_errors else "dim"))
        return Panel(g, title=self._t("session"), box=box.ROUNDED,
                     padding=(0, 1))

    def _dir_row(self, t: Table, label: str, buy, sell, hurdle_bps: float,
                 armed_key: str) -> None:
        """One direction: executable premium vs its full hurdle (fees and
        inventory surcharge included)."""
        eng = self.eng
        ba, sb = buy.book.best_ask(), sell.book.best_bid()
        hurdle = (hurdle_bps + buy.fee_bps + sell.fee_bps
                  + eng._inv_add_bps(buy, sell))
        if not (ba and sb):
            t.add_row(label, Text("—", style="dim"),
                      f"{hurdle:+.1f}", Text("—", style="dim"), "")
            return
        prem = (sb / ba - 1) * 1e4
        gap = prem - hurdle
        armed = Text("●", style="green") if eng._armed.get(armed_key) else ""
        t.add_row(label,
                  Text(f"{prem:+.2f}", style="bold green" if gap >= 0 else ""),
                  f"{hurdle:+.2f}",
                  Text(f"{gap:+.2f}", style="green" if gap >= 0 else "dim"),
                  armed)

    def _signal_panel(self):
        eng, cfg = self.eng, self.eng.cfg
        prem = eng.premium_bps()
        mid, up, lo = eng._band()
        sess = eng.session_label()
        sess_zh = {"intraday": "盘中", "offhours": "盘外", "weekend": "周末休市"}
        head = Text()
        head.append(self._t("mid premium "), style="dim")
        head.append(f"{prem:+.2f} bps" if prem is not None else "—",
                    style="bold cyan")
        head.append(self._t("   midline "), style="dim")
        head.append(f"{mid:+.2f}")
        head.append(self._t("   band "), style="dim")
        head.append(f"[{mid - lo:+.2f} … {mid + up:+.2f}]")
        if sess:
            head.append(self._t("   session "), style="dim")
            head.append(self._t(sess))
        t = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 1))
        t.add_column(self._t("direction"))
        t.add_column(self._t("exec prem bps"), justify="right")
        t.add_column(self._t("hurdle bps"), justify="right")
        t.add_column(self._t("gap bps"), justify="right")
        t.add_column("", justify="left")
        self._dir_row(t, self._t("SELL entropy → buy {h}", h=eng.hedge.name),
                      eng.hedge, eng.entropy, mid + up, "sell_entropy")
        self._dir_row(t, self._t("BUY entropy → sell {h}", h=eng.hedge.name),
                      eng.entropy, eng.hedge, lo - mid, "buy_entropy")
        return Panel(Group(head, t),
                     title=self._t("signal — executable premium vs full "
                                   "hurdle incl. fees (● = armed)"),
                     box=box.ROUNDED, padding=(0, 1))

    def _trades_panel(self, max_rows: int = TRADE_ROWS):
        eng = self.eng
        rows = list(eng.recent_trades)[-max_rows:]
        exp_sum = sum(r["exp"] for r in rows)
        fills = [r["fill"] for r in rows if r["fill"] is not None]
        t = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 1),
                  show_footer=bool(rows))
        t.add_column(self._t("time"),
                     footer=Text(self._t("Σ last {n}", n=len(rows)),
                                 style="dim"))
        t.add_column(self._t("direction"))
        t.add_column(self._t("qty"), justify="right")
        t.add_column(self._t("notional"), justify="right")
        t.add_column(self._t("prem bps"), justify="right")
        t.add_column(self._t("expected $"), justify="right", footer=_usd(exp_sum))
        t.add_column(self._t("actual $"), justify="right",
                     footer=_usd(sum(fills) if fills else None))
        t.add_column(self._t("status"))
        for r in reversed(rows):
            style = "green" if r["ok"] else "bold red"
            t.add_row(time.strftime("%H:%M:%S", time.localtime(r["ts"])),
                      r["direction"], f"{r['qty']:.6g}",
                      f"${r['notional']:,.0f}", f"{r['prem_bps']:+.1f}",
                      _usd(r["exp"]), _usd(r["fill"]),
                      Text(r["status"], style=style))
        if not rows:
            t.add_row(Text(self._t("no executions yet"), style="dim"),
                      "", "", "", "", "", "", "")
        return Panel(t, title=self._t("last {n} executions (net of fees)",
                                      n=max_rows),
                     box=box.ROUNDED, padding=(0, 1))

    def _events_panel(self, max_rows: int = EVENT_LINES):
        """Show the newest log lines within an exact visual-row budget so
        the panel (and therefore the whole TUI) fills the terminal bottom
        without blank space. Long lines wrap; CJK double-width is counted."""
        width = max((self.console.size.width or 120) - 4, 10)
        body = Text()
        picked = []
        used = 0
        for lvl, msg in reversed(self.log_buffer.lines):
            rows = self._msg_rows(msg, width)
            if picked and used + rows > max_rows:
                break
            picked.append((lvl, msg))
            used += rows
            if used >= max_rows:
                break
        if not picked:
            body.append("—", style="dim")
        for i, (lvl, msg) in enumerate(reversed(picked)):
            if i:
                body.append("\n")
            body.append(msg, style=LEVEL_STYLE.get(lvl, ""))
        if picked and used < max_rows:
            # pad with blank lines so the panel is EXACTLY max_rows visual
            # rows and the TUI fills the terminal bottom with no gaps
            body.append("\n" * (max_rows - used))
        return Panel(body, title=self._t("events (full log: {f})",
                                         f=self.log_file),
                     box=box.ROUNDED, padding=(0, 1))

    @staticmethod
    def _msg_rows(msg: str, width: int) -> int:
        """Visual rows a message occupies once folded at `width` cells."""
        from rich.cells import cell_len
        total = 0
        for seg in str(msg).split("\n"):
            w = cell_len(seg)
            total += max(1, -(-w // width))
        return max(total, 1)

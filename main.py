#!/usr/bin/env python3
"""entropy-arb entry point.

    # collect minute data only — no strategy, no credentials needed
    python3 main.py --record-only --symbol SNDK --hedge lighter-rh

    # LIVE trading: real orders, real money (needs .env credentials)
    python3 main.py --symbol SNDK --hedge lighter-rh

--symbol and --hedge are required on every start: the markets you trade are
an explicit decision, not a config default. Add --cn for a Chinese-language
dashboard. There is no paper mode. Collect data with --record-only, set
your thresholds with tools/analyze.py, then go live with small position
caps.

On a terminal the bot shows a live Rich dashboard (books, signal, positions,
PnL, last executions) and writes log lines to logging.file; use
--no-dashboard for plain console logs (nohup/systemd). Strategy lives in
config.yaml, credentials in .env — see the README (English) /
README.zh-CN.md (中文).
"""
import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys

from entropy_arb.config import HEDGE_VENUES, ConfigError, load_config
from entropy_arb.engine import Engine


class InstanceLock:
    """One process per config file: a second start with the same --config
    would double-write the CSVs and race the same signing keys."""

    def __init__(self, config_file: str) -> None:
        stem = os.path.splitext(os.path.basename(config_file))[0]
        self.path = os.path.join("logs", f".lock-{stem}.pid")
        self.acquired = False

    def acquire(self) -> None:
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                pid = self._existing_pid()
                if pid is not None and _pid_alive(pid):
                    print(f"another instance is running with this config "
                          f"(PID {pid}) — refuse to start. If that is wrong, "
                          f"delete {self.path}", file=sys.stderr)
                    sys.exit(2)
                try:
                    os.remove(self.path)   # stale lock from a crashed run
                except OSError:
                    pass
                continue
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            self.acquired = True
            return
        print("could not acquire the instance lock — giving up",
              file=sys.stderr)
        sys.exit(2)

    def _existing_pid(self) -> int | None:
        try:
            with open(self.path) as fh:
                return int(fh.read().strip() or 0)
        except (OSError, ValueError):
            return None

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            os.remove(self.path)
        except OSError:
            pass
        self.acquired = False


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))
        if h:   # PROCESS_QUERY_LIMITED_INFORMATION
            k.CloseHandle(h)
            return True
        return False
    try:
        os.kill(pid, 0)   # POSIX existence probe
        return True
    except OSError:
        return False


def setup_logging(level: str, log_file: str = None,
                  extra_handler: logging.Handler = None) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S")
    if log_file:
        d = os.path.dirname(log_file)
        if d:
            os.makedirs(d, exist_ok=True)
        h = logging.FileHandler(log_file)
    else:
        h = logging.StreamHandler()
    h.setFormatter(fmt)
    root.addHandler(h)
    if extra_handler is not None:
        root.addHandler(extra_handler)
    logging.getLogger("websockets").setLevel(logging.WARNING)


async def amain(cfg, record_only: bool, use_dashboard: bool, force_tty: bool,
                log_buffer, lang: str) -> None:
    eng = Engine(cfg, record_only=record_only)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, eng.request_stop)
        except NotImplementedError:
            pass
    if not use_dashboard:
        await eng.run()
        return
    from entropy_arb.dashboard import Dashboard
    dash = Dashboard(eng, log_buffer, cfg.log_file, force_terminal=force_tty,
                     lang=lang)
    dash_task = asyncio.create_task(dash.run(), name="dashboard")
    try:
        await eng.run()
    finally:
        eng.request_stop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(dash_task, timeout=5)
        if not dash_task.done():
            dash_task.cancel()


def main() -> None:
    p = argparse.ArgumentParser(
        description="Two-venue LIVE arbitrage: Entropy vs Lighter mainnet / "
                    "Lighter Robinhood / trade.xyz. Without --record-only, "
                    "real orders are sent.")
    p.add_argument("--symbol", required=True,
                   help="symbol traded on both venues, e.g. SNDK / "
                        "两个交易所共同交易的品种")
    p.add_argument("--hedge", required=True, choices=HEDGE_VENUES,
                   metavar="VENUE",
                   help=f"hedge venue, one of: {', '.join(HEDGE_VENUES)} / "
                        f"对冲腿，三选一")
    p.add_argument("--config", default="config.yaml",
                   help="strategy config (default: config.yaml)")
    p.add_argument("--env-file", default=".env",
                   help="credentials file (default: .env)")
    p.add_argument("--record-only", action="store_true",
                   help="only collect minute data, run no strategy, send no "
                        "orders (needs no credentials)")
    p.add_argument("--preflight", action="store_true",
                   help="run startup checks (env format, SDK imports, "
                        "markets, margin vs caps, data freshness, midline "
                        "drift) and exit / 上线预检，输出红绿清单后退出")
    p.add_argument("--cn", action="store_true",
                   help="display the dashboard in Chinese / 仪表盘使用中文")
    disp = p.add_mutually_exclusive_group()
    disp.add_argument("--dashboard", action="store_true",
                      help="force the Rich dashboard even without a tty")
    disp.add_argument("--no-dashboard", action="store_true",
                      help="plain console logs instead of the dashboard")
    args = p.parse_args()

    try:
        cfg = load_config(args.config, args.env_file,
                          symbol=args.symbol, hedge_venue=args.hedge)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)

    if args.preflight:
        from entropy_arb.preflight import run_preflight
        ok = asyncio.run(run_preflight(cfg))
        sys.exit(0 if ok else 1)

    lock = InstanceLock(args.config)
    lock.acquire()

    use_dashboard = (cfg.dashboard or args.dashboard) and not args.no_dashboard
    force_tty = args.dashboard
    if use_dashboard and not (sys.stdout.isatty() or force_tty):
        use_dashboard = False

    log_buffer = None
    if use_dashboard:
        try:
            from entropy_arb.dashboard import BufferLogHandler
        except ImportError:
            print("`rich` is not installed — falling back to plain logs "
                  "(pip install -r requirements.txt)", file=sys.stderr)
            use_dashboard = False
    if use_dashboard:
        log_buffer = BufferLogHandler()
        setup_logging(cfg.log_level, log_file=cfg.log_file,
                      extra_handler=log_buffer)
    else:
        setup_logging(cfg.log_level)

    try:
        asyncio.run(amain(cfg, record_only=args.record_only,
                          use_dashboard=use_dashboard, force_tty=force_tty,
                          log_buffer=log_buffer,
                          lang="zh" if args.cn else "en"))
    except RuntimeError as e:
        # startup failures (missing credentials, market not found, venue
        # unreachable) — a clean message, not a traceback
        print(f"startup error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    finally:
        lock.release()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Analyze recorded minute data and suggest config.yaml thresholds.

Reads the CSV written by the built-in recorder (logs/minutes.csv by default)
and prints:

  * the premium distribution (midline candidates),
  * how often each candidate upper/lower band would have fired,
  * a ready-to-paste `thresholds:` snippet.

分析机器人自动采集的分钟级盘口数据，输出溢价分布、各档阈值的触发频率，
以及可直接粘贴进 config.yaml 的 thresholds 建议值。

Usage:
    python3 tools/analyze.py                    # logs/minutes.csv
    python3 tools/analyze.py --csv path.csv --hours 24 --min-samples 10
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")   # DST-correct US equity hours
CANDIDATES = [1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 15.0, 20.0]


def pctl(sorted_vals: list, q: float) -> float:
    """Linear-interpolated percentile of a pre-sorted list, q in [0, 100]."""
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * q / 100.0
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


def load_rows(path: str, hours: float) -> list:
    cutoff = time.time() - hours * 3600 if hours > 0 else 0.0
    rows = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                if float(r["minute_ts"]) < cutoff:
                    continue
                rows.append({
                    "ts": float(r["minute_ts"]),
                    "samples": int(r["samples"]),
                    "prem": float(r["premium_close_bps"]),
                    "prem_mean": float(r["premium_mean_bps"]),
                    "sell_max": float(r["sell_edge_max_bps"]),
                    "buy_max": float(r["buy_edge_max_bps"]),
                })
            except (KeyError, ValueError):
                continue
    return rows


def is_us_session(ts: float) -> bool:
    """US equity cash hours 09:30-16:00 America/New_York (DST-correct)."""
    dt = datetime.fromtimestamp(ts, tz=_ET)
    if dt.weekday() >= 5:
        return False
    mins = dt.hour * 60 + dt.minute
    return 9 * 60 + 30 <= mins < 16 * 60


def _stats_block(label: str, subset: list, fees: float, midline: float) -> None:
    if len(subset) < 10:
        print(f"  {label}: <10 minutes, skipped")
        return
    prem = sorted(r["prem"] for r in subset)
    med = pctl(prem, 50)
    mean = sum(prem) / len(prem)
    var = sum((x - mean) ** 2 for x in prem) / len(prem)
    s90 = pctl(sorted(r["sell_max"] - midline - fees for r in subset), 90)
    b90 = pctl(sorted(r["buy_max"] + midline - fees for r in subset), 90)
    print(f"  {label}: n={len(subset):>5}  median {med:+.2f}  "
          f"std {math.sqrt(var):.2f}  | p90 room: sell {max(s90, 0.0):.1f} / "
          f"buy {max(b90, 0.0):.1f} bps")


def main() -> None:
    p = argparse.ArgumentParser(description="suggest thresholds from recorded "
                                            "minute data")
    p.add_argument("--csv", default="logs/minutes.csv")
    p.add_argument("--hours", type=float, default=0.0,
                   help="only use the last N hours (0 = all data)")
    p.add_argument("--min-samples", type=int, default=10,
                   help="skip minutes with fewer fresh samples than this")
    p.add_argument("--fees-bps", type=float, default=0.0,
                   help="SUM of both venues' taker fees in bps (each crossing "
                        "pays both legs); recorded edges are pre-fee, so this "
                        "is subtracted before counting firings (default 0.0 — "
                        "pass ~1.0 with a tradexyz hedge)")
    args = p.parse_args()

    try:
        all_rows = load_rows(args.csv, args.hours)
    except FileNotFoundError:
        print(f"{args.csv} not found — run the bot (even --record-only) to "
              f"collect data first / 未找到数据文件，请先运行机器人采集数据",
              file=sys.stderr)
        sys.exit(1)
    rows = [r for r in all_rows if r["samples"] >= args.min_samples]
    if len(rows) < 30:
        print(f"only {len(rows)} usable minute(s) in {args.csv} — collect at "
              f"least a few hours before trusting the numbers / 数据太少，"
              f"建议至少采集数小时", file=sys.stderr)
        if not rows:
            sys.exit(1)
    rows.sort(key=lambda r: r["ts"])   # spans/buckets assume time order

    span_h = (rows[-1]["ts"] - rows[0]["ts"]) / 3600.0 + 1 / 60.0
    prem = sorted(r["prem"] for r in rows)
    mean = sum(prem) / len(prem)
    var = sum((x - mean) ** 2 for x in prem) / len(prem)
    median = pctl(prem, 50)

    print(f"\n=== {args.csv}: {len(rows)} usable minutes over {span_h:.1f}h "
          f"===\n")
    print("premium of Entropy over hedge, minute close (bps) / "
          "Entropy 相对对冲腿的溢价:")
    print(f"  mean {mean:+.2f}   std {math.sqrt(var):.2f}   "
          f"median {median:+.2f}")
    print(f"  p5 {pctl(prem, 5):+.2f}   p25 {pctl(prem, 25):+.2f}   "
          f"p75 {pctl(prem, 75):+.2f}   p95 {pctl(prem, 95):+.2f}")

    # ---- coverage (v2): how complete is the recording
    n = len(all_rows)
    cov30 = sum(1 for r in all_rows if r["samples"] >= 30) / n * 100 if n else 0
    full = sum(1 for r in all_rows if r["samples"] == 60) / n * 100 if n else 0
    print(f"\ncoverage / 采集完整度: >=30s {cov30:.0f}%   full-minute "
          f"{full:.0f}%   (skipped {n - len(rows)} low-sample minutes)")

    midline = round(median, 1) or 0.0   # normalize -0.0
    # room beyond the midline that was actually executable each minute, net
    # of taker fees (config thresholds are net-of-fee: the engine adds fees
    # on top, and recorded edges are pre-fee)
    fees = args.fees_bps
    sell_room = sorted((r["sell_max"] - midline - fees for r in rows),
                       reverse=True)
    buy_room = sorted((r["buy_max"] + midline - fees for r in rows),
                      reverse=True)

    # ---- drift table (v2): 6h buckets
    print("\npremium median per 6h window / 每6小时溢价中枢:")
    buckets: dict = {}
    for r in rows:
        buckets.setdefault(int(r["ts"] // 21600), []).append(r["prem"])
    for b in sorted(buckets)[-12:]:
        vals = sorted(buckets[b])
        t0 = datetime.fromtimestamp(b * 21600, tz=timezone.utc)
        print(f"  {t0:%m-%d %H}:00Z  median {pctl(vals, 50):+.2f}  "
              f"n={len(vals)}")

    # ---- session split (v2): US equity hours vs off-hours
    print("\nby session / 分时段（美股现金时段 13:30-20:00 UTC 周一至周五）:")
    _stats_block("US hours ", [r for r in rows if is_us_session(r["ts"])],
                 fees, midline)
    _stats_block("off-hours", [r for r in rows if not is_us_session(r["ts"])],
                 fees, midline)

    print(f"\nwith midline_bps = {midline:+.1f} (median) and {fees:.1f} bps "
          f"round-trip taker fees, minutes each band would have fired / "
          f"各档净阈值触发的分钟数:")
    print(f"  {'band bps':>9} | {'SELL entropy':>17} | {'BUY entropy':>17}")
    print(f"  {'':>9} | {'minutes':>8} {'per day':>8} | "
          f"{'minutes':>8} {'per day':>8}")
    per_day = 24.0 / span_h if span_h > 0 else 0.0
    for t in CANDIDATES:
        s_hits = sum(1 for x in sell_room if x >= t)
        b_hits = sum(1 for x in buy_room if x >= t)
        print(f"  {t:>9.1f} | {s_hits:>8} {s_hits * per_day:>8.1f} | "
              f"{b_hits:>8} {b_hits * per_day:>8.1f}")

    # default suggestion: the band that fired in ~10% of minutes (p90 of the
    # fee-adjusted executable room), floored at 1 bps — tune from the table
    sug_upper = max(round(pctl(sorted(sell_room), 90) * 2) / 2, 1.0)
    sug_lower = max(round(pctl(sorted(buy_room), 90) * 2) / 2, 1.0)
    print(f"""
suggested starting point (fires ~10% of minutes, already net of the
{fees:.1f} bps fees passed via --fees-bps; a full round trip nets
>= upper+lower bps after fees) /
建议起点（约 10% 的分钟触发；已扣除 --fees-bps 传入的 {fees:.1f} bps 手续费，
一次完整往返扣费后净赚 >= upper+lower bps）:

thresholds:
  midline_bps: {midline}
  upper_bps: {sug_upper}
  lower_bps: {sug_lower}

Re-run with --hours to focus on recent regimes; premiums drift, so refresh
these numbers regularly. / 溢价中枢会漂移，请定期重新分析并更新配置。
""")


if __name__ == "__main__":
    main()

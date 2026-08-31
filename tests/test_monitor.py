"""Monitor utilities: median, CSV series reader, drift report.

Run:  python -m pytest tests/  (or  python tests/test_monitor.py)
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.monitor import (drift_report, last_row_age_sec,  # noqa: E402
                                 median, read_premium_series,
                                 regime_drift_report)

HEADER = "minute_ts,premium_close_bps\n"


def write_csv(rows) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                    newline="")
    f.write(HEADER)
    for ts, px in rows:
        f.write(f"{ts},{px}\n")
    f.close()
    return f.name


def test_median():
    assert median([]) is None
    assert median([3.0]) == 3.0
    assert median([1.0, 2.0]) == 1.5
    assert median([3.0, 1.0, 2.0]) == 2.0


def test_read_series_window_and_drift():
    now = time.time()
    # boundary row parked well outside the 1h window (an exact-edge row
    # would race the clock between the test and the reader)
    p = write_csv([(now - 3700, 5.0), (now - 60, 4.0), (now - 30, 6.0)])
    xs = read_premium_series(p, hours=1.0)
    assert xs == [4.0, 6.0]
    rep = drift_report(p, midline_bps=3.0, hours=1.0, min_samples=2)
    assert rep["n"] == 2 and rep["median"] == 5.0 and rep["drift"] == 2.0


def test_drift_not_enough_samples():
    now = time.time()
    p = write_csv([(now - 60, 4.0)])
    rep = drift_report(p, 0.0, hours=1.0, min_samples=30)
    assert rep == {"median": None, "drift": None, "n": 1}


def test_missing_file_is_empty():
    assert read_premium_series("no-such.csv", 1.0) == []
    assert last_row_age_sec("no-such.csv") is None


def test_regime_drift_tracks_current_not_blend():
    # 40 old-regime rows (-2.5) at 8-20h ago (inside 24h, outside 6h) plus 30
    # new-regime rows (-6.4) at 5-40min ago. The 24h blend sits in nobody's
    # market; a config set to the NEW regime must not be flagged as drifted.
    now = time.time()
    rows = [(now - a, -2.5) for a in range(8 * 3600, 8 * 3600 + 40 * 1080, 1080)]
    rows += [(now - a, -6.4) for a in range(300, 300 + 30 * 72, 72)]
    assert len(rows) == 70 and sum(r[1] == -2.5 for r in rows) == 40
    p = write_csv(rows)
    rep = regime_drift_report(p, midline_bps=-6.4)
    assert rep["window_hours"] == 6.0 and rep["recent_enough"]
    assert abs(rep["drift"]) < 0.5            # recent regime == config
    assert rep["shifted"] is True             # 24h blend disagrees
    assert abs(rep["base_drift"]) >= 3.0


def test_regime_drift_falls_back_without_fresh_data():
    # only old-regime data, nothing in the recent window -> can't confirm a
    # new regime, so fall back to the 24h median and signal not-enough
    now = time.time()
    rows = [(now - a, -2.5) for a in range(8 * 3600, 8 * 3600 + 40 * 1080, 1080)]
    p = write_csv(rows)
    rep = regime_drift_report(p, midline_bps=-2.5)
    assert rep["window_hours"] == 24.0 and not rep["recent_enough"]
    assert abs(rep["drift"]) < 0.5



def test_bad_rows_skipped():
    now = time.time()
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                    newline="")
    f.write(HEADER)
    f.write(f"{now - 30},not-a-number\n")
    f.write(f"not-a-ts,7.5\n")
    f.write(f"{now - 10},7.5\n")
    f.close()
    age = last_row_age_sec(f.name)
    assert age is not None and 5 <= age < 60
    assert read_premium_series(f.name, hours=1.0) == [7.5]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")

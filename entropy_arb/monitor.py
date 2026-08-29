"""Read-only monitors shared by the engine's watch loop and --preflight.

The drift monitor reads the recorder's minute CSV (premium_close_bps column)
and reports a rolling median so the operator can see when the configured
midline has drifted away from where the premium actually sits. Nothing here
places orders or mutates state.
"""
from __future__ import annotations

import csv
import os
import time
from typing import List, Optional


def median(xs: List[float]) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def read_premium_series(csv_path: str, hours: float) -> List[float]:
    """premium_close_bps values from the recorder CSV within the last
    `hours` wall-clock. Missing/unreadable file yields []."""
    try:
        if not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0:
            return []
        cutoff = time.time() - hours * 3600.0
        out: List[float] = []
        with open(csv_path, newline="") as fh:
            for row in csv.DictReader(fh):
                try:
                    ts = float(row["minute_ts"])
                    if ts >= cutoff:
                        out.append(float(row["premium_close_bps"]))
                except (KeyError, TypeError, ValueError):
                    continue
        return out
    except OSError:
        return []


def last_row_age_sec(csv_path: str) -> Optional[float]:
    """Age of the newest minute row in seconds; None if no data."""
    try:
        if not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0:
            return None
        last_ts: Optional[float] = None
        with open(csv_path, newline="") as fh:
            for row in csv.DictReader(fh):
                try:
                    last_ts = float(row["minute_ts"])
                except (KeyError, TypeError, ValueError):
                    continue
        return None if last_ts is None else time.time() - last_ts
    except OSError:
        return None


def drift_report(csv_path: str, midline_bps: float, hours: float = 24.0,
                 min_samples: int = 30) -> dict:
    """{"median", "drift", "n"} — drift = rolling median - configured
    midline. n = 0 when there is not enough recent data."""
    xs = read_premium_series(csv_path, hours)
    if len(xs) < min_samples:
        return {"median": None, "drift": None, "n": len(xs)}
    med = median(xs)
    return {"median": med, "drift": med - midline_bps, "n": len(xs)}

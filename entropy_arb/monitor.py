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


def regime_drift_report(csv_path: str, midline_bps: float,
                        recent_hours: float = 6.0, base_hours: float = 24.0,
                        min_samples: int = 20) -> dict:
    """Drift measured against the *current* regime, not a stale blend.

    A single 24h rolling median is a blend of whatever regimes happened to
    fall inside the window: when the premium jumps (e.g. -2.8 -> -6.4) the
    blended median sits in nobody's market for up to a day, so a config
    correctly updated to the new regime would be flagged as drifted. To
    avoid that, the gate tracks a short recent window and only reports a
    mismatch against the base window as an informational `shifted` flag.

    Returns {median, drift, n, window_hours} describing the chosen regime
    window, plus base_*/recent_* detail and `recent_enough`/`shifted`."""
    recent = drift_report(csv_path, midline_bps, recent_hours,
                          min_samples=min_samples)
    base = drift_report(csv_path, midline_bps, base_hours)
    if recent["drift"] is not None:
        med, drift, n, window = (recent["median"], recent["drift"],
                                 recent["n"], recent_hours)
    else:
        med, drift, n, window = (base["median"], base["drift"],
                                 base["n"], base_hours)
    shifted = (recent["median"] is not None and base["median"] is not None
               and abs(recent["median"] - base["median"]) >= 3.0)
    return {"median": med, "drift": drift, "n": n, "window_hours": window,
            "recent_median": recent["median"], "recent_drift": recent["drift"],
            "recent_n": recent["n"], "recent_enough": recent["drift"] is not None,
            "base_median": base["median"], "base_drift": base["drift"],
            "base_n": base["n"], "shifted": shifted}


def auto_midline_target(csv_path: str,
                        candidates=(2.0, 4.0, 6.0, 12.0, 24.0),
                        min_samples: int = 20,
                        stability_bps: float = 0.8):
    """(median, window_hours) of the current stable regime, or (None, None)
    while mid-shift.

    Walk windows widening from shortest; the first candidate that carries
    enough SAMPLES is the verdict window:
      - internally stable (its chronological thirds agree) -> that median is
        the settled regime, use it (this is what snaps the midline across a
        day after a jump, once >= a full short window is the new regime);
      - a third diverges -> we are mid-jump, FROZE (return None) so auto-
        tuning never chases a blended or diluted median. Longer windows are
        deliberately NOT consulted here — they merely dilute the fresh jump
        into an older third and would falsely look stable.
    Wider windows are only used when a narrower one lacks samples (recorder
    restart / data gap), so a fresh boot still tunes from a longer tail.
    """
    for hours in candidates:
        xs = read_premium_series(csv_path, hours)
        if len(xs) < max(min_samples, 18):
            continue                        # not enough DATA — widen
        n3 = len(xs) // 3
        t1, t2, t3 = (median(xs[:n3]), median(xs[n3:2 * n3]),
                      median(xs[2 * n3:]))
        if t1 is None or t2 is None or t3 is None:
            continue
        if abs(t2 - t1) > stability_bps or abs(t3 - t2) > stability_bps:
            return None, None               # enough data, but mid-jump: hold
        return median(xs), hours            # settled on this regime
    return None, None

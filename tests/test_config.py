"""Config loading: example file, validation, CLI-selected markets.

Run:  python3 -m pytest tests/  (or  python3 tests/test_config.py)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.config import ConfigError, load_config  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
EXAMPLE = os.path.join(ROOT, "config.example.yaml")
NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


def write_tmp(text: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(text)
    f.close()
    return f.name


MINIMAL = """
thresholds:
  midline_bps: 5.0
  upper_bps: 4.0
  lower_bps: 3.0
"""


def load(yaml_text: str, symbol="SNDK", hedge="lighter-rh"):
    return load_config(write_tmp(yaml_text), NO_ENV,
                       symbol=symbol, hedge_venue=hedge)


def test_example_config_loads():
    cfg = load_config(EXAMPLE, NO_ENV,
                      symbol="SNDK", hedge_venue="lighter-rh")
    assert cfg.symbol == "SNDK"
    assert cfg.entropy.kind == "hl" and cfg.entropy.hl_dex == "io"
    assert cfg.hedge_venue == "lighter-rh"
    assert cfg.hedge.kind == "lighter"
    assert cfg.hedge.lighter_profile.chain_id == 466324
    assert cfg.entropy.symbol == "SNDK" and cfg.hedge.symbol == "SNDK"
    assert cfg.recorder_enabled and cfg.recorder_csv
    assert cfg.dashboard and cfg.log_file


def test_minimal_defaults():
    cfg = load(MINIMAL, hedge="lighter")
    assert cfg.midline_bps == 5.0 and cfg.upper_bps == 4.0 and cfg.lower_bps == 3.0
    assert cfg.hedge.label == "LIGHTER"
    assert cfg.hedge.lighter_profile.chain_id == 304
    assert cfg.take_fraction == 0.5          # defaults kick in
    assert cfg.recorder_enabled is True


def test_tradexyz_hedge():
    cfg = load(MINIMAL, hedge="tradexyz")
    assert cfg.hedge.kind == "hl" and cfg.hedge.hl_dex == "xyz"
    assert cfg.hedge.label == "XYZ"


def expect_error(yaml_text: str, needle: str, **kw):
    try:
        load(yaml_text, **kw)
    except ConfigError as e:
        assert needle in str(e), f"{needle!r} not in {e}"
        return
    raise AssertionError(f"expected ConfigError containing {needle!r}")


def test_unknown_key_rejected():
    expect_error(MINIMAL + "\nthresholdz:\n  x: 1\n",
                 "unknown config key 'thresholdz'")
    expect_error(MINIMAL + "\nsizing:\n  take_fractionn: 0.5\n",
                 "sizing.take_fractionn")


def test_markets_no_longer_config_keys():
    # symbol / hedge_venue moved to --symbol / --hedge: leftovers in the
    # YAML must fail loudly, not silently override the flags
    expect_error("symbol: SNDK\n" + MINIMAL, "unknown config key 'symbol'")
    expect_error("hedge_venue: tradexyz\n" + MINIMAL,
                 "unknown config key 'hedge_venue'")


def test_bad_cli_markets():
    expect_error(MINIMAL, "--hedge", hedge="binance")
    expect_error(MINIMAL, "--symbol", symbol="")


def test_missing_thresholds():
    expect_error("recorder:\n  enabled: true\n", "thresholds.")


def test_nonpositive_band():
    expect_error("thresholds:\n"
                 "  midline_bps: 5\n  upper_bps: 0\n  lower_bps: 3\n",
                 "must be > 0")


SESSION = """
thresholds:
  midline_bps: -4.6
  upper_bps: 5.0
  lower_bps: 6.0
  auto_midline: true
  by_session:
    start_utc: "13:30"
    end_utc: "20:00"
    midline_bps: -5.7
    upper_bps: 7.0
    lower_bps: 7.0
"""


def test_session_and_auto_midline_defaults():
    cfg = load(MINIMAL, hedge="lighter")
    assert cfg.auto_midline is False
    assert cfg.session_upper_bps is None and cfg.session_lower_bps is None
    assert cfg.session_midline_bps is None


def test_session_and_auto_midline_load():
    cfg = load(SESSION, hedge="lighter")
    assert cfg.auto_midline is True
    assert cfg.session_upper_bps == 7.0 and cfg.session_lower_bps == 7.0
    assert cfg.session_midline_bps == -5.7
    assert cfg.session_start_utc == "13:30" and cfg.session_end_utc == "20:00"


def test_session_requires_all_keys():
    expect_error("""thresholds:
  midline_bps: -4.6
  upper_bps: 5.0
  lower_bps: 6.0
  by_session:
    start_utc: "13:30"
""", "'thresholds.by_session.end_utc' is required")


def test_session_bad_time_format():
    expect_error("""thresholds:
  midline_bps: -4.6
  upper_bps: 5.0
  lower_bps: 6.0
  by_session:
    start_utc: "9am"
    end_utc: "20:00"
    upper_bps: 7.0
    lower_bps: 7.0
""", "must be HH:MM UTC")


def test_session_nonpositive_band():
    expect_error("""thresholds:
  midline_bps: -4.6
  upper_bps: 5.0
  lower_bps: 6.0
  by_session:
    start_utc: "13:30"
    end_utc: "20:00"
    upper_bps: 0.0
    lower_bps: 7.0
""", "by_session upper_bps/lower_bps must be > 0")


def test_window_bad_days():
    expect_error("""thresholds:
  midline_bps: -4.6
  upper_bps: 5.0
  lower_bps: 6.0
  windows:
    - start_utc: "13:30"
      end_utc: "20:00"
      days: [1, 8]
      upper_bps: 7.0
      lower_bps: 7.0
""", ".days must be a non-empty list")


def test_window_start_eq_end_rejected():
    expect_error("""thresholds:
  midline_bps: -4.6
  upper_bps: 5.0
  lower_bps: 6.0
  windows:
    - start_utc: "13:30"
      end_utc: "13:30"
      upper_bps: 7.0
      lower_bps: 7.0
""", "start_utc == end_utc is ambiguous")


def test_window_unknown_key():
    expect_error("""thresholds:
  midline_bps: -4.6
  upper_bps: 5.0
  lower_bps: 6.0
  windows:
    - start_utc: "13:30"
      end_utc: "20:00"
      upper_bps: 7.0
      lower_bps: 7.0
      spop: 3
""", "unknown config key")


def test_windows_and_by_session_exclusive():
    expect_error("""thresholds:
  midline_bps: -4.6
  upper_bps: 5.0
  lower_bps: 6.0
  by_session:
    start_utc: "13:30"
    end_utc: "20:00"
    upper_bps: 7.0
    lower_bps: 7.0
  windows:
    - start_utc: "13:30"
      end_utc: "20:00"
      upper_bps: 7.0
      lower_bps: 7.0
""", "either 'windows' or legacy")


def test_all_day_window_valid():
    cfg = load("""thresholds:
  midline_bps: -4.6
  upper_bps: 5.0
  lower_bps: 6.0
  windows:
    - name: weekend
      all_day: true
      days: [5, 6]
      upper_bps: 8.0
      lower_bps: 3.5
""")
    assert len(cfg.windows) == 1 and cfg.windows[0].all_day
    assert cfg.windows[0].days == (5, 6) and cfg.windows[0].name == "weekend"


def test_auto_midline_tuning_keys():
    cfg = load("""thresholds:
  midline_bps: -4.6
  upper_bps: 5.0
  lower_bps: 6.0
  auto_midline: true
  auto_midline_clamp_bps: 5.0
  auto_midline_hours: 3.0
""")
    assert cfg.auto_midline is True
    assert cfg.auto_midline_clamp_bps == 5.0
    assert cfg.auto_midline_hours == 3.0


def test_auto_midline_tuning_defaults():
    cfg = load("""thresholds:
  midline_bps: -4.6
  upper_bps: 5.0
  lower_bps: 6.0
""")
    assert cfg.auto_midline_clamp_bps == 3.0
    assert cfg.auto_midline_hours is None          # adaptive


def test_auto_midline_clamp_must_be_positive():
    expect_error("""thresholds:
  midline_bps: -4.6
  upper_bps: 5.0
  lower_bps: 6.0
  auto_midline_clamp_bps: 0
""", "auto_midline_clamp_bps must be > 0")


def test_auto_midline_hours_must_be_positive():
    expect_error("""thresholds:
  midline_bps: -4.6
  upper_bps: 5.0
  lower_bps: 6.0
  auto_midline_hours: -1
""", "auto_midline_hours must be > 0")


def test_auto_band_defaults():
    cfg = load("""thresholds:
  midline_bps: -4.6
  upper_bps: 5.0
  lower_bps: 6.0
""")
    assert cfg.auto_band is False
    assert cfg.auto_band_trigger_pct == 10.0
    assert cfg.auto_band_floor_bps == 2.0
    assert cfg.auto_band_ceiling_bps == 8.0
    assert cfg.min_net_edge_bps == 0.0


def test_auto_band_keys_parse():
    cfg = load("""thresholds:
  midline_bps: -4.6
  upper_bps: 5.0
  lower_bps: 6.0
  auto_band: true
  auto_band_trigger_pct: 12.5
  auto_band_floor_bps: 2.5
  auto_band_ceiling_bps: 7.5
""")
    assert cfg.auto_band is True
    assert cfg.auto_band_trigger_pct == 12.5
    assert (cfg.auto_band_floor_bps, cfg.auto_band_ceiling_bps) == (2.5, 7.5)


def test_auto_band_trigger_range():
    expect_error("""thresholds:
  midline_bps: -4.6
  upper_bps: 5.0
  lower_bps: 6.0
  auto_band_trigger_pct: 60
""", "auto_band_trigger_pct must be in")


def test_auto_band_ceiling_below_floor():
    expect_error("""thresholds:
  midline_bps: -4.6
  upper_bps: 5.0
  lower_bps: 6.0
  auto_band_floor_bps: 5.0
  auto_band_ceiling_bps: 3.0
""", "auto_band_ceiling_bps >= floor")


def test_min_net_edge_bps_parse():
    cfg = load("""thresholds:
  midline_bps: -5.0
  upper_bps: 5.0
  lower_bps: 5.0
  min_net_edge_bps: 2.5
""")
    assert cfg.min_net_edge_bps == 2.5


def test_min_net_edge_bps_rejects_negative():
    expect_error("""thresholds:
  midline_bps: -5.0
  upper_bps: 5.0
  lower_bps: 5.0
  min_net_edge_bps: -1.0
""", "min_net_edge_bps must be >= 0")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")

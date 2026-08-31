"""Configuration: strategy from a YAML file, credentials from .env, market
selection (symbol + hedge venue) from the command line.

The split is deliberate: config.yaml IS the strategy (thresholds, sizing,
risk) and is safe to share/commit as an example; .env holds only secrets;
which markets to trade is stated explicitly on every start (--symbol,
--hedge). Every YAML key is validated against the schema below, so a typo
is an error rather than a setting that silently does nothing.

Threshold model (fixed numbers the user derives from recorded minute data):

    premium_bps = (entropy_price / hedge_price - 1) * 10_000

    SELL entropy / BUY hedge  fires when the executable premium
        (entropy bid over hedge ask) >= midline_bps + upper_bps
    BUY entropy / SELL hedge  fires when the executable premium
        (entropy ask under hedge bid) <= midline_bps - lower_bps

    Both hurdles are net of both venues' taker fees, so a full round trip
    nets >= (upper_bps + lower_bps) after fees by construction.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time as dtime
from typing import Any, Dict, List, Optional, Tuple

import yaml
from dotenv import load_dotenv

HL_API_URL = "https://api.hyperliquid.xyz"
HL_WS_URL = "wss://api.hyperliquid.xyz/ws"   # official ws — the only HL feed used

HEDGE_VENUES = ("lighter", "lighter-rh", "tradexyz")


@dataclass(frozen=True)
class LighterProfile:
    name: str
    api_url: str
    ws_url: str
    chain_id: int


# Endpoint profiles for the two supported zkLighter deployments (these match
# lighter-python's lighter.endpoint_profiles, duplicated here so --record-only
# data collection works without the SDK installed).
LIGHTER_PROFILES: Dict[str, LighterProfile] = {
    "lighter": LighterProfile(
        "mainnet", "https://mainnet.zklighter.elliot.ai",
        "wss://mainnet.zklighter.elliot.ai/stream", 304),
    "lighter-rh": LighterProfile(
        "robinhood", "https://api.rh.lighter.xyz",
        "wss://api.rh.lighter.xyz/stream", 466324),
}


@dataclass
class LighterCreds:
    account_index: Optional[int]
    api_key_index: Optional[int]
    api_private_key: Optional[str]

    @property
    def complete(self) -> bool:
        return (self.account_index is not None and self.api_key_index is not None
                and bool(self.api_private_key))


@dataclass
class HLCreds:
    private_key: Optional[str]
    account_address: Optional[str]

    @property
    def complete(self) -> bool:
        return bool(self.private_key)


@dataclass
class VenueConf:
    key: str                  # "entropy" | "hedge"
    kind: str                 # "hl" | "lighter"
    label: str                # human name for logs, e.g. "ENTROPY", "RH"
    symbol: str
    fee_bps: float
    fee_auto: bool            # exchange fee discovery on (default)
    cap_usd: float
    orders_per_min: int
    # hl
    hl_dex: str = ""
    hl_leverage: int = 1      # isolated leverage declared at startup (HL
                              # defaults isolated assets to 1x = full margin)
    hl_creds: Optional[HLCreds] = None
    # lighter
    lighter_profile: Optional[LighterProfile] = None
    lighter_creds: Optional[LighterCreds] = None


@dataclass(frozen=True)
class ThresholdWindow:
    """One time window with its own band. days uses python weekday
    (0=Mon .. 6=Sun); all_day ignores start/end. Times are UTC wall-clock,
    start==end means wrap past midnight when not all_day."""
    name: str
    start_utc: Optional[dtime]
    end_utc: Optional[dtime]
    days: Tuple[int, ...]
    upper_bps: float
    lower_bps: float
    midline_bps: Optional[float] = None
    all_day: bool = False


def window_contains(w: ThresholdWindow, now_utc) -> bool:
    if now_utc.weekday() not in w.days:
        return False
    if w.all_day:
        return True
    t = now_utc.time()
    if w.start_utc < w.end_utc:
        return w.start_utc <= t < w.end_utc
    return t >= w.start_utc or t < w.end_utc   # wraps midnight


@dataclass
class Config:
    symbol: str
    hedge_venue: str
    entropy: VenueConf
    hedge: VenueConf
    # thresholds (the whole signal)
    midline_bps: float
    upper_bps: float
    lower_bps: float
    # sizing
    take_fraction: float
    max_order_notional: float
    min_order_notional: float
    # inventory ladder
    inventory_scale_bps: float
    inventory_floor_frac: float
    # execution
    premium_persist_sec: float
    cooldown_sec: float
    settle_timeout_sec: float
    leg_slippage_bps: float
    hedge_slippage_bps: float
    net_tolerance_base: float
    max_consecutive_errors: int
    rate_limit_pause_sec: float
    staleness_sec: float
    reconcile_sec: float
    venue_probe_sec: float
    http_keepalive_sec: float
    # recorder
    recorder_enabled: bool
    recorder_csv: str
    # logging
    log_level: str
    status_interval_sec: float
    trades_csv: str
    dashboard: bool
    log_file: str
    # watch / auto-tuning (all opt-in; defaults keep upstream behaviour)
    auto_midline: bool = False
    auto_midline_clamp_bps: float = 3.0
    auto_midline_hours: Optional[float] = None   # None = adaptive window
    auto_band: bool = False
    auto_band_trigger_pct: float = 10.0
    auto_band_floor_bps: float = 2.0
    auto_band_ceiling_bps: float = 8.0
    auto_band: bool = False
    auto_band_trigger_pct: float = 10.0
    auto_band_floor_bps: float = 2.0
    auto_band_ceiling_bps: float = 8.0
    session_upper_bps: Optional[float] = None
    session_lower_bps: Optional[float] = None
    session_midline_bps: Optional[float] = None
    session_start_utc: Optional[str] = None
    session_end_utc: Optional[str] = None
    # multi-window session bands (US pre/regular/post/overnight/weekend ...):
    # first matching window wins; legacy by_session is converted to one
    windows: List["ThresholdWindow"] = field(default_factory=list)
    windows_from_session: bool = False
    # runtime
    hl_api_url: str = HL_API_URL
    hl_ws_url: str = HL_WS_URL

    @property
    def creds_complete(self) -> bool:
        for v in (self.entropy, self.hedge):
            if v.kind == "hl" and not (v.hl_creds and v.hl_creds.complete):
                return False
            if v.kind == "lighter" and not (v.lighter_creds
                                            and v.lighter_creds.complete):
                return False
        return True


# ----------------------------------------------------------------- YAML layer

# Schema: nested dict of key -> type (or nested dict). Unknown keys are errors.
_SCHEMA: Dict[str, Any] = {
    "thresholds": {
        "midline_bps": float,
        "upper_bps": float,
        "lower_bps": float,
        "auto_midline": bool,
        "auto_midline_clamp_bps": float,
        "auto_midline_hours": float,
        "auto_band": bool,
        "auto_band_trigger_pct": float,
        "auto_band_floor_bps": float,
        "auto_band_ceiling_bps": float,
        "windows": "list",
        "by_session": {
            "start_utc": str,
            "end_utc": str,
            "midline_bps": float,
            "upper_bps": float,
            "lower_bps": float,
        },
    },
    "entropy": {
        "dex": str,
        "taker_fee_bps": float,
        "fee_auto": bool,
        "max_position_usd": float,
        "max_orders_per_min": int,
        "leverage": int,
    },
    "hedge": {
        "taker_fee_bps": float,
        "fee_auto": bool,
        "max_position_usd": float,
        "max_orders_per_min": int,
        "leverage": int,
    },
    "sizing": {
        "take_fraction": float,
        "max_order_notional_usd": float,
        "min_order_notional_usd": float,
    },
    "inventory": {
        "scale_bps": float,
        "floor_frac": float,
    },
    "execution": {
        "premium_persist_sec": float,
        "cooldown_sec": float,
        "settle_timeout_sec": float,
        "leg_slippage_bps": float,
        "hedge_slippage_bps": float,
        "net_tolerance_base": float,
        "max_consecutive_errors": int,
        "rate_limit_pause_sec": float,
        "staleness_sec": float,
        "reconcile_sec": float,
        "venue_probe_sec": float,
        "http_keepalive_sec": float,
    },
    "recorder": {
        "enabled": bool,
        "csv": str,
    },
    "logging": {
        "level": str,
        "status_interval_sec": float,
        "trades_csv": str,
        "dashboard": bool,
        "file": str,
    },
}


class ConfigError(ValueError):
    pass


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys — a repeated
    `thresholds:` block silently overriding itself contradicts the
    'a typo is an error' contract."""

    def construct_mapping(self, node, deep=False):
        seen = set()
        for k_node, _ in node.value:
            key = self.construct_object(k_node, deep=deep)
            try:
                dup = key in seen
            except TypeError:
                dup = False
            if dup:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate config key {key!r}",
                    k_node.start_mark)
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _validate(node: Any, schema: Dict[str, Any], path: str = "") -> None:
    if not isinstance(node, dict):
        raise ConfigError(f"'{path or '<root>'}' must be a mapping")
    for key, val in node.items():
        here = f"{path}.{key}" if path else str(key)
        if key not in schema:
            raise ConfigError(f"unknown config key '{here}' "
                              f"(valid: {', '.join(sorted(schema))})")
        want = schema[key]
        if isinstance(want, dict):
            _validate(val, want, here)
        elif want is float:
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                raise ConfigError(f"'{here}' must be a number, got {val!r}")
        elif want is int:
            if not isinstance(val, int) or isinstance(val, bool):
                raise ConfigError(f"'{here}' must be an integer, got {val!r}")
        elif want is bool:
            if not isinstance(val, bool):
                raise ConfigError(f"'{here}' must be true/false, got {val!r}")
        elif want is str:
            if not isinstance(val, str):
                raise ConfigError(f"'{here}' must be a string, got {val!r}")
        elif want == "list":
            if not isinstance(val, list):
                raise ConfigError(f"'{here}' must be a list, got {val!r}")


def _get(d: dict, section: str, key: str, default):
    return (d.get(section) or {}).get(key, default)


def _parse_hhmm(hhmm: Any, path: str) -> dtime:
    parts = str(hhmm).split(":")
    if (len(parts) != 2 or not all(p.isdigit() and len(p) == 2 for p in parts)
            or not (0 <= int(parts[0]) <= 23) or not (0 <= int(parts[1]) <= 59)):
        raise ConfigError(f"{path} must be HH:MM UTC, got {hhmm!r}")
    return dtime(int(parts[0]), int(parts[1]))


_ALL_DAYS: Tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)


def _parse_window(wd: Any, path: str) -> ThresholdWindow:
    if not isinstance(wd, dict):
        raise ConfigError(f"{path} must be a mapping, got {wd!r}")
    allowed = {"name", "start_utc", "end_utc", "days", "all_day",
               "midline_bps", "upper_bps", "lower_bps"}
    for k in wd:
        if k not in allowed:
            raise ConfigError(f"unknown config key '{path}.{k}' "
                              f"(valid: {', '.join(sorted(allowed))})")
    for k in ("upper_bps", "lower_bps"):
        if k not in wd:
            raise ConfigError(f"'{path}.{k}' is required")
    up, lo = float(wd["upper_bps"]), float(wd["lower_bps"])
    if up <= 0 or lo <= 0:
        raise ConfigError(f"{path} upper_bps/lower_bps must be > 0")
    mid = (float(wd["midline_bps"])
           if wd.get("midline_bps") is not None else None)
    all_day = bool(wd.get("all_day", False))
    days = wd.get("days")
    if days is None:
        days_t = _ALL_DAYS
    else:
        if (not isinstance(days, list) or not days
                or not all(isinstance(d, int) and 0 <= d <= 6 for d in days)):
            raise ConfigError(f"{path}.days must be a non-empty list of "
                              f"integers 0(Mon)..6(Sun), got {days!r}")
        days_t = tuple(days)
    if all_day:
        start = end = None
    else:
        for k in ("start_utc", "end_utc"):
            if k not in wd:
                raise ConfigError(f"'{path}.{k}' is required unless all_day")
        start = _parse_hhmm(wd["start_utc"], f"{path}.start_utc")
        end = _parse_hhmm(wd["end_utc"], f"{path}.end_utc")
        if start == end:
            raise ConfigError(f"{path} start_utc == end_utc is ambiguous — "
                              f"use all_day: true for an all-day window")
    return ThresholdWindow(name=str(wd.get("name") or "window"),
                           start_utc=start, end_utc=end, days=days_t,
                           upper_bps=up, lower_bps=lo, midline_bps=mid,
                           all_day=all_day)


# ------------------------------------------------------------------ env layer

def _env_s(name: str) -> Optional[str]:
    v = os.getenv(name)
    return v.strip() if v not in (None, "") else None


def _env_i(name: str) -> Optional[int]:
    v = os.getenv(name)
    if v in (None, ""):
        return None
    try:
        return int(v)
    except ValueError:
        raise ConfigError(f"environment variable {name}={v!r} is not an "
                          f"integer / 环境变量必须是整数") from None


def _validated_log_level(level) -> str:
    lv = str(level).strip().upper()
    import logging
    if lv not in logging.getLevelNamesMapping():
        raise ConfigError(f"logging.level {level!r} is not a valid level "
                          f"(valid: DEBUG, INFO, WARNING, ERROR, CRITICAL) "
                          f"/ 日志级别无效")
    return lv


# -------------------------------------------------------------------- loading

def load_config(config_file: str = "config.yaml", env_file: str = ".env", *,
                symbol: str, hedge_venue: str) -> Config:
    load_dotenv(env_file)
    try:
        with open(config_file, encoding="utf-8-sig") as fh:
            raw = yaml.load(fh, Loader=_StrictLoader) or {}
    except FileNotFoundError:
        raise ConfigError(
            f"config file '{config_file}' not found — copy config.example.yaml "
            f"to config.yaml and edit it / 未找到配置文件，请先复制 "
            f"config.example.yaml 为 config.yaml 并修改")
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in '{config_file}': {e} / "
                          f"配置文件 YAML 语法错误") from e
    _validate(raw, _SCHEMA)

    symbol = (symbol or "").strip()
    if not symbol:
        raise ConfigError("--symbol is required, e.g. --symbol SNDK / "
                          "必须用 --symbol 指定交易品种")
    if hedge_venue not in HEDGE_VENUES:
        raise ConfigError(
            f"--hedge must be one of {list(HEDGE_VENUES)}, got "
            f"{hedge_venue!r} / --hedge 必须是 {list(HEDGE_VENUES)} 之一")

    thr = raw.get("thresholds") or {}
    for k in ("midline_bps", "upper_bps", "lower_bps"):
        if k not in thr:
            raise ConfigError(f"'thresholds.{k}' is required — derive it from "
                              f"recorded minute data / 必须填写，请用采集的分钟"
                              f"数据计算后填入")
    upper, lower = float(thr["upper_bps"]), float(thr["lower_bps"])
    if upper <= 0 or lower <= 0:
        raise ConfigError("thresholds.upper_bps and lower_bps must be > 0 "
                          "(the round trip nets upper+lower bps after fees)")

    auto_midline = bool(thr.get("auto_midline", False))
    auto_clamp = float(thr.get("auto_midline_clamp_bps", 3.0))
    if auto_clamp <= 0:
        raise ConfigError("thresholds.auto_midline_clamp_bps must be > 0")
    amh = thr.get("auto_midline_hours")
    auto_hours = float(amh) if amh is not None else None
    if auto_hours is not None and auto_hours <= 0:
        raise ConfigError("thresholds.auto_midline_hours must be > 0 "
                          "(omit it to let the window adapt itself)")
    auto_band = bool(thr.get("auto_band", False))
    abtp = float(thr.get("auto_band_trigger_pct", 10.0))
    if not 1.0 <= abtp <= 50.0:
        raise ConfigError("thresholds.auto_band_trigger_pct must be in "
                          "1..50 (share of minutes that should fire)")
    abf = float(thr.get("auto_band_floor_bps", 2.0))
    abc = float(thr.get("auto_band_ceiling_bps", 8.0))
    if abf <= 0 or abc < abf:
        raise ConfigError("thresholds: auto_band_floor_bps must be > 0 and "
                          "auto_band_ceiling_bps >= floor")
    sess = thr.get("by_session") or None
    sess_start = sess_end = None
    sess_upper = sess_lower = sess_midline = None
    if sess is not None:
        for k in ("start_utc", "end_utc", "upper_bps", "lower_bps"):
            if k not in sess:
                raise ConfigError(f"'thresholds.by_session.{k}' is required "
                                  f"when by_session is configured")
        sess_start, sess_end = sess["start_utc"], sess["end_utc"]
        sess_upper, sess_lower = float(sess["upper_bps"]), float(sess["lower_bps"])
        sess_midline = (float(sess["midline_bps"])
                        if sess.get("midline_bps") is not None else None)
        if sess_upper <= 0 or sess_lower <= 0:
            raise ConfigError("thresholds.by_session upper_bps/lower_bps must "
                              "be > 0")
        for name, hhmm in (("start_utc", sess_start), ("end_utc", sess_end)):
            parts = hhmm.split(":")
            if (len(parts) != 2 or not all(p.isdigit() and len(p) == 2
                                           for p in parts)
                    or not (0 <= int(parts[0]) <= 23)
                    or not (0 <= int(parts[1]) <= 59)):
                raise ConfigError(
                    f"thresholds.by_session.{name} must be HH:MM UTC, "
                    f"got {hhmm!r}")
        if sess_start == sess_end:
            raise ConfigError("thresholds.by_session start_utc == end_utc is "
                              "ambiguous (treated as all-day) — remove the "
                              "by_session block for one band around the "
                              "clock, or fix the times")

    win_raw = thr.get("windows")
    windows: List[ThresholdWindow] = []
    from_session = False
    if win_raw is not None and sess is not None:
        raise ConfigError("thresholds: configure either 'windows' or legacy "
                          "'by_session', not both / 二选一")
    if win_raw is not None:
        if not win_raw:
            raise ConfigError("thresholds.windows is empty — remove it to "
                              "use one global band")
        windows = [_parse_window(w, f"thresholds.windows[{i}]")
                   for i, w in enumerate(win_raw)]
    elif sess is not None:
        windows = [ThresholdWindow(
            name="intraday",
            start_utc=_parse_hhmm(sess_start, "thresholds.by_session"),
            end_utc=_parse_hhmm(sess_end, "thresholds.by_session"),
            days=(0, 1, 2, 3, 4),
            upper_bps=sess_upper, lower_bps=sess_lower,
            midline_bps=sess_midline)]
        from_session = True

    take_fraction = float(_get(raw, "sizing", "take_fraction", 0.5))
    if not 0.0 < take_fraction <= 1.0:
        raise ConfigError("sizing.take_fraction must be in (0, 1] — taking "
                          "more than the profitable depth loses money on the "
                          "tail / 必须在 (0, 1] 之间")

    max_ntl = float(_get(raw, "sizing", "max_order_notional_usd", 500.0))
    min_ntl = float(_get(raw, "sizing", "min_order_notional_usd", 10.0))
    if max_ntl < min_ntl:
        raise ConfigError(
            "sizing.max_order_notional_usd must be >= "
            "min_order_notional_usd — otherwise every slice is below the "
            "minimum and the bot never trades / 单笔上限不能小于下限")

    entropy_dex = _get(raw, "entropy", "dex", "io")
    if hedge_venue == "tradexyz" and entropy_dex == "xyz":
        raise ConfigError("entropy.dex 'xyz' with hedge_venue 'tradexyz' is "
                          "the same market on both legs / 两条腿是同一个市场")

    entropy_hl_creds = HLCreds(_env_s("HL_PRIVATE_KEY"),
                               _env_s("HL_ACCOUNT_ADDRESS"))
    entropy = VenueConf(
        key="entropy", kind="hl", label="ENTROPY",
        symbol=symbol,
        fee_bps=float(_get(raw, "entropy", "taker_fee_bps", 0.0)),
        fee_auto=bool(_get(raw, "entropy", "fee_auto", True)),
        cap_usd=float(_get(raw, "entropy", "max_position_usd", 1000.0)),
        orders_per_min=int(_get(raw, "entropy", "max_orders_per_min", 120)),
        hl_dex=entropy_dex,
        hl_leverage=int(_get(raw, "entropy", "leverage", 1)),
        hl_creds=entropy_hl_creds,
    )

    if hedge_venue == "tradexyz":
        hedge = VenueConf(
            key="hedge", kind="hl", label="XYZ",
            symbol=symbol,
            fee_bps=float(_get(raw, "hedge", "taker_fee_bps", 1.0)),
            fee_auto=bool(_get(raw, "hedge", "fee_auto", True)),
            cap_usd=float(_get(raw, "hedge", "max_position_usd", 1000.0)),
            orders_per_min=int(_get(raw, "hedge", "max_orders_per_min", 120)),
            hl_dex="xyz",
            hl_leverage=int(_get(raw, "hedge", "leverage", 1)),
            hl_creds=HLCreds(
                _env_s("HL_PRIVATE_KEY_XYZ") or _env_s("HL_PRIVATE_KEY"),
                _env_s("HL_ACCOUNT_ADDRESS_XYZ") or _env_s("HL_ACCOUNT_ADDRESS")),
        )
    else:
        hedge = VenueConf(
            key="hedge", kind="lighter",
            label="LIGHTER" if hedge_venue == "lighter" else "RH",
            symbol=symbol,
            fee_bps=float(_get(raw, "hedge", "taker_fee_bps", 0.0)),
            fee_auto=bool(_get(raw, "hedge", "fee_auto", True)),
            cap_usd=float(_get(raw, "hedge", "max_position_usd", 1000.0)),
            orders_per_min=int(_get(raw, "hedge", "max_orders_per_min", 30)),
            lighter_profile=LIGHTER_PROFILES[hedge_venue],
            lighter_creds=LighterCreds(_env_i("LIGHTER_ACCOUNT_INDEX"),
                                       _env_i("LIGHTER_API_KEY_INDEX"),
                                       _env_s("LIGHTER_API_PRIVATE_KEY")),
        )

    return Config(
        symbol=symbol,
        hedge_venue=hedge_venue,
        entropy=entropy,
        hedge=hedge,
        midline_bps=float(thr["midline_bps"]),
        upper_bps=upper,
        lower_bps=lower,
        auto_midline=auto_midline,
        auto_midline_clamp_bps=auto_clamp,
        auto_midline_hours=auto_hours,
        auto_band=auto_band,
        auto_band_trigger_pct=abtp,
        auto_band_floor_bps=abf,
        auto_band_ceiling_bps=abc,
        session_upper_bps=sess_upper,
        session_lower_bps=sess_lower,
        session_midline_bps=sess_midline,
        windows=windows,
        windows_from_session=from_session,
        session_start_utc=sess_start,
        session_end_utc=sess_end,
        take_fraction=take_fraction,
        max_order_notional=max_ntl,
        min_order_notional=min_ntl,
        inventory_scale_bps=float(_get(raw, "inventory", "scale_bps", 10.0)),
        inventory_floor_frac=float(_get(raw, "inventory", "floor_frac", 0.5)),
        premium_persist_sec=float(_get(raw, "execution", "premium_persist_sec", 0.3)),
        cooldown_sec=float(_get(raw, "execution", "cooldown_sec", 0.0)),
        settle_timeout_sec=float(_get(raw, "execution", "settle_timeout_sec", 5.0)),
        leg_slippage_bps=float(_get(raw, "execution", "leg_slippage_bps", 50.0)),
        hedge_slippage_bps=float(_get(raw, "execution", "hedge_slippage_bps", 20.0)),
        net_tolerance_base=float(_get(raw, "execution", "net_tolerance_base", 0.001)),
        max_consecutive_errors=int(_get(raw, "execution", "max_consecutive_errors", 3)),
        rate_limit_pause_sec=float(_get(raw, "execution", "rate_limit_pause_sec", 10.0)),
        staleness_sec=float(_get(raw, "execution", "staleness_sec", 10.0)),
        reconcile_sec=float(_get(raw, "execution", "reconcile_sec", 15.0)),
        venue_probe_sec=float(_get(raw, "execution", "venue_probe_sec", 30.0)),
        http_keepalive_sec=float(_get(raw, "execution", "http_keepalive_sec", 10.0)),
        recorder_enabled=bool(_get(raw, "recorder", "enabled", True)),
        recorder_csv=_get(raw, "recorder", "csv", "logs/minutes.csv"),
        log_level=_validated_log_level(_get(raw, "logging", "level", "INFO")),
        status_interval_sec=float(_get(raw, "logging", "status_interval_sec", 30.0)),
        trades_csv=_get(raw, "logging", "trades_csv", "logs/trades.csv"),
        dashboard=bool(_get(raw, "logging", "dashboard", True)),
        log_file=_get(raw, "logging", "file", "logs/engine.log"),
    )

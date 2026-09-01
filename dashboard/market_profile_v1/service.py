"""Build anchored market profiles for the dashboard page.

Compute lives in ``orderbook_analyse.market_profile`` and is reused as-is
rather than reimplemented, so the page and the offline validation run cannot
drift apart. This module only adapts it: request validation, the ClickHouse
client, a small result cache, and JSON shaping.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import clickhouse_connect

from research_charts.clickhouse_config import load_clickhouse_config
from research_charts.oa_import import load_market_profile

CACHE_TTL_S = 120.0
CACHE_MAX_ENTRIES = 24

# Each window is one ClickHouse aggregation, so the window count is the real
# cost driver. A day view over three months is fine; a year of sessions is not.
MAX_WINDOWS = 96
MAX_RANGE_DAYS = 180

SUPPORTED_TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "4h")
SUPPORTED_ANCHORS = ("day", "session", "composite")

DEFAULT_VALUE_AREA_PCT = 0.70
DEFAULT_TARGET_BINS = 160
MIN_TARGET_BINS = 40
MAX_TARGET_BINS = 400

UNIVERSE_PATH = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/config/universe_tradeable_51.json"
)

# The shape verdict is shown on the chart, so the page has to carry the fact
# that it failed validation. See results/market_profile_validation_v1.
SHAPE_NOTICE = (
    "Shape-Klassifikation ist unvalidiert: die Messung ueber 51 Symbole und "
    "2142 Fenster fand keinen Vorhersagewert (POC-Fortsetzung 50.8%, "
    "Kanten-Ablehnung ohne Effekt, POC-Rueckkehr durch Distanz erklaert). "
    "Als Beschreibung brauchbar, nicht als Signal."
)


class ProfileRequestError(ValueError):
    """Bad request with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def clear_cache_for_tests() -> None:
    with _cache_lock:
        _cache.clear()


def _client():
    cfg = load_clickhouse_config()
    return clickhouse_connect.get_client(**cfg.connect_kwargs())


def known_symbols() -> list[str]:
    """Frozen research universe, or empty if the config is unavailable."""
    try:
        raw = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(raw, dict):
        raw = raw.get("symbols") or []
    out = [str(s).strip().upper() for s in raw if isinstance(s, str)]
    return sorted(set(out))


def session_names() -> list[str]:
    return list(load_market_profile()["SESSIONS"].keys())


def _utc(unix_s: int) -> datetime:
    return datetime.fromtimestamp(int(unix_s), tz=timezone.utc)


def _normalize_request(
    *,
    symbol: str,
    start: int,
    end: int,
    anchor: str,
    sessions: str | None,
    timeframe: str,
    value_area_pct: float,
    target_bins: int,
    use_final: bool,
) -> dict[str, Any]:
    sym = str(symbol or "").strip().upper()
    if not sym or not sym.isalnum():
        raise ProfileRequestError("bad_symbol", f"invalid symbol: {symbol!r}")

    try:
        s_unix, e_unix = int(start), int(end)
    except (TypeError, ValueError) as exc:
        raise ProfileRequestError("bad_range", "start/end must be unix seconds") from exc
    if e_unix <= s_unix:
        raise ProfileRequestError("bad_range", "end must be after start")

    span_days = (e_unix - s_unix) / 86400.0
    if span_days > MAX_RANGE_DAYS:
        raise ProfileRequestError(
            "range_too_large",
            f"range spans {span_days:.1f} days, limit is {MAX_RANGE_DAYS}",
        )

    mode = str(anchor or "day").strip().lower()
    if mode not in SUPPORTED_ANCHORS:
        raise ProfileRequestError(
            "bad_anchor", f"anchor must be one of {', '.join(SUPPORTED_ANCHORS)}"
        )

    available = session_names()
    if sessions:
        wanted = [x.strip().lower() for x in str(sessions).split(",") if x.strip()]
        unknown = [x for x in wanted if x not in available]
        if unknown:
            raise ProfileRequestError(
                "bad_session", f"unknown session(s): {', '.join(unknown)}"
            )
    else:
        wanted = list(available)

    tf = str(timeframe or "15m").strip().lower()
    if tf not in SUPPORTED_TIMEFRAMES:
        raise ProfileRequestError(
            "bad_timeframe", f"timeframe must be one of {', '.join(SUPPORTED_TIMEFRAMES)}"
        )

    try:
        va = float(value_area_pct)
    except (TypeError, ValueError) as exc:
        raise ProfileRequestError("bad_value_area", "value_area_pct must be numeric") from exc
    if not 0.3 <= va <= 0.95:
        raise ProfileRequestError(
            "bad_value_area", "value_area_pct must be between 0.30 and 0.95"
        )

    try:
        bins = int(target_bins)
    except (TypeError, ValueError) as exc:
        raise ProfileRequestError("bad_target_bins", "target_bins must be an integer") from exc
    if not MIN_TARGET_BINS <= bins <= MAX_TARGET_BINS:
        raise ProfileRequestError(
            "bad_target_bins",
            f"target_bins must be between {MIN_TARGET_BINS} and {MAX_TARGET_BINS}",
        )

    return {
        "symbol": sym,
        "start": _utc(s_unix),
        "end": _utc(e_unix),
        "start_unix": s_unix,
        "end_unix": e_unix,
        "anchor_mode": mode,
        "sessions": tuple(wanted),
        "timeframe": tf,
        "value_area_pct": va,
        "target_bins": bins,
        "use_final": bool(use_final),
    }


def _cache_key(req: dict[str, Any], include_bins: bool) -> str:
    return "|".join(
        [
            req["symbol"],
            str(req["start_unix"]),
            str(req["end_unix"]),
            req["anchor_mode"],
            ",".join(req["sessions"]),
            req["timeframe"],
            f"{req['value_area_pct']:.4f}",
            str(req["target_bins"]),
            "final" if req["use_final"] else "plain",
            "bins" if include_bins else "nobins",
        ]
    )


def _cache_get(key: str) -> dict[str, Any] | None:
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and (now - hit[0]) < CACHE_TTL_S:
            return hit[1]
        if hit:
            _cache.pop(key, None)
    return None


def _cache_put(key: str, payload: dict[str, Any]) -> None:
    with _cache_lock:
        if len(_cache) >= CACHE_MAX_ENTRIES:
            oldest = min(_cache.items(), key=lambda kv: kv[1][0])[0]
            _cache.pop(oldest, None)
        _cache[key] = (time.time(), payload)


def _candles_payload(df) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    out: list[dict[str, Any]] = []
    for row in df.itertuples(index=False):
        ts = row.open_time
        # Candle timestamps are naive UTC in ClickHouse.
        unix = int(ts.replace(tzinfo=timezone.utc).timestamp())
        out.append(
            {
                "time": unix,
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
                "volume": float(getattr(row, "volume", 0.0) or 0.0),
            }
        )
    return out


def load_profiles(
    *,
    symbol: str,
    start: int,
    end: int,
    anchor: str = "day",
    sessions: str | None = None,
    timeframe: str = "15m",
    value_area_pct: float = DEFAULT_VALUE_AREA_PCT,
    target_bins: int = DEFAULT_TARGET_BINS,
    use_final: bool = False,
    include_bins: bool = True,
) -> dict[str, Any]:
    """Anchored profiles plus the candles to draw them against.

    `use_final` defaults to off: FINAL deduplication costs roughly 60x on the
    trade scan, and parity on this range was checked in the offline validation
    run. The toggle stays exposed so a suspicious window can be re-checked.
    """
    req = _normalize_request(
        symbol=symbol,
        start=start,
        end=end,
        anchor=anchor,
        sessions=sessions,
        timeframe=timeframe,
        value_area_pct=value_area_pct,
        target_bins=target_bins,
        use_final=use_final,
    )

    key = _cache_key(req, include_bins)
    cached = _cache_get(key)
    if cached is not None:
        return {**cached, "cached": True}

    mp = load_market_profile()
    windows = mp["build_windows"](
        anchor_mode=req["anchor_mode"],
        start=req["start"],
        end=req["end"],
        sessions=req["sessions"] if req["anchor_mode"] == "session" else None,
    )
    if not windows:
        raise ProfileRequestError("no_windows", "no profile windows in that range")
    if len(windows) > MAX_WINDOWS:
        raise ProfileRequestError(
            "too_many_windows",
            f"{len(windows)} windows requested, limit is {MAX_WINDOWS}. "
            "Narrow the range or switch anchor to day/composite.",
        )

    client = _client()
    df_1m = mp["fetch_candles_1m"](client, req["symbol"], req["start"], req["end"])
    if df_1m is None or df_1m.empty:
        raise ProfileRequestError(
            "no_candles", f"no 1m candles for {req['symbol']} in that range"
        )
    tf = req["timeframe"]
    df_tf = df_1m.copy() if tf in ("1m", "1min") else mp["aggregate_timeframe"](df_1m, tf)

    thresholds = mp["ShapeThresholds"]()
    profiles = []
    skipped: list[str] = []
    for window in windows:
        built = mp["build_profile"](
            client,
            req["symbol"],
            window,
            value_area_pct=req["value_area_pct"],
            target_bins=req["target_bins"],
            use_final=req["use_final"],
            thresholds=thresholds,
        )
        if built is None:
            skipped.append(window.window_id)
            continue
        profiles.append(built)

    if not profiles:
        raise ProfileRequestError(
            "no_profiles", "no window in that range had trade data"
        )

    profiles = mp["mark_naked_pocs"](profiles, df_1m)

    payload = {
        "success": True,
        "symbol": req["symbol"],
        "anchor_mode": req["anchor_mode"],
        "sessions": list(req["sessions"]),
        "timeframe": tf,
        "requested_start": req["start_unix"],
        "requested_end": req["end_unix"],
        "candles": _candles_payload(df_tf),
        "profiles": [p.to_dict(include_bins=include_bins) for p in profiles],
        "meta": {
            "windows": len(windows),
            "profiles_built": len(profiles),
            "skipped_windows": skipped,
            "value_area_pct": req["value_area_pct"],
            "target_bins": req["target_bins"],
            "use_final": req["use_final"],
            "shape_thresholds": thresholds.to_dict(),
            "shape_unvalidated": True,
            "shape_notice": SHAPE_NOTICE,
        },
        "cached": False,
    }
    _cache_put(key, payload)
    return payload

"""Call dashboard dual-profile builder — no local formula rewrite."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from orderbook_analyse.market_profile.anchor import as_utc
from orderbook_analyse.market_profile.contracts import ShapeThresholds

from ..timeparse import format_utc_z
from . import TIMEFRAME_ROLES, USE_FINAL
from .provenance import ensure_mp_import_path
from .windows import (
    current_full_bounds,
    developing_bounds,
    make_window,
    previous_closed_bounds,
)


def _ch_client() -> Any:
    from research_charts.clickhouse_config import load_clickhouse_config
    import clickhouse_connect

    return clickhouse_connect.get_client(**load_clickhouse_config().connect_kwargs())


def _defaults() -> tuple[float, int]:
    ensure_mp_import_path()
    from market_profile_v1.service import DEFAULT_TARGET_BINS, DEFAULT_VALUE_AREA_PCT

    return float(DEFAULT_VALUE_AREA_PCT), int(DEFAULT_TARGET_BINS)


def build_one_profile(
    *,
    symbol: str,
    tf: str,
    start: datetime,
    end: datetime,
    kind: str,
    client: Any | None = None,
    include_bins: bool = False,
) -> dict[str, Any] | None:
    """Direct dashboard ``build_dual_window_profile`` for ``[start, end)``."""
    ensure_mp_import_path()
    from market_profile_v1.dual_profile import build_dual_window_profile

    va, bins = _defaults()
    window = make_window(tf, start, end, kind=kind)
    own = client is None
    cl = client or _ch_client()
    try:
        raw = build_dual_window_profile(
            cl,
            symbol.upper(),
            window,
            value_area_pct=va,
            target_bins=bins,
            use_final=USE_FINAL,
            thresholds=ShapeThresholds(),
            candles_1m=None,
            include_bins=include_bins,
        )
    finally:
        if own:
            try:
                cl.close()
            except Exception:  # noqa: BLE001
                pass
    if raw is None:
        return None
    return _normalize(raw, tf=tf, kind=kind, start=start, end=end)


def dashboard_service_profile(
    *,
    symbol: str,
    tf: str,
    start: datetime,
    end: datetime,
    kind: str,
) -> dict[str, Any] | None:
    """Independent dashboard ``load_profiles`` call for the same UTC window."""
    ensure_mp_import_path()
    from market_profile_v1.service import load_profiles

    start = as_utc(start)
    end = as_utc(end)
    payload = load_profiles(
        symbol=symbol.upper(),
        start=int(start.timestamp()),
        end=int(end.timestamp()),
        mp_timeframe=tf,
        include_bins=False,
    )
    profiles = list(payload.get("profiles") or [])
    if not profiles:
        return None
    chosen = None
    for raw in profiles:
        win = raw.get("window") or {}
        ws = str(win.get("start") or "")
        we = str(win.get("end") or "")
        if ws.startswith(format_utc_z(start)[:19]) and we.startswith(format_utc_z(end)[:19]):
            chosen = raw
            break
    if chosen is None:
        chosen = profiles[0]
    return _normalize(chosen, tf=tf, kind=kind, start=start, end=end)


def _normalize(
    raw: dict[str, Any],
    *,
    tf: str,
    kind: str,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    tpo = raw.get("tpo") or {}
    vol = raw.get("volume") or {}
    tva = tpo.get("value_area") or {}
    vva = vol.get("value_area") or {}
    shape = raw.get("shape") or {}
    return {
        "kind": kind,
        "timeframe": tf,
        "role": TIMEFRAME_ROLES.get(tf),
        "profile_start": format_utc_z(as_utc(start)),
        "profile_end_exclusive": format_utc_z(as_utc(end)),
        "available_at": format_utc_z(as_utc(end)),
        "as_of": format_utc_z(as_utc(end)),
        "price_step": raw.get("price_step"),
        "range_low": raw.get("price_low"),
        "range_high": raw.get("price_high"),
        "open_price": raw.get("open_price"),
        "close_price": raw.get("close_price"),
        "naked_poc": raw.get("naked_poc"),
        "dual_contract_version": raw.get("dual_contract_version"),
        "tpo": {
            "poc": tva.get("poc"),
            "vah": tva.get("vah"),
            "val": tva.get("val"),
            "volume_share": tva.get("volume_share"),
            "shape": None,  # TPO shape is not a separate dashboard field
            "status": tpo.get("status"),
            "brackets": tpo.get("brackets") or {},
        },
        "volume": {
            "vpoc": vva.get("poc"),
            "vah": vva.get("vah"),
            "val": vva.get("val"),
            "volume_share": vva.get("volume_share"),
            "shape": shape.get("kind"),
            "shape_letter": shape.get("letter"),
            "shape_metrics": {
                k: shape.get(k)
                for k in ("poc_position", "va_range_share", "poc_concentration", "directional_share")
            },
            "status": vol.get("status"),
        },
        "shape_source": "volume_profile_classify_shape",
        "raw_window": (raw.get("window") or {}),
    }


def build_timeframe_bundle(
    *,
    symbol: str,
    focus_ts: datetime,
    tf: str,
    client: Any,
    include_final: bool,
) -> dict[str, Any]:
    prev_s, prev_e = previous_closed_bounds(focus_ts, tf)
    dev_s, dev_e = developing_bounds(focus_ts, tf)
    prev = build_one_profile(
        symbol=symbol, tf=tf, start=prev_s, end=prev_e, kind="PREVIOUS_CLOSED", client=client
    )
    dev = build_one_profile(
        symbol=symbol, tf=tf, start=dev_s, end=dev_e, kind="DEVELOPING_AS_OF_FOCUS", client=client
    )
    final = None
    if include_final:
        fin_s, fin_e = current_full_bounds(focus_ts, tf)
        # Only if the full block ends after focus — otherwise it equals previous/current closed
        if fin_e > as_utc(focus_ts):
            final = build_one_profile(
                symbol=symbol,
                tf=tf,
                start=fin_s,
                end=fin_e,
                kind="FINAL_PROFILE_FOR_PARITY_ONLY",
                client=client,
            )
    return {
        "timeframe": tf,
        "previous_closed": prev,
        "developing": dev,
        "final_for_parity_only": final,
    }

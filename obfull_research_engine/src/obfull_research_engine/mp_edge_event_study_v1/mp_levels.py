"""previous_closed TPO VAH/VAL levels — reuse market_profile_context (no new MP formula)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Sequence

from obfull_research_engine.market_profile_context.windows import (
    floor_period,
    period_s,
    previous_closed_bounds,
)
from obfull_research_engine.timeparse import format_utc_z

from .params import MP_KIND, PILOT_TIMEFRAMES, PROFILE_SOURCE
from .schema import ActiveLevel, MpProfile
from .util import as_utc, dt_to_ns, stable_hash


ProfileBuilder = Callable[..., dict[str, Any] | None]


def _profile_id(tf: str, start_ns: int, end_ns: int) -> str:
    return "mp_" + stable_hash([MP_KIND, tf, start_ns, end_ns], n=16)


def _extract_tpo(raw: dict[str, Any]) -> tuple[float, float, float | None, str]:
    tpo = raw.get("tpo") or {}
    # adapter._normalize stores vah/val directly under tpo
    vah = tpo.get("vah")
    val = tpo.get("val")
    poc = tpo.get("poc")
    status = str(tpo.get("status") or raw.get("status") or "OK")
    if vah is None or val is None:
        va = tpo.get("value_area") or {}
        vah = vah if vah is not None else va.get("vah")
        val = val if val is not None else va.get("val")
        poc = poc if poc is not None else va.get("poc")
    if vah is None or val is None:
        raise RuntimeError(f"MP profile missing TPO VAH/VAL: keys={list(raw.keys())}")
    return float(vah), float(val), (float(poc) if poc is not None else None), status


def period_ends_in_range(start: datetime, end: datetime, tf: str) -> list[datetime]:
    """UTC period boundaries T where previous_closed [T-P,T) becomes available at T,
    for T in (start, end] plus the boundary active at `start` (previous_closed end).
    """
    start = as_utc(start)
    end = as_utc(end)
    p = period_s(tf)
    # Profile active at start
    _ps, pe = previous_closed_bounds(start, tf)
    ends = {pe}
    # All period floors strictly after start up to end
    cur = floor_period(start, tf)
    # first boundary after start that is a period end
    if cur <= start:
        cur = datetime.fromtimestamp(int(cur.timestamp()) + p, tz=timezone.utc)
    while cur <= end:
        ends.add(cur)
        cur = datetime.fromtimestamp(int(cur.timestamp()) + p, tz=timezone.utc)
    return sorted(ends)


def build_profiles_for_window(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    timeframes: Sequence[str] = PILOT_TIMEFRAMES,
    client: Any | None = None,
    builder: ProfileBuilder | None = None,
    profile_source: str = PROFILE_SOURCE,
) -> list[MpProfile]:
    """Build all previous_closed profiles needed for [start, end)."""
    if builder is None:
        from obfull_research_engine.market_profile_context.adapter import build_one_profile

        def builder(  # type: ignore[misc]
            *,
            symbol: str,
            tf: str,
            start: datetime,
            end: datetime,
            kind: str,
            client: Any | None = None,
            include_bins: bool = False,
        ) -> dict[str, Any] | None:
            return build_one_profile(
                symbol=symbol,
                tf=tf,
                start=start,
                end=end,
                kind=kind,
                client=client,
                include_bins=include_bins,
            )

    out: list[MpProfile] = []
    seen: set[str] = set()
    for tf in timeframes:
        for pe in period_ends_in_range(start, end, tf):
            ps = datetime.fromtimestamp(int(pe.timestamp()) - period_s(tf), tz=timezone.utc)
            start_ns = dt_to_ns(ps)
            end_ns = dt_to_ns(pe)
            pid = _profile_id(tf, start_ns, end_ns)
            if pid in seen:
                continue
            seen.add(pid)
            raw = builder(
                symbol=symbol,
                tf=tf,
                start=ps,
                end=pe,
                kind=MP_KIND,
                client=client,
                include_bins=False,
            )
            if raw is None:
                continue
            vah, val, poc, status = _extract_tpo(raw)
            avail = end_ns  # available exactly at period end (exclusive window end)
            if avail != end_ns:
                raise RuntimeError("internal: available_at must equal profile_end")
            out.append(
                MpProfile(
                    profile_id=pid,
                    timeframe=tf,
                    profile_start_ts_ns=start_ns,
                    profile_end_ts_ns=end_ns,
                    profile_available_ts_ns=avail,
                    profile_source=profile_source,
                    vah=vah,
                    val=val,
                    poc=poc,
                    status=status,
                )
            )
    out.sort(key=lambda p: (p.timeframe, p.profile_available_ts_ns, p.profile_id))
    return out


def profiles_to_levels(profiles: Sequence[MpProfile]) -> list[ActiveLevel]:
    levels: list[ActiveLevel] = []
    for p in profiles:
        for role, price in (("UPPER", p.vah), ("LOWER", p.val)):
            lid = "lv_" + stable_hash([p.profile_id, role, round(price, 6)], n=14)
            levels.append(
                ActiveLevel(
                    level_id=lid,
                    profile_id=p.profile_id,
                    timeframe=p.timeframe,
                    role=role,
                    level_price=float(price),
                    profile_start_ts_ns=p.profile_start_ts_ns,
                    profile_end_ts_ns=p.profile_end_ts_ns,
                    profile_available_ts_ns=p.profile_available_ts_ns,
                    profile_source=p.profile_source,
                )
            )
    return levels


def active_levels_at(
    all_levels: Sequence[ActiveLevel],
    *,
    ts_ns: int,
    timeframes: Sequence[str] = PILOT_TIMEFRAMES,
) -> list[ActiveLevel]:
    """Latest previous_closed level per (tf, role) with available_at <= ts_ns."""
    out: list[ActiveLevel] = []
    for tf in timeframes:
        for role in ("UPPER", "LOWER"):
            cands = [
                lv
                for lv in all_levels
                if lv.timeframe == tf
                and lv.role == role
                and lv.profile_available_ts_ns <= ts_ns
            ]
            if not cands:
                continue
            best = max(cands, key=lambda lv: (lv.profile_available_ts_ns, lv.level_id))
            if best.profile_available_ts_ns > ts_ns:
                raise RuntimeError(
                    f"CAUSALITY_VIOLATION: profile_available_ts > event_ts "
                    f"({best.profile_available_ts_ns} > {ts_ns})"
                )
            out.append(best)
    return out


def assert_causality(level: ActiveLevel, event_ts_ns: int) -> None:
    if level.profile_available_ts_ns > event_ts_ns:
        raise RuntimeError(
            f"CAUSALITY_VIOLATION: profile_available_ts={format_utc_z(datetime.fromtimestamp(level.profile_available_ts_ns/1e9, tz=timezone.utc))} "
            f"> event_ts for level {level.level_id}"
        )


def fixture_profile(
    *,
    tf: str,
    start: datetime,
    end: datetime,
    vah: float,
    val: float,
    poc: float | None = None,
) -> MpProfile:
    start = as_utc(start)
    end = as_utc(end)
    start_ns = dt_to_ns(start)
    end_ns = dt_to_ns(end)
    return MpProfile(
        profile_id=_profile_id(tf, start_ns, end_ns),
        timeframe=tf,
        profile_start_ts_ns=start_ns,
        profile_end_ts_ns=end_ns,
        profile_available_ts_ns=end_ns,
        profile_source="fixture",
        vah=float(vah),
        val=float(val),
        poc=float(poc) if poc is not None else (float(vah) + float(val)) / 2.0,
        status="OK",
    )

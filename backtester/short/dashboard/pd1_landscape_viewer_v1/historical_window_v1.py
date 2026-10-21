"""Read-only PD1 landscape historical window builder (same contract as replay bundle)."""

from __future__ import annotations

import inspect
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import clickhouse_connect

from research_charts.clickhouse_config import load_clickhouse_config
from research_charts.clickhouse_source import ClickHouseResearchCandleSource
from research_charts.short_pool_oracle_v1 import parse_decision_iso
from research_charts.historical_lmv2_pd_contract_v1 import (
    ALLOWED_VIEWER_SCOPES,
    HistoricalPdConfig,
    membership_slot_minutes,
    resolve_viewer_historical_scope,
)
from research_charts.historical_pool_state_batch_reader_v1 import (
    historical_lmv2_batch_reader_enabled,
)
from research_charts.historical_pool_state_v1 import (
    HistoricalPoolStateError,
    get_published_decision_closes,
    load_historical_pool_state_at_close,
    static_timeline_import_bounds,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import PublishedActiveReader
from collections import defaultdict

PD1_POOL_DETAIL = 1
SYMBOL = "DOGEUSDT"
TIMEFRAME_5M = "5m"
WARMUP_HOURS = 168
MAX_EXPORT_HOURS = 72
MAX_5M_BARS = MAX_EXPORT_HOURS * 12

_PROFILE = os.environ.get("PD1_LANDSCAPE_WINDOW_PROFILE") == "1"
_LAST_PROFILE: dict[str, Any] | None = None
_profile_ctx: dict[str, Any] = {}


def get_last_window_profile() -> dict[str, Any] | None:
    return _LAST_PROFILE


_DEFAULT_FILL = {
    "Extreme": "#f4433633",
    "Medium": "#8bc34a33",
    "Strong": "#ffeb3b33",
    "Very Strong": "#ff980033",
    "Weak": "#2196f333",
}
_DEFAULT_BORDERS = {"lower": "#00bcd4", "upper": "#ef5350"}

def _color_contract_path() -> Path | None:
    rel = Path("logs/historical_import/overnight_2026/phase6xt_pd1_strength_density_v1/renderer_color_contract.json")
    here = Path(__file__).resolve()
    for base in (here.parents[4], here.parents[2], Path("/home/telgenbuescher/projects/pools+ob+delta_bot")):
        p = (base / rel).resolve()
        if p.is_file():
            return p
    return None


class LandscapeWindowError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return _utc(value)
    s = str(value).strip().replace(" ", "T")
    if not s.endswith("Z"):
        s += "Z"
    return parse_decision_iso(s)


def _causal_decision_close(t: datetime, slot_minutes: int) -> datetime:
    t = _utc(t)
    if slot_minutes >= 60:
        total_min = t.hour * 60 + t.minute
        aligned = (total_min // slot_minutes) * slot_minutes
        return t.replace(hour=aligned // 60, minute=aligned % 60, second=0, microsecond=0)
    slot = (t.minute // slot_minutes) * slot_minutes
    return t.replace(minute=slot, second=0, microsecond=0)


def _causal_15m_decision_close(t: datetime) -> datetime:
    return _causal_decision_close(t, 15)


def _resample_5m_from_candles(candles: list) -> dict[datetime, dict]:
    m1: dict[datetime, dict] = {}
    for c in candles:
        ts = _utc(c.timestamp if hasattr(c, "timestamp") else c["timestamp"])
        m1[ts] = {
            "open": float(c.open if hasattr(c, "open") else c["open"]),
            "high": float(c.high if hasattr(c, "high") else c["high"]),
            "low": float(c.low if hasattr(c, "low") else c["low"]),
            "close": float(c.close if hasattr(c, "close") else c["close"]),
        }
    buckets: dict[datetime, list] = defaultdict(list)
    for ts, bar in sorted(m1.items()):
        slot = ts.replace(minute=(ts.minute // 5) * 5, second=0, microsecond=0)
        buckets[slot].append(bar)
    out: dict[datetime, dict] = {}
    for slot, bars in buckets.items():
        out[slot] = {
            "open": bars[0]["open"],
            "high": max(b["high"] for b in bars),
            "low": min(b["low"] for b in bars),
            "close": bars[-1]["close"],
        }
    return out


def _load_style() -> tuple[dict[str, str], dict[str, str]]:
    contract = _color_contract_path()
    if contract is not None:
        raw = json.loads(contract.read_text(encoding="utf-8"))
        return dict(raw.get("FILL_BY_CLASS") or _DEFAULT_FILL), dict(
            raw.get("side_borders") or _DEFAULT_BORDERS
        )
    return dict(_DEFAULT_FILL), dict(_DEFAULT_BORDERS)


def _published_for(hcfg: HistoricalPdConfig) -> list[datetime]:
    reader = PublishedActiveReader()
    return get_published_decision_closes(reader, hcfg.config_hash, pipeline_line_id=hcfg.pipeline_line_id)


def _scope_bounds(hcfg: HistoricalPdConfig) -> tuple[datetime, datetime]:
    published = _published_for(hcfg)
    return static_timeline_import_bounds(published)


def landscape_capabilities() -> dict[str, Any]:
    default = resolve_viewer_historical_scope(timeframe="15m", pool_detail=PD1_POOL_DETAIL)
    lo, hi = _scope_bounds(default)
    fill, borders = _load_style()
    supported: list[dict[str, Any]] = []
    for tf, pd in sorted(ALLOWED_VIEWER_SCOPES):
        hcfg = resolve_viewer_historical_scope(timeframe=tf, pool_detail=pd)
        slo, shi = _scope_bounds(hcfg)
        supported.append(
            {
                **hcfg.to_api_dict(),
                "data_available_from": _iso(slo),
                "data_available_to": _iso(shi),
                "membership_slot_minutes": membership_slot_minutes(tf),
            }
        )
    return {
        "version": "pd1_landscape_historical_window_v1",
        "symbol": SYMBOL,
        "pool_detail": PD1_POOL_DETAIL,
        "timeframe": default.timeframe,
        "timeframe_5m": TIMEFRAME_5M,
        "pipeline_line_id": default.pipeline_line_id,
        "config_hash": default.config_hash,
        "data_available_from": _iso(lo),
        "data_available_to": _iso(hi),
        "supported_viewer_scopes": supported,
        "membership_slot_minutes": membership_slot_minutes(default.timeframe),
        "warmup_hours": WARMUP_HOURS,
        "max_export_hours": MAX_EXPORT_HOURS,
        "max_5m_bars_per_request": MAX_5M_BARS,
        "membership_loader": "load_historical_pool_state_at_close + canonical_active_member",
        "remaining_included": False,
        "causality_rule": "known_at <= as_of via historical_pool_state_v1",
        "FILL_BY_CLASS": fill,
        "side_borders": borders,
        "dynamic_historical": True,
    }


def _iter_5m(start: datetime, end: datetime) -> list[datetime]:
    out: list[datetime] = []
    t = start
    while t <= end:
        out.append(t)
        t += timedelta(minutes=5)
    return out


def _canonical_active_member(row: dict[str, Any]) -> dict[str, Any]:
    lower = row.get("lower_bound")
    if lower is None:
        lower = row.get("lower")
    upper = row.get("upper_bound")
    if upper is None:
        upper = row.get("upper")
    return {
        "pool_id": row.get("pool_id"),
        "pipeline_line_id": row.get("pipeline_line_id"),
        "snapshot_id": row.get("snapshot_id"),
        "side": row.get("side"),
        "pool_type": row.get("pool_type"),
        "lower_bound": lower,
        "upper_bound": upper,
        "strength": row.get("strength"),
        "strength_class": row.get("strength_class"),
        "known_at": row.get("known_at"),
        "break_at": row.get("break_at"),
        "confirmed_at": row.get("confirmed_at"),
        "invalidated_at": row.get("invalidated_at"),
        "decision_close": row.get("decision_close"),
        "active_membership": row.get("active_membership"),
        "removal_cause": row.get("removal_cause"),
    }


def _origin_logical_v2_key(pool_id: str) -> str:
    parts = pool_id.split(":")
    if len(parts) >= 6 and parts[0] == "lldv2":
        return f"{parts[0]}:{parts[1]}:{parts[2]}:{parts[4]}:{parts[5]}"
    if len(parts) >= 7:
        return f"{parts[0]}:{parts[1]}:{parts[2]}:{parts[5]}:{parts[6]}"
    return pool_id


def _pool_row(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "pool_id": m["pool_id"],
        "side": m.get("side"),
        "bottom": float(m["lower_bound"]),
        "top": float(m["upper_bound"]),
        "strength": m.get("strength"),
        "strength_class": m.get("strength_class") or "Weak",
        "origin": _origin_logical_v2_key(m["pool_id"]),
        "known_at": m.get("known_at"),
    }


def _load_members(
    cache: dict[str, list[dict]],
    dc: datetime,
    client: Any,
    db: str,
    hcfg: HistoricalPdConfig,
) -> list[dict]:
    profile = _profile_ctx if _PROFILE else None
    key = _iso(dc)
    if key not in cache:
        t_lm = time.perf_counter()
        load_kw: dict[str, Any] = {
            "pool_detail": hcfg.pool_detail,
            "timeframe": hcfg.timeframe,
            "client": client,
            "database": db,
        }
        if "include_remaining" in inspect.signature(load_historical_pool_state_at_close).parameters:
            load_kw["include_remaining"] = False
        st = load_historical_pool_state_at_close(dc, **load_kw)
        cache[key] = [_canonical_active_member(m) for m in st.active_members]
        if profile is not None:
            profile["load_members_miss"] = profile.get("load_members_miss", 0) + 1
            profile["load_members_ms"] = profile.get("load_members_ms", 0.0) + (
                time.perf_counter() - t_lm
            ) * 1000
    elif profile is not None:
        profile["load_members_hit"] = profile.get("load_members_hit", 0) + 1
    if profile is not None:
        profile["load_members_calls"] = profile.get("load_members_calls", 0) + 1
    return cache[key]


def _build_lifetimes(
    grid: list[datetime],
    cache: dict[str, list[dict]],
    client: Any,
    db: str,
    hcfg: HistoricalPdConfig,
    slot_minutes: int,
) -> list[dict[str, Any]]:
    """ACTIVE intervals on 5m timeline; membership constant between publication slots."""
    by_pool: dict[str, dict[str, Any]] = {}
    windows: list[tuple[datetime, datetime, list[dict]]] = []
    win_start = grid[0]
    prev_dc: datetime | None = None
    for dt in grid:
        dc = _causal_decision_close(dt, slot_minutes)
        if prev_dc is not None and dc != prev_dc:
            windows.append(
                (win_start, dt - timedelta(minutes=5), _load_members(cache, prev_dc, client, db, hcfg))
            )
            win_start = dt
        prev_dc = dc
    windows.append((win_start, grid[-1], _load_members(cache, prev_dc, client, db, hcfg)))

    for w0, w1, members in windows:
        seg = [_iso(w0), _iso(w1)]
        for m in members:
            pid = m["pool_id"]
            if pid not in by_pool:
                by_pool[pid] = {**_pool_row(m), "segments": [seg[:]]}
            else:
                pl = by_pool[pid]
                pl.update(
                    {
                        "bottom": float(m["lower_bound"]),
                        "top": float(m["upper_bound"]),
                        "strength": m.get("strength"),
                        "strength_class": m.get("strength_class") or "Weak",
                    }
                )
                last = pl["segments"][-1]
                if last[1] == seg[0] or parse_decision_iso(last[1]) + timedelta(minutes=5) == w0:
                    last[1] = seg[1]
                else:
                    pl["segments"].append(seg[:])

    return sorted(by_pool.values(), key=lambda x: x["pool_id"])


def _clip_segments_to_window(pools: list[dict], vis_from: datetime, vis_to: datetime) -> list[dict]:
    out: list[dict] = []
    for pl in pools:
        segs = []
        for seg in pl.get("segments") or []:
            s0 = parse_decision_iso(seg[0])
            s1 = parse_decision_iso(seg[1])
            if s1 < vis_from or s0 > vis_to:
                continue
            segs.append(seg)
        if segs:
            out.append({**pl, "segments": segs})
    return out


def _build_clusters_by_dc(
    grid: list[datetime],
    cache: dict[str, list[dict]],
    client: Any,
    db: str,
    bars: dict[datetime, dict],
    hcfg: HistoricalPdConfig,
    slot_minutes: int,
) -> dict[str, Any]:
    try:
        from research_charts.dynamic_liquidity_landscape_v1 import analyze_side
    except ImportError:
        return {}

    clusters_by_dc: dict[str, Any] = {}
    prev_dc = None
    for dt in grid:
        dc = _causal_decision_close(dt, slot_minutes)
        if dc == prev_dc:
            continue
        dc_s = _iso(dc)
        members = _load_members(cache, dc, client, db, hcfg)
        price = float(bars[dt]["close"]) if dt in bars else float("nan")
        for side in ("lower", "upper"):
            sm = [_pool_row(m) for m in members if m.get("side") == side]
            an = analyze_side(sm, price, dedupe_micro=False) if sm else {}
            for i, cl in enumerate(an.get("clusters") or []):
                smem = [m for m in sm if m["pool_id"] in cl["pool_ids"]]
                origins = sorted({m["origin"] for m in smem})
                strengths = [float(m["strength"]) for m in smem if m.get("strength")]
                clusters_by_dc.setdefault(dc_s, {}).setdefault(side, []).append(
                    {
                        "cluster_index": i,
                        "bottom": cl["bottom"],
                        "top": cl["top"],
                        "pool_ids": cl["pool_ids"],
                        "origins": origins,
                        "origin_count": len(origins),
                        "strength_max": cl.get("strength_max"),
                        "strength_median": sorted(strengths)[len(strengths) // 2] if strengths else None,
                        "max_overlap_depth": cl.get("max_overlap_depth"),
                        "union_width": cl["top"] - cl["bottom"],
                    }
                )
        prev_dc = dc
    return clusters_by_dc


def build_landscape_window(
    from_utc: str | datetime,
    to_utc: str | datetime,
    *,
    timeframe: str = "15m",
    pool_detail: int = PD1_POOL_DETAIL,
    client: Any | None = None,
    database: str | None = None,
) -> dict[str, Any]:
    global _LAST_PROFILE, _profile_ctx
    try:
        hcfg = resolve_viewer_historical_scope(timeframe=timeframe, pool_detail=pool_detail)
    except HistoricalPoolStateError as exc:
        raise LandscapeWindowError(exc.code, exc.message, exc.details) from exc
    slot_minutes = membership_slot_minutes(hcfg.timeframe)
    if _PROFILE:
        _profile_ctx = {}
    t0 = time.perf_counter()
    vis_from = _parse_utc(from_utc)
    vis_to = _parse_utc(to_utc)
    if vis_from > vis_to:
        raise LandscapeWindowError("INVALID_REQUEST", "from_utc must be <= to_utc")
    span_h = (vis_to - vis_from).total_seconds() / 3600.0
    if span_h > MAX_EXPORT_HOURS + 1e-6:
        raise LandscapeWindowError(
            "INVALID_REQUEST",
            f"window exceeds {MAX_EXPORT_HOURS}h export limit",
            {"requested_hours": span_h, "max_export_hours": MAX_EXPORT_HOURS},
        )

    published = _published_for(hcfg)
    data_lo, data_hi = static_timeline_import_bounds(published)
    if vis_from < data_lo or vis_to > data_hi:
        raise LandscapeWindowError(
            "OUT_OF_SCOPE",
            "request outside Apr–Sep imported publications",
            {
                "data_available_from": _iso(data_lo),
                "data_available_to": _iso(data_hi),
                "requested_from": _iso(vis_from),
                "requested_to": _iso(vis_to),
            },
        )

    context_from = max(data_lo, vis_from - timedelta(hours=WARMUP_HOURS))
    grid = _iter_5m(context_from, vis_to)
    if _PROFILE:
        dcs = {_iso(_causal_decision_close(t, slot_minutes)) for t in grid}
        _profile_ctx["grid_5m_steps"] = len(grid)
        _profile_ctx["unique_decision_closes"] = len(dcs)
        _profile_ctx["warmup_hours"] = WARMUP_HOURS
        _profile_ctx["visible_hours"] = round(span_h, 4)
        _profile_ctx["context_hours"] = round((vis_to - context_from).total_seconds() / 3600.0, 4)
    visible_5m_steps = int((vis_to - vis_from).total_seconds() // 300) + 1
    if visible_5m_steps > MAX_5M_BARS:
        raise LandscapeWindowError(
            "INVALID_REQUEST",
            "too many 5m steps in visible window",
            {"max_5m_bars": MAX_5M_BARS, "requested_5m_steps": visible_5m_steps},
        )

    cfg = load_clickhouse_config()
    own_client = client is None
    if client is None:
        client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = database or cfg.database

    t_ch = time.perf_counter()
    src = ClickHouseResearchCandleSource()
    bars_list = src.get_candles(SYMBOL, TIMEFRAME_5M, start=context_from, end=vis_to + timedelta(minutes=5))
    bars = _resample_5m_from_candles(bars_list)
    t_ch_done = time.perf_counter()

    cache: dict[str, list[dict]] = {}
    phase5s_prefill: dict[str, Any] | None = None
    t_prefill = time.perf_counter()
    if historical_lmv2_batch_reader_enabled():
        from .historical_window_batch_v1 import prefill_member_cache_phase5s

        pre = prefill_member_cache_phase5s(
            cache,
            context_from,
            vis_to,
            client=client,
            database=db,
            pool_detail=hcfg.pool_detail,
            timeframe=hcfg.timeframe,
        )
        if pre is not None:
            phase5s_prefill = {
                "active": True,
                "decision_closes": pre.decision_closes,
                "cache_keys_filled": pre.cache_keys_filled,
                "clickhouse_query_count": pre.stats.get("clickhouse_query_count"),
                "prefill_ms": round((time.perf_counter() - t_prefill) * 1000, 1),
            }
    t_life = time.perf_counter()
    pools_full = _build_lifetimes(grid, cache, client, db, hcfg, slot_minutes)
    t_life_done = time.perf_counter()
    pools = _clip_segments_to_window(pools_full, vis_from, vis_to)

    vis_grid = [t for t in grid if vis_from <= t <= vis_to]
    price_rows = [
        {
            "t": _iso(t),
            "o": float(bars[t]["open"]),
            "h": float(bars[t]["high"]),
            "l": float(bars[t]["low"]),
            "c": float(bars[t]["close"]),
        }
        for t in vis_grid
        if t in bars
    ]
    t_cl = time.perf_counter()
    clusters_by_dc = _build_clusters_by_dc(grid, cache, client, db, bars, hcfg, slot_minutes)
    t_cl_done = time.perf_counter()
    # Keep only DC keys overlapping visible window
    clusters_by_dc = {
        k: v for k, v in clusters_by_dc.items() if vis_from <= parse_decision_iso(k) <= vis_to
    }

    fill, borders = _load_style()
    payload = {
        "meta": {
            "period_start": _iso(vis_from),
            "period_end": _iso(vis_to),
            "pool_count": len(pools),
            "FILL_BY_CLASS": fill,
            "side_borders": borders,
            "anchors": [],
        },
        "prices": price_rows,
        "pools": pools,
        "events": [],
        "clusters_by_dc": clusters_by_dc,
        "provenance": {
            "pipeline_line_id": hcfg.pipeline_line_id,
            "config_hash": hcfg.config_hash,
            "pool_detail": hcfg.pool_detail,
            "timeframe": hcfg.timeframe,
            "membership_slot_minutes": slot_minutes,
            "context_from": _iso(context_from),
            "membership_loader": (
                "phase5s_episode_walk_batch_cache"
                if phase5s_prefill
                else "load_historical_pool_state_at_close"
            ),
            "remaining": False,
            "phase5s_batch_reader_active": phase5s_prefill is not None,
            **(
                {"phase5s_batch_stats": phase5s_prefill}
                if phase5s_prefill
                else {}
            ),
        },
        "window": {
            "from_utc": _iso(vis_from),
            "to_utc": _iso(vis_to),
            "context_from_utc": _iso(context_from),
        },
    }
    t_json = time.perf_counter()
    json.dumps(payload)
    t_json_done = time.perf_counter()
    t_done = time.perf_counter()

    if own_client:
        try:
            client.close()
        except Exception:
            pass

    timing = {
        "clickhouse_candles": round((t_ch_done - t_ch) * 1000, 1),
        "build_lifetimes": round((t_life_done - t_life) * 1000, 1),
        "build_clusters_by_dc": round((t_cl_done - t_cl) * 1000, 1),
        "json_serialize": round((t_json_done - t_json) * 1000, 1),
        "total": round((t_done - t0) * 1000, 1),
    }
    payload["timing_ms"] = timing
    if _PROFILE:
        _profile_ctx["timing_ms"] = timing
        _profile_ctx["pools_out"] = len(pools)
        _profile_ctx["prices_out"] = len(price_rows)
        _profile_ctx["clusters_dc_keys"] = len(clusters_by_dc)
        _profile_ctx["ssot_cache_keys"] = len(cache)
        _LAST_PROFILE = dict(_profile_ctx)
        payload["profile"] = dict(_profile_ctx)
    return payload


def membership_at_utc(asof_utc: str) -> dict[str, Any]:
    """Causal membership probe for audits (read-only)."""
    dt = _parse_utc(asof_utc)
    dc = _causal_15m_decision_close(dt)
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    try:
        hcfg = resolve_viewer_historical_scope(timeframe="15m", pool_detail=PD1_POOL_DETAIL)
        members = _load_members({}, dc, client, cfg.database, hcfg)
    finally:
        try:
            client.close()
        except Exception:
            pass
    return {
        "asof_utc": _iso(dt),
        "decision_close": _iso(dc),
        "members": [_pool_row(m) for m in members],
    }


def landscape_window_from_state_err(exc: HistoricalPoolStateError) -> LandscapeWindowError:
    return LandscapeWindowError(exc.code, exc.message, exc.details)

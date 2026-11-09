"""BT-P9B — EMA59 6M WATCH episode diagnostic overlays (session-only, read-only)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from .bt_d12_latest_backtester import resolve_pools_ob_repo
from .ema59_6m_band_overlay_v1 import (
    TIMEFRAME,
    decision_close_to_chart_open_unix,
)

SOURCE = "ema59_p9b_watch_diagnostic_overlay_v1"
STRATEGY_ID = "ema59_6m_watch_diagnostic_v1"
PREFIX = "ema59-p9b-"
EXPECTED_EPISODES = 20

TIMELINES_REL = Path("results/phase_ema59_bt_p9a_audit_v1/episode_timelines.json")
CHART_EVENTS_REL = Path("results/phase_ema59_bt_p9_6m_watch_v1/chart_events.json")
POOL_AUDIT_REL = Path("results/phase_ema59_bt_p9a_audit_v1/candidate_pool_audit.json")

WATCH_END_STYLES: dict[str, tuple[str, str]] = {
    "next_ema_down_cross": ("#64748b", "WATCH end · next cross"),
    "return_above_minus_2sigma": ("#a855f7", "WATCH end · −2σ"),
    "return_above_ema59": ("#f59e0b", "WATCH end · EMA59"),
    "first_confirmed_rebound": ("#22c55e", "WATCH end · 1st B"),
    "end_lower_pool_reaction_phase": ("#ec4899", "WATCH end · pool phase"),
}


def _pools_path(rel: Path) -> Path:
    return resolve_pools_ob_repo() / rel


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def _open_ts(decision_close_utc: str) -> datetime:
    return _parse_utc(decision_close_utc) - timedelta(minutes=5)


@lru_cache(maxsize=1)
def _load_timelines() -> list[dict[str, Any]] | None:
    path = _pools_path(TIMELINES_REL)
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, list) else None


@lru_cache(maxsize=1)
def _load_chart_events() -> list[dict[str, Any]] | None:
    path = _pools_path(CHART_EVENTS_REL)
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, list) else None


@lru_cache(maxsize=1)
def _pool_audit_by_touch() -> dict[str, dict[str, Any]]:
    path = _pools_path(POOL_AUDIT_REL)
    if not path.is_file():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(rows, list):
        return out
    for row in rows:
        key = f"{row.get('cross_id')}|{row.get('bar_a_close_utc')}"
        out[key] = row
    return out


def cross_index_to_episode_id(cross_index: int) -> int:
    return int(cross_index) + 1


def cross_index_to_cross_id(cross_index: int) -> str:
    return f"6M-X-{cross_index_to_episode_id(cross_index):02d}"


def artifacts_available() -> dict[str, Any]:
    tl = _load_timelines()
    ev = _load_chart_events()
    return {
        "timelines_path": str(_pools_path(TIMELINES_REL)),
        "chart_events_path": str(_pools_path(CHART_EVENTS_REL)),
        "timelines_ok": tl is not None and len(tl) == EXPECTED_EPISODES,
        "chart_events_ok": ev is not None,
        "episode_count": len(tl) if tl else 0,
    }


def _tooltip_touch(row: dict[str, Any] | None, fallback: dict[str, Any]) -> str:
    r = row or fallback
    lines = [
        f"Touch A · {r.get('bar_close_utc') or r.get('bar_a_close_utc')}",
        f"Pool: {r.get('pool_id')}",
        f"Band: {r.get('lower_edge')} – {r.get('upper_edge')}",
        f"Width %: {r.get('band_width_pct')}",
        f"Origins: {r.get('distinct_origin_count') or r.get('origin_count')}",
        "Historisch · Echtzeit (Bar Close A)",
    ]
    deeper = r.get("deeper_active_lowers_at_a") or r.get("deeper_lowers_at_a") or []
    if deeper:
        lines.append("Deeper ACTIVE D2:")
        for d in deeper[:5]:
            lines.append(f"  · {d.get('pool_id')} ({d.get('distinct_origins')} orig)")
    return "\n".join(str(x) for x in lines)


def build_episode_overlays(*, cross_index: int, symbol: str) -> tuple[list[Any], dict[str, Any]]:
    from .trp_import import load_trp

    if cross_index < 0 or cross_index >= EXPECTED_EPISODES:
        return [], {"error": "cross_index_out_of_range"}

    timelines = _load_timelines()
    events_all = _load_chart_events()
    if not timelines or len(timelines) != EXPECTED_EPISODES:
        return [], {"error": "timelines_unavailable"}
    if not events_all:
        return [], {"error": "chart_events_unavailable"}

    episode_id = cross_index_to_episode_id(cross_index)
    cross_id = cross_index_to_cross_id(cross_index)
    tl = timelines[cross_index]
    if str(tl.get("cross_id")) != cross_id:
        tl = next((t for t in timelines if t.get("cross_id") == cross_id), tl)

    audit_by_touch = _pool_audit_by_touch()
    trp = load_trp()
    OverlayMarker = trp["OverlayMarker"]
    OverlayLine = trp["OverlayLine"]
    OverlayStyle = trp["OverlayStyle"]
    ensure_utc = trp["ensure_utc"]
    sym = str(symbol or "").strip().upper()
    out: list[Any] = []

    ev_chrono = tl.get("events_chronological") or {}
    cross_close = str(ev_chrono.get("ema_down_cross") or tl.get("cross_close_utc") or "")

    def add_vline(suffix: str, decision_close: str, color: str, label: str, meta: dict[str, Any]) -> None:
        if not decision_close:
            return
        ts = _open_ts(decision_close)
        out.append(
            OverlayLine(
                overlay_id=f"{PREFIX}{cross_id}-vl-{suffix}",
                symbol=sym,
                kind="vertical",
                timestamp=ensure_utc(ts),
                style=OverlayStyle(color=color, width=1.0, opacity=0.75, line_style="dashed"),
                label_text=label,
                timeframe_scope=TIMEFRAME,
                visible=True,
                z_order=36,
                metadata={
                    "origin": SOURCE,
                    "strategy_id": STRATEGY_ID,
                    "episode_cross_id": cross_id,
                    "decision_close_utc": decision_close,
                    **meta,
                },
            )
        )

    # WATCH start (distinct from cross marker — subtle vline)
    add_vline(
        "watch-start",
        cross_close,
        "#38bdf8",
        "WATCH",
        {"kind": "WATCH_START", "realtime": True},
    )

    watch_ends = tl.get("watch_end_by_variant") or {}
    for variant_id, (color, label) in WATCH_END_STYLES.items():
        row = watch_ends.get(variant_id) or {}
        end_close = row.get("watch_end_close_utc")
        if not end_close:
            continue
        add_vline(
            f"end-{variant_id}",
            end_close,
            color,
            label.split("·")[-1].strip() if "·" in label else variant_id,
            {"kind": "WATCH_END", "variant_id": variant_id, "realtime": True, "note": row.get("note")},
        )

    # Post-hoc global min (retrospective — not a trigger)
    post_min = ev_chrono.get("post_hoc_global_min") or {}
    pm_bar = post_min.get("bar_close_utc")
    if pm_bar:
        ts = _open_ts(pm_bar)
        out.append(
            OverlayMarker(
                overlay_id=f"{PREFIX}{cross_id}-posthoc-min",
                symbol=sym,
                timestamp=ensure_utc(ts),
                price=float(post_min.get("low") or 0) or None,
                position="below",
                shape="diamond",
                text="MIN†",
                size=9.0,
                style=OverlayStyle(color="#94a3b8", width=1.0, opacity=0.85),
                timeframe_scope=TIMEFRAME,
                visible=True,
                z_order=35,
                metadata={
                    "origin": SOURCE,
                    "strategy_id": STRATEGY_ID,
                    "kind": "POST_HOC_EPISODE_GLOBAL_MIN",
                    "episode_cross_id": cross_id,
                    "realtime": False,
                    "retrospective": True,
                    "tooltip": post_min.get("note") or "Rückblick — kein Echtzeit-Trigger",
                },
            )
        )

    ep_events = [e for e in events_all if int(e.get("episode_id") or 0) == episode_id]

    for i, ev in enumerate(ep_events):
        kind = str(ev.get("event") or "")
        bar = ev.get("bar_close_utc") or ev.get("bar_a_close_utc") or ev.get("bar_b_close_utc") or ev.get("bar_c_close_utc")
        if not bar:
            continue
        ts = ensure_utc(_open_ts(str(bar)))
        oid_base = f"{PREFIX}{cross_id}-{kind}-{i}"

        if kind in ("TOUCH_A", "TOUCH_A_DEEPER"):
            audit_key = f"{cross_id}|{bar}"
            audit_row = audit_by_touch.get(audit_key)
            tooltip = _tooltip_touch(
                audit_row,
                {
                    "pool_id": ev.get("pool_id"),
                    "bar_close_utc": bar,
                    "lower_edge": (audit_row or {}).get("lower_edge"),
                    "upper_edge": (audit_row or {}).get("upper_edge"),
                    "band_width_pct": (audit_row or {}).get("band_width_pct"),
                    "distinct_origin_count": (audit_row or {}).get("origin_count"),
                    "deeper_lowers_at_a": (audit_row or {}).get("deeper_active_lowers_at_a"),
                },
            )
            out.append(
                OverlayMarker(
                    overlay_id=oid_base,
                    symbol=sym,
                    timestamp=ts,
                    price=ev.get("entry_open_c"),
                    position="below",
                    shape="circle",
                    text="A",
                    size=7.0,
                    style=OverlayStyle(color="#3b82f6", width=1.0),
                    timeframe_scope=TIMEFRAME,
                    visible=True,
                    z_order=43,
                    metadata={
                        "origin": SOURCE,
                        "strategy_id": STRATEGY_ID,
                        "kind": "TOUCH_A",
                        "episode_cross_id": cross_id,
                        "pool_id": ev.get("pool_id"),
                        "realtime": True,
                        "tooltip": tooltip,
                    },
                )
            )
        elif kind == "CONFIRM_B":
            out.append(
                OverlayMarker(
                    overlay_id=oid_base,
                    symbol=sym,
                    timestamp=ts,
                    price=None,
                    position="below",
                    shape="square",
                    text="B",
                    size=7.0,
                    style=OverlayStyle(color="#22c55e", width=1.0),
                    timeframe_scope=TIMEFRAME,
                    visible=True,
                    z_order=44,
                    metadata={
                        "origin": SOURCE,
                        "strategy_id": STRATEGY_ID,
                        "kind": "CONFIRM_B",
                        "episode_cross_id": cross_id,
                        "touch_a": ev.get("touch_a"),
                        "realtime": True,
                        "tooltip": f"Bestätigung B · {bar}\nTouch A: {ev.get('touch_a')}",
                    },
                )
            )
        elif kind == "ENTRY_CANDIDATE":
            c_bar = str(ev.get("bar_c_close_utc") or bar)
            ts_c = ensure_utc(_open_ts(c_bar))
            out.append(
                OverlayMarker(
                    overlay_id=oid_base,
                    symbol=sym,
                    timestamp=ts_c,
                    price=ev.get("entry_open_c"),
                    position="above",
                    shape="arrow_up",
                    text="C?",
                    size=8.0,
                    style=OverlayStyle(color="#eab308", width=1.0),
                    timeframe_scope=TIMEFRAME,
                    visible=True,
                    z_order=45,
                    metadata={
                        "origin": SOURCE,
                        "strategy_id": STRATEGY_ID,
                        "kind": "ENTRY_CANDIDATE",
                        "episode_cross_id": cross_id,
                        "realtime": True,
                        "research_note": "Kandidat — kein Trade",
                        "tooltip": (
                            f"Kandidat C · Open {ev.get('entry_open_c')}\n"
                            f"A={ev.get('bar_a_close_utc')} B={ev.get('bar_b_close_utc')}\n"
                            f"Pool: {ev.get('pool_id')}"
                        ),
                    },
                )
            )

    summary = {
        "cross_id": cross_id,
        "cross_index": cross_index,
        "episode_id": episode_id,
        "overlay_count": len(out),
        "candidate_markers": sum(1 for e in ep_events if e.get("event") == "ENTRY_CANDIDATE"),
        "touch_markers": sum(1 for e in ep_events if e.get("event") == "TOUCH_A"),
        "minutes_to_next_cross": tl.get("minutes_to_next_cross"),
        "next_cross_exclusive_utc": tl.get("next_cross_exclusive_utc"),
    }
    return out, summary

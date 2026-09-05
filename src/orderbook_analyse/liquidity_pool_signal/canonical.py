"""Canonical chart pool provider — single SoT for dashboard, scanner, audit.

``pool_arrivals_v2.csv`` is an event/monitoring artifact only. Pool identity and
bounds must be resolved via ``export_snapshot(as_of=...)`` for every decision time.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from orderbook_analyse.liquidity_pool_signal.chart_pool_adapter import (
    DEFAULT_LIQUIDITY,
    DEFAULT_RESISTANCE_COLOR,
    DEFAULT_SUPPORT_COLOR,
    _iso_z,
    _load_chart_bindings,
    _utc,
    _unix,
    export_snapshot,
    fingerprint,
    normalize_pool_payload,
    run_chart_backend_lld,
)

CANONICAL_PROVIDER_VERSION = "liquidity_pool_signal/canonical_v1"
CANONICAL_RESTORE_ANCHOR_SHA = "9b8fe7cf1947d3b821d6ae4d1df2719ec94107f4"
POOL_ARRIVALS_CSV_NOT_POOL_SOURCE_OF_TRUTH = True

VERDICT_POOL_NOT_VISIBLE = "CANONICAL_POOL_NOT_VISIBLE_AS_OF"
VERDICT_BOUNDS_MISMATCH = "CANONICAL_POOL_BOUNDS_MISMATCH"
VERDICT_SIDE_MISMATCH = "CANONICAL_POOL_SIDE_MISMATCH"
VERDICT_TIMEFRAME_MISMATCH = "CANONICAL_POOL_TIMEFRAME_MISMATCH"
VERDICT_ASOF_MISMATCH = "CANONICAL_POOL_ASOF_MISMATCH"


class CanonicalPoolParityError(Exception):
    """Fail-closed when a case pool does not match canonical snapshot."""

    def __init__(self, verdict: str, detail: str = ""):
        self.verdict = verdict
        super().__init__(f"{verdict}: {detail}" if detail else verdict)


def parse_as_of_iso(raw: str) -> datetime:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("invalid_liquidity_location_as_of")
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid_liquidity_location_as_of") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def liquidity_settings_dict(raw: dict[str, Any] | None) -> dict[str, Any]:
    base = dict(DEFAULT_LIQUIDITY)
    if raw:
        base.update(raw)
    base["enabled"] = True
    return base


def overlay_fields_for_pool(
    serialized_overlays: list[dict[str, Any]], pool_id: str, *, as_of_unix: int
) -> dict[str, Any]:
    zone_id = f"{pool_id}:zone"
    for o in serialized_overlays:
        oid = str(o.get("id") or "")
        meta = o.get("metadata") or {}
        if oid == zone_id or meta.get("pool_id") == pool_id:
            st = o.get("start_timestamp")
            en = o.get("end_timestamp")
            ext = bool(o.get("extend_right"))
            visible = (st is None or int(st) <= as_of_unix) and (
                ext or (en is not None and int(en) >= as_of_unix)
            )
            return {
                "overlay_start_ts": st,
                "overlay_end_ts": en,
                "extend_right": ext,
                "overlay_visible_at_as_of": visible,
                "chart_color": (o.get("style") or {}).get("color"),
            }
    return {
        "overlay_start_ts": None,
        "overlay_end_ts": None,
        "extend_right": None,
        "overlay_visible_at_as_of": False,
        "chart_color": None,
    }


def canonical_pool_record(
    row: dict[str, Any],
    *,
    as_of: datetime,
    overlay_fields: dict[str, Any] | None = None,
    cluster_id: str | None = None,
) -> dict[str, Any]:
    ov = overlay_fields or {}
    side = row.get("side")
    color = ov.get("chart_color") or row.get("chart_color")
    if color is None:
        color = DEFAULT_RESISTANCE_COLOR if side == "ASK" else DEFAULT_SUPPORT_COLOR
    return {
        "pool_id": row["pool_id"],
        "symbol": row["symbol"],
        "timeframe": row["source_timeframe"],
        "side": side,
        "lower": float(row["lower_edge"]),
        "upper": float(row["upper_edge"]),
        "source_bar_ts": row.get("source_timestamp") or row.get("origin_ts"),
        "available_at": row["available_at"],
        "active_as_of": bool(row.get("active_as_of")),
        "invalidated_ts": row.get("invalidated_ts") or row.get("invalidated_at"),
        "cluster_component_id": cluster_id,
        "extend_right": ov.get("extend_right"),
        "overlay_start_ts": ov.get("overlay_start_ts"),
        "overlay_end_ts": ov.get("overlay_end_ts"),
        "overlay_visible_at_as_of": ov.get("overlay_visible_at_as_of"),
        "chart_color": color,
        "canonical_snapshot_as_of": _iso_z(as_of),
        "canonical_provider_version": CANONICAL_PROVIDER_VERSION,
    }


def clip_overlays_to_as_of(serialized: list[dict[str, Any]], as_of_unix: int) -> list[dict[str, Any]]:
    """Causal pane overlays: no zone may imply knowledge beyond as_of."""
    out: list[dict[str, Any]] = []
    for raw in serialized:
        o = dict(raw)
        meta = dict(o.get("metadata") or {})
        src = meta.get("source")
        if src not in ("lld", "lld-cluster"):
            out.append(o)
            continue
        st = o.get("start_timestamp")
        if st is not None and int(st) > as_of_unix:
            continue
        o["extend_right"] = False
        en = o.get("end_timestamp")
        if en is None or int(en) > as_of_unix:
            o["end_timestamp"] = as_of_unix
        meta["causal_as_of_unix"] = as_of_unix
        meta["causal_render"] = True
        meta["projected_after_as_of"] = False
        o["metadata"] = meta
        out.append(o)
    return out


def _pool_id_from_overlay(o: dict[str, Any]) -> str:
    meta = o.get("metadata") or {}
    pid = str(meta.get("pool_id") or "").strip()
    if pid:
        return pid
    oid = str(o.get("id") or "")
    if oid.endswith(":zone"):
        return oid[: -len(":zone")]
    return ""


def project_overlays_for_replay_window(
    clipped: list[dict[str, Any]],
    *,
    as_of_unix: int,
    render_end_unix: int,
    active_pool_ids: set[str] | frozenset[str],
) -> list[dict[str, Any]]:
    """Visual-only extension of active-as-of pools to the replay window end.

    Does not change canonical snapshot SHA (call only after SHA is computed).
    """
    if render_end_unix <= as_of_unix:
        return clipped
    active = {str(p) for p in active_pool_ids}
    out: list[dict[str, Any]] = []
    for raw in clipped:
        o = dict(raw)
        meta = dict(o.get("metadata") or {})
        src = meta.get("source")
        if src not in ("lld", "lld-cluster"):
            out.append(o)
            continue
        pool_id = _pool_id_from_overlay(o)
        if pool_id not in active:
            out.append(o)
            continue
        st = o.get("start_timestamp")
        if st is not None and int(st) > as_of_unix:
            continue
        o["extend_right"] = False
        o["end_timestamp"] = int(render_end_unix)
        meta["causal_as_of"] = _iso_z(datetime.fromtimestamp(as_of_unix, tz=timezone.utc))
        meta["projected_after_as_of"] = True
        meta["actual_data_end"] = int(as_of_unix)
        meta["render_end"] = int(render_end_unix)
        meta["causal_render_end"] = int(as_of_unix)
        style = dict(o.get("style") or {})
        if meta.get("projected_after_as_of"):
            style["border_style"] = "dashed"
            o["border_style"] = "dashed"
            o["opacity"] = min(float(o.get("opacity") or style.get("opacity") or 0.16), 0.14)
        o["style"] = style
        o["metadata"] = meta
        out.append(o)
    return out


def causal_pane_lld_bundle(
    *,
    symbol: str,
    timeframe: str,
    as_of: datetime,
    liquidity: dict[str, Any] | None = None,
    render_end: datetime | None = None,
) -> dict[str, Any]:
    """Dashboard causal LLD branch — same engine/settings as live, end=as_of."""
    from orderbook_analyse.liquidity_pool_signal.chart_pool_adapter import chart_lookback_start

    as_of_u = _utc(as_of)
    liq = liquidity_settings_dict(liquidity)
    snap = export_snapshot(
        symbol=symbol,
        timeframe=timeframe,
        window_start=as_of_u,
        as_of=as_of_u,
        liquidity=liq,
    )
    bundle = run_chart_backend_lld(
        symbol=symbol,
        timeframe=timeframe,
        start=chart_lookback_start(as_of_u, timeframe),
        end=as_of_u,
        liquidity=liq,
    )
    trp, _ = _load_chart_bindings()
    cfg = bundle["config"]
    as_of_unix = _unix(as_of_u)
    serialized = trp["serialize_overlays"](bundle["overlays"])
    clipped = clip_overlays_to_as_of(serialized, as_of_unix)

    canonical_rows = []
    for row in snap["pools"]:
        ov = overlay_fields_for_pool(clipped, row["pool_id"], as_of_unix=as_of_unix)
        canonical_rows.append(canonical_pool_record(row, as_of=as_of_u, overlay_fields=ov))

    active_canonical = [r for r in canonical_rows if r["active_as_of"]]
    pool_norm = normalize_pool_payload(snap["pools"])
    sha = fingerprint({"pools": pool_norm, "as_of": snap["as_of"], "provider": CANONICAL_PROVIDER_VERSION})

    render_end_unix = _unix(_utc(render_end)) if render_end is not None else as_of_unix
    active_ids = {str(r["pool_id"]) for r in active_canonical}
    display_overlays = project_overlays_for_replay_window(
        clipped,
        as_of_unix=as_of_unix,
        render_end_unix=render_end_unix,
        active_pool_ids=active_ids,
    )

    clusters = {"3": 0, "4-5": 0, "6+": 0}
    if cfg.clusters_enabled:
        clusters_raw = trp["cluster_pools"](
            bundle["engine_result"].pools,
            gap_pct=float(cfg.cluster_gap_pct),
            active_only=True,
        )
        shown = trp["filter_clusters"](clusters_raw, minimum_pools=int(cfg.minimum_cluster_pools))
        clusters = trp["cluster_bucket_counts"](shown)

    return {
        "overlays": display_overlays,
        "ema": trp["lld_ema_payload"](bundle["engine_result"], cfg),
        "clusters": clusters,
        "meta": {
            "mode": "causal_as_of",
            "liquidity_location_as_of": _iso_z(as_of_u),
            "render_end": _iso_z(_utc(render_end)) if render_end is not None else _iso_z(as_of_u),
            "visual_projection": render_end_unix > as_of_unix,
            "canonical_provider_version": CANONICAL_PROVIDER_VERSION,
            "canonical_snapshot_sha256": sha,
            "canonical_restore_anchor_sha": CANONICAL_RESTORE_ANCHOR_SHA,
            "n_active_pools": len(active_canonical),
        },
        "snapshot": snap,
        "canonical_pools": canonical_rows,
        "active_canonical_pools": active_canonical,
        "canonical_snapshot_sha256": sha,
    }


def normalized_pools_for_parity(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    keys = ("pool_id", "side", "timeframe", "lower", "upper", "available_at", "active_as_of")
    rows = []
    for p in snapshot.get("active_canonical_pools") or []:
        rows.append({k: p.get(k if k != "timeframe" else "timeframe") for k in keys})
    rows.sort(key=lambda r: (r["pool_id"], r["side"]))
    return rows


def assert_canonical_pool_parity(
    case_pool: dict[str, Any],
    canonical_snapshot: dict[str, Any],
    *,
    as_of: str,
) -> dict[str, Any]:
    """Fail-closed guardrail for Contract V3 (not wired into frozen V2 batch)."""
    snap_as_of = canonical_snapshot.get("as_of") or canonical_snapshot.get("meta", {}).get(
        "liquidity_location_as_of"
    )
    if snap_as_of and _iso_z(_utc(as_of)) != _iso_z(_utc(snap_as_of)):
        raise CanonicalPoolParityError(VERDICT_ASOF_MISMATCH, f"case as_of={as_of} snap={snap_as_of}")

    pool_id = str(case_pool.get("pool_id") or "")
    active = canonical_snapshot.get("active_canonical_pools") or canonical_snapshot.get("active_pools") or []
    by_id = {str(p["pool_id"]): p for p in active}
    canon = by_id.get(pool_id)
    if canon is None:
        # Also accept full canonical_pools list with active_as_of
        for p in canonical_snapshot.get("canonical_pools") or canonical_snapshot.get("pools") or []:
            if str(p.get("pool_id")) == pool_id and p.get("active_as_of"):
                canon = p
                break
    if canon is None:
        raise CanonicalPoolParityError(VERDICT_POOL_NOT_VISIBLE, pool_id)

    def _side(v: Any) -> str:
        return str(v or "").upper()

    if _side(case_pool.get("pool_side") or case_pool.get("side")) != _side(canon.get("side")):
        raise CanonicalPoolParityError(
            VERDICT_SIDE_MISMATCH,
            f"{case_pool.get('side')} != {canon.get('side')}",
        )

    tf_case = str(case_pool.get("pool_timeframe") or case_pool.get("timeframe") or "")
    tf_canon = str(canon.get("timeframe") or canon.get("source_timeframe") or "")
    if tf_case and tf_canon and tf_case != tf_canon:
        raise CanonicalPoolParityError(VERDICT_TIMEFRAME_MISMATCH, f"{tf_case} != {tf_canon}")

    lo = float(case_pool.get("pool_lower") or case_pool.get("pool_lower_edge") or case_pool.get("lower") or case_pool.get("lower_edge"))
    hi = float(case_pool.get("pool_upper") or case_pool.get("pool_upper_edge") or case_pool.get("upper") or case_pool.get("upper_edge"))
    if abs(lo - float(canon.get("lower") or canon.get("lower_edge"))) > 1e-6:
        raise CanonicalPoolParityError(VERDICT_BOUNDS_MISMATCH, f"lower {lo} != {canon.get('lower')}")
    if abs(hi - float(canon.get("upper") or canon.get("upper_edge"))) > 1e-6:
        raise CanonicalPoolParityError(VERDICT_BOUNDS_MISMATCH, f"upper {hi} != {canon.get('upper')}")

    if not bool(canon.get("active_as_of")):
        raise CanonicalPoolParityError(VERDICT_POOL_NOT_VISIBLE, "inactive in canonical snapshot")

    return {
        "ok": True,
        "pool_id": pool_id,
        "as_of": _iso_z(_utc(as_of)),
        "canonical_snapshot_sha256": canonical_snapshot.get("canonical_snapshot_sha256"),
    }

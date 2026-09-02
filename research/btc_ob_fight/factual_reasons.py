"""Deterministic factual reason codes (Phase 0–1 only)."""

from __future__ import annotations

from typing import Any

from .contracts import FORBIDDEN_REASON_CODES


def derive_factual_reason_codes(
    profile_facts: dict[str, Any],
    level_events: list[dict[str, Any]],
    trade_facts: dict[str, Any],
    wall_facts: list[dict[str, Any]],
    oi_liq_facts: dict[str, Any],
    volume_profile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    codes: list[dict[str, Any]] = []

    if profile_facts.get("inside_tpo_value_area") is True:
        codes.append(_code("ANCHOR_INSIDE_TPO_VALUE_AREA", profile_facts, ["price_at_anchor", "tpo_vah", "tpo_val"]))

    if volume_profile and volume_profile.get("volume_profile_status") == "COMPUTED_SEPARATELY":
        cov = volume_profile.get("coverage") or {}
        codes.append(
            _code(
                "VOLUME_PROFILE_COMPUTED_FROM_TRADES",
                {
                    "deduped_trade_rows_used": cov.get("deduped_trade_rows_used"),
                    "session_start_utc": cov.get("session_start_utc"),
                    "cutoff_utc": cov.get("cutoff_utc"),
                    "vpoc_price": (volume_profile.get("vpoc") or {}).get("vpoc_price"),
                },
                ["deduped_trade_rows_used", "session_start_utc", "cutoff_utc", "vpoc_price"],
            )
        )
        va = volume_profile.get("value_area") or {}
        codes.append(
            _code(
                "VOLUME_VALUE_AREA_COMPUTED",
                {
                    "vval": va.get("vval"),
                    "vvah": va.get("vvah"),
                    "actual_value_area_share": va.get("actual_value_area_share"),
                    "value_area_percentage": va.get("value_area_percentage"),
                },
                ["vval", "vvah", "actual_value_area_share", "value_area_percentage"],
            )
        )
        if profile_facts.get("inside_volume_value_area") is True:
            codes.append(
                _code(
                    "ANCHOR_INSIDE_VOLUME_VALUE_AREA",
                    profile_facts,
                    ["price_at_anchor", "volume_vah", "volume_val"],
                )
            )
        elif profile_facts.get("inside_volume_value_area") is False:
            codes.append(
                _code(
                    "ANCHOR_OUTSIDE_VOLUME_VALUE_AREA",
                    profile_facts,
                    ["price_at_anchor", "volume_vah", "volume_val"],
                )
            )
        for conf in profile_facts.get("tpo_volume_level_confluence") or []:
            if profile_facts.get("tpo_volume_confluence_status") != "VALID_INDEPENDENT_MEASURES":
                continue
            if conf.get("evaluation_status") != "EVALUATED":
                continue
            if conf.get("same_price_bin"):
                continue
            codes.append(
                _code(
                    "TPO_VOLUME_LEVEL_DISTANCE_OBSERVED",
                    conf,
                    ["tpo_kind", "volume_kind", "tpo_price", "volume_price", "distance_bps"],
                )
            )

    for wlabel, wf in _iter_trade_windows(trade_facts):
        delta = wf.get("delta_notional")
        if delta is not None and delta > 0:
            codes.append(
                _code("POSITIVE_TAKER_DELTA_OBSERVED", wf, ["delta_notional", "start_utc", "end_utc"], extra={"window": wlabel})
            )
        elif delta is not None and delta < 0:
            codes.append(
                _code("NEGATIVE_TAKER_DELTA_OBSERVED", wf, ["delta_notional", "start_utc", "end_utc"], extra={"window": wlabel})
            )
        bps = wf.get("price_change_bps")
        if bps is not None and bps > 0:
            codes.append(_code("PRICE_MOVED_UP_IN_WINDOW", wf, ["price_change_bps", "start_utc", "end_utc"], extra={"window": wlabel}))
        elif bps is not None and bps < 0:
            codes.append(_code("PRICE_MOVED_DOWN_IN_WINDOW", wf, ["price_change_bps", "start_utc", "end_utc"], extra={"window": wlabel}))

    for ev in level_events:
        if ev.get("first_touch_ts"):
            codes.append(
                _code(
                    "LEVEL_TOUCHED",
                    ev,
                    ["first_touch_ts", "price", "level_id"],
                    extra={"label": ev.get("label")},
                )
            )
        for ep in ev.get("episodes") or []:
            direction = ep.get("direction")
            complete = ep.get("complete")
            if complete and (ep.get("duration_seconds") or 0) <= 0:
                continue
            if direction == "ABOVE" and complete:
                codes.append(
                    _code(
                        "PROFILE_LEVEL_ABOVE_EPISODE_COMPLETE",
                        ep,
                        [
                            "episode_id",
                            "episode_index",
                            "level_id",
                            "level_price",
                            "start_ts",
                            "end_ts",
                            "duration_seconds",
                            "max_excursion_bps",
                        ],
                        extra={"label": ev.get("label")},
                    )
                )
            elif direction == "ABOVE" and not complete:
                codes.append(
                    _code(
                        "PROFILE_LEVEL_ABOVE_EPISODE_INCOMPLETE",
                        ep,
                        ["episode_id", "episode_index", "level_id", "level_price", "start_ts"],
                        extra={"label": ev.get("label")},
                    )
                )
            elif direction == "BELOW" and complete:
                codes.append(
                    _code(
                        "PROFILE_LEVEL_BELOW_EPISODE_COMPLETE",
                        ep,
                        [
                            "episode_id",
                            "episode_index",
                            "level_id",
                            "level_price",
                            "start_ts",
                            "end_ts",
                            "duration_seconds",
                            "max_excursion_bps",
                        ],
                        extra={"label": ev.get("label")},
                    )
                )
            elif direction == "BELOW" and not complete:
                codes.append(
                    _code(
                        "PROFILE_LEVEL_BELOW_EPISODE_INCOMPLETE",
                        ep,
                        ["episode_id", "episode_index", "level_id", "level_price", "start_ts"],
                        extra={"label": ev.get("label")},
                    )
                )

    for w in wall_facts:
        for he in w.get("heuristic_events") or []:
            hc = he.get("code")
            if hc == "HEURISTIC_TRADE_BACKED_REDUCTION":
                side = w["side"]
                code = (
                    "HEURISTIC_TRADE_BACKED_ASK_REDUCTION_OBSERVED"
                    if side == "ASK"
                    else "HEURISTIC_TRADE_BACKED_BID_REDUCTION_OBSERVED"
                )
                codes.append(_code(code, he, ["ts", "reduced_qty"], extra={"price": w["price"]}))
            elif hc == "HEURISTIC_WALL_PULLED_OR_CANCELLED":
                side = w["side"]
                code = (
                    "HEURISTIC_ASK_PULLING_OBSERVED"
                    if side == "ASK"
                    else "HEURISTIC_BID_PULLING_OBSERVED"
                )
                codes.append(_code(code, he, ["ts", "reduced_qty"], extra={"price": w["price"]}))

    oi_delta = oi_liq_facts.get("oi_delta")
    if oi_delta is not None and oi_delta > 0:
        codes.append(_code("OI_INCREASE_OBSERVED", oi_liq_facts, ["oi_first", "oi_last", "oi_delta", "oi_delta_pct", "oi_unit"]))
    elif oi_delta is not None and oi_delta < 0:
        codes.append(_code("OI_DECREASE_OBSERVED", oi_liq_facts, ["oi_first", "oi_last", "oi_delta", "oi_delta_pct", "oi_unit"]))
    if (oi_liq_facts.get("liquidation_count") or 0) > 0:
        codes.append(_code("LIQUIDATIONS_OBSERVED", oi_liq_facts, ["liquidation_count", "liquidation_summary"]))

    codes.sort(key=lambda c: (c["code"], str(c.get("fields"))))
    for c in codes:
        if c["code"] in FORBIDDEN_REASON_CODES:
            raise ValueError(f"forbidden reason code emitted: {c['code']}")
    return codes


def _code(code: str, source: dict[str, Any], fields: list[str], *, extra: dict | None = None) -> dict[str, Any]:
    payload = {f: source.get(f) for f in fields}
    if extra:
        payload.update(extra)
    return {"code": code, "fields": payload}


def _iter_trade_windows(trade_facts: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Yield unique time windows; prefer descriptive labels over generic keys."""
    priority = {
        "anchor_pre_30m": 0,
        "before_anchor": 1,
        "anchor_0_10m": 2,
        "anchor_0_30m": 3,
        "after_anchor": 4,
        "full": 5,
    }
    candidates: list[tuple[str, dict[str, Any]]] = []
    for key in ("before_window", "after_window", "full_window"):
        wf = trade_facts.get(key)
        if wf:
            label = wf.get("label") or key.replace("_window", "")
            candidates.append((label, wf))
    for w in trade_facts.get("relative_windows") or []:
        candidates.append((w.get("label") or "relative", w))
    seen: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
    for label, wf in candidates:
        key = (wf.get("start_utc") or "", wf.get("end_utc") or "")
        if key not in seen:
            seen[key] = (label, wf)
            continue
        existing_label, _ = seen[key]
        if priority.get(label, 99) < priority.get(existing_label, 99):
            seen[key] = (label, wf)
    out = sorted(seen.values(), key=lambda x: (x[1].get("start_utc") or "", x[0]))
    return out

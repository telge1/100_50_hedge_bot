"""Audit table / coverage / markdown writers for visual XRPUSDT review."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from .models import ConfirmationVariant, EventState, SetupDirection, SweepEvent

MANUAL_VERDICTS = (
    "MATCH",
    "FALSE_POSITIVE",
    "MISSED_EVENT",
    "WRONG_CLUSTER",
    "WRONG_TIMESTAMP",
    "LOOKAHEAD",
    "UNCLEAR",
    "INCONCLUSIVE_DATA",
)

REVIEW_FIELDS = (
    "manual_chart_verdict",
    "manual_reason",
    "expected_direction",
    "expected_confirmation_at",
    "reviewer_notes",
)


def final_status(event: SweepEvent) -> str:
    states = set(event.states)
    if EventState.INVALIDATED in states:
        return "INVALIDATED"
    # CONFIRMED only if a confirmation fired AND structure was ok at confirm
    fired_ok = False
    for v in ConfirmationVariant:
        info = event.confirmations.get(v.value) or {}
        if info.get("fired") and info.get("structure_ok_at_confirm", True):
            audit = (event.features or {}).get("ema_audit") or {}
            conf_ema = audit.get("confirmation") or {}
            if conf_ema and conf_ema.get("structure_ok") is False:
                continue
            fired_ok = True
            break
    if fired_ok:
        return "CONFIRMED"
    if EventState.EXPIRED in states:
        return "NO_CONFIRMATION"
    if EventState.CLUSTER_BREAK in states and not fired_ok:
        return "INVALIDATED"
    if not fired_ok:
        return "NO_CONFIRMATION"
    return "CONFIRMED"


def earliest_confirmation(event: SweepEvent) -> tuple[str | None, str | None]:
    best_t: datetime | None = None
    best_v: str | None = None
    for v in ConfirmationVariant:
        info = event.confirmations.get(v.value) or {}
        if not info.get("fired"):
            continue
        bt = info.get("bar_time")
        if bt is None:
            continue
        ts = pd.Timestamp(bt)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        dt = ts.to_pydatetime()
        if best_t is None or dt < best_t:
            best_t = dt
            best_v = v.value
    return best_v, None if best_t is None else best_t.isoformat()


def structure_flags(event: SweepEvent) -> dict[str, Any]:
    f = event.features or {}
    direction = event.setup_direction
    e9, e20, e59 = f.get("ema_9"), f.get("ema_20"), f.get("ema_59")
    px = f.get("close")
    out: dict[str, Any] = {
        "ema_9": e9,
        "ema_20": e20,
        "ema_59": e59,
        "ema_9_slope_1": f.get("ema_9_slope_1"),
        "ema_20_slope_1": f.get("ema_20_slope_1"),
        "ema_59_slope_1": f.get("ema_59_slope_1"),
        "gap_9_20": f.get("ema_9_20_gap"),
        "gap_9_59": f.get("ema_9_59_gap"),
        "gap_20_59": f.get("ema_20_59_gap"),
    }
    if None not in (e9, e20, e59):
        out["ema9_gt_ema59"] = bool(e9 > e59)
        out["ema20_gt_ema59"] = bool(e20 > e59)
        out["ema9_lt_ema59"] = bool(e9 < e59)
        out["ema20_lt_ema59"] = bool(e20 < e59)
    else:
        out["ema9_gt_ema59"] = None
        out["ema20_gt_ema59"] = None
        out["ema9_lt_ema59"] = None
        out["ema20_lt_ema59"] = None
    if px is not None and e59 is not None:
        out["price_below_ema59"] = bool(px < e59)
        out["price_above_ema59"] = bool(px > e59)
    else:
        out["price_below_ema59"] = None
        out["price_above_ema59"] = None
    if direction == SetupDirection.BULLISH:
        out["structure_ok"] = bool(f.get("ema_bull_stack"))
    else:
        out["structure_ok"] = bool(f.get("ema_bear_stack"))
    return out


def primary_outcome(event: SweepEvent) -> dict[str, Any]:
    """Pick first fired confirmation's h8 MFE/MAE if present (label-only)."""
    for v in ConfirmationVariant:
        oc = (event.outcomes or {}).get(v.value)
        if not oc or oc.get("status") == "NO_ENTRY_TIME":
            continue
        h = oc.get("h8") or oc.get("h4") or {}
        return {
            "confirmation_type": v.value,
            "entry_time": oc.get("entry_time"),
            "entry_price": oc.get("entry_price"),
            "mfe": h.get("mfe"),
            "mae": h.get("mae"),
            "t_mfe_bars": h.get("t_mfe_bars"),
            "t_mae_bars": h.get("t_mae_bars"),
            "outcome_horizon": "h8" if "h8" in oc else ("h4" if "h4" in oc else None),
            "tp_sl_sample": (h.get("tp_sl") or {}).get("tp0.01_sl0.005"),
        }
    return {
        "confirmation_type": None,
        "entry_time": None,
        "entry_price": None,
        "mfe": None,
        "mae": None,
        "t_mfe_bars": None,
        "t_mae_bars": None,
        "outcome_horizon": None,
        "tp_sl_sample": None,
    }


def event_audit_row(event: SweepEvent) -> dict[str, Any]:
    conf_type, conf_at = earliest_confirmation(event)
    oc = primary_outcome(event)
    sf = structure_flags(event)
    c = event.cluster
    age_min = (event.features or {}).get("cluster_age_bars_proxy_minutes")
    row: dict[str, Any] = {
        "event_id": event.event_id,
        "direction": event.setup_direction.value,
        "cluster_id": c.cluster_id,
        "cluster_side": c.side,
        "cluster_low": c.low,
        "cluster_high": c.high,
        "cluster_mid": c.mid,
        "cluster_pool_count": c.pool_count,
        "cluster_strength_mean": c.strength_mean,
        "cluster_strength_max": c.strength_max,
        "cluster_created_at": None if c.oldest_created is None else str(c.oldest_created),
        "cluster_as_of": None if event.t_first_touch is None else str(event.t_first_touch),
        "cluster_age_minutes_at_contact": age_min,
        "prior_touch_count": (event.features or {}).get("prior_touch_count"),
        "approach_at": None if event.t_approach is None else str(event.t_approach),
        "first_touch_at": None if event.t_first_touch is None else str(event.t_first_touch),
        "cluster_entry_at": None if event.t_entry is None else str(event.t_entry),
        "price_cross_ema59_at": (
            None if event.t_price_cross_ema59 is None else str(event.t_price_cross_ema59)
        ),
        "max_sweep_at": None if event.t_max_sweep is None else str(event.t_max_sweep),
        "confirmation_at": conf_at or (None if event.t_reclaim_or_reject is None else str(event.t_reclaim_or_reject)),
        "confirmation_type": conf_type or oc.get("confirmation_type"),
        "entry_at": oc.get("entry_time") or (None if event.t_earliest_entry is None else str(event.t_earliest_entry)),
        "entry_price": oc.get("entry_price"),
        "invalidated_at": None if event.t_invalidated is None else str(event.t_invalidated),
        "final_status": final_status(event),
        "states": "|".join(s.value for s in event.states),
        "orderflow_coverage": json.dumps(event.coverage or {}, default=str),
        "mfe": oc.get("mfe"),
        "mae": oc.get("mae"),
        "mfe_bars_after_entry": oc.get("t_mfe_bars"),
        "mae_bars_after_entry": oc.get("t_mae_bars"),
        "outcome_horizon": oc.get("outcome_horizon"),
        "tp_sl_first": oc.get("tp_sl_sample"),
        "confirmations_json": json.dumps(event.confirmations or {}, default=str),
        "orderflow_windows_json": json.dumps((event.features or {}).get("orderflow") or {}, default=str),
    }
    row.update(sf)
    for f in REVIEW_FIELDS:
        row[f] = ""
    return row


def write_audit_artifacts(
    out_dir: Path,
    events: Sequence[SweepEvent],
    *,
    coverage: dict[str, Any],
    meta: dict[str, Any],
    chart_path: Path | None = None,
) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    rows = [event_audit_row(e) for e in events]

    p_csv = out_dir / "events.csv"
    pd.DataFrame(rows).to_csv(p_csv, index=False)
    paths["events.csv"] = str(p_csv)

    payload = {
        "meta": meta,
        "manual_verdicts_allowed": list(MANUAL_VERDICTS),
        "n_events": len(events),
        "n_bullish": sum(1 for e in events if e.setup_direction == SetupDirection.BULLISH),
        "n_bearish": sum(1 for e in events if e.setup_direction == SetupDirection.BEARISH),
        "status_counts": _status_counts(events),
        "confirmation_variant_counts": _conf_counts(events),
        "events": rows,
        "coverage": coverage,
    }
    p_json = out_dir / "events.json"
    p_json.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    paths["events.json"] = str(p_json)

    p_cov = out_dir / "coverage.json"
    p_cov.write_text(json.dumps(coverage, indent=2, default=str) + "\n", encoding="utf-8")
    paths["coverage.json"] = str(p_cov)

    sc = payload["status_counts"]
    md = [
        f"# XRPUSDT 5m Cluster Sweep Visual Audit\n\n",
        f"- Symbol: `{meta.get('symbol')}` (exactly one)\n",
        f"- Timeframe: `{meta.get('timeframe')}`\n",
        f"- Window (UTC): `{meta.get('start')}` → `{meta.get('end')}`\n",
        f"- Window rationale: {meta.get('window_rationale', '')}\n",
        f"- LLD verdict: `{meta.get('lld_verdict')}`\n",
        f"- Events: {len(events)} "
        f"(bullish={payload['n_bullish']}, bearish={payload['n_bearish']})\n",
        f"- Status counts: `{sc}`\n",
        f"- Confirmation variants fired: `{payload['confirmation_variant_counts']}`\n",
        f"- Chart: `{chart_path or meta.get('chart_html', '')}`\n",
        "\n## Manual review\n\n",
        "Fill `manual_chart_verdict` / `manual_reason` / `expected_*` / `reviewer_notes` "
        "in CSV/JSON after visual inspection.\n\n",
        "Allowed verdicts: " + ", ".join(MANUAL_VERDICTS) + "\n\n",
        "**No automatic MATCH. No profitability claim.**\n",
        "\n## Coverage\n\n",
        "```json\n" + json.dumps(coverage, indent=2, default=str) + "\n```\n",
    ]
    p_md = out_dir / "audit.md"
    p_md.write_text("".join(md), encoding="utf-8")
    paths["audit.md"] = str(p_md)
    return paths


def _status_counts(events: Sequence[SweepEvent]) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in events:
        s = final_status(e)
        out[s] = out.get(s, 0) + 1
    return out


def _conf_counts(events: Sequence[SweepEvent]) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in events:
        for k, v in (e.confirmations or {}).items():
            if v.get("fired"):
                out[k] = out.get(k, 0) + 1
    return out

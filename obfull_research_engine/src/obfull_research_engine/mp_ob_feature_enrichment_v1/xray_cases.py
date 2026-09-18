"""Manual X-Ray case selection (fixed criteria, not cherry-picked by profit)."""

from __future__ import annotations

from typing import Any


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "y"}


def _f(v: Any) -> float | None:
    try:
        if v is None or v == "" or v == "None":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def select_manual_xray_cases(
    *,
    events: list[dict[str, str]],
    episodes: list[dict[str, str]],
    outcomes: list[dict[str, str]],
    feature_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    feat = {r["event_id"]: r for r in feature_rows}
    out1800 = {o["event_id"]: o for o in outcomes if str(o.get("horizon_s")) == "1800"}
    first = {e["event_id"] for e in episodes if _truthy(e.get("is_first_touch_of_zone_version"))}

    def score_event(e: dict[str, str]) -> tuple:
        conf = e.get("confluence_class") or ""
        conf_rank = 0 if conf.startswith("C3") else (1 if conf.startswith("C2") else 2)
        ft = 0 if e["event_id"] in first else 1
        return (ft, conf_rank, e.get("first_touch_ts_ns") or "")

    buckets: dict[tuple[str, bool], list] = {
        ("FAILED_BREAK", True): [],
        ("FAILED_BREAK", False): [],
        ("ABSORB", True): [],
        ("ABSORB", False): [],
        ("TRUE_BREAK", True): [],
        ("TRUE_BREAK", False): [],
    }
    for e in events:
        lab = e.get("label_price_only")
        if lab not in ("FAILED_BREAK", "ABSORB", "TRUE_BREAK"):
            continue
        o = out1800.get(e["event_id"])
        if not o or _truthy(o.get("is_censored")) or _truthy(o.get("censored")):
            continue
        g = _f(o.get("gross_return_bps"))
        if g is None:
            continue
        success = g > 0
        buckets[(lab, success)].append(e)

    rows: list[dict[str, Any]] = []
    lines = [
        "# Manual X-Ray Report",
        "",
        "Selection: first-touch preferred, then higher confluence, chronological.",
        "",
    ]
    for lab in ("FAILED_BREAK", "ABSORB", "TRUE_BREAK"):
        for success in (True, False):
            tag = "successful" if success else "unsuccessful"
            cands = sorted(buckets[(lab, success)], key=score_event)[:3]
            lines.append(f"## {lab} — {tag} (n={len(cands)})")
            lines.append("")
            for e in cands:
                fr = feat.get(e["event_id"], {})
                o = out1800[e["event_id"]]
                g = _f(o.get("gross_return_bps"))
                row = {
                    "case_group": f"{lab}_{'SUCCESS' if success else 'FAIL'}",
                    "event_id": e["event_id"],
                    "first_touch_ts_ns": e.get("first_touch_ts_ns"),
                    "mp_zone_low": e.get("confluence_low"),
                    "mp_zone_high": e.get("confluence_high"),
                    "event_role": e.get("event_role"),
                    "label_price_only": e.get("label_price_only"),
                    "trade_side": e.get("trade_side"),
                    "confluence_class": e.get("confluence_class"),
                    "depth_before_touch": fr.get("depth_at_zone_approach"),
                    "hit_qty": fr.get("hit_qty"),
                    "pull_qty": fr.get("pull_qty"),
                    "add_qty": fr.get("add_qty"),
                    "refill_ratio": fr.get("refill_ratio"),
                    "wall_present_contact_fraction": fr.get("wall_present_contact_fraction"),
                    "aggression_against_zone": fr.get("aggression_against_zone"),
                    "trigger_ts_ns": e.get("trigger_ts_ns"),
                    "mfe_bps_1800": o.get("mfe_bps_gross") or o.get("mfe_bps"),
                    "mae_bps_1800": o.get("mae_bps_gross") or o.get("mae_bps"),
                    "gross_return_bps_1800": o.get("gross_return_bps"),
                    "net_return_bps_1800_8bps": _f(o.get("net_return_bps_8"))
                    if _f(o.get("net_return_bps_8")) is not None
                    else (None if g is None else g - 8.0),
                }
                rows.append(row)
                lines.append(
                    f"- `{e['event_id']}` role={e.get('event_role')} conf={e.get('confluence_class')} "
                    f"depth_appr={fr.get('depth_at_zone_approach')} hit={fr.get('hit_qty')} "
                    f"pull={fr.get('pull_qty')} add={fr.get('add_qty')} refill={fr.get('refill_ratio')} "
                    f"agg={fr.get('aggression_against_zone')} "
                    f"MFE={o.get('mfe_bps_gross')} MAE={o.get('mae_bps_gross')} "
                    f"gross30m={o.get('gross_return_bps')}"
                )
            lines.append("")
    return {"rows": rows, "report_md": "\n".join(lines) + "\n"}

"""PILOT_REPORT.md writer."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Sequence

from obfull_research_engine.timeparse import format_utc_z

from .labels_price import LABEL_DEFINITIONS
from .params import PilotParams
from .schema import TouchEvent
from .util import format_ns_z


def choose_verdict(
    *,
    n_events: int,
    blocked: str | None = None,
    resource_abort: bool = False,
) -> str:
    if resource_abort:
        return "MP_PILOT_RESOURCE_ABORT"
    if blocked:
        if any(
            x in blocked
            for x in ("DATA", "COVERAGE", "EPOCH", "SILVER", "MP_PILOT_BLOCKED_DATA")
        ):
            return "MP_PILOT_BLOCKED_DATA"
        return "MP_PILOT_BLOCKED_IMPLEMENTATION"
    if n_events < 30:
        return "MP_PILOT_SUCCESS_LOW_SAMPLE"
    return "MP_PILOT_SUCCESS"


def pick_sample_events(events: Sequence[TouchEvent], n: int = 3) -> list[TouchEvent]:
    out: list[TouchEvent] = []
    for lab in ("ABSORB", "FAILED_BREAK", "TRUE_BREAK"):
        cands = [e for e in events if e.label == lab]
        cands.sort(
            key=lambda e: (
                0
                if e.confluence_class.startswith("C2") or e.confluence_class.startswith("C3")
                else 1,
                e.first_touch_ts_ns,
            )
        )
        out.extend(cands[:n])
    return out


def write_pilot_report(
    path: Path,
    *,
    params: PilotParams,
    result: dict[str, Any],
    verdict: str,
    sample_events: Sequence[TouchEvent] | None = None,
) -> None:
    events: list[TouchEvent] = result["events"]
    outcomes = result["outcomes"]
    profiles = result["profiles"]
    zones = result["zones"]
    runtime = result.get("runtime") or {}
    assessment = result.get("assessment") or {}
    by_tf = Counter(p.timeframe for p in profiles)
    by_cls = Counter(z.confluence_class for z in zones)
    event_cls = Counter(e.confluence_class for e in events)
    by_label = Counter(e.label for e in events)
    upper = sum(1 for e in events if e.event_role == "UPPER")
    lower = sum(1 for e in events if e.event_role == "LOWER")
    censored = sum(1 for e in events if e.is_censored)

    mfe_groups: dict[str, list[float]] = defaultdict(list)
    for o in outcomes:
        if o.outcome_status != "OK" or o.horizon_s != 300:
            continue
        if o.mfe_bps_gross is None:
            continue
        key = f"{o.label}|{o.fade_side}"
        mfe_groups[key].append(o.mfe_bps_gross)

    bucket: dict[str, Counter] = defaultdict(Counter)
    for o in outcomes:
        if o.outcome_status != "OK" or o.horizon_s != 300:
            continue
        for kk, vv in o.tp_sl_results.items():
            bucket[kk][vv] += 1
    tpsl = {k: dict(v) for k, v in bucket.items()}

    lines: list[str] = []
    lines.append("# MP Edge Event Pilot Report v1")
    lines.append("")
    lines.append(f"## 1. Verdict\n\n`{verdict}`\n")
    lines.append("## 2. Datenquellen\n")
    lines.append(f"- Silver-DB: `{params.silver_database}`")
    lines.append(f"- Metrics: `{params.silver_database}.ob_metrics_100ms_v1_3`")
    lines.append(f"- MP: previous_closed TPO VAH/VAL via `{params.profile_source}`")
    lines.append(f"- Edge-Definition: `{params.mp_edge_definition}`")
    lines.append(f"- Chain: `{assessment.get('chain_version', '')}`")
    lines.append("")
    lines.append("## 3. Kausalitätsprüfungen\n")
    lines.append("- Nur `previous_closed` Profile; `profile_available_ts = period_end`.")
    lines.append("- Hard assert: `profile_available_ts <= event_ts`.")
    lines.append("- Keine forming/final-for-parity Profile.")
    lines.append("")
    lines.append("## 4. Pilotfenster\n")
    lines.append(f"- Start: `{format_utc_z(params.start)}`")
    lines.append(f"- End: `{format_utc_z(params.end)}`")
    lines.append("")
    lines.append("## 5. Replay-Epoch\n")
    lines.append(f"- `{result.get('epoch_id')}`")
    lines.append("")
    lines.append("## 6. Parameter\n")
    lines.append("```json")
    lines.append(json.dumps(params.to_manifest_dict(), indent=2, sort_keys=True))
    lines.append("```\n")
    lines.append("## 7. Profile nach Timeframe\n")
    for tf in params.timeframes:
        lines.append(f"- {tf}: {by_tf.get(tf, 0)}")
    lines.append("")
    lines.append("## 8. Aktive Kanten\n")
    lines.append(f"- Level-Zeilen (alle Profile×UPPER/LOWER): {len(result['levels'])}")
    lines.append("")
    lines.append("## 9. Konfluenzzonen nach Klasse (Snapshots über Timeline)\n")
    for k, v in sorted(by_cls.items()):
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## 10. Touch-Events nach Konfluenzklasse\n")
    for k, v in sorted(event_cls.items()):
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## 11. Events UPPER/SHORT vs LOWER/LONG\n")
    lines.append(f"- UPPER/SHORT: {upper}")
    lines.append(f"- LOWER/LONG: {lower}")
    lines.append("")
    lines.append("## 12. Labels\n")
    for lab in ("ABSORB", "FAILED_BREAK", "TRUE_BREAK", "UNRESOLVED"):
        lines.append(f"- {lab}: {by_label.get(lab, 0)}")
    lines.append("")
    lines.append("### Label-Definitionen\n")
    for k, v in LABEL_DEFINITIONS.items():
        lines.append(f"- **{k}**: {v}")
    lines.append("")
    lines.append("## 13. Zensierte Events\n")
    lines.append(f"- n_censored: {censored}")
    for k, v in Counter(e.censor_reason for e in events if e.is_censored).items():
        lines.append(f"- {k or '(empty)'}: {v}")
    lines.append("")
    lines.append("## 14. MFE (gross) @300s nach Label|Richtung\n")
    if not mfe_groups:
        lines.append("- (keine OK-Outcomes @300s)")
    else:
        for k, vals in sorted(mfe_groups.items()):
            avg = sum(vals) / len(vals)
            lines.append(f"- {k}: n={len(vals)} mean_mfe_bps={avg:.2f}")
    lines.append("")
    lines.append("## 15. TP/SL-first (gross, horizon 300s)\n")
    lines.append("```json")
    lines.append(json.dumps(tpsl, indent=2, sort_keys=True))
    lines.append("```\n")
    lines.append("## 16. Vergleich Konfluenzgruppen (Event-Counts)\n")
    mapping = {
        "30m ohne höhere": "C1_30M",
        "30m + 1h": "C2_30M_1H",
        "30m + 4h": "C2_30M_4H",
        "1h + 4h": "C2_1H_4H",
        "30m + 1h + 4h": "C3_30M_1H_4H",
    }
    for title, cls in mapping.items():
        lines.append(f"- {title} (`{cls}`): {event_cls.get(cls, 0)}")
    lines.append("")
    lines.append("## 17. Laufzeit\n")
    lines.append(f"- elapsed_s: {runtime.get('elapsed_s')}")
    lines.append("")
    lines.append("## 18. Gelesene Silver-Zeilen\n")
    lines.append(f"- mids_read: {result.get('mids_read')}")
    lines.append(f"- mids_valid: {result.get('mids_valid')}")
    lines.append("")
    lines.append("## 19. Peak RAM\n")
    lines.append(f"- peak_rss_mib: {runtime.get('peak_rss_mib')}")
    lines.append("")
    lines.append("## 20. Warnungen / Datenqualität\n")
    warns = list(result.get("warnings") or [])
    lines.append("- keine" if not warns else "\n".join(f"- {w}" for w in warns))
    lines.append("")
    lines.append("## 21. Empfehlung nächster Schritt\n")
    if verdict.endswith("LOW_SAMPLE"):
        lines.append(
            "- Technisch OK, Stichprobe klein: weitere Single-Epoch-Fenster "
            "preisbasiert erweitern — noch kein LC/Hit-Pull, keine starken Trading-Aussagen."
        )
    else:
        lines.append(
            "- Pilot technisch erfolgreich. Nächste Single-Epoch-Fenster aggregieren; "
            "LC/Wall-Features erst nach stabilem Preis-Baseline."
        )
    lines.append("")
    lines.append("## Stichprobe (manuell prüfbar)\n")
    samples = list(sample_events or [])
    if not samples:
        lines.append("- (keine)")
    for e in samples:
        lines.append(
            f"- `{e.label}` {e.event_role}/{e.fade_side} class={e.confluence_class} "
            f"touch={format_ns_z(e.first_touch_ts_ns)} "
            f"zone=[{e.confluence_low:.2f},{e.confluence_high:.2f}] "
            f"pen={e.max_penetration_bps:.2f}bps reclaim_ts={e.reclaim_ts_ns} "
            f"trigger_ts={e.trigger_ts_ns} reason={e.trigger_reason}"
        )
    lines.append("")
    lines.append("## Confluence Tie-Break\n")
    lines.append(
        "Same-role only; sort by (price, timeframe 30m→1h→4h, level_id); "
        "greedy adjacent merge if bps(distance to cluster high) ≤ confluence_tolerance_bps; "
        "each level in exactly one cluster."
    )
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

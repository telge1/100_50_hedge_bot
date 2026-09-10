"""Markdown report for a materialization run."""

from __future__ import annotations

from typing import Any


def render_report(
    manifest: dict[str, Any],
    config: dict[str, Any],
    mp_rows: list[dict[str, Any]],
    lld_snaps: list[dict[str, Any]],
    lld_zones: list[dict[str, Any]],
    parity: dict[str, Any],
) -> str:
    lines = [
        "# MARKET_PROFILE_LLD_SHARED_EVENT_MATERIALIZATION_V1",
        "",
        f"Verdict: **{parity.get('verdict')}**",
        "",
        f"- run_key: `{manifest.get('run_key')}`",
        f"- status: `{manifest.get('status')}`",
        f"- shared generator parity: `{parity.get('shared_generator_parity')}`",
        f"- historical rendered chart payload parity: `{parity.get('historical_rendered_chart_payload_parity')}`",
        f"- ClickHouse writes: `{manifest.get('clickhouse_writes', 0)}`",
        "",
        "## Market Profile events",
        "",
    ]
    for row in mp_rows:
        ev = row["event"]
        lines.extend(
            [
                f"### {ev['case_id']} ({ev['timeframe']} {ev['profile_state']})",
                "",
                f"- request_as_of: {ev['request_as_of']}",
                f"- natural_profile_end: {ev['natural_profile_end']}",
                f"- effective_profile_end: {ev['effective_profile_end']}",
                f"- TPO POC/VAH/VAL: {ev['tpo_poc']} / {ev['tpo_vah']} / {ev['tpo_val']}",
                f"- available_at: {ev['available_at']}",
                f"- canonical_payload_hash: `{ev['canonical_payload_hash']}`",
                f"- raw_payload_hash: `{row['raw_payload_hash']}`",
                "",
            ]
        )
    lines.extend(["## LLD snapshots", ""])
    for snap in lld_snaps:
        s = snap["snapshot"]
        n = sum(1 for z in lld_zones if z["snapshot_id"] == s["snapshot_id"])
        lines.extend(
            [
                f"### {s['case_id']} as_of={s['liquidity_location_as_of']}",
                "",
                f"- active={s['zone_count_active']} invalidated={s['zone_count_invalidated']} stored_zones={n}",
                f"- engine end: {s['as_of_passed_to_engine_end']}",
                f"- snapshot hash: `{s['canonical_payload_hash']}`",
                "",
            ]
        )
    lines.extend(
        [
            "## Parity class",
            "",
            "`SHARED_GENERATOR_PARITY_PROVEN` — raw payloads come from the chart generators;",
            "normalized events copy TPO/LLD fields without a second formula.",
            "",
            "`HISTORICAL_RENDERED_CHART_PAYLOAD_PARITY_PROVEN` is still **not** claimed:",
            "Phase 1 stored no live chart GET/POST bodies.",
            "",
        ]
    )
    return "\n".join(lines) + "\n"

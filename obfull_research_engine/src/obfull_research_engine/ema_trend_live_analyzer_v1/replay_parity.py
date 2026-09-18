"""Live vs replay parity checks using continuous Full-OB archive helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orderbook_analyse.orderbook_v2_live.full_ob_continuous_raw_archive.replay import (
    iter_records,
    manifest_path_for,
)


@dataclass
class ParityResult:
    ok: bool
    differences: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "differences": self.differences, "stats": self.stats}


def compare_live_vs_archive(
    *,
    live_events: list[dict[str, Any]],
    archive_path: str | Path,
    live_features: dict[str, Any] | None = None,
    replay_features: dict[str, Any] | None = None,
    candidate_live: list[dict[str, Any]] | None = None,
    candidate_replay: list[dict[str, Any]] | None = None,
) -> ParityResult:
    path = Path(archive_path)
    diffs: list[dict[str, Any]] = []
    manifest_path = manifest_path_for(path)
    if not manifest_path.is_file():
        return ParityResult(
            ok=False, differences=[{"kind": "missing_manifest", "detail": str(manifest_path)}]
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format_version") != "full_ob_continuous_raw_archive_v1":
        diffs.append(
            {
                "kind": "unexpected_format_version",
                "got": manifest.get("format_version"),
                "expected": "full_ob_continuous_raw_archive_v1",
            }
        )

    archived = []
    for rec in iter_records(path):
        kind = str(rec.get("message_type") or "")
        if kind in {"snapshot", "delta", "checkpoint"}:
            archived.append(rec)

    live_valid = [
        e
        for e in live_events
        if e.get("valid_for_analysis", True)
        and e.get("event_type") in {"delta", "snapshot", None, "overflow"} is False
        or (
            e.get("valid_for_analysis", True)
            and str(e.get("event_type") or "") in {"delta", "snapshot"}
        )
    ]
    # simplify live_valid
    live_valid = [
        e
        for e in live_events
        if e.get("valid_for_analysis", True) and str(e.get("event_type") or "") in {"delta", "snapshot"}
    ]

    live_uids = [e.get("update_id") for e in live_valid if e.get("update_id") is not None]
    arch_uids = [r.get("u") for r in archived if r.get("u") is not None and r.get("message_type") in {"delta", "snapshot", "checkpoint"}]

    live_prog: list[int] = []
    last = None
    for u in live_uids:
        iu = int(u)
        if iu != last:
            live_prog.append(iu)
            last = iu
    arch_prog: list[int] = []
    last = None
    for u in arch_uids:
        iu = int(u)
        if iu != last:
            arch_prog.append(iu)
            last = iu

    # Archive starts with checkpoint u then deltas; live fanout is level-expanded.
    # Compare that every live progressive u appears in archive progressive u set.
    missing = [u for u in live_prog if u not in set(arch_prog)]
    if missing:
        diffs.append(
            {
                "kind": "live_update_ids_missing_in_archive",
                "missing_head": missing[:20],
                "live_head": live_prog[:10],
                "archive_head": arch_prog[:10],
            }
        )

    if live_features and replay_features:
        for key in ("mass", "qdh", "persistence_ratio", "impact_efficiency"):
            lv = live_features.get(key)
            rv = replay_features.get(key)
            if isinstance(lv, (int, float)) and isinstance(rv, (int, float)):
                if abs(float(lv) - float(rv)) > 1e-9:
                    diffs.append({"kind": "feature_mismatch", "field": key, "live": lv, "replay": rv})
            elif lv != rv:
                diffs.append({"kind": "feature_mismatch", "field": key, "live": lv, "replay": rv})

    if candidate_live is not None and candidate_replay is not None:
        live_states = [t.get("to") for t in candidate_live]
        rep_states = [t.get("to") for t in candidate_replay]
        if live_states != rep_states:
            diffs.append(
                {"kind": "candidate_parity_mismatch", "live": live_states, "replay": rep_states}
            )

    stats = {
        "live_event_count": len(live_events),
        "live_valid_count": len(live_valid),
        "archive_book_record_count": len(archived),
        "manifest_message_count": manifest.get("message_count"),
        "manifest_completion_status": manifest.get("completion_status"),
        "format_version": manifest.get("format_version"),
        "has_valid_anchor": manifest.get("has_valid_anchor"),
    }
    return ParityResult(ok=len(diffs) == 0, differences=diffs, stats=stats)


def write_parity_report(path: Path, result: ParityResult, extra: dict[str, Any] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "title": "LIVE_VS_REPLAY_PARITY_REPORT",
        "ok": result.ok,
        "differences": result.differences,
        "stats": result.stats,
        "extra": extra or {},
    }
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md = path.with_suffix(".md")
    lines = [
        "# LIVE_VS_REPLAY_PARITY_REPORT",
        "",
        f"**ok:** `{result.ok}`",
        "",
        "## Stats",
        "",
        "```json",
        json.dumps(result.stats, indent=2),
        "```",
        "",
        "## Differences",
        "",
    ]
    if not result.differences:
        lines.append("_none_")
    else:
        for d in result.differences:
            lines.append(f"- `{d.get('kind')}`: `{json.dumps(d)}`")
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")

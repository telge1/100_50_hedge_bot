"""Read-only Remaining candle-prefix audit for pilot pools (manifest + adapter rules)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from data.timeframes import timeframe_seconds

from ..phase11u_b.adapter import candle_window_status
from .pilot_config import (
    AUDIT_POOL_IDS,
    CATALOG_ROOT,
    PILOT_DECISION_CLOSE_STEMS,
    PILOT_POOL_DETAIL,
    PILOT_TIMEFRAME,
)

CoverageStatus = Literal[
    "COMPLETE_MANIFEST_BACKED",
    "COMPLETE_BAR_VERIFIED",
    "INCOMPLETE",
    "UNPROVEN",
]

FROZEN_MANIFESTS = {
    "84c22453fc6e2b5cc2791ab2301bdc600a9804632088a8e144b5007941e52c93": CATALOG_ROOT
    / "inputs"
    / "84c22453fc6e2b5cc2791ab2301bdc600a9804632088a8e144b5007941e52c93.manifest.json",
    "cfae2571806c823407767e4d81da82fe984a928ce29843c9f061fc66d79c115b": CATALOG_ROOT.parent
    / "doge_multi_month_2026_history"
    / "inputs"
    / "cfae2571806c823407767e4d81da82fe984a928ce29843c9f061fc66d79c115b.manifest.json",
}


@dataclass(frozen=True)
class PoolPrefixAudit:
    pool_id: str
    known_at: str
    max_decision_close: str
    frozen_input_id: str
    manifest_range_start: str
    manifest_range_end_exclusive: str
    adapter_status: str
    coverage: CoverageStatus
    notes: str


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _load_manifest(frozen_id: str) -> dict[str, Any]:
    path = FROZEN_MANIFESTS.get(frozen_id)
    if path is None or not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _bar_close_open(decision_close: datetime, timeframe: str) -> datetime:
    step = timeframe_seconds(timeframe)
    ts = int(_utc(decision_close).timestamp())
    first_1m = ts - step
    return datetime.fromtimestamp(first_1m, tz=timezone.utc)


def audit_pilot_pools(catalog_root: Path = CATALOG_ROOT) -> dict[str, Any]:
    combo = catalog_root / "production" / "results" / "2026-08" / "15m_PD10"
    pool_latest: dict[str, dict[str, Any]] = {}
    frozen_by_pool: dict[str, str] = {}

    for stem in PILOT_DECISION_CLOSE_STEMS:
        payload = json.loads((combo / f"{stem}.json").read_text(encoding="utf-8"))
        dc = _utc(datetime.fromisoformat(str(payload["decision_close"]).replace("Z", "+00:00")))
        fid = str((payload.get("frozen_input") or {}).get("snapshot_id") or "")
        for pid in payload.get("active_pool_ids") or []:
            row = (payload.get("pools") or {}).get(pid) or {}
            frozen_by_pool[pid] = fid
            prev = pool_latest.get(pid)
            if prev is None or dc > prev["decision_close"]:
                pool_latest[pid] = {
                    "known_at": row.get("known_at"),
                    "decision_close": dc,
                    "stem": stem,
                }

    audits: list[PoolPrefixAudit] = []
    for pid, meta in sorted(pool_latest.items()):
        known = _utc(datetime.fromisoformat(str(meta["known_at"]).replace("Z", "+00:00")))
        dc = meta["decision_close"]
        fid = frozen_by_pool.get(pid, "")
        manifest = _load_manifest(fid)
        if not manifest:
            audits.append(
                PoolPrefixAudit(
                    pool_id=pid,
                    known_at=str(meta["known_at"]),
                    max_decision_close=dc.isoformat(),
                    frozen_input_id=fid,
                    manifest_range_start="",
                    manifest_range_end_exclusive="",
                    adapter_status="UNPROVEN",
                    coverage="UNPROVEN",
                    notes="manifest file not loaded locally",
                )
            )
            continue
        rs = datetime.fromisoformat(str(manifest["range_start"]).replace("Z", "+00:00"))
        re = datetime.fromisoformat(str(manifest["range_end_exclusive"]).replace("Z", "+00:00"))
        status = candle_window_status(
            range_start=rs,
            range_end_exclusive=re,
            decision_close=dc,
            known_ats=[known],
            timeframe=PILOT_TIMEFRAME,
        )
        first_bar = _bar_close_open(dc, PILOT_TIMEFRAME)
        coverage: CoverageStatus = (
            "COMPLETE_MANIFEST_BACKED" if status == "READY" else "INCOMPLETE"
        )
        notes = f"first_bar_open={first_bar.isoformat()}; adapter={status}"
        if int(known.timestamp()) < int(rs.timestamp()):
            coverage = "INCOMPLETE"
            notes += "; known_at before manifest range_start"
        audits.append(
            PoolPrefixAudit(
                pool_id=pid,
                known_at=str(meta["known_at"]),
                max_decision_close=dc.isoformat(),
                frozen_input_id=fid,
                manifest_range_start=manifest["range_start"],
                manifest_range_end_exclusive=manifest["range_end_exclusive"],
                adapter_status=status,
                coverage=coverage,
                notes=notes,
            )
        )

    audit_only = [a for a in audits if a.pool_id in set(AUDIT_POOL_IDS)]
    incomplete = [a.pool_id for a in audits if a.coverage == "INCOMPLETE"]
    from .pilot_bar_verify import verify_pilot_frozen_inputs

    bar_report = verify_pilot_frozen_inputs()
    return {
        "unique_active_pools": len(pool_latest),
        "audit_pool_count": len(audit_only),
        "incomplete_count": len(incomplete),
        "audit_pools": [a.__dict__ for a in audit_only],
        "summary": {
            "COMPLETE_MANIFEST_BACKED": sum(
                1 for a in audits if a.coverage == "COMPLETE_MANIFEST_BACKED"
            ),
            "INCOMPLETE": sum(1 for a in audits if a.coverage == "INCOMPLETE"),
            "UNPROVEN": sum(1 for a in audits if a.coverage == "UNPROVEN"),
        },
        "pilot_bar_verification": bar_report,
        "verification_note": (
            "Per-pool rows are COMPLETE_MANIFEST_BACKED only; "
            "COMPLETE_BAR_VERIFIED applies to pilot_week frozen parquet checks in pilot_bar_verification."
        ),
    }


if __name__ == "__main__":
    import sys

    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root.parents[2]))
    sys.path.insert(0, str(root))
    from research_charts.trp_import import ensure_trp_on_path

    ensure_trp_on_path()
    print(json.dumps(audit_pilot_pools(), indent=2))

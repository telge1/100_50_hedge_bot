"""Select pilot decision-closes and audit pool IDs from the historical catalog."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research_charts.historical_lmv2.provider import combo_folder, decision_close_to_stem

from .pilot_config import (
    CATALOG_ROOT,
    PILOT_POOL_DETAIL,
    PILOT_RANGE_END_EXCLUSIVE,
    PILOT_RANGE_START,
    PILOT_TIMEFRAME,
)


@dataclass(frozen=True)
class PilotSelection:
    stems: tuple[str, ...]
    audit_pool_ids: tuple[str, ...]
    per_stem_active_count: dict[str, int]
    strength_classes: dict[str, str]
    notes: list[str]


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _load_payload(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _pool_meta(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "pool_id": str(row.get("pool_id") or ""),
        "side": str(row.get("side") or ""),
        "strength_class": str(row.get("strength_class") or ""),
        "known_at": str(row.get("known_at") or ""),
    }


def _diverse_audit_pools(pool_rows: dict[str, dict[str, Any]], limit: int = 20) -> list[str]:
    """Pick representative pool IDs from a union map pool_id → latest row."""
    buckets: dict[str, list[str]] = defaultdict(list)
    for pid, row in pool_rows.items():
        meta = _pool_meta(row)
        known = _utc(datetime.fromisoformat(meta["known_at"].replace("Z", "+00:00")))
        era = "pre_aug" if known < datetime(2026, 8, 1, tzinfo=timezone.utc) else "aug"
        key = f"{meta['side']}|{meta['strength_class']}|{era}"
        buckets[key].append(pid)

    chosen: list[str] = []
    keys = sorted(buckets.keys())
    idx = 0
    while len(chosen) < limit and keys:
        key = keys[idx % len(keys)]
        pool = buckets[key].pop(0)
        if pool not in chosen:
            chosen.append(pool)
        if not buckets[key]:
            keys.remove(key)
        idx += 1

    if len(chosen) < limit:
        for pid in sorted(pool_rows.keys()):
            if pid not in chosen:
                chosen.append(pid)
            if len(chosen) >= limit:
                break
    return chosen[:limit]


def select_pilot_closes(
    catalog_root: Path | None = None,
    *,
    target_audit_pools: int = 20,
    min_closes: int = 5,
    max_closes: int = 10,
) -> PilotSelection:
    root = (catalog_root or CATALOG_ROOT).resolve()
    combo = combo_folder(PILOT_TIMEFRAME, PILOT_POOL_DETAIL)
    start = _utc(datetime.fromisoformat(PILOT_RANGE_START.replace("Z", "+00:00")))
    end = _utc(datetime.fromisoformat(PILOT_RANGE_END_EXCLUSIVE.replace("Z", "+00:00")))
    month_dir = root / "production" / "results" / "2026-08" / combo
    paths = sorted(month_dir.glob("*.json"))
    candidates: list[tuple[datetime, Path, dict[str, Any]]] = []
    for path in paths:
        payload = _load_payload(path)
        dc = _utc(datetime.fromisoformat(str(payload["decision_close"]).replace("Z", "+00:00")))
        if dc < start or dc >= end:
            continue
        candidates.append((dc, path, payload))

    notes: list[str] = []
    if not candidates:
        raise RuntimeError(f"no snapshots under {month_dir} for pilot range")

    candidates.sort(key=lambda x: x[0])
    n = len(candidates)
    stride = max(1, n // max_closes)
    seed_indices = list(range(0, n, stride))[:max_closes]
    if len(seed_indices) < min_closes:
        seed_indices = list(range(min(min_closes, n)))

    chosen: list[tuple[datetime, Path, dict[str, Any]]] = [candidates[i] for i in seed_indices]
    union_rows: dict[str, dict[str, Any]] = {}
    for _, _, payload in chosen:
        for pid in payload.get("active_pool_ids") or []:
            union_rows[pid] = (payload.get("pools") or {}).get(pid) or {}

    # Add closes where ACTIVE membership changes vs previous pick (Jaccard drop).
    chosen_dcs = {c[0] for c in chosen}
    for dc, path, payload in candidates:
        if len(chosen) >= max_closes:
            break
        if dc in chosen_dcs:
            continue
        ids = set(payload.get("active_pool_ids") or [])
        if not ids:
            chosen.append((dc, path, payload))
            chosen_dcs.add(dc)
            notes.append(f"included empty ACTIVE snapshot {decision_close_to_stem(dc)}")
            continue
        overlap = max(
            len(ids & set(prev_ids))
            for _, _, prev in chosen
            for prev_ids in [prev.get("active_pool_ids") or []]
        )
        if overlap / max(len(ids), 1) < 0.98:
            chosen.append((dc, path, payload))
            chosen_dcs.add(dc)
            for pid in ids:
                union_rows[pid] = (payload.get("pools") or {}).get(pid) or {}

    chosen.sort(key=lambda x: x[0])
    stems = tuple(decision_close_to_stem(dc) for dc, _, _ in chosen)
    audit_ids = tuple(_diverse_audit_pools(union_rows, limit=target_audit_pools))
    per_stem = {
        decision_close_to_stem(dc): len(p.get("active_pool_ids") or [])
        for dc, _, p in chosen
    }
    strength_map = {
        pid: str((union_rows.get(pid) or {}).get("strength_class") or "")
        for pid in audit_ids
    }
    if len(chosen) < min_closes:
        notes.append(f"only {len(chosen)} closes selected (< {min_closes})")

    return PilotSelection(
        stems=stems,
        audit_pool_ids=audit_ids,
        per_stem_active_count=per_stem,
        strength_classes=strength_map,
        notes=notes,
    )


if __name__ == "__main__":
    import sys
    from pathlib import Path

    _dash = Path(__file__).resolve().parents[2]
    _repo = _dash.parents[2]
    for p in (_repo, _dash):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    from research_charts.trp_import import ensure_trp_on_path

    ensure_trp_on_path()
    sel = select_pilot_closes()
    print("stems", sel.stems)
    print("audit_count", len(sel.audit_pool_ids))
    print("per_stem", sel.per_stem_active_count)
    print("notes", sel.notes)

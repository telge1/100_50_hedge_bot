"""Catalog ↔ membership identity checks (pre-publish vs reader scope)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from ..active_snapshot.extract import ActiveStateSnapshot


class CatalogIdentityError(Exception):
    """Pool-ID set or catalog row integrity violation."""


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def validate_catalog_rows_in_memory(
    catalog_rows: Sequence[Mapping[str, Any]],
    state: ActiveStateSnapshot,
) -> None:
    """
    Pre-publish gate (import bundle): full field parity vs intended ACTIVE state.

    Uses import-time dicts (derived from original JSON).
    """
    expected_ids = set(state.active_pool_ids)
    seen: set[str] = set()
    for row in catalog_rows:
        pid = str(row["pool_id"])
        if pid in seen:
            raise CatalogIdentityError(f"duplicate catalog pool_id in import batch: {pid}")
        seen.add(pid)
    if len(catalog_rows) != len(expected_ids):
        raise CatalogIdentityError(
            f"catalog row count {len(catalog_rows)} != active_pool_count {len(expected_ids)}"
        )
        if pid not in expected_ids:
            raise CatalogIdentityError(f"unexpected catalog pool_id {pid}")
        side = str(row["side"])
        idx = state.active_pool_ids.index(pid)
        if side != state.active_sides[idx]:
            raise CatalogIdentityError(f"side mismatch for {pid}: catalog {side!r}")


def assert_member_catalog_id_sets(
    member_pool_ids: Sequence[str],
    catalog_pool_ids: Sequence[str],
) -> None:
    """
    Reader / post-write gate: set equality only (no JSON field parity).
    """
    if len(member_pool_ids) != len(set(member_pool_ids)):
        raise CatalogIdentityError("duplicate pool_id in member rows")
    if len(catalog_pool_ids) != len(set(catalog_pool_ids)):
        raise CatalogIdentityError("duplicate pool_id in catalog rows")
    mset = set(member_pool_ids)
    cset = set(catalog_pool_ids)
    if mset != cset:
        missing = sorted(mset - cset)
        extra = sorted(cset - mset)
        raise CatalogIdentityError(
            f"member/catalog pool_id mismatch missing={missing[:5]} extra={extra[:5]}"
        )


def load_catalog_pool_ids_from_ch(client, db: str, table: str, snapshot_id: str) -> list[str]:
    q = f"""
    SELECT pool_id FROM {db}.{table}
    WHERE snapshot_id = {{sid:String}}
    ORDER BY pool_id
    """
    return [str(r[0]) for r in client.query(q, parameters={"sid": snapshot_id}).result_rows]


def verify_ch_catalog_identity(
    client,
    db: str,
    table: str,
    snapshot_id: str,
    member_pool_ids: Sequence[str],
) -> None:
    """Post-catalog-write, pre-publish: CH catalog IDs must match members exactly."""
    cat_ids = load_catalog_pool_ids_from_ch(client, db, table, snapshot_id)
    assert_member_catalog_id_sets(member_pool_ids, cat_ids)


def compare_catalog_rows_to_json_fields(
    catalog_rows: Sequence[Mapping[str, Any]],
    json_pools_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """Full JSON field parity for readback / pre-write double-check."""
    mismatches: list[str] = []
    for row in catalog_rows:
        pid = str(row["pool_id"])
        src = json_pools_by_id.get(pid)
        if src is None:
            mismatches.append(f"missing JSON pool {pid}")
            continue
        for field in (
            "side",
            "top_price",
            "bottom_price",
            "strength",
            "strength_class",
            "row_fingerprint",
        ):
            jv, cv = src.get(field), row.get(field)
            if field in ("top_price", "bottom_price", "strength"):
                if float(jv) != float(cv):
                    mismatches.append(f"{pid}.{field}: json={jv} catalog={cv}")
            elif str(jv) != str(cv):
                mismatches.append(f"{pid}.{field}: json={jv!r} catalog={cv!r}")
        jk = str(src.get("known_at") or "")
        ck = row.get("known_at")
        if hasattr(ck, "isoformat"):
            ck = _utc(ck).isoformat()
        if jk.replace("Z", "+00:00") != str(ck).replace("Z", "+00:00"):
            mismatches.append(f"{pid}.known_at: json={jk} catalog={ck}")
    return mismatches

"""Compare a local publish artifact with an existing Phase-10 catalog row. Read-only."""

from __future__ import annotations

from ..serialize import _norm_iso


def _price(value) -> float:
    return round(float(value), 8)


def compare_published_close(local: dict, published: dict, catalog: list[dict]) -> list[str]:
    """Return concrete mismatches. Empty means the close matches the original publication."""
    errors: list[str] = []
    close = local.get("decision_close")
    if _norm_iso(local["decision_close"]) != _norm_iso(published["decision_close"]):
        errors.append(f"decision_close {close}")
    if local["active_state_fingerprint"] != published["active_state_fingerprint"]:
        errors.append(f"membership_checksum {close}")
    local_ids = list(local["active_pool_ids"])
    catalog_ids = [row["pool_id"] for row in catalog]
    if len(catalog_ids) != len(set(catalog_ids)):
        errors.append(f"duplicate_catalog_ids {close}")
    if sorted(local_ids) != sorted(catalog_ids):
        errors.append(f"catalog_membership {close}")
    if not catalog and local_ids:
        errors.append(f"missing_catalog {close}")
    by_id = {row["pool_id"]: row for row in catalog}
    pools = local.get("pools") or {}
    if set(pools) != set(local_ids):
        errors.append(f"local_pool_map {close}")
    for pool_id in local_ids:
        pool = pools.get(pool_id)
        row = by_id.get(pool_id)
        if pool is None or row is None:
            errors.append(f"missing_pool {pool_id}")
            continue
        if str(row["side"]).lower() != str(pool["side"]).lower():
            errors.append(f"side {pool_id}")
        if _price(row["top_price"]) != _price(pool["top_price"]) or _price(row["bottom_price"]) != _price(pool["bottom_price"]):
            errors.append(f"bounds {pool_id}")
        if _price(row["strength"]) != _price(pool["strength"]):
            errors.append(f"strength {pool_id}")
        if str(row.get("strength_class")) != str(pool.get("strength_class")):
            errors.append(f"strength_class {pool_id}")
        if _norm_iso(row["known_at"]) != _norm_iso(pool["known_at"]):
            errors.append(f"known_at {pool_id}")
        if str(row["row_fingerprint"]) != str(pool["row_fingerprint"]):
            errors.append(f"row_fingerprint {pool_id}")
    return errors

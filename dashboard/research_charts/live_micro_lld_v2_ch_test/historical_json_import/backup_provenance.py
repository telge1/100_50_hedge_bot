"""
ClickHouse backup verification and manifest/run-state provenance (read-only).

``scope_fingerprint`` does not include ``backup_recovery_id``; operational runs must
still record which backup was verified before writes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .controlled_run_manifest import ControlledRunManifest

VERIFIED_BACKUP_ENV = "HISTORICAL_IMPORT_VERIFIED_BACKUP_RECOVERY_ID"


class BackupProvenanceError(RuntimeError):
    """Backup reference missing or not BACKUP_CREATED in ClickHouse."""


@dataclass(frozen=True)
class BackupProvenanceRecord:
    manifest_backup_recovery_id: str
    verified_backup_recovery_id: str
    backup_status: str
    backup_name: str
    manifest_differs_from_verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_backup_recovery_id": self.manifest_backup_recovery_id,
            "verified_backup_recovery_id": self.verified_backup_recovery_id,
            "backup_status": self.backup_status,
            "backup_name": self.backup_name,
            "manifest_differs_from_verified": self.manifest_differs_from_verified,
        }


def fetch_backup_row(backup_id: str) -> dict[str, Any] | None:
    from .ch_import_state import ch_client_and_db

    client, _db = ch_client_and_db()
    try:
        rows = client.query(
            """
            SELECT id, name, status, num_files, total_size
            FROM system.backups
            WHERE id = {bid:String}
            """,
            parameters={"bid": backup_id},
        ).result_rows
    finally:
        client.close()
    if not rows:
        return None
    r = rows[0]
    return {
        "id": str(r[0]),
        "name": str(r[1]),
        "status": str(r[2]),
        "num_files": int(r[3]),
        "total_size": int(r[4]),
    }


def resolve_verified_backup_id(manifest: ControlledRunManifest) -> str:
    env_id = (os.environ.get(VERIFIED_BACKUP_ENV) or "").strip()
    if env_id:
        return env_id
    return manifest.backup_recovery_id


def verify_and_record_backup_provenance(
    manifest: ControlledRunManifest,
) -> BackupProvenanceRecord:
    """
    Fail closed when the resolved backup id is absent or not BACKUP_CREATED.

    When ``HISTORICAL_IMPORT_VERIFIED_BACKUP_RECOVERY_ID`` is set, it overrides the
    manifest id for verification (manifest id is still recorded for audit).
    """
    verified_id = resolve_verified_backup_id(manifest)
    if not verified_id:
        raise BackupProvenanceError("empty verified backup recovery id")
    row = fetch_backup_row(verified_id)
    if row is None:
        raise BackupProvenanceError(f"backup id not found in system.backups: {verified_id}")
    if row["status"] != "BACKUP_CREATED":
        raise BackupProvenanceError(
            f"backup {verified_id} status={row['status']} (expected BACKUP_CREATED)"
        )
    return BackupProvenanceRecord(
        manifest_backup_recovery_id=manifest.backup_recovery_id,
        verified_backup_recovery_id=verified_id,
        backup_status=row["status"],
        backup_name=row["name"],
        manifest_differs_from_verified=manifest.backup_recovery_id != verified_id,
    )

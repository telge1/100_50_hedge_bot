"""Shared mocks for historical import orchestrator integration tests."""

from __future__ import annotations

from unittest import mock

from research_charts.live_micro_lld_v2_ch_test.historical_json_import.backup_provenance import (
    BackupProvenanceRecord,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_run_manifest import (
    ControlledRunManifest,
)


def stub_backup_provenance_verification(monkeypatch: mock.Mock) -> None:
    def _verify(manifest: ControlledRunManifest) -> BackupProvenanceRecord:
        verified = "923fc087-9e6b-47f1-bbaf-37d765b1182a"
        return BackupProvenanceRecord(
            manifest_backup_recovery_id=manifest.backup_recovery_id,
            verified_backup_recovery_id=verified,
            backup_status="BACKUP_CREATED",
            backup_name="test-backup",
            manifest_differs_from_verified=manifest.backup_recovery_id != verified,
        )

    monkeypatch.setattr(
        "research_charts.live_micro_lld_v2_ch_test.historical_json_import."
        "controlled_monthly_orchestrator.verify_and_record_backup_provenance",
        _verify,
    )

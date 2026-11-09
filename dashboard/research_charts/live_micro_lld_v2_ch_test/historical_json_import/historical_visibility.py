"""
Historical vs live signal visibility (documentation only — no live ingest in Phase 4C).

Publish ledger rows make a decision_close *published* for a pipeline line.
Provenance sidecar rows (once deployed) mark *historical file integrity verified*
for that snapshot + line + batch.

Rules:
- **Legacy publications** (phase6/9/10/pilot without sidecar): remain readable via
  ``PublishedActiveReader``; sidecar is NOT required for read path today.
- **New historical batch imports** (Apr–Sep 2026): MUST persist sidecar BEFORE publish;
  missing sidecar after catalog = incomplete import (fail closed for new writes).
- **Live signal processing** (future): must filter on ingestion_mode / visibility_time
  so backfilled historical publishes are not treated as real-time signals.
- Phase 3E pilot reader: unchanged; pilot has publish without sidecar (legacy compat).

**Live causality:** Sidecar proves *which original JSON file* was imported at which batch time.
It does **not** prove the snapshot was available at the original ``decision_close`` for live
trading. Backfilled historical publishes must not be treated as retroactive live signals
(see ``LIVE_TREATS_BACKFILL_AS_SIGNAL``).
"""

LEGACY_PUBLISH_WITHOUT_SIDECAR = "ALLOWED_FOR_READ"
NEW_BATCH_PUBLISH_WITHOUT_SIDECAR = "FORBIDDEN"
LIVE_TREATS_BACKFILL_AS_SIGNAL = "FORBIDDEN_WITHOUT_VISIBILITY_GATE"

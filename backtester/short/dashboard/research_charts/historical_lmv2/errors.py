"""Fail-closed errors for historical Live Micro LLD V2 pool loading."""


class HistoricalLmV2Error(Exception):
    """Base error for historical LMV2 research backtester."""


class ManifestGateError(HistoricalLmV2Error):
    """Month or catalog not approved for backtester use."""


class FrozenInputMismatchError(HistoricalLmV2Error):
    """Snapshot frozen_input does not match catalog identity."""


class SnapshotMissingError(HistoricalLmV2Error):
    """No published snapshot for the requested decision close."""


class SnapshotValidationError(HistoricalLmV2Error):
    """Snapshot JSON failed structural or causality checks."""


class UnknownCatalogError(HistoricalLmV2Error):
    """catalog_id is not registered on this server."""


class RequestFrozenMismatchError(HistoricalLmV2Error):
    """Client expected_frozen_input_id does not match catalog."""


class ProvenanceMissingError(HistoricalLmV2Error):
    """Snapshot path has no provenance entry in the catalog lookup."""


class ProvenanceMismatchError(HistoricalLmV2Error):
    """Payload frozen_input does not match per-path provenance."""


class Sha256MismatchError(HistoricalLmV2Error):
    """On-disk snapshot file hash does not match provenance."""

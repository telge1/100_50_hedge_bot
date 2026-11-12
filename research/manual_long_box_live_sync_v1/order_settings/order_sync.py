"""Wire live adapter into existing ManualLongBoxSyncEngine without chart logic."""

from __future__ import annotations

from pathlib import Path

from research.manual_long_box_live_sync_v1.exchange_dry_run import DryRunExchangeAdapter
from research.manual_long_box_live_sync_v1.persistence import EventLog, StateStore
from research.manual_long_box_live_sync_v1.sync_engine import ManualLongBoxSyncEngine

from .config import LiveOrderSettings, load_settings
from .exchange_adapter import BybitLiveExchangeAdapter


def build_sync_engine(
    state_path: Path,
    event_log_path: Path,
    *,
    force_dry_run: bool = False,
    settings: LiveOrderSettings | None = None,
    exchange=None,
) -> tuple[ManualLongBoxSyncEngine, str]:
    """
    Returns (engine, mode_label).
    Default: DRY-RUN unless settings.writes_allowed and not force_dry_run.
    """
    settings = settings or load_settings()
    if force_dry_run or not settings.writes_allowed:
        ex = exchange or DryRunExchangeAdapter()
        eng = ManualLongBoxSyncEngine(
            ex,
            StateStore(state_path),
            EventLog(event_log_path),
            dry_run=True,
        )
        return eng, "dry_run"
    ex = exchange or BybitLiveExchangeAdapter.from_settings(settings)
    eng = ManualLongBoxSyncEngine(
        ex,
        StateStore(state_path),
        EventLog(event_log_path),
        dry_run=False,
    )
    return eng, "bybit_live"


def startup_reconcile_notes(state: StateStore) -> list[str]:
    """Do not auto-create from stale JSON — surface open local states for operator."""
    notes: list[str] = []
    for st in state.all_boxes():
        if st.order_status in ("FILLED", "CANCELLED"):
            continue
        notes.append(
            f"reconcile_required drawing={st.drawing_id} order={st.exchange_order_id} "
            f"status={st.order_status} entry={st.entry_price}"
        )
    return notes

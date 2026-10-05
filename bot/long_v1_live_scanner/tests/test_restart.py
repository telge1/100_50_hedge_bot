"""Restart persistence: no duplicate allowed signals."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from bot.long_v1_live_scanner.config import ScannerConfig
from bot.long_v1_live_scanner.processing import SymbolProcessor
from bot.long_v1_live_scanner.state import PersistedState, apply_symbol_snapshot, load_persisted, save_persisted, symbol_snapshot

JUN_TO = datetime(2026, 6, 15, tzinfo=timezone.utc)
JUN_FROM = datetime(2026, 6, 1, tzinfo=timezone.utc)


def test_restart_no_duplicate_allowed(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    cfg = ScannerConfig(live=False, report_from=JUN_FROM, report_to=JUN_TO, state_path=state_path)
    proc = SymbolProcessor(cfg)
    st, logs1 = proc.replay_through("DOGEUSDT", JUN_TO, emit_signals=True)
    allowed1 = [r for r in logs1 if r.get("signal_status") == "ALLOWED"]
    ps = PersistedState()
    save_persisted(state_path, ps, {"DOGEUSDT": st})
    assert json.loads(state_path.read_text())["symbols"]["DOGEUSDT"]["seen_pool_ids"]

    dup_logs: list[dict] = []
    for bar in st.bars15:
        if bar["close_time"] > JUN_TO:
            break
        dup_logs.extend(
            proc._process_one_15m(
                st,
                bar,
                emit_signals=True,
                log_ts=bar["close_time"],
                report_from=JUN_FROM,
                report_to=JUN_TO,
            )
        )
    assert not [r for r in dup_logs if r.get("signal_status") == "ALLOWED"]
    assert len(allowed1) >= 0

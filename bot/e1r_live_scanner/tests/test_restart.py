"""Warmup replay should reproduce E1R machine state."""

from __future__ import annotations

from datetime import datetime, timezone

from bot.e1r_live_scanner.config import ScannerConfig
from bot.e1r_live_scanner.parity import PANE_FROM, SIM_FROM, SIM_TO
from bot.e1r_live_scanner.processing import SymbolProcessor


def test_restart_e1r_machine_xrp() -> None:
    cfg = ScannerConfig(
        pane_from=PANE_FROM,
        e1r_sim_from=SIM_FROM,
        report_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        report_to=SIM_TO,
        live=False,
    )
    proc = SymbolProcessor(cfg)
    st1, _ = proc.replay_through("XRPUSDT", SIM_TO, emit_signals=False)
    snap1 = st1.e1r_engine.snapshot()

    st2, _ = proc.replay_through("XRPUSDT", SIM_TO, emit_signals=False)
    snap2 = st2.e1r_engine.snapshot()

    assert snap1["e1r_state"] == snap2["e1r_state"]
    assert snap1["e1r_machine"] == snap2["e1r_machine"]
    assert snap1["pending"] == snap2["pending"]

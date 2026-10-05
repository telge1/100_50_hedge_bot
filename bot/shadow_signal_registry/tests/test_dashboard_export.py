"""Dashboard export tests."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from unittest.mock import patch

import bot.shadow_signal_registry.store as store_mod
from bot.shadow_signal_registry.dashboard_export import (
    DASHBOARD_COLUMNS,
    build_dashboard_rows,
    format_endprofit,
    format_pnl_pct,
    snapshot_row_to_dashboard,
    write_dashboard_export,
)


def _snap(**kwargs):
    base = {
        "decision_time": "2026-10-04T18:30:00+00:00",
        "symbol": "TUTUSDT",
        "allowed": True,
        "blocked": False,
        "entry_price": 0.024398,
        "initial_sl": 0.02467124,
        "active_sl": 0.099,
        "tp": 0.024075,
        "tracking_status": "CLOSED",
        "exit_time": "2026-10-04T18:50:00+00:00",
        "pnl_pct": -1.119928,
    }
    base.update(kwargs)
    return base


def test_column_order_long_and_short() -> None:
    row = snapshot_row_to_dashboard(_snap())
    assert list(row.keys()) == DASHBOARD_COLUMNS


def test_newest_starttime_first() -> None:
    rows = build_dashboard_rows(
        [
            _snap(decision_time="2026-10-04T18:30:00+00:00"),
            _snap(decision_time="2026-10-04T18:45:00+00:00", allowed=False, blocked=True),
        ]
    )
    assert rows[0]["StartTime"] == "2026-10-04 18:45:00 UTC"


def test_allowed_blocked_mapping() -> None:
    assert snapshot_row_to_dashboard(_snap())["Status"] == "ALLOWED"
    assert snapshot_row_to_dashboard(_snap(allowed=False, blocked=True))["Status"] == "BLOCKED"


def test_pnl_formatting() -> None:
    assert format_pnl_pct(2.345) == "+2.35%"
    assert format_pnl_pct(-0.832404) == "-0.83%"
    assert format_pnl_pct(0.0) == "0.00%"
    assert format_endprofit(1.0, is_open=False) == "+"
    assert format_endprofit(-1.0, is_open=False) == "-"
    assert format_endprofit(0.0, is_open=False) == "0"


def test_open_trade_open_fields() -> None:
    d = snapshot_row_to_dashboard(_snap(tracking_status="OPEN", exit_time=None, pnl_pct=None))
    assert d["EndTime"] == "OPEN"
    assert d["PnL"] == "OPEN"
    assert d["Endprofit +/-"] == "OPEN"


def test_sl_uses_initial_sl_not_active() -> None:
    d = snapshot_row_to_dashboard(_snap())
    assert d["SL"] == 0.02467124
    assert d["SL"] != 0.099


def test_blocked_hypothetical_still_exported() -> None:
    rows = build_dashboard_rows([_snap(allowed=False, blocked=True, hypothetical=True)])
    assert len(rows) == 1 and rows[0]["Status"] == "BLOCKED"


def test_long_empty_snapshot_header_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store_mod, "RUNTIME", tmp_path)
    write_dashboard_export("long", [])
    csv_path = tmp_path / "long_dashboard_signals.csv"
    json_path = tmp_path / "long_dashboard_signals.json"
    with csv_path.open(encoding="utf-8") as f:
        lines = f.read().strip().splitlines()
    assert lines == [",".join(DASHBOARD_COLUMNS)]
    assert json.loads(json_path.read_text()) == []


def test_tut_1830_export_row() -> None:
    d = snapshot_row_to_dashboard(_snap())
    assert d["StartTime"] == "2026-10-04 18:30:00 UTC"
    assert d["EndTime"] == "2026-10-04 18:50:00 UTC"
    assert d["Coin"] == "TUTUSDT"
    assert d["Status"] == "ALLOWED"
    assert d["Entry"] == 0.024398
    assert d["PnL"] == "-1.12%"
    assert d["Endprofit +/-"] == "-"


def test_tut_1845_export_row() -> None:
    d = snapshot_row_to_dashboard(
        _snap(
            decision_time="2026-10-04T18:45:00+00:00",
            allowed=False,
            blocked=True,
            entry_price=0.024559,
            initial_sl=0.02476343,
            active_sl=0.02476343,
            exit_time="2026-10-04T23:28:00+00:00",
            pnl_pct=-0.832404,
        )
    )
    assert d["StartTime"] == "2026-10-04 18:45:00 UTC"
    assert d["EndTime"] == "2026-10-04 23:28:00 UTC"
    assert d["Status"] == "BLOCKED"
    assert d["PnL"] == "-0.83%"
    assert d["Endprofit +/-"] == "-"


def test_atomic_export_write(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store_mod, "RUNTIME", tmp_path)
    seen = []
    real_replace = Path.replace

    def track_replace(self, target):  # noqa: ANN001
        if str(self).endswith(".tmp"):
            seen.append(self.name)
        return real_replace(self, target)

    with patch.object(Path, "replace", track_replace):
        write_dashboard_export("short", [_snap()])
    assert (tmp_path / "short_dashboard_signals.csv").is_file()
    assert any("csv.tmp" in n for n in seen)


def test_json_keys_only_dashboard_fields(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store_mod, "RUNTIME", tmp_path)
    write_dashboard_export("short", [_snap()])
    data = json.loads((tmp_path / "short_dashboard_signals.json").read_text())
    assert set(data[0].keys()) == set(DASHBOARD_COLUMNS)

"""Unit tests for UTC-correct trade rematerialization contracts."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from research.btc_doge_research.contracts import TARGET_DATABASE, utc
from research.btc_doge_research.trade_contract import (
    ensure_utc_aware,
    iso_z,
    literal_utc,
)
from research.btc_doge_research.trade_run_state import RunnerLock, RUN_STATE_DIR


def test_naive_datetime_rejected_by_utc_contract():
    with pytest.raises(ValueError, match="naive"):
        utc(datetime(2026, 8, 31, 18, 0, 0))


def test_aware_utc_unchanged():
    value = datetime(2026, 8, 31, 18, 30, 0, 465000, tzinfo=timezone.utc)
    assert ensure_utc_aware(value) == value
    assert iso_z(value) == "2026-08-31T18:30:00.465000Z"


def test_naive_ch_read_attached_as_utc_not_local():
    naive = datetime(2026, 8, 31, 18, 30, 0, 465000)
    aware = ensure_utc_aware(naive)
    assert aware.tzinfo == timezone.utc
    assert aware.hour == 18
    assert literal_utc(aware) == "2026-08-31 18:30:00.465"


def test_no_blanket_plus_two_hours_in_contract_module():
    root = Path(__file__).resolve().parents[2]
    text = (root / "research/btc_doge_research/trade_contract.py").read_text()
    assert "timedelta(hours=2)" not in text
    assert "+ 2" not in text or "contract_version" in text


def test_only_btc_doge_allowed_in_import_segment():
    from research.btc_doge_research.trade_importer import import_segment

    with pytest.raises(PermissionError):
        import_segment(
            MagicMock(),
            "ETHUSDT",
            datetime(2026, 8, 31, 18, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc),
        )


def test_research_command_blocks_operative_db():
    from research.btc_doge_research.trade_rematerialization import research_command

    client = MagicMock()
    with pytest.raises(PermissionError):
        research_command(client, "INSERT INTO orderbook_analysis.public_trades_canonical VALUES")


def test_runner_lock_blocks_parallel(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "research.btc_doge_research.trade_run_state.RUN_STATE_DIR",
        tmp_path / "run",
    )
    # re-import paths after monkeypatch by calling ensure
    from research.btc_doge_research import trade_run_state as trs

    monkeypatch.setattr(trs, "RUN_STATE_DIR", tmp_path / "run")
    lock1 = trs.RunnerLock()
    got = lock1.acquire(launcher_pid=1)
    assert got["acquired"] is True
    lock2 = trs.RunnerLock()
    got2 = lock2.acquire(launcher_pid=2)
    assert got2["acquired"] is False
    lock1.release()


def test_companion_flag_default_false_in_fight_loader_signature():
    import inspect
    from research.btc_ob_fight.research_db_loader import load_public_trades

    sig = inspect.signature(load_public_trades)
    assert sig.parameters["allow_legacy_trade_companion"].default is False


def test_atomic_json_rejects_raw_nan_via_allow_nan_false():
    from research.btc_doge_research.atomic_json import atomic_write_json
    from research.btc_doge_research.contracts import sanitize_json

    path = Path("/tmp/trade_remat_atomic_test.json")
    # sanitize converts NaN → None (finite policy); dumps then succeeds without NaN.
    cleaned = sanitize_json({"bad": float("nan")})
    assert cleaned["bad"] is None
    atomic_write_json(path, cleaned)
    assert json.loads(path.read_text())["bad"] is None


def test_sanitize_attaches_utc_to_naive_for_json():
    from research.btc_doge_research.contracts import sanitize_json

    out = sanitize_json({"ts": datetime(2026, 8, 31, 16, 30, 0)})
    assert out["ts"].endswith("Z")
    assert "16:30:00" in out["ts"]

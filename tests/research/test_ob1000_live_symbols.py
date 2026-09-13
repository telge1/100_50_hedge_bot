"""OB1000 live symbol registry vs BTC/DOGE research pilot contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research.btc_doge_research.contracts import ALLOWED_SYMBOLS, validate_symbol
from research.btc_doge_research.ob1000_live_symbols import (
    load_ob1000_live_symbols,
    normalize_bybit_linear_usdt_symbol,
    partition_ob1000_live_symbols,
    validate_ob1000_live_symbol,
)
from research.btc_doge_research.ob1000_materializer_runner import main as runner_main
from research.btc_doge_research.ob200_parser import OB200SegmentReader
from research.btc_doge_research.source_file_registry import SourceFile
from research.btc_doge_research.contracts import parse_utc


def _cfg(tmp_path: Path, symbols: list[str]) -> Path:
    path = tmp_path / "ob1000_live_symbols.json"
    path.write_text(json.dumps({"symbols": symbols}), encoding="utf-8")
    return path


def test_pilot_contracts_remain_btc_doge_only() -> None:
    assert ALLOWED_SYMBOLS == frozenset({"BTCUSDT", "DOGEUSDT"})
    assert validate_symbol("BTCUSDT") == "BTCUSDT"
    assert validate_symbol("DOGEUSDT") == "DOGEUSDT"
    with pytest.raises(ValueError, match="unsupported symbol"):
        validate_symbol("NVDAUSDT")
    with pytest.raises(ValueError, match="unsupported symbol"):
        validate_symbol("ETHUSDT")


def test_ob1000_live_accepts_registered_nvda(tmp_path: Path) -> None:
    path = _cfg(tmp_path, ["BTCUSDT", "DOGEUSDT", "NVDAUSDT"])
    assert validate_ob1000_live_symbol("nvdaUSDT", path=path) == "NVDAUSDT"
    assert validate_ob1000_live_symbol("BTCUSDT", path=path) == "BTCUSDT"
    assert validate_ob1000_live_symbol("DOGEUSDT", path=path) == "DOGEUSDT"


def test_ob1000_live_rejects_unregistered_and_invalid(tmp_path: Path) -> None:
    path = _cfg(tmp_path, ["BTCUSDT", "DOGEUSDT"])
    with pytest.raises(ValueError, match="not registered"):
        validate_ob1000_live_symbol("NVDAUSDT", path=path)
    with pytest.raises(ValueError, match="syntax"):
        normalize_bybit_linear_usdt_symbol("nvda")
    with pytest.raises(ValueError, match="syntax"):
        validate_ob1000_live_symbol("BAD;DROP", path=path)


def test_partition_isolates_invalid_without_dropping_pilots(tmp_path: Path) -> None:
    path = _cfg(tmp_path, ["BTCUSDT", "DOGEUSDT", "NVDAUSDT"])
    accepted, rejected = partition_ob1000_live_symbols(
        ["BTCUSDT", "DOGEUSDT", "NOTAREAL", "NVDAUSDT"],
        path=path,
    )
    assert accepted == ("BTCUSDT", "DOGEUSDT", "NVDAUSDT")
    assert len(rejected) == 1
    assert rejected[0][0] == "NOTAREAL"


def test_runner_continues_with_valid_when_one_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _cfg(tmp_path, ["BTCUSDT", "DOGEUSDT"])
    monkeypatch.setenv("OB1000_SYMBOLS_FILE", str(path))

    def fake_run_once(**kwargs):  # noqa: ANN003
        assert kwargs["symbols"] == ("BTCUSDT", "DOGEUSDT")
        return {
            "segments": 0,
            "rows_inserted": 0,
            "lag": {"worst_lag_seconds": 0, "overall": "OK"},
        }

    monkeypatch.setattr(
        "research.btc_doge_research.ob1000_materializer_runner.run_once",
        fake_run_once,
    )
    monkeypatch.setattr(
        "research.btc_doge_research.ob1000_materializer_runner.ensure_run_dirs",
        lambda: None,
    )
    rc = runner_main(["--once", "--symbols", "BTCUSDT,DOGEUSDT,ETHUSDT", "--root", str(tmp_path)])
    assert rc == 0
    err = capsys.readouterr().out
    assert "REJECT symbol=ETHUSDT" in err
    assert "continuing with accepted" in err


def test_runner_exits_when_all_invalid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _cfg(tmp_path, ["BTCUSDT", "DOGEUSDT"])
    monkeypatch.setenv("OB1000_SYMBOLS_FILE", str(path))
    monkeypatch.setattr(
        "research.btc_doge_research.ob1000_materializer_runner.ensure_run_dirs",
        lambda: None,
    )
    with pytest.raises(SystemExit, match="no valid OB1000 live symbols"):
        runner_main(["--once", "--symbols", "ETHUSDT", "--root", str(tmp_path)])


def test_parser_default_still_rejects_nvda(tmp_path: Path) -> None:
    path = tmp_path / "x.ndjson"
    path.write_text("{}\n", encoding="utf-8")
    source = SourceFile(
        path=path,
        relative_path=path.name,
        fingerprint="a" * 64,
        source_file_id="b" * 64,
        size=path.stat().st_size,
        manifest={"format_version": "ob200_v3_live_archive/v1", "parser_version": "ob200_v3"},
        segment_start=parse_utc("2026-08-31T18:00:00Z"),
        segment_end=parse_utc("2026-08-31T19:00:00Z"),
    )
    with pytest.raises(ValueError, match="unsupported symbol"):
        OB200SegmentReader(source, "NVDAUSDT")
    reader = OB200SegmentReader(
        source,
        "NVDAUSDT",
        symbol_validator=lambda s: validate_ob1000_live_symbol(
            s, registered=frozenset({"NVDAUSDT"})
        ),
    )
    assert reader.symbol == "NVDAUSDT"


def test_load_ob1000_live_symbols(tmp_path: Path) -> None:
    path = _cfg(tmp_path, ["BTCUSDT", "dogeusdt", "NVDAUSDT", "BTCUSDT"])
    assert load_ob1000_live_symbols(path) == ("BTCUSDT", "DOGEUSDT", "NVDAUSDT")

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from stoch_fade_research_evaluations.artifacts import (
    SIDE_EFFECT_FLAGS,
    assemble_root_outcomes,
    combined_summary_from_rows,
    derive_evaluation_data_bounds,
    finalize_root_artifacts,
)
from stoch_universe_51.jsonio import write_json_atomic as atomic

CANARY = (
    Path(__file__).resolve().parents[3]
    / "results"
    / "stoch_fade_research_evaluations"
    / "633afb7ac1184f16a8aa6a40cd501649"
)
CANARY_HASHES = {
    "coin_runs/1000BONKUSDT/outcomes.jsonl": "9f2bc76b7751f32fc75ff3b7cbbd7c729de682c277530923ee7a5518e5ff8292",
    "coin_runs/1000BONKUSDT/summary.json": "fcb34b2e1925497c15eea33dadde1c4334c9fb7b6dcdd582cd87590821441832",
    "coin_runs/1000BONKUSDT/window.json": "fa2a025d6346ec6dca7c180923e799a12175604dbb8f3c7af9051745e797488e",
    "coin_runs/AAVEUSDT/outcomes.jsonl": "9349cc82a58585c64aeade7079b71755dc6f7c32a7a28b37dc23daf0880040e5",
    "coin_runs/AAVEUSDT/summary.json": "2b9d55e1dc6a2047d0c014c33684324db7e18e5f3eb99388367a024e1b5b2def",
    "coin_runs/AAVEUSDT/window.json": "3626c7ebbf8eb1c71ff4b731c58f358583fc293eaf118759345a32d20799eef6",
    "duplicate_audit.json": "06a99b06fcaf3f00a575418081e983d90f09844f9031f5e83f084358b3d87a9f",
    "evaluation_manifest.json": "266dea5dd8821978722f6f3be45f5259f06585456e319d5b9ff4b01413e44201",
    "per_symbol_summary.json": "e54b9b0dc18b36296b2f43e4f2fd15d2b70c3260e06433a55cf119b8a7c3f9c3",
    "progress.json": "55cb3d82d7f482defd8c78817ec02511d413527ef1edfec40a736124d00f34da",
    "request.json": "b7642ca75622c86975ca40c017b017c5bade90be81e09b7620c3ba97c8138ac6",
    "snapshot_after.json": "1d49eb18a8789afa09e5be128f4edf08e0361f652de9d5a40c3da63800258a6f",
    "snapshot_before.json": "691b333c35fdd3aaa06755a11de8d60afa82b08b23898112e762293ab6c04f73",
    "source_index.json": "7c0a13d940a755372ab51811f1f1cb98b90613243615f5d3f2c16092dbe75988",
    "status.json": "51199639babd9d392713d112d4b28f5368ca56f8fb816d48670d9531107496bb",
    "summary.json": "1deb95c5c5c7a211a2420bff56c3b43a1dc23b5e51cb26d02684195b628afdcd",
    "worker.log": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_canary_folder_untouched():
    assert CANARY.is_dir()
    names = sorted(p.relative_to(CANARY).as_posix() for p in CANARY.rglob("*") if p.is_file())
    assert names == sorted(CANARY_HASHES)
    for rel, expected in CANARY_HASHES.items():
        assert _sha(CANARY / rel) == expected
    assert not (CANARY / "outcomes.jsonl").exists()
    assert not (CANARY / "combined_summary.json").exists()


def _write_coin(root: Path, symbol: str, rows: list[dict], *, start: str, end: str, candle_to: str) -> None:
    d = root / "coin_runs" / symbol
    d.mkdir(parents=True)
    (d / "outcomes.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows),
        encoding="utf-8",
    )
    atomic(
        d / "window.json",
        {
            "symbol": symbol,
            "evaluation_data_start": start,
            "evaluation_data_end": end,
            "candle_rows": 3,
        },
    )
    atomic(
        d / "summary.json",
        {
            "symbol": symbol,
            "signals": len(rows),
            "evaluation_data_start": start,
            "evaluation_data_end": end,
            "identity": {"candle_data_to": candle_to},
        },
    )


def test_finalize_root_contract(tmp_path):
    directory = tmp_path / "eval"
    directory.mkdir()
    atomic(
        directory / "evaluation_manifest.json",
        {
            "evaluation_data_start": "2026-08-01T00:00:00Z",
            "evaluation_data_end": None,
            "side_effect_flags": dict(SIDE_EFFECT_FLAGS),
        },
    )
    _write_coin(
        directory,
        "AAVEUSDT",
        [
            {
                "signal_id": "a1",
                "symbol": "AAVEUSDT",
                "timeframe": "15m",
                "direction": "LONG",
                "outcome": "LOSS",
                "pnl_pct_gross": -3.5,
                "exit_reason": "SL",
            },
            {
                "signal_id": "a1",
                "symbol": "AAVEUSDT",
                "outcome": "LOSS",
                "pnl_pct_gross": -3.5,
                "exit_reason": "SL",
            },
        ],
        start="2026-08-01T07:01:00Z",
        end="2026-08-11T08:02:00Z",
        candle_to="2026-08-11T08:01:00Z",
    )
    _write_coin(
        directory,
        "BONKUSDT",
        [
            {
                "signal_id": "b1",
                "symbol": "BONKUSDT",
                "timeframe": "30m",
                "direction": "SHORT",
                "outcome": "WIN",
                "pnl_pct_gross": 2.0,
                "exit_reason": "TP",
            },
            {
                "signal_id": "be-bad",
                "symbol": "BONKUSDT",
                "outcome": "BE / WIN",
                "pnl_pct_gross": 0.0,
                "exit_reason": "BE",
            },
        ],
        start="2026-08-01T08:00:00Z",
        end="2026-08-10T00:00:00Z",
        candle_to="2026-08-09T23:59:00Z",
    )
    coins = [
        {"symbol": "AAVEUSDT", "state": "COMPLETED"},
        {"symbol": "FAILUSDT", "state": "FAILED"},
        {"symbol": "BONKUSDT", "state": "COMPLETED"},
    ]
    combined = finalize_root_artifacts(directory, coins)
    root_rows = [
        json.loads(line)
        for line in (directory / "outcomes.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [r["signal_id"] for r in root_rows] == ["a1", "b1"]
    assert combined["signals"] == 2
    assert combined["wins"] == 1
    assert combined["losses"] == 1
    assert combined["open"] == 0
    assert combined["be50_activated_count"] == 0
    assert combined["exit_policy"] == "NO_BE50"
    assert combined["outcome_engine"] == "evaluate_signal_no_be50_full_1m"
    assert combined["intrabar_policy"] == "SL_FIRST"
    assert combined["side_effect_flags"] == SIDE_EFFECT_FLAGS
    assert (directory / "combined_summary.json").read_bytes() == (directory / "summary.json").read_bytes()
    manifest = json.loads((directory / "evaluation_manifest.json").read_text(encoding="utf-8"))
    assert manifest["evaluation_data_start"] == "2026-08-01T07:01:00Z"
    assert manifest["evaluation_data_end"] == "2026-08-11T08:01:00Z"
    assert manifest["evaluation_data_end_status"] is None
    assert list(manifest["side_effect_flags"]) == sorted(SIDE_EFFECT_FLAGS)


def test_missing_windows_stay_null(tmp_path):
    directory = tmp_path / "eval"
    directory.mkdir()
    start, end, reason = derive_evaluation_data_bounds(
        directory, [{"symbol": "X", "state": "COMPLETED"}]
    )
    assert start is None
    assert end is None
    assert reason == "NO_CANDLE_WINDOWS"


def test_assemble_skips_failed_coin(tmp_path):
    directory = tmp_path / "eval"
    _write_coin(
        directory,
        "AAVEUSDT",
        [{"signal_id": "ok", "symbol": "AAVEUSDT", "outcome": "WIN", "pnl_pct_gross": 1}],
        start="2026-08-01T00:00:00Z",
        end="2026-08-02T00:00:00Z",
        candle_to="2026-08-01T12:00:00Z",
    )
    _write_coin(
        directory,
        "FAILUSDT",
        [{"signal_id": "nope", "symbol": "FAILUSDT", "outcome": "WIN", "pnl_pct_gross": 1}],
        start="2026-08-01T00:00:00Z",
        end="2026-08-02T00:00:00Z",
        candle_to="2026-08-01T12:00:00Z",
    )
    rows = assemble_root_outcomes(
        directory,
        [
            {"symbol": "AAVEUSDT", "state": "COMPLETED"},
            {"symbol": "FAILUSDT", "state": "FAILED"},
        ],
    )
    assert [r["signal_id"] for r in rows] == ["ok"]
    summary = combined_summary_from_rows(rows, [])
    assert summary["signals"] == 1

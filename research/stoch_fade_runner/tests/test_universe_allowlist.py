from __future__ import annotations

import json
from pathlib import Path

import pytest

from research.stoch_fade_runner.cli import main
from research.stoch_fade_runner.universe import (
    EXPECTED_UNIVERSE_COUNT,
    FULL_51_RUN_FORBIDDEN,
    MULTI_SYMBOL_FORBIDDEN,
    SYMBOL_NOT_ALLOWLISTED,
    UNIVERSE_COUNT_MISMATCH,
    UNIVERSE_DUPLICATE_SYMBOL,
    UNIVERSE_FILE_MISSING,
    UNIVERSE_INVALID_JSON,
    UNIVERSE_INVALID_SCHEMA,
    UniverseConfigError,
    load_tradeable_universe,
    select_single_cli_symbol,
)

VALID = {
    "generated_at": "2026-08-11T14:20:05.664602+00:00",
    "selection_method": "coin_scanner_crypto_tradeability_pass",
    "source": "coin_scanner/results/coin_tradeability_scanner/passed_coins.txt",
    "symbols": [f"SYM{i:02d}USDT" for i in range(EXPECTED_UNIVERSE_COUNT)],
    "target_size": EXPECTED_UNIVERSE_COUNT,
}


def _write(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_authoritative_universe_has_51_unique_and_required_coins() -> None:
    uni = load_tradeable_universe()
    assert uni["count"] == 51
    assert len(uni["symbols"]) == 51
    assert len(uni["allowlist"]) == 51
    assert uni["target_size"] == 51
    assert uni["symbols"] == tuple(dict.fromkeys(uni["symbols"]))
    for coin in ("1000PEPEUSDT", "AAVEUSDT", "1000BONKUSDT"):
        assert coin in uni["allowlist"]
    assert "ACEUSDT" not in uni["allowlist"]
    assert uni["path"].endswith("universe_tradeable_51.json")


def test_select_accepts_universe_coins_and_rejects_outsiders() -> None:
    allow = load_tradeable_universe()["allowlist"]
    assert select_single_cli_symbol(["--symbol", "1000PEPEUSDT"], "1000PEPEUSDT", allow) == "1000PEPEUSDT"
    assert select_single_cli_symbol(["--symbol", "aaveusdt"], "aaveusdt", allow) == "AAVEUSDT"
    assert select_single_cli_symbol(["--symbol", "1000BONKUSDT"], "1000BONKUSDT", allow) == "1000BONKUSDT"
    with pytest.raises(UniverseConfigError, match=SYMBOL_NOT_ALLOWLISTED):
        select_single_cli_symbol(["--symbol", "ACEUSDT"], "ACEUSDT", allow)
    with pytest.raises(UniverseConfigError, match=FULL_51_RUN_FORBIDDEN):
        select_single_cli_symbol(["--symbol", "ALL"], "ALL", allow)
    with pytest.raises(UniverseConfigError, match=FULL_51_RUN_FORBIDDEN):
        select_single_cli_symbol(["--symbol", "*"], "*", allow)
    with pytest.raises(UniverseConfigError, match=MULTI_SYMBOL_FORBIDDEN):
        select_single_cli_symbol(["--symbol", "AAVEUSDT,BTCUSDT"], "AAVEUSDT,BTCUSDT", allow)
    with pytest.raises(UniverseConfigError, match=MULTI_SYMBOL_FORBIDDEN):
        select_single_cli_symbol(
            ["--symbol", "AAVEUSDT", "--symbol", "BTCUSDT"], "BTCUSDT", allow
        )


def test_cli_accepts_aave_and_rejects_ace_without_run_dir(tmp_path: Path, capsys) -> None:
    rc_ok = main(["--dry-run-empty", "--symbol", "AAVEUSDT", "--out-root", str(tmp_path)])
    assert rc_ok == 0
    assert any(tmp_path.iterdir())
    rc_bad = main(["--dry-run-empty", "--symbol", "ACEUSDT", "--out-root", str(tmp_path)])
    assert rc_bad == 2
    assert SYMBOL_NOT_ALLOWLISTED in capsys.readouterr().err
    rc_all = main(["--dry-run-empty", "--symbol", "ALL", "--out-root", str(tmp_path)])
    assert rc_all == 2
    rc_star = main(["--dry-run-empty", "--symbol", "*", "--out-root", str(tmp_path)])
    assert rc_star == 2
    rc_two = main(
        ["--dry-run-empty", "--symbol", "AAVEUSDT", "--symbol", "1000BONKUSDT", "--out-root", str(tmp_path)]
    )
    assert rc_two == 2
    dirs_after = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(dirs_after) == 1


def test_missing_and_invalid_universe_files(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    with pytest.raises(UniverseConfigError, match=UNIVERSE_FILE_MISSING):
        load_tradeable_universe(missing)
    bad_json = _write(tmp_path / "bad.json", None)
    bad_json.write_text("{not json", encoding="utf-8")
    with pytest.raises(UniverseConfigError, match=UNIVERSE_INVALID_JSON):
        load_tradeable_universe(bad_json)
    schema = dict(VALID)
    del schema["symbols"]
    with pytest.raises(UniverseConfigError, match=UNIVERSE_INVALID_SCHEMA):
        load_tradeable_universe(_write(tmp_path / "schema.json", schema))
    as_list = _write(tmp_path / "list.json", ["ETHUSDT"])
    with pytest.raises(UniverseConfigError, match=UNIVERSE_INVALID_SCHEMA):
        load_tradeable_universe(as_list)


def test_duplicate_and_count_mismatch(tmp_path: Path) -> None:
    dup = dict(VALID)
    dup["symbols"] = list(VALID["symbols"])
    dup["symbols"][1] = dup["symbols"][0]
    with pytest.raises(UniverseConfigError, match=UNIVERSE_DUPLICATE_SYMBOL):
        load_tradeable_universe(_write(tmp_path / "dup.json", dup))
    short = dict(VALID)
    short["symbols"] = VALID["symbols"][:50]
    with pytest.raises(UniverseConfigError, match=UNIVERSE_COUNT_MISMATCH):
        load_tradeable_universe(_write(tmp_path / "short.json", short))
    wrong_target = dict(VALID)
    wrong_target["target_size"] = 8
    with pytest.raises(UniverseConfigError, match=UNIVERSE_COUNT_MISMATCH):
        load_tradeable_universe(_write(tmp_path / "target.json", wrong_target))

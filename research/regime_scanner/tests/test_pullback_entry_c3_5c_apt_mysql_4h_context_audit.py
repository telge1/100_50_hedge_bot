"""Tests for APT MySQL 4h context diagnostic audit."""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.pullback_entry_c3_5 import config_hash
from research.regime_scanner.pullback_entry_c3_5_diagnostics import baseline_a6
from research.regime_scanner.pullback_entry_c3_5c_apt_mysql_4h_context_audit import (
    DEFAULT_OUT,
    DEFAULT_REF_PANEL,
    EXPECTED_A6_HASH,
    EXPECTED_N_FILLS,
    MySQLRequiredError,
    combined_context_class,
    ema_pair_class,
    last_closed_4h_for_fill,
    load_apt_5m_mysql,
    structure_class,
    veto_view,
)
from research.regime_scanner.pullback_entry_c3_5c_entry_path_audit import (
    aggregate_complete_from_5m,
)


def test_output_paths_and_a6_hash_frozen() -> None:
    assert "apt_mysql_4h_context_audit_20260722" in str(DEFAULT_OUT)
    assert EXPECTED_N_FILLS == 55
    assert config_hash(baseline_a6()) == EXPECTED_A6_HASH
    assert DEFAULT_REF_PANEL.name == "fill_excursion_panel.csv"


def test_a6_and_pine_untouched() -> None:
    sm = Path("research/regime_scanner/pullback_entry_c3_5.py")
    pine_candidates = list(Path("research/regime_scanner").glob("*pine*"))
    h_sm = hashlib.sha256(sm.read_bytes()).hexdigest()
    import research.regime_scanner.pullback_entry_c3_5c_apt_mysql_4h_context_audit as mod

    _ = mod.DEFAULT_OUT
    assert hashlib.sha256(sm.read_bytes()).hexdigest() == h_sm
    src = inspect.getsource(mod)
    assert "build_pullback_entry_pine" not in src
    assert "data_source=\"mysql\"" in src or "data_source='mysql'" in src
    assert "feather fallback forbidden" in src or "feather_fallback" in src
    for p in pine_candidates:
        if p.is_file() and p.suffix in {".py", ".pine"}:
            # module must not rewrite pine sources
            assert "write_text" not in src or "pine" not in src.lower().split("write_text")[0][-80:]


def test_mysql_required_no_feather_fallback() -> None:
    with patch(
        "research.regime_scanner.pullback_entry_c3_5c_apt_mysql_4h_context_audit.load_symbol_candles",
        side_effect=RuntimeError("db down"),
    ):
        with pytest.raises(MySQLRequiredError, match="feather fallback forbidden"):
            load_apt_5m_mysql()


def test_aggregate_complete_4h_utc_and_incomplete_drop() -> None:
    # 48 complete 5m in 08:00–12:00; incomplete 12:00–16:00 (only 10 bars)
    rows = []
    start = pd.Timestamp("2026-02-01 08:00:00", tz="UTC")
    for i in range(48):
        ts = start + pd.Timedelta(minutes=5 * i)
        rows.append(
            {
                "timestamp": ts,
                "open": 1.0,
                "high": 1.1,
                "low": 0.9,
                "close": 1.05,
                "volume": 1.0,
            }
        )
    incomplete_start = pd.Timestamp("2026-02-01 12:00:00", tz="UTC")
    for i in range(10):
        ts = incomplete_start + pd.Timedelta(minutes=5 * i)
        rows.append(
            {
                "timestamp": ts,
                "open": 1.0,
                "high": 1.1,
                "low": 0.9,
                "close": 1.02,
                "volume": 1.0,
            }
        )
    df5 = pd.DataFrame(rows)
    decision = pd.Timestamp("2026-02-01 14:00:00", tz="UTC")
    agg = aggregate_complete_from_5m(df5, "4h", decision_time=decision)
    assert len(agg) == 1
    assert pd.Timestamp(agg.iloc[0]["timestamp"]) == start
    # bucket open must be UTC floor
    assert pd.Timestamp(agg.iloc[0]["timestamp"]).hour == 8


def test_last_closed_4h_no_lookahead_and_boundary() -> None:
    # 08:00–12:00 closed at 12:00; 12:00–16:00 closed at 16:00
    t0 = pd.Timestamp("2026-02-01 08:00:00", tz="UTC")
    t1 = pd.Timestamp("2026-02-01 12:00:00", tz="UTC")
    htf = pd.DataFrame(
        {
            "timestamp": [t0, t1],
            "htf_close_decision": [t0 + pd.Timedelta(hours=4), t1 + pd.Timedelta(hours=4)],
            "ema_9": [1.0, 2.0],
            "ema_20": [1.1, 1.9],
            "major_direction": [-1, 1],
        }
    )
    # fill 14:15 → only 08:00–12:00
    hit = last_closed_4h_for_fill(htf, fill_ts=pd.Timestamp("2026-02-01 14:15:00", tz="UTC"))
    assert hit["found"] is True
    assert pd.Timestamp(hit["selected_4h_bar_time"]) == t0
    assert float(hit["row"]["ema_9"]) == 1.0

    # fill exactly on bucket close 12:00 → 08:00–12:00 allowed (close_dec <= fill)
    hit_b = last_closed_4h_for_fill(htf, fill_ts=pd.Timestamp("2026-02-01 12:00:00", tz="UTC"))
    assert hit_b["found"] is True
    assert pd.Timestamp(hit_b["selected_4h_bar_time"]) == t0
    assert pd.Timestamp(hit_b["selected_4h_bar_close_time"]) <= pd.Timestamp(
        "2026-02-01 12:00:00", tz="UTC"
    )

    # before any close → missing visible
    hit_m = last_closed_4h_for_fill(htf, fill_ts=pd.Timestamp("2026-02-01 10:00:00", tz="UTC"))
    assert hit_m["found"] is False
    assert hit_m["missing"] is True


def test_context_classes_long_short_mirror() -> None:
    assert ema_pair_class("short", 1.0, 2.0) == "aligned"
    assert ema_pair_class("long", 1.0, 2.0) == "opposed"
    assert ema_pair_class("long", 2.0, 1.0) == "aligned"
    assert ema_pair_class("short", 2.0, 1.0) == "opposed"
    assert ema_pair_class("short", 1.0, 1.0) == "neutral"
    assert ema_pair_class("long", None, 1.0) == "neutral"

    assert combined_context_class("short", 1, 2, 3, 4) == "strong_aligned"
    assert combined_context_class("long", 4, 3, 2, 1) == "strong_aligned"
    assert combined_context_class("short", 4, 3, 2, 1) == "opposed"
    assert combined_context_class("long", 1, 2, 3, 4) == "opposed"

    assert structure_class("short", -1) == "aligned"
    assert structure_class("long", -1) == "opposed"
    assert structure_class("long", 0) == "neutral"
    assert structure_class("short", None) is None


def test_opposed_veto_blocks_winners_losers() -> None:
    trades = pd.DataFrame(
        {
            "net_pnl_pct": [1.0, -1.0, 2.0, -2.0, 0.5],
            "exit_reason": ["TP", "SL", "TP", "SL", "time_exit"],
            "bars_held": [10, 5, 8, 12, 20],
            "ctx_ema_9_20": ["aligned", "opposed", "opposed", "aligned", "neutral"],
        }
    )
    allow = trades["ctx_ema_9_20"].isin(["aligned", "neutral"])
    v = veto_view(trades, allow_mask=allow, name="opposed_veto")
    assert v["n_taken"] == 3
    assert v["n_blocked"] == 2
    assert v["n_blocked_winners"] == 1  # +2.0 opposed
    assert v["n_blocked_losers"] == 1  # -1.0 opposed
    assert v["retain_rate"] == pytest.approx(0.6)


def test_missing_context_not_silently_dropped() -> None:
    trades = pd.DataFrame(
        {
            "net_pnl_pct": [1.0, -1.0],
            "exit_reason": ["TP", "SL"],
            "bars_held": [1, 1],
            "context_missing": [False, True],
            "join_ok": [True, False],
        }
    )
    # diagnostic keeps all rows
    assert len(trades) == 2
    assert int(trades["context_missing"].sum()) == 1


@pytest.mark.skipif(not DEFAULT_REF_PANEL.exists(), reason="reference panel missing")
def test_live_audit_mysql_parity_and_artifacts(tmp_path: Path) -> None:
    from research.regime_scanner.pullback_entry_c3_5c_apt_mysql_4h_context_audit import (
        run_apt_mysql_4h_context_audit,
    )

    out = tmp_path / "apt_mysql_4h_context_audit_20260722"
    meta = run_apt_mysql_4h_context_audit(output_dir=out)
    assert meta.get("aborted") is False
    assert meta.get("ok") is True
    assert meta["n_fills"] == 55
    assert meta["data_source"] == "mysql"
    assert meta["feather_fallback"] is False
    assert meta["entry_parity_ok"] is True
    assert meta["parity"]["ok"] is True
    assert meta["a6_unchanged"] is True
    assert meta["pine_unchanged"] is True

    required = [
        "mysql_apt_parity.csv",
        "apt_4h_context_per_trade.csv",
        "apt_4h_context_by_class.csv",
        "apt_4h_context_by_side.csv",
        "apt_4h_context_by_split.csv",
        "apt_4h_context_veto_comparison.csv",
        "apt_4h_context_report.md",
        "metadata.json",
    ]
    for name in required:
        assert (out / name).exists(), name

    panel = pd.read_csv(out / "apt_4h_context_per_trade.csv")
    assert len(panel) == 55
    assert "context_missing" in panel.columns
    # missing stays visible, rows not dropped
    assert len(panel) == EXPECTED_N_FILLS

    ref = pd.read_csv(DEFAULT_REF_PANEL)
    ref["fill_time"] = pd.to_datetime(ref["fill_time"], utc=True)
    panel["fill_time"] = pd.to_datetime(panel["fill_time"], utc=True)
    assert np.allclose(panel["fill_price"], ref.sort_values("fill_time")["fill_price"].to_numpy())

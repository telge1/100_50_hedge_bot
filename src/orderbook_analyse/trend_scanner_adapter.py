"""Read-only adapter for existing C3.4B protected_medium scanner.

Does not change scanner rules. Adds causal availability timestamps only.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pandas as pd

DEFAULT_SCANNER_ROOT = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev"
)
TF_MINUTES: dict[str, int] = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}
CANDLE_TF = pd.Timedelta(minutes=TF_MINUTES["5m"])


def ensure_scanner_on_path(scanner_root: Path | str = DEFAULT_SCANNER_ROOT) -> Path:
    root = Path(scanner_root).resolve()
    s = str(root)
    if s not in sys.path:
        sys.path.insert(0, s)
    return root


def scanner_audit_info(scanner_root: Path | str = DEFAULT_SCANNER_ROOT) -> dict[str, Any]:
    ensure_scanner_on_path(scanner_root)
    from research.regime_scanner.market_structure_c3_4b import (  # noqa: WPS433
        RESEARCH_MATRIX,
        ProtectedStructureConfig,
    )

    cfg = ProtectedStructureConfig.from_matrix_entry(RESEARCH_MATRIX[0])
    variant = getattr(cfg, "variant_name", None) or str(RESEARCH_MATRIX[0])
    return {
        "scanner_root": str(Path(scanner_root).resolve()),
        "module": "research.regime_scanner.market_structure_c3_4b",
        "entrypoints": [
            "ProtectedStructureConfig.from_matrix_entry",
            "apply_protected_structure",
            "enrich_indicators (pullback_entry_c3_5)",
        ],
        "sot_variant": variant,
        "config": {
            "variant_name": cfg.variant_name,
            "swing_sensitivity": cfg.swing_sensitivity,
            "break_mode": cfg.break_mode,
            "choch_mode": cfg.choch_mode,
            "lookback": cfg.lookback,
            "confirm_bars": cfg.confirm_bars,
            "choch_hold_bars": cfg.choch_hold_bars,
            "rule_spec_version": cfg.rule_spec_version,
        },
        "major_direction_semantics": {
            "1": "sticky_bullish_protected_structure",
            "-1": "sticky_bearish_protected_structure_HTF_BEAR",
            "0": "unknown_unclear_or_transition_blocked",
        },
        "causal_availability": (
            "Row i is for closed candle with open=timestamp; "
            "available_at = timestamp + TF minutes (parameterized via timeframe; default 5m). "
            "major_direction flips only after CHoCH hold confirmation, not on first external BOS. "
            "Micro swings confirm after confirm_bars (>=3 for protected_medium)."
        ),
        "fields_present": [
            "major_direction",
            "structure_strength",
            "protected_high",
            "protected_low",
            "external_bos_up",
            "external_bos_down",
            "internal_bos_up",
            "internal_bos_down",
            "choch_side",
            "new_micro_high",
            "new_micro_low",
            "protected_structure_state",
        ],
        "fields_not_present": [
            "scanner_confidence (numeric)",
            "ema_alignment (as trading gate — diagnostic only)",
            "htf_alignment on 5m path (use separate 4h attach if needed)",
        ],
    }


def load_ohlcv_feather(path: Path) -> pd.DataFrame:
    raw = pd.read_feather(path)
    cols = {c.lower(): c for c in raw.columns}
    if "date" not in cols:
        raise ValueError(f"expected date column in {path}, got {list(raw.columns)}")
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(raw[cols["date"]], utc=True),
            "open": pd.to_numeric(raw[cols["open"]], errors="coerce"),
            "high": pd.to_numeric(raw[cols["high"]], errors="coerce"),
            "low": pd.to_numeric(raw[cols["low"]], errors="coerce"),
            "close": pd.to_numeric(raw[cols["close"]], errors="coerce"),
            "volume": pd.to_numeric(raw[cols["volume"]], errors="coerce"),
        }
    )
    return df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)


def run_c34b_structure(
    ohlcv: pd.DataFrame,
    *,
    scanner_root: Path | str = DEFAULT_SCANNER_ROOT,
    timeframe: str = "5m",
) -> pd.DataFrame:
    """Causal bar-by-bar C3.4B protected_medium replay. Does not mutate rules.

    ``timeframe`` only parameterizes candle_close_ts / available_at (+ output label).
    Default ``5m`` keeps existing callers bit-identical on availability timestamps.
    """
    ensure_scanner_on_path(scanner_root)
    from research.regime_scanner.market_structure_c3_4b import (  # noqa: WPS433
        RESEARCH_MATRIX,
        ProtectedStructureConfig,
        apply_protected_structure,
    )
    from research.regime_scanner.pullback_entry_c3_5 import enrich_indicators  # noqa: WPS433

    tf_key = str(timeframe).strip().lower()
    if tf_key not in TF_MINUTES:
        raise ValueError(f"unsupported timeframe={timeframe!r}; known={sorted(TF_MINUTES)}")
    candle_tf = pd.Timedelta(minutes=TF_MINUTES[tf_key])

    need = ["timestamp", "open", "high", "low", "close", "volume"]
    missing = [c for c in need if c not in ohlcv.columns]
    if missing:
        raise ValueError(f"ohlcv missing {missing}")

    feat = enrich_indicators(ohlcv[need].copy())
    cfg = ProtectedStructureConfig.from_matrix_entry(RESEARCH_MATRIX[0])
    structure = apply_protected_structure(feat, cfg)
    structure = structure.copy()
    structure["candle_open_ts"] = pd.to_datetime(structure["timestamp"], utc=True)
    structure["candle_close_ts"] = structure["candle_open_ts"] + candle_tf
    structure["available_at"] = structure["candle_close_ts"]
    structure["timeframe"] = tf_key
    structure["trend_direction"] = structure["major_direction"].astype(int)
    structure["trend_state"] = structure["protected_structure_state"].astype(str)
    structure["regime"] = structure["trend_state"]
    structure["trend_strength"] = structure["structure_strength"].astype(str)
    structure["bullish_bos"] = structure["external_bos_up"].fillna(False).astype(bool)
    structure["bearish_bos"] = structure["external_bos_down"].fillna(False).astype(bool)
    # choch_side may be NaN / 'up' / 'down'
    cs = structure["choch_side"]
    structure["bullish_choch"] = cs.astype(str).str.lower().eq("up")
    structure["bearish_choch"] = cs.astype(str).str.lower().eq("down")
    structure["higher_high"] = structure.get("new_micro_high", False)
    structure["higher_low"] = False  # not directly exported; leave explicit
    structure["lower_high"] = False
    structure["lower_low"] = structure.get("new_micro_low", False)
    if "new_micro_high" in structure.columns:
        structure["higher_high"] = structure["new_micro_high"].fillna(False).astype(bool)
    if "new_micro_low" in structure.columns:
        structure["lower_low"] = structure["new_micro_low"].fillna(False).astype(bool)
    structure["ema_alignment"] = structure.get("structure_indicator_alignment", pd.NA)
    structure["htf_alignment"] = pd.NA  # 5m path; 4h attach not required for this audit
    structure["scanner_confidence"] = pd.NA
    structure["config_variant"] = cfg.variant_name
    return structure

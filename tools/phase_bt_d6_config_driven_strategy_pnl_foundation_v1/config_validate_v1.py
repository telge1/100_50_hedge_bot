"""Fail-closed validation for BT-D6 strategy/backtest JSON configs."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

CONFIG_VERSION = "bt_d6_strategy_backtest_config_v1"

REQUIRED_TOP = frozenset(
    {
        "config_version",
        "strategy_identity",
        "density",
        "short_entry",
        "risk",
        "take_profit",
        "pnl",
        "backtest",
    }
)


class ConfigValidationError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _reject_unknown_keys(obj: dict[str, Any], allowed: frozenset[str], path: str) -> None:
    extra = set(obj.keys()) - set(allowed)
    if extra:
        raise ConfigValidationError(
            "UNKNOWN_CONFIG_KEYS",
            f"unknown keys at {path}",
            {"unknown": sorted(extra)},
        )


def _require_keys(obj: dict[str, Any], required: frozenset[str], path: str) -> None:
    missing = required - set(obj.keys())
    if missing:
        raise ConfigValidationError(
            "MISSING_REQUIRED_KEYS",
            f"missing keys at {path}",
            {"missing": sorted(missing)},
        )


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def validate_config_dict(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigValidationError("INVALID_ROOT", "config root must be object")
    _reject_unknown_keys(raw, REQUIRED_TOP, "root")
    _require_keys(raw, REQUIRED_TOP, "root")
    if raw["config_version"] != CONFIG_VERSION:
        raise ConfigValidationError(
            "CONFIG_VERSION_MISMATCH",
            f"expected {CONFIG_VERSION}",
            {"got": raw["config_version"]},
        )

    si = raw["strategy_identity"]
    _reject_unknown_keys(
        si,
        frozenset({"strategy_id", "strategy_version", "frozen_source_reference", "research_mode"}),
        "strategy_identity",
    )
    _require_keys(
        si,
        frozenset({"strategy_id", "strategy_version", "frozen_source_reference", "research_mode"}),
        "strategy_identity",
    )
    if si["research_mode"] not in ("depth2_density_research_v1", "config_experiment_v1"):
        raise ConfigValidationError("INVALID_RESEARCH_MODE", str(si["research_mode"]))

    dens = raw["density"]
    _reject_unknown_keys(
        dens,
        frozenset(
            {
                "timeframe",
                "pool_detail",
                "method",
                "min_unique_origins",
                "density_source",
                "validated_cache",
                "published_clickhouse",
            }
        ),
        "density",
    )
    if dens["timeframe"] != "5m":
        raise ConfigValidationError("DENSITY_TIMEFRAME_LOCKED", "only 5m allowed in v1")
    if dens["pool_detail"] != 1:
        raise ConfigValidationError("DENSITY_POOL_DETAIL_LOCKED", "only pool_detail=1")
    if dens["method"] != "exact_unique_origin":
        raise ConfigValidationError("DENSITY_METHOD_LOCKED", "only exact_unique_origin")
    if dens["min_unique_origins"] != 2:
        raise ConfigValidationError("DENSITY_MIN_ORIGINS_LOCKED", "only min_unique_origins=2")

    density_source = dens.get("density_source", "canonical_provider")
    if density_source not in ("canonical_provider", "validated_cache", "published_clickhouse"):
        raise ConfigValidationError(
            "INVALID_DENSITY_SOURCE",
            "density_source must be canonical_provider, validated_cache, or published_clickhouse",
            {"got": density_source},
        )
    if density_source == "published_clickhouse":
        si = raw.get("strategy_identity") or {}
        if si.get("research_mode") != "depth2_density_research_v1":
            raise ConfigValidationError(
                "PUBLISHED_CH_RESEARCH_ONLY",
                "published_clickhouse requires strategy_identity.research_mode=depth2_density_research_v1",
            )
        pc = dens.get("published_clickhouse")
        if pc is not None and not isinstance(pc, dict):
            raise ConfigValidationError("PUBLISHED_CH_INVALID", "published_clickhouse must be object or omitted")
        if isinstance(pc, dict):
            _reject_unknown_keys(pc, frozenset({"require_pipeline_line_id"}), "density.published_clickhouse")
    if density_source == "validated_cache":
        vc = dens.get("validated_cache")
        if not isinstance(vc, dict):
            raise ConfigValidationError(
                "VALIDATED_CACHE_MISSING",
                "validated_cache object required when density_source=validated_cache",
            )
        _reject_unknown_keys(
            vc,
            frozenset({"bundle_path", "required_config_hash"}),
            "density.validated_cache",
        )
        _require_keys(vc, frozenset({"bundle_path"}), "density.validated_cache")
        if not str(vc.get("bundle_path") or "").strip():
            raise ConfigValidationError("VALIDATED_CACHE_PATH_EMPTY", "bundle_path required")

    se = raw["short_entry"]
    se_keys = frozenset(
        {
            "entry_pool_selection_mode",
            "entry_price_reference",
            "entry_offset_pct",
            "pool_min_age_bars",
            "upper_gap_min_pct",
            "upper_gap_measurement_mode",
            "upper_pool_proximity_pct",
            "pending_limit_enabled",
            "pending_limit_expiry_bars",
            "pending_limit_fill_model",
            "pool_cancel_identity_mode",
        }
    )
    _reject_unknown_keys(se, se_keys, "short_entry")
    _require_keys(
        se,
        se_keys - frozenset({"entry_offset_pct", "pending_limit_fill_model", "pool_cancel_identity_mode"}),
        "short_entry",
    )
    fill_model = str(se.get("pending_limit_fill_model") or "legacy_v1")
    if fill_model not in ("legacy_v1", "causal_sell_limit_v2"):
        raise ConfigValidationError("INVALID_PENDING_LIMIT_FILL_MODEL", fill_model)
    ref = str(se["entry_price_reference"])
    if ref not in ("upper_pool_lower_edge", "upper_pool_upper_edge"):
        raise ConfigValidationError("INVALID_ENTRY_PRICE_REFERENCE", ref)
    offset_raw = se.get("entry_offset_pct")
    offset = 0.0 if offset_raw is None else float(offset_raw)
    if offset < 0:
        raise ConfigValidationError("INVALID_ENTRY_OFFSET_PCT", "must be non-negative")
    if ref == "upper_pool_upper_edge" and offset != 0.0:
        raise ConfigValidationError(
            "ENTRY_OFFSET_REFERENCE_MISMATCH",
            "entry_offset_pct must be 0 for upper_pool_upper_edge",
            {"entry_price_reference": ref, "entry_offset_pct": offset},
        )
    if se["upper_gap_measurement_mode"] != "adjacent_union_interval_gap_pct_v1":
        raise ConfigValidationError(
            "GAP_MEASUREMENT_NOT_EXECUTABLE",
            "only adjacent_union_interval_gap_pct_v1 is documented/executable in v1",
        )
    sel_mode = str(se["entry_pool_selection_mode"])
    if sel_mode == "user_relevant_upper_unresolved":
        raise ConfigValidationError(
            "ENTRY_SELECTION_UNRESOLVED",
            "user_relevant_upper_unresolved is not executable",
        )
    if sel_mode not in ("nearest_upper_proximity_v1", "pool_first_structural_v1"):
        raise ConfigValidationError("INVALID_ENTRY_POOL_SELECTION_MODE", sel_mode)
    if sel_mode == "pool_first_structural_v1" and ref == "upper_pool_upper_edge":
        raise ConfigValidationError(
            "POOL_FIRST_ENTRY_REFERENCE",
            "pool_first_structural_v1 requires upper_pool_lower_edge (+ optional offset)",
        )
    pcm = str(se.get("pool_cancel_identity_mode") or "exact_band_v1")
    if pcm not in ("exact_band_v1", "origin_set_v1"):
        raise ConfigValidationError("INVALID_POOL_CANCEL_IDENTITY_MODE", pcm)
    if not _is_number(se["upper_gap_min_pct"]):
        raise ConfigValidationError("INVALID_UPPER_GAP_MIN_PCT", "must be number")

    risk = raw["risk"]
    _reject_unknown_keys(risk, frozenset({"sl_above_upper_top_pct", "max_concurrent_positions"}), "risk")
    if risk["max_concurrent_positions"] != 1:
        raise ConfigValidationError("MAX_POSITIONS_LOCKED", "only 1 in v1")

    tp = raw["take_profit"]
    tp_keys = frozenset(
        {
            "selection_mode",
            "min_distance_pct",
            "distance_reference",
            "dense_cluster_semantics_approved",
            "tp_pool_quality_gate_enabled",
        }
    )
    _reject_unknown_keys(tp, tp_keys, "take_profit")
    _require_keys(
        tp,
        frozenset(
            {
                "selection_mode",
                "min_distance_pct",
                "distance_reference",
                "dense_cluster_semantics_approved",
            }
        ),
        "take_profit",
    )
    if "tp_pool_quality_gate_enabled" in tp and not isinstance(tp["tp_pool_quality_gate_enabled"], bool):
        raise ConfigValidationError("INVALID_TP_QUALITY_GATE", "must be boolean")
    if tp["distance_reference"] != "actual_fill_to_lower_upper_edge":
        raise ConfigValidationError("TP_DISTANCE_REFERENCE_LOCKED", tp["distance_reference"])
    if tp["selection_mode"] == "dense_cluster":
        if not tp.get("dense_cluster_semantics_approved"):
            raise ConfigValidationError(
                "DENSE_CLUSTER_NOT_APPROVED",
                "dense_cluster requires dense_cluster_semantics_approved=true (user rule O-01 unresolved)",
            )
    if tp["selection_mode"] not in ("next_eligible_band", "dense_cluster"):
        raise ConfigValidationError("INVALID_TP_MODE", str(tp["selection_mode"]))

    pnl = raw["pnl"]
    pnl_keys = frozenset(
        {
            "initial_equity_usdt",
            "sizing_mode",
            "position_notional_usdt",
            "risk_fraction_pct",
            "leverage",
            "trading_fee_model",
            "slippage_model",
            "funding_model",
            "intrabar_ambiguity_policy",
            "end_of_test_open_position_policy",
        }
    )
    _reject_unknown_keys(pnl, pnl_keys, "pnl")
    _require_keys(pnl, pnl_keys, "pnl")
    if not _is_number(pnl["initial_equity_usdt"]) or pnl["initial_equity_usdt"] <= 0:
        raise ConfigValidationError("INVALID_INITIAL_EQUITY", "initial_equity_usdt must be > 0")
    mode = pnl["sizing_mode"]
    if mode == "fixed_notional_usdt":
        n = pnl["position_notional_usdt"]
        if n is None or not _is_number(n) or n <= 0:
            raise ConfigValidationError("MISSING_POSITION_NOTIONAL", "position_notional_usdt required")
        if pnl["risk_fraction_pct"] is not None:
            raise ConfigValidationError("CONFLICTING_SIZING", "risk_fraction_pct must be null")
    elif mode == "risk_fraction_of_equity":
        r = pnl["risk_fraction_pct"]
        if r is None or not _is_number(r) or r <= 0:
            raise ConfigValidationError("MISSING_RISK_FRACTION", "risk_fraction_pct required")
        if pnl["position_notional_usdt"] is not None:
            raise ConfigValidationError("CONFLICTING_SIZING", "position_notional_usdt must be null")
    else:
        raise ConfigValidationError("INVALID_SIZING_MODE", str(mode))

    _validate_fee_model(pnl["trading_fee_model"])
    _validate_slippage_model(pnl["slippage_model"])
    _validate_funding_model(pnl["funding_model"])

    bt = raw["backtest"]
    bt_keys = frozenset(
        {
            "symbol",
            "symbols",
            "start_utc",
            "end_utc",
            "end_utc_exclusive",
            "execution_timeframe",
            "outcome_blind_signals",
            "deterministic_seed",
        }
    )
    _reject_unknown_keys(bt, bt_keys, "backtest")
    _require_keys(
        bt,
        frozenset(
            {
                "symbol",
                "start_utc",
                "end_utc",
                "end_utc_exclusive",
                "execution_timeframe",
                "outcome_blind_signals",
                "deterministic_seed",
            }
        ),
        "backtest",
    )
    if bt["execution_timeframe"] != "5m":
        raise ConfigValidationError("BACKTEST_TF_LOCKED", "execution_timeframe must be 5m")
    sym = str(bt["symbol"] or "").strip().upper()
    if not sym:
        raise ConfigValidationError("EMPTY_SYMBOL", "backtest.symbol required")
    bt["symbol"] = sym
    if bt.get("symbols"):
        if not bt["symbols"] or str(bt["symbols"][0]).upper() != sym:
            raise ConfigValidationError("SYMBOL_SYMBOLS_MISMATCH", "backtest.symbols must match symbol")
    else:
        bt["symbols"] = [sym]
    if not isinstance(bt["end_utc_exclusive"], bool):
        raise ConfigValidationError("END_UTC_EXCLUSIVE_INVALID", "end_utc_exclusive must be boolean")
    _validate_backtest_utc_window(bt)

    return raw


def _parse_utc_z(s: str) -> datetime:
    if not _UTC_RE.match(str(s)):
        raise ConfigValidationError("UTC_FORMAT_INVALID", f"timestamp must be YYYY-MM-DDTHH:MM:SSZ: {s}")
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _validate_backtest_utc_window(bt: dict[str, Any]) -> None:
    start = _parse_utc_z(bt["start_utc"])
    end = _parse_utc_z(bt["end_utc"])
    for label, dt in (("start_utc", start), ("end_utc", end)):
        if dt.second != 0 or dt.microsecond != 0:
            raise ConfigValidationError("UTC_NOT_ON_MINUTE", label)
        if dt.minute % 5 != 0:
            raise ConfigValidationError("NOT_5M_ALIGNED", label)
    if end <= start:
        raise ConfigValidationError("BACKTEST_WINDOW_INVALID", "end_utc must be after start_utc")


def _validate_fee_model(m: dict[str, Any]) -> None:
    _reject_unknown_keys(m, frozenset({"model", "roundtrip_pct"}), "pnl.trading_fee_model")
    if m["model"] != "roundtrip_pct_of_notional":
        raise ConfigValidationError("INVALID_FEE_MODEL", str(m.get("model")))
    if not _is_number(m["roundtrip_pct"]) or m["roundtrip_pct"] < 0:
        raise ConfigValidationError("INVALID_FEE_PCT", "roundtrip_pct")


def _validate_slippage_model(m: dict[str, Any]) -> None:
    _reject_unknown_keys(
        m, frozenset({"model", "entry_slippage_pct", "exit_slippage_pct"}), "pnl.slippage_model"
    )
    if m["model"] not in ("none", "pct_of_notional_per_side"):
        raise ConfigValidationError("INVALID_SLIPPAGE_MODEL", str(m.get("model")))


def _validate_funding_model(m: dict[str, Any]) -> None:
    _reject_unknown_keys(
        m, frozenset({"model", "funding_rate_bps_per_8h"}), "pnl.funding_model"
    )
    if m["model"] not in ("none", "constant_bps_per_8h_short"):
        raise ConfigValidationError("INVALID_FUNDING_MODEL", str(m.get("model")))


def load_and_validate_config(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise ConfigValidationError("CONFIG_NOT_FOUND", str(path))
    if path.suffix.lower() in (".yaml", ".yml"):
        from config_yaml_v1 import load_yaml_and_validate

        return load_yaml_and_validate(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    return validate_config_dict(raw)

"""Config and initial exit-level diagnostics for backtests (Phase 9)."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from fixed_cycle_hedge_bot.models import StrategyIntent

from .purpose_utils import preserve_bot_purpose

EXIT_PURPOSES = frozenset(
    {
        "LONG_TP_EXIT",
        "LONG_SL_EXIT",
        "SHORT_TP_EXIT",
        "SHORT_SL_EXIT",
    }
)

RELEVANT_CONFIG_KEY_FRAGMENTS = (
    "limit",
    "tp",
    "sl",
    "distance",
    "trigger",
    "offset",
    "tick",
    "cycle",
    "recovery",
    "notional",
    "hedge",
    "profit",
    "fee",
    "grid",
    "fill",
)

RELEVANT_STRATEGY_ATTR_FRAGMENTS = RELEVANT_CONFIG_KEY_FRAGMENTS

LIVE_CONFIG_PATHS: dict[str, Path] = {
    "long": Path("live_bots/100_50_hedge_bot/long_bot_1/config/fixed_cycle_config.json"),
    "short": Path("live_bots/short_hedge_bot/short_bot_1/config/fixed_cycle_config.json"),
}

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _safe_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def config_to_dict(config: object) -> dict[str, Any]:
    if config is None:
        return {}
    if is_dataclass(config):
        return asdict(config)
    if isinstance(config, dict):
        return dict(config)
    return {key: getattr(config, key) for key in dir(config) if not key.startswith("_")}


def filter_relevant_config_keys(config_dict: dict[str, Any]) -> dict[str, Any]:
    filtered: dict[str, Any] = {}
    for key, value in sorted(config_dict.items()):
        key_lower = key.lower()
        if any(fragment in key_lower for fragment in RELEVANT_CONFIG_KEY_FRAGMENTS):
            filtered[key] = value
    return filtered


def scan_strategy_attributes(strategy: object) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for name in dir(strategy):
        if name.startswith("_"):
            continue
        name_lower = name.lower()
        if not any(fragment in name_lower for fragment in RELEVANT_STRATEGY_ATTR_FRAGMENTS):
            continue
        try:
            value = getattr(strategy, name)
        except Exception:
            continue
        if callable(value):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            found[name] = value
    config = getattr(strategy, "config", None)
    if config is not None:
        found["config"] = filter_relevant_config_keys(config_to_dict(config))
    return found


def _candidate(
    *,
    name: str,
    value: float | None,
    source: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric <= 0:
        return None
    return {"name": name, "value": numeric, "source": source}


def compute_exit_price_candidates(
    *,
    entry_price: float | None,
    config: dict[str, Any],
    strategy_state: dict[str, Any] | None = None,
    long_qty: float | None = None,
    short_qty: float | None = None,
    long_avg: float | None = None,
    short_avg: float | None = None,
) -> list[dict[str, Any]]:
    """Build candidate trigger prices from config and entry context."""
    if entry_price is None or entry_price <= 0:
        return []

    entry = float(entry_price)
    candidates: list[dict[str, Any]] = []
    state = strategy_state or {}

    tick_size = _safe_float(config.get("price_tick_size"))
    if tick_size is not None and tick_size > 0:
        for mult in (1, 2):
            item = _candidate(
                name=f"entry+{mult}*price_tick_size",
                value=entry + mult * tick_size,
                source="computed:entry+tick",
            )
            if item:
                candidates.append(item)
        clamp_item = _candidate(
            name="entry+2*price_tick_size (strategy min_long_trigger clamp)",
            value=entry + 2 * tick_size,
            source="strategy_exit_clamp:current_price+2*tick_size",
        )
        if clamp_item:
            candidates.append(clamp_item)

    for offset in (0.2, 0.5, 1.0):
        item = _candidate(
            name=f"entry+{offset}",
            value=entry + offset,
            source="computed:absolute_offset",
        )
        if item:
            candidates.append(item)

    pct_keys = (
        "long_fill_distance_pct",
        "short_fill_distance_pct",
        "tp_profit_target_pct",
        "tp_buffer_pct",
        "second_order_safety_offset_pct",
        "fee_safety_buffer_pct",
        "hard_stop_pct",
        "recovery_mode_trigger_override_pct",
    )
    for key in pct_keys:
        pct = _safe_float(config.get(key))
        if pct is None:
            continue
        item_pct = _candidate(
            name=f"entry*(1+{key}/100)",
            value=entry * (1.0 + pct / 100.0),
            source=f"config:{key}",
        )
        if item_pct:
            candidates.append(item_pct)
        item_abs = _candidate(
            name=f"entry+{key}",
            value=entry + pct,
            source=f"config:{key}_as_absolute",
        )
        if item_abs:
            candidates.append(item_abs)

    tp_pct = _safe_float(config.get("tp_profit_target_pct"))
    if tp_pct is not None:
        item = _candidate(
            name="entry*(1+tp_profit_target_pct) [misinterpreted multiplier]",
            value=entry * (1.0 + tp_pct),
            source="computed:tp_as_multiplier_not_div100",
        )
        if item:
            candidates.append(item)

    latest_tp = _safe_float(state.get("latest_tp_price"))
    if latest_tp is not None:
        candidates.append(
            {
                "name": "strategy_state.latest_tp_price",
                "value": latest_tp,
                "source": "strategy_state:latest_tp_price",
            }
        )
    latest_be = _safe_float(state.get("latest_break_even_price"))
    if latest_be is not None:
        candidates.append(
            {
                "name": "strategy_state.latest_break_even_price",
                "value": latest_be,
                "source": "strategy_state:latest_break_even_price",
            }
        )

    try:
        from fixed_cycle_hedge_bot.hedge_exit_math import calculate_hedge_exit_price

        l_avg = long_avg if long_avg is not None else entry
        s_avg = short_avg if short_avg is not None else entry
        l_qty = long_qty if long_qty is not None else _safe_float(state.get("open_long_qty"))
        s_qty = short_qty if short_qty is not None else _safe_float(state.get("open_short_qty"))
        if l_qty and s_qty and tp_pct is not None:
            components = calculate_hedge_exit_price(
                long_avg=l_avg,
                long_qty=l_qty,
                short_avg=s_avg,
                short_qty=s_qty,
                tp_profit_target_pct=tp_pct,
                tp_buffer_pct=_safe_float(config.get("tp_buffer_pct")) or 0.0,
                realized_cycle_net=0.0,
            )
            item = _candidate(
                name="hedge_exit_math.exit_price",
                value=components.exit_price,
                source="hedge_exit_math:tp_profit_target_pct",
            )
            if item:
                candidates.append(item)
    except Exception:
        pass

    deduped: dict[tuple[str, float], dict[str, Any]] = {}
    for item in candidates:
        deduped[(item["name"], round(float(item["value"]), 12))] = item
    return list(deduped.values())


def find_nearest_candidate(
    trigger_price: float | None,
    candidates: list[dict[str, Any]],
    *,
    tolerance: float = 1e-4,
) -> dict[str, Any] | None:
    if trigger_price is None or not candidates:
        return None
    trigger = float(trigger_price)
    best: dict[str, Any] | None = None
    best_delta = float("inf")
    for item in candidates:
        value = float(item["value"])
        delta = abs(value - trigger)
        if delta < best_delta:
            best_delta = delta
            best = {
                **item,
                "delta_abs": delta,
                "delta_pct": ((value - trigger) / trigger * 100.0) if trigger else None,
            }
    if best is not None and best_delta <= max(tolerance, abs(trigger) * 0.001):
        return best
    if best is not None:
        return best
    return None


def build_backtest_config_diagnostics(
    strategy: object,
    config: object,
    *,
    symbol: str,
    entry_price: float | None,
    config_source: str,
    strategy_state: dict[str, Any] | None = None,
    exit_trigger_price: float | None = None,
    long_qty: float | None = None,
    short_qty: float | None = None,
    long_avg: float | None = None,
    short_avg: float | None = None,
) -> dict[str, Any]:
    config_dict = config_to_dict(config)
    relevant_config = filter_relevant_config_keys(config_dict)
    candidates = compute_exit_price_candidates(
        entry_price=entry_price,
        config=relevant_config,
        strategy_state=strategy_state,
        long_qty=long_qty,
        short_qty=short_qty,
        long_avg=long_avg,
        short_avg=short_avg,
    )
    nearest = find_nearest_candidate(exit_trigger_price, candidates)
    entry = _safe_float(entry_price)
    trigger = _safe_float(exit_trigger_price)
    diagnostics: dict[str, Any] = {
        "strategy_class": type(strategy).__name__,
        "config_source": config_source,
        "symbol": symbol.upper(),
        "entry_price": entry,
        "relevant_config": relevant_config,
        "strategy_attributes": scan_strategy_attributes(strategy),
        "computed_candidates": candidates,
        "exit_trigger_price": trigger,
        "nearest_candidate_to_exit_trigger": nearest,
    }
    if entry is not None and trigger is not None:
        diagnostics["trigger_minus_entry"] = trigger - entry
        diagnostics["trigger_distance_pct"] = ((trigger / entry) - 1.0) * 100.0 if entry else None
    if nearest:
        diagnostics["nearest_config_candidate"] = nearest.get("value")
        diagnostics["nearest_config_candidate_source"] = nearest.get("source")
        diagnostics["nearest_config_candidate_name"] = nearest.get("name")
    return diagnostics


def enrich_exit_intent_metadata(
    intent: StrategyIntent,
    *,
    entry_price: float | None,
    config_source: str,
    config: object | None = None,
    strategy_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add exit-level fields to intent metadata excerpt."""
    purpose = preserve_bot_purpose(intent.purpose)
    if purpose not in EXIT_PURPOSES:
        return {}

    trigger = _safe_float(intent.trigger_price)
    price = _safe_float(intent.price)
    entry = _safe_float(entry_price)
    excerpt: dict[str, Any] = {
        "config_source": config_source,
    }
    if entry is not None:
        excerpt["entry_price_at_intent"] = entry
    if trigger is not None and entry is not None:
        excerpt["trigger_minus_entry"] = trigger - entry
        excerpt["trigger_distance_pct"] = ((trigger / entry) - 1.0) * 100.0
    if price is not None and entry is not None:
        excerpt["price_minus_entry"] = price - entry

    if config is not None:
        candidates = compute_exit_price_candidates(
            entry_price=entry,
            config=filter_relevant_config_keys(config_to_dict(config)),
            strategy_state=strategy_state,
        )
        nearest = find_nearest_candidate(trigger or price, candidates)
        if nearest:
            excerpt["nearest_config_candidate"] = nearest.get("value")
            excerpt["config_candidate_source"] = nearest.get("source")
            excerpt["config_candidate_name"] = nearest.get("name")
    return excerpt


def extract_initial_exit_trigger(intent_log: list[dict[str, Any]]) -> float | None:
    for entry in intent_log:
        if entry.get("purpose") not in EXIT_PURPOSES:
            continue
        trigger = _safe_float(entry.get("trigger_price"))
        if trigger is not None:
            return trigger
    return None


def build_exit_level_diagnostics_from_intents(
    intent_log: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for entry in intent_log:
        if entry.get("purpose") not in EXIT_PURPOSES:
            continue
        excerpt = dict(entry.get("metadata_excerpt") or {})
        diagnostics.append(
            {
                "purpose": entry.get("purpose"),
                "trigger_price": entry.get("trigger_price"),
                "trigger_direction": entry.get("trigger_direction"),
                "entry_price_at_intent": excerpt.get("entry_price_at_intent"),
                "trigger_minus_entry": excerpt.get("trigger_minus_entry"),
                "trigger_distance_pct": excerpt.get("trigger_distance_pct"),
                "nearest_config_candidate": excerpt.get("nearest_config_candidate"),
                "config_candidate_source": excerpt.get("config_candidate_source"),
                "config_candidate_name": excerpt.get("config_candidate_name"),
                "config_source": excerpt.get("config_source"),
            }
        )
    return diagnostics


def _load_json_if_exists(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def compare_backtest_config_to_live_configs(
    backtest_config: object,
    *,
    project_root: Path | None = None,
) -> dict[str, Any]:
    root = project_root or PROJECT_ROOT
    backtest_dict = filter_relevant_config_keys(config_to_dict(backtest_config))
    comparison: dict[str, Any] = {
        "backtest_relevant_config": backtest_dict,
        "live_configs_found": {},
        "differences": {},
        "notes": [],
    }

    for direction, rel_path in LIVE_CONFIG_PATHS.items():
        path = root / rel_path
        live_data = _load_json_if_exists(path)
        if live_data is None:
            comparison["notes"].append(f"missing live config: {rel_path}")
            continue
        live_relevant = filter_relevant_config_keys(live_data)
        comparison["live_configs_found"][direction] = {
            "path": str(rel_path),
            "relevant_config": live_relevant,
        }
        diff: dict[str, Any] = {}
        all_keys = sorted(set(backtest_dict) | set(live_relevant))
        for key in all_keys:
            back_val = backtest_dict.get(key)
            live_val = live_relevant.get(key)
            if back_val != live_val:
                diff[key] = {"backtest": back_val, "live": live_val}
        comparison["differences"][direction] = diff

    comparison["backtest_uses_defaults"] = bool(comparison["differences"])
    return comparison


def config_diagnostics_summary_fields(diagnostics: dict[str, Any]) -> dict[str, Any]:
    nearest = diagnostics.get("nearest_candidate_to_exit_trigger") or {}
    return {
        "config_source": diagnostics.get("config_source"),
        "initial_exit_trigger": diagnostics.get("exit_trigger_price"),
        "initial_exit_trigger_distance_abs": diagnostics.get("trigger_minus_entry"),
        "initial_exit_trigger_distance_pct": diagnostics.get("trigger_distance_pct"),
        "nearest_config_candidate": nearest.get("value") or diagnostics.get("nearest_config_candidate"),
        "nearest_config_candidate_source": nearest.get("source")
        or diagnostics.get("nearest_config_candidate_source"),
    }

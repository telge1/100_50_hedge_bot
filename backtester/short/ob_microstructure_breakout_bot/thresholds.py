from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ob_microstructure_breakout_bot.models import CoinThresholds, SideThresholds

_CONFIG_DIR = Path(__file__).resolve().parent / "config"

_SYMBOL_FILE = {
    "DOGEUSDT": "doge_usdt.yaml",
    "DOGEUSDT_CALIBRATED": "doge_usdt_calibrated.yaml",
}


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config must be a mapping: {path}")
    return data


def _side_from_mapping(
    raw: dict[str, Any],
    *,
    default_require_ob: bool,
    flip_must_be_negative: bool | None = None,
) -> SideThresholds:
    flip = float(raw["fakeout_followthrough_flip_delta"])
    if flip_must_be_negative is True and flip > 0:
        flip = -abs(flip)
    if flip_must_be_negative is False and flip < 0:
        flip = abs(flip)
    return SideThresholds(
        fakeout_max_confirm_delta=float(raw["fakeout_max_confirm_delta"]),
        tier1_min_confirm_delta=float(raw["tier1_min_confirm_delta"]),
        tier2_min_confirm_delta=float(raw["tier2_min_confirm_delta"]),
        tier1_min_ob_ratio_5bps=float(
            raw.get(
                "tier1_min_ob_ratio_5bps",
                raw.get("tier1_min_bid_ask_ratio_5bps"),
            )
        ),
        tier2_min_ob_ratio_5bps=float(
            raw.get(
                "tier2_min_ob_ratio_5bps",
                raw.get("tier2_min_bid_ask_ratio_5bps"),
            )
        ),
        fakeout_followthrough_flip_delta=flip,
        require_ob_support=bool(raw.get("require_ob_support", default_require_ob)),
    )


def _mirror_short_from_long(long: SideThresholds) -> SideThresholds:
    """Legacy behavior: short uses mirrored long magnitudes."""
    return SideThresholds(
        fakeout_max_confirm_delta=long.fakeout_max_confirm_delta,
        tier1_min_confirm_delta=long.tier1_min_confirm_delta,
        tier2_min_confirm_delta=long.tier2_min_confirm_delta,
        tier1_min_ob_ratio_5bps=long.tier1_min_ob_ratio_5bps,
        tier2_min_ob_ratio_5bps=long.tier2_min_ob_ratio_5bps,
        # Short FT flip is positive magnitude of the long negative flip.
        fakeout_followthrough_flip_delta=abs(long.fakeout_followthrough_flip_delta),
        require_ob_support=True,
    )


def thresholds_from_dict(raw: dict[str, Any], *, symbol_fallback: str) -> CoinThresholds:
    """Parse flat legacy YAML or nested long/short YAML."""
    symbol = str(raw.get("symbol", symbol_fallback))
    if "long" in raw or "short" in raw:
        long_raw = raw.get("long") or raw
        short_raw = raw.get("short")
        long = _side_from_mapping(
            long_raw if isinstance(long_raw, dict) else raw,
            default_require_ob=True,
            flip_must_be_negative=True,
        )
        if isinstance(short_raw, dict):
            short = _side_from_mapping(
                short_raw,
                default_require_ob=False,
                flip_must_be_negative=False,
            )
        else:
            short = _mirror_short_from_long(long)
    else:
        long = _side_from_mapping(raw, default_require_ob=True, flip_must_be_negative=True)
        short = _mirror_short_from_long(long)

    return CoinThresholds(
        symbol=symbol,
        long=long,
        short=short,
        context_lookback_minutes=int(raw.get("context_lookback_minutes", 30)),
        confirm_window_minutes=int(raw.get("confirm_window_minutes", 5)),
        followthrough_candles=int(raw.get("followthrough_candles", 2)),
    )


def load_thresholds(
    symbol: str,
    *,
    config_path: Path | None = None,
) -> CoinThresholds:
    if config_path is not None:
        path = Path(config_path)
        raw = _load_yaml(path)
        return thresholds_from_dict(raw, symbol_fallback=symbol.upper().replace("/", ""))

    key = symbol.upper().replace("/", "")
    filename = _SYMBOL_FILE.get(key)
    if filename is None:
        raise KeyError(
            f"No threshold config for {symbol!r}. "
            f"Known: {sorted(_SYMBOL_FILE)}"
        )
    path = _CONFIG_DIR / filename
    raw = _load_yaml(path)
    return thresholds_from_dict(raw, symbol_fallback=key)


def list_configured_symbols() -> list[str]:
    return sorted(_SYMBOL_FILE)


def make_thresholds(
    *,
    symbol: str,
    fakeout_max_confirm_delta: float,
    tier1_min_confirm_delta: float,
    tier2_min_confirm_delta: float,
    tier1_min_bid_ask_ratio_5bps: float,
    tier2_min_bid_ask_ratio_5bps: float,
    fakeout_followthrough_flip_delta: float,
    context_lookback_minutes: int = 30,
    confirm_window_minutes: int = 5,
    followthrough_candles: int = 2,
    short: SideThresholds | None = None,
) -> CoinThresholds:
    """Convenience constructor used by tests and legacy flat configs."""
    long = SideThresholds(
        fakeout_max_confirm_delta=fakeout_max_confirm_delta,
        tier1_min_confirm_delta=tier1_min_confirm_delta,
        tier2_min_confirm_delta=tier2_min_confirm_delta,
        tier1_min_ob_ratio_5bps=tier1_min_bid_ask_ratio_5bps,
        tier2_min_ob_ratio_5bps=tier2_min_bid_ask_ratio_5bps,
        fakeout_followthrough_flip_delta=fakeout_followthrough_flip_delta
        if fakeout_followthrough_flip_delta < 0
        else -abs(fakeout_followthrough_flip_delta),
        require_ob_support=True,
    )
    return CoinThresholds(
        symbol=symbol,
        long=long,
        short=short if short is not None else _mirror_short_from_long(long),
        context_lookback_minutes=context_lookback_minutes,
        confirm_window_minutes=confirm_window_minutes,
        followthrough_candles=followthrough_candles,
    )

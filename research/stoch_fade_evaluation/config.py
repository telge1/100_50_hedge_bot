"""Pinned Frozen-signal + NO_BE50 evaluation identity. No strategy formulas."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SG_ROOT = Path(
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves"
)
SIGNAL_STRATEGY_VERSION = "wave_fade_frozen_f16ae32"
STRATEGY_VERSION = SIGNAL_STRATEGY_VERSION
SIGNAL_SOURCE_COMMIT = "f16ae32da38da86f39e75b09c63c31f62d11996b"
SOURCE_COMMIT = SIGNAL_SOURCE_COMMIT
EXIT_POLICY = "NO_BE50"
SIGNAL_SCOPE = "TIER_A_ONLY"
OUTCOME_ENGINE = "signal_generator.pipeline.outcome_eval.evaluate_signal_no_be50"
OUTCOME_ENGINE_NAME = "evaluate_signal_no_be50"
SCAN_EXIT = "signal_generator.strategy.wave_fade.exits.scan_exit_sl_first"
PNL_BASIS = "gross"
FEE_POLICY = "existing_FEE_PCT_diagnostic_only_not_used_for_cards"
INTRABAR_POLICY = "SL_FIRST"
CANDLE_SOURCE = "signal_generator.candles_1m FINAL exchange=bybit interval=1m is_closed=1"

ENGINE_SHA256 = {
    "src/signal_generator/pipeline/outcome_eval.py": (
        "983b6e25fd4a60a1646ee8f206cdaa81132f51e4738de11506236edc799423d0"
    ),
    "src/signal_generator/strategy/wave_fade/exits.py": (
        "3f39937dce6a34ff432f97c4588ae35764b88d8c19d1a8d10c743300123e6fea"
    ),
    "src/signal_generator/pipeline/versions.py": (
        "0919699d7ac702bc13983a9f810afc201898d7e3d565dd83a535f265afd12680"
    ),
}

SIDE_EFFECT_FLAGS = {
    "writes_to_clickhouse": False,
    "writes_to_signals": False,
    "writes_to_signal_outcomes": False,
    "writes_to_processing_state": False,
    "cleanup_enabled": False,
    "publish_enabled": False,
    "live_orders_enabled": False,
}


def sg_root(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = str(env.get("STOCH_FADE_SIGNAL_GENERATOR_ROOT") or "").strip()
    if override:
        return Path(override)
    return DEFAULT_SG_ROOT


def ensure_sg_on_path(environ: dict | None = None) -> Path:
    import sys

    root = sg_root(environ)
    src = str(root / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    return root


def iso_z(ts: datetime | None = None) -> str:
    now = ts or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

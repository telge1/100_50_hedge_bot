"""Dashboard Frozen-signal NO_BE50 evaluation job paths. No engine import in FastAPI process."""

from __future__ import annotations

import os
from pathlib import Path

DASHBOARD_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = DASHBOARD_ROOT.parent
DEFAULT_SG_ROOT = Path(
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves"
)
DEFAULT_SG_PYTHON = DEFAULT_SG_ROOT / ".venv" / "bin" / "python"
DEFAULT_EVAL_ROOT = REPO_ROOT / "results" / "stoch_fade_research_evaluations"
WORKER_SCRIPT = Path(__file__).resolve().parent / "worker.py"

STRATEGY_VERSION = "wave_fade_frozen_f16ae32"
SIGNAL_STRATEGY_VERSION = STRATEGY_VERSION
SIGNAL_SOURCE_COMMIT = "f16ae32da38da86f39e75b09c63c31f62d11996b"
EXIT_POLICY = "NO_BE50"
SIGNAL_SCOPE = "TIER_A_ONLY"
OUTCOME_ENGINE = "evaluate_signal_no_be50"
INTRABAR_POLICY = "SL_FIRST"
SOURCE = "FROZEN_RESEARCH_EVALUATION"
DEFAULT_COIN_TIMEOUT_S = 600
COIN_TERM_GRACE_S = 5
PER_COIN_DISK_BYTES = 20 * 1024 * 1024
DISK_RESERVE_BYTES = 256 * 1024 * 1024


def evaluations_root(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = str(env.get("STOCH_FADE_RESEARCH_EVALUATIONS_ROOT") or "").strip()
    if override:
        return Path(override)
    return DEFAULT_EVAL_ROOT


def sg_python(environ: dict | None = None) -> str:
    env = environ if environ is not None else os.environ
    override = str(env.get("STOCH_FADE_SG_PYTHON") or "").strip()
    if override:
        return override
    return str(DEFAULT_SG_PYTHON)


def coin_timeout_s(environ: dict | None = None) -> int:
    env = environ if environ is not None else os.environ
    raw = str(env.get("STOCH_FADE_EVAL_COIN_TIMEOUT_S") or "").strip()
    if raw:
        return max(30, int(raw))
    return DEFAULT_COIN_TIMEOUT_S

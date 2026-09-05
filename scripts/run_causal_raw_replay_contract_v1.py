#!/usr/bin/env python3
"""Launcher: CAUSAL_RAW_REPLAY_CONTRACT_V1 validation."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.causal_raw_replay_contract_v1.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

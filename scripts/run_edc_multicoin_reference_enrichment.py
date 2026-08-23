#!/usr/bin/env python3
"""Causal feature enrichment for frozen multi-coin EDC reference cell (research-only).

Modes (exactly one required): --dry-run | --enrich | --analyze | --report-only

Code-only phase: use --help or --dry-run only. Do not run --enrich.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.reference_enrichment.cli import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())

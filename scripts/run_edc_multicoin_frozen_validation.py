#!/usr/bin/env python3
"""CLI entry: causal multi-coin frozen validation (research-only).

Examples (run manually later — not in the code-ready Cursor turn):

  PYTHONPATH=src python scripts/run_edc_multicoin_frozen_validation.py --dry-run
  PYTHONPATH=src python scripts/run_edc_multicoin_frozen_validation.py --preflight-only
  PYTHONPATH=src python scripts/run_edc_multicoin_frozen_validation.py --run --max-workers 1
  PYTHONPATH=src python scripts/run_edc_multicoin_frozen_validation.py --resume --max-workers 1
  PYTHONPATH=src python scripts/run_edc_multicoin_frozen_validation.py --report-only
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.cli import (  # noqa: E402
    main,
)


if __name__ == "__main__":
    raise SystemExit(main())

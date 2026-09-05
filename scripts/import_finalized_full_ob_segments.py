#!/usr/bin/env python3
"""Import finalized Full-OB flight-recorder segments into an isolated ClickHouse DB.

Never run inside the live collector process.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure src on path when invoked as script
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orderbook_analyse.full_ob_segment_import.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

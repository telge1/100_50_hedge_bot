#!/usr/bin/env python3
"""CLI for causal liquidation history analysis (ClickHouse read-only)."""

from __future__ import annotations

import sys

from orderbook_analyse.liquidation_analysis import main


if __name__ == "__main__":
    raise SystemExit(main())

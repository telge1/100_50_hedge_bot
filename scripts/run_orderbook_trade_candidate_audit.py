#!/usr/bin/env python3
"""CLI for causal orderbook trade-candidate audit (ClickHouse read-only)."""

from __future__ import annotations

import sys

from orderbook_analyse.orderbook_trade_candidate_audit import main


if __name__ == "__main__":
    raise SystemExit(main())

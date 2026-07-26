#!/usr/bin/env python3
"""CLI for causal bid-weakening / reversal warning audit (ClickHouse read-only)."""

from __future__ import annotations

import sys

from orderbook_analyse.orderbook_bid_weakening_reversal_audit import main


if __name__ == "__main__":
    raise SystemExit(main())

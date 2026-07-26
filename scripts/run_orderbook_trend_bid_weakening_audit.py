#!/usr/bin/env python3
"""CLI for integrated trend + bid-weakening audit (research only)."""

from __future__ import annotations

import sys

from orderbook_analyse.orderbook_trend_bid_weakening_audit import main


if __name__ == "__main__":
    raise SystemExit(main())

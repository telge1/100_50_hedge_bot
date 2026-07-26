#!/usr/bin/env python3
"""CLI for causal wall movement tracking (ClickHouse read-only)."""

from __future__ import annotations

import sys

from orderbook_analyse.wall_movement_tracker import main


if __name__ == "__main__":
    raise SystemExit(main())

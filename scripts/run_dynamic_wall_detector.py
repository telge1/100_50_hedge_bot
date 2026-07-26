#!/usr/bin/env python3
"""CLI for the dynamic wall detector (ClickHouse read-only research)."""

from __future__ import annotations

import sys

from orderbook_analyse.dynamic_wall_detector import main


if __name__ == "__main__":
    raise SystemExit(main())

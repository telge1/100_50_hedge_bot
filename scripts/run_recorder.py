#!/usr/bin/env python3
"""CLI entrypoint for the Bybit → ClickHouse recorder."""

from __future__ import annotations

import sys

from orderbook_analyse.bybit_recorder import main


if __name__ == "__main__":
    raise SystemExit(main())

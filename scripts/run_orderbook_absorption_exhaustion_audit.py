#!/usr/bin/env python3
"""CLI for absorption / exhaustion research audit (read-only)."""

from __future__ import annotations

import sys

from orderbook_analyse.orderbook_absorption_exhaustion_audit import main


if __name__ == "__main__":
    raise SystemExit(main())

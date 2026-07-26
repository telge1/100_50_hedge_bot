#!/usr/bin/env python3
"""CLI for A2→G5 handoff research audit (read-only CSV)."""

from __future__ import annotations

import sys

from orderbook_analyse.orderbook_absorption_g5_handoff_audit import main


if __name__ == "__main__":
    raise SystemExit(main())

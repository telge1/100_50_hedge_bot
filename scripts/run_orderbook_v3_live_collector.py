#!/usr/bin/env python3
"""Foreground/nohup entry for the ADAUSDT Orderbook V3 live pilot."""
from orderbook_analyse.orderbook_v2_live.collector import main

if __name__ == "__main__":
    raise SystemExit(main())

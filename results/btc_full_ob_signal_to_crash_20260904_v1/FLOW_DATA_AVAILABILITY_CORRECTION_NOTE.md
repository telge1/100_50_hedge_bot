# Integration note (read-only pointer)

The statement „Trades/OI Sep-4 = nicht verfügbar“ in this folder’s REPORT was **partially incorrect**.

- Public trades **were present** in `orderbook_analysis.public_trades_canonical`.
- This analysis queried `btc_doge_research.research_*` and treated `orderbook_analysis` as fully unloadable.

Corrected audit + offline adapter:

`../btc_sep4_trade_oi_liquidation_availability_audit_v1/`

This file does not change prior Full-OB conclusions; it only documents the flow-data availability error.

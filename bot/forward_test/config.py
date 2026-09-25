"""Forward-test defaults (frozen short baseline rules, dry-run only)."""

from __future__ import annotations

from pathlib import Path

# First dry-run universe (10 liquid coins).
# BTCUSDT excluded: OB live collection does not cover BTC.
DEFAULT_SYMBOLS: tuple[str, ...] = (
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "BNBUSDT",
    "SUIUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
)

# Frozen short bounce rule (mirror of backtester).
MAX_RANKS = 2
WATCH_BEFORE_PCT = 0.8
MIN_TP_ROOM_PCT = 0.8
# If nearest usable 5m TP is farther than this, switch TP source to 1m pools.
MAX_5M_TP_ROOM_PCT = 3.0
SL_ABOVE_POOL_TOP_PCT = 0.2
FLOW_OB_OK = 1.05
FLOW_DELTA_OK = 100_000.0

# Regime filter (1h + 4h). Asked only when a short signal exists.
# Bullish = EMA9 and EMA20 both above EMA59. A short is ignored if either TF is bullish.
REGIME_LOOKBACK_DAYS = 45

# How far back we load bars / pools each scan.
BAR_LOOKBACK_HOURS = 6
POOL_LOOKBACK_HOURS = 12
POOL_LOOKFORWARD_HOURS = 2

# Live / forward-test: refuse entries whose reversal candle is already stale.
# ~1 closed 5m bar + poll slack. Older setups would book TP/SL from past bars.
MAX_ENTRY_LAG_MINUTES = 10.0

# Failure exit for an open short (checked every poll on live data, not bar count).
# Close only when ALL are true:
# - price has come back to the entry (within this % below, or already through entry)
# - a thick ACTIVE upper pool sits just above the entry
# - live OB is strong and 10m delta is positive (flow flipped against the short)
FAILURE_NEAR_ENTRY_PCT = 0.20
FAILURE_MIN_POOL_STRENGTH = 4.0  # solid cluster mass ("dicker Pool")
FAILURE_MAX_POOL_DIST_PCT = 1.5  # pool bottom must sit close above entry
FAILURE_OB_MIN = FLOW_OB_OK
FAILURE_DELTA_MIN = 0.0

# Loop timing.
DEFAULT_POLL_SECONDS = 30.0

# Artifacts under bot/forward_test/
PACKAGE_DIR = Path(__file__).resolve().parent
LOG_DIR = PACKAGE_DIR / "logs"
STATE_DIR = PACKAGE_DIR / "state"
EVENTS_LOG = LOG_DIR / "events.jsonl"
SIGNALS_LOG = LOG_DIR / "signals.jsonl"
TRADES_LOG = LOG_DIR / "trades.jsonl"
OPEN_TRADES_FILE = STATE_DIR / "open_trades.json"
SUMMARY_FILE = STATE_DIR / "pnl_summary.json"
STATE_FILE = STATE_DIR / "seen.json"

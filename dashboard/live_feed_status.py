"""Read-only probe of OB V3 live + OI/Liq collector processes.

Does not start/stop anything. Used by /stoch-signale collector status UI.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ORDERBOOK_ROOT = Path(
    os.environ.get(
        "ORDERBOOK_ANALYSE_ROOT",
        "/home/telgenbuescher/projects/orderbook_analyse",
    )
)
OB_PID_PATH = Path(
    os.environ.get(
        "OB_V3_LIVE_PID_PATH",
        str(ORDERBOOK_ROOT / "logs" / "orderbook_v3_live_collector.pid"),
    )
)
OI_PID_PATH = Path(
    os.environ.get(
        "OI_LIQ_PID_PATH",
        str(ORDERBOOK_ROOT / "logs" / "oi_liquidation_collector.pid"),
    )
)
OI_UNIVERSE_PLAN = Path(
    os.environ.get(
        "OI_LIQ_UNIVERSE_PLAN",
        str(ORDERBOOK_ROOT / "results" / "oi_liquidation_collector" / "universe_plan.json"),
    )
)
TRADEABLE_51 = Path(
    os.environ.get(
        "TRADEABLE_51_PATH",
        "/home/telgenbuescher/projects/wave_fade_gold_f16ae32/config/universe_tradeable_51.json",
    )
)

# Same 51 as orderbook_analyse.orderbook_v2_live.universe.SYMBOLS_51
_OB_SHADOW3 = ("ADAUSDT", "BTCUSDT", "ETHUSDT")
_OB_48 = (
    "SOLUSDT",
    "XRPUSDT",
    "BNBUSDT",
    "DOGEUSDT",
    "LINKUSDT",
    "AVAXUSDT",
    "LTCUSDT",
    "DOTUSDT",
    "SUIUSDT",
    "APTUSDT",
    "NEARUSDT",
    "ATOMUSDT",
    "UNIUSDT",
    "AAVEUSDT",
    "ARBUSDT",
    "OPUSDT",
    "TRXUSDT",
    "XLMUSDT",
    "HBARUSDT",
    "ALGOUSDT",
    "INJUSDT",
    "TIAUSDT",
    "ICPUSDT",
    "RENDERUSDT",
    "CRVUSDT",
    "MNTUSDT",
    "HYPEUSDT",
    "ZECUSDT",
    "XMRUSDT",
    "TAOUSDT",
    "WLDUSDT",
    "ENAUSDT",
    "ONDOUSDT",
    "JTOUSDT",
    "1000PEPEUSDT",
    "SHIB1000USDT",
    "1000BONKUSDT",
    "WIFUSDT",
    "PENGUUSDT",
    "TRUMPUSDT",
    "PUMPFUNUSDT",
    "FARTCOINUSDT",
    "KAITOUSDT",
    "WLFIUSDT",
    "XPLUSDT",
    "LITUSDT",
    "XAUTUSDT",
    "PAXGUSDT",
)
OB_UNIVERSE_51 = tuple(_OB_SHADOW3 + _OB_48)


def _read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def _proc_cmdline(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", errors="replace"
        )
    except OSError:
        return None


def _load_symbols_json(path: Path) -> list[str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    if isinstance(raw, dict) and isinstance(raw.get("symbols"), list):
        return [str(s).strip().upper() for s in raw["symbols"] if str(s).strip()]
    if isinstance(raw, dict) and isinstance(raw.get("supported"), list):
        return [str(s).strip().upper() for s in raw["supported"] if str(s).strip()]
    if isinstance(raw, dict) and isinstance(raw.get("requested"), list):
        return [str(s).strip().upper() for s in raw["requested"] if str(s).strip()]
    return []


def _probe_process(
    *,
    name: str,
    pid_path: Path,
    cmdline_needles: tuple[str, ...],
) -> dict[str, Any]:
    pid = _read_pid(pid_path)
    cmdline = _proc_cmdline(pid) if pid is not None else None
    running = bool(
        cmdline
        and all(needle in cmdline for needle in cmdline_needles)
    )
    return {
        "name": name,
        "running": running,
        "pid": pid if running else None,
        "pid_path": str(pid_path),
        "cmdline": (cmdline or "")[:240] if running else None,
    }


def probe_ob_live() -> dict[str, Any]:
    base = _probe_process(
        name="orderbook_v3_live",
        pid_path=OB_PID_PATH,
        cmdline_needles=("orderbook_v2_live",),
    )
    symbols: list[str] = []
    mode = None
    if base["running"] and base.get("cmdline"):
        cmd = base["cmdline"]
        if "universe51" in cmd:
            mode = "universe51"
            symbols = list(OB_UNIVERSE_51)
        elif "shadow3" in cmd:
            mode = "shadow3"
            symbols = list(_OB_SHADOW3)
        elif "ada" in cmd:
            mode = "ada"
            symbols = ["ADAUSDT"]
    base["mode"] = mode
    base["symbols"] = symbols
    base["symbol_count"] = len(symbols)
    return base


def probe_oi_liq() -> dict[str, Any]:
    base = _probe_process(
        name="oi_liquidation",
        pid_path=OI_PID_PATH,
        cmdline_needles=("oi_liquidation_collector",),
    )
    symbols = _load_symbols_json(OI_UNIVERSE_PLAN)
    if not symbols:
        symbols = _load_symbols_json(TRADEABLE_51)
    # XAU is never part of OI/OB public streams
    symbols = [s for s in symbols if s != "XAUUSDT"]
    base["symbols"] = symbols if base["running"] else []
    base["symbol_count"] = len(base["symbols"])
    base["universe_plan_path"] = str(OI_UNIVERSE_PLAN)
    return base


def live_feeds_overview() -> dict[str, Any]:
    """Process-level overview for dashboard collector status."""
    ob = probe_ob_live()
    oi = probe_oi_liq()
    return {
        "orderbook_live": ob,
        "oi_liquidation": oi,
        "notes": (
            "Process probe only (pid + cmdline). Per-symbol ON means the live "
            "collector process covers that symbol; not a per-tick lag check."
        ),
    }

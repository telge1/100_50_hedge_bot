"""SL episode context research from BT-D46 chronological events (read-only)."""

from __future__ import annotations

import copy
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_sl_research_rows(root: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    events_path = root / "results/phase_bt_d46_integrated_short_backtester_v1/chronological_events.csv"
    if not events_path.is_file():
        return [{"status": "D46_EVENTS_MISSING", "path": str(events_path)}]

    import sys

    bt7 = root / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1"
    bt6 = root / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1"
    bt8 = root / "tools/phase_bt_d8_canonical_density_cache_v1"
    bt59 = root / "tools/phase_bt_d59_market_regime_pool_evolution_v1"
    for p in (bt59, bt7, bt6, bt8):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))

    from cache_session_v1 import ValidatedDensityCacheSessionV1  # noqa: E402
    from price_feed_v1 import load_prices_from_ssot_window  # noqa: E402
    from ema_research_v1 import ema_map_for_bars  # noqa: E402
    from regime_features_v1 import compute_regime_features_at_close  # noqa: E402
    from research_windows_v1 import DENSITY_NOTE_CACHE, PRICE_FEED_NOTE  # noqa: E402

    bundle = root / "results/phase_bt_d9_first_24h_short_pnl_v1/cache_bundle"
    cfg = copy.deepcopy(config)
    cfg["density"]["density_source"] = "validated_cache"
    cfg["density"]["validated_cache"] = {"bundle_path": str(bundle)}
    dens = ValidatedDensityCacheSessionV1(bundle, strategy_config=cfg) if bundle.is_dir() else None

    sl_events = []
    for row in csv.DictReader(events_path.open()):
        if row.get("event") == "POSITION_CLOSED_SL":
            sl_events.append(row)

    # Research subset: first 5 distinct SL times on Sep-11
    seen = set()
    picked = []
    for e in sl_events:
        t = e.get("decision_close_utc") or e.get("bar_utc") or ""
        if not t or t in seen:
            continue
        if not t.startswith("2026-09-11"):
            continue
        seen.add(t)
        picked.append(e)
        if len(picked) >= 5:
            break

    out: list[dict[str, Any]] = []
    for e in picked:
        sl_bar = e.get("decision_close_utc") or ""
        end = _iso(_parse_utc(sl_bar) + timedelta(hours=2))
        start = _iso(_parse_utc(sl_bar) - timedelta(hours=3))
        prices = load_prices_from_ssot_window(start, end, end_utc_exclusive=False)
        bars = sorted(prices.keys())
        if not bars or sl_bar not in prices:
            out.append({"sl_bar_utc": sl_bar, "status": "NO_OHLC_FOR_EPISODE"})
            continue
        emas = ema_map_for_bars(bars, prices)
        idx = bars.index(sl_bar)

        def _feat(offset: int) -> dict[str, Any]:
            i = idx + offset
            if i < 0 or i >= len(bars):
                return {"status": "OUT_OF_RANGE"}
            b = bars[i]
            return compute_regime_features_at_close(bar=b, bars=bars, prices=prices, emas=emas)

        out.append(
            {
                "sl_bar_utc": sl_bar,
                "position_id": e.get("position_id"),
                "episode_phase": "AT_SL",
                "data_source_ohlc": PRICE_FEED_NOTE,
                "data_source_density": DENSITY_NOTE_CACHE if dens else "CACHE_UNAVAILABLE",
                "regime_at_sl": _feat(0),
                "regime_3b_before": _feat(-3),
                "regime_3b_after": _feat(3),
                "research_note": "SL triggers re-evaluation only; not proof downtrend ended",
            }
        )
    return out

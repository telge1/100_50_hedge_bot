#!/usr/bin/env python3
"""BT-D60 regime counter-test — only after EMA history validation passes."""

from __future__ import annotations

import csv
import json
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "tools/phase_bt_d60_ema_history_validation_v1"
OUT = ROOT / "results/phase_bt_d60_ema_history_v1"
MANIFEST = OUT / "ema_history_manifest.json"
REPORT = ROOT / "reports/PHASE_BT_D60_REGIME_GEGENPROBE_V1.md"

for p in (
    PKG,
    ROOT / "tools/phase_bt_d59_market_regime_pool_evolution_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from ema_definitions_v1 import EMA_PERIODS, ema_series_for_path  # noqa: E402
from ohlc_loader_v1 import _iso, _parse_utc, load_5m_ohlc_ch  # noqa: E402
from regime_features_v1 import compute_regime_features_at_close  # noqa: E402
from research_windows_v1 import CONTROL_WINDOWS, REFERENCE_WINDOWS  # noqa: E402
from validation_config_v1 import FIRST_DECISION_CLOSE, PROBE_DECISION_CLOSES, REFERENCE_WARMUP_HOURS  # noqa: E402


def _require_manifest() -> dict:
    if not MANIFEST.is_file():
        raise SystemExit("Missing ema_history_manifest.json — run run_ema_history_validation.py first")
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if data.get("verdict") != "EMA_HISTORY_VALIDATED":
        raise SystemExit(
            f"Blocked: verdict={data.get('verdict')!r} — regime Gegenprobe requires EMA_HISTORY_VALIDATED"
        )
    return data


def _ema_map_trp(bar_keys: list[str], prices: dict) -> dict[int, dict[str, float]]:
    closes = [float(prices[b]["c"]) for b in bar_keys]
    ema: dict[int, dict[str, float]] = {p: {} for p in EMA_PERIODS}
    for period in EMA_PERIODS:
        series = ema_series_for_path("trp_research_charts", closes, period)
        for i, bar in enumerate(bar_keys):
            v = series[i]
            if v is not None:
                ema[period][bar] = float(v)
    return ema


def main() -> int:
    _require_manifest()
    first = _parse_utc(FIRST_DECISION_CLOSE)
    load_start = first - timedelta(hours=REFERENCE_WARMUP_HOURS + 24)
    load_end = max(_parse_utc(p) for p in PROBE_DECISION_CLOSES)
    bar_keys, prices, _meta = load_5m_ohlc_ch(_iso(load_start), _iso(load_end))
    emas = _ema_map_trp(bar_keys, prices)
    first_ready = next(
        (i for i, b in enumerate(bar_keys) if all(b in emas[p] for p in EMA_PERIODS)),
        len(bar_keys),
    )
    bars_regime = bar_keys[first_ready:]

    windows = REFERENCE_WINDOWS + CONTROL_WINDOWS
    rows: list[dict] = []
    for win in windows:
        start, end = win["start_utc"], win["end_utc"]
        for bar in bars_regime:
            if bar < start or bar > end:
                continue
            feats = compute_regime_features_at_close(bar=bar, bars=bars_regime, prices=prices, emas=emas)
            feats["window_id"] = win["window_id"]
            feats["ema_path"] = "trp_research_charts"
            rows.append(feats)

    OUT.mkdir(parents=True, exist_ok=True)
    out_csv = OUT / "regime_gegenprobe_asof.csv"
    if rows:
        with out_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    REPORT.write_text(
        "\n".join(
            [
                "# BT-D60 — Regime Gegenprobe V1",
                "",
                "EMA path: **TRP Research Charts** (SMA seed), long ClickHouse OHLC warmup.",
                "",
                f"Rows written: **{len(rows)}** → `{out_csv.relative_to(ROOT)}`",
                "",
                "Compare key columns (`ema9_lt_20_lt_59_lt_200`, slopes, chop) against BT-D59 preliminary CSVs.",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps({"rows": len(rows), "report": str(REPORT)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

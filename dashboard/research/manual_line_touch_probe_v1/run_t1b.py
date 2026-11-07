#!/usr/bin/env python3
"""T1B: 24h visual touch smoke test (2026-08-26 .. 2026-08-27 UTC, exclusive end)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

PROBE_DIR = Path(__file__).resolve().parent
if str(PROBE_DIR) not in sys.path:
    sys.path.insert(0, str(PROBE_DIR))

import run_probe as rp  # noqa: E402
from line_geometry import utc_ts  # noqa: E402

HITS = ("TOUCH", "NEAR_0_10", "NEAR_0_20")


def main() -> int:
    out = PROBE_DIR / "t1b"
    out.mkdir(parents=True, exist_ok=True)
    rp.OUT_DIR = out
    rp.WINDOW_START = utc_ts(datetime(2026, 8, 26, 0, 0, 0, tzinfo=timezone.utc))
    rp.WINDOW_END = utc_ts(datetime(2026, 8, 27, 0, 0, 0, tzinfo=timezone.utc))
    code = rp.main()

    rows = sorted(
        [
            r
            for r in getattr(rp, "_LAST_TOUCH_ROWS", [])
            if r.classification in HITS and r.distance_method != "inactive"
        ],
        key=lambda r: (r.timestamp_utc, r.drawing_id),
    )

    lines = [
        "# T1B hit list — DOGEUSDT 5m",
        f"Window: {rp.fmt_utc(rp.WINDOW_START)} .. {rp.fmt_utc(rp.WINDOW_END)} (end exclusive)",
        f"Hits: {len(rows)} (TOUCH / NEAR_0_10 / NEAR_0_20 only)",
        "",
        "| UTC | drawing_id | class | line_price | high | low | dist_% |",
        "|-----|------------|-------|------------|------|-----|--------|",
    ]
    for r in rows:
        lines.append(
            f"| {r.timestamp_utc} | `{r.drawing_id}` | {r.classification} | "
            f"{r.line_price:.8f} | {r.candle_high:.8f} | {r.candle_low:.8f} | {r.distance_pct:.4f} |"
        )
        print(
            f"{r.timestamp_utc}\t{r.drawing_id}\t{r.classification}\t"
            f"line={r.line_price:.8f}\thi={r.candle_high:.8f}\tlo={r.candle_low:.8f}\t"
            f"dist={r.distance_pct:.4f}%"
        )
    (out / "t1b_hits.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nT1B outputs: {out}/ (chart=diagnostic_chart.png, hits={len(rows)})")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

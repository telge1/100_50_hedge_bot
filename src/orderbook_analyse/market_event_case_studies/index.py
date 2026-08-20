"""Build case_index.csv / case_index.md from report summaries."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


INDEX_COLUMNS = [
    "case_id",
    "case_type",
    "symbol",
    "event_time",
    "event_minute_return",
    "delta_ratio",
    "trade_delta",
    "primary_classification",
    "future_return_60m",
    "future_return_240m",
    "long_mfe_60m",
    "long_mae_60m",
    "short_mfe_60m",
    "short_mae_60m",
    "long_mfe_240m",
    "long_mae_240m",
    "short_mfe_240m",
    "short_mae_240m",
    "nearest_upper_pool_distance_bps",
    "nearest_lower_pool_distance_bps",
    "report_path",
]


def row_from_summary(
    *,
    case_id: str,
    case_type: str,
    symbol: str,
    event_time: str,
    report_path: str,
    summary: dict[str, Any],
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fb = fallback or {}
    price = summary.get("price") or {}
    known = price.get("known_before_event") or {}
    event = price.get("event_minute") or {}
    after = price.get("after_event") or {}
    path = price.get("path_metrics") or {}
    p60 = path.get("60m") or {}
    p240 = path.get("240m") or {}
    trades = summary.get("trades") or {}
    te = trades.get("event_minute") or {}
    lld = summary.get("lld") or {}
    cls = summary.get("classification") or {}

    def g(*keys, default=None):
        cur: Any = summary
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur if cur is not None else default

    return {
        "case_id": case_id,
        "case_type": case_type,
        "symbol": symbol,
        "event_time": event_time,
        "event_minute_return": event.get("event_minute_return", fb.get("event_minute_return")),
        "delta_ratio": te.get("delta_ratio", fb.get("delta_ratio")),
        "trade_delta": te.get("trade_delta", fb.get("trade_delta")),
        "primary_classification": cls.get("primary"),
        "future_return_60m": after.get("future_return_60m", fb.get("ret_60m")),
        "future_return_240m": after.get("future_return_240m", fb.get("ret_240m")),
        "long_mfe_60m": (p60.get("LONG") or {}).get("mfe", fb.get("long_mfe_60m")),
        "long_mae_60m": (p60.get("LONG") or {}).get("mae", fb.get("long_mae_60m")),
        "short_mfe_60m": (p60.get("SHORT") or {}).get("mfe", fb.get("short_mfe_60m")),
        "short_mae_60m": (p60.get("SHORT") or {}).get("mae", fb.get("short_mae_60m")),
        "long_mfe_240m": (p240.get("LONG") or {}).get("mfe", fb.get("long_mfe_240m")),
        "long_mae_240m": (p240.get("LONG") or {}).get("mae", fb.get("long_mae_240m")),
        "short_mfe_240m": (p240.get("SHORT") or {}).get("mfe", fb.get("short_mfe_240m")),
        "short_mae_240m": (p240.get("SHORT") or {}).get("mae", fb.get("short_mae_240m")),
        "nearest_upper_pool_distance_bps": lld.get("distance_upper_bps"),
        "nearest_lower_pool_distance_bps": lld.get("distance_lower_bps"),
        "report_path": report_path,
        # unused but keep linter quiet
        "_known_dummy": known.get("return_1m"),
        "_g": g("price"),
    }


def write_case_index(rows: list[dict[str, Any]], output_dir: Path) -> tuple[Path, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "case_index.csv"
    md_path = output_dir / "case_index.md"

    clean_rows = []
    for r in rows:
        clean_rows.append({k: r.get(k) for k in INDEX_COLUMNS})

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=INDEX_COLUMNS)
        w.writeheader()
        for r in clean_rows:
            w.writerow(r)

    lines = [
        "# Market Event Case Study Index",
        "",
        f"Cases: **{len(clean_rows)}**",
        "",
        "| " + " | ".join(INDEX_COLUMNS[:8]) + " | ... |",
        "| " + " | ".join(["---"] * 8) + " | --- |",
    ]
    for r in clean_rows:
        lines.append(
            "| "
            + " | ".join(
                str(r.get(c) if r.get(c) is not None else "")
                for c in INDEX_COLUMNS[:8]
            )
            + f" | `{r.get('report_path')}` |"
        )
    lines.append("")
    # Compact type counts
    from collections import Counter

    counts = Counter(r["case_type"] for r in clean_rows)
    lines.append("## Counts by case_type")
    lines.append("")
    for k, v in sorted(counts.items()):
        lines.append(f"- `{k}`: {v}")
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return csv_path, md_path

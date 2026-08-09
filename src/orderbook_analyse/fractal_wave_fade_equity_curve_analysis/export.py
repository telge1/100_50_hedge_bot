"""Export equity/reserve curve artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.fractal_wave_fade_equity_curve_analysis import DEFINITIONS_DOC


def _jsonable(x: Any) -> Any:
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, pd.Timestamp):
        t = x
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        else:
            t = t.tz_convert("UTC")
        return t.strftime("%Y-%m-%d %H:%M:%S UTC")
    if hasattr(x, "item"):
        try:
            return x.item()
        except Exception:
            pass
    if isinstance(x, float) and (x != x):
        return None
    return x


def write_results(payload: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    all_df: pd.DataFrame = payload["equity_curve_all"]
    p = out_dir / "equity_curve_all_leverage.csv"
    all_df.to_csv(p, index=False)
    paths["equity_curve_all"] = p

    p = out_dir / "monthly_equity_reserve.csv"
    payload["monthly"].to_csv(p, index=False)
    paths["monthly"] = p

    p = out_dir / "reserve_events.csv"
    payload["reserve_events"].to_csv(p, index=False)
    paths["reserve_events"] = p

    p = out_dir / "leverage_summaries.csv"
    payload["summaries_df"].to_csv(p, index=False)
    paths["summaries"] = p

    p = out_dir / "DEFINITIONS.md"
    p.write_text(DEFINITIONS_DOC.strip() + "\n", encoding="utf-8")
    paths["definitions"] = p

    p = out_dir / "summary.json"
    p.write_text(json.dumps(_jsonable(payload["summary"]), indent=2) + "\n", encoding="utf-8")
    paths["summary"] = p

    # plot paths already written by plots module
    for k, v in payload.get("plot_paths", {}).items():
        paths[k] = Path(v)

    return paths

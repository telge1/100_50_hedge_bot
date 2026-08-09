"""Write parity smoke artifacts."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pandas as pd


def _write_csv(path: Path, rows: list[dict[str, Any]] | pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(rows, pd.DataFrame):
        rows.to_csv(path, index=False)
        return
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})


def _j(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _j(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_j(v) for v in obj]
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if hasattr(obj, "item"):
        try:
            return obj.item()
        except Exception:
            return str(obj)
    return obj


def write_artifacts(result: dict[str, Any], out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    win = result.get("win") or {}
    coverage = {
        "decision": result.get("decision"),
        "comparison_start": _j(win.get("comparison_start")),
        "comparison_end": _j(win.get("comparison_end")),
        "mysql_range": {
            "start": _j(win.get("mysql_start")),
            "end": _j(win.get("mysql_end")),
        },
        "feather_range": {
            "start": _j(win.get("feather_start")),
            "end": _j(win.get("feather_end")),
        },
        "mysql_quality": result.get("mysql_q"),
        "raw_5m": {k: v for k, v in (result.get("raw_parity") or {}).items() if k != "examples"},
        "resample_1h": {
            k: v for k, v in (result.get("resample_1h") or {}).items() if k != "examples"
        },
        "resample_4h": {
            k: v for k, v in (result.get("resample_4h") or {}).items() if k != "examples"
        },
        "extras": result.get("extras"),
    }
    (out_dir / "coverage_comparison.json").write_text(
        json.dumps(_j(coverage), indent=2), encoding="utf-8"
    )

    _write_csv(out_dir / "raw_5m_parity.csv", result.get("raw_rows") or [])
    _write_csv(out_dir / "resample_1h_parity.csv", result.get("r1_rows") or [])
    _write_csv(out_dir / "resample_4h_parity.csv", result.get("r4_rows") or [])

    m_ev = result.get("mysql_ev_df")
    f_ev = result.get("feather_ev_df")
    if isinstance(m_ev, pd.DataFrame):
        m_ev.to_csv(out_dir / "mysql_break_events.csv", index=False)
    else:
        _write_csv(out_dir / "mysql_break_events.csv", [])
    if isinstance(f_ev, pd.DataFrame):
        f_ev.to_csv(out_dir / "feather_break_events.csv", index=False)
    else:
        _write_csv(out_dir / "feather_break_events.csv", [])

    p_df = result.get("parity_df")
    if isinstance(p_df, pd.DataFrame):
        p_df.to_csv(out_dir / "break_event_parity.csv", index=False)
    else:
        _write_csv(out_dir / "break_event_parity.csv", [])

    summary = {
        "primary_decision": result.get("decision"),
        "comparison_window": {
            "start": _j(win.get("comparison_start")),
            "end": _j(win.get("comparison_end")),
        },
        "raw_5m": {k: v for k, v in (result.get("raw_parity") or {}).items() if k != "examples"},
        "resample_1h": {
            k: v for k, v in (result.get("resample_1h") or {}).items() if k != "examples"
        },
        "resample_4h": {
            k: v for k, v in (result.get("resample_4h") or {}).items() if k != "examples"
        },
        "break_parity": result.get("parity_stats"),
        "causality": result.get("causal"),
        "extras": result.get("extras"),
        "stop_reason": (result.get("extras") or {}).get("stop_reason"),
    }
    (out_dir / "summary.json").write_text(json.dumps(_j(summary), indent=2), encoding="utf-8")
    (out_dir / "summary.md").write_text(_render_md(result), encoding="utf-8")
    return out_dir


def _render_md(result: dict[str, Any]) -> str:
    win = result.get("win") or {}
    raw = result.get("raw_parity") or {}
    r1 = result.get("resample_1h") or {}
    r4 = result.get("resample_4h") or {}
    ps = result.get("parity_stats") or {}
    by = ps.get("by_tf_side") or {}
    causal = result.get("causal") or {}
    lines = [
        "# APT MySQL ↔ Feather Trend Scanner Parity",
        "",
        f"## Primärentscheidung",
        "",
        f"`{result.get('decision')}`",
        "",
        "## Gemeinsames Fenster",
        "",
        f"- Start: `{_j(win.get('comparison_start'))}`",
        f"- Ende: `{_j(win.get('comparison_end'))}`",
        f"- MySQL full: `{_j(win.get('mysql_start'))}` → `{_j(win.get('mysql_end'))}`",
        f"- Feather full: `{_j(win.get('feather_start'))}` → `{_j(win.get('feather_end'))}`",
        "",
        "## Raw 5m",
        "",
        f"- Rows MySQL / Feather: {raw.get('n_mysql')} / {raw.get('n_feather')}",
        f"- Both: {raw.get('n_both')}",
        f"- Missing L/R: {raw.get('missing_in_feather')} / {raw.get('missing_in_mysql')}",
        f"- OHLC mismatches (open/high/low/close): "
        f"{raw.get('open_mismatch')}/{raw.get('high_mismatch')}/"
        f"{raw.get('low_mismatch')}/{raw.get('close_mismatch')}",
        f"- Volume mismatches: {raw.get('volume_mismatch')}",
        "",
        "## 1h / 4h Resample",
        "",
        f"- 1h counts MySQL/Feather: {r1.get('n_mysql')} / {r1.get('n_feather')}; "
        f"OHLC mm={r1.get('open_mismatch')}/{r1.get('high_mismatch')}/"
        f"{r1.get('low_mismatch')}/{r1.get('close_mismatch')}; vol={r1.get('volume_mismatch')}",
        f"- 4h counts MySQL/Feather: {r4.get('n_mysql')} / {r4.get('n_feather')}; "
        f"OHLC mm={r4.get('open_mismatch')}/{r4.get('high_mismatch')}/"
        f"{r4.get('low_mismatch')}/{r4.get('close_mismatch')}; vol={r4.get('volume_mismatch')}",
        "",
        "## Break Events",
        "",
        "| TF | Side | MySQL | Feather | Exact | Only MySQL | Only Feather | Level mismatch |",
        "|----|------|------:|--------:|------:|-----------:|-------------:|---------------:|",
    ]
    for tf in ("1h", "4h"):
        for side in ("PH_break", "PL_break"):
            s = by.get(f"{tf}|{side}") or {}
            lines.append(
                f"| {tf} | {side} | {s.get('mysql', 0)} | {s.get('feather', 0)} | "
                f"{s.get('exact', 0)} | {s.get('mysql_only', 0)} | {s.get('feather_only', 0)} | "
                f"{s.get('level_mismatch', 0)} |"
            )
    lines += ["", "## Kausalität", ""]
    for k, v in causal.items():
        lines.append(f"- `{k}`: **{v}**")
    lines += [
        "",
        "## Nächster Schritt",
        "",
    ]
    d = result.get("decision")
    if d in ("PARITY_GREEN", "PARITY_GREEN_WITH_MINOR_SOURCE_DIFFERENCES"):
        lines.append(
            "Parity ausreichend → Full Trend-Quality-Audit auf APT/DOGE/BTC (1h/4h) starten."
        )
    else:
        lines.append("Zuerst Root Cause der Paritätsabweichung beheben; kein Full-Audit.")
    lines.append("")
    return "\n".join(lines)

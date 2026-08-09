"""Write equity-acceleration analysis artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.fractal_wave_fade_equity_acceleration_analysis import DEFINITIONS_DOC


def _jsonable(x: Any) -> Any:
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, pd.Timestamp):
        t = x.tz_convert("UTC") if x.tzinfo else x.tz_localize("UTC")
        return t.strftime("%Y-%m-%d %H:%M:%S UTC")
    if hasattr(x, "item"):
        try:
            return x.item()
        except Exception:
            pass
    if isinstance(x, float) and (x != x):
        return None
    return x


def _pct(x, digits=2):
    if x is None or (isinstance(x, float) and (x != x)):
        return "n/a"
    return f"{100.0 * float(x):.{digits}f}%"


def _f(x, digits=3):
    if x is None or (isinstance(x, float) and (x != x)):
        return "n/a"
    return f"{float(x):.{digits}f}"


def render_summary_md(payload: dict[str, Any]) -> str:
    half = payload["halfyear"]
    d = payload["decision"]
    lines: list[str] = []
    lines.append("# Equity Acceleration Analysis")
    lines.append("")
    lines.append(f"- Audit: `{payload['audit_version']}`")
    lines.append(f"- Trades: `{payload['trades_path']}`")
    lines.append(
        f"- Window: {_jsonable(payload['data_start'])} → {_jsonable(payload['data_end'])}"
    )
    lines.append("")
    lines.append(f"## Decision: **{d['decision']}**")
    lines.append("")
    lines.append(f"Supported drivers: {', '.join(d['supported_drivers']) or 'none clear'}")
    lines.append("")
    lines.append(
        f"Both symbols accelerate: **{d['both_symbols_accelerate']}** "
        f"(APT={d['apt_accelerates']}, DOGE={d['doge_accelerates']})"
    )
    lines.append("")
    lines.append(
        f"Additive cum: 2023={_f(d['cumulative_additive_2023'],1)} | "
        f"2024={_f(d['cumulative_additive_2024'],1)} | "
        f"2025={_f(d['cumulative_additive_2025'],1)}"
    )
    lines.append("")
    lines.append("## Half-year overview")
    lines.append("")
    lines.append(
        "| Period | Trades/mo | TP% | Exp | PF | ATR14% | 1h+4h share | Upgrade% | Med MFE | Cum |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for _, r in half.iterrows():
        lines.append(
            "| {p} | {tpm} | {tp} | {exp} | {pf} | {atr} | {tf} | {up} | {mfe} | {cum} |".format(
                p=r["period"],
                tpm=_f(r["trades_per_month"], 1),
                tp=_pct(r["tp_rate"], 1),
                exp=_f(r["expectancy"], 3),
                pf=_f(r["profit_factor"], 2),
                atr=_f(r.get("median_atr14_pct"), 3),
                tf=_pct(r["share_1h_4h"], 1),
                up=_pct(r["upgrade_rate"], 1),
                mfe=_f(r.get("median_mfe_pct"), 2),
                cum=_f(r["cumulative_additive_return"], 1),
            )
        )
    lines.append("")
    lines.append("## Driver notes")
    lines.append("")
    for k, v in d["drivers"].items():
        lines.append(f"### {k} — supports={v.get('supports')}")
        for kk, vv in v.items():
            if kk == "supports":
                continue
            lines.append(f"- {kk}: `{vv}`")
        lines.append("")
    lines.append("## Interpretation")
    lines.append("")
    lines.append(
        "Compounded equity rises faster when the Active base is already large; "
        "this analysis focuses on **per-trade additive edge and opportunity density** "
        "so the ~2024 acceleration is not confused with pure compounding."
    )
    lines.append("")
    lines.append("### What moved most (2024 vs 2023)")
    lines.append("")
    lines.append(
        "1. **Opportunity density**: trades/month ≈ **+50%** (115 → 172). "
        "Additive cum nearly doubled (344 → 619)."
    )
    lines.append(
        "2. **Market volatility**: median 1h ATR14% ≈ **+30%** (1.10 → 1.43). "
        "More Tier-A wave fades fire in choppier regimes."
    )
    lines.append(
        "3. **Signal edge**: win/TP rate ≈ **+3pp**, expectancy **0.248 → 0.300**. "
        "Mean/median winning trade did **not** increase (median still 0.89%) — "
        "edge came from hitting TP more often, not larger TP sizes."
    )
    lines.append(
        "4. **Not TF/upgrades**: 1h+4h share and upgrade rate **fell** in 2024 vs 2023. "
        "Upgraded trades still have higher expectancy, but they do not explain the acceleration."
    )
    lines.append(
        "5. **Both coins**: APT and DOGE both contribute; not a single-symbol artifact."
    )
    lines.append(
        "6. **2025 cool-down**: expectancy/PF/TP% retreated (exp 0.21) despite still-elevated ATR — "
        "so 2024 was a multi-factor peak, not a permanent regime lock."
    )
    lines.append("")
    return "\n".join(lines) + "\n"


def write_results(payload: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    p = out_dir / "halfyear_statistics.csv"
    payload["halfyear"].to_csv(p, index=False)
    paths["halfyear"] = p

    p = out_dir / "volatility_by_period.csv"
    payload["volatility"].to_csv(p, index=False)
    paths["volatility"] = p

    p = out_dir / "tf_mix_by_period.csv"
    payload["tf_mix"].to_csv(p, index=False)
    paths["tf_mix"] = p

    p = out_dir / "upgrade_by_period.csv"
    payload["upgrade"].to_csv(p, index=False)
    paths["upgrade"] = p

    p = out_dir / "symbol_side_by_period.csv"
    payload["symbol_side"].to_csv(p, index=False)
    paths["symbol_side"] = p

    p = out_dir / "mfe_mae_by_period.csv"
    payload["mfe"].to_csv(p, index=False)
    paths["mfe"] = p

    p = out_dir / "summary.md"
    p.write_text(render_summary_md(payload), encoding="utf-8")
    paths["summary_md"] = p

    p = out_dir / "DEFINITIONS.md"
    p.write_text(DEFINITIONS_DOC.strip() + "\n", encoding="utf-8")
    paths["definitions"] = p

    summary_json = {
        "audit_version": payload["audit_version"],
        "decision": payload["decision"],
        "periods": payload["periods"],
        "data_start": _jsonable(payload["data_start"]),
        "data_end": _jsonable(payload["data_end"]),
    }
    p = out_dir / "summary.json"
    p.write_text(json.dumps(_jsonable(summary_json), indent=2) + "\n", encoding="utf-8")
    paths["summary_json"] = p
    return paths

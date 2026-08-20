"""Orchestrate discovery + limited report generation for case studies."""

from __future__ import annotations

import json
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

from orderbook_analyse.market_event_case_studies.discover import (
    build_discovery_panel,
    discover_symbol_cases,
)
from orderbook_analyse.market_event_case_studies.index import row_from_summary, write_case_index
from orderbook_analyse.market_event_case_studies.select import (
    CaseCandidate,
    select_rare_confluence,
    select_top_n,
)
from orderbook_analyse.market_event_report.build import build_report, write_artifacts
from orderbook_analyse.market_event_report.loaders import (
    as_utc,
    fetch_candles_1m,
    fetch_oi_liq_optional,
    fetch_orderbook_1m,
    fetch_trades_1m,
    q,
)
from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

WARM = datetime(2026, 8, 10, 0, 0, 0)
START = datetime(2026, 8, 11, 0, 0, 0)
END = datetime(2026, 8, 18, 0, 0, 0)  # exclusive
STUDY_NAME = "20260811_17_51coins"
TOP_N = 5


def list_symbols(client) -> list[str]:
    rows = q(
        client,
        """
        SELECT symbol
        FROM orderbook_analysis.orderbook_features_1s_v2 FINAL
        WHERE parser_version='ob200_v3' AND depth=200
          AND bucket_start >= {a:DateTime64(3,'UTC')}
          AND bucket_start <  {b:DateTime64(3,'UTC')}
        GROUP BY symbol
        HAVING countDistinct(toDate(bucket_start)) >= 7
        ORDER BY symbol
        """,
        {"a": as_utc(START), "b": as_utc(END)},
    )
    return [r[0] for r in rows]


def select_cases(raw: dict[str, list[CaseCandidate]]) -> list[CaseCandidate]:
    selected: list[CaseCandidate] = []
    selected.extend(select_top_n(raw.get("long_big_move") or [], TOP_N))
    selected.extend(select_top_n(raw.get("short_big_move") or [], TOP_N))
    selected.extend(select_top_n(raw.get("flow_opposed_reversal") or [], TOP_N))
    selected.extend(select_top_n(raw.get("flow_aligned_move") or [], TOP_N))
    selected.extend(select_top_n(raw.get("failed_directional") or [], TOP_N))
    selected.extend(select_rare_confluence(raw.get("rare_confluence") or []))
    seen: set[tuple[str, str, datetime]] = set()
    uniq: list[CaseCandidate] = []
    for c in selected:
        key = (c.case_type, c.symbol, c.event_time)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    return uniq


def _load_symbol(client, symbol: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    end_incl = END + timedelta(hours=5) - timedelta(minutes=1)
    end_excl = END + timedelta(hours=5)
    candles = fetch_candles_1m(client, symbol, WARM, end_incl)
    trades = fetch_trades_1m(client, symbol, WARM, end_excl)
    ob = fetch_orderbook_1m(client, symbol, WARM, end_excl)
    return candles, trades, ob


def run_case_studies(
    *,
    output_root: Path,
    trp_root: Path | None = None,
    skip_oi_liq: bool = False,
    max_symbols: int | None = None,
) -> dict[str, Any]:
    root = Path("/home/telgenbuescher/projects/orderbook_analyse")
    load_dotenv(root / ".env", override=False)

    study_dir = Path(output_root) / STUDY_NAME
    study_dir.mkdir(parents=True, exist_ok=True)

    client = get_clickhouse_client()
    symbols = list_symbols(client)
    if max_symbols is not None:
        symbols = symbols[:max_symbols]

    raw_all: dict[str, list[CaseCandidate]] = defaultdict(list)
    found_counts: Counter[str] = Counter()
    errors: list[dict[str, str]] = []

    t0 = time.time()
    print(f"discovering cases for {len(symbols)} symbols…")
    for i, symbol in enumerate(symbols, 1):
        try:
            candles, trades, ob = _load_symbol(client, symbol)
            if candles.empty:
                continue
            panel = build_discovery_panel(candles, trades, ob)
            found = discover_symbol_cases(panel, symbol=symbol, start=START, end_exclusive=END)
            for k, lst in found.items():
                raw_all[k].extend(lst)
                found_counts[k] += len(lst)
            if i % 5 == 0 or i == len(symbols):
                print(f"  [{i}/{len(symbols)}] {symbol} done; rare={found_counts['rare_confluence']}")
        except Exception as exc:  # noqa: BLE001
            errors.append({"symbol": symbol, "stage": "discover", "error": f"{type(exc).__name__}:{exc}"})
            traceback.print_exc()

    # Global top-N across coins (cooldown already per-symbol)
    selected = select_cases(dict(raw_all))
    print(f"selected {len(selected)} cases for reporting")

    index_rows: list[dict[str, Any]] = []
    report_ok = 0
    # Cache per-symbol frames for report generation
    cache: dict[str, tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]] = {}

    for j, case in enumerate(selected, 1):
        rel = case.report_relpath()
        out_dir = study_dir / rel
        try:
            if case.symbol not in cache:
                cache[case.symbol] = _load_symbol(client, case.symbol)
            candles, trades, ob = cache[case.symbol]
            # Slice enough context for report builder (2d warm already in load)
            event_t = case.event_time
            if skip_oi_liq:
                oi_liq = {"available": False, "reason": "skipped_by_flag"}
            else:
                oi_liq = fetch_oi_liq_optional(
                    client,
                    case.symbol,
                    event_t - timedelta(minutes=15),
                    event_t + timedelta(minutes=241),
                )
            report = build_report(
                symbol=case.symbol,
                event_time_utc=event_t.replace(tzinfo=timezone.utc),
                candles=candles,
                trades=trades,
                orderbook=ob,
                oi_liq=oi_liq,
                trp_root=trp_root,
            )
            write_artifacts(out_dir, report=report, candles=candles, trades=trades, orderbook=ob)
            # Attach selection meta into summary sidecar
            meta_path = out_dir / "case_meta.json"
            meta_path.write_text(
                json.dumps(
                    {
                        "case_id": case.case_id,
                        "case_type": case.case_type,
                        "score": case.score,
                        "meta": case.meta,
                    },
                    indent=2,
                    default=str,
                )
                + "\n",
                encoding="utf-8",
            )
            row = row_from_summary(
                case_id=case.case_id,
                case_type=case.case_type,
                symbol=case.symbol,
                event_time=event_t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                report_path=str(out_dir.relative_to(study_dir)),
                summary=report["summary"],
                fallback=case.meta,
            )
            # Drop helper keys
            index_rows.append({k: row[k] for k in row if not k.startswith("_")})
            report_ok += 1
            print(f"  report [{j}/{len(selected)}] {case.case_id} -> {out_dir}")
        except Exception as exc:  # noqa: BLE001
            errors.append(
                {
                    "symbol": case.symbol,
                    "stage": "report",
                    "case_id": case.case_id,
                    "error": f"{type(exc).__name__}:{exc}",
                }
            )
            traceback.print_exc()

    csv_path, md_path = write_case_index(index_rows, study_dir)

    summary = {
        "study": STUDY_NAME,
        "window_start": START.isoformat() + "Z",
        "window_end_exclusive": END.isoformat() + "Z",
        "n_symbols": len(symbols),
        "found_counts": dict(found_counts),
        "n_selected": len(selected),
        "n_reports_ok": report_ok,
        "index_csv": str(csv_path),
        "index_md": str(md_path),
        "elapsed_s": round(time.time() - t0, 1),
        "errors": errors,
        "selected_case_ids": [c.case_id for c in selected],
    }
    (study_dir / "run_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    _write_human_summary(study_dir, summary, index_rows)
    return summary


def _write_human_summary(study_dir: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    df = pd.DataFrame(rows) if rows else pd.DataFrame()

    def top_interesting(case_type: str, n: int = 5) -> list[dict[str, Any]]:
        if df.empty:
            return []
        sub = df.loc[df["case_type"] == case_type].copy()
        if sub.empty:
            return []
        if case_type == "flow_opposed_reversal":
            sub["_s"] = sub["future_return_60m"].abs()
        elif case_type == "flow_aligned_move":
            sub["_s"] = sub["future_return_60m"].abs()
        elif case_type == "long_big_move":
            sub["_s"] = sub["long_mfe_60m"]
        elif case_type == "short_big_move":
            sub["_s"] = sub["short_mfe_60m"]
        else:
            sub["_s"] = sub["future_return_60m"].abs()
        sub = sub.sort_values("_s", ascending=False).head(n)
        return sub.to_dict(orient="records")

    rev = top_interesting("flow_opposed_reversal")
    cont = top_interesting("flow_aligned_move")

    # Pattern notes from selected index
    patterns = []
    if not df.empty and "flow_opposed_reversal" in set(df["case_type"]):
        opp = df.loc[df["case_type"] == "flow_opposed_reversal"]
        patterns.append(
            f"Flow-opposed selected n={len(opp)}; mean |ret60|="
            f"{opp['future_return_60m'].abs().mean():.4%}" if opp["future_return_60m"].notna().any() else "n/a"
        )
    if not df.empty:
        with_lld = df.dropna(subset=["nearest_upper_pool_distance_bps", "nearest_lower_pool_distance_bps"], how="all")
        patterns.append(f"LLD distance present on {len(with_lld)}/{len(df)} selected reports")

    lines = [
        "# Market Event Case Studies — Summary",
        "",
        f"Window: `{summary['window_start']}` → `{summary['window_end_exclusive']}`",
        f"Symbols: {summary['n_symbols']}",
        f"Reports OK: **{summary['n_reports_ok']}** / selected {summary['n_selected']}",
        f"Elapsed: {summary['elapsed_s']}s",
        "",
        "## Found (after per-coin cooldown)",
        "",
    ]
    for k, v in sorted((summary.get("found_counts") or {}).items()):
        lines.append(f"- `{k}`: {v}")
    lines.extend(["", "## Selected for reports", ""])
    # counts by type in index
    if not df.empty:
        for k, v in df["case_type"].value_counts().items():
            lines.append(f"- `{k}`: {v}")
    lines.extend(["", "## 5 interesting reversal cases (flow-opposed)", ""])
    for r in rev:
        lines.append(
            f"- `{r['symbol']}` `{r['event_time']}` ret60={r.get('future_return_60m')} "
            f"delta_ratio={r.get('delta_ratio')} class={r.get('primary_classification')} "
            f"path=`{r.get('report_path')}`"
        )
    lines.extend(["", "## 5 interesting continuation cases (flow-aligned)", ""])
    for r in cont:
        lines.append(
            f"- `{r['symbol']}` `{r['event_time']}` ret60={r.get('future_return_60m')} "
            f"delta_ratio={r.get('delta_ratio')} class={r.get('primary_classification')} "
            f"path=`{r.get('report_path')}`"
        )
    lines.extend(
        [
            "",
            "## Pattern notes (selected set only)",
            "",
        ]
    )
    for p in patterns:
        lines.append(f"- {p}")
    lines.extend(
        [
            "- Sell-flow → reversal vs continuation: compare `flow_opposed_reversal` vs `flow_aligned_move` folders.",
            "- OB imbalance vs move: inspect event `imbalance_l50` / OFI in each `report.md`.",
            "- LLD/pool proximity: see `nearest_*_pool_distance_bps` in `case_index.csv`.",
            "",
            "## Suggested next manual reading",
            "",
            "1. All `flow_opposed_reversal` reports (likely highest learning value).",
            "2. `rare_confluence` winners vs losers (1h/4h) via `case_meta.json`.",
            "3. `failed_directional` vs matching big-move same coin/day.",
            "",
        ]
    )
    (study_dir / "SUMMARY.md").write_text("\n".join(lines), encoding="utf-8")


def main_summary_text(summary: dict[str, Any], study_dir: Path) -> str:
    path = study_dir / "SUMMARY.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return json.dumps(summary, indent=2)

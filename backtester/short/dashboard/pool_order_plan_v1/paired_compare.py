"""ACEUSDT 48h paired baseline vs POOL_ORDER_PLAN_V1. Research only. No publish."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

from .batch import run_batch
from .candles import ensure_utc
from .config import REPO_ROOT
from .dedupe import dedupe_signals
from .metrics import dashboard_style_summary, decide, display_round, strategy_stats
from .schema import STATUS_READY


COLLECTOR = os.environ.get("STOCH_COLLECTOR_API_BASE", "http://127.0.0.1:8787").rstrip("/")


def _iso(ts) -> str:
    return ensure_utc(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, default=str) + "\n")


def fetch_frozen_feed(*, hours: int = 48, symbol: str | None = None, as_of: datetime | None = None) -> dict[str, Any]:
    end = ensure_utc(as_of) if as_of is not None else datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    params = {
        "start": _iso(start),
        "end": _iso(end),
        "time_field": "candle_close_time",
        "limit": "500",
        "offset": "0",
        "tier_a": "true",
        "strategy_version": "wave_fade_no_be50_v1",
    }
    if symbol:
        params["symbol"] = str(symbol).upper()
    url = f"{COLLECTOR}/api/signals?{urlencode(params)}"
    with urlopen(url, timeout=90) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    payload["_frozen"] = {
        "snapshot_as_of_utc": _iso(end),
        "window_start_utc": _iso(start),
        "window_end_utc": _iso(end),
        "start_inclusive": True,
        "end_inclusive": False,
        "time_field": "candle_close_time",
        "filter_basis": "candle_close_time",
        "api_timezone": "UTC",
        "database_timezone": "UTC",
        "frontend_display_timezone": "UTC",
        "request_url": url,
        "hours": hours,
        "symbol_filter": symbol,
    }
    return payload


def slim_baseline_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "signal_id": row.get("signal_id"),
        "symbol": str(row.get("symbol") or "").upper(),
        "direction": str(row.get("direction") or "").upper(),
        "timeframe": row.get("timeframe"),
        "entry_time": row.get("entry_time"),
        "entry_price": row.get("entry_price"),
        "tp_price": row.get("tp_price"),
        "sl_price": row.get("sl_price"),
        "result": row.get("result") or row.get("display_result"),
        "exit_time": row.get("exit_time"),
        "pnl_pct": row.get("pnl_pct"),
        "fees_pct": 0.0,
        "net_pnl_pct": row.get("pnl_pct"),
        "available_at": row.get("candle_close_time"),
        "created_at": row.get("generated_at"),
        "candle_close_time": row.get("candle_close_time"),
        "pnl_basis": "gross",
    }


def matches_observed_kpis(summary: dict[str, Any]) -> bool:
    disp = display_round(summary)
    return (
        disp["signals"] == 22
        and disp["wins"] == 15
        and disp["losses"] == 7
        and disp["open"] == 0
        and disp["win_rate_pct_1dp"] == 68.2
        and disp["gross_profit_pct_1dp"] == 25.0
        and disp["gross_loss_pct_1dp"] == -8.0
        and disp["total_pnl_pct_1dp"] == 17.0
    )


def run_paired_aceusdt_48h(*, as_of: datetime | None = None, out_root: Path | None = None) -> dict[str, Any]:
    all_feed = fetch_frozen_feed(hours=48, symbol=None, as_of=as_of)
    frozen = all_feed["_frozen"]
    stamp = frozen["snapshot_as_of_utc"].replace(":", "").replace("-", "")
    out_dir = out_root or (REPO_ROOT / "results" / "pool_order_plan_v1_comparisons" / f"aceusdt_48h_{stamp}")
    out_dir.mkdir(parents=True, exist_ok=True)
    os.environ["POOL_ORDER_PLAN_ARTIFACT_DIR"] = str(out_dir / "pool_artifacts")

    all_rows = [slim_baseline_row(r) for r in (all_feed.get("signals") or [])]
    all_summary = dashboard_style_summary(all_rows)
    ace_feed = fetch_frozen_feed(hours=48, symbol="ACEUSDT", as_of=ensure_utc(frozen["snapshot_as_of_utc"]))
    ace_direct = [slim_baseline_row(r) for r in (ace_feed.get("signals") or [])]
    _write_jsonl(out_dir / "baseline_dashboard_raw.jsonl", all_rows)
    _write_json(
        out_dir / "baseline_dashboard_raw_summary.json",
        {
            "frozen": frozen,
            "observed_target": {
                "signals": 22,
                "wins": 15,
                "losses": 7,
                "open": 0,
                "win_rate_pct": 68.2,
                "gross_profit_pct": 25.0,
                "gross_loss_pct": -8.0,
                "total_pnl_pct": 17.0,
            },
            "reproduced_all_symbols": all_summary,
            "reproduced_display": display_round(all_summary),
            "matches_observed_kpis": matches_observed_kpis(all_summary),
            "kpi_source": "ALL_SYMBOLS_48H_UNFILTERED",
            "aceusdt_direct_n": len(ace_direct),
            "aceusdt_direct_summary": dashboard_style_summary(ace_direct),
            "symbol_counts": _counts(all_rows, "symbol"),
            "note": (
                "Dashboard KPIs 22/15/7 reproduce with NO symbol filter. "
                "ACEUSDT-only in the same frozen window is a subset."
            ),
            "pnl_basis": "gross",
            "fees_in_dashboard": False,
            "rounding": "sum then toFixed(1) in UI",
            "window": "half-open [start, end) on candle_close_time UTC",
        },
    )
    if not matches_observed_kpis(all_summary):
        return {"ok": False, "reason": "baseline_kpis_not_reproduced", "out_dir": str(out_dir)}

    split = dedupe_signals(ace_direct)
    winners = split["winners"]
    ignored = split["ignored"]
    _write_jsonl(out_dir / "paired_signal_winners.jsonl", winners)
    _write_jsonl(out_dir / "ignored_duplicates.jsonl", ignored)
    _write_json(
        out_dir / "baseline_deduped_summary.json",
        {
            "raw_aceusdt": len(ace_direct),
            "unique_entry_keys": len(winners),
            "duplicates": len(ignored),
            "duplicate_signal_ids": [r.get("signal_id") for r in ignored],
            "dashboard_kpis_include_non_ace": True,
            "before": dashboard_style_summary(ace_direct),
            "after": dashboard_style_summary(winners),
        },
    )

    as_of_dt = ensure_utc(frozen["snapshot_as_of_utc"])
    pool = run_batch(signals=winners, skip_pin=False, publish=False, outcome_as_of=as_of_dt)
    pool_by_id = {str(r.get("signal_id")): r for r in _read_jsonl(Path(pool["run_dir"]) / "outcomes.jsonl")}
    paired = []
    for w in winners:
        sid = str(w["signal_id"])
        p = pool_by_id.get(sid) or {}
        b_net = float(w.get("pnl_pct") or 0.0) if str(w.get("result")).upper() in ("WIN", "LOSS") else 0.0
        p_status = p.get("plan_status")
        p_net = 0.0 if p_status != STATUS_READY or str(p.get("outcome") or "OPEN") == "OPEN" else float(p.get("net_pnl_pct") or 0.0)
        paired.append(
            {
                "signal_id": sid,
                "entry_key": w.get("dedupe_key"),
                "symbol": w.get("symbol"),
                "direction": w.get("direction"),
                "timeframe": w.get("timeframe"),
                "entry_time": w.get("entry_time"),
                "entry_price": w.get("entry_price"),
                "baseline_outcome": w.get("result"),
                "baseline_sl": w.get("sl_price"),
                "baseline_tp": w.get("tp_price"),
                "baseline_gross_pnl_pct": w.get("pnl_pct") if str(w.get("result")).upper() in ("WIN", "LOSS") else None,
                "baseline_fees_pct": 0.0,
                "baseline_net_pnl_pct": w.get("pnl_pct") if str(w.get("result")).upper() in ("WIN", "LOSS") else None,
                "pool_plan_status": p.get("plan_status"),
                "pool_mode": p.get("initial_target_mode"),
                "pool_sl": p.get("sl_price"),
                "pool_tp1": p.get("tp1_price"),
                "pool_tp2": p.get("tp2_price"),
                "pool_tp1_size": p.get("tp1_size"),
                "pool_tp2_size": p.get("tp2_size"),
                "sl_too_wide": p.get("sl_too_wide"),
                "pool_outcome": p.get("outcome"),
                "pool_gross_pnl_pct": p.get("gross_pnl_pct"),
                "pool_fees_pct": p.get("fees_pct"),
                "pool_net_pnl_pct": p.get("net_pnl_pct"),
                "net_pnl_diff_pool_minus_baseline": p_net - b_net,
                "same_entry_time": True,
                "same_entry_price": True,
                "outcome_as_of": frozen["snapshot_as_of_utc"],
            }
        )
    _write_jsonl(out_dir / "paired_comparison.jsonl", paired)

    ready_ids = {r["signal_id"] for r in paired if r.get("pool_plan_status") == STATUS_READY}
    baseline_all = strategy_stats(winners, kind="baseline")
    pool_all_rows = []
    for w in winners:
        pool_all_rows.append(pool_by_id.get(str(w["signal_id"])) or {"plan_status": "NO_PLAN", "signal_id": w["signal_id"], "entry_time": w["entry_time"]})
    pool_all = strategy_stats(pool_all_rows, kind="pool")
    base_ready = strategy_stats([w for w in winners if str(w["signal_id"]) in ready_ids], kind="baseline")
    pool_ready = strategy_stats([p for p in pool_all_rows if str(p.get("signal_id")) in ready_ids], kind="pool")

    breakdowns = {
        "LONG": _both(winners, pool_all_rows, lambda r: str(r.get("direction")).upper() == "LONG"),
        "SHORT": _both(winners, pool_all_rows, lambda r: str(r.get("direction")).upper() == "SHORT"),
    }
    tfs = sorted({str(w.get("timeframe")) for w in winners})
    tf_bd = {tf: _both(winners, pool_all_rows, lambda r, t=tf: str(r.get("timeframe")) == t) for tf in tfs}
    sl_false = _pool_filter(winners, pool_all_rows, lambda p: p.get("plan_status") == STATUS_READY and not p.get("sl_too_wide"))
    sl_true = _pool_filter(winners, pool_all_rows, lambda p: p.get("plan_status") == STATUS_READY and p.get("sl_too_wide"))
    one = _pool_filter(winners, pool_all_rows, lambda p: p.get("initial_target_mode") == "ONE_VISIBLE_TARGET")
    two = _pool_filter(winners, pool_all_rows, lambda p: p.get("initial_target_mode") == "TWO_VISIBLE_TARGETS")
    no_plan = _pool_filter(winners, pool_all_rows, lambda p: p.get("plan_status") != STATUS_READY)

    cov = json.loads((Path(pool["run_dir"]) / "coverage.json").read_text(encoding="utf-8"))
    man = json.loads((Path(pool["run_dir"]) / "manifest.json").read_text(encoding="utf-8"))
    decision = decide(baseline_all["gross_pnl_pct"], pool_all["net_pnl_pct"], coverage_ready=len(ready_ids) / max(len(winners), 1))
    report = {
        "frozen": frozen,
        "BASELINE_VS_POOL_ALL": {"baseline": baseline_all, "pool": pool_all},
        "BASELINE_VS_POOL_READY_ONLY": {"baseline": base_ready, "pool": pool_ready, "n": len(ready_ids)},
        "DIRECTION_BREAKDOWN": breakdowns,
        "TIMEFRAME_BREAKDOWN": tf_bd,
        "SL_TOO_WIDE_BREAKDOWN": {"false": sl_false, "true": sl_true},
        "ONE_VS_TWO_TARGET_BREAKDOWN": {"one": one, "two": two, "no_plan": no_plan},
        "coverage": cov,
        "pool_manifest_window": man.get("window"),
        "pool_engine_runs": (man.get("counts") or {}).get("pool_engine_runs"),
        "published": pool.get("published"),
        "decision": decision,
        "out_dir": str(out_dir),
        "pool_run_dir": pool["run_dir"],
    }
    _write_json(out_dir / "paired_report.json", report)
    return report


def _counts(rows, key):
    out: dict[str, int] = {}
    for r in rows:
        k = str(r.get(key))
        out[k] = out.get(k, 0) + 1
    return out


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _both(winners, pool_rows, pred):
    w = [r for r in winners if pred(r)]
    ids = {str(r.get("signal_id")) for r in w}
    p = [r for r in pool_rows if str(r.get("signal_id")) in ids]
    return {"baseline": strategy_stats(w, kind="baseline"), "pool": strategy_stats(p, kind="pool")}


def _pool_filter(winners, pool_rows, pred):
    p = [r for r in pool_rows if pred(r)]
    ids = {str(r.get("signal_id")) for r in p}
    w = [r for r in winners if str(r.get("signal_id")) in ids]
    return {"baseline": strategy_stats(w, kind="baseline"), "pool": strategy_stats(p, kind="pool"), "n": len(p)}


if __name__ == "__main__":
    result = run_paired_aceusdt_48h()
    print(json.dumps({"decision": result.get("decision"), "out_dir": result.get("out_dir"), "reason": result.get("reason")}, indent=2), flush=True)

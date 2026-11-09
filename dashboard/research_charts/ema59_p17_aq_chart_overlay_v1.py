"""P17AQ — Executed P17 LONG trades + August Bar-C diagnostic markers (frozen P17AO/P17AP)."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from .ema59_6m_current_backtests_v1 import resolve_pools_ob_repo

P17AO_REL = (
    "results/phase_ema59_bt_p17_landscape_long_research_v1/"
    "p17ao_may_jun_sep_extension_backtest_v1.json"
)
P17AP_REL = (
    "results/phase_ema59_bt_p17_landscape_long_research_v1/"
    "p17ap_august_signal_count_audit_v1.json"
)
CSV_EXECUTED_REL = (
    "results/phase_ema59_bt_p17_landscape_long_research_v1/"
    "p17aq_executed_trades_chart_list_v1.csv"
)
CSV_AUGUST_REL = (
    "results/phase_ema59_bt_p17_landscape_long_research_v1/"
    "p17aq_august_candidates_chart_list_v1.csv"
)

EXEC_SOURCE = "p17aq_executed_trades_v1"
AUG_SOURCE = "p17aq_august_diagnostic_v1"
EXEC_STRATEGY_ID = "p17_landscape_long_executed_may_jun_aug_2026"
AUG_STRATEGY_ID = "p17_landscape_long_august_bar_c_diagnostic_2026"
EXEC_PREFIX = "p17aq-ex-"
AUG_MARKER_PREFIX = "p17aq-aug-mk-"
EXEC_MARKER_PREFIX = "p17aq-ex-mk-"

MarkerCategory = Literal[
    "EXECUTED_TRADE",
    "DIAGNOSTIC_PASS",
    "BLOCKED_SINGLE_ENTRY",
    "REJECTED_SL",
    "REJECTED_UPPER",
]


def _repo(root: Path | None) -> Path:
    return root or resolve_pools_ob_repo()


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def load_executed_trades(root: Path | None = None) -> list[dict[str, Any]]:
    path = _repo(root) / P17AO_REL
    doc = json.loads(path.read_text(encoding="utf-8"))
    rows = list(doc.get("trade_chart_references") or [])
    if len(rows) != 11:
        raise ValueError(f"P17AO expected 11 trade_chart_references, got {len(rows)}")
    return sorted(rows, key=lambda r: str(r.get("entry_utc") or ""))


def load_august_candidates(root: Path | None = None) -> list[dict[str, Any]]:
    path = _repo(root) / P17AP_REL
    doc = json.loads(path.read_text(encoding="utf-8"))
    rows = list(doc.get("section_b_bar_c_rows_all_58") or [])
    if len(rows) != 58:
        raise ValueError(f"P17AP expected 58 bar_c rows, got {len(rows)}")
    return sorted(rows, key=lambda r: str(r.get("entry_utc") or ""))


def single_entry_keys(root: Path | None = None) -> frozenset[tuple[str, str]]:
    path = _repo(root) / P17AP_REL
    doc = json.loads(path.read_text(encoding="utf-8"))
    pairs = doc.get("section_b_five_vs_four_single_entry", {}).get("single_entry_executables") or []
    return frozenset((str(a), str(b)) for a, b in pairs)


def classify_august_row(row: dict[str, Any], *, singles: frozenset[tuple[str, str]]) -> MarkerCategory:
    outcome = str(row.get("outcome") or "")
    key = (str(row.get("cross_id") or ""), str(row.get("entry_utc") or ""))
    if outcome == "REJECTED_SL":
        return "REJECTED_SL"
    if outcome == "REJECTED_UPPER_GATE":
        return "REJECTED_UPPER"
    if outcome == "DIAGNOSTIC_EXECUTABLE" and key not in singles:
        return "BLOCKED_SINGLE_ENTRY"
    if outcome == "DIAGNOSTIC_EXECUTABLE":
        return "DIAGNOSTIC_PASS"
    return "REJECTED_SL"


def _fmt_zone(lo: Any, hi: Any) -> str:
    if lo is None and hi is None:
        return ""
    return f"[{lo}–{hi}]"


def _executed_status_label(tr: dict[str, Any]) -> str:
    cross = str(tr.get("cross_id") or "")
    entry_utc = str(tr.get("entry_utc") or "")
    exit_utc = str(tr.get("exit_utc") or "")
    reason = str(tr.get("exit_reason") or "")
    gross = tr.get("gross_return_pct")
    net = tr.get("net_return_pct_research")
    lines = [
        f"P17 LONG Executed · {cross} · {entry_utc} UTC",
        f"Entry {float(tr['entry_price']):.8f} · SL {float(tr['sl_price']):.8f} · TP {float(tr['tp_price']):.8f}",
        f"Exit {exit_utc} UTC @ {float(tr.get('exit_price') or 0):.8f} · {reason}",
    ]
    if gross is not None and net is not None:
        lines.append(f"Gross {float(gross):+.4f}% · Net {float(net):+.4f}%")
    lo, hi = tr.get("lower_zone_lo"), tr.get("lower_zone_hi")
    if lo is not None:
        lines.append(f"Lower {_fmt_zone(lo, hi)}")
    return "\n".join(lines)


def executed_position_spec(tr: dict[str, Any], *, seq: int) -> dict[str, Any]:
    start = _parse_dt(str(tr["entry_utc"]))
    end = _parse_dt(str(tr["exit_utc"]))
    if end <= start:
        end = start
    safe = str(tr["entry_utc"]).replace(":", "").replace("-", "")
    cross = str(tr.get("cross_id") or "")
    return {
        "drawing_id": f"{EXEC_PREFIX}pos-{seq:02d}-{safe}",
        "drawing_type": "long_position",
        "symbol": "DOGEUSDT",
        "timeframe": "5m",
        "start": start,
        "end": end,
        "entry": float(tr["entry_price"]),
        "stop": float(tr["sl_price"]),
        "target": float(tr["tp_price"]),
        "signal_id": f"p17aq-ex-{seq:02d}",
        "direction": "LONG",
        "source": EXEC_SOURCE,
        "exit_reason": str(tr.get("exit_reason") or ""),
        "realized_pnl_pct": tr.get("gross_return_pct"),
        "metadata": {
            "origin": EXEC_SOURCE,
            "strategy_id": EXEC_STRATEGY_ID,
            "layer": "P17 LONG – Executed",
            "status_label": _executed_status_label(tr),
            "cross_id": cross,
            "entry_utc": str(tr["entry_utc"]),
            "exit_utc": str(tr.get("exit_utc") or ""),
            "exit_price": tr.get("exit_price"),
            "gross_return_pct": tr.get("gross_return_pct"),
            "net_return_pct_research": tr.get("net_return_pct_research"),
            "month": tr.get("month"),
            "p17aq_signal_id": f"p17aq-ex-{seq:02d}",
            "p17aq_group": "executed",
        },
    }


def executed_entry_marker_spec(tr: dict[str, Any], *, seq: int) -> dict[str, Any]:
    safe = str(tr["entry_utc"]).replace(":", "").replace("-", "")
    cross = str(tr.get("cross_id") or "")
    return {
        "overlay_id": f"{EXEC_MARKER_PREFIX}{seq:02d}-{safe}",
        "kind": "P17AQ_EXECUTED_ENTRY",
        "symbol": "DOGEUSDT",
        "timestamp": str(tr["entry_utc"]),
        "price": float(tr["entry_price"]),
        "position": "below",
        "shape": "arrow_up",
        "color": "#22c55e",
        "size": 11.0,
        "text": cross,
        "tooltip": _executed_status_label(tr),
        "meta": {
            "origin": EXEC_SOURCE,
            "strategy_id": EXEC_STRATEGY_ID,
            "p17aq_signal_id": f"p17aq-ex-{seq:02d}",
            "p17aq_group": "executed",
            "cross_id": cross,
        },
    }


def _august_tooltip(row: dict[str, Any], category: MarkerCategory) -> str:
    cross = str(row.get("cross_id") or "")
    entry_utc = str(row.get("entry_utc") or "")
    ep = row.get("entry_price")
    lines = [
        f"P17 August Diagnostic · {cross} · {entry_utc} UTC",
        f"Entry {float(ep):.8f}" if ep is not None else "Entry —",
        f"Filter: {row.get('outcome')} · {row.get('code_reason')} · {row.get('audit_category')}",
    ]
    lo, hi = row.get("lower_lo"), row.get("lower_hi")
    if lo is not None or hi is not None:
        lines.append(f"Lower support {_fmt_zone(lo, hi)}")
    if row.get("sl_price") is not None:
        lines.append(
            f"Structural SL {row.get('sl_price')} ({row.get('sl_mode') or ''}) "
            f"dist={row.get('sl_distance_pct')}"
        )
    ulo, uhi = row.get("upper_lo"), row.get("upper_hi")
    if ulo is not None or uhi is not None:
        lines.append(f"Upper D2 {_fmt_zone(ulo, uhi)} tp_dist={row.get('tp_distance_pct')}")
    elif category == "REJECTED_UPPER":
        lines.append("Upper D2: kein qualifizierter R1-TP (Audit)")
    if category == "BLOCKED_SINGLE_ENTRY":
        lines.append("DIAGNOSTIC_PASS / BLOCKED_SINGLE_ENTRY — keine Ausführung")
    audit = str(row.get("audit_category") or "")
    if audit == "UPPER_GAP_BELOW_0_50PCT":
        lines.append("Upper reject: TP-Gap < 0,50%")
    elif audit == "DECLINE_SEGMENT_UNCERTAIN":
        lines.append("Upper reject: Decline-Segment unsicher")
    elif audit == "UPPER_ORIGIN_BEFORE_DECLINE":
        lines.append("Upper reject: Origin vor Decline")
    elif audit == "STRUCTURAL_SL_FAIL_NO_FALLBACK" or row.get("code_reason") == "SL_INSIDE_SUPPORT":
        lines.append(f"SL reject: {row.get('code_reason')}")
    return "\n".join(lines)


def august_marker_spec(
    row: dict[str, Any],
    *,
    seq: int,
    singles: frozenset[tuple[str, str]],
    skip_duplicate_with_executed: bool,
) -> dict[str, Any] | None:
    category = classify_august_row(row, singles=singles)
    key = (str(row.get("cross_id") or ""), str(row.get("entry_utc") or ""))
    if skip_duplicate_with_executed and category in ("DIAGNOSTIC_PASS", "EXECUTED_TRADE"):
        if key in singles:
            return None
    entry_utc = str(row["entry_utc"])
    safe = entry_utc.replace(":", "").replace("-", "")
    cross = str(row.get("cross_id") or "")
    ep = float(row["entry_price"])
    audit = str(row.get("audit_category") or "")

    if category == "REJECTED_SL":
        shape, color, text = "square", "#f97316", "!"
    elif category == "REJECTED_UPPER":
        shape, color, text = "diamond", "#a855f7", "×"
    elif category == "BLOCKED_SINGLE_ENTRY":
        shape, color, text = "circle", "#3b82f6", "1×"
    else:
        shape, color, text = "arrow_up", "#86efac", cross

    return {
        "overlay_id": f"{AUG_MARKER_PREFIX}{seq:02d}-{safe}",
        "kind": f"P17AQ_AUG_{category}",
        "symbol": "DOGEUSDT",
        "timestamp": entry_utc,
        "price": ep,
        "position": "below" if shape == "arrow_up" else "at_price",
        "shape": shape,
        "color": color,
        "size": 10.0 if category != "REJECTED_SL" else 9.0,
        "text": text,
        "tooltip": _august_tooltip(row, category),
        "meta": {
            "origin": AUG_SOURCE,
            "strategy_id": AUG_STRATEGY_ID,
            "p17aq_signal_id": f"p17aq-aug-{seq:02d}",
            "p17aq_group": "august_diagnostic",
            "cross_id": cross,
            "audit_category": audit,
            "code_reason": row.get("code_reason"),
            "marker_category": category,
        },
    }


def build_signal_catalog(root: Path | None = None) -> dict[str, Any]:
    trades = load_executed_trades(root)
    august = load_august_candidates(root)
    singles = single_entry_keys(root)
    exec_signals = []
    for i, tr in enumerate(trades, start=1):
        sid = f"p17aq-ex-{i:02d}"
        exec_signals.append(
            {
                "signal_id": sid,
                "label": (
                    f"{i:02d} · {tr.get('month')} · {tr.get('cross_id')} · "
                    f"{tr.get('entry_utc')} · {tr.get('exit_reason')} · "
                    f"net {float(tr.get('net_return_pct_research') or 0):+.2f}%"
                ),
                "entry_utc": tr.get("entry_utc"),
                "entry_price": tr.get("entry_price"),
                "cross_id": tr.get("cross_id"),
                "goto_ts_utc": tr.get("entry_utc"),
            }
        )
    aug_signals = []
    for i, row in enumerate(august, start=1):
        cat = classify_august_row(row, singles=singles)
        executed = (str(row.get("cross_id")), str(row.get("entry_utc"))) in singles and cat == "DIAGNOSTIC_PASS"
        aug_signals.append(
            {
                "signal_id": f"p17aq-aug-{i:02d}",
                "label": (
                    f"{i:02d} · {row.get('cross_id')} · {row.get('entry_utc')} · "
                    f"{cat} · {row.get('audit_category')}"
                ),
                "entry_utc": row.get("entry_utc"),
                "entry_price": row.get("entry_price"),
                "cross_id": row.get("cross_id"),
                "goto_ts_utc": row.get("entry_utc"),
                "marker_category": cat,
                "also_executed": executed,
            }
        )
    return {
        "executed_count": len(exec_signals),
        "august_count": len(aug_signals),
        "executed_signals": exec_signals,
        "august_signals": aug_signals,
        "source_p17ao": P17AO_REL,
        "source_p17ap": P17AP_REL,
    }


def write_csv_exports(root: Path | None = None) -> dict[str, str]:
    repo = _repo(root)
    trades = load_executed_trades(repo)
    august = load_august_candidates(repo)
    singles = single_entry_keys(repo)

    exec_path = repo / CSV_EXECUTED_REL
    aug_path = repo / CSV_AUGUST_REL
    exec_path.parent.mkdir(parents=True, exist_ok=True)

    with exec_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "nr",
                "month",
                "cross_id",
                "entry_utc",
                "entry_price",
                "sl_price",
                "tp_price",
                "exit_utc",
                "exit_price",
                "exit_reason",
                "net_return_pct",
            ]
        )
        for i, tr in enumerate(trades, start=1):
            w.writerow(
                [
                    i,
                    tr.get("month"),
                    tr.get("cross_id"),
                    tr.get("entry_utc"),
                    tr.get("entry_price"),
                    tr.get("sl_price"),
                    tr.get("tp_price"),
                    tr.get("exit_utc"),
                    tr.get("exit_price"),
                    tr.get("exit_reason"),
                    tr.get("net_return_pct_research"),
                ]
            )

    with aug_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "nr",
                "cross_id",
                "candidate_entry_utc",
                "candidate_entry_price",
                "filter_decision",
                "code_reason",
                "audit_category",
                "lower_zone",
                "upper_zone",
                "sl_info",
                "actually_executed",
            ]
        )
        for i, row in enumerate(august, start=1):
            key = (str(row.get("cross_id") or ""), str(row.get("entry_utc") or ""))
            cat = classify_august_row(row, singles=singles)
            sl_info = ""
            if row.get("sl_price") is not None:
                sl_info = (
                    f"sl={row.get('sl_price')};mode={row.get('sl_mode')};"
                    f"dist_pct={row.get('sl_distance_pct')};reason={row.get('code_reason')}"
                )
            w.writerow(
                [
                    i,
                    row.get("cross_id"),
                    row.get("entry_utc"),
                    row.get("entry_price"),
                    row.get("outcome"),
                    row.get("code_reason"),
                    row.get("audit_category"),
                    _fmt_zone(row.get("lower_lo"), row.get("lower_hi")),
                    _fmt_zone(row.get("upper_lo"), row.get("upper_hi")),
                    sl_info,
                    "yes" if key in singles else "no",
                ]
            )

    return {"executed_csv": str(exec_path.relative_to(repo)), "august_csv": str(aug_path.relative_to(repo))}


def build_executed_drawing_bundle(root: Path | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    trades = load_executed_trades(root)
    positions = []
    markers = []
    for i, tr in enumerate(trades, start=1):
        positions.append(executed_position_spec(tr, seq=i))
        markers.append(executed_entry_marker_spec(tr, seq=i))
    return positions, markers


def build_august_marker_specs(
    root: Path | None = None,
    *,
    skip_duplicate_with_executed: bool = False,
) -> list[dict[str, Any]]:
    august = load_august_candidates(root)
    singles = single_entry_keys(root)
    specs: list[dict[str, Any]] = []
    for i, row in enumerate(august, start=1):
        spec = august_marker_spec(
            row,
            seq=i,
            singles=singles,
            skip_duplicate_with_executed=skip_duplicate_with_executed,
        )
        if spec:
            specs.append(spec)
    return specs


def marker_spec_to_overlay(spec: dict[str, Any], trp: dict[str, Any]) -> Any:
    OverlayMarker = trp["OverlayMarker"]
    OverlayStyle = trp["OverlayStyle"]
    ensure_utc = trp["ensure_utc"]
    ts = _parse_dt(str(spec["timestamp"]))
    return OverlayMarker(
        overlay_id=str(spec["overlay_id"]),
        symbol=str(spec.get("symbol") or "DOGEUSDT"),
        timestamp=ensure_utc(ts),
        price=float(spec["price"]) if spec.get("price") is not None else None,
        position=str(spec.get("position") or "at_price"),
        shape=str(spec.get("shape") or "circle"),
        text=str(spec.get("text") or ""),
        size=float(spec.get("size") or 8.0),
        style=OverlayStyle(color=str(spec.get("color") or "#888"), width=1.0),
        timeframe_scope="all",
        visible=True,
        z_order=48,
        metadata={
            "tooltip": spec.get("tooltip"),
            "kind": spec.get("kind"),
            **(spec.get("meta") or {}),
        },
    )


def august_category_counts(root: Path | None = None) -> dict[str, int]:
    august = load_august_candidates(root)
    singles = single_entry_keys(root)
    counts: dict[str, int] = {}
    for row in august:
        cat = classify_august_row(row, singles=singles)
        counts[cat] = counts.get(cat, 0) + 1
    return counts


def is_p17aq_drawing(drawing: dict[str, Any]) -> bool:
    meta = drawing.get("metadata") or {}
    if meta.get("origin") in (EXEC_SOURCE, AUG_SOURCE):
        return True
    did = str(drawing.get("id") or drawing.get("drawing_id") or "")
    return did.startswith(EXEC_PREFIX)


def is_p17aq_overlay_id(overlay_id: str) -> bool:
    oid = str(overlay_id or "")
    return oid.startswith(EXEC_MARKER_PREFIX) or oid.startswith(AUG_MARKER_PREFIX)


__all__ = [
    "EXEC_SOURCE",
    "AUG_SOURCE",
    "EXEC_PREFIX",
    "build_signal_catalog",
    "build_executed_drawing_bundle",
    "build_august_marker_specs",
    "marker_spec_to_overlay",
    "write_csv_exports",
    "august_category_counts",
    "load_executed_trades",
    "load_august_candidates",
    "is_p17aq_drawing",
    "is_p17aq_overlay_id",
    "classify_august_row",
]

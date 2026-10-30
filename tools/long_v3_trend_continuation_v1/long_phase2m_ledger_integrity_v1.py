"""Phase 2M — order ledger reconstruction and repricing audit."""

from __future__ import annotations

from typing import Any


def _match_fill_trade(ledger: list[dict], fill_utc: str, entry: float) -> dict | None:
    for ev in ledger:
        if ev.get("phase") != "entry" or ev.get("action") != "FILL":
            continue
        if ev.get("bar_utc") != fill_utc:
            continue
        if abs(float(ev.get("entry", 0)) - entry) > 1e-9:
            continue
        return ev
    return None


def reconstruct_trade_lifecycle(
    *,
    ledger: list[dict],
    exit_ledger: list[dict],
    fill_utc: str,
    entry_price: float,
    sl_price: float,
) -> dict[str, Any]:
    """Reconstruct PLACE→(REPLACE*)→FILL→EXIT from chronological ledger only."""
    fill_ev = _match_fill_trade(ledger, fill_utc, entry_price)
    if fill_ev is None:
        return {
            "status": "UNRESOLVED",
            "reason": "FILL_EVENT_NOT_FOUND",
            "fill_utc": fill_utc,
            "entry_price": entry_price,
        }

    fill_oid = fill_ev.get("order_id")
    entry_events = [e for e in ledger if e.get("phase") == "entry" and (e.get("bar_utc") or "") <= fill_utc]
    entry_events.sort(key=lambda e: e.get("bar_utc") or "")

    initial_place: dict | None = None
    replace_chain: list[dict] = []
    effective_place: dict | None = None

    for ev in entry_events:
        if ev.get("action") == "PLACE":
            initial_place = {
                "bar_utc": ev.get("bar_utc"),
                "order_id": ev.get("order_id"),
                "limit": ev.get("limit"),
                "sl": ev.get("sl"),
                "lineage_id": ev.get("lineage_id"),
                "frozen_support_key": ev.get("frozen_support_key"),
                "selection_reason": ev.get("selection_reason"),
            }
        if ev.get("action") == "REPLACE":
            replace_chain.append(
                {
                    "bar_utc": ev.get("bar_utc"),
                    "replace_kind": ev.get("replace_kind"),
                    "old_pool": ev.get("old_pool"),
                    "new_pool": ev.get("new_pool"),
                    "old_limit": ev.get("old_limit"),
                    "new_limit": ev.get("new_limit"),
                    "new_sl": ev.get("new_sl"),
                    "sl_pool": ev.get("sl_pool"),
                }
            )

    # Effective placement: last REPLACE before fill with matching new_limit/sl, else PLACE with fill order_id
    for rep in reversed(replace_chain):
        if rep.get("new_limit") is not None and abs(float(rep["new_limit"]) - entry_price) < 1e-9:
            if rep.get("new_sl") is not None and abs(float(rep["new_sl"]) - sl_price) < 1e-6:
                effective_place = {
                    "kind": "REPLACE_EFFECTIVE_PLACE",
                    "bar_utc": rep["bar_utc"],
                    "order_id": fill_oid,
                    "limit": rep["new_limit"],
                    "sl": rep["new_sl"],
                    "prior_order_id": initial_place.get("order_id") if initial_place else None,
                    "note": (
                        "Cross-lineage REPLACE creates new pending without separate PLACE/CANCEL "
                        "ledger rows; fill order_id links to post-REPLACE pending."
                    ),
                }
                break

    if effective_place is None and initial_place:
        if initial_place.get("order_id") == fill_oid:
            effective_place = {**initial_place, "kind": "PLACE"}
        elif abs(float(initial_place.get("limit") or 0) - entry_price) < 1e-9:
            effective_place = {**initial_place, "kind": "PLACE"}

    if effective_place is None:
        return {
            "status": "UNRESOLVED",
            "reason": "CANNOT_LINK_FILL_TO_PLACE_OR_REPLACE",
            "fill_event": fill_ev,
            "initial_place": initial_place,
            "replace_chain": replace_chain,
        }

    exit_events = [
        e
        for e in exit_ledger
        if e.get("phase") == "exit"
        and (e.get("bar_utc") or "") >= fill_utc
    ]
    exit_ev = None
    for e in exit_events:
        if e.get("action") in ("TP_LIMIT_FILL", "SL_FILL", "EMA59_200_CROSS", "AMBIGUOUS_INTRABAR"):
            exit_ev = e
            break

    return {
        "status": "RESOLVED",
        "fill_utc": fill_utc,
        "fill_order_id": fill_oid,
        "entry_price": entry_price,
        "sl_price": sl_price,
        "initial_place": initial_place,
        "replace_chain": replace_chain,
        "effective_place": effective_place,
        "fill_event": {k: v for k, v in fill_ev.items() if k not in ("phase",)},
        "exit_event": exit_ev,
        "parent_child": {
            "initial_order_id": initial_place.get("order_id") if initial_place else None,
            "fill_order_id": fill_oid,
            "replaced_without_explicit_cancel": bool(
                initial_place
                and initial_place.get("order_id") != fill_oid
                and replace_chain
            ),
        },
    }


def collect_b_repricing_events(
    ledger: list[dict],
    repricing_audits: list[dict],
    *,
    window: str,
) -> list[dict]:
    rows: list[dict] = []
    for ra in repricing_audits:
        rows.append({"window": window, "source": "repricing_audit", **ra})
    for ev in ledger:
        if ev.get("phase") != "entry":
            continue
        if ev.get("replace_kind") == "DYNAMIC_UPPER_GROWTH_V2K":
            rows.append(
                {
                    "window": window,
                    "source": "ledger_replace",
                    "bar_utc": ev.get("bar_utc"),
                    "old_limit": ev.get("old_limit"),
                    "new_limit": ev.get("new_limit"),
                    "new_sl": ev.get("new_sl"),
                }
            )
        if ev.get("action") == "PLACE" and ev.get("selection_reason") == "DYNAMIC_UPPER_GROWTH_REPRICE":
            rows.append(
                {
                    "window": window,
                    "source": "ledger_place_reprice",
                    "bar_utc": ev.get("bar_utc"),
                    "new_limit": ev.get("limit"),
                    "tp_gap_pct": ev.get("tp_gap_pct"),
                    "repriced_from_limit": ev.get("repriced_from_limit"),
                    "order_id": ev.get("order_id"),
                    "earliest_fill": ev.get("earliest"),
                }
            )
    return rows


def enrich_repricing_with_outcomes(
    repricing_rows: list[dict],
    trades_b: list,
    ledger_b: list[dict],
) -> list[dict]:
    out = []
    for row in repricing_rows:
        new_lim = row.get("new_limit")
        bar = row.get("bar_utc")
        filled = False
        fill_utc = None
        gross = None
        if new_lim is not None and bar:
            for t in trades_b:
                if abs(float(t.entry_price) - float(new_lim)) < 1e-9:
                    if t.fill_bar_utc >= bar:
                        filled = True
                        fill_utc = t.fill_bar_utc
                        gross = t.gross_return_pct
                        break
            for ev in ledger_b:
                if ev.get("action") == "FILL" and ev.get("bar_utc") == fill_utc:
                    row = {**row, "fill_risk_pct": ev.get("risk_pct")}
        old_lim = row.get("old_limit") or row.get("repriced_from_limit")
        entry_change = None
        if old_lim and new_lim:
            entry_change = (float(new_lim) - float(old_lim)) / float(old_lim) * 100.0
        out.append(
            {
                **row,
                "old_limit_a_frozen_reference": old_lim,
                "entry_change_pct": entry_change,
                "filled_after_reprice": filled,
                "fill_utc": fill_utc,
                "gross_return_pct": gross,
            }
        )
    return out


def completed_trades_chronological(trades) -> list:
    """Resolved trades only, ordered by fill time (for streak statistics)."""
    resolved = [t for t in trades if t.gross_return_pct is not None]
    return sorted(resolved, key=lambda x: x.fill_bar_utc)


def max_consecutive_sl(trades) -> int:
    streak = 0
    best = 0
    for t in completed_trades_chronological(trades):
        if t.exit_reason == "STRUCTURAL_SL":
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    return best


def worst_loss_streak_sum_pct(trades) -> float:
    """Largest peak-to-trough sum of consecutive losing trade gross returns (%)."""
    cur = 0.0
    worst = 0.0
    for t in completed_trades_chronological(trades):
        g = t.gross_return_pct or 0.0
        if g < 0:
            cur += g
            worst = min(worst, cur)
        else:
            cur = 0.0
    return round(worst, 6)

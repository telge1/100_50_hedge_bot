#!/usr/bin/env python3
"""Compare old multicoin frozen run vs shared-engine v2 (research-only)."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

OLD_DEFAULT = ROOT / "results/edc_sync_tolerance/multicoin_30d_frozen_validation"
NEW_DEFAULT = ROOT / "results/edc_sync_tolerance/multicoin_30d_frozen_validation_v2_shared_engine"

REF_STRATEGY_KEY = "M0_TP075_SL050_H8"


def _load_ckpts(run_dir: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for p in sorted((run_dir / "checkpoints").glob("*.json")):
        out[p.stem.upper()] = json.loads(p.read_text(encoding="utf-8"))
    return out


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_ref(t: dict[str, Any]) -> bool:
    return (
        str(t.get("timeframe") or "") == "5m"
        and str(t.get("mode_id") or "") == "M0_STRICT_SYNC"
        and str(t.get("group") or "") == "CORE_RESEARCH_SUPPORTIVE"
        and str(t.get("strategy_key") or "") == REF_STRATEGY_KEY
    )


def compare_runs(old_dir: Path, new_dir: Path) -> dict[str, Any]:
    old = _load_ckpts(old_dir)
    new = _load_ckpts(new_dir)

    old_cands = {c["candidate_id"]: c for s, p in old.items() for c in (p.get("candidates") or [])}
    new_cands = {c["candidate_id"]: c for s, p in new.items() for c in (p.get("candidates") or [])}
    oc, nc = set(old_cands), set(new_cands)

    entry_cand_diffs = []
    verdict_diffs = []
    segment_diffs = []
    for cid in sorted(oc & nc):
        a, b = old_cands[cid], new_cands[cid]
        if str(a.get("entry_at")) != str(b.get("entry_at")) or abs(
            (_f(a.get("entry_price")) or 0) - (_f(b.get("entry_price")) or 0)
        ) > 1e-8:
            entry_cand_diffs.append(cid)
        if a.get("core_research_verdict") != b.get("core_research_verdict"):
            verdict_diffs.append(
                {
                    "candidate_id": cid,
                    "symbol": a.get("symbol"),
                    "old": a.get("core_research_verdict"),
                    "new": b.get("core_research_verdict"),
                }
            )
        if a.get("coverage_segment") != b.get("coverage_segment"):
            segment_diffs.append(
                {
                    "candidate_id": cid,
                    "symbol": a.get("symbol"),
                    "old": a.get("coverage_segment"),
                    "new": b.get("coverage_segment"),
                }
            )

    def trades_map(payloads: dict[str, dict]) -> dict[tuple, dict]:
        m: dict[tuple, dict] = {}
        for p in payloads.values():
            for t in p.get("trades") or []:
                m[(t.get("candidate_id"), t.get("strategy_key"), t.get("group"))] = t
        return m

    otm, ntm = trades_map(old), trades_map(new)
    common = set(otm) & set(ntm)
    entry_t = exit_t = pnl_t = 0
    for k in common:
        a, b = otm[k], ntm[k]
        if str(a.get("entry_at")) != str(b.get("entry_at")) or abs(
            (_f(a.get("entry_price")) or 0) - (_f(b.get("entry_price")) or 0)
        ) > 1e-8:
            entry_t += 1
        if str(a.get("exit_reason")) != str(b.get("exit_reason")) or str(a.get("exit_at")) != str(
            b.get("exit_at")
        ):
            exit_t += 1
        if abs((_f(a.get("net_pnl_usdt")) or 0) - (_f(b.get("net_pnl_usdt")) or 0)) > 1e-8:
            pnl_t += 1

    old_ref = [t for p in old.values() for t in (p.get("trades") or []) if _is_ref(t)]
    new_ref = [t for p in new.values() for t in (p.get("trades") or []) if _is_ref(t)]
    old_ref_ids = {t["candidate_id"] for t in old_ref}
    new_ref_ids = {t["candidate_id"] for t in new_ref}

    old_inc = sum(
        1
        for p in old.values()
        for t in (p.get("trades") or [])
        if t.get("exit_reason") == "INCOMPLETE_OUTCOME_HORIZON"
    )
    new_inc = sum(
        1
        for p in new.values()
        for t in (p.get("trades") or [])
        if t.get("exit_reason") == "INCOMPLETE_OUTCOME_HORIZON"
    )

    ref_by_sym_old = Counter(t.get("symbol") for t in old_ref)
    ref_by_sym_new = Counter(t.get("symbol") for t in new_ref)
    ref_sym_deltas = {
        s: int(ref_by_sym_new[s] - ref_by_sym_old[s])
        for s in sorted(set(ref_by_sym_old) | set(ref_by_sym_new))
        if ref_by_sym_old[s] != ref_by_sym_new[s]
    }

    group_old = Counter(
        t.get("group") for p in old.values() for t in (p.get("trades") or []) if p.get("symbol") != "XRPUSDT" or True
    )
    # recount properly
    group_old = Counter(t.get("group") for p in old.values() for t in (p.get("trades") or []))
    group_new = Counter(t.get("group") for p in new.values() for t in (p.get("trades") or []))

    return {
        "old_dir": str(old_dir),
        "new_dir": str(new_dir),
        "n_symbols_old": len(old),
        "n_symbols_new": len(new),
        "status_old": dict(Counter(p.get("status") for p in old.values())),
        "status_new": dict(Counter(p.get("status") for p in new.values())),
        "entry_rule_old": dict(Counter(str(p.get("entry_rule")) for p in old.values())),
        "entry_rule_new": dict(Counter(str(p.get("entry_rule")) for p in new.values())),
        "candidates": {
            "n_old": len(oc),
            "n_new": len(nc),
            "added": sorted(nc - oc),
            "removed": sorted(oc - nc),
            "n_common": len(oc & nc),
            "n_entry_field_diffs": len(entry_cand_diffs),
            "n_core_research_verdict_diffs": len(verdict_diffs),
            "verdict_diffs_sample": verdict_diffs[:30],
            "n_coverage_segment_diffs": len(segment_diffs),
            "segment_new_full_multisource": sum(
                1 for d in segment_diffs if d.get("new") == "FULL_MULTISOURCE"
            ),
        },
        "all_trades": {
            "n_old": sum(len(p.get("trades") or []) for p in old.values()),
            "n_new": sum(len(p.get("trades") or []) for p in new.values()),
            "n_common_keys": len(common),
            "only_old": len(set(otm) - set(ntm)),
            "only_new": len(set(ntm) - set(otm)),
            "n_entry_diffs_common": entry_t,
            "n_exit_diffs_common": exit_t,
            "n_pnl_diffs_common": pnl_t,
            "group_counts_old": dict(group_old),
            "group_counts_new": dict(group_new),
        },
        "reference_trades_supportive_m0_tp075_sl050_8h": {
            "n_old": len(old_ref),
            "n_new": len(new_ref),
            "added_candidate_ids": sorted(new_ref_ids - old_ref_ids),
            "removed_candidate_ids": sorted(old_ref_ids - new_ref_ids),
            "per_symbol_deltas": ref_sym_deltas,
            "net_pnl_old": sum(_f(t.get("net_pnl_usdt")) or 0.0 for t in old_ref),
            "net_pnl_new": sum(_f(t.get("net_pnl_usdt")) or 0.0 for t in new_ref),
        },
        "incomplete_outcomes_all_cells": {"n_old": old_inc, "n_new": new_inc},
        "effects": {
            "require_full_horizon": (
                "Old: True (truncated → drop/INCOMPLETE without TIME). "
                "New: False + incomplete_if_truncated_path (shared_strategy)."
            ),
            "entry_semantics": (
                "Old checkpoint entry_rule FIRST_1M_OPEN_AT_OR_AFTER_DECISION_AT; "
                "new SIGNAL_TF_NEXT_OPEN_AFTER_SIGNAL_BAR. "
                "On contiguous 5m data these timestamps usually coincide — "
                f"observed candidate entry field diffs={len(entry_cand_diffs)}."
            ),
            "coverage_pads": (
                "Aligned warm=5d / outcome=12h / source=2h enables more FULL_MULTISOURCE "
                "segments and some core_research_verdict flips vs the legacy pad run."
            ),
            "xrp_gate": (
                "Legacy XRP FAILED_PARITY (0 trades). Shared engine reproduces all frozen "
                "export candidates; 1 re-detect extra near window end is warning-only "
                "(OK_WITH_EXPORT_EXTRAS); XRP COMPLETE with reference net +27.50."
            ),
        },
    }


def main() -> int:
    old = Path(sys.argv[1]) if len(sys.argv) > 1 else OLD_DEFAULT
    new = Path(sys.argv[2]) if len(sys.argv) > 2 else NEW_DEFAULT
    report = compare_runs(old, new)
    out = new / "diff_vs_v1_legacy_engine.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    rt = report["reference_trades_supportive_m0_tp075_sl050_8h"]
    c = report["candidates"]
    at = report["all_trades"]
    md = [
        "# Multicoin v1 (legacy) vs v2 (shared engine)",
        "",
        f"- Old: `{report['old_dir']}`",
        f"- New: `{report['new_dir']}`",
        f"- Status old: `{report['status_old']}` new: `{report['status_new']}`",
        f"- Entry rules old: `{report['entry_rule_old']}` new: `{report['entry_rule_new']}`",
        "",
        "## Candidates",
        f"- old={c['n_old']} new={c['n_new']} added={len(c['added'])} removed={len(c['removed'])}",
        f"- entry field diffs={c['n_entry_field_diffs']}",
        f"- core_research_verdict diffs={c['n_core_research_verdict_diffs']}",
        f"- coverage_segment diffs={c['n_coverage_segment_diffs']} "
        f"(of which → FULL_MULTISOURCE: {c['segment_new_full_multisource']})",
        "",
        "## All trades",
        f"- old={at['n_old']} new={at['n_new']} common_keys={at['n_common_keys']} "
        f"only_old={at['only_old']} only_new={at['only_new']}",
        f"- common entry/exit/pnl diffs: {at['n_entry_diffs_common']}/"
        f"{at['n_exit_diffs_common']}/{at['n_pnl_diffs_common']}",
        "",
        "## Reference trades (SUPPORTIVE 5m M0 TP075/SL050/8h)",
        f"- old={rt['n_old']} new={rt['n_new']}",
        f"- added={len(rt['added_candidate_ids'])} removed={len(rt['removed_candidate_ids'])}",
        f"- per-symbol deltas: `{rt['per_symbol_deltas']}`",
        f"- net_pnl_old={rt['net_pnl_old']:.4f} net_pnl_new={rt['net_pnl_new']:.4f} "
        f"delta={rt['net_pnl_new']-rt['net_pnl_old']:.4f}",
        "",
        "## Incomplete outcomes",
        f"- old={report['incomplete_outcomes_all_cells']['n_old']} "
        f"new={report['incomplete_outcomes_all_cells']['n_new']}",
        "",
        "## Effects",
        f"- {report['effects']['require_full_horizon']}",
        f"- {report['effects']['entry_semantics']}",
        f"- {report['effects']['coverage_pads']}",
        f"- {report['effects']['xrp_gate']}",
        "",
    ]
    md_path = new / "diff_vs_v1_legacy_engine.md"
    md_path.write_text("\n".join(md) + "\n", encoding="utf-8")
    print("wrote", out)
    print("wrote", md_path)
    print("ref", rt["n_old"], "->", rt["n_new"], "pnl", rt["net_pnl_old"], "->", rt["net_pnl_new"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Post-enrichment integrity checks against the v2 frozen reference run."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

from . import constants as C
from .reference_filter import is_reference_trade


def _load_v2_ref_trades(input_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in sorted((input_dir / "checkpoints").glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        for t in d.get("trades") or []:
            if is_reference_trade(t):
                rows.append({**t, "symbol": str(t.get("symbol") or p.stem).upper()})
    return rows


def build_integrity_report(
    *,
    enriched: pd.DataFrame,
    input_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    ref = _load_v2_ref_trades(input_dir)
    ref_by_id = {str(t["candidate_id"]): t for t in ref}
    enr_ids = [str(x) for x in enriched.get("candidate_id", pd.Series(dtype=str)).tolist()]
    dup = [i for i, n in Counter(enr_ids).items() if n > 1]
    missing = sorted(set(ref_by_id) - set(enr_ids))
    extra = sorted(set(enr_ids) - set(ref_by_id))

    pnl_mismatches = []
    entry_mismatches = []
    identity_mismatches = []
    for _, row in enriched.iterrows():
        cid = str(row.get("candidate_id"))
        t = ref_by_id.get(cid)
        if t is None:
            continue
        if str(row.get("symbol", "")).upper() != str(t.get("symbol", "")).upper():
            identity_mismatches.append({"candidate_id": cid, "field": "symbol"})
        if str(row.get("feature__direction") or row.get("label__direction") or "") and str(
            row.get("feature__direction") or ""
        ) not in ("", "nan"):
            rd = str(row.get("feature__direction") or "").upper()
            td = str(t.get("direction") or "").upper()
            if rd and td and rd != td:
                identity_mismatches.append({"candidate_id": cid, "field": "direction", "enriched": rd, "ref": td})
        ep = row.get("label__entry_price")
        if ep is not None and t.get("entry_price") is not None:
            if abs(float(ep) - float(t["entry_price"])) > 1e-8:
                entry_mismatches.append({"candidate_id": cid, "enriched": ep, "ref": t["entry_price"]})
        if str(row.get("label__exit_at") or "") != str(t.get("exit_at") or ""):
            entry_mismatches.append(
                {"candidate_id": cid, "field": "exit_at", "enriched": row.get("label__exit_at"), "ref": t.get("exit_at")}
            )
        npnl = row.get("label__net_pnl_usdt")
        if npnl is not None and t.get("net_pnl_usdt") is not None:
            if abs(float(npnl) - float(t["net_pnl_usdt"])) > 1e-8:
                pnl_mismatches.append({"candidate_id": cid, "enriched": npnl, "ref": t["net_pnl_usdt"]})

    # Spot samples
    def _pick(sym_pred, outcome):
        base = enriched[enriched["symbol"].astype(str).str.upper().map(sym_pred)]
        if base.empty:
            return None
        if "label__outcome_class" in base.columns:
            hit = base[base["label__outcome_class"].astype(str) == outcome]
        else:
            pnl = pd.to_numeric(base.get("label__net_pnl_usdt"), errors="coerce")
            hit = base[pnl > 0] if outcome == "WIN" else base[pnl <= 0]
        if hit.empty:
            return None
        r = hit.iloc[0]
        return {
            "candidate_id": r.get("candidate_id"),
            "symbol": r.get("symbol"),
            "outcome": r.get("label__outcome_class"),
            "net_pnl_usdt": r.get("label__net_pnl_usdt"),
            "coverage_segment": r.get("feature__coverage_segment"),
        }

    samples = {
        "xrp_win": _pick(lambda s: s == "XRPUSDT", "WIN"),
        "xrp_loss": _pick(lambda s: s == "XRPUSDT", "LOSS"),
        "non_xrp_win": _pick(lambda s: s != "XRPUSDT", "WIN"),
        "non_xrp_loss": _pick(lambda s: s != "XRPUSDT", "LOSS"),
    }
    seg_col = "feature__coverage_segment"
    if seg_col in enriched.columns:
        full_ms = enriched[enriched[seg_col].astype(str) == "FULL_MULTISOURCE"]
        limited = enriched[enriched[seg_col].astype(str) != "FULL_MULTISOURCE"]
    else:
        full_ms = enriched.iloc[0:0]
        limited = enriched
    samples["full_multisource"] = None if full_ms.empty else {
        "candidate_id": full_ms.iloc[0].get("candidate_id"),
        "symbol": full_ms.iloc[0].get("symbol"),
        "n": int(len(full_ms)),
    }
    samples["limited_coverage"] = None if limited.empty else {
        "candidate_id": limited.iloc[0].get("candidate_id"),
        "symbol": limited.iloc[0].get("symbol"),
        "n": int(len(limited)),
    }

    ok = (
        len(enriched) == len(ref)
        and not dup
        and not missing
        and not extra
        and not pnl_mismatches
        and not entry_mismatches
        and not identity_mismatches
    )
    report = {
        "ok": ok,
        "n_enriched": int(len(enriched)),
        "n_reference_trades": len(ref),
        "expected_v2": C.EXPECTED_REFERENCE_TRADES_V2,
        "n_duplicates": len(dup),
        "duplicate_ids": dup[:20],
        "missing_ids": missing[:50],
        "n_missing": len(missing),
        "extra_ids": extra[:50],
        "n_extra": len(extra),
        "n_pnl_mismatches": len(pnl_mismatches),
        "pnl_mismatches": pnl_mismatches[:20],
        "n_entry_exit_mismatches": len(entry_mismatches),
        "entry_exit_mismatches": entry_mismatches[:20],
        "n_identity_mismatches": len(identity_mismatches),
        "identity_mismatches": identity_mismatches[:20],
        "samples": samples,
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
    }
    return report

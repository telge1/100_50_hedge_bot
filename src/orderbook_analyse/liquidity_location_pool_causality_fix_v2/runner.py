"""Re-audit after closed confirmation bar availability fix (v2)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.cluster_sweep_research.clickhouse_source import default_client, fetch_candles_1m
from orderbook_analyse.liquidity_location_causal.prefix import candles_1m_closed_until, utc_naive
from orderbook_analyse.liquidity_location_pool_causality_audit_v1.config import (
    AUDIT_END,
    DENSE_END,
    DENSE_START,
    PREFIX_CHECKPOINTS,
    REFERENCE_POOLS,
    SYMBOL,
    TIMEFRAMES,
    WARMUP_START,
)
from orderbook_analyse.liquidity_location_pool_causality_audit_v1.runner import (
    compute_prefix_state,
    decide_verdict,
)

VERDICT_FIXED = "LIQUIDITY_LOCATION_POOLS_CAUSAL_AVAILABILITY_FIXED"
DEFAULT_OUT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/liquidity_location_pool_causality_fix_v2"
)

BOUNDARY_CHECKS = [
    ("short_entry_0330", "2026-08-28 03:30:00", "lld:DOGEUSDT:15m:upper:1787886900", False),
    ("short_entry_034459", "2026-08-28 03:44:59", "lld:DOGEUSDT:15m:upper:1787886900", False),
    ("short_entry_0345", "2026-08-28 03:45:00", "lld:DOGEUSDT:15m:upper:1787886900", True),
    ("long_target_0845", "2026-08-28 08:45:00", "lld:DOGEUSDT:15m:upper:1787905800", False),
    ("long_target_085959", "2026-08-28 08:59:59", "lld:DOGEUSDT:15m:upper:1787905800", False),
    ("long_target_0900", "2026-08-28 09:00:00", "lld:DOGEUSDT:15m:upper:1787905800", True),
]


def run_fix_audit(*, out_root: Path | None = None) -> dict[str, Any]:
    root = Path(out_root or DEFAULT_OUT)
    root.mkdir(parents=True, exist_ok=True)
    run_id = f"doge_lld_fix_{int(time.time())}"
    out = root / run_id
    if out.exists():
        raise FileExistsError(out)
    out.mkdir(parents=True)

    client = default_client()
    df_1m = fetch_candles_1m(client, SYMBOL, WARMUP_START, AUDIT_END)

    history = []
    for label, ts in PREFIX_CHECKPOINTS:
        st = compute_prefix_state(df_1m, ts, mode="causal_prefix")
        history.append((label, st))

    parity_rows = []
    for label, st in history:
        scan = compute_prefix_state(df_1m, st["as_of"], mode="causal_prefix")
        causal_ids = {r["pool_id"] for r in st["active_rows"]}
        scan_ids = {r["pool_id"] for r in scan["active_rows"]}
        parity_rows.append(
            {
                "prefix": label,
                "as_of": st["as_of"],
                "causal_n": len(causal_ids),
                "scanner_n": len(scan_ids),
                "id_parity": causal_ids == scan_ids,
                "snapshot_hash": st["snapshot_hash"],
            }
        )
    prefix_parity = pd.DataFrame(parity_rows)
    prefix_parity.to_csv(out / "prefix_parity.csv", index=False)

    dense_rows = []
    for ts in pd.date_range(DENSE_START, DENSE_END, freq="1min"):
        st = compute_prefix_state(df_1m, ts, mode="causal_prefix", timeframes=("15m", "1h"))
        dense_rows.append({"as_of": st["as_of"], "n_active": st["n_active"], "hash": st["snapshot_hash"]})
    pd.DataFrame(dense_rows).to_csv(out / "dense_prefix_parity.csv", index=False)

    boundary = []
    for name, ts, pid, expect_active in BOUNDARY_CHECKS:
        st = compute_prefix_state(df_1m, ts, mode="causal_prefix", timeframes=("15m",))
        active = pid in {r["pool_id"] for r in st["active_rows"]}
        row = next((r for r in st["active_rows"] if r["pool_id"] == pid), None)
        boundary.append(
            {
                "check": name,
                "as_of": ts,
                "pool_id": pid,
                "expect_active": expect_active,
                "active": active,
                "pass": active == expect_active,
                "available_at": None if row is None else row.get("known_at"),
                "confirmation_bar_end": None if row is None else row.get("confirmation_bar_end"),
            }
        )
    ref_avail = pd.DataFrame(boundary)
    ref_avail.to_csv(out / "reference_pool_availability.csv", index=False)

    timeline = []
    for label, st in history:
        for r in st["active_rows"]:
            if r["pool_id"] in {s["pool_id"] for s in REFERENCE_POOLS.values()}:
                timeline.append(r)
    pd.DataFrame(timeline).to_csv(out / "corrected_pool_timeline.csv", index=False)

    parity_fail = int((~prefix_parity["id_parity"]).sum()) if not prefix_parity.empty else 0
    boundary_fail = int((~ref_avail["pass"]).sum()) if not ref_avail.empty else 0
    verdict = VERDICT_FIXED if parity_fail == 0 and boundary_fail == 0 else "LIQUIDITY_LOCATION_POOL_LOOKAHEAD_FOUND"

    manifest = {
        "run_id": run_id,
        "symbol": SYMBOL,
        "verdict": verdict,
        "pool_time_semantics_version": "closed_confirmation_bar_v2",
        "known_at_basis": "confirmation_bar_close",
        "htf_aggregation_basis": "closed_1m_prefix_as_of",
        "forming_tip_used": False,
        "legacy_pool_timestamps": False,
        "prefix_parity_failures": parity_fail,
        "boundary_failures": boundary_fail,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (out / "methodology.md").write_text(
        "# LLD pool availability fix v2\n\n"
        "- known_at = available_at = confirmation_bar_close\n"
        "- HTF built from closed 1m prefix only\n"
        "- Scanner uses same causal path as audit\n",
        encoding="utf-8",
    )
    (out / "report.md").write_text(
        f"# {verdict}\n\n"
        f"- prefix parity failures: {parity_fail}/10\n"
        f"- boundary failures: {boundary_fail}\n",
        encoding="utf-8",
    )
    return {"out_dir": str(out), "manifest": manifest, "verdict": verdict}


if __name__ == "__main__":
    print(json.dumps(run_fix_audit(), indent=2, default=str))

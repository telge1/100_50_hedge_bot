"""Deterministic discovery and validation of single-epoch analysis windows."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from obfull_research_engine.mp_edge_event_study_v1.silver_mids import (
    _as_text,
    _query,
    _rows_from_result,
    assess_window_chunks,
    estimate_mid_rows,
    load_chain_meta,
)
from obfull_research_engine.mp_edge_event_study_v1.util import dt_to_ns, format_ns_z, ns_to_dt
from obfull_research_engine.timeparse import format_utc_z

from .params import EST_SECONDS_PER_MID_ROW, MIN_EPOCH_DURATION_S


@dataclass
class BatchWindow:
    window_id: str
    start_ts: str
    end_ts: str
    start_ns: int
    end_ns: int
    duration_s: float
    replay_epoch: str
    safe_coverage: str
    parity_status: str
    included: bool
    exclusion_reason: str
    estimated_rows: int
    estimated_runtime_s: float
    chain_version: str = ""
    status_epoch: str = ""

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


def load_epochs_from_ch(
    client: Any,
    *,
    database: str,
    symbol: str,
) -> list[dict[str, Any]]:
    meta = load_chain_meta(client, database=database, symbol=symbol)
    chain_version = str(meta["chain_version"])
    sql = f"""
SELECT
  epoch_id,
  status,
  safe_start_ns,
  safe_end_ns
FROM {database}.replay_epochs_v1_3 FINAL
WHERE chain_version = {{chain_version:String}}
ORDER BY safe_start_ns, epoch_id
""".strip()
    rows = _rows_from_result(_query(client, sql, {"chain_version": chain_version}))
    out = []
    for r in rows:
        eid = _as_text(r["epoch_id"])
        start_ns = int(r["safe_start_ns"])
        end_ns = int(r["safe_end_ns"])
        out.append(
            {
                "epoch_id": eid,
                "status": str(r["status"]),
                "start_ns": start_ns,
                "end_ns": end_ns,
                "duration_s": (end_ns - start_ns) / 1e9,
                "chain_version": chain_version,
            }
        )
    return out


def build_batch_windows(
    client: Any,
    *,
    database: str,
    symbol: str,
    min_duration_s: float = MIN_EPOCH_DURATION_S,
    validate_assessment: bool = True,
) -> list[BatchWindow]:
    """One analysis window per COMPLETE epoch with duration ≥ min_duration_s.

    Multi-epoch continuous merges are intentionally split: each window has a
    unique replay_epoch (fail-closed for cross-epoch FSM/outcomes).
    BOUNDED_COMPLETE epochs are excluded.
    """
    epochs = load_epochs_from_ch(client, database=database, symbol=symbol)
    windows: list[BatchWindow] = []
    for i, ep in enumerate(epochs, start=1):
        wid = f"ep{i:03d}_{ep['epoch_id'][:12]}"
        start_ns = int(ep["start_ns"])
        end_ns = int(ep["end_ns"])
        dur = float(ep["duration_s"])
        included = True
        reason = ""
        safe = "UNKNOWN"
        parity = "UNKNOWN"
        est_rows = 0
        est_rt = 0.0

        if ep["status"] != "COMPLETE":
            included = False
            reason = f"STATUS_{ep['status']}"
        elif dur < float(min_duration_s):
            included = False
            reason = f"DURATION_LT_{int(min_duration_s)}S"

        if included and validate_assessment:
            try:
                assessment = assess_window_chunks(
                    client,
                    database=database,
                    symbol=symbol,
                    start=ns_to_dt(start_ns),
                    end=ns_to_dt(end_ns),
                )
                if assessment["epoch_id"] != ep["epoch_id"]:
                    included = False
                    reason = "EPOCH_MISMATCH_ASSESSMENT"
                elif assessment["status"] != "READY":
                    included = False
                    reason = assessment.get("reason") or "NOT_READY"
                else:
                    safe = "FULL_CHUNKS_COMPLETE"
                    parity = "READY_SINGLE_EPOCH"
                    est_rows = estimate_mid_rows(
                        client,
                        database=database,
                        symbol=symbol,
                        start_ns=start_ns,
                        end_ns=end_ns,
                        chunk_keys=assessment["chunk_keys"],
                    )
                    est_rt = float(est_rows) * EST_SECONDS_PER_MID_ROW
            except Exception as exc:  # noqa: BLE001
                included = False
                reason = f"ASSESS_FAIL:{type(exc).__name__}"

        windows.append(
            BatchWindow(
                window_id=wid,
                start_ts=format_ns_z(start_ns),
                end_ts=format_ns_z(end_ns),
                start_ns=start_ns,
                end_ns=end_ns,
                duration_s=dur,
                replay_epoch=ep["epoch_id"],
                safe_coverage=safe if included else "EXCLUDED",
                parity_status=parity if included else "EXCLUDED",
                included=included,
                exclusion_reason=reason,
                estimated_rows=int(est_rows),
                estimated_runtime_s=float(est_rt),
                chain_version=ep["chain_version"],
                status_epoch=ep["status"],
            )
        )
    return windows


def write_batch_windows_csv(path: Path, windows: Sequence[BatchWindow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [w.to_row() for w in windows]
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def read_batch_windows_csv(path: Path) -> list[BatchWindow]:
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    out: list[BatchWindow] = []
    for r in rows:
        out.append(
            BatchWindow(
                window_id=r["window_id"],
                start_ts=r["start_ts"],
                end_ts=r["end_ts"],
                start_ns=int(r["start_ns"]),
                end_ns=int(r["end_ns"]),
                duration_s=float(r["duration_s"]),
                replay_epoch=r["replay_epoch"],
                safe_coverage=r["safe_coverage"],
                parity_status=r["parity_status"],
                included=str(r["included"]).lower() in ("1", "true", "yes"),
                exclusion_reason=r.get("exclusion_reason") or "",
                estimated_rows=int(float(r.get("estimated_rows") or 0)),
                estimated_runtime_s=float(r.get("estimated_runtime_s") or 0),
                chain_version=r.get("chain_version") or "",
                status_epoch=r.get("status_epoch") or "",
            )
        )
    return out


def select_windows(
    windows: Sequence[BatchWindow],
    *,
    window_id: str | None = None,
    max_windows: int | None = None,
    included_only: bool = True,
) -> list[BatchWindow]:
    sel = [w for w in windows if (w.included if included_only else True)]
    if window_id:
        sel = [w for w in sel if w.window_id == window_id]
    if max_windows is not None:
        sel = sel[: int(max_windows)]
    return sel

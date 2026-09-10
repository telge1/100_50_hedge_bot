"""Per-span candidate detection for LOCALIZED_EXCLUSION_V1.

`detect_candidates` / `assert_contiguous_seconds` stay fail-closed inside each
analyzable coverage span. Disjoint spans are never glued into one time series.
Frozen episode_candidate_v1 required fields are not versioned here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

import pandas as pd

from .detector import candidates_content_sha256, detect_candidates


DetectFn = Callable[..., tuple[pd.DataFrame, dict[str, Any]]]


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fmt(dt: datetime | pd.Timestamp | None) -> str | None:
    if dt is None or (isinstance(dt, float) and pd.isna(dt)):
        return None
    ts = pd.to_datetime(dt, utc=True)
    if pd.isna(ts):
        return None
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ts.microsecond == 0 else ts.isoformat().replace("+00:00", "Z")


def span_id_of(span: dict[str, Any]) -> str:
    return str(span.get("span_id") or span.get("coverage_span_id") or "")


def span_bounds(span: dict[str, Any]) -> tuple[datetime, datetime]:
    return _parse_ts(span["start"]), _parse_ts(span["end"])


def span_eligible_start(span: dict[str, Any]) -> datetime:
    raw = (
        span.get("final_analysis_eligible_start")
        or span.get("analysis_eligible_start")
        or span.get("start")
    )
    return _parse_ts(raw)


def is_analyzable_span(span: dict[str, Any]) -> bool:
    flag = span.get("span_analyzable", True)
    if isinstance(flag, str):
        return flag.strip().lower() in {"true", "1", "yes"}
    return bool(flag)


def select_analyzable_spans(coverage_report: dict[str, Any] | None) -> list[dict[str, Any]]:
    usable = list((coverage_report or {}).get("usable_spans") or [])
    return [sp for sp in usable if is_analyzable_span(sp)]


def slice_states_for_span(state_df: pd.DataFrame, span: dict[str, Any]) -> pd.DataFrame:
    if state_df is None or state_df.empty:
        return state_df.iloc[0:0].copy() if state_df is not None else pd.DataFrame()
    start, end = span_bounds(span)
    ts = pd.to_datetime(state_df["state_ts"], utc=True)
    out = state_df.loc[(ts >= start) & (ts < end)].copy()
    return out.sort_values("state_ts").reset_index(drop=True)


def filter_candidates_to_span_eligible(
    candidates: pd.DataFrame,
    span: dict[str, Any],
) -> pd.DataFrame:
    """Keep candidates in [eligible_start, span_end). Stamp coverage_span_id."""
    sid = span_id_of(span)
    start, end = span_bounds(span)
    elig = span_eligible_start(span)
    if candidates is None or candidates.empty:
        out = candidates.copy() if candidates is not None else pd.DataFrame()
        if sid and "coverage_span_id" not in out.columns:
            out["coverage_span_id"] = pd.Series(dtype=object)
        return out
    ts = pd.to_datetime(candidates["trigger_ts"], utc=True)
    keep = (ts >= elig) & (ts >= start) & (ts < end)
    out = candidates.loc[keep].copy()
    if sid:
        out["coverage_span_id"] = sid
    return out


def _empty_candidates() -> pd.DataFrame:
    return pd.DataFrame()


def _merge_diagnostics(parts: list[dict[str, Any]]) -> dict[str, Any]:
    if not parts:
        return {
            "baseline_diagnostics": [],
            "trigger_fire_counts": {},
            "candidate_counts": {},
            "cooldown": {},
            "n_state_rows": 0,
            "n_candidates": 0,
        }
    trigger_fire: dict[str, int] = {}
    cand_counts: dict[str, int] = {}
    baseline: list[dict[str, Any]] = []
    n_state = 0
    n_cand = 0
    for d in parts:
        for k, v in (d.get("trigger_fire_counts") or {}).items():
            trigger_fire[k] = trigger_fire.get(k, 0) + int(v)
        for k, v in (d.get("candidate_counts") or {}).items():
            cand_counts[k] = cand_counts.get(k, 0) + int(v)
        baseline.extend(list(d.get("baseline_diagnostics") or []))
        n_state += int(d.get("n_state_rows") or 0)
        n_cand += int(d.get("n_candidates") or 0)
    return {
        "baseline_diagnostics": baseline,
        "trigger_fire_counts": trigger_fire,
        "candidate_counts": cand_counts,
        "cooldown": parts[-1].get("cooldown") if parts else {},
        "n_state_rows": n_state,
        "n_candidates": n_cand,
        "per_span": True,
    }


def _validate_merged(candidates: pd.DataFrame, spans: list[dict[str, Any]]) -> None:
    if candidates is None or candidates.empty:
        return
    if candidates["candidate_id"].duplicated().any():
        dups = candidates.loc[candidates["candidate_id"].duplicated(keep=False), "candidate_id"].tolist()
        raise ValueError(f"duplicate_candidate_id count={len(dups)}")
    if "coverage_span_id" not in candidates.columns:
        raise ValueError("missing_coverage_span_id_after_per_span_detect")
    if candidates["coverage_span_id"].isna().any() or (candidates["coverage_span_id"].astype(str) == "").any():
        raise ValueError("candidate_missing_coverage_span_id")
    by_id = {span_id_of(sp): sp for sp in spans}
    ts = pd.to_datetime(candidates["trigger_ts"], utc=True)
    for i, row in candidates.iterrows():
        sid = str(row["coverage_span_id"])
        sp = by_id.get(sid)
        if sp is None:
            raise ValueError(f"candidate_unknown_span id={sid}")
        start, end = span_bounds(sp)
        elig = span_eligible_start(sp)
        t = pd.to_datetime(ts.loc[i], utc=True).to_pydatetime()
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        t = t.astimezone(timezone.utc)
        if t < start or t >= end:
            raise ValueError(f"candidate_outside_span id={sid} ts={_fmt(t)}")
        if t < elig:
            raise ValueError(f"candidate_before_eligible id={sid} ts={_fmt(t)}")


def detect_candidates_span_aware(
    state_df: pd.DataFrame,
    *,
    cfg: dict[str, Any],
    symbol: str,
    coverage_status: str,
    source_state_hash: str,
    replay_epoch: int,
    coverage_report: dict[str, Any] | None,
    detect_fn: DetectFn = detect_candidates,
) -> tuple[pd.DataFrame, dict[str, Any], dict[str, Any]]:
    """Detect candidates per analyzable span, then merge.

    No usable/analyzable spans → one `detect_candidates` call on the full frame
    (STRICT_WHOLE_WINDOW / single contiguous window parity).
    """
    n_loaded = 0 if state_df is None else int(len(state_df))
    spans = select_analyzable_spans(coverage_report)
    common = dict(
        cfg=cfg,
        symbol=symbol,
        coverage_status=coverage_status,
        source_state_hash=source_state_hash,
        replay_epoch=replay_epoch,
    )

    if not spans:
        cands, diag = detect_fn(state_df, **common)
        if cands is None:
            cands = _empty_candidates()
        report = {
            "mode": "SINGLE_FRAME",
            "n_state_rows_loaded": n_loaded,
            "n_localized_state_rows": n_loaded,
            "analyzable_span_count": 0,
            "spans": [],
            "n_candidates": int(len(cands)),
            "n_duplicate_candidate_ids": 0,
            "n_cross_span_candidates": 0,
            "internal_contiguity": "checked_in_detect_candidates",
        }
        diag = dict(diag)
        diag["span_aware"] = report
        return cands, diag, report

    parts: list[pd.DataFrame] = []
    diag_parts: list[dict[str, Any]] = []
    span_rows: list[dict[str, Any]] = []
    n_localized = 0

    for sp in spans:
        sliced = slice_states_for_span(state_df, sp)
        n_localized += int(len(sliced))
        sid = span_id_of(sp)
        start, end = span_bounds(sp)
        elig = span_eligible_start(sp)
        try:
            raw, diag = detect_fn(sliced, **common)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"span_detect_failed span_id={sid}: {exc}") from exc
        kept = filter_candidates_to_span_eligible(raw, sp)
        ts_state = (
            pd.to_datetime(sliced["state_ts"], utc=True)
            if sliced is not None and not sliced.empty
            else pd.Series(dtype="datetime64[ns, UTC]")
        )
        warmup_rows = int((ts_state < elig).sum()) if len(ts_state) else 0
        eligible_rows = int(((ts_state >= elig) & (ts_state < end)).sum()) if len(ts_state) else 0
        state_start = _fmt(ts_state.iloc[0]) if len(ts_state) else None
        state_end = _fmt(ts_state.iloc[-1]) if len(ts_state) else None
        span_rows.append(
            {
                "coverage_span_id": sid,
                "state_start": state_start,
                "state_end": state_end,
                "span_start": _fmt(start),
                "span_end": _fmt(end),
                "state_rows": int(len(sliced)),
                "analysis_eligible_start": _fmt(elig),
                "warmup_rows": warmup_rows,
                "eligible_rows": eligible_rows,
                "candidates": int(len(kept)),
                "internal_contiguity": True,
            }
        )
        parts.append(kept)
        diag_parts.append(diag)

    nonempty = [p for p in parts if p is not None and not p.empty]
    if nonempty:
        merged = pd.concat(nonempty, ignore_index=True)
        merged = merged.sort_values(["trigger_ts", "candidate_type", "candidate_id"]).reset_index(drop=True)
    else:
        merged = _empty_candidates()

    _validate_merged(merged, spans)
    n_dup = int(merged["candidate_id"].duplicated().sum()) if not merged.empty else 0
    n_cross = 0
    if not merged.empty and "coverage_span_id" in merged.columns:
        # one id → one span already required; cross means any row matching multiple spans
        ts = pd.to_datetime(merged["trigger_ts"], utc=True)
        for i, t in ts.items():
            hits = 0
            tt = pd.to_datetime(t, utc=True).to_pydatetime().astimezone(timezone.utc)
            for sp in spans:
                a, b = span_bounds(sp)
                if a <= tt < b:
                    hits += 1
            if hits != 1:
                n_cross += 1
        if n_cross:
            raise ValueError(f"cross_span_candidates count={n_cross}")

    diag = _merge_diagnostics(diag_parts)
    diag["n_candidates"] = int(len(merged))
    report = {
        "mode": "PER_SPAN",
        "n_state_rows_loaded": n_loaded,
        "n_localized_state_rows": n_localized,
        "analyzable_span_count": len(spans),
        "spans": span_rows,
        "n_candidates": int(len(merged)),
        "n_duplicate_candidate_ids": n_dup,
        "n_cross_span_candidates": n_cross,
        "internal_contiguity": "per_span",
        "candidates_content_sha256": candidates_content_sha256(merged),
    }
    diag["span_aware"] = report
    return merged, diag, report


def candidate_span_mapping_frame(candidates: pd.DataFrame) -> pd.DataFrame:
    if candidates is None or candidates.empty:
        return pd.DataFrame(columns=["candidate_id", "coverage_span_id", "trigger_ts", "candidate_type"])
    cols = [c for c in ["candidate_id", "coverage_span_id", "trigger_ts", "candidate_type"] if c in candidates.columns]
    return candidates[cols].copy()


def format_span_detect_terminal(report: dict[str, Any]) -> str:
    lines = [
        "CANDIDATE_DETECT span-aware",
        f"Total state rows loaded:     {report.get('n_state_rows_loaded')}",
        f"Total localized state rows:  {report.get('n_localized_state_rows')}",
        f"Analyzable span count:       {report.get('analyzable_span_count')}",
        f"Mode:                        {report.get('mode')}",
    ]
    for sp in report.get("spans") or []:
        lines.extend(
            [
                f"  span {sp.get('coverage_span_id')}",
                f"    state_start/end:         {sp.get('state_start')}–{sp.get('state_end')}",
                f"    state_rows:              {sp.get('state_rows')}",
                f"    analysis_eligible_start: {sp.get('analysis_eligible_start')}",
                f"    warmup_rows:             {sp.get('warmup_rows')}",
                f"    eligible_rows:           {sp.get('eligible_rows')}",
                f"    candidates:              {sp.get('candidates')}",
                f"    internal_contiguity:     {sp.get('internal_contiguity')}",
            ]
        )
    lines.extend(
        [
            f"Total candidates:            {report.get('n_candidates')}",
            f"Duplicate candidate IDs:     {report.get('n_duplicate_candidate_ids')}",
            f"Cross-span candidates:       {report.get('n_cross_span_candidates')}",
        ]
    )
    return "\n".join(lines) + "\n"

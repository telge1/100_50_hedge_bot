"""Parity and duplicate audits. Count/export only. No execution dedup."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .candles import _as_utc
from .config import (
    REQUESTED_SIGNAL_END_EXCLUSIVE,
    REQUESTED_SIGNAL_START,
    STRATEGY_ID,
    ensure_sg_on_path,
)
from .identity import CANDIDATE_LIVE_STRATEGY
from .query import ReadOnlyQueryClient

PRICE_ABS_TOL = 1e-10
PRICE_REL_TOL = 1e-8


def generation_key(row: dict[str, Any]) -> tuple:
    return (
        str(row.get("symbol") or "").upper(),
        str(row.get("timeframe") or ""),
        str(row.get("direction") or "").upper(),
        str(row.get("signal_type") or ""),
        str(row.get("candle_open_time") or "")[:19],
    )


def _meta(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("metadata")
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _norm_ts(value: Any) -> str:
    text = str(value or "")
    return text.replace("+00:00", "Z")[:19]


def _num_close(a: Any, b: Any) -> bool:
    try:
        x = float(a)
        y = float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)
    scale = max(abs(x), abs(y), 1.0)
    return abs(x - y) <= max(PRICE_ABS_TOL, PRICE_REL_TOL * scale)


def _dup_keys(rows: list[dict[str, Any]]) -> int:
    seen: dict[tuple, int] = defaultdict(int)
    for r in rows:
        seen[generation_key(r)] += 1
    return sum(1 for n in seen.values() if n > 1)


def classify_parity(
    research: list[dict[str, Any]],
    production: list[dict[str, Any]],
    *,
    scope_symbol: str,
) -> dict[str, Any]:
    if not scope_symbol:
        raise ValueError("parity requires explicit run symbol")
    research = [r for r in research if str(r.get("symbol") or "").upper() == scope_symbol.upper()]
    production = [p for p in production if str(p.get("symbol") or "").upper() == scope_symbol.upper()]
    by_id_r = {str(r["signal_id"]): r for r in research}
    by_id_p = {str(p["signal_id"]): p for p in production}
    exact = 0
    field_mismatch = 0
    research_only = 0
    production_only = 0
    not_comp_version = 0
    details: list[dict[str, Any]] = []

    for sid, r in by_id_r.items():
        if sid in by_id_p:
            p = by_id_p[sid]
            diffs = []
            for key in ("symbol", "timeframe", "direction", "signal_type", "tier_a"):
                if str(r.get(key)) != str(p.get(key)):
                    diffs.append(key)
            if diffs:
                field_mismatch += 1
                details.append({"class": "FIELD_MISMATCH", "signal_id": sid, "fields": diffs})
            else:
                exact += 1
                details.append({"class": "EXACT_MATCH", "signal_id": sid})
        else:
            research_only += 1
            details.append({"class": "RESEARCH_ONLY", "signal_id": sid, "generation_key": list(generation_key(r))})

    for sid, p in by_id_p.items():
        if sid not in by_id_r:
            sv = str(p.get("strategy_version") or "")
            if sv and sv != "wave_fade_frozen_f16ae32":
                not_comp_version += 1
                details.append({"class": "NOT_COMPARABLE_VERSION", "signal_id": sid, "strategy_version": sv})
            else:
                production_only += 1
                details.append({"class": "PRODUCTION_ONLY", "signal_id": sid})

    by_gen_r = {generation_key(r): r for r in research}
    by_gen_p = {generation_key(p): p for p in production}
    gen_match = 0
    gen_mismatch = 0
    gen_research_only = 0
    gen_prod_only = 0
    gen_mismatch_sample: list[dict[str, Any]] = []
    for key, r in by_gen_r.items():
        p = by_gen_p.get(key)
        if p is None:
            gen_research_only += 1
            continue
        diffs: list[str] = []
        pm = _meta(p)
        if bool(r.get("tier_a")) != bool(p.get("tier_a")):
            diffs.append("tier_a")
        if str(r.get("direction") or "").upper() != str(p.get("direction") or "").upper():
            diffs.append("direction")
        if str(r.get("signal_type") or "") != str(p.get("signal_type") or ""):
            diffs.append("signal_type")
        if _norm_ts(r.get("candle_open_time")) != _norm_ts(p.get("candle_open_time")):
            diffs.append("candle_open_time")
        if _norm_ts(r.get("confirmation_available_at") or r.get("generated_at")) != _norm_ts(
            pm.get("confirmation_available_at") or pm.get("available_at") or p.get("generated_at")
        ):
            diffs.append("confirmation_available_at")
        r_entry = _norm_ts(r.get("entry_time"))
        p_entry = _norm_ts(pm.get("entry_time") or p.get("entry_time"))
        if r_entry and p_entry and r_entry != p_entry:
            diffs.append("entry_time")
        for name in ("initial_entry_price", "entry_price", "tp_price", "sl_price"):
            rv = r.get("initial_entry_price") if name == "initial_entry_price" else r.get(name)
            if name == "initial_entry_price" and rv is None:
                rv = r.get("entry_price")
            pv = p.get(name)
            if pv is None:
                pv = pm.get(name)
            if name == "initial_entry_price" and pv is None:
                pv = pm.get("entry_price") or p.get("entry_price")
            if rv is None or pv is None:
                continue
            if not _num_close(rv, pv):
                diffs.append(name)
        if diffs:
            gen_mismatch += 1
            if len(gen_mismatch_sample) < 20:
                gen_mismatch_sample.append({"generation_key": list(key), "fields": diffs})
        else:
            gen_match += 1
    gen_dup_r = _dup_keys(research)
    gen_dup_p = _dup_keys(production)
    for key in by_gen_p:
        if key not in by_gen_r:
            gen_prod_only += 1

    prod_times = [_norm_ts(p.get("candle_open_time")) for p in production if p.get("candle_open_time")]
    return {
        "scope_symbol": scope_symbol,
        "strategy_research": STRATEGY_ID,
        "strategy_production": CANDIDATE_LIVE_STRATEGY,
        "research_keys": len(by_gen_r),
        "production_keys": len(by_gen_p),
        "intersection": gen_match + gen_mismatch,
        "field_matches": gen_match,
        "field_mismatches": gen_mismatch,
        "research_only": gen_research_only,
        "production_only": gen_prod_only,
        "EXACT_MATCH": exact,
        "FIELD_MISMATCH": field_mismatch,
        "RESEARCH_ONLY": research_only,
        "PRODUCTION_ONLY": production_only,
        "NOT_COMPARABLE_VERSION": not_comp_version,
        "NOT_COMPARABLE_WINDOW": 0,
        "research_generation_keys": len(by_gen_r),
        "production_generation_keys": len(by_gen_p),
        "generation_key_intersection": gen_match + gen_mismatch,
        "generation_key_match": gen_match,
        "generation_key_field_mismatch": gen_mismatch,
        "generation_key_research_only": gen_research_only,
        "generation_key_production_only": gen_prod_only,
        "duplicate_generation_keys_research": gen_dup_r,
        "duplicate_generation_keys_production": gen_dup_p,
        "production_window_min": min(prod_times) if prod_times else None,
        "production_window_max": max(prod_times) if prod_times else None,
        "price_tolerance": {"abs": PRICE_ABS_TOL, "rel": PRICE_REL_TOL},
        "generation_mismatch_sample": gen_mismatch_sample,
        "details_sample": details[:50],
        "detail_count": len(details),
        "note": "UUID includes strategy_version; generation_key ignores version tag",
    }


def load_production_signals(
    client: ReadOnlyQueryClient,
    *,
    symbol: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict[str, Any]]:
    if not symbol:
        raise ValueError("production load requires explicit run symbol")
    db = client.database
    result = client.query(
        f"""
        SELECT
            toString(signal_id) AS signal_id,
            symbol,
            timeframe,
            direction,
            signal_type,
            candle_open_time,
            generated_at,
            tier_a,
            strategy_version,
            metadata
        FROM {db}.signals FINAL
        WHERE symbol = {{symbol:String}}
          AND strategy_version = {{sv:String}}
          AND candle_open_time >= {{start:DateTime64(3, 'UTC')}}
          AND candle_open_time < {{end:DateTime64(3, 'UTC')}}
        ORDER BY candle_open_time
        """,
        {
            "symbol": symbol,
            "sv": CANDIDATE_LIVE_STRATEGY,
            "start": start or REQUESTED_SIGNAL_START,
            "end": end or REQUESTED_SIGNAL_END_EXCLUSIVE,
        },
    )
    cols = result.column_names
    out = []
    for row in result.result_rows:
        rec = dict(zip(cols, row, strict=True))
        rec["signal_id"] = str(rec["signal_id"])
        ot = rec.get("candle_open_time")
        if hasattr(ot, "isoformat"):
            rec["candle_open_time"] = _as_utc(ot).isoformat().replace("+00:00", "Z")
        ga = rec.get("generated_at")
        if hasattr(ga, "isoformat"):
            rec["generated_at"] = _as_utc(ga).isoformat().replace("+00:00", "Z")
        out.append(rec)
    return out


def duplicate_audit(tier_a: list[dict[str, Any]]) -> dict[str, Any]:
    ensure_sg_on_path()
    from signal_generator.strategy.wave_fade.parameters import PAIR_WINDOW_MIN

    by_entry: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in tier_a:
        if s.get("entry_time"):
            by_entry[str(s["entry_time"])].append(s)
    same_entry_multi_tf = {
        k: [{"timeframe": x["timeframe"], "direction": x["direction"]} for x in v]
        for k, v in by_entry.items()
        if len({x["timeframe"] for x in v}) > 1
    }
    same_dir = sum(
        1
        for v in same_entry_multi_tf.values()
        if len({x["direction"] for x in v}) == 1
    )
    mixed_dir = sum(
        1
        for v in same_entry_multi_tf.values()
        if len({x["direction"] for x in v}) > 1
    )
    return {
        "tier_a_n": len(tier_a),
        "same_entry_multi_tf_count": len(same_entry_multi_tf),
        "same_entry_same_direction": same_dir,
        "same_entry_mixed_direction": mixed_dir,
        "pair_window_min": {f"{a}|{b}": m for (a, b), m in PAIR_WINDOW_MIN.items()},
        "execution_dedup_applied": False,
        "same_entry_multi_tf_sample": dict(list(same_entry_multi_tf.items())[:30]),
    }

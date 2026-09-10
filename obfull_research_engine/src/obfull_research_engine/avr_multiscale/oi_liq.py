"""OI and liquidation multi-window aggregates from mb_state_1s (causal)."""

from __future__ import annotations

from typing import Any

import pandas as pd

from . import OI_MAX_AGE_SECONDS


def _state_index(state_df: pd.DataFrame) -> dict[int, pd.Series]:
    out: dict[int, pd.Series] = {}
    if state_df is None or state_df.empty:
        return out
    sdf = state_df.copy()
    sdf["_u"] = pd.to_datetime(sdf["state_ts"], utc=True).astype("int64") // 10**9
    for _, r in sdf.iterrows():
        out[int(r["_u"])] = r
    return out


def oi_window_from_states(
    state_by_ts: dict[int, pd.Series],
    *,
    t_unix: float,
    window_s: int,
    prefix: str,
) -> dict[str, Any]:
    """OI start/end over [t-window, t) using completed state seconds with state_ts+1 <= t."""
    w = int(window_s)
    t = float(t_unix)
    # Use states whose conceptual available_at (state_ts+1) is in (t-w, t]
    # i.e. state_ts in [floor(t)-w, floor(t)-1]
    end_state = int(t) - 1
    start_state = end_state - w + 1
    samples: list[tuple[int, pd.Series]] = []
    for ts in range(start_state, end_state + 1):
        if ts in state_by_ts:
            samples.append((ts, state_by_ts[ts]))

    base = {
        f"{prefix}_oi_start": None,
        f"{prefix}_oi_end": None,
        f"{prefix}_oi_delta_abs": None,
        f"{prefix}_oi_delta_pct": None,
        f"{prefix}_oi_sample_count": 0,
        f"{prefix}_oi_start_age_seconds": None,
        f"{prefix}_oi_end_age_seconds": None,
        f"{prefix}_oi_max_age_seconds": None,
        f"{prefix}_oi_direction": "UNKNOWN",
        f"{prefix}_oi_coverage_status": "NO_SAMPLES",
        f"{prefix}_oi_max_age_contract_s": OI_MAX_AGE_SECONDS,
    }
    if not samples:
        return base

    def _oi(row: pd.Series) -> float | None:
        v = row.get("open_interest")
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        # respect validity / age contract
        age_ms = row.get("open_interest_age_ms")
        age_s = float(age_ms) / 1000.0 if age_ms is not None and pd.notna(age_ms) else None
        valid = row.get("open_interest_valid")
        if valid is False:
            return None
        if age_s is not None and age_s > OI_MAX_AGE_SECONDS:
            return None
        return float(v)

    def _age(row: pd.Series) -> float | None:
        age_ms = row.get("open_interest_age_ms")
        if age_ms is None or (isinstance(age_ms, float) and pd.isna(age_ms)):
            return None
        return float(age_ms) / 1000.0

    oi_vals: list[tuple[int, float, float | None]] = []
    ages: list[float] = []
    for ts, row in samples:
        oi = _oi(row)
        age = _age(row)
        if age is not None:
            ages.append(age)
        if oi is not None:
            oi_vals.append((ts, oi, age))

    if not oi_vals:
        base[f"{prefix}_oi_sample_count"] = 0
        base[f"{prefix}_oi_max_age_seconds"] = max(ages) if ages else None
        base[f"{prefix}_oi_coverage_status"] = "NO_VALID_OI"
        return base

    oi_vals.sort(key=lambda x: x[0])
    start_oi = oi_vals[0][1]
    end_oi = oi_vals[-1][1]
    delta = end_oi - start_oi
    pct = (delta / start_oi * 100.0) if start_oi != 0 else None
    if delta == 0.0:
        direction = "FLAT"
    elif delta > 0:
        direction = "RISING"
    else:
        direction = "FALLING"

    base.update(
        {
            f"{prefix}_oi_start": start_oi,
            f"{prefix}_oi_end": end_oi,
            f"{prefix}_oi_delta_abs": delta,
            f"{prefix}_oi_delta_pct": pct,
            f"{prefix}_oi_sample_count": len(oi_vals),
            f"{prefix}_oi_start_age_seconds": oi_vals[0][2],
            f"{prefix}_oi_end_age_seconds": oi_vals[-1][2],
            f"{prefix}_oi_max_age_seconds": max(ages) if ages else None,
            f"{prefix}_oi_direction": direction,
            f"{prefix}_oi_coverage_status": "OK",
        }
    )
    return base


def liq_window_from_states(
    state_by_ts: dict[int, pd.Series],
    *,
    t_unix: float,
    window_s: int,
    prefix: str,
) -> dict[str, Any]:
    """Sum true liquidations over causal state seconds in [t-window, t)."""
    w = int(window_s)
    t = float(t_unix)
    end_state = int(t) - 1
    start_state = end_state - w + 1

    long_n = short_n = 0.0
    long_c = short_c = 0
    total_events = 0
    any_valid = False
    any_row = False
    max_evt = 0.0
    max_evt_ts = None
    max_side = None

    for ts in range(start_state, end_state + 1):
        if ts not in state_by_ts:
            continue
        any_row = True
        row = state_by_ts[ts]
        valid = row.get("liquidations_valid")
        if valid is False:
            continue
        any_valid = True
        ln = float(row.get("long_liquidation_notional_usdt") or 0.0)
        sn = float(row.get("short_liquidation_notional_usdt") or 0.0)
        if pd.isna(ln):
            ln = 0.0
        if pd.isna(sn):
            sn = 0.0
        cnt = row.get("liquidation_count")
        c = int(cnt) if cnt is not None and pd.notna(cnt) else (1 if (ln + sn) > 0 else 0)
        long_n += ln
        short_n += sn
        total_events += c
        if ln > 0:
            long_c += 1
        if sn > 0:
            short_c += 1
        if ln > max_evt:
            max_evt, max_evt_ts, max_side = ln, ts, "LONG"
        if sn > max_evt:
            max_evt, max_evt_ts, max_side = sn, ts, "SHORT"

    if not any_row:
        cov = "SOURCE_MISSING"
        # distinguish: no state rows in window
        status_note = "NO_STATE_ROWS"
    elif not any_valid:
        cov = "UNKNOWN"
        status_note = "LIQUIDATIONS_INVALID"
    else:
        cov = "OK"
        status_note = "OK"

    return {
        f"{prefix}_liq_long_count": long_c if cov == "OK" else None,
        f"{prefix}_liq_short_count": short_c if cov == "OK" else None,
        f"{prefix}_liq_long_notional": long_n if cov == "OK" else None,
        f"{prefix}_liq_short_notional": short_n if cov == "OK" else None,
        f"{prefix}_liq_total_notional": (long_n + short_n) if cov == "OK" else None,
        f"{prefix}_liq_net_notional": (long_n - short_n) if cov == "OK" else None,
        f"{prefix}_liq_event_count": total_events if cov == "OK" else None,
        f"{prefix}_liq_max_event_notional": max_evt if cov == "OK" else None,
        f"{prefix}_liq_max_event_state_ts_unix": max_evt_ts if cov == "OK" else None,
        f"{prefix}_liq_max_event_side": max_side if cov == "OK" else None,
        f"{prefix}_liq_coverage_status": cov,
        f"{prefix}_liq_coverage_note": status_note,
    }


def build_state_index(state_df: pd.DataFrame) -> dict[int, pd.Series]:
    return _state_index(state_df)

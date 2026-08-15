"""Audit helpers for unresolved individual_tp_scaled multistart cases.

Cause-classification rules (deterministic, non-ML):
- CONTINUED_DOWNTREND: max_drop_from_start_pct <= -10 AND
  (end_ret_pct <= -8 OR end_price within 3% of window min)
- INSUFFICIENT_REBOUND: max_drop_from_start_pct <= -5 AND
  max_rally_from_low_pct < 5
- TP_HARVEST_TOO_SLOW: overlay_grows_faster_than_tp_harvest is True
  (cumulative adds exceed cumulative TP closes for >50% of post-first-add bars)
- OVERLAY_SATURATED: max_overlay_to_core_ratio >= 3.5 OR number_of_short_adds >= 7
- FEES_DRAG: total_fees_usdt >= 5 AND best_total_economics_usdt < 0.25
- NEAR_BE_AT_HORIZON: best_total_economics_usdt >= -1.0
- LARGE_OPEN_OVERLAY: unresolved_overlay_qty >= 0.5 * initial_long_qty
- LOW_VOLATILITY_AFTER_DROP: max_drop <= -5 AND
  (max_price - min_price)/start_price < 0.08 after the low is set
  (proxy: max_rally_from_low < 4 AND continued mild drift)
- V_REVERSAL: max_rally_from_low_pct >= 10 AND max_drop_from_start_pct <= -5
  AND still unresolved (rebound happened but BE not locked)
- OTHER: none of the above
"""

from __future__ import annotations

from typing import Any

BE_THRESHOLD = 0.25  # target 0 + safety 0.25

CAUSE_RULES_TEXT = """\
Cause-classification rules (deterministic, non-ML):
- CONTINUED_DOWNTREND: max_drop_from_start_pct <= -10 AND
  (end_ret_pct <= -8 OR end_price within 3% of window min)
- INSUFFICIENT_REBOUND: max_drop_from_start_pct <= -5 AND
  max_rally_from_low_pct < 5
- TP_HARVEST_TOO_SLOW: overlay_grows_faster_than_tp_harvest is True
  (cumulative adds exceed cumulative TP closes for >50% of post-first-add bars)
- OVERLAY_SATURATED: max_overlay_to_core_ratio >= 3.5 OR number_of_short_adds >= 7
- FEES_DRAG: total_fees_usdt >= 5 AND best_total_economics_usdt < 0.25
- NEAR_BE_AT_HORIZON: best_total_economics_usdt >= -1.0
- LARGE_OPEN_OVERLAY: unresolved_overlay_qty >= 0.5 * initial_long_qty
- LOW_VOLATILITY_AFTER_DROP: max_drop <= -5 AND
  (max_price - min_price)/start_price < 0.08 after the low is set
  (proxy: max_rally_from_low < 4 AND continued mild drift)
- V_REVERSAL: max_rally_from_low_pct >= 10 AND max_drop_from_start_pct <= -5
  AND still unresolved (rebound happened but BE not locked)
- OTHER: none of the above
"""


def _f(x: Any, default: float = 0.0) -> float:
    if x is None or x == "":
        return float(default)
    return float(x)


def classify_unresolved_causes(case: dict[str, Any]) -> list[str]:
    causes: list[str] = []
    drop = _f(case.get("max_drop_from_start_pct"))
    rally = _f(case.get("max_rally_from_low_pct"))
    end_ret = _f(case.get("end_ret_pct"))
    best = _f(case.get("best_total_economics_usdt"), -1e18)
    fees = _f(case.get("total_fees_usdt"))
    ov_ratio = _f(case.get("max_overlay_to_core_ratio"))
    adds = int(_f(case.get("number_of_short_adds")))
    ov_qty = _f(case.get("unresolved_overlay_qty"))
    core_qty = _f(case.get("initial_long_qty"))
    end_near_min = bool(case.get("end_near_window_min"))
    grows_faster = bool(case.get("overlay_grows_faster_than_tp_harvest"))

    if drop <= -0.10 and (end_ret <= -0.08 or end_near_min):
        causes.append("CONTINUED_DOWNTREND")
    if drop <= -0.05 and rally < 0.05:
        causes.append("INSUFFICIENT_REBOUND")
    if grows_faster:
        causes.append("TP_HARVEST_TOO_SLOW")
    if ov_ratio >= 3.5 or adds >= 7:
        causes.append("OVERLAY_SATURATED")
    if fees >= 5.0 and best < BE_THRESHOLD:
        causes.append("FEES_DRAG")
    if best >= -1.0:
        causes.append("NEAR_BE_AT_HORIZON")
    if core_qty > 0 and ov_qty >= 0.5 * core_qty:
        causes.append("LARGE_OPEN_OVERLAY")
    if drop <= -0.05 and rally < 0.04:
        causes.append("LOW_VOLATILITY_AFTER_DROP")
    if drop <= -0.05 and rally >= 0.10:
        causes.append("V_REVERSAL")
    if not causes:
        causes.append("OTHER")
    # de-dupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for c in causes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def near_be_flags(best_econ: float) -> dict[str, bool]:
    return {
        "near_be_1": best_econ >= -1.0,
        "near_be_5": best_econ >= -5.0,
        "near_be_10": best_econ >= -10.0,
    }


def replay_metric_keys() -> list[str]:
    return [
        "final_status",
        "recovered_be",
        "unresolved",
        "final_total_economics_usdt",
        "number_of_short_adds",
        "number_of_partial_tp_events",
        "max_overlay_qty",
        "recovery_bars",
        "max_adverse_total_economics_usdt",
        "total_fees_usdt",
    ]


def _as_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower() == "true"
    return None


def _as_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def compare_replay(
    expected: dict[str, Any], actual: dict[str, Any], *, tol: float = 1e-6
) -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []
    for key in replay_metric_keys():
        ev = expected.get(key)
        av = actual.get(key)
        eb = _as_bool(ev)
        ab = _as_bool(av)
        if eb is not None or ab is not None:
            if eb != ab:
                diffs.append(
                    {
                        "metric": key,
                        "expected": ev,
                        "actual": av,
                        "abs_diff": None,
                    }
                )
            continue
        ef = _as_float(ev)
        af = _as_float(av)
        if ef is not None and af is not None:
            if abs(ef - af) > tol:
                diffs.append(
                    {
                        "metric": key,
                        "expected": ef,
                        "actual": af,
                        "abs_diff": af - ef,
                    }
                )
            continue
        if str(ev) != str(av):
            diffs.append(
                {"metric": key, "expected": ev, "actual": av, "abs_diff": None}
            )
    return diffs

"""Post-decision directional outcome labels (not trades / not PnL)."""

from __future__ import annotations

from typing import Any

import pandas as pd

from orderbook_analyse.ema_zone_microstructure_confirmation.continuous_defaults import (
    GROSS_MFE_THRESHOLD_PCT,
    OUTCOME_HORIZONS_S,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows import MISSING
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.outcomes import (
    side_mfe_mae,
)


def build_price_path(
    samples: list[Any],
    candles_1m: pd.DataFrame,
    *,
    start_ms: int,
    end_ms: int,
) -> list[tuple[int, float]]:
    """Prefer genuine L2 mids; extend with 1m closes after L2 ends (price path only)."""
    path: list[tuple[int, float]] = [
        (s.ts_ms, s.mid)
        for s in samples
        if getattr(s, "genuine", True)
        and not getattr(s, "carried_forward", False)
        and start_ms <= s.ts_ms <= end_ms
    ]
    if candles_1m is None or candles_1m.empty:
        return path
    last_l2 = path[-1][0] if path else start_ms
    times = pd.to_datetime(candles_1m["open_time"], utc=True)
    for _, row in candles_1m.iterrows():
        ts = pd.Timestamp(row["open_time"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        ts_ms = int(ts.timestamp() * 1000)
        # use bar end ≈ open+1m as price availability
        avail = ts_ms + 60_000
        if avail <= last_l2 or avail < start_ms or avail > end_ms:
            continue
        path.append((avail, float(row["close"])))
    path.sort(key=lambda x: x[0])
    return path


def label_outcomes_for_candidates(
    candidates: list[dict[str, Any]],
    *,
    path: list[tuple[int, float]],
    path_end_ms: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for c in candidates:
        direction = str(c.get("candidate_direction") or "").upper()
        if direction not in ("LONG", "SHORT"):
            direction = ""
        anchor = c.get("label_anchor_at")
        px = c.get("label_anchor_price")
        if not direction or anchor in (None, MISSING) or px in (None, MISSING):
            for h in OUTCOME_HORIZONS_S:
                rows.append(
                    {
                        "symbol": c.get("symbol"),
                        "episode_id": c.get("episode_id"),
                        "candidate_state": c.get("candidate_state"),
                        "candidate_direction": c.get("candidate_direction") or "NONE",
                        "horizon_s": h,
                        "mfe_pct": MISSING,
                        "mae_pct": MISSING,
                        "endpoint_pct": MISSING,
                        "time_to_mfe": MISSING,
                        "time_to_mae": MISSING,
                        "horizon_complete": False,
                        "path_coverage": 0.0,
                        "gross_mfe_above_0_15_pct": MISSING,
                        "label_anchor_at": anchor if anchor else MISSING,
                        "decision_at": c.get("decision_at", MISSING),
                        "skipped_reason": "no_direction_or_anchor",
                    }
                )
            continue
        from datetime import datetime

        anchor_ms = int(datetime.fromisoformat(str(anchor).replace("Z", "+00:00")).timestamp() * 1000)
        decision_ms = int(
            datetime.fromisoformat(str(c["decision_at"]).replace("Z", "+00:00")).timestamp() * 1000
        )
        assert anchor_ms > decision_ms, "label_anchor must be strictly after decision_at"
        entry_px = float(px)
        for h in OUTCOME_HORIZONS_S:
            need_end = anchor_ms + h * 1000
            complete = path_end_ms >= need_end
            m = side_mfe_mae(
                path,
                entry_ts_ms=anchor_ms,
                entry_px=entry_px,
                direction=direction,
                horizon_s=h,
            )
            # path coverage: fraction of horizon with samples
            pts = [t for t, _ in path if anchor_ms <= t <= need_end]
            coverage = 0.0
            if h > 0 and pts:
                span = (pts[-1] - pts[0]) / 1000.0
                coverage = min(1.0, span / h)
            mfe = m["mfe_pct"]
            above = MISSING
            if mfe not in (None, MISSING) and complete:
                above = bool(float(mfe) > GROSS_MFE_THRESHOLD_PCT)
            rows.append(
                {
                    "symbol": c.get("symbol"),
                    "episode_id": c.get("episode_id"),
                    "candidate_state": c.get("candidate_state"),
                    "candidate_direction": direction,
                    "regime": c.get("regime"),
                    "zone_name": c.get("zone_name"),
                    "mechanism": c.get("mechanism"),
                    "major_wall_confluence": c.get("major_wall_confluence"),
                    "horizon_s": h,
                    "mfe_pct": m["mfe_pct"] if complete else MISSING,
                    "mae_pct": m["mae_pct"] if complete else MISSING,
                    "endpoint_pct": m["endpoint_pct"] if complete else MISSING,
                    "time_to_mfe": m["t_mfe_s"] if complete else MISSING,
                    "time_to_mae": m["t_mae_s"] if complete else MISSING,
                    "horizon_complete": complete,
                    "path_coverage": coverage,
                    "gross_mfe_above_0_15_pct": above,
                    "label_anchor_at": anchor,
                    "decision_at": c.get("decision_at"),
                    "skipped_reason": "" if complete else "horizon_incomplete",
                }
            )
    return rows


def summarize_outcomes(rows: list[dict[str, Any]], group_keys: list[str]) -> list[dict[str, Any]]:
    if not rows:
        return []
    df = pd.DataFrame(rows)
    df = df[df["horizon_complete"] == True]  # noqa: E712
    for col in ("mfe_pct", "mae_pct", "endpoint_pct"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    out: list[dict[str, Any]] = []
    if df.empty:
        return out
    groups = group_keys + ["horizon_s"]
    for keys, g in df.groupby(groups, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        rec = {k: v for k, v in zip(groups, keys)}
        rec.update(
            {
                "n": int(len(g)),
                "n_episodes": int(g["episode_id"].nunique()),
                "mean_mfe_pct": float(g["mfe_pct"].mean()),
                "median_mfe_pct": float(g["mfe_pct"].median()),
                "mean_mae_pct": float(g["mae_pct"].mean()),
                "mean_endpoint_pct": float(g["endpoint_pct"].mean()),
                "frac_mfe_above_0_15": float((g["mfe_pct"] > GROSS_MFE_THRESHOLD_PCT).mean()),
                "statistical_significance_claimed": False,
            }
        )
        out.append(rec)
    return out

"""Incremental E1R V1 state machine (ported 1:1 from frozen simulate_e1_e1r + signal_ignore)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from bot.e1r_live_scanner.config import ensure_runtime_paths


def gap_label(gap: float | None) -> str:
    if gap is None:
        return "unknown"
    if gap < 0:
        return "overlap"
    if abs(gap) < 1e-9:
        return "touching"
    return "positive"


def _structure_payload(ctx: dict, pools: list[dict], moment: datetime, close: float, anchor: dict) -> dict:
    ensure_runtime_paths()
    from analyze_xrp_bull_regime_exit_shadow_v1 import structural_support_failure

    fail, why = structural_support_failure(ctx, pools, moment, close, anchor)
    return {
        "rising_3": ctx.get("lower_cluster_rising_3"),
        "cluster_1_top": ctx.get("cluster_1_top"),
        "cluster_2_top": ctx.get("cluster_2_top"),
        "cluster_3_top": ctx.get("cluster_3_top"),
        "gap_1_2": ctx.get("gap_1_to_2"),
        "gap_2_3": ctx.get("gap_2_to_3"),
        "gap_1_2_status": gap_label(ctx.get("gap_1_to_2")),
        "gap_2_3_status": gap_label(ctx.get("gap_2_to_3")),
        "overlap_1_2": ctx.get("overlap_1_2"),
        "overlap_2_3": ctx.get("overlap_2_3"),
        "structural_gap_intact": not fail,
        "structure_fail_reason": why if fail else None,
        "cluster_2_pool_ids": ctx.get("cluster_2_pool_ids"),
        "cluster_3_pool_ids": ctx.get("cluster_3_pool_ids"),
    }


def _bullish_cross_at(bars15: list[dict], i: int) -> dict | None:
    if i < 1:
        return None
    p, c = bars15[i - 1], bars15[i]
    e0, e1 = p.get("ema200"), c.get("ema200")
    if e0 is None or e1 is None:
        return None
    if p["close"] <= e0 and c["close"] > e1:
        return {
            "cross_time": c["close_time"].isoformat(),
            "cross_direction": "bullish",
            "close_before": p["close"],
            "ema_before": e0,
            "close_after": c["close"],
            "ema_after": e1,
            "known_at": c["close_time"].isoformat(),
            "bar_index": i,
        }
    return None


class E1REngine:
    """One bar at a time; mirrors frozen E1R branch of simulate_e1_e1r."""

    def __init__(self, sim_from: datetime, sim_to: datetime) -> None:
        self.sim_from = sim_from
        self.sim_to = sim_to
        self.e1r_state = "INACTIVE"
        self.e1r_ep: dict | None = None
        self.pending: dict | None = None
        self.e1r_machine: dict[datetime, str] = {}
        self.reclaim_events: list[dict] = []

    def in_sim_window(self, ct: datetime) -> bool:
        return self.sim_from <= ct <= self.sim_to

    def process_bar(
        self,
        i: int,
        bar: dict,
        bars15: list[dict],
        candles15,
        cfg,
        cache: dict,
    ) -> None:
        ct = bar["close_time"]
        if not self.in_sim_window(ct):
            return

        ensure_runtime_paths()
        from analyze_xrp_bull_regime_exit_shadow_v1 import close_episode, episode_start_payload
        from analyze_xrp_ema200_cluster3_persistent_shadow_v1 import ladder_at_index
        from dashboard.research_charts.lld_research_kernel import scanner_pools_for_index

        ctx = ladder_at_index(candles15, bars15, i, cfg, cache)
        pools, _, _ = scanner_pools_for_index(candles15, "15m", i, cfg, cache=cache)
        rising_3 = ctx["lower_cluster_rising_3"]
        close = float(bar["close"])
        ema = bar.get("ema200")
        above = close > ema if ema else False
        cross = _bullish_cross_at(bars15, i)
        is_start = cross is not None and rising_3

        anchor = self.e1r_ep or {}
        if self.e1r_state == "INACTIVE":
            if is_start and cross is not None:
                self.e1r_state = "ACTIVE"
                self.e1r_ep = episode_start_payload(ctx, cross, bar)
                self.e1r_ep["variant"] = "E1R"
        elif self.e1r_state == "ACTIVE":
            if not rising_3:
                self.e1r_state = "INACTIVE"
                if self.e1r_ep:
                    close_episode(self.e1r_ep, ct, "rising_3_false", ctx, bar)
                    self.e1r_ep = None
            elif above:
                pass
            else:
                sp = _structure_payload(ctx, pools, ct, close, anchor)
                if sp["rising_3"] and sp["structural_gap_intact"]:
                    self.e1r_state = "PENDING"
                    self.pending = {
                        "dip_time": ct.isoformat(),
                        "dip_close": close,
                        "ema200": ema,
                        "distance_below_ema_pct": round((close - ema) / ema * 100.0, 4) if ema else None,
                        **sp,
                    }
                else:
                    self.e1r_state = "INACTIVE"
                    if self.e1r_ep:
                        close_episode(self.e1r_ep, ct, "ema_dip_structure_broken", ctx, bar)
                        self.e1r_ep = None
        elif self.e1r_state == "PENDING" and self.pending:
            sp_next = _structure_payload(ctx, pools, ct, close, anchor)
            success = above and sp_next["rising_3"] and sp_next["structural_gap_intact"]
            ev = {
                **self.pending,
                "next_bar_time": ct.isoformat(),
                "next_close": close,
                "next_ema200": ema,
                "next_distance_close_to_ema_pct": round((close - ema) / ema * 100.0, 4) if ema else None,
                "rising_3_next_bar": sp_next["rising_3"],
                "structure_next_bar": sp_next,
                "result": "EMA_RECLAIM_SUCCESS" if success else "EMA_RECLAIM_FAILED",
            }
            self.reclaim_events.append(ev)
            if success:
                self.e1r_state = "ACTIVE"
                if self.e1r_ep:
                    self.e1r_ep.setdefault("reclaims", []).append(ev)
            else:
                self.e1r_state = "INACTIVE"
                if self.e1r_ep:
                    close_episode(self.e1r_ep, ct, "ema_reclaim_failed", ctx, bar)
                    self.e1r_ep = None
            self.pending = None

        self.e1r_machine[ct] = self.e1r_state

    def evaluate_signal(
        self,
        decision: datetime,
        entry_price: float,
        bars15: list[dict],
        candles15,
        cfg,
        cache: dict,
    ) -> dict:
        """Frozen signal_ignore(..., variant='E1R')."""
        ensure_runtime_paths()
        from analyze_xrp_ema200_cluster3_persistent_shadow_v1 import ladder_at_index, last_closed_idx

        idx = last_closed_idx(bars15, decision)
        if idx is None:
            return {"active": False, "ignore": False, "status": "NO_BAR", "e1r_state": "INACTIVE"}
        bar = bars15[idx]
        ct = bar["close_time"]
        ctx = ladder_at_index(candles15, bars15, idx, cfg, cache, price=entry_price)
        above = ctx["price_above_ema200"] is True
        rising_3 = ctx["lower_cluster_rising_3"]

        st = self.e1r_machine.get(ct, "INACTIVE")
        if st == "PENDING":
            return {
                "active": False,
                "ignore": False,
                "status": "PENDING_NOT_RESOLVED",
                "e1r_state": st,
                "ema200": bar.get("ema200"),
                "rising_3": rising_3,
                "cluster_1_top": ctx.get("cluster_1_top"),
                "cluster_2_top": ctx.get("cluster_2_top"),
                "cluster_3_top": ctx.get("cluster_3_top"),
            }
        if st == "ACTIVE":
            ok = above and rising_3
            return {
                "active": True,
                "ignore": ok,
                "status": "ACTIVE" if ok else "ACTIVE_NO_CONTINUATION",
                "e1r_state": st,
                "ema200": bar.get("ema200"),
                "rising_3": rising_3,
                "cluster_1_top": ctx.get("cluster_1_top"),
                "cluster_2_top": ctx.get("cluster_2_top"),
                "cluster_3_top": ctx.get("cluster_3_top"),
            }
        return {
            "active": False,
            "ignore": False,
            "status": "INACTIVE",
            "e1r_state": st,
            "ema200": bar.get("ema200"),
            "rising_3": rising_3,
            "cluster_1_top": ctx.get("cluster_1_top"),
            "cluster_2_top": ctx.get("cluster_2_top"),
            "cluster_3_top": ctx.get("cluster_3_top"),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "e1r_state": self.e1r_state,
            "e1r_machine": {k.isoformat(): v for k, v in self.e1r_machine.items()},
            "pending": self.pending,
            "reclaim_count": len(self.reclaim_events),
        }

    def restore_machine(self, machine: dict[datetime, str], e1r_state: str, pending: dict | None) -> None:
        self.e1r_machine = dict(machine)
        self.e1r_state = e1r_state
        self.pending = pending

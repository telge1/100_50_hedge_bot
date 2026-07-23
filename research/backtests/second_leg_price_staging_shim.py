"""Backtest-only shim: multi-price SHORT_REDUCE staging without live defaults."""

from __future__ import annotations

from typing import Any

from fixed_cycle_hedge_bot.models import StrategyIntent

from research.backtests.adaptive_distance_staging import (
    adaptive_diagnostics_payload,
    compute_original_distance_pct,
    config_with_adaptive_policy,
    is_adaptive_profile,
    select_adaptive_policy,
    select_distance_bucket,
)
from research.backtests.fixed_step_distance_staging import (
    config_with_fixed_step_plan,
    fixed_step_diagnostics_payload,
    is_fixed_step_profile,
    select_fixed_step_plan,
)
from research.backtests.full_dynamic_second_leg_restaging import (
    ECONOMIC_TOLERANCE_USDT,
    FD_ANCHOR_PRICE,
    FD_COVERED,
    FD_ORIGINAL_FULL_TRIGGER,
    FD_PLAN_REVISION,
    FD_REPLAN_ACTIVE,
    append_replan_event,
    build_residual_stage_plan,
    collect_open_residual_staged_orders,
    init_cycle_economics_state,
    read_canonical_economics,
    recompute_required_qty,
    sync_pending_from_canonical,
)
from research.backtests.second_leg_price_staging import (
    SecondLegPriceStagingConfig,
    StagePlan,
    build_stage_plan,
    dedupe_staged_intents_by_identity,
    legacy_config,
)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _is_short_reduce_purpose(purpose: str) -> bool:
    p = str(purpose or "").upper()
    return "SHORT_REDUCE" in p or p.endswith("_SHORT_TP")


def _extract_cycle_index(purpose: str, meta: dict[str, Any]) -> int:
    if meta.get("cycle_index") is not None:
        try:
            return int(meta.get("cycle_index") or 0)
        except (TypeError, ValueError):
            pass
    import re

    m = re.match(r"^CYCLE_(\d+)_", str(purpose or ""))
    return int(m.group(1)) if m else 0


def _persist_plan_state(
    runtime_state: Any,
    plan: StagePlan,
    *,
    adaptive_diag: dict[str, Any] | None = None,
) -> None:
    state = runtime_state.strategy_state
    cycle_key = str(plan.cycle_index)
    required_total = float(plan.required_net or 0.0)
    if required_total <= 0:
        required_total = float(state.get("pending_cycle_loss_usdt") or 0.0)
    state.setdefault("staged_second_leg_tp_required_net_total", {})[cycle_key] = required_total
    state.setdefault("staged_second_leg_tp_stage_count", {})[cycle_key] = int(plan.stage_count)
    state.setdefault("staged_second_leg_tp_realized_net", {}).setdefault(cycle_key, 0.0)
    state.setdefault("staged_second_leg_tp_filled_stages", {}).setdefault(cycle_key, [])
    # Clear same-price qty-split maps so completion does not wait on split stages.
    split_count = state.get("normal_cycle_second_leg_split_stage_count")
    if isinstance(split_count, dict):
        split_count.pop(cycle_key, None)
    split_filled = state.get("normal_cycle_second_leg_split_filled_stages")
    if isinstance(split_filled, dict):
        split_filled.pop(cycle_key, None)
    payload: dict[str, Any] = {
        "cycle_index": plan.cycle_index,
        "purpose": plan.purpose,
        "first_leg_fill_price": plan.first_leg_fill_price,
        "full_trigger_price": plan.full_trigger_price,
        "total_qty": plan.total_qty,
        "required_net": required_total,
        "stage_count": plan.stage_count,
        "fallback_used": plan.fallback_used,
        "stages": [
            {
                "stage_index": s.stage_index,
                "trigger_price": s.trigger_price,
                "qty": s.qty,
                "expected_net": s.expected_net,
                "notional": s.notional,
                "price_fraction": s.price_fraction,
                "qty_fraction": s.qty_fraction,
            }
            for s in plan.stages
        ],
    }
    if adaptive_diag:
        payload.update({k: v for k, v in adaptive_diag.items() if k != "stage_specs"})
    state["research_second_leg_price_staging_plan"] = payload


def _intents_from_plan(
    *,
    plan: StagePlan,
    template: StrategyIntent,
    strategy: Any,
    adaptive_diag: dict[str, Any] | None = None,
    plan_revision: int = 0,
    full_dynamic: bool = False,
) -> list[StrategyIntent]:
    base_meta = dict(getattr(template, "metadata", None) or {})
    base_meta.pop("normal_cycle_second_leg_split", None)
    base_meta.pop("split_stage_index", None)
    base_meta.pop("split_stage_count", None)
    base_meta.pop("split_stage_qtys", None)
    base_meta.pop("split_total_qty", None)
    base_meta.pop("replace_open_purpose", None)
    intents: list[StrategyIntent] = []
    cfg = getattr(strategy, "_backtest_slps_config", legacy_config())
    meta_revision = int(plan_revision)
    meta_full_dynamic = bool(full_dynamic or getattr(cfg, "full_dynamic", False))
    for stage in plan.stages:
        meta = {
            **base_meta,
            "is_staged_second_leg_tp": True,
            "research_price_staging": True,
            "research_price_staging_profile": cfg.profile_name,
            "stage_index": stage.stage_index,
            "stage_count": plan.stage_count,
            "stage_expected_net": stage.expected_net,
            "required_net": float(plan.required_net or 0.0),
            "stage_required_net_total": float(plan.required_net or 0.0),
            "stage_trigger_price": stage.trigger_price,
            "first_leg_fill_price": plan.first_leg_fill_price,
            "final_second_leg_trigger_price": plan.full_trigger_price,
            "second_leg_cycle_role": "short_reduce",
            "first_leg_cycle_role": "long_add",
            "parent_second_leg_purpose": plan.purpose,
            "cycle_index": plan.cycle_index,
            "price_fraction": stage.price_fraction,
            "qty_fraction": stage.qty_fraction,
            "stage_identity": (
                plan.cycle_index,
                plan.purpose,
                int(plan_revision),
                stage.stage_index,
            ),
            "plan_revision": meta_revision,
            "stage_generation": meta_revision,
            "full_dynamic": meta_full_dynamic,
        }
        if adaptive_diag:
            for key in (
                "original_distance_pct",
                "distance_bucket",
                "theoretical_distance_bucket",
                "distance_status",
                "grid_step_pct",
                "requested_absolute_stage_distances_pct",
                "effective_absolute_stage_distances_pct",
                "requested_price_fractions",
                "effective_price_fractions",
                "requested_stage_count",
                "capped_stage_count",
                "stage_cap_applied",
                "selected_stage_count",
                "selected_price_fractions",
                "selected_qty_fractions",
                "requested_qty_fractions",
                "effective_qty_fractions",
                "effective_stage_count_after_rounding",
                "skipped_small_stages",
                "merged_stage_count",
                "residual_qty",
                "fallback_used",
                "adaptive_family",
                "fixed_step_qty_family",
                "diagnostic_only",
            ):
                if key in adaptive_diag:
                    meta[key] = adaptive_diag[key]
        intents.append(
            StrategyIntent(
                side=getattr(template, "side", "short"),
                qty=float(stage.qty),
                purpose=plan.purpose,
                order_type=getattr(template, "order_type", "Market"),
                reduce_only=True,
                trigger_price=float(stage.trigger_price),
                trigger_direction=getattr(template, "trigger_direction", 2),
                trigger_by=getattr(template, "trigger_by", "LastPrice"),
                close_on_trigger=getattr(template, "close_on_trigger", True),
                position_idx=getattr(template, "position_idx", 2),
                metadata=meta,
            )
        )
    return dedupe_staged_intents_by_identity(
        intents, cycle_index=plan.cycle_index, purpose=plan.purpose
    )


def install_second_leg_price_staging(
    strategy: Any,
    config: SecondLegPriceStagingConfig | None,
) -> None:
    """Install research shim. ``enabled=False`` / None ⇒ no wrap (exact legacy path)."""
    cfg = config or legacy_config()
    strategy._backtest_slps_config = cfg
    strategy._backtest_slps_plans = []
    if not cfg.enabled:
        # Critical: do not wrap — bit-identical builder path.
        return
    if getattr(strategy, "_backtest_slps_shim_installed", False):
        return

    original = strategy._build_short_tp_follow_up

    def _wrapped(snapshot: Any, runtime_state: Any, context: Any = None) -> list[StrategyIntent]:
        intents = list(original(snapshot, runtime_state, context) or [])
        active_cfg: SecondLegPriceStagingConfig = getattr(
            strategy, "_backtest_slps_config", legacy_config()
        )
        if not active_cfg.enabled:
            return intents
        if "long_primary_short_reduce" not in active_cfg.apply_to:
            return intents
        # Only long-primary strategies for this research prototype.
        side = str(getattr(getattr(strategy, "config", None), "side", "long") or "long").lower()
        if side != "long":
            return intents

        short_reduce = [
            i
            for i in intents
            if _is_short_reduce_purpose(str(getattr(i, "purpose", "") or ""))
        ]
        if not short_reduce:
            return intents

        template = short_reduce[0]
        meta = dict(getattr(template, "metadata", None) or {})
        purpose = str(getattr(template, "purpose", "") or "")
        cycle_index = _extract_cycle_index(purpose, meta)

        # FULL_DYNAMIC: after the first replan, residuals are owned by on_fill restaging.
        # Do not recreate a fresh multi-stage plan during structure rebuild.
        if bool(getattr(active_cfg, "full_dynamic", False)):
            state = runtime_state.strategy_state
            ck = str(cycle_index)
            rev = int((state.get(FD_PLAN_REVISION) or {}).get(ck) or 0)
            covered = bool((state.get(FD_COVERED) or {}).get(ck))
            if covered or rev > 0:
                return [
                    i
                    for i in intents
                    if not _is_short_reduce_purpose(str(getattr(i, "purpose", "") or ""))
                ]

        if active_cfg.only_cycles is not None and cycle_index not in active_cfg.only_cycles:
            return intents
        total_qty = sum(_safe_float(getattr(i, "qty", 0.0)) for i in short_reduce)
        # Full coverage trigger = deepest (lowest) among returned intents.
        triggers = [
            _safe_float(getattr(i, "trigger_price", 0.0))
            for i in short_reduce
            if _safe_float(getattr(i, "trigger_price", 0.0)) > 0
        ]
        full_trigger = min(triggers) if triggers else 0.0
        first_leg = _safe_float(meta.get("first_leg_fill_price"))
        if first_leg <= 0 and hasattr(strategy, "_get_first_leg_fill_price"):
            try:
                first_leg = _safe_float(
                    strategy._get_first_leg_fill_price(runtime_state, cycle_index)
                )
            except Exception:
                first_leg = 0.0
        required_net = _safe_float(
            meta.get("required_net")
            or meta.get("stage_required_net_total")
            or runtime_state.strategy_state.get("pending_cycle_loss_usdt")
        )
        # Prefer effective pending (includes relief carry) when strategy exposes it.
        get_effective = getattr(strategy, "_get_effective_pending_cycle_loss_usdt", None)
        if callable(get_effective):
            try:
                effective = _safe_float(get_effective(runtime_state))
                if effective > 0:
                    required_net = max(required_net, effective)
            except Exception:
                pass
        if required_net <= 0:
            # Fall back to absolute first-leg loss metadata when present.
            for key in ("first_leg_loss_usdt", "pending_loss_usdt", "loss_usdt"):
                candidate = _safe_float(meta.get(key))
                if candidate > 0:
                    required_net = candidate
                    break
        short_entry = _safe_float(getattr(snapshot, "short_avg", 0.0))
        fee_rate = 0.00055
        try:
            pct = _safe_float(getattr(strategy.config, "order_fee_rate_pct", 0.055), 0.055)
            if pct > 0:
                fee_rate = pct / 100.0 if pct > 0.01 else pct
        except Exception:
            fee_rate = 0.00055

        price_tick = _safe_float(getattr(strategy.config, "price_tick_size", 0.0), 0.0001)
        qty_step = _safe_float(getattr(strategy.config, "qty_step", 0.0), 0.01)
        min_order_qty = _safe_float(getattr(strategy.config, "min_order_qty", 0.0), 0.01)
        try:
            _, rules, _ = strategy._resolve_instrument_rules(runtime_state)
            if rules:
                price_tick = _safe_float(rules.get("tick_size"), price_tick) or price_tick
                qty_step = _safe_float(rules.get("qty_step"), qty_step) or qty_step
                min_order_qty = _safe_float(rules.get("min_order_qty"), min_order_qty) or min_order_qty
        except Exception:
            pass

        plan_cfg = active_cfg
        adaptive_diag: dict[str, Any] | None = None
        from research.backtests.full_dynamic_second_leg_restaging import base_profile_name

        planner_profile = base_profile_name(active_cfg.profile_name)
        use_adaptive = bool(getattr(active_cfg, "adaptive", False)) or is_adaptive_profile(
            planner_profile
        )
        use_fixed_step = bool(getattr(active_cfg, "fixed_step", False)) or is_fixed_step_profile(
            planner_profile
        )
        distance_pct = compute_original_distance_pct(first_leg, full_trigger)
        bucket = select_distance_bucket(distance_pct)
        if use_fixed_step:
            fs_plan = select_fixed_step_plan(planner_profile, distance_pct)
            if fs_plan is None or fs_plan.stage_count <= 1:
                plans = getattr(strategy, "_backtest_slps_plans", None)
                if plans is None:
                    strategy._backtest_slps_plans = []
                    plans = strategy._backtest_slps_plans
                plans.append(
                    {
                        "accepted": False,
                        "rejection_reason": "fixed_step_single_or_invalid",
                        "fallback_used": "legacy_intents",
                        "cycle_index": cycle_index,
                        "purpose": purpose,
                        "stage_count": 0,
                        "first_leg_fill_price": first_leg,
                        "full_trigger_price": full_trigger,
                        "total_qty": total_qty,
                        "required_net": required_net,
                        "stages": [],
                        **fixed_step_diagnostics_payload(
                            plan=fs_plan,
                            distance_pct=distance_pct,
                            bucket=bucket,
                            plan_accepted=False,
                            plan_stage_count=0,
                            fallback_used="legacy_intents",
                            residual_qty=None,
                            first_leg_fill=first_leg,
                            profile_name=active_cfg.profile_name,
                        ),
                    }
                )
                return intents
            plan_cfg = config_with_fixed_step_plan(active_cfg, fs_plan)
        elif use_adaptive:
            policy = select_adaptive_policy(planner_profile, distance_pct)
            if policy is None:
                plans = getattr(strategy, "_backtest_slps_plans", None)
                if plans is None:
                    strategy._backtest_slps_plans = []
                    plans = strategy._backtest_slps_plans
                plans.append(
                    {
                        "accepted": False,
                        "rejection_reason": f"adaptive_bucket:{bucket.value}",
                        "fallback_used": "legacy_intents",
                        "cycle_index": cycle_index,
                        "purpose": purpose,
                        "stage_count": 0,
                        "first_leg_fill_price": first_leg,
                        "full_trigger_price": full_trigger,
                        "total_qty": total_qty,
                        "required_net": required_net,
                        "stages": [],
                        **adaptive_diagnostics_payload(
                            policy=None,
                            distance_pct=distance_pct,
                            bucket=bucket,
                            plan_accepted=False,
                            plan_stage_count=0,
                            fallback_used="legacy_intents",
                            residual_qty=None,
                            profile_name=active_cfg.profile_name,
                        ),
                    }
                )
                return intents
            plan_cfg = config_with_adaptive_policy(active_cfg, policy)

        plan = build_stage_plan(
            config=plan_cfg,
            cycle_index=cycle_index,
            purpose=purpose,
            first_leg_fill_price=first_leg,
            full_trigger_price=full_trigger,
            total_qty=total_qty,
            required_net=required_net,
            short_entry_price=short_entry,
            fee_rate=fee_rate,
            price_tick=price_tick,
            qty_step=qty_step,
            min_order_qty=min_order_qty,
            direction="long_primary_short_reduce",
        )

        residual = float(plan.stages[-1].qty) if plan.accepted and plan.stages else None
        if use_fixed_step:
            fs_plan = select_fixed_step_plan(planner_profile, distance_pct)
            adaptive_diag = fixed_step_diagnostics_payload(
                plan=fs_plan,
                distance_pct=distance_pct,
                bucket=bucket,
                plan_accepted=bool(plan.accepted),
                plan_stage_count=int(plan.stage_count),
                fallback_used=plan.fallback_used,
                residual_qty=residual,
                stages=plan.stages,
                first_leg_fill=first_leg,
                profile_name=active_cfg.profile_name,
            )
        elif use_adaptive:
            policy = select_adaptive_policy(planner_profile, distance_pct)
            adaptive_diag = adaptive_diagnostics_payload(
                policy=policy,
                distance_pct=distance_pct,
                bucket=bucket,
                plan_accepted=bool(plan.accepted),
                plan_stage_count=int(plan.stage_count),
                fallback_used=plan.fallback_used,
                residual_qty=residual,
                stages=plan.stages,
                profile_name=active_cfg.profile_name,
            )
        else:
            # Fixed profiles (TEM): diagnostic distance only — policy unchanged.
            adaptive_diag = adaptive_diagnostics_payload(
                policy=None,
                distance_pct=distance_pct,
                bucket=bucket,
                plan_accepted=bool(plan.accepted),
                plan_stage_count=int(plan.stage_count),
                fallback_used=plan.fallback_used,
                residual_qty=residual,
                stages=plan.stages,
                diagnostic_only=True,
                profile_name=active_cfg.profile_name,
            )

        plans = getattr(strategy, "_backtest_slps_plans", None)
        if plans is None:
            strategy._backtest_slps_plans = []
            plans = strategy._backtest_slps_plans
        plan_row: dict[str, Any] = {
            "accepted": plan.accepted,
            "rejection_reason": plan.rejection_reason,
            "fallback_used": plan.fallback_used,
            "cycle_index": plan.cycle_index,
            "purpose": plan.purpose,
            "stage_count": plan.stage_count,
            "first_leg_fill_price": plan.first_leg_fill_price,
            "full_trigger_price": plan.full_trigger_price,
            "total_qty": plan.total_qty,
            "required_net": plan.required_net,
            "stages": [
                {
                    "stage_index": s.stage_index,
                    "trigger_price": s.trigger_price,
                    "qty": s.qty,
                    "expected_net": s.expected_net,
                    "notional": s.notional,
                }
                for s in plan.stages
            ],
        }
        if adaptive_diag:
            plan_row.update({k: v for k, v in adaptive_diag.items() if k != "stage_specs"})
        plans.append(plan_row)
        if not plan.accepted or not plan.stages:
            return intents
        # Single-stage fallback ≡ legacy economics; keep original intents for parity.
        if plan.stage_count <= 1:
            return intents

        _persist_plan_state(runtime_state, plan, adaptive_diag=adaptive_diag)
        fd_enabled = bool(getattr(active_cfg, "full_dynamic", False))
        if fd_enabled:
            target_profit = _safe_float(getattr(strategy.config, "target_profit_usdt", 0.0))
            initial_pending = max(float(required_net) - target_profit, 0.0)
            # Prefer live pending if it matches economics more closely.
            live_pending = _safe_float(runtime_state.strategy_state.get("pending_cycle_loss_usdt"))
            if live_pending > 0:
                initial_pending = live_pending
                required_net_total = initial_pending + target_profit
            else:
                required_net_total = float(required_net)
            init_cycle_economics_state(
                runtime_state,
                cycle_index=cycle_index,
                required_net_total=required_net_total,
                initial_pending=initial_pending,
                target_profit=target_profit,
                full_trigger=full_trigger,
            )
            # Keep staged required map as canonical required_net_total.
            runtime_state.strategy_state.setdefault(
                "staged_second_leg_tp_required_net_total", {}
            )[str(cycle_index)] = float(required_net_total)
        staged = _intents_from_plan(
            plan=plan,
            template=template,
            strategy=strategy,
            adaptive_diag=adaptive_diag,
            plan_revision=0,
            full_dynamic=fd_enabled,
        )
        others = [
            i
            for i in intents
            if not _is_short_reduce_purpose(str(getattr(i, "purpose", "") or ""))
        ]
        return others + staged

    strategy._build_short_tp_follow_up = _wrapped  # type: ignore[method-assign]

    # FULL_DYNAMIC: wrap on_fill to replan residuals after each confirmed stage fill.
    if bool(getattr(cfg, "full_dynamic", False)):
        _install_full_dynamic_on_fill(strategy)
        _install_full_dynamic_coverage_guards(strategy)

    strategy._backtest_slps_shim_installed = True


def _fd_has_uncovered_remaining(runtime_state: Any) -> bool:
    """True when FULL_DYNAMIC still has remaining_required_net above tolerance."""
    state = getattr(runtime_state, "strategy_state", None) or {}
    req_map = state.get("research_fd_required_net_total") or state.get(
        "staged_second_leg_tp_required_net_total"
    ) or {}
    realized_map = state.get("staged_second_leg_tp_realized_net") or {}
    covered_map = state.get(FD_COVERED) or {}
    for ck, req in req_map.items():
        if bool(covered_map.get(ck)):
            continue
        remaining = max(_safe_float(req) - _safe_float(realized_map.get(ck)), 0.0)
        if remaining > ECONOMIC_TOLERANCE_USDT:
            return True
    pending = _safe_float(state.get("pending_cycle_loss_usdt"))
    return pending > ECONOMIC_TOLERANCE_USDT


def _install_full_dynamic_coverage_guards(strategy: Any) -> None:
    """Research-only: atomic replan + never skip FinalExitEconomics while FD uncovered."""
    if getattr(strategy, "_backtest_fd_coverage_guards_installed", False):
        return

    original_eval = strategy.evaluate_basket_exit_coverage
    original_build_exits = strategy._build_exit_intents

    def _eval_wrapped(
        *,
        snapshot: Any,
        runtime_state: Any,
        long_tp_price: float,
        short_sl_price: float,
        projection: Any = None,
    ):
        state = runtime_state.strategy_state
        if bool(state.get(FD_REPLAN_ACTIVE)):
            from fixed_cycle_hedge_bot.fixed_cycle_strategy import (
                BasketExitCoverageDecision,
                FinalExitEconomics,
            )

            dummy = FinalExitEconomics(
                expected_total_net_after_exit=0.0,
                target_delta_usdt=-1.0,
                required_profit_to_cover_loss=_safe_float(state.get("pending_cycle_loss_usdt")),
                min_profit_target_usdt=0.0,
                min_required_total_usdt=_safe_float(state.get("pending_cycle_loss_usdt")),
                sufficient=False,
            )
            return BasketExitCoverageDecision(
                required_net_usdt=float(dummy.min_required_total_usdt),
                realized_net_usdt=0.0,
                expected_basket_net_usdt=0.0,
                remaining_required_usdt=float(dummy.min_required_total_usdt),
                coverage_after_exit_usdt=0.0,
                coverage_ok=False,
                tolerance_usdt=0.0,
                reason_code="coverage_blocked_fd_replan_in_progress",
                staging_incomplete=True,
                pending_cycle_loss_usdt=float(dummy.required_profit_to_cover_loss),
                economics=dummy,
            )

        decision = original_eval(
            snapshot=snapshot,
            runtime_state=runtime_state,
            long_tp_price=long_tp_price,
            short_sl_price=short_sl_price,
            projection=projection,
        )
        # After residual cancel, open staged orders may briefly be empty so the
        # base gate reports coverage_skipped_not_staged. While FD remaining is
        # still uncovered, force FinalExitEconomics.sufficient as the decision.
        if (
            str(getattr(decision, "reason_code", "") or "") == "coverage_skipped_not_staged"
            and _fd_has_uncovered_remaining(runtime_state)
        ):
            econ = getattr(decision, "economics", None)
            sufficient = bool(getattr(econ, "sufficient", False)) if econ is not None else False
            if not sufficient:
                return type(decision)(
                    required_net_usdt=float(decision.required_net_usdt),
                    realized_net_usdt=float(decision.realized_net_usdt),
                    expected_basket_net_usdt=float(decision.expected_basket_net_usdt),
                    remaining_required_usdt=float(decision.remaining_required_usdt),
                    coverage_after_exit_usdt=float(decision.coverage_after_exit_usdt),
                    coverage_ok=False,
                    tolerance_usdt=float(decision.tolerance_usdt),
                    reason_code="coverage_blocked_insufficient_basket",
                    staging_incomplete=True,
                    pending_cycle_loss_usdt=float(decision.pending_cycle_loss_usdt),
                    economics=econ,
                )
            return type(decision)(
                required_net_usdt=float(decision.required_net_usdt),
                realized_net_usdt=float(decision.realized_net_usdt),
                expected_basket_net_usdt=float(decision.expected_basket_net_usdt),
                remaining_required_usdt=float(decision.remaining_required_usdt),
                coverage_after_exit_usdt=float(decision.coverage_after_exit_usdt),
                coverage_ok=True,
                tolerance_usdt=float(decision.tolerance_usdt),
                reason_code="coverage_ok_basket_compensates_partial_stages",
                staging_incomplete=True,
                pending_cycle_loss_usdt=float(decision.pending_cycle_loss_usdt),
                economics=econ,
            )
        return decision

    def _build_exits_wrapped(*args: Any, **kwargs: Any):
        runtime_state = None
        if len(args) >= 2:
            runtime_state = args[1]
        if runtime_state is None:
            runtime_state = kwargs.get("runtime_state")
        state = getattr(runtime_state, "strategy_state", None) if runtime_state else None
        if isinstance(state, dict) and bool(state.get(FD_REPLAN_ACTIVE)):
            return []
        return original_build_exits(*args, **kwargs)

    strategy.evaluate_basket_exit_coverage = _eval_wrapped  # type: ignore[method-assign]
    strategy._build_exit_intents = _build_exits_wrapped  # type: ignore[method-assign]
    strategy._backtest_fd_coverage_guards_installed = True


def _install_full_dynamic_on_fill(strategy: Any) -> None:
    if getattr(strategy, "_backtest_fd_on_fill_installed", False):
        return
    original_on_fill = strategy.on_fill

    def _on_fill_wrapped(fill_event: Any, snapshot: Any, runtime_state: Any, context: Any = None):
        meta = dict(getattr(fill_event, "metadata", None) or {})
        purpose = str(getattr(fill_event, "purpose", "") or "")
        is_staged = bool(meta.get("is_staged_second_leg_tp") or meta.get("research_price_staging"))
        is_sr = _is_short_reduce_purpose(purpose)
        cycle_index = _extract_cycle_index(purpose, meta)
        state = runtime_state.strategy_state
        fill_rev = int(meta.get("plan_revision") or meta.get("stage_generation") or 0)
        current_rev = int((state.get(FD_PLAN_REVISION) or {}).get(str(cycle_index)) or 0)

        stale = bool(is_staged and is_sr and fill_rev < current_rev)
        if stale:
            state["research_fd_stale_generation_fills"] = (
                int(state.get("research_fd_stale_generation_fills") or 0) + 1
            )

        economics_before = None
        pending_before = float(state.get("pending_cycle_loss_usdt") or 0.0)
        if is_staged and is_sr and not stale:
            economics_before = read_canonical_economics(runtime_state, cycle_index)

        intents = list(original_on_fill(fill_event, snapshot, runtime_state, context) or [])
        cfg: SecondLegPriceStagingConfig = getattr(
            strategy, "_backtest_slps_config", legacy_config()
        )
        if not bool(getattr(cfg, "full_dynamic", False)):
            return intents
        if not (is_staged and is_sr and str(getattr(fill_event, "status", "") or "").upper() == "FILLED"):
            return intents
        if stale:
            return intents
        if bool((state.get(FD_COVERED) or {}).get(str(cycle_index))):
            return intents

        return intents + _full_dynamic_replan_after_fill(
            strategy=strategy,
            fill_event=fill_event,
            snapshot=snapshot,
            runtime_state=runtime_state,
            context=context,
            purpose=purpose,
            cycle_index=cycle_index,
            meta=meta,
            economics_before=economics_before,
            pending_before=pending_before,
        )

    strategy.on_fill = _on_fill_wrapped  # type: ignore[method-assign]
    strategy._backtest_fd_on_fill_installed = True


def _full_dynamic_replan_after_fill(
    *,
    strategy: Any,
    fill_event: Any,
    snapshot: Any,
    runtime_state: Any,
    context: Any,
    purpose: str,
    cycle_index: int,
    meta: dict[str, Any],
    economics_before: Any = None,
    pending_before: float = 0.0,
) -> list[StrategyIntent]:
    state = runtime_state.strategy_state
    cfg: SecondLegPriceStagingConfig = getattr(
        strategy, "_backtest_slps_config", legacy_config()
    )
    ck = str(cycle_index)
    if economics_before is None:
        # Bootstrap if missing (should be rare).
        required_total = _safe_float(
            meta.get("stage_required_net_total") or meta.get("required_net")
        )
        target_profit = _safe_float(getattr(strategy.config, "target_profit_usdt", 0.0))
        initial_pending = max(required_total - target_profit, 0.0)
        init_cycle_economics_state(
            runtime_state,
            cycle_index=cycle_index,
            required_net_total=required_total,
            initial_pending=initial_pending,
            target_profit=target_profit,
            full_trigger=_safe_float(
                meta.get("final_second_leg_trigger_price") or meta.get("trigger_price")
            ),
        )
        # Reconstruct pre-fill economics from post-fill realized - confirmed_net.
        economics = read_canonical_economics(runtime_state, cycle_index)
        confirmed_net_boot = _safe_float(
            meta.get("closed_pnl")
            or meta.get("confirmed_closed_pnl")
            or getattr(fill_event, "confirmed_pnl", None)
        )
        if economics is not None:
            from research.backtests.full_dynamic_second_leg_restaging import (
                compute_canonical_economics,
            )

            economics_before = compute_canonical_economics(
                required_net_total=economics.required_net_total,
                confirmed_stage_realized_net=max(
                    economics.confirmed_stage_realized_net - confirmed_net_boot, 0.0
                ),
                initial_pending_cycle_loss_usdt=economics.initial_pending_cycle_loss_usdt,
                target_profit_usdt=economics.target_profit_usdt,
            )
    if economics_before is None:
        return []

    # Realized map already updated by strategy.on_fill staged block.
    economics = read_canonical_economics(runtime_state, cycle_index)
    if economics is None:
        return []
    sync_pending_from_canonical(runtime_state, economics)
    pending_after = float(state.get("pending_cycle_loss_usdt") or 0.0)

    residuals = collect_open_residual_staged_orders(
        snapshot, runtime_state, cycle_index=cycle_index, purpose=purpose
    )
    old_ids = []
    old_prices = []
    old_qtys = []
    seen_oids: set[str] = set()
    for o in residuals:
        oid = str(getattr(o, "order_id", None) or getattr(o, "client_order_id", None) or "")
        if oid and oid in seen_oids:
            continue
        if oid:
            seen_oids.add(oid)
        old_ids.append(oid)
        md = getattr(o, "metadata", None) or {}
        if not isinstance(md, dict):
            md = {}
        px = _safe_float(
            getattr(o, "trigger_price", None)
            or getattr(o, "price", None)
            or md.get("stage_trigger_price")
            or md.get("trigger_price")
        )
        old_prices.append(px)
        old_qtys.append(_safe_float(getattr(o, "qty", None) or getattr(o, "remaining_qty", None)))
    prior_remaining_qty = sum(old_qtys)
    from research.backtests.full_dynamic_second_leg_restaging import _iter_runtime_active_orders

    old_basket = []
    for o in _iter_runtime_active_orders(runtime_state):
        pur = str(getattr(o, "purpose", "") or "")
        if pur in {"LONG_TP_EXIT", "SHORT_SL_EXIT"}:
            old_basket.append(
                {
                    "purpose": pur,
                    "trigger": _safe_float(
                        getattr(o, "trigger_price", None) or getattr(o, "price", None)
                    ),
                    "qty": _safe_float(getattr(o, "qty", None)),
                }
            )

    fill_price = _safe_float(getattr(fill_event, "exec_price", None))
    fill_qty = _safe_float(getattr(fill_event, "exec_qty", None))
    confirmed_net = _safe_float(
        meta.get("closed_pnl")
        or meta.get("confirmed_closed_pnl")
        or getattr(fill_event, "confirmed_pnl", None)
    )
    realized_after = economics.confirmed_stage_realized_net
    realized_before = float(economics_before.confirmed_stage_realized_net)

    state[FD_REPLAN_ACTIVE] = True
    canceled_ids: list[str] = []
    try:
        cancel_fn = getattr(context, "cancel_open_orders_by_purpose", None) if context else None
        if callable(cancel_fn):
            cancel_fn([purpose])
            canceled_ids = [x for x in old_ids if x]
        else:
            for o in residuals:
                oid = getattr(o, "order_id", None) or getattr(o, "client_order_id", None)
                if oid and hasattr(runtime_state, "active_orders"):
                    runtime_state.active_orders.pop(str(oid), None)
                    canceled_ids.append(str(oid))

        state.setdefault(FD_ANCHOR_PRICE, {})[ck] = fill_price
        revision = int((state.get(FD_PLAN_REVISION) or {}).get(ck) or 0) + 1
        state.setdefault(FD_PLAN_REVISION, {})[ck] = revision

        candle_index = state.get("_backtest_candle_index")
        if candle_index is None:
            candle_index = getattr(runtime_state, "candle_index", None)
        if candle_index is None and context is not None:
            candle_index = getattr(context, "candle_index", None)
        eligible_from = int(candle_index) + 1 if candle_index is not None else None

        out: list[StrategyIntent] = []
        cycle_completed = False
        fallback_reason = None
        new_plan = None
        new_full_trigger = _safe_float(
            (state.get(FD_ORIGINAL_FULL_TRIGGER) or {}).get(ck)
            or meta.get("final_second_leg_trigger_price")
        )
        recomputed_qty = 0.0
        rounding_residual = 0.0
        min_notional_fallback = False
        coverage_sufficient = False
        expected_total = None

        if economics.remaining_required_net <= ECONOMIC_TOLERANCE_USDT:
            # Remaining stage requirement is zero, but basket flat still needs
            # FinalExitEconomics.sufficient — never complete on residual math alone.
            fee_sufficient = False
            try:
                be, _ = strategy._calculate_break_even(snapshot, runtime_state)
                proj = strategy._calculate_tp_projection(be, snapshot, runtime_state)
                tp = float(getattr(proj, "tp_price", 0.0) or 0.0)
                fee_econ = strategy._evaluate_final_exit_economics(
                    long_tp_price=tp,
                    short_sl_price=tp,
                    snapshot=snapshot,
                    runtime_state=runtime_state,
                    projection=proj,
                )
                fee_sufficient = bool(getattr(fee_econ, "sufficient", False))
                expected_total = float(
                    getattr(fee_econ, "expected_total_net_after_exit", 0.0) or 0.0
                )
                coverage_sufficient = fee_sufficient
            except Exception:
                fee_sufficient = False
            if fee_sufficient:
                state.setdefault(FD_COVERED, {})[ck] = True
                state["cycle_short_tp_filled"] = True
                cycle_completed = True
                fallback_reason = "economic_coverage_complete"
            else:
                fallback_reason = "remaining_zero_but_final_exit_insufficient"
                min_notional_fallback = True
        elif not cycle_completed:
            short_entry = _safe_float(getattr(snapshot, "short_avg", None))
            if short_entry <= 0:
                short_entry = _safe_float(meta.get("short_entry_price"))
            actual_short = _safe_float(getattr(snapshot, "short_qty", None))
            prior_full = new_full_trigger if new_full_trigger > 0 else fill_price * 0.9
            fee_rate = 0.00055
            try:
                pct = _safe_float(getattr(strategy.config, "order_fee_rate_pct", 0.055), 0.055)
                if pct > 0:
                    fee_rate = pct / 100.0 if pct > 0.01 else pct
            except Exception:
                fee_rate = 0.00055
            price_tick = _safe_float(getattr(strategy.config, "price_tick_size", 0.0), 0.0001)
            qty_step = _safe_float(getattr(strategy.config, "qty_step", 0.0), 0.01)
            min_order_qty = _safe_float(getattr(strategy.config, "min_order_qty", 0.0), 0.01)
            try:
                _, rules, _ = strategy._resolve_instrument_rules(runtime_state)
                if rules:
                    price_tick = _safe_float(rules.get("tick_size"), price_tick) or price_tick
                    qty_step = _safe_float(rules.get("qty_step"), qty_step) or qty_step
                    min_order_qty = (
                        _safe_float(rules.get("min_order_qty"), min_order_qty) or min_order_qty
                    )
            except Exception:
                pass

            prior_qty_cap = prior_remaining_qty if prior_remaining_qty > 0 else actual_short
            recomputed_qty, rounding_residual = recompute_required_qty(
                remaining_required_net=economics.remaining_required_net,
                short_entry=short_entry,
                full_trigger=prior_full,
                fee_rate=fee_rate,
                actual_short_qty=actual_short,
                prior_remaining_stage_qty=prior_qty_cap if prior_qty_cap > 0 else actual_short,
                qty_step=qty_step,
            )
            new_plan, new_full_trigger, fallback_reason = build_residual_stage_plan(
                config=cfg,
                cycle_index=cycle_index,
                purpose=purpose,
                anchor_price=fill_price,
                remaining_required_net=economics.remaining_required_net,
                remaining_qty=recomputed_qty,
                short_entry=short_entry,
                fee_rate=fee_rate,
                price_tick=price_tick,
                qty_step=qty_step,
                min_order_qty=min_order_qty,
                prior_full_trigger=prior_full,
            )
            if new_plan is None or not new_plan.stages:
                min_notional_fallback = True
                fallback_reason = fallback_reason or "no_residual_plan"
            else:
                if fallback_reason in {"collapsed_single_stage", "filtered_deeper_only"}:
                    min_notional_fallback = True
                template = StrategyIntent(
                    side="short",
                    qty=float(new_plan.total_qty),
                    purpose=purpose,
                    order_type="Market",
                    reduce_only=True,
                    trigger_price=float(new_plan.full_trigger_price),
                    trigger_direction=2,
                    trigger_by="LastPrice",
                    close_on_trigger=True,
                    position_idx=2,
                    metadata=dict(meta),
                )
                staged = _intents_from_plan(
                    plan=new_plan,
                    template=template,
                    strategy=strategy,
                    plan_revision=revision,
                    full_dynamic=True,
                )
                if staged:
                    staged[0].metadata["replace_open_purpose"] = purpose
                    if eligible_from is not None:
                        for intent in staged:
                            intent.metadata["eligible_from_candle_index"] = eligible_from
                            intent.metadata["created_candle_index"] = (
                                int(candle_index) if candle_index is not None else None
                            )
                out.extend(staged)
                state["research_second_leg_price_staging_plan"] = {
                    "cycle_index": cycle_index,
                    "purpose": purpose,
                    "plan_revision": revision,
                    "first_leg_fill_price": fill_price,
                    "full_trigger_price": new_full_trigger,
                    "total_qty": new_plan.total_qty,
                    "required_net": economics.remaining_required_net,
                    "stage_count": new_plan.stage_count,
                    "stages": [
                        {
                            "stage_index": s.stage_index,
                            "trigger_price": s.trigger_price,
                            "qty": s.qty,
                            "expected_net": s.expected_net,
                        }
                        for s in new_plan.stages
                    ],
                    "full_dynamic": True,
                }
                state.setdefault("staged_second_leg_tp_stage_count", {})[ck] = int(
                    new_plan.stage_count
                )

        state["force_exit_rebuild"] = True
        state["exit_rebuild_allowed"] = True
        state["exit_locked"] = False
        state["last_exit_signature"] = None
        try:
            strategy._force_exit_rebuild_after_cycle_fill(runtime_state, fill_event)
        except Exception:
            state["force_exit_rebuild"] = True
        try:
            rebuild = list(
                strategy._rebuild_structure(
                    snapshot, runtime_state, context, reason="full_dynamic_replan"
                )
                or []
            )
            out.extend(rebuild)
        except Exception:
            rebuild = []

        new_basket = [
            {
                "purpose": getattr(i, "purpose", None),
                "trigger": getattr(i, "trigger_price", None),
                "qty": getattr(i, "qty", None),
            }
            for i in out
            if getattr(i, "purpose", None) in {"LONG_TP_EXIT", "SHORT_SL_EXIT"}
        ]
        try:
            be, _ = strategy._calculate_break_even(snapshot, runtime_state)
            proj = strategy._calculate_tp_projection(be, snapshot, runtime_state)
            expected_total = float(getattr(proj, "expected_total_net_after_exit", 0.0) or 0.0)
            allow = getattr(strategy, "allow_cancel_residual_staged_second_leg_orders", None)
            if callable(allow):
                decision = allow(snapshot, runtime_state)
                coverage_sufficient = bool(getattr(decision, "coverage_ok", False))
        except Exception:
            pass

        event = {
            "profile": cfg.profile_name,
            "cycle_index": cycle_index,
            "plan_revision": int((state.get(FD_PLAN_REVISION) or {}).get(ck) or 0),
            "candle_index": candle_index,
            "fill_purpose": purpose,
            "fill_price": fill_price,
            "fill_qty": fill_qty,
            "confirmed_fill_net": confirmed_net,
            "required_net_total_before": economics_before.required_net_total,
            "realized_net_before": realized_before,
            "realized_net_after": realized_after,
            "remaining_required_before": economics_before.remaining_required_net,
            "remaining_required_after": economics.remaining_required_net,
            "pending_cycle_loss_before": pending_before,
            "pending_cycle_loss_after": pending_after,
            "old_residual_order_ids": old_ids,
            "old_residual_prices": old_prices,
            "old_residual_qtys": old_qtys,
            "canceled_residual_order_ids": canceled_ids,
            "recomputed_required_qty": recomputed_qty,
            "rounding_residual_qty": rounding_residual,
            "min_notional_fallback": min_notional_fallback,
            "new_stage_count": int(new_plan.stage_count) if new_plan else 0,
            "new_stage_prices": (
                [float(s.trigger_price) for s in new_plan.stages] if new_plan else []
            ),
            "new_stage_qtys": ([float(s.qty) for s in new_plan.stages] if new_plan else []),
            "new_stage_eligible_from_candle": eligible_from,
            "old_basket_exit_prices": old_basket,
            "new_basket_exit_prices": new_basket,
            "expected_total_net_after_exit": expected_total,
            "coverage_sufficient": coverage_sufficient,
            "cycle_completed": cycle_completed,
            "fallback_reason": fallback_reason,
            "actual_short_qty": _safe_float(getattr(snapshot, "short_qty", None)),
            "prior_remaining_stage_qty": prior_remaining_qty,
            "submitted_remaining_stage_qty": (
                float(new_plan.total_qty) if new_plan else 0.0
            ),
        }
        append_replan_event(runtime_state, event)
        return out
    finally:
        state[FD_REPLAN_ACTIVE] = False

"""Variant set execution and ranking."""

from __future__ import annotations

from typing import Any, Protocol

from research.regime_scanner.research_runs.baseline_runner import run_baseline_research
from research.regime_scanner.research_runs.compare import compare_runs
from research.regime_scanner.research_runs.parameters import (
    apply_parameter_overrides,
    assert_baseline_parameter_hash,
    build_baseline_parameter_set,
    parameter_hash,
)
from research.regime_scanner.research_variants.model import (
    ResearchVariant,
    ResearchVariantSet,
    variant_hash,
    variant_set_hash,
    variant_set_json,
)
from research.regime_scanner.research_variants.report import write_variant_set_report
from research.regime_scanner.research_variants.schema import VARIANT_STATUS_COMPLETED, VARIANT_STATUS_FAILED
from research.regime_scanner.research_variants.stability import (
    compute_stability_metrics,
    stability_metrics_to_run_metrics,
)
from research.regime_scanner.research_variants.store_memory import InMemoryVariantStore

BASELINE_REFERENCE_RUN_ID = "64534bb1-3be8-4050-8f10-7fda99fc0de1"


class ResearchRunStore(Protocol):
    def load_trend_states(self, run_id: str) -> list[dict[str, Any]]: ...
    def load_structure_events(self, run_id: str) -> list[dict[str, Any]]: ...
    def get_run(self, run_id: str) -> dict[str, Any] | None: ...
    def count_candles(self) -> int: ...
    def count_validation_runs(self) -> int: ...


class VariantStore(Protocol):
    def ensure_variant_set(
        self, *, variant_set_hash: str, name: str, description: str, variants_json: str
    ) -> int: ...
    def upsert_variant_run(self, **kwargs: Any) -> None: ...
    def list_variant_runs(self, variant_set_id: int) -> list[dict[str, Any]]: ...
    def update_rankings(self, variant_set_id: int, rankings: list[tuple[str, int]]) -> None: ...
    def append_run_metrics(self, run_id: str, metrics: list[dict[str, Any]]) -> None: ...


def build_variant_parameters(
    variant: ResearchVariant,
    *,
    exchange: str,
    symbol: str,
    data_source: str,
) -> Any:
    base = build_baseline_parameter_set(
        exchange=exchange,
        symbol=symbol,
        data_source=data_source,
    )
    if not variant.parameter_overrides:
        assert_baseline_parameter_hash(base)
        return base
    return apply_parameter_overrides(base, variant.parameter_overrides)


def compute_baseline_deltas(
    baseline_metrics: dict[str, Any],
    variant_metrics: dict[str, Any],
) -> dict[str, float | None]:
    keys = [
        "state_change_count",
        "short_state_run_count",
        "transition_share",
        "trend_structure_conflict_count",
        "average_state_duration_bars",
        "detected_turn_count",
        "avg_bars_choch_to_new_trend",
        "score",
    ]
    deltas: dict[str, float | None] = {}
    for key in keys:
        a = baseline_metrics.get(key)
        b = variant_metrics.get(key)
        if a is None or b is None:
            deltas[f"delta_{key}"] = None
        else:
            deltas[f"delta_{key}"] = float(b) - float(a)
    return deltas


def rank_variants(rows: list[dict[str, Any]]) -> list[tuple[str, int]]:
    sortable = []
    for row in rows:
        metrics = row.get("stability_metrics") or {}
        degenerate = bool(metrics.get("degenerate"))
        score = float(row.get("score") if row.get("score") is not None else metrics.get("score") or -999)
        sortable.append((row["variant_name"], degenerate, score))
    sortable.sort(key=lambda x: (x[1], -x[2], x[0]))
    return [(name, i + 1) for i, (name, _, _) in enumerate(sortable)]


def run_variant_set(
    research_store: Any,
    variant_store: VariantStore,
    variant_set: ResearchVariantSet,
    *,
    exchange: str = "bybit",
    symbol: str = "APTUSDT",
    data_source: str = "mysql",
    warmup_start: str = "2025-12-27T00:00:00Z",
    start: str = "2026-03-01T00:00:00Z",
    end: str = "2026-03-08T00:00:00Z",
    skip_pipeline: bool = True,
    stop_on_error: bool = True,
) -> dict[str, Any]:
    vhash = variant_set_hash(variant_set)
    variant_set_id = variant_store.ensure_variant_set(
        variant_set_hash=vhash,
        name=variant_set.name,
        description=variant_set.description,
        variants_json=variant_set_json(variant_set),
    )

    # Protect baseline before any runs.
    baseline_variant = next(v for v in variant_set.variants if v.name == "baseline")
    baseline_params = build_variant_parameters(
        baseline_variant, exchange=exchange, symbol=symbol, data_source=data_source
    )
    assert_baseline_parameter_hash(baseline_params)

    names = [v.name for v in variant_set.variants]
    if len(names) != len(set(names)):
        raise ValueError("variant names must be unique")

    completed_rows: list[dict[str, Any]] = []
    baseline_metrics: dict[str, Any] | None = None

    for variant in variant_set.variants:
        params = build_variant_parameters(
            variant, exchange=exchange, symbol=symbol, data_source=data_source
        )
        phash = parameter_hash(params)
        v_hash = variant_hash(variant, resulting_parameter_hash=phash)
        try:
            result = run_baseline_research(
                research_store,
                exchange=exchange,
                symbol=symbol,
                data_source=data_source,
                warmup_start=warmup_start,
                start=start,
                end=end,
                include_pipeline=not skip_pipeline,
                params=params,
            )
            run_id = str(result["run_id"])
            trend = research_store.load_trend_states(run_id)
            structure = research_store.load_structure_events(run_id)
            stability = compute_stability_metrics(
                trend_states=trend,
                structure_events=structure,
            )
            variant_store.append_run_metrics(
                run_id, stability_metrics_to_run_metrics(stability)
            )
            row = {
                "variant_name": variant.name,
                "variant_hash": v_hash,
                "parameter_hash": phash,
                "parameter_overrides": dict(variant.parameter_overrides),
                "run_id": run_id,
                "status": VARIANT_STATUS_COMPLETED,
                "score": stability.get("score"),
                "runtime_seconds": result.get("duration_seconds"),
                "stability_metrics": stability,
                "hashes": result.get("hashes"),
                "counts": result.get("counts"),
            }
            if variant.name == "baseline":
                baseline_metrics = stability
                parity = verify_baseline_parity(research_store, run_id)
                row["baseline_parity"] = parity
                if not parity.get("equivalent"):
                    raise ValueError(
                        f"baseline parity failed vs reference: {parity.get('first_divergence')}"
                    )
            elif baseline_metrics is not None:
                row["baseline_deltas"] = compute_baseline_deltas(baseline_metrics, stability)
            completed_rows.append(row)
            variant_store.upsert_variant_run(
                variant_set_id=variant_set_id,
                variant_name=variant.name,
                variant_hash=v_hash,
                run_id=run_id,
                parameter_hash=phash,
                status=VARIANT_STATUS_COMPLETED,
                score=float(stability.get("score") or 0.0),
                rank_position=None,
                metadata_json={
                    "stability_metrics": stability,
                    "parameter_overrides": variant.parameter_overrides,
                    "hashes": result.get("hashes"),
                },
            )
        except Exception as exc:
            variant_store.upsert_variant_run(
                variant_set_id=variant_set_id,
                variant_name=variant.name,
                variant_hash=v_hash,
                run_id="",
                parameter_hash=phash,
                status=VARIANT_STATUS_FAILED,
                score=None,
                rank_position=None,
                metadata_json={"error_type": type(exc).__name__, "error_message": str(exc)},
            )
            if stop_on_error:
                raise
            completed_rows.append(
                {
                    "variant_name": variant.name,
                    "status": VARIANT_STATUS_FAILED,
                    "error": str(exc),
                }
            )

    rankings = rank_variants(completed_rows)
    variant_store.update_rankings(variant_set_id, rankings)
    rank_map = dict(rankings)
    for row in completed_rows:
        row["rank_position"] = rank_map.get(row.get("variant_name"))

    baseline_row = next((r for r in completed_rows if r.get("variant_name") == "baseline"), None)
    artifacts = write_variant_set_report(
        variant_set_name=variant_set.name,
        rows=completed_rows,
        baseline_row=baseline_row,
    )

    return {
        "variant_set": variant_set.name,
        "variant_set_id": variant_set_id,
        "variant_set_hash": vhash,
        "variants": completed_rows,
        "ranking": rankings,
        "artifacts": {k: str(v) for k, v in artifacts.items()},
        "baseline_parameter_hash": parameter_hash(baseline_params),
    }


def verify_baseline_parity(
    research_store: ResearchRunStore,
    new_baseline_run_id: str,
    *,
    reference_run_id: str = BASELINE_REFERENCE_RUN_ID,
) -> dict[str, Any]:
    ref = research_store.get_run(reference_run_id)
    new = research_store.get_run(new_baseline_run_id)
    if ref is None:
        return {"equivalent": False, "error": f"reference run not found: {reference_run_id}"}
    if new is None:
        return {"equivalent": False, "error": f"new run not found: {new_baseline_run_id}"}
    ref_ph = ref.get("parameter_hash_value") or ref.get("parameter_hash")
    new_ph = new.get("parameter_hash_value") or new.get("parameter_hash")
    if ref_ph != new_ph:
        return {
            "equivalent": False,
            "first_divergence": {"field": "parameter_hash", "reference": ref_ph, "new": new_ph},
        }
    return compare_runs(research_store, reference_run_id, new_baseline_run_id)


def repeat_variant(
    research_store: Any,
    variant_store: VariantStore,
    variant: ResearchVariant,
    *,
    exchange: str = "bybit",
    symbol: str = "APTUSDT",
    data_source: str = "mysql",
    warmup_start: str = "2025-12-27T00:00:00Z",
    start: str = "2026-03-01T00:00:00Z",
    end: str = "2026-03-08T00:00:00Z",
    skip_pipeline: bool = True,
) -> dict[str, Any]:
    params = build_variant_parameters(
        variant, exchange=exchange, symbol=symbol, data_source=data_source
    )
    result = run_baseline_research(
        research_store,
        exchange=exchange,
        symbol=symbol,
        data_source=data_source,
        warmup_start=warmup_start,
        start=start,
        end=end,
        include_pipeline=not skip_pipeline,
        params=params,
    )
    run_id = str(result["run_id"])
    trend = research_store.load_trend_states(run_id)
    structure = research_store.load_structure_events(run_id)
    stability = compute_stability_metrics(trend_states=trend, structure_events=structure)
    return {
        "run_id": run_id,
        "parameter_hash": parameter_hash(params),
        "hashes": result.get("hashes"),
        "stability_metrics": stability,
        "score": stability.get("score"),
    }


__all__ = ["InMemoryVariantStore", "run_variant_set", "repeat_variant", "verify_baseline_parity"]

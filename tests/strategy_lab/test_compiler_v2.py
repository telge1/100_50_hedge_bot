"""P5 StrategySpecV2 compiler and stable hash tests."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from orderbook_analyse.strategy_lab.compiler_v2 import (
    CanonicalizationError,
    CompiledStrategyV2,
    _canonicalize_value,
    _decimal_json_token,
    _render_json_value,
    compile_strategy_v2,
    hash_canonical_strategy_v2_json,
    render_canonical_strategy_v2_json,
)
from orderbook_analyse.strategy_lab.models import (
    ContractVersion,
    DecimalParam,
    DurationValue,
    FeatureParameterTargetV2,
    IntParam,
    PluginProvenanceRefV2,
    RateParam,
    RateValue,
    ResearchDimensionV2,
    ResearchParameterSpaceV2,
    RoundtripCostTargetV2,
    SignalTimeframeTargetV2,
    TimeframeParam,
    VersionedUniverseRefV2,
)
from orderbook_analyse.strategy_lab.models.enums import DurationUnit, RateUnit
from orderbook_analyse.strategy_lab.schema import COMMITTED_SCHEMA_V2_PATH
from orderbook_analyse.strategy_lab.validation import (
    ValidationFailedError,
    production_catalog_bundle_v2,
)
from tests.strategy_lab.conftest import _tf
from tests.strategy_lab.validation.conftest import (
    p4c_valid_cluster_strategy,
    p4c_valid_edc_strategy,
    p4c_valid_rule_based_long_strategy,
    sid,
    valid_state_machine_long_strategy,
)


@pytest.fixture
def catalogs():
    return production_catalog_bundle_v2()


def _p4c_valid_state_machine_strategy():
    from orderbook_analyse.strategy_lab.catalogs.v2.registry import FEATURE_CATALOG_V2
    from orderbook_analyse.strategy_lab.models import (
        CausalityStatus,
        ProvenanceSpecV2,
        SignalEngineWarmupV2,
        ValidationRequirements,
        WarmupSpecV2,
    )
    from orderbook_analyse.strategy_lab.models.contracts_v2.padding import (
        OutcomeEvaluationPaddingV2,
        SourceLoadingPaddingV2,
    )

    base = valid_state_machine_long_strategy()
    reqs = []
    seen: set[str] = set()
    for binding in base.features:
        feature = FEATURE_CATALOG_V2.get(binding.catalog_feature_id.value)
        for req in feature.data_requirements:
            if req.requirement_id.value in seen:
                continue
            seen.add(req.requirement_id.value)
            reqs.append(req)
    return dataclasses.replace(
        base,
        data_requirements=tuple(reqs),
        warmup=WarmupSpecV2(
            signal_engine=SignalEngineWarmupV2(minimum_bars=79, bar_timeframe=_tf(5)),
            source_loading=SourceLoadingPaddingV2(
                candle_history=DurationValue(
                    value=Decimal("120"), unit=DurationUnit.HOURS
                ),
                auxiliary_source_history=DurationValue(
                    value=Decimal("2"), unit=DurationUnit.HOURS
                ),
            ),
            outcome_evaluation=OutcomeEvaluationPaddingV2(
                post_window_duration=DurationValue(
                    value=Decimal("12"), unit=DurationUnit.HOURS
                ),
            ),
        ),
        research_parameter_space=ResearchParameterSpaceV2(dimensions=()),
        validation_requirements=ValidationRequirements(
            require_causality_audit=True,
            require_strategy_parity_check=True,
            allowed_causality_statuses=(),
        ),
        provenance=ProvenanceSpecV2(
            git_commit="0000000000000000000000000000000000000000",
            source_repository="orderbook_analyse",
            source_paths=("tests/strategy_lab/",),
            catalog_contract_version=ContractVersion(value="catalog/v2"),
            plugin_refs=(),
            causality_status=CausalityStatus.CAUSALITY_UNPROVEN,
        ),
    )


def test_compile_edc(catalogs) -> None:
    compiled = compile_strategy_v2(p4c_valid_edc_strategy(), catalogs)
    assert isinstance(compiled.canonical_bytes, bytes)
    assert compiled.strategy_hash == hashlib.sha256(compiled.canonical_bytes).hexdigest()


def test_compile_cluster(catalogs) -> None:
    assert compile_strategy_v2(p4c_valid_cluster_strategy(), catalogs).strategy_hash


def test_compile_rule_based(catalogs) -> None:
    assert compile_strategy_v2(p4c_valid_rule_based_long_strategy(), catalogs).strategy_hash


def test_compile_state_machine(catalogs) -> None:
    assert compile_strategy_v2(_p4c_valid_state_machine_strategy(), catalogs).strategy_hash


def test_p4c_called_exactly_once(catalogs, monkeypatch) -> None:
    calls: list[object] = []

    def spy(spec, cats):
        calls.append((spec, cats))

    monkeypatch.setattr(
        "orderbook_analyse.strategy_lab.compiler_v2.require_valid_strategy_v2_p4c",
        spy,
    )
    compile_strategy_v2(p4c_valid_edc_strategy(), catalogs)
    assert len(calls) == 1


def test_invalid_strategy_stops_before_render_and_hash(catalogs, monkeypatch) -> None:
    render_calls: list[object] = []
    hash_calls: list[object] = []

    monkeypatch.setattr(
        "orderbook_analyse.strategy_lab.compiler_v2.render_canonical_strategy_v2_json",
        lambda spec: render_calls.append(spec) or b"{}",
    )
    monkeypatch.setattr(
        "orderbook_analyse.strategy_lab.compiler_v2.hash_canonical_strategy_v2_json",
        lambda data: hash_calls.append(data) or "0" * 64,
    )
    broken = dataclasses.replace(p4c_valid_edc_strategy(), data_requirements=())
    with pytest.raises(ValidationFailedError) as exc:
        compile_strategy_v2(broken, catalogs)
    assert exc.value.report.errors
    assert render_calls == []
    assert hash_calls == []


def test_identical_spec_identical_bytes_and_hash(catalogs) -> None:
    spec = p4c_valid_edc_strategy()
    a = compile_strategy_v2(spec, catalogs)
    b = compile_strategy_v2(spec, catalogs)
    assert a.canonical_bytes == b.canonical_bytes
    assert a.strategy_hash == b.strategy_hash
    assert a.canonical_json == a.canonical_bytes.decode("utf-8")


def test_cross_process_determinism(catalogs) -> None:
    repo = Path(__file__).resolve().parents[2]
    script = r"""
import sys
sys.path.insert(0, 'src')
from orderbook_analyse.strategy_lab.compiler_v2 import compile_strategy_v2
from orderbook_analyse.strategy_lab.validation import production_catalog_bundle_v2
from tests.strategy_lab.validation.conftest import p4c_valid_edc_strategy
c = compile_strategy_v2(p4c_valid_edc_strategy(), production_catalog_bundle_v2())
print(c.strategy_hash)
print(c.canonical_bytes.hex())
"""
    runs = []
    for _ in range(2):
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        hash_line, hex_line = proc.stdout.strip().splitlines()
        runs.append((hash_line, bytes.fromhex(hex_line)))
    assert runs[0] == runs[1]


def test_hash_is_64_lowercase_hex_not_embedded(catalogs) -> None:
    compiled = compile_strategy_v2(p4c_valid_edc_strategy(), catalogs)
    assert re.fullmatch(r"[0-9a-f]{64}", compiled.strategy_hash)
    assert compiled.strategy_hash not in compiled.canonical_json
    assert "catalog_contract_version" in compiled.canonical_json  # from provenance only


def test_no_bom_no_trailing_newline_no_whitespace_formatting(catalogs) -> None:
    raw = compile_strategy_v2(p4c_valid_edc_strategy(), catalogs).canonical_bytes
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert not raw.endswith(b"\n")
    text = raw.decode("utf-8")
    assert "\n" not in text
    assert ": " not in text
    assert ", " not in text


def test_object_keys_are_lexicographically_sorted(catalogs) -> None:
    text = compile_strategy_v2(p4c_valid_edc_strategy(), catalogs).canonical_json
    # Spot-check costs object key order in rendered text.
    assert '"funding":' in text and '"roundtrip_cost":' in text and '"slippage":' in text
    funding = text.index('"funding":')
    roundtrip = text.index('"roundtrip_cost":')
    slippage = text.index('"slippage":')
    assert funding < roundtrip < slippage


def test_tuple_order_preserved_in_research_candidates(catalogs) -> None:
    spec = dataclasses.replace(
        p4c_valid_edc_strategy(),
        research_parameter_space=ResearchParameterSpaceV2(
            dimensions=(
                ResearchDimensionV2(
                    dimension_id=sid("periods"),
                    target=FeatureParameterTargetV2(
                        feature_alias=sid("ema_fast"),
                        parameter_name=sid("period"),
                    ),
                    candidates=(IntParam(value=9), IntParam(value=12), IntParam(value=20)),
                ),
            )
        ),
    )
    payload = json.loads(compile_strategy_v2(spec, catalogs).canonical_json)
    values = [
        item["value"]
        for item in payload["research_parameter_space"]["dimensions"][0]["candidates"]
    ]
    assert values == [9, 12, 20]


def test_unicode_and_string_escaping_stable(catalogs) -> None:
    spec = dataclasses.replace(
        p4c_valid_edc_strategy(),
        metadata=dataclasses.replace(
            p4c_valid_edc_strategy().metadata,
            title='Quote "äöü" and \\ slash',
        ),
    )
    text = compile_strategy_v2(spec, catalogs).canonical_json
    assert "äöü" in text
    assert '\\"' in text or '"Quote \\"' in text
    assert json.loads(text)["metadata"]["title"] == 'Quote "äöü" and \\ slash'


def test_decimal_is_json_number_not_string(catalogs) -> None:
    raw = compile_strategy_v2(p4c_valid_edc_strategy(), catalogs).canonical_bytes
    text = raw.decode("utf-8")
    assert '"0.15"' not in text
    assert ":0.15" in text or ":0.15," in text or ":0.15}" in text
    payload = json.loads(text)
    assert isinstance(payload["costs"]["roundtrip_cost"]["value"], (int, float))
    assert not isinstance(payload["costs"]["roundtrip_cost"]["value"], str)
    assert isinstance(payload["exit"]["take_profit"]["value"], (int, float))
    assert isinstance(payload["exit"]["stop_loss"]["value"], (int, float))
    assert isinstance(payload["execution_assumptions"]["fixed_notional"], (int, float))


def test_decimal_normalization_rules() -> None:
    assert _decimal_json_token(Decimal("-0"), path="$") == "0"
    assert _decimal_json_token(Decimal("0.1500"), path="$") == "0.15"
    assert _decimal_json_token(Decimal("1.000"), path="$") == "1"
    assert _decimal_json_token(Decimal("0.0001"), path="$") == "0.0001"
    assert _decimal_json_token(Decimal("1E+2"), path="$") == "100"
    assert "e" not in _decimal_json_token(Decimal("1E-4"), path="$").lower()


def test_nan_infinity_rejected() -> None:
    with pytest.raises(CanonicalizationError) as exc:
        _decimal_json_token(Decimal("NaN"), path="$.x")
    assert exc.value.path == "$.x"
    with pytest.raises(CanonicalizationError):
        _decimal_json_token(Decimal("Infinity"), path="$.x")


def test_float_list_set_unknown_rejected_by_internal_renderer() -> None:
    with pytest.raises(CanonicalizationError) as exc:
        _render_json_value(1.5, path="$.bad")
    assert exc.value.path == "$.bad"
    with pytest.raises(CanonicalizationError):
        _render_json_value([1, 2], path="$.bad")
    with pytest.raises(CanonicalizationError):
        _render_json_value({1, 2}, path="$.bad")
    with pytest.raises(CanonicalizationError) as exc2:
        _canonicalize_value(object(), path="$.obj")
    assert exc2.value.path == "$.obj"


def test_error_contains_exact_model_path() -> None:
    with pytest.raises(CanonicalizationError) as exc:
        _canonicalize_value(1.23, path="$.costs.roundtrip_cost.value")
    assert exc.value.path == "$.costs.roundtrip_cost.value"


def test_spec_and_catalogs_not_mutated(catalogs) -> None:
    spec = p4c_valid_edc_strategy()
    before_spec = dataclasses.asdict(spec)
    before_ids = (
        id(catalogs),
        id(catalogs.features),
        id(catalogs.plugins),
    )
    compile_strategy_v2(spec, catalogs)
    assert dataclasses.asdict(spec) == before_spec
    assert (
        id(catalogs),
        id(catalogs.features),
        id(catalogs.plugins),
    ) == before_ids


def test_cost_change_changes_hash(catalogs) -> None:
    base = compile_strategy_v2(p4c_valid_edc_strategy(), catalogs)
    changed = dataclasses.replace(
        p4c_valid_edc_strategy(),
        costs=dataclasses.replace(
            p4c_valid_edc_strategy().costs,
            roundtrip_cost=RateValue(value=Decimal("0.20"), unit=RateUnit.PERCENT),
        ),
    )
    assert compile_strategy_v2(changed, catalogs).strategy_hash != base.strategy_hash


def test_universe_change_changes_hash(catalogs) -> None:
    base = compile_strategy_v2(p4c_valid_edc_strategy(), catalogs)
    changed = dataclasses.replace(
        p4c_valid_edc_strategy(),
        universe=VersionedUniverseRefV2(
            universe_id=sid("tradeable_51"),
            version="v2",
            content_hash="sha256:other",
        ),
    )
    assert compile_strategy_v2(changed, catalogs).strategy_hash != base.strategy_hash


def test_provenance_change_changes_hash(catalogs) -> None:
    base = compile_strategy_v2(p4c_valid_edc_strategy(), catalogs)
    changed = dataclasses.replace(
        p4c_valid_edc_strategy(),
        provenance=dataclasses.replace(
            p4c_valid_edc_strategy().provenance,
            git_commit="1111111111111111111111111111111111111111",
        ),
    )
    assert compile_strategy_v2(changed, catalogs).strategy_hash != base.strategy_hash


def test_research_change_changes_hash(catalogs) -> None:
    base = compile_strategy_v2(p4c_valid_edc_strategy(), catalogs)
    changed = dataclasses.replace(
        p4c_valid_edc_strategy(),
        research_parameter_space=ResearchParameterSpaceV2(
            dimensions=(
                ResearchDimensionV2(
                    dimension_id=sid("tf"),
                    target=SignalTimeframeTargetV2(),
                    candidates=(TimeframeParam(value=_tf(5)),),
                ),
            )
        ),
    )
    assert compile_strategy_v2(changed, catalogs).strategy_hash != base.strategy_hash


def test_tuple_order_change_changes_hash(catalogs) -> None:
    dim_a = ResearchDimensionV2(
        dimension_id=sid("a"),
        target=FeatureParameterTargetV2(
            feature_alias=sid("ema_fast"),
            parameter_name=sid("period"),
        ),
        candidates=(IntParam(value=9),),
    )
    dim_b = ResearchDimensionV2(
        dimension_id=sid("b"),
        target=FeatureParameterTargetV2(
            feature_alias=sid("ema_slow"),
            parameter_name=sid("period"),
        ),
        candidates=(IntParam(value=20),),
    )
    fwd = dataclasses.replace(
        p4c_valid_edc_strategy(),
        research_parameter_space=ResearchParameterSpaceV2(dimensions=(dim_a, dim_b)),
    )
    rev = dataclasses.replace(
        p4c_valid_edc_strategy(),
        research_parameter_space=ResearchParameterSpaceV2(dimensions=(dim_b, dim_a)),
    )
    assert (
        compile_strategy_v2(fwd, catalogs).strategy_hash
        != compile_strategy_v2(rev, catalogs).strategy_hash
    )


def _strategy_spec_v2_schema() -> dict[str, object]:
    """Committed V2 schema document scoped to StrategySpecV2 (same pattern as schema tests)."""
    schema = json.loads(COMMITTED_SCHEMA_V2_PATH.read_text(encoding="utf-8"))
    return {"$defs": schema["$defs"], "$ref": "#/$defs/StrategySpecV2"}


def test_compiler_output_validates_against_v2_schema(catalogs) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    compiled = compile_strategy_v2(p4c_valid_edc_strategy(), catalogs)
    payload = json.loads(compiled.canonical_bytes)
    jsonschema.validate(payload, _strategy_spec_v2_schema())

    assert isinstance(payload["exit"]["take_profit"]["value"], (int, float))
    assert isinstance(payload["exit"]["stop_loss"]["value"], (int, float))
    assert isinstance(payload["costs"]["roundtrip_cost"]["value"], (int, float))
    assert isinstance(payload["execution_assumptions"]["fixed_notional"], (int, float))
    assert not isinstance(payload["exit"]["take_profit"]["value"], str)
    assert not isinstance(payload["exit"]["stop_loss"]["value"], str)
    assert not isinstance(payload["costs"]["roundtrip_cost"]["value"], str)
    assert not isinstance(payload["execution_assumptions"]["fixed_notional"], str)


def test_decimal_and_rate_candidates_are_numbers_in_schema_payload(catalogs) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    spec = dataclasses.replace(
        p4c_valid_edc_strategy(),
        research_parameter_space=ResearchParameterSpaceV2(
            dimensions=(
                ResearchDimensionV2(
                    dimension_id=sid("cost"),
                    target=RoundtripCostTargetV2(),
                    candidates=(
                        RateParam(
                            value=RateValue(
                                value=Decimal("0.1500"),
                                unit=RateUnit.PERCENT,
                            )
                        ),
                        RateParam(
                            value=RateValue(
                                value=Decimal("0"),
                                unit=RateUnit.PERCENT,
                            )
                        ),
                    ),
                ),
                ResearchDimensionV2(
                    dimension_id=sid("dec"),
                    target=FeatureParameterTargetV2(
                        feature_alias=sid("ema_fast"),
                        parameter_name=sid("period"),
                    ),
                    candidates=(IntParam(value=9),),
                ),
            )
        ),
    )
    # DecimalParam is not a valid RoundtripCost candidate under P4C; prove number
    # tokens for RateParam via full compile + schema, DecimalParam via renderer.
    compiled = compile_strategy_v2(spec, catalogs)
    payload = json.loads(compiled.canonical_bytes)
    jsonschema.validate(payload, _strategy_spec_v2_schema())
    rate_cand = payload["research_parameter_space"]["dimensions"][0]["candidates"][0]
    assert rate_cand["kind"] == "rate"
    assert isinstance(rate_cand["value"]["value"], (int, float))
    assert not isinstance(rate_cand["value"]["value"], str)
    assert rate_cand["value"]["value"] == 0.15

    rendered = _render_json_value(
        _canonicalize_value(DecimalParam(value=Decimal("1.000")), path="$.x"),
        path="$.x",
    )
    assert rendered == '{"kind":"decimal","value":1}'
    loaded = json.loads(rendered)
    assert isinstance(loaded["value"], (int, float))
    assert not isinstance(loaded["value"], str)


def test_large_and_small_decimal_lossless_tokens() -> None:
    assert _decimal_json_token(Decimal("12345678901234567890.123456789"), path="$") == (
        "12345678901234567890.123456789"
    )
    assert _decimal_json_token(Decimal("0.00000000000000000001"), path="$") == (
        "0.00000000000000000001"
    )


def test_hash_helper_matches_sha256(catalogs) -> None:
    raw = render_canonical_strategy_v2_json(p4c_valid_edc_strategy())
    assert hash_canonical_strategy_v2_json(raw) == hashlib.sha256(raw).hexdigest()


def test_compiled_strategy_rejects_non_bytes() -> None:
    with pytest.raises(TypeError):
        CompiledStrategyV2(
            canonical_bytes="{}",  # type: ignore[arg-type]
            strategy_hash="a" * 64,
        )


def test_public_api_has_no_mutable_canonicalize_export() -> None:
    import orderbook_analyse.strategy_lab.compiler_v2 as mod

    assert not hasattr(mod, "canonicalize_strategy_v2") or not callable(
        getattr(mod, "canonicalize_strategy_v2", None)
    )
    # Ensure the public name is absent
    assert "canonicalize_strategy_v2" not in getattr(mod, "__all__", ())
    assert not hasattr(mod, "canonicalize_strategy_v2")

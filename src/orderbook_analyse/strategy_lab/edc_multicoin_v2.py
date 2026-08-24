"""EDC M0 multicoin runner (P2D2).

Sequential per-symbol orchestration:
``load_edc_m0_market_data_v2`` → ``execute_edc_m0_strict_sync_v2`` → atomic checkpoint.

No parallelization, no exports, no enrichment, no Cluster.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from orderbook_analyse.strategy_lab.adapters.edc_io import (
    ClickHouseQueryClient,
    StrategyMarketDataError,
    load_edc_m0_market_data_v2,
)
from orderbook_analyse.strategy_lab.adapters.edc_m0 import (
    StrategyAdapterError,
    execute_edc_m0_strict_sync_v2,
)
from orderbook_analyse.strategy_lab.catalogs.v2.models import CATALOG_CONTRACT_VERSION
from orderbook_analyse.strategy_lab.compiler_v2 import (
    CompiledStrategyV2,
    compile_strategy_v2,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    ResearchConfirmationPolicyV2,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.phase1_contracts import (
    VersionedUniverseRefV2,
)
from orderbook_analyse.strategy_lab.models.enums import (
    ModelingStatus,
    RateUnit,
    SideName,
    TimeframeUnit,
)
from orderbook_analyse.strategy_lab.models.identifiers import ContractVersion, StableIdentifier
from orderbook_analyse.strategy_lab.models.signals import PluginSignalSpec
from orderbook_analyse.strategy_lab.models.strategy import RateValue, TimeframeValue
from orderbook_analyse.strategy_lab.models.strategy_v2 import StrategySpecV2
from orderbook_analyse.strategy_lab.results_v2 import (
    SourceEventIdV2,
    StrategyRunResultV2,
    StrategyRunStatusV2,
    StrategyTradeV2,
    TradeExitReasonV2,
)
from orderbook_analyse.strategy_lab.validation.catalogs import CatalogBundleV2
from orderbook_analyse.strategy_lab.validation.models import ValidationFailedError
from orderbook_analyse.strategy_lab.validation.p4c import require_valid_strategy_v2_p4c

_PLUGIN_ID = "edc_m0_strict_sync"
_MODE_ID = "m0_strict_sync"
_ZERO = timedelta(0)
_CHECKPOINT_FORMAT_VERSION = "edc_multicoin_checkpoint/v1"
_ADDR_RE = re.compile(r" at 0x[0-9a-fA-F]+")

try:
    from clickhouse_connect.driver.exceptions import ClickHouseError as _ClickHouseError
except ImportError:  # pragma: no cover
    _ClickHouseError = None

_SYMBOL_CATCH: tuple[type[BaseException], ...] = (
    StrategyMarketDataError,
    StrategyAdapterError,
) + ((_ClickHouseError,) if _ClickHouseError is not None else ())


class StrategyMulticoinError(ValueError):
    """Deterministic multicoin runner rejection (preflight / checkpoint)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolRunFailureV2:
    """One symbol failure without tracebacks or object addresses."""

    symbol: str
    error_type: str
    message: str

    def __post_init__(self) -> None:
        if type(self.symbol) is not str or not self.symbol or self.symbol != self.symbol.strip():
            raise ValueError("symbol must be a non-empty str without padding")
        if type(self.error_type) is not str or not self.error_type:
            raise ValueError("error_type must be a non-empty str")
        if type(self.message) is not str:
            raise TypeError("message must be str")
        if "0x" in self.message and _ADDR_RE.search(self.message):
            raise ValueError("message must not contain object addresses")


@dataclass(frozen=True, slots=True, kw_only=True)
class EdcMulticoinRunV2:
    """Aggregated multicoin run (no duplicated summary fields)."""

    strategy_hash: str
    universe: VersionedUniverseRefV2
    start: datetime
    end: datetime
    requested_symbols: tuple[str, ...]
    completed_runs: tuple[StrategyRunResultV2, ...]
    failures: tuple[SymbolRunFailureV2, ...]

    def __post_init__(self) -> None:
        if type(self.strategy_hash) is not str or len(self.strategy_hash) != 64:
            raise ValueError("strategy_hash must be 64-char hex str")
        if type(self.universe) is not VersionedUniverseRefV2:
            raise TypeError("universe must be VersionedUniverseRefV2")
        if type(self.requested_symbols) is not tuple or not self.requested_symbols:
            raise ValueError("requested_symbols must be a non-empty tuple")
        if type(self.completed_runs) is not tuple:
            raise TypeError("completed_runs must be a tuple")
        if type(self.failures) is not tuple:
            raise TypeError("failures must be a tuple")
        for run in self.completed_runs:
            if type(run) is not StrategyRunResultV2:
                raise TypeError("completed_runs must contain StrategyRunResultV2")
        for fail in self.failures:
            if type(fail) is not SymbolRunFailureV2:
                raise TypeError("failures must contain SymbolRunFailureV2")

    @property
    def completed_symbols(self) -> tuple[str, ...]:
        out: list[str] = []
        for run in self.completed_runs:
            out.extend(run.symbols)
        return tuple(out)

    @property
    def failed_symbols(self) -> tuple[str, ...]:
        return tuple(f.symbol for f in self.failures)

    @property
    def candidate_count(self) -> int:
        return sum(run.candidate_count for run in self.completed_runs)

    @property
    def trade_count(self) -> int:
        return sum(run.trade_count for run in self.completed_runs)

    @property
    def gross_pnl_usdt(self) -> Decimal:
        total = Decimal("0")
        for run in self.completed_runs:
            total += run.gross_pnl_usdt
        return total

    @property
    def costs_usdt(self) -> Decimal:
        total = Decimal("0")
        for run in self.completed_runs:
            total += run.costs_usdt
        return total

    @property
    def net_pnl_usdt(self) -> Decimal:
        total = Decimal("0")
        for run in self.completed_runs:
            total += run.net_pnl_usdt
        return total


def run_edc_m0_multicoin_v2(
    spec: StrategySpecV2,
    compiled: CompiledStrategyV2,
    catalogs: CatalogBundleV2,
    *,
    client: ClickHouseQueryClient,
    universe_path: Path,
    start: datetime,
    end: datetime,
    checkpoint_dir: Path,
    symbols: tuple[str, ...] | None = None,
    resume: bool = False,
    retry_failures: bool = False,
) -> EdcMulticoinRunV2:
    """Run EDC M0 Strict Sync sequentially for a universe subset.

    Checkpoints are written under ``checkpoint_dir / "symbols" / "<SYMBOL>.json``.
    """
    if type(spec) is not StrategySpecV2:
        raise StrategyMulticoinError("spec must be StrategySpecV2")
    if type(compiled) is not CompiledStrategyV2:
        raise StrategyMulticoinError("compiled must be CompiledStrategyV2")
    if type(catalogs) is not CatalogBundleV2:
        raise StrategyMulticoinError("catalogs must be CatalogBundleV2")
    if not isinstance(client, ClickHouseQueryClient):
        raise StrategyMulticoinError(
            "client must provide a query(...) method returning result_rows"
        )
    if not isinstance(universe_path, Path):
        raise StrategyMulticoinError("universe_path must be pathlib.Path")
    if not isinstance(checkpoint_dir, Path):
        raise StrategyMulticoinError("checkpoint_dir must be pathlib.Path")
    if type(resume) is not bool or type(retry_failures) is not bool:
        raise StrategyMulticoinError("resume and retry_failures must be bool")

    start_u = _require_utc(start, field_name="start")
    end_u = _require_utc(end, field_name="end")
    if end_u <= start_u:
        raise StrategyMulticoinError("end must be > start")

    _assert_edc_preflight(spec, compiled, catalogs)
    universe_symbols, universe_hash = _load_and_verify_universe(
        universe_path, expected=spec.universe
    )
    requested = _resolve_requested_symbols(universe_symbols, symbols)

    symbols_dir = checkpoint_dir / "symbols"
    symbols_dir.mkdir(parents=True, exist_ok=True)

    fingerprint = _run_fingerprint(spec, compiled, universe_hash, start_u, end_u)
    completed: list[StrategyRunResultV2] = []
    failures: list[SymbolRunFailureV2] = []

    for symbol in requested:
        path = symbols_dir / f"{symbol}.json"
        if resume and path.is_file():
            action, payload = _inspect_checkpoint(
                path, fingerprint=fingerprint, symbol=symbol
            )
            if action == "use_success":
                assert isinstance(payload, StrategyRunResultV2)
                completed.append(payload)
                continue
            if action == "use_failure" and not retry_failures:
                assert isinstance(payload, SymbolRunFailureV2)
                failures.append(payload)
                continue
            # action == "rerun" or retry_failures on failure

        try:
            market = load_edc_m0_market_data_v2(
                spec,
                catalogs,
                client=client,
                symbol=symbol,
                start=start_u,
                end=end_u,
            )
            result = execute_edc_m0_strict_sync_v2(
                spec,
                compiled,
                catalogs,
                symbol=symbol,
                start=start_u,
                end=end_u,
                market_data=market,
            )
        except _SYMBOL_CATCH as exc:
            # Only expected per-symbol runtime failures are isolated.
            # Programming/integrity errors (TypeError, AssertionError, …) propagate.
            failure = SymbolRunFailureV2(
                symbol=symbol,
                error_type=type(exc).__name__,
                message=_sanitize_message(exc),
            )
            _write_checkpoint(
                path,
                fingerprint=fingerprint,
                symbol=symbol,
                status="failed",
                failure=failure,
            )
            failures.append(failure)
            continue

        _write_checkpoint(
            path,
            fingerprint=fingerprint,
            symbol=symbol,
            status="complete",
            result=result,
        )
        completed.append(result)

    return EdcMulticoinRunV2(
        strategy_hash=compiled.strategy_hash,
        universe=spec.universe,
        start=start_u,
        end=end_u,
        requested_symbols=requested,
        completed_runs=tuple(completed),
        failures=tuple(failures),
    )


def _assert_edc_preflight(
    spec: StrategySpecV2,
    compiled: CompiledStrategyV2,
    catalogs: CatalogBundleV2,
) -> None:
    if type(spec.signal) is not PluginSignalSpec:
        raise StrategyMulticoinError("spec.signal must be PluginSignalSpec")
    signal = spec.signal
    if signal.plugin.plugin_id.value != _PLUGIN_ID:
        raise StrategyMulticoinError(
            f"plugin_id must be {_PLUGIN_ID!r}, got {signal.plugin.plugin_id.value!r}"
        )
    if signal.plugin.contract_version.value != CATALOG_CONTRACT_VERSION:
        raise StrategyMulticoinError(
            "plugin contract_version must be "
            f"{CATALOG_CONTRACT_VERSION!r}, got {signal.plugin.contract_version.value!r}"
        )
    if signal.mode_id is None or signal.mode_id.value != _MODE_ID:
        raise StrategyMulticoinError(
            f"mode_id must be {_MODE_ID!r}, got "
            f"{None if signal.mode_id is None else signal.mode_id.value!r}"
        )
    if signal.confirmation_policy is not ResearchConfirmationPolicyV2.CORE_RESEARCH_SUPPORTIVE:
        raise StrategyMulticoinError(
            "confirmation_policy must be core_research_supportive"
        )
    try:
        require_valid_strategy_v2_p4c(spec, catalogs)
    except ValidationFailedError as exc:
        raise StrategyMulticoinError("StrategySpecV2 failed P4C validation") from exc
    recomputed = compile_strategy_v2(spec, catalogs)
    if recomputed.strategy_hash != compiled.strategy_hash:
        raise StrategyMulticoinError("compiled.strategy_hash does not match Spec")
    if recomputed.canonical_bytes != compiled.canonical_bytes:
        raise StrategyMulticoinError("compiled.canonical_bytes do not match Spec")


def _load_and_verify_universe(
    path: Path,
    *,
    expected: VersionedUniverseRefV2,
) -> tuple[tuple[str, ...], str]:
    if not path.is_file():
        raise StrategyMulticoinError(f"universe file not found: {path}")
    raw_bytes = path.read_bytes()
    digest = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
    if digest != expected.content_hash:
        raise StrategyMulticoinError(
            "universe file content_hash does not match StrategySpec "
            f"(file={digest}, spec={expected.content_hash})"
        )
    try:
        doc = json.loads(raw_bytes.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise StrategyMulticoinError("universe file is not valid JSON") from exc
    if type(doc) is not dict:
        raise StrategyMulticoinError("universe file must be a JSON object")
    symbols_raw = doc.get("symbols")
    if type(symbols_raw) is not list:
        raise StrategyMulticoinError("universe file must contain a symbols list")
    symbols: list[str] = []
    seen: set[str] = set()
    for item in symbols_raw:
        if type(item) is not str or not item or item != item.strip():
            raise StrategyMulticoinError(
                "universe symbols must be non-empty strings without padding"
            )
        # No silent case normalization — reject if Spec would see a different form.
        if item in seen:
            raise StrategyMulticoinError(f"duplicate symbol in universe file: {item!r}")
        seen.add(item)
        symbols.append(item)
    if len(symbols) != 51:
        raise StrategyMulticoinError(
            f"universe must contain exactly 51 symbols, got {len(symbols)}"
        )
    if "n" in doc and doc["n"] != 51:
        raise StrategyMulticoinError("universe file n must be 51 when present")
    if "version" in doc and str(doc["version"]) != expected.version:
        raise StrategyMulticoinError(
            f"universe file version {doc['version']!r} does not match Spec "
            f"{expected.version!r}"
        )
    return tuple(symbols), digest


def _resolve_requested_symbols(
    universe: tuple[str, ...],
    symbols: tuple[str, ...] | None,
) -> tuple[str, ...]:
    if symbols is None:
        return universe
    if type(symbols) is not tuple:
        raise StrategyMulticoinError("symbols must be a tuple[str, ...] or None")
    if not symbols:
        raise StrategyMulticoinError("symbols subset must be non-empty")
    seen: set[str] = set()
    for sym in symbols:
        if type(sym) is not str or not sym or sym != sym.strip():
            raise StrategyMulticoinError(
                "symbols entries must be non-empty str without padding"
            )
        if sym in seen:
            raise StrategyMulticoinError(f"duplicate symbol in request: {sym!r}")
        seen.add(sym)
        if sym not in universe:
            raise StrategyMulticoinError(
                f"symbol {sym!r} is not in the Strategy universe"
            )
    # Preserve full-universe order, not request or set order.
    return tuple(s for s in universe if s in seen)


def _run_fingerprint(
    spec: StrategySpecV2,
    compiled: CompiledStrategyV2,
    universe_hash: str,
    start: datetime,
    end: datetime,
) -> dict[str, object]:
    signal = spec.signal
    assert type(signal) is PluginSignalSpec
    assert signal.mode_id is not None
    return {
        "checkpoint_format_version": _CHECKPOINT_FORMAT_VERSION,
        "strategy_hash": compiled.strategy_hash,
        "universe_id": spec.universe.universe_id.value,
        "universe_version": spec.universe.version,
        "universe_content_hash": universe_hash,
        "start": _dt_to_iso(start),
        "end": _dt_to_iso(end),
        "plugin_id": signal.plugin.plugin_id.value,
        "plugin_contract_version": signal.plugin.contract_version.value,
        "mode_id": signal.mode_id.value,
        "confirmation_policy": signal.confirmation_policy.value
        if signal.confirmation_policy is not None
        else None,
        "roundtrip_cost_value": str(spec.costs.roundtrip_cost.value),
        "roundtrip_cost_unit": spec.costs.roundtrip_cost.unit.value,
        "slippage_status": spec.costs.slippage.value,
        "funding_status": spec.costs.funding.value,
        "signal_timeframe_value": spec.timeframes.signal.value,
        "signal_timeframe_unit": spec.timeframes.signal.unit.value,
        "execution_timeframe_value": spec.timeframes.execution.value,
        "execution_timeframe_unit": spec.timeframes.execution.unit.value,
    }


def _inspect_checkpoint(
    path: Path,
    *,
    fingerprint: Mapping[str, object],
    symbol: str,
) -> tuple[str, StrategyRunResultV2 | SymbolRunFailureV2 | None]:
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise StrategyMulticoinError(
            f"corrupt checkpoint for {symbol}: {type(exc).__name__}"
        ) from exc
    if type(data) is not dict:
        raise StrategyMulticoinError(f"corrupt checkpoint for {symbol}: not an object")
    allowed_keys = set(fingerprint) | {"symbol", "status", "result", "failure"}
    extra = set(data) - allowed_keys
    if extra:
        raise StrategyMulticoinError(
            f"corrupt checkpoint for {symbol}: unknown fields "
            f"{tuple(sorted(extra))}"
        )
    for key, expected in fingerprint.items():
        if key not in data:
            raise StrategyMulticoinError(
                f"corrupt checkpoint for {symbol}: missing field {key!r}"
            )
        if data[key] != expected:
            raise StrategyMulticoinError(
                f"incompatible checkpoint for {symbol}: "
                f"{key} expected {expected!r}, got {data[key]!r}"
            )
    if data.get("symbol") != symbol:
        raise StrategyMulticoinError(
            f"incompatible checkpoint symbol: expected {symbol!r}, "
            f"got {data.get('symbol')!r}"
        )
    status = data.get("status")
    try:
        if status == "complete":
            if "result" not in data:
                raise StrategyMulticoinError(
                    f"corrupt checkpoint for {symbol}: missing result"
                )
            result = _result_from_json(data["result"])
            return "use_success", result
        if status == "failed":
            if "failure" not in data:
                raise StrategyMulticoinError(
                    f"corrupt checkpoint for {symbol}: missing failure"
                )
            failure = _failure_from_json(data["failure"])
            return "use_failure", failure
    except StrategyMulticoinError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise StrategyMulticoinError(
            f"corrupt checkpoint for {symbol}: {type(exc).__name__}"
        ) from exc
    raise StrategyMulticoinError(
        f"corrupt checkpoint for {symbol}: unknown status {status!r}"
    )


def _write_checkpoint(
    path: Path,
    *,
    fingerprint: Mapping[str, object],
    symbol: str,
    status: str,
    result: StrategyRunResultV2 | None = None,
    failure: SymbolRunFailureV2 | None = None,
) -> None:
    payload: dict[str, object] = {
        **fingerprint,
        "symbol": symbol,
        "status": status,
    }
    if status == "complete":
        if result is None:
            raise StrategyMulticoinError("complete checkpoint requires result")
        payload["result"] = _result_to_json(result)
    elif status == "failed":
        if failure is None:
            raise StrategyMulticoinError("failed checkpoint requires failure")
        payload["failure"] = _failure_to_json(failure)
    else:
        raise StrategyMulticoinError(f"unknown checkpoint status: {status!r}")

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, indent=2)
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _failure_to_json(failure: SymbolRunFailureV2) -> dict[str, str]:
    return {
        "symbol": failure.symbol,
        "error_type": failure.error_type,
        "message": failure.message,
    }


def _failure_from_json(data: object) -> SymbolRunFailureV2:
    if type(data) is not dict:
        raise StrategyMulticoinError("failure payload must be an object")
    return SymbolRunFailureV2(
        symbol=str(data["symbol"]),
        error_type=str(data["error_type"]),
        message=str(data["message"]),
    )


def _result_to_json(result: StrategyRunResultV2) -> dict[str, object]:
    return {
        "strategy_hash": result.strategy_hash,
        "plugin_id": result.plugin_id.value,
        "plugin_contract_version": result.plugin_contract_version.value,
        "universe": {
            "universe_id": result.universe.universe_id.value,
            "version": result.universe.version,
            "content_hash": result.universe.content_hash,
        },
        "start": _dt_to_iso(result.start),
        "end": _dt_to_iso(result.end),
        "symbols": list(result.symbols),
        "signal_timeframe": _tf_to_json(result.signal_timeframe),
        "execution_timeframe": _tf_to_json(result.execution_timeframe),
        "roundtrip_cost": {
            "value": str(result.roundtrip_cost.value),
            "unit": result.roundtrip_cost.unit.value,
        },
        "slippage_status": result.slippage_status.value,
        "funding_status": result.funding_status.value,
        "status": result.status.value,
        "candidate_count": result.candidate_count,
        "trades": [_trade_to_json(t) for t in result.trades],
    }


def _result_from_json(data: object) -> StrategyRunResultV2:
    if type(data) is not dict:
        raise StrategyMulticoinError("result payload must be an object")
    uni = data["universe"]
    if type(uni) is not dict:
        raise StrategyMulticoinError("result.universe must be an object")
    trades_raw = data["trades"]
    if type(trades_raw) is not list:
        raise StrategyMulticoinError("result.trades must be a list")
    return StrategyRunResultV2(
        strategy_hash=str(data["strategy_hash"]),
        plugin_id=StableIdentifier(value=str(data["plugin_id"])),
        plugin_contract_version=ContractVersion(
            value=str(data["plugin_contract_version"])
        ),
        universe=VersionedUniverseRefV2(
            universe_id=StableIdentifier(value=str(uni["universe_id"])),
            version=str(uni["version"]),
            content_hash=str(uni["content_hash"]),
        ),
        start=_dt_from_iso(str(data["start"])),
        end=_dt_from_iso(str(data["end"])),
        symbols=tuple(str(s) for s in data["symbols"]),
        signal_timeframe=_tf_from_json(data["signal_timeframe"]),
        execution_timeframe=_tf_from_json(data["execution_timeframe"]),
        roundtrip_cost=RateValue(
            value=Decimal(str(data["roundtrip_cost"]["value"])),
            unit=RateUnit(str(data["roundtrip_cost"]["unit"])),
        ),
        slippage_status=ModelingStatus(str(data["slippage_status"])),
        funding_status=ModelingStatus(str(data["funding_status"])),
        status=StrategyRunStatusV2(str(data["status"])),
        candidate_count=int(data["candidate_count"]),
        trades=tuple(_trade_from_json(t) for t in trades_raw),
    )


def _trade_to_json(trade: StrategyTradeV2) -> dict[str, object]:
    return {
        "source_event_id": trade.source_event_id.value,
        "symbol": trade.symbol,
        "side": trade.side.value,
        "decision_time": _dt_to_iso(trade.decision_time),
        "entry_time": _dt_to_iso(trade.entry_time),
        "entry_price": str(trade.entry_price),
        "exit_time": None if trade.exit_time is None else _dt_to_iso(trade.exit_time),
        "exit_price": None if trade.exit_price is None else str(trade.exit_price),
        "exit_reason": trade.exit_reason.value,
        "gross_return_pct": _dec_or_none(trade.gross_return_pct),
        "roundtrip_cost_pct": str(trade.roundtrip_cost_pct),
        "net_return_pct": _dec_or_none(trade.net_return_pct),
        "gross_pnl_usdt": _dec_or_none(trade.gross_pnl_usdt),
        "costs_usdt": _dec_or_none(trade.costs_usdt),
        "net_pnl_usdt": _dec_or_none(trade.net_pnl_usdt),
        "mode_id": None if trade.mode_id is None else trade.mode_id.value,
        "confirmation_policy": None
        if trade.confirmation_policy is None
        else trade.confirmation_policy.value,
    }


def _trade_from_json(data: object) -> StrategyTradeV2:
    if type(data) is not dict:
        raise StrategyMulticoinError("trade payload must be an object")
    mode_raw = data.get("mode_id")
    policy_raw = data.get("confirmation_policy")
    return StrategyTradeV2(
        source_event_id=SourceEventIdV2(value=str(data["source_event_id"])),
        symbol=str(data["symbol"]),
        side=SideName(str(data["side"])),
        decision_time=_dt_from_iso(str(data["decision_time"])),
        entry_time=_dt_from_iso(str(data["entry_time"])),
        entry_price=Decimal(str(data["entry_price"])),
        exit_time=None
        if data.get("exit_time") is None
        else _dt_from_iso(str(data["exit_time"])),
        exit_price=None
        if data.get("exit_price") is None
        else Decimal(str(data["exit_price"])),
        exit_reason=TradeExitReasonV2(str(data["exit_reason"])),
        gross_return_pct=_dec_from_json(data.get("gross_return_pct")),
        roundtrip_cost_pct=Decimal(str(data["roundtrip_cost_pct"])),
        net_return_pct=_dec_from_json(data.get("net_return_pct")),
        gross_pnl_usdt=_dec_from_json(data.get("gross_pnl_usdt")),
        costs_usdt=_dec_from_json(data.get("costs_usdt")),
        net_pnl_usdt=_dec_from_json(data.get("net_pnl_usdt")),
        mode_id=None if mode_raw is None else StableIdentifier(value=str(mode_raw)),
        confirmation_policy=None
        if policy_raw is None
        else ResearchConfirmationPolicyV2(str(policy_raw)),
    )


def _tf_to_json(tf: TimeframeValue) -> dict[str, object]:
    return {"value": tf.value, "unit": tf.unit.value}


def _tf_from_json(data: object) -> TimeframeValue:
    if type(data) is not dict:
        raise StrategyMulticoinError("timeframe payload must be an object")
    return TimeframeValue(
        value=int(data["value"]),
        unit=TimeframeUnit(str(data["unit"])),
    )


def _dec_or_none(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value)


def _dec_from_json(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _dt_to_iso(value: datetime) -> str:
    # Caller already required UTC; do not rewrite tz via astimezone.
    return value.isoformat()


def _dt_from_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _require_utc(parsed, field_name="datetime")


def _require_utc(value: datetime, *, field_name: str) -> datetime:
    if type(value) is not datetime:
        raise StrategyMulticoinError(f"{field_name} must be datetime")
    if value.tzinfo is None:
        raise StrategyMulticoinError(f"{field_name} must be timezone-aware UTC")
    offset = value.utcoffset()
    if offset is None or offset != _ZERO:
        raise StrategyMulticoinError(f"{field_name} must be UTC (zero offset)")
    return value


def _sanitize_message(exc: BaseException) -> str:
    text = str(exc)
    text = _ADDR_RE.sub("", text)
    # Drop angle-bracket object reprs that may embed ids.
    text = re.sub(r"<[^>]*object at [^>]*>", "<object>", text)
    if not text:
        text = type(exc).__name__
    return text[:2000]

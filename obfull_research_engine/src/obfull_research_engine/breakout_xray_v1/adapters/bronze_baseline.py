"""Bronze baseline replay using FullBookState + Silver book_map_sha256 semantics."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Sequence

from orderbook_analyse.orderbook_v2_live.full_book_state import FullBookState
from orderbook_analyse.orderbook_v2_live.full_ob_sync import DeltaOutcome

from ..execution_hold import DEFAULT_SILVER_LOCK, ExecutionSentinel, assert_full_run_allowed
from ..models import BaselineBookState
from ..resource_preflight import assert_swap_growth_within_tolerance
from ..time_windows import ns_to_dt
from obfull_research_engine.clickhouse_research_store_v1.silver_replay import (
    book_map_sha256,
)

BRONZE_BOUND_APPLY_KEYS = "apply_keys"
BRONZE_BOUND_CHAIN_INDEX_FALLBACK = "chain_index_fallback"
BASELINE_VALIDATION_REQUIRES_LIVE_PARITY = "REQUIRES_LIVE_PARITY_CONFIRMATION"


@dataclass(frozen=True)
class BronzeRecordFull:
    """Fixture/live-normalized bronze row for offline FullBook replay."""

    canonical_segment_chain_index: int
    record_ordinal: int
    event_time_ns: int
    message_type: str  # snapshot | checkpoint | delta | gap_marker
    bids: list[Any] = field(default_factory=list)
    asks: list[Any] = field(default_factory=list)
    u: int | None = None
    seq: int | None = None
    gap: bool = False


@dataclass(frozen=True)
class BronzeRecordLite:
    """Legacy single-level fixture record (promoted to FullBook path)."""

    canonical_segment_chain_index: int
    record_ordinal: int
    event_time_ns: int
    message_type: str
    bids: list[Any] | None = None
    asks: list[Any] | None = None
    side: str | None = None
    price: float | None = None
    size: float | None = None
    u: int | None = None
    seq: int | None = None
    gap: bool = False


def sort_key(rec: BronzeRecordFull) -> tuple[int, int]:
    return (int(rec.canonical_segment_chain_index), int(rec.record_ordinal))


def promote_record(rec: Any) -> BronzeRecordFull:
    if isinstance(rec, BronzeRecordFull):
        return rec
    if getattr(rec, "gap", False) or getattr(rec, "message_type", "") == "gap_marker":
        return BronzeRecordFull(
            int(rec.canonical_segment_chain_index),
            int(rec.record_ordinal),
            int(rec.event_time_ns),
            "gap_marker",
            gap=True,
        )
    mt = str(rec.message_type)
    if mt in {"snapshot", "checkpoint"}:
        return BronzeRecordFull(
            int(rec.canonical_segment_chain_index),
            int(rec.record_ordinal),
            int(rec.event_time_ns),
            mt,
            bids=list(getattr(rec, "bids", None) or []),
            asks=list(getattr(rec, "asks", None) or []),
            u=getattr(rec, "u", None) or 1,
            seq=getattr(rec, "seq", None) or 1,
        )
    bids, asks = list(getattr(rec, "bids", None) or []), list(
        getattr(rec, "asks", None) or []
    )
    if not bids and not asks:
        side = getattr(rec, "side", None)
        price = getattr(rec, "price", None)
        size = getattr(rec, "size", None)
        if side == "bid" and price is not None:
            bids = [[float(price), float(size or 0.0)]]
        if side == "ask" and price is not None:
            asks = [[float(price), float(size or 0.0)]]
    return BronzeRecordFull(
        int(rec.canonical_segment_chain_index),
        int(rec.record_ordinal),
        int(rec.event_time_ns),
        "delta" if mt == "delta" else mt,
        bids=bids,
        asks=asks,
        u=getattr(rec, "u", None),
        seq=getattr(rec, "seq", None),
    )


def replay_baseline_fullbook(
    *,
    symbol: str,
    records: Sequence[BronzeRecordFull],
    start_ns: int,
    expected_book_hash: str | None = None,
    enforce_continuity: bool = True,
) -> BaselineBookState:
    """Apply snapshot then deltas with event_time < start_ns."""
    ordered = sorted(records, key=sort_key)
    state = FullBookState(symbol=symbol.upper())
    last_ns: int | None = None
    saw_anchor = False
    gap_seen = False
    next_u: int | None = None

    for rec in ordered:
        if rec.gap or rec.message_type == "gap_marker":
            gap_seen = True
            continue
        if int(rec.event_time_ns) >= int(start_ns):
            break
        if rec.message_type in {"snapshot", "checkpoint"}:
            if not rec.bids or not rec.asks:
                return BaselineBookState(
                    source="bronze_replay_from_epoch_anchor",
                    timestamp=None,
                    age_ms=None,
                    complete=False,
                    hash=None,
                    unresolved_reason="UNRESOLVED_BASELINE",
                )
            u = rec.u if rec.u is not None else 1
            state.apply_snapshot(
                bids=list(rec.bids),
                asks=list(rec.asks),
                u=u,
                seq=rec.seq,
                ts_ms=int(rec.event_time_ns // 1_000_000),
                mark_ready=True,
            )
            next_u = int(u) + 1
            saw_anchor = True
            last_ns = int(rec.event_time_ns)
            continue
        if not saw_anchor:
            continue
        if rec.message_type != "delta":
            continue
        u = rec.u if rec.u is not None else next_u
        outcome = state.apply_delta(
            bids=list(rec.bids or []),
            asks=list(rec.asks or []),
            u=u,
            seq=rec.seq,
            ts_ms=int(rec.event_time_ns // 1_000_000),
            enforce_continuity=enforce_continuity,
        )
        if enforce_continuity and outcome is not DeltaOutcome.APPLIED:
            return BaselineBookState(
                source="bronze_replay_from_epoch_anchor",
                timestamp=ns_to_dt(int(rec.event_time_ns)),
                age_ms=(start_ns - int(rec.event_time_ns)) / 1e6,
                complete=False,
                hash=None,
                unresolved_reason="UNRESOLVED_BASELINE",
            )
        if u is not None:
            next_u = int(u) + 1
        last_ns = int(rec.event_time_ns)

    if gap_seen or not saw_anchor or last_ns is None:
        return BaselineBookState(
            source="bronze_replay_from_epoch_anchor",
            timestamp=None if last_ns is None else ns_to_dt(last_ns),
            age_ms=None if last_ns is None else (start_ns - last_ns) / 1e6,
            complete=False,
            hash=None if not saw_anchor else book_map_sha256(state.bids, state.asks),
            unresolved_reason="UNRESOLVED_BASELINE",
        )

    digest = book_map_sha256(state.bids, state.asks)
    if expected_book_hash is not None and digest != expected_book_hash:
        return BaselineBookState(
            source="bronze_replay_from_epoch_anchor",
            timestamp=ns_to_dt(last_ns),
            age_ms=(start_ns - last_ns) / 1e6,
            complete=False,
            hash=digest,
            unresolved_reason="UNRESOLVED_BASELINE_HASH_MISMATCH",
        )

    levels = tuple(
        {"side": "bid", "price": p, "size": s} for p, s in sorted(state.bids.items())
    ) + tuple(
        {"side": "ask", "price": p, "size": s} for p, s in sorted(state.asks.items())
    )
    return BaselineBookState(
        source="bronze_replay_from_epoch_anchor",
        timestamp=ns_to_dt(last_ns),
        age_ms=(start_ns - last_ns) / 1e6,
        complete=True,
        hash=digest,
        unresolved_reason=None,
        levels_in_band=levels,
    )


class FixtureBaselineBookRepository:
    def __init__(
        self,
        records: Sequence[Any],
        *,
        enforce_continuity: bool = True,
        symbol_default: str = "BTCUSDT",
    ):
        self.records = [promote_record(r) for r in records]
        self.enforce_continuity = enforce_continuity
        self.symbol_default = symbol_default

    def load_baseline(
        self,
        *,
        symbol: str,
        start_ns: int,
        epoch_id: str,
        expected_book_hash: str | None,
    ) -> BaselineBookState:
        return replay_baseline_fullbook(
            symbol=symbol or self.symbol_default,
            records=self.records,
            start_ns=start_ns,
            expected_book_hash=expected_book_hash,
            enforce_continuity=self.enforce_continuity,
        )


class LiveBaselineBookRepository:
    """Production Bronze baseline loader behind dual execution gates.

    Silver ``book_hash`` on a 100ms metric row is the FullBookState digest at
    **bucket end** (emitted in ``emit_due_buckets`` when ``end_ns <= next_event``,
    before applying that event). Baseline at analysis ``start_ns`` is the warm-up
    state after all bronze events with ``event_time < start_ns`` — identical to
    Silver warm-up end, and **not** equal to the first in-window bucket hash
    (which includes updates inside ``[start_ns, start_ns+100ms)``).
    """

    STOP_BRONZE_MAX_RECORDS = "STOP_BRONZE_MAX_RECORDS"
    STOP_BRONZE_MAX_DURATION = "STOP_BRONZE_MAX_DURATION"
    STOP_BRONZE_RESOURCE_GUARD = "STOP_BRONZE_RESOURCE_GUARD"

    def __init__(
        self,
        client: Any | None = None,
        *,
        build_config: Any | None = None,
        lock_path=DEFAULT_SILVER_LOCK,
        require_lock_gate: bool = True,
        max_records: int = 2_000_000,
        max_runtime_s: float = 120.0,
        min_available_ram_bytes: int = 6 * 1024**3,
        record_source: Any | None = None,
        epochs: Sequence[Any] | None = None,
        sentinel: ExecutionSentinel | None = None,
        swap_baseline_bytes: int | None = None,
        max_additional_swap_bytes: int | None = 512 * 1024**2,
        batch_size: int = 50_000,
    ):
        self.client = client
        self.build_config = build_config
        self.lock_path = lock_path
        self.require_lock_gate = require_lock_gate
        self.max_records = max_records
        self.max_runtime_s = max_runtime_s
        self.min_available_ram_bytes = min_available_ram_bytes
        self.record_source = record_source
        self.epochs = list(epochs or [])
        self.sentinel = sentinel
        self.swap_baseline_bytes = swap_baseline_bytes
        self.max_additional_swap_bytes = max_additional_swap_bytes
        self.batch_size = batch_size
        self.calls: list[str] = []
        self.last_error: str | None = None
        self.last_failure_class: str | None = None
        self.last_bronze_bound_mode: str | None = None

    def _gate(self, stage: str) -> None:
        if self.sentinel is not None:
            self.sentinel.check(stage)
        elif self.require_lock_gate:
            assert_full_run_allowed(self.lock_path)

    def load_baseline(
        self,
        *,
        symbol: str,
        start_ns: int,
        epoch_id: str,
        expected_book_hash: str | None,
    ) -> BaselineBookState:
        self._gate("bronze_baseline_start")
        self.calls.append("load_baseline")
        self.last_error = None
        self.last_failure_class = None
        self.last_bronze_bound_mode = None

        try:
            if self.record_source is None:
                self._resource_guard_preflight()
            epoch = self._resolve_epoch(epoch_id=epoch_id, start_ns=start_ns)
            if epoch is None:
                self.last_error = "epoch_unresolved"
                self.last_failure_class = "EXPECTED_DATA_QUALITY_FAILURE"
                return BaselineBookState(
                    source="bronze_replay_from_epoch_anchor",
                    timestamp=None,
                    age_ms=None,
                    complete=False,
                    hash=None,
                    unresolved_reason="UNRESOLVED_BASELINE",
                )

            records = self._load_records(epoch=epoch, start_ns=int(start_ns))
            full = [_bronze_record_to_full(r) for r in records]
            state = replay_baseline_fullbook(
                symbol=symbol,
                records=full,
                start_ns=int(start_ns),
                expected_book_hash=expected_book_hash,
                enforce_continuity=True,
            )
            return self._attach_bound_provenance(state)
        except RuntimeError as exc:
            msg = str(exc)
            self.last_error = f"{type(exc).__name__}:{msg}"
            code = msg.split(":", 1)[0]
            if code.startswith("STOP_") or code.startswith("UNRESOLVED_"):
                self.last_failure_class = "EXPECTED_DATA_QUALITY_FAILURE"
                return self._attach_bound_provenance(
                    BaselineBookState(
                        source="bronze_replay_from_epoch_anchor",
                        timestamp=None,
                        age_ms=None,
                        complete=False,
                        hash=None,
                        unresolved_reason=code,
                    )
                )
            self.last_failure_class = "INTERNAL_ANALYSIS_ERROR"
            return self._attach_bound_provenance(
                BaselineBookState(
                    source="bronze_replay_from_epoch_anchor",
                    timestamp=None,
                    age_ms=None,
                    complete=False,
                    hash=None,
                    unresolved_reason="INTERNAL_ANALYSIS_ERROR",
                )
            )
        except Exception as exc:  # noqa: BLE001 — outer fail-closed
            from ..errors import sanitize_error_message

            self.last_error = f"{type(exc).__name__}:{sanitize_error_message(exc)}"
            self.last_failure_class = "INTERNAL_ANALYSIS_ERROR"
            return self._attach_bound_provenance(
                BaselineBookState(
                    source="bronze_replay_from_epoch_anchor",
                    timestamp=None,
                    age_ms=None,
                    complete=False,
                    hash=None,
                    unresolved_reason="INTERNAL_ANALYSIS_ERROR",
                )
            )

    def _attach_bound_provenance(self, state: BaselineBookState) -> BaselineBookState:
        mode = self.last_bronze_bound_mode
        if mode is None:
            return state
        validation = (
            BASELINE_VALIDATION_REQUIRES_LIVE_PARITY
            if mode == BRONZE_BOUND_CHAIN_INDEX_FALLBACK
            else None
        )
        return replace(
            state,
            bronze_bound_mode=mode,
            baseline_validation_status=validation,
        )

    def _resolve_epoch(self, *, epoch_id: str, start_ns: int) -> Any | None:
        epochs = list(self.epochs)
        if not epochs and self.client is not None and self.build_config is not None:
            from obfull_research_engine.clickhouse_research_store_v1.analysis_readiness_v1_3 import (
                load_persisted_epochs,
            )

            self._gate("bronze_load_epochs")
            self.calls.append("load_persisted_epochs")
            epochs = load_persisted_epochs(self.client, self.build_config)
            self.epochs = list(epochs)
        for ep in epochs:
            if epoch_id and getattr(ep, "epoch_id", None) == epoch_id:
                return ep
            if (
                int(getattr(ep, "safe_start_ns", -1))
                <= int(start_ns)
                < int(getattr(ep, "safe_end_ns", -1))
            ):
                return ep
        return None

    def _load_records(self, *, epoch: Any, start_ns: int) -> list[Any]:
        import time

        if self.record_source is not None:
            self.calls.append("record_source")
            out = []
            for r in self.record_source(epoch=epoch, start_ns=start_ns):
                out.append(r)
                if len(out) > self.max_records:
                    raise RuntimeError(self.STOP_BRONZE_MAX_RECORDS)
            return out

        if self.client is None or self.build_config is None:
            raise RuntimeError("DATA_SOURCE_UNAVAILABLE:bronze_client_or_config")

        self._gate("bronze_stream_start")
        self.calls.append("iter_bronze_records_xray")
        start_key = (
            int(epoch.anchor_segment_chain_index),
            int(epoch.anchor_record_ordinal),
        )
        end_key = None
        if getattr(epoch, "apply_end_segment_chain_index", None) is not None:
            end_key = (
                int(epoch.apply_end_segment_chain_index),
                int(epoch.apply_end_record_ordinal or 0),
            )
        if end_key is not None:
            self.last_bronze_bound_mode = BRONZE_BOUND_APPLY_KEYS
            stream_start_key: tuple[int, int] | None = start_key
            stream_end_key: tuple[int, int] | None = end_key
        else:
            self.last_bronze_bound_mode = BRONZE_BOUND_CHAIN_INDEX_FALLBACK
            stream_start_key = None
            stream_end_key = None
        t0 = time.monotonic()
        out: list[Any] = []
        stream = iter_bronze_records_xray(
            self.client,
            self.build_config,
            start_apply_key=stream_start_key,
            end_apply_key=stream_end_key,
            event_time_ns_to=int(start_ns) - 1,
        )
        try:
            for rec in stream:
                if time.monotonic() - t0 > self.max_runtime_s:
                    raise RuntimeError(self.STOP_BRONZE_MAX_DURATION)
                if int(rec.event_time_ns) >= int(start_ns):
                    break
                if (
                    int(rec.canonical_segment_chain_index),
                    int(rec.record_ordinal),
                ) < start_key:
                    continue
                out.append(rec)
                if len(out) > self.max_records:
                    raise RuntimeError(self.STOP_BRONZE_MAX_RECORDS)
                if len(out) % self.batch_size == 0:
                    self._gate("bronze_stream_batch")
                    self._resource_guard_preflight()
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
        return out

    def _resource_guard_preflight(self) -> None:
        from obfull_research_engine.clickhouse_research_store_v1.silver_full_build_v1_3 import (
            _available_memory_bytes,
        )

        try:
            available = int(_available_memory_bytes())
            if available < int(self.min_available_ram_bytes):
                raise RuntimeError(
                    f"{self.STOP_BRONZE_RESOURCE_GUARD}:ram_available={available}"
                    f"<min={self.min_available_ram_bytes}"
                )
            if (
                self.swap_baseline_bytes is not None
                and self.max_additional_swap_bytes is not None
            ):
                assert_swap_growth_within_tolerance(
                    baseline_swap_used_bytes=int(self.swap_baseline_bytes),
                    max_additional_swap_bytes=int(self.max_additional_swap_bytes),
                )
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"{self.STOP_BRONZE_RESOURCE_GUARD}:{type(exc).__name__}"
            ) from exc


XRAY_BRONZE_STREAM_SETTINGS = {
    "readonly": 1,
    "max_threads": 1,
    "max_memory_usage": 1_610_612_736,
    "max_execution_time": 120,
    "max_result_rows": 2_000_000,
    "max_block_size": 8192,
}


def bronze_stream_sql(*, input_database: str, chain_version: str) -> str:
    from obfull_research_engine.clickhouse_research_store_v1.silver_full_build_v1_3 import (
        BRONZE_EVENTS_TABLE,
        validate_input_database,
    )

    validate_input_database(input_database)
    # Fixed SELECT template — no SELECT *
    return f"""
SELECT
  e.record_id, e.symbol, e.message_type, e.event_time_ns, e.receive_time_ns,
  e.update_id, e.seq, e.update_id_present, e.seq_present,
  e.source_segment_sha256, e.record_ordinal, e.payload_sha256,
  e.payload,
  e.canonical_segment_chain_index
FROM {input_database}.{BRONZE_EVENTS_TABLE} AS e FINAL
WHERE e.symbol = {{symbol:String}}
  AND e.chain_version = {{chain_version:String}}
  AND (e.canonical_segment_chain_index, e.record_ordinal) >=
      ({{start_rank:UInt64}}, {{start_ordinal:UInt64}})
  AND (e.canonical_segment_chain_index, e.record_ordinal) <
      ({{end_rank:UInt64}}, {{end_ordinal:UInt64}})
  AND e.event_time_ns <= {{event_time_ns_to:UInt64}}
ORDER BY e.canonical_segment_chain_index, e.record_ordinal
""".strip()


def iter_bronze_records_xray(
    client: Any,
    build_config: Any,
    *,
    start_apply_key: tuple[int, int] | None,
    end_apply_key: tuple[int, int] | None,
    event_time_ns_to: int,
):
    """Bounded bronze stream with explicit analysis safety settings."""
    from obfull_research_engine.clickhouse_research_store_v1.silver_full_build_v1_3 import (
        BRONZE_EVENTS_TABLE,
        validate_input_database,
    )
    from obfull_research_engine.clickhouse_research_store_v1.silver_replay import (
        bronze_row_from_ch,
    )

    validate_input_database(build_config.input_database)
    if start_apply_key is None or end_apply_key is None:
        # Fall back to chain-index window from build config with event-time bound.
        sql = f"""
SELECT
  e.record_id, e.symbol, e.message_type, e.event_time_ns, e.receive_time_ns,
  e.update_id, e.seq, e.update_id_present, e.seq_present,
  e.source_segment_sha256, e.record_ordinal, e.payload_sha256,
  e.payload,
  e.canonical_segment_chain_index
FROM {build_config.input_database}.{BRONZE_EVENTS_TABLE} AS e FINAL
WHERE e.symbol = {{symbol:String}}
  AND e.chain_version = {{chain_version:String}}
  AND e.canonical_segment_chain_index >= {{start_index:UInt64}}
  AND e.canonical_segment_chain_index <= {{end_index:UInt64}}
  AND e.event_time_ns <= {{event_time_ns_to:UInt64}}
ORDER BY e.canonical_segment_chain_index, e.record_ordinal
""".strip()
        parameters = {
            "symbol": build_config.symbol,
            "chain_version": build_config.chain_version,
            "start_index": int(build_config.start_chain_index),
            "end_index": int(build_config.end_chain_index),
            "event_time_ns_to": int(event_time_ns_to),
        }
    else:
        sql = bronze_stream_sql(
            input_database=build_config.input_database,
            chain_version=build_config.chain_version,
        )
        parameters = {
            "symbol": build_config.symbol,
            "chain_version": build_config.chain_version,
            "start_rank": int(start_apply_key[0]),
            "start_ordinal": int(start_apply_key[1]),
            "end_rank": int(end_apply_key[0]),
            "end_ordinal": int(end_apply_key[1]),
            "event_time_ns_to": int(event_time_ns_to),
        }

    stream_ctx = client.query_row_block_stream(
        sql,
        parameters=parameters,
        settings=dict(XRAY_BRONZE_STREAM_SETTINGS),
    )
    stream = stream_ctx.__enter__()

    def _gen():
        try:
            for block in stream:
                for row in block:
                    yield bronze_row_from_ch(tuple(row))
        finally:
            stream_ctx.__exit__(None, None, None)

    return _gen()



def _bronze_record_to_full(rec: Any) -> BronzeRecordFull:
    """Map silver_replay.BronzeRecord (or compatible) into fixture Full record."""
    if isinstance(rec, BronzeRecordFull):
        return rec
    from obfull_research_engine.clickhouse_research_store_v1.silver_replay import (
        ensure_original_payload,
    )

    mt = str(getattr(rec, "message_type", "delta"))
    if mt == "gap_marker" or getattr(rec, "gap", False):
        return BronzeRecordFull(
            int(rec.canonical_segment_chain_index),
            int(rec.record_ordinal),
            int(rec.event_time_ns),
            "gap_marker",
            gap=True,
        )
    payload = ensure_original_payload(rec) if hasattr(rec, "payload") else {}
    if mt in {"snapshot", "checkpoint"}:
        if mt == "checkpoint":
            bids, asks = payload.get("bids") or [], payload.get("asks") or []
            u, seq = payload.get("u"), payload.get("seq")
        else:
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            bids, asks = data.get("b") or [], data.get("a") or []
            u, seq = data.get("u"), data.get("seq")
        return BronzeRecordFull(
            int(rec.canonical_segment_chain_index),
            int(rec.record_ordinal),
            int(rec.event_time_ns),
            mt,
            bids=list(bids),
            asks=list(asks),
            u=None if u is None else int(u),
            seq=None if seq is None else int(seq),
        )
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    bids, asks = data.get("b") or [], data.get("a") or []
    u = data.get("u", getattr(rec, "update_id", None))
    seq = data.get("seq", getattr(rec, "seq", None))
    return BronzeRecordFull(
        int(rec.canonical_segment_chain_index),
        int(rec.record_ordinal),
        int(rec.event_time_ns),
        "delta",
        bids=list(bids or []),
        asks=list(asks or []),
        u=None if u is None else int(u),
        seq=None if seq is None else int(seq),
    )


def replay_baseline_from_records(
    *,
    records: Sequence[Any],
    start_ns: int,
    expected_book_hash: str | None = None,
    hash_fn=None,
    symbol: str = "BTCUSDT",
    enforce_continuity: bool = False,
) -> BaselineBookState:
    """Public helper; legacy fixtures often omit continuous ``u`` so default off."""
    full = [promote_record(r) for r in records]
    # Auto-assign contiguous u when missing (legacy lite fixtures)
    u = 1
    fixed: list[BronzeRecordFull] = []
    for r in full:
        if r.message_type in {"snapshot", "checkpoint"}:
            uu = r.u if r.u is not None else u
            fixed.append(
                BronzeRecordFull(
                    r.canonical_segment_chain_index,
                    r.record_ordinal,
                    r.event_time_ns,
                    r.message_type,
                    bids=r.bids,
                    asks=r.asks,
                    u=uu,
                    seq=r.seq or 1,
                )
            )
            u = int(uu) + 1
        elif r.message_type == "delta":
            uu = r.u if r.u is not None else u
            fixed.append(
                BronzeRecordFull(
                    r.canonical_segment_chain_index,
                    r.record_ordinal,
                    r.event_time_ns,
                    "delta",
                    bids=r.bids,
                    asks=r.asks,
                    u=uu,
                    seq=r.seq,
                )
            )
            u = int(uu) + 1
        else:
            fixed.append(r)
    return replay_baseline_fullbook(
        symbol=symbol,
        records=fixed,
        start_ns=start_ns,
        expected_book_hash=expected_book_hash,
        enforce_continuity=enforce_continuity,
    )

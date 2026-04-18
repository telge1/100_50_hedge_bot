#!/usr/bin/env python3
import argparse
import ast
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

RUNTIME_PATTERN = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) (?P<level>[A-Z]+) (?P<logger>[^\s]+) (?P<body>.*)"
)
START_EVENTS = {"fixed_cycle_start", "runtime_bootstrap", "websocket_started"}
GAP_THRESHOLD_SECONDS = 60
LOG_TIMEZONE = timezone(timedelta(hours=3))
TIMELINE_SKIP_EVENTS = {
    "snapshot_refreshed",
    "strategy_noop",
}
BLOCK_START_EVENT = "analyzer_block_started"
BLOCK_RECOVERY_EVENT = "analyzer_recovery_detected"
BLOCK_EXIT_ARMED_EVENT = "analyzer_exit_armed"
BLOCK_CLOSED_EVENT = "analyzer_block_closed"

MARKER_EVENTS = {
    BLOCK_START_EVENT,
    BLOCK_RECOVERY_EVENT,
    BLOCK_EXIT_ARMED_EVENT,
    BLOCK_CLOSED_EVENT,
}

CALCULATION_EVENT_NAMES = {
    "fixed_cycle_break_even_inputs",
    "fixed_cycle_tp_components",
    "fixed_cycle_short_tp_pair_planned",
    "fixed_cycle_exit_manifest",
    "fixed_cycle_structure_rebuilt",
    "fixed_cycle_downside_build_result",
}

CHAIN_STATE_KEYS = {
    "bot_state",
    "cycle_index",
    "current_long_cycle_index",
    "current_short_cycle_index",
    "current_effective_cycle",
    "cycle_waiting_for_short_tp",
    "short_tp_pending_cycle",
    "entry_reference_price",
    "long_qty",
    "short_qty",
}

REASON_CODES = {
    "MISSING_TRIGGER",
    "MISSING_CALCULATION",
    "MISSING_ORDER_SUBMIT",
    "MISSING_RUNTIME_LINK",
    "MISSING_AUDIT_LINK",
    "TP_MISMATCH",
    "REPLACEMENT_NOT_CLOSED",
    "PARTIAL_LIFECYCLE",
    "MISSING_RESULT",
    "WAITING_FOR_FILL",
}

ORDER_CHAIN_EVENT_PRIORITY = [
    "fixed_cycle_long_reduce_planned",
    "intent_submit_started",
    "order_submitted",
    "order_reconciled_open",
]

ORDER_CHAIN_CALC_EVENTS = {
    "fixed_cycle_long_reduce_planned",
    "fixed_cycle_break_even_inputs",
    "fixed_cycle_tp_components",
    "fixed_cycle_structure_rebuilt",
    "analyzer_exit_armed",
    "fixed_cycle_exit_manifest",
    "exit_trigger_clamp",
    "exit_trigger_result",
}


class ChainType(str, Enum):
    LONG_ADD_ORDER_CHAIN = "LONG_ADD_ORDER_CHAIN"
    LONG_ADD_FILL_EFFECT_CHAIN = "LONG_ADD_FILL_EFFECT_CHAIN"

REASON_CODES = {
    "MISSING_TRIGGER",
    "MISSING_CALCULATION",
    "MISSING_ORDER_SUBMIT",
    "MISSING_RUNTIME_LINK",
    "MISSING_AUDIT_LINK",
    "TP_MISMATCH",
    "REPLACEMENT_NOT_CLOSED",
    "PARTIAL_LIFECYCLE",
    "MISSING_RESULT",
}


def _split_event_name_and_tail(message: str) -> Tuple[Optional[str], str]:
    content = message.strip()
    if not content:
        return None, ""
    if " " in content:
        head, tail = content.split(" ", 1)
    else:
        head, tail = content, ""
    return head, tail.strip()



def _parse_runtime_timestamp(raw: str) -> Optional[datetime]:
    try:
        naive = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S,%f")
        return naive.replace(tzinfo=LOG_TIMEZONE)
    except ValueError:
        return None


def _parse_audit_timestamp(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw)
        if ts.tzinfo is None:
            return ts.replace(tzinfo=LOG_TIMEZONE)
        return ts.astimezone(LOG_TIMEZONE)
    except ValueError:
        return None


def _safe_json_load(text: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, SyntaxError):
            return None
        return None


def _extract_order_ids(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    order_link_id = payload.get("order_link_id") or payload.get("client_order_id")
    exchange_order_id = payload.get("exchange_order_id")
    if not exchange_order_id:
        exchange_obj = payload.get("exchange_order") or {}
        exchange_order_id = exchange_obj.get("order_id") or exchange_obj.get("exchange_order_id")
    order = payload.get("order") or {}
    order_link_id = order_link_id or order.get("order_link_id") or order.get("client_order_id")
    exchange_order_id = exchange_order_id or order.get("exchange_order_id")
    fill = payload.get("fill") or {}
    order_link_id = order_link_id or fill.get("client_order_id") or fill.get("order_link_id")
    exchange_order_id = exchange_order_id or fill.get("exchange_order_id")
    return order_link_id, exchange_order_id


def _extract_cycle_index(payload: Dict[str, Any]) -> Optional[int]:
    def _candidate_from(container: Optional[Dict[str, Any]]) -> Optional[Any]:
        if not isinstance(container, dict):
            return None
        candidate = container.get("cycle_index")
        if candidate is not None:
            return candidate
        metadata = container.get("metadata")
        if isinstance(metadata, dict):
            candidate = metadata.get("cycle_index")
            if candidate is not None:
                return candidate
        return None

    candidate = _candidate_from(payload)
    for container_key in ("order", "managed_order", "exchange_order", "fill"):
        if candidate is not None:
            break
        container = payload.get(container_key)
        candidate = _candidate_from(container)
    try:
        return int(candidate) if candidate is not None else None
    except (TypeError, ValueError):
        return None


def _extract_symbol(payload: Dict[str, Any]) -> Optional[str]:
    return payload.get("symbol") or payload.get("order", {}).get("symbol")


def _extract_purpose(payload: Dict[str, Any]) -> Optional[str]:
    return payload.get("purpose") or payload.get("order", {}).get("purpose")


def _extract_status(payload: Dict[str, Any]) -> Optional[str]:
    return payload.get("status") or payload.get("state")


def _extract_level(payload: Dict[str, Any], default: Optional[str]) -> Optional[str]:
    return payload.get("level") or default


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _extract_price_fields(payload: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], Optional[float]]:
    order = payload.get("order") or {}
    metadata = payload.get("metadata") or {}
    exchange_order = payload.get("exchange_order") or {}
    fill = payload.get("fill") or {}
    fill_metadata = fill.get("metadata") or {}

    price = (
        payload.get("price")
        or payload.get("fill_price")
        or payload.get("avgPrice")
        or payload.get("avg_price")
        or order.get("price")
        or order.get("fill_price")
        or exchange_order.get("avgPrice")
        or exchange_order.get("price")
        or fill.get("price")
        or fill.get("exec_price")
    )

    qty = (
        payload.get("qty")
        or payload.get("filled_qty")
        or payload.get("fill_qty")
        or payload.get("cum_exec_qty")
        or order.get("qty")
        or order.get("filled_qty")
        or exchange_order.get("qty")
        or exchange_order.get("cumExecQty")
        or fill.get("exec_qty")
        or fill.get("incremental_qty")
    )

    avg_price = (
        payload.get("avg_price")
        or payload.get("avgPrice")
        or exchange_order.get("avgPrice")
        or order.get("avg_price")
        or order.get("avgPrice")
        or fill.get("avg_price")
        or fill.get("avgPrice")
    )

    trigger_price = (
        payload.get("trigger_price")
        or metadata.get("trigger_price")
        or order.get("trigger_price")
        or fill_metadata.get("trigger_price")
        or fill.get("trigger_price")
    )

    closed_pnl = (
        payload.get("closed_pnl")
        or payload.get("closedPnl")
        or exchange_order.get("closedPnl")
        or metadata.get("closed_pnl")
        or fill_metadata.get("closed_pnl")
        or fill_metadata.get("closedPnl")
    )

    return (
        _safe_float(price),
        _safe_float(qty),
        _safe_float(avg_price),
        _safe_float(trigger_price),
        _safe_float(closed_pnl),
    )

    
@dataclass
class NormalizedEvent:
    timestamp: Optional[datetime]
    source: str
    level: Optional[str]
    logger: Optional[str]
    event_name: Optional[str]
    symbol: Optional[str]
    strategy: Optional[str]
    purpose: Optional[str]
    side: Optional[str]
    status: Optional[str]
    cycle_index: Optional[int]
    order_link_id: Optional[str]
    exchange_order_id: Optional[str]
    bot_state: Optional[str]
    decision: Optional[str]
    reason: Optional[str]
    raw_message: str
    payload: Dict[str, Any]
    sequence: int

    price: Optional[float] = None
    qty: Optional[float] = None
    avg_price: Optional[float] = None
    trigger_price: Optional[float] = None
    closed_pnl: Optional[float] = None
    tp_initial: Optional[float] = None
    tp_final: Optional[float] = None
    tp_steps: Optional[int] = None

    event_id: Optional[int] = None
    logged_fields: Set[str] = field(default_factory=set)
    derived_fields: Dict[str, str] = field(default_factory=dict)
    calculation_name: Optional[str] = None
    calculation_inputs: Dict[str, Any] = field(default_factory=dict)

    def add_derived_field(self, name: str, description: str) -> None:
        self.derived_fields[name] = description


@dataclass
class ChainValidation:
    chain_id: int
    status: str
    issues: List[str] = field(default_factory=list)
    missing_links: List[str] = field(default_factory=list)
    tp_deviation: Optional[float] = None
    missing_runtime: bool = False
    missing_audit: bool = False
    replacement_issues: List[str] = field(default_factory=list)
    reason_codes: List[str] = field(default_factory=list)


@dataclass
class ChainSummary:
    total: int
    complete: int
    broken: int
    partial: int
    mismatch_tp: int
    missing_runtime_links: int
    missing_audit_links: int
    replacement_issues: int
    waiting_for_fill: int


@dataclass
class ReplacementInfo:
    old_order_id: Optional[str]
    new_order_id: Optional[str]
    reason: Optional[str]


@dataclass
class OrderChain:
    key: Tuple[str, ...]
    purpose: Optional[str]
    side: Optional[str]
    submitted_event: Optional[NormalizedEvent]
    lifecycle_events: List[NormalizedEvent] = field(default_factory=list)
    fill_events: List[NormalizedEvent] = field(default_factory=list)
    replacement: Optional[ReplacementInfo] = None


@dataclass
class EventChain:
    chain_id: int
    chain_type: ChainType
    trigger_event: NormalizedEvent
    trigger_event_ts: Optional[datetime]
    trigger_purpose: Optional[str]
    trigger_order_id: Optional[str]
    dedup_reason: Optional[str] = None
    linked_fill_chain_id: Optional[int] = None
    calculation_events: List[NormalizedEvent] = field(default_factory=list)
    order_chains: List[OrderChain] = field(default_factory=list)
    result_events: List[NormalizedEvent] = field(default_factory=list)
    state_before: Optional[Dict[str, Any]] = None
    state_after: Optional[Dict[str, Any]] = None
    derived_summary: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RunBlock:
    block_id: int
    start_event_id: Optional[int]
    end_event_id: Optional[int]
    start_ts: Optional[datetime]
    end_ts: Optional[datetime]
    symbol: Optional[str]
    strategy: Optional[str]
    block_type: Optional[str]
    bot_state: Optional[str]
    cycle_index: Optional[int]
    is_closed: bool
    events: List[NormalizedEvent] = field(default_factory=list)
    exit_armed_event_id: Optional[int] = None
    recovery_event_id: Optional[int] = None

@dataclass
class Session:
    session_id: int
    start_ts: Optional[datetime]
    end_ts: Optional[datetime]
    event_ids: List[int] = field(default_factory=list)
    symbols: Set[str] = field(default_factory=set)
    strategies: Set[str] = field(default_factory=set)
    restart_reason: Optional[str] = None


@dataclass
class OrderLifecycle:
    key: Tuple[str, ...]
    events: List[NormalizedEvent] = field(default_factory=list)
    sources: Set[str] = field(default_factory=set)


@dataclass
class Anomaly:
    description: str
    confidence: str
    order_key: Optional[Tuple[str, ...]]
    event_ids: List[int] = field(default_factory=list)


def _is_marker_event(event: NormalizedEvent) -> bool:
    return (event.event_name or "") in MARKER_EVENTS


def build_run_blocks(events: Sequence[NormalizedEvent]) -> List[RunBlock]:
    blocks: List[RunBlock] = []
    current: Optional[RunBlock] = None
    block_counter = 0

    def _finalize_block(block: RunBlock, closed: bool = False, end_event: Optional[NormalizedEvent] = None) -> None:
        if end_event:
            block.end_event_id = end_event.event_id
            block.end_ts = end_event.timestamp
        block.is_closed = closed
        blocks.append(block)

    for event in events:
        name = event.event_name or ""
        if name == BLOCK_START_EVENT:
            if current:
                _finalize_block(current, closed=False)
            block_counter += 1
            current = RunBlock(
                block_id=block_counter,
                start_event_id=event.event_id,
                end_event_id=None,
                start_ts=event.timestamp,
                end_ts=None,
                symbol=event.symbol,
                strategy=event.strategy,
                block_type=event.payload.get("block_type"),
                bot_state=event.payload.get("bot_state"),
                cycle_index=event.cycle_index,
                is_closed=False,
            )
            if current.symbol is None:
                current.symbol = _extract_symbol(event.payload)
            if current.strategy is None:
                current.strategy = event.payload.get("strategy")
            current.events.append(event)
            continue

        if not current:
            continue

        current.events.append(event)
        if name == BLOCK_RECOVERY_EVENT and current.recovery_event_id is None:
            current.recovery_event_id = event.event_id
        if name == BLOCK_EXIT_ARMED_EVENT and current.exit_armed_event_id is None:
            current.exit_armed_event_id = event.event_id
        if name == BLOCK_CLOSED_EVENT:
            _finalize_block(current, closed=True, end_event=event)
            current = None

    if current:
        _finalize_block(current, closed=False)

    return blocks


def map_event_to_block(blocks: Sequence[RunBlock]) -> Dict[int, int]:
    event_map: Dict[int, int] = {}
    for block in blocks:
        for ev in block.events:
            if ev.event_id is not None:
                event_map[ev.event_id] = block.block_id
    return event_map


ALLOWED_EVENT_NAMES = {
    "analyzer_block_started",
    "analyzer_recovery_detected",
    "analyzer_exit_armed",
    "analyzer_block_closed",
    "order_submitted",
    "fill_received",
    "order_finalized",
    "fixed_cycle_long_reduce_planned",
    "fixed_cycle_exit_skip",
    "intent_reuse_existing_order",
    "intent_replace_decision",
    "intent_equivalence_check",
    "reconcile_guard_noop",
    "intent_replaced_cancel",
}
NO_RENDER_EVENT_NAMES = {
    "modular_hedge_runtime.order_manager",
    "order_payload_ready",
}


def _is_block_signal_event(event: NormalizedEvent) -> bool:
    name = event.event_name or ""
    if name in NO_RENDER_EVENT_NAMES:
        return False
    if name in ALLOWED_EVENT_NAMES:
        return True
    if event.level and event.level.upper() in {"WARNING", "ERROR"}:
        return True
    return False

def parse_runtime_events(path: Path) -> Iterable[NormalizedEvent]:
    sequence = 0
    try:
        with path.open() as fh:
            block: List[str] = []
            for raw_line in fh:
                line = raw_line.rstrip("\n")
                match = RUNTIME_PATTERN.match(line)
                if match:
                    if block:
                        yield _normalize_runtime_block(block, sequence)
                        sequence += 1
                    block = [line]
                else:
                    if block:
                        block.append(line)
                    else:
                        block = [line]
            if block:
                yield _normalize_runtime_block(block, sequence)
    except FileNotFoundError:
        pass


def _normalize_runtime_block(lines: List[str], sequence: int) -> NormalizedEvent:
    first = lines[0]
    match = RUNTIME_PATTERN.match(first)
    timestamp = None
    level = None
    logger = None
    raw_body = "\n".join(lines)
    event_name = None
    payload: Dict[str, Any] = {}
    metadata: Dict[str, Any] = {}
    if match:
        timestamp = _parse_runtime_timestamp(match.group("ts"))
        level = match.group("level")
        logger = match.group("logger")
        body = match.group("body")
        extra = ""
        if len(lines) > 1:
            extra = "\n".join(lines[1:])
        message_body = f"{body}\n{extra}" if extra else body
        json_start = message_body.find("{")
        payload = {}
        if json_start != -1:
            candidate = message_body[json_start:]
            parsed = _safe_json_load(candidate)
            if parsed is not None:
                payload = parsed
                message_body = message_body[:json_start].strip()
                metadata = payload.get("metadata") or {}
        if not payload:
            payload = {"message": message_body}
        event_name = payload.get("event") or payload.get("event_name")
        candidate_event, remainder = _split_event_name_and_tail(message_body)
        if not event_name and candidate_event:
            event_name = candidate_event
        if remainder.startswith("{"):
            appended = _safe_json_load(remainder)
            if appended:
                payload = {**payload, **appended}
        if not event_name:
            event_name = logger
        raw_body = message_body if payload != {"message": message_body} else message_body
    else:
        payload = {"message": raw_body}
        event_name = "runtime_trace"
    symbol = _extract_symbol(payload)
    purpose = _extract_purpose(payload)
    order_link_id, exchange_order_id = _extract_order_ids(payload)
    cycle_index = _extract_cycle_index(payload)
    status = _extract_status(payload)
    side = payload.get("side")
    strategy = payload.get("strategy")
    bot_state = payload.get("bot_state")
    decision = payload.get("decision")
    reason = payload.get("reason")
    price, qty, avg_price, trigger_price, closed_pnl = _extract_price_fields(payload)
    normalized = NormalizedEvent(
        timestamp=timestamp,
        source="runtime",
        level=level,
        logger=logger,
        event_name=event_name,
        symbol=symbol,
        strategy=strategy,
        purpose=purpose,
        side=side,
        status=status,
        cycle_index=cycle_index,
        order_link_id=order_link_id,
        exchange_order_id=exchange_order_id,
        bot_state=bot_state,
        decision=decision,
        reason=reason,
        price=price,
        qty=qty,
        avg_price=avg_price,
        trigger_price=trigger_price,
        closed_pnl=closed_pnl,
        raw_message="\n".join(lines),
        payload=payload,
        sequence=sequence,
        tp_initial=_safe_float(metadata.get("tp_initial")),
        tp_final=_safe_float(metadata.get("tp_final")),
        tp_steps=metadata.get("tp_adjust_steps"),
    )
    normalized.logged_fields.update(payload.keys())
    normalized.logged_fields.update({"event_name", "logger", "source"})
    if normalized.event_name in CALCULATION_EVENT_NAMES:
        normalized.calculation_name = normalized.event_name
        normalized.calculation_inputs = payload.copy()
    return normalized


def parse_audit_events(path: Path) -> Iterable[NormalizedEvent]:
    sequence = 0
    try:
        with path.open() as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError as err:
                    yield NormalizedEvent(
                        timestamp=None,
                        source="audit",
                        level="WARNING",
                        logger="audit_parser",
                        event_name="invalid_json",
                        symbol=None,
                        strategy=None,
                        purpose=None,
                        side=None,
                        status=None,
                        cycle_index=None,
                        order_link_id=None,
                        exchange_order_id=None,
                        bot_state=None,
                        decision=None,
                        reason=None,
                        raw_message=line,
                        payload={"parse_error": str(err)},
                        sequence=sequence,
                    )
                    sequence += 1
                    continue
                timestamp = _parse_audit_timestamp(data.get("timestamp"))
                payload = data
                order_link_id, exchange_order_id = _extract_order_ids(payload)
                cycle_index = _extract_cycle_index(payload)
                price, qty, avg_price, trigger_price, closed_pnl = _extract_price_fields(payload)
                normalized = NormalizedEvent(
                    timestamp=timestamp,
                    source="audit",
                    level=_extract_level(payload, None),
                    logger=payload.get("logger"),
                    event_name=payload.get("event"),
                    symbol=_extract_symbol(payload),
                    strategy=payload.get("strategy"),
                    purpose=_extract_purpose(payload),
                    side=payload.get("side"),
                    status=_extract_status(payload),
                    cycle_index=cycle_index,
                    order_link_id=order_link_id,
                    exchange_order_id=exchange_order_id,
                    bot_state=payload.get("bot_state"),
                    decision=payload.get("decision"),
                    reason=payload.get("reason"),
                    price=price,
                    qty=qty,
                    avg_price=avg_price,
                    trigger_price=trigger_price,
                    closed_pnl=closed_pnl,
                    raw_message=line,
                    payload=payload,
                    sequence=sequence,
                )
                sequence += 1
                yield normalized
    except FileNotFoundError:
        pass


def merge_events(
    runtime_events: Iterable[NormalizedEvent], audit_events: Iterable[NormalizedEvent]
) -> List[NormalizedEvent]:
    runtime_iter = iter(runtime_events)
    audit_iter = iter(audit_events)
    runtime_next: Optional[NormalizedEvent] = None
    audit_next: Optional[NormalizedEvent] = None
    merged: List[NormalizedEvent] = []
    global_seq = 0

    def _advance(iterator: Iterator[NormalizedEvent]) -> Optional[NormalizedEvent]:
        try:
            return next(iterator)
        except StopIteration:
            return None

    runtime_next = _advance(runtime_iter)
    audit_next = _advance(audit_iter)

    def _event_sort_key(event: NormalizedEvent) -> datetime:
        return event.timestamp or datetime.max

    while runtime_next is not None or audit_next is not None:
        pick: Optional[NormalizedEvent] = None
        if runtime_next and audit_next:
            runtime_key = _event_sort_key(runtime_next)
            audit_key = _event_sort_key(audit_next)
            if runtime_key < audit_key:
                pick = runtime_next
                runtime_next = _advance(runtime_iter)
            elif audit_key < runtime_key:
                pick = audit_next
                audit_next = _advance(audit_iter)
            else:
                pick = runtime_next
                runtime_next = _advance(runtime_iter)
        elif runtime_next:
            pick = runtime_next
            runtime_next = _advance(runtime_iter)
        elif audit_next:
            pick = audit_next
            audit_next = _advance(audit_iter)
        if pick is None:
            continue
        pick.event_id = global_seq
        merged.append(pick)
        global_seq += 1
    return merged


def group_sessions(
    events: Sequence[NormalizedEvent], gap_threshold_seconds: int = GAP_THRESHOLD_SECONDS
) -> Tuple[List[Session], Dict[int, int]]:
    sessions: List[Session] = []
    event_to_session: Dict[int, int] = {}
    current: Optional[Session] = None
    last_ts: Optional[datetime] = None

    def _should_start(new_event: NormalizedEvent) -> bool:
        if new_event.event_name in START_EVENTS:
            return True
        if (
            last_ts
            and new_event.timestamp
            and (new_event.timestamp - last_ts).total_seconds() > gap_threshold_seconds
        ):
            return True
        return False

    for event in events:
        if _should_start(event) or current is None:
            session_id = len(sessions) + 1
            current = Session(session_id=session_id, start_ts=event.timestamp, end_ts=event.timestamp)
            sessions.append(current)
        if event.timestamp and current.end_ts:
            if current.end_ts is None or (event.timestamp and event.timestamp > current.end_ts):
                current.end_ts = event.timestamp
        if event.symbol:
            current.symbols.add(event.symbol)
        if event.strategy:
            current.strategies.add(event.strategy)
        current.event_ids.append(event.event_id if event.event_id is not None else -1)
        event_to_session[event.event_id if event.event_id is not None else -1] = current.session_id
        last_ts = event.timestamp or last_ts
    return sessions, event_to_session


def build_order_lifecycles(events: Sequence[NormalizedEvent]) -> Dict[Tuple[str, ...], OrderLifecycle]:
    lifecycles: Dict[Tuple[str, ...], OrderLifecycle] = {}
    for event in events:
        order_key = None
        if event.order_link_id or event.exchange_order_id:
            key = ("link", event.order_link_id or "", event.exchange_order_id or "")
            order_key = key
        elif event.symbol and event.purpose and event.cycle_index is not None:
            order_key = ("fallback", event.symbol, event.purpose, str(event.cycle_index))
        if not order_key:
            continue
        lifecycle = lifecycles.setdefault(order_key, OrderLifecycle(key=order_key))
        lifecycle.events.append(event)
        lifecycle.sources.add(event.source)
    return lifecycles


def detect_anomalies(lifecycles: Dict[Tuple[str, ...], OrderLifecycle]) -> List[Anomaly]:
    anomalies: List[Anomaly] = []
    for key, lifecycle in lifecycles.items():
        fills = [ev for ev in lifecycle.events if ev.event_name == "fill_received"]
        finals = [ev for ev in lifecycle.events if ev.event_name == "order_finalized"]
        for final in finals:
            status = (final.payload.get("status") or final.status or "").upper()
            if status == "FILLED" and not fills:
                anomalies.append(
                    Anomaly(
                        description="Order finalized as FILLED without recorded fill",
                        confidence="medium",
                        order_key=key,
                        event_ids=[final.event_id]
                        + [ev.event_id for ev in fills if ev.event_id is not None],
                    )
                )
            if status in {"REJECTED", "CANCELLED"} and fills:
                anomalies.append(
                    Anomaly(
                        description="Order rejected/cancelled after fill recorded",
                        confidence="high",
                        order_key=key,
                        event_ids=[final.event_id]
                        + [ev.event_id for ev in fills if ev.event_id is not None],
                    )
                )
    return anomalies


def _timestamp_or_min(ts: Optional[datetime]) -> datetime:
    if ts is None:
        return datetime.min
    return ts


def _extract_state_snapshot(event: Optional[NormalizedEvent]) -> Optional[Dict[str, Any]]:
    if not event:
        return None
    state: Dict[str, Any] = {
        "symbol": event.symbol,
        "purpose": event.purpose,
        "side": event.side,
    }
    if event.timestamp:
        state["timestamp"] = event.timestamp.isoformat()
    for key in CHAIN_STATE_KEYS:
        value = event.payload.get(key)
        if value is not None and key not in state:
            state[key] = value
    return state


def _string_contains_long_add(value: Optional[str]) -> bool:
    if not value:
        return False
    text = str(value).lower()
    return "long_add" in text or "long add" in text or ("cycle_" in text and "long" in text)


def _metadata_contains_long_add(metadata: Mapping[str, Any]) -> List[str]:
    reasons: List[str] = []
    for key, value in metadata.items():
        if isinstance(value, str) and _string_contains_long_add(value):
            reasons.append(f"metadata:{key}")
    return reasons


def _long_add_detection_signal(event: NormalizedEvent) -> Dict[str, Any]:
    reasons: List[str] = []
    purpose_like = False
    if _string_contains_long_add(event.purpose):
        reasons.append("purpose_field")
        purpose_like = True
    if event.event_name and _string_contains_long_add(event.event_name):
        reasons.append("event_name")
    payload = event.payload
    for container_name in ("order", "managed_order", "fill"):
        container = payload.get(container_name) or {}
        if isinstance(container, dict):
            if _string_contains_long_add(container.get("purpose")):
                reasons.append(f"{container_name}_purpose")
            metadata = container.get("metadata") or {}
            reasons.extend(
                f"{container_name}_{entry}" for entry in _metadata_contains_long_add(metadata)
            )
    metadata = payload.get("metadata") or {}
    reasons.extend(_metadata_contains_long_add(metadata))
    client_id = payload.get("client_order_id") or event.order_link_id
    if _string_contains_long_add(client_id):
        reasons.append("client_order_id")
    return {"has_indicator": bool(reasons), "reasons": reasons, "purpose_like": purpose_like}


def _debug_print_candidate(
    event: NormalizedEvent,
    matched: bool,
    reasons: List[str],
    old_match: bool,
    old_reason: Optional[str],
) -> None:
    fill = event.payload.get("fill") or {}
    fill_meta = fill.get("metadata") or {}
    fill_keys = list(fill.keys())
    print(
        f"DEBUG_LONG_ADD | matched={matched} old_match={old_match} old_reason={old_reason or 'n/a'} "
        f"reasons={reasons}"
    )
    print(
        f"  source={event.source} event={event.event_name} purpose={event.purpose} "
        f"client_order={event.order_link_id} exchange_order={event.exchange_order_id} "
        f"side={event.side} cycle_index={event.cycle_index} "
        f"qty={event.qty} price={event.price}"
    )
    print(f"  logged_fields={sorted(event.logged_fields)}")
    print(f"  fill_metadata={fill_meta} fill_keys={fill_keys}")


def detect_long_add_candidates(
    events: Sequence[NormalizedEvent], debug: bool = False
) -> List[NormalizedEvent]:
    direct_info: Dict[int, Dict[str, Any]] = {}
    long_add_order_ids: Set[str] = set()
    for event in events:
        info = _long_add_detection_signal(event)
        direct_info[id(event)] = info
        if info["has_indicator"]:
            long_add_order_ids.update(_order_event_keys(event))

    candidates: List[NormalizedEvent] = []
    debug_stats = {"total": 0, "matched": 0, "rejected": []}
    for event in events:
        info = direct_info[id(event)]
        order_keys = _order_event_keys(event)
        matched = False
        reasons: List[str] = []
        if info["has_indicator"]:
            matched = True
            reasons.extend(info["reasons"])
        if not matched:
            for key in order_keys:
                if "long_add" in key.lower():
                    matched = True
                    reasons.append("order_id_contains_long_add")
                    break
        if not matched and long_add_order_ids and set(order_keys) & long_add_order_ids:
            matched = True
            reasons.append("linked_long_add_order_id")
        if matched:
            candidates.append(event)
        if debug and (info["has_indicator"] or order_keys):
            debug_stats["total"] += 1
            old_match = (
                event.event_name == "fill_received"
                and info["purpose_like"]
            )
            old_reason = None
            if not old_match:
                if event.event_name != "fill_received":
                    old_reason = "not_fill_event"
                else:
                    old_reason = "missing_long_add_purpose"
            if matched:
                debug_stats["matched"] += 1
            else:
                debug_stats["rejected"].append((event, old_reason))
            _debug_print_candidate(event, matched, reasons, old_match, old_reason)
    if debug:
        print(
            f"DEBUG_LONG_ADD SUMMARY: total_candidates={debug_stats['total']} "
            f"matched={debug_stats['matched']} "
            f"rejected={len(debug_stats['rejected'])}"
        )
        for event, reason in debug_stats["rejected"]:
            print(
                f"  Rejected event_id={event.event_id or '??'} old_reason={reason or 'n/a'} "
                f"event_name={event.event_name} purpose={event.purpose}"
            )
    return candidates

def _summarize_order_chain(lifecycle: OrderLifecycle) -> OrderChain:
    submitted = next((ev for ev in lifecycle.events if ev.event_name == "order_submitted"), None)
    purpose = submitted.payload.get("purpose") if submitted else None
    side = submitted.payload.get("side") if submitted else None
    fills = [ev for ev in lifecycle.events if ev.event_name == "fill_received"]
    replacement: Optional[ReplacementInfo] = None
    for event in lifecycle.events:
        if event.event_name == "intent_replaced_cancel":
            old_id = event.payload.get("exchange_order_id") or event.payload.get("client_order_id")
            reason = event.payload.get("reason")
            new_id = None
            for later in lifecycle.events:
                if later.event_name == "order_submitted" and event.timestamp and later.timestamp and later.timestamp >= event.timestamp:
                    new_id = later.payload.get("exchange_order_id") or later.payload.get("order_link_id")
                    break
            replacement = ReplacementInfo(old_order_id=old_id, new_order_id=new_id, reason=reason)
            break
    return OrderChain(
        key=lifecycle.key,
        purpose=purpose,
        side=side,
        submitted_event=submitted,
        lifecycle_events=list(lifecycle.events),
        fill_events=fills,
        replacement=replacement,
    )


def _find_chain_for_timestamp(
    timestamp: Optional[datetime], windows: List[Tuple[datetime, Optional[datetime], EventChain]]
) -> Optional[EventChain]:
    if timestamp is None:
        return None
    for start, end, chain in windows:
        if start <= timestamp and (end is None or timestamp < end):
            return chain
    return None


def _order_event_keys(event: NormalizedEvent) -> List[str]:
    keys: List[str] = []
    for candidate in (
        event.payload.get("exchange_order_id"),
        event.payload.get("order_link_id"),
        event.payload.get("client_order_id"),
        event.order_link_id,
        event.exchange_order_id,
    ):
        if candidate:
            keys.append(str(candidate))
    return keys


def _build_order_index(events: Sequence[NormalizedEvent]) -> Dict[str, List[NormalizedEvent]]:
    index: Dict[str, List[NormalizedEvent]] = defaultdict(list)
    for ev in events:
        for key in _order_event_keys(ev):
            index[key].append(ev)
    return index


def validate_event_chains(
    chains: Sequence[EventChain], events: Sequence[NormalizedEvent]
) -> Tuple[List[ChainValidation], ChainSummary]:
    validations: List[ChainValidation] = []
    order_index = _build_order_index(events)
    missing_runtime_links = 0
    missing_audit_links = 0
    replacement_issues = 0
    mismatch_tp = 0
    complete = 0
    broken = 0
    partial = 0
    waiting_for_fill = 0

    for chain in chains:
        if chain.chain_type == ChainType.LONG_ADD_ORDER_CHAIN:
            validation = ChainValidation(chain_id=chain.chain_id, status="COMPLETE")
            if not chain.linked_fill_chain_id:
                validation.status = "WAITING_FOR_FILL"
                validation.issues.append("no fill yet")
                validation.reason_codes.append("WAITING_FOR_FILL")
                waiting_for_fill += 1
            else:
                complete += 1
            validations.append(validation)
            continue
        has_calc = bool(chain.calculation_events)
        has_order_submit = any(order.submitted_event for order in chain.order_chains)
        has_lifecycle = any(order.lifecycle_events for order in chain.order_chains)
        has_result = bool(chain.result_events)
        status = "COMPLETE"
        missing_links: List[str] = []
        validation = ChainValidation(chain_id=chain.chain_id, status=status)
        if not has_calc:
            status = "MISSING_CALCULATION"
            missing_links.append("calculation")
            validation.reason_codes.append("MISSING_CALCULATION")
            validation.issues.append("missing calculation step")
        if not has_order_submit:
            status = "MISSING_ORDER"
            missing_links.append("order_submit")
            validation.reason_codes.append("MISSING_ORDER_SUBMIT")
            validation.issues.append("missing order submit")
        if not has_lifecycle:
            status = "BROKEN"
            missing_links.append("order_lifecycle")
            validation.reason_codes.append("PARTIAL_LIFECYCLE")
            validation.issues.append("missing lifecycle events")
        if not has_result:
            status = "PARTIAL"
            missing_links.append("result")
            validation.reason_codes.append("MISSING_RESULT")
            validation.issues.append("missing result event")
        validation.missing_links = missing_links

        for order in chain.order_chains:
            submit_runtime = (
                order.submitted_event and order.submitted_event.source == "runtime"
            )
            lifecycle_runtime = any(ev.source == "runtime" for ev in order.lifecycle_events)
            lifecycle_audit = any(ev.source == "audit" for ev in order.lifecycle_events)
            final_statuses = {"FILLED", "CANCELED", "CANCELLED", "REJECTED"}
            lifecycle_statuses = {
                (ev.payload.get("status") or ev.status or "").upper()
                for ev in order.lifecycle_events
                if ev.payload or ev.status
            }
            if lifecycle_statuses and not lifecycle_statuses & final_statuses:
                validation.reason_codes.append("PARTIAL_LIFECYCLE")
                validation.issues.append("lifecycle lacks final status")
            if lifecycle_audit and not (submit_runtime or lifecycle_runtime):
                validation.missing_runtime = True
                missing_runtime_links += 1
                validation.issues.append("audit event without runtime order")
                validation.reason_codes.append("MISSING_RUNTIME_LINK")
            if submit_runtime and not lifecycle_audit:
                validation.missing_audit = True
                missing_audit_links += 1
                validation.issues.append("runtime order without audit lifecycle")
                validation.reason_codes.append("MISSING_AUDIT_LINK")

            if order.replacement:
                old_id = order.replacement.old_order_id
                new_id = order.replacement.new_order_id
                if old_id:
                    old_events = order_index.get(old_id, [])
                    old_closed = any(
                        (
                            (ev.payload.get("status") or ev.status or "").upper()
                            in {"CANCELED", "CANCELLED", "FILLED", "REJECTED"}
                        )
                        for ev in old_events
                    )
                    if not old_closed:
                        issue = f"replacement old order {old_id} not closed"
                        validation.replacement_issues.append(issue)
                        validation.reason_codes.append("REPLACEMENT_NOT_CLOSED")
                        replacement_issues += 1
                        validation.issues.append(issue)
                if new_id:
                    new_events = order_index.get(new_id, [])
                    if not new_events:
                        issue = f"replacement new order {new_id} missing lifecycle"
                        validation.replacement_issues.append(issue)
                        validation.reason_codes.append("REPLACEMENT_NOT_CLOSED")
                        replacement_issues += 1
                        validation.issues.append(issue)

        expected = chain.derived_summary.get("expected_tp_price")
        actual = chain.derived_summary.get("actual_trigger_price") or chain.derived_summary.get(
            "normalized_trigger_price"
        )
        deviation = None
        if expected is not None and actual is not None:
            deviation = actual - expected
            validation.tp_deviation = deviation
            tolerance = 0.0005
            if abs(deviation) > tolerance:
                validation.issues.append(
                    f"tp mismatch deviation={deviation:.6f} expected={expected:.6f} actual={actual:.6f}"
                )
                validation.reason_codes.append("TP_MISMATCH")
                mismatch_tp += 1
                if status == "COMPLETE":
                    status = "MISMATCH_TP"
                validation.status = status
        validation.status = status
        if status == "COMPLETE":
            complete += 1
        elif status == "PARTIAL":
            partial += 1
            broken += 1
        else:
            broken += 1
        validations.append(validation)

    summary = ChainSummary(
        total=len(chains),
        complete=complete,
        broken=broken,
        partial=partial,
        mismatch_tp=mismatch_tp,
        missing_runtime_links=missing_runtime_links,
        missing_audit_links=missing_audit_links,
        replacement_issues=replacement_issues,
        waiting_for_fill=waiting_for_fill,
    )
    return validations, summary


def summarize_validations(validations: Sequence[ChainValidation]) -> ChainSummary:
    total = len(validations)
    complete = sum(1 for val in validations if val.status == "COMPLETE")
    partial = sum(1 for val in validations if val.status == "PARTIAL")
    broken_statuses = {"BROKEN", "MISMATCH_TP", "MISSING_ORDER", "MISSING_CALCULATION"}
    broken = sum(1 for val in validations if val.status in broken_statuses)
    mismatch_tp = sum(1 for val in validations if "TP_MISMATCH" in val.reason_codes)
    missing_runtime_links = sum(1 for val in validations if val.missing_runtime)
    missing_audit_links = sum(1 for val in validations if val.missing_audit)
    replacement_issues = sum(len(val.replacement_issues) for val in validations)
    waiting_for_fill = sum(1 for val in validations if val.status == "WAITING_FOR_FILL")
    return ChainSummary(
        total=total,
        complete=complete,
        broken=broken,
        partial=partial,
        mismatch_tp=mismatch_tp,
        missing_runtime_links=missing_runtime_links,
        missing_audit_links=missing_audit_links,
        replacement_issues=replacement_issues,
        waiting_for_fill=waiting_for_fill,
    )


def aggregate_reason_summary(validations: Sequence[ChainValidation]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for val in validations:
        counter.update(val.reason_codes)
    return counter


def filter_chain_validations(
    chains: Sequence[EventChain],
    validations: Sequence[ChainValidation],
    *,
    only_issues: bool,
    status: Optional[str],
    purpose_contains: Optional[str],
    cycle_index: Optional[int],
) -> Tuple[List[EventChain], List[ChainValidation]]:
    zipped = [(chain, val) for chain, val in zip(chains, validations)]
    if only_issues:
        zipped = [(chain, val) for chain, val in zipped if val.issues]
    if status:
        zipped = [(chain, val) for chain, val in zipped if val.status == status]
    if purpose_contains:
        needle = purpose_contains.lower()
        zipped = [
            (chain, val)
            for chain, val in zipped
            if chain.trigger_purpose and needle in chain.trigger_purpose.lower()
        ]
    if cycle_index is not None:
        zipped = [
            (chain, val)
            for chain, val in zipped
            if chain.trigger_event.cycle_index == cycle_index
        ]
    return [chain for chain, _ in zipped], [val for _, val in zipped]
def _calculate_chain_metrics(chain: EventChain) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    break_even = next(
        (
            ev.payload.get("break_even_price")
            for ev in chain.calculation_events
            if ev.event_name == "fixed_cycle_break_even_inputs"
        ),
        None,
    )
    tp_event = next(
        (ev for ev in chain.calculation_events if ev.event_name == "fixed_cycle_tp_components"),
        None,
    )
    tp_price = tp_event.payload.get("tp_price") if tp_event else None
    short_tp_plan = next(
        (
            ev
            for ev in chain.calculation_events
            if ev.event_name == "fixed_cycle_short_tp_pair_planned"
        ),
        None,
    )
    normalized_trigger = short_tp_plan.payload.get("trigger_price_normalized") if short_tp_plan else None
    short_qty = next(
        (
            ev.qty
            for ev in chain.result_events
            if ev.purpose and "SHORT_TP" in ev.purpose and ev.qty is not None
        ),
        None,
    )
    if short_qty is None and short_tp_plan:
        short_qty = short_tp_plan.payload.get("qty_normalized") or short_tp_plan.payload.get("qty_raw")
    if break_even is not None and tp_price is not None:
        summary["required_price_move"] = round(tp_price - break_even, 8)
    if short_qty is not None and normalized_trigger is not None:
        try:
            normalized_qty = float(short_qty)
            normalized_price = float(normalized_trigger)
            summary["required_short_gross"] = round(normalized_qty * normalized_price, 6)
        except (TypeError, ValueError):
            pass
    if short_qty is not None and tp_price is not None and break_even is not None:
        diff = tp_price - break_even
        try:
            summary["target_profit_usdt"] = round(diff * float(short_qty), 6)
        except (TypeError, ValueError):
            pass
    if short_tp_plan:
        summary["trigger_formula"] = short_tp_plan.payload.get("trigger_formula")
        summary["raw_trigger_price"] = short_tp_plan.payload.get("trigger_price_raw")
        summary["normalized_trigger_price"] = normalized_trigger
    expected_tp = None
    if tp_event:
        be_price = tp_event.payload.get("break_even_price")
        goal_price = tp_event.payload.get("goal_profit_price_component")
        buffer_price = tp_event.payload.get("buffer_price_component")
        try:
            expected_tp = sum(
                value or 0.0
                for value in (be_price, goal_price, buffer_price)
                if value is not None
            )
            summary["expected_tp_price"] = expected_tp
        except (TypeError, ValueError):
            expected_tp = None
    actual_trigger = normalized_trigger
    if actual_trigger is not None:
        summary["actual_trigger_price"] = actual_trigger
    if expected_tp is not None and actual_trigger is not None:
        deviation = actual_trigger - expected_tp
        summary["tp_deviation"] = deviation
        try:
            summary["tp_relative_diff_pct"] = round(
                abs(deviation) / expected_tp * 100, 4
            )
        except (TypeError, ZeroDivisionError):
            summary["tp_relative_diff_pct"] = None
    return summary


def build_event_chains(
    events: Sequence[NormalizedEvent],
    lifecycles: Dict[Tuple[str, ...], OrderLifecycle],
    debug_long_add: bool = False,
) -> List[EventChain]:
    long_add_candidates = detect_long_add_candidates(events, debug_long_add)
    long_add_order_ids = {
        key for ev in long_add_candidates for key in _order_event_keys(ev)
    }
    order_triggers: Dict[str, Dict[str, Any]] = {}
    order_dedup_logs: List[Tuple[NormalizedEvent, str]] = []
    sorted_events = sorted(events, key=lambda ev: _timestamp_or_min(ev.timestamp))
    for event in sorted_events:
        if event.event_name not in ORDER_CHAIN_EVENT_PRIORITY:
            continue
        order_ids = [oid for oid in _order_event_keys(event) if oid in long_add_order_ids]
        if not order_ids:
            continue
        priority = ORDER_CHAIN_EVENT_PRIORITY.index(event.event_name)
        for order_id in order_ids:
            existing = order_triggers.get(order_id)
            if existing and priority >= existing["priority"]:
                order_dedup_logs.append((event, order_id))
                continue
            order_triggers[order_id] = {"event": event, "priority": priority}

    order_windows: List[Tuple[datetime, Optional[datetime], EventChain]] = []
    chains: List[EventChain] = []
    chain_id = 1
    sorted_order_ids = sorted(
        order_triggers.items(), key=lambda item: _timestamp_or_min(item[1]["event"].timestamp)
    )
    for idx, (order_id, info) in enumerate(sorted_order_ids):
        event = info["event"]
        start = event.timestamp or datetime.min
        end = (
            _timestamp_or_min(sorted_order_ids[idx + 1][1]["event"].timestamp)
            if idx + 1 < len(sorted_order_ids)
            else None
        )
        chain = EventChain(
            chain_id=chain_id,
            chain_type=ChainType.LONG_ADD_ORDER_CHAIN,
            trigger_event=event,
            trigger_event_ts=event.timestamp,
            trigger_purpose=event.purpose,
            trigger_order_id=order_id,
            state_before=_extract_state_snapshot(event),
        )
        order_windows.append((start, end, chain))
        chains.append(chain)
        chain_id += 1
    if debug_long_add and order_dedup_logs:
        print("LONG_ADD_ORDER_CHAIN DEDUPLICATION:")
        for event, oid in order_dedup_logs:
            print(
                f"  skipped {event.event_name} for {oid} repurposed by existing order trigger"
            )
    for event in events:
        chain = _find_chain_for_timestamp(event.timestamp, order_windows)
        if not chain:
            continue
        if event.event_name in ORDER_CHAIN_CALC_EVENTS:
            chain.calculation_events.append(event)
    for start, end, chain in order_windows:
        if chain.calculation_events:
            after_event = chain.calculation_events[-1]
            chain.state_after = _extract_state_snapshot(after_event)
            chain.derived_summary = _calculate_chain_metrics(chain)

    fill_triggers: Dict[str, Dict[str, Any]] = {}
    fill_dedup_logs: List[Tuple[NormalizedEvent, str]] = []
    for event in long_add_candidates:
        if event.event_name != "fill_received":
            continue
        order_ids = _order_event_keys(event)
        for order_id in order_ids:
            if order_id in fill_triggers:
                fill_dedup_logs.append((event, order_id))
                continue
            fill_triggers[order_id] = {"event": event}
    if debug_long_add and fill_dedup_logs:
        print("LONG_ADD_FILL_EFFECT_CHAIN DEDUPLICATION:")
        for event, oid in fill_dedup_logs:
            print(f"  skipped duplicate fill {event.event_id} for order {oid}")
    fill_events = [info["event"] for info in fill_triggers.values()]
    if not fill_events:
        return chains
    windows: List[Tuple[datetime, Optional[datetime], EventChain]] = []
    sorted_fills = sorted(fill_events, key=lambda ev: _timestamp_or_min(ev.timestamp))
    for idx, trigger in enumerate(sorted_fills):
        start = trigger.timestamp or datetime.min
        end = sorted_fills[idx + 1].timestamp if idx + 1 < len(sorted_fills) else None
        chain = EventChain(
            chain_id=chain_id,
            chain_type=ChainType.LONG_ADD_FILL_EFFECT_CHAIN,
            trigger_event=trigger,
            trigger_event_ts=trigger.timestamp,
            trigger_purpose=trigger.purpose,
            trigger_order_id=(
                trigger.payload.get("fill", {}).get("client_order_id")
                or trigger.payload.get("fill", {}).get("order_link_id")
                or (_order_event_keys(trigger)[0] if _order_event_keys(trigger) else None)
            ),
            state_before=_extract_state_snapshot(trigger),
        )
        windows.append((start, end, chain))
        chain_id += 1
    fill_chain_map = {
        chain.trigger_order_id: chain.chain_id for _, _, chain in windows if chain.trigger_order_id
    }
    for event in events:
        chain = _find_chain_for_timestamp(event.timestamp, windows)
        if not chain:
            continue
        if event.event_name in CALCULATION_EVENT_NAMES.union(ORDER_CHAIN_CALC_EVENTS):
            chain.calculation_events.append(event)
        if event.event_name == "fill_received":
            chain.result_events.append(event)
    for start, end, chain in windows:
        if chain.calculation_events:
            after_event = next(
                (
                    ev
                    for ev in reversed(chain.calculation_events)
                    if ev.event_name in {"fixed_cycle_exit_manifest", "fixed_cycle_structure_rebuilt"}
                ),
                chain.calculation_events[-1],
            )
            chain.state_after = _extract_state_snapshot(after_event)
        elif chain.result_events:
            chain.state_after = _extract_state_snapshot(chain.result_events[-1])
        chain.derived_summary = _calculate_chain_metrics(chain)
    assigned_lifecycles: Set[Tuple[str, ...]] = set()
    for lifecycle in lifecycles.values():
        lifecycle_ts = min(
            (ev.timestamp for ev in lifecycle.events if ev.timestamp),
            default=None,
        )
        if not lifecycle_ts:
            continue
        chain = _find_chain_for_timestamp(lifecycle_ts, windows)
        if chain and lifecycle.key not in assigned_lifecycles:
            chain.order_chains.append(_summarize_order_chain(lifecycle))
            assigned_lifecycles.add(lifecycle.key)
    chains.extend(chain for _, _, chain in windows)
    # link order chains to fill chains
    for chain in chains:
        if chain.chain_type == ChainType.LONG_ADD_ORDER_CHAIN and chain.trigger_order_id:
            linked_id = fill_chain_map.get(chain.trigger_order_id)
            chain.linked_fill_chain_id = linked_id
            if debug_long_add:
                status = "fill detected" if linked_id else "no fill detected"
                print(f"DEBUG_CHAIN_LINK | order {chain.trigger_order_id} -> {status}")
    return chains

def _event_effective_price(event: NormalizedEvent) -> Optional[float]:
    return event.price or event.avg_price or event.trigger_price


def _is_long_add_purpose(purpose: Optional[str]) -> bool:
    if not purpose:
        return False
    purpose = purpose.upper()
    return "LONG_ADD" in purpose


def _is_short_tp_purpose(purpose: Optional[str]) -> bool:
    if not purpose:
        return False
    purpose = purpose.upper()
    return "SHORT_TP" in purpose or ("SHORT" in purpose and "TP" in purpose)


def _is_fill_like_event(event: NormalizedEvent) -> bool:
    if event.event_name == "fill_received":
        return True
    status = (event.status or "").upper()
    return status == "FILLED"


def _find_best_fill_event(events: List[NormalizedEvent]) -> Optional[NormalizedEvent]:
    fill_events = [ev for ev in events if _is_fill_like_event(ev)]
    priced_fill_events = [
        ev for ev in fill_events if _event_effective_price(ev) is not None and ev.qty is not None
    ]
    if priced_fill_events:
        priced_fill_events.sort(key=lambda ev: (ev.timestamp or datetime.max, ev.sequence))
        return priced_fill_events[-1]
    if fill_events:
        fill_events.sort(key=lambda ev: (ev.timestamp or datetime.max, ev.sequence))
        return fill_events[-1]

    priced = [ev for ev in events if _event_effective_price(ev) is not None and ev.qty is not None]
    if priced:
        priced.sort(key=lambda ev: (ev.timestamp or datetime.max, ev.sequence))
        return priced[-1]

    return None


def _find_best_submit_event(events: List[NormalizedEvent]) -> Optional[NormalizedEvent]:
    candidates = [
        ev for ev in events
        if ev.event_name in {"order_submitted", "order_created", "order_registered"}
    ]
    if not candidates:
        candidates = [ev for ev in events if ev.trigger_price is not None or ev.price is not None]
    if not candidates:
        return None
    candidates.sort(key=lambda ev: (ev.timestamp or datetime.max, ev.sequence))
    return candidates[-1]


def build_cycle_pnl_report(
    lifecycles: Dict[Tuple[str, ...], OrderLifecycle],
    fee_rate: float,
    symbol_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[str, int], Dict[str, List[NormalizedEvent]]] = {}

    for lifecycle in lifecycles.values():
        relevant_symbol = None
        relevant_cycle = None
        relevant_purpose = None

        for ev in lifecycle.events:
            if ev.symbol and relevant_symbol is None:
                relevant_symbol = ev.symbol
            if ev.cycle_index is not None and relevant_cycle is None:
                relevant_cycle = ev.cycle_index
            if ev.purpose and relevant_purpose is None:
                relevant_purpose = ev.purpose

        if relevant_cycle is None or not relevant_purpose:
            continue

        resolved_symbol = relevant_symbol or symbol_filter or "unknown"
        if symbol_filter and resolved_symbol != symbol_filter:
            continue

        bucket = grouped.setdefault(
            (resolved_symbol, relevant_cycle),
            {
                "long_add_events": [],
                "short_tp_events": [],
            },
        )

        if _is_long_add_purpose(relevant_purpose):
            bucket["long_add_events"].extend(lifecycle.events)
        elif _is_short_tp_purpose(relevant_purpose):
            bucket["short_tp_events"].extend(lifecycle.events)

    reports: List[Dict[str, Any]] = []

    for (symbol, cycle_index), bucket in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1])):
        long_fill = _find_best_fill_event(bucket["long_add_events"])
        short_fill = _find_best_fill_event(bucket["short_tp_events"])
        short_submit = _find_best_submit_event(bucket["short_tp_events"])

        long_fill_price = _event_effective_price(long_fill) if long_fill else None
        long_fill_qty = long_fill.qty if long_fill else None
        long_closed_pnl = long_fill.closed_pnl if long_fill else None
        long_loss_usdt = max(-(long_closed_pnl or 0.0), 0.0) if long_closed_pnl is not None else None

        short_entry_price = _event_effective_price(short_fill) if short_fill else None
        short_tp_price = short_submit.trigger_price if short_submit else None
        if short_tp_price is None and short_submit is not None:
            short_tp_price = short_submit.price

        short_tp_qty = None
        if short_submit and short_submit.qty is not None:
            short_tp_qty = short_submit.qty
        elif short_fill and short_fill.qty is not None:
            short_tp_qty = short_fill.qty

        gross_profit_usdt = None
        open_fee_usdt = None
        close_fee_usdt = None
        net_profit_usdt = None
        net_after_cover_usdt = None
        fully_covers_long_loss = None

        if (
            short_entry_price is not None
            and short_tp_price is not None
            and short_tp_qty is not None
        ):
            gross_profit_usdt = (short_entry_price - short_tp_price) * short_tp_qty
            open_fee_usdt = short_entry_price * short_tp_qty * fee_rate
            close_fee_usdt = short_tp_price * short_tp_qty * fee_rate
            net_profit_usdt = gross_profit_usdt - open_fee_usdt - close_fee_usdt

        if long_loss_usdt is not None and net_profit_usdt is not None:
            net_after_cover_usdt = net_profit_usdt - long_loss_usdt
            fully_covers_long_loss = net_after_cover_usdt >= 0

        reports.append(
            {
                "symbol": symbol,
                "cycle": cycle_index,
                "long_add": {
                    "fill_price": long_fill_price,
                    "qty": long_fill_qty,
                    "closed_pnl_usdt": long_closed_pnl,
                    "loss_usdt": long_loss_usdt,
                    "event_id": long_fill.event_id if long_fill else None,
                },
                "short_tp": {
                    "entry_price": short_entry_price,
                    "tp_price": short_tp_price,
                    "qty": short_tp_qty,
                    "gross_profit_usdt": gross_profit_usdt,
                    "open_fee_usdt": open_fee_usdt,
                    "close_fee_usdt": close_fee_usdt,
                    "net_profit_usdt": net_profit_usdt,
                    "fill_event_id": short_fill.event_id if short_fill else None,
                    "submit_event_id": short_submit.event_id if short_submit else None,
                },
                "coverage": {
                    "long_loss_usdt": long_loss_usdt,
                    "short_tp_net_profit_usdt": net_profit_usdt,
                    "net_after_cover_usdt": net_after_cover_usdt,
                    "fully_covers_long_loss": fully_covers_long_loss,
                },
            }
        )

    return reports


def render_cycle_pnl(reports: Sequence[Dict[str, Any]]) -> None:
    if not reports:
        print("No cycle pnl data found.")
        return

    def fmt(value: Any, digits: int = 6) -> str:
        if value is None:
            return "n/a"
        return f"{float(value):.{digits}f}"

    for report in reports:
        long_add = report["long_add"]
        short_tp = report["short_tp"]
        coverage = report["coverage"]

        print(
            f"{report['symbol']} | CYCLE {report['cycle']} | "
            f"LONG fill={fmt(long_add['fill_price'])} qty={fmt(long_add['qty'])} "
            f"closed_pnl={fmt(long_add['closed_pnl_usdt'])} loss=${fmt(long_add['loss_usdt'])}"
        )
        print(
            f"    SHORT TP entry={fmt(short_tp['entry_price'])} tp={fmt(short_tp['tp_price'])} "
            f"qty={fmt(short_tp['qty'])} gross=${fmt(short_tp['gross_profit_usdt'])} "
            f"fees_open=${fmt(short_tp['open_fee_usdt'])} fees_close=${fmt(short_tp['close_fee_usdt'])} "
            f"net=${fmt(short_tp['net_profit_usdt'])}"
        )
        print(
            f"    COVER long_loss=${fmt(coverage['long_loss_usdt'])} "
            f"short_net=${fmt(coverage['short_tp_net_profit_usdt'])} "
            f"diff=${fmt(coverage['net_after_cover_usdt'])} "
            f"covered={coverage['fully_covers_long_loss']}"
        )


def filter_events(
    events: Sequence[NormalizedEvent],
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    symbol: Optional[str] = None,
    event_name: Optional[str] = None,
    level: Optional[str] = None,
    purpose: Optional[str] = None,
    sources: Optional[Set[str]] = None,
    session_ids: Optional[Set[int]] = None,
    only_important: bool = False,
    only_anomalies: bool = False,
    anomalies: Optional[List[Anomaly]] = None,
    event_to_session: Optional[Dict[int, int]] = None,
    block_ids: Optional[Set[int]] = None,
    event_to_block: Optional[Dict[int, int]] = None,
) -> List[NormalizedEvent]:
    anomaly_event_ids: Set[int] = set()
    if only_anomalies and anomalies:
        for anomaly in anomalies:
            anomaly_event_ids.update(anomaly.event_ids)
    filtered: List[NormalizedEvent] = []
    for event in events:
        if since and (not event.timestamp or event.timestamp < since):
            continue
        if until and (not event.timestamp or event.timestamp > until):
            continue
        if symbol and event.symbol != symbol:
            continue
        if event_name and event.event_name != event_name:
            continue
        if level and (not event.level or event.level.upper() != level.upper()):
            continue
        if purpose and event.purpose != purpose:
            continue
        if sources and event.source not in sources:
            continue
        if session_ids and event.event_id is not None:
            sid = event_to_session.get(event.event_id) if event_to_session else None
            if not sid or sid not in session_ids:
                continue
        if block_ids and event.event_id is not None:
            bid = event_to_block.get(event.event_id) if event_to_block else None
            if bid not in block_ids:
                continue
        if only_important:
            if not event.level or event.level.upper() not in {"WARNING", "ERROR"}:
                continue
        if only_anomalies:
            if event.event_id not in anomaly_event_ids:
                continue
        filtered.append(event)
    return filtered


def render_summary(
    events: Sequence[NormalizedEvent],
    sessions: List[Session],
    anomalies: List[Anomaly],
    lifecycles: Dict[Tuple[str, ...], OrderLifecycle],
) -> None:
    print("Sessions:", len(sessions))
    for session in sessions:
        duration = (
            f"{(session.end_ts - session.start_ts).total_seconds():.0f}s"
            if session.start_ts
            and session.end_ts
            and session.end_ts >= session.start_ts
            else "unknown"
        )
        print(
            f"  Session {session.session_id}: {session.start_ts} → {session.end_ts} ({duration}) |"
            f" Symbols={sorted(session.symbols)} | Strategies={sorted(session.strategies)}"
        )
    error_count = sum(1 for ev in events if ev.level and ev.level.upper() == "ERROR")
    warning_count = sum(1 for ev in events if ev.level and ev.level.upper() == "WARNING")
    submits = sum(1 for ev in events if ev.event_name == "order_submitted")
    fills = sum(1 for ev in events if ev.event_name == "fill_received")
    rejects = sum(
        1
        for ev in events
        if ev.event_name == "order_finalized"
        and ((ev.payload.get("status") or ev.status) or "").upper() != "FILLED"
    )
    reconciles = sum(1 for ev in events if ev.event_name and "reconcile" in ev.event_name)
    incomplete = sum(
        1 for lifecycle in lifecycles.values() for ev in lifecycle.events if ev.event_name == "order_submitted"
    )
    print("Errors:", error_count, "Warnings:", warning_count)
    print("Submitted:", submits, "Fills:", fills, "Rejects:", rejects)
    print("Reconcile events:", reconciles)
    print("Order lifecycles:", len(lifecycles))
    print("Anomalies:", len(anomalies))
    print("Source coverage per lifecycle:")
    for key, lifecycle in lifecycles.items():
        print(f"  {key}: {sorted(lifecycle.sources)}")


def render_timeline(
    events: Sequence[NormalizedEvent],
    event_to_session: Dict[int, int] | None = None,
    sessions: List[Session] | None = None,
) -> None:
    filtered = [event for event in events if event.event_name not in TIMELINE_SKIP_EVENTS]
    session_lookup = {session.session_id: session for session in sessions} if sessions else {}
    prev_session_id: Optional[int] = None
    for event in filtered:
        current_session_id = (
            event_to_session.get(event.event_id) if event.event_id is not None and event_to_session else None
        )
        if current_session_id and current_session_id != prev_session_id:
            session = session_lookup.get(current_session_id)
            start_ts = session.start_ts.isoformat() if session and session.start_ts else "unknown"
            print(f"\n=== Session {current_session_id} start {start_ts} ===")
            prev_session_id = current_session_id
        ts = event.timestamp.isoformat() if event.timestamp else "N/A"
        print(
            f"{event.event_id:04d} [{event.source}] {ts} {event.level or 'INFO'} {event.event_name} "
            f"{event.symbol or ''} {event.purpose or ''} {event.order_link_id or ''}"
        )


def render_orders(lifecycles: Dict[Tuple[str, ...], OrderLifecycle]) -> None:
    for key, lifecycle in lifecycles.items():
        print(f"Order {key}:")
        for ev in lifecycle.events:
            ts = ev.timestamp.isoformat() if ev.timestamp else "N/A"
            print(
                f"  {ev.event_id} {ts} [{ev.source}] {ev.event_name} status={ev.status} "
                f"level={ev.level} symbol={ev.symbol} purpose={ev.purpose}"
            )


def render_sessions(sessions: List[Session], anomalies: List[Anomaly]) -> None:
    for session in sessions:
        print(
            f"Session {session.session_id}: {session.start_ts} -> {session.end_ts} "
            f"Symbols={sorted(session.symbols)} Strategies={sorted(session.strategies)}"
        )
        related = [a for a in anomalies if any(
            ev_id in session.event_ids for ev_id in a.event_ids
        )]
        if related:
            print("  Anomalies:")
            for anomaly in related:
                print(f"    - {anomaly.description} ({anomaly.confidence})")


def render_anomalies(anomalies: List[Anomaly]) -> None:
    for anomaly in anomalies:
        print(
            f"[{anomaly.confidence}] {anomaly.description} | order={anomaly.order_key} | events={anomaly.event_ids}"
        )


def render_blocks(
    blocks: Sequence[RunBlock],
    lifecycles: Dict[Tuple[str, ...], OrderLifecycle],
    cycle_pnl_reports: Sequence[Dict[str, Any]],
    fee_rate: float,
    symbol_filter: Optional[str] = None,
) -> None:
    ALWAYS_PRINT_EVENT_NAMES = {
        BLOCK_START_EVENT,
        BLOCK_RECOVERY_EVENT,
        BLOCK_EXIT_ARMED_EVENT,
        BLOCK_CLOSED_EVENT,
        "fill_received",
        "order_finalized",
        "fixed_cycle_long_reduce_planned",
        "fixed_cycle_exit_skip",
    }

    if not blocks:
        print("No run blocks found.")
        return

    report_lookup = {
        (report["symbol"], report["cycle"]): report for report in cycle_pnl_reports
    }

    for block in blocks:
        if symbol_filter and block.symbol != symbol_filter:
            continue
        start_ts = block.start_ts.isoformat() if block.start_ts else "N/A"
        end_ts = block.end_ts.isoformat() if block.end_ts else "open"
        status = "CLOSED" if block.is_closed else "OPEN"
        print(
            f"\nBlock {block.block_id}: {status} | {start_ts} → {end_ts} | "
            f"symbol={block.symbol or 'n/a'} strategy={block.strategy or 'n/a'} "
            f"type={block.block_type or 'n/a'} bot_state={block.bot_state or 'n/a'} "
            f"cycle={block.cycle_index or 'n/a'}"
        )
        print(f"  Fee rate: {fee_rate:.6f}")
        if block.recovery_event_id:
            print(f"  Recovery marker event_id={block.recovery_event_id}")
        if block.exit_armed_event_id:
            print(f"  Exit armed marker event_id={block.exit_armed_event_id}")
        if block.end_event_id:
            print(f"  Closed at event_id={block.end_event_id}")

        block_event_ids = {ev.event_id for ev in block.events if ev.event_id is not None}
        lifecycle_hits = sum(
            1
            for lifecycle in lifecycles.values()
            if any(ev.event_id in block_event_ids for ev in lifecycle.events if ev.event_id is not None)
        )
        summary = None
        if block.symbol and block.cycle_index is not None:
            summary = report_lookup.get((block.symbol, block.cycle_index))
        if summary:
            long_add = summary["long_add"]
            short_tp = summary["short_tp"]
            print(
                f"  Cycle summary: long_loss=${long_add['loss_usdt'] or 0:.4f}, "
                f"short_net=${short_tp['net_profit_usdt'] or 0:.4f}"
            )

        print(f"  Related lifecycles: {lifecycle_hits}")
        processed_events: List[NormalizedEvent] = []
        dedup_map: Dict[Tuple[str, str, str, float, float, float], Tuple[NormalizedEvent, int]] = {}
        runtime_preferred = {"order_submitted", "fill_received"}

        def render_key(event: NormalizedEvent) -> Tuple[str, str, str, float, float, float, str]:
            name = event.event_name or "unknown"
            return (
                name,
                (event.purpose or "").upper(),
                event.status or "",
                round(event.price or 0.0, 8),
                round(event.trigger_price or 0.0, 8),
                round(event.qty or 0.0, 4),
                event.reason or "",
            )

        def canonical_render_key(event: NormalizedEvent) -> Tuple[str, str, float, float, float]:
            return (
                event.event_name or "",
                (event.purpose or "").upper(),
                round(event.price or 0.0, 8),
                round(event.trigger_price or 0.0, 8),
                round(event.qty or 0.0, 4),
            )

        runtime_keys: Set[Tuple[str, str, str, float, float, float]] = set()
        for ev in block.events:
            if ev.source != "runtime":
                continue
            if not _is_block_signal_event(ev):
                continue
            name = ev.event_name or "unknown"
            if name in NO_RENDER_EVENT_NAMES:
                continue
            runtime_keys.add(canonical_render_key(ev))

        for ev in block.events:
            if not _is_block_signal_event(ev):
                continue
            event_name = ev.event_name or "unknown"
            if event_name in NO_RENDER_EVENT_NAMES:
                continue
            if event_name in ALWAYS_PRINT_EVENT_NAMES or (
                ev.level and ev.level.upper() in {"WARNING", "ERROR"}
            ):
                processed_events.append(ev)
                continue
            key = render_key(ev)
            if ev.source == "audit" and canonical_render_key(ev) in runtime_keys:
                continue
            existing = dedup_map.get(key)
            if existing is None:
                dedup_map[key] = (ev, len(processed_events))
                processed_events.append(ev)
            else:
                existing_event, idx = existing
                prefer_new = (
                    event_name in runtime_preferred
                    and ev.source == "runtime"
                    and existing_event.source != "runtime"
                )
                if prefer_new:
                    dedup_map[key] = (ev, idx)
                    processed_events[idx] = ev
                # otherwise keep the first seen event
        for ev in processed_events:
            event_name = ev.event_name or "unknown"
            ts = ev.timestamp.isoformat() if ev.timestamp else "N/A"
            details = []
            if ev.price is not None:
                details.append(f"price={ev.price:.6f}")
            if ev.qty is not None:
                details.append(f"qty={ev.qty:.3f}")
            if ev.trigger_price is not None:
                details.append(f"trigger={ev.trigger_price:.6f}")
            if ev.decision:
                details.append(f"decision={ev.decision}")
            if ev.reason:
                details.append(f"reason={ev.reason}")
            existing_trigger = ev.payload.get("existing_trigger_price")
            new_trigger = ev.payload.get("new_trigger_price")
            existing_qty = ev.payload.get("existing_qty")
            new_qty = ev.payload.get("new_qty")
            if existing_trigger is not None and new_trigger is not None:
                diff = abs(existing_trigger - new_trigger)
                details.append(f"trigger_diff={diff:.8f}")
            if existing_qty is not None and new_qty is not None:
                details.append(f"qty_diff={abs(existing_qty - new_qty):.6f}")
            if ev.closed_pnl is not None:
                details.append(f"closed_pnl={ev.closed_pnl:.4f}")
            detail_str = " ".join(details)
            print(
                f"  {ev.event_id or '??'} [{ev.source}] {ts} {event_name} "
                f"{ev.purpose or ''} {detail_str}".strip()
            )
            if ev.event_name == "intent_reuse_existing_order":
                print(f"    🔁 REUSED ORDER: {ev.purpose} {ev.side}")
            if ev.event_name == "intent_replaced_cancel":
                print(f"    ❌ REPLACED ORDER: {ev.purpose} {ev.side}")
                if ev.reason:
                    print(f"      reason={ev.reason}")
                existing_trigger = ev.payload.get("existing_trigger_price")
                new_trigger = ev.payload.get("new_trigger_price")
                existing_qty = ev.payload.get("existing_qty")
                new_qty = ev.payload.get("new_qty")
                if existing_trigger is not None and new_trigger is not None:
                    print(f"      trigger_diff={abs(existing_trigger - new_trigger):.8f}")
                if existing_qty is not None and new_qty is not None:
                    print(f"      qty_diff={abs(existing_qty - new_qty):.6f}")
            if ev.event_name == "intent_equivalence_check":
                print(
                    f"    🔍 CHECK: {ev.purpose} result={ev.payload.get('result')} "
                    f"reason={ev.payload.get('reject_reason')}"
                )
            if ev.event_name == "reconcile_guard_noop":
                print("    🟢 RECONCILE NOOP (structure unchanged)")
        reuse_count = sum(1 for ev in processed_events if ev.event_name == "intent_reuse_existing_order")
        replace_count = sum(1 for ev in processed_events if ev.event_name == "intent_replaced_cancel")
        check_count = sum(1 for ev in processed_events if ev.event_name == "intent_equivalence_check")
        print(f"  Decisions: reused={reuse_count} replaced={replace_count} checks={check_count}")


LOG_KEY_BLACKLIST = {"fill", "order", "exchange_order", "managed_order"}


def _format_state(state: Optional[Dict[str, Any]]) -> str:
    if not state:
        return "n/a"
    parts = [f"{key}={state[key]}" for key in sorted(state.keys())]
    return " | ".join(parts)


def _log_value_summary(event: NormalizedEvent, keys: Optional[Iterable[str]] = None) -> str:
    keys_to_render = []
    if keys:
        keys_to_render = [key for key in keys if key in event.payload]
    else:
        candidate_keys = [k for k in sorted(event.logged_fields) if k not in LOG_KEY_BLACKLIST]
        keys_to_render = candidate_keys[:4]
        if not keys_to_render and event.payload:
            keys_to_render = sorted(event.payload.keys())[:4]
    items = []
    for key in keys_to_render:
        value = event.payload.get(key)
        items.append(f"{key}={value!r}")
    return ", ".join(items) if items else "n/a"


def render_chains(
    chains: Sequence[EventChain],
    validations: Sequence[ChainValidation],
    summary: ChainSummary,
) -> None:
    if not chains:
        print("No long-add chains detected.")
        return
    validation_map = {val.chain_id: val for val in validations}
    for chain in chains:
        validation = validation_map.get(chain.chain_id)
        verdict = validation.status if validation else "UNKNOWN"
        trigger_label = chain.trigger_purpose or "LONG_ADD"
        if any(ev.purpose and "SHORT_TP" in ev.purpose.upper() for ev in chain.result_events):
            result_label = "SHORT_TP"
        elif any(ev.purpose and "EXIT" in ev.purpose.upper() for ev in chain.result_events):
            result_label = "EXIT_REBUILD"
        else:
            result_label = "SHORT_TP_LIFECYCLE"
        chain_type_label = chain.chain_type.value
        dedup_note = f" dedup={chain.dedup_reason}" if chain.dedup_reason else ""
        print(f"[{verdict}] {chain_type_label} {trigger_label} -> {result_label}{dedup_note}")
        trigger = chain.trigger_event
        trigger_ts = trigger.timestamp.isoformat() if trigger.timestamp else "N/A"
        print(f"\nCHAIN {chain.chain_id} | Trigger {trigger.event_name} @ {trigger_ts}")
        if validation:
            issue_summary = "; ".join(validation.issues) if validation.issues else "none"
            print(f"  Status={validation.status} issues={issue_summary}")
            if validation.tp_deviation is not None:
                print(f"    TP deviation: {validation.tp_deviation:.6f}")
            if validation.missing_runtime:
                print("    Missing runtime link for audit events")
            if validation.missing_audit:
                print("    Missing audit coverage for runtime order")

        print(
            f"  Purpose={chain.trigger_purpose} order_id={chain.trigger_order_id} "
            f"state_before={_format_state(chain.state_before)}"
        )
        if chain.chain_type == ChainType.LONG_ADD_ORDER_CHAIN:
            fill_status = "fill linked" if chain.linked_fill_chain_id else "no fill detected"
            print(f"  Fill trace: {fill_status}")
        print(f"  State after: {_format_state(chain.state_after)}")
        if chain.calculation_events:
            print("  Calculations:")
            for ev in chain.calculation_events:
                ts = ev.timestamp.isoformat() if ev.timestamp else "N/A"
                log_summary = _log_value_summary(ev)
                print(
                    f"    - {ev.event_name} [{ev.source}] @{ts} | log values=({log_summary})"
                )
            if chain.derived_summary:
                derived = ", ".join(f"{k}={v}" for k, v in chain.derived_summary.items())
                print(f"    Derived chain values: {derived}")
            expected_tp = chain.derived_summary.get("expected_tp_price")
            actual_tp = chain.derived_summary.get("actual_trigger_price") or chain.derived_summary.get(
                "normalized_trigger_price"
            )
            rel = chain.derived_summary.get("tp_relative_diff_pct")
            if expected_tp is not None and actual_tp is not None:
                abs_diff = abs(actual_tp - expected_tp)
                rel_diff = f"{rel:.4f}%" if rel is not None else "n/a"
                print(
                    f"    TP snapshot: expected={expected_tp} actual={actual_tp} "
                    f"abs_diff={abs_diff:.6f} rel_diff={rel_diff}"
                )
        else:
            print("  Calculations: none")
        if chain.order_chains:
            print("  Orders:")
            for idx, order in enumerate(chain.order_chains, 1):
                submitted_ts = (
                    order.submitted_event.timestamp.isoformat()
                    if order.submitted_event and order.submitted_event.timestamp
                    else "N/A"
                )
                submitted_price = (
                    order.submitted_event.payload.get("price")
                    if order.submitted_event
                    else None
                )
                print(
                    f"    {idx}. purpose={order.purpose} side={order.side} "
                    f"submitted=@{submitted_ts} price={submitted_price}"
                )
                if order.replacement:
                    print(
                        f"      Replacement: {order.replacement.old_order_id} "
                        f"→ {order.replacement.new_order_id} "
                        f"reason={order.replacement.reason}"
                    )
                if order.lifecycle_events:
                    lifecycle_summary = ", ".join(
                        f"{ev.event_name}[{ev.status or ''}]"
                        for ev in order.lifecycle_events
                        if ev.event_name
                    )
                    print(f"      Lifecycle: {lifecycle_summary}")
                if order.fill_events:
                    for fill in order.fill_events:
                        fill_ts = fill.timestamp.isoformat() if fill.timestamp else "N/A"
                        print(
                            f"      Fill: purpose={fill.purpose} qty={fill.qty} price={fill.price} status={fill.status} @{fill_ts}"
                        )
        else:
            print("  Orders: none")
        if chain.result_events:
            print("  Result events:")
            for ev in chain.result_events:
                ts = ev.timestamp.isoformat() if ev.timestamp else "N/A"
                log_summary = _log_value_summary(ev)
                print(
                    f"    - {ev.event_name} ({ev.purpose}) status={ev.status} "
                    f"price={ev.price} qty={ev.qty} @{ts} | log fields=({log_summary})"
                )
    print("\nChain validation summary:")
    print(f"  Total chains: {summary.total}")
    print(f"  Complete chains: {summary.complete}")
    print(f"  Broken chains: {summary.broken}")
    print(f"  Partial chains: {summary.partial}")
    print(f"  Mismatched TP: {summary.mismatch_tp}")
    print(f"  Missing runtime links: {summary.missing_runtime_links}")
    print(f"  Missing audit links: {summary.missing_audit_links}")
    print(f"  Replacement issues: {summary.replacement_issues}")
    print(f"  Waiting for fill: {summary.waiting_for_fill}")
    reason_counter = aggregate_reason_summary(validations)
    if reason_counter:
        print("Reason code counts:")
        for code, count in sorted(reason_counter.items()):
            print(f"  {code}: {count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze fixed/generic hedge logs")
    parser.add_argument(
        "--runtime-log",
        type=Path,
        default=Path("logs/fixed_cycle_hedge_runtime.log"),
        help="Runtime log path",
    )
    parser.add_argument(
        "--audit-log",
        type=Path,
        default=Path("logs/generic_hedge_runtime_audit.jsonl"),
        help="Audit log path",
    )
    parser.add_argument("--from", dest="since", help="Start timestamp (YYYY-MM-DD HH:MM:SS)")
    parser.add_argument("--to", dest="until", help="End timestamp (YYYY-MM-DD HH:MM:SS)")
    parser.add_argument("--symbol", help="Symbol filter")
    parser.add_argument("--level", help="Level filter")
    parser.add_argument("--event", help="Event name filter")
    parser.add_argument("--purpose", help="Purpose filter")
    parser.add_argument("--session", help="Session id (number or 'last')")
    parser.add_argument("--source", action="append", help="Source filter (runtime|audit)")
    parser.add_argument("--only-important", action="store_true", help="Show warnings/errors only")
    parser.add_argument("--only-anomalies", action="store_true", help="Show only anomaly events")
    parser.add_argument(
        "--fee-rate",
        type=float,
        default=0.00055,
        help="Fee rate per side, e.g. 0.00055 for 0.055%",
    )
    parser.add_argument(
        "--mode",
        choices=[
            "summary",
            "timeline",
            "orders",
            "sessions",
            "anomalies",
            "cycle-pnl",
            "json",
            "blocks",
            "chains",
        ],
        default="summary",
    )
    parser.add_argument("--only-issues", action="store_true", help="Show only chains with issues")
    parser.add_argument(
        "--chain-status",
        choices=["COMPLETE", "PARTIAL", "BROKEN", "MISMATCH_TP", "MISSING_ORDER", "MISSING_CALCULATION"],
        help="Filter chains by validation status",
    )
    parser.add_argument("--purpose-contains", help="Only show chains whose trigger purpose contains this substring")
    parser.add_argument("--cycle-index", type=int, help="Only show chains tied to this cycle index")
    parser.add_argument("--debug-long-add", action="store_true", help="Dump detection candidates for long-add chains")
    parser.add_argument("--block", help="Block id (number or 'last')")
    parser.add_argument("--gap-threshold", type=int, default=GAP_THRESHOLD_SECONDS)
    args = parser.parse_args()

    since_ts = (
        datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S") if args.since else None
    )
    until_ts = datetime.strptime(args.until, "%Y-%m-%d %H:%M:%S") if args.until else None
    runtime_events = parse_runtime_events(args.runtime_log)
    audit_events = parse_audit_events(args.audit_log)
    merged = merge_events(runtime_events, audit_events)
    sessions, event_to_session = group_sessions(merged, gap_threshold_seconds=args.gap_threshold)
    lifecycles = build_order_lifecycles(merged)
    anomalies = detect_anomalies(lifecycles)
    chains = build_event_chains(merged, lifecycles, debug_long_add=args.debug_long_add)
    cycle_pnl_reports = build_cycle_pnl_report(
        lifecycles,
        fee_rate=args.fee_rate,
        symbol_filter=args.symbol,
    )
    validations, chain_summary = validate_event_chains(chains, merged)
    blocks = build_run_blocks(merged)
    event_to_block = map_event_to_block(blocks)

    session_filter: Optional[Set[int]] = None
    if args.session:
        if args.session.lower() == "last":
            if sessions:
                session_filter = {sessions[-1].session_id}
        else:
            try:
                session_filter = {int(args.session)}
            except ValueError:
                pass
    block_filter: Optional[Set[int]] = None
    if args.block:
        if args.block.lower() == "last":
            if blocks:
                block_filter = {blocks[-1].block_id}
        else:
            try:
                block_id = int(args.block)
            except ValueError:
                block_id = None
            if block_id is not None and any(block.block_id == block_id for block in blocks):
                block_filter = {block_id}

    source_filter = set(args.source) if args.source else None
    filtered_events = filter_events(
        merged,
        since=since_ts,
        until=until_ts,
        symbol=args.symbol,
        event_name=args.event,
        level=args.level,
        purpose=args.purpose,
        sources=source_filter,
        session_ids=session_filter,
        only_important=args.only_important,
        only_anomalies=args.only_anomalies,
        anomalies=anomalies,
        event_to_session=event_to_session,
        block_ids=block_filter,
        event_to_block=event_to_block,
    )

    selected_blocks = [block for block in blocks if not block_filter or block.block_id in block_filter]

    if args.mode == "summary":
        render_summary(filtered_events, sessions, anomalies, lifecycles)
    elif args.mode == "timeline":
        render_timeline(filtered_events, event_to_session, sessions)
    elif args.mode == "orders":
        render_orders(lifecycles)
    elif args.mode == "sessions":
        render_sessions(sessions, anomalies)
    elif args.mode == "anomalies":
        render_anomalies(anomalies)
    elif args.mode == "cycle-pnl":
        render_cycle_pnl(cycle_pnl_reports)
    elif args.mode == "json":
        output = {
            "events": [event.__dict__ for event in filtered_events],
            "sessions": [session.__dict__ for session in sessions],
            "anomalies": [anomaly.__dict__ for anomaly in anomalies],
            "cycle_pnl": cycle_pnl_reports,
            "blocks": [
                {
                    "block_id": block.block_id,
                    "start_event_id": block.start_event_id,
                    "end_event_id": block.end_event_id,
                    "start_ts": block.start_ts,
                    "end_ts": block.end_ts,
                    "symbol": block.symbol,
                    "strategy": block.strategy,
                    "block_type": block.block_type,
                    "bot_state": block.bot_state,
                    "cycle_index": block.cycle_index,
                    "is_closed": block.is_closed,
                    "exit_armed_event_id": block.exit_armed_event_id,
                    "recovery_event_id": block.recovery_event_id,
                    "event_ids": [
                        ev.event_id for ev in block.events if ev.event_id is not None
                    ],
                }
                for block in blocks
            ],
        }
        print(json.dumps(output, default=str, indent=2))
    elif args.mode == "blocks":
        render_blocks(
            selected_blocks,
            lifecycles,
            cycle_pnl_reports,
            args.fee_rate,
            symbol_filter=args.symbol,
        )
    elif args.mode == "chains":
        filtered_chains, filtered_validations = filter_chain_validations(
            chains,
            validations,
            only_issues=args.only_issues,
            status=args.chain_status,
            purpose_contains=args.purpose_contains,
            cycle_index=args.cycle_index,
        )
        filtered_summary = summarize_validations(filtered_validations)
        render_chains(filtered_chains, filtered_validations, filtered_summary)


if __name__ == "__main__":
    main()

# Example invocations:
# python analyze_hedge_logs.py --summary
# python analyze_hedge_logs.py --timeline --session last
# python analyze_hedge_logs.py --anomalies
# python analyze_hedge_logs.py --orders --symbol XRPUSDT
# python analyze_hedge_logs.py --from "2026-04-12 11:30:00" --to "2026-04-12 12:00:00" --mode timeline
# python analyze_hedge_logs.py --purpose CYCLE_1_LONG_ADD --mode timeline
# python analyze_hedge_logs.py --source audit --event fill_received --mode json

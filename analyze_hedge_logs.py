#!/usr/bin/env python3
import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

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

    event_id: Optional[int] = None

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
        parsed = None
        if json_start != -1:
            candidate = message_body[json_start:]
            parsed = _safe_json_load(candidate)
            if parsed is not None:
                payload = parsed
                message_body = message_body[:json_start].strip()
        if not payload:
            payload = {"message": message_body}
        event_name = payload.get("event") or payload.get("event_name")
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
    )
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
        choices=["summary", "timeline", "orders", "sessions", "anomalies", "cycle-pnl", "json"],
        default="summary",
    )
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
    cycle_pnl_reports = build_cycle_pnl_report(
        lifecycles,
        fee_rate=args.fee_rate,
        symbol_filter=args.symbol,
    )

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
    )

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
        }
        print(json.dumps(output, default=str, indent=2))


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

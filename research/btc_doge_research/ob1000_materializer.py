"""Materialize OB1000 FS raw archive → research_ob1000_snapshots_1s (BTC+DOGE).

Reuses the OB200SegmentReader wire format (NDJSON zstd). No OB200 fallback.
Idempotent per (symbol, snapshot_ts, producer_id, build_id).
"""

from __future__ import annotations

import argparse
import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .clickhouse import connect, execute_ddl, insert, rows
from .config import OB1000_ROOT
from .contracts import (
    OB1000_CONTRACT_VERSION,
    TARGET_DATABASE,
    assert_target_database,
    utc,
)
from .ob1000_live_symbols import partition_ob1000_live_symbols, validate_ob1000_live_symbol
from .ob1000_storage import OB1000_MAX_DEPTH, OB1000_PRODUCER_ID, OB1000_SNAPSHOT_1S_COLUMNS, OB1000_TABLE
from .ob200_parser import OB200SegmentReader
from .phase2_ddl import statements as phase2_statements
from .phase2_transform import compact_ob_state
from .source_file_registry import SourceFile, sha256_file

CLOSED_RE = re.compile(
    r"^(?P<symbol>[A-Z0-9]+)_(?P<start>\d{8}T\d{6}Z)_(?P<end>\d{8}T\d{6}Z)_ob1000_v1\.zst$"
)
OPEN_RE = re.compile(
    r"^(?P<symbol>[A-Z0-9]+)_(?P<start>\d{8}T\d{6}Z)_open_ob1000_v1\.zst\.tmp$"
)
SOURCE_SEMANTICS = "ob1000_v1_live_archive_1s"


def _parse_stamp(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def _build_id(symbol: str, start: datetime, end: datetime, fingerprint: str) -> str:
    payload = f"{symbol}|{start.isoformat()}|{end.isoformat()}|{fingerprint}|{OB1000_CONTRACT_VERSION}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _open_segment_fingerprint(path: Path) -> str:
    """Stable-enough id for growing open segments (avoid full-file SHA each cycle)."""
    st = path.stat()
    payload = f"open|{path.name}|{st.st_size}|{st.st_mtime_ns}"
    return hashlib.sha256(payload.encode()).hexdigest()


def load_ob1000_source_file(path: Path, root: Path) -> SourceFile:
    """Load closed or open OB1000 archive segment (no OB200 format checks)."""
    import json

    resolved = path.resolve(strict=True)
    root_resolved = root.resolve(strict=True)
    if not resolved.is_relative_to(root_resolved):
        raise PermissionError(f"source outside OB1000 root: {path}")
    name = resolved.name
    is_open = bool(OPEN_RE.match(name))
    is_closed = bool(CLOSED_RE.match(name))
    if not is_open and not is_closed:
        raise ValueError(f"unsupported OB1000 segment name: {name}")
    relative = resolved.relative_to(root_resolved).as_posix()
    # Closed: full content hash. Open: size/mtime (file still growing).
    fingerprint = _open_segment_fingerprint(resolved) if is_open else sha256_file(resolved)
    manifest: dict[str, Any] = {}
    if is_closed:
        man_path = resolved.with_suffix(".manifest.json")
        if man_path.is_file():
            manifest = json.loads(man_path.read_text(encoding="utf-8"))
            if manifest.get("parser_version") not in {None, "ob1000_v1"}:
                if str(manifest.get("parser_version") or "") != "ob1000_v1":
                    raise ValueError(f"unexpected parser_version: {manifest.get('parser_version')}")
            if int(manifest.get("depth", 1000)) != 1000:
                raise ValueError("unexpected OB1000 depth")
    m = CLOSED_RE.match(name) or OPEN_RE.match(name)
    assert m is not None
    start = _parse_stamp(m.group("start"))
    if is_open:
        end = datetime.now(timezone.utc)
    else:
        end = _parse_stamp(m.group("end"))
    if manifest.get("start_utc"):
        start = datetime.fromisoformat(str(manifest["start_utc"]).replace("Z", "+00:00"))
    if manifest.get("end_utc"):
        end = datetime.fromisoformat(str(manifest["end_utc"]).replace("Z", "+00:00"))
    return SourceFile(
        path=resolved,
        relative_path=relative,
        fingerprint=fingerprint,
        source_file_id=hashlib.sha256(relative.encode()).hexdigest(),
        size=resolved.stat().st_size,
        manifest=manifest or {
            "parser_version": "ob1000_v1",
            "depth": 1000,
            "format_version": "ob1000_v1_live_archive/v1",
            "completion_status": "open" if is_open else "closed",
        },
        segment_start=start,
        segment_end=end,
    )


def iter_ob1000_segments(
    root: Path = OB1000_ROOT,
    *,
    symbols: frozenset[str] | None = None,
) -> Iterator[dict[str, Any]]:
    root = Path(root)
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*ob1000_v1*")):
        if not path.is_file():
            continue
        name = path.name
        m = CLOSED_RE.match(name)
        is_open = False
        if not m:
            m = OPEN_RE.match(name)
            is_open = True
        if not m:
            continue
        symbol = m.group("symbol")
        if symbols and symbol not in symbols:
            continue
        start = _parse_stamp(m.group("start"))
        if is_open:
            end = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=1)
        else:
            end = _parse_stamp(m.group("end"))
        yield {
            "path": path,
            "symbol": symbol,
            "start": start,
            "end": end,
            "is_open": is_open,
            "relative_path": str(path.relative_to(root)),
        }


def _ensure_table(client: Any) -> None:
    assert_target_database(TARGET_DATABASE)
    for sql in phase2_statements():
        if OB1000_TABLE in sql:
            execute_ddl(client, sql)
            return
    raise RuntimeError(f"{OB1000_TABLE} DDL missing from phase2_ddl")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return utc(value)


def _existing_seconds(
    client: Any, symbol: str, start: datetime, end: datetime, producer_id: str
) -> set[datetime]:
    sql = f"""
        SELECT snapshot_ts
        FROM {TARGET_DATABASE}.{OB1000_TABLE}
        WHERE symbol = %(symbol)s
          AND producer_id = %(producer)s
          AND snapshot_ts >= %(start)s
          AND snapshot_ts < %(end)s
    """
    return {
        _as_utc(r[0]).replace(microsecond=0)
        for r in rows(
            client,
            sql,
            {"symbol": symbol, "producer": producer_id, "start": start, "end": end},
        )
    }


def _find_prior_closed_segment(
    root: Path, symbol: str, before: datetime, *, exclude: Path | None = None
) -> Path | None:
    """Closed segment whose end stamp matches / precedes ``before``."""
    best: tuple[datetime, Path] | None = None
    exclude_res = exclude.resolve() if exclude is not None else None
    for segment in iter_ob1000_segments(root, symbols=frozenset({symbol})):
        if segment["is_open"]:
            continue
        path = Path(segment["path"])
        if exclude_res is not None and path.resolve() == exclude_res:
            continue
        end = utc(segment["end"])
        if end > before:
            continue
        if best is None or end > best[0]:
            best = (end, path)
    return None if best is None else best[1]


def _segment_starts_with_delta(path: Path) -> bool:
    from .ob200_parser import SUPPORTED_TYPES, iter_json_records

    for _record, obj in iter_json_records(path):
        event_type = str(obj.get("type", ""))
        if event_type in SUPPORTED_TYPES:
            return event_type == "delta"
    return False


def _warmup_sources_for_segment(
    root: Path, symbol: str, segment_path: Path, segment_start: datetime
) -> tuple[tuple[Any, ...], str | None]:
    """Build oldest→newest warmup chain when segment (or priors) start with deltas."""
    if not _segment_starts_with_delta(segment_path):
        return (), None
    chain_paths: list[Path] = []
    cursor = segment_start
    seen: set[Path] = set()
    while True:
        prior = _find_prior_closed_segment(
            root, symbol, cursor, exclude=segment_path
        )
        if prior is None:
            break
        resolved = prior.resolve()
        if resolved in seen:
            break
        seen.add(resolved)
        chain_paths.append(prior)
        if not _segment_starts_with_delta(prior):
            break
        name = prior.name
        m = CLOSED_RE.match(name)
        if not m:
            break
        cursor = _parse_stamp(m.group("start"))
    if not chain_paths:
        return (), None
    chain_paths.reverse()
    sources = tuple(load_ob1000_source_file(p, root) for p in chain_paths)
    return sources, sources[-1].relative_path


def materialize_segment(
    client: Any,
    *,
    segment: dict[str, Any],
    root: Path = OB1000_ROOT,
    now: datetime | None = None,
    expected_depth: int = OB1000_MAX_DEPTH,
) -> dict[str, Any]:
    now = utc(now or datetime.now(timezone.utc))
    symbol = validate_ob1000_live_symbol(segment["symbol"])
    start = utc(segment["start"])
    end = utc(segment["end"])
    source = load_ob1000_source_file(Path(segment["path"]), root)
    build_id = _build_id(symbol, start, end, source.fingerprint)
    existing = _existing_seconds(client, symbol, start, end, OB1000_PRODUCER_ID)
    warmup, warmup_path = _warmup_sources_for_segment(
        root, symbol, Path(segment["path"]), start
    )
    reader = OB200SegmentReader(
        source,
        symbol,
        max_depth=expected_depth,
        symbol_validator=validate_ob1000_live_symbol,
    )
    by_second: dict[datetime, tuple[Any, int]] = {}
    counts: dict[datetime, int] = {}
    parse_error: str | None = None
    try:
        for event in reader.iter_full_books(
            start, end, emit="second_end", warmup_sources=warmup
        ):
            second = event.event_time.replace(microsecond=0)
            if second < start or second >= end:
                continue
            if second in existing:
                continue
            counts[second] = counts.get(second, 0) + 1
            by_second[second] = (event, counts[second])
    except ValueError as exc:
        # Restart / rotation seams can briefly lack checkpoint continuity.
        # Keep other symbols/segments flowing; retry next cycle.
        parse_error = f"{type(exc).__name__}: {exc}"

    batch: list[tuple[Any, ...]] = []
    skipped = len(existing)
    for second in sorted(by_second):
        event, event_count = by_second[second]
        # Allow short books near open-segment warmup; mark genuine_depth=0.
        try:
            state = compact_ob_state(
                symbol, event.bids, event.asks, expected_depth=expected_depth
            )
        except ValueError:
            continue
        if state["bid_level_count"] < 1 or state["ask_level_count"] < 1:
            continue
        coverage = "COMPLETE" if state["genuine_depth"] else "PARTIAL"
        batch.append(
            (
                symbol,
                second,
                OB1000_PRODUCER_ID,
                state["bid_price_ticks"],
                state["bid_quantities"],
                state["ask_price_ticks"],
                state["ask_quantities"],
                state["best_bid"],
                state["best_ask"],
                state["mid"],
                state["spread"],
                state["bid_level_count"],
                state["ask_level_count"],
                state["genuine_depth"],
                "EVENT_TIME_END_OF_SECOND",
                event_count,
                event.event_time,
                event.update_id,
                "EXACT_EVENT_ORDER",
                source.fingerprint,
                OB1000_CONTRACT_VERSION,
                SOURCE_SEMANTICS,
                build_id,
                coverage,
                now,
            )
        )
    if batch:
        insert(client, OB1000_TABLE, batch, OB1000_SNAPSHOT_1S_COLUMNS)
    seconds_seen = len(existing) + len(by_second)
    # Closed + no pending new seconds after full parse ⇒ safe to cache-skip later.
    complete = (not bool(segment["is_open"])) and len(by_second) == 0 and len(existing) > 0
    return {
        "symbol": symbol,
        "relative_path": segment["relative_path"],
        "is_open": bool(segment["is_open"]),
        "seconds_seen": seconds_seen,
        "rows_inserted": len(batch),
        "rows_skipped_existing": skipped,
        "build_id": build_id,
        "source_fingerprint": source.fingerprint,
        "complete": complete,
        "warmup_relative_path": warmup_path,
        "error": parse_error,
    }


def materialize_all(
    *,
    symbols: tuple[str, ...] = ("BTCUSDT", "DOGEUSDT"),
    root: Path = OB1000_ROOT,
    include_open: bool = True,
    skip_completed: dict[str, str] | None = None,
) -> dict[str, Any]:
    client = connect()
    _ensure_table(client)
    accepted, rejected = partition_ob1000_live_symbols(symbols)
    if rejected:
        for bad, err in rejected:
            print(f"ob1000_materialize REJECT symbol={bad}: {err}", flush=True)
    if not accepted:
        raise ValueError(
            f"no valid OB1000 live symbols after validation (input={list(symbols)!r}, rejected={rejected!r})"
        )
    wanted = frozenset(accepted)
    results = []
    skipped_completed = 0
    for segment in iter_ob1000_segments(root, symbols=wanted):
        if segment["is_open"] and not include_open:
            continue
        rel = str(segment["relative_path"])
        if skip_completed is not None and not segment["is_open"] and rel in skip_completed:
            # Closed segments are immutable; fingerprint cache avoids re-SHA/re-parse.
            skipped_completed += 1
            results.append(
                {
                    "symbol": segment["symbol"],
                    "relative_path": rel,
                    "is_open": False,
                    "seconds_seen": 0,
                    "rows_inserted": 0,
                    "rows_skipped_existing": 0,
                    "build_id": "",
                    "source_fingerprint": skip_completed[rel],
                    "complete": True,
                    "skipped_completed_cache": True,
                }
            )
            continue
        results.append(materialize_segment(client, segment=segment, root=root))
    return {
        "table": f"{TARGET_DATABASE}.{OB1000_TABLE}",
        "root": str(root),
        "symbols": sorted(wanted),
        "segments": len(results),
        "rows_inserted": sum(int(r["rows_inserted"]) for r in results),
        "skipped_completed": skipped_completed,
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Materialize OB1000 FS → CH 1s snapshots")
    parser.add_argument("--symbols", default="BTCUSDT,DOGEUSDT")
    parser.add_argument("--root", default=str(OB1000_ROOT))
    parser.add_argument("--closed-only", action="store_true")
    args = parser.parse_args(argv)
    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    out = materialize_all(
        symbols=symbols,
        root=Path(args.root),
        include_open=not args.closed_only,
    )
    print(
        f"ob1000_materialize segments={out['segments']} rows_inserted={out['rows_inserted']} "
        f"table={out['table']}"
    )
    for r in out["results"]:
        print(
            f"  {r['symbol']} {r['relative_path']} inserted={r['rows_inserted']} "
            f"skipped={r['rows_skipped_existing']} seen={r['seconds_seen']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

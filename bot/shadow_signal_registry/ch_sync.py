"""Sync registry snapshots to ClickHouse (live_forward)."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

import bot.shadow_signal_registry.store as registry_store

from bot.shadow_signal_registry.ch_config import (
    SHADOW_CH_DATABASE,
    load_shadow_ch_config,
    shadow_ch_sync_enabled,
)

logger = logging.getLogger(__name__)

Side = registry_store.Side

HASH_FIELDS = (
    "signal_status",
    "allowed",
    "blocked",
    "hypothetical",
    "block_reason",
    "tracking_status",
    "outcome",
    "entry_price",
    "initial_sl",
    "active_sl",
    "tp",
    "pnl_pct",
    "mae_pct",
    "mfe_pct",
    "be_triggered",
    "be_trigger_time",
    "last_processed_1m",
    "updated_at",
    "exit_time",
    "ladder24_pass",
    "m15_lower_2_age_h",
    "e1r_state",
    "e1r_block_reason",
    "floor_guard_state",
)

LONG_TABLE = f"{SHADOW_CH_DATABASE}.shadow_signals_long"
SHORT_TABLE = f"{SHADOW_CH_DATABASE}.shadow_signals_short"

LONG_COLUMNS = [
    "signal_id",
    "strategy_name",
    "strategy_version",
    "symbol",
    "side",
    "decision_time",
    "detected_at",
    "end_time",
    "signal_status",
    "allowed",
    "blocked",
    "hypothetical",
    "block_reason",
    "raw_block_reason",
    "entry_price",
    "initial_sl",
    "active_sl",
    "tp",
    "pool_id",
    "ladder24_pass",
    "m15_lower_2_age_h",
    "be_trigger_pct",
    "be_triggered",
    "be_trigger_time",
    "tracking_status",
    "outcome",
    "pnl_pct",
    "mae_pct",
    "mfe_pct",
    "duration_min",
    "horizon_time",
    "backfilled",
    "backfill_source",
    "last_processed_1m",
    "causality_status",
    "created_at",
    "updated_at",
    "version",
]

SHORT_COLUMNS = [
    "signal_id",
    "strategy_name",
    "strategy_version",
    "symbol",
    "side",
    "decision_time",
    "detected_at",
    "end_time",
    "signal_status",
    "allowed",
    "blocked",
    "hypothetical",
    "block_reason",
    "raw_block_reason",
    "entry_price",
    "initial_sl",
    "active_sl",
    "tp",
    "pool_id",
    "e1r_state",
    "e1r_block_reason",
    "floor_guard_state",
    "tracking_status",
    "outcome",
    "pnl_pct",
    "mae_pct",
    "mfe_pct",
    "duration_min",
    "horizon_time",
    "backfilled",
    "backfill_source",
    "last_processed_1m",
    "causality_status",
    "created_at",
    "updated_at",
    "version",
]


class ChClient(Protocol):
    def insert(self, table: str, data: list, column_names: list[str]) -> None: ...
    def command(self, cmd: str) -> None: ...
    def query(self, sql: str, parameters: dict | None = None) -> Any: ...
    def close(self) -> None: ...


def pending_path(side: Side) -> Path:
    return registry_store.RUNTIME / f"ch_pending_{side}.jsonl"


def _parse_utc(iso: str | None) -> datetime | None:
    if not iso:
        return None
    dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _ch_datetime(dt: datetime | None) -> datetime | None:
    """Naive UTC for ClickHouse DateTime64('UTC') inserts."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def version_from_updated_at(updated_at: str | None) -> int:
    if not updated_at:
        return 0
    dt = _parse_utc(updated_at)
    assert dt is not None
    return int(dt.timestamp() * 1000)


def content_hash(row: dict[str, Any]) -> str:
    payload = {k: row.get(k) for k in HASH_FIELDS}
    payload["end_time"] = row.get("exit_time")
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _u8(val: Any) -> int:
    return 1 if val else 0


def _nullable_u8(val: Any) -> int | None:
    if val is None:
        return None
    return 1 if val else 0


def snapshot_to_ch_row(side: Side, row: dict[str, Any]) -> dict[str, Any]:
    updated_at = row.get("updated_at") or row.get("created_at") or row.get("decision_time")
    base: dict[str, Any] = {
        "signal_id": row["signal_id"],
        "strategy_name": row.get("strategy_name") or "",
        "strategy_version": row.get("strategy_version") or "",
        "symbol": row.get("symbol") or "",
        "side": (row.get("side") or side.upper()).upper(),
        "decision_time": _ch_datetime(_parse_utc(row.get("decision_time"))),
        "detected_at": _ch_datetime(_parse_utc(row.get("detected_at") or row.get("decision_time"))),
        "end_time": _ch_datetime(_parse_utc(row.get("exit_time"))),
        "signal_status": row.get("signal_status") or "",
        "allowed": _u8(row.get("allowed")),
        "blocked": _u8(row.get("blocked")),
        "hypothetical": _u8(row.get("hypothetical")),
        "block_reason": row.get("block_reason") or "NONE",
        "raw_block_reason": row.get("raw_block_reason"),
        "entry_price": float(row.get("entry_price") or 0),
        "initial_sl": float(row.get("initial_sl") or 0),
        "active_sl": float(row.get("active_sl") or 0),
        "tp": float(row.get("tp") or 0),
        "pool_id": row.get("pool_id") or "",
        "tracking_status": row.get("tracking_status") or "OPEN",
        "outcome": row.get("outcome"),
        "pnl_pct": row.get("pnl_pct"),
        "mae_pct": float(row.get("mae_pct") or 0),
        "mfe_pct": float(row.get("mfe_pct") or 0),
        "duration_min": row.get("duration_min"),
        "horizon_time": _ch_datetime(_parse_utc(row.get("horizon_time"))),
        "backfilled": _u8(row.get("backfilled")),
        "backfill_source": row.get("backfill_source"),
        "last_processed_1m": _ch_datetime(_parse_utc(row.get("last_processed_1m"))),
        "causality_status": row.get("causality_status") or "PASS",
        "created_at": _ch_datetime(_parse_utc(row.get("created_at") or row.get("decision_time"))),
        "updated_at": _ch_datetime(_parse_utc(updated_at)),
        "version": version_from_updated_at(updated_at),
    }
    if side == "long":
        base.update(
            {
                "ladder24_pass": _nullable_u8(row.get("ladder24_pass")),
                "m15_lower_2_age_h": row.get("m15_lower_2_age_h"),
                "be_trigger_pct": row.get("be_trigger_pct"),
                "be_triggered": _u8(row.get("be_triggered")),
                "be_trigger_time": _ch_datetime(_parse_utc(row.get("be_trigger_time"))),
            }
        )
    else:
        base.update(
            {
                "e1r_state": row.get("e1r_state"),
                "e1r_block_reason": row.get("e1r_block_reason"),
                "floor_guard_state": row.get("floor_guard_state"),
            }
        )
    return base


def _row_to_insert_tuple(columns: list[str], row: dict[str, Any]) -> list[Any]:
    return [row.get(c) for c in columns]


def _load_pending(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _save_pending(path: Path, rows: list[dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")
    tmp.replace(path)


def _append_pending(path: Path, ch_rows: list[dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for r in ch_rows:
            f.write(json.dumps(r, default=str) + "\n")


def _deserialize_pending_row(side: Side, raw: dict[str, Any]) -> dict[str, Any]:
    """Rehydrate datetimes from JSON pending spool."""
    out = dict(raw)
    for key in (
        "decision_time",
        "detected_at",
        "end_time",
        "be_trigger_time",
        "horizon_time",
        "last_processed_1m",
        "created_at",
        "updated_at",
    ):
        if key in out and isinstance(out[key], str):
            out[key] = _parse_utc(out[key])
    if side == "long" and "be_trigger_time" not in out:
        pass
    return out


def _insert_batch(client: ChClient, side: Side, ch_rows: list[dict[str, Any]]) -> None:
    if not ch_rows:
        return
    table = LONG_TABLE if side == "long" else SHORT_TABLE
    columns = LONG_COLUMNS if side == "long" else SHORT_COLUMNS
    data = [_row_to_insert_tuple(columns, r) for r in ch_rows]
    client.insert(table, data, column_names=columns)


def _get_client() -> ChClient:
    import clickhouse_connect

    cfg = load_shadow_ch_config()
    kwargs = cfg.connect_kwargs()
    # Tables use live_forward.* FQN; connect via default until DB exists.
    kwargs["database"] = "signal_generator"
    return clickhouse_connect.get_client(**kwargs)


def apply_ch_schema(client: ChClient | None = None) -> None:
    own = client is None
    if own:
        client = _get_client()
    schema_path = Path(__file__).resolve().parent / "ch_schema.sql"
    sql = schema_path.read_text(encoding="utf-8")
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    for stmt in statements:
        client.command(stmt)
    if own:
        client.close()


def sync_snapshots_to_clickhouse(
    side: Side,
    snapshots: list[dict[str, Any]],
    ch_last_hash: dict[str, str],
    *,
    client: ChClient | None = None,
    force: bool = False,
) -> None:
    """Best-effort sync; never raises to caller."""
    if not force and not shadow_ch_sync_enabled():
        return
    own_client = client is None
    if own_client:
        try:
            client = _get_client()
        except Exception as exc:  # noqa: BLE001
            logger.warning("ch_sync client failed side=%s: %s", side, exc)
            return
    assert client is not None
    pending = pending_path(side)
    to_insert: list[dict[str, Any]] = []
    try:
        for raw in _load_pending(pending):
            to_insert.append(_deserialize_pending_row(side, raw))

        for row in snapshots:
            if not row.get("signal_id"):
                continue
            h = content_hash(row)
            sid = row["signal_id"]
            if not force and ch_last_hash.get(sid) == h:
                continue
            to_insert.append(snapshot_to_ch_row(side, row))

        if not to_insert:
            return

        _insert_batch(client, side, to_insert)
        if pending.is_file():
            pending.unlink()
        for row in snapshots:
            sid = row.get("signal_id")
            if sid:
                ch_last_hash[sid] = content_hash(row)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ch_sync insert failed side=%s: %s", side, exc)
        try:
            serializable: list[dict[str, Any]] = []
            for r in to_insert:
                ser = {
                    k: (v.isoformat() if isinstance(v, datetime) else v)
                    for k, v in r.items()
                }
                serializable.append(ser)
            _save_pending(pending, serializable)
        except Exception:  # noqa: BLE001
            pass
    finally:
        if own_client:
            client.close()


def flush_ch_sync_hook(side: Side, snapshots: list[dict[str, Any]], ch_last_hash: dict[str, str]) -> None:
    try:
        sync_snapshots_to_clickhouse(side, snapshots, ch_last_hash)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ch_sync hook failed side=%s: %s", side, exc)


def fetch_latest_from_ch(side: Side, client: ChClient | None = None) -> list[dict[str, Any]]:
    own = client is None
    if own:
        client = _get_client()
    view = (
        f"{SHADOW_CH_DATABASE}.shadow_signals_long_latest"
        if side == "long"
        else f"{SHADOW_CH_DATABASE}.shadow_signals_short_latest"
    )
    result = client.query(f"SELECT * FROM {view}")
    cols = result.column_names
    rows = []
    for tup in result.result_rows:
        rows.append(dict(zip(cols, tup)))
    if own:
        client.close()
    return rows

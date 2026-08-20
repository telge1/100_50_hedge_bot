"""Factory for OrderBookEventSource implementations."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from orderbook_analyse.dynamic_wall_detector import ReadOnlyClickHouse
from orderbook_analyse.ob_data_source.clickhouse_source import ClickHouseOrderBookEventSource
from orderbook_analyse.ob_data_source.ob200_file_source import Ob200FileOrderBookEventSource
from orderbook_analyse.ob_data_source.protocol import OrderBookEventSource

ObSourceName = Literal["clickhouse", "files"]


def create_orderbook_event_source(
    ob_source: str = "clickhouse",
    *,
    db: ReadOnlyClickHouse | None = None,
    files_root: Path | str | None = None,
    file_pattern: str = "*/*.data",
    strict: bool = True,
    boundary_dedupe: bool = True,
) -> OrderBookEventSource:
    """Create an orderbook event source. Default is ClickHouse (unchanged behaviour)."""
    name = str(ob_source).strip().lower()
    if name == "clickhouse":
        return ClickHouseOrderBookEventSource(db=db)
    if name == "files":
        if files_root is None:
            raise ValueError("files_root is required when ob_source='files'")
        return Ob200FileOrderBookEventSource(
            files_root,
            file_pattern=file_pattern,
            strict=strict,
            boundary_dedupe=boundary_dedupe,
        )
    raise ValueError(f"unsupported ob_source={ob_source!r}; expected clickhouse|files")


def source_kind(source: Any) -> str:
    return getattr(source, "source_name", type(source).__name__)

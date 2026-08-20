"""Factory for PublicTradeSource implementations."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from orderbook_analyse.dynamic_wall_detector import ReadOnlyClickHouse
from orderbook_analyse.public_trade_source.clickhouse_source import ClickHousePublicTradeSource
from orderbook_analyse.public_trade_source.csv_gzip_source import CsvGzipPublicTradeSource
from orderbook_analyse.public_trade_source.protocol import PublicTradeSource

TradesSourceName = Literal["clickhouse", "files"]


def create_public_trade_source(
    trades_source: str = "clickhouse",
    *,
    db: ReadOnlyClickHouse | None = None,
    files_root: Path | str | None = None,
    file_pattern: str = "*.csv.gz",
    strict: bool = True,
    allow_partial_coverage: bool = False,
    dedupe: bool = True,
) -> PublicTradeSource:
    """Create a public-trade source. Default is ClickHouse."""
    name = str(trades_source).strip().lower()
    if name == "clickhouse":
        return ClickHousePublicTradeSource(db=db)
    if name == "files":
        if files_root is None:
            raise ValueError("files_root is required when trades_source='files'")
        return CsvGzipPublicTradeSource(
            files_root,
            file_pattern=file_pattern,
            strict=strict,
            allow_partial_coverage=allow_partial_coverage,
            dedupe=dedupe,
        )
    raise ValueError(
        f"unsupported trades_source={trades_source!r}; expected clickhouse|files"
    )


def source_kind(source: Any) -> str:
    return getattr(source, "source_name", type(source).__name__)

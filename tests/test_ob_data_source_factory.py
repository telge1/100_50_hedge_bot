"""Factory defaults and ClickHouse/file wiring."""

from __future__ import annotations

from pathlib import Path

import pytest

from orderbook_analyse.ob_data_source.clickhouse_source import ClickHouseOrderBookEventSource
from orderbook_analyse.ob_data_source.factory import create_orderbook_event_source
from orderbook_analyse.ob_data_source.ob200_file_source import Ob200FileOrderBookEventSource


def test_factory_default_is_clickhouse() -> None:
    src = create_orderbook_event_source()
    assert isinstance(src, ClickHouseOrderBookEventSource)
    assert src.source_name == "clickhouse"


def test_factory_clickhouse_explicit() -> None:
    src = create_orderbook_event_source("clickhouse")
    assert isinstance(src, ClickHouseOrderBookEventSource)


def test_factory_files_requires_root() -> None:
    with pytest.raises(ValueError, match="files_root"):
        create_orderbook_event_source("files")


def test_factory_files(tmp_path: Path) -> None:
    src = create_orderbook_event_source("files", files_root=tmp_path)
    assert isinstance(src, Ob200FileOrderBookEventSource)
    assert src.source_name == "files"


def test_factory_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        create_orderbook_event_source("duckdb")

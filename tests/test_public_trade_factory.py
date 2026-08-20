"""Factory tests for public trade sources."""

from __future__ import annotations

from pathlib import Path

import pytest

from orderbook_analyse.public_trade_source.clickhouse_source import ClickHousePublicTradeSource
from orderbook_analyse.public_trade_source.csv_gzip_source import CsvGzipPublicTradeSource
from orderbook_analyse.public_trade_source.factory import create_public_trade_source


def test_default_is_clickhouse() -> None:
    assert isinstance(create_public_trade_source(), ClickHousePublicTradeSource)


def test_files(tmp_path: Path) -> None:
    src = create_public_trade_source("files", files_root=tmp_path)
    assert isinstance(src, CsvGzipPublicTradeSource)


def test_unknown() -> None:
    with pytest.raises(ValueError):
        create_public_trade_source("kafka")

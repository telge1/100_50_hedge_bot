"""OB200 Bybit day-file importer writes only research_ob200_snapshots_1s."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from research.btc_doge_research.ob200_day_zip_importer import (
    DAY_ZIP_PRODUCER_ID,
    OB200_TABLE,
    iter_day_snapshots,
)


def _line(ts_ms: int, update_id: int, bids: list, asks: list, *, kind: str = "snapshot") -> str:
    return json.dumps(
        {
            "topic": "orderbook.200.DOGEUSDT",
            "type": kind,
            "ts": ts_ms,
            "cts": ts_ms - 1,
            "data": {
                "s": "DOGEUSDT",
                "b": bids,
                "a": asks,
                "u": update_id,
                "seq": update_id,
            },
        }
    )


def test_end_of_second_keeps_last_book(tmp_path: Path) -> None:
    day = datetime(2026, 1, 6, tzinfo=timezone.utc)
    base_ms = int(day.timestamp() * 1000)
    path = tmp_path / "2026-01-06_DOGEUSDT_ob200.data"
    path.write_text(
        "\n".join(
            [
                _line(base_ms + 100, 1, [["0.10000", "1"]], [["0.10010", "2"]]),
                _line(
                    base_ms + 400,
                    2,
                    [["0.10000", "5"]],
                    [["0.10010", "2"]],
                    kind="delta",
                ),
                _line(
                    base_ms + 1500,
                    3,
                    [["0.10000", "5"]],
                    [["0.10010", "9"]],
                    kind="delta",
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    snaps = list(iter_day_snapshots(path, "DOGEUSDT", "2026-01-06"))
    assert [snap["snapshot_ts"] for snap in snaps] == [
        day,
        datetime.fromtimestamp((base_ms // 1000) + 1, tz=timezone.utc),
    ]
    assert format(snaps[0]["state"]["bid_quantities"][0], "f") == "5"
    assert format(snaps[1]["state"]["ask_quantities"][0], "f") == "9"
    assert snaps[0]["source_event_count"] == 2
    assert snaps[1]["source_event_count"] == 1


def test_table_and_producer_stay_on_ob200() -> None:
    assert OB200_TABLE == "research_ob200_snapshots_1s"
    assert "ob1000" not in OB200_TABLE
    assert DAY_ZIP_PRODUCER_ID == "BYBIT_OB200_DAY_ZIP_IMPORTER_V3"
    source = Path(__file__).resolve().parents[2] / "research/btc_doge_research/ob200_day_zip_importer.py"
    text = source.read_text(encoding="utf-8")
    assert "research_ob1000_snapshots_1s" not in text

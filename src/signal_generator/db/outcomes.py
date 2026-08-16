"""Repository for multi-horizon signal outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Sequence
from uuid import UUID

from signal_generator.db.client import ClickHouseClient

OUTCOME_COLUMNS = [
    "signal_id",
    "evaluated_at",
    "horizon",
    "mfe_pct",
    "mae_pct",
    "tp_hit",
    "sl_hit",
    "time_to_tp_seconds",
    "time_to_sl_seconds",
    "price_after_horizon",
    "metadata",
    "ingested_at",
]

HORIZON_TRADE = "TRADE"
HORIZON_TRADE_NO_BE50 = "TRADE_NO_BE50"


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


@dataclass(slots=True)
class SignalOutcome:
    signal_id: UUID
    horizon: str
    evaluated_at: datetime | None = None
    mfe_pct: float | None = None
    mae_pct: float | None = None
    tp_hit: bool = False
    sl_hit: bool = False
    time_to_tp_seconds: int | None = None
    time_to_sl_seconds: int | None = None
    price_after_horizon: Decimal | float | str | None = None
    metadata: str = "{}"
    ingested_at: datetime | None = None

    def as_row(self) -> list[Any]:
        evaluated = self.evaluated_at or datetime.now(timezone.utc)
        ingested = self.ingested_at or datetime.now(timezone.utc)
        return [
            self.signal_id,
            _ensure_utc(evaluated),
            self.horizon,
            self.mfe_pct,
            self.mae_pct,
            1 if self.tp_hit else 0,
            1 if self.sl_hit else 0,
            self.time_to_tp_seconds,
            self.time_to_sl_seconds,
            self.price_after_horizon,
            self.metadata,
            _ensure_utc(ingested),
        ]


class SignalOutcomeRepository:
    TABLE = "signal_outcomes"

    def __init__(self, client: ClickHouseClient) -> None:
        self._client = client

    def insert_signal_outcome(self, outcome: SignalOutcome) -> None:
        self.insert_signal_outcomes([outcome])

    def insert_signal_outcomes(self, outcomes: Sequence[SignalOutcome]) -> int:
        if not outcomes:
            return 0
        rows = [o.as_row() for o in outcomes]
        self._client.insert(self.TABLE, rows, OUTCOME_COLUMNS)
        return len(rows)

    def get_outcomes(self, signal_id: UUID) -> list[dict[str, Any]]:
        result = self._client.query(
            f"""
            SELECT
                signal_id, evaluated_at, horizon,
                mfe_pct, mae_pct, tp_hit, sl_hit,
                time_to_tp_seconds, time_to_sl_seconds,
                price_after_horizon, metadata, ingested_at
            FROM {self._client.database}.{self.TABLE} FINAL
            WHERE signal_id = {{signal_id:UUID}}
            ORDER BY horizon ASC
            """,
            parameters={"signal_id": signal_id},
        )
        return [dict(zip(result.column_names, row, strict=True)) for row in result.result_rows]

    def get_outcomes_by_signal_ids(
        self,
        signal_ids: Sequence[UUID | str],
        *,
        horizon: str = HORIZON_TRADE,
    ) -> dict[str, Any]:
        """Batch-load outcomes for one horizon → ``signal_id`` → TradeOutcomeView."""
        from signal_generator.pipeline.outcome_eval import trade_outcome_from_metadata

        ids = [UUID(str(s)) for s in signal_ids if s]
        if not ids:
            return {}
        result = self._client.query(
            f"""
            SELECT
                signal_id, evaluated_at, horizon,
                mfe_pct, mae_pct, tp_hit, sl_hit,
                time_to_tp_seconds, time_to_sl_seconds,
                price_after_horizon, metadata, ingested_at
            FROM {self._client.database}.{self.TABLE} FINAL
            WHERE horizon = {{horizon:String}}
              AND signal_id IN {{ids:Array(UUID)}}
            """,
            parameters={"horizon": str(horizon), "ids": ids},
        )
        out: dict[str, Any] = {}
        for row in result.result_rows:
            d = dict(zip(result.column_names, row, strict=True))
            sid = str(d["signal_id"])
            view = trade_outcome_from_metadata(d.get("metadata"), signal_id=sid)
            if view is not None:
                out[sid] = view
        return out

    def get_trade_outcomes_by_signal_ids(
        self, signal_ids: Sequence[UUID | str]
    ) -> dict[str, Any]:
        """Batch-load TRADE horizon outcomes → ``signal_id`` → TradeOutcomeView."""
        return self.get_outcomes_by_signal_ids(signal_ids, horizon=HORIZON_TRADE)

    def get_no_be50_outcomes_by_signal_ids(
        self, signal_ids: Sequence[UUID | str]
    ) -> dict[str, Any]:
        """Batch-load TRADE_NO_BE50 outcomes (NO_BE50 TP/SL path)."""
        return self.get_outcomes_by_signal_ids(signal_ids, horizon=HORIZON_TRADE_NO_BE50)

    def delete_for_signal_ids(self, signal_ids: Sequence[UUID]) -> None:
        if not signal_ids:
            return
        self._client.command(
            f"""
            ALTER TABLE {self._client.database}.{self.TABLE}
            DELETE WHERE signal_id IN {{ids:Array(UUID)}}
            """,
            parameters={"ids": list(signal_ids)},
        )

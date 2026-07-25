"""Load candles and run the Cobertura engine; write artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol

from .config import CoberturaConfig
from .engine import CoberturaEngine, EngineResult, _parse_ts
from .report import write_run_artifacts


class CoberturaRunError(ValueError):
    pass


def resolve_start_index(
    candles: Sequence[dict[str, Any]], cfg: CoberturaConfig
) -> int:
    target = _parse_ts(cfg.start_timestamp)
    for i, row in enumerate(candles):
        if _parse_ts(row["timestamp"]) == target:
            return i
    raise CoberturaRunError(
        f"start_timestamp {cfg.start_timestamp!r} not found in candle series"
    )


def resolve_end_index(
    candles: Sequence[dict[str, Any]], cfg: CoberturaConfig, start_index: int
) -> int:
    if cfg.end_timestamp is None:
        return len(candles) - 1
    target = _parse_ts(cfg.end_timestamp)
    for i in range(start_index, len(candles)):
        if _parse_ts(candles[i]["timestamp"]) == target:
            return i
    raise CoberturaRunError(
        f"end_timestamp {cfg.end_timestamp!r} not found after start"
    )


def resolve_start_price(
    candles: Sequence[dict[str, Any]], cfg: CoberturaConfig, start_index: int
) -> float:
    src = cfg.start_price_source
    row = candles[start_index]
    if src == "config_start_price":
        return float(cfg.start_price)
    if src == "candle_open":
        return float(row["open"])
    if src == "candle_close":
        return float(row["close"])
    raise CoberturaRunError(f"unknown start_price_source: {src}")


def run_cobertura(
    cfg: CoberturaConfig,
    *,
    candles: list[dict[str, Any]] | None = None,
    write_outputs: bool = True,
    data_dir: str | Path = DEFAULT_DATA_DIR,
) -> EngineResult:
    """Run one Cobertura recovery backtest."""
    cfg.validate()
    if candles is None:
        candles = load_candles_for_symbol(
            cfg.symbol,
            timeframe=cfg.timeframe,
            data_dir=data_dir,
            limit=cfg.candle_limit,
        )
    if not candles:
        raise CoberturaRunError("no candles loaded")

    start_index = resolve_start_index(candles, cfg)
    end_index = resolve_end_index(candles, cfg, start_index)
    start_price = resolve_start_price(candles, cfg, start_index)

    # Bind resolved reference into a copy-like mutation of cfg for this run.
    cfg.start_price = float(start_price)

    engine = CoberturaEngine(cfg)
    for i in range(start_index, end_index + 1):
        engine.process_candle(candles[i])
        if engine.state in ("RECOVERED", "STOPPED"):
            break

    result = engine.finalize(start_index=start_index)

    if write_outputs:
        out = Path(
            cfg.output_dir
            or (
                Path(__file__).resolve().parent
                / "results"
                / (
                    cfg.run_id
                    or f"{cfg.symbol.lower()}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
                )
            )
        )
        write_run_artifacts(out, result)

    return result


def run_from_json(
    path: str | Path,
    *,
    write_outputs: bool = True,
) -> EngineResult:
    cfg = CoberturaConfig.from_json(path)
    return run_cobertura(cfg, write_outputs=write_outputs)

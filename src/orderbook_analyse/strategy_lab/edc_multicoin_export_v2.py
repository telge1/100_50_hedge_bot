"""EDC M0 multicoin export + CLI orchestration (P2D3).

Loads Strategy YAML → P4C/compile → P2D2 runner → deterministic artifacts.
No detection, PnL, or cost logic; no Cluster; no global ClickHouse client.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from orderbook_analyse.strategy_lab.adapters.edc_io import ClickHouseQueryClient
from orderbook_analyse.strategy_lab.compiler_v2 import compile_strategy_v2
from orderbook_analyse.strategy_lab.decoder_v2 import load_strategy_v2_yaml_file
from orderbook_analyse.strategy_lab.edc_multicoin_v2 import (
    EdcMulticoinRunV2,
    StrategyMulticoinError,
    run_edc_m0_multicoin_v2,
)
from orderbook_analyse.strategy_lab.models.signals import PluginSignalSpec
from orderbook_analyse.strategy_lab.models.strategy_v2 import StrategySpecV2
from orderbook_analyse.strategy_lab.results_v2 import (
    StrategyRunResultV2,
    StrategyTradeV2,
    TradeExitReasonV2,
)
from orderbook_analyse.strategy_lab.validation.catalogs import production_catalog_bundle_v2

_EXPORT_FORMAT_VERSION = "edc_multicoin_export/v1"
_ZERO = timedelta(0)
_RESOLVED = frozenset(
    {
        TradeExitReasonV2.TP_EXIT,
        TradeExitReasonV2.SL_EXIT,
        TradeExitReasonV2.TIME_EXIT,
    }
)

_COIN_SUMMARY_COLUMNS = (
    "symbol",
    "status",
    "error_type",
    "error_message",
    "candidate_count",
    "trade_count",
    "gross_pnl_usdt",
    "costs_usdt",
    "net_pnl_usdt",
    "winning_trades",
    "losing_trades",
    "unresolved_trades",
    "win_rate",
    "avg_net_pnl_usdt",
)

_TRADE_COLUMNS = (
    "strategy_hash",
    "plugin_id",
    "symbol",
    "source_event_id",
    "side",
    "decision_time",
    "entry_time",
    "entry_price",
    "exit_time",
    "exit_price",
    "exit_reason",
    "gross_return_pct",
    "roundtrip_cost_pct",
    "net_return_pct",
    "gross_pnl_usdt",
    "costs_usdt",
    "net_pnl_usdt",
    "mode_id",
    "confirmation_policy",
)


class StrategyMulticoinExportError(ValueError):
    """Deterministic export / CLI rejection."""


def export_edc_multicoin_artifacts_v2(
    run: EdcMulticoinRunV2,
    spec: StrategySpecV2,
    *,
    output_dir: Path,
) -> None:
    """Write run_manifest.json, coin_summary.csv, trades.csv, failures.json."""
    if type(run) is not EdcMulticoinRunV2:
        raise StrategyMulticoinExportError("run must be EdcMulticoinRunV2")
    if type(spec) is not StrategySpecV2:
        raise StrategyMulticoinExportError("spec must be StrategySpecV2")
    if not isinstance(output_dir, Path):
        raise StrategyMulticoinExportError("output_dir must be pathlib.Path")
    if type(spec.signal) is not PluginSignalSpec:
        raise StrategyMulticoinExportError("spec.signal must be PluginSignalSpec")

    output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(output_dir / "run_manifest.json", _manifest_json(run, spec))
    _atomic_write_text(output_dir / "coin_summary.csv", _coin_summary_csv(run))
    _atomic_write_text(output_dir / "trades.csv", _trades_csv(run))
    _atomic_write_text(output_dir / "failures.json", _failures_json(run))


def run_and_export_edc_m0_multicoin_v2(
    *,
    strategy_path: Path,
    universe_path: Path,
    start: datetime,
    end: datetime,
    output_dir: Path,
    client: ClickHouseQueryClient,
    symbols: tuple[str, ...] | None = None,
    checkpoint_dir: Path | None = None,
    resume: bool = False,
    retry_failures: bool = False,
) -> EdcMulticoinRunV2:
    """Load/compile → P2D2 runner → export artifacts under output_dir.

    P4C runs once via ``compile_strategy_v2`` (and again inside P2D2 preflight).
    """
    if not isinstance(strategy_path, Path):
        raise StrategyMulticoinExportError("strategy_path must be pathlib.Path")
    if not isinstance(universe_path, Path):
        raise StrategyMulticoinExportError("universe_path must be pathlib.Path")
    if not isinstance(output_dir, Path):
        raise StrategyMulticoinExportError("output_dir must be pathlib.Path")
    if checkpoint_dir is not None and not isinstance(checkpoint_dir, Path):
        raise StrategyMulticoinExportError("checkpoint_dir must be pathlib.Path")
    if not isinstance(client, ClickHouseQueryClient):
        raise StrategyMulticoinExportError(
            "client must provide a query(...) method returning result_rows"
        )
    if not strategy_path.is_file():
        raise StrategyMulticoinExportError(f"strategy file not found: {strategy_path}")
    if not universe_path.is_file():
        raise StrategyMulticoinExportError(f"universe file not found: {universe_path}")
    _require_utc_dt(start, field_name="start")
    _require_utc_dt(end, field_name="end")
    if end <= start:
        raise StrategyMulticoinExportError("end must be > start")

    catalogs = production_catalog_bundle_v2()
    spec = load_strategy_v2_yaml_file(strategy_path)
    compiled = compile_strategy_v2(spec, catalogs)

    ck = checkpoint_dir if checkpoint_dir is not None else output_dir / "checkpoints"
    run = run_edc_m0_multicoin_v2(
        spec,
        compiled,
        catalogs,
        client=client,
        universe_path=universe_path,
        start=start,
        end=end,
        checkpoint_dir=ck,
        symbols=symbols,
        resume=resume,
        retry_failures=retry_failures,
    )
    export_edc_multicoin_artifacts_v2(run, spec, output_dir=output_dir)
    return run


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run EDC M0 multicoin (P2D2) and write Strategy Lab export artifacts."
    )
    parser.add_argument("--strategy", type=Path, required=True)
    parser.add_argument("--universe", type=Path, required=True)
    parser.add_argument("--start", type=str, required=True, help="UTC inclusive ISO")
    parser.add_argument("--end", type=str, required=True, help="UTC exclusive ISO")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--symbol",
        action="append",
        dest="symbols",
        default=None,
        help="Repeatable symbol filter (universe order still applies)",
    )
    parser.add_argument("--checkpoint-dir", type=Path, default=None)
    parser.add_argument("--resume", action="store_true", default=False)
    parser.add_argument("--retry-failures", action="store_true", default=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry. Creates ClickHouse client only when invoked."""
    try:
        args = build_arg_parser().parse_args(argv)
        start = _parse_utc(args.start, field_name="start")
        end = _parse_utc(args.end, field_name="end")
        if end <= start:
            raise StrategyMulticoinExportError("end must be > start")
        if not args.strategy.is_file():
            raise StrategyMulticoinExportError(
                f"strategy file not found: {args.strategy}"
            )
        if not args.universe.is_file():
            raise StrategyMulticoinExportError(
                f"universe file not found: {args.universe}"
            )
        symbols: tuple[str, ...] | None = None
        if args.symbols is not None:
            symbols = tuple(args.symbols)

        from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

        client = get_clickhouse_client()
        run = run_and_export_edc_m0_multicoin_v2(
            strategy_path=args.strategy,
            universe_path=args.universe,
            start=start,
            end=end,
            output_dir=args.output_dir,
            client=client,
            symbols=symbols,
            checkpoint_dir=args.checkpoint_dir,
            resume=args.resume,
            retry_failures=args.retry_failures,
        )
        print(
            "edc_multicoin_export "
            f"hash={run.strategy_hash} "
            f"symbols={len(run.requested_symbols)} "
            f"completed={len(run.completed_symbols)} "
            f"failed={len(run.failed_symbols)} "
            f"trades={run.trade_count} "
            f"net_pnl_usdt={run.net_pnl_usdt} "
            f"output_dir={args.output_dir}"
        )
        return 0
    except (StrategyMulticoinExportError, StrategyMulticoinError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _manifest_json(run: EdcMulticoinRunV2, spec: StrategySpecV2) -> str:
    signal = spec.signal
    assert type(signal) is PluginSignalSpec
    payload = {
        "export_format_version": _EXPORT_FORMAT_VERSION,
        "strategy_hash": run.strategy_hash,
        "plugin_id": signal.plugin.plugin_id.value,
        "plugin_contract_version": signal.plugin.contract_version.value,
        "universe_id": run.universe.universe_id.value,
        "universe_version": run.universe.version,
        "universe_content_hash": run.universe.content_hash,
        "start": _dt_to_iso(run.start),
        "end": _dt_to_iso(run.end),
        "signal_timeframe_value": spec.timeframes.signal.value,
        "signal_timeframe_unit": spec.timeframes.signal.unit.value,
        "execution_timeframe_value": spec.timeframes.execution.value,
        "execution_timeframe_unit": spec.timeframes.execution.unit.value,
        "roundtrip_cost_value": str(spec.costs.roundtrip_cost.value),
        "roundtrip_cost_unit": spec.costs.roundtrip_cost.unit.value,
        "slippage_status": spec.costs.slippage.value,
        "funding_status": spec.costs.funding.value,
        "requested_symbols": list(run.requested_symbols),
        "execution_order": list(run.requested_symbols),
        "completed_symbols": list(run.completed_symbols),
        "failed_symbols": list(run.failed_symbols),
        "candidate_count": run.candidate_count,
        "trade_count": run.trade_count,
        "gross_pnl_usdt": str(run.gross_pnl_usdt),
        "costs_usdt": str(run.costs_usdt),
        "net_pnl_usdt": str(run.net_pnl_usdt),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=True, indent=2) + "\n"


def _failures_json(run: EdcMulticoinRunV2) -> str:
    rows = [
        {
            "symbol": f.symbol,
            "error_type": f.error_type,
            "error_message": f.message,
        }
        for f in run.failures
    ]
    return json.dumps(rows, sort_keys=True, ensure_ascii=True, indent=2) + "\n"


def _coin_summary_csv(run: EdcMulticoinRunV2) -> str:
    by_success = {r.symbols[0]: r for r in run.completed_runs}
    by_fail = {f.symbol: f for f in run.failures}
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_COIN_SUMMARY_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for symbol in run.requested_symbols:
        if symbol in by_success:
            writer.writerow(_coin_success_row(by_success[symbol]))
        elif symbol in by_fail:
            fail = by_fail[symbol]
            writer.writerow(
                {
                    "symbol": symbol,
                    "status": "failed",
                    "error_type": fail.error_type,
                    "error_message": fail.message,
                    "candidate_count": "",
                    "trade_count": "",
                    "gross_pnl_usdt": "",
                    "costs_usdt": "",
                    "net_pnl_usdt": "",
                    "winning_trades": "",
                    "losing_trades": "",
                    "unresolved_trades": "",
                    "win_rate": "",
                    "avg_net_pnl_usdt": "",
                }
            )
        else:  # pragma: no cover — runner always covers requested symbols
            raise StrategyMulticoinExportError(
                f"symbol {symbol!r} missing from completed_runs and failures"
            )
    return buf.getvalue()


def _coin_success_row(result: StrategyRunResultV2) -> dict[str, str]:
    winning = 0
    losing = 0
    unresolved = 0
    resolved_nets: list[Decimal] = []
    for trade in result.trades:
        if trade.exit_reason in _RESOLVED:
            assert trade.net_pnl_usdt is not None
            resolved_nets.append(trade.net_pnl_usdt)
            if trade.net_pnl_usdt > 0:
                winning += 1
            elif trade.net_pnl_usdt < 0:
                losing += 1
            # net_pnl_usdt == 0: neither winner nor loser
        else:
            unresolved += 1
    decided = winning + losing
    if decided == 0:
        win_rate = ""
    else:
        win_rate = str(Decimal(winning) / Decimal(decided))
    if not resolved_nets:
        avg_net = ""
    else:
        avg_net = str(sum(resolved_nets, Decimal("0")) / Decimal(len(resolved_nets)))
    return {
        "symbol": result.symbols[0],
        "status": "complete",
        "error_type": "",
        "error_message": "",
        "candidate_count": str(result.candidate_count),
        "trade_count": str(result.trade_count),
        "gross_pnl_usdt": str(result.gross_pnl_usdt),
        "costs_usdt": str(result.costs_usdt),
        "net_pnl_usdt": str(result.net_pnl_usdt),
        "winning_trades": str(winning),
        "losing_trades": str(losing),
        "unresolved_trades": str(unresolved),
        "win_rate": win_rate,
        "avg_net_pnl_usdt": avg_net,
    }


def _trades_csv(run: EdcMulticoinRunV2) -> str:
    rows: list[tuple[str, datetime, str, dict[str, str]]] = []
    for result in run.completed_runs:
        for trade in result.trades:
            row = _trade_row(result, trade)
            rows.append(
                (
                    trade.symbol,
                    trade.decision_time,
                    trade.source_event_id.value,
                    row,
                )
            )
    rows.sort(key=lambda item: (item[0], item[1], item[2]))
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_TRADE_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for _, _, _, row in rows:
        writer.writerow(row)
    return buf.getvalue()


def _trade_row(result: StrategyRunResultV2, trade: StrategyTradeV2) -> dict[str, str]:
    return {
        "strategy_hash": result.strategy_hash,
        "plugin_id": result.plugin_id.value,
        "symbol": trade.symbol,
        "source_event_id": trade.source_event_id.value,
        "side": trade.side.value,
        "decision_time": _dt_to_iso(trade.decision_time),
        "entry_time": _dt_to_iso(trade.entry_time),
        "entry_price": str(trade.entry_price),
        "exit_time": "" if trade.exit_time is None else _dt_to_iso(trade.exit_time),
        "exit_price": "" if trade.exit_price is None else str(trade.exit_price),
        "exit_reason": trade.exit_reason.value,
        "gross_return_pct": _dec_cell(trade.gross_return_pct),
        "roundtrip_cost_pct": str(trade.roundtrip_cost_pct),
        "net_return_pct": _dec_cell(trade.net_return_pct),
        "gross_pnl_usdt": _dec_cell(trade.gross_pnl_usdt),
        "costs_usdt": _dec_cell(trade.costs_usdt),
        "net_pnl_usdt": _dec_cell(trade.net_pnl_usdt),
        "mode_id": "" if trade.mode_id is None else trade.mode_id.value,
        "confirmation_policy": (
            ""
            if trade.confirmation_policy is None
            else trade.confirmation_policy.value
        ),
    }


def _dec_cell(value: Decimal | None) -> str:
    if value is None:
        return ""
    return str(value)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _parse_utc(raw: str, *, field_name: str) -> datetime:
    if type(raw) is not str or not raw:
        raise StrategyMulticoinExportError(f"{field_name} must be a non-empty ISO string")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StrategyMulticoinExportError(
            f"{field_name} must be ISO-8601 datetime"
        ) from exc
    return _require_utc_dt(parsed, field_name=field_name)


def _require_utc_dt(value: datetime, *, field_name: str) -> datetime:
    if type(value) is not datetime:
        raise StrategyMulticoinExportError(f"{field_name} must be datetime")
    if value.tzinfo is None:
        raise StrategyMulticoinExportError(f"{field_name} must be timezone-aware UTC")
    offset = value.utcoffset()
    if offset is None or offset != _ZERO:
        raise StrategyMulticoinExportError(f"{field_name} must be UTC (zero offset)")
    return value


def _dt_to_iso(value: datetime) -> str:
    return value.isoformat()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))

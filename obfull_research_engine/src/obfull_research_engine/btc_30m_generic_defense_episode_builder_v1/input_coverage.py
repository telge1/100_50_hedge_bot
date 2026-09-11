"""Validate discovery input coverage for pilot vs full-run contracts."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.episodes import parse_utc
from .config import (
    INPUT_CONTRACT_FULL_RUN_GENERIC_V1,
    INPUT_CONTRACT_PILOT_FIXTURE_V1,
    BuilderConfig,
)


class FullRunInputError(ValueError):
    """Full mode refused because inputs are incomplete or still pilot fixtures."""


def _jsonl_time_span(path: Path, keys: tuple[str, ...]) -> tuple[str | None, str | None, int]:
    tmin: str | None = None
    tmax: str | None = None
    n = 0
    if not path.is_file():
        return None, None, 0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            n += 1
            ts = None
            for k in keys:
                if row.get(k) is not None:
                    ts = str(row[k])
                    break
            if ts is None:
                continue
            if tmin is None or ts < tmin:
                tmin = ts
            if tmax is None or ts > tmax:
                tmax = ts
    return tmin, tmax, n


def _csv_time_span(path: Path, keys: tuple[str, ...]) -> tuple[str | None, str | None, int]:
    if not path.is_file():
        return None, None, 0
    with path.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    tmin: str | None = None
    tmax: str | None = None
    for row in rows:
        for k in keys:
            ts = row.get(k)
            if not ts:
                continue
            s = str(ts)
            if tmin is None or s < tmin:
                tmin = s
            if tmax is None or s > tmax:
                tmax = s
    return tmin, tmax, len(rows)


def probe_input_coverage(cfg: BuilderConfig) -> dict[str, Any]:
    """Read-only coverage probe for configured discovery inputs."""
    mp = Path(cfg.mp_events_path)
    trades = Path(cfg.trades_path)
    candles = Path(cfg.candles_path)
    clusters = Path(cfg.level_clusters_path)
    archive = Path(cfg.full_ob_archive)

    mp_min, mp_max, mp_n = _csv_time_span(mp, ("available_at", "natural_profile_end", "profile_start"))
    tr_min, tr_max, tr_n = _jsonl_time_span(trades, ("trade_ts", "exchange_ts", "ts", "timestamp"))
    ca_min, ca_max, ca_n = _jsonl_time_span(candles, ("open_time", "start", "timestamp", "time"))

    return {
        "window_start": cfg.window_start,
        "window_end": cfg.window_end,
        "mp_events": {
            "path": str(mp),
            "exists": mp.is_file(),
            "n_rows": mp_n,
            "t_min": mp_min,
            "t_max": mp_max,
        },
        "level_clusters": {
            "path": str(clusters),
            "exists": clusters.is_file(),
        },
        "trades": {
            "path": str(trades),
            "exists": trades.is_file(),
            "n_rows": tr_n,
            "t_min": tr_min,
            "t_max": tr_max,
        },
        "candles": {
            "path": str(candles),
            "exists": candles.is_file(),
            "n_rows": ca_n,
            "t_min": ca_min,
            "t_max": ca_max,
        },
        "full_ob_archive": {
            "path": str(archive),
            "exists": archive.exists(),
        },
        "uses_pilot_fixture_paths": cfg.uses_pilot_fixture_paths(),
        "input_contract": cfg.input_contract,
        "run_mode": cfg.run_mode(),
    }


def validate_run_inputs(cfg: BuilderConfig, *, skip_disk: bool = False) -> dict[str, Any]:
    """
    Enforce pilot vs full input contracts.

    Full mode must declare full_run_generic_v1 and must not reuse Episode-1
    trade/candle/MP pilot fixtures as a silent "full" run.

    skip_disk=True (synthetic unit tests) records mode/limit metadata only.
    """
    coverage = {
        "window_start": cfg.window_start,
        "window_end": cfg.window_end,
        "input_contract": cfg.input_contract,
        "run_mode": cfg.run_mode(),
        "uses_pilot_fixture_paths": cfg.uses_pilot_fixture_paths(),
        "ok": True,
        "errors": [],
    }
    if skip_disk:
        coverage["skipped_disk_probe"] = True
        return coverage

    probed = probe_input_coverage(cfg)
    coverage.update(probed)

    errors: list[str] = []
    if cfg.pilot:
        if cfg.input_contract not in (
            INPUT_CONTRACT_PILOT_FIXTURE_V1,
            INPUT_CONTRACT_FULL_RUN_GENERIC_V1,
        ):
            errors.append(f"unknown input_contract for pilot: {cfg.input_contract}")
    else:
        if cfg.input_contract != INPUT_CONTRACT_FULL_RUN_GENERIC_V1:
            errors.append(
                "full mode requires input_contract=full_run_generic_v1 "
                f"(got {cfg.input_contract!r}); refusing silent full-run on pilot defaults"
            )
        if cfg.uses_pilot_fixture_paths():
            errors.append(
                "full mode refuses Episode-1/lf1 pilot fixture paths; "
                "regenerate generic mp_level_events, level_clusters, trades, candles "
                "for the target window and point config at those paths"
            )
        # Empty / missing files
        for name in ("mp_events", "trades", "candles"):
            meta = coverage.get(name) or {}
            if not meta.get("exists"):
                errors.append(f"full mode missing {name} file: {meta.get('path')}")
            elif name != "mp_events" and int(meta.get("n_rows") or 0) <= 0:
                errors.append(f"full mode empty {name}: {meta.get('path')}")
        if not (coverage.get("level_clusters") or {}).get("exists"):
            errors.append(
                f"full mode missing level_clusters: {(coverage.get('level_clusters') or {}).get('path')}"
            )
        if not (coverage.get("full_ob_archive") or {}).get("exists"):
            errors.append(
                f"full mode missing full_ob_archive: {(coverage.get('full_ob_archive') or {}).get('path')}"
            )

        ws = parse_utc(cfg.window_start)
        we = parse_utc(cfg.window_end)
        trades = coverage.get("trades") or {}
        candles = coverage.get("candles") or {}
        if trades.get("exists"):
            if not trades.get("t_min") or not trades.get("t_max"):
                errors.append("full mode trades have no usable timestamps")
            else:
                tr_s, tr_e = parse_utc(str(trades["t_min"])), parse_utc(str(trades["t_max"]))
                if tr_e < ws or tr_s > we:
                    errors.append(
                        f"full mode trades span {trades['t_min']}..{trades['t_max']} "
                        f"does not intersect window {cfg.window_start}..{cfg.window_end}"
                    )
        if candles.get("exists"):
            if not candles.get("t_min") or not candles.get("t_max"):
                errors.append("full mode candles have no usable timestamps")
            else:
                ca_s, ca_e = parse_utc(str(candles["t_min"])), parse_utc(str(candles["t_max"]))
                if ca_e < ws or ca_s > we:
                    errors.append(
                        f"full mode candles span {candles['t_min']}..{candles['t_max']} "
                        f"does not intersect window {cfg.window_start}..{cfg.window_end}"
                    )

    coverage["errors"] = errors
    coverage["ok"] = not errors
    if errors:
        raise FullRunInputError("; ".join(errors))
    return coverage
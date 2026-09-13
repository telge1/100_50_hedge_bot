"""Local Phase-1D episode loading and fail-closed validation."""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contract import (
    ALLOWED_OUTCOMES,
    EXPECTED_DATASET_FINGERPRINT,
    EXPECTED_EPISODE_HASHES,
    REQUIRED_COLUMNS,
)


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("naive datetime forbidden")
    return parsed.astimezone(timezone.utc)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dataset_fingerprint(btc_bytes: bytes, doge_bytes: bytes) -> str:
    return hashlib.sha256(btc_bytes + b"\n" + doge_bytes).hexdigest()


@dataclass(frozen=True)
class EpisodeRow:
    episode_id: str
    symbol: str
    t0_utc: datetime
    distance_upper_bps: float
    distance_lower_bps: float
    outcome: str
    raw: dict[str, str]


def load_episodes_csv(path: Path) -> list[EpisodeRow]:
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError(f"empty CSV schema: {path}")
        missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
        if missing:
            raise ValueError(f"schema missing columns {missing} in {path}")
        rows: list[EpisodeRow] = []
        for raw in reader:
            outcome = raw["outcome"]
            if outcome not in ALLOWED_OUTCOMES:
                raise ValueError(f"unknown outcome {outcome!r} in {path}")
            rows.append(
                EpisodeRow(
                    episode_id=raw["episode_id"],
                    symbol=raw["symbol"],
                    t0_utc=parse_utc(raw["t0_utc"]),
                    distance_upper_bps=float(raw["distance_upper_bps"]),
                    distance_lower_bps=float(raw["distance_lower_bps"]),
                    outcome=outcome,
                    raw=dict(raw),
                )
            )
    return rows


def verify_and_load_dataset(
    dataset_root: Path,
    *,
    require_frozen_manifest: bool = True,
) -> tuple[list[EpisodeRow], dict[str, Any]]:
    """Load BTC+DOGE episodes with fail-closed hash/schema gates. No DB access."""
    dataset_root = Path(dataset_root)
    paths = {
        "BTCUSDT": dataset_root / "BTCUSDT" / "episodes.csv",
        "DOGEUSDT": dataset_root / "DOGEUSDT" / "episodes.csv",
    }
    for symbol, path in paths.items():
        if not path.is_file():
            raise FileNotFoundError(f"missing episodes.csv for {symbol}: {path}")

    hashes = {symbol: sha256_file(path) for symbol, path in paths.items()}
    for symbol, expected in EXPECTED_EPISODE_HASHES.items():
        if hashes[symbol] != expected:
            raise ValueError(
                f"episode hash mismatch for {symbol}: {hashes[symbol]} != {expected}"
            )

    btc_bytes = paths["BTCUSDT"].read_bytes()
    doge_bytes = paths["DOGEUSDT"].read_bytes()
    fingerprint = dataset_fingerprint(btc_bytes, doge_bytes)
    if fingerprint != EXPECTED_DATASET_FINGERPRINT:
        raise ValueError(
            f"dataset fingerprint mismatch: {fingerprint} != {EXPECTED_DATASET_FINGERPRINT}"
        )

    if require_frozen_manifest:
        import json

        phase1d_manifest = dataset_root / "artifact_manifest.json"
        if not phase1d_manifest.is_file():
            raise FileNotFoundError(
                f"--require-frozen-manifest needs {phase1d_manifest}"
            )
        phase1d = json.loads(phase1d_manifest.read_text(encoding="utf-8"))
        for symbol, path in paths.items():
            key = None
            suffix = f"{symbol}/episodes.csv"
            for candidate in phase1d.get("files", {}):
                if candidate.endswith(suffix):
                    key = candidate
                    break
            if key is None:
                raise ValueError(f"phase-1d manifest missing {symbol} episodes.csv")
            if phase1d["files"][key]["sha256"] != hashes[symbol]:
                raise ValueError(f"phase-1d manifest hash mismatch for {symbol}")

        freeze_path = (
            Path("results/liquidity_destination_bias_phase_2_baselines")
            / "dataset_freeze_manifest.json"
        )
        if freeze_path.is_file():
            freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
            if freeze.get("dataset_fingerprint_sha256") != fingerprint:
                raise ValueError("phase-2 freeze manifest fingerprint mismatch")

    episodes: list[EpisodeRow] = []
    for symbol, path in paths.items():
        loaded = load_episodes_csv(path)
        if any(row.symbol != symbol for row in loaded):
            raise ValueError(f"symbol mismatch inside {path}")
        episodes.extend(loaded)

    meta = {
        "dataset_root": str(dataset_root),
        "dataset_fingerprint_sha256": fingerprint,
        "episode_hashes": hashes,
        "episode_count": len(episodes),
        "research_only": True,
        "database_connection": False,
    }
    return episodes, meta

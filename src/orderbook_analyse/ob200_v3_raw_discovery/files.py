"""Discover closed raw archive segments (exclude open *.tmp)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_NAME_RE = re.compile(
    r"^(?P<symbol>[A-Z0-9]+)_"
    r"(?P<start>\d{8}T\d{6}Z)_"
    r"(?P<end>\d{8}T\d{6}Z)_"
    r"ob200_v3\.zst$"
)


@dataclass(frozen=True)
class SegmentRef:
    path: Path
    symbol: str
    start_utc: datetime
    end_utc: datetime

    @property
    def manifest_path(self) -> Path:
        return self.path.with_suffix(".manifest.json")

    @property
    def is_boundary_stub(self) -> bool:
        """Hour-boundary zero-duration files (checkpoint-only rotate artifacts)."""
        return self.start_utc == self.end_utc

    @property
    def duration_sec(self) -> float:
        return (self.end_utc - self.start_utc).total_seconds()


def _parse_stamp(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def list_closed_segments(
    raw_root: Path,
    *,
    symbols: tuple[str, ...],
    start: datetime | None = None,
    end: datetime | None = None,
    include_boundary_stubs: bool = False,
) -> list[SegmentRef]:
    out: list[SegmentRef] = []
    for symbol in symbols:
        sym_root = raw_root / symbol.upper()
        if not sym_root.is_dir():
            continue
        for path in sorted(sym_root.rglob("*_ob200_v3.zst")):
            name = path.name
            if "_open_" in name or name.endswith(".tmp"):
                continue
            m = _NAME_RE.match(name)
            if not m:
                continue
            start_utc = _parse_stamp(m.group("start"))
            end_utc = _parse_stamp(m.group("end"))
            ref = SegmentRef(
                path=path,
                symbol=m.group("symbol"),
                start_utc=start_utc,
                end_utc=end_utc,
            )
            if not include_boundary_stubs and ref.is_boundary_stub:
                continue
            if start is not None and ref.end_utc <= start:
                continue
            if end is not None and ref.start_utc >= end:
                continue
            out.append(ref)
    return out


def excluded_tmp_files(raw_root: Path, symbols: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    for symbol in symbols:
        sym_root = raw_root / symbol.upper()
        if not sym_root.is_dir():
            continue
        out.extend(sorted(sym_root.rglob("*_open_*.zst.tmp")))
        out.extend(sorted(sym_root.rglob("*.tmp")))
    # unique
    return sorted(set(out))

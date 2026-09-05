"""Segment readiness gates for finalized Full-OB JSONL.zst files."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.replay import sha256_file

from . import DELTA_FILENAME, OPEN_SUFFIXES
from .ids import segment_key


@dataclass
class SegmentCandidate:
    path: Path
    event_dir: Path
    fight_event_id: str
    symbol: str
    continuation_index: int
    expected_sha256: str | None
    actual_sha256: str | None = None
    file_size: int = 0
    topic: str = ""
    segment_first_ts: str | None = None
    segment_last_ts: str | None = None
    segment_first_u: int | None = None
    segment_last_u: int | None = None
    previous_segment_sha256: str | None = None
    status: str = "DISCOVERED"
    reasons: list[str] = field(default_factory=list)
    segment_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["path"] = str(self.path)
        d["event_dir"] = str(self.event_dir)
        return d


def _is_open_name(name: str) -> bool:
    lower = name.lower()
    return any(lower.endswith(suf) for suf in OPEN_SUFFIXES)


def _file_has_writers(path: Path) -> bool:
    """Best-effort: true if any FD still has the file open for write (Linux)."""
    try:
        target = path.resolve()
    except Exception:
        return False
    proc = Path("/proc")
    if not proc.exists():
        return False
    for pid_dir in proc.iterdir():
        if not pid_dir.name.isdigit():
            continue
        fd_dir = pid_dir / "fd"
        if not fd_dir.is_dir():
            continue
        try:
            for fd in fd_dir.iterdir():
                try:
                    link = Path(os.readlink(fd))
                except OSError:
                    continue
                if link.resolve() != target:
                    continue
                # Check access mode via fdinfo
                info = pid_dir / "fdinfo" / fd.name
                try:
                    text = info.read_text()
                except OSError:
                    continue
                for line in text.splitlines():
                    if line.startswith("flags:"):
                        flags = int(line.split()[1], 8)
                        # O_WRONLY=1, O_RDWR=2
                        if flags & 0x3:
                            return True
        except Exception:
            continue
    return False


def discover_event_segments(event_dir: Path) -> list[SegmentCandidate]:
    event_dir = event_dir.resolve()
    man_path = event_dir / "event_manifest.json"
    if not man_path.exists():
        # fall back to single-segment layout with local manifest.json
        return []
    man = json.loads(man_path.read_text())
    fight = man.get("fight_event_id") or man.get("symbol_event_id") or event_dir.name
    symbol = (man.get("symbol") or fight.split("_")[0]).upper()
    topic = f"orderbook.full.{symbol}"
    out: list[SegmentCandidate] = []
    for seg in man.get("segments") or []:
        idx = int(seg.get("continuation_index") or 0)
        if idx == 0:
            path = event_dir / DELTA_FILENAME
        else:
            path = event_dir / f"cont_{idx:03d}" / DELTA_FILENAME
        out.append(
            SegmentCandidate(
                path=path,
                event_dir=event_dir,
                fight_event_id=str(fight),
                symbol=symbol,
                continuation_index=idx,
                expected_sha256=seg.get("segment_sha256"),
                topic=topic,
                segment_first_ts=seg.get("segment_first_ts"),
                segment_last_ts=seg.get("segment_last_ts"),
                segment_first_u=seg.get("segment_first_u"),
                segment_last_u=seg.get("segment_last_u"),
                previous_segment_sha256=seg.get("previous_segment_sha256"),
                status="DISCOVERED",
                reasons=[],
            )
        )
    return out


def validate_candidate(cand: SegmentCandidate) -> SegmentCandidate:
    cand.status = "VALIDATING"
    cand.reasons = []
    name = cand.path.name

    if _is_open_name(name) or any(_is_open_name(p.name) for p in cand.path.parents if p != cand.path):
        # also if path itself is under a name with tmp? check path string
        pass
    if _is_open_name(str(cand.path)):
        cand.status = "OPEN_NOT_ELIGIBLE"
        cand.reasons.append("path_looks_open")
        return cand
    # sibling open tmp with same stem
    tmp = cand.path.with_suffix(cand.path.suffix + ".tmp") if not str(cand.path).endswith(".tmp") else cand.path
    # actual open file would be full_ob_raw_deltas.jsonl.zst.tmp
    open_tmp = Path(str(cand.path) + ".tmp") if not str(cand.path).endswith(".tmp") else cand.path
    if open_tmp.exists() and not cand.path.exists():
        cand.status = "OPEN_NOT_ELIGIBLE"
        cand.reasons.append("only_tmp_present")
        return cand
    if str(cand.path).endswith(OPEN_SUFFIXES):
        cand.status = "OPEN_NOT_ELIGIBLE"
        cand.reasons.append("open_suffix")
        return cand

    if not cand.path.exists():
        # unfinished segment entry in manifest
        if cand.expected_sha256 is None:
            cand.status = "OPEN_NOT_ELIGIBLE"
            cand.reasons.append("segment_not_finalized_missing_file")
            return cand
        cand.status = "FAILED_PERMANENT"
        cand.reasons.append("missing_file")
        return cand

    if any(str(cand.path).endswith(s) for s in OPEN_SUFFIXES):
        cand.status = "OPEN_NOT_ELIGIBLE"
        cand.reasons.append("open_suffix")
        return cand

    if not name.endswith(".jsonl.zst"):
        cand.status = "FAILED_PERMANENT"
        cand.reasons.append("bad_extension")
        return cand

    if cand.expected_sha256 is None:
        cand.status = "OPEN_NOT_ELIGIBLE"
        cand.reasons.append("expected_sha256_absent_not_finalized")
        return cand

    man = cand.event_dir / "event_manifest.json"
    if not man.exists():
        cand.status = "FAILED_PERMANENT"
        cand.reasons.append("manifest_missing")
        return cand

    cand.file_size = cand.path.stat().st_size
    if cand.file_size <= 0:
        cand.status = "FAILED_PERMANENT"
        cand.reasons.append("empty_file")
        return cand

    if _file_has_writers(cand.path):
        cand.status = "OPEN_NOT_ELIGIBLE"
        cand.reasons.append("file_open_for_write")
        return cand

    cand.actual_sha256 = sha256_file(cand.path)
    if cand.actual_sha256 != cand.expected_sha256:
        cand.status = "FAILED_PERMANENT"
        cand.reasons.append("sha256_mismatch")
        return cand

    if not cand.symbol or not cand.fight_event_id:
        cand.status = "FAILED_PERMANENT"
        cand.reasons.append("invalid_symbol_or_event")
        return cand

    if cand.continuation_index < 0:
        cand.status = "FAILED_PERMANENT"
        cand.reasons.append("invalid_continuation_index")
        return cand

    # Predecessor lineage: first segment or previous sha present
    if cand.continuation_index > 0 and not cand.previous_segment_sha256:
        cand.reasons.append("missing_previous_segment_sha_warning")

    cand.segment_id = segment_key(
        fight_event_id=cand.fight_event_id,
        continuation_index=cand.continuation_index,
        source_sha256=cand.actual_sha256,
    )
    cand.status = "VALIDATED"
    return cand


def discover_and_validate(source_root: Path, *, symbols: set[str] | None = None) -> list[SegmentCandidate]:
    """Walk flight-recorder root for event dirs with event_manifest.json."""
    source_root = source_root.resolve()
    results: list[SegmentCandidate] = []
    for man in source_root.rglob("event_manifest.json"):
        event_dir = man.parent
        # skip if path contains open tmp names oddly
        cands = discover_event_segments(event_dir)
        for c in cands:
            if symbols and c.symbol.upper() not in symbols:
                continue
            results.append(validate_candidate(c))
    return results

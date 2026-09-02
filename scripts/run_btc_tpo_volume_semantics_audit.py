#!/usr/bin/env python3
"""Run TPO vs Volume semantics provenance audit (read-only)."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.btc_ob_fight.tpo_volume_semantics_audit import (  # noqa: E402
    GOLDEN_ANCHOR,
    write_audit_outputs,
)


def parse_ts(raw: str) -> datetime:
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return dt.astimezone(timezone.utc)


def main() -> int:
    p = argparse.ArgumentParser(description="BTC TPO vs Volume semantics audit")
    p.add_argument("--timestamp", default="2026-08-31T19:00:00Z")
    p.add_argument(
        "--out-dir",
        type=Path,
        default=Path("results/btc_tpo_volume_semantics_audit_20260831_1900_v1"),
    )
    args = p.parse_args()
    anchor = parse_ts(args.timestamp) if args.timestamp else GOLDEN_ANCHOR
    out_dir = args.out_dir.expanduser().resolve()
    if out_dir.exists() and any(out_dir.iterdir()):
        # collision-free: append _run_N
        base = out_dir
        n = 2
        while out_dir.exists() and any(out_dir.iterdir()):
            out_dir = Path(f"{base}_run_{n:03d}")
            n += 1
    result = write_audit_outputs(out_dir, anchor=anchor)
    print(result["verdict"])
    print(result["out_dir"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

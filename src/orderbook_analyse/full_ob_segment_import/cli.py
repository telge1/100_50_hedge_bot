"""CLI for full_ob_finalized_segment_clickhouse_import_v1."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _maybe_nice() -> None:
    try:
        os.nice(10)
    except Exception:
        pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="import_finalized_full_ob_segments")
    p.add_argument("--source-root", type=Path, required=True)
    p.add_argument("--database", type=str, default="")
    p.add_argument("--symbols", type=str, default="")
    p.add_argument("--once", action="store_true", default=True)
    p.add_argument("--watch", action="store_true", help="Not activated in this release")
    p.add_argument("--poll-seconds", type=int, default=30)
    p.add_argument("--require-finalized", action="store_true", default=True)
    p.add_argument("--verify-replay", action="store_true", default=False)
    p.add_argument("--dry-run", action="store_true", default=False)
    p.add_argument("--event-id", type=str, default=None)
    p.add_argument("--segment", type=int, default=None)
    p.add_argument("--max-files", type=int, default=None)
    p.add_argument("--max-bytes", type=int, default=None)
    p.add_argument("--resume", action="store_true", default=True)
    p.add_argument("--no-resume", action="store_true")
    p.add_argument("--verify-only", action="store_true")
    p.add_argument("--status", action="store_true")
    p.add_argument("--state-path", type=Path, default=None)
    p.add_argument("--batch-size", type=int, default=200)
    p.add_argument("--allow-production-db", action="store_true", help="FORBIDDEN unless explicit")
    p.add_argument("--json-out", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    _maybe_nice()
    args = build_parser().parse_args(argv)
    if args.watch:
        print("ERROR: --watch not activated in this release (ACTIVATION_REQUIRED)", file=sys.stderr)
        return 2
    if args.allow_production_db:
        print("ERROR: --allow-production-db is refused by policy", file=sys.stderr)
        return 2

    dry_run = bool(args.dry_run)
    database = (args.database or "").strip()
    if not dry_run and not database:
        print("ERROR: --database required unless --dry-run", file=sys.stderr)
        return 2
    if dry_run and not database:
        database = "research_full_ob_import_pilot_v1"

    symbols = {s.strip().upper() for s in args.symbols.split(",") if s.strip()} or None
    state_path = args.state_path or Path("results/full_ob_finalized_segment_clickhouse_import_v1/import_state.json")

    from orderbook_analyse.full_ob_segment_import.importer import run_import
    from orderbook_analyse.full_ob_segment_import.state_machine import LocalStateStore

    if args.status:
        store = LocalStateStore(state_path)
        print(json.dumps([s.to_dict() for s in store.all()], indent=2))
        return 0

    if args.verify_only:
        from orderbook_analyse.full_ob_segment_import.importer import get_ch_client, verify_segment_parity
        from orderbook_analyse.full_ob_segment_import.readiness import discover_and_validate

        client = get_ch_client()
        store = LocalStateStore(state_path)
        cands = discover_and_validate(args.source_root, symbols=symbols)
        if args.event_id:
            cands = [c for c in cands if c.fight_event_id == args.event_id]
        out = []
        for c in cands:
            if c.status != "VALIDATED":
                continue
            if args.segment is not None and c.continuation_index != args.segment:
                continue
            out.append(verify_segment_parity(client, database, c, store))
        print(json.dumps(out, indent=2, default=str))
        return 0

    report = run_import(
        source_root=args.source_root,
        database=database,
        symbols=symbols,
        state_path=state_path,
        dry_run=dry_run,
        once=True,
        max_files=args.max_files,
        event_id=args.event_id,
        segment=args.segment,
        verify_replay=bool(args.verify_replay),
        resume=not args.no_resume,
        batch_size=args.batch_size,
    )
    text = json.dumps(report, indent=2, default=str)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

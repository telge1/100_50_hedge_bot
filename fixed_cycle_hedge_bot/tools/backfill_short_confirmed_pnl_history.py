import argparse
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


@dataclass
class BackfillStats:
    source_path: Path
    target_path: Path
    scanned_count: int = 0
    candidate_short_count: int = 0
    already_exists_count: int = 0
    appended_count: int = 0


def _is_short_relevant(entry: Dict) -> bool:
    side = str(entry.get("side") or "").strip().lower()
    if side == "short":
        return True
    purpose = str(entry.get("purpose") or "").upper()
    return "SHORT" in purpose


def _dedupe_key(entry: Dict) -> str:
    if entry.get("dedupe_key"):
        return str(entry["dedupe_key"])
    exchange_order_id = str(entry.get("exchange_order_id") or "").strip()
    purpose = str(entry.get("purpose") or "").strip()
    if exchange_order_id and purpose:
        return f"{exchange_order_id}:{purpose}"
    timestamp = str(entry.get("timestamp") or "")
    qty = str(entry.get("qty") or entry.get("exec_qty") or "")
    fill_price = str(
        entry.get("fill_price")
        or entry.get("avg_fill_price")
        or entry.get("price")
        or ""
    )
    return f"{timestamp}:{purpose}:{qty}:{fill_price}"


def _load_existing_keys(path: Path) -> Tuple[List[Dict], set]:
    if not path.exists():
        return [], set()
    entries: List[Dict] = []
    keys: set = set()
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
                entries.append(payload)
                keys.add(_dedupe_key(payload))
    except Exception:
        # Defensive: bei Fehlern lieber keine Backfills erzeugen
        return [], keys
    return entries, keys


def _iter_long_bot_histories(group_root: Path) -> Iterable[Tuple[int, Path]]:
    """
    Finde alle long_bot_N confirmed_order_pnl_history.jsonl Dateien unterhalb
    des angegebenen group_root.
    """
    if not group_root.exists():
        return
    for child in sorted(group_root.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        if not name.startswith("long_bot_"):
            continue
        suffix = name.split("_", maxsplit=2)[-1]
        try:
            index = int(suffix)
        except ValueError:
            continue
        source_path = child / "logs" / "confirmed_order_pnl_history.jsonl"
        if source_path.exists():
            yield index, source_path


def backfill_for_pair(group_root: Path, index: int, source_path: Path, apply: bool) -> BackfillStats:
    short_bot_name = f"short_bot_{index}"
    target_logs_dir = group_root / short_bot_name / "logs"
    target_path = target_logs_dir / "confirmed_order_pnl_history.jsonl"
    stats = BackfillStats(source_path=source_path, target_path=target_path)

    # Quelle einlesen
    try:
        with source_path.open("r", encoding="utf-8", errors="ignore") as fh:
            source_lines = list(fh)
    except Exception:
        return stats

    existing_entries, existing_keys = _load_existing_keys(target_path)

    new_entries: List[Dict] = []
    for line in source_lines:
        line = line.strip()
        if not line:
            continue
        stats.scanned_count += 1
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if not _is_short_relevant(payload):
            continue
        stats.candidate_short_count += 1
        key = _dedupe_key(payload)
        if key in existing_keys:
            stats.already_exists_count += 1
            continue
        # Kopie mit aktualisiertem bot_name
        entry = dict(payload)
        entry["bot_name"] = short_bot_name
        new_entries.append(entry)
        existing_keys.add(key)

    if not new_entries:
        return stats

    stats.appended_count = len(new_entries)

    if not apply:
        # Dry-Run: nur zählen, nicht schreiben
        return stats

    target_logs_dir.mkdir(parents=True, exist_ok=True)

    # Optionales Backup der Ziel-Datei, falls sie existiert
    if target_path.exists():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = target_path.with_suffix(target_path.suffix + f".bak_{timestamp}")
        try:
            shutil.copy2(target_path, backup_path)
        except Exception:
            # Backup-Fehler ignorieren, da wir nur anhängen
            pass

    try:
        with target_path.open("a", encoding="utf-8") as fh:
            for entry in new_entries:
                fh.write(json.dumps(entry, ensure_ascii=False))
                fh.write("\n")
    except Exception:
        # Im Fehlerfall keine weiteren Stats manipulieren
        pass

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill short_bot_N confirmed_order_pnl_history.jsonl from long_bot_N histories."
    )
    parser.add_argument(
        "--group-root",
        type=Path,
        required=True,
        help="Pfad zum Bot-Gruppen-Root, z.B. live_bots/100_50_hedge_bot",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Nur zeigen, was geändert würde (Standard).",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Backfill tatsächlich in die short_bot_N Dateien schreiben.",
    )
    args = parser.parse_args()

    group_root: Path = args.group_root.resolve()
    apply_changes: bool = bool(args.apply) and not bool(args.dry_run)

    print(f"Backfill Short Confirmed-PnL History")
    print(f"group_root: {group_root}")
    print(f"mode: {'APPLY' if apply_changes else 'DRY-RUN'}")

    total_stats: List[BackfillStats] = []
    for index, source_path in _iter_long_bot_histories(group_root):
        stats = backfill_for_pair(group_root, index, source_path, apply_changes)
        total_stats.append(stats)
        print(
            f"[bot_{index}] "
            f"source={stats.source_path} "
            f"target={stats.target_path} "
            f"scanned={stats.scanned_count} "
            f"candidates_short={stats.candidate_short_count} "
            f"already_exists={stats.already_exists_count} "
            f"appended={stats.appended_count}"
        )

    scanned_total = sum(s.scanned_count for s in total_stats)
    candidates_total = sum(s.candidate_short_count for s in total_stats)
    exists_total = sum(s.already_exists_count for s in total_stats)
    appended_total = sum(s.appended_count for s in total_stats)
    print(
        f"Summary: scanned={scanned_total} "
        f"candidates_short={candidates_total} "
        f"already_exists={exists_total} "
        f"appended={appended_total}"
    )


if __name__ == "__main__":
    main()


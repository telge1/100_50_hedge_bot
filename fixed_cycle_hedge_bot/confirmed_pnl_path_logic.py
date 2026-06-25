from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping


def path_bot_name_from_logs_path(path: str | Path) -> str | None:
    for part in Path(path).parts:
        normalized = str(part).strip().lower()
        if re.fullmatch(r"(long|short)_bot_\d+", normalized):
            return normalized
    return None


def purpose_implies_bot_side(purpose: str | None) -> str | None:
    purpose_upper = str(purpose or "").strip().upper()
    if not purpose_upper:
        return None

    cycle_match = re.match(r"CYCLE_\d+_(LONG|SHORT)_", purpose_upper)
    if cycle_match:
        return cycle_match.group(1).lower()

    long_purposes = {
        "LONG_TP_EXIT",
        "SHORT_SL_EXIT",
        "REFILL_LONG",
        "RECOVERY_RELOAD_LONG_ENTRY",
        "INITIAL_LONG_ENTRY",
    }
    short_purposes = {
        "SHORT_TP_EXIT",
        "LONG_SL_EXIT",
        "REFILL_SHORT",
        "RECOVERY_RELOAD_SHORT_ENTRY",
        "INITIAL_SHORT_ENTRY",
    }
    if purpose_upper in long_purposes:
        return "long"
    if purpose_upper in short_purposes:
        return "short"
    return None


def path_bot_side(path_bot_name: str | None) -> str | None:
    normalized = str(path_bot_name or "").strip().lower()
    if normalized.startswith("short_bot_"):
        return "short"
    if normalized.startswith("long_bot_"):
        return "long"
    return None


def purpose_allowed_for_path_bot(path_bot_name: str | None, purpose: str | None) -> bool:
    implied_side = purpose_implies_bot_side(purpose)
    if implied_side is None:
        return True
    file_side = path_bot_side(path_bot_name)
    if file_side is None:
        return True
    return implied_side == file_side


def row_bot_name_matches_path(path_bot_name: str | None, row: Mapping[str, Any]) -> bool:
    row_bot_name = str(row.get("bot_name") or "").strip().lower()
    normalized_path_bot = str(path_bot_name or "").strip().lower()
    if not normalized_path_bot:
        return True
    if not row_bot_name:
        return True
    return row_bot_name == normalized_path_bot


def confirmed_row_dedupe_key(row: Mapping[str, Any]) -> str:
    explicit_dedupe_key = str(row.get("dedupe_key") or "").strip()
    return "|".join(
        [
            str(row.get("bot_name") or "").strip().lower(),
            str(row.get("trade_block_id") or "").strip(),
            str(row.get("pnl_scope") or "").strip().lower(),
            str(row.get("purpose") or "").strip().upper(),
            str(row.get("cycle_index") or ""),
            str(row.get("exchange_order_id") or row.get("client_order_id") or "").strip(),
            str(row.get("timestamp") or "").strip(),
            str(row.get("closed_pnl") if row.get("closed_pnl") is not None else ""),
            explicit_dedupe_key,
        ]
    )


def validate_confirmed_pnl_row_for_path(
    row: Mapping[str, Any],
    path: str | Path,
) -> tuple[bool, str | None, dict[str, Any]]:
    path_bot_name = path_bot_name_from_logs_path(path)
    row_bot_name = str(row.get("bot_name") or "").strip().lower()
    purpose = str(row.get("purpose") or row.get("trade_type") or "").strip()
    skip_payload = {
        "path_bot_name": path_bot_name,
        "row_bot_name": row_bot_name or None,
        "purpose": purpose or None,
        "exchange_order_id": str(
            row.get("exchange_order_id") or row.get("client_order_id") or ""
        ).strip()
        or None,
        "trade_block_id": str(row.get("trade_block_id") or "").strip() or None,
        "file_path": str(path),
    }
    if path_bot_name and not row_bot_name_matches_path(path_bot_name, row):
        return False, "confirmed_pnl_history_path_bot_mismatch_skipped", skip_payload
    if path_bot_name and purpose and not purpose_allowed_for_path_bot(path_bot_name, purpose):
        return False, "confirmed_pnl_history_path_purpose_mismatch_skipped", skip_payload
    return True, None, skip_payload


def load_valid_confirmed_pnl_rows_from_paths(
    paths: Iterable[str | Path],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        file_path = Path(path)
        if not file_path.exists():
            continue
        path_bot_name = path_bot_name_from_logs_path(file_path)
        with file_path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
                if not isinstance(payload, dict):
                    continue
                ok, _event, _skip_payload = validate_confirmed_pnl_row_for_path(payload, file_path)
                if not ok:
                    continue
                if path_bot_name and not str(payload.get("bot_name") or "").strip():
                    payload = dict(payload)
                    payload["bot_name"] = path_bot_name
                rows.append(payload)
    return rows


def should_skip_foreign_confirmed_pnl_write(
    *,
    payload: Mapping[str, Any],
    default_bot_name: str,
    target_bot_name: str,
    target_path: str | Path,
    purpose: str,
) -> tuple[bool, str | None, dict[str, Any]]:
    """
    Decide whether a confirmed PnL row must not be written to target_path.

    Runtime-stamped payload bot_name (current/default bot) must not block
    purpose-based routing. Only explicit foreign import context (source_path
    and/or payload owner conflicting with target while source_path is set)
    should hard-skip.
    """
    path_bot_name = path_bot_name_from_logs_path(target_path) or target_bot_name
    normalized_target = str(path_bot_name or target_bot_name or "").strip().lower()
    payload_bot_name = str(payload.get("bot_name") or "").strip().lower()
    source_path = str(payload.get("source_path") or "").strip()
    context = {
        "current_bot_name": default_bot_name,
        "row_bot_name": payload_bot_name or None,
        "purpose": purpose,
        "exchange_order_id": str(payload.get("exchange_order_id") or "").strip() or None,
        "trade_block_id": str(payload.get("trade_block_id") or "").strip() or None,
        "target_file_path": str(target_path),
        "source_path": source_path or None,
        "path_bot_name": normalized_target or None,
    }

    if normalized_target and not purpose_allowed_for_path_bot(normalized_target, purpose):
        return True, "purpose_path_mismatch", context

    if not source_path:
        return False, None, context

    source_bot_name = path_bot_name_from_logs_path(source_path)
    if source_bot_name and normalized_target and source_bot_name != normalized_target:
        return True, "source_path_foreign_bot", context

    if payload_bot_name and normalized_target and payload_bot_name != normalized_target:
        return True, "payload_bot_name_mismatch", context

    return False, None, context

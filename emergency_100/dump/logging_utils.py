from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import logging
from pathlib import Path
from typing import Any

_AUDIT_LOG_PATH: Path | None = None


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, set):
        return sorted(value)
    return str(value)


def json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, default=_json_default)


def configure_audit_log(path: str) -> None:
    global _AUDIT_LOG_PATH
    audit_path = Path(path)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    _AUDIT_LOG_PATH = audit_path


def append_jsonl(path: str | Path, payload: dict[str, Any]) -> None:
    target_path = Path(path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with target_path.open("a", encoding="utf-8") as handle:
        handle.write(json_dumps(payload))
        handle.write("\n")


def log_event(logger: logging.Logger, event: str, **payload: Any) -> None:
    envelope = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **payload,
    }
    logger.info(
        "EMERGENCY100 %s %s",
        event,
        json_dumps(envelope),
    )
    if _AUDIT_LOG_PATH is not None:
        append_jsonl(_AUDIT_LOG_PATH, envelope)

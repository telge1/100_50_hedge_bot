from __future__ import annotations

import re

from .config import JOB_ID_RE

_ID = re.compile(JOB_ID_RE)


def require_id(value: str, label: str) -> str:
    text = str(value or "").strip().lower()
    if not _ID.fullmatch(text):
        raise ValueError(f"INVALID_{label}")
    return text

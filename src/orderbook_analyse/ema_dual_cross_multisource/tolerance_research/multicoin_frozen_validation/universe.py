"""Universe load/audit — no performance-based filtering."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def load_universe(symbols_file: str | Path) -> dict[str, Any]:
    path = Path(symbols_file)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        symbols = [str(s).upper() for s in raw]
        meta: dict[str, Any] = {"name": path.stem, "source": str(path)}
    else:
        symbols = [str(s).upper() for s in raw.get("symbols") or []]
        meta = {k: v for k, v in raw.items() if k != "symbols"}
    return {"path": str(path.resolve()), "meta": meta, "symbols": symbols}


def universe_hash(symbols: list[str]) -> str:
    payload = "\n".join(symbols).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def audit_universe(symbols: list[str], *, expected_n: int | None = 51) -> dict[str, Any]:
    """Validate uniqueness / size. Never filters by performance."""
    n = len(symbols)
    unique = sorted(set(symbols))
    dupes = sorted({s for s in symbols if symbols.count(s) > 1})
    issues: list[str] = []
    if dupes:
        issues.append(f"duplicate_symbols:{','.join(dupes)}")
    if expected_n is not None and n != expected_n:
        issues.append(f"expected_n={expected_n}_got={n}")
    if len(unique) != n:
        issues.append("not_unique")
    return {
        "n_symbols": n,
        "n_unique": len(unique),
        "unique": len(unique) == n,
        "duplicates": dupes,
        "expected_n": expected_n,
        "matches_expected_n": expected_n is None or n == expected_n,
        "universe_hash": universe_hash(symbols),
        "symbols_in_file_order": list(symbols),
        "issues": issues,
        "ok": not issues,
        "performance_filter_applied": False,
    }


def apply_limit_symbols(symbols: list[str], limit: int | None) -> list[str]:
    """Optional CLI limit — order-preserving prefix, never performance-based."""
    if limit is None:
        return list(symbols)
    if limit < 0:
        raise ValueError("limit_symbols must be >= 0")
    return list(symbols[:limit])

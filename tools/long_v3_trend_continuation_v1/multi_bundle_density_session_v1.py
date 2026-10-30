"""Read-only composite density session across ordered frozen month bundles."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from density_session_adapter_v1 import FrozenBundleDensitySessionV1


class MultiBundleDensitySessionV1:
    """Route collect_at_close to the bundle that owns each decision close."""

    def __init__(self, bundle_dirs: list[tuple[str, Path]]) -> None:
        self.month_labels = [label for label, _ in bundle_dirs]
        self._by_close: dict[str, FrozenBundleDensitySessionV1] = {}
        self._sessions: list[tuple[str, FrozenBundleDensitySessionV1]] = []
        for label, path in bundle_dirs:
            sess = FrozenBundleDensitySessionV1(path)
            self._sessions.append((label, sess))
            for c in sess.decision_closes():
                prev = self._by_close.get(c)
                if prev is not None and prev is not sess:
                    raise ValueError(f"DUPLICATE_DECISION_CLOSE:{c}")
                self._by_close[c] = sess
        self.manifest = {
            "composite": True,
            "months": [label for label, _ in bundle_dirs],
            "entry_count": len(self._by_close),
        }

    def has_close(self, decision_close_utc: str) -> bool:
        return decision_close_utc in self._by_close

    def collect_at_close(self, decision_close_utc: str) -> dict[str, Any]:
        sess = self._by_close.get(decision_close_utc)
        if sess is None:
            raise KeyError(decision_close_utc)
        return sess.collect_at_close(decision_close_utc)

    def decision_closes(self) -> list[str]:
        return sorted(self._by_close.keys())

    def session_for_close(self, decision_close_utc: str) -> str | None:
        for label, sess in self._sessions:
            if sess.has_close(decision_close_utc):
                return label
        return None

"""Group adjacent 5m observations into coherent episodes."""

from __future__ import annotations

from typing import Any


def build_episodes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    sorted_rows = sorted(rows, key=lambda r: (r["window_id"], r["decision_close_utc"]))
    episodes: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for r in sorted_rows:
        key = (r["window_id"], r.get("research_regime"), r.get("research_regime"))
        if cur is None:
            cur = _start_episode(r)
            continue
        same_window = r["window_id"] == cur["window_id"]
        same_regime = r.get("research_regime") == cur["research_regime"]
        if same_window and same_regime:
            cur["end_close_utc"] = r["decision_close_utc"]
            cur["bar_count"] += 1
        else:
            episodes.append(cur)
            cur = _start_episode(r)
    if cur:
        episodes.append(cur)

    for i, ep in enumerate(episodes):
        ep["episode_id"] = f"{ep['window_id']}:{i+1}"
        ep["evidence_class"] = _evidence_class(ep["window_id"])
    return episodes


def _start_episode(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "window_id": r["window_id"],
        "research_regime": r.get("research_regime"),
        "start_close_utc": r["decision_close_utc"],
        "end_close_utc": r["decision_close_utc"],
        "bar_count": 1,
    }


def _evidence_class(window_id: str) -> str:
    if window_id.startswith("sep9_"):
        return "development_reference"
    if window_id.startswith("sep11_dense"):
        return "development_reference_problem"
    if window_id == "sep4_sep5_sideways":
        return "transition_reference"
    if window_id.startswith("sep11_control"):
        return "chronological_control"
    return "other"

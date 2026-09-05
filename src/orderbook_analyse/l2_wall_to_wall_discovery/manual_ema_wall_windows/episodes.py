"""Deduplicate overlapping manual windows into episodes."""

from __future__ import annotations

from typing import Any

from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows import parse_utc


def dedupe_episodes(window_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Merge overlapping windows that share the same primary zone + similar wall price
    into episodes. Always keep per-window rows separately; this is additive.
    """
    # Sort by center
    rows = sorted(window_rows, key=lambda r: r["center_utc"])
    episodes: list[dict[str, Any]] = []
    used: set[str] = set()

    def wall_key(r: dict[str, Any]) -> tuple:
        wp = r.get("primary_wall_price")
        try:
            wpf = round(float(wp), 1) if wp not in (None, "MISSING") else None
        except (TypeError, ValueError):
            wpf = None
        return (r.get("primary_zone"), wpf, r.get("zone_role"))

    i = 0
    ep_id = 0
    while i < len(rows):
        if rows[i]["window_id"] in used:
            i += 1
            continue
        ep_id += 1
        members = [rows[i]]
        used.add(rows[i]["window_id"])
        end = parse_utc(rows[i]["end_utc"])
        key = wall_key(rows[i])
        j = i + 1
        while j < len(rows):
            if rows[j]["window_id"] in used:
                j += 1
                continue
            start_j = parse_utc(rows[j]["start_utc"])
            # overlap or contiguous within 5m and same wall/zone key
            same = wall_key(rows[j]) == key and key[0] not in (None, "MISSING", "none")
            if start_j <= end and same:
                members.append(rows[j])
                used.add(rows[j]["window_id"])
                end = max(end, parse_utc(rows[j]["end_utc"]))
            elif start_j <= end and rows[j].get("primary_zone") == rows[i].get("primary_zone"):
                # overlapping same zone even if wall price drifted slightly
                members.append(rows[j])
                used.add(rows[j]["window_id"])
                end = max(end, parse_utc(rows[j]["end_utc"]))
            j += 1
        classes = [m.get("primary_class") for m in members]
        # sequence special-case: rectangle then final_circle
        ids = [m["window_id"] for m in members]
        note = "independent_attack"
        if set(ids) >= {"rectangle", "final_circle"} or (
            "rectangle" in ids and "final_circle" in {r["window_id"] for r in rows}
        ):
            pass
        if len(members) > 1:
            note = "multi_window_same_zone_episode"
        episodes.append(
            {
                "episode_id": f"ep_{ep_id}",
                "member_windows": "|".join(ids),
                "n_windows": len(members),
                "start_utc": members[0]["start_utc"],
                "end_utc": max(m["end_utc"] for m in members),
                "primary_zone": members[0].get("primary_zone"),
                "zone_role": members[0].get("zone_role"),
                "classes": "|".join(str(c) for c in classes),
                "dominant_class": max(set(classes), key=classes.count),
                "shared_wall": note != "independent_attack",
                "note": note,
            }
        )
        i += 1

    # Explicit sequence episode for rectangle -> final_circle if separate
    rect = next((r for r in rows if r["window_id"] == "rectangle"), None)
    fin = next((r for r in rows if r["window_id"] == "final_circle"), None)
    if rect and fin:
        episodes.append(
            {
                "episode_id": "ep_sequence_ema20_to_ema59",
                "member_windows": "rectangle|final_circle",
                "n_windows": 2,
                "start_utc": rect["start_utc"],
                "end_utc": fin["end_utc"],
                "primary_zone": "EMA20_THEN_EMA59",
                "zone_role": "sequence",
                "classes": f"{rect.get('primary_class')}|{fin.get('primary_class')}",
                "dominant_class": "SEQUENCE",
                "shared_wall": False,
                "note": "post_ema20_move_toward_ema59_hypothesis",
            }
        )
    return episodes

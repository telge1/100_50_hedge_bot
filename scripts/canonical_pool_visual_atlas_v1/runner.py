#!/usr/bin/env python3
"""Deterministic outcome-blind visual atlas of frozen 30-day pool classes."""

from __future__ import annotations

import hashlib
import json
import math
import resource
import subprocess
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np
import pandas as pd

OA_ROOT = Path("/home/telgenbuescher/projects/orderbook_analyse")
DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
STRUCT = OA_ROOT / "results/canonical_pool_structural_class_analysis_v1"
OUT = OA_ROOT / "results/canonical_pool_visual_atlas_v1"

EXPECTED_SPEC = "cbe69a4da27e18596246bfa997758c5f81173962572b1350793e93f6b36b0e02"
EXPECTED_BUNDLE = "81b03ad86f4345937fcedd33304e4fd8fbb923f16269c62ae6c02d40f18fb6e4"
ATLAS_SEED = "CANONICAL_CLEAR_POOL_VISUAL_ATLAS_V1"
SYMBOL = "BTCUSDT"
BARS_WINDOW = 96
EXAMPLES_PER_CELL = 3
ASK_COLOR = "#ec4079"
BID_COLOR = "#228bab"
MEMBER_ALPHA = 0.18
HULL_ALPHA = 0.28
FORBIDDEN = frozenset(
    {
        "future_return",
        "forward_return",
        "mfe",
        "mae",
        "pnl",
        "tp_hit",
        "sl_hit",
        "winning",
        "losing",
        "outcome",
        "profit",
        "tradeable",
        "winner",
        "loser",
    }
)

sys.path.insert(0, str(OA_ROOT / "src"))
sys.path.insert(0, str(DASH))


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    return sha256_bytes(p.read_bytes())


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str) + "\n", encoding="utf-8")


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def git_info(repo: Path) -> dict:
    return {
        "repo": str(repo),
        "branch": subprocess.check_output(["git", "branch", "--show-current"], cwd=repo, text=True).strip(),
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip(),
        "dirty_count": len(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True).splitlines()
        ),
    }


def membership_tag(p: int) -> str:
    if p <= 1:
        return "SINGLETON_P1"
    if p == 2:
        return "PAIR_P2"
    if p <= 4:
        return "CLUSTER_P3_4"
    if p <= 8:
        return "CLUSTER_P5_8"
    return "CLUSTER_P9_PLUS"


def q_tag(value: float | None, qs: np.ndarray, prefix: str) -> str | None:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    for i, q in enumerate(qs):
        if value <= q:
            return f"{prefix}_Q{i + 1}"
    return f"{prefix}_Q4"


def selection_hash(spec_sha: str, episode_id: str, timeframe: str, side: str, primary: str) -> str:
    payload = f"{spec_sha}|{episode_id}|{timeframe}|{side}|{primary}|{ATLAS_SEED}"
    return sha256_bytes(payload.encode())


def verify_structural() -> dict:
    missing = []
    for name in (
        "structural_analysis_spec.json",
        "freeze_manifest.json",
        "same_tf_component_snapshot_features.parquet",
        "raw_pool_snapshot_features.parquet",
        "component_episodes.parquet",
        "multi_tf_component_snapshot_features.parquet",
        "exp_04_structural_classification.json",
    ):
        if not (STRUCT / name).is_file():
            missing.append(name)
    if missing:
        raise RuntimeError(f"STRUCTURAL_ARTIFACTS_MISSING:{missing}")
    spec = json.loads((STRUCT / "structural_analysis_spec.json").read_text())
    freeze = json.loads((STRUCT / "freeze_manifest.json").read_text())
    got_spec = freeze.get("structural_analysis_spec_sha256") or spec.get("structural_analysis_spec_sha256")
    got_bundle = freeze.get("structural_class_bundle_sha256")
    if got_spec != EXPECTED_SPEC:
        raise RuntimeError(f"STRUCTURAL_BUNDLE_HASH_MISMATCH:spec {got_spec}!={EXPECTED_SPEC}")
    if got_bundle != EXPECTED_BUNDLE:
        raise RuntimeError(f"STRUCTURAL_BUNDLE_HASH_MISMATCH:bundle {got_bundle}!={EXPECTED_BUNDLE}")
    # outcome columns
    for pq in STRUCT.glob("*.parquet"):
        cols = [c.lower() for c in pd.read_parquet(pq).columns]
        for c in cols:
            for k in FORBIDDEN:
                if c == k or c.endswith(f"_{k}") or c.startswith(f"{k}_"):
                    raise RuntimeError(f"OUTCOME_BLINDNESS_VIOLATION:{pq.name}:{c}")
    return {"spec_sha": got_spec, "bundle_sha": got_bundle, "structural_spec": spec}


def build_component_universe() -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """One representative row per component episode (max-P snapshot, then latest)."""
    comp = pd.read_parquet(STRUCT / "same_tf_component_snapshot_features.parquet")
    raw = pd.read_parquet(STRUCT / "raw_pool_snapshot_features.parquet")
    mtf = pd.read_parquet(STRUCT / "multi_tf_component_snapshot_features.parquet")
    ceps = pd.read_parquet(STRUCT / "component_episodes.parquet")

    # representative snapshot: among episode rows, prefer max P then latest snapshot_ts
    comp = comp.copy()
    comp["P"] = comp["pool_count"].astype(int)
    idx = (
        comp.sort_values(["component_id", "P", "snapshot_ts"], ascending=[True, False, False])
        .groupby("component_id", as_index=False)
        .head(1)
    )
    # strength / sigma / height / age quantiles per TF+side
    q_cache: dict[tuple, dict[str, np.ndarray]] = {}
    for (tf, side), g in idx.groupby(["timeframe", "side"]):
        q_cache[(tf, side)] = {
            "sigma": np.quantile(g["strength_sum"].dropna(), [0.25, 0.5, 0.75])
            if g["strength_sum"].notna().sum() >= 4
            else None,
            "height": np.quantile(g["component_height_bps"].dropna(), [0.25, 0.5, 0.75])
            if g["component_height_bps"].notna().sum() >= 4
            else None,
            "age": np.quantile(g["newest_member_age_bars"].dropna(), [0.25, 0.5, 0.75])
            if g["newest_member_age_bars"].notna().sum() >= 4
            else None,
            "strength": np.quantile(g["strength_max"].dropna(), [0.25, 0.5, 0.75])
            if g["strength_max"].notna().sum() >= 4
            else None,
        }

    # pool-level tags for members at same snapshot
    raw_key = raw.set_index(["snapshot_ts", "pool_id"], drop=False)

    # mtf parent lookup
    mtf_by_snap_side: dict[tuple, list] = defaultdict(list)
    for _, r in mtf.iterrows():
        mtf_by_snap_side[(r["snapshot_ts"], r["side"])].append(r)

    rows = []
    for _, r in idx.iterrows():
        tf, side = r["timeframe"], r["side"]
        qs = q_cache.get((tf, side), {})
        tags = [f"TF_{tf.upper()}", membership_tag(int(r["P"]))]
        if qs.get("sigma") is not None:
            t = q_tag(r.get("strength_sum"), qs["sigma"], "SIGMA")
            if t:
                tags.append(t)
        if qs.get("strength") is not None:
            t = q_tag(r.get("strength_max"), qs["strength"], "STRENGTH")
            if t:
                tags.append(t)
        if qs.get("height") is not None:
            t = q_tag(r.get("component_height_bps"), qs["height"], "HEIGHT")
            if t:
                tags.append(t)
        if qs.get("age") is not None:
            t = q_tag(r.get("newest_member_age_bars"), qs["age"], "AGE")
            if t:
                tags.append(t)

        if r.get("rendered_as_cluster_hull"):
            tags.append("CLEAR_CLUSTER_HULL")
        elif int(r["P"]) == 1:
            tags.append("CLEAR_FILLED_SINGLE")
        else:
            tags.append("MEMBER_ONLY")

        if int(r.get("labeled_member_count") or 0) > 0:
            tags.append("LABEL_VISIBLE")
        else:
            tags.append("NO_LABEL")

        if int(r["P"]) <= 1:
            tags.append("ISOLATED_COMPONENT")
            tags.append("EXTERIOR_COMPONENT_EDGE")
        else:
            tags.append("EXTERIOR_COMPONENT_EDGE")

        # HTF from multi-tf containing this component
        parent_tags = ["NO_HTF_PARENT"]
        for m in mtf_by_snap_side.get((r["snapshot_ts"], side), []):
            kids_raw = m.get("child_component_ids")
            if kids_raw is None:
                kids = []
            elif isinstance(kids_raw, np.ndarray):
                kids = list(kids_raw.tolist())
            else:
                kids = list(kids_raw)
            related_ids = [m.get("parent_component_id")] + kids
            if r["component_id"] not in related_ids:
                continue
            tfs_raw = m.get("participating_timeframes")
            if tfs_raw is None:
                tfs = []
            elif isinstance(tfs_raw, np.ndarray):
                tfs = list(tfs_raw.tolist())
            else:
                tfs = list(tfs_raw)
            has15 = "15m" in tfs or bool(m.get("has_15m"))
            has30 = "30m" in tfs or bool(m.get("has_30m"))
            parent_tags = []
            if has15 and has30:
                parent_tags.append("PARENT_15M_30M")
            elif has30:
                parent_tags.append("PARENT_30M")
            elif has15:
                parent_tags.append("PARENT_15M")
            else:
                parent_tags.append("NO_HTF_PARENT")
            tc = int(m.get("timeframe_count") or len(tfs) or 1)
            if tc >= 2:
                parent_tags.append("MULTI_TF_OVERLAP_2")
            if tc >= 3:
                parent_tags.append("MULTI_TF_OVERLAP_3")
            break
        tags.extend(parent_tags)

        # test history from member pools at snapshot
        members = list(r["member_pool_ids"]) if isinstance(r["member_pool_ids"], (list, np.ndarray)) else []
        deep = untested = multi = single = 0
        touches = 0
        for pid in members[:20]:
            try:
                pr = raw_key.loc[(r["snapshot_ts"], pid)]
                if isinstance(pr, pd.DataFrame):
                    pr = pr.iloc[0]
            except KeyError:
                continue
            touches += int(pr.get("number_of_front_edge_touches") or 0) + int(
                pr.get("number_of_inside_entries") or 0
            )
            if pr.get("currently_deeply_tested"):
                deep += 1
            elif pr.get("currently_untested"):
                untested += 1
            else:
                multi += 1
        if deep:
            tags.append("DEEP_TESTED")
        elif untested and not multi and not deep:
            tags.append("UNTESTED")
        elif touches <= 1:
            tags.append("SINGLE_TEST")
        else:
            tags.append("MULTI_TEST")

        ep = ceps[ceps["episode_id"] == r["component_id"]]
        rows.append(
            {
                **{k: r[k] for k in r.index},
                "episode_id": r["component_id"],
                "class_tags": sorted(set(tags)),
                "membership": membership_tag(int(r["P"])),
                "first_seen": ep["first_seen"].iloc[0] if len(ep) else r["snapshot_ts"],
                "last_seen": ep["last_seen"].iloc[0] if len(ep) else r["snapshot_ts"],
                "snapshot_count": int(ep["snapshot_count"].iloc[0]) if len(ep) else 1,
                "touch_count_proxy": touches,
            }
        )

    universe = pd.DataFrame(rows)
    return universe, raw, {"q_cache_keys": list(q_cache.keys())}


def pick_examples(
    universe: pd.DataFrame, filters: dict[str, Any], primary: str, n: int, used: set[str], spec_sha: str
) -> list[dict]:
    df = universe
    for k, v in filters.items():
        if k == "tag":
            df = df[df["class_tags"].apply(lambda tags, vv=v: vv in (tags or []))]
        elif k == "tags_all":
            df = df[df["class_tags"].apply(lambda tags, vv=v: all(t in (tags or []) for t in vv))]
        elif k == "membership":
            df = df[df["membership"] == v]
        elif k == "timeframe":
            df = df[df["timeframe"] == v]
        elif k == "side":
            df = df[df["side"] == v]
        elif k == "P_min":
            df = df[df["P"] >= v]
        elif k == "P_max":
            df = df[df["P"] <= v]
        elif k == "height_q":
            # similar geometry: HEIGHT_Q2 or HEIGHT_Q3 preferably
            df = df[df["class_tags"].apply(lambda tags, vv=v: vv in (tags or []))]
    # exclude already used in this board
    df = df[~df["episode_id"].isin(used)]
    if len(df) == 0:
        return []
    scored = []
    for _, r in df.iterrows():
        h = selection_hash(spec_sha, r["episode_id"], r["timeframe"], r["side"], primary)
        scored.append((h, r))
    scored.sort(key=lambda x: x[0])
    out = []
    for h, r in scored[:n]:
        used.add(r["episode_id"])
        d = r.to_dict()
        d["selection_hash"] = h
        d["primary_class"] = primary
        out.append(d)
    return out


def freeze_selection(universe: pd.DataFrame, atlas_spec_sha: str) -> dict:
    boards = {}
    examples = []
    atlas_id = 0

    def add(board: str, items: list[dict], cell: str):
        nonlocal atlas_id
        boards.setdefault(board, {})
        boards[board][cell] = []
        for it in items:
            atlas_id += 1
            eid = f"ATLAS_{atlas_id:04d}"
            rec = {
                "atlas_example_id": eid,
                "board": board,
                "cell": cell,
                "episode_id": it["episode_id"],
                "component_id": it["component_id"],
                "snapshot_ts": it["snapshot_ts"],
                "timeframe": it["timeframe"],
                "side": it["side"],
                "P": int(it["P"]),
                "strength_sum": float(it["strength_sum"]) if it.get("strength_sum") is not None else None,
                "strength_max": float(it["strength_max"]) if it.get("strength_max") is not None else None,
                "component_lower": float(it["component_lower"]),
                "component_upper": float(it["component_upper"]),
                "component_height_bps": float(it["component_height_bps"])
                if it.get("component_height_bps") is not None
                else None,
                "newest_member_age_bars": int(it["newest_member_age_bars"])
                if it.get("newest_member_age_bars") is not None
                else None,
                "rendered_as_cluster_hull": bool(it.get("rendered_as_cluster_hull")),
                "chart_label": it.get("chart_label"),
                "member_pool_ids": list(it["member_pool_ids"])
                if isinstance(it["member_pool_ids"], (list, np.ndarray))
                else [],
                "class_tags": list(it["class_tags"]),
                "primary_class": it["primary_class"],
                "selection_hash": it["selection_hash"],
                "exterior_front_edge": float(it["exterior_front_edge"]),
                "exterior_back_edge": float(it["exterior_back_edge"]),
                "market_price": float(it["market_price"]) if it.get("market_price") is not None else None,
                "touch_count_proxy": int(it.get("touch_count_proxy") or 0),
            }
            boards[board][cell].append(eid)
            examples.append(rec)

    # A membership
    for tf in ("5m", "15m", "30m"):
        for side in ("ASK", "BID"):
            used: set[str] = set()
            for mem in ("SINGLETON_P1", "PAIR_P2", "CLUSTER_P3_4", "CLUSTER_P5_8", "CLUSTER_P9_PLUS"):
                items = pick_examples(
                    universe,
                    {"timeframe": tf, "side": side, "membership": mem},
                    mem,
                    EXAMPLES_PER_CELL,
                    used,
                    atlas_spec_sha,
                )
                add("membership_by_tf", items, f"{tf}_{side}_{mem}")

    # B strength by membership (within P3-4 and P5-8)
    for mem in ("CLUSTER_P3_4", "CLUSTER_P5_8"):
        for tf in ("5m", "15m", "30m"):
            for side in ("ASK", "BID"):
                used = set()
                for sq in ("STRENGTH_Q1", "STRENGTH_Q2", "STRENGTH_Q3", "STRENGTH_Q4"):
                    items = pick_examples(
                        universe,
                        {"timeframe": tf, "side": side, "membership": mem, "tag": sq},
                        f"{mem}|{sq}",
                        EXAMPLES_PER_CELL,
                        used,
                        atlas_spec_sha,
                    )
                    add("strength_by_membership", items, f"{tf}_{side}_{mem}_{sq}")

    # C sigma by geometry (HEIGHT_Q2/Q3)
    for hq in ("HEIGHT_Q2", "HEIGHT_Q3"):
        for tf in ("5m", "15m", "30m"):
            for side in ("ASK", "BID"):
                used = set()
                for sq in ("SIGMA_Q1", "SIGMA_Q2", "SIGMA_Q3", "SIGMA_Q4"):
                    items = pick_examples(
                        universe,
                        {"timeframe": tf, "side": side, "tags_all": [hq, sq]},
                        f"{hq}|{sq}",
                        EXAMPLES_PER_CELL,
                        used,
                        atlas_spec_sha,
                    )
                    add("sigma_by_geometry", items, f"{tf}_{side}_{hq}_{sq}")

    # D timeframe comparison — pick similar height Q2 BID/ASK clusters P5-8
    for side in ("ASK", "BID"):
        used = set()
        for tf in ("5m", "15m", "30m"):
            items = pick_examples(
                universe,
                {"timeframe": tf, "side": side, "membership": "CLUSTER_P5_8", "tag": "HEIGHT_Q2"},
                f"TF_COMPARE|{tf}",
                EXAMPLES_PER_CELL,
                used,
                atlas_spec_sha,
            )
            add("timeframe_comparison", items, f"{side}_{tf}")

    # E HTF parent
    for side in ("ASK", "BID"):
        used = set()
        for tag in ("NO_HTF_PARENT", "PARENT_15M", "PARENT_30M", "PARENT_15M_30M"):
            items = pick_examples(
                universe,
                {"side": side, "tag": tag},
                tag,
                EXAMPLES_PER_CELL,
                used,
                atlas_spec_sha,
            )
            add("htf_parent_comparison", items, f"{side}_{tag}")

    # F exterior vs child — component exterior vs raw internal child pools
    # For exterior: isolated/exterior components; for child we store component with MEMBER_ONLY + P>=3
    for tf in ("5m", "15m", "30m"):
        for side in ("ASK", "BID"):
            used = set()
            items = pick_examples(
                universe,
                {"timeframe": tf, "side": side, "tag": "ISOLATED_COMPONENT"},
                "EXTERIOR_COMPONENT_EDGE",
                EXAMPLES_PER_CELL,
                used,
                atlas_spec_sha,
            )
            add("exterior_vs_child", items, f"{tf}_{side}_EXTERIOR")
            items = pick_examples(
                universe,
                {"timeframe": tf, "side": side, "membership": "CLUSTER_P5_8", "tag": "CLEAR_CLUSTER_HULL"},
                "INTERNAL_CHILD_EDGE",
                EXAMPLES_PER_CELL,
                used,
                atlas_spec_sha,
            )
            add("exterior_vs_child", items, f"{tf}_{side}_CHILD_CONTEXT")

    # G test history
    for tf in ("5m", "15m", "30m"):
        for side in ("BID", "ASK"):
            used = set()
            for tag in ("UNTESTED", "SINGLE_TEST", "MULTI_TEST", "DEEP_TESTED"):
                items = pick_examples(
                    universe,
                    {"timeframe": tf, "side": side, "tag": tag},
                    tag,
                    EXAMPLES_PER_CELL,
                    used,
                    atlas_spec_sha,
                )
                add("test_history", items, f"{tf}_{side}_{tag}")

    # H visibility
    for tf in ("5m", "15m", "30m"):
        for side in ("ASK", "BID"):
            used = set()
            for tag in ("CLEAR_CLUSTER_HULL", "CLEAR_FILLED_SINGLE", "MEMBER_ONLY"):
                items = pick_examples(
                    universe,
                    {"timeframe": tf, "side": side, "tag": tag},
                    tag,
                    EXAMPLES_PER_CELL,
                    used,
                    atlas_spec_sha,
                )
                add("visibility_classes", items, f"{tf}_{side}_{tag}")

    # I age
    for mem in ("CLUSTER_P3_4", "CLUSTER_P5_8"):
        for tf in ("5m", "15m", "30m"):
            for side in ("ASK", "BID"):
                used = set()
                for aq in ("AGE_Q1", "AGE_Q2", "AGE_Q3", "AGE_Q4"):
                    items = pick_examples(
                        universe,
                        {"timeframe": tf, "side": side, "membership": mem, "tag": aq},
                        f"{mem}|{aq}",
                        EXAMPLES_PER_CELL,
                        used,
                        atlas_spec_sha,
                    )
                    add("age_classes", items, f"{tf}_{side}_{mem}_{aq}")

    # J multi-tf confluence — use MULTI_TF tags on 5m components
    for side in ("ASK", "BID"):
        used = set()
        items = pick_examples(
            universe,
            {"timeframe": "5m", "side": side, "tag": "NO_HTF_PARENT"},
            "ONLY_5M",
            EXAMPLES_PER_CELL,
            used,
            atlas_spec_sha,
        )
        add("multi_tf_confluence", items, f"{side}_ONLY_5M")
        items = pick_examples(
            universe,
            {"timeframe": "5m", "side": side, "tag": "PARENT_15M"},
            "5M_15M",
            EXAMPLES_PER_CELL,
            used,
            atlas_spec_sha,
        )
        add("multi_tf_confluence", items, f"{side}_5M_15M")
        items = pick_examples(
            universe,
            {"timeframe": "5m", "side": side, "tag": "PARENT_30M"},
            "5M_30M",
            EXAMPLES_PER_CELL,
            used,
            atlas_spec_sha,
        )
        add("multi_tf_confluence", items, f"{side}_5M_30M")
        items = pick_examples(
            universe,
            {"timeframe": "5m", "side": side, "tag": "PARENT_15M_30M"},
            "5M_15M_30M",
            EXAMPLES_PER_CELL,
            used,
            atlas_spec_sha,
        )
        add("multi_tf_confluence", items, f"{side}_5M_15M_30M")

    return {"boards": boards, "examples": examples, "n_examples": len(examples)}


def load_candles_cache() -> dict[str, Any]:
    from research_charts.service import resolve_candle_pack, _candles_from_packed

    end = int(parse_iso("2026-08-31T14:00:00Z").timestamp())
    start = int(parse_iso("2026-07-01T00:00:00Z").timestamp())
    stores = {}
    for tf in ("5m", "15m", "30m"):
        packed = resolve_candle_pack(SYMBOL, tf, start=start, end=end, limit=3000, allow_stale=True)
        candles = _candles_from_packed(packed, allow_stale=True)
        stores[tf] = candles
    return stores


def candle_window(candles: list, as_of: datetime, n: int = BARS_WINDOW) -> list:
    from research_charts.service import _TF_SEC

    # use timeframe from first candle
    tf = candles[0].timeframe if candles else "5m"
    sec = int(_TF_SEC.get(tf, 300))
    as_ts = int(as_of.timestamp())
    closed = [c for c in candles if c.unix_seconds + sec <= as_ts]
    if not closed:
        closed = [c for c in candles if c.unix_seconds <= as_ts]
    return closed[-n:]


def render_panel(ex: dict, candles: list, mode: str, out_path: Path, members_detail: list[dict]) -> dict:
    """mode: native | normalized_bps"""
    try:
        as_of = parse_iso(ex["snapshot_ts"])
        win = candle_window(candles, as_of, BARS_WINDOW)
        if len(win) < 10:
            return {"ok": False, "error": "insufficient_bars", "path": str(out_path)}
        if any(c.unix_seconds > int(as_of.timestamp()) for c in win):
            return {"ok": False, "error": "future_candle", "path": str(out_path)}

        lo, hi = float(ex["component_lower"]), float(ex["component_upper"])
        mid = (lo + hi) / 2.0
        color = ASK_COLOR if ex["side"] == "ASK" else BID_COLOR

        fig, ax = plt.subplots(figsize=(6.4, 4.0), dpi=120)
        xs = list(range(len(win)))

        def y_price(p: float) -> float:
            if mode == "normalized_bps":
                return (p - mid) / mid * 10000.0
            return p

        # candles
        for i, c in enumerate(win):
            o, h, l, cl = y_price(c.open), y_price(c.high), y_price(c.low), y_price(c.close)
            up = cl >= o
            col = "#26a69a" if up else "#ef5350"
            ax.plot([i, i], [l, h], color=col, linewidth=0.8, zorder=2)
            body_lo, body_hi = min(o, cl), max(o, cl)
            ax.add_patch(
                Rectangle(
                    (i - 0.35, body_lo),
                    0.7,
                    max(body_hi - body_lo, 1e-9 if mode == "native" else 0.05),
                    facecolor=col,
                    edgecolor=col,
                    linewidth=0.4,
                    zorder=3,
                )
            )

        # member pools
        for m in members_detail:
            mlo, mhi = float(m["lower"]), float(m["upper"])
            ax.add_patch(
                Rectangle(
                    (-0.5, y_price(mlo)),
                    len(win),
                    y_price(mhi) - y_price(mlo),
                    facecolor=color,
                    alpha=MEMBER_ALPHA,
                    edgecolor=color,
                    linewidth=0.6,
                    linestyle="--",
                    zorder=1,
                )
            )

        # component hull
        ax.add_patch(
            Rectangle(
                (-0.5, y_price(lo)),
                len(win),
                y_price(hi) - y_price(lo),
                facecolor=color,
                alpha=HULL_ALPHA,
                edgecolor=color,
                linewidth=2.0,
                linestyle="-",
                zorder=4,
            )
        )
        # exterior edges
        ax.axhline(y_price(float(ex["exterior_front_edge"])), color=color, linewidth=1.6, zorder=5)
        ax.axhline(
            y_price(float(ex["exterior_back_edge"])), color=color, linewidth=1.0, linestyle=":", zorder=5
        )

        title = (
            f"{ex['atlas_example_id']}  {ex['timeframe']} {ex['side']}  "
            f"P={ex['P']}  Σ={ex['strength_sum']:.2f}" if ex.get("strength_sum") is not None
            else f"{ex['atlas_example_id']}  {ex['timeframe']} {ex['side']}  P={ex['P']}"
        )
        ax.set_title(title, fontsize=9)
        ylab = "bps vs component mid" if mode == "normalized_bps" else "price"
        ax.set_ylabel(ylab, fontsize=8)
        ax.set_xlabel(f"last {len(win)} closed bars ≤ {ex['snapshot_ts']}", fontsize=8)
        meta = (
            f"ep={ex['episode_id'][-16:]}\n"
            f"as_of={ex['snapshot_ts']} age={ex.get('newest_member_age_bars')}\n"
            f"[{lo:.1f},{hi:.1f}] h_bps={ex.get('component_height_bps')}\n"
            f"hull={ex.get('rendered_as_cluster_hull')} primary={ex.get('primary_class')}"
        )
        ax.text(
            0.01,
            0.99,
            meta,
            transform=ax.transAxes,
            va="top",
            ha="left",
            fontsize=6.5,
            family="monospace",
            bbox=dict(boxstyle="round,pad=0.2", facecolor="white", alpha=0.75),
        )
        ax.set_xlim(-1, len(win))
        if mode == "normalized_bps":
            # fixed comparative scale unless extreme
            span = abs(y_price(hi) - y_price(lo))
            pad = max(span * 1.5, 20)
            if pad > 800:
                ax.set_title(title + "  [SCALE_OUTLIER]", fontsize=9, color="#b71c1c")
            ax.set_ylim(-pad, pad)
        ax.tick_params(labelsize=7)
        fig.tight_layout()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_path, dpi=120)
        plt.close(fig)
        return {"ok": True, "path": str(out_path), "bars": len(win), "mode": mode}
    except Exception as exc:
        plt.close("all")
        return {"ok": False, "error": str(exc), "path": str(out_path)}


def member_bounds_at(raw: pd.DataFrame, snapshot_ts: str, member_ids: list[str]) -> list[dict]:
    out = []
    sub = raw[(raw["snapshot_ts"] == snapshot_ts) & (raw["pool_id"].isin(member_ids))]
    for _, r in sub.iterrows():
        out.append(
            {
                "pool_id": r["pool_id"],
                "lower": r["lower"],
                "upper": r["upper"],
                "raw_strength": r.get("raw_strength"),
            }
        )
    return out


def make_contact_sheet(board: str, cell_to_paths: dict[str, list[Path]], out_path: Path) -> None:
    # flatten up to 12 panels
    paths = []
    for cell, ps in sorted(cell_to_paths.items()):
        for p in ps[:1]:
            if p.is_file():
                paths.append((cell, p))
    if not paths:
        return
    n = min(len(paths), 12)
    cols = 4
    rows = math.ceil(n / cols)
    fig, axes = plt.subplots(rows, cols, figsize=(cols * 3.2, rows * 2.4), dpi=100)
    axes = np.array(axes).reshape(-1)
    for i, ax in enumerate(axes):
        if i >= n:
            ax.axis("off")
            continue
        cell, p = paths[i]
        img = plt.imread(p)
        ax.imshow(img)
        ax.set_title(cell[:40], fontsize=6)
        ax.axis("off")
    fig.suptitle(board, fontsize=11)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=100)
    plt.close(fig)


def build_html(examples: list[dict], boards: dict) -> str:
    # embed as data URIs is too heavy — use relative paths
    rows_js = json.dumps(examples, ensure_ascii=True, default=str)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Canonical Pool Class Visual Atlas v1</title>
<style>
body {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; margin: 0; background:#111; color:#eee; }}
header {{ padding: 12px 16px; background:#1b1b1b; border-bottom:1px solid #333; position:sticky; top:0; z-index:10; }}
h1 {{ margin:0 0 8px; font-size:16px; }}
.filters {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; }}
.filters label {{ font-size:11px; color:#aaa; }}
select, input, button {{ background:#222; color:#eee; border:1px solid #444; padding:4px 6px; font-size:11px; }}
.grid {{ display:grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap:12px; padding:12px; }}
.card {{ background:#1a1a1a; border:1px solid #333; border-radius:6px; overflow:hidden; }}
.card img {{ width:100%; display:block; background:#000; }}
.meta {{ padding:8px; font-size:10px; line-height:1.4; white-space:pre-wrap; }}
.badge {{ display:inline-block; background:#333; padding:1px 5px; margin:1px; border-radius:3px; }}
.count {{ font-size:11px; color:#8cf; }}
</style>
</head>
<body>
<header>
  <h1>Canonical Pool Class Visual Atlas v1 <span class="count" id="count"></span></h1>
  <div class="filters">
    <label>TF <select id="tf"><option value="">all</option><option>5m</option><option>15m</option><option>30m</option></select></label>
    <label>Side <select id="side"><option value="">all</option><option>ASK</option><option>BID</option></select></label>
    <label>Board <select id="board"><option value="">all</option></select></label>
    <label>P-class <select id="membership"><option value="">all</option>
      <option>SINGLETON_P1</option><option>PAIR_P2</option><option>CLUSTER_P3_4</option>
      <option>CLUSTER_P5_8</option><option>CLUSTER_P9_PLUS</option></select></label>
    <label>Mode <select id="mode"><option value="native">Native Price</option><option value="normalized_bps">Normalized BPS</option></select></label>
    <label>Search <input id="q" placeholder="episode / component / atlas id" size="28"/></label>
    <button onclick="applyFilters()">Apply</button>
  </div>
</header>
<main class="grid" id="grid"></main>
<script>
const EXAMPLES = {rows_js};
const boards = {json.dumps(list(boards.keys()))};
const boardSel = document.getElementById('board');
boards.forEach(b => {{ const o=document.createElement('option'); o.value=b; o.textContent=b; boardSel.appendChild(o); }});

function membershipOf(ex) {{
  const t = ex.class_tags || [];
  for (const m of ['CLUSTER_P9_PLUS','CLUSTER_P5_8','CLUSTER_P3_4','PAIR_P2','SINGLETON_P1']) if (t.includes(m)) return m;
  return '';
}}
function applyFilters() {{
  const tf = document.getElementById('tf').value;
  const side = document.getElementById('side').value;
  const board = document.getElementById('board').value;
  const mem = document.getElementById('membership').value;
  const mode = document.getElementById('mode').value;
  const q = document.getElementById('q').value.trim().toLowerCase();
  const grid = document.getElementById('grid');
  grid.innerHTML = '';
  let n = 0;
  for (const ex of EXAMPLES) {{
    if (tf && ex.timeframe !== tf) continue;
    if (side && ex.side !== side) continue;
    if (board && ex.board !== board) continue;
    if (mem && membershipOf(ex) !== mem) continue;
    const blob = (ex.atlas_example_id + ' ' + ex.episode_id + ' ' + (ex.component_id||'') + ' ' + (ex.member_pool_ids||[]).join(' ')).toLowerCase();
    if (q && !blob.includes(q)) continue;
    n++;
    const card = document.createElement('div');
    card.className = 'card';
    const img = document.createElement('img');
    img.src = 'panels/' + mode + '/' + ex.atlas_example_id + '.png';
    img.alt = ex.atlas_example_id;
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = [
      ex.atlas_example_id + ' | ' + ex.board + ' / ' + ex.cell,
      ex.timeframe + ' ' + ex.side + ' P=' + ex.P + ' Σ=' + (ex.strength_sum!=null?ex.strength_sum.toFixed(2):'na'),
      'as_of=' + ex.snapshot_ts + ' age_bars=' + ex.newest_member_age_bars,
      'hull=' + ex.rendered_as_cluster_hull + ' h_bps=' + (ex.component_height_bps!=null?ex.component_height_bps.toFixed(1):'na'),
      'episode=' + ex.episode_id,
      'primary=' + ex.primary_class,
      'tags=' + (ex.class_tags||[]).join(',')
    ].join('\\n');
    card.appendChild(img); card.appendChild(meta); grid.appendChild(card);
  }}
  document.getElementById('count').textContent = '(' + n + ' panels)';
}}
applyFilters();
</script>
</body>
</html>
"""


def exp04_selection(universe: pd.DataFrame, atlas_spec_sha: str, structural_exp: dict) -> dict:
    used: set[str] = set()
    picks = {}
    for label, filt, primary in [
        ("P3_4_BID", {"timeframe": "5m", "side": "BID", "membership": "CLUSTER_P3_4"}, "EXP04_CMP_P3_4"),
        ("P5_8_BID", {"timeframe": "5m", "side": "BID", "membership": "CLUSTER_P5_8"}, "EXP04_CMP_P5_8"),
        ("P9_PLUS_BID", {"timeframe": "5m", "side": "BID", "membership": "CLUSTER_P9_PLUS"}, "EXP04_CMP_P9"),
        ("STRONG_15M_BID", {"timeframe": "15m", "side": "BID", "membership": "CLUSTER_P5_8", "tag": "STRENGTH_Q4"}, "EXP04_CMP_15M"),
        ("STRONG_30M_BID", {"timeframe": "30m", "side": "BID", "membership": "CLUSTER_P5_8", "tag": "STRENGTH_Q4"}, "EXP04_CMP_30M"),
    ]:
        items = pick_examples(universe, filt, primary, 1, used, atlas_spec_sha)
        picks[label] = items[0] if items else None
    return {
        "exp04": {
            "pool_id": "lld:BTCUSDT:5m:lower:1787740200",
            "snapshot_ts": "2026-08-26T11:30:00Z",
            "reference_ts": "2026-08-26T11:34:51Z",
            "structural_classification": structural_exp.get("classification") or structural_exp,
        },
        "comparisons": {
            k: {
                "episode_id": v["episode_id"] if v else None,
                "component_id": v["component_id"] if v else None,
                "snapshot_ts": v["snapshot_ts"] if v else None,
                "P": int(v["P"]) if v else None,
                "selection_hash": v["selection_hash"] if v else None,
                "class_tags": list(v["class_tags"]) if v else None,
            }
            for k, v in picks.items()
        },
        "comparison_full": {k: (None if v is None else {kk: (list(vv) if isinstance(vv, (list, np.ndarray)) else vv) for kk, vv in v.items() if kk != "member_pool_ids" or True}) for k, v in picks.items()},
    }


def bundle_hash(out: Path) -> str:
    parts = []
    for p in sorted(out.rglob("*")):
        if not p.is_file():
            continue
        if p.name in ("visual_atlas_bundle_sha256",) or p.suffix == ".pyc":
            continue
        if p.suffix in (".png", ".parquet"):
            parts.append(sha256_file(p))
        elif p.suffix in (".json", ".csv", ".html", ".md"):
            data = p.read_bytes()
            if p.suffix == ".json":
                try:
                    obj = json.loads(data)
                    if isinstance(obj, dict):
                        obj.pop("created_at", None)
                        obj.pop("visual_atlas_bundle_sha256", None)
                        obj.pop("full_elapsed_s", None)
                        obj.pop("peak_rss_mb", None)
                        data = canonical_json(obj)
                except json.JSONDecodeError:
                    pass
            parts.append(sha256_bytes(data))
    return sha256_bytes("".join(parts).encode())


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for sub in ("assets", "panels/native", "panels/normalized_bps", "contact_sheets", "exp_04"):
        (OUT / sub).mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    peak = 0

    def rss():
        nonlocal peak
        ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        mb = ru / 1024
        peak = max(peak, mb)
        return mb

    try:
        verified = verify_structural()
    except RuntimeError as exc:
        verdict = "OUTCOME_BLINDNESS_VIOLATION" if "OUTCOME" in str(exc) else "STRUCTURAL_BUNDLE_HASH_MISMATCH"
        write_json(OUT / "freeze_manifest.json", {"verdict": verdict, "error": str(exc)})
        print("FAIL", exc)
        return 1

    atlas_spec = {
        "schema_version": "canonical_pool_visual_atlas_v1",
        "atlas_seed": ATLAS_SEED,
        "structural_analysis_spec_sha256": EXPECTED_SPEC,
        "structural_class_bundle_sha256": EXPECTED_BUNDLE,
        "analysis_start_utc": "2026-08-01T14:00:00Z",
        "analysis_end_utc": "2026-08-31T14:00:00Z",
        "symbol": SYMBOL,
        "timeframes": ["5m", "15m", "30m"],
        "bars_window": BARS_WINDOW,
        "examples_per_cell": EXAMPLES_PER_CELL,
        "primary_unit": "same_tf_component_episode",
        "selection_hash_formula": "SHA256(visual_atlas_spec_sha256 + episode_id + timeframe + side + primary_class + atlas_seed)",
        "outcome_forbidden": sorted(FORBIDDEN),
        "colors": {"ASK": ASK_COLOR, "BID": BID_COLOR},
        "git": {
            "orderbook_analyse": git_info(OA_ROOT),
            "spread_recovery_hedge_short_dev": git_info(DASH.parent),
        },
        "canonical_provider": verified["structural_spec"].get("canonical_provider_version"),
        "lld_config": verified["structural_spec"].get("lld_config"),
    }
    # placeholder sha excluded from own hash computation
    body = {k: v for k, v in atlas_spec.items() if k != "visual_atlas_spec_sha256"}
    atlas_spec_sha = sha256_bytes(canonical_json(body))
    atlas_spec["visual_atlas_spec_sha256"] = atlas_spec_sha
    write_json(OUT / "visual_atlas_spec.json", atlas_spec)

    print("Building universe...")
    universe, raw, _meta = build_component_universe()
    print("universe episodes", len(universe), "rss", rss())

    print("Freezing selection...")
    selection = freeze_selection(universe, atlas_spec_sha)
    selection_body = {
        "atlas_spec_sha256": atlas_spec_sha,
        "seed": ATLAS_SEED,
        "n_examples": selection["n_examples"],
        "boards": selection["boards"],
        "examples": [
            {
                k: ex[k]
                for k in (
                    "atlas_example_id",
                    "board",
                    "cell",
                    "episode_id",
                    "component_id",
                    "snapshot_ts",
                    "timeframe",
                    "side",
                    "P",
                    "primary_class",
                    "selection_hash",
                    "class_tags",
                )
            }
            for ex in selection["examples"]
        ],
    }
    selection_sha = sha256_bytes(canonical_json(selection_body))
    selection_out = {
        **selection_body,
        "representative_pool_selection_sha256": selection_sha,
        "full_examples": selection["examples"],
    }
    write_json(OUT / "representative_pool_selection.json", selection_out)
    print("selection frozen", selection["n_examples"], "sha", selection_sha[:16])

    # EXP04 comparison selection (also before render)
    structural_exp = json.loads((STRUCT / "exp_04_structural_classification.json").read_text())
    exp04 = exp04_selection(universe, atlas_spec_sha, structural_exp)
    write_json(OUT / "exp_04_comparison_manifest.json", exp04)

    # empty review CSV
    review_cols = [
        "atlas_example_id",
        "episode_id",
        "timeframe",
        "side",
        "primary_class",
        "visually_clear",
        "dominant_main_pool",
        "edge_is_unambiguous",
        "child_edge_should_be_ignored",
        "notes",
        "reviewed_by",
        "reviewed_at",
    ]
    pd.DataFrame(
        [
            {
                "atlas_example_id": ex["atlas_example_id"],
                "episode_id": ex["episode_id"],
                "timeframe": ex["timeframe"],
                "side": ex["side"],
                "primary_class": ex["primary_class"],
                "visually_clear": "",
                "dominant_main_pool": "",
                "edge_is_unambiguous": "",
                "child_edge_should_be_ignored": "",
                "notes": "",
                "reviewed_by": "",
                "reviewed_at": "",
            }
            for ex in selection["examples"]
        ],
        columns=review_cols,
    ).to_csv(OUT / "manual_pool_clarity_review.csv", index=False)

    print("Loading candles...")
    stores = load_candles_cache()
    print("candles loaded", {k: len(v) for k, v in stores.items()}, "rss", rss())

    # Render all selected examples
    render_manifest = []
    failures = []
    board_paths: dict[str, dict[str, list[Path]]] = defaultdict(lambda: defaultdict(list))

    examples = selection["examples"]
    for i, ex in enumerate(examples):
        if i % 50 == 0:
            print(f"render {i}/{len(examples)} rss={rss():.0f}")
        members = member_bounds_at(raw, ex["snapshot_ts"], ex["member_pool_ids"])
        for mode, sub in (("native", "native"), ("normalized_bps", "normalized_bps")):
            path = OUT / "panels" / sub / f"{ex['atlas_example_id']}.png"
            res = render_panel(ex, stores[ex["timeframe"]], mode, path, members)
            res.update(
                {
                    "atlas_example_id": ex["atlas_example_id"],
                    "episode_id": ex["episode_id"],
                    "board": ex["board"],
                    "cell": ex["cell"],
                    "mode": mode,
                }
            )
            render_manifest.append(res)
            if not res["ok"]:
                failures.append(res)
            elif mode == "native":
                board_paths[ex["board"]][ex["cell"]].append(path)

    # EXP04 panels
    exp_pool = structural_exp.get("classification") or {}
    # find component at EXP04 snapshot
    exp_comp = universe[
        (universe["snapshot_ts"] == "2026-08-26T11:30:00Z")
        & (universe["timeframe"] == "5m")
        & (universe["side"] == "BID")
        & universe["member_pool_ids"].apply(
            lambda ids: "lld:BTCUSDT:5m:lower:1787740200" in list(ids) if isinstance(ids, (list, np.ndarray)) else False
        )
    ]
    exp_examples = []
    if len(exp_comp):
        er = exp_comp.iloc[0].to_dict()
        er["atlas_example_id"] = "EXP04_DIAG"
        er["primary_class"] = "EXP04"
        er["board"] = "exp_04_comparison"
        er["cell"] = "EXP04"
        er["selection_hash"] = selection_hash(atlas_spec_sha, er["episode_id"], "5m", "BID", "EXP04")
        er["member_pool_ids"] = list(er["member_pool_ids"])
        er["class_tags"] = list(er["class_tags"])
        er["P"] = int(er["P"])
        exp_examples.append(er)
    for label, full in (exp04.get("comparison_full") or {}).items():
        if not full:
            continue
        full = dict(full)
        full["atlas_example_id"] = f"EXP04_CMP_{label}"
        full["board"] = "exp_04_comparison"
        full["cell"] = label
        full["primary_class"] = full.get("primary_class") or label
        full["member_pool_ids"] = list(full.get("member_pool_ids") or [])
        full["class_tags"] = list(full.get("class_tags") or [])
        full["P"] = int(full["P"])
        exp_examples.append(full)

    for ex in exp_examples:
        members = member_bounds_at(raw, ex["snapshot_ts"], ex["member_pool_ids"])
        for mode, sub in (("native", "native"), ("normalized_bps", "normalized_bps")):
            path = OUT / "exp_04" / f"{ex['atlas_example_id']}_{mode}.png"
            # also copy into panels for html consistency for non-EXP04 cmp
            res = render_panel(ex, stores[ex["timeframe"]], mode, path, members)
            if mode == "native":
                alt = OUT / "panels" / "native" / f"{ex['atlas_example_id']}.png"
                if res["ok"]:
                    alt.write_bytes(path.read_bytes())
                    board_paths["exp_04_comparison"][ex["cell"]].append(alt)
            else:
                alt = OUT / "panels" / "normalized_bps" / f"{ex['atlas_example_id']}.png"
                if res["ok"]:
                    alt.write_bytes(path.read_bytes())
            render_manifest.append({**res, "atlas_example_id": ex["atlas_example_id"], "board": "exp_04_comparison"})
            if not res["ok"]:
                failures.append(res)
        # add to examples list for HTML
        selection["examples"].append(
            {
                "atlas_example_id": ex["atlas_example_id"],
                "board": "exp_04_comparison",
                "cell": ex["cell"],
                "episode_id": ex["episode_id"],
                "component_id": ex["component_id"],
                "snapshot_ts": ex["snapshot_ts"],
                "timeframe": ex["timeframe"],
                "side": ex["side"],
                "P": int(ex["P"]),
                "strength_sum": float(ex["strength_sum"]) if ex.get("strength_sum") is not None else None,
                "strength_max": float(ex["strength_max"]) if ex.get("strength_max") is not None else None,
                "component_lower": float(ex["component_lower"]),
                "component_upper": float(ex["component_upper"]),
                "component_height_bps": float(ex["component_height_bps"])
                if ex.get("component_height_bps") is not None
                else None,
                "newest_member_age_bars": int(ex["newest_member_age_bars"])
                if ex.get("newest_member_age_bars") is not None
                else None,
                "rendered_as_cluster_hull": bool(ex.get("rendered_as_cluster_hull")),
                "chart_label": ex.get("chart_label"),
                "member_pool_ids": list(ex["member_pool_ids"]),
                "class_tags": list(ex["class_tags"]),
                "primary_class": ex.get("primary_class"),
                "selection_hash": ex.get("selection_hash"),
                "exterior_front_edge": float(ex["exterior_front_edge"]),
                "exterior_back_edge": float(ex["exterior_back_edge"]),
                "market_price": float(ex["market_price"]) if ex.get("market_price") is not None else None,
                "touch_count_proxy": int(ex.get("touch_count_proxy") or 0),
            }
        )

    write_json(OUT / "render_manifest.json", {"n": len(render_manifest), "rows": render_manifest})
    write_json(OUT / "render_failures.json", {"n": len(failures), "rows": failures})

    # contact sheets
    for board, cells in board_paths.items():
        make_contact_sheet(board, cells, OUT / "contact_sheets" / f"{board}.png")

    # atlas_examples.csv
    pd.DataFrame(selection["examples"]).to_csv(OUT / "atlas_examples.csv", index=False)

    # coverage
    cov_rows = []
    for mem in ("SINGLETON_P1", "PAIR_P2", "CLUSTER_P3_4", "CLUSTER_P5_8", "CLUSTER_P9_PLUS"):
        avail = int((universe["membership"] == mem).sum())
        sel = sum(1 for e in selection["examples"] if mem in (e.get("class_tags") or []) or e.get("primary_class") == mem)
        cov_rows.append({"stratum": mem, "available_episodes": avail, "selected": sel})
    for tf in ("5m", "15m", "30m"):
        for side in ("ASK", "BID"):
            avail = int(((universe["timeframe"] == tf) & (universe["side"] == side)).sum())
            sel = sum(1 for e in selection["examples"] if e["timeframe"] == tf and e["side"] == side)
            cov_rows.append({"stratum": f"{tf}_{side}", "available_episodes": avail, "selected": sel})
    pd.DataFrame(cov_rows).to_csv(OUT / "atlas_class_coverage.csv", index=False)

    # HTML
    boards_for_html = selection["boards"]
    boards_for_html["exp_04_comparison"] = {ex["cell"]: [ex["atlas_example_id"]] for ex in exp_examples}
    (OUT / "POOL_CLASS_ATLAS.html").write_text(
        build_html(selection["examples"], boards_for_html), encoding="utf-8"
    )

    # outcome blindness audit
    viol = []
    for p in OUT.rglob("*"):
        if p.suffix == ".parquet":
            for c in pd.read_parquet(p).columns:
                cl = c.lower()
                for k in FORBIDDEN:
                    if cl == k or cl.endswith(f"_{k}") or cl.startswith(f"{k}_"):
                        viol.append({"file": p.name, "column": c})
        elif p.suffix == ".json" and p.name != "visual_atlas_spec.json":
            try:
                obj = json.loads(p.read_text())
            except Exception:
                continue

            def walk(o, path=""):
                if isinstance(o, dict):
                    for kk, vv in o.items():
                        kl = str(kk).lower()
                        for fk in FORBIDDEN:
                            if kl == fk:
                                # allow word "outcome" only in note strings? fail if key
                                viol.append({"file": p.name, "field": path + kk})
                        walk(vv, path + str(kk) + ".")
                elif isinstance(o, list):
                    for i, vv in enumerate(o[:30]):
                        walk(vv, path + f"[{i}].")

            walk(obj)
    # filter known benign: note fields containing "Outcome-blind"
    viol = [v for v in viol if not str(v.get("field", "")).endswith("note")]
    write_json(OUT / "outcome_blindness_audit.json", {"passed": len(viol) == 0, "violations": viol})

    # parity sample: recompute bounds from universe vs selection
    u_map = universe.set_index("episode_id")
    parity_ok = True
    parity_rows = []
    for ex in selection["examples"][:40]:
        if ex["episode_id"] not in u_map.index and not str(ex["atlas_example_id"]).startswith("EXP04"):
            parity_ok = False
            parity_rows.append({"id": ex["atlas_example_id"], "ok": False, "reason": "missing_episode"})
            continue
        if ex["episode_id"] in u_map.index:
            u = u_map.loc[ex["episode_id"]]
            if isinstance(u, pd.DataFrame):
                u = u.iloc[0]
            ok = abs(float(u["component_lower"]) - float(ex["component_lower"])) < 1e-6
            ok = ok and abs(float(u["component_upper"]) - float(ex["component_upper"])) < 1e-6
            ok = ok and int(u["P"]) == int(ex["P"])
            parity_ok = parity_ok and ok
            parity_rows.append({"id": ex["atlas_example_id"], "ok": bool(ok)})
    # selection determinism recompute
    selection2 = freeze_selection(universe, atlas_spec_sha)
    det_ok = [e["episode_id"] for e in selection["examples"] if not str(e["atlas_example_id"]).startswith("EXP04")] == [
        e["episode_id"] for e in selection2["examples"]
    ]
    # mutation test
    mut = dict(selection_body)
    mut["n_examples"] = mut["n_examples"] + 1
    mut_sha = sha256_bytes(canonical_json(mut))
    write_json(
        OUT / "parity_report.json",
        {
            "selection_deterministic": det_ok,
            "bounds_sample_ok": parity_ok,
            "sample_rows": parity_rows,
            "mutation_changes_hash": mut_sha != selection_sha,
            "exp04_pool_id_ok": structural_exp.get("pool_id") == "lld:BTCUSDT:5m:lower:1787740200",
            "exp04_snapshot_ok": structural_exp.get("nearest_snapshot_ts") == "2026-08-26T11:30:00Z",
        },
    )

    write_json(
        OUT / "test_results.json",
        {
            "structural_spec_hash_ok": True,
            "structural_bundle_hash_ok": True,
            "selection_before_render": True,
            "selection_deterministic": det_ok,
            "mutation_test": mut_sha != selection_sha,
            "outcome_blind": len(viol) == 0,
            "render_failures": len(failures),
            "parity_ok": parity_ok,
        },
    )

    # report
    verdict = "CANONICAL_POOL_VISUAL_ATLAS_COMPLETE"
    if failures:
        verdict = "CANONICAL_POOL_VISUAL_ATLAS_PARTIAL_COVERAGE" if len(failures) < len(render_manifest) * 0.05 else "CANONICAL_POOL_VISUAL_ATLAS_RENDER_FAILURE"
    if not det_ok or not parity_ok:
        verdict = "CANONICAL_POOL_PARITY_FAILURE"
    if viol:
        verdict = "OUTCOME_BLINDNESS_VIOLATION"

    bhash = bundle_hash(OUT)
    freeze = {
        "verdict": verdict,
        "visual_atlas_spec_sha256": atlas_spec_sha,
        "representative_pool_selection_sha256": selection_sha,
        "visual_atlas_bundle_sha256": bhash,
        "structural_analysis_spec_sha256": EXPECTED_SPEC,
        "structural_class_bundle_sha256": EXPECTED_BUNDLE,
        "n_examples": len(selection["examples"]),
        "n_render_ok": sum(1 for r in render_manifest if r.get("ok")),
        "n_render_fail": len(failures),
        "full_elapsed_s": time.perf_counter() - t0,
        "peak_rss_mb": peak,
    }
    write_json(OUT / "freeze_manifest.json", freeze)

    report = f"""# Canonical Pool Visual Atlas v1

**Verdict:** `{verdict}`

## Hashes
- structural_analysis_spec_sha256: `{EXPECTED_SPEC}`
- structural_class_bundle_sha256: `{EXPECTED_BUNDLE}`
- visual_atlas_spec_sha256: `{atlas_spec_sha}`
- representative_pool_selection_sha256: `{selection_sha}`
- visual_atlas_bundle_sha256: `{bhash}`

## Selection
- Seed: `{ATLAS_SEED}`
- Primary unit: same-TF component episode (max-P snapshot)
- Examples (incl. EXP04 board): **{len(selection['examples'])}**
- Panels rendered OK: **{sum(1 for r in render_manifest if r.get('ok'))}**
- Failures: **{len(failures)}**
- Selection deterministic recompute: **{det_ok}**

## Coverage
See `atlas_class_coverage.csv`. Boards: {', '.join(sorted(selection['boards'].keys()))}.

## EXP_04
Reference pool `lld:BTCUSDT:5m:lower:1787740200` at 2026-08-26T11:30:00Z compared to deterministically selected P3–4 / P5–8 / P9+ 5m BID clusters and strong 15m/30m BID parents.
See `exp_04/` and `exp_04_comparison_manifest.json`.

## How to review
1. Open `POOL_CLASS_ATLAS.html` in a local browser (file://).
2. Filter by TF / Side / Board / P-class; toggle Native vs Normalized BPS.
3. Fill `manual_pool_clarity_review.csv` columns `visually_clear`, `dominant_main_pool`, `edge_is_unambiguous`, `child_edge_should_be_ignored` manually.
4. Do **not** use outcomes or forward returns.

## Live safety
Read-only. No ClickHouse writes, no engine/strategy/freeze changes, no EXP execution, no commit.

## Output
`{OUT}`
"""
    (OUT / "VISUAL_ATLAS_REPORT.md").write_text(report, encoding="utf-8")
    print("DONE", verdict, "bundle", bhash[:16], "examples", len(selection["examples"]))
    return 0 if verdict in ("CANONICAL_POOL_VISUAL_ATLAS_COMPLETE", "CANONICAL_POOL_VISUAL_ATLAS_PARTIAL_COVERAGE") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        write_json(OUT / "freeze_manifest.json", {"verdict": "FAILED", "trace": traceback.format_exc()})
        raise

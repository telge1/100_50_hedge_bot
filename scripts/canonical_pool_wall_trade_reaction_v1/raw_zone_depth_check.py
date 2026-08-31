#!/usr/bin/env python3
"""Raw-OB200 zone-depth check for Strong pool touches (24–28 Aug window).

Read-only. Compares 1s dominant-wall proxy vs full-depth liquidity inside
[lower, upper] at first_touch ±60s.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path("/home/telgenbuescher/projects/orderbook_analyse")
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.ob200_v3_raw_discovery.audit import (  # noqa: E402
    is_replayable_line,
    iter_decompressed_lines,
    line_to_replay_payload,
)
from orderbook_analyse.ob200_v3_raw_discovery.files import list_closed_segments  # noqa: E402
from orderbook_analyse.ob200_v3_raw_discovery.mutable_book import MutableBook  # noqa: E402

OUT = ROOT / "results/canonical_pool_wall_trade_reaction_v1"
RAW_ROOT = ROOT / "data/orderbook_raw_shadow/ob200_v3"
SYMBOL = "BTCUSDT"
ZERO = Decimal("0")


def _utc(ts: str | datetime | pd.Timestamp) -> datetime:
    if isinstance(ts, pd.Timestamp):
        ts = ts.to_pydatetime()
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _ms(dt: datetime) -> int:
    return int(_utc(dt).timestamp() * 1000)


def zone_stats(book: MutableBook, *, side: str, lower: float, upper: float) -> dict[str, Any]:
    lo = Decimal(str(lower))
    hi = Decimal(str(upper))
    levels = book.sorted_bids() if side == "BID" else book.sorted_asks()
    inside = [(p, q) for p, q in levels if lo <= p <= hi and q > ZERO]
    qty = sum((q for _, q in inside), ZERO)
    notional = sum((p * q for p, q in inside), ZERO)
    max_lvl = max(inside, key=lambda x: x[1]) if inside else None
    # dominant wall on full side (top 25)
    window = levels[:25]
    dom = max(window, key=lambda x: x[1]) if window else None
    dom_in = bool(dom and lo <= dom[0] <= hi)
    mid = None
    if book.bids and book.asks:
        bb = max(book.bids)
        ba = min(book.asks)
        mid = float((bb + ba) / 2)
    return {
        "book_valid": bool(book.is_valid),
        "mid": mid,
        "zone_level_count": len(inside),
        "zone_qty": float(qty),
        "zone_notional": float(notional),
        "zone_max_level_price": float(max_lvl[0]) if max_lvl else None,
        "zone_max_level_qty": float(max_lvl[1]) if max_lvl else None,
        "dom_wall_price": float(dom[0]) if dom else None,
        "dom_wall_qty": float(dom[1]) if dom else None,
        "dom_wall_in_zone": dom_in,
        "zone_has_depth": len(inside) >= 2 and float(qty) > 0,
        "zone_vs_dom_qty_ratio": float(qty / dom[1]) if dom and dom[1] > 0 else None,
    }


def replay_capture(
    refs: list,
    *,
    targets_ms: dict[str, int],
) -> dict[str, MutableBook | None]:
    """Replay segments covering target times; return book clone-ish snapshots.

    We keep only level dicts at each target (shallow copy of bids/asks).
    """
    want = dict(sorted(targets_ms.items(), key=lambda kv: kv[1]))
    remaining = set(want.keys())
    out: dict[str, MutableBook | None] = {k: None for k in want}
    if not refs or not remaining:
        return out

    book = MutableBook()
    # process refs in order
    for ref in sorted(refs, key=lambda r: r.start_utc):
        for _, obj in iter_decompressed_lines(ref.path):
            if not is_replayable_line(obj):
                continue
            payload = line_to_replay_payload(obj)
            data = payload.get("data") or {}
            mtype = payload.get("type")
            ts = obj.get("ts")
            if mtype == "snapshot":
                book.apply_snapshot(data)
            elif mtype == "delta":
                book.apply_delta(data)
            else:
                continue
            if not isinstance(ts, int) or not book.is_valid:
                continue
            done = []
            for name, tms in list(want.items()):
                if name not in remaining:
                    continue
                if ts >= tms and out[name] is None:
                    snap = MutableBook()
                    snap.bids = dict(book.bids)
                    snap.asks = dict(book.asks)
                    snap.last_u = book.last_u
                    snap.last_seq = book.last_seq
                    snap.is_valid = book.is_valid
                    out[name] = snap
                    remaining.discard(name)
                    done.append(name)
            if not remaining:
                return out
    return out


def main() -> int:
    t0 = time.perf_counter()
    strong = pd.read_csv(OUT / "strong_in_raw_window.csv")
    shortlist = pd.read_csv(OUT / "visual_check_shortlist.csv")
    # ensure shortlist-in-raw cases are included
    short_ids = set(
        shortlist[pd.to_datetime(shortlist["first_touch_ts"], utc=True) >= "2026-08-24"]["pool_id"]
    )
    # sample: all 41 is doable if each is ~1 hour segment — might be slow.
    # Process all 41; each case typically 1 segment (~few seconds).
    cases = strong.sort_values("first_touch_ts").copy()
    print(f"cases={len(cases)} shortlist_in_set={len(short_ids & set(cases.pool_id))}", flush=True)

    rows: list[dict[str, Any]] = []
    for i, ep in enumerate(cases.itertuples(index=False), start=1):
        touch = _utc(ep.first_touch_ts)
        lower = float(ep.lower)
        upper = float(ep.upper)
        side = str(ep.side).upper()
        t_pre = touch - timedelta(seconds=60)
        t_post = touch + timedelta(seconds=60)
        refs = list_closed_segments(
            RAW_ROOT,
            symbols=(SYMBOL,),
            start=t_pre - timedelta(seconds=5),
            end=t_post + timedelta(seconds=5),
        )
        targets = {
            "pre": _ms(t_pre),
            "touch": _ms(touch),
            "post": _ms(t_post),
        }
        snaps = replay_capture(refs, targets_ms=targets) if refs else {k: None for k in targets}
        row: dict[str, Any] = {
            "pool_id": ep.pool_id,
            "timeframe": ep.timeframe,
            "side": side,
            "lower": lower,
            "upper": upper,
            "first_touch_ts": ep.first_touch_ts,
            "reaction_1s": ep.reaction,
            "wall_in_pool_1s": ep.wall_in_pool,
            "wall_in_zone_frac_1s": ep.wall_in_zone_frac,
            "median_wall_notional_1s": ep.median_wall_notional_in_zone,
            "wall_fate_1s": ep.wall_fate,
            "max_penetration_pct_1s": ep.max_penetration_pct,
            "in_visual_shortlist": ep.pool_id in short_ids,
            "n_segments": len(refs),
        }
        for label in ("pre", "touch", "post"):
            book = snaps.get(label)
            if book is None:
                row[f"{label}_raw_ok"] = False
                continue
            st = zone_stats(book, side=side, lower=lower, upper=upper)
            row[f"{label}_raw_ok"] = True
            for k, v in st.items():
                row[f"{label}_{k}"] = v
        # Derived: raw confirms wall-in-zone at touch?
        row["raw_dom_in_zone_at_touch"] = row.get("touch_dom_wall_in_zone")
        row["raw_zone_has_depth_at_touch"] = row.get("touch_zone_has_depth")
        row["raw_zone_notional_at_touch"] = row.get("touch_zone_notional")
        row["raw_zone_notional_drop_60s"] = None
        if row.get("pre_zone_notional") is not None and row.get("post_zone_notional") is not None:
            row["raw_zone_notional_drop_60s"] = float(row["pre_zone_notional"]) - float(
                row["post_zone_notional"]
            )
        # mid reaction check from raw mids
        row["raw_reaction_check"] = None
        if row.get("touch_mid") and row.get("post_mid"):
            front = upper if side == "BID" else lower
            back = lower if side == "BID" else upper
            zone_h = max(upper - lower, 1e-9)
            m0 = float(row["touch_mid"])
            m1 = float(row["post_mid"])
            if side == "BID":
                crossed = m1 <= back
                rev = (m1 - front) / zone_h
            else:
                crossed = m1 >= back
                rev = (front - m1) / zone_h
            if crossed:
                row["raw_reaction_check"] = "PASSED_HINT"
            elif rev >= 0.5:
                row["raw_reaction_check"] = "REJECT_HINT"
            else:
                row["raw_reaction_check"] = "AMBIGUOUS_HINT"
        rows.append(row)
        if i % 5 == 0 or i == 1 or i == len(cases):
            print(
                f"  {i}/{len(cases)} {ep.pool_id} segs={len(refs)} "
                f"touch_ok={row.get('touch_raw_ok')} zone_n={row.get('touch_zone_notional')}",
                flush=True,
            )

    df = pd.DataFrame(rows)
    out_csv = OUT / "raw_zone_depth_check.csv"
    df.to_csv(out_csv, index=False)

    ok = df[df["touch_raw_ok"] == True]  # noqa: E712
    summary = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_cases": int(len(df)),
        "n_touch_raw_ok": int(len(ok)),
        "raw_root": str(RAW_ROOT),
        "proxy_wall_yes_rate": float((df["wall_in_pool_1s"] == "YES").mean()) if len(df) else None,
        "raw_dom_in_zone_rate": float(ok["raw_dom_in_zone_at_touch"].mean()) if len(ok) else None,
        "raw_zone_has_depth_rate": float(ok["raw_zone_has_depth_at_touch"].mean()) if len(ok) else None,
        "median_zone_notional_at_touch": float(ok["raw_zone_notional_at_touch"].median())
        if len(ok)
        else None,
        "median_zone_level_count_at_touch": float(ok["touch_zone_level_count"].median())
        if len(ok)
        else None,
        "median_zone_vs_dom_qty_ratio": float(ok["touch_zone_vs_dom_qty_ratio"].median())
        if len(ok)
        else None,
        "agreement_1s_wall_vs_raw_dom_in_zone": float(
            (ok["wall_in_pool_1s"].eq("YES") == ok["raw_dom_in_zone_at_touch"]).mean()
        )
        if len(ok)
        else None,
        "reject_rate_1s_among_ok": float((ok["reaction_1s"] == "REJECTED").mean()) if len(ok) else None,
        "reject_rate_when_raw_zone_depth": float(
            (ok[ok["raw_zone_has_depth_at_touch"] == True]["reaction_1s"] == "REJECTED").mean()  # noqa: E712
        )
        if len(ok[ok["raw_zone_has_depth_at_touch"] == True])  # noqa: E712
        else None,
        "reject_rate_when_no_raw_zone_depth": float(
            (ok[ok["raw_zone_has_depth_at_touch"] == False]["reaction_1s"] == "REJECTED").mean()  # noqa: E712
        )
        if len(ok[ok["raw_zone_has_depth_at_touch"] == False])  # noqa: E712
        else None,
        "elapsed_s": round(time.perf_counter() - t0, 1),
        "output": str(out_csv),
    }
    (OUT / "raw_zone_depth_check.json").write_text(json.dumps(summary, indent=2) + "\n")

    # short human report
    lines = [
        "# Raw OB200 zone-depth check (Strong cases 24–28 Aug)",
        "",
        f"Generated: `{summary['generated_at']}`",
        f"Cases: **{summary['n_cases']}** · raw touch OK: **{summary['n_touch_raw_ok']}**",
        "",
        "## Does the 1s wall proxy match full depth?",
        "",
        f"- 1s `wall_in_pool=YES` rate (input set): **{summary['proxy_wall_yes_rate']}**",
        f"- Raw dominant wall inside zone at touch: **{summary['raw_dom_in_zone_rate']}**",
        f"- Agreement (1s YES ↔ raw dom-in-zone): **{summary['agreement_1s_wall_vs_raw_dom_in_zone']}**",
        f"- Raw zone has ≥2 levels with size: **{summary['raw_zone_has_depth_rate']}**",
        f"- Median zone notional at touch: **{summary['median_zone_notional_at_touch']}**",
        f"- Median levels in zone: **{summary['median_zone_level_count_at_touch']}**",
        f"- Median zone_qty / dominant_wall_qty: **{summary['median_zone_vs_dom_qty_ratio']}**",
        "",
        "## Does raw zone depth separate rejects?",
        "",
        f"- Reject rate overall (1s label): **{summary['reject_rate_1s_among_ok']}**",
        f"- When raw zone has depth: **{summary['reject_rate_when_raw_zone_depth']}**",
        f"- When raw zone empty/thin: **{summary['reject_rate_when_no_raw_zone_depth']}**",
        "",
        f"Details: `{out_csv.name}`",
        "",
    ]
    (OUT / "RAW_ZONE_CHECK.md").write_text("\n".join(lines))
    print(json.dumps(summary, indent=2), flush=True)
    print(f"DONE → {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

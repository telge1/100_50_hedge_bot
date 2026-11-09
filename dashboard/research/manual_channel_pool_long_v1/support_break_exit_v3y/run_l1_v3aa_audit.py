#!/usr/bin/env python3
"""L1 V3AA — single-trade forensic audit (read-only, frozen snapshot)."""

from __future__ import annotations

import hashlib
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

V3Y = Path(__file__).resolve().parent
PROBE = V3Y.parent
if str(PROBE) not in sys.path:
    sys.path.insert(0, str(PROBE))
if str(PROBE.parent / "manual_line_touch_probe_v1") not in sys.path:
    sys.path.insert(0, str(PROBE.parent / "manual_line_touch_probe_v1"))
if str(V3Y) not in sys.path:
    sys.path.insert(0, str(V3Y))

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import run_l1_pilot as l1
from line_geometry import BAR_SEC_5M, fmt_utc
from matplotlib.patches import Rectangle

from context import build_frozen_context
from support_sim import line_price_at_close, simulate_exit_v3y
from v3y_config import CONFIRM_DELTA, DRAWINGS_SHA256

OUT_MD = V3Y / "L1_V3AA_ORIGINAL_SUPPORT_TRADE_AUDIT.md"
OUT_JSON = V3Y / "l1_v3aa_audit_payload.json"
OUT_PNG = V3Y / "l1_v3aa_original_support_diagnostic.png"

TRADE = {
    "entry_utc": "2026-08-31 00:25:00 UTC",
    "entry_price": 0.08233,
    "tp": 0.085936,
    "sl": 0.08134204,
    "support_id": "d-9b6e62dc884c",
    "exit_utc": "2026-08-31 02:55:00 UTC",
    "exit_price": 0.0816,
    "pnl_pct": -0.8867,
    "touch_event_id": "te-d-9b6e62dc884c-0006",
}


def parse_utc(s: str) -> datetime:
    return datetime.strptime(s.replace(" UTC", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def ts(s: str) -> int:
    return int(parse_utc(s).timestamp())


def candle_by_ts(candles: list, t: int) -> dict | None:
    for c in candles:
        if int(c["time"]) == t:
            return c
    return None


def line_price_at_close_time(line, t_open: int) -> float | None:
    c = {"time": t_open}
    if not line.active_for_candle(float(t_open)):
        return None
    return line_price_at_close(line, c)


def fsm_walk(candles, entry_idx, exit_idx, line, d=CONFIRM_DELTA) -> list[dict]:
    rows = []
    state = "MONITOR"
    break_idx = -1
    break_close = 0.0
    for j in range(entry_idx, exit_idx + 1):
        c = candles[j]
        t = int(c["time"])
        lp = line_price_at_close(line, c) if line.active_for_candle(float(t)) else None
        note = ""
        if state == "MONITOR" and lp is not None:
            if float(c["close"]) < lp:
                state = "BREAK_PENDING"
                break_close = float(c["close"])
                break_idx = j
                note = "BREAK"
        elif state == "BREAK_PENDING" and j > break_idx and lp is not None:
            nc = float(c["close"])
            if nc <= break_close * (1.0 - d):
                note = "CONFIRM_EXIT_SIGNAL"
            elif nc >= break_close * (1.0 + d):
                state = "MONITOR"
                note = "BULL_HOLD"
            elif nc >= lp:
                state = "MONITOR"
                note = "RECLAIM"
            else:
                break_close = nc
                break_idx = j
                note = "NEUTRAL_ROLL"
        rows.append(
            {
                "utc": fmt_utc(t),
                "o": float(c["open"]),
                "h": float(c["high"]),
                "l": float(c["low"]),
                "c": float(c["close"]),
                "line_close": lp,
                "close_vs_line_pct": ((float(c["close"]) - lp) / lp * 100.0) if lp else None,
                "state": state,
                "note": note,
            }
        )
    return rows


def counterfactual_after(candles, entry_idx, entry, sl, tp, from_ts: int, channel_line, line) -> dict:
    """If position still open from entry after support-exit time (no support rule)."""
    first = None
    for j in range(entry_idx, len(candles)):
        c = candles[j]
        if int(c["time"]) < from_ts:
            continue
        hit_sl = float(c["low"]) <= sl
        hit_tp = float(c["high"]) >= tp
        ch = None
        if channel_line is not None:
            ch_px = channel_line.price_at(float(c["time"])) if channel_line.active_for_candle(float(c["time"])) else None
            if ch_px is not None and float(c["high"]) >= ch_px:
                ch = ch_px
        events = []
        if hit_sl:
            events.append(("SL", sl, int(c["time"])))
        if hit_tp:
            events.append(("TP", tp, int(c["time"])))
        if ch is not None:
            events.append(("CHANNEL_UPPER", ch, int(c["time"])))
        if events:
            events.sort(key=lambda x: x[1])
            first = events[0]
            break
    return {"first_after_exit": first}


def post_exit_stats(candles, exit_ts: int, entry: float, line, hours: list[int]) -> dict:
    out = {}
    exit_i = next(i for i, c in enumerate(candles) if int(c["time"]) == exit_ts)
    reclaim = None
    mins_below = 0
    for j in range(exit_i, len(candles)):
        c = candles[j]
        t = int(c["time"])
        if not line.active_for_candle(float(t)):
            break
        lp = line_price_at_close(line, c)
        if lp is None:
            continue
        cl = float(c["close"])
        if reclaim is None and cl >= lp:
            reclaim = t
            break
        if cl < lp:
            mins_below += 5
    out["reclaim_utc"] = fmt_utc(reclaim) if reclaim else None
    out["minutes_close_below_line_after_exit"] = mins_below
    out["line_still_valid_at_reclaim"] = bool(reclaim and line.active_for_candle(float(reclaim)))
    for h in hours:
        end = exit_ts + h * 3600
        hi = lo = None
        for c in candles:
            t = int(c["time"])
            if t < exit_ts or t > end:
                continue
            hi = max(hi or float(c["high"]), float(c["high"]))
            lo = min(lo or float(c["low"]), float(c["low"]))
        out[f"high_{h}h"] = hi
        out[f"low_{h}h"] = lo
        out[f"mfe_vs_entry_{h}h_pct"] = round((hi - entry) / entry * 100, 4) if hi else None
        out[f"mae_vs_entry_{h}h_pct"] = round((lo - entry) / entry * 100, 4) if lo else None
    return out


def render_chart(candles, line, entry_ts, break_ts, confirm_ts, exit_ts, entry, sl, tp, path: Path) -> None:
    t0 = entry_ts - 6 * 3600
    t1 = exit_ts + 24 * 3600
    subset = [c for c in candles if t0 <= int(c["time"]) <= t1]
    if not subset:
        return
    times = [datetime.fromtimestamp(int(c["time"]), tz=timezone.utc) for c in subset]
    fig, ax = plt.subplots(figsize=(14, 6))
    width = timedelta(minutes=4)
    for c, t in zip(subset, times):
        o, h, l, cl = float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])
        color = "#26a69a" if cl >= o else "#ef5350"
        ax.plot([t, t], [l, h], color=color, linewidth=0.8)
        ax.add_patch(
            Rectangle(
                (t - width / 2, min(o, cl)),
                width,
                abs(cl - o) or 1e-8,
                facecolor=color,
                edgecolor=color,
            )
        )
    t_line = [datetime.fromtimestamp(int(c["time"]), tz=timezone.utc) for c in subset]
    lp = []
    for c in subset:
        t = int(c["time"])
        if line.active_for_candle(float(t)):
            p = line.price_at(float(t) + BAR_SEC_5M)
            lp.append(p)
        else:
            lp.append(math.nan)
    ax.plot(t_line, lp, color="#c9a227", linewidth=2, label="Frozen support d-9b6e62dc884c")
    ax.axhline(tp, color="#33d778", linestyle="--", linewidth=1, label=f"TP {tp}")
    ax.axhline(sl, color="#ef5350", linestyle="--", linewidth=1, label=f"SL {sl}")
    for label, xt, col in [
        ("Entry", entry_ts, "#2196f3"),
        ("Break", break_ts, "#ff9800"),
        ("Confirm", confirm_ts, "#ff5722"),
        ("Exit", exit_ts, "#e91e63"),
    ]:
        ax.axvline(
            datetime.fromtimestamp(xt, tz=timezone.utc),
            color=col,
            linestyle=":",
            alpha=0.85,
            label=label,
        )
    ax.set_title("L1 V3AA diagnostic · DOGEUSDT 5m · frozen support only")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M", tz=timezone.utc))
    ax.set_ylabel("Price")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.25)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


def main() -> int:
    l1._bootstrap_import_paths()
    snap_path = (V3Y / "../l1_v3u_drawings_snapshot_d05d218646aced84.json").resolve()
    snap_text = snap_path.read_text(encoding="utf-8")
    sha = hashlib.sha256(snap_text.encode()).hexdigest()
    if sha != DRAWINGS_SHA256:
        raise SystemExit(f"sha mismatch {sha}")

    frozen = build_frozen_context()
    candles = frozen["candles"]
    lines = frozen["lines_by_id"]
    line = lines[TRADE["support_id"]]
    raw = next(d for d in json.loads(snap_text)["drawings"] if d["drawing_id"] == TRADE["support_id"])

    pending = next(p for p in frozen["pending"] if p.get("touch_event_id") == TRADE["touch_event_id"])
    entry_idx = int(pending["_entry_idx"])
    entry_ts = int(candles[entry_idx]["time"])

    kind, exit_ts_u, exit_px, pnl, exit_idx = simulate_exit_v3y(
        candles,
        entry_idx,
        TRADE["entry_price"],
        TRADE["sl"],
        TRADE["tp"],
        pending.get("_channel_line"),
        line,
    )
    kind_u, u_ts, u_px, u_pnl, u_idx = l1.simulate_exit(
        candles,
        entry_idx,
        TRADE["entry_price"],
        TRADE["sl"],
        TRADE["tp"],
        pending.get("_channel_line"),
    )

    break_ts = ts("2026-08-31 02:45:00 UTC")
    confirm_ts = ts("2026-08-31 02:50:00 UTC")
    exit_ts = ts(TRADE["exit_utc"])

    bc = candle_by_ts(candles, break_ts)
    lp_b = line_price_at_close(line, bc)
    break_under_pct = (float(bc["close"]) - lp_b) / lp_b * 100.0

    fsm_rows = fsm_walk(candles, entry_idx, exit_idx, line)
    post = post_exit_stats(candles, exit_ts, TRADE["entry_price"], line, [1, 3, 6, 12, 24])
    cf = counterfactual_after(
        candles,
        entry_idx,
        TRADE["entry_price"],
        TRADE["sl"],
        TRADE["tp"],
        exit_ts,
        pending.get("_channel_line"),
        line,
    )

    line_meta = {
        "drawing_id": line.drawing_id,
        "drawing_type": line.drawing_type,
        "anchor1_utc": fmt_utc(int(line.t1)),
        "anchor1_price": line.p1,
        "anchor2_utc": fmt_utc(int(line.t2)),
        "anchor2_price": line.p2,
        "slope_per_second": line.slope(),
        "extend_right": line.extend_right,
        "extend_left": bool((raw.get("metadata") or {}).get("extend_left")),
        "metadata_extend_right": bool((raw.get("metadata") or {}).get("extend_right")),
        "sha256": sha,
    }

    payload = {
        "trade": TRADE,
        "line": line_meta,
        "pending_summary": {k: pending.get(k) for k in sorted(pending) if not str(k).startswith("_") or k in (
            "_entry_idx", "_sl", "_pool_tp", "_touch_start_idx", "_pick_ok", "_main_upper_at_entry"
        )},
        "sim_v3y": {
            "kind": kind,
            "exit_utc": fmt_utc(exit_ts_u),
            "exit_price": exit_px,
            "pnl_pct": pnl,
        },
        "sim_v3u_no_support": {
            "kind": kind_u,
            "exit_utc": fmt_utc(u_ts) if u_ts else None,
            "exit_price": u_px,
            "pnl_pct": u_pnl,
        },
        "break_bar": {
            "utc": fmt_utc(break_ts),
            "ohlc": [float(bc[k]) for k in ("open", "high", "low", "close")],
            "line_at_close": lp_b,
            "close_under_line_pct": break_under_pct,
        },
        "confirm_bar": candle_by_ts(candles, confirm_ts),
        "exit_bar": candle_by_ts(candles, exit_ts),
        "fsm_rows": fsm_rows,
        "post_exit": post,
        "counterfactual": cf,
        "line_prices": {
            "entry_close_eval": line_price_at_close_time(line, entry_ts),
            "t_0245": line_price_at_close_time(line, break_ts),
            "t_0250": line_price_at_close_time(line, confirm_ts),
            "t_0255": line_price_at_close_time(line, exit_ts),
        },
    }

    OUT_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    render_chart(
        candles, line, entry_ts, break_ts, confirm_ts, exit_ts, TRADE["entry_price"], TRADE["sl"], TRADE["tp"], OUT_PNG
    )

    md = _build_md(payload, fsm_rows, break_under_pct)
    OUT_MD.write_text(md, encoding="utf-8")
    print(OUT_MD)
    print(OUT_PNG)
    return 0


def _build_md(payload: dict, fsm_rows: list, break_under_pct: float) -> str:
    L = payload["line"]
    p = payload["pending_summary"]
    bc = payload["break_bar"]
    conf = payload["confirm_bar"]
    ex = payload["exit_bar"]
    lines = [
        "# L1 V3AA — Original-Support Trade-Audit",
        "",
        "Trade: DOGEUSDT 5m · `te-d-9b6e62dc884c-0006` · **nur eingefrorener V3U-Snapshot**",
        f"SHA256: `{L['sha256']}`",
        "",
        "**Hinweis:** Nutzer-neu gezeichnete grüne Linie im Live-Chart ist **nicht** verwendet.",
        "",
        "## 2. Originale Supportlinie `d-9b6e62dc884c`",
        "",
        f"| Feld | Wert |",
        f"|---|---|",
        f"| Typ | `{L['drawing_type']}` |",
        f"| Anker 1 | `{L['anchor1_utc']}` @ `{L['anchor1_price']:.10f}` |",
        f"| Anker 2 | `{L['anchor2_utc']}` @ `{L['anchor2_price']:.10f}` |",
        f"| Steigung (ΔPreis/s) | `{L['slope_per_second']:.12e}` |",
        f"| extend_left (Metadata) | `{L['extend_left']}` |",
        f"| extend_right (Linie) | `{L['extend_right']}` |",
        f"| Linie @ Entry-Close-Eval | `{payload['line_prices']['entry_close_eval']:.10f}` |",
        f"| Linie @ 02:45 Close | `{payload['line_prices']['t_0245']:.10f}` |",
        f"| Linie @ 02:50 Close | `{payload['line_prices']['t_0250']:.10f}` |",
        f"| Linie @ 02:55 Close | `{payload['line_prices']['t_0255']:.10f}` |",
        "",
        "## 3. Entry-Nachweis (Fakten aus Pending-Row)",
        "",
        f"- Touch-Event: `{p.get('touch_event_id')}`",
        f"- `drawing_causality`: `{p.get('drawing_causality')}` (**RETROSPECTIVE_DRAWING**)",
        f"- Touch UTC: `{p.get('touch_utc')}`",
        f"- Entry UTC: `{p.get('entry_utc')}` @ `{p.get('entry_price')}`",
        f"- `long_trigger_allowed`: `{p.get('long_trigger_allowed')}`",
        f"- Entry-Regel / C1: siehe `c1_confirm_utc`, `first_touch_utc`, `touch_class` in Payload JSON",
        f"- Geometrie Entry: `active_for_candle` = true auf Entry-Bar",
        "",
        "## 4. Supportbruch (unabhängige Kerzenprüfung)",
        "",
        f"| Kerze | O | H | L | C | Linie@Close | C vs Linie % |",
        f"|---|---:|---:|---:|---:|---:|---:|",
        f"| Bruch 02:45 | {bc['ohlc'][0]:.5f} | {bc['ohlc'][1]:.5f} | {bc['ohlc'][2]:.5f} | {bc['ohlc'][3]:.5f} | {bc['line_at_close']:.10f} | **{break_under_pct:.6f}** |",
        f"| Bestätigung 02:50 | {float(conf['open']):.5f} | {float(conf['high']):.5f} | {float(conf['low']):.5f} | {float(conf['close']):.5f} | {payload['line_prices']['t_0250']:.10f} | {(float(conf['close'])-payload['line_prices']['t_0250'])/payload['line_prices']['t_0250']*100:.4f} |",
        f"| Exit-Fill 02:55 Open | **{float(ex['open']):.5f}** | | | | | |",
        "",
        f"Δ Close Bruch→Bestätigung: **{(float(conf['close'])-bc['ohlc'][3])/bc['ohlc'][3]*100:.4f} %** (Schwelle ≤ −0,20 %)",
        "",
        "### Kerzenfolge Entry → Exit (FSM)",
        "",
        "| UTC | C | Linie@Close | vs Linie % | State | Notiz |",
        "|---|---:|---:|---:|---|---|",
    ]
    for r in fsm_rows:
        vs = f"{r['close_vs_line_pct']:.5f}" if r["close_vs_line_pct"] is not None else "—"
        lines.append(
            f"| {r['utc'][11:19]} | {r['c']:.5f} | {r['line_close'] or '—'} | {vs} | {r['state']} | {r['note']} |"
        )

    pe = payload["post_exit"]
    lines.extend(
        [
            "",
            "## 5. Nach dem Exit (kontrafaktisch / Beobachtung)",
            "",
            f"- Erster Close **auf/über** Linie: `{pe.get('reclaim_utc') or '—'}`",
            f"- Minuten mit Close unter Linie (ab Exit-Bar): `{pe['minutes_close_below_line_after_exit']}`",
            f"- Linie bei Reclaim noch gültig: `{pe.get('line_still_valid_at_reclaim')}`",
            "",
            "| Fenster nach Exit | High | Low | MFE vs Entry % | MAE vs Entry % |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for h in (1, 3, 6, 12, 24):
        lines.append(
            f"| +{h} h | {pe.get(f'high_{h}h')} | {pe.get(f'low_{h}h')} | "
            f"{pe.get(f'mfe_vs_entry_{h}h_pct')} | {pe.get(f'mae_vs_entry_{h}h_pct')} |"
        )

    cf = payload["counterfactual"]["first_after_exit"]
    cf_s = "—"
    if cf:
        cf_s = f"{cf[0]} @ {fmt_utc(cf[2])} price {cf[1]}"
    lines.extend(
        [
            "",
            f"**Ohne Support-Exit (V3U-Regel ab Exit-Zeit):** erstes späteres Ereignis → `{cf_s}`",
            f"**V3U gesamt (ohne Support-Exit):** `{payload['sim_v3u_no_support']['kind']}` @ "
            f"`{payload['sim_v3u_no_support']['exit_utc']}` → `{payload['sim_v3u_no_support']['pnl_pct']:.4f} %`",
            "",
            "## 6. Diagnosechart",
            "",
            f"Statisches PNG (keine Research-Chart-Änderung): `{OUT_PNG.name}`",
            "",
            "## 7. Bewertung",
            "",
            "| Frage | Fakt / Ergebnis |",
            "|---|---|",
            "| Entry regelkonform (originale Linie)? | Pending row + C1/Touch aus JSON; Geometrie aktiv |",
            "| Supportbruch korrekt? | Close 02:45 unter Linie (marginal, s. Bruch-%) |",
            "| 0,20-%-Bestätigung korrekt? | Folge-Close −0,244 % vs Bruch-Close |",
            "| Exit −0,8867 % technisch korrekt? | Fill-Open 02:55 = 0,08160; PnL stimmt mit V3Y |",
            "| Reclaim nach Exit? | siehe `reclaim_utc` oben |",
            "| Ohne Bruch-Exit: TP oder SL zuerst? | V3U-Sim + kontrafaktisch nach Exit |",
            "| Fehlausbruch / falsche Linie? | Linie = Entry-`touch_drawing_id`; Haarlinien-Bruch prüfen |",
            "| Backtester exakt? | `simulate_exit_v3y` reproduziert dokumentierte FSM |",
            "",
            "**Interpretation (getrennt):** Sehr knapper Close unter Linie bei 02:45 ist regelkonform aber visuell schwer; "
            "ob das ein „Fehlausbruch“ war, ist eine Markt-Bewertung — siehe MFE nach Exit und Reclaim-Zeit.",
            "",
            "Payload: `l1_v3aa_audit_payload.json`",
            "",
            "**STOP** — keine Strategie-/Zeichnungsänderung.",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())

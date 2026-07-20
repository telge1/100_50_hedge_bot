"""Generate Pine v6 audit overlay for C3.5D exit-level diagnosis (research-only).

Embeds frozen APT entry snapshots + protected-break post-path metrics as arrays.
Does not compute live C3.5D entries. No exit rules, no D3 runtime, no live bot.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.trend_pine_export import (
    build_pine_header,
    validate_pine_script,
)

PHASE = "C3.5D_EXIT_LEVELS_PINE_AUDIT"
DEFAULT_APT_DIR = Path(
    "research/regime_scanner/results/phase_c3_5d_continuation_early_failure/apt_audit"
)
DEFAULT_OUT = Path(
    "research/regime_scanner/results/phase_c3_5d_continuation_early_failure/pine_exit_levels"
)
MAIN_PINE = "C3_5D_APT_exit_levels_audit.pine"


def _finite(x: Any, default: float = float("nan")) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _bool01(x: Any) -> int:
    try:
        if pd.isna(x):
            return 0
    except (TypeError, ValueError):
        pass
    if isinstance(x, str):
        return 1 if x.strip().lower() in {"1", "true", "yes"} else 0
    return 1 if bool(x) else 0


def _pine_float(x: Any) -> str:
    v = _finite(x)
    if not math.isfinite(v):
        return "na"
    # Pine accepts decimal literals
    return repr(float(v))


def _pine_int(x: Any, default: int = 0) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)) or pd.isna(x):
        return str(default)
    return str(int(x))


def _ts_parts(ts: Any) -> tuple[int, int, int, int, int]:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return int(t.year), int(t.month), int(t.day), int(t.hour), int(t.minute)


def _array_from_floats(name: str, values: Sequence[Any]) -> str:
    body = ", ".join(_pine_float(v) for v in values)
    return f"{name} = array.from({body})"


def _array_from_ints(name: str, values: Sequence[Any]) -> str:
    body = ", ".join(_pine_int(v) for v in values)
    return f"{name} = array.from({body})"


def _array_from_timestamps(name: str, values: Sequence[Any]) -> str:
    parts = []
    for v in values:
        y, m, d, h, mi = _ts_parts(v)
        parts.append(f"timestamp({y}, {m}, {d}, {h}, {mi})")
    return f"{name} = array.from({', '.join(parts)})"


def load_entry_snapshots(apt_dir: Path) -> pd.DataFrame:
    entries = pd.read_csv(apt_dir / "d1_entries_apt.csv")
    fills = pd.read_csv(apt_dir / "fills.csv")
    # Prefer fill timestamps / frozen atr from fills; levels from entries
    keep_e = [
        "setup_id",
        "direction",
        "side",
        "trigger_timestamp",
        "fill_bar",
        "entry_price",
        "frozen_breakout_level",
        "frozen_pullback_high",
        "frozen_pullback_low",
        "setup_protected_level",
        "entry_protected_level",
    ]
    for c in keep_e:
        if c not in entries.columns:
            raise RuntimeError(f"d1_entries_apt.csv missing {c}")
    e = entries[keep_e].drop_duplicates("setup_id")
    f = fills[
        [
            "setup_id",
            "fill_timestamp",
            "frozen_atr_14",
            "entry_price",
            "entry_protected_level",
            "frozen_breakout_level",
            "direction",
            "side",
        ]
    ].drop_duplicates("setup_id")
    # One row per fill setup_id from fills (112); merge entry structure fields
    m = f.merge(
        e[
            [
                "setup_id",
                "trigger_timestamp",
                "frozen_pullback_high",
                "frozen_pullback_low",
                "setup_protected_level",
            ]
        ],
        on="setup_id",
        how="left",
    )
    m = m.sort_values("setup_id").reset_index(drop=True)
    return m


def attach_protected_break_metrics(snaps: pd.DataFrame, break_path: Path) -> pd.DataFrame:
    if not break_path.exists():
        raise RuntimeError(f"missing protected-break audit: {break_path}")
    pb = pd.read_csv(break_path)
    cols = [
        "setup_id",
        "protected_break_bar",
        "protected_break_timestamp",
        "signed_return_pct_at_break",
        "h24__add_adverse_pct",
        "h48__add_adverse_pct",
        "h96__add_adverse_pct",
        "full__add_adverse_pct",
        "h24__prot_wick_retest",
        "h24__prot_close_reclaim",
        "h24__bars_to_prot_wick_retest",
        "h24__bars_to_prot_close_reclaim",
        "h24__entry_close_recovery",
        "h48__entry_close_recovery",
        "h96__entry_close_recovery",
        "full__entry_close_recovery",
        "full__bars_to_prot_close_reclaim",
        "full__prot_wick_retest",
        "full__prot_close_reclaim",
        "full__bars_to_prot_wick_retest",
        "post24__prot_close_reclaim",
        "post24__bars_to_prot_close_reclaim",
        "post24__entry_close_recovery",
    ]
    have = [c for c in cols if c in pb.columns]
    out = snaps.merge(pb[have], on="setup_id", how="left")
    return out


def build_exit_levels_pine(df: pd.DataFrame, *, title: str | None = None) -> str:
    n = len(df)
    if n == 0:
        raise RuntimeError("no entry snapshots")

    sides = [int(x) for x in df["side"].tolist()]
    fill_ts = df["fill_timestamp"].tolist()
    # optional break timestamps: use fill if missing
    break_ts = []
    for _, r in df.iterrows():
        if pd.notna(r.get("protected_break_timestamp")) and str(r.get("protected_break_timestamp")).strip():
            break_ts.append(r["protected_break_timestamp"])
        else:
            break_ts.append(r["fill_timestamp"])

    has_break = [
        1
        if pd.notna(r.get("signed_return_pct_at_break"))
        and math.isfinite(_finite(r.get("signed_return_pct_at_break")))
        else 0
        for _, r in df.iterrows()
    ]

    lines: list[str] = [
        *build_pine_header(title or "C3.5D APT Exit Levels Audit"),
        "// RESEARCH ONLY — generated audit overlay (hardcoded APT snapshots).",
        "// SoT entries: apt_audit/d1_entries_apt.csv + fills.csv",
        "// Protected post-break: apt_audit/protected_break_path/",
        "// No live C3.5D SM. No exit orders. No D3 runtime.",
        "// Protected reclaim != entry recovery (markers are separate).",
        "// Horizons h24/h48/h96 vs full are separate flags — do not mix.",
        "",
        f"nSetups = {n}",
        'maxVisible = input.int(12, "Max visible trades", minval=1, maxval=112)',
        'lineHorizonBars = input.int(96, "Line length (bars)", minval=8, maxval=500)',
        'showEntry = input.bool(true, "Show entry line")',
        'showEntryMarkers = input.bool(true, "Show entry markers at fill bar")',
        'showEntryVLine = input.bool(true, "Show vertical line at fill bar")',
        'showBreakout = input.bool(true, "Show breakout (warning)")',
        'showPullback = input.bool(true, "Show pullback extreme")',
        'showProtected = input.bool(true, "Show entry protected")',
        'showSetupProtected = input.bool(false, "Show setup protected (dashed)")',
        'showProtBreakMarkers = input.bool(true, "Show protected-break markers")',
        'showProtReclaimMarkers = input.bool(true, "Show protected reclaim (not full recovery)")',
        'showEntryRecoveryMarkers = input.bool(true, "Show entry-recovery markers")',
        'showBreakLabels = input.bool(true, "Show break loss / add-adverse labels")',
        'confirmOnBarClose = input.bool(true, "Confirm markers on bar close")',
        "",
        "// --- embedded snapshots ---",
        _array_from_ints("setupIds", df["setup_id"].tolist()),
        _array_from_ints("sides", sides),
        _array_from_timestamps("fillTimes", fill_ts),
        _array_from_floats("entryPx", df["entry_price"].tolist()),
        _array_from_floats("breakoutLv", df["frozen_breakout_level"].tolist()),
        _array_from_floats("pbHigh", df["frozen_pullback_high"].tolist()),
        _array_from_floats("pbLow", df["frozen_pullback_low"].tolist()),
        _array_from_floats("setupProt", df["setup_protected_level"].tolist()),
        _array_from_floats("entryProt", df["entry_protected_level"].tolist()),
        _array_from_ints("hasProtBreak", has_break),
        _array_from_timestamps("protBreakTimes", break_ts),
        _array_from_floats("lossAtBreakPct", df.get("signed_return_pct_at_break", pd.Series([float("nan")] * n)).tolist()),
        _array_from_floats("addAdvH24Pct", df.get("h24__add_adverse_pct", pd.Series([float("nan")] * n)).tolist()),
        _array_from_floats("addAdvFullPct", df.get("full__add_adverse_pct", pd.Series([float("nan")] * n)).tolist()),
        _array_from_ints(
            "h24WickRetest",
            [_bool01(x) for x in df.get("h24__prot_wick_retest", pd.Series([0] * n)).tolist()],
        ),
        _array_from_ints(
            "h24CloseReclaim",
            [_bool01(x) for x in df.get("h24__prot_close_reclaim", pd.Series([0] * n)).tolist()],
        ),
        _array_from_ints(
            "barsToReclaimH24",
            df.get("h24__bars_to_prot_close_reclaim", pd.Series([0] * n)).fillna(0).astype(int).tolist(),
        ),
        _array_from_ints(
            "barsToReclaimFull",
            df.get("full__bars_to_prot_close_reclaim", pd.Series([0] * n)).fillna(0).astype(int).tolist(),
        ),
        _array_from_ints(
            "entryRecH24",
            [_bool01(x) for x in df.get("h24__entry_close_recovery", pd.Series([0] * n)).tolist()],
        ),
        _array_from_ints(
            "entryRecH48",
            [_bool01(x) for x in df.get("h48__entry_close_recovery", pd.Series([0] * n)).tolist()],
        ),
        _array_from_ints(
            "entryRecH96",
            [_bool01(x) for x in df.get("h96__entry_close_recovery", pd.Series([0] * n)).tolist()],
        ),
        _array_from_ints(
            "entryRecFull",
            [_bool01(x) for x in df.get("full__entry_close_recovery", pd.Series([0] * n)).tolist()],
        ),
        _array_from_ints(
            "barsToWickRetestH24",
            df.get("h24__bars_to_prot_wick_retest", pd.Series([0] * n)).fillna(0).astype(int).tolist(),
        ),
        "",
        "pullbackLevel(i) =>",
        "    array.get(sides, i) > 0 ? array.get(pbLow, i) : array.get(pbHigh, i)",
        "",
        "var line[] entryLines = array.new_line()",
        "var line[] entryVLines = array.new_line()",
        "var line[] brkLines = array.new_line()",
        "var line[] pbLines = array.new_line()",
        "var line[] protLines = array.new_line()",
        "var line[] setupProtLines = array.new_line()",
        "var label[] entryMarkers = array.new_label()",
        "var label[] eventLabels = array.new_label()",
        "var bool drawn = false",
        "",
        "clearAll() =>",
        "    if array.size(entryLines) > 0",
        "        for j = 0 to array.size(entryLines) - 1",
        "            line.delete(array.get(entryLines, j))",
        "        array.clear(entryLines)",
        "    if array.size(entryVLines) > 0",
        "        for j = 0 to array.size(entryVLines) - 1",
        "            line.delete(array.get(entryVLines, j))",
        "        array.clear(entryVLines)",
        "    if array.size(brkLines) > 0",
        "        for j = 0 to array.size(brkLines) - 1",
        "            line.delete(array.get(brkLines, j))",
        "        array.clear(brkLines)",
        "    if array.size(pbLines) > 0",
        "        for j = 0 to array.size(pbLines) - 1",
        "            line.delete(array.get(pbLines, j))",
        "        array.clear(pbLines)",
        "    if array.size(protLines) > 0",
        "        for j = 0 to array.size(protLines) - 1",
        "            line.delete(array.get(protLines, j))",
        "        array.clear(protLines)",
        "    if array.size(setupProtLines) > 0",
        "        for j = 0 to array.size(setupProtLines) - 1",
        "            line.delete(array.get(setupProtLines, j))",
        "        array.clear(setupProtLines)",
        "    if array.size(entryMarkers) > 0",
        "        for j = 0 to array.size(entryMarkers) - 1",
        "            label.delete(array.get(entryMarkers, j))",
        "        array.clear(entryMarkers)",
        "    if array.size(eventLabels) > 0",
        "        for j = 0 to array.size(eventLabels) - 1",
        "            label.delete(array.get(eventLabels, j))",
        "        array.clear(eventLabels)",
        "",
        "barOffsetMs = timeframe.in_seconds() * 1000",
        "",
        "drawSetup(i) =>",
        "    t0 = array.get(fillTimes, i)",
        "    t1 = t0 + lineHorizonBars * barOffsetMs",
        "    side = array.get(sides, i)",
        "    ep = array.get(entryPx, i)",
        "    bl = array.get(breakoutLv, i)",
        "    pbl = pullbackLevel(i)",
        "    pr = array.get(entryProt, i)",
        "    spr = array.get(setupProt, i)",
        "    longSide = side > 0",
        "    if showEntry and not na(ep)",
        "        array.push(entryLines, line.new(t0, ep, t1, ep, xloc=xloc.bar_time, extend=extend.none, color=color.new(color.gray, 0), width=1, style=line.style_solid))",
        "    // Exact fill-bar entry marker (not later touches of the gray entry line).",
        "    if showEntryMarkers and not na(ep)",
        "        entryTxt = longSide ? 'LONG ENTRY' : 'SHORT ENTRY'",
        "        entryStyle = longSide ? label.style_label_up : label.style_label_down",
        "        entryCol = longSide ? color.new(color.teal, 0) : color.new(color.fuchsia, 0)",
        "        array.push(entryMarkers, label.new(t0, ep, entryTxt, xloc=xloc.bar_time, style=entryStyle, color=entryCol, textcolor=color.white, size=size.small))",
        "    if showEntryVLine and not na(ep)",
        "        yPad = math.max(math.abs(ep) * 0.004, syminfo.mintick * 4)",
        "        array.push(entryVLines, line.new(t0, ep - yPad, t0, ep + yPad, xloc=xloc.bar_time, extend=extend.none, color=color.new(color.aqua, 30), width=1, style=line.style_dashed))",
        "    if showBreakout and not na(bl)",
        "        array.push(brkLines, line.new(t0, bl, t1, bl, xloc=xloc.bar_time, extend=extend.none, color=color.new(color.yellow, 0), width=1, style=line.style_dashed))",
        "    if showPullback and not na(pbl)",
        "        array.push(pbLines, line.new(t0, pbl, t1, pbl, xloc=xloc.bar_time, extend=extend.none, color=color.new(color.orange, 0), width=2, style=line.style_solid))",
        "    // Protected line keeps running after break (same end t1) so reclaim is visible.",
        "    if showProtected and not na(pr)",
        "        array.push(protLines, line.new(t0, pr, t1, pr, xloc=xloc.bar_time, extend=extend.none, color=color.new(color.red, 0), width=2, style=line.style_solid))",
        "    if showSetupProtected and not na(spr)",
        "        array.push(setupProtLines, line.new(t0, spr, t1, spr, xloc=xloc.bar_time, extend=extend.none, color=color.new(color.maroon, 40), width=1, style=line.style_dotted))",
        "    if showProtBreakMarkers and array.get(hasProtBreak, i) == 1",
        "        bt = array.get(protBreakTimes, i)",
        "        lossPct = array.get(lossAtBreakPct, i)",
        "        addH24 = array.get(addAdvH24Pct, i)",
        "        yBreak = longSide ? pr * 0.998 : pr * 1.002",
        "        if showBreakLabels and not na(lossPct)",
        "            txt = 'PROT BREAK close\\nloss ' + str.tostring(lossPct, '#.##') + '%\\nadd_adv h24 ' + str.tostring(addH24, '#.##') + '%\\n(NOT entry recovery)'",
        "            array.push(eventLabels, label.new(bt, yBreak, txt, xloc=xloc.bar_time, style=longSide ? label.style_label_down : label.style_label_up, color=color.new(color.red, 20), textcolor=color.white, size=size.small))",
        "        // First protected wick-retest (h24 flag); marker only — not full recovery.",
        "        if showProtReclaimMarkers and array.get(h24WickRetest, i) == 1",
        "            tw = bt + array.get(barsToWickRetestH24, i) * barOffsetMs",
        "            array.push(eventLabels, label.new(tw, pr, 'PROT wick retest\\n(not entry rec)', xloc=xloc.bar_time, style=label.style_xcross, color=color.new(color.orange, 0), textcolor=color.black, size=size.tiny))",
        "        // First protected close-reclaim — explicitly NOT full recovery.",
        "        if showProtReclaimMarkers and array.get(h24CloseReclaim, i) == 1",
        "            tr = bt + array.get(barsToReclaimH24, i) * barOffsetMs",
        "            array.push(eventLabels, label.new(tr, pr, 'PROT close reclaim\\nbars=' + str.tostring(array.get(barsToReclaimH24, i)) + '\\n≠ entry recovery', xloc=xloc.bar_time, style=label.style_diamond, color=color.new(color.olive, 0), textcolor=color.white, size=size.tiny))",
        "        // Entry recovery markers — separate from protected reclaim; per-horizon flags.",
        "        if showEntryRecoveryMarkers",
        "            er24 = array.get(entryRecH24, i)",
        "            er48 = array.get(entryRecH48, i)",
        "            er96 = array.get(entryRecH96, i)",
        "            erFull = array.get(entryRecFull, i)",
        "            if er24 == 1 or er48 == 1 or er96 == 1 or erFull == 1",
        "                etxt = 'ENTRY recovery flags\\nh24=' + str.tostring(er24) + ' h48=' + str.tostring(er48) + ' h96=' + str.tostring(er96) + ' full=' + str.tostring(erFull)",
        "                array.push(eventLabels, label.new(t0 + 3 * barOffsetMs, ep, etxt, xloc=xloc.bar_time, style=label.style_label_left, color=color.new(color.teal, 10), textcolor=color.white, size=size.tiny))",
        "",
        "if barstate.islastconfirmedhistory or (not confirmOnBarClose and barstate.islast)",
        "    if not drawn",
        "        clearAll()",
        "        startIdx = math.max(0, nSetups - maxVisible)",
        "        if startIdx <= nSetups - 1",
        "            for i = startIdx to nSetups - 1",
        "                drawSetup(i)",
        "        drawn := true",
        "",
        "// Data window: last setup summary",
        "plot(array.get(entryPx, nSetups - 1), 'last_entry_px', display=display.data_window)",
        "plot(array.get(entryProt, nSetups - 1), 'last_entry_prot', display=display.data_window)",
        "plot(array.get(lossAtBreakPct, nSetups - 1), 'last_loss_at_prot_break_pct', display=display.data_window)",
        "plot(array.get(addAdvH24Pct, nSetups - 1), 'last_add_adv_h24_pct', display=display.data_window)",
        "plot(array.get(entryRecH24, nSetups - 1), 'last_entry_rec_h24', display=display.data_window)",
        "plot(array.get(entryRecFull, nSetups - 1), 'last_entry_rec_full', display=display.data_window)",
        "",
    ]
    text = "\n".join(lines) + "\n"
    validate_pine_script(text)
    return text


def export_snapshots_csv(df: pd.DataFrame, path: Path) -> None:
    cols = [
        "setup_id",
        "direction",
        "side",
        "fill_timestamp",
        "trigger_timestamp",
        "entry_price",
        "frozen_breakout_level",
        "frozen_pullback_high",
        "frozen_pullback_low",
        "setup_protected_level",
        "entry_protected_level",
        "signed_return_pct_at_break",
        "protected_break_timestamp",
        "h24__add_adverse_pct",
        "h24__prot_wick_retest",
        "h24__prot_close_reclaim",
        "h24__bars_to_prot_close_reclaim",
        "h24__entry_close_recovery",
        "h48__entry_close_recovery",
        "h96__entry_close_recovery",
        "full__entry_close_recovery",
        "full__add_adverse_pct",
        "full__bars_to_prot_close_reclaim",
    ]
    use = [c for c in cols if c in df.columns]
    df[use].to_csv(path, index=False)


def run_export(
    *,
    apt_dir: Path = DEFAULT_APT_DIR,
    output_dir: Path = DEFAULT_OUT,
) -> dict[str, Any]:
    apt_dir = Path(apt_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    snaps = load_entry_snapshots(apt_dir)
    break_csv = apt_dir / "protected_break_path" / "protected_break_path_per_fill.csv"
    snaps = attach_protected_break_metrics(snaps, break_csv)

    pine = build_exit_levels_pine(snaps)
    pine_path = output_dir / MAIN_PINE
    pine_path.write_text(pine, encoding="utf-8")

    snap_path = output_dir / "entry_level_snapshots.csv"
    export_snapshots_csv(snaps, snap_path)

    # Break-event parity table from protected-break reclaim events if present
    ev_src = apt_dir / "protected_break_path" / "protected_break_reclaim_events.csv"
    if ev_src.exists():
        ev = pd.read_csv(ev_src)
        ev.to_csv(output_dir / "break_event_parity.csv", index=False)
    else:
        pd.DataFrame().to_csv(output_dir / "break_event_parity.csv", index=False)

    meta = {
        "phase": PHASE,
        "n_setups": int(len(snaps)),
        "n_with_protected_break": int(
            snaps["signed_return_pct_at_break"].notna().sum()
            if "signed_return_pct_at_break" in snaps.columns
            else 0
        ),
        "pine_path": str(pine_path),
        "entry_level_snapshots": str(snap_path),
        "notes": [
            "Entry is exactly at fillTimes[i] / entryPx[i]; LONG/SHORT ENTRY markers mark that bar only",
            "Protected reclaim markers are labeled ≠ entry recovery",
            "Entry recovery shown as separate per-horizon flags (h24/h48/h96/full)",
            "Protected line extends full lineHorizonBars after fill (survives break)",
            "No exit orders / no D3 runtime / no live bot",
        ],
        "no_exit_rule": True,
        "no_d3_runtime": True,
        "no_live_bot": True,
        "no_commit": True,
    }
    (output_dir / "pine_export_summary.json").write_text(
        json.dumps(json_safe(meta), indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "README.md").write_text(
        "\n".join(
            [
                "# C3.5D APT Exit Levels Pine Audit",
                "",
                "Generated overlay with frozen entry levels + protected-break diagnostics.",
                "",
                f"- Pine: `{MAIN_PINE}`",
                f"- Setups: `{meta['n_setups']}`",
                f"- With protected break: `{meta['n_with_protected_break']}`",
                "",
                "Protected close-reclaim ≠ entry recovery. Horizons are not mixed.",
                "",
                "No exit rule. No D3 runtime. No live bot.",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return meta


def main() -> None:
    p = argparse.ArgumentParser(description="Generate C3.5D exit-levels audit Pine")
    p.add_argument("--apt-dir", type=Path, default=DEFAULT_APT_DIR)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = p.parse_args()
    meta = run_export(apt_dir=args.apt_dir, output_dir=args.output_dir)
    print(json.dumps(json_safe(meta), indent=2))


if __name__ == "__main__":
    main()

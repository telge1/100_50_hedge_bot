#!/usr/bin/env python3
"""Read-only printer: 1h/4h Protected Low/High timestamps for chart checks.

Reads only existing artefacts under results/trend_scanner_multitimeframe_structure/.
Does not run the scanner, recompute levels, or write files.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT_DIR = ROOT / "results" / "trend_scanner_multitimeframe_structure"
TZ_TZ = timezone(timedelta(hours=3))  # Tanzania / East Africa Time
SYMBOLS = ("APTUSDT", "DOGEUSDT", "BTCUSDT")

# Canonical sources (documented in printed header):
# - Level confirmation / origin / state: structure_states_{1h,4h}.parquet
# - Break rising edges: protected_{low,high}_break_events.csv
# - Inventory CSVs are summary-only (first_seen); not used as primary confirmation SoT


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return None
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        # pd.NaT is a datetime subclass in some pandas builds
        if type(value).__name__ == "NaTType" or str(value) == "NaT":
            return None
        ts = value
    else:
        try:
            ts = pd.Timestamp(value)
        except (TypeError, ValueError):
            return None
        if pd.isna(ts):
            return None
        ts = ts.to_pydatetime()
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _fmt_utc(ts: Any) -> str:
    ts = _parse_ts(ts)
    if ts is None:
        return ""
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _fmt_tz(ts: Any) -> str:
    ts = _parse_ts(ts)
    if ts is None:
        return ""
    return ts.astimezone(TZ_TZ).strftime("%Y-%m-%d %H:%M EAT")


def _round_level(x: float) -> float:
    return round(float(x), 8)


def extract_levels_from_structure(df: pd.DataFrame, *, level_col: str) -> pd.DataFrame:
    """First causal appearance of each distinct protected level value per symbol.

    Confirmation bar = first row where protected_* equals that value (level known
    at that row's available_at). Origin from protected_*_time / *_origin_ts when set.
    """
    need = ["symbol", "available_at", level_col]
    for c in need:
        if c not in df.columns:
            raise ValueError(f"structure missing {c}")
    work = df.copy()
    work["available_at"] = pd.to_datetime(work["available_at"], utc=True)
    work = work.sort_values(["symbol", "available_at"], kind="mergesort")
    work = work[work[level_col].notna()].copy()
    work["_lvl"] = work[level_col].map(_round_level)

    origin_col = (
        "protected_low_origin_ts"
        if level_col == "protected_low"
        else "protected_high_origin_ts"
    )
    time_col = (
        "protected_low_time" if level_col == "protected_low" else "protected_high_time"
    )
    conf_col = (
        "protected_low_confirmed_at"
        if level_col == "protected_low"
        else "protected_high_confirmed_at"
    )

    rows: list[dict[str, Any]] = []
    for (symbol, lvl), g in work.groupby(["symbol", "_lvl"], sort=False):
        first = g.iloc[0]
        last = g.iloc[-1]
        origin = None
        if origin_col in g.columns and pd.notna(first.get(origin_col)):
            origin = _parse_ts(first[origin_col])
        elif time_col in g.columns and pd.notna(first.get(time_col)):
            origin = _parse_ts(first[time_col])
        # Causal know-time = structure row available_at (= candle close), never SoT swing stamp.
        confirmed = _parse_ts(first["available_at"])
        sot_confirmed = None
        if conf_col in g.columns and pd.notna(first.get(conf_col)):
            sot_confirmed = _parse_ts(first[conf_col])
        active_until = _parse_ts(last["available_at"])
        # Find first bar after first where level changes away (invalidation approx)
        invalidated = False
        invalid_at = None
        # Within same symbol series: after last appearance, next row has different level
        sym = work[work["symbol"] == symbol]
        after = sym[sym["available_at"] > last["available_at"]]
        if not after.empty:
            nxt = after.iloc[0]
            if pd.isna(nxt[level_col]) or _round_level(nxt[level_col]) != float(lvl):
                invalidated = True
                invalid_at = _parse_ts(nxt["available_at"])

        state = None
        if "trend_state" in first.index:
            state = first["trend_state"]
        elif "protected_structure_state" in first.index:
            state = first["protected_structure_state"]

        seg = first["trend_segment_id"] if "trend_segment_id" in first.index else None

        rows.append(
            {
                "symbol": str(symbol),
                "level_price": float(lvl),
                "level_origin_ts_utc": origin,
                "level_confirmed_at_utc": confirmed,
                "sot_protected_confirmed_at_utc": sot_confirmed,
                "trend_segment_id": seg,
                "scanner_state_at_confirmation": state,
                "protected_level_active_until": active_until,
                "level_invalidated": invalidated,
                "level_invalidated_at_utc": invalid_at,
                "n_bars_at_level": int(len(g)),
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values(
        ["symbol", "level_confirmed_at_utc"], kind="mergesort"
    ).reset_index(drop=True)


def load_breaks(path: Path, *, timeframe: str, side: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df = df[df["timeframe"].astype(str) == timeframe].copy()
    df["available_at"] = pd.to_datetime(df["available_at"], utc=True)
    df["candle_open_ts"] = pd.to_datetime(df["candle_open_ts"], utc=True)
    df["_lvl"] = df["level"].map(_round_level)
    df["_side"] = side
    return df


def attach_first_break(
    levels: pd.DataFrame,
    breaks: pd.DataFrame,
    *,
    break_type: str,
) -> pd.DataFrame:
    if levels.empty:
        return levels
    out_rows: list[dict[str, Any]] = []
    for _, lv in levels.iterrows():
        row = dict(lv)
        conf = lv["level_confirmed_at_utc"]
        sym = lv["symbol"]
        lvl = _round_level(lv["level_price"])
        cand = breaks[
            (breaks["symbol"] == sym)
            & (breaks["_lvl"] == lvl)
            & (breaks["available_at"] >= pd.Timestamp(conf))
        ].sort_values("available_at")
        if cand.empty:
            # also try match by level only after confirm (float tolerance via round)
            row.update(
                {
                    "break_exists": False,
                    "break_type": None,
                    "break_candle_open_utc": None,
                    "break_known_at_utc": None,
                    "break_close": None,
                    "external_bos": None,
                    "choch": None,
                    "break_trend_segment_id": None,
                }
            )
        else:
            b = cand.iloc[0]
            row.update(
                {
                    "break_exists": True,
                    "break_type": break_type,
                    "break_candle_open_utc": _parse_ts(b["candle_open_ts"]),
                    "break_known_at_utc": _parse_ts(b["available_at"]),
                    "break_close": float(b["close"]) if pd.notna(b["close"]) else None,
                    "external_bos": None,  # not stored on break CSV; leave unavailable
                    "choch": bool(b["choch"]) if "choch" in b.index else None,
                    "break_trend_segment_id": b.get("trend_segment_id"),
                }
            )
        out_rows.append(row)
    return pd.DataFrame(out_rows)


def select_1h_examples(levels: pd.DataFrame, n: int = 3) -> pd.DataFrame:
    """Early / mid / late by confirmation time; prefer mix of broken / unbroken."""
    if levels.empty:
        return levels
    parts: list[pd.DataFrame] = []
    for _sym, g in levels.groupby("symbol", sort=False):
        g = g.sort_values("level_confirmed_at_utc").reset_index(drop=True)
        if len(g) <= n:
            parts.append(g)
            continue
        idxs = [0, len(g) // 2, len(g) - 1]
        picked = g.iloc[sorted(set(idxs))].copy()
        with_b = g[g["break_exists"] == True]  # noqa: E712
        without = g[g["break_exists"] == False]  # noqa: E712

        def _replace_slot(frame: pd.DataFrame, slot: int, donor: pd.DataFrame) -> pd.DataFrame:
            if donor.empty:
                return frame
            # avoid duplicate level_price already in frame
            have = set(frame["level_price"].map(_round_level))
            for _, row in donor.iterrows():
                if _round_level(row["level_price"]) in have:
                    continue
                out = frame.copy()
                out.iloc[slot] = row
                return out.reset_index(drop=True)
            return frame

        if not with_b.empty and not bool(picked["break_exists"].any()):
            picked = _replace_slot(picked, 1, with_b)  # replace mid
        if not without.empty and bool(picked["break_exists"].all()):
            picked = _replace_slot(picked, 1, without)
        parts.append(picked.sort_values("level_confirmed_at_utc").reset_index(drop=True))
    return pd.concat(parts, ignore_index=True) if parts else levels.iloc[0:0]


def select_4h_levels(
    levels: pd.DataFrame,
    breaks: pd.DataFrame,
    *,
    break_type: str,
) -> pd.DataFrame:
    """All levels that have a 4h break, plus early/mid/late fillers if needed."""
    if levels.empty:
        return levels
    broken = levels[levels["break_exists"] == True].copy()  # noqa: E712
    parts: list[pd.DataFrame] = []
    for sym, g in levels.groupby("symbol", sort=False):
        g = g.sort_values("level_confirmed_at_utc").reset_index(drop=True)
        br = broken[broken["symbol"] == sym]
        # All broken levels for this symbol
        selected = br.copy()
        # Ensure ≥3 total with early/mid/late if few breaks
        if len(selected) < 3:
            idxs = [0, len(g) // 2, len(g) - 1] if len(g) else []
            filler = g.iloc[sorted(set(i for i in idxs if i < len(g)))]
            selected = (
                pd.concat([selected, filler], ignore_index=True)
                .drop_duplicates(subset=["symbol", "level_price", "level_confirmed_at_utc"])
                .sort_values("level_confirmed_at_utc")
            )
        # Also: if some break events don't match inventory levels (float), add from breaks
        sym_breaks = breaks[breaks["symbol"] == sym].sort_values("available_at")
        missing_rows: list[dict[str, Any]] = []
        have = {
            (_round_level(r.level_price), pd.Timestamp(r.break_known_at_utc))
            for r in selected.itertuples()
            if r.break_exists
        }
        for _, b in sym_breaks.iterrows():
            key = (_round_level(b["_lvl"]), pd.Timestamp(b["available_at"]))
            # already represented if any selected row has same level+break known
            matched = False
            for r in selected.itertuples():
                if (
                    r.break_exists
                    and _round_level(r.level_price) == key[0]
                    and pd.Timestamp(r.break_known_at_utc) == key[1]
                ):
                    matched = True
                    break
            if matched:
                continue
            # create break-centric row
            missing_rows.append(
                {
                    "symbol": sym,
                    "level_price": float(b["_lvl"]),
                    "level_origin_ts_utc": None,
                    "level_confirmed_at_utc": None,
                    "trend_segment_id": b.get("trend_segment_id"),
                    "scanner_state_at_confirmation": None,
                    "protected_level_active_until": None,
                    "level_invalidated": None,
                    "level_invalidated_at_utc": None,
                    "n_bars_at_level": None,
                    "break_exists": True,
                    "break_type": break_type,
                    "break_candle_open_utc": _parse_ts(b["candle_open_ts"]),
                    "break_known_at_utc": _parse_ts(b["available_at"]),
                    "break_close": float(b["close"]) if pd.notna(b["close"]) else None,
                    "external_bos": None,
                    "choch": bool(b["choch"]) if "choch" in b.index else None,
                    "break_trend_segment_id": b.get("trend_segment_id"),
                    "_from_break_only": True,
                }
            )
        if missing_rows:
            selected = pd.concat(
                [selected, pd.DataFrame(missing_rows)], ignore_index=True
            )
        selected = selected.sort_values(
            ["break_exists", "break_known_at_utc", "level_confirmed_at_utc"],
            ascending=[False, True, True],
            kind="mergesort",
        )
        parts.append(selected)
    return pd.concat(parts, ignore_index=True) if parts else levels.iloc[0:0]


def _table_row(r: pd.Series, *, timeframe: str) -> dict[str, str]:
    origin = r.get("level_origin_ts_utc")
    known = r.get("level_confirmed_at_utc")
    tz_show = known if _parse_ts(known) is not None else origin
    br_open = r.get("break_candle_open_utc")
    br_known = r.get("break_known_at_utc")
    return {
        "Symbol": r["symbol"],
        "TF": timeframe,
        "Level": f"{float(r['level_price']):.8g}",
        "Origin UTC": _fmt_utc(origin) or "n/a",
        "Known UTC": _fmt_utc(known) or "n/a",
        "Tanzania": _fmt_tz(tz_show) or "n/a",
        "Break?": "yes" if bool(r.get("break_exists")) else "no",
        "Break Open UTC": _fmt_utc(br_open),
        "Break Known UTC": _fmt_utc(br_known),
    }


def print_markdown_table(rows: list[dict[str, str]], title: str) -> None:
    print(f"\n## {title}\n")
    if not rows:
        print("_keine Einträge_\n")
        return
    cols = list(rows[0].keys())
    print("| " + " | ".join(cols) + " |")
    print("| " + " | ".join("---" if c != "Level" else "---:" for c in cols) + " |")
    for r in rows:
        print("| " + " | ".join(str(r[c]) for c in cols) + " |")
    print()


def print_chart_hint(kind: str) -> None:
    hints = {
        "1h_low": (
            "In TradingView die Origin-UTC-Candle (1h Open) suchen; Level ist erst ab Known UTC "
            "(Candle-Close) kausal bestätigt. Break auf Break-Open prüfen, Close unter dem Level "
            "erst ab Break Known UTC werten."
        ),
        "1h_high": (
            "1h Protected High an Origin-Open markieren; Bestätigung erst am Known-UTC-Close. "
            "Break = Close über dem Level ab Break Known UTC. Danach Reclaim unter Level vs. "
            "Fortsetzung nach oben prüfen."
        ),
        "4h_low": (
            "4h-Blöcke sind UTC 00/04/08/12/16/20. Level erst am 4h-Close (Known UTC) gültig. "
            "Break-Open ist die 4h-Candle, die unter dem PL schließt; bekannt ab Break Known UTC."
        ),
        "4h_high": (
            "4h Protected High erst nach Block-Close nutzen. Break-Open = 4h-Candle über PH; "
            "kein Intrabar-Claim vor Break Known UTC. Reclaim zurück unter PH vs. Breakout-Hold prüfen."
        ),
    }
    print(f"_{hints[kind]}_\n")


def print_detail_block(
    *,
    symbol: str,
    timeframe: str,
    level_type: str,
    frame: pd.DataFrame,
    source_structure: str,
    source_breaks: str,
) -> None:
    print(f"### {timeframe} {level_type.replace('_', ' ').title()}s\n")
    sub = frame[frame["symbol"] == symbol]
    if sub.empty:
        print("_keine_\n")
        return
    for _, r in sub.iterrows():
        origin = r.get("level_origin_ts_utc")
        known = r.get("level_confirmed_at_utc")
        br_open = r.get("break_candle_open_utc")
        br_known = r.get("break_known_at_utc")
        active_until = r.get("protected_level_active_until")
        inv_at = r.get("level_invalidated_at_utc")
        print(f"- **level_price** = `{float(r['level_price']):.8g}`")
        print(f"  - symbol / timeframe / level_type: `{symbol}` / `{timeframe}` / `{level_type}`")
        print(f"  - level_origin_ts_utc: `{_fmt_utc(origin) or 'n/a'}`")
        print(f"  - level_origin_ts_tanzania: `{_fmt_tz(origin) or 'n/a'}`")
        print(f"  - level_confirmed_at_utc / available_at: `{_fmt_utc(known) or 'n/a'}`")
        print(f"  - level_confirmed_at_tanzania: `{_fmt_tz(known) or 'n/a'}`")
        print(f"  - trend_segment_id: `{r.get('trend_segment_id')}`")
        print(f"  - scanner_state_at_confirmation: `{r.get('scanner_state_at_confirmation')}`")
        print(
            f"  - protected_level_active_until: "
            f"`{_fmt_utc(active_until) if active_until is not None else 'n/a'}`"
        )
        inv = r.get("level_invalidated")
        print(
            f"  - level_invalidated: `{inv if inv is not None else 'n/a'}`"
            + (f" @ `{_fmt_utc(inv_at)}`" if inv_at is not None else "")
        )
        print(f"  - source_file (level): `{source_structure}`")
        if bool(r.get("break_exists")):
            print(f"  - break_exists: `true`")
            print(f"  - break_type: `{r.get('break_type')}`")
            print(f"  - break_candle_open_utc: `{_fmt_utc(br_open)}`")
            print(f"  - break_candle_open_tanzania: `{_fmt_tz(br_open)}`")
            print(f"  - break_known_at_utc / break_available_at: `{_fmt_utc(br_known)}`")
            print(f"  - break_known_at_tanzania: `{_fmt_tz(br_known)}`")
            print(f"  - break_close: `{r.get('break_close')}`")
            print(f"  - choch: `{r.get('choch')}`")
            print(f"  - external_bos: `n/a (not on break CSV)`")
            print(f"  - source_file (break): `{source_breaks}`")
        else:
            print(f"  - break_exists: `false`")
        print()


def build_catalog(artifact_dir: Path) -> dict[str, Any]:
    s1h = pd.read_parquet(artifact_dir / "structure_states_1h.parquet")
    s4h = pd.read_parquet(artifact_dir / "structure_states_4h.parquet")
    s1h = s1h[s1h["symbol"].isin(SYMBOLS)]
    s4h = s4h[s4h["symbol"].isin(SYMBOLS)]

    pl_br = load_breaks(
        artifact_dir / "protected_low_break_events.csv", timeframe="1h", side="low"
    )
    ph_br = load_breaks(
        artifact_dir / "protected_high_break_events.csv", timeframe="1h", side="high"
    )
    pl_br4 = load_breaks(
        artifact_dir / "protected_low_break_events.csv", timeframe="4h", side="low"
    )
    ph_br4 = load_breaks(
        artifact_dir / "protected_high_break_events.csv", timeframe="4h", side="high"
    )

    pl_1h = attach_first_break(
        extract_levels_from_structure(s1h, level_col="protected_low"),
        pl_br,
        break_type="close_break_protected_down",
    )
    ph_1h = attach_first_break(
        extract_levels_from_structure(s1h, level_col="protected_high"),
        ph_br,
        break_type="close_break_protected_up",
    )
    pl_4h = attach_first_break(
        extract_levels_from_structure(s4h, level_col="protected_low"),
        pl_br4,
        break_type="close_break_protected_down",
    )
    ph_4h = attach_first_break(
        extract_levels_from_structure(s4h, level_col="protected_high"),
        ph_br4,
        break_type="close_break_protected_up",
    )

    return {
        "pl_1h_examples": select_1h_examples(pl_1h),
        "ph_1h_examples": select_1h_examples(ph_1h),
        "pl_4h_selected": select_4h_levels(
            pl_4h, pl_br4, break_type="close_break_protected_down"
        ),
        "ph_4h_selected": select_4h_levels(
            ph_4h, ph_br4, break_type="close_break_protected_up"
        ),
        "counts": {
            "1h_pl_levels": int(len(pl_1h)),
            "1h_ph_levels": int(len(ph_1h)),
            "4h_pl_levels": int(len(pl_4h)),
            "4h_ph_levels": int(len(ph_4h)),
            "4h_pl_breaks": int(len(pl_br4)),
            "4h_ph_breaks": int(len(ph_br4)),
        },
        "paths": {
            "structure_1h": str(artifact_dir / "structure_states_1h.parquet"),
            "structure_4h": str(artifact_dir / "structure_states_4h.parquet"),
            "pl_breaks": str(artifact_dir / "protected_low_break_events.csv"),
            "ph_breaks": str(artifact_dir / "protected_high_break_events.csv"),
        },
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Print 1h/4h Protected Low/High timestamps (read-only)."
    )
    p.add_argument(
        "--artifact-dir",
        type=Path,
        default=DEFAULT_ARTIFACT_DIR,
        help="Existing MTF structure artefact directory",
    )
    p.add_argument(
        "--symbols",
        default=",".join(SYMBOLS),
        help="Comma-separated symbols (default: APTUSDT,DOGEUSDT,BTCUSDT)",
    )
    args = p.parse_args(argv)
    artifact_dir = args.artifact_dir
    if not artifact_dir.is_dir():
        print(f"ERROR: artefact dir not found: {artifact_dir}", file=sys.stderr)
        return 1

    cat = build_catalog(artifact_dir)

    print("# Multi-TF Protected Level timestamps (read-only)\n")
    print("## Canonical sources\n")
    print(
        "- **Levels / confirmed_at / origin / state:** "
        "`structure_states_1h.parquet`, `structure_states_4h.parquet` "
        "(first bar where each distinct `protected_low` / `protected_high` appears)."
    )
    print(
        "- **Breaks:** `protected_low_break_events.csv`, `protected_high_break_events.csv` "
        "(rising-edge, `require_choch=True`)."
    )
    print(
        "- Inventory CSVs are first-seen summaries only — **not** used as confirmation SoT."
    )
    print(
        "\n## Time semantics\n"
        "- Candle interval `[open, close)`; TradingView shows **open** times.\n"
        "- `Known UTC` / `confirmed_at` / `available_at` = causal know-time (= candle **close**).\n"
        "- `Break Known UTC` = break candle close.\n"
        "- Tanzania = UTC+3 (EAT).\n"
    )
    print("## Selection rule\n")
    print(
        "- **1h:** per symbol early / mid / late by `level_confirmed_at`; "
        "prefer mix of broken + unbroken when available (3 examples).\n"
        "- **4h:** **all** levels that later have a choch-required break event, "
        "plus early/mid/late fillers if fewer than 3 levels.\n"
    )
    print(f"Counts: `{cat['counts']}`\n")

    pl1 = cat["pl_1h_examples"]
    ph1 = cat["ph_1h_examples"]
    pl4 = cat["pl_4h_selected"]
    ph4 = cat["ph_4h_selected"]

    print_markdown_table(
        [_table_row(r, timeframe="1h") for _, r in pl1.iterrows()],
        "Tabelle A – 1h Protected Lows",
    )
    print_chart_hint("1h_low")
    print_markdown_table(
        [_table_row(r, timeframe="1h") for _, r in ph1.iterrows()],
        "Tabelle B – 1h Protected Highs",
    )
    print_chart_hint("1h_high")
    print_markdown_table(
        [_table_row(r, timeframe="4h") for _, r in pl4.iterrows()],
        "Tabelle C – 4h Protected Lows (alle Breaks + ggf. Filler)",
    )
    print_chart_hint("4h_low")
    print_markdown_table(
        [_table_row(r, timeframe="4h") for _, r in ph4.iterrows()],
        "Tabelle D – 4h Protected Highs (alle Breaks + ggf. Filler)",
    )
    print_chart_hint("4h_high")

    print("\n# Detail je Symbol\n")
    for sym in [s.strip().upper() for s in str(args.symbols).split(",") if s.strip()]:
        print(f"## {sym}\n")
        print_detail_block(
            symbol=sym,
            timeframe="1h",
            level_type="PROTECTED_LOW",
            frame=pl1,
            source_structure=cat["paths"]["structure_1h"],
            source_breaks=cat["paths"]["pl_breaks"],
        )
        print_detail_block(
            symbol=sym,
            timeframe="1h",
            level_type="PROTECTED_HIGH",
            frame=ph1,
            source_structure=cat["paths"]["structure_1h"],
            source_breaks=cat["paths"]["ph_breaks"],
        )
        print_detail_block(
            symbol=sym,
            timeframe="4h",
            level_type="PROTECTED_LOW",
            frame=pl4,
            source_structure=cat["paths"]["structure_4h"],
            source_breaks=cat["paths"]["pl_breaks"],
        )
        print_detail_block(
            symbol=sym,
            timeframe="4h",
            level_type="PROTECTED_HIGH",
            frame=ph4,
            source_structure=cat["paths"]["structure_4h"],
            source_breaks=cat["paths"]["ph_breaks"],
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

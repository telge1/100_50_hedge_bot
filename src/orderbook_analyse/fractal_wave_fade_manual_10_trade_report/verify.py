"""MySQL 1m TP/SL/entry/exit verification for selected trades."""

from __future__ import annotations

from typing import Any

import pandas as pd
from sqlalchemy import text

from orderbook_analyse.fractal_cycle_wave_analysis import EXCHANGE
from orderbook_analyse.fractal_dynamic_cluster_upgrade_db.simulate import tpsl_for_tf
from orderbook_analyse.fractal_signal_confluence_db import ENV_FILE, TPSL_BY_TF
from orderbook_analyse.trend_scanner_mysql_feather_parity.load import _engine, load_env_file


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def load_1m_range(symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    load_env_file(ENV_FILE)
    eng = _engine()
    sql = text(
        """
        SELECT open_time, open, high, low, close
        FROM market_candles
        WHERE exchange = :exchange
          AND symbol = :symbol
          AND timeframe = BINARY '1m'
          AND is_closed = 1
          AND open_time >= :start
          AND open_time <= :end
        ORDER BY open_time
        """
    )
    a = _ts(start).tz_convert("UTC").to_pydatetime().replace(tzinfo=None)
    b = _ts(end).tz_convert("UTC").to_pydatetime().replace(tzinfo=None)
    with eng.connect() as conn:
        df = pd.read_sql(
            sql,
            conn,
            params={
                "exchange": EXCHANGE,
                "symbol": symbol,
                "start": a,
                "end": b,
            },
        )
    if df.empty:
        return df
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def levels_for_trade(tr: pd.Series) -> dict[str, Any]:
    epx = float(tr["entry_price"])
    side = str(tr["side"])
    first_tf = str(tr["first_signal_tf"])
    high_tf = str(tr["highest_tf_reached"])
    itp, isl = tpsl_for_tf(first_tf, extra_4h=False)
    ftp, fsl = tpsl_for_tf(high_tf, extra_4h=False)

    def prices(tp_pct: float, sl_pct: float) -> tuple[float, float]:
        if side == "LONG":
            return epx * (1.0 + tp_pct / 100.0), epx * (1.0 - sl_pct / 100.0)
        return epx * (1.0 - tp_pct / 100.0), epx * (1.0 + sl_pct / 100.0)

    itp_px, isl_px = prices(itp, isl)
    ftp_px, fsl_px = prices(ftp, fsl)
    return {
        "initial_tp_pct": itp,
        "initial_sl_pct": isl,
        "initial_tp_price": itp_px,
        "initial_sl_price": isl_px,
        "final_tp_pct": ftp,
        "final_sl_pct": fsl,
        "final_tp_price": ftp_px,
        "final_sl_price": fsl_px,
    }


def _exit_level(side: str, reason: str, tp_px: float, sl_px: float) -> float | None:
    if reason == "TP":
        return tp_px
    if reason == "SL":
        return sl_px
    return None


def verify_trade(tr: pd.Series, candles: pd.DataFrame, levels: dict[str, Any]) -> dict[str, Any]:
    side = str(tr["side"])
    epx = float(tr["entry_price"])
    entry_t = _ts(tr["entry_time"])
    exit_t = _ts(tr["exit_time"])
    reason = str(tr["exit_reason"])
    gross = float(tr["gross_return_pct"])
    fee = float(tr["fee_pct"])
    net = float(tr["net_return_pct"])

    entry_ok = False
    exit_ok = False
    tpsl_ok = False
    upgrade_ok = True
    notes: list[str] = []

    # Entry candle
    erow = candles.loc[candles["open_time"] == entry_t]
    if erow.empty:
        notes.append("ENTRY_CANDLE_MISSING")
    else:
        o = float(erow.iloc[0]["open"])
        entry_ok = abs(o - epx) <= max(1e-10, abs(epx) * 1e-8)
        if not entry_ok:
            notes.append(f"ENTRY_OPEN_MISMATCH open={o} vs entry_price={epx}")

    # TP/SL geometry
    ftp, fsl = levels["final_tp_pct"], levels["final_sl_pct"]
    ftp_px, fsl_px = levels["final_tp_price"], levels["final_sl_price"]
    itp_px, isl_px = levels["initial_tp_price"], levels["initial_sl_price"]

    if side == "LONG":
        geo = (ftp_px > epx) and (fsl_px < epx) and (itp_px > epx) and (isl_px < epx)
    else:
        geo = (ftp_px < epx) and (fsl_px > epx) and (itp_px < epx) and (isl_px > epx)

    # mathematical gross vs plan for TP/SL exits
    math_ok = True
    if reason == "TP":
        math_ok = abs(gross - ftp) < 1e-6
    elif reason == "SL":
        math_ok = abs(gross - (-fsl)) < 1e-6
    fee_ok = abs((gross - fee) - net) < 1e-9
    tpsl_ok = bool(geo and math_ok and fee_ok)
    if not geo:
        notes.append("TP_SL_GEOMETRY_FAIL")
    if not math_ok:
        notes.append(f"GROSS_VS_PLAN_FAIL gross={gross} final_tp={ftp} final_sl={fsl}")
    if not fee_ok:
        notes.append("FEE_NET_INCONSISTENT")

    # Exit candle touches level
    xrow = candles.loc[candles["open_time"] == exit_t]
    if xrow.empty:
        notes.append("EXIT_CANDLE_MISSING")
    else:
        h = float(xrow.iloc[0]["high"])
        l = float(xrow.iloc[0]["low"])
        lvl = _exit_level(side, reason, ftp_px, fsl_px)
        if lvl is None:
            # TIMEOUT / CONFLICT — verify exit_price equals open (conflict) or close path
            exit_ok = abs(float(tr["exit_price"]) - float(xrow.iloc[0]["open"])) < max(
                1e-10, abs(epx) * 1e-6
            ) or abs(float(tr["exit_price"]) - float(xrow.iloc[0]["close"])) < max(
                1e-10, abs(epx) * 1e-6
            )
            notes.append(f"NON_TPSL_EXIT:{reason}")
        else:
            if side == "LONG" and reason == "TP":
                exit_ok = h + 1e-12 >= lvl
            elif side == "LONG" and reason == "SL":
                exit_ok = l - 1e-12 <= lvl
            elif side == "SHORT" and reason == "TP":
                exit_ok = l - 1e-12 <= lvl
            elif side == "SHORT" and reason == "SL":
                exit_ok = h + 1e-12 >= lvl
            if not exit_ok:
                notes.append(f"EXIT_LEVEL_NOT_TOUCHED high={h} low={l} level={lvl}")

            # SL_FIRST: if both touched on exit bar, reason must be SL
            hit_tp = (h >= ftp_px) if side == "LONG" else (l <= ftp_px)
            hit_sl = (l <= fsl_px) if side == "LONG" else (h >= fsl_px)
            if hit_tp and hit_sl and reason != "SL":
                exit_ok = False
                notes.append("SL_FIRST_VIOLATION")

    # Upgrade reconstruction
    upc = int(tr["upgrade_count"])
    first_tf = str(tr["first_signal_tf"])
    high_tf = str(tr["highest_tf_reached"])
    if upc > 0:
        upgrade_ok = (
            first_tf != high_tf
            and abs(levels["initial_tp_pct"] - levels["final_tp_pct"])
            + abs(levels["initial_sl_pct"] - levels["final_sl_pct"])
            > 1e-12
            and "->" in str(tr["upgrade_sequence"])
        )
        if not upgrade_ok:
            notes.append("UPGRADE_FIELDS_INCONSISTENT")
        # old levels apply until upgrade: verify initial plan differs and final used at exit
        notes.append("P5A_UPGRADE_PRESENT")
    else:
        upgrade_ok = first_tf == high_tf and abs(levels["initial_tp_pct"] - levels["final_tp_pct"]) < 1e-12
        if not upgrade_ok:
            notes.append("NO_UPGRADE_BUT_TF_CHANGED")

    # ladder sanity vs frozen table
    for tf in (first_tf, high_tf):
        if tf not in TPSL_BY_TF:
            notes.append(f"UNKNOWN_TF:{tf}")
            tpsl_ok = False

    status = "PASS" if (entry_ok and exit_ok and tpsl_ok and upgrade_ok) else "FAIL"
    return {
        "entry_verified": entry_ok,
        "exit_verified": exit_ok,
        "tp_sl_verified": tpsl_ok,
        "upgrade_verified": upgrade_ok,
        "manual_audit_status": status,
        "verify_notes": ";".join(notes) if notes else "",
    }

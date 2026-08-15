"""Multicoin Feather→MySQL 5m inventory, import, and parity (reuses mysql_candle_store)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

from research.backtests.candle_loader import DEFAULT_DATA_DIR, symbol_to_feather_name
from research.regime_scanner.candle_sources import load_regime_db_env_file
from research.regime_scanner.mysql_candle_store.config import load_regime_db_config
from research.regime_scanner.mysql_candle_store.importer import import_feather
from research.regime_scanner.mysql_candle_store.repository import load_candles
from research.regime_scanner.mysql_candle_store.store_memory import InMemoryCandleStore
from research.regime_scanner.mysql_candle_store.store_mysql import MySQLCandleStore

DEFAULT_SYMBOLS: tuple[str, ...] = (
    "APTUSDT",
    "ENAUSDT",
    "ARBUSDT",
    "OPUSDT",
    "SUIUSDT",
    "SEIUSDT",
    "ADAUSDT",
    "XRPUSDT",
    "SOLUSDT",
    "DOGEUSDT",
    "LINKUSDT",
    "AVAXUSDT",
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
)

# Soft reference fill counts from prior feather multicoin audit (same A6 window semantics).
SOFT_EXPECTED_FILLS: dict[str, int] = {
    "APTUSDT": 55,
    "ENAUSDT": 58,
    "ARBUSDT": 63,
    "OPUSDT": 59,
    "SUIUSDT": 56,
    "SEIUSDT": 64,
    "ADAUSDT": 50,
    "XRPUSDT": 60,
    "SOLUSDT": 197,
    "DOGEUSDT": 52,
    "LINKUSDT": 55,
    "AVAXUSDT": 51,
}


def feather_path_5m(symbol: str, *, data_dir: Path | None = None) -> Path:
    root = Path(data_dir) if data_dir is not None else Path(DEFAULT_DATA_DIR)
    return root / symbol_to_feather_name(symbol, "5m")


def _validate_feather_ohlcv(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "feather_present": False,
            "status": "unavailable",
            "error": f"missing feather: {path}",
        }
    df = pd.read_feather(path)
    if "date" in df.columns and "timestamp" not in df.columns:
        df = df.rename(columns={"date": "timestamp"})
    ts = pd.to_datetime(df["timestamp"], utc=True)
    o = pd.to_numeric(df["open"], errors="coerce")
    h = pd.to_numeric(df["high"], errors="coerce")
    l = pd.to_numeric(df["low"], errors="coerce")
    c = pd.to_numeric(df["close"], errors="coerce")
    v = pd.to_numeric(df["volume"], errors="coerce")
    invalid = int((~(h >= l) | o.isna() | h.isna() | l.isna() | c.isna() | v.isna()).sum())
    dups = int(ts.duplicated().sum())
    expected = pd.Timedelta(minutes=5)
    gaps = 0
    if len(ts) > 1:
        sorted_ts = ts.sort_values()
        deltas = sorted_ts.diff().iloc[1:]
        gaps = int((deltas != expected).sum())
    return {
        "feather_present": True,
        "feather_path": str(path),
        "feather_n": int(len(df)),
        "feather_t0": str(ts.min()) if len(ts) else None,
        "feather_t1": str(ts.max()) if len(ts) else None,
        "feather_duplicates": dups,
        "feather_gaps": gaps,
        "feather_invalid_ohlc": invalid,
        "feather_sha1": hashlib.sha1(path.read_bytes()).hexdigest(),
    }


def _mysql_span(store: MySQLCandleStore, symbol: str) -> dict[str, Any]:
    n = store.count_candles(exchange="bybit", symbol=symbol.upper(), timeframe="5m")
    if n <= 0:
        return {"mysql_present": False, "mysql_n": 0, "mysql_t0": None, "mysql_t1": None}
    frame = load_candles(store, "bybit", symbol.upper(), "5m", closed_only=True)
    ts = pd.to_datetime(frame["timestamp"], utc=True)
    return {
        "mysql_present": True,
        "mysql_n": int(len(frame)),
        "mysql_t0": str(ts.iloc[0]) if len(ts) else None,
        "mysql_t1": str(ts.iloc[-1]) if len(ts) else None,
    }


def compare_feather_mysql_ohlcv(
    feather_df: pd.DataFrame, mysql_df: pd.DataFrame
) -> dict[str, Any]:
    f = feather_df.copy()
    m = mysql_df.copy()
    if "date" in f.columns and "timestamp" not in f.columns:
        f = f.rename(columns={"date": "timestamp"})
    f["timestamp"] = pd.to_datetime(f["timestamp"], utc=True)
    m["timestamp"] = pd.to_datetime(m["timestamp"], utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        f[col] = pd.to_numeric(f[col], errors="coerce")
        m[col] = pd.to_numeric(m[col], errors="coerce")
    common = sorted(set(f["timestamp"]) & set(m["timestamp"]))
    if not common:
        return {
            "classification": "incomplete",
            "ok_for_store": False,
            "n_common": 0,
            "mysql_n": int(len(m)),
            "feather_n": int(len(f)),
        }
    fc = f.set_index("timestamp").loc[common]
    mc = m.set_index("timestamp").loc[common]
    max_px = 0.0
    for c in ("open", "high", "low", "close"):
        max_px = max(max_px, float((fc[c] - mc[c]).abs().max()))
    max_vol = float((fc["volume"] - mc["volume"]).abs().max())
    only_f = int(len(set(f["timestamp"]) - set(m["timestamp"])))
    only_m = int(len(set(m["timestamp"]) - set(f["timestamp"])))
    if len(f) == len(m) and only_f == 0 and only_m == 0 and max_px == 0.0 and max_vol == 0.0:
        cls = "exact_match"
    elif only_f == 0 and only_m == 0 and max_px < 1e-10 and max_vol < 1e-6:
        cls = "rounding_match"
    elif only_f or only_m:
        cls = "incomplete"
    else:
        cls = "mismatch"
    return {
        "classification": cls,
        "ok_for_store": cls in {"exact_match", "rounding_match"},
        "n_common": int(len(common)),
        "feather_n": int(len(f)),
        "mysql_n": int(len(m)),
        "only_feather": only_f,
        "only_mysql": only_m,
        "max_abs_price_diff": max_px,
        "max_abs_volume_diff": max_vol,
    }


def inventory_symbol(
    symbol: str,
    *,
    store: MySQLCandleStore | None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    path = feather_path_5m(symbol, data_dir=data_dir)
    row: dict[str, Any] = {"symbol": symbol.upper(), **_validate_feather_ohlcv(path)}
    if store is not None:
        row.update(_mysql_span(store, symbol))
    else:
        row.update({"mysql_present": None, "mysql_n": None, "mysql_t0": None, "mysql_t1": None})

    if not row.get("feather_present"):
        row["status"] = "unavailable"
        row["import_required"] = False
    elif row.get("feather_invalid_ohlc", 0) > 0:
        row["status"] = "incomplete"
        row["import_required"] = False
    elif not row.get("mysql_present"):
        row["status"] = "import_required"
        row["import_required"] = True
    elif int(row.get("mysql_n") or 0) != int(row.get("feather_n") or -1):
        row["status"] = "import_required"
        row["import_required"] = True
    else:
        row["status"] = "ready"
        row["import_required"] = False
    return row


def run_multicoin_import(
    *,
    symbols: Sequence[str],
    regime_db_env: Path,
    output_dir: Path,
    dry_run: bool = True,
    data_dir: Path | None = None,
    exchange: str = "bybit",
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    load_regime_db_env_file(regime_db_env)
    cfg = load_regime_db_config()
    store = MySQLCandleStore(cfg)
    inventory: list[dict[str, Any]] = []
    dry_rows: list[dict[str, Any]] = []
    parity_rows: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    try:
        store.init_schema()
        for sym in symbols:
            inv = inventory_symbol(sym, store=store, data_dir=data_dir)
            inventory.append(inv)
            path = feather_path_5m(sym, data_dir=data_dir)
            if inv.get("status") == "unavailable":
                failed.append({"symbol": sym, "stage": "inventory", "error": inv.get("error")})
                parity_rows.append(
                    {
                        "symbol": sym,
                        "classification": "unavailable",
                        "ok_for_store": False,
                    }
                )
                continue

            # Dry-run import always (validates feather via importer)
            mem = InMemoryCandleStore()
            mem.init_schema()
            try:
                dry_report = import_feather(
                    mem,
                    input_path=path,
                    exchange=exchange,
                    symbol=sym.upper(),
                    timeframe="5m",
                    dry_run=True,
                )
                dry_rows.append(dry_report.to_dict())
                if dry_report.errors:
                    failed.append(
                        {
                            "symbol": sym,
                            "stage": "dry_run",
                            "error": "; ".join(dry_report.errors),
                        }
                    )
                    continue
            except Exception as exc:  # noqa: BLE001
                failed.append({"symbol": sym, "stage": "dry_run", "error": f"{type(exc).__name__}: {exc}"})
                continue

            if not dry_run and inv.get("import_required"):
                try:
                    report = import_feather(
                        store,
                        input_path=path,
                        exchange=exchange,
                        symbol=sym.upper(),
                        timeframe="5m",
                        dry_run=False,
                    )
                    if report.errors:
                        failed.append(
                            {
                                "symbol": sym,
                                "stage": "import",
                                "error": "; ".join(report.errors),
                            }
                        )
                        continue
                except Exception as exc:  # noqa: BLE001
                    failed.append(
                        {"symbol": sym, "stage": "import", "error": f"{type(exc).__name__}: {exc}"}
                    )
                    continue

            # Post parity (after import or if already present)
            try:
                feather_df = pd.read_feather(path)
                mysql_df = load_candles(
                    store, exchange, sym.upper(), "5m", closed_only=True
                )
                par = compare_feather_mysql_ohlcv(feather_df, mysql_df)
                par["symbol"] = sym.upper()
                parity_rows.append(par)
                if not par.get("ok_for_store"):
                    failed.append(
                        {
                            "symbol": sym,
                            "stage": "parity",
                            "error": par.get("classification"),
                        }
                    )
                # refresh inventory status
                inv2 = inventory_symbol(sym, store=store, data_dir=data_dir)
                inv.update(inv2)
                if par.get("ok_for_store") and inv2.get("mysql_present"):
                    inv["status"] = "ready"
                    inv["import_required"] = False
            except Exception as exc:  # noqa: BLE001
                failed.append(
                    {"symbol": sym, "stage": "parity", "error": f"{type(exc).__name__}: {exc}"}
                )
                parity_rows.append(
                    {
                        "symbol": sym,
                        "classification": "mismatch",
                        "ok_for_store": False,
                        "error": str(exc),
                    }
                )

        inv_df = pd.DataFrame(inventory)
        inv_df.to_csv(output_dir / "mysql_multicoin_import_inventory.csv", index=False)
        pd.DataFrame(dry_rows).to_csv(output_dir / "mysql_multicoin_import_dry_run.csv", index=False)
        pd.DataFrame(parity_rows).to_csv(output_dir / "mysql_multicoin_parity.csv", index=False)
        pd.DataFrame(failed).to_csv(output_dir / "multicoin_failed_symbols.csv", index=False)

        ready = [r["symbol"] for r in inventory if r.get("status") == "ready"]
        # prefer parity ok
        parity_ok = {r["symbol"] for r in parity_rows if r.get("ok_for_store")}
        ready_ok = [s for s in ready if s in parity_ok] or [
            r["symbol"] for r in parity_rows if r.get("ok_for_store")
        ]

        return {
            "ok": True,
            "dry_run": dry_run,
            "n_requested": len(symbols),
            "n_ready": len(ready_ok),
            "ready_symbols": ready_ok,
            "n_failed": len(failed),
            "failed": failed,
            "inventory": inventory,
            "parity": parity_rows,
        }
    finally:
        store.close()

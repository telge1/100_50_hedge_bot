"""Download + validate Bybit LINEAR historical PUBLIC TRADES (productId=trade)."""

from __future__ import annotations

import csv
import gzip
import io
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Iterator

from research.orderbook.bybit_historical_download_common import (
    LIST_FILES_URL,
    atomic_download,
    build_session,
    decompress_gzip_safe,
    extract_zip_safe,
    is_gzip_file,
    list_file_items,
    list_files_for_day,
    validate_date,
    validate_zip,
    warmup_session,
)

logger = logging.getLogger("bybit_trade_download")

# Confirmed from Bybit v5 recent-trade docs + project CH ingest convention:
# side is the *taker/aggressor* side.
SIDE_SEMANTICS = {
    "Buy": "taker_buy_aggressor_hits_ask",
    "Sell": "taker_sell_aggressor_hits_bid",
    "source": (
        "Bybit API v5 market/recent-trade: side = Side of taker Buy/Sell; "
        "same convention used in orderbook_analyse public_trade_source and CH ingest."
    ),
}

PRODUCT_ID = "trade"
BIZ_TYPE = "contract"


@dataclass
class TradeDayResult:
    symbol: str
    date: str
    product_id: str = PRODUCT_ID
    biz_type: str = BIZ_TYPE
    api_http_status: int | None = None
    api_ret_code: int | None = None
    api_ret_msg: str | None = None
    list_file_count: int | None = None
    available: bool = False
    filename: str | None = None
    download_url: str | None = None
    reported_size: int | None = None
    downloaded_size: int | None = None
    archive_kind: str | None = None
    archive_valid: bool | None = None
    extracted: bool = False
    extracted_filename: str | None = None
    uncompressed_size: int | None = None
    detected_format: str | None = None
    columns: list[str] = field(default_factory=list)
    trade_count: int | None = None
    buy_count: int | None = None
    sell_count: int | None = None
    first_trade_ts_raw: str | None = None
    last_trade_ts_raw: str | None = None
    first_trade_ts_utc: str | None = None
    last_trade_ts_utc: str | None = None
    min_price: float | None = None
    max_price: float | None = None
    skipped_download: bool = False
    status: str = "PENDING"
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["columns"] = "|".join(self.columns)
        row["notes"] = " || ".join(self.notes)
        return row


def list_trade_files_for_day(
    session,
    *,
    symbol: str,
    day: str,
    connect: float,
    read: float,
    max_retries: int,
) -> tuple[int, dict[str, Any] | None, str | None]:
    return list_files_for_day(
        session,
        symbol=symbol,
        day=day,
        product_id=PRODUCT_ID,
        connect=connect,
        read=read,
        max_retries=max_retries,
        biz_type=BIZ_TYPE,
    )


def pick_trade_day_entry(
    payload: dict[str, Any], *, symbol: str, day: str
) -> dict[str, Any] | None:
    """Pick trade file from API list — prefer exact day/symbol match; no guessed names."""
    items = list_file_items(payload)
    if not items:
        return None

    def score(item: dict[str, Any]) -> tuple[int, str]:
        fn = str(item.get("filename") or "")
        url = str(item.get("url") or "")
        blob = f"{fn} {url}".lower()
        s = 0
        if day in fn or day in url:
            s += 50
        if symbol.lower() in blob or symbol.upper() in fn or symbol.upper() in url:
            s += 40
        if any(x in blob for x in ("trade", "trading", ".csv")):
            s += 10
        if fn.endswith((".csv.gz", ".gz", ".zip", ".csv")):
            s += 5
        return (s, fn)

    ranked = sorted(items, key=score, reverse=True)
    best = ranked[0]
    best_score = score(best)[0]
    if best_score < 50:
        return None
    return best


def detect_archive_kind(path: Path) -> str:
    name = path.name.lower()
    with path.open("rb") as fh:
        head = fh.read(8)
    if head.startswith(b"PK") or name.endswith(".zip"):
        ok, _, _, _ = validate_zip(path)
        if ok:
            return "zip"
    if head.startswith(b"\x1f\x8b") or name.endswith(".gz"):
        return "gzip"
    if name.endswith(".csv") or head.lstrip().startswith((b"timestamp", b"id,", b"{")):
        return "plain_csv"
    return "unknown"


def materialize_trade_file(archive_path: Path, day_dir: Path) -> tuple[str, int, str, list[str]]:
    """Decompress/extract; keep original. Returns (data_name, size, kind, notes)."""
    notes: list[str] = []
    kind = detect_archive_kind(archive_path)
    if kind == "zip":
        ok, crc_ok, names, err = validate_zip(archive_path)
        if not ok or not crc_ok:
            raise RuntimeError(err or "invalid zip")
        notes.append(f"zip_members={names}")
        name, size, all_names = extract_zip_safe(archive_path, day_dir)
        # If member is still .gz, decompress further
        member_path = day_dir / name
        if is_gzip_file(member_path) or name.endswith(".gz"):
            notes.append(f"zip_member_gzip={name}")
            csv_name, csv_size = decompress_gzip_safe(member_path, day_dir)
            return csv_name, csv_size, "zip+gzip", notes
        return name, size, "zip", notes
    if kind == "gzip":
        csv_name, csv_size = decompress_gzip_safe(archive_path, day_dir)
        return csv_name, csv_size, "gzip", notes
    if kind in {"plain_csv", "plain_csv_or_json"}:
        return archive_path.name, archive_path.stat().st_size, kind, notes
    raise RuntimeError(f"unsupported archive kind={kind} for {archive_path}")


def _open_text_stream(path: Path):
    if is_gzip_file(path) or path.name.endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def parse_trade_timestamp(raw: str) -> tuple[datetime, str, str]:
    """Return (utc_dt, unit_label, iso_z). Detect s / ms / us / ns from magnitude."""
    s = str(raw).strip()
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid timestamp {raw!r}") from exc
    # Integer-ish magnitude
    abs_d = abs(d)
    if abs_d >= Decimal("1e17"):
        unit = "ns"
        seconds = d / Decimal("1e9")
    elif abs_d >= Decimal("1e14"):
        unit = "us"
        seconds = d / Decimal("1e6")
    elif abs_d >= Decimal("1e11"):
        unit = "ms"
        seconds = d / Decimal("1e3")
    else:
        unit = "s"
        seconds = d
    whole = int(seconds)
    frac = seconds - Decimal(whole)
    micros = int((frac * Decimal("1000000")).to_integral_value())
    if micros >= 1_000_000:
        whole += 1
        micros -= 1_000_000
    if micros < 0:
        whole -= 1
        micros += 1_000_000
    dt = datetime.fromtimestamp(whole, tz=timezone.utc).replace(microsecond=max(0, micros))
    iso = dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return dt, unit, iso


def inspect_trade_csv(path: Path, *, max_samples: int = 5) -> dict[str, Any]:
    """Streaming CSV inspect + full-day coverage stats (line scan, not full RAM load)."""
    out: dict[str, Any] = {
        "path": str(path),
        "detected_format": "unknown",
        "columns": [],
        "sample_records": [],
        "sample_timestamp_conversions": [],
        "trade_count": 0,
        "buy_count": 0,
        "sell_count": 0,
        "other_side_count": 0,
        "first_trade_ts_raw": None,
        "last_trade_ts_raw": None,
        "first_trade_ts_utc": None,
        "last_trade_ts_utc": None,
        "timestamp_unit": None,
        "min_price": None,
        "max_price": None,
        "side_values_seen": [],
        "notes": [],
    }
    sides: set[str] = set()
    min_px: float | None = None
    max_px: float | None = None

    with _open_text_stream(path) as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            out["notes"].append("no CSV header")
            return out
        cols = [c.strip() for c in reader.fieldnames]
        out["columns"] = cols
        out["detected_format"] = "csv"
        if path.suffix == ".gz" or path.name.endswith(".csv.gz") or is_gzip_file(path):
            # Inspect path is normally the decompressed CSV; keep tag if still gz.
            out["detected_format"] = "csv"

        ts_col = next(
            (c for c in cols if c.lower() in {"timestamp", "time", "trade_time", "ts"}),
            None,
        )
        side_col = next((c for c in cols if c.lower() == "side"), None)
        price_col = next((c for c in cols if c.lower() == "price"), None)
        if ts_col is None:
            out["notes"].append(f"no timestamp-like column in {cols}")

        for row in reader:
            out["trade_count"] += 1
            if side_col:
                side = str(row.get(side_col) or "").strip()
                sides.add(side)
                if side == "Buy":
                    out["buy_count"] += 1
                elif side == "Sell":
                    out["sell_count"] += 1
                else:
                    out["other_side_count"] += 1
            if price_col:
                try:
                    px = float(str(row.get(price_col) or "").strip())
                    min_px = px if min_px is None else min(min_px, px)
                    max_px = px if max_px is None else max(max_px, px)
                except ValueError:
                    pass
            raw_ts = str(row.get(ts_col) or "").strip() if ts_col else ""
            if raw_ts:
                if out["first_trade_ts_raw"] is None:
                    out["first_trade_ts_raw"] = raw_ts
                    try:
                        _dt, unit, iso = parse_trade_timestamp(raw_ts)
                        out["first_trade_ts_utc"] = iso
                        out["timestamp_unit"] = unit
                    except ValueError as exc:
                        out["notes"].append(f"first_ts_parse_error: {exc}")
                out["last_trade_ts_raw"] = raw_ts
            if len(out["sample_records"]) < max_samples:
                sample_row = {c: row.get(c) for c in cols}
                out["sample_records"].append(sample_row)
                if raw_ts:
                    try:
                        _dt, unit, iso = parse_trade_timestamp(raw_ts)
                        out["sample_timestamp_conversions"].append(
                            {
                                "raw": raw_ts,
                                "unit": unit,
                                "utc": iso,
                                "assumed_timezone": "UTC",
                            }
                        )
                        out["timestamp_unit"] = out["timestamp_unit"] or unit
                    except ValueError as exc:
                        out["sample_timestamp_conversions"].append(
                            {"raw": raw_ts, "error": str(exc)}
                        )

        if out["last_trade_ts_raw"]:
            try:
                _dt, unit, iso = parse_trade_timestamp(out["last_trade_ts_raw"])
                out["last_trade_ts_utc"] = iso
                out["timestamp_unit"] = out["timestamp_unit"] or unit
            except ValueError as exc:
                out["notes"].append(f"last_ts_parse_error: {exc}")

    out["min_price"] = min_px
    out["max_price"] = max_px
    out["side_values_seen"] = sorted(sides)
    out["side_semantics"] = SIDE_SEMANTICS
    return out


def count_trades_in_window(
    path: Path,
    *,
    start_ms: int,
    end_ms: int,
) -> dict[str, Any]:
    """Stream-count trades with timestamp in [start_ms, end_ms] inclusive."""
    n = 0
    buy = 0
    sell = 0
    first_raw = None
    last_raw = None
    first_utc = None
    last_utc = None
    with _open_text_stream(path) as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return {"n": 0, "buy": 0, "sell": 0}
        cols = list(reader.fieldnames)
        ts_col = next((c for c in cols if c.lower() in {"timestamp", "time", "trade_time", "ts"}), None)
        side_col = next((c for c in cols if c.lower() == "side"), None)
        if ts_col is None:
            return {"n": 0, "buy": 0, "sell": 0, "error": "no_timestamp_column"}
        for row in reader:
            raw = str(row.get(ts_col) or "").strip()
            if not raw:
                continue
            try:
                dt, unit, iso = parse_trade_timestamp(raw)
            except ValueError:
                continue
            ms = int(dt.timestamp() * 1000)
            if ms < start_ms or ms > end_ms:
                continue
            n += 1
            if first_raw is None:
                first_raw = raw
                first_utc = iso
            last_raw = raw
            last_utc = iso
            side = str(row.get(side_col) or "").strip() if side_col else ""
            if side == "Buy":
                buy += 1
            elif side == "Sell":
                sell += 1
    return {
        "n": n,
        "buy": buy,
        "sell": sell,
        "first_trade_ts_raw": first_raw,
        "last_trade_ts_raw": last_raw,
        "first_trade_ts_utc": first_utc,
        "last_trade_ts_utc": last_utc,
    }



def process_trade_day(
    session,
    *,
    symbol: str,
    day: str,
    out_root: Path,
    connect: float,
    read: float,
    max_retries: int,
    do_inspect: bool = True,
) -> tuple[TradeDayResult, dict[str, Any] | None]:
    result = TradeDayResult(symbol=symbol, date=day)
    day_dir = out_root / symbol / day
    day_dir.mkdir(parents=True, exist_ok=True)
    inspect: dict[str, Any] | None = None

    try:
        status, payload, err = list_trade_files_for_day(
            session,
            symbol=symbol,
            day=day,
            connect=connect,
            read=read,
            max_retries=max_retries,
        )
        result.api_http_status = status
        if payload is not None:
            try:
                result.api_ret_code = int(payload.get("ret_code"))
            except (TypeError, ValueError):
                result.api_ret_code = None
            result.api_ret_msg = str(payload.get("ret_msg") or "")
            items = list_file_items(payload)
            result.list_file_count = len(items)
            result.notes.append(
                "list_files="
                + json.dumps(
                    [
                        {
                            "filename": it.get("filename"),
                            "url": it.get("url"),
                            "size": it.get("size"),
                        }
                        for it in items
                    ],
                    ensure_ascii=True,
                )
            )
        if err or payload is None:
            result.status = "API_ERROR"
            result.error = err or "empty payload"
            return result, None

        entry = pick_trade_day_entry(payload, symbol=symbol, day=day)
        if entry is None:
            result.available = False
            result.status = "NOT_AVAILABLE"
            return result, None

        result.available = True
        result.filename = str(entry.get("filename") or "")
        result.download_url = str(entry.get("url") or "")
        try:
            result.reported_size = int(entry.get("size"))
        except (TypeError, ValueError):
            result.reported_size = None
        if not result.filename or not result.download_url:
            result.status = "BAD_LIST_ENTRY"
            result.error = f"missing filename/url in {entry}"
            return result, None

        dest = day_dir / Path(result.filename).name
        need_download = True
        if dest.exists() and dest.is_file() and dest.stat().st_size > 0:
            if result.reported_size is None or dest.stat().st_size == result.reported_size:
                need_download = False
                result.skipped_download = True
                result.downloaded_size = dest.stat().st_size
                result.notes.append("archive already present+size_ok; skip download")
            else:
                result.notes.append("existing archive size mismatch; re-downloading")
                dest.unlink(missing_ok=True)

        if need_download:
            logger.info("downloading %s -> %s", result.download_url, dest)
            result.downloaded_size = atomic_download(
                session,
                url=result.download_url,
                dest=dest,
                expected_size=result.reported_size,
                connect=connect,
                read=read,
                max_retries=max_retries,
            )

        result.archive_kind = detect_archive_kind(dest)
        result.archive_valid = True
        data_name, unc_size, kind, mat_notes = materialize_trade_file(dest, day_dir)
        result.notes.extend(mat_notes)
        result.archive_kind = kind
        result.extracted = True
        result.extracted_filename = data_name
        result.uncompressed_size = unc_size

        data_path = day_dir / data_name
        if do_inspect:
            inspect = inspect_trade_csv(data_path)
            result.detected_format = inspect.get("detected_format")
            result.columns = list(inspect.get("columns") or [])
            result.trade_count = inspect.get("trade_count")
            result.buy_count = inspect.get("buy_count")
            result.sell_count = inspect.get("sell_count")
            result.first_trade_ts_raw = inspect.get("first_trade_ts_raw")
            result.last_trade_ts_raw = inspect.get("last_trade_ts_raw")
            result.first_trade_ts_utc = inspect.get("first_trade_ts_utc")
            result.last_trade_ts_utc = inspect.get("last_trade_ts_utc")
            result.min_price = inspect.get("min_price")
            result.max_price = inspect.get("max_price")
            result.notes.append(f"timestamp_unit={inspect.get('timestamp_unit')}")

        result.status = "OK"
        return result, inspect
    except Exception as exc:  # noqa: BLE001
        logger.exception("trade day failed %s %s", symbol, day)
        result.status = "FAILED"
        result.error = f"{type(exc).__name__}: {exc}"
        # Ensure failed download leaves no final dest if .part cleanup already handled;
        # if dest exists but incomplete, leave for inspection — atomic_download removes .part.
        return result, None


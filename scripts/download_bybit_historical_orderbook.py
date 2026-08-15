#!/usr/bin/env python3
"""Download Bybit LINEAR historical orderbook (ob200) daily ZIPs.

Stores under:
  data/bybit_historical_orderbook/<SYMBOL>/<YYYY-MM-DD>/

Smoke / per-day workflow:
  - list-files API for each date separately
  - atomic ZIP download (*.part → final)
  - CRC-validated extract into the same day folder
  - optional format inspection (--inspect)

Does not delete ZIPs. Does not import to ClickHouse or reconstruct books.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
import tempfile
import time
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_ROOT = PROJECT_ROOT / "data" / "bybit_historical_orderbook"

LIST_FILES_URL = (
    "https://www.bybit.com/x-api/quote/public/support/download/list-files"
)
WARMUP_URL = "https://www.bybit.com/data-download"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

logger = logging.getLogger("bybit_ob_download")


@dataclass
class DayResult:
    symbol: str
    date: str
    api_http_status: int | None = None
    available: bool = False
    filename: str | None = None
    download_url: str | None = None
    reported_size: int | None = None
    downloaded_size: int | None = None
    zip_valid: bool | None = None
    zip_crc_valid: bool | None = None
    extracted: bool = False
    extracted_filename: str | None = None
    uncompressed_size: int | None = None
    detected_format: str | None = None
    snapshot_seen: bool | None = None
    delta_seen: bool | None = None
    max_bid_levels: int | None = None
    max_ask_levels: int | None = None
    status: str = "PENDING"
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Download Bybit historical linear orderbook daily ZIPs"
    )
    p.add_argument("--symbol", required=True, help="e.g. APTUSDT")
    p.add_argument(
        "--dates",
        nargs="+",
        required=True,
        help="One or more YYYY-MM-DD dates (non-contiguous OK)",
    )
    p.add_argument(
        "--out-root",
        type=Path,
        default=DEFAULT_OUT_ROOT,
        help=f"Output root (default: {DEFAULT_OUT_ROOT})",
    )
    p.add_argument(
        "--inspect",
        action="store_true",
        help="After extract, inspect NDJSON format (first records + level counts)",
    )
    p.add_argument(
        "--inspect-date",
        default=None,
        help="Prefer this date for deep inspect (default: first successful day)",
    )
    p.add_argument("--connect-timeout", type=float, default=15.0)
    p.add_argument("--read-timeout", type=float, default=120.0)
    p.add_argument("--max-retries", type=int, default=5)
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def validate_date(day: str) -> str:
    if not DATE_RE.match(day):
        raise ValueError(f"invalid date {day!r}; expected YYYY-MM-DD")
    datetime.strptime(day, "%Y-%m-%d")
    return day


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": DEFAULT_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://www.bybit.com",
            "Referer": "https://www.bybit.com/data-download",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
    )
    return session


def warmup_session(session: requests.Session, *, connect: float, read: float) -> None:
    """Hit data-download page so Akamai issues cookies (_abck / bm_sz)."""
    try:
        resp = session.get(
            WARMUP_URL,
            timeout=(connect, read),
            headers={"Accept": "text/html,application/xhtml+xml"},
            allow_redirects=True,
        )
        logger.info("warmup %s -> HTTP %s", WARMUP_URL, resp.status_code)
    except requests.RequestException as exc:
        logger.warning("warmup failed (continuing): %s", exc)


def request_with_retries(
    session: requests.Session,
    method: str,
    url: str,
    *,
    connect: float,
    read: float,
    max_retries: int,
    stream: bool = False,
    **kwargs: Any,
) -> requests.Response:
    last_exc: BaseException | None = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.request(
                method,
                url,
                timeout=(connect, read),
                stream=stream,
                **kwargs,
            )
            if resp.status_code in {429, 500, 502, 503, 504}:
                raise requests.HTTPError(
                    f"transient HTTP {resp.status_code}", response=resp
                )
            return resp
        except (requests.RequestException, requests.HTTPError) as exc:
            last_exc = exc
            if attempt >= max_retries:
                break
            sleep_s = min(60.0, 1.5 ** attempt)
            logger.warning(
                "attempt %s/%s failed for %s: %s; retry in %.1fs",
                attempt,
                max_retries,
                url,
                exc,
                sleep_s,
            )
            time.sleep(sleep_s)
    assert last_exc is not None
    raise last_exc


def list_files_for_day(
    session: requests.Session,
    *,
    symbol: str,
    day: str,
    connect: float,
    read: float,
    max_retries: int,
) -> tuple[int, dict[str, Any] | None, str | None]:
    params = {
        "bizType": "contract",
        "productId": "orderbook",
        "symbols": symbol,
        "interval": "daily",
        "periods": "",
        "startDay": day,
        "endDay": day,
    }
    resp = request_with_retries(
        session,
        "GET",
        LIST_FILES_URL,
        connect=connect,
        read=read,
        max_retries=max_retries,
        params=params,
    )
    status = resp.status_code
    if status != 200:
        return status, None, f"list-files HTTP {status}: {resp.text[:300]}"
    try:
        payload = resp.json()
    except ValueError:
        return status, None, f"list-files non-JSON: {resp.text[:300]}"
    if int(payload.get("ret_code", -1)) != 0:
        return status, payload, f"ret_code={payload.get('ret_code')} msg={payload.get('ret_msg')}"
    return status, payload, None


def pick_day_entry(payload: dict[str, Any], *, symbol: str, day: str) -> dict[str, Any] | None:
    result = payload.get("result") or {}
    items = result.get("list") or []
    want = f"{day}_{symbol}_ob200.data.zip"
    for item in items:
        if str(item.get("filename") or "") == want:
            return item
    # Fallback: any matching symbol/date orderbook zip
    for item in items:
        fn = str(item.get("filename") or "")
        if day in fn and symbol in fn and "ob200" in fn and fn.endswith(".zip"):
            return item
    return None


def atomic_download(
    session: requests.Session,
    *,
    url: str,
    dest: Path,
    expected_size: int | None,
    connect: float,
    read: float,
    max_retries: int,
) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    if part.exists():
        part.unlink()

    resp = request_with_retries(
        session,
        "GET",
        url,
        connect=connect,
        read=read,
        max_retries=max_retries,
        stream=True,
        headers={"Referer": "https://www.bybit.com/", "Accept": "*/*"},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"download HTTP {resp.status_code} for {url}")

    written = 0
    try:
        with part.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                fh.write(chunk)
                written += len(chunk)
            fh.flush()
        if expected_size is not None and written != expected_size:
            raise RuntimeError(
                f"size mismatch: downloaded={written} reported={expected_size}"
            )
        part.replace(dest)
    except Exception:
        if part.exists():
            part.unlink(missing_ok=True)
        raise
    finally:
        resp.close()
    return written


def validate_zip(path: Path) -> tuple[bool, bool, list[str], str | None]:
    """Return (zip_opens, crc_ok, namelist, error)."""
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = zf.namelist()
            bad = zf.testzip()
            if bad is not None:
                return True, False, names, f"CRC failed for member {bad!r}"
            return True, True, names, None
    except zipfile.BadZipFile as exc:
        return False, False, [], f"BadZipFile: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, False, [], f"{type(exc).__name__}: {exc}"


def extract_zip_safe(zip_path: Path, day_dir: Path) -> tuple[str, int, list[str]]:
    """Extract into day_dir safely. Returns (primary_extracted_name, size, all_names)."""
    day_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        if not names:
            raise RuntimeError("ZIP has no files")

        # Prefer the *.data member matching the zip stem
        preferred = zip_path.name.replace(".zip", "")
        primary = next((n for n in names if Path(n).name == preferred), names[0])

        existing_ok: list[str] = []
        need_extract: list[str] = []
        for name in names:
            target = day_dir / Path(name).name
            info = zf.getinfo(name)
            if target.exists() and target.is_file() and target.stat().st_size == info.file_size:
                existing_ok.append(Path(name).name)
            elif target.exists():
                raise RuntimeError(
                    f"refusing to overwrite existing extracted file with size mismatch: {target}"
                )
            else:
                need_extract.append(name)

        if not need_extract:
            primary_name = Path(primary).name
            size = (day_dir / primary_name).stat().st_size
            return primary_name, size, [Path(n).name for n in names]

        with tempfile.TemporaryDirectory(prefix=".extract_", dir=day_dir) as tmp:
            tmp_dir = Path(tmp)
            for name in need_extract:
                # Extract member basename only (no zip-slip)
                base = Path(name).name
                if not base or base in {".", ".."}:
                    raise RuntimeError(f"unsafe zip member name: {name!r}")
                src = zf.open(name)
                tmp_path = tmp_dir / base
                with src, tmp_path.open("wb") as out:
                    shutil.copyfileobj(src, out, length=1024 * 1024)
                info = zf.getinfo(name)
                if tmp_path.stat().st_size != info.file_size:
                    raise RuntimeError(
                        f"extract size mismatch for {base}: "
                        f"{tmp_path.stat().st_size} != {info.file_size}"
                    )
                final = day_dir / base
                if final.exists():
                    raise RuntimeError(f"target appeared during extract: {final}")
                tmp_path.replace(final)

        primary_name = Path(primary).name
        size = (day_dir / primary_name).stat().st_size
        return primary_name, size, [Path(n).name for n in names]


def _looks_like_json_object(line: str) -> bool:
    s = line.strip()
    return s.startswith("{") and s.endswith("}")


def inspect_ob_file(path: Path, *, max_records: int = 5) -> dict[str, Any]:
    """Streaming format smoke-test — does not load the full file."""
    out: dict[str, Any] = {
        "path": str(path),
        "detected_format": "unknown",
        "sample_records": [],
        "fields_seen": sorted([]),
        "snapshot_seen": False,
        "delta_seen": False,
        "max_bid_levels": 0,
        "max_ask_levels": 0,
        "first_snapshot_bid_levels": None,
        "first_snapshot_ask_levels": None,
        "has_u": False,
        "has_seq": False,
        "has_type": False,
        "has_ts": False,
        "has_cts": False,
        "notes": [],
    }
    fields: set[str] = set()
    samples: list[Any] = []
    first_snap_done = False

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if i >= 20000 and first_snap_done and len(samples) >= max_records:
                break
            if not line.strip():
                continue
            if not _looks_like_json_object(line):
                out["detected_format"] = "non_json_line"
                out["notes"].append(f"line {i+1} not JSON object: {line[:120]!r}")
                break
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                out["detected_format"] = "broken_json_lines"
                out["notes"].append(f"JSON decode error line {i+1}: {exc}")
                break

            out["detected_format"] = "json_lines_ndjson"
            if isinstance(obj, dict):
                fields.update(obj.keys())
                data = obj.get("data")
                if isinstance(data, dict):
                    fields.update(f"data.{k}" for k in data.keys())
                msg_type = str(obj.get("type") or "")
                if msg_type == "snapshot":
                    out["snapshot_seen"] = True
                elif msg_type == "delta":
                    out["delta_seen"] = True
                out["has_type"] = out["has_type"] or ("type" in obj)
                out["has_ts"] = out["has_ts"] or ("ts" in obj)
                out["has_cts"] = out["has_cts"] or ("cts" in obj)
                if isinstance(data, dict):
                    out["has_u"] = out["has_u"] or ("u" in data)
                    out["has_seq"] = out["has_seq"] or ("seq" in data)
                    bids = data.get("b") or data.get("bids") or []
                    asks = data.get("a") or data.get("asks") or []
                    if isinstance(bids, list):
                        out["max_bid_levels"] = max(out["max_bid_levels"], len(bids))
                    if isinstance(asks, list):
                        out["max_ask_levels"] = max(out["max_ask_levels"], len(asks))
                    if msg_type == "snapshot" and not first_snap_done:
                        out["first_snapshot_bid_levels"] = (
                            len(bids) if isinstance(bids, list) else None
                        )
                        out["first_snapshot_ask_levels"] = (
                            len(asks) if isinstance(asks, list) else None
                        )
                        first_snap_done = True

                if len(samples) < max_records:
                    # Compact sample for printing
                    sample = {
                        "topic": obj.get("topic"),
                        "type": obj.get("type"),
                        "ts": obj.get("ts"),
                        "cts": obj.get("cts"),
                    }
                    if isinstance(data, dict):
                        b = data.get("b") or []
                        a = data.get("a") or []
                        sample["data"] = {
                            "s": data.get("s"),
                            "u": data.get("u"),
                            "seq": data.get("seq"),
                            "b_levels": len(b) if isinstance(b, list) else None,
                            "a_levels": len(a) if isinstance(a, list) else None,
                            "b_head": b[:2] if isinstance(b, list) else None,
                            "a_head": a[:2] if isinstance(a, list) else None,
                        }
                    samples.append(sample)

            if i >= max_records - 1 and first_snap_done:
                # keep scanning a bit for max levels / delta
                pass

    out["fields_seen"] = sorted(fields)
    out["sample_records"] = samples
    if out["detected_format"] == "unknown":
        out["notes"].append("file empty or no parseable lines in scan window")
    return out


def process_day(
    session: requests.Session,
    *,
    symbol: str,
    day: str,
    out_root: Path,
    connect: float,
    read: float,
    max_retries: int,
    do_inspect: bool,
) -> DayResult:
    result = DayResult(symbol=symbol, date=day)
    day_dir = out_root / symbol / day
    day_dir.mkdir(parents=True, exist_ok=True)

    try:
        status, payload, err = list_files_for_day(
            session,
            symbol=symbol,
            day=day,
            connect=connect,
            read=read,
            max_retries=max_retries,
        )
        result.api_http_status = status
        if err or payload is None:
            result.status = "API_ERROR"
            result.error = err or "empty payload"
            return result

        entry = pick_day_entry(payload, symbol=symbol, day=day)
        if entry is None:
            result.available = False
            result.status = "NOT_AVAILABLE"
            result.notes.append(f"list={payload.get('result')}")
            return result

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
            return result

        zip_path = day_dir / result.filename
        need_download = True
        if zip_path.exists():
            ok, crc_ok, names, zerr = validate_zip(zip_path)
            result.zip_valid = ok
            result.zip_crc_valid = crc_ok
            result.downloaded_size = zip_path.stat().st_size
            if ok and crc_ok and (
                result.reported_size is None
                or result.downloaded_size == result.reported_size
            ):
                need_download = False
                result.notes.append("ZIP already present+valid; skip download")
            else:
                result.notes.append(
                    f"existing ZIP invalid ({zerr}); re-downloading"
                )
                zip_path.unlink(missing_ok=True)

        if need_download:
            logger.info("downloading %s -> %s", result.download_url, zip_path)
            result.downloaded_size = atomic_download(
                session,
                url=result.download_url,
                dest=zip_path,
                expected_size=result.reported_size,
                connect=connect,
                read=read,
                max_retries=max_retries,
            )
            ok, crc_ok, names, zerr = validate_zip(zip_path)
            result.zip_valid = ok
            result.zip_crc_valid = crc_ok
            if not ok or not crc_ok:
                result.status = "ZIP_INVALID"
                result.error = zerr
                return result
            result.notes.append(f"ZIP members: {names}")
        else:
            ok, crc_ok, names, zerr = validate_zip(zip_path)
            result.zip_valid = ok
            result.zip_crc_valid = crc_ok
            result.notes.append(f"ZIP members: {names}")

        # Extract (or skip if already complete)
        extracted_name, unc_size, all_names = extract_zip_safe(zip_path, day_dir)
        result.extracted = True
        result.extracted_filename = extracted_name
        result.uncompressed_size = unc_size
        if set(all_names) != {extracted_name}:
            result.notes.append(f"extracted members: {all_names}")

        if do_inspect:
            extracted_path = day_dir / extracted_name
            info = inspect_ob_file(extracted_path)
            result.detected_format = info["detected_format"]
            result.snapshot_seen = info["snapshot_seen"]
            result.delta_seen = info["delta_seen"]
            result.max_bid_levels = info["max_bid_levels"]
            result.max_ask_levels = info["max_ask_levels"]
            result.notes.append(f"inspect={json.dumps(info, ensure_ascii=True)}")
        else:
            # Cheap format tag from first line only
            extracted_path = day_dir / extracted_name
            with extracted_path.open("r", encoding="utf-8", errors="replace") as fh:
                first = fh.readline()
            if _looks_like_json_object(first):
                result.detected_format = "json_lines_ndjson"
            else:
                result.detected_format = "unknown_first_line"

        result.status = "OK"
        return result
    except Exception as exc:  # noqa: BLE001
        logger.exception("day failed %s %s", symbol, day)
        result.status = "FAILED"
        result.error = f"{type(exc).__name__}: {exc}"
        return result


def print_day_report(r: DayResult) -> None:
    print("\n" + "=" * 60)
    print(f"{r.symbol} {r.date}")
    print("=" * 60)
    for key in [
        "symbol",
        "date",
        "api_http_status",
        "available",
        "filename",
        "download_url",
        "reported_size",
        "downloaded_size",
        "zip_valid",
        "zip_crc_valid",
        "extracted",
        "extracted_filename",
        "uncompressed_size",
        "detected_format",
        "status",
        "error",
    ]:
        print(f"{key}: {getattr(r, key)}")
    if r.notes:
        print("notes:")
        for n in r.notes:
            if n.startswith("inspect="):
                continue
            print(f"  - {n}")


def print_summary_table(results: list[DayResult]) -> None:
    headers = [
        "symbol",
        "date",
        "available",
        "zip_mb",
        "extracted_mb",
        "format",
        "snapshot_seen",
        "delta_seen",
        "max_bid_levels",
        "max_ask_levels",
        "status",
    ]
    rows: list[list[str]] = []
    for r in results:
        zip_mb = (
            f"{(r.downloaded_size or 0) / (1024*1024):.1f}"
            if r.downloaded_size is not None
            else "-"
        )
        ext_mb = (
            f"{(r.uncompressed_size or 0) / (1024*1024):.1f}"
            if r.uncompressed_size is not None
            else "-"
        )
        rows.append(
            [
                r.symbol,
                r.date,
                str(r.available),
                zip_mb,
                ext_mb,
                r.detected_format or "-",
                str(r.snapshot_seen) if r.snapshot_seen is not None else "-",
                str(r.delta_seen) if r.delta_seen is not None else "-",
                str(r.max_bid_levels) if r.max_bid_levels is not None else "-",
                str(r.max_ask_levels) if r.max_ask_levels is not None else "-",
                r.status,
            ]
        )
    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(headers)]
    fmt = " | ".join(f"{{:{w}}}" for w in widths)
    print("\n" + fmt.format(*headers))
    print("-+-".join("-" * w for w in widths))
    for row in rows:
        print(fmt.format(*row))


def decide(results: list[DayResult]) -> str:
    if not results:
        return "HISTORICAL_OB_SMOKE_TEST_FAILED"
    statuses = [r.status for r in results]
    if all(s == "OK" for s in statuses):
        return "HISTORICAL_OB_SMOKE_TEST_OK"
    if any(s == "OK" for s in statuses):
        return "HISTORICAL_OB_SMOKE_TEST_PARTIAL"
    return "HISTORICAL_OB_SMOKE_TEST_FAILED"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    symbol = args.symbol.upper().strip()
    dates = [validate_date(d) for d in args.dates]
    out_root: Path = args.out_root
    if not out_root.is_absolute():
        out_root = (PROJECT_ROOT / out_root).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    print(f"project_root: {PROJECT_ROOT}")
    print(f"out_root: {out_root}")
    print(f"symbol: {symbol}")
    print(f"dates: {dates}")

    session = build_session()
    warmup_session(session, connect=args.connect_timeout, read=args.read_timeout)

    inspect_date = args.inspect_date
    if args.inspect and inspect_date is None and dates:
        # Prefer APT 2026-01-06 style if present, else first date
        inspect_date = "2026-01-06" if "2026-01-06" in dates else dates[0]

    results: list[DayResult] = []
    for day in dates:
        do_inspect = bool(args.inspect and day == inspect_date)
        r = process_day(
            session,
            symbol=symbol,
            day=day,
            out_root=out_root,
            connect=args.connect_timeout,
            read=args.read_timeout,
            max_retries=args.max_retries,
            do_inspect=do_inspect,
        )
        print_day_report(r)
        if do_inspect:
            # Pretty-print inspect payload from notes
            for n in r.notes:
                if n.startswith("inspect="):
                    info = json.loads(n[len("inspect=") :])
                    print("\n--- FORMAT INSPECT ---")
                    print(f"detected_format: {info.get('detected_format')}")
                    print(f"fields_seen: {info.get('fields_seen')}")
                    print(
                        "flags:",
                        {
                            k: info.get(k)
                            for k in (
                                "has_type",
                                "has_ts",
                                "has_cts",
                                "has_u",
                                "has_seq",
                                "snapshot_seen",
                                "delta_seen",
                                "first_snapshot_bid_levels",
                                "first_snapshot_ask_levels",
                                "max_bid_levels",
                                "max_ask_levels",
                            )
                        },
                    )
                    print("sample_records:")
                    print(json.dumps(info.get("sample_records"), indent=2))
                    if info.get("notes"):
                        print("inspect_notes:", info["notes"])
        results.append(r)

    print_summary_table(results)

    # Persist machine-readable summary next to data root (new file only)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    summary_dir = out_root / "_smoke_reports"
    summary_dir.mkdir(parents=True, exist_ok=True)
    summary_path = summary_dir / f"{symbol}_{stamp}.json"
    summary_path.write_text(
        json.dumps(
            {
                "project_root": str(PROJECT_ROOT),
                "out_root": str(out_root),
                "symbol": symbol,
                "dates": dates,
                "results": [r.to_row() for r in results],
                "decision_hint": decide(results),
                "generated_at_utc": stamp,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nwrote summary: {summary_path}")
    print(f"decision_hint: {decide(results)}")
    return 0 if all(r.status == "OK" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

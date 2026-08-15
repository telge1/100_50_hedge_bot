"""Shared Bybit historical data-download helpers (orderbook + trades).

Extracted from the validated orderbook downloader so both productId paths
reuse the same session/warmup/retry/.part/ZIP mechanics without duplication.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

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

logger = logging.getLogger("bybit_hist_download")


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
            sleep_s = min(60.0, 1.5**attempt)
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
    product_id: str,
    connect: float,
    read: float,
    max_retries: int,
    biz_type: str = "contract",
) -> tuple[int, dict[str, Any] | None, str | None]:
    """Call Bybit list-files for one calendar day.

    ``product_id`` must be ``orderbook`` or ``trade`` (contract historical).
    """
    params = {
        "bizType": biz_type,
        "productId": product_id,
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
        return (
            status,
            payload,
            f"ret_code={payload.get('ret_code')} msg={payload.get('ret_msg')}",
        )
    return status, payload, None


def list_file_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = payload.get("result") or {}
    items = result.get("list") or []
    return [it for it in items if isinstance(it, dict)]


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

        preferred = zip_path.name.replace(".zip", "")
        primary = next((n for n in names if Path(n).name == preferred), names[0])

        need_extract: list[str] = []
        for name in names:
            target = day_dir / Path(name).name
            info = zf.getinfo(name)
            if target.exists() and target.is_file() and target.stat().st_size == info.file_size:
                continue
            if target.exists():
                raise RuntimeError(
                    f"refusing to overwrite existing extracted file with size mismatch: {target}"
                )
            need_extract.append(name)

        if not need_extract:
            primary_name = Path(primary).name
            size = (day_dir / primary_name).stat().st_size
            return primary_name, size, [Path(n).name for n in names]

        with tempfile.TemporaryDirectory(prefix=".extract_", dir=day_dir) as tmp:
            tmp_dir = Path(tmp)
            for name in need_extract:
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


def is_gzip_file(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(2) == b"\x1f\x8b"
    except OSError:
        return False


def decompress_gzip_safe(gz_path: Path, day_dir: Path) -> tuple[str, int]:
    """Decompress .gz into day_dir; keep original archive. Returns (csv_name, size)."""
    import gzip

    day_dir.mkdir(parents=True, exist_ok=True)
    name = gz_path.name
    if name.endswith(".csv.gz"):
        out_name = name[: -len(".gz")]
    elif name.endswith(".gz"):
        out_name = name[: -len(".gz")]
        if not out_name.endswith(".csv"):
            out_name = out_name + ".csv"
    else:
        out_name = name + ".decompressed.csv"

    dest = day_dir / out_name
    if dest.exists() and dest.is_file() and dest.stat().st_size > 0:
        return out_name, dest.stat().st_size

    part = dest.with_suffix(dest.suffix + ".part")
    if part.exists():
        part.unlink()
    try:
        with gzip.open(gz_path, "rb") as src, part.open("wb") as out:
            shutil.copyfileobj(src, out, length=1024 * 1024)
            out.flush()
        part.replace(dest)
    except Exception:
        if part.exists():
            part.unlink(missing_ok=True)
        raise
    return out_name, dest.stat().st_size

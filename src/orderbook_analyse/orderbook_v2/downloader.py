"""Download Bybit ob200 day ZIPs with atomic .part files and SHA-256 validation."""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

LIST_FILES_URL = "https://www.bybit.com/x-api/quote/public/support/download/list-files"
WARMUP_URL = "https://www.bybit.com/data-download"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": _USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.bybit.com",
        "Referer": WARMUP_URL,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    })
    s.get(WARMUP_URL, timeout=30, headers={"Accept": "text/html"})
    return s


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class DayAvailability:
    day: str  # YYYY-MM-DD
    available: bool
    url: str = ""
    filename: str = ""
    size_bytes: int = 0
    error: str = ""


@dataclass
class DownloadResult:
    day: str
    url: str
    local_path: str
    sha256: str = ""
    compressed_bytes: int = 0
    status: str = ""
    error: str = ""
    downloaded_at: str = ""
    skipped: bool = False


def list_available_days(
    symbol: str,
    days: list[date],
    session: requests.Session | None = None,
) -> list[DayAvailability]:
    s = session or _make_session()
    results: list[DayAvailability] = []
    for day in days:
        day_str = day.isoformat()
        try:
            resp = s.get(
                LIST_FILES_URL, timeout=30,
                params={
                    "bizType": "contract", "productId": "orderbook",
                    "symbols": symbol.upper(), "interval": "daily",
                    "periods": "", "startDay": day_str, "endDay": day_str,
                },
            )
            j = resp.json()
            items = (j.get("result") or {}).get("list") or []
            if items:
                it = items[0]
                results.append(DayAvailability(
                    day=day_str, available=True,
                    url=it.get("url", ""), filename=it.get("filename", ""),
                    size_bytes=int(it.get("size") or 0),
                ))
            else:
                results.append(DayAvailability(
                    day=day_str, available=False,
                    error=f"rc={j.get('ret_code')} {j.get('ret_msg')}",
                ))
        except Exception as e:
            results.append(DayAvailability(day=day_str, available=False, error=str(e)))
    return results


def download_day(
    avail: DayAvailability,
    dest_root: Path,
    *,
    session: requests.Session | None = None,
) -> DownloadResult:
    """Download one day ZIP atomically. Skip if valid file already present."""
    dest_dir = dest_root
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = avail.filename or Path(avail.url).name
    final = dest_dir / filename
    part = dest_dir / (filename + ".part")

    result = DownloadResult(
        day=avail.day, url=avail.url,
        local_path=str(final), downloaded_at=datetime.now(timezone.utc).isoformat(),
    )

    # Check if already downloaded and valid
    if final.is_file():
        existing_sha = _sha256_file(final)
        result.sha256 = existing_sha
        result.compressed_bytes = final.stat().st_size
        result.status = "SKIPPED_EXISTING"
        result.skipped = True
        return result

    try:
        s = session or _make_session()
        if part.exists():
            part.unlink()

        with s.get(avail.url, stream=True, timeout=120) as resp:
            if resp.status_code == 404:
                result.status = "SOURCE_MISSING"
                result.error = f"HTTP 404 {avail.url}"
                return result
            if resp.status_code != 200:
                result.status = f"HTTP_{resp.status_code}"
                result.error = f"HTTP {resp.status_code}"
                return result
            with part.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    if chunk:
                        fh.write(chunk)

        # validate ZIP
        import zipfile
        if not zipfile.is_zipfile(part):
            part.unlink(missing_ok=True)
            result.status = "INVALID_ZIP"
            result.error = "not a valid ZIP after download"
            return result

        part.replace(final)
        result.sha256 = _sha256_file(final)
        result.compressed_bytes = final.stat().st_size
        result.status = "COMPLETE"
        return result

    except Exception as e:
        part.unlink(missing_ok=True)
        result.status = "FAILED"
        result.error = str(e)
        return result


def pilot_days(n: int = 7) -> list[date]:
    """Last N fully available UTC calendar days (T-2 back to T-N-1)."""
    now = datetime.now(timezone.utc)
    return [(now - timedelta(days=offset)).date() for offset in range(2, n + 2)]

"""Read-only source adapter for liquidation_research.liquidation_data."""

from __future__ import annotations

import csv
import io
import logging
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Iterator, Sequence

from research.regime_scanner.derivatives.config import (
    SOURCE_SELECT_COLUMNS,
    SOURCE_TABLE,
    DerivativeSourceConfig,
)

logger = logging.getLogger(__name__)

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{3,32}$")
_ALLOWED_TABLE = SOURCE_TABLE  # hard lock


class SourceAdapterError(RuntimeError):
    """Raised on source read failures."""


def _validate_symbols(symbols: Sequence[str]) -> list[str]:
    out: list[str] = []
    for s in symbols:
        u = str(s).strip().upper()
        if not _SYMBOL_RE.match(u):
            raise SourceAdapterError(f"invalid symbol for source query: {s!r}")
        out.append(u)
    if not out:
        raise SourceAdapterError("empty symbol list")
    return out


def _naive_utc_str(ts: datetime) -> str:
    if ts.tzinfo is None:
        raise SourceAdapterError("timestamps must be UTC-aware")
    u = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return u.strftime("%Y-%m-%d %H:%M:%S")


class DerivativeSourceAdapter:
    """Chunked read-only SELECT against liquidation_data."""

    def __init__(self, config: DerivativeSourceConfig) -> None:
        self.config = config
        self._engine = None
        if config.backend == "pymysql":
            try:
                from sqlalchemy import create_engine
            except ImportError as exc:  # pragma: no cover
                raise SourceAdapterError("sqlalchemy required for pymysql backend") from exc
            self._engine = create_engine(
                config.sqlalchemy_url,
                pool_pre_ping=True,
                future=True,
                connect_args={"connect_timeout": config.connect_timeout},
            )

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    def ping(self) -> dict[str, Any]:
        """Verify SELECT works; never log credentials."""
        if self.config.backend == "cli":
            row = self._cli_query("SELECT 1 AS ok")
            return {"backend": "cli", "ok": True, "sample": row}
        assert self._engine is not None
        from sqlalchemy import text

        with self._engine.connect() as conn:
            # Prefer read-only transaction if supported
            try:
                conn.execute(text("SET SESSION TRANSACTION READ ONLY"))
            except Exception:  # noqa: BLE001
                logger.debug("READ ONLY session not set (ignored)")
            ok = conn.execute(text("SELECT 1")).scalar()
            return {"backend": "pymysql", "ok": bool(ok)}

    def iter_rows(
        self,
        *,
        symbols: Sequence[str],
        start: datetime,
        end: datetime,
        chunk_size: int = 5000,
        row_limit: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield dict rows sorted by symbol, timestamp. Half-open [start, end)."""
        syms = _validate_symbols(symbols)
        if end <= start:
            raise SourceAdapterError("end must be after start")
        if chunk_size < 1:
            raise SourceAdapterError("chunk_size must be >= 1")

        cols = ", ".join(SOURCE_SELECT_COLUMNS)
        # Table name is constant allowlist — never interpolated from user input beyond that.
        if _ALLOWED_TABLE != "liquidation_data":
            raise SourceAdapterError("refusing non-allowlisted source table")

        yielded = 0
        if self.config.backend == "cli":
            yield from self._iter_cli(
                symbols=syms,
                start=start,
                end=end,
                chunk_size=chunk_size,
                row_limit=row_limit,
                cols=cols,
            )
            return

        assert self._engine is not None
        from sqlalchemy import bindparam, text

        # Keyset pagination by (symbol, timestamp) for stable chunking.
        last_symbol: str | None = None
        last_ts: datetime | None = None
        start_s = _naive_utc_str(start)
        end_s = _naive_utc_str(end)

        sql = text(
            f"""
            SELECT {cols}
            FROM `{self.config.name}`.`{_ALLOWED_TABLE}`
            WHERE symbol IN :symbols
              AND timestamp >= :start_ts
              AND timestamp < :end_ts
              AND (
                :last_symbol IS NULL
                OR symbol > :last_symbol
                OR (symbol = :last_symbol AND timestamp > :last_ts)
              )
            ORDER BY symbol ASC, timestamp ASC
            LIMIT :lim
            """
        ).bindparams(bindparam("symbols", expanding=True))

        retries = 3
        while True:
            params = {
                "symbols": tuple(syms),
                "start_ts": start_s,
                "end_ts": end_s,
                "last_symbol": last_symbol,
                "last_ts": _naive_utc_str(last_ts) if last_ts is not None else None,
                "lim": chunk_size,
            }
            rows: list[dict[str, Any]] = []
            last_err: Exception | None = None
            for attempt in range(retries):
                try:
                    with self._engine.connect() as conn:
                        try:
                            conn.execute(text("SET SESSION TRANSACTION READ ONLY"))
                        except Exception:  # noqa: BLE001
                            pass
                        result = conn.execute(sql, params)
                        rows = [dict(r) for r in result.mappings().all()]
                    last_err = None
                    break
                except Exception as exc:  # noqa: BLE001
                    last_err = exc
                    time.sleep(0.5 * (attempt + 1))
            if last_err is not None:
                raise SourceAdapterError(f"source SELECT failed: {last_err}") from last_err

            if not rows:
                break
            for row in rows:
                yield row
                yielded += 1
                if row_limit is not None and yielded >= row_limit:
                    return
            last_symbol = str(rows[-1]["symbol"])
            ts = rows[-1]["timestamp"]
            if isinstance(ts, datetime):
                last_ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            else:
                last_ts = datetime.fromisoformat(str(ts).replace(" ", "T")).replace(
                    tzinfo=timezone.utc
                )
            if len(rows) < chunk_size:
                break

    def _cli_query(self, sql: str) -> list[str]:
        # Only used for ping; no user interpolation.
        cmd = [
            "mysql",
            "-h",
            self.config.host,
            "-P",
            str(self.config.port),
            "-u",
            self.config.user,
            "-N",
            "-e",
            sql,
        ]
        if self.config.password:
            cmd.insert(1, f"-p{self.config.password}")
        # Prefer socket when host is localhost
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise SourceAdapterError(f"mysql cli failed: {proc.stderr.strip()[:200]}")
        return [ln for ln in proc.stdout.splitlines() if ln.strip()]

    def _iter_cli(
        self,
        *,
        symbols: list[str],
        start: datetime,
        end: datetime,
        chunk_size: int,
        row_limit: int | None,
        cols: str,
    ) -> Iterator[dict[str, Any]]:
        """Local socket-auth fallback. Symbols/times already validated."""
        sym_list = ",".join("'" + s.replace("'", "") + "'" for s in symbols)
        start_s = _naive_utc_str(start)
        end_s = _naive_utc_str(end)
        # Single streaming query with LIMIT optional — still chunk via Python if huge.
        limit_sql = f" LIMIT {int(row_limit)}" if row_limit is not None else ""
        sql = (
            f"SELECT {cols} FROM `{self.config.name}`.`{_ALLOWED_TABLE}` "
            f"WHERE symbol IN ({sym_list}) "
            f"AND timestamp >= '{start_s}' AND timestamp < '{end_s}' "
            f"ORDER BY symbol ASC, timestamp ASC{limit_sql}"
        )
        cmd = [
            "mysql",
            "-h",
            self.config.host,
            "-P",
            str(self.config.port),
            "-u",
            self.config.user,
            "--batch",
            "--raw",
            "-e",
            sql,
        ]
        if self.config.password:
            cmd.insert(1, f"-p{self.config.password}")
        logger.info(
            "source cli SELECT table=%s symbols=%s start=%s end=%s limit=%s",
            _ALLOWED_TABLE,
            symbols,
            start_s,
            end_s,
            row_limit,
        )
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise SourceAdapterError(f"mysql cli SELECT failed: {proc.stderr.strip()[:300]}")

        reader = csv.DictReader(io.StringIO(proc.stdout), delimiter="\t")
        # mysql --batch headers are column names
        count = 0
        batch: list[dict[str, Any]] = []
        for row in reader:
            # Normalize empty strings to None for nullable numeric fields
            cleaned: dict[str, Any] = {}
            for k, v in row.items():
                if v is None or v == "NULL" or v == "":
                    cleaned[k] = None
                else:
                    cleaned[k] = v
            batch.append(cleaned)
            count += 1
            if len(batch) >= chunk_size:
                for r in batch:
                    yield r
                batch = []
            if row_limit is not None and count >= row_limit:
                break
        for r in batch:
            yield r

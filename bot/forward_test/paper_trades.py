"""Paper-trade ledger for dry-run: track open signals to TP/SL and PnL."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bot.forward_test.config import BAR_LOOKBACK_HOURS
from bot.forward_test.logger import append_jsonl
from bot.forward_test.paths import ensure_import_paths


@dataclass
class PaperTrade:
    trade_id: str
    symbol: str
    side: str
    status: str  # open | closed
    rank: int
    cluster_id: str
    entry_ts: str
    entry_price: float
    stop_price: float
    tp_price: float
    opened_at: str
    exit_ts: str | None = None
    exit_price: float | None = None
    exit_reason: str | None = None  # tp | sl | failure
    exit_note: str | None = None
    pnl_pct: float | None = None
    dry_run: bool = True
    place_order: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PaperTrade":
        return cls(
            trade_id=str(raw["trade_id"]),
            symbol=str(raw["symbol"]).upper(),
            side=str(raw.get("side") or "short"),
            status=str(raw.get("status") or "open"),
            rank=int(raw.get("rank") or 0),
            cluster_id=str(raw.get("cluster_id") or ""),
            entry_ts=str(raw.get("entry_ts") or raw.get("short_entry_ts") or ""),
            entry_price=float(raw["entry_price"]),
            stop_price=float(raw["stop_price"]),
            tp_price=float(raw["tp_price"]),
            opened_at=str(raw.get("opened_at") or raw.get("ts") or ""),
            exit_ts=raw.get("exit_ts"),
            exit_price=(float(raw["exit_price"]) if raw.get("exit_price") is not None else None),
            exit_reason=raw.get("exit_reason"),
            exit_note=raw.get("exit_note"),
            pnl_pct=(float(raw["pnl_pct"]) if raw.get("pnl_pct") is not None else None),
            dry_run=bool(raw.get("dry_run", True)),
            place_order=bool(raw.get("place_order", False)),
        )


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _short_pnl_pct(entry: float, exit_px: float) -> float:
    if entry <= 0:
        return 0.0
    return (entry - exit_px) / entry * 100.0


class PaperLedger:
    """One open paper trade per symbol; persists open state and closed exits."""

    def __init__(
        self,
        *,
        open_path: Path,
        closed_log: Path,
        summary_path: Path,
    ) -> None:
        self.open_path = open_path
        self.closed_log = closed_log
        self.summary_path = summary_path
        self.open_by_symbol: dict[str, PaperTrade] = {}
        self._load_open()

    def _load_open(self) -> None:
        if not self.open_path.is_file():
            return
        try:
            raw = json.loads(self.open_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        rows = raw.get("open_trades") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            return
        for item in rows:
            if not isinstance(item, dict):
                continue
            trade = PaperTrade.from_dict(item)
            if trade.status == "open":
                self.open_by_symbol[trade.symbol] = trade

    def save_open(self) -> None:
        self.open_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": _iso(datetime.now(timezone.utc)),
            "open_trades": [t.to_dict() for t in self.open_by_symbol.values()],
        }
        self.open_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def has_open(self, symbol: str) -> bool:
        return symbol.upper() in self.open_by_symbol

    def bootstrap_from_signals_log(self, signals_log: Path) -> int:
        """DEPRECATED for live-parity: do not re-open historical signals.

        Restart recovery must come from ``open_trades.json`` only. Re-opening
        from ``signals.jsonl`` re-books already-closed entries (lookahead PnL).
        Kept as no-op so older runner calls do not break.
        """
        return 0

    def closed_trade_ids(self) -> set[str]:
        ids: set[str] = set()
        if not self.closed_log.is_file():
            return ids
        for line in self.closed_log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("trade_id"):
                ids.add(str(row["trade_id"]))
        return ids

    def open_from_signal(self, signal: dict[str, Any]) -> PaperTrade | None:
        """Open a paper trade from a dry signal. One open trade per symbol."""
        from bot.forward_test.config import MAX_ENTRY_LAG_MINUTES

        symbol = str(signal.get("symbol") or "").upper()
        if not symbol or self.has_open(symbol):
            return None
        entry_ts = str(signal.get("short_entry_ts") or signal.get("ts") or "")
        opened_raw = str(signal.get("ts") or _iso(datetime.now(timezone.utc)))
        entry_dt = _parse_ts(entry_ts)
        opened_dt = _parse_ts(opened_raw) or datetime.now(timezone.utc)
        if entry_dt is not None:
            lag_min = (opened_dt - entry_dt).total_seconds() / 60.0
            if lag_min > float(MAX_ENTRY_LAG_MINUTES):
                # Stale backfill — opening would enable exit lookahead on past bars.
                return None
        from bot.forward_test.fill_identity import fill_key_text

        trade_id = fill_key_text(
            symbol=symbol,
            side=str(signal.get("side") or ""),
            entry_ts=entry_ts,
            stop_price=signal.get("stop_price"),
            tp_price=signal.get("tp_price"),
        )
        if trade_id in self.closed_trade_ids():
            return None
        trade = PaperTrade(
            trade_id=trade_id,
            symbol=symbol,
            side=str(signal.get("side") or "short"),
            status="open",
            rank=int(signal.get("rank") or 0),
            cluster_id=str(signal.get("cluster_id") or ""),
            entry_ts=entry_ts,
            entry_price=float(signal["entry_price"]),
            stop_price=float(signal["stop_price"]),
            tp_price=float(signal["tp_price"]),
            opened_at=opened_raw,
        )
        self.open_by_symbol[symbol] = trade
        self.save_open()
        return trade

    def update_open_trades(self) -> list[PaperTrade]:
        """Check open paper trades against recent 5m bars; close on TP/SL."""
        ensure_import_paths()
        from ob_microstructure_breakout_bot.data.bars import load_5m_bars

        closed: list[PaperTrade] = []
        now = datetime.now(timezone.utc)
        for symbol, trade in list(self.open_by_symbol.items()):
            entry_ts = _parse_ts(trade.entry_ts) or _parse_ts(trade.opened_at)
            if entry_ts is None:
                continue
            try:
                bars = load_5m_bars(
                    symbol,
                    entry_ts - timedelta(minutes=5),
                    now,
                )
            except Exception:
                continue
            # No timeout: leave trade open until TP or SL is hit.
            # No lookahead: ignore exit candles that already finished before we
            # detected the trade (stale late opens must not book past TP/SL).
            opened_at = _parse_ts(trade.opened_at) or now
            opened_bar = opened_at.replace(
                minute=(opened_at.minute // 5) * 5,
                second=0,
                microsecond=0,
            )
            after = [b for b in bars if b.ts > entry_ts]
            exit_hit: tuple[str, float, datetime] | None = None
            for bar in after:
                bar_ts = (
                    bar.ts.replace(tzinfo=timezone.utc)
                    if bar.ts.tzinfo is None
                    else bar.ts.astimezone(timezone.utc)
                )
                # Bars before detection are already known history — do not use them.
                if bar_ts < opened_bar:
                    continue
                high = float(bar.high)
                low = float(bar.low)
                if trade.side == "short":
                    hit_sl = high >= float(trade.stop_price)
                    hit_tp = low <= float(trade.tp_price)
                    if hit_sl and hit_tp:
                        # Same-bar conflict: SL first (conservative, like backtester).
                        exit_hit = ("sl", float(trade.stop_price), bar.ts)
                        break
                    if hit_sl:
                        exit_hit = ("sl", float(trade.stop_price), bar.ts)
                        break
                    if hit_tp:
                        exit_hit = ("tp", float(trade.tp_price), bar.ts)
                        break
                else:
                    hit_sl = low <= float(trade.stop_price)
                    hit_tp = high >= float(trade.tp_price)
                    if hit_sl and hit_tp:
                        exit_hit = ("sl", float(trade.stop_price), bar.ts)
                        break
                    if hit_sl:
                        exit_hit = ("sl", float(trade.stop_price), bar.ts)
                        break
                    if hit_tp:
                        exit_hit = ("tp", float(trade.tp_price), bar.ts)
                        break

            if exit_hit is None and trade.side in {"short", "long"}:
                # Live reclaim exit: only at this moment, never on past bars.
                if trade.side == "short":
                    from bot.forward_test.failure_exit import evaluate_short_failure

                    snap = evaluate_short_failure(
                        symbol,
                        entry_price=float(trade.entry_price),
                        stop_price=float(trade.stop_price),
                        now=now,
                    )
                else:
                    from bot.forward_test.failure_exit import evaluate_long_failure

                    snap = evaluate_long_failure(
                        symbol,
                        entry_price=float(trade.entry_price),
                        stop_price=float(trade.stop_price),
                        now=now,
                    )
                if snap.get("ok") and snap.get("last_price") is not None:
                    trade.exit_note = (
                        f"near_entry ob={snap.get('ob_ratio')} "
                        f"delta={snap.get('delta_10m')} "
                        f"pool_strength={snap.get('pool_strength')} "
                        f"pool_dist_pct={snap.get('pool_dist_pct')}"
                    )
                    exit_hit = ("failure", float(snap["last_price"]), now)

            if exit_hit is None:
                # Still open — TP/SL or failure conditions not met.
                continue

            reason, exit_px, exit_ts = exit_hit
            if trade.side == "short":
                pnl = _short_pnl_pct(float(trade.entry_price), exit_px)
            else:
                pnl = (exit_px - float(trade.entry_price)) / float(trade.entry_price) * 100.0
            trade.status = "closed"
            trade.exit_reason = reason
            trade.exit_price = exit_px
            trade.exit_ts = _iso(exit_ts if exit_ts.tzinfo else exit_ts.replace(tzinfo=timezone.utc))
            trade.pnl_pct = float(pnl)
            append_jsonl(self.closed_log, trade.to_dict())
            closed.append(trade)
            self.open_by_symbol.pop(symbol, None)

        if closed:
            self.save_open()
            self.write_summary()
        return closed

    def write_summary(self) -> dict[str, Any]:
        closed_rows: list[dict[str, Any]] = []
        if self.closed_log.is_file():
            for line in self.closed_log.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    closed_rows.append(row)

        pnls = [float(r["pnl_pct"]) for r in closed_rows if r.get("pnl_pct") is not None]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        summary = {
            "updated_at": _iso(datetime.now(timezone.utc)),
            "open_trades": len(self.open_by_symbol),
            "closed_trades": len(closed_rows),
            "wins": len(wins),
            "losses": len(losses),
            "sum_pnl_pct": round(sum(pnls), 4) if pnls else 0.0,
            "avg_pnl_pct": round(sum(pnls) / len(pnls), 4) if pnls else None,
            "open_symbols": sorted(self.open_by_symbol),
        }
        self.summary_path.parent.mkdir(parents=True, exist_ok=True)
        self.summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        return summary

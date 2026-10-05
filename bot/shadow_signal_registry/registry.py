"""Shadow registry orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Literal

from bot.shadow_signal_registry.models import (
    BLOCK_REASON_NONE,
    LONG_STRATEGY,
    LONG_VERSION,
    SHORT_STRATEGY,
    SHORT_VERSION,
    empty_snapshot_row,
    make_signal_id,
)
from bot.shadow_signal_registry.rebuild import export_row, normalize_block_reason
from bot.shadow_signal_registry.outcome_tracker import TrackedShadow, incremental_step
from bot.shadow_signal_registry.ch_sync import flush_ch_sync_hook
from bot.shadow_signal_registry.dashboard_export import write_dashboard_export
from bot.shadow_signal_registry.snapshot import write_snapshot
from bot.shadow_signal_registry.store import RegistryStore
from bot.shadow_signal_registry.summary import build_summary

Side = Literal["long", "short"]


class ShadowRegistry:
    def __init__(self, side: Side) -> None:
        self.side = side
        self.store = RegistryStore(side)
        self._load()

    def _load(self) -> None:
        st = self.store.load_state()
        self.known_ids: set[str] = set(st.get("signal_ids") or [])
        self.snapshots: dict[str, dict[str, Any]] = dict(st.get("snapshots") or {})
        self.ch_last_hash: dict[str, str] = dict(st.get("ch_last_hash") or {})
        self.open: dict[str, TrackedShadow] = {}
        for sid, raw in (st.get("open_trades") or {}).items():
            self.open[sid] = TrackedShadow.from_dict(raw)
        for row in self.snapshots.values():
            normalize_block_reason(row)
        if not self.known_ids and self.store.events_path.is_file():
            for ev in self.store.load_events():
                sid = ev.get("signal_id")
                if sid:
                    self.known_ids.add(sid)

    def _persist(self) -> None:
        self.store.save_state(
            {
                "signal_ids": sorted(self.known_ids),
                "snapshots": self.snapshots,
                "open_trades": {k: v.to_dict() for k, v in self.open.items()},
                "ch_last_hash": self.ch_last_hash,
            }
        )

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _append(self, signal_id: str, event_type: str, symbol: str, payload: dict[str, Any]) -> None:
        self.store.append_event(
            {
                "event_time": self._now(),
                "signal_id": signal_id,
                "side": self.side.upper(),
                "symbol": symbol,
                "event_type": event_type,
                "payload": payload,
            }
        )

    def _ensure_row(self, signal_id: str) -> dict[str, Any]:
        if signal_id not in self.snapshots:
            self.snapshots[signal_id] = empty_snapshot_row(self.side, signal_id, self._now())
        return self.snapshots[signal_id]

    def register_long_geometry(self, sig: dict, *, detected_at: str | None = None) -> str | None:
        decision_time = sig["decision_time"]
        signal_id = make_signal_id("long", LONG_STRATEGY, LONG_VERSION, sig["symbol"], decision_time, sig["pool_id"])
        if signal_id in self.known_ids:
            return None
        self.known_ids.add(signal_id)
        row = self._ensure_row(signal_id)
        now = detected_at or self._now()
        row.update(
            {
                "detected_at": now,
                "decision_time": decision_time,
                "symbol": sig["symbol"],
                "pool_id": sig["pool_id"],
                "entry_price": float(sig["entry_price"]),
                "initial_sl": float(sig["stop"]),
                "active_sl": float(sig["stop"]),
                "tp": float(sig["tp"]),
                "m15_lower_2_age_h": sig.get("m15_lower_2_age_h"),
                "ladder24_pass": bool(sig.get("ladder_pass")),
                "be_trigger_pct": sig.get("be_trigger_pct", 1.0),
                "updated_at": now,
            }
        )
        self._append(signal_id, "SIGNAL_DETECTED", sig["symbol"], {"pool_id": sig["pool_id"]})
        if sig.get("ladder_pass"):
            row.update(
                {
                    "allowed": True,
                    "blocked": False,
                    "block_reason": BLOCK_REASON_NONE,
                    "hypothetical": False,
                    "signal_status": "ALLOWED",
                }
            )
            self._append(signal_id, "SIGNAL_ALLOWED", sig["symbol"], {})
        else:
            row.update(
                {
                    "allowed": False,
                    "blocked": True,
                    "block_reason": "LADDER24",
                    "raw_block_reason": "LADDER24",
                    "hypothetical": True,
                    "signal_status": "LADDER_BLOCKED",
                }
            )
            self._append(signal_id, "SIGNAL_BLOCKED", sig["symbol"], {"block_reason": "LADDER24"})
        self._open_trade(
            signal_id,
            sig["symbol"],
            decision_time,
            float(sig["entry_price"]),
            float(sig["stop"]),
            float(sig["tp"]),
            hypothetical=not bool(sig.get("ladder_pass")),
            pool_id=sig["pool_id"],
        )
        self._persist()
        return signal_id

    def register_short_product(self, product: dict) -> str | None:
        decision_time = product["decision_time"]
        signal_id = make_signal_id(
            "short", SHORT_STRATEGY, SHORT_VERSION, product["symbol"], decision_time, product["pool_id"]
        )
        if signal_id in self.known_ids:
            return None
        self.known_ids.add(signal_id)
        row = self._ensure_row(signal_id)
        now = product.get("detected_at") or self._now()
        row.update(
            {
                "detected_at": now,
                "decision_time": decision_time,
                "symbol": product["symbol"],
                "pool_id": product["pool_id"],
                "entry_price": float(product["entry_price"]),
                "initial_sl": float(product["stop"]),
                "active_sl": float(product["stop"]),
                "tp": float(product["tp"]),
                "allowed": product["allowed"],
                "blocked": product["blocked"],
                "block_reason": product.get("block_reason"),
                "raw_block_reason": product.get("raw_block_reason"),
                "hypothetical": product["hypothetical"],
                "signal_status": product["signal_status"],
                "e1r_state": product.get("e1r_state"),
                "e1r_block_reason": product.get("e1r_block_reason"),
                "floor_guard_state": product.get("floor_guard_state"),
                "updated_at": now,
            }
        )
        self._append(signal_id, "SIGNAL_DETECTED", product["symbol"], {})
        if product["allowed"]:
            self._append(signal_id, "SIGNAL_ALLOWED", product["symbol"], {})
        else:
            self._append(signal_id, "SIGNAL_BLOCKED", product["symbol"], {"block_reason": product.get("block_reason")})
        self._open_trade(
            signal_id,
            product["symbol"],
            decision_time,
            float(product["entry_price"]),
            float(product["stop"]),
            float(product["tp"]),
            hypothetical=product["hypothetical"],
            pool_id=product["pool_id"],
        )
        self._persist()
        return signal_id

    def _open_trade(
        self,
        signal_id: str,
        symbol: str,
        decision_time: str,
        entry: float,
        stop: float,
        tp: float,
        *,
        hypothetical: bool,
        pool_id: str,
    ) -> None:
        row = self.snapshots[signal_id]
        row["tracking_status"] = "OPEN"
        row["signal_status"] = row.get("signal_status") or "OPEN"
        self.open[signal_id] = TrackedShadow(
            signal_id=signal_id,
            symbol=symbol,
            side=self.side,
            decision_time=decision_time,
            entry_price=entry,
            initial_sl=stop,
            active_sl=stop,
            tp=tp,
            hypothetical=hypothetical,
            pool_id=pool_id,
        )
        self._append(
            signal_id,
            "SHADOW_OPENED",
            symbol,
            {"hypothetical": hypothetical, "row": export_row(row)},
        )

    def backfill_row(self, row: dict[str, Any]) -> None:
        sid = row["signal_id"]
        self.known_ids.add(sid)
        row["tracking_status"] = "CLOSED"
        row["backfilled"] = True
        normalize_block_reason(row)
        self.snapshots[sid] = row
        self._append(
            sid,
            "SHADOW_OPENED",
            row["symbol"],
            {"backfilled": True, "hypothetical": row.get("hypothetical"), "row": export_row(row)},
        )
        out = row.get("outcome") or "SL"
        self._append(
            sid,
            f"SHADOW_{out}",
            row["symbol"],
            {
                "pnl_pct": row.get("pnl_pct"),
                "outcome": out,
                "exit_time": row.get("exit_time"),
                "exit_price": row.get("exit_price"),
                "mae_pct": row.get("mae_pct"),
                "mfe_pct": row.get("mfe_pct"),
                "duration_min": row.get("duration_min"),
                "last_processed_1m": row.get("last_processed_1m"),
            },
        )
        self._persist()

    def sync_close_from_scanner(self, trade: Any) -> None:
        sid = trade.signal_id
        if sid not in self.snapshots:
            return
        row = self.snapshots[sid]
        outcome = trade.status
        row.update(
            {
                "tracking_status": "CLOSED",
                "outcome": outcome,
                "exit_time": trade.exit_time,
                "exit_price": trade.exit_price,
                "pnl_pct": trade.pnl_pct,
                "mae_pct": trade.mae_pct,
                "mfe_pct": trade.mfe_pct,
                "be_triggered": trade.be_triggered,
                "be_trigger_time": trade.be_trigger_time,
                "last_processed_1m": trade.last_processed_1m_close,
                "updated_at": self._now(),
                "signal_status": outcome,
            }
        )
        ev = f"SHADOW_{outcome}"
        if outcome == "INTRABAR_AMBIGUOUS":
            ev = "SHADOW_BE"
        self._append(sid, ev, trade.symbol, {"pnl_pct": trade.pnl_pct})
        self.open.pop(sid, None)
        self._persist()

    def flush_snapshots(self) -> None:
        for row in self.snapshots.values():
            normalize_block_reason(row)
        write_snapshot(self.side, list(self.snapshots.values()), self.store.snapshot_csv, self.store.snapshot_json)
        summary = build_summary(self.side, list(self.snapshots.values()))
        tmp = self.store.summary_json.with_suffix(".json.tmp")
        import json

        tmp.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        tmp.replace(self.store.summary_json)
        write_dashboard_export(self.side, list(self.snapshots.values()))
        flush_ch_sync_hook(self.side, list(self.snapshots.values()), self.ch_last_hash)
        self._persist()

    def sync_long_from_scanner(self, open_trades: list[Any]) -> None:
        for t in open_trades:
            sid = t.signal_id
            if sid not in self.snapshots:
                continue
            row = self.snapshots[sid]
            row.update(
                {
                    "mae_pct": round(t.mae_pct, 6),
                    "mfe_pct": round(t.mfe_pct, 6),
                    "last_processed_1m": t.last_processed_1m_close,
                    "be_triggered": t.be_triggered,
                    "be_trigger_time": t.be_trigger_time,
                    "active_sl": t.active_stop,
                    "tracking_status": "OPEN" if t.status == "OPEN" else "CLOSED",
                    "updated_at": self._now(),
                }
            )

    def track_open(
        self,
        load_1m: Callable[[str, datetime, datetime], list],
        now: datetime | None = None,
    ) -> None:
        if self.side != "short":
            return
        now = now or datetime.now(timezone.utc)
        for sid, trade in list(self.open.items()):
            if trade.status != "OPEN":
                continue
            events = incremental_step(trade, [], load_1m, now)
            row = self.snapshots[sid]
            row["mae_pct"] = round(trade.mae_pct, 6)
            row["mfe_pct"] = round(trade.mfe_pct, 6)
            row["last_processed_1m"] = trade.last_processed_1m
            row["be_triggered"] = trade.be_triggered
            row["be_trigger_time"] = trade.be_trigger_time
            row["active_sl"] = trade.active_sl
            row["updated_at"] = self._now()
            for ev in events:
                if ev == "BE_TRIGGERED":
                    self._append(sid, "BE_TRIGGERED", trade.symbol, {})
                    row["be_triggered"] = True
                    row["be_trigger_time"] = trade.be_trigger_time
                elif ev.startswith("SHADOW_"):
                    self._append(sid, ev, trade.symbol, {"pnl_pct": trade.pnl_pct, "outcome": trade.outcome})
                    row["tracking_status"] = "CLOSED"
                    row["outcome"] = trade.outcome
                    row["exit_time"] = trade.exit_time
                    row["exit_price"] = trade.exit_price
                    row["pnl_pct"] = trade.pnl_pct
                    row["duration_min"] = trade.duration_min
                    row["horizon_time"] = trade.horizon_time
                    row["signal_status"] = trade.outcome
                    del self.open[sid]
        self._persist()

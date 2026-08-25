"""GUI-free TRP workspace: drawings, overlays, indicator settings, persistence.

Ports MainWindow drawing/settings semantics without PySide. Host JS is the shell.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .boundary import PANE_COUNT, SUPPORTED_LAYOUTS, SUPPORTED_TIMEFRAMES
from .stoch_backtester import BACKTESTER_SOURCE, signal_to_position_spec
from .trp_import import load_trp

CLUSTER_SWEEP_SOURCE = "cluster_sweep_backtester"
CLUSTER_SWEEP_STRATEGY_ID = "cluster_sweep_ema_9_20_59"
EMA_DUAL_CROSS_SOURCE = "ema_dual_cross_backtester"
EMA_DUAL_CROSS_STRATEGY_ID = "ema_dual_cross_multisource_v1"

PANE_IDS = ("pane-0", "pane-1", "pane-2", "pane-3")
DEFAULT_PANE_TFS = {
    "pane-0": "1m",
    "pane-1": "5m",
    "pane-2": "15m",
    "pane-3": "1h",
}
DEFAULT_VOLUME_PROFILE = {
    "enabled": False,
    "rows": "auto",
    "display": "buy_sell",
    "poc": True,
    "value_area": True,
    "width": "normal",
    "volume_mode": "base",
    "mode": "visible_range",
}

DEFAULT_ORDERBOOK_PROFILE = {
    "enabled": False,
    "width": "normal",
    "mode": "visible_range",
}


def normalize_volume_profile(raw: dict | None) -> dict[str, Any]:
    src = dict(DEFAULT_VOLUME_PROFILE)
    if isinstance(raw, dict):
        if "enabled" in raw:
            src["enabled"] = bool(raw["enabled"])
        rows = str(raw.get("rows") or src["rows"]).strip().lower()
        if rows in {"auto", "24", "48", "72", "100"}:
            src["rows"] = "auto" if rows == "auto" else rows
        display = str(raw.get("display") or src["display"]).strip().lower()
        if display in {"buy_sell", "total", "delta"}:
            src["display"] = display
        if "poc" in raw:
            src["poc"] = bool(raw["poc"])
        if "value_area" in raw:
            src["value_area"] = bool(raw["value_area"])
        width = str(raw.get("width") or src["width"]).strip().lower()
        if width in {"compact", "normal", "wide"}:
            src["width"] = width
        mode = str(raw.get("volume_mode") or src["volume_mode"]).strip().lower()
        if mode in {"base", "quote"}:
            src["volume_mode"] = mode
        src["mode"] = "visible_range"
    return src


def normalize_orderbook_profile(raw: dict | None) -> dict[str, Any]:
    src = dict(DEFAULT_ORDERBOOK_PROFILE)
    if isinstance(raw, dict):
        if "enabled" in raw:
            src["enabled"] = bool(raw["enabled"])
        width = str(raw.get("width") or src["width"]).strip().lower()
        if width in {"compact", "normal", "wide"}:
            src["width"] = width
        mode = str(raw.get("mode") or src["mode"]).strip().lower()
        if mode in {"visible_range", "snapshot_at"}:
            src["mode"] = mode
        else:
            src["mode"] = "visible_range"
    return src


USER_DATA_DIR = Path(__file__).resolve().parent / "user_data"
DRAWINGS_PATH = USER_DATA_DIR / "drawings.json"
SETTINGS_PATH = USER_DATA_DIR / "indicator_settings.json"
TOOLS = (
    "select",
    "trend",
    "hline",
    "vline",
    "rectangle",
    "circle",
    "arrow",
    "measure",
    "long_position",
    "short_position",
)

_lock = threading.RLock()
_SESSION: Optional["ResearchWorkspace"] = None


def datetime_from_unix(unix: object) -> Optional[datetime]:
    try:
        value = float(unix)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if value != value:
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


class ResearchWorkspace:
    """In-process TRP drawing + settings session for the browser host."""

    def __init__(self) -> None:
        trp = load_trp()
        self._trp = trp
        self.drawings = trp["DrawingManager"]()
        self.overlays = trp["OverlayManager"]()
        self._draw_style = trp["DrawingStyle"]()
        self._drawing_tool = "select"
        self._selected_drawing_id: Optional[str] = None
        self._pending_draw = None
        self._overlay_test = False
        self._preview_anchor: Optional[dict[str, Any]] = None
        self.indicator_store = trp["IndicatorSettingsStore"](SETTINGS_PATH)
        self.indicator_store.load()
        self.ema_config = self.indicator_store.get_config(trp["EMA_OVERLAYS"])
        self.stoch_config = self.indicator_store.get_config(trp["STOCHASTIC"])
        self.lld_config = self.indicator_store.get_config(trp["LIQUIDITY_LOCATION"])
        self.volume_profile = dict(DEFAULT_VOLUME_PROFILE)
        self.orderbook_profile = dict(DEFAULT_ORDERBOOK_PROFILE)
        self._cluster_sweep_run: dict[str, Any] | None = None
        self._cluster_sweep_visible: bool = False
        self._cluster_sweep_event_index: int = 0
        self._ema_dual_cross_run: dict[str, Any] | None = None
        self._ema_dual_cross_visible: bool = False
        self._ema_dual_cross_event_index: int = 0
        self._load_persisted_drawings()

    def _load_persisted_drawings(self) -> None:
        trp = self._trp
        try:
            items = trp["load_drawings"](DRAWINGS_PATH)
            stored = trp["load_default_style"](DRAWINGS_PATH)
        except (OSError, ValueError, TypeError, KeyError):
            items = []
            stored = None
        if items:
            self.drawings.replace_drawings(items)
        if stored is not None:
            self._draw_style = stored

    def persist_drawings(self) -> None:
        try:
            USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
            self._trp["save_drawings"](
                DRAWINGS_PATH,
                self.drawings.get_drawings(),
                default_style=self._draw_style,
            )
        except OSError:
            pass

    def persist_settings(self) -> None:
        trp = self._trp
        try:
            USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
            self.indicator_store.set_config(trp["EMA_OVERLAYS"], self.ema_config)
            self.indicator_store.set_config(trp["STOCHASTIC"], self.stoch_config)
            self.indicator_store.set_config(trp["LIQUIDITY_LOCATION"], self.lld_config)
        except OSError:
            pass

    def snapshot(self) -> dict[str, Any]:
        trp = self._trp
        selected = None
        if self._selected_drawing_id and self._selected_drawing_id in self.drawings:
            selected = self.drawings.get_drawing(self._selected_drawing_id)
        style = selected.style if selected is not None else self._draw_style
        color = style.color if str(style.color).startswith("#") else trp["resolve_color"](style.color)
        return {
            "success": True,
            "tool": self._drawing_tool,
            "selected_id": self._selected_drawing_id,
            "pending": self._pending_draw is not None,
            "preview_anchor": self._preview_anchor,
            "overlay_test": self._overlay_test,
            "position_settings": bool(
                selected is not None and selected.drawing_type in trp["POSITION_TYPES"]
            ),
            "style": {
                "color": color,
                "width": float(style.width),
            },
            "ema": self.ema_config.to_dict(),
            "stochastic": self.stoch_config.to_dict(),
            "liquidity": self.lld_config.to_dict(),
            "volume_profile": dict(self.volume_profile),
            "orderbook_profile": dict(self.orderbook_profile),
            "license_notice": trp["LICENSE_NOTICE"],
            "tools": list(TOOLS),
            "layouts": list(SUPPORTED_LAYOUTS),
            "timeframes": list(SUPPORTED_TIMEFRAMES),
            "pane_ids": list(PANE_IDS),
            "default_pane_timeframes": dict(DEFAULT_PANE_TFS),
            "pane_count": dict(PANE_COUNT),
            "cluster_sweep": self._cluster_sweep_snapshot(),
            "ema_dual_cross": self._ema_dual_cross_snapshot(),
        }

    def _cluster_sweep_snapshot(self) -> dict[str, Any]:
        run = self._cluster_sweep_run or {}
        events = list(run.get("events") or [])
        n = len(events)
        idx = self._cluster_sweep_event_index if n else 0
        if n and idx >= n:
            idx = n - 1
        cur = events[idx] if n else None
        return {
            "strategy_id": CLUSTER_SWEEP_STRATEGY_ID,
            "loaded": bool(run),
            "visible": bool(self._cluster_sweep_visible),
            "meta": run.get("meta") or {},
            "coverage": run.get("coverage") or {},
            "n_events": n,
            "event_index": idx,
            "event": cur,
            "events": events,
            "manual_verdicts_allowed": [
                "MATCH",
                "FALSE_POSITIVE",
                "MISSED_EVENT",
                "WRONG_CLUSTER",
                "WRONG_TIMESTAMP",
                "LOOKAHEAD",
                "UNCLEAR",
                "INCONCLUSIVE_DATA",
            ],
        }

    def _ema_dual_cross_snapshot(self) -> dict[str, Any]:
        run = self._ema_dual_cross_run or {}
        cands = list(run.get("candidates") or [])
        n = len(cands)
        idx = self._ema_dual_cross_event_index if n else 0
        if n and idx >= n:
            idx = n - 1
        cur = cands[idx] if n else None
        return {
            "strategy_id": EMA_DUAL_CROSS_STRATEGY_ID,
            "loaded": bool(run),
            "visible": bool(self._ema_dual_cross_visible),
            "meta": run.get("meta") or {},
            "coverage": run.get("coverage") or {},
            "n_candidates": n,
            "candidate_index": idx,
            "candidate": cur,
            "candidates": cands,
        }

    def settings_defaults(self) -> dict[str, Any]:
        trp = self._trp
        return {
            "ema": trp["EmaOverlaysConfig"].defaults().to_dict(),
            "stochastic": trp["StochasticConfig"].defaults().to_dict(),
            "liquidity": trp["LiquidityLocationConfig"].defaults().to_dict(),
            "volume_profile": dict(DEFAULT_VOLUME_PROFILE),
            "orderbook_profile": dict(DEFAULT_ORDERBOOK_PROFILE),
        }

    def apply_settings(
        self,
        *,
        ema: dict | None = None,
        stochastic: dict | None = None,
        liquidity: dict | None = None,
        volume_profile: dict | None = None,
        orderbook_profile: dict | None = None,
    ) -> dict[str, Any]:
        trp = self._trp
        if ema is not None:
            self.ema_config = trp["EmaOverlaysConfig"].from_dict(ema)
        if stochastic is not None:
            enabled = bool(self.stoch_config.enabled)
            cfg = trp["StochasticConfig"].from_dict(stochastic)
            if "enabled" not in stochastic:
                cfg.enabled = enabled
            self.stoch_config = cfg
        if liquidity is not None:
            enabled = bool(self.lld_config.enabled)
            cfg = trp["LiquidityLocationConfig"].from_dict(liquidity)
            if "enabled" not in liquidity:
                cfg.enabled = enabled
            self.lld_config = cfg
        if volume_profile is not None:
            self.volume_profile = normalize_volume_profile(volume_profile)
        if orderbook_profile is not None:
            self.orderbook_profile = normalize_orderbook_profile(orderbook_profile)
        self.persist_settings()
        return self.snapshot()

    def set_indicator_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        if name == "stochastic":
            self.stoch_config.enabled = bool(enabled)
        elif name in {"liquidity", "liquidity_location", "lld"}:
            self.lld_config.enabled = bool(enabled)
        else:
            raise ValueError(f"unknown indicator {name}")
        self.persist_settings()
        return self.snapshot()

    def set_overlay_test(self, enabled: bool, symbol: str) -> dict[str, Any]:
        self._overlay_test = bool(enabled)
        self._reload_test_overlays(symbol)
        return self.snapshot()

    def _reload_test_overlays(self, symbol: str) -> None:
        trp = self._trp
        for oid in list(self.overlays.ids()):
            ov = self.overlays.get_overlay(oid)
            if trp["is_test_overlay"](ov):
                self.overlays.remove_overlay(oid)
        if not self._overlay_test:
            return
        for overlay in trp["build_test_overlays"](symbol):
            self.overlays.add_overlay(overlay)

    def set_drawing_tool(self, tool: str) -> dict[str, Any]:
        if tool not in TOOLS:
            tool = "select"
        self._drawing_tool = tool
        self._pending_draw = None
        self._preview_anchor = None
        return self.snapshot()

    def cancel_drawing(self) -> dict[str, Any]:
        self._pending_draw = None
        self._preview_anchor = None
        self._drawing_tool = "select"
        return self.snapshot()

    def cancel_preview(self) -> dict[str, Any]:
        self._pending_draw = None
        self._preview_anchor = None
        return self.snapshot()

    def delete_selected(self) -> dict[str, Any]:
        if not self._selected_drawing_id:
            return self.snapshot()
        if self._selected_drawing_id not in self.drawings:
            self._selected_drawing_id = None
            return self.snapshot()
        self.drawings.remove_drawing(self._selected_drawing_id)
        self._selected_drawing_id = None
        self.persist_drawings()
        return self.snapshot()

    def clear_drawings(self, symbol: str) -> dict[str, Any]:
        self.drawings.clear_drawings(symbol)
        self._selected_drawing_id = None
        self._pending_draw = None
        self._preview_anchor = None
        self.persist_drawings()
        return self.snapshot()

    def import_stoch_backtester(self, symbol: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Replace Stoch-Signale positions on this symbol with long/short tools."""
        trp = self._trp
        dc_replace = trp["dc_replace"]
        sym = str(symbol or "").strip().upper()
        existing = [
            d
            for d in self.drawings.get_drawings(sym, include_hidden=True)
            if (d.metadata or {}).get("origin") == BACKTESTER_SOURCE
            or (d.metadata or {}).get("source") == BACKTESTER_SOURCE
            or str(d.drawing_id).startswith("stoch-")
        ]
        for drawing in existing:
            self.drawings.remove_drawing(drawing.drawing_id)
        loaded = 0
        skipped = 0
        for row in rows or []:
            spec = signal_to_position_spec(row)
            if spec is None or spec["symbol"] != sym:
                skipped += 1
                continue
            factory = (
                trp["make_long_position"]
                if spec["drawing_type"] == "long_position"
                else trp["make_short_position"]
            )
            drawing = factory(
                symbol=sym,
                timestamp_a=spec["start"],
                price_a=spec["entry"],
                timestamp_b=spec["end"],
                price_b=spec["target"],
                created_on_timeframe=spec["timeframe"],
                timeframe_scope="all",
                drawing_id=spec["drawing_id"] or None,
                entry_price=spec["entry"],
                stop_price=spec["stop"],
                target_price=spec["target"],
            )
            drawing = dc_replace(
                drawing,
                metadata={
                    "origin": BACKTESTER_SOURCE,
                    "signal_id": spec["signal_id"],
                    "direction": spec["direction"],
                },
            )
            if spec["drawing_id"] and spec["drawing_id"] in self.drawings:
                self.drawings.remove_drawing(spec["drawing_id"])
            self.drawings.add_drawing(drawing)
            loaded += 1
        self.persist_drawings()
        snap = self.snapshot()
        snap["backtester"] = {
            "symbol": sym,
            "loaded": loaded,
            "skipped": skipped,
            "source": BACKTESTER_SOURCE,
        }
        return snap

    def _clear_cluster_sweep_overlays(self, symbol: str | None = None) -> int:
        removed = 0
        for oid in list(self.overlays.ids()):
            try:
                ov = self.overlays.get_overlay(oid)
            except KeyError:
                continue
            meta = getattr(ov, "metadata", None) or {}
            if meta.get("origin") != CLUSTER_SWEEP_SOURCE and meta.get("strategy_id") != CLUSTER_SWEEP_STRATEGY_ID:
                continue
            if symbol and ov.symbol != symbol:
                continue
            self.overlays.remove_overlay(oid)
            removed += 1
        return removed

    def store_cluster_sweep_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._cluster_sweep_run = {
            "meta": payload.get("meta") or {},
            "events": payload.get("events") or [],
            "markers": payload.get("markers") or [],
            "coverage": payload.get("coverage") or {},
        }
        self._cluster_sweep_event_index = 0
        sym = str((payload.get("meta") or {}).get("symbol") or "").upper()
        if sym:
            self._clear_cluster_sweep_overlays(sym)
        self._cluster_sweep_visible = False
        snap = self.snapshot()
        snap["backtester"] = {
            "strategy_id": CLUSTER_SWEEP_STRATEGY_ID,
            "symbol": sym,
            "loaded": len(payload.get("events") or []),
            "source": CLUSTER_SWEEP_SOURCE,
            "message": "Cluster Sweep run gespeichert — Backtester klicken zum Einblenden",
        }
        return snap

    def set_cluster_sweep_visible(self, visible: bool, symbol: str | None = None) -> dict[str, Any]:
        from .cluster_sweep_backtester import build_overlay_markers

        run = self._cluster_sweep_run or {}
        sym = str(symbol or (run.get("meta") or {}).get("symbol") or "").upper()
        self._clear_cluster_sweep_overlays(sym or None)
        self._cluster_sweep_visible = bool(visible)
        if self._cluster_sweep_visible and sym:
            existing = [
                d
                for d in self.drawings.get_drawings(sym, include_hidden=True)
                if (d.metadata or {}).get("origin") == BACKTESTER_SOURCE
                or str(d.drawing_id).startswith("stoch-")
            ]
            for drawing in existing:
                self.drawings.remove_drawing(drawing.drawing_id)
            markers = build_overlay_markers(list(run.get("markers") or []), symbol=sym)
            for ov in markers:
                if ov.overlay_id in self.overlays:
                    self.overlays.remove_overlay(ov.overlay_id)
                self.overlays.add_overlay(ov)
            self.persist_drawings()
        snap = self.snapshot()
        snap["backtester"] = {
            "strategy_id": CLUSTER_SWEEP_STRATEGY_ID,
            "symbol": sym,
            "visible": self._cluster_sweep_visible,
            "loaded": len(run.get("events") or []),
            "source": CLUSTER_SWEEP_SOURCE,
        }
        return snap

    def navigate_cluster_sweep_event(self, *, delta: int = 0, index: int | None = None) -> dict[str, Any]:
        run = self._cluster_sweep_run or {}
        events = list(run.get("events") or [])
        n = len(events)
        if not n:
            return self.snapshot()
        if index is not None:
            self._cluster_sweep_event_index = max(0, min(n - 1, int(index)))
        else:
            self._cluster_sweep_event_index = (self._cluster_sweep_event_index + int(delta)) % n
        return self.snapshot()

    def _clear_ema_dual_cross_overlays(self, symbol: str | None = None) -> int:
        removed = 0
        for oid in list(self.overlays.ids()):
            try:
                ov = self.overlays.get_overlay(oid)
            except KeyError:
                continue
            meta = getattr(ov, "metadata", None) or {}
            if meta.get("origin") != EMA_DUAL_CROSS_SOURCE and meta.get("strategy_id") != EMA_DUAL_CROSS_STRATEGY_ID:
                continue
            if symbol and ov.symbol != symbol:
                continue
            self.overlays.remove_overlay(oid)
            removed += 1
        return removed

    def store_ema_dual_cross_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._ema_dual_cross_run = {
            "meta": payload.get("meta") or {},
            "candidates": payload.get("candidates") or [],
            "rejected_ema_crosses": payload.get("rejected_ema_crosses") or [],
            "markers": payload.get("markers") or [],
            "coverage": payload.get("coverage") or {},
            "summary": payload.get("summary") or {},
            "policy": payload.get("policy") or {},
        }
        self._ema_dual_cross_event_index = 0
        sym = str((payload.get("meta") or {}).get("symbol") or "").upper()
        if sym:
            self._clear_ema_dual_cross_overlays(sym)
        self._ema_dual_cross_visible = False
        snap = self.snapshot()
        snap["backtester"] = {
            "strategy_id": EMA_DUAL_CROSS_STRATEGY_ID,
            "symbol": sym,
            "loaded": len(payload.get("candidates") or []),
            "source": EMA_DUAL_CROSS_SOURCE,
            "message": "EMA Dual Cross run gespeichert — Backtester klicken zum Einblenden",
        }
        return snap

    def set_ema_dual_cross_visible(self, visible: bool, symbol: str | None = None) -> dict[str, Any]:
        from .ema_dual_cross_backtester import build_overlay_markers

        run = self._ema_dual_cross_run or {}
        sym = str(symbol or (run.get("meta") or {}).get("symbol") or "").upper()
        self._clear_ema_dual_cross_overlays(sym or None)
        self._ema_dual_cross_visible = bool(visible)
        if self._ema_dual_cross_visible and sym:
            markers = build_overlay_markers(list(run.get("markers") or []), symbol=sym)
            for ov in markers:
                if ov.overlay_id in self.overlays:
                    self.overlays.remove_overlay(ov.overlay_id)
                self.overlays.add_overlay(ov)
            self.persist_drawings()
        snap = self.snapshot()
        snap["backtester"] = {
            "strategy_id": EMA_DUAL_CROSS_STRATEGY_ID,
            "symbol": sym,
            "visible": self._ema_dual_cross_visible,
            "loaded": len(run.get("candidates") or []),
            "source": EMA_DUAL_CROSS_SOURCE,
        }
        return snap

    def navigate_ema_dual_cross_candidate(self, *, delta: int = 0, index: int | None = None) -> dict[str, Any]:
        run = self._ema_dual_cross_run or {}
        cands = list(run.get("candidates") or [])
        n = len(cands)
        if not n:
            return self.snapshot()
        if index is not None:
            self._ema_dual_cross_event_index = max(0, min(n - 1, int(index)))
        else:
            self._ema_dual_cross_event_index = (self._ema_dual_cross_event_index + int(delta)) % n
        return self.snapshot()

    def clear_backtester_strategy(self, symbol: str, *, strategy_id: str | None = None) -> dict[str, Any]:
        sym = str(symbol or "").upper()
        sid = str(strategy_id or "")
        if not sid or sid == CLUSTER_SWEEP_STRATEGY_ID or sid == "cluster_sweep":
            self._clear_cluster_sweep_overlays(sym or None)
            if sid:
                self._cluster_sweep_visible = False
        if not sid or sid == EMA_DUAL_CROSS_STRATEGY_ID or sid == "ema_dual_cross":
            self._clear_ema_dual_cross_overlays(sym or None)
            if sid:
                self._ema_dual_cross_visible = False
        if not sid or sid.startswith("stoch") or sid == "wave_fade" or sid == BACKTESTER_SOURCE:
            if sym:
                existing = [
                    d
                    for d in self.drawings.get_drawings(sym, include_hidden=True)
                    if (d.metadata or {}).get("origin") == BACKTESTER_SOURCE
                    or str(d.drawing_id).startswith("stoch-")
                ]
                for drawing in existing:
                    self.drawings.remove_drawing(drawing.drawing_id)
                self.persist_drawings()
        return self.snapshot()

    def apply_style(self, *, color: Optional[str] = None, width: Optional[float] = None) -> dict[str, Any]:
        trp = self._trp
        dc_replace = trp["dc_replace"]
        sid = self._selected_drawing_id
        if sid and sid in self.drawings:
            drawing = self.drawings.get_drawing(sid)
            style = drawing.style
            if color is not None:
                style = dc_replace(style, color=color)
            if width is not None:
                style = dc_replace(style, width=float(width))
            self.drawings.update_drawing(dc_replace(drawing, style=style))
            self.persist_drawings()
            return self.snapshot()
        if color is not None:
            self._draw_style = dc_replace(self._draw_style, color=color)
        if width is not None:
            self._draw_style = dc_replace(self._draw_style, width=float(width))
        self.persist_drawings()
        return self.snapshot()

    def _snapshot_draw_style(self):
        s = self._draw_style
        return self._trp["DrawingStyle"](
            color=s.color,
            width=s.width,
            line_style=s.line_style,
            opacity=s.opacity,
            fill_opacity=s.fill_opacity,
            profit_color=s.profit_color,
            loss_color=s.loss_color,
            text_color=s.text_color,
            label_background=s.label_background,
        )

    def _set_preview(self, tool: str, ts: datetime, price: float) -> None:
        trp = self._trp
        self._preview_anchor = {
            "tool": tool,
            "time": trp["to_unix_seconds"](ts),
            "price": price,
            "color": trp["resolve_color"](self._draw_style.color)
            if not str(self._draw_style.color).startswith("#")
            else self._draw_style.color,
            "width": self._draw_style.width,
        }

    def _commit(self, drawing) -> None:
        self.drawings.add_drawing(drawing)
        self._selected_drawing_id = drawing.drawing_id
        self._drawing_tool = "select"
        self._pending_draw = None
        self._preview_anchor = None
        self.persist_drawings()

    def on_point(self, *, pane_id: str, timeframe: str, symbol: str, ts, price) -> dict[str, Any]:
        if self._drawing_tool == "select":
            return self.snapshot()
        trp = self._trp
        tool = self._drawing_tool
        price_f = float(price) if price is not None else None
        ts_dt = ts if isinstance(ts, datetime) else datetime_from_unix(ts)
        style = self._snapshot_draw_style()
        if tool == "hline":
            if price_f is None:
                return self.snapshot()
            self._commit(trp["make_hline"](symbol=symbol, price=price_f, created_on_timeframe=timeframe, style=style))
            return self.snapshot()
        if tool == "vline":
            if ts_dt is None:
                return self.snapshot()
            self._commit(
                trp["make_vline"](symbol=symbol, timestamp=ts_dt, created_on_timeframe=timeframe, style=style)
            )
            return self.snapshot()
        if not trp["is_two_point"](tool) or ts_dt is None or price_f is None:
            return self.snapshot()
        if self._pending_draw is None:
            self._pending_draw = (tool, pane_id, ts_dt, price_f, timeframe)
            self._set_preview(tool, ts_dt, price_f)
            return self.snapshot()
        ptool, _first_pane, ts_a, price_a, first_tf = self._pending_draw
        if ptool != tool:
            self._pending_draw = (tool, pane_id, ts_dt, price_f, timeframe)
            self._set_preview(tool, ts_dt, price_f)
            return self.snapshot()
        if ts_dt == ts_a and abs(price_f - price_a) < 1e-12:
            return self.snapshot()
        factories = {
            "trend": trp["make_trend"],
            "rectangle": trp["make_rectangle"],
            "measure": trp["make_measure"],
            "circle": trp["make_circle"],
            "arrow": trp["make_arrow"],
            "long_position": trp["make_long_position"],
            "short_position": trp["make_short_position"],
        }
        factory = factories.get(tool)
        if factory is None:
            return self.snapshot()
        drawing = factory(
            symbol=symbol,
            timestamp_a=ts_a,
            price_a=price_a,
            timestamp_b=ts_dt,
            price_b=price_f,
            created_on_timeframe=first_tf,
            style=style,
        )
        self._pending_draw = None
        self._preview_anchor = None
        self._commit(drawing)
        return self.snapshot()

    def on_hit(self, overlay_id: str) -> dict[str, Any]:
        did = overlay_id.rsplit(":", 1)[0] if overlay_id else ""
        if did not in self.drawings:
            return self.snapshot()
        self._selected_drawing_id = did
        return self.snapshot()

    def on_edit(self, overlay_id: str) -> dict[str, Any]:
        did = overlay_id.rsplit(":", 1)[0] if overlay_id else ""
        if did not in self.drawings:
            return self.snapshot()
        drawing = self.drawings.get_drawing(did)
        if drawing.drawing_type not in self._trp["POSITION_TYPES"]:
            return self.snapshot()
        self._selected_drawing_id = did
        return self.snapshot()

    def on_drag(
        self,
        overlay_id: str,
        ts,
        price,
        extra: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        trp = self._trp
        dc_replace = trp["dc_replace"]
        did = overlay_id.rsplit(":", 1)[0] if overlay_id else ""
        if did not in self.drawings:
            return self.snapshot()
        drawing = self.drawings.get_drawing(did)
        if drawing.locked:
            return self.snapshot()
        extra_dict = extra if isinstance(extra, dict) else {}
        ts_dt = ts if isinstance(ts, datetime) else datetime_from_unix(ts)
        if drawing.drawing_type == "hline" and price is not None:
            self.drawings.update_drawing(dc_replace(drawing, price=float(price)))
        elif drawing.drawing_type == "vline" and ts_dt is not None:
            self.drawings.update_drawing(dc_replace(drawing, timestamp=ts_dt))
        elif drawing.drawing_type in trp["POSITION_TYPES"]:
            st = datetime_from_unix(extra_dict.get("start_timestamp"))
            et = datetime_from_unix(extra_dict.get("end_timestamp"))
            entry = extra_dict.get("entry_price")
            stop = extra_dict.get("stop_price")
            target = extra_dict.get("target_price")
            if st is None or et is None or entry is None or stop is None or target is None:
                return self.snapshot()
            try:
                self.drawings.update_drawing(
                    dc_replace(
                        drawing,
                        start_timestamp=st,
                        end_timestamp=et,
                        entry_price=float(entry),
                        stop_price=float(stop),
                        target_price=float(target),
                    )
                )
            except ValueError:
                return self.snapshot()
        elif drawing.drawing_type in ("circle", "arrow"):
            st = datetime_from_unix(extra_dict.get("start_timestamp"))
            et = datetime_from_unix(extra_dict.get("end_timestamp"))
            sp = extra_dict.get("start_price")
            ep = extra_dict.get("end_price")
            if st is None or et is None or sp is None or ep is None:
                return self.snapshot()
            if st == et and abs(float(sp) - float(ep)) < 1e-12:
                return self.snapshot()
            self.drawings.update_drawing(
                dc_replace(
                    drawing,
                    start_timestamp=st,
                    end_timestamp=et,
                    start_price=float(sp),
                    end_price=float(ep),
                )
            )
        else:
            return self.snapshot()
        self._selected_drawing_id = did
        self.persist_drawings()
        return self.snapshot()

    def handle_event(self, body: dict[str, Any]) -> dict[str, Any]:
        kind = str(body.get("type") or "")
        if kind == "point":
            return self.on_point(
                pane_id=str(body.get("pane_id") or "pane-0"),
                timeframe=str(body.get("timeframe") or "5m"),
                symbol=str(body.get("symbol") or "").upper(),
                ts=body.get("time"),
                price=body.get("price"),
            )
        if kind == "hit":
            return self.on_hit(str(body.get("overlay_id") or ""))
        if kind == "edit":
            return self.on_edit(str(body.get("overlay_id") or ""))
        if kind == "drag":
            extra = {}
            for key in (
                "mode",
                "start_timestamp",
                "start_price",
                "end_timestamp",
                "end_price",
                "entry_price",
                "stop_price",
                "target_price",
                "position_notional",
            ):
                if key in body:
                    extra[key] = body[key]
            return self.on_drag(str(body.get("overlay_id") or ""), body.get("time"), body.get("price"), extra)
        if kind == "cancel_preview":
            return self.cancel_preview()
        if kind == "escape":
            return self.cancel_drawing()
        if kind == "delete":
            return self.delete_selected()
        return self.snapshot()

    def selected_position(self) -> Optional[dict[str, Any]]:
        sid = self._selected_drawing_id
        if not sid or sid not in self.drawings:
            return None
        drawing = self.drawings.get_drawing(sid)
        if drawing.drawing_type not in self._trp["POSITION_TYPES"]:
            return None
        return self._trp["serialize_drawing"](drawing)

    def update_position(self, body: dict[str, Any]) -> dict[str, Any]:
        trp = self._trp
        dc_replace = trp["dc_replace"]
        sid = str(body.get("drawing_id") or self._selected_drawing_id or "")
        if not sid or sid not in self.drawings:
            raise KeyError("unknown_drawing")
        drawing = self.drawings.get_drawing(sid)
        if drawing.drawing_type not in trp["POSITION_TYPES"]:
            raise ValueError("not_a_position")
        side = trp["side_from_type"](drawing.drawing_type)
        stats = trp["compute_position"](
            side,
            float(body.get("entry_price") or drawing.entry_price or 0),
            float(body.get("stop_price") or drawing.stop_price or 0),
            float(body.get("target_price") or drawing.target_price or 0),
            notional=float(body.get("position_notional") or drawing.position_notional or trp["DEFAULT_NOTIONAL"]),
        )
        style = drawing.style
        style = dc_replace(
            style,
            color=str(body.get("color") or style.color),
            width=float(body.get("width") if body.get("width") is not None else style.width),
            fill_opacity=float(
                body.get("fill_opacity") if body.get("fill_opacity") is not None else style.fill_opacity
            ),
            profit_color=str(body.get("profit_color") or style.profit_color or "#3dcc91"),
            loss_color=str(body.get("loss_color") or style.loss_color or "#f0616d"),
        )
        updated = dc_replace(
            drawing,
            entry_price=stats.entry,
            stop_price=stats.stop,
            target_price=stats.target,
            position_notional=stats.notional,
            default_risk_reward=float(
                body.get("default_risk_reward")
                if body.get("default_risk_reward") is not None
                else (drawing.default_risk_reward or trp["DEFAULT_RISK_REWARD"])
            ),
            style=style,
        )
        self.drawings.update_drawing(updated)
        self._selected_drawing_id = updated.drawing_id
        self.persist_drawings()
        snap = self.snapshot()
        snap["position"] = trp["serialize_drawing"](updated)
        return snap

    def composed_overlays(self, symbol: str, timeframe: str, lld_overlays: Optional[list] = None) -> list[dict]:
        trp = self._trp
        items = list(self.overlays.get_overlays(symbol, timeframe))
        if lld_overlays:
            items.extend(lld_overlays)
        items.extend(
            trp["compose_drawings"](
                self.drawings.get_drawings(symbol),
                symbol=symbol,
                timeframe=timeframe,
                selected_id=self._selected_drawing_id,
            )
        )
        payloads = trp["serialize_overlays"](items)
        for payload in payloads:
            payload["namespace"] = overlay_namespace(payload)
        return payloads

    def lld_objects(self, candles, config=None) -> tuple[list, dict, dict]:
        trp = self._trp
        cfg = config or self.lld_config
        empty_ema = {
            "fast": [],
            "slow": [],
            "fast_color": cfg.ema_fast_color,
            "slow_color": cfg.ema_slow_color,
            "fast_visible": False,
            "slow_visible": False,
        }
        if not cfg.enabled:
            return [], empty_ema, {"3": 0, "4-5": 0, "6+": 0}
        from .service import lld_config_for_timeframe

        tf = str(getattr(candles[0], "timeframe", "") or "") if candles else ""
        cfg = lld_config_for_timeframe(cfg, tf)
        result = trp["run_liquidity_location"](candles, cfg)
        clusters = None
        counts = {"3": 0, "4-5": 0, "6+": 0}
        if cfg.clusters_enabled:
            clusters = trp["cluster_pools"](
                result.pools,
                gap_pct=float(cfg.cluster_gap_pct),
                active_only=True,
            )
            shown = trp["filter_clusters"](clusters, minimum_pools=int(cfg.minimum_cluster_pools))
            counts = trp["cluster_bucket_counts"](shown)
        overlays = trp["compose_lld_overlays"](result, cfg, clusters=clusters)
        ema = trp["lld_ema_payload"](result, cfg)
        return overlays, ema, counts


def overlay_namespace(payload: dict[str, Any]) -> str:
    oid = str(payload.get("id") or "")
    meta = payload.get("metadata") or {}
    src = str(meta.get("source") or "")
    drawing_type = str(meta.get("drawing_type") or "")
    if oid.startswith("lld:") or oid.startswith("lldc:"):
        return "LLD"
    if meta.get("origin") == "cluster_sweep_backtester" or meta.get("strategy_id") == "cluster_sweep_ema_9_20_59" or oid.startswith("csw-"):
        return "CLUSTER_SWEEP"
    if src == "drawing" or meta.get("drawing_id"):
        if payload.get("type") == "position" or drawing_type in ("long_position", "short_position"):
            return "POSITION"
        return "USER_DRAWING"
    if src == "test" or oid.startswith("test:"):
        return "SYSTEM_TEST"
    if oid.startswith("__preview"):
        return "SYSTEM_PREVIEW"
    if "selected" in oid:
        return "SYSTEM_SELECTED"
    return "SYSTEM"


def get_workspace() -> ResearchWorkspace:
    global _SESSION
    with _lock:
        if _SESSION is None:
            _SESSION = ResearchWorkspace()
        return _SESSION


def reset_workspace_for_tests() -> ResearchWorkspace:
    global _SESSION
    with _lock:
        _SESSION = ResearchWorkspace()
        return _SESSION

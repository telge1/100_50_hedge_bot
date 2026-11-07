"""GUI-free TRP workspace: drawings, overlays, indicator settings, persistence.

Ports MainWindow drawing/settings semantics without PySide. Host JS is the shell.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .boundary import PANE_COUNT, SUPPORTED_LAYOUTS, SUPPORTED_TIMEFRAMES
from .exit_pool_backtester import (
    BACKTESTER_SOURCE as EXIT_POOL_SOURCE,
    DRAWING_PREFIX as EXIT_POOL_DRAWING_PREFIX,
    STRATEGY_ID as EXIT_POOL_STRATEGY_ID,
)
from .bt_d7_short_research_backtester import (
    BACKTESTER_SOURCE as BT_D7_SOURCE,
    DRAWING_PREFIX as BT_D7_DRAWING_PREFIX,
    HLINE_PREFIX as BT_D7_HLINE_PREFIX,
    ARROW_PREFIX as BT_D7_ARROW_PREFIX,
    STRATEGY_ID as BT_D7_STRATEGY_ID,
    build_drawing_specs,
)
from .bt_d23_setup_scanner_backtester import (
    BACKTESTER_SOURCE as BT_D23_SOURCE,
    DRAWING_PREFIX as BT_D23_DRAWING_PREFIX,
    STRATEGY_ID as BT_D23_STRATEGY_ID,
    filter_setups_viewport as filter_d23_setups_viewport,
    setup_to_position_spec as d23_setup_to_position_spec,
)
from .bt_d26_dynamic_selector_backtester import (
    BACKTESTER_SOURCE as BT_D26_SOURCE,
    DISPLAY_MODE_ALL_SETUPS as BT_D26_DISPLAY_ALL,
    DISPLAY_MODE_CURRENT_CANDIDATE as BT_D26_DISPLAY_CURRENT,
    DRAWING_PREFIX as BT_D26_DRAWING_PREFIX,
    MARKER_PREFIX as BT_D26_MARKER_PREFIX,
    STRATEGY_ID as BT_D26_STRATEGY_ID,
    build_clean_view_draw_specs as d26_build_clean_view_draw_specs,
    filter_setups_viewport as filter_d26_setups_viewport,
    setup_to_position_spec as d26_setup_to_position_spec,
)
from .bt_d46_chronological_backtester import (
    BACKTESTER_SOURCE as BT_D46_SOURCE,
    BOX_PREFIX as BT_D46_BOX_PREFIX,
    LABEL_PREFIX as BT_D46_LABEL_PREFIX,
    LINK_PREFIX as BT_D46_LINK_PREFIX,
    MARKER_PREFIX as BT_D46_MARKER_PREFIX,
    STRATEGY_ID as BT_D46_STRATEGY_ID,
    build_viewport_draw_specs as d46_build_viewport_draw_specs,
    position_detail_panel as d46_position_detail_panel,
)
from .frozen_month_research_overlay_v1 import (
    all_profiles as frozen_month_all_profiles,
    build_drawing_specs as frozen_month_build_drawing_specs,
    build_summary as frozen_month_build_summary,
    get_profile as frozen_month_get_profile,
    load_trade_ledger as frozen_month_load_trade_ledger,
    validate_ledger as frozen_month_validate_ledger,
)
from .short_main_v2_research_overlay_v1 import (
    BACKTESTER_SOURCE as SHORT_MAIN_V2_BACKTESTER_SOURCE,
    all_drawing_prefixes as short_main_v2_all_drawing_prefixes,
    build_drawing_specs as short_main_v2_build_drawing_specs,
    get_profile as short_main_v2_get_profile,
)
from .bt_d85_july_research_backtester import (
    BACKTESTER_SOURCE as BT_D85_SOURCE,
    BOX_PREFIX as BT_D85_BOX_PREFIX,
    LINK_PREFIX as BT_D85_LINK_PREFIX,
    STRATEGY_ID as BT_D85_STRATEGY_ID,
    build_drawing_specs as d85_build_drawing_specs,
    build_summary as d85_build_summary,
    load_trade_ledger as d85_load_trade_ledger,
    validate_ledger as d85_validate_ledger,
)
from .ema59_e1c_signal_overlay_v1 import (
    BACKTESTER_SOURCE as EMA59_E1C_SOURCE,
    DISPLAY_NAME as EMA59_E1C_DISPLAY_NAME,
    PAYLOAD_KIND as EMA59_E1C_PAYLOAD_KIND,
    STRATEGY_ID as EMA59_E1C_STRATEGY_ID,
    WORKSPACE_SNAP_KEY as EMA59_E1C_SNAP_KEY,
    build_marker_specs as ema59_e1c_build_marker_specs,
    build_overlay_markers as ema59_e1c_build_overlay_markers,
    build_summary as ema59_e1c_build_summary,
)
from .ema59_pilot_overlay_v1 import (
    SOURCE as EMA59_PILOT_SOURCE,
    PREFIX as EMA59_PILOT_PREFIX,
    STRATEGY_ID as EMA59_PILOT_STRATEGY_ID,
    build_drawing_specs as ema59_pilot_build_drawing_specs,
    load_overlay_bundle as load_ema59_pilot_overlay,
    overlay_path as ema59_pilot_overlay_path,
)
from .ema59_6m_current_backtests_v1 import (
    CATALOG as EMA59_CURRENT_BT_CATALOG,
    DEFAULT_BACKTEST_ID as EMA59_DEFAULT_CURRENT_BACKTEST_ID,
    Ema59CurrentBacktestSpec,
    JULY_2026_ZOOM,
    all_sources as ema59_current_backtest_all_sources,
    build_drawing_specs as ema59_multi_current_backtest_build_specs,
    get_spec as ema59_current_backtest_get_spec,
    is_current_backtest_drawing,
    list_catalog as ema59_current_backtests_list_catalog,
    load_overlay_bundle as load_ema59_catalog_backtest_bundle,
)
from .ema59_p11_legacy_archive_overlay_v1 import (
    BACKTEST_ID as EMA59_P11_ARCHIVE_BACKTEST_ID,
    DISPLAY_NAME as EMA59_P11_ARCHIVE_DISPLAY_NAME,
    SOURCE as EMA59_P11_ARCHIVE_SOURCE,
    PREFIX as EMA59_P11_ARCHIVE_PREFIX,
    load_overlay_bundle as load_ema59_p11_archive_overlay,
    overlay_path as ema59_p11_archive_overlay_path,
)
from .ema59_6m_band_overlay_v1 import (
    SOURCE as EMA59_6M_BAND_SOURCE,
    STRATEGY_ID as EMA59_6M_BAND_STRATEGY_ID,
    MARKER_PREFIX as EMA59_6M_BAND_MARKER_PREFIX,
    build_overlay_markers as ema59_6m_build_overlay_markers,
    build_summary as ema59_6m_build_summary,
    load_july_cross_rows as ema59_6m_load_cross_rows,
)
from .ema59_p9b_watch_diagnostic_overlay_v1 import (
    SOURCE as EMA59_P9B_SOURCE,
    STRATEGY_ID as EMA59_P9B_STRATEGY_ID,
    PREFIX as EMA59_P9B_PREFIX,
    artifacts_available as ema59_p9b_artifacts_available,
    build_episode_overlays as ema59_p9b_build_episode_overlays,
    cross_index_to_cross_id as ema59_p9b_cross_id,
)
from .ema59_p17_landscape_long_overlay_v1 import (
    SOURCE as EMA59_P17_LANDSCAPE_SOURCE,
    STRATEGY_ID as EMA59_P17_LANDSCAPE_STRATEGY_ID,
    PREFIX as EMA59_P17_LANDSCAPE_PREFIX,
    DISPLAY_NAME as EMA59_P17_LANDSCAPE_DISPLAY_NAME,
    artifacts_available as ema59_p17_artifacts_available,
    build_jump_list as ema59_p17_build_jump_list,
    build_overlay_objects as ema59_p17_build_overlay_objects,
    build_summary as ema59_p17_build_summary,
    load_chart_payload as ema59_p17_load_chart_payload,
)
from .ema59_p17_ab_diagnostic_overlay_v1 import (
    SOURCE as P17_AB_SOURCE,
    STRATEGY_ID as P17_AB_STRATEGY_ID,
    DRAWING_PREFIX as P17_AB_PREFIX,
    LAYER_DISPLAY_NAME as P17_AB_LAYER_NAME,
    build_drawing_specs as p17_ab_build_drawing_specs,
    build_overlay_bundle as p17_ab_build_overlay_bundle,
    expected_position_count as p17_ab_expected_position_count,
)
from .ema59_p17_ag_restored_long_overlay_v1 import (
    SOURCE as P17_AG_SOURCE,
    STRATEGY_ID as P17_AG_STRATEGY_ID,
    DRAWING_PREFIX as P17_AG_PREFIX,
    LAYER_DISPLAY_NAME as P17_AG_LAYER_NAME,
    build_overlay_bundle as p17_ag_build_overlay_bundle,
)
from .ema59_p17_aq_chart_overlay_v1 import (
    EXEC_PREFIX as P17_AQ_EXEC_PREFIX,
    EXEC_SOURCE as P17_AQ_EXEC_SOURCE,
    build_august_marker_specs as p17_aq_build_august_marker_specs,
    build_executed_drawing_bundle as p17_aq_build_executed_bundle,
    build_signal_catalog as p17_aq_build_signal_catalog,
    is_p17aq_overlay_id as p17_aq_is_overlay_id,
    marker_spec_to_overlay as p17_aq_marker_to_overlay,
    write_csv_exports as p17_aq_write_csv_exports,
)
from .stoch_backtester import BACKTESTER_SOURCE, signal_to_position_spec
from .trp_import import load_trp

CLUSTER_SWEEP_SOURCE = "cluster_sweep_backtester"
CLUSTER_SWEEP_STRATEGY_ID = "cluster_sweep_ema_9_20_59"
EMA_DUAL_CROSS_SOURCE = "ema_dual_cross_backtester"
EMA_DUAL_CROSS_STRATEGY_ID = "ema_dual_cross_multisource_v1"
EZM_SOURCE = "ezm_candidate_discovery"
EZM_STRATEGY_ID = "ema_zone_microstructure_confirmation_v1"
APS_SOURCE = "a_plus_pool_signal_scanner_v1"
APS_STRATEGY_ID = "a_plus_liquidity_pool_signal_scanner_v1"
NAP_SOURCE = "a_plus_nested_ask_pool_edge_short_v1"
NAP_STRATEGY_ID = "a_plus_nested_ask_pool_edge_short_v1"

LEGACY_RESEARCH_DRAWING_ORIGINS: frozenset[str] = frozenset(
    {
        EMA59_PILOT_SOURCE,
        EMA59_P11_ARCHIVE_SOURCE,
        "ema59_bt_p11_6m_current_backtest_v1",
        EMA59_E1C_SOURCE,
        EXIT_POOL_SOURCE,
        BT_D7_SOURCE,
        BT_D23_SOURCE,
        BT_D26_SOURCE,
        BT_D46_SOURCE,
        BT_D85_SOURCE,
        SHORT_MAIN_V2_BACKTESTER_SOURCE,
        BACKTESTER_SOURCE,
        CLUSTER_SWEEP_SOURCE,
        EMA_DUAL_CROSS_SOURCE,
        EZM_SOURCE,
        APS_SOURCE,
        NAP_SOURCE,
        "frozen_month_research_overlay_v1",
        "bt_d12_latest_backtester",
    }
)
CURRENT_RESEARCH_BACKTEST_ORIGINS: frozenset[str] = frozenset(
    (*ema59_current_backtest_all_sources(), P17_AB_SOURCE, P17_AG_SOURCE)
)

LEGACY_RESEARCH_DRAWING_PREFIXES: tuple[str, ...] = (
    EMA59_PILOT_PREFIX,
    "ema59p11-",
    EMA59_P11_ARCHIVE_PREFIX,
    EXIT_POOL_DRAWING_PREFIX,
    BT_D7_DRAWING_PREFIX,
    BT_D7_HLINE_PREFIX,
    BT_D7_ARROW_PREFIX,
    BT_D23_DRAWING_PREFIX,
    BT_D26_DRAWING_PREFIX,
    BT_D26_MARKER_PREFIX,
    BT_D46_BOX_PREFIX,
    BT_D46_MARKER_PREFIX,
    BT_D46_LINK_PREFIX,
    BT_D46_LABEL_PREFIX,
    BT_D85_BOX_PREFIX,
    BT_D85_LINK_PREFIX,
    "stoch-",
    "bt-frozen-",
    "bt-d12-",
    "smv2-",
)

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
    "mode": "snapshot_at",
}

DEFAULT_ORDERBOOK_LEVELS = {
    "enabled": False,
    "depth": 1000,  # research default: live OB1000; 0=FULL, 200=archive
    "mode": "aggregated",
    "scale": "sqrt",
    "width_px": 140,
}

DEFAULT_OPEN_INTEREST = {
    "enabled": False,
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
        # UI default is current snapshot only; legacy visible_range → snapshot_at.
        if mode in {"snapshot_at", "current"}:
            src["mode"] = "snapshot_at"
        elif mode in {"history", "visible_range"}:
            src["mode"] = "history"
        else:
            src["mode"] = "snapshot_at"
    return src


def normalize_orderbook_levels(raw: dict | None) -> dict[str, Any]:
    src = dict(DEFAULT_ORDERBOOK_LEVELS)
    if isinstance(raw, dict):
        if "enabled" in raw:
            src["enabled"] = bool(raw["enabled"])
        try:
            depth = int(raw.get("depth", src["depth"]))
        except (TypeError, ValueError):
            depth = int(src["depth"])
        if depth in {0, 200, 1000}:
            src["depth"] = depth
        mode = str(raw.get("mode") or src["mode"]).strip().lower()
        if mode in {"raw", "aggregated"}:
            src["mode"] = mode
        scale = str(raw.get("scale") or src["scale"]).strip().lower()
        if scale in {"sqrt", "linear", "log"}:
            src["scale"] = scale
        try:
            w = int(raw.get("width_px", src["width_px"]))
        except (TypeError, ValueError):
            w = int(src["width_px"])
        src["width_px"] = max(100, min(220, w))
    return src


def normalize_open_interest(raw: dict | None) -> dict[str, Any]:
    src = dict(DEFAULT_OPEN_INTEREST)
    if isinstance(raw, dict) and "enabled" in raw:
        src["enabled"] = bool(raw["enabled"])
    return src


USER_DATA_DIR = Path(__file__).resolve().parent / "user_data"
DRAWINGS_PATH = USER_DATA_DIR / "drawings.json"
SETTINGS_PATH = USER_DATA_DIR / "indicator_settings.json"
OVERLAY_FLAGS_PATH = USER_DATA_DIR / "research_overlay_flags.json"
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
        global _SESSION
        trp = load_trp()
        with _lock:
            _SESSION = self
        self._trp = trp
        self.drawings = trp["DrawingManager"]()
        self.overlays = trp["OverlayManager"]()
        self._draw_style = trp["DrawingStyle"]()
        self._drawing_tool = "select"
        self._selected_drawing_id: Optional[str] = None
        self._pending_draw = None
        self._overlay_test = False
        self._ema59_pilot_visible = False
        self._ema59_p11_archive_visible = False
        self._current_backtest_id: str | None = None
        self._ema59_6m_band_visible = False
        self._ema59_p17_landscape_visible = False
        self._p17_ab_diagnostic_visible = False
        self._p17_ab_filter_mode: str = "first_only"
        self._p17_ag_restored_visible = False
        self._p17aq_executed_visible = False
        self._p17aq_august_visible = False
        self._p17aq_catalog: dict[str, Any] | None = None
        self._ema59_p9b_cross_index: int | None = None
        self._research_legacy_mode = False
        self._preview_anchor: Optional[dict[str, Any]] = None
        self.indicator_store = trp["IndicatorSettingsStore"](SETTINGS_PATH)
        self.indicator_store.load()
        self.ema_config = self.indicator_store.get_config(trp["EMA_OVERLAYS"])
        self.stoch_config = self.indicator_store.get_config(trp["STOCHASTIC"])
        self.lld_config = self.indicator_store.get_config(trp["LIQUIDITY_LOCATION"])
        self.volume_profile = dict(DEFAULT_VOLUME_PROFILE)
        self.orderbook_profile = dict(DEFAULT_ORDERBOOK_PROFILE)
        self.orderbook_levels = dict(DEFAULT_ORDERBOOK_LEVELS)
        self.open_interest = self._load_open_interest()
        self._cluster_sweep_run: dict[str, Any] | None = None
        self._cluster_sweep_visible: bool = False
        self._cluster_sweep_event_index: int = 0
        self._ema_dual_cross_run: dict[str, Any] | None = None
        self._ema_dual_cross_visible: bool = False
        self._ema_dual_cross_event_index: int = 0
        self._ezm_run: dict[str, Any] | None = None
        self._ezm_visible: bool = False
        self._ezm_layer_mode: str = "both"
        self._pool_signals_run: dict[str, Any] | None = None
        self._nested_ask_pool_run: dict[str, Any] | None = None
        self._nested_ask_pool_visible: bool = False
        self._nested_ask_pool_show_rejected: bool = False
        self._pool_signals_display_mode: str = "off"
        self._bt_d23_catalog: dict[str, Any] | None = None
        self._bt_d23_rendered_ids: set[str] = set()
        self._bt_d26_catalog: dict[str, Any] | None = None
        self._bt_d26_rendered_ids: set[str] = set()
        self._bt_d46_catalog: dict[str, Any] | None = None
        self._bt_d46_rendered_ids: set[str] = set()
        self._ema59_e1c_visible: bool = False
        self._bt_d46_overlay_ids: set[str] = set()
        self._persist_migrated_current_backtest_flag = False
        self._init_complete = False
        self._load_persisted_drawings()
        self._load_overlay_visibility_flags()
        self._reconcile_overlay_layers(persist_if_changed=True)
        if self._p17_ag_restored_visible:
            self.set_p17_ag_restored_overlay(True, "DOGEUSDT")
        elif self._p17_ab_diagnostic_visible:
            self.set_p17_ab_diagnostic_overlay(
                True, "DOGEUSDT", filter_mode=self._p17_ab_filter_mode
            )
        elif self._current_backtest_id and not self._research_legacy_mode:
            self._ensure_current_backtest_layer_synced("DOGEUSDT", persist=True)
        self._init_complete = True

    def _load_overlay_visibility_flags(self) -> None:
        try:
            if not OVERLAY_FLAGS_PATH.is_file():
                return
            raw = json.loads(OVERLAY_FLAGS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._research_legacy_mode = bool(raw.get("research_legacy_mode"))
                self._ema59_pilot_visible = bool(raw.get("ema59_pilot_overlay"))
                self._ema59_6m_band_visible = bool(raw.get("ema59_6m_band_overlay"))
                self._ema59_p17_landscape_visible = bool(raw.get("ema59_p17_landscape_overlay"))
                self._p17_ab_diagnostic_visible = bool(raw.get("ema59_p17_ab_diagnostic_overlay"))
                mode = str(raw.get("p17_ab_filter_mode") or "first_only").strip()
                self._p17_ab_filter_mode = mode if mode in ("first_only", "all") else "first_only"
                self._p17_ag_restored_visible = bool(
                    raw.get("ema59_p17_ag_restored_overlay")
                )
                cb = raw.get("current_backtest_id")
                if cb == "ema59_6m_juli_sl_diagnose":
                    cb = None
                if cb == "ema59_6m_juli_watch_p12":
                    cb = EMA59_DEFAULT_CURRENT_BACKTEST_ID
                    self._persist_migrated_current_backtest_flag = True
                self._current_backtest_id = str(cb) if cb else None
                self._ema59_p11_archive_visible = bool(raw.get("ema59_p11_archive_overlay"))
                if not self._research_legacy_mode:
                    self._ema59_pilot_visible = False
                    self._overlay_test = False
        except (OSError, ValueError, TypeError):
            return

    def _save_overlay_visibility_flags(self) -> None:
        try:
            USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
            OVERLAY_FLAGS_PATH.write_text(
                json.dumps(
                    {
                        "research_legacy_mode": bool(self._research_legacy_mode),
                        "ema59_pilot_overlay": bool(self._ema59_pilot_visible),
                        "ema59_6m_band_overlay": bool(self._ema59_6m_band_visible),
                        "ema59_p17_landscape_overlay": bool(self._ema59_p17_landscape_visible),
                        "ema59_p17_ab_diagnostic_overlay": bool(self._p17_ab_diagnostic_visible),
                        "p17_ab_filter_mode": self._p17_ab_filter_mode,
                        "ema59_p17_ag_restored_overlay": bool(self._p17_ag_restored_visible),
                        "current_backtest_id": self._current_backtest_id,
                        "ema59_p11_archive_overlay": bool(self._ema59_p11_archive_visible),
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    def _is_legacy_research_drawing(self, drawing: Any) -> bool:
        did = str(getattr(drawing, "drawing_id", "") or "")
        meta = drawing.metadata or {}
        origin = str(meta.get("origin") or meta.get("source") or "")
        if origin in CURRENT_RESEARCH_BACKTEST_ORIGINS:
            return False
        if origin in LEGACY_RESEARCH_DRAWING_ORIGINS:
            return True
        if meta.get("strategy_id"):
            return True
        return any(did.startswith(p) for p in LEGACY_RESEARCH_DRAWING_PREFIXES)

    def _clear_legacy_archived_drawings(self, symbol: str | None = None) -> int:
        sym = str(symbol or "").strip().upper() if symbol else None
        removed = 0
        symbols = [sym] if sym else ["DOGEUSDT"]
        for s in symbols:
            for drawing in list(self.drawings.get_drawings(s, include_hidden=True)):
                if not self._is_legacy_research_drawing(drawing):
                    continue
                self.drawings.remove_drawing(drawing.drawing_id)
                removed += 1
        return removed

    def _reconcile_overlay_layers(self, *, persist_if_changed: bool = False) -> dict[str, int]:
        """Drop persisted strategy drawings when their overlay flag is off."""
        removed: dict[str, int] = {}
        if not self._research_legacy_mode:
            self._ema59_pilot_visible = False
            self._overlay_test = False
            self._ema59_e1c_visible = False
            n = self._clear_legacy_archived_drawings("DOGEUSDT")
            if n:
                removed["legacy_archived_drawings"] = n
            self._clear_ema59_e1c_signal_overlays("DOGEUSDT")
            self._reload_test_overlays("DOGEUSDT")
        if not self._ema59_pilot_visible:
            n = self._clear_ema59_pilot_drawings("DOGEUSDT")
            if n:
                removed["ema59_pilot_drawings"] = n
        if not self._ema59_6m_band_visible:
            n = self._clear_ema59_6m_band_overlays("DOGEUSDT")
            if n:
                removed["ema59_6m_band_markers"] = n
            self._ema59_p9b_cross_index = None
            n2 = self._clear_ema59_p9b_overlays("DOGEUSDT")
            if n2:
                removed["ema59_p9b_diagnostic"] = n2
        if not self._ema59_p17_landscape_visible:
            n3 = self._clear_ema59_p17_landscape_overlays("DOGEUSDT")
            if n3:
                removed["ema59_p17_landscape"] = n3
        if not self._p17_ab_diagnostic_visible:
            n_ab = self._clear_p17_ab_diagnostic_drawings("DOGEUSDT")
            if n_ab:
                removed["p17_ab_diagnostic"] = n_ab
        if not self._p17_ag_restored_visible:
            n_ag = self._clear_p17_ag_restored_drawings("DOGEUSDT")
            if n_ag:
                removed["p17_ag_restored"] = n_ag
        if self._p17_ag_restored_visible:
            self._p17_ab_diagnostic_visible = False
            n_ab = self._clear_p17_ab_diagnostic_drawings("DOGEUSDT")
            if n_ab:
                removed["p17_ab_suppressed_for_p17ag"] = n_ab
            n_p16 = self._clear_ema59_current_backtest_drawings("DOGEUSDT")
            if n_p16:
                removed["ema59_current_backtest_suppressed_for_p17ag"] = n_p16
            if self._count_p17_ag_position_drawings("DOGEUSDT") != 4:
                self.set_p17_ag_restored_overlay(True, "DOGEUSDT")
                removed["p17_ag_restored_resync"] = 1
        elif self._p17_ab_diagnostic_visible:
            n_p16 = self._clear_ema59_current_backtest_drawings("DOGEUSDT")
            if n_p16:
                removed["ema59_current_backtest_suppressed_for_p17ab"] = n_p16
            expected = p17_ab_expected_position_count(self._p17_ab_filter_mode)
            loaded = self._count_p17_ab_position_drawings("DOGEUSDT")
            if expected and loaded != expected:
                self.set_p17_ab_diagnostic_overlay(
                    True, "DOGEUSDT", filter_mode=self._p17_ab_filter_mode
                )
                removed["p17_ab_diagnostic_resync"] = 1
        elif not self._current_backtest_id:
            n = self._clear_ema59_current_backtest_drawings("DOGEUSDT")
            if n:
                removed["ema59_current_backtest"] = n
        elif not self._research_legacy_mode:
            if self._ensure_current_backtest_layer_synced("DOGEUSDT", persist=persist_if_changed):
                removed["ema59_current_backtest_resync"] = 1
        if not self._ema59_p11_archive_visible:
            n = self._clear_ema59_p11_archive_drawings("DOGEUSDT")
            if n:
                removed["ema59_p11_archive"] = n
        if not self._ema59_e1c_visible:
            n = self._clear_ema59_e1c_signal_overlays("DOGEUSDT")
            if n:
                removed["ema59_e1c_markers"] = n
        if removed and persist_if_changed:
            self.persist_drawings()
        return removed

    def audit_chart_layers(self, symbol: str = "DOGEUSDT") -> dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        drawings_by_origin: dict[str, int] = {}
        drawings_by_type: dict[str, int] = {}
        for drawing in self.drawings.get_drawings(sym, include_hidden=True):
            meta = drawing.metadata or {}
            origin = str(meta.get("origin") or meta.get("source") or "unknown")
            drawings_by_origin[origin] = drawings_by_origin.get(origin, 0) + 1
            dt = str(getattr(drawing, "drawing_type", "") or "unknown")
            drawings_by_type[dt] = drawings_by_type.get(dt, 0) + 1
        overlays_by_origin: dict[str, int] = {}
        for tf in ("1m", "5m", "15m", "30m", "1h", "4h"):
            for ov in self.overlays.get_overlays(sym, tf):
                meta = getattr(ov, "metadata", None) or {}
                origin = str(meta.get("origin") or meta.get("strategy_id") or "unknown")
                overlays_by_origin[origin] = overlays_by_origin.get(origin, 0) + 1
        bt = self.snapshot().get("backtester") or {}
        return {
            "symbol": sym,
            "visibility_flags": {
                "research_legacy_mode": bool(self._research_legacy_mode),
                "ema59_pilot_overlay": bool(self._ema59_pilot_visible),
                "ema59_6m_band_overlay": bool(self._ema59_6m_band_visible),
                "ema59_e1c_july_backtester": bool(self._ema59_e1c_visible),
                "overlay_test": bool(self._overlay_test),
                "backtester_strategy_id": bt.get("strategy_id"),
                "backtester_visible": bt.get("visible"),
            },
            "drawings_by_origin": drawings_by_origin,
            "drawings_by_type": drawings_by_type,
            "overlays_by_origin": overlays_by_origin,
            "known_sources": [
                {
                    "origin": EMA59_PILOT_SOURCE,
                    "ui_control": "checkbox EMA59 July Pilot",
                    "kind": "long_position + vline drawings (persisted)",
                },
                {
                    "origin": EMA59_6M_BAND_SOURCE,
                    "ui_control": "checkbox EMA59 6M + −2σ",
                    "kind": "OverlayMarker only (session)",
                },
                {
                    "origin": EMA59_P9B_SOURCE,
                    "ui_control": "6M Cross dropdown (WATCH diagnose)",
                    "kind": "OverlayMarker + OverlayLine (session, one episode)",
                },
                {
                    "origin": EMA59_E1C_SOURCE,
                    "ui_control": "Backtester EMA59 −2σ Signale — Juli 2026",
                    "kind": "OverlayMarker (session)",
                },
            ],
        }

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
        self._persist_open_interest()

    def _load_open_interest(self) -> dict[str, Any]:
        try:
            if not SETTINGS_PATH.is_file():
                return dict(DEFAULT_OPEN_INTEREST)
            raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return dict(DEFAULT_OPEN_INTEREST)
        if not isinstance(raw, dict):
            return dict(DEFAULT_OPEN_INTEREST)
        return normalize_open_interest(raw.get("open_interest"))

    def _persist_open_interest(self) -> None:
        try:
            USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
            if SETTINGS_PATH.is_file():
                raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raw = {"version": 1, "indicators": {}}
            else:
                raw = {"version": 1, "indicators": {}}
            raw["open_interest"] = dict(self.open_interest)
            SETTINGS_PATH.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        except (OSError, ValueError, TypeError):
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
            "ema59_pilot_overlay": self._ema59_pilot_visible,
            "ema59_6m_band_overlay": self._ema59_6m_band_visible,
            "ema59_p17_landscape_overlay": self._ema59_p17_landscape_visible,
            "p17_ab_diagnostic_overlay": self._p17_ab_diagnostic_visible,
            "p17_ab_filter_mode": self._p17_ab_filter_mode,
            "p17_ab_layer_name": P17_AB_LAYER_NAME,
            "p17_ag_restored_overlay": self._p17_ag_restored_visible,
            "p17_ag_layer_name": P17_AG_LAYER_NAME,
            "p17aq": self._p17aq_snapshot(),
            "ema59_p9b_cross_index": self._ema59_p9b_cross_index,
            "current_backtest_id": self._current_backtest_id,
            "current_backtests_catalog": [
                {
                    "id": row["backtest_id"],
                    "label": row["label"],
                    "research_disclaimer": "FORSCHUNG / NICHT VALIDIERTE STRATEGIE",
                    "trade_count_hint": row.get("trade_count_hint"),
                    "is_default": row.get("is_default"),
                }
                for row in ema59_current_backtests_list_catalog()
            ],
            "ema59_p11_archive_overlay": self._ema59_p11_archive_visible,
            "research_legacy_mode": self._research_legacy_mode,
            "position_settings": bool(
                selected is not None and selected.drawing_type in trp["POSITION_TYPES"]
            ),
            "extend_right": self._extend_right_ui_state(selected),
            "style": {
                "color": color,
                "width": float(style.width),
            },
            "ema": self.ema_config.to_dict(),
            "stochastic": self.stoch_config.to_dict(),
            "open_interest": dict(self.open_interest),
            "liquidity": self.lld_config.to_dict(),
            "volume_profile": dict(self.volume_profile),
            "orderbook_profile": dict(self.orderbook_profile),
            "orderbook_levels": dict(self.orderbook_levels),
            "license_notice": trp["LICENSE_NOTICE"],
            "tools": list(TOOLS),
            "layouts": list(SUPPORTED_LAYOUTS),
            "timeframes": list(SUPPORTED_TIMEFRAMES),
            "pane_ids": list(PANE_IDS),
            "default_pane_timeframes": dict(DEFAULT_PANE_TFS),
            "pane_count": dict(PANE_COUNT),
            "cluster_sweep": self._cluster_sweep_snapshot(),
            "ema_dual_cross": self._ema_dual_cross_snapshot(),
            "ezm": self._ezm_snapshot(),
            "pool_signals": self._pool_signals_snapshot(),
            "nested_ask_pool": self._nested_ask_pool_snapshot(),
            **(self._scanner_settings_meta() if self._init_complete else {}),
        }

    def _scanner_settings_meta(self) -> dict[str, Any]:
        from .scanner_settings_guard_v1 import workspace_frozen_engine_parity

        return workspace_frozen_engine_parity()

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

    def _ezm_snapshot(self) -> dict[str, Any]:
        run = self._ezm_run or {}
        cands = list(run.get("candidates") or [])
        setup = list(run.get("ema_setup_events") or [])
        micro = list(run.get("microstructure_confirmation_events") or [])
        layer_mode = str(self._ezm_layer_mode or "both")
        show_setup, show_micro = self._ezm_layer_flags(layer_mode)
        from .ezm_backtester import research_layers_to_marker_specs

        active_specs = research_layers_to_marker_specs(
            ema_setup_rows=setup,
            micro_rows=micro or cands,
            show_ema_setup=show_setup,
            show_microstructure=show_micro,
        )
        setup_n = len([m for m in active_specs if m.get("layer") == "ema_setup"])
        micro_n = len([m for m in active_specs if m.get("layer") == "microstructure_confirmation"])
        return {
            "strategy_id": EZM_STRATEGY_ID,
            "run_intent": "candidate_discovery",
            "loaded": bool(run),
            "visible": bool(self._ezm_visible),
            "layer_mode": layer_mode,
            "meta": run.get("meta") or {},
            "coverage": run.get("coverage") or {},
            "summary": run.get("summary") or {},
            "n_candidates": len(cands),
            "n_ema_setup_events": len(setup),
            "n_micro_events": len(micro),
            "n_markers": len(active_specs) if self._ezm_visible else 0,
            "n_setup_markers": setup_n if self._ezm_visible else 0,
            "n_micro_markers": micro_n if self._ezm_visible else 0,
            "n_markers_total": len(active_specs),
        }

    @staticmethod
    def _normalize_ezm_layer_mode(mode: str | None) -> str:
        m = str(mode or "both").strip().lower()
        if m in {"ema", "ema_only", "setup", "ema_setup"}:
            return "ema_only"
        if m in {"micro", "micro_only", "microstructure", "microstructure_only"}:
            return "micro_only"
        return "both"

    @staticmethod
    def _ezm_layer_flags(layer_mode: str) -> tuple[bool, bool]:
        mode = ResearchWorkspace._normalize_ezm_layer_mode(layer_mode)
        if mode == "ema_only":
            return True, False
        if mode == "micro_only":
            return False, True
        return True, True

    def _ezm_marker_specs(self, run: dict[str, Any]) -> list[dict[str, Any]]:
        from .ezm_backtester import research_layers_to_marker_specs

        setup = list(run.get("ema_setup_events") or [])
        micro = list(run.get("microstructure_confirmation_events") or [])
        if not micro:
            micro = list(run.get("candidates") or [])
        show_setup, show_micro = self._ezm_layer_flags(self._ezm_layer_mode)
        return research_layers_to_marker_specs(
            ema_setup_rows=setup,
            micro_rows=micro,
            show_ema_setup=show_setup,
            show_microstructure=show_micro,
        )

    def settings_defaults(self) -> dict[str, Any]:
        trp = self._trp
        return {
            "ema": trp["EmaOverlaysConfig"].defaults().to_dict(),
            "stochastic": trp["StochasticConfig"].defaults().to_dict(),
            "open_interest": dict(DEFAULT_OPEN_INTEREST),
            "liquidity": trp["LiquidityLocationConfig"].defaults().to_dict(),
            "volume_profile": dict(DEFAULT_VOLUME_PROFILE),
            "orderbook_profile": dict(DEFAULT_ORDERBOOK_PROFILE),
            "orderbook_levels": dict(DEFAULT_ORDERBOOK_LEVELS),
        }

    def apply_settings(
        self,
        *,
        ema: dict | None = None,
        stochastic: dict | None = None,
        open_interest: dict | None = None,
        liquidity: dict | None = None,
        volume_profile: dict | None = None,
        orderbook_profile: dict | None = None,
        orderbook_levels: dict | None = None,
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
        if open_interest is not None:
            self.open_interest = normalize_open_interest(open_interest)
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
        if orderbook_levels is not None:
            self.orderbook_levels = normalize_orderbook_levels(orderbook_levels)
        self.persist_settings()
        snap = self.snapshot()
        if liquidity is not None:
            snap["settings_affects"] = ["research_lld_only"]
        return snap

    def set_indicator_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        if name == "stochastic":
            self.stoch_config.enabled = bool(enabled)
        elif name in {"open_interest", "oi"}:
            self.open_interest["enabled"] = bool(enabled)
        elif name in {"liquidity", "liquidity_location", "lld"}:
            self.lld_config.enabled = bool(enabled)
        else:
            raise ValueError(f"unknown indicator {name}")
        self.persist_settings()
        return self.snapshot()

    def set_research_legacy_mode(self, enabled: bool) -> dict[str, Any]:
        self._research_legacy_mode = bool(enabled)
        if not self._research_legacy_mode:
            self._ema59_pilot_visible = False
            self._overlay_test = False
        else:
            self._current_backtest_id = None
            self._clear_ema59_current_backtest_drawings("DOGEUSDT")
            self._ema59_p11_archive_visible = False
        self._save_overlay_visibility_flags()
        removed = self._reconcile_overlay_layers(persist_if_changed=True)
        snap = self.snapshot()
        snap["research_legacy_mode"] = {
            "enabled": self._research_legacy_mode,
            "reconcile_removed": removed,
        }
        return snap

    def set_overlay_test(self, enabled: bool, symbol: str) -> dict[str, Any]:
        if enabled and not self._research_legacy_mode:
            snap = self.snapshot()
            snap["overlay_test_error"] = "legacy_mode_required"
            return snap
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

    def _clear_all_position_drawings(self, symbol: str) -> int:
        """Remove every long/short position drawing for this symbol.

        Research backtester mode should never mix leftover Stoch/NAP shorts with
        the current exit-pool long report.
        """
        sym = str(symbol or "").strip().upper()
        if not sym:
            return 0
        removed = 0
        for drawing in list(self.drawings.get_drawings(sym, include_hidden=True)):
            dtype = str(getattr(drawing, "drawing_type", "") or "")
            did = str(getattr(drawing, "drawing_id", "") or "")
            meta = drawing.metadata or {}
            origin = str(meta.get("origin") or meta.get("source") or "")
            is_position = dtype in ("long_position", "short_position")
            is_bt = (
                did.startswith("stoch-")
                or did.startswith(EXIT_POOL_DRAWING_PREFIX)
                or "backtester" in origin
                or origin in {
                    BACKTESTER_SOURCE,
                    EXIT_POOL_SOURCE,
                    APS_SOURCE,
                    NAP_SOURCE,
                }
            )
            if is_position or is_bt:
                self.drawings.remove_drawing(drawing.drawing_id)
                removed += 1
        if removed:
            self.persist_drawings()
        return removed

    def _clear_exit_pool_drawings(self, symbol: str | None = None) -> int:
        if not symbol:
            return 0
        return self._clear_all_position_drawings(symbol)

    def clear_exit_pool_backtester(self, symbol: str) -> dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        self._clear_exit_pool_drawings(sym)
        snap = self.snapshot()
        snap["backtester"] = {
            "strategy_id": EXIT_POOL_STRATEGY_ID,
            "symbol": sym,
            "loaded": 0,
            "visible": False,
            "source": EXIT_POOL_SOURCE,
            "message": "Exit-Pool Overlays ausgeblendet",
        }
        return snap

    def import_exit_pool_backtester(
        self,
        symbol: str,
        specs: list[dict[str, Any]],
        *,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Replace Exit-Pool long positions (Entry/TP/SL) for this symbol."""
        trp = self._trp
        dc_replace = trp["dc_replace"]
        sym = str(symbol or "").strip().upper()
        self._clear_exit_pool_drawings(sym)
        self._clear_ema59_e1c_signal_overlays(sym)
        loaded = 0
        skipped = 0
        for spec in specs or []:
            if not spec or spec.get("symbol") != sym:
                skipped += 1
                continue
            factory = trp["make_long_position"]
            drawing = factory(
                symbol=sym,
                timestamp_a=spec["start"],
                price_a=spec["entry"],
                timestamp_b=spec["end"],
                price_b=spec["target"],
                created_on_timeframe=spec.get("timeframe") or "5m",
                timeframe_scope="all",
                drawing_id=spec.get("drawing_id") or None,
                entry_price=spec["entry"],
                stop_price=spec["stop"],
                target_price=spec["target"],
            )
            drawing = dc_replace(
                drawing,
                metadata={
                    "origin": EXIT_POOL_SOURCE,
                    "strategy_id": EXIT_POOL_STRATEGY_ID,
                    "signal_id": spec.get("signal_id"),
                    "direction": spec.get("direction") or "LONG",
                    "exit_reason": spec.get("exit_reason"),
                    "tp_mode": spec.get("tp_mode"),
                    "pnl_pct": spec.get("pnl_pct"),
                    "tier": spec.get("tier"),
                },
            )
            if spec.get("drawing_id") and spec["drawing_id"] in self.drawings:
                self.drawings.remove_drawing(spec["drawing_id"])
            self.drawings.add_drawing(drawing)
            loaded += 1
        self.persist_drawings()
        snap = self.snapshot()
        summary = (meta or {}).get("summary") or {}
        by_exit_reason = summary.get("by_exit_reason") or {}
        tp_count = int(by_exit_reason.get("tp") or 0)
        sl_count = int(by_exit_reason.get("sl") or 0)
        snap["backtester"] = {
            "strategy_id": EXIT_POOL_STRATEGY_ID,
            "symbol": sym,
            "loaded": loaded,
            "skipped": skipped,
            "visible": loaded > 0,
            "source": EXIT_POOL_SOURCE,
            "summary": summary,
            "tp_sl": f"{tp_count}/{sl_count}",
            "report_path": (meta or {}).get("report_path"),
            "message": (meta or {}).get("message")
            or f"TP/SL {tp_count}/{sl_count}",
        }
        return snap

    def _clear_bt_d7_short_research_drawings(self, symbol: str) -> int:
        sym = str(symbol or "").strip().upper()
        removed = 0
        for drawing in list(self.drawings.get_drawings(sym, include_hidden=True)):
            did = str(getattr(drawing, "drawing_id", "") or "")
            meta = drawing.metadata or {}
            origin = str(meta.get("origin") or "")
            if (
                did.startswith(BT_D7_DRAWING_PREFIX)
                or did.startswith(BT_D7_HLINE_PREFIX)
                or did.startswith(BT_D7_ARROW_PREFIX)
                or origin == BT_D7_SOURCE
            ):
                self.drawings.remove_drawing(drawing.drawing_id)
                removed += 1
        return removed

    def clear_bt_d7_short_research(self, symbol: str) -> dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        self._clear_bt_d7_short_research_drawings(sym)
        self.persist_drawings()
        snap = self.snapshot()
        snap["backtester"] = {
            "strategy_id": BT_D7_STRATEGY_ID,
            "symbol": sym,
            "loaded": 0,
            "visible": False,
            "source": BT_D7_SOURCE,
            "message": "Pool Landscape SHORT research overlays hidden",
        }
        snap["bt_d7_short_research"] = {"visible": False}
        return snap

    def import_bt_d7_short_research(
        self,
        symbol: str,
        bundle: dict[str, Any],
        summary: dict[str, Any],
        *,
        visible: bool = True,
    ) -> dict[str, Any]:
        trp = self._trp
        dc_replace = trp["dc_replace"]
        sym = str(symbol or "").strip().upper()
        self._clear_bt_d7_short_research_drawings(sym)
        self._bt_d23_catalog = None
        self._bt_d26_catalog = None
        self._bt_d46_catalog = None
        self._clear_bt_d23_setup_scanner_drawings(sym)
        self._clear_bt_d26_dynamic_selector_drawings(sym)
        self._clear_bt_d46_chronological_drawings(sym)
        positions, hlines, markers = build_drawing_specs(bundle)
        loaded_pos = 0
        loaded_aux = 0
        skipped = 0

        for spec in positions:
            if spec.get("symbol") != sym:
                skipped += 1
                continue
            factory = (
                trp["make_short_position"]
                if spec.get("drawing_type") == "short_position"
                else trp["make_long_position"]
            )
            drawing = factory(
                symbol=sym,
                timestamp_a=spec["start"],
                price_a=spec["entry"],
                timestamp_b=spec["end"],
                price_b=spec["target"],
                created_on_timeframe=spec.get("timeframe") or "5m",
                timeframe_scope="all",
                drawing_id=spec.get("drawing_id") or None,
                entry_price=spec["entry"],
                stop_price=spec["stop"],
                target_price=spec["target"],
            )
            drawing = dc_replace(
                drawing,
                metadata={
                    "origin": BT_D7_SOURCE,
                    "strategy_id": BT_D7_STRATEGY_ID,
                    "signal_id": spec.get("signal_id"),
                    "direction": spec.get("direction") or "SHORT",
                    **(spec.get("metadata") or {}),
                },
            )
            self.drawings.add_drawing(drawing)
            loaded_pos += 1

        for hl in hlines:
            if hl.get("symbol") != sym:
                continue
            drawing = trp["make_hline"](
                symbol=sym,
                price=float(hl["price"]),
                created_on_timeframe="5m",
                timeframe_scope="all",
                drawing_id=hl.get("drawing_id"),
            )
            drawing = dc_replace(drawing, metadata=dict(hl.get("metadata") or {}))
            self.drawings.add_drawing(drawing)
            loaded_aux += 1

        for mk in markers:
            if mk.get("symbol") != sym:
                continue
            ts = mk.get("timestamp")
            if not ts:
                continue
            if isinstance(ts, str):
                ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            else:
                ts_dt = ts
            drawing = trp["make_vline"](
                symbol=sym,
                timestamp=ts_dt,
                created_on_timeframe="5m",
                timeframe_scope="all",
                drawing_id=mk.get("drawing_id"),
            )
            drawing = dc_replace(
                drawing,
                metadata={
                    "origin": BT_D7_SOURCE,
                    "marker_kind": mk.get("kind"),
                    "label": mk.get("label"),
                },
            )
            self.drawings.add_drawing(drawing)
            loaded_aux += 1

        self.persist_drawings()
        snap = self.snapshot()
        n_sig = int(summary.get("signal_count") or 0)
        n_tr = int(summary.get("filled_trade_count") or 0)
        net = summary.get("net_pnl_usdt")
        msg = (
            f"{n_sig} Signale · {n_tr} gefüllte Trades · {loaded_pos} Positionen gezeichnet"
            if visible
            else "ausgeblendet"
        )
        if n_tr == 0 and visible:
            msg += " · 0 Trades / keine realisierten Handelsgewinne"
        snap["bt_d7_short_research"] = {
            "visible": visible and (loaded_pos > 0 or loaded_aux > 0 or n_sig > 0),
            "summary": summary,
            "loaded_positions": loaded_pos,
            "loaded_aux": loaded_aux,
        }
        snap["backtester"] = {
            "strategy_id": BT_D7_STRATEGY_ID,
            "symbol": sym,
            "loaded": loaded_pos,
            "loaded_aux": loaded_aux,
            "skipped": skipped,
            "visible": visible and (loaded_pos > 0 or loaded_aux > 0 or n_sig > 0),
            "source": BT_D7_SOURCE,
            "summary": summary,
            "message": msg,
            "time_span": summary.get("time_span"),
            "net_pnl_usdt": net,
            "return_pct": summary.get("return_pct"),
        }
        return snap

    def _clear_bt_d23_setup_scanner_drawings(self, symbol: str) -> int:
        sym = str(symbol or "").strip().upper()
        removed = 0
        for drawing in list(self.drawings.get_drawings(sym, include_hidden=True)):
            did = str(getattr(drawing, "drawing_id", "") or "")
            meta = drawing.metadata or {}
            origin = str(meta.get("origin") or "")
            if did.startswith(BT_D23_DRAWING_PREFIX) or origin == BT_D23_SOURCE:
                self.drawings.remove_drawing(drawing.drawing_id)
                removed += 1
        self._bt_d23_rendered_ids.clear()
        return removed

    def clear_bt_d23_setup_scanner(self, symbol: str) -> dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        self._clear_bt_d23_setup_scanner_drawings(sym)
        self._bt_d23_catalog = None
        self.persist_drawings()
        snap = self.snapshot()
        snap["backtester"] = {
            "strategy_id": BT_D23_STRATEGY_ID,
            "symbol": sym,
            "loaded": 0,
            "visible": False,
            "source": BT_D23_SOURCE,
            "payload_kind": "bt_d23_setup_scanner_chart_integration_v1",
            "message": "Setup scanner planned overlays hidden",
        }
        snap["bt_d23_setup_scanner"] = {"visible": False}
        return snap

    def load_bt_d23_setup_scanner_catalog(
        self,
        symbol: str,
        doc: dict[str, Any],
        summary: dict[str, Any],
        *,
        run_id: str,
        visible: bool = True,
    ) -> dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        self._clear_bt_d7_short_research_drawings(sym)
        self._clear_bt_d23_setup_scanner_drawings(sym)
        self._bt_d26_catalog = None
        self._bt_d46_catalog = None
        self._clear_bt_d26_dynamic_selector_drawings(sym)
        self._clear_bt_d46_chronological_drawings(sym)
        setups = list(doc.get("setups") or [])
        self._bt_d23_catalog = {
            "run_id": run_id,
            "symbol": sym,
            "config_hash": doc.get("config_hash"),
            "setups": setups,
            "summary": summary,
            "visible": visible,
        }
        snap = self.snapshot()
        n = len(setups)
        snap["bt_d23_setup_scanner"] = {
            "visible": visible,
            "run_id": run_id,
            "setup_count_total": n,
            "setup_count_rendered": 0,
            "summary": summary,
            "payload_kind": "bt_d23_setup_scanner_chart_integration_v1",
        }
        snap["backtester"] = {
            "strategy_id": BT_D23_STRATEGY_ID,
            "symbol": sym,
            "loaded": 0,
            "visible": visible,
            "source": BT_D23_SOURCE,
            "payload_kind": "bt_d23_setup_scanner_chart_integration_v1",
            "summary": summary,
            "plan_only": True,
            "message": (
                f"BT-D23 setup scanner · {n} PLANNED setups catalog loaded "
                f"(viewport rendering; not executed)"
            ),
            "time_span": summary.get("time_span"),
        }
        return snap

    def sync_bt_d23_setup_scanner_viewport(
        self,
        symbol: str,
        *,
        view_start_unix: int,
        view_end_unix: int,
    ) -> dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        cat = self._bt_d23_catalog
        if not cat or not cat.get("visible") or cat.get("symbol") != sym:
            return self.snapshot()
        setups = cat.get("setups") or []
        wanted = filter_d23_setups_viewport(
            setups,
            view_start_unix=int(view_start_unix),
            view_end_unix=int(view_end_unix),
        )
        wanted_ids = {
            d23_setup_to_position_spec(
                s,
                run_id=str(cat.get("run_id") or ""),
                config_hash=cat.get("config_hash"),
            )["drawing_id"]
            for s in wanted
        }
        for did in list(self._bt_d23_rendered_ids - wanted_ids):
            try:
                self.drawings.remove_drawing(did)
            except Exception:
                pass
            self._bt_d23_rendered_ids.discard(did)

        trp = self._trp
        dc_replace = trp["dc_replace"]
        run_id = str(cat.get("run_id") or "")
        ch = cat.get("config_hash")
        for s in wanted:
            spec = d23_setup_to_position_spec(s, run_id=run_id, config_hash=ch)
            did = spec["drawing_id"]
            if did in self._bt_d23_rendered_ids:
                continue
            drawing = trp["make_short_position"](
                symbol=sym,
                timestamp_a=spec["start"],
                price_a=spec["entry"],
                timestamp_b=spec["end"],
                price_b=spec["target"],
                created_on_timeframe="5m",
                timeframe_scope="all",
                drawing_id=did,
                entry_price=spec["entry"],
                stop_price=spec["stop"],
                target_price=spec["target"],
            )
            drawing = dc_replace(drawing, metadata=dict(spec.get("metadata") or {}))
            self.drawings.add_drawing(drawing)
            self._bt_d23_rendered_ids.add(did)

        self.persist_drawings()
        snap = self.snapshot()
        snap["bt_d23_setup_scanner"] = {
            "visible": True,
            "run_id": run_id,
            "setup_count_total": len(setups),
            "setup_count_rendered": len(self._bt_d23_rendered_ids),
            "setup_count_viewport": len(wanted),
            "viewport": {"start_unix": view_start_unix, "end_unix": view_end_unix},
            "summary": cat.get("summary"),
            "payload_kind": "bt_d23_setup_scanner_chart_integration_v1",
        }
        snap["backtester"] = {
            "strategy_id": BT_D23_STRATEGY_ID,
            "symbol": sym,
            "loaded": len(self._bt_d23_rendered_ids),
            "visible": True,
            "source": BT_D23_SOURCE,
            "payload_kind": "bt_d23_setup_scanner_chart_integration_v1",
            "plan_only": True,
            "message": (
                f"Viewport: {len(wanted)} / {len(setups)} setups · "
                f"{len(self._bt_d23_rendered_ids)} boxes drawn · PLANNED ONLY"
            ),
            "time_span": (cat.get("summary") or {}).get("time_span"),
        }
        return snap

    def _clear_bt_d26_dynamic_selector_drawings(self, symbol: str) -> int:
        sym = str(symbol or "").strip().upper()
        removed = 0
        for drawing in list(self.drawings.get_drawings(sym, include_hidden=True)):
            did = str(getattr(drawing, "drawing_id", "") or "")
            meta = drawing.metadata or {}
            origin = str(meta.get("origin") or "")
            if (
                did.startswith(BT_D26_DRAWING_PREFIX)
                or did.startswith(BT_D26_MARKER_PREFIX)
                or origin == BT_D26_SOURCE
            ):
                self.drawings.remove_drawing(drawing.drawing_id)
                removed += 1
        self._bt_d26_rendered_ids.clear()
        return removed

    def clear_bt_d26_dynamic_selector(self, symbol: str) -> dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        self._clear_bt_d26_dynamic_selector_drawings(sym)
        self._bt_d26_catalog = None
        self.persist_drawings()
        snap = self.snapshot()
        snap["backtester"] = {
            "strategy_id": BT_D26_STRATEGY_ID,
            "symbol": sym,
            "loaded": 0,
            "visible": False,
            "source": BT_D26_SOURCE,
            "payload_kind": "bt_d26_dynamic_selector_chart_integration_v1",
            "message": "Dynamic selector planned overlays hidden",
        }
        snap["bt_d26_dynamic_selector"] = {"visible": False}
        return snap

    def load_bt_d26_dynamic_selector_catalog(
        self,
        symbol: str,
        doc: dict[str, Any],
        summary: dict[str, Any],
        *,
        run_id: str,
        visible: bool = True,
        display_mode: str | None = None,
    ) -> dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        self._clear_bt_d7_short_research_drawings(sym)
        self._clear_bt_d23_setup_scanner_drawings(sym)
        self._bt_d23_catalog = None
        self._clear_bt_d26_dynamic_selector_drawings(sym)
        self._bt_d46_catalog = None
        self._clear_bt_d46_chronological_drawings(sym)
        setups = list(doc.get("setups") or [])
        mode = str(display_mode or BT_D26_DISPLAY_CURRENT)
        if mode not in (BT_D26_DISPLAY_ALL, BT_D26_DISPLAY_CURRENT):
            mode = BT_D26_DISPLAY_CURRENT
        self._bt_d26_catalog = {
            "run_id": run_id,
            "symbol": sym,
            "config_hash": doc.get("config_hash"),
            "setups": setups,
            "candidate_transitions": list(doc.get("candidate_transitions") or []),
            "summary": summary,
            "visible": visible,
            "display_mode": mode,
            "catalog_doc": {
                "setups": setups,
                "candidate_transitions": list(doc.get("candidate_transitions") or []),
            },
        }
        snap = self.snapshot()
        n = len(setups)
        snap["bt_d26_dynamic_selector"] = {
            "visible": visible,
            "run_id": run_id,
            "display_mode": mode,
            "setup_count_total": n,
            "setup_count_rendered": 0,
            "summary": summary,
            "payload_kind": "bt_d26_dynamic_selector_chart_integration_v1",
        }
        snap["backtester"] = {
            "strategy_id": BT_D26_STRATEGY_ID,
            "symbol": sym,
            "loaded": 0,
            "visible": visible,
            "source": BT_D26_SOURCE,
            "payload_kind": "bt_d26_dynamic_selector_chart_integration_v1",
            "summary": summary,
            "plan_only": True,
            "message": (
                f"BT-D26 dynamic selector · {n} PLANNED setups catalog loaded "
                f"(viewport rendering; not executed)"
            ),
            "time_span": summary.get("time_span"),
        }
        return snap

    def sync_bt_d26_dynamic_selector_viewport(
        self,
        symbol: str,
        *,
        view_start_unix: int,
        view_end_unix: int,
        display_mode: str | None = None,
        as_of_unix: int | None = None,
    ) -> dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        cat = self._bt_d26_catalog
        if not cat or not cat.get("visible") or cat.get("symbol") != sym:
            return self.snapshot()
        setups = cat.get("setups") or []
        mode = str(display_mode or cat.get("display_mode") or BT_D26_DISPLAY_CURRENT)
        if mode not in (BT_D26_DISPLAY_ALL, BT_D26_DISPLAY_CURRENT):
            mode = BT_D26_DISPLAY_CURRENT
        cat["display_mode"] = mode
        run_id = str(cat.get("run_id") or "")
        ch = cat.get("config_hash")
        candidate_view: dict[str, Any] | None = None
        marker_specs: list[dict[str, Any]] = []

        if mode == BT_D26_DISPLAY_ALL:
            wanted = filter_d26_setups_viewport(
                setups,
                view_start_unix=int(view_start_unix),
                view_end_unix=int(view_end_unix),
            )
            position_specs = [
                d26_setup_to_position_spec(s, run_id=run_id, config_hash=ch) for s in wanted
            ]
        else:
            as_of = int(as_of_unix if as_of_unix is not None else view_end_unix)
            doc = cat.get("catalog_doc") or {
                "setups": setups,
                "candidate_transitions": cat.get("candidate_transitions") or [],
            }
            clean = d26_build_clean_view_draw_specs(
                doc,
                run_id=run_id,
                config_hash=ch,
                view_start_unix=int(view_start_unix),
                view_end_unix=int(view_end_unix),
                as_of_unix=as_of,
            )
            position_specs = clean.get("position_specs") or []
            marker_specs = clean.get("marker_specs") or []
            candidate_view = clean.get("resolved") or {}
            candidate_view["display_mode"] = mode
            candidate_view["marker_count_viewport"] = len(marker_specs)
            wanted = [s for s in setups if any(
                ps.get("setup_id") == s.get("setup_id") for ps in position_specs
            )]

        wanted_ids = {ps["drawing_id"] for ps in position_specs}
        wanted_ids.update(ms["drawing_id"] for ms in marker_specs)
        for did in list(self._bt_d26_rendered_ids - wanted_ids):
            try:
                self.drawings.remove_drawing(did)
            except Exception:
                pass
            self._bt_d26_rendered_ids.discard(did)

        trp = self._trp
        dc_replace = trp["dc_replace"]
        for spec in position_specs:
            did = spec["drawing_id"]
            if did in self._bt_d26_rendered_ids:
                continue
            drawing = trp["make_short_position"](
                symbol=sym,
                timestamp_a=spec["start"],
                price_a=spec["entry"],
                timestamp_b=spec["end"],
                price_b=spec["target"],
                created_on_timeframe="5m",
                timeframe_scope="all",
                drawing_id=did,
                entry_price=spec["entry"],
                stop_price=spec["stop"],
                target_price=spec["target"],
            )
            drawing = dc_replace(drawing, metadata=dict(spec.get("metadata") or {}))
            self.drawings.add_drawing(drawing)
            self._bt_d26_rendered_ids.add(did)

        for ms in marker_specs:
            did = ms["drawing_id"]
            if did in self._bt_d26_rendered_ids:
                continue
            ts = ms.get("timestamp")
            drawing = trp["make_vline"](
                symbol=sym,
                timestamp=ts,
                created_on_timeframe="5m",
                timeframe_scope="all",
                drawing_id=did,
            )
            drawing = dc_replace(drawing, metadata=dict(ms.get("metadata") or {}))
            self.drawings.add_drawing(drawing)
            self._bt_d26_rendered_ids.add(did)

        self.persist_drawings()
        snap = self.snapshot()
        snap["bt_d26_dynamic_selector"] = {
            "visible": True,
            "run_id": run_id,
            "display_mode": mode,
            "setup_count_total": len(setups),
            "setup_count_rendered": len(self._bt_d26_rendered_ids),
            "setup_count_viewport": len(wanted),
            "setup_count_boxes": len(position_specs),
            "setup_count_markers": len(marker_specs),
            "viewport": {"start_unix": view_start_unix, "end_unix": view_end_unix},
            "as_of_unix": int(as_of_unix if as_of_unix is not None else view_end_unix)
            if mode == BT_D26_DISPLAY_CURRENT
            else None,
            "candidate_view": candidate_view,
            "summary": cat.get("summary"),
            "payload_kind": "bt_d26_dynamic_selector_chart_integration_v1",
        }
        snap["backtester"] = {
            "strategy_id": BT_D26_STRATEGY_ID,
            "symbol": sym,
            "loaded": len(self._bt_d26_rendered_ids),
            "visible": True,
            "source": BT_D26_SOURCE,
            "payload_kind": "bt_d26_dynamic_selector_chart_integration_v1",
            "plan_only": True,
            "message": (
                f"Viewport: {len(wanted)} / {len(setups)} setups · "
                f"{len(self._bt_d26_rendered_ids)} boxes drawn · PLANNED ONLY"
            ),
            "time_span": (cat.get("summary") or {}).get("time_span"),
        }
        return snap

    def _clear_bt_d46_chronological_drawings(self, symbol: str) -> int:
        sym = str(symbol or "").strip().upper()
        removed = 0
        for drawing in list(self.drawings.get_drawings(sym, include_hidden=True)):
            did = str(getattr(drawing, "drawing_id", "") or "")
            meta = drawing.metadata or {}
            origin = str(meta.get("origin") or "")
            if (
                did.startswith(BT_D46_BOX_PREFIX)
                or did.startswith(BT_D46_MARKER_PREFIX)
                or did.startswith(BT_D46_LINK_PREFIX)
                or origin == BT_D46_SOURCE
            ):
                self.drawings.remove_drawing(drawing.drawing_id)
                removed += 1
        self._bt_d46_rendered_ids.clear()
        for oid in list(self._bt_d46_overlay_ids):
            try:
                self.overlays.remove_overlay(oid)
            except Exception:
                pass
            self._bt_d46_overlay_ids.discard(oid)
        return removed

    def _clear_bt_d46_chronological_overlays(self) -> None:
        for oid in list(self._bt_d46_overlay_ids):
            try:
                self.overlays.remove_overlay(oid)
            except Exception:
                pass
            self._bt_d46_overlay_ids.discard(oid)

    def clear_bt_d46_chronological_backtester(self, symbol: str) -> dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        self._clear_bt_d46_chronological_drawings(sym)
        self._bt_d46_catalog = None
        self.persist_drawings()
        snap = self.snapshot()
        snap["backtester"] = {
            "strategy_id": BT_D46_STRATEGY_ID,
            "symbol": sym,
            "loaded": 0,
            "visible": False,
            "source": BT_D46_SOURCE,
            "payload_kind": "bt_d46_chronological_chart_integration_v1",
            "message": "Chronological backtester overlays hidden",
        }
        snap["bt_d46_chronological"] = {"visible": False}
        return snap

    def load_bt_d46_chronological_catalog(
        self,
        symbol: str,
        doc: dict[str, Any],
        summary: dict[str, Any],
        audit_by_pid: dict[str, dict[str, Any]],
        *,
        visible: bool = True,
    ) -> dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        self._clear_bt_d7_short_research_drawings(sym)
        self._clear_bt_d23_setup_scanner_drawings(sym)
        self._bt_d23_catalog = None
        self._clear_bt_d26_dynamic_selector_drawings(sym)
        self._bt_d26_catalog = None
        self._clear_bt_d46_chronological_drawings(sym)
        self._clear_bt_d85_july_drawings(sym)
        self._bt_d46_catalog = {
            "symbol": sym,
            "overlay_doc": doc,
            "summary": summary,
            "audit_by_pid": audit_by_pid,
            "visible": visible,
        }
        snap = self.snapshot()
        snap["bt_d46_chronological"] = {
            "visible": visible,
            "summary": summary,
            "marker_count_total": summary.get("marker_count_total"),
            "event_counts": summary.get("event_counts"),
            "fill_disclaimer": summary.get("fill_disclaimer"),
            "scanner_boxes_included": False,
            "payload_kind": "bt_d46_chronological_chart_integration_v1",
        }
        snap["backtester"] = {
            "strategy_id": BT_D46_STRATEGY_ID,
            "symbol": sym,
            "loaded": 0,
            "visible": visible,
            "source": BT_D46_SOURCE,
            "payload_kind": "bt_d46_chronological_chart_integration_v1",
            "summary": summary,
            "plan_only": False,
            "simulated_only": True,
            "message": (
                f"BT-D46 chronological backtester · {summary.get('trade_count', 19)} "
                f"simulated trades · box-only chart (viewport; not exchange confirmed)"
            ),
            "time_span": summary.get("time_span"),
        }
        return snap

    def sync_bt_d46_chronological_viewport(
        self,
        symbol: str,
        *,
        view_start_unix: int,
        view_end_unix: int,
        as_of_unix: int | None = None,
        selected_position_id: str | None = None,
    ) -> dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        cat = self._bt_d46_catalog
        if not cat or not cat.get("visible") or cat.get("symbol") != sym:
            return self.snapshot()
        doc = cat.get("overlay_doc") or {}
        audit_by_pid = cat.get("audit_by_pid") or {}
        as_of = int(as_of_unix if as_of_unix is not None else view_end_unix)
        built = d46_build_viewport_draw_specs(
            doc,
            audit_by_pid,
            view_start_unix=int(view_start_unix),
            view_end_unix=int(view_end_unix),
            as_of_unix=as_of,
        )
        position_specs = built.get("position_specs") or []
        wanted_ids = {ps["drawing_id"] for ps in position_specs}

        self._clear_bt_d46_chronological_overlays()

        for did in list(self._bt_d46_rendered_ids - wanted_ids):
            try:
                self.drawings.remove_drawing(did)
            except Exception:
                pass
            self._bt_d46_rendered_ids.discard(did)

        trp = self._trp
        dc_replace = trp["dc_replace"]
        for spec in position_specs:
            did = spec["drawing_id"]
            if did in self._bt_d46_rendered_ids:
                continue
            drawing = trp["make_short_position"](
                symbol=sym,
                timestamp_a=spec["start"],
                price_a=float(spec["entry"]),
                timestamp_b=spec["end"],
                price_b=float(spec["target"]),
                created_on_timeframe="5m",
                timeframe_scope="all",
                drawing_id=did,
                entry_price=float(spec["entry"]),
                stop_price=float(spec["stop"]),
                target_price=float(spec["target"]),
            )
            drawing = dc_replace(drawing, metadata=dict(spec.get("metadata") or {}))
            self.drawings.add_drawing(drawing)
            self._bt_d46_rendered_ids.add(did)

        self.persist_drawings()
        snap = self.snapshot()
        detail = None
        if selected_position_id:
            detail = d46_position_detail_panel(
                selected_position_id, audit_by_pid, doc
            )
        snap["bt_d46_chronological"] = {
            "visible": True,
            "summary": cat.get("summary"),
            "viewport": {"start_unix": view_start_unix, "end_unix": view_end_unix},
            "as_of_unix": as_of,
            "as_of_utc": built.get("as_of_utc"),
            "counts": built.get("counts"),
            "same_bar_unresolved_positions": built.get("same_bar_unresolved_positions"),
            "box_count_rendered": len(self._bt_d46_rendered_ids),
            "display_mode": built.get("display_mode"),
            "scanner_boxes_included": False,
            "fill_disclaimer": (cat.get("summary") or {}).get("fill_disclaimer"),
            "selected_position": detail,
            "payload_kind": "bt_d46_chronological_chart_integration_v1",
        }
        snap["backtester"] = {
            "strategy_id": BT_D46_STRATEGY_ID,
            "symbol": sym,
            "loaded": len(self._bt_d46_rendered_ids),
            "visible": True,
            "source": BT_D46_SOURCE,
            "payload_kind": "bt_d46_chronological_chart_integration_v1",
            "simulated_only": True,
            "message": (
                f"Viewport: {built.get('counts', {}).get('viewport_boxes', 0)} trade boxes · "
                f"causal as-of {built.get('as_of_utc', '')} · SIMULATED — NOT EXCHANGE CONFIRMED"
            ),
            "time_span": (cat.get("summary") or {}).get("time_span"),
        }
        return snap

    def _clear_frozen_month_research_drawings(self, symbol: str) -> int:
        sym = str(symbol or "").strip().upper()
        removed = 0
        prefixes: set[str] = set()
        origins: set[str] = set()
        for prof in frozen_month_all_profiles():
            prefixes.add(prof.box_prefix)
            prefixes.add(prof.link_prefix)
            origins.add(prof.backtester_source)
        for drawing in list(self.drawings.get_drawings(sym, include_hidden=True)):
            did = str(getattr(drawing, "drawing_id", "") or "")
            meta = drawing.metadata or {}
            origin = str(meta.get("origin") or "")
            if any(did.startswith(p) for p in prefixes) or origin in origins:
                self.drawings.remove_drawing(drawing.drawing_id)
                removed += 1
        return removed

    def _clear_bt_d85_july_drawings(self, symbol: str) -> int:
        return self._clear_frozen_month_research_drawings(symbol)

    def clear_frozen_month_research_backtester(self, month_key: str, symbol: str) -> dict[str, Any]:
        prof = frozen_month_get_profile(month_key)
        sym = str(symbol or "").strip().upper()
        self._clear_frozen_month_research_drawings(sym)
        self.persist_drawings()
        snap = self.snapshot()
        snap["backtester"] = {
            "strategy_id": prof.strategy_id,
            "symbol": sym,
            "loaded": 0,
            "visible": False,
            "source": prof.backtester_source,
            "payload_kind": prof.payload_kind,
            "message": f"{prof.display_name} overlay hidden",
        }
        snap[prof.workspace_snap_key] = {"visible": False}
        return snap

    def clear_bt_d85_july_backtester(self, symbol: str) -> dict[str, Any]:
        return self.clear_frozen_month_research_backtester("july", symbol)

    def load_frozen_month_research_backtester(
        self,
        month_key: str,
        symbol: str,
        rows: list[dict[str, Any]],
        summary: dict[str, Any],
        *,
        visible: bool = True,
    ) -> dict[str, Any]:
        prof = frozen_month_get_profile(month_key)
        trp = self._trp
        dc_replace = trp["dc_replace"]
        sym = str(symbol or "").strip().upper()
        self._clear_bt_d7_short_research_drawings(sym)
        self._clear_bt_d23_setup_scanner_drawings(sym)
        self._bt_d23_catalog = None
        self._clear_bt_d26_dynamic_selector_drawings(sym)
        self._bt_d26_catalog = None
        self._clear_bt_d46_chronological_drawings(sym)
        self._bt_d46_catalog = None
        self._clear_frozen_month_research_drawings(sym)
        self._clear_short_main_v2_research_drawings(sym)
        self._clear_ema59_e1c_signal_overlays(sym)

        positions, links = frozen_month_build_drawing_specs(prof, rows)
        loaded = 0
        for spec in positions:
            factory = (
                trp["make_long_position"]
                if spec.get("drawing_type") == "long_position"
                else trp["make_short_position"]
            )
            entry_color = "#3b82f6" if prof.position_side == "long" else "#f97316"
            drawing = factory(
                symbol=sym,
                timestamp_a=spec["start"],
                price_a=float(spec["entry"]),
                timestamp_b=spec["end"],
                price_b=float(spec["target"]),
                created_on_timeframe="5m",
                timeframe_scope="all",
                drawing_id=spec["drawing_id"],
                entry_price=float(spec["entry"]),
                stop_price=float(spec["stop"]),
                target_price=float(spec["target"]),
            )
            meta = dict(spec.get("metadata") or {})
            status = str(meta.get("trade_status") or "")
            profit = "#22c55e" if status == "EXIT_TP" else "#ef4444"
            base_style = self._snapshot_draw_style()
            style = dc_replace(base_style, color=entry_color, width=1.5, profit_color=profit, loss_color="#ef4444")
            drawing = dc_replace(drawing, metadata=meta, style=style)
            self.drawings.add_drawing(drawing)
            loaded += 1

        for spec in links:
            link_style = dc_replace(
                self._snapshot_draw_style(),
                color="#94a3b8",
                width=1.0,
                line_style="dotted",
                opacity=0.55,
            )
            drawing = trp["make_trend"](
                symbol=sym,
                timestamp_a=spec["start"],
                price_a=float(spec["price_a"]),
                timestamp_b=spec["end"],
                price_b=float(spec["price_b"]),
                created_on_timeframe="5m",
                timeframe_scope="all",
                drawing_id=spec["drawing_id"],
                style=link_style,
            )
            drawing = dc_replace(drawing, metadata=dict(spec.get("metadata") or {}))
            self.drawings.add_drawing(drawing)
            loaded += 1

        self.persist_drawings()
        snap = self.snapshot()
        snap[prof.workspace_snap_key] = {
            "visible": visible,
            "summary": summary,
            "trade_count": summary.get("trade_count"),
            "tp_count": summary.get("tp_count"),
            "sl_count": summary.get("sl_count"),
            "payload_kind": prof.payload_kind,
        }
        snap["backtester"] = {
            "strategy_id": prof.strategy_id,
            "symbol": sym,
            "loaded": loaded,
            "visible": visible,
            "source": prof.backtester_source,
            "payload_kind": prof.payload_kind,
            "summary": summary,
            "simulated_only": True,
            "plan_only": False,
            "message": (
                f"{prof.display_name} · {summary.get('trade_count', 0)} modeled "
                f"{'longs' if prof.position_side == 'long' else 'shorts'} · "
                f"TP {summary.get('tp_count')} / SL {summary.get('sl_count')} · research replay only"
            ),
            "time_span": summary.get("time_span"),
        }
        return snap

    def _clear_short_main_v2_research_drawings(self, symbol: str) -> int:
        sym = str(symbol or "").strip().upper()
        removed = 0
        prefixes = short_main_v2_all_drawing_prefixes()
        origins = {SHORT_MAIN_V2_BACKTESTER_SOURCE}
        for drawing in list(self.drawings.get_drawings(sym, include_hidden=True)):
            did = str(getattr(drawing, "drawing_id", "") or "")
            meta = drawing.metadata or {}
            origin = str(meta.get("origin") or "")
            if any(did.startswith(p) for p in prefixes) or origin in origins:
                self.drawings.remove_drawing(drawing.drawing_id)
                removed += 1
        return removed

    def clear_short_main_v2_research_backtester(self, month_key: str, symbol: str) -> dict[str, Any]:
        prof = short_main_v2_get_profile(month_key)
        sym = str(symbol or "").strip().upper()
        self._clear_short_main_v2_research_drawings(sym)
        self.persist_drawings()
        snap = self.snapshot()
        snap["backtester"] = {
            "strategy_id": prof.strategy_id,
            "symbol": sym,
            "loaded": 0,
            "visible": False,
            "source": prof.backtester_source,
            "payload_kind": prof.payload_kind,
            "message": f"{prof.display_name} overlay hidden",
        }
        snap[prof.workspace_snap_key] = {"visible": False}
        return snap

    def load_short_main_v2_research_backtester(
        self,
        month_key: str,
        symbol: str,
        rows: list[dict[str, Any]],
        summary: dict[str, Any],
        *,
        visible: bool = True,
    ) -> dict[str, Any]:
        prof = short_main_v2_get_profile(month_key)
        trp = self._trp
        dc_replace = trp["dc_replace"]
        sym = str(symbol or "").strip().upper()
        self._clear_bt_d7_short_research_drawings(sym)
        self._clear_bt_d23_setup_scanner_drawings(sym)
        self._bt_d23_catalog = None
        self._clear_bt_d26_dynamic_selector_drawings(sym)
        self._bt_d26_catalog = None
        self._clear_bt_d46_chronological_drawings(sym)
        self._bt_d46_catalog = None
        self._clear_frozen_month_research_drawings(sym)
        self._clear_short_main_v2_research_drawings(sym)
        self._clear_ema59_e1c_signal_overlays(sym)

        positions, links, tp_segs = short_main_v2_build_drawing_specs(prof, rows)
        loaded = 0
        for spec in positions:
            drawing = trp["make_short_position"](
                symbol=sym,
                timestamp_a=spec["start"],
                price_a=float(spec["entry"]),
                timestamp_b=spec["end"],
                price_b=float(spec["target"]),
                created_on_timeframe="5m",
                timeframe_scope="all",
                drawing_id=spec["drawing_id"],
                entry_price=float(spec["entry"]),
                stop_price=float(spec["stop"]),
                target_price=float(spec["target"]),
            )
            meta = dict(spec.get("metadata") or {})
            gross = meta.get("gross_return_pct_short")
            profit = "#22c55e" if (gross is not None and float(gross) > 0) else "#ef4444"
            base_style = self._snapshot_draw_style()
            style = dc_replace(base_style, color="#f97316", width=1.5, profit_color=profit, loss_color="#ef4444")
            drawing = dc_replace(drawing, metadata=meta, style=style)
            self.drawings.add_drawing(drawing)
            loaded += 1

        for spec in links:
            link_style = dc_replace(
                self._snapshot_draw_style(),
                color="#94a3b8",
                width=1.0,
                line_style="dotted",
                opacity=0.55,
            )
            drawing = trp["make_trend"](
                symbol=sym,
                timestamp_a=spec["start"],
                price_a=float(spec["price_a"]),
                timestamp_b=spec["end"],
                price_b=float(spec["price_b"]),
                created_on_timeframe="5m",
                timeframe_scope="all",
                drawing_id=spec["drawing_id"],
                style=link_style,
            )
            drawing = dc_replace(drawing, metadata=dict(spec.get("metadata") or {}))
            self.drawings.add_drawing(drawing)
            loaded += 1

        for spec in tp_segs:
            seg_meta = dict(spec.get("metadata") or {})
            seg_style = dc_replace(
                self._snapshot_draw_style(),
                color=str(seg_meta.get("color") or "#38bdf8"),
                width=float(seg_meta.get("width") or 1.0),
                line_style=str(seg_meta.get("line_style") or "solid"),
                opacity=float(seg_meta.get("opacity") or 0.85),
            )
            drawing = trp["make_trend"](
                symbol=sym,
                timestamp_a=spec["start"],
                price_a=float(spec["price_a"]),
                timestamp_b=spec["end"],
                price_b=float(spec["price_b"]),
                created_on_timeframe="5m",
                timeframe_scope="all",
                drawing_id=spec["drawing_id"],
                style=seg_style,
            )
            drawing = dc_replace(drawing, metadata=seg_meta)
            self.drawings.add_drawing(drawing)
            loaded += 1

        self.persist_drawings()
        snap = self.snapshot()
        snap[prof.workspace_snap_key] = {
            "visible": visible,
            "summary": summary,
            "trade_count": summary.get("trade_count"),
            "tp_count": summary.get("tp_count"),
            "sl_count": summary.get("sl_count"),
            "gap_tp_count": summary.get("gap_tp_count"),
            "ema_exit_count": summary.get("ema_exit_count"),
            "coverage": summary.get("coverage"),
            "payload_kind": prof.payload_kind,
        }
        cov = summary.get("coverage") or prof.coverage
        snap["backtester"] = {
            "strategy_id": prof.strategy_id,
            "symbol": sym,
            "loaded": loaded,
            "visible": visible,
            "source": prof.backtester_source,
            "payload_kind": prof.payload_kind,
            "summary": summary,
            "simulated_only": True,
            "plan_only": False,
            "message": (
                f"{prof.display_name} · {summary.get('trade_count', 0)} modeled shorts · "
                f"Gap-TP {summary.get('gap_tp_count')} / SL {summary.get('sl_count')} / "
                f"EMA {summary.get('ema_exit_count')}"
                + (f" · {cov}" if cov == "PARTIAL" else "")
                + " · research replay only"
            ),
            "time_span": summary.get("time_span"),
        }
        return snap

    def load_bt_d85_july_backtester(
        self,
        symbol: str,
        rows: list[dict[str, Any]],
        summary: dict[str, Any],
        *,
        visible: bool = True,
    ) -> dict[str, Any]:
        return self.load_frozen_month_research_backtester(
            "july", symbol, rows, summary, visible=visible
        )

    def _clear_ema59_e1c_signal_overlays(self, symbol: str | None = None) -> int:
        removed = 0
        for oid in list(self.overlays.ids()):
            try:
                ov = self.overlays.get_overlay(oid)
            except KeyError:
                continue
            meta = getattr(ov, "metadata", None) or {}
            origin = str(meta.get("origin") or "")
            sid = str(meta.get("strategy_id") or "")
            if origin != EMA59_E1C_SOURCE and sid != EMA59_E1C_STRATEGY_ID:
                if not str(oid).startswith("e1c59-july-"):
                    continue
            if symbol and ov.symbol != symbol:
                continue
            self.overlays.remove_overlay(oid)
            removed += 1
        if removed:
            self._ema59_e1c_visible = False
        return removed

    def clear_ema59_e1c_july_signals_backtester(self, symbol: str) -> dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        self._clear_ema59_e1c_signal_overlays(sym)
        self.persist_drawings()
        snap = self.snapshot()
        snap["backtester"] = {
            "strategy_id": EMA59_E1C_STRATEGY_ID,
            "symbol": sym,
            "loaded": 0,
            "visible": False,
            "source": EMA59_E1C_SOURCE,
            "payload_kind": EMA59_E1C_PAYLOAD_KIND,
            "message": f"{EMA59_E1C_DISPLAY_NAME} ausgeblendet",
        }
        snap[EMA59_E1C_SNAP_KEY] = {"visible": False}
        return snap

    def load_ema59_e1c_july_signals_backtester(
        self,
        symbol: str,
        rows: list[dict[str, Any]],
        summary: dict[str, Any],
        *,
        visible: bool = True,
    ) -> dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        self._clear_bt_d7_short_research_drawings(sym)
        self._clear_bt_d23_setup_scanner_drawings(sym)
        self._bt_d23_catalog = None
        self._clear_bt_d26_dynamic_selector_drawings(sym)
        self._bt_d26_catalog = None
        self._clear_bt_d46_chronological_drawings(sym)
        self._bt_d46_catalog = None
        self._clear_frozen_month_research_drawings(sym)
        self._clear_short_main_v2_research_drawings(sym)
        self._clear_ema59_e1c_signal_overlays(sym)

        marker_specs = ema59_e1c_build_marker_specs(rows)
        markers = ema59_e1c_build_overlay_markers(marker_specs, symbol=sym)
        loaded = 0
        for ov in markers:
            if ov.overlay_id in self.overlays:
                self.overlays.remove_overlay(ov.overlay_id)
            self.overlays.add_overlay(ov)
            loaded += 1

        self._ema59_e1c_visible = bool(visible) and loaded > 0
        self.persist_drawings()
        snap = self.snapshot()
        snap[EMA59_E1C_SNAP_KEY] = {
            "visible": self._ema59_e1c_visible,
            "summary": summary,
            "signal_count": summary.get("signal_count"),
            "marker_count": loaded,
            "payload_kind": EMA59_E1C_PAYLOAD_KIND,
        }
        snap["backtester"] = {
            "strategy_id": EMA59_E1C_STRATEGY_ID,
            "symbol": sym,
            "loaded": loaded,
            "visible": self._ema59_e1c_visible,
            "source": EMA59_E1C_SOURCE,
            "payload_kind": EMA59_E1C_PAYLOAD_KIND,
            "summary": summary,
            "simulated_only": False,
            "plan_only": True,
            "message": (
                f"{EMA59_E1C_DISPLAY_NAME} · {loaded} Long-Überdehnungssignale · "
                f"Marker auf 5m Candle-Open · Entscheidung am Close (E1C)"
            ),
            "time_span": summary.get("time_span"),
        }
        return snap

    def _clear_ema59_pilot_drawings(self, symbol: str) -> int:
        sym = str(symbol or "").strip().upper()
        removed = 0
        for drawing in list(self.drawings.get_drawings(sym, include_hidden=True)):
            did = str(getattr(drawing, "drawing_id", "") or "")
            meta = drawing.metadata or {}
            if did.startswith(EMA59_PILOT_PREFIX) or meta.get("origin") == EMA59_PILOT_SOURCE:
                self.drawings.remove_drawing(drawing.drawing_id)
                removed += 1
        return removed

    def set_ema59_pilot_overlay(self, enabled: bool, symbol: str) -> dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        if sym != "DOGEUSDT":
            snap = self.snapshot()
            snap["ema59_pilot"] = {"visible": False, "error": "symbol_not_dogeusdt"}
            return snap
        if enabled and not self._research_legacy_mode:
            snap = self.snapshot()
            snap["ema59_pilot"] = {"visible": False, "error": "legacy_mode_required"}
            return snap
        self._ema59_pilot_visible = bool(enabled)
        self._clear_ema59_pilot_drawings(sym)
        self._save_overlay_visibility_flags()
        if not self._ema59_pilot_visible:
            self.persist_drawings()
            snap = self.snapshot()
            snap["ema59_pilot"] = {"visible": False, "loaded_positions": 0}
            return snap
        bundle = load_ema59_pilot_overlay()
        if bundle is None:
            snap = self.snapshot()
            snap["ema59_pilot"] = {
                "visible": False,
                "error": "overlay_file_missing",
                "expected_path": str(ema59_pilot_overlay_path()),
            }
            return snap
        positions, _hlines, markers = ema59_pilot_build_drawing_specs(bundle)
        return self._import_ema59_pilot_drawings(sym, positions, markers, bundle)

    def _import_ema59_pilot_drawings(
        self,
        sym: str,
        positions: list[dict[str, Any]],
        markers: list[dict[str, Any]],
        bundle: dict[str, Any],
    ) -> dict[str, Any]:
        trp = self._trp
        dc_replace = trp["dc_replace"]
        loaded_pos = 0
        loaded_aux = 0
        for spec in positions:
            if spec.get("symbol") != sym:
                continue
            factory = (
                trp["make_long_position"]
                if spec.get("drawing_type") == "long_position"
                else trp["make_short_position"]
            )
            drawing = factory(
                symbol=sym,
                timestamp_a=spec["start"],
                price_a=spec["entry"],
                timestamp_b=spec["end"],
                price_b=spec["target"],
                created_on_timeframe=spec.get("timeframe") or "5m",
                timeframe_scope="all",
                drawing_id=spec.get("drawing_id") or None,
                entry_price=spec["entry"],
                stop_price=spec["stop"],
                target_price=spec["target"],
            )
            drawing = dc_replace(
                drawing,
                metadata={
                    "origin": EMA59_PILOT_SOURCE,
                    "strategy_id": EMA59_PILOT_STRATEGY_ID,
                    **(spec.get("metadata") or {}),
                },
            )
            self.drawings.add_drawing(drawing)
            loaded_pos += 1
        for mk in markers:
            if mk.get("symbol") != sym:
                continue
            ts = mk.get("timestamp")
            if not ts:
                continue
            ts_dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            drawing = trp["make_vline"](
                symbol=sym,
                timestamp=ts_dt,
                created_on_timeframe="5m",
                timeframe_scope="all",
                drawing_id=mk.get("drawing_id"),
            )
            drawing = dc_replace(
                drawing,
                metadata={
                    "origin": EMA59_PILOT_SOURCE,
                    "marker_kind": mk.get("kind"),
                    "label": mk.get("label"),
                    **(mk.get("metadata") or {}),
                },
            )
            self.drawings.add_drawing(drawing)
            loaded_aux += 1
        self.persist_drawings()
        snap = self.snapshot()
        snap["ema59_pilot"] = {
            "visible": loaded_pos > 0,
            "loaded_positions": loaded_pos,
            "loaded_markers": loaded_aux,
            "trade_count": len(bundle.get("records") or []),
            "overlay_path": str(ema59_pilot_overlay_path()),
            "time_span": {
                "start": "2026-07-01T00:00:00Z",
                "end": "2026-07-31T23:55:00Z",
            },
            "message": f"EMA59 July Pilot · {loaded_pos} Long-Trades (Variante A)",
        }
        return snap

    def set_ema59_p11_archive_overlay(self, enabled: bool, symbol: str) -> dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        if sym != "DOGEUSDT":
            snap = self.snapshot()
            snap["ema59_p11_archive"] = {"visible": False, "error": "symbol_not_dogeusdt"}
            return snap
        if enabled and not self._research_legacy_mode:
            snap = self.snapshot()
            snap["ema59_p11_archive"] = {"visible": False, "error": "legacy_mode_required"}
            return snap
        self._ema59_p11_archive_visible = bool(enabled)
        self._clear_ema59_p11_archive_drawings(sym)
        self._save_overlay_visibility_flags()
        if not self._ema59_p11_archive_visible:
            self.persist_drawings()
            snap = self.snapshot()
            snap["ema59_p11_archive"] = {"visible": False, "loaded_positions": 0}
            return snap
        bundle = load_ema59_p11_archive_overlay()
        if bundle is None:
            snap = self.snapshot()
            snap["ema59_p11_archive"] = {
                "visible": False,
                "error": "overlay_file_missing",
                "expected_path": str(ema59_p11_archive_overlay_path()),
            }
            return snap
        positions, _h, markers = ema59_multi_current_backtest_build_specs(bundle)
        trp = self._trp
        dc_replace = trp["dc_replace"]
        loaded_pos = 0
        for spec in positions:
            if spec.get("symbol") != sym:
                continue
            tid = str((spec.get("metadata") or {}).get("trade_id") or spec.get("signal_id") or "pos")
            safe = tid.replace("|", "_").replace(":", "")
            drawing = trp["make_long_position"](
                symbol=sym,
                timestamp_a=spec["start"],
                price_a=spec["entry"],
                timestamp_b=spec["end"],
                price_b=spec["target"],
                created_on_timeframe=spec.get("timeframe") or "5m",
                timeframe_scope="all",
                drawing_id=f"{EMA59_P11_ARCHIVE_PREFIX}pos-{safe}",
                entry_price=spec["entry"],
                stop_price=spec["stop"],
                target_price=spec["target"],
            )
            meta = dict(spec.get("metadata") or {})
            meta["origin"] = EMA59_P11_ARCHIVE_SOURCE
            meta["backtest_id"] = EMA59_P11_ARCHIVE_BACKTEST_ID
            drawing = dc_replace(drawing, metadata=meta)
            self.drawings.add_drawing(drawing)
            loaded_pos += 1
        self.persist_drawings()
        snap = self.snapshot()
        snap["ema59_p11_archive"] = {
            "visible": loaded_pos > 0,
            "loaded_positions": loaded_pos,
            "trade_count": len(bundle.get("records") or []),
            "overlay_path": str(ema59_p11_archive_overlay_path()),
            "message": f"{EMA59_P11_ARCHIVE_DISPLAY_NAME} · {loaded_pos} Trades · Archiv",
        }
        return snap

    def _clear_ema59_current_backtest_drawings(self, symbol: str) -> int:
        sym = str(symbol or "").strip().upper()
        removed = 0
        for drawing in list(self.drawings.get_drawings(sym, include_hidden=True)):
            did = str(getattr(drawing, "drawing_id", "") or "")
            meta = drawing.metadata or {}
            matched = any(
                is_current_backtest_drawing({"drawing_id": did, "metadata": meta}, spec)
                for spec in EMA59_CURRENT_BT_CATALOG
            )
            if matched or meta.get("origin") in ema59_current_backtest_all_sources():
                self.drawings.remove_drawing(drawing.drawing_id)
                removed += 1
        return removed

    def _is_catalog_current_backtest_drawing(self, drawing: Any) -> bool:
        did = str(getattr(drawing, "drawing_id", "") or "")
        meta = drawing.metadata or {}
        if meta.get("origin") in ema59_current_backtest_all_sources():
            return True
        return any(
            is_current_backtest_drawing({"drawing_id": did, "metadata": meta}, spec)
            for spec in EMA59_CURRENT_BT_CATALOG
        )

    def _drawing_matches_active_current_backtest(
        self, drawing: Any, active: Ema59CurrentBacktestSpec
    ) -> bool:
        did = str(getattr(drawing, "drawing_id", "") or "")
        meta = drawing.metadata or {}
        bid = str(meta.get("backtest_id") or "")
        if bid and bid != active.backtest_id:
            return False
        if did.startswith(active.drawing_prefix):
            return True
        return meta.get("origin") == active.source and (not bid or bid == active.backtest_id)

    def _clear_stale_non_active_current_backtest_drawings(self, symbol: str) -> int:
        sym = str(symbol or "").strip().upper()
        active = ema59_current_backtest_get_spec(self._current_backtest_id or "")
        if active is None:
            return 0
        removed = 0
        for drawing in list(self.drawings.get_drawings(sym, include_hidden=True)):
            if not self._is_catalog_current_backtest_drawing(drawing):
                continue
            if self._drawing_matches_active_current_backtest(drawing, active):
                continue
            self.drawings.remove_drawing(drawing.drawing_id)
            removed += 1
        return removed

    def _count_active_current_backtest_position_drawings(
        self, symbol: str, active: Ema59CurrentBacktestSpec
    ) -> int:
        sym = str(symbol or "").strip().upper()
        count = 0
        for drawing in self.drawings.get_drawings(sym, include_hidden=True):
            if str(getattr(drawing, "drawing_type", "") or "") != "long_position":
                continue
            if self._drawing_matches_active_current_backtest(drawing, active):
                count += 1
        return count

    def _expected_current_backtest_position_count(self, backtest_id: str) -> int | None:
        bundle, _errors = load_ema59_catalog_backtest_bundle(backtest_id)
        if bundle is None:
            return None
        positions = bundle.get("position_specs") or []
        if positions:
            return len(positions)
        return len(bundle.get("records") or [])

    def _ensure_current_backtest_layer_synced(self, symbol: str, *, persist: bool = False) -> bool:
        """Drop catalog backtest drawings from other variants; reload active overlay if incomplete."""
        if self._research_legacy_mode or not self._current_backtest_id:
            return False
        sym = str(symbol or "").strip().upper()
        active = ema59_current_backtest_get_spec(self._current_backtest_id)
        if active is None:
            return False
        stale_removed = self._clear_stale_non_active_current_backtest_drawings(sym)
        expected = self._expected_current_backtest_position_count(self._current_backtest_id)
        loaded = self._count_active_current_backtest_position_drawings(sym, active)
        needs_reload = expected is not None and loaded != expected
        if stale_removed or needs_reload:
            self.set_ema59_current_backtest(self._current_backtest_id, sym)
            if getattr(self, "_persist_migrated_current_backtest_flag", False):
                self._persist_migrated_current_backtest_flag = False
            return True
        if getattr(self, "_persist_migrated_current_backtest_flag", False):
            self._save_overlay_visibility_flags()
            self._persist_migrated_current_backtest_flag = False
        return False

    def _clear_ema59_p11_archive_drawings(self, symbol: str) -> int:
        sym = str(symbol or "").strip().upper()
        removed = 0
        for drawing in list(self.drawings.get_drawings(sym, include_hidden=True)):
            did = str(getattr(drawing, "drawing_id", "") or "")
            meta = drawing.metadata or {}
            if (
                did.startswith(EMA59_P11_ARCHIVE_PREFIX)
                or did.startswith("ema59p11-")
                or meta.get("origin") in (EMA59_P11_ARCHIVE_SOURCE, "ema59_bt_p11_6m_current_backtest_v1")
            ):
                self.drawings.remove_drawing(drawing.drawing_id)
                removed += 1
        return removed

    def set_ema59_current_backtest(self, backtest_id: str | None, symbol: str) -> dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        if sym != "DOGEUSDT":
            snap = self.snapshot()
            snap["current_backtest"] = {"visible": False, "error": "symbol_not_dogeusdt"}
            return snap
        if self._research_legacy_mode:
            snap = self.snapshot()
            snap["current_backtest"] = {"visible": False, "error": "disable_legacy_mode_first"}
            return snap
        bid = str(backtest_id).strip() if backtest_id else None
        if bid == "":
            bid = None
        if bid == "ema59_6m_juli_sl_diagnose":
            bid = None
        if bid and self._p17_ab_diagnostic_visible:
            self.set_p17_ab_diagnostic_overlay(False, sym)
        if bid and self._p17_ag_restored_visible:
            self.set_p17_ag_restored_overlay(False, sym)
        self._clear_ema59_current_backtest_drawings(sym)
        self._current_backtest_id = None
        if not bid:
            self._save_overlay_visibility_flags()
            self.persist_drawings()
            snap = self.snapshot()
            snap["current_backtest"] = {"visible": False, "backtest_id": None, "loaded_positions": 0}
            return snap
        spec = ema59_current_backtest_get_spec(bid)
        if spec is None:
            self._save_overlay_visibility_flags()
            self.persist_drawings()
            snap = self.snapshot()
            snap["current_backtest"] = {"visible": False, "backtest_id": bid, "errors": ["unknown_backtest_id"]}
            return snap
        bundle, errors = load_ema59_catalog_backtest_bundle(bid)
        if bundle is None:
            snap = self.snapshot()
            snap["current_backtest"] = {"visible": False, "backtest_id": bid, "errors": errors}
            return snap
        positions, _hlines, markers = ema59_multi_current_backtest_build_specs(bundle)
        trp = self._trp
        dc_replace = trp["dc_replace"]
        loaded_pos = 0
        loaded_aux = 0
        for ps in positions:
            if ps.get("symbol") != sym:
                continue
            drawing = trp["make_long_position"](
                symbol=sym,
                timestamp_a=ps["start"],
                price_a=ps["entry"],
                timestamp_b=ps["end"],
                price_b=ps["target"],
                created_on_timeframe=ps.get("timeframe") or "5m",
                timeframe_scope="all",
                drawing_id=ps.get("drawing_id") or None,
                entry_price=ps["entry"],
                stop_price=ps["stop"],
                target_price=ps["target"],
            )
            meta = dict(ps.get("metadata") or {})
            meta["origin"] = spec.source
            meta["backtest_id"] = bid
            drawing = dc_replace(drawing, metadata=meta)
            self.drawings.add_drawing(drawing)
            loaded_pos += 1
        for mk in markers:
            if mk.get("symbol") != sym:
                continue
            ts = mk.get("timestamp")
            if not ts:
                continue
            ts_dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            drawing = trp["make_vline"](
                symbol=sym,
                timestamp=ts_dt,
                created_on_timeframe="5m",
                timeframe_scope="all",
                drawing_id=mk.get("drawing_id"),
            )
            drawing = dc_replace(
                drawing,
                metadata={
                    "origin": spec.source,
                    "backtest_id": bid,
                    "marker_kind": mk.get("kind"),
                    "label": mk.get("label"),
                    **(mk.get("metadata") or {}),
                },
            )
            self.drawings.add_drawing(drawing)
            loaded_aux += 1
        self._clear_ema59_p11_archive_drawings(sym)
        self._current_backtest_id = bid
        self._save_overlay_visibility_flags()
        self.persist_drawings()
        snap = self.snapshot()
        cb: dict[str, Any] = {
            "visible": loaded_pos > 0,
            "backtest_id": bid,
            "label": spec.label,
            "research_disclaimer": bundle.get("research_disclaimer"),
            "loaded_positions": loaded_pos,
            "loaded_markers": loaded_aux,
            "trade_count": len(bundle.get("records") or []),
            "records_count": len(bundle.get("records") or []),
            "overlay_source": spec.source,
            "message": f"{spec.label} · {loaded_pos} Trades · FORSCHUNG",
        }
        if spec.zoom_july_2026:
            cb["zoom_july_2026"] = dict(JULY_2026_ZOOM)
        snap["current_backtest"] = cb
        return snap

    def _clear_ema59_6m_band_overlays(self, symbol: str | None = None) -> int:
        removed = 0
        for oid in list(self.overlays.ids()):
            try:
                ov = self.overlays.get_overlay(oid)
            except KeyError:
                continue
            meta = getattr(ov, "metadata", None) or {}
            if (
                str(oid).startswith(EMA59_6M_BAND_MARKER_PREFIX)
                or meta.get("origin") == EMA59_6M_BAND_SOURCE
                or meta.get("strategy_id") == EMA59_6M_BAND_STRATEGY_ID
            ):
                if symbol and ov.symbol != symbol:
                    continue
                self.overlays.remove_overlay(oid)
                removed += 1
        return removed

    def set_ema59_6m_band_overlay(self, enabled: bool, symbol: str) -> dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        if sym != "DOGEUSDT":
            snap = self.snapshot()
            snap["ema59_6m_band"] = {"visible": False, "error": "symbol_not_dogeusdt"}
            return snap
        self._ema59_6m_band_visible = bool(enabled)
        self._clear_ema59_6m_band_overlays(sym)
        if not self._ema59_6m_band_visible:
            self._ema59_p9b_cross_index = None
            self._clear_ema59_p9b_overlays(sym)
        self._save_overlay_visibility_flags()
        summary = ema59_6m_build_summary()
        if not self._ema59_6m_band_visible:
            snap = self.snapshot()
            snap["ema59_6m_band"] = {"visible": False, "summary": summary}
            return snap
        rows, errs = ema59_6m_load_cross_rows()
        if errs and not rows:
            snap = self.snapshot()
            snap["ema59_6m_band"] = {
                "visible": False,
                "error": "crosses_unavailable",
                "errors": errs,
                "summary": summary,
            }
            self._ema59_6m_band_visible = False
            return snap
        for ov in ema59_6m_build_overlay_markers(rows, symbol=sym):
            self.overlays.add_overlay(ov)
        snap = self.snapshot()
        snap["ema59_6m_band"] = {
            "visible": True,
            "marker_count": len(rows),
            "summary": summary,
            "errors": errs,
            "message": f"EMA59 6M + −2σ · {len(rows)} Crosses · {summary.get('diagnostic_bars')} Jul bars",
            "time_span": {"start": "2026-07-01T00:00:00Z", "end": "2026-07-31T23:50:00Z"},
            "data_gap_note": summary.get("data_gap_note"),
        }
        return snap

    def _count_p17_ab_position_drawings(self, symbol: str) -> int:
        sym = str(symbol or "").strip().upper()
        n = 0
        trp = self._trp
        for drawing in self.drawings.get_drawings(sym, include_hidden=True):
            did = str(getattr(drawing, "drawing_id", "") or "")
            meta = drawing.metadata or {}
            if did.startswith(P17_AB_PREFIX) and getattr(drawing, "drawing_type", "") in trp["POSITION_TYPES"]:
                if meta.get("origin") == P17_AB_SOURCE:
                    n += 1
        return n

    def _count_p17_ag_position_drawings(self, symbol: str) -> int:
        sym = str(symbol or "").strip().upper()
        n = 0
        trp = self._trp
        for drawing in self.drawings.get_drawings(sym, include_hidden=True):
            did = str(getattr(drawing, "drawing_id", "") or "")
            meta = drawing.metadata or {}
            if (
                did.startswith(P17_AG_PREFIX)
                and getattr(drawing, "drawing_type", "") in trp["POSITION_TYPES"]
                and meta.get("origin") == P17_AG_SOURCE
            ):
                n += 1
        return n

    def _clear_p17_ag_restored_drawings(self, symbol: str) -> int:
        sym = str(symbol or "").strip().upper()
        removed = 0
        for drawing in list(self.drawings.get_drawings(sym, include_hidden=True)):
            did = str(getattr(drawing, "drawing_id", "") or "")
            meta = drawing.metadata or {}
            if did.startswith(P17_AG_PREFIX) or meta.get("origin") == P17_AG_SOURCE:
                self.drawings.remove_drawing(drawing.drawing_id)
                removed += 1
        return removed

    def set_p17_ag_restored_overlay(self, enabled: bool, symbol: str) -> dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        if sym != "DOGEUSDT":
            snap = self.snapshot()
            snap["p17_ag_restored"] = {
                "visible": False,
                "error": "symbol_not_dogeusdt",
            }
            return snap
        if self._research_legacy_mode:
            snap = self.snapshot()
            snap["p17_ag_restored"] = {
                "visible": False,
                "error": "disable_legacy_mode_first",
            }
            return snap
        self._p17_ag_restored_visible = bool(enabled)
        self._clear_p17_ag_restored_drawings(sym)
        if self._p17_ag_restored_visible:
            self._p17_ab_diagnostic_visible = False
            self._current_backtest_id = None
            self._clear_p17_ab_diagnostic_drawings(sym)
            self._clear_ema59_current_backtest_drawings(sym)
        self._save_overlay_visibility_flags()
        if not self._p17_ag_restored_visible:
            self.persist_drawings()
            snap = self.snapshot()
            snap["p17_ag_restored"] = {
                "visible": False,
                "loaded_positions": 0,
                "loaded_markers": 0,
                "message": f"{P17_AG_LAYER_NAME} hidden",
            }
            return snap
        try:
            bundle = p17_ag_build_overlay_bundle()
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._p17_ag_restored_visible = False
            self._save_overlay_visibility_flags()
            snap = self.snapshot()
            snap["p17_ag_restored"] = {
                "visible": False,
                "error": str(exc),
                "message": "P17AF results missing or invalid",
            }
            return snap
        snap = self._import_p17_ag_restored_drawings(
            sym,
            list(bundle.get("position_specs") or []),
            list(bundle.get("marker_specs") or []),
        )
        snap["p17_ag_restored"]["source_p17af"] = bundle.get("source_p17af")
        snap["p17_ag_restored"]["layer_name"] = P17_AG_LAYER_NAME
        return snap

    def _import_p17_ag_restored_drawings(
        self,
        symbol: str,
        positions: list[dict[str, Any]],
        markers: list[dict[str, Any]],
    ) -> dict[str, Any]:
        trp = self._trp
        dc_replace = trp["dc_replace"]
        sym = str(symbol or "").strip().upper()
        loaded_pos = 0
        loaded_markers = 0
        for spec in positions:
            if spec.get("symbol") != sym:
                continue
            drawing = trp["make_long_position"](
                symbol=sym,
                timestamp_a=spec["start"],
                price_a=spec["entry"],
                timestamp_b=spec["end"],
                price_b=spec["target"],
                created_on_timeframe=spec.get("timeframe") or "5m",
                timeframe_scope="all",
                drawing_id=spec.get("drawing_id") or None,
                entry_price=spec["entry"],
                stop_price=spec["stop"],
                target_price=spec["target"],
            )
            drawing = dc_replace(
                drawing,
                metadata={
                    "origin": P17_AG_SOURCE,
                    "strategy_id": P17_AG_STRATEGY_ID,
                    **(spec.get("metadata") or {}),
                },
            )
            self.drawings.add_drawing(drawing)
            loaded_pos += 1
        for marker in markers:
            if marker.get("symbol") != sym:
                continue
            timestamp = marker.get("timestamp")
            if not timestamp:
                continue
            drawing = trp["make_vline"](
                symbol=sym,
                timestamp=datetime.fromisoformat(
                    str(timestamp).replace("Z", "+00:00")
                ),
                created_on_timeframe="5m",
                timeframe_scope="all",
                drawing_id=marker.get("drawing_id"),
            )
            drawing = dc_replace(
                drawing,
                metadata={
                    "origin": P17_AG_SOURCE,
                    "strategy_id": P17_AG_STRATEGY_ID,
                    "label": marker.get("label"),
                    **(marker.get("metadata") or {}),
                },
            )
            self.drawings.add_drawing(drawing)
            loaded_markers += 1
        self.persist_drawings()
        snap = self.snapshot()
        snap["p17_ag_restored"] = {
            "visible": True,
            "loaded_positions": loaded_pos,
            "loaded_markers": loaded_markers,
            "message": (
                f"{P17_AG_LAYER_NAME}: {loaded_pos} Long-Boxen · "
                f"{loaded_markers} Entry-Marker · RESEARCH_ONLY"
            ),
        }
        return snap

    def _clear_p17_ab_diagnostic_drawings(self, symbol: str) -> int:
        sym = str(symbol or "").strip().upper()
        removed = 0
        for drawing in list(self.drawings.get_drawings(sym, include_hidden=True)):
            did = str(getattr(drawing, "drawing_id", "") or "")
            meta = drawing.metadata or {}
            if did.startswith(P17_AB_PREFIX) or meta.get("origin") == P17_AB_SOURCE:
                self.drawings.remove_drawing(drawing.drawing_id)
                removed += 1
        return removed

    def set_p17_ab_diagnostic_overlay(
        self,
        enabled: bool,
        symbol: str,
        *,
        filter_mode: str = "first_only",
    ) -> dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        if sym != "DOGEUSDT":
            snap = self.snapshot()
            snap["p17_ab_diagnostic"] = {"visible": False, "error": "symbol_not_dogeusdt"}
            return snap
        if self._research_legacy_mode:
            snap = self.snapshot()
            snap["p17_ab_diagnostic"] = {"visible": False, "error": "disable_legacy_mode_first"}
            return snap
        mode = str(filter_mode or "first_only").strip()
        if mode not in ("first_only", "all"):
            mode = "first_only"
        self._p17_ab_diagnostic_visible = bool(enabled)
        self._p17_ab_filter_mode = mode
        self._clear_p17_ab_diagnostic_drawings(sym)
        if self._p17_ab_diagnostic_visible:
            self._p17_ag_restored_visible = False
            self._clear_p17_ag_restored_drawings(sym)
            self._clear_ema59_current_backtest_drawings(sym)
        self._save_overlay_visibility_flags()
        snap = self.snapshot()
        if not self._p17_ab_diagnostic_visible:
            self.persist_drawings()
            snap["p17_ab_diagnostic"] = {
                "visible": False,
                "filter_mode": mode,
                "loaded_positions": 0,
                "message": f"{P17_AB_LAYER_NAME} hidden",
            }
            return snap
        try:
            bundle = p17_ab_build_overlay_bundle(filter_mode=mode)  # type: ignore[arg-type]
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self._p17_ab_diagnostic_visible = False
            self._save_overlay_visibility_flags()
            snap = self.snapshot()
            snap["p17_ab_diagnostic"] = {
                "visible": False,
                "error": str(exc),
                "message": "P17AA results missing or invalid",
            }
            return snap
        positions, _hlines, markers = p17_ab_build_drawing_specs(bundle)
        snap = self._import_p17_ab_diagnostic_drawings(sym, positions, markers)
        snap["p17_ab_diagnostic"]["filter_mode"] = mode
        snap["p17_ab_diagnostic"]["candidate_count"] = len(bundle.get("candidates") or [])
        snap["p17_ab_diagnostic"]["source_p17aa"] = bundle.get("source_p17aa")
        snap["p17_ab_diagnostic"]["layer_name"] = P17_AB_LAYER_NAME
        return snap

    def _import_p17_ab_diagnostic_drawings(
        self,
        symbol: str,
        positions: list[dict[str, Any]],
        markers: list[dict[str, Any]],
    ) -> dict[str, Any]:
        trp = self._trp
        dc_replace = trp["dc_replace"]
        sym = str(symbol or "").strip().upper()
        loaded_pos = 0
        loaded_aux = 0
        for spec in positions:
            if spec.get("symbol") != sym:
                continue
            drawing = trp["make_long_position"](
                symbol=sym,
                timestamp_a=spec["start"],
                price_a=spec["entry"],
                timestamp_b=spec["end"],
                price_b=spec["target"],
                created_on_timeframe=spec.get("timeframe") or "5m",
                timeframe_scope="all",
                drawing_id=spec.get("drawing_id") or None,
                entry_price=spec["entry"],
                stop_price=spec["stop"],
                target_price=spec["target"],
            )
            drawing = dc_replace(
                drawing,
                metadata={
                    "origin": P17_AB_SOURCE,
                    "strategy_id": P17_AB_STRATEGY_ID,
                    **(spec.get("metadata") or {}),
                },
            )
            self.drawings.add_drawing(drawing)
            loaded_pos += 1
        for mk in markers:
            if mk.get("symbol") != sym:
                continue
            ts = mk.get("timestamp")
            if not ts:
                continue
            ts_dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            drawing = trp["make_vline"](
                symbol=sym,
                timestamp=ts_dt,
                created_on_timeframe="5m",
                timeframe_scope="all",
                drawing_id=mk.get("drawing_id"),
            )
            drawing = dc_replace(
                drawing,
                metadata={
                    "origin": P17_AB_SOURCE,
                    "marker_kind": mk.get("kind"),
                    "label": mk.get("label"),
                    **(mk.get("metadata") or {}),
                },
            )
            self.drawings.add_drawing(drawing)
            loaded_aux += 1
        self.persist_drawings()
        snap = self.snapshot()
        snap["p17_ab_diagnostic"] = {
            "visible": True,
            "loaded_positions": loaded_pos,
            "loaded_markers": loaded_aux,
            "message": f"{P17_AB_LAYER_NAME}: {loaded_pos} diagnostic boxes · DIAGNOSTIC_ONLY",
        }
        return snap

    def _p17aq_catalog_cached(self) -> dict[str, Any]:
        if self._p17aq_catalog is None:
            try:
                p17_aq_write_csv_exports()
                self._p17aq_catalog = p17_aq_build_signal_catalog()
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._p17aq_catalog = {"error": str(exc)}
        return self._p17aq_catalog or {}

    def _p17aq_snapshot(self) -> dict[str, Any]:
        cat = self._p17aq_catalog_cached()
        return {
            "executed_visible": self._p17aq_executed_visible,
            "august_visible": self._p17aq_august_visible,
            **cat,
        }

    def _clear_p17aq_executed_drawings(self, symbol: str) -> int:
        sym = str(symbol or "").strip().upper()
        removed = 0
        for drawing in list(self.drawings.get_drawings(sym, include_hidden=True)):
            did = str(getattr(drawing, "drawing_id", "") or "")
            meta = drawing.metadata or {}
            if did.startswith(P17_AQ_EXEC_PREFIX) or meta.get("origin") == P17_AQ_EXEC_SOURCE:
                self.drawings.remove_drawing(drawing.drawing_id)
                removed += 1
        return removed

    def _clear_p17aq_overlays(self, symbol: str | None = None) -> int:
        removed = 0
        sym = str(symbol or "").strip().upper()
        for oid in list(self.overlays.ids()):
            if not p17_aq_is_overlay_id(oid):
                continue
            ov = self.overlays.get_overlay(oid)
            if sym and getattr(ov, "symbol", "") != sym:
                continue
            self.overlays.remove_overlay(oid)
            removed += 1
        return removed

    def set_p17aq_executed_overlay(self, enabled: bool, symbol: str) -> dict[str, Any]:
        self._p17aq_executed_visible = bool(enabled)
        return self._refresh_p17aq_layers(str(symbol or "").strip().upper())

    def set_p17aq_august_diagnostic_overlay(self, enabled: bool, symbol: str) -> dict[str, Any]:
        self._p17aq_august_visible = bool(enabled)
        return self._refresh_p17aq_layers(str(symbol or "").strip().upper())

    def _refresh_p17aq_layers(self, sym: str) -> dict[str, Any]:
        self._clear_p17aq_executed_drawings(sym)
        self._clear_p17aq_overlays(sym)
        loaded_pos = loaded_exec_mk = loaded_aug = 0
        err: str | None = None
        trp = self._trp
        dc_replace = trp["dc_replace"]
        if self._p17aq_executed_visible:
            try:
                positions, marker_specs = p17_aq_build_executed_bundle()
                for spec in positions:
                    if spec.get("symbol") != sym:
                        continue
                    drawing = trp["make_long_position"](
                        symbol=sym,
                        timestamp_a=spec["start"],
                        price_a=spec["entry"],
                        timestamp_b=spec["end"],
                        price_b=spec["target"],
                        created_on_timeframe=spec.get("timeframe") or "5m",
                        timeframe_scope="all",
                        drawing_id=spec.get("drawing_id") or None,
                        entry_price=spec["entry"],
                        stop_price=spec["stop"],
                        target_price=spec["target"],
                    )
                    drawing = dc_replace(
                        drawing,
                        metadata={**(spec.get("metadata") or {}), "origin": P17_AQ_EXEC_SOURCE},
                    )
                    self.drawings.add_drawing(drawing)
                    loaded_pos += 1
                for ms in marker_specs:
                    if ms.get("symbol") != sym:
                        continue
                    self.overlays.add_overlay(p17_aq_marker_to_overlay(ms, trp))
                    loaded_exec_mk += 1
                self.persist_drawings()
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                err = str(exc)
        if self._p17aq_august_visible and not err:
            try:
                specs = p17_aq_build_august_marker_specs(
                    skip_duplicate_with_executed=self._p17aq_executed_visible,
                )
                for ms in specs:
                    if ms.get("symbol") != sym:
                        continue
                    self.overlays.add_overlay(p17_aq_marker_to_overlay(ms, trp))
                    loaded_aug += 1
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                err = str(exc)
        snap = self.snapshot()
        pq = snap.setdefault("p17aq", {})
        pq["loaded_executed_positions"] = loaded_pos
        pq["loaded_executed_markers"] = loaded_exec_mk
        pq["loaded_august_markers"] = loaded_aug
        pq["august_markers_skipped_duplicates"] = bool(
            self._p17aq_executed_visible and self._p17aq_august_visible
        )
        if err:
            pq["error"] = err
        elif not self._p17aq_executed_visible and not self._p17aq_august_visible:
            pq["message"] = "P17AQ layers hidden"
        return snap

    def _clear_ema59_p17_landscape_overlays(self, symbol: str | None = None) -> int:
        removed = 0
        for oid in list(self.overlays.ids()):
            try:
                ov = self.overlays.get_overlay(oid)
            except KeyError:
                continue
            meta = getattr(ov, "metadata", None) or {}
            if (
                str(oid).startswith(EMA59_P17_LANDSCAPE_PREFIX)
                or meta.get("origin") == EMA59_P17_LANDSCAPE_SOURCE
                or meta.get("strategy_id") == EMA59_P17_LANDSCAPE_STRATEGY_ID
            ):
                if symbol and ov.symbol != symbol:
                    continue
                self.overlays.remove_overlay(oid)
                removed += 1
        return removed

    def set_ema59_p17_landscape_overlay(self, enabled: bool, symbol: str) -> dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        if sym != "DOGEUSDT":
            snap = self.snapshot()
            snap["ema59_p17_landscape"] = {"visible": False, "error": "symbol_not_dogeusdt"}
            return snap
        self._ema59_p17_landscape_visible = bool(enabled)
        self._clear_ema59_p17_landscape_overlays(sym)
        self._save_overlay_visibility_flags()
        payload = ema59_p17_load_chart_payload()
        if not self._ema59_p17_landscape_visible:
            snap = self.snapshot()
            snap["ema59_p17_landscape"] = {"visible": False, "artifacts": ema59_p17_artifacts_available()}
            return snap
        if not payload:
            self._ema59_p17_landscape_visible = False
            snap = self.snapshot()
            snap["ema59_p17_landscape"] = {
                "visible": False,
                "error": "artifact_missing",
                "artifacts": ema59_p17_artifacts_available(),
            }
            return snap
        cands = payload.get("candidates") or []
        summary = ema59_p17_build_summary(payload)
        for ov in ema59_p17_build_overlay_objects(cands, symbol=sym, mode="diagnostic"):
            if ov.overlay_id in self.overlays:
                self.overlays.remove_overlay(ov.overlay_id)
            self.overlays.add_overlay(ov)
        loaded = len(cands)
        snap = self.snapshot()
        snap["ema59_p17_landscape"] = {
            "visible": True,
            "candidate_count": loaded,
            "summary": summary,
            "jump_list": ema59_p17_build_jump_list(payload),
            "artifacts": ema59_p17_artifacts_available(),
            "message": (
                f"{EMA59_P17_LANDSCAPE_DISPLAY_NAME} · {loaded} DIAGNOSTIC Entry+SL · "
                "kein TP · kein PnL"
            ),
            "time_span": summary.get("time_span"),
        }
        return snap

    def _clear_ema59_p9b_overlays(self, symbol: str | None = None) -> int:
        removed = 0
        for oid in list(self.overlays.ids()):
            try:
                ov = self.overlays.get_overlay(oid)
            except KeyError:
                continue
            meta = getattr(ov, "metadata", None) or {}
            if (
                str(oid).startswith(EMA59_P9B_PREFIX)
                or meta.get("origin") == EMA59_P9B_SOURCE
                or meta.get("strategy_id") == EMA59_P9B_STRATEGY_ID
            ):
                if symbol and ov.symbol != symbol:
                    continue
                self.overlays.remove_overlay(oid)
                removed += 1
        return removed

    def set_ema59_p9b_watch_diagnostic(
        self,
        cross_index: int | None,
        symbol: str,
    ) -> dict[str, Any]:
        sym = str(symbol or "").strip().upper()
        snap_base = self.snapshot()
        if sym != "DOGEUSDT":
            snap_base["ema59_p9b"] = {"visible": False, "error": "symbol_not_dogeusdt"}
            return snap_base
        if not self._ema59_6m_band_visible:
            self._ema59_p9b_cross_index = None
            self._clear_ema59_p9b_overlays(sym)
            snap_base["ema59_p9b"] = {
                "visible": False,
                "error": "ema59_6m_band_required",
                "message": "WATCH-Diagnose: zuerst EMA59 6M + −2σ aktivieren",
            }
            return snap_base
        self._clear_ema59_p9b_overlays(sym)
        if cross_index is None or int(cross_index) < 0:
            self._ema59_p9b_cross_index = None
            snap_base["ema59_p9b"] = {"visible": False, "message": "WATCH-Diagnose aus"}
            return snap_base
        idx = int(cross_index)
        overlays, summary = ema59_p9b_build_episode_overlays(cross_index=idx, symbol=sym)
        if summary.get("error"):
            self._ema59_p9b_cross_index = None
            snap_base["ema59_p9b"] = {"visible": False, **summary, **ema59_p9b_artifacts_available()}
            return snap_base
        for ov in overlays:
            self.overlays.add_overlay(ov)
        self._ema59_p9b_cross_index = idx
        snap = self.snapshot()
        snap["ema59_p9b"] = {
            "visible": True,
            "cross_index": idx,
            "cross_id": ema59_p9b_cross_id(idx),
            **summary,
            "message": (
                f"WATCH-Diagnose {summary.get('cross_id')} · "
                f"{summary.get('touch_markers')} A · {summary.get('candidate_markers')} Kandidaten · "
                f"MIN† = rückblickend"
            ),
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

    def _clear_ezm_overlays(self, symbol: str | None = None) -> int:
        removed = 0
        for oid in list(self.overlays.ids()):
            try:
                ov = self.overlays.get_overlay(oid)
            except KeyError:
                continue
            meta = getattr(ov, "metadata", None) or {}
            if meta.get("origin") != EZM_SOURCE and meta.get("strategy_id") != EZM_STRATEGY_ID:
                continue
            if symbol and ov.symbol != symbol:
                continue
            self.overlays.remove_overlay(oid)
            removed += 1
        return removed

    def store_ezm_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._ezm_run = {
            "meta": payload.get("meta") or {},
            "candidates": payload.get("candidates") or [],
            "ema_setup_events": payload.get("ema_setup_events") or [],
            "microstructure_confirmation_events": payload.get("microstructure_confirmation_events") or [],
            "markers": payload.get("markers") or [],
            "coverage": payload.get("coverage") or {},
            "summary": payload.get("summary") or {},
        }
        sym = str((payload.get("meta") or {}).get("symbol") or "").upper()
        if sym:
            self._clear_ezm_overlays(sym)
        self._ezm_visible = False
        snap = self.snapshot()
        snap["backtester"] = {
            "strategy_id": EZM_STRATEGY_ID,
            "symbol": sym,
            "loaded": len(payload.get("markers") or []),
            "source": EZM_SOURCE,
            "run_intent": "candidate_discovery",
            "message": "EZM Candidates gespeichert — Backtester klicken zum Einblenden",
        }
        return snap

    def set_ezm_visible(
        self,
        visible: bool,
        symbol: str | None = None,
        *,
        layer_mode: str | None = None,
    ) -> dict[str, Any]:
        from .ezm_backtester import build_overlay_markers

        if layer_mode is not None:
            self._ezm_layer_mode = self._normalize_ezm_layer_mode(layer_mode)
        run = self._ezm_run or {}
        sym = str(symbol or (run.get("meta") or {}).get("symbol") or "").upper()
        self._clear_ezm_overlays(sym or None)
        self._ezm_visible = bool(visible)
        specs: list[dict[str, Any]] = []
        if self._ezm_visible and sym:
            specs = self._ezm_marker_specs(run)
            markers = build_overlay_markers(specs, symbol=sym)
            for ov in markers:
                if ov.overlay_id in self.overlays:
                    self.overlays.remove_overlay(ov.overlay_id)
                self.overlays.add_overlay(ov)
            # EZM overlays are ephemeral session state — do not rewrite drawings.json
            # (persist of 1k+ user drawings on every Backtester toggle freezes the UI).
        snap = self.snapshot()
        setup_n = len([m for m in specs if m.get("layer") == "ema_setup"])
        micro_n = len([m for m in specs if m.get("layer") == "microstructure_confirmation"])
        # Keep event totals from stored run for status; loaded = chart overlays.
        run_meta = run.get("meta") or {}
        snap["backtester"] = {
            "strategy_id": EZM_STRATEGY_ID,
            "symbol": sym,
            "visible": self._ezm_visible,
            "loaded": len(specs),
            "layer_mode": self._ezm_layer_mode,
            "n_setup_markers": setup_n,
            "n_micro_markers": micro_n,
            "n_ema_setup_events": int(run_meta.get("n_ema_setup_events") or len(run.get("ema_setup_events") or [])),
            "source": EZM_SOURCE,
            "run_intent": "candidate_discovery",
        }
        return snap

    def set_ezm_layer_mode(self, layer_mode: str, symbol: str | None = None) -> dict[str, Any]:
        """Switch EMA vs micro chart layers without changing run data."""
        self._ezm_layer_mode = self._normalize_ezm_layer_mode(layer_mode)
        if not self._ezm_run:
            snap = self.snapshot()
            snap["backtester"] = {
                "strategy_id": EZM_STRATEGY_ID,
                "symbol": str(symbol or "").upper(),
                "visible": False,
                "loaded": 0,
                "layer_mode": self._ezm_layer_mode,
                "source": EZM_SOURCE,
                "run_intent": "candidate_discovery",
            }
            return snap
        sym = str(symbol or (self._ezm_run.get("meta") or {}).get("symbol") or "").upper()
        return self.set_ezm_visible(self._ezm_visible, sym)

    def _normalize_pool_signals_mode(self, mode: str) -> str:
        m = str(mode or "confirmed").strip().lower()
        if m in {"off", "aus", "none", "0"}:
            return "off"
        if m in {"debug", "candidates"}:
            return "debug"
        if m in {"active", "armed", "active_plans", "aktive"}:
            return "active"
        if m in {"all", "all_states", "alle"}:
            return "all_states"
        return "confirmed"

    def _pool_signals_time_span(self, run: dict[str, Any]) -> dict[str, Any]:
        """Earliest/latest signal times for chart sync after import."""
        times: list[datetime] = []
        for row in list(run.get("confirmed") or []) + list(run.get("signal_intents") or []):
            if not isinstance(row, dict):
                continue
            for key in (
                "armed_at",
                "signal_at",
                "confirmation_at",
                "confirmed_at",
                "decision_at",
                "hypothetical_filled_at",
            ):
                raw = row.get(key)
                if raw is None:
                    continue
                try:
                    if isinstance(raw, datetime):
                        times.append(raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc))
                    else:
                        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                        # Scanner artifacts are UTC-naive — never treat as local wall time.
                        times.append(dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))
                    break
                except ValueError:
                    continue
        if not times:
            return {}
        start = min(times)
        end = max(times)
        # Prefer a mid signal as focus
        focus = times[len(times) // 2]
        return {
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
            "focus": focus.isoformat().replace("+00:00", "Z"),
            "n_timed": len(times),
        }

    def _pool_signals_snapshot(self) -> dict[str, Any]:
        run = self._pool_signals_run or {}
        span = self._pool_signals_time_span(run)
        return {
            "strategy_id": APS_STRATEGY_ID,
            "loaded": bool(run),
            "display_mode": self._pool_signals_display_mode,
            "meta": run.get("meta") or {},
            "n_confirmed": len(run.get("confirmed") or []),
            "n_debug_rows": len(run.get("debug_rows") or []),
            "source": APS_SOURCE,
            "run_intent": "candidate_discovery",
            "time_span": span,
        }

    def _clear_pool_signals_overlays(self, symbol: str | None = None) -> int:
        removed = 0
        for oid in list(self.overlays.ids()):
            try:
                ov = self.overlays.get_overlay(oid)
            except KeyError:
                continue
            meta = getattr(ov, "metadata", None) or {}
            if meta.get("origin") != APS_SOURCE and meta.get("strategy_id") != APS_STRATEGY_ID:
                continue
            if not str(oid).startswith("aps-"):
                continue
            if symbol and ov.symbol != symbol:
                continue
            self.overlays.remove_overlay(oid)
            removed += 1
        return removed

    def _pool_signals_marker_specs(self, run: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            from .oa_import import ensure_oa_on_path

            ensure_oa_on_path()
            from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.markers import (
                dedupe_plan_rows,
                signals_to_marker_specs,
            )
        except Exception:  # noqa: BLE001
            return []
        mode = self._pool_signals_display_mode
        run_id = str((run.get("meta") or {}).get("run_id") or "")

        def _strip_bulk_lines(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
            # Dashboard: never paint ENTRY/TP/SL axis labels for historical imports.
            # Even "active" includes confirmed rows → 29×3 lines = chaos. Keep arrows only;
            # levels stay in marker tooltip. Live ARMED-only runs rarely need lines on chart.
            return [s for s in specs if str(s.get("kind") or "") != "APS_LINE"]

        if mode == "active":
            # Prefer true armed intents; fall back to confirmed markers (arrows only).
            armed_rows = dedupe_plan_rows(
                list(run.get("signal_intents") or [])
                + [
                    c
                    for c in (run.get("candidates") or [])
                    if str(c.get("state") or "") in {"LIMIT_INTENT_ARMED", "LIMIT_ARMED"}
                ]
            )
            confirmed_rows = list(run.get("confirmed") or [])
            if armed_rows:
                specs = signals_to_marker_specs(armed_rows, display_mode="active", run_id=run_id or None)
                specs += signals_to_marker_specs(
                    confirmed_rows, display_mode="confirmed", run_id=run_id or None
                )
            else:
                # No live armed plans in this import — show confirmed arrows, not plan lines
                specs = signals_to_marker_specs(
                    confirmed_rows, display_mode="confirmed", run_id=run_id or None
                )
            return _strip_bulk_lines(specs)
        if mode == "all_states":
            rows = dedupe_plan_rows(
                list(run.get("signal_intents") or [])
                + list(run.get("candidates") or [])
                + list(run.get("confirmed") or [])
                + list(run.get("debug_rows") or [])
            )
            return _strip_bulk_lines(
                signals_to_marker_specs(rows, display_mode="all_states", run_id=run_id or None)
            )
        specs = signals_to_marker_specs(run.get("confirmed") or [], display_mode=mode, run_id=run_id or None)
        if mode == "debug":
            specs = signals_to_marker_specs(run.get("debug_rows") or [], display_mode="debug", run_id=run_id or None)
            specs += signals_to_marker_specs(
                run.get("confirmed") or [], display_mode="confirmed", run_id=run_id or None
            )
        return _strip_bulk_lines(specs)

    def store_pool_signals_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._pool_signals_run = {
            "meta": payload.get("meta") or {},
            "confirmed": payload.get("confirmed") or [],
            "debug_rows": payload.get("debug_rows") or [],
            "signal_intents": payload.get("signal_intents") or [],
            "candidates": payload.get("candidates") or [],
        }
        sym = str((payload.get("meta") or {}).get("symbol") or "").upper()
        if sym:
            self._clear_pool_signals_overlays(sym)
        snap = self.snapshot()
        snap["backtester"] = {
            "strategy_id": APS_STRATEGY_ID,
            "symbol": sym,
            "loaded": len(self._pool_signals_run.get("confirmed") or []),
            "source": APS_SOURCE,
            "run_intent": "candidate_discovery",
            "message": "A+ Pool Signals gespeichert — Anzeige umschalten ohne neuen Job",
        }
        return snap

    def set_pool_signals_display_mode(self, mode: str, symbol: str | None = None) -> dict[str, Any]:
        from .pool_signals_backtester import build_overlay_markers

        self._pool_signals_display_mode = self._normalize_pool_signals_mode(mode)
        run = self._pool_signals_run or {}
        sym = str(symbol or (run.get("meta") or {}).get("symbol") or "").upper()
        self._clear_pool_signals_overlays(sym or None)
        if self._pool_signals_display_mode != "off" and sym and run:
            specs = self._pool_signals_marker_specs(run)
            markers = build_overlay_markers(specs, symbol=sym)
            for ov in markers:
                if ov.overlay_id in self.overlays:
                    self.overlays.remove_overlay(ov.overlay_id)
                self.overlays.add_overlay(ov)
        snap = self.snapshot()
        snap["backtester"] = {
            "strategy_id": APS_STRATEGY_ID,
            "symbol": sym,
            "display_mode": self._pool_signals_display_mode,
            "loaded": len(self._pool_signals_marker_specs(run)) if run else 0,
            "source": APS_SOURCE,
            "run_intent": "candidate_discovery",
        }
        return snap

    def _nested_ask_pool_snapshot(self) -> dict[str, Any]:
        run = self._nested_ask_pool_run or {}
        meta = run.get("meta") or {}
        return {
            "strategy_id": NAP_STRATEGY_ID,
            "loaded": bool(run),
            "visible": bool(self._nested_ask_pool_visible),
            "show_rejected": bool(self._nested_ask_pool_show_rejected),
            "meta": meta,
            "summary": run.get("ui_summary") or {},
            "n_overlays": len(run.get("markers") or []),
            "source": NAP_SOURCE,
            "run_intent": "historical_backtest",
            "time_span": {
                "start": meta.get("start_utc"),
                "end": meta.get("end_utc"),
            },
        }

    def _clear_nested_ask_pool_overlays(self, symbol: str | None = None) -> int:
        removed = 0
        for oid in list(self.overlays.ids()):
            try:
                ov = self.overlays.get_overlay(oid)
            except KeyError:
                continue
            meta = getattr(ov, "metadata", None) or {}
            if meta.get("origin") != NAP_SOURCE and meta.get("strategy_id") != NAP_STRATEGY_ID:
                continue
            if not str(oid).startswith("nap-"):
                continue
            if symbol and ov.symbol != symbol:
                continue
            self.overlays.remove_overlay(oid)
            removed += 1
        return removed

    def store_nested_ask_pool_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        from .nested_ask_pool_backtester import build_overlay_objects, ui_summary_from_payload

        markers = list(payload.get("markers") or (payload.get("overlay") or {}).get("specs") or [])
        ui_summary = ui_summary_from_payload(payload)
        self._nested_ask_pool_run = {
            "meta": payload.get("meta") or {},
            "summary": payload.get("summary") or {},
            "provenance": payload.get("provenance") or {},
            "markers": markers,
            "ui_summary": ui_summary,
        }
        sym = str((payload.get("meta") or {}).get("symbol") or "").upper()
        self._clear_nested_ask_pool_overlays(sym or None)
        self._nested_ask_pool_visible = True
        if sym and markers:
            for ov in build_overlay_objects(markers, symbol=sym):
                if ov.overlay_id in self.overlays:
                    self.overlays.remove_overlay(ov.overlay_id)
                self.overlays.add_overlay(ov)
        snap = self.snapshot()
        snap["backtester"] = {
            "strategy_id": NAP_STRATEGY_ID,
            "symbol": sym,
            "loaded": len(markers),
            "source": NAP_SOURCE,
            "run_intent": "historical_backtest",
            "summary": ui_summary,
        }
        return snap

    def set_nested_ask_pool_visible(self, visible: bool, symbol: str | None = None) -> dict[str, Any]:
        from .nested_ask_pool_backtester import build_overlay_objects

        run = self._nested_ask_pool_run or {}
        sym = str(symbol or (run.get("meta") or {}).get("symbol") or "").upper()
        self._clear_nested_ask_pool_overlays(sym or None)
        self._nested_ask_pool_visible = bool(visible)
        if self._nested_ask_pool_visible and sym and run:
            markers = list(run.get("markers") or [])
            for ov in build_overlay_objects(markers, symbol=sym):
                if ov.overlay_id in self.overlays:
                    self.overlays.remove_overlay(ov.overlay_id)
                self.overlays.add_overlay(ov)
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
        if not sid or sid == EZM_STRATEGY_ID or sid == "ezm":
            self._clear_ezm_overlays(sym or None)
            if sid:
                self._ezm_visible = False
        if not sid or sid == APS_STRATEGY_ID or sid == "pool_signals" or sid == "a_plus":
            self._clear_pool_signals_overlays(sym or None)
            if sid:
                self._pool_signals_display_mode = "off"
        if not sid or sid == NAP_STRATEGY_ID or sid == "nested_ask_pool" or sid == "nested_ask":
            self._clear_nested_ask_pool_overlays(sym or None)
            if sid:
                self._nested_ask_pool_visible = False
        if not sid or sid == EMA59_E1C_STRATEGY_ID or sid == "ema59_e1c":
            self._clear_ema59_e1c_signal_overlays(sym or None)
            if sid:
                self._ema59_e1c_visible = False
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
        if (
            not sid
            or sid == EXIT_POOL_STRATEGY_ID
            or sid == "exit_pool"
            or sid == EXIT_POOL_SOURCE
        ):
            if sym:
                self._clear_exit_pool_drawings(sym)
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

    def on_deselect(self) -> dict[str, Any]:
        self._selected_drawing_id = None
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

    def _extend_right_ui_state(self, selected: Any) -> dict[str, bool]:
        if selected is None or getattr(selected, "drawing_type", None) != "trend":
            return {"available": False, "enabled": False}
        meta = dict(getattr(selected, "metadata", None) or {})
        return {"available": True, "enabled": bool(meta.get("extend_right"))}

    def toggle_selected_extend_right(self) -> dict[str, Any]:
        trp = self._trp
        dc_replace = trp["dc_replace"]
        did = self._selected_drawing_id
        if not did or did not in self.drawings:
            return self.snapshot()
        drawing = self.drawings.get_drawing(did)
        if drawing.drawing_type != "trend":
            return self.snapshot()
        meta = dict(drawing.metadata or {})
        meta["extend_right"] = not bool(meta.get("extend_right"))
        self.drawings.update_drawing(dc_replace(drawing, metadata=meta))
        self.persist_drawings()
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
        elif drawing.drawing_type in ("circle", "arrow", "trend", "measure", "rectangle"):
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
        if kind == "deselect":
            return self.on_deselect()
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
        from .nested_ask_pool_backtester import json_safe

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
        safe: list[dict] = []
        for payload in payloads:
            payload["namespace"] = overlay_namespace(payload)
            meta = payload.get("metadata") or {}
            if (
                payload.get("type") == "line"
                and payload.get("kind") == "segment"
                and meta.get("drawing_type") == "trend"
                and meta.get("extend_right")
            ):
                payload["extend_right"] = True
            cleaned = json_safe(payload)
            if isinstance(cleaned, dict):
                safe.append(cleaned)
        return safe

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
        from .lld_research_kernel import compute_lld_selected
        from .service import strip_lld_strength_labels

        tf = str(getattr(candles[0], "timeframe", "") or "") if candles else ""
        selected, cfg, result, _tip = compute_lld_selected(candles, tf, cfg, tip=None)
        result.pools = selected
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
        overlays = strip_lld_strength_labels(
            trp["compose_lld_overlays"](result, cfg, clusters=clusters)
        )
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
    if meta.get("origin") == APS_SOURCE or meta.get("strategy_id") == APS_STRATEGY_ID or oid.startswith("aps-"):
        return "POOL_SIGNALS"
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

"""Localhost-only HTTP control API for the live collector.

Binds to 127.0.0.1 by default. No public exposure. No shell exec.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from signal_generator.bybit.history import ensure_utc
from signal_generator.bybit.live.demand_symbols import DemandSymbolStore
from signal_generator.bybit.live.desired_state import DesiredStateStore
from signal_generator.bybit.live.health import HealthState
from signal_generator.db.candles import CandleRepository
from signal_generator.db.outcomes import SignalOutcomeRepository
from signal_generator.db.signals import SignalRepository
from signal_generator.pipeline.outcome_eval import RESULT_OPEN

logger = logging.getLogger(__name__)


class CollectorControlService:
    """Shared state for the control API handlers."""

    def __init__(
        self,
        *,
        health: HealthState,
        desired: DesiredStateStore,
        signals: SignalRepository | None = None,
        outcomes: SignalOutcomeRepository | None = None,
        candles: CandleRepository | None = None,
        on_desired_change: Callable[[str], None] | None = None,
        demand: DemandSymbolStore | None = None,
        get_collector: Callable[[], Any] | None = None,
    ) -> None:
        self.health = health
        self.desired = desired
        self.signals = signals
        self.outcomes = outcomes
        self.candles = candles
        self.on_desired_change = on_desired_change
        self.demand = demand
        self.get_collector = get_collector
        self._lock = threading.Lock()

    def forming(self, symbol: str | None = None) -> dict[str, Any]:
        with self._lock:
            return self.health.forming_payload(symbol)

    def status(self) -> dict[str, Any]:
        with self._lock:
            self.health.desired_state = self.desired.read()
            return self.health.to_dict()

    def get_desired(self) -> dict[str, Any]:
        return self.desired.to_dict()

    def set_desired(self, value: str, *, reason: str = "api") -> dict[str, Any]:
        value = str(value).upper()
        if value not in ("RUNNING", "STOPPED"):
            raise ValueError("desired_state must be RUNNING or STOPPED")
        payload = self.desired.write(value, reason=reason)  # type: ignore[arg-type]
        self.health.desired_state = value
        if self.on_desired_change:
            self.on_desired_change(value)
        return payload

    def ensure_symbol(self, symbol: str) -> dict[str, Any]:
        """Collect only this symbol (history recovery + live WS). Restarts if the set changed."""
        if self.demand is None:
            raise RuntimeError("demand store not configured")
        written = self.demand.write_singleton(symbol, reason="ensure_symbol")
        desired = self.set_desired("RUNNING", reason="ensure_symbol")
        collector = self.get_collector() if self.get_collector else None
        current = [s.upper() for s in (getattr(collector, "symbols", None) or [])]
        want = list(written.get("symbols") or [])
        restarted = False
        if collector is not None and current != want:
            collector.request_stop()
            restarted = True
        return {
            "ok": True,
            "demand": written,
            "desired_state": desired,
            "restarted": restarted,
            "previous_symbols": current,
        }

    def trade_fanout_op(self, op: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Localhost trade-fanout IPC (no public route semantics beyond 127.0.0.1 bind)."""
        body = body or {}
        collector = self.get_collector() if self.get_collector else None
        if collector is None or not hasattr(collector, "trade_fanout"):
            return {"ok": False, "error": "collector_unavailable"}
        fanout = collector.trade_fanout()
        if fanout is None:
            return {"ok": False, "error": "trade_fanout_disabled"}
        if op == "create_trade_subscriber":
            return fanout.create_subscriber(
                symbol=body.get("symbol"),
                symbols=body.get("symbols"),
                max_queue=body.get("max_queue"),
                subscriber_id=body.get("subscriber_id"),
            )
        if op == "poll_trade_events":
            return fanout.poll_events(
                subscriber_id=str(body.get("subscriber_id") or ""),
                cursor=body.get("cursor"),
                limit=int(body.get("limit") or 256),
            )
        if op == "trade_subscriber_heartbeat":
            return fanout.heartbeat(str(body.get("subscriber_id") or ""))
        if op == "remove_trade_subscriber":
            return fanout.remove_subscriber(str(body.get("subscriber_id") or ""))
        if op == "trade_fanout_status":
            return fanout.status()
        if op == "trade_fanout_cleanup":
            return fanout.timeout_cleanup()
        return {"ok": False, "error": f"unknown_op:{op}"}


def _json_response(handler: BaseHTTPRequestHandler, code: int, payload: Any) -> None:
    body = json.dumps(payload, default=str).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def make_handler(service: CollectorControlService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            logger.debug("control_api " + fmt, *args)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            try:
                if path == "/api/collector/forming":
                    qs = parse_qs(parsed.query)
                    symbol = (qs.get("symbol") or [None])[0]
                    _json_response(self, 200, service.forming(symbol))
                    return
                if path == "/api/collector/status":
                    _json_response(self, 200, service.status())
                    return
                if path == "/api/trade_fanout/trade_fanout_status":
                    _json_response(self, 200, service.trade_fanout_op("trade_fanout_status", {}))
                    return
                if path == "/api/collector/desired_state":
                    _json_response(self, 200, service.get_desired())
                    return
                if path == "/api/signals":
                    self._handle_signals(parsed)
                    return
                if path == "/api/research/1m_timing_signals":
                    self._handle_research_1m_timing(parsed)
                    return
                if path == "/healthz":
                    _json_response(self, 200, {"ok": True})
                    return
                _json_response(self, 404, {"error": "not_found", "path": path})
            except Exception as exc:  # noqa: BLE001
                logger.exception("GET failed: %s", exc)
                _json_response(self, 500, {"error": str(exc)})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8") or "{}")
                if path == "/api/collector/desired_state":
                    desired = body.get("desired_state")
                    if desired is None:
                        _json_response(
                            self, 400, {"error": "desired_state required"}
                        )
                        return
                    payload = service.set_desired(str(desired), reason="http_post")
                    _json_response(self, 200, payload)
                    return
                if path == "/api/collector/ensure_symbol":
                    symbol = body.get("symbol")
                    if not symbol:
                        _json_response(self, 400, {"error": "symbol required"})
                        return
                    payload = service.ensure_symbol(str(symbol))
                    _json_response(self, 200, payload)
                    return
                trade_ops = {
                    "/api/trade_fanout/create_trade_subscriber": "create_trade_subscriber",
                    "/api/trade_fanout/poll_trade_events": "poll_trade_events",
                    "/api/trade_fanout/trade_subscriber_heartbeat": "trade_subscriber_heartbeat",
                    "/api/trade_fanout/remove_trade_subscriber": "remove_trade_subscriber",
                    "/api/trade_fanout/trade_fanout_status": "trade_fanout_status",
                    "/api/trade_fanout/trade_fanout_cleanup": "trade_fanout_cleanup",
                }
                if path in trade_ops:
                    payload = service.trade_fanout_op(trade_ops[path], body)
                    code = 200 if payload.get("ok") else 400
                    _json_response(self, code, payload)
                    return
                _json_response(self, 404, {"error": "not_found", "path": path})
            except ValueError as exc:
                _json_response(self, 400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                logger.exception("POST failed: %s", exc)
                _json_response(self, 500, {"error": str(exc)})

        def _handle_signals(self, parsed) -> None:
            if service.signals is None:
                _json_response(self, 503, {"error": "signals_repo_unavailable"})
                return
            qs = parse_qs(parsed.query)
            symbol = (qs.get("symbol") or [None])[0]
            start_s = (qs.get("start") or [None])[0]
            end_s = (qs.get("end") or [None])[0]
            timeframe = (qs.get("timeframe") or [None])[0]
            direction = (qs.get("direction") or [None])[0]
            tier_a_s = (qs.get("tier_a") or [None])[0]
            selected_s = (qs.get("selected") or [None])[0]
            strategy_version_s = (qs.get("strategy_version") or [None])[0]
            time_field = (qs.get("time_field") or ["candle_close_time"])[0]
            limit_s = (qs.get("limit") or ["200"])[0]
            offset_s = (qs.get("offset") or ["0"])[0]
            page_s = (qs.get("page") or [None])[0]
            page_size_s = (qs.get("page_size") or [None])[0]

            def _parse(ts: str) -> datetime:
                text = ts.strip()
                if text.endswith("Z"):
                    text = text[:-1] + "+00:00"
                return ensure_utc(datetime.fromisoformat(text))

            # Feed / dashboard mode: start+end required; symbol optional
            if start_s and end_s:
                from signal_generator.pipeline.outcome_eval import (
                    HORIZON_TRADE,
                    HORIZON_TRADE_NO_BE50,
                    summarize_trade_views,
                )
                from signal_generator.pipeline.versions import (
                    STRATEGY_VERSION_BE50_FROZEN,
                    STRATEGY_VERSION_NO_BE50,
                )

                start = _parse(start_s)
                end = _parse(end_s)
                try:
                    limit = int(limit_s)
                except ValueError:
                    limit = 200
                try:
                    offset = int(offset_s)
                except ValueError:
                    offset = 0
                if page_s is not None and page_size_s is not None:
                    try:
                        page = max(1, int(page_s))
                        page_size = max(1, min(int(page_size_s), 500))
                        limit = page_size
                        offset = (page - 1) * page_size
                    except ValueError:
                        pass

                tier_a: bool | None = None
                if tier_a_s is not None and str(tier_a_s).strip() != "":
                    tier_a = str(tier_a_s).lower() in ("1", "true", "yes")
                selected: bool | None = None
                if selected_s is not None and str(selected_s).strip() != "":
                    selected = str(selected_s).lower() in ("1", "true", "yes")

                # Default active strategy = NO_BE50. Optional frozen BE50 research view.
                sv_raw = (strategy_version_s or STRATEGY_VERSION_NO_BE50).strip()
                if sv_raw in ("", "active", "default", "no_be50", "NO_BE50"):
                    strategy_version = STRATEGY_VERSION_NO_BE50
                elif sv_raw in ("be50", "BE50", "frozen"):
                    strategy_version = STRATEGY_VERSION_BE50_FROZEN
                else:
                    strategy_version = sv_raw

                use_no_be50 = strategy_version == STRATEGY_VERSION_NO_BE50
                outcome_horizon = HORIZON_TRADE_NO_BE50 if use_no_be50 else HORIZON_TRADE

                symbols = [symbol.upper()] if symbol else None
                # Do not filter signals by strategy_version: historical Tier-A rows keep
                # frozen tag; dashboard strategy selects the *exit outcome horizon*.
                rows, total = service.signals.query_signals(
                    start=start,
                    end=end,
                    symbols=symbols,
                    timeframe=timeframe or None,
                    direction=direction or None,
                    tier_a=tier_a,
                    selected=selected,
                    time_field=time_field or "candle_close_time",
                    limit=limit,
                    offset=offset,
                )
                out = [_signal_row_to_api(r) for r in rows]

                def _attach(items: list[dict[str, Any]], views: dict) -> None:
                    for item in items:
                        view = views.get(str(item["signal_id"]))
                        if view is not None:
                            api = view.as_api()
                            if use_no_be50:
                                # Productive NO_BE50: WIN/LOSS/OPEN only
                                api["result"] = view.result
                                api["frozen_result"] = view.result
                                api["display_result"] = view.result
                                api["be50_activated"] = False
                                api["counterfactual_no_be_result"] = None
                            item.update(api)
                            item["outcome_horizon"] = outcome_horizon
                            item["active_strategy_version"] = strategy_version
                        else:
                            item.setdefault("result", RESULT_OPEN)
                            item.setdefault("frozen_result", RESULT_OPEN)
                            item.setdefault("display_result", RESULT_OPEN)
                            item.setdefault("pnl_pct", None)
                            item.setdefault("duration_seconds", None)
                            item.setdefault("exit_time", None)
                            item.setdefault("exit_price", None)
                            item.setdefault("exit_reason", None)
                            item.setdefault("be50_activated", False)
                            item.setdefault("outcome_horizon", outcome_horizon)
                            item.setdefault("active_strategy_version", strategy_version)

                summary = {
                    "signals": total,
                    "wins": 0,
                    "losses": 0,
                    "open": total,
                    "be": 0,
                    "win_rate_pct": None,
                    "gross_profit_pct": 0.0,
                    "gross_loss_pct": 0.0,
                    "total_pnl_pct": 0.0,
                    "pnl_basis": "gross",
                    "strategy_version": strategy_version,
                    "outcome_horizon": outcome_horizon,
                }

                if service.outcomes is not None:
                    try:
                        # Page outcomes
                        if out:
                            page_views = service.outcomes.get_outcomes_by_signal_ids(
                                [r["signal_id"] for r in out],
                                horizon=outcome_horizon,
                            )
                            _attach(out, page_views)

                        # Full-filter summary (ignores pagination)
                        all_views_list = []
                        off_s = 0
                        page_sz = 2000
                        while off_s < total:
                            all_rows, _ = service.signals.query_signals(
                                start=start,
                                end=end,
                                symbols=symbols,
                                timeframe=timeframe or None,
                                direction=direction or None,
                                tier_a=tier_a,
                                selected=selected,
                                time_field=time_field or "candle_close_time",
                                limit=page_sz,
                                offset=off_s,
                            )
                            if not all_rows:
                                break
                            vmap = service.outcomes.get_outcomes_by_signal_ids(
                                [r["signal_id"] for r in all_rows],
                                horizon=outcome_horizon,
                            )
                            for r in all_rows:
                                all_views_list.append(vmap.get(str(r["signal_id"])))
                            off_s += len(all_rows)
                            if len(all_rows) < page_sz:
                                break
                        summary = summarize_trade_views(all_views_list)
                        summary["strategy_version"] = strategy_version
                        summary["outcome_horizon"] = outcome_horizon
                    except Exception:  # noqa: BLE001
                        logger.exception("trade outcome join/summary failed")
                        for item in out:
                            item.setdefault("result", RESULT_OPEN)
                            item.setdefault("display_result", RESULT_OPEN)

                page_size = limit
                page = (offset // page_size) + 1 if page_size else 1
                _json_response(
                    self,
                    200,
                    {
                        "count": len(out),
                        "total": total,
                        "page": page,
                        "page_size": page_size,
                        "offset": offset,
                        "strategy_version": strategy_version,
                        "outcome_horizon": outcome_horizon,
                        "summary": summary,
                        "signals": out,
                        "items": out,
                    },
                )
                return

            _json_response(
                self,
                400,
                {"error": "start and end query params required (ISO-8601 UTC)"},
            )

        def _handle_research_1m_timing(self, parsed) -> None:
            """Read-only research feed. Never writes signals/outcomes."""
            if service.signals is None or service.candles is None:
                _json_response(
                    self,
                    503,
                    {"error": "research_feed_unavailable", "detail": "signals/candles repo missing"},
                )
                return
            qs = parse_qs(parsed.query)

            def _parse(ts: str) -> datetime:
                text = ts.strip()
                if text.endswith("Z"):
                    text = text[:-1] + "+00:00"
                return ensure_utc(datetime.fromisoformat(text))

            start_s = (qs.get("start") or [None])[0]
            end_s = (qs.get("end") or [None])[0]
            if not start_s or not end_s:
                _json_response(
                    self,
                    400,
                    {"error": "start and end query params required (ISO-8601 UTC)"},
                )
                return

            from signal_generator.research.one_m_entry_timing.constants import (
                DASHBOARD_VARIANT_CHOICES,
                DEFAULT_RESEARCH_DISPLAY_VARIANT,
                TIMING_VARIANTS,
            )
            from signal_generator.research.one_m_entry_timing.feed import (
                build_research_timing_feed,
            )

            variant = (qs.get("timing_variant") or [DEFAULT_RESEARCH_DISPLAY_VARIANT])[0]
            variant = str(variant or DEFAULT_RESEARCH_DISPLAY_VARIANT).strip()
            if variant not in TIMING_VARIANTS:
                _json_response(
                    self,
                    400,
                    {"error": "invalid_timing_variant", "allowed": list(TIMING_VARIANTS)},
                )
                return
            # Baseline not offered as active dashboard default source
            if variant == "BASELINE_IMMEDIATE":
                # Allowed for audit compare API, but flagged
                pass

            symbol = (qs.get("symbol") or [None])[0]
            timeframe = (qs.get("timeframe") or [None])[0]
            direction = (qs.get("direction") or [None])[0]
            limit_s = (qs.get("limit") or ["300"])[0]
            offset_s = (qs.get("offset") or ["0"])[0]
            try:
                limit = max(1, min(int(limit_s), 2000))
            except ValueError:
                limit = 300
            try:
                offset = max(0, int(offset_s))
            except ValueError:
                offset = 0

            start = _parse(start_s)
            end = _parse(end_s)
            symbols = [symbol.upper()] if symbol else None

            # Tier-A only as audit seed (read-only)
            rows, total = service.signals.query_signals(
                start=start,
                end=end,
                symbols=symbols,
                timeframe=timeframe or None,
                direction=direction or None,
                tier_a=True,
                selected=None,
                time_field="candle_close_time",
                limit=limit,
                offset=offset,
            )
            api_rows = [_signal_row_to_api(r) for r in rows]

            def _load(sym: str, a: datetime, b: datetime):
                import pandas as pd

                raw = service.candles.get_candles(sym, a, b)
                if not raw:
                    return pd.DataFrame()
                return pd.DataFrame(raw)

            research = build_research_timing_feed(
                api_rows,
                load_candles=_load,
                timing_variant=variant,
                as_of=None,
            )
            # Summary over research triggered entries only
            wins = sum(1 for r in research if r.get("result") == "WIN")
            losses = sum(1 for r in research if r.get("result") == "LOSS")
            pending = sum(
                1
                for r in research
                if str(r.get("trigger_state") or "").startswith("WAITING")
                or r.get("trigger_state") == "NO_ENTRY_TIMEOUT"
            )
            opened = sum(1 for r in research if r.get("trigger_state") == "ENTRY_TRIGGERED" and r.get("result") == "OPEN")
            closed = wins + losses
            gp = sum(float(r["pnl_pct"]) for r in research if r.get("result") == "WIN" and r.get("pnl_pct") is not None)
            gl = sum(float(r["pnl_pct"]) for r in research if r.get("result") == "LOSS" and r.get("pnl_pct") is not None)
            summary = {
                "signals": len(research),
                "wins": wins,
                "losses": losses,
                "open": opened + pending,
                "pending": pending,
                "entry_triggered": sum(1 for r in research if r.get("trigger_state") == "ENTRY_TRIGGERED"),
                "win_rate_pct": (100.0 * wins / closed) if closed else None,
                "gross_profit_pct": gp,
                "gross_loss_pct": gl,
                "total_pnl_pct": gp + gl,
                "pnl_basis": "gross",
                "strategy_version": "research_1m_timing",
                "timing_variant": variant,
                "production_strategy_unchanged": True,
            }
            page_size = limit
            page = (offset // page_size) + 1 if page_size else 1
            _json_response(
                self,
                200,
                {
                    "count": len(research),
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                    "offset": offset,
                    "feed_source": "RESEARCH_1M_TIMING",
                    "timing_variant": variant,
                    "variant_choices": [{"id": a, "label": b} for a, b in DASHBOARD_VARIANT_CHOICES],
                    "production_strategy_unchanged": True,
                    "writes_production_signals": False,
                    "summary": summary,
                    "signals": research,
                    "items": research,
                },
            )

    return Handler


def _signal_row_to_api(r: dict[str, Any]) -> dict[str, Any]:
    direction = str(r.get("direction") or "")
    marker = "▲" if direction == "LONG" else ("▼" if direction == "SHORT" else "")

    def _iso(v: Any) -> str | None:
        if v is None:
            return None
        if hasattr(v, "isoformat"):
            return v.isoformat()
        return str(v)

    from signal_generator.pipeline.trade_plan import parse_trade_plan_from_metadata

    plan = parse_trade_plan_from_metadata(r.get("metadata"))

    price = r.get("signal_price")
    try:
        price_out: float | str | None = float(price) if price is not None else None
    except (TypeError, ValueError):
        price_out = str(price) if price is not None else None

    entry_price = plan.get("entry_price")
    if entry_price is None and price_out is not None:
        try:
            entry_price = float(price_out) if float(price_out) > 0 else None
        except (TypeError, ValueError):
            entry_price = None

    return {
        "signal_id": str(r.get("signal_id")),
        "symbol": r.get("symbol"),
        "timeframe": r.get("timeframe"),
        "direction": direction,
        "signal_type": r.get("signal_type"),
        "signal_price": price_out,
        "entry_price": entry_price,
        "entry_time": plan.get("entry_time"),
        "entry_valid": plan.get("entry_valid"),
        "tp_pct": plan.get("tp_pct"),
        "sl_pct": plan.get("sl_pct"),
        "tp_price": plan.get("tp_price"),
        "sl_price": plan.get("sl_price"),
        "be_trigger_price": plan.get("be_trigger_price"),
        "break_even_price": plan.get("break_even_price"),
        "price_source": plan.get("price_source"),
        "candle_open_time": _iso(r.get("candle_open_time")),
        "candle_close_time": _iso(r.get("candle_close_time")),
        "generated_at": _iso(r.get("generated_at")),
        "tier_a": bool(r.get("tier_a")),
        "tier_a_context": r.get("tier_a_context") or "",
        "stoch_k": r.get("stoch_k"),
        "stoch_d": r.get("stoch_d"),
        "wave_state": r.get("wave_state"),
        "rank_score": r.get("rank_score"),
        "selected": bool(r.get("selected")),
        "selection_reason": r.get("selection_reason") or "",
        "trend_15m": r.get("trend_15m"),
        "trend_30m": r.get("trend_30m"),
        "trend_1h": r.get("trend_1h"),
        "trend_4h": r.get("trend_4h"),
        "signal_bias": r.get("signal_bias"),
        "traded": bool(r.get("traded")),
        "generator_version": r.get("generator_version"),
        "strategy_version": r.get("strategy_version"),
        "marker": marker,
        "metadata": r.get("metadata"),
    }


def start_control_api(
    service: CollectorControlService,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            f"control API must bind localhost only for now, got host={host!r}"
        )
    handler = make_handler(service)
    httpd = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=httpd.serve_forever, name="collector-control-api", daemon=True)
    thread.start()
    logger.info("control API listening on http://%s:%s", host, port)
    return httpd, thread

"""Lazy historical heatmap payloads. One composite load, one close per request."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from data.models import ensure_utc
from indicators.live_micro_lld_v2.availability import bar_close

from ..phase11u_b.adapter import (
    READY,
    AdapterStop,
    CatalogRow,
    candle_window_status,
    check_membership,
    project_active_pools,
    resolve_publication,
)
from ..phase11u_d.multi_file import CompositeCandleCache, FilePart
from ..remaining_pool_projection_v1_config import STRENGTH_CLASS_COLORS

ROOT = Path(__file__).resolve().parents[1]
CHART_BARS = 288
COMPOSITE_ID = "4b509b6565b06c1e37a09f195f001b55e7517de29355a0482871798472de7d31"
BASE_ID = "dba0ebaf41432e81177b8dc0c52dd252f9302506a72833b5f049290bd8509ebc"
BASE_PRICE = "093b2bd57f6525a7886c0bc3c76cd27c24ef4e51d640f155fe410b435e4acd28"
BASE_REV = "df049cde8eb41cf916847b9063f7f4ddd1a894d574ba713e9568c2b4c8bf4f25"
TAIL_ID = "79a5e932749c8539d9caf0ff24911741b4492f6c08de9ba1ae187492233f8394"
TAIL_PRICE = "deeabd3fb8228e51b73cddd5da71cd847bc3197ab60e6f1c09106ee988c25c1f"
TAIL_REV = "a139f4c7c14e8c7b52cd79d7e02c5448a81b05278ce8b08bc4e3bd50f4f9ecea"
GRID_START = datetime(2026, 9, 24, tzinfo=timezone.utc)
GRID_END = datetime(2026, 9, 30, 23, 55, tzinfo=timezone.utc)
ORACLES = {
    "2026-09-24T00:00:00Z": (279, 6302),
    "2026-09-24T12:00:00Z": (281, 6364),
    "2026-09-28T12:00:00Z": (308, 6984),
    "2026-09-30T12:00:00Z": (310, 7055),
    "2026-09-30T12:05:00Z": (312, 7103),
    "2026-09-30T23:55:00Z": (315, 7168),
}


class ProviderError(AdapterStop):
    pass


class PayloadCache:
    def __init__(self, max_entries: int = 8) -> None:
        self.max_entries = max_entries
        self.hits = 0
        self.misses = 0
        self._store: dict = {}
        self._order: list = []

    def get(self, key):
        if key in self._store:
            self.hits += 1
            return self._store[key]
        self.misses += 1
        return None

    def put(self, key, value) -> None:
        if key in self._store:
            return
        if len(self._order) >= self.max_entries:
            self._store.pop(self._order.pop(0), None)
        self._store[key] = value
        self._order.append(key)


def decision_grid() -> list[datetime]:
    out = []
    cursor = GRID_START
    while cursor <= GRID_END:
        out.append(cursor)
        cursor += timedelta(minutes=5)
    return out


def iso(value: datetime) -> str:
    return ensure_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_close(value: str) -> datetime:
    text = str(value).strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ProviderError("INVALID_REQUEST", "decision close is not a UTC timestamp") from exc
    if parsed.tzinfo is None:
        raise ProviderError("INVALID_REQUEST", "decision close must include a timezone")
    parsed = parsed.astimezone(timezone.utc).replace(microsecond=0)
    if parsed.second != 0 or parsed.minute % 5 != 0:
        raise ProviderError("INVALID_REQUEST", "decision close is not a 5m boundary")
    return parsed


def neighbor(closes: list[str], current: str, step: int) -> str:
    if current not in closes:
        raise ProviderError("INVALID_REQUEST", "decision close is not published")
    index = closes.index(current) + int(step)
    if index < 0 or index >= len(closes):
        raise ProviderError("INVALID_REQUEST", "no neighboring close")
    return closes[index]


def assert_supported(symbol: str, timeframe: str, pool_detail: int) -> None:
    if str(symbol).upper() != "DOGEUSDT" or timeframe != "5m" or int(pool_detail) != 10:
        raise ProviderError("INVALID_REQUEST", "only DOGEUSDT 5m PD10 is available")


def _select_manifest(snapshot_id: str, expected: dict) -> Path:
    chosen = None
    for path in sorted(ROOT.glob(f"research_runs/**/{snapshot_id}.manifest.json")):
        body = json.loads(path.read_text(encoding="utf-8"))
        if all(body.get(key) == value for key, value in expected.items()):
            chosen = path.parent
            break
    if chosen is None:
        raise ProviderError("MISSING_IMMUTABLE_INPUT", f"no manifest for {snapshot_id}")
    return chosen


def load_cases() -> list[dict]:
    report = ROOT / "phase11u_e" / "run_result.json"
    if not report.is_file():
        raise ProviderError("MISSING_PUBLICATION", "phase 11U-E case report is missing")
    body = json.loads(report.read_text(encoding="utf-8"))
    cases = []
    for row in body.get("full_examples") or []:
        if row.get("remaining_status") != "FULLY_TRAVERSED" or row.get("original_active") is not True:
            continue
        cases.append(
            {
                "pool_id": row["pool_id"],
                "side": row["side"],
                "bottom": row["bottom"],
                "top": row["top"],
                "decision_close": row["decision_close"],
                "wick_high": row.get("wick_high"),
                "wick_low": row.get("wick_low"),
                "original_active": True,
                "remaining_visibility": "NONE",
                "strength": "NOT_PROVEN",
                "strength_class": "NOT_PROVEN",
            }
        )
    if len(cases) != 10:
        raise ProviderError("INVALID_REQUEST", f"expected 10 stored full-traversal cases, found {len(cases)}")
    return cases


def payload_fingerprint(payload: dict) -> str:
    rows = sorted(
        (row["pool_id"], row["bottom"], row["top"], row["strength"], row["strength_class"])
        for row in payload["original"]
    )
    raw = json.dumps(
        {
            "decision_close": payload["decision_close"],
            "snapshot_id": payload["snapshot_id"],
            "composite_input_id": payload["composite_input_id"],
            "original": rows,
            "remaining": len(payload["remaining"]),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _chart_candles(candles, decision: datetime) -> list[dict]:
    dc_unix = int(decision.timestamp())
    view = []
    for candle in candles:
        open_unix = int(ensure_utc(candle.timestamp).timestamp())
        if int(bar_close(ensure_utc(candle.timestamp), "5m").timestamp()) > dc_unix:
            continue
        if open_unix + 300 != int(bar_close(ensure_utc(candle.timestamp), "5m").timestamp()):
            raise ProviderError("HISTORICAL_CAUSALITY", "candle close does not match the 5m boundary")
        view.append(candle)
    view = view[-CHART_BARS:]
    out = [
        {
            "time": int(ensure_utc(candle.timestamp).timestamp()),
            "open": float(candle.open),
            "high": float(candle.high),
            "low": float(candle.low),
            "close": float(candle.close),
        }
        for candle in view
    ]
    if any(row["time"] + 300 > dc_unix for row in out):
        raise ProviderError("HISTORICAL_CAUSALITY", "chart contains a future candle")
    return out


def build_chart_payload(
    *,
    decision_close: datetime,
    snapshot_id: str,
    composite_id: str,
    pipeline_line_ids: list[str],
    pools: list[CatalogRow],
    candles,
    focus_pool_id: str | None = None,
) -> dict:
    decision = ensure_utc(decision_close)
    projected = project_active_pools(pools, candles, decision_close=decision, timeframe="5m")
    chart = _chart_candles(candles, decision)
    if not chart:
        raise ProviderError("INSUFFICIENT_CANDLE_COVERAGE", "no closed chart candles")
    chart_start = chart[0]["time"]
    original = []
    remaining = []
    for row in projected["active"]:
        known_unix = int(datetime.strptime(row["known_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
        if known_unix > int(decision.timestamp()):
            raise ProviderError("HISTORICAL_CAUSALITY", "pool is known after the decision close")
        t0 = max(known_unix, chart_start)
        original.append({**row, "t0": t0})
    active_ids = {row["pool_id"] for row in projected["active"]}
    for row in projected["remaining"]:
        if row["pool_id"] not in active_ids or row["pool_id"] in projected["fully_traversed_active_ids"]:
            raise ProviderError("REMAINING_POOL_LAYER", "remaining box is not an active partial pool")
        source = next(item for item in original if item["pool_id"] == row["pool_id"])
        if row["bottom"] < source["bottom"] - 1e-9 or row["top"] > source["top"] + 1e-9:
            raise ProviderError("FIXED_SEGMENT_RENDER", "remaining box left the original bounds")
        if row["color"] not in STRENGTH_CLASS_COLORS.values():
            raise ProviderError("STRENGTH_COLOR_RENDER", "remaining color is not an original strength color")
        remaining.append({**row, "t0": source["t0"], "side": source["side"]})
    payload = {
        "symbol": "DOGEUSDT",
        "timeframe": "5m",
        "pool_detail": 10,
        "decision_close": iso(decision),
        "snapshot_id": snapshot_id,
        "composite_input_id": composite_id,
        "pipeline_line_ids": list(pipeline_line_ids),
        "chart_end_unix": int(decision.timestamp()),
        "candles": chart,
        "original": original,
        "remaining": remaining,
        "fully_traversed_active_ids": list(projected["fully_traversed_active_ids"]),
        "focus_pool_id": focus_pool_id,
        "clickhouse_candle_reads": 0,
    }
    payload["fingerprint"] = payload_fingerprint(payload)
    return payload


class HistoricalHeatmapSession:
    def __init__(self, source) -> None:
        self.source = source
        base = _select_manifest(
            BASE_ID,
            {
                "snapshot_id": BASE_ID,
                "ohlcv_content_hash": BASE_PRICE,
                "revision_metadata_hash": BASE_REV,
                "row_count": 187596,
                "range_start": "2026-05-23T05:25:00.000Z",
                "range_end_exclusive": "2026-09-30T12:01:00.000Z",
            },
        )
        tail = _select_manifest(
            TAIL_ID,
            {
                "snapshot_id": TAIL_ID,
                "ohlcv_content_hash": TAIL_PRICE,
                "revision_metadata_hash": TAIL_REV,
                "row_count": 714,
                "range_start": "2026-09-30T12:01:00.000Z",
                "range_end_exclusive": "2026-09-30T23:55:00.000Z",
            },
        )
        self.parts = [
            FilePart(base, BASE_ID, BASE_PRICE, BASE_REV),
            FilePart(tail, TAIL_ID, TAIL_PRICE, TAIL_REV),
        ]
        self.candles_cache = CompositeCandleCache(max_entries=2)
        loaded = self.candles_cache.get(self.parts, "5m")
        if loaded.composite_id != COMPOSITE_ID or loaded.clickhouse_candle_reads != 0:
            raise ProviderError("INPUT_IDENTITY_MISMATCH", "composite identity does not match")
        self.loaded = loaded
        self.payloads = PayloadCache()
        self.closes = self._published_closes()
        self.cases = load_cases()
        self._enrich_cases()

    def _published_closes(self) -> list[str]:
        expected = [iso(item) for item in decision_grid()]
        rows = self.source.closes()
        if rows != expected:
            raise ProviderError("MISSING_PUBLICATION", "published closes do not match the 2016-close grid")
        return rows

    def index(self) -> dict:
        return {
            "symbol": "DOGEUSDT",
            "timeframe": "5m",
            "pool_detail": 10,
            "composite_input_id": self.loaded.composite_id,
            "closes": self.closes,
            "cases": self.cases,
        }

    def _enrich_cases(self) -> None:
        for case in self.cases:
            payload = self.payload_for(case["decision_close"], focus_pool_id=case["pool_id"])
            match = next((row for row in payload["original"] if row["pool_id"] == case["pool_id"]), None)
            if match is None:
                continue
            case["strength"] = match["strength"]
            case["strength_class"] = match["strength_class"]
            case["in_original"] = True
            case["remaining_boxes"] = sum(1 for row in payload["remaining"] if row["pool_id"] == case["pool_id"])

    def payload_for(self, decision_close: str, focus_pool_id: str | None = None) -> dict:
        assert_supported("DOGEUSDT", "5m", 10)
        decision = parse_close(decision_close)
        stamp = iso(decision)
        if stamp not in self.closes:
            raise ProviderError("INVALID_REQUEST", "decision close is not in the published set")
        key = (self.loaded.composite_id, stamp, focus_pool_id)
        cached = self.payloads.get(key)
        if cached is not None:
            return cached
        hits = self.source.find(symbol="DOGEUSDT", timeframe="5m", pool_detail=10, decision_close=decision)
        snapshot_id, fingerprint, lines = resolve_publication(hits)
        members = self.source.members(snapshot_id)
        catalog = self.source.catalog(snapshot_id)
        check_membership(snapshot_id, fingerprint, members, catalog)
        if self.loaded.clickhouse_candle_reads != 0:
            raise ProviderError("INVALID_REQUEST", "clickhouse candle fallback")
        status = candle_window_status(
            range_start=self.loaded.range_start,
            range_end_exclusive=self.loaded.range_end_exclusive,
            decision_close=decision,
            known_ats=[row.known_at for row in catalog],
            timeframe="5m",
        )
        if status != READY:
            raise ProviderError(status, "composite candle window does not cover this close")
        payload = build_chart_payload(
            decision_close=decision,
            snapshot_id=snapshot_id,
            composite_id=self.loaded.composite_id,
            pipeline_line_ids=list(lines),
            pools=catalog,
            candles=self.loaded.rows,
            focus_pool_id=focus_pool_id,
        )
        again = payload_fingerprint(payload)
        if again != payload["fingerprint"]:
            raise ProviderError("INPUT_IDENTITY_MISMATCH", "payload fingerprint is not stable")
        self.payloads.put(key, payload)
        return payload

"""Causal pool-edge catalog from Raw-OB200 (reuse existing wall extractors)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from orderbook_analyse.l2_wall_attack_discovery.models import tick_size
from orderbook_analyse.ob200_v3_raw_discovery.audit import process_segment
from orderbook_analyse.ob200_v3_raw_discovery.files import list_closed_segments
from orderbook_analyse.ob200_v3_raw_discovery.lifecycles_v2 import WallLifecycle, build_wall_lifecycles
from orderbook_analyse.ob200_v3_raw_discovery.reconstruct import SampleRow
from orderbook_analyse.ob200_v3_raw_discovery.walls import extract_wall_events


DEFAULT_RAW_ROOT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/ob200_v3"
)


@dataclass
class CausalEdge:
    edge_id: str
    symbol: str
    wall_side: str  # ASK | BID
    edge_price: float
    tick_size: float
    first_seen_ts: datetime
    edge_observed_ts: datetime  # same as first_seen for WALL_APPEAR
    edge_available_ts: datetime  # first causal availability
    edge_source: str
    edge_source_event_id: str
    initial_notional: float
    data_quality_status: str
    causal_eligible: bool
    causal_rejection_reason: Optional[str] = None
    # filled as-of a specific attack by matcher (not stored globally)
    source_file: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("first_seen_ts", "edge_observed_ts", "edge_available_ts"):
            v = getattr(self, k)
            d[k] = v.isoformat().replace("+00:00", "Z") if v else None
        return d


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def _wall_fields(sample: SampleRow, side: str) -> tuple[Optional[float], Optional[float]]:
    if side == "BID":
        return sample.bid_wall_price, sample.bid_wall_qty
    return sample.ask_wall_price, sample.ask_wall_qty


def sample_at_or_before(samples: list[SampleRow], ts_ms: int) -> Optional[SampleRow]:
    if not samples:
        return None
    lo, hi = 0, len(samples) - 1
    ans = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if samples[mid].ts_ms <= ts_ms:
            ans = samples[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def wall_present_asof(
    sample: Optional[SampleRow],
    *,
    side: str,
    edge_price: float,
    symbol: str,
    match_bps: float = 2.0,
) -> tuple[bool, Optional[float], Optional[float]]:
    """Return (present, qty, mid) if wall price still matches at sample."""
    if sample is None or sample.mid is None or sample.mid <= 0:
        return False, None, None
    px, qty = _wall_fields(sample, side)
    if px is None or qty is None or qty <= 0:
        return False, None, None
    dist = abs(px - edge_price) / sample.mid * 1e4
    if dist > match_bps:
        return False, None, sample.mid
    return True, float(qty), float(sample.mid)


def load_ob200_samples(
    *,
    symbols: tuple[str, ...],
    start: datetime,
    end: datetime,
    raw_root: Path = DEFAULT_RAW_ROOT,
    sample_ms: int = 250,
    seed: int = 42,
) -> tuple[dict[str, list[SampleRow]], list[dict[str, Any]], int]:
    """Replay closed OB200 segments → causal samples. Returns (samples, segment_meta, n_ok)."""
    segments = list_closed_segments(
        raw_root, symbols=symbols, start=start, end=end, include_boundary_stubs=False
    )
    samples_by: dict[str, list[SampleRow]] = {s: [] for s in symbols}
    meta: list[dict[str, Any]] = []
    n_ok = 0
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    for i, ref in enumerate(segments):
        audit, samples = process_segment(
            ref, collect_samples=True, sample_ms=sample_ms, warmup_ms=60_000
        )
        usable = audit.replay_verdict in {
            "REPLAY_CONFIRMED",
            "REPLAY_CONFIRMED_FROM_LOCAL_CHECKPOINT",
            "PARTIAL_BUT_DISCOVERY_USABLE",
        }
        meta.append(
            {
                "path": str(ref.path),
                "symbol": ref.symbol,
                "replay_verdict": audit.replay_verdict,
                "usable": usable,
                "n_samples_raw": len(samples),
            }
        )
        if not usable:
            continue
        n_ok += 1
        kept = [s for s in samples if start_ms <= s.ts_ms < end_ms]
        samples_by[ref.symbol].extend(kept)
    for sym in symbols:
        samples_by[sym].sort(key=lambda s: s.ts_ms)
    return samples_by, meta, n_ok


def build_causal_edges_from_samples(
    samples_by: dict[str, list[SampleRow]],
    *,
    seed: int = 42,
) -> tuple[list[CausalEdge], list[WallLifecycle], list[dict[str, Any]]]:
    """Extract WALL_APPEAR-based edges via existing causal detectors (no future sizing)."""
    all_events = []
    for i, (sym, samples) in enumerate(samples_by.items()):
        if not samples:
            continue
        all_events.extend(extract_wall_events(samples, seed=seed + i))
    lifecycles = build_wall_lifecycles(all_events, seed=seed)
    edges: list[CausalEdge] = []
    for lc in lifecycles:
        if lc.appear_ts is None or lc.wall_price is None or lc.wall_price <= 0:
            continue
        first = _ms_to_dt(int(lc.appear_ts))
        # initial notional from peak is NOT used as as-of attack size; store appear-time
        # peak_qty may include later growth — mark initial as unknown/0 and fill as-of in matcher.
        tick = tick_size(lc.symbol)
        edges.append(
            CausalEdge(
                edge_id=str(lc.lifecycle_id),
                symbol=str(lc.symbol).upper(),
                wall_side=str(lc.side).upper(),
                edge_price=float(lc.wall_price),
                tick_size=float(tick),
                first_seen_ts=first,
                edge_observed_ts=first,
                edge_available_ts=first,
                edge_source="raw_ob200_wall_lifecycle",
                edge_source_event_id=str(lc.lifecycle_id),
                initial_notional=0.0,  # filled as-of attack from samples
                data_quality_status="OK",
                causal_eligible=True,
                causal_rejection_reason=None,
                source_file=str(lc.source_file or ""),
            )
        )
    event_rows = [
        {
            "lifecycle_id": lc.lifecycle_id,
            "symbol": lc.symbol,
            "side": lc.side,
            "wall_price": lc.wall_price,
            "appear_ts": lc.appear_ts,
            "end_ts": lc.end_ts,
            "completion_class": lc.completion_class,
        }
        for lc in lifecycles
    ]
    return edges, lifecycles, event_rows

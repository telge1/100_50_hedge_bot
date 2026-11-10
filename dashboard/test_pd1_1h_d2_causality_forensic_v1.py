"""Independent 1h lower D2 causality vs API lifetimes + JS sweep (Aug 2026 DOGEUSDT)."""

from __future__ import annotations

import json
import math
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

DASH = Path(__file__).resolve().parent

from pd1_landscape_viewer_v1.historical_window_v1 import (
    WARMUP_HOURS,
    _causal_decision_close,
    _iter_5m,
    _origin_logical_v2_key,
    _parse_utc,
)
from pd1_landscape_viewer_v1.on_demand_1hpd1_v1 import (
    SLOT_MINUTES,
    _active_members_at,
    _load_engine_candles,
    build_landscape_window_on_demand_1hpd1_v1,
)
from research_charts.short_pool_oracle_v1 import parse_decision_iso
from research_charts.trp_import import ensure_trp_on_path


def _parse_known_at(s: str) -> datetime:
    s = str(s).strip()
    if s.endswith("+00:00+00:00"):
        s = s[: -len("+00:00")]
    return parse_decision_iso(s)

STEP_SEC = 300
SLOT_MIN = 60

# Aug 2026 channel / screenshot neighborhood (no screenshot pixels — API price span).
FORENSIC_FROM = "2026-08-24T00:00:00Z"
FORENSIC_TO = "2026-08-27T00:00:00Z"
Y_LO = 0.065
Y_HI = 0.095


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_exact_bands_lower(members: list[dict], y_min: float, y_max: float) -> list[dict]:
    """Mirror JS computeExactBandsForSide (unique origins by origin key)."""
    events: list[tuple[float, int, str, str]] = []
    for pl in members:
        if pl.get("side") != "lower":
            continue
        lo = max(y_min, float(pl["bottom"]))
        hi = min(y_max, float(pl["top"]))
        if hi < lo:
            continue
        pid = pl["pool_id"]
        origin = pl.get("origin") or _origin_logical_v2_key(pid)
        events.append((lo, 1, pid, origin))
        events.append((hi, -1, pid, origin))
    if not events:
        return []
    events.sort(key=lambda e: (e[0], -e[1]))
    active_pools: dict[str, str] = {}
    bands: list[dict] = []
    prev_price: float | None = None
    prev_depth = 0

    def depth_now() -> int:
        return len(set(active_pools.values()))

    for price, delta, pid, origin in events:
        if prev_price is not None and price > prev_price and prev_depth > 0:
            bands.append({"y0": prev_price, "y1": price, "depth": prev_depth})
        if delta > 0:
            active_pools[pid] = origin
        else:
            active_pools.pop(pid, None)
        prev_depth = depth_now()
        prev_price = price
    return bands


def d2_bands_at_dc(members: list[dict], y_min: float, y_max: float) -> list[dict]:
    return [b for b in compute_exact_bands_lower(members, y_min, y_max) if b["depth"] >= 2]


def iter_hourly_dcs(t0: datetime, t1: datetime) -> list[datetime]:
    out: list[datetime] = []
    t = _causal_decision_close(t0, SLOT_MIN)
    end = _causal_decision_close(t1, SLOT_MIN)
    while t <= end:
        out.append(t)
        t += timedelta(minutes=SLOT_MIN)
    return out


def load_raw_pools(vis_from: datetime, vis_to: datetime) -> list[dict]:
    ensure_trp_on_path()
    from indicators.live_micro_lld_v2.engine import run_live_micro_lld_v2
    from research_charts.live_micro_lld_v2_ch_test.identity import build_live_micro_lld_v2_config

    candles, _ = _load_engine_candles(vis_from, vis_to)
    cfg = build_live_micro_lld_v2_config(1)
    result = run_live_micro_lld_v2(candles, cfg)
    return [p.to_dict() for p in result.pools]


def reconstruct_d2_timeline(
    raw_pools: list[dict], context_from: datetime, vis_to: datetime
) -> dict[tuple[int, int], dict]:
    """Key = (bottom*1e8, top*1e8) rounded band; value = per-DC D2 records."""
    by_band: dict[tuple[int, int], dict] = defaultdict(lambda: {"events": [], "origins_by_dc": {}})
    for dc in iter_hourly_dcs(context_from, vis_to):
        members = _active_members_at(raw_pools, dc)
        for b in d2_bands_at_dc(members, Y_LO, Y_HI):
            key = (int(round(b["y0"] * 1e8)), int(round(b["y1"] * 1e8)))
            origins = sorted(
                {
                    m.get("origin") or _origin_logical_v2_key(m["pool_id"])
                    for m in members
                    if m.get("side") == "lower"
                    and float(m["bottom"]) < b["y1"]
                    and float(m["top"]) > b["y0"]
                }
            )
            by_band[key]["events"].append(
                {
                    "dc": _iso_z(dc),
                    "depth": b["depth"],
                    "y0": b["y0"],
                    "y1": b["y1"],
                    "origins": origins,
                    "unique_origins": len(origins),
                }
            )
            by_band[key]["origins_by_dc"][_iso_z(dc)] = origins
    return dict(by_band)


def api_pool_active_at_iso(pl: dict, dc: datetime) -> bool:
    dc_s = _iso_z(dc)
    for seg in pl.get("segments") or []:
        s0 = _parse_utc(seg[0])
        s1 = _parse_utc(seg[1])
        if s0 <= dc <= s1:
            return True
    return False


def d2_from_api_lifetimes(pools: list[dict], t0: datetime, t1: datetime) -> dict[tuple[int, int], list]:
    out: dict[tuple[int, int], list] = defaultdict(list)
    for dc in iter_hourly_dcs(t0, t1):
        active = [
            pl
            for pl in pools
            if pl.get("side") == "lower" and api_pool_active_at_iso(pl, dc)
        ]
        for b in d2_bands_at_dc(active, Y_LO, Y_HI):
            key = (int(round(b["y0"] * 1e8)), int(round(b["y1"] * 1e8)))
            out[key].append({"dc": _iso_z(dc), "depth": b["depth"], **b})
    return dict(out)


def frontend_lower_d2_zones(pools: list[dict], from_utc: str, to_utc: str) -> list[dict]:
    pools_path = DASH / "logs" / "_d2_forensic_pools.json"
    pools_path.write_text(json.dumps(pools), encoding="utf-8")
    t0 = int(_parse_utc(from_utc).timestamp() * 1000)
    t1 = int(_parse_utc(to_utc).timestamp() * 1000)
    js_path = DASH / "logs" / "_d2_forensic_zones.js"
    js_path.write_text(
        f"""
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const adapterPath = path.join({json.dumps(str(DASH))}, 'static/js/research/pd1_exact_density_trp_adapter.js');
const code = fs.readFileSync(adapterPath, 'utf8');
const pools = JSON.parse(fs.readFileSync({json.dumps(str(pools_path))}, 'utf8'));
const sandbox = {{ console, Math, Date, Number, Map, Set, parseInt, parseFloat, JSON }};
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
const AD = sandbox.PD1ExactDensityTrpAdapter;
const zones = AD.poolsToExactOriginZones(pools, {t0}, {t1}, {Y_LO}, {Y_HI}, {{ slotMinutes: 60 }});
const merged = AD.mergeExactOriginSegments(zones);
const lowerD2 = merged.filter(z => z.side === 'lower' && (z.metadata && z.metadata.depth) >= 2);
console.log(JSON.stringify(lowerD2));
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["node", str(js_path)],
        cwd=str(DASH),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout.strip())


def first_d2_dc(timeline: dict[tuple[int, int], list]) -> dict[tuple[int, int], str]:
    return {k: (v[0]["dc"] if v else None) for k, v in timeline.items()}


@pytest.mark.timeout(300)
def test_1h_lower_d2_independent_matches_api_and_frontend():
    """Causal D2 from pools_as_of must match API lifetimes and JS zones (post lifetime_v2)."""
    vis_from = _parse_utc(FORENSIC_FROM)
    vis_to = _parse_utc(FORENSIC_TO)
    payload = build_landscape_window_on_demand_1hpd1_v1(FORENSIC_FROM, FORENSIC_TO)
    api_pools = payload["pools"]
    context_from = _parse_utc(payload["window"]["context_from_utc"])

    raw = load_raw_pools(vis_from, vis_to)

    lookahead_dcs: list[str] = []
    extra_active: list[str] = []
    for dc in iter_hourly_dcs(context_from, vis_to):
        members = _active_members_at(raw, dc)
        api_active = [
            pl
            for pl in api_pools
            if pl.get("side") == "lower" and api_pool_active_at_iso(pl, dc)
        ]
        if d2_bands_at_dc(api_active, Y_LO, Y_HI) and not d2_bands_at_dc(members, Y_LO, Y_HI):
            lookahead_dcs.append(_iso_z(dc))
        m_ids = {p["pool_id"] for p in members if p.get("side") == "lower"}
        a_ids = {p["pool_id"] for p in api_active}
        if a_ids - m_ids:
            extra_active.append(_iso_z(dc))

    # API lifetimes must not active-before-known_at
    birth_violations = []
    for pl in api_pools:
        ka = pl.get("known_at")
        if not ka:
            continue
        birth = _parse_known_at(str(ka))
        for seg in pl.get("segments") or []:
            if _parse_utc(seg[0]) < birth:
                birth_violations.append((pl["pool_id"], ka, seg))

    fe_zones = frontend_lower_d2_zones(api_pools, FORENSIC_FROM, FORENSIC_TO)
    fe_mismatch = []
    for z in fe_zones:
        dc_ms = z.get("metadata", {}).get("decision_close_ms")
        if dc_ms is None:
            continue
        dc = datetime.fromtimestamp(int(dc_ms) / 1000, tz=timezone.utc)
        members = _active_members_at(raw, dc)
        depth = z.get("metadata", {}).get("depth", 0)
        y0, y1 = float(z["bottom_price"]), float(z["top_price"])
        bands = [
            b
            for b in d2_bands_at_dc(members, Y_LO, Y_HI)
            if b["y1"] > y0 and b["y0"] < y1 and b["depth"] == depth
        ]
        if depth >= 2 and not bands:
            fe_mismatch.append(("fe_d2_without_recon", dc_ms, y0, y1, depth))

    report = {
        "lookahead_dcs": lookahead_dcs[:10],
        "extra_active": extra_active[:10],
        "birth_violations": birth_violations[:10],
        "fe_mismatch": fe_mismatch[:10],
        "fe_zone_count": len(fe_zones),
    }
    if lookahead_dcs or extra_active or birth_violations or fe_mismatch:
        Path(DASH / "logs/d2_forensic_report.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
    assert not birth_violations, birth_violations[:3]
    assert not lookahead_dcs, lookahead_dcs[:3]
    assert not extra_active, extra_active[:3]
    assert not fe_mismatch, fe_mismatch[:3]


@pytest.mark.timeout(180)
def test_d2_two_origin_causality_synthetic_pattern():
    """Regression pattern: D2 only when two origins overlap; never before second birth."""
    vis_from = _parse_utc(FORENSIC_FROM)
    vis_to = _parse_utc(FORENSIC_TO)
    raw = load_raw_pools(vis_from, vis_to)
    by_origin: dict[str, dict] = {}
    for p in raw:
        o = _origin_logical_v2_key(p["pool_id"])
        if p.get("side") != "lower":
            continue
        if not str(p.get("pool_id", "")).startswith("lldv2:DOGEUSDT:1h:"):
            continue
        ka = _parse_known_at(str(p["known_at"]))
        if o not in by_origin or ka < _parse_known_at(str(by_origin[o]["known_at"])):
            by_origin[o] = p

    # Find any DC where D2 exists; verify >=2 origins and each known_at <= dc
    context_from = vis_from - timedelta(hours=WARMUP_HOURS)
    violations = []
    for dc in iter_hourly_dcs(context_from, vis_to):
        members = _active_members_at(raw, dc)
        bands = d2_bands_at_dc(members, Y_LO, Y_HI)
        for b in bands:
            contributing = []
            for m in members:
                if m.get("side") != "lower":
                    continue
                if float(m["bottom"]) < b["y1"] and float(m["top"]) > b["y0"]:
                    contributing.append(m)
            origins = {m.get("origin") or _origin_logical_v2_key(m["pool_id"]) for m in contributing}
            if len(origins) < 2:
                violations.append(("depth_without_two_origins", _iso_z(dc), b, len(origins)))
            for m in contributing:
                if _parse_known_at(str(m.get("known_at"))) > dc:
                    violations.append(("active_before_known", _iso_z(dc), m["pool_id"], m.get("known_at")))
    assert not violations, violations[:5]


MARKED_CORRIDORS = (
    ("0.078-0.080", 0.078, 0.080),
    ("0.081-0.083", 0.081, 0.083),
    ("0.083-0.085", 0.083, 0.085),
)


@pytest.mark.timeout(300)
@pytest.mark.parametrize("label,y_lo,y_hi", MARKED_CORRIDORS)
def test_marked_corridor_no_d2_lookahead(label, y_lo, y_hi):
    """Screenshot corridors Aug 20–30: no API/FE D2 before pools_as_of truth."""
    raw = load_raw_pools(_parse_utc("2026-08-20T00:00:00Z"), _parse_utc("2026-08-30T12:00:00Z"))
    api_by_id: dict[str, dict] = {}
    for f, t in (
        ("2026-08-20T00:00:00Z", "2026-08-23T00:00:00Z"),
        ("2026-08-23T00:00:00Z", "2026-08-26T00:00:00Z"),
        ("2026-08-26T00:00:00Z", "2026-08-29T00:00:00Z"),
        ("2026-08-29T00:00:00Z", "2026-08-30T12:00:00Z"),
    ):
        for pl in build_landscape_window_on_demand_1hpd1_v1(f, t)["pools"]:
            api_by_id[pl["pool_id"]] = pl
    api_pools = list(api_by_id.values())
    for dc in iter_hourly_dcs(
        _parse_utc("2026-08-20T00:00:00Z"),
        _parse_utc("2026-08-30T12:00:00Z"),
    ):
        members = _active_members_at(raw, dc)
        recon = d2_bands_at_dc(members, y_lo, y_hi)
        recon_any = any(b["depth"] >= 2 for b in recon)
        api_active = [
            pl for pl in api_pools if pl.get("side") == "lower" and api_pool_active_at_iso(pl, dc)
        ]
        api_any = any(b["depth"] >= 2 for b in d2_bands_at_dc(api_active, y_lo, y_hi))
        assert not api_any or recon_any, (_iso_z(dc), label)


@pytest.mark.timeout(300)
def test_t1_t2_d2_band_causality_example():
    """Origin B at 2026-08-12T22:00Z: no D2 in overlap before that DC; D2 from that DC onward."""
    raw = load_raw_pools(_parse_utc("2026-08-06T00:00:00Z"), _parse_utc("2026-08-16T00:00:00Z"))
    y0, y1 = 0.06794, 0.0682
    t1 = _parse_utc("2026-08-06T14:00:00Z")
    t2 = _parse_utc("2026-08-12T22:00:00Z")
    before_t1 = _parse_utc("2026-08-06T13:00:00Z")
    between = _parse_utc("2026-08-12T21:00:00Z")
    at_t2 = t2
    for dc, expect in [(before_t1, False), (t1, False), (between, False), (at_t2, True)]:
        members = _active_members_at(raw, dc)
        has_d2 = bool(
            [
                b
                for b in d2_bands_at_dc(members, 0.06, 0.1)
                if b["depth"] >= 2 and b["y1"] > y0 and b["y0"] < y1
            ]
        )
        assert has_d2 == expect, (_iso_z(dc), expect, has_d2)



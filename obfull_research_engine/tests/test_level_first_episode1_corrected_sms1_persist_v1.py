"""Corrected sms1 persist + derived-event contract tests for Episode 1.

Golden comparisons use an independent book or live FullBookState.
No source-string asserts. No A-vs-A of the same production function.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_ROOT / "src"))
sys.path.insert(0, str(ENGINE_ROOT.parent / "src"))

from orderbook_analyse.orderbook_v2_live.full_book_state import FullBookState  # noqa: E402

from obfull_research_engine.drilldown.aggregation_100ms import (  # noqa: E402
    book_map_sha256,
    build_states_100ms,
    last_complete_state,
    reconstruct_book_asof_exclusive,
)
from obfull_research_engine.drilldown.engine import _book_at  # noqa: E402
from obfull_research_engine.level_first_episode1_corrected_sms1_persist_v1 import (  # noqa: E402
    ALLOW_ARCHIVE_REPLAY,
    ALLOW_CLICKHOUSE_WRITES,
    FORBIDDEN_FIELDS,
    FROZEN,
    SMS1_EP1_STATES_SHA256,
)
from obfull_research_engine.level_first_episode1_corrected_sms1_persist_v1.persist import (  # noqa: E402
    event_available_at,
    serialize_state,
    write_tables,
)
from obfull_research_engine.level_first_episode1_corrected_sms1_persist_v1.reader import (  # noqa: E402
    last_state_asof,
    load_table,
    reconstruct_from_persist,
)
from obfull_research_engine.market_profile_lld_shared_event_materialization_v1.hashing import (  # noqa: E402
    file_sha256,
)
from obfull_research_engine.paths import ENGINE_ROOT as PKG_ROOT  # noqa: E402

T0 = datetime(2026, 9, 6, 20, 14, 2, 200000, tzinfo=timezone.utc)
EVIDENCE = datetime(2026, 9, 6, 20, 14, 2, 229000, tzinfo=timezone.utc)
DETECTION = datetime(2026, 9, 6, 20, 21, 0, tzinfo=timezone.utc)


class IndependentBook:
    def __init__(self, bids: dict[float, float], asks: dict[float, float], epoch=1):
        self.bids = dict(bids)
        self.asks = dict(asks)
        self.epoch = epoch
        self.update_id = None
        self.seq = None

    def apply_reset(self, bids, asks, epoch=None, update_id=None, seq=None):
        self.bids = {float(p): float(q) for p, q in dict(bids).items() if float(q) > 0}
        self.asks = {float(p): float(q) for p, q in dict(asks).items() if float(q) > 0}
        if epoch is not None:
            self.epoch = epoch
        if update_id is not None:
            self.update_id = update_id
        if seq is not None:
            self.seq = seq

    def apply_change(self, side, price, new_size, epoch=None, update_id=None, seq=None):
        book = self.bids if side == "bid" else self.asks
        if new_size <= 0:
            book.pop(float(price), None)
        else:
            book[float(price)] = float(new_size)
        if epoch is not None:
            self.epoch = epoch
        if update_id is not None:
            self.update_id = update_id
        if seq is not None:
            self.seq = seq

    def top(self):
        return (max(self.bids) if self.bids else None, min(self.asks) if self.asks else None)


def _change(*, ts, side, price, new_size, old_size, u, seq, epoch, order):
    delta = new_size - old_size
    return {
        "event_time": ts,
        "event_type": "level_change",
        "side": side,
        "price": price,
        "old_size": old_size,
        "new_size": new_size,
        "size_delta": delta,
        "notional_delta": delta * price,
        "u": u,
        "seq": seq,
        "epoch": epoch,
        "apply_order": order,
        "source_event_id": f"lvl:{side}:{price}:{u}",
    }


def _reset(*, ts, bids, asks, u, seq, epoch, order, kind="snapshot"):
    return {
        "event_time": ts,
        "event_type": kind,
        "bids": dict(bids),
        "asks": dict(asks),
        "update_id": u,
        "sequence_id": seq,
        "replay_epoch": epoch,
        "source": kind,
        "source_event_id": f"{kind}:{u}:{seq}:{ts.isoformat()}",
        "checkpoint_or_snapshot_id": f"{kind}:{u}:{seq}:{ts.isoformat()}",
        "apply_order": order,
    }


def _build(*, start, end, changes, resets=None, bids=None, asks=None, evidence_start=None, **kw):
    return build_states_100ms(
        window_start=start,
        window_end=end,
        timeline=[],
        level_changes=changes,
        trades=[],
        book_snapshots_by_time=None,
        book_resets=resets,
        initial_bids=dict(bids or {100.0: 1.0}),
        initial_asks=dict(asks or {101.0: 1.0}),
        bucket_ms=100,
        evidence_start=evidence_start,
        **kw,
    )


def _replay_stub(states, changes, resets, bids, asks, start, end, epoch=1):
    return {
        "window_start": start,
        "window_end": end,
        "level_changes": changes,
        "book_resets": resets or [],
        "book_snapshots_by_time": [],
        "checkpoint_before_after": [],
        "initial_bids": dict(bids),
        "initial_asks": dict(asks),
        "initial_update_id": 1,
        "initial_sequence_id": 1,
        "initial_replay_epoch": epoch,
        "initial_checkpoint_id": "warmup",
        "initial_book_event_time_semantics": "event_time_strict_lt_window_start",
    }


def test_01_event_inside_bucket():
    ts = T0 + timedelta(milliseconds=50)
    change = _change(ts=ts, side="bid", price=100.0, new_size=4.0, old_size=1.0, u=2, seq=2, epoch=1, order=1)
    independent = IndependentBook({100.0: 1.0}, {101.0: 1.0})
    independent.apply_change("bid", 100.0, 4.0, epoch=1)
    states = _build(start=T0, end=T0 + timedelta(milliseconds=100), changes=[change])
    assert len(states) == 1
    assert states[0]["best_bid"] == independent.top()[0] == 100.0
    assert states[0]["bid_update_count"] == 1
    assert states[0]["available_at"] == T0 + timedelta(milliseconds=100)
    assert T0 <= ts < states[0]["bucket_end_exclusive"]


def test_02_event_exactly_on_exclusive_bucket_end():
    end_ts = T0 + timedelta(milliseconds=100)
    change = _change(ts=end_ts, side="bid", price=100.0, new_size=7.0, old_size=1.0, u=8, seq=8, epoch=1, order=1)
    states = _build(start=T0, end=T0 + timedelta(milliseconds=200), changes=[change])
    assert states[0]["bid_update_count"] == 0
    assert states[1]["bid_update_count"] == 1
    assert states[1]["best_bid"] == 100.0
    recon = reconstruct_book_asof_exclusive(
        initial_bids={100.0: 1.0},
        initial_asks={101.0: 1.0},
        level_changes=[change],
        until=end_ts,
    )
    assert recon["bids"][100.0] == 1.0


def test_03_partial_first_bucket():
    post = _change(
        ts=EVIDENCE + timedelta(milliseconds=10),
        side="ask",
        price=101.0,
        new_size=3.0,
        old_size=1.0,
        u=2,
        seq=2,
        epoch=1,
        order=2,
    )
    states = _build(
        start=EVIDENCE,
        end=T0 + timedelta(milliseconds=100),
        changes=[post],
        evidence_start=EVIDENCE,
    )
    assert states[0]["effective_bucket_start"] == EVIDENCE
    assert states[0]["bucket_start"] == T0
    assert states[0]["available_at"] == T0 + timedelta(milliseconds=100)
    assert states[0]["ask_update_count"] == 1


def test_04_no_pre_evidence_as_evidence():
    pre = _change(
        ts=T0 + timedelta(milliseconds=10),
        side="bid",
        price=100.0,
        new_size=99.0,
        old_size=1.0,
        u=1,
        seq=1,
        epoch=1,
        order=1,
    )
    post = _change(
        ts=EVIDENCE + timedelta(milliseconds=10),
        side="ask",
        price=101.0,
        new_size=3.0,
        old_size=1.0,
        u=2,
        seq=2,
        epoch=1,
        order=2,
    )
    states = _build(
        start=EVIDENCE,
        end=T0 + timedelta(milliseconds=100),
        changes=[pre, post],
        evidence_start=EVIDENCE,
        bids={100.0: 1.0},
        asks={101.0: 1.0},
    )
    assert states[0]["bid_update_count"] == 0
    assert states[0]["best_bid"] == 100.0
    recon = reconstruct_book_asof_exclusive(
        initial_bids={100.0: 1.0},
        initial_asks={101.0: 1.0},
        level_changes=[pre, post],
        until=T0 + timedelta(milliseconds=100),
        evidence_start=EVIDENCE,
    )
    assert recon["bids"][100.0] == 1.0


def test_05_full_book_reset():
    snap_ts = T0 + timedelta(milliseconds=40)
    reset = _reset(ts=snap_ts, bids={200.0: 2.0}, asks={201.0: 2.0}, u=9, seq=90, epoch=2, order=1)
    independent = IndependentBook({100.0: 1.0}, {101.0: 1.0})
    independent.apply_reset(reset["bids"], reset["asks"], epoch=2)
    states = _build(start=T0, end=T0 + timedelta(milliseconds=100), changes=[], resets=[reset])
    assert states[0]["best_bid"] == independent.top()[0] == 200.0
    assert 100.0 != states[0]["best_bid"]
    assert states[0]["replay_epoch"] == 2
    assert states[0]["n_bid_levels"] == 1


def test_06_identity_reset_without_depth_change():
    snap_ts = T0 + timedelta(milliseconds=40)
    reset = _reset(ts=snap_ts, bids={100.0: 1.0}, asks={101.0: 1.0}, u=5, seq=5, epoch=2, order=1)
    states = _build(
        start=T0,
        end=T0 + timedelta(milliseconds=100),
        changes=[],
        resets=[reset],
        initial_replay_epoch=1,
        initial_update_id=1,
        initial_sequence_id=1,
    )
    assert states[0]["best_bid"] == 100.0
    assert states[0]["best_ask"] == 101.0
    assert states[0]["replay_epoch"] == 2
    assert states[0]["last_update_id"] == 5
    assert states[0]["available_at"] == T0 + timedelta(milliseconds=100)


def test_07_true_epoch_change():
    reset_a = _reset(ts=T0 + timedelta(milliseconds=20), bids={10.0: 1.0}, asks={11.0: 1.0}, u=2, seq=2, epoch=2, order=1)
    reset_b = _reset(ts=T0 + timedelta(milliseconds=120), bids={20.0: 1.0}, asks={21.0: 1.0}, u=3, seq=3, epoch=3, order=2)
    states = _build(
        start=T0,
        end=T0 + timedelta(milliseconds=200),
        changes=[],
        resets=[reset_a, reset_b],
        initial_replay_epoch=1,
    )
    assert states[0]["replay_epoch"] == 2
    assert states[1]["replay_epoch"] == 3
    assert states[0]["replay_epoch"] != states[1]["replay_epoch"]


def test_08_sms1_write_read_roundtrip(tmp_path: Path):
    change = _change(ts=T0 + timedelta(milliseconds=30), side="bid", price=100.0, new_size=2.0, old_size=1.0, u=2, seq=2, epoch=1, order=1)
    reset = _reset(ts=T0 + timedelta(milliseconds=60), bids={100.0: 2.0}, asks={101.0: 1.0}, u=3, seq=3, epoch=2, order=2)
    states = _build(start=T0, end=T0 + timedelta(milliseconds=100), changes=[change], resets=[reset], initial_replay_epoch=1)
    replay = _replay_stub(states, [change], [reset], {100.0: 1.0}, {101.0: 1.0}, T0, T0 + timedelta(milliseconds=100))
    write_tables(
        tmp_path,
        states=states,
        replay=replay,
        walls=[],
        refills=[],
        touches=[],
        detections=[],
        config_hash="abc",
        input_hash="def",
    )
    loaded = load_table(tmp_path, "states_100ms")
    assert len(loaded) == 1
    assert loaded[0]["bucket_start"] == serialize_state(states[0])["bucket_start"]
    assert loaded[0]["bucket_end_exclusive"] == serialize_state(states[0])["bucket_end_exclusive"]
    assert loaded[0]["available_at"] == serialize_state(states[0])["available_at"]
    assert loaded[0]["effective_bucket_start"] == serialize_state(states[0])["effective_bucket_start"]
    assert loaded[0]["replay_epoch"] == 2
    resets = load_table(tmp_path, "book_resets")
    assert len(resets) == 1
    recon = reconstruct_from_persist(tmp_path, T0 + timedelta(milliseconds=100))
    assert recon["best_bid"] == 100.0
    assert recon["replay_epoch"] == 2


def test_09_available_at_preserved(tmp_path: Path):
    states = _build(start=T0, end=T0 + timedelta(milliseconds=200), changes=[])
    for row in states:
        assert row["available_at"] == row["bucket_end_exclusive"]
        assert row["available_at"] != row["bucket_start"]
    replay = _replay_stub(states, [], [], {100.0: 1.0}, {101.0: 1.0}, T0, T0 + timedelta(milliseconds=200))
    write_tables(
        tmp_path,
        states=states,
        replay=replay,
        walls=[],
        refills=[],
        touches=[],
        detections=[],
        config_hash="abc",
        input_hash="def",
    )
    loaded = load_table(tmp_path, "states_100ms")
    for row in loaded:
        assert row["available_at"] == row["bucket_end_exclusive"]
        assert row["available_at"] != row["bucket_start"]


def test_10_downstream_not_before_available_at():
    change = _change(ts=T0 + timedelta(milliseconds=73), side="bid", price=100.0, new_size=2.0, old_size=1.0, u=2, seq=2, epoch=1, order=1)
    states = _build(start=T0, end=T0 + timedelta(milliseconds=100), changes=[change])
    avail = event_available_at(T0 + timedelta(milliseconds=73))
    assert avail == T0 + timedelta(milliseconds=100)
    last = last_complete_state(states, T0 + timedelta(milliseconds=73))
    assert last is None
    last_ready = last_complete_state(states, avail)
    assert last_ready is not None
    asof = T0 + timedelta(milliseconds=50)
    assert last_state_asof(states, asof) is None


def test_11_touch_and_detection_same_corrected_stream():
    touch = T0 + timedelta(milliseconds=50)
    det = T0 + timedelta(milliseconds=150)
    reset = _reset(ts=T0 + timedelta(milliseconds=20), bids={70.0: 1.0}, asks={71.0: 1.0}, u=4, seq=4, epoch=2, order=1)
    tbook = reconstruct_book_asof_exclusive(
        initial_bids={100.0: 1.0},
        initial_asks={101.0: 1.0},
        level_changes=[],
        book_resets=[reset],
        until=touch,
    )
    dbook = reconstruct_book_asof_exclusive(
        initial_bids={100.0: 1.0},
        initial_asks={101.0: 1.0},
        level_changes=[],
        book_resets=[reset],
        until=det,
    )
    blind_t = _book_at({100.0: 1.0}, {101.0: 1.0}, [], touch)
    assert tbook["state_source"] == dbook["state_source"] == "checkpoint_capable_event_stream"
    assert tbook["best_bid"] == dbook["best_bid"] == 70.0
    assert max(blind_t[0]) == 100.0


def test_12_deterministic_rebuild(tmp_path: Path):
    change = _change(ts=T0 + timedelta(milliseconds=30), side="bid", price=100.0, new_size=2.0, old_size=1.0, u=2, seq=2, epoch=1, order=1)
    states_a = _build(start=T0, end=T0 + timedelta(milliseconds=100), changes=[change])
    states_b = _build(start=T0, end=T0 + timedelta(milliseconds=100), changes=[change])
    assert [serialize_state(s) for s in states_a] == [serialize_state(s) for s in states_b]
    replay = _replay_stub(states_a, [change], [], {100.0: 1.0}, {101.0: 1.0}, T0, T0 + timedelta(milliseconds=100))
    d1 = tmp_path / "a"
    d2 = tmp_path / "b"
    write_tables(d1, states=states_a, replay=replay, walls=[], refills=[], touches=[], detections=[], config_hash="x", input_hash="y")
    write_tables(d2, states=states_b, replay=replay, walls=[], refills=[], touches=[], detections=[], config_hash="x", input_hash="y")
    assert file_sha256(d1 / "states_100ms.jsonl.zst") == file_sha256(d2 / "states_100ms.jsonl.zst")
    assert file_sha256(d1 / "level_changes.jsonl.zst") == file_sha256(d2 / "level_changes.jsonl.zst")


def test_13_episode1_golden_all_cutoffs_independent_book():
    start = T0
    end = T0 + timedelta(milliseconds=400)
    changes = [
        _change(ts=T0 + timedelta(milliseconds=50), side="bid", price=100.0, new_size=2.0, old_size=1.0, u=2, seq=2, epoch=1, order=1),
        _change(ts=T0 + timedelta(milliseconds=200), side="ask", price=101.0, new_size=3.0, old_size=1.0, u=3, seq=3, epoch=1, order=2),
        _change(ts=T0 + timedelta(milliseconds=300), side="bid", price=99.0, new_size=1.0, old_size=0.0, u=4, seq=4, epoch=1, order=3),
    ]
    resets = [
        _reset(ts=T0 + timedelta(milliseconds=250), bids={80.0: 1.0}, asks={81.0: 1.0}, u=9, seq=9, epoch=2, order=4),
    ]
    states = _build(start=start, end=end, changes=changes, resets=resets, initial_replay_epoch=1)
    independent = IndependentBook({100.0: 1.0}, {101.0: 1.0}, epoch=1)
    events = sorted(
        [(c["event_time"], "c", c) for c in changes] + [(r["event_time"], "r", r) for r in resets],
        key=lambda x: (x[0], 0 if x[1] == "r" else 1),
    )
    mismatches = []
    for st in states:
        cutoff = st["available_at"]
        book = IndependentBook({100.0: 1.0}, {101.0: 1.0}, epoch=1)
        for et, kind, payload in events:
            if et >= cutoff:
                break
            if kind == "r":
                book.apply_reset(payload["bids"], payload["asks"], epoch=payload["replay_epoch"], update_id=payload["update_id"], seq=payload["sequence_id"])
            else:
                book.apply_change(payload["side"], payload["price"], payload["new_size"], epoch=payload["epoch"], update_id=payload["u"], seq=payload["seq"])
        bb, ba = book.top()
        digest = book_map_sha256(book.bids, book.asks)
        if st["best_bid"] != bb or st["best_ask"] != ba or st["book_map_sha256"] != digest or st["replay_epoch"] != book.epoch:
            mismatches.append(
                {
                    "cutoff": cutoff.isoformat(),
                    "state": (st["best_bid"], st["best_ask"], st["replay_epoch"], st["book_map_sha256"]),
                    "independent": (bb, ba, book.epoch, digest),
                    "first_level": "best_bid"
                    if st["best_bid"] != bb
                    else ("best_ask" if st["best_ask"] != ba else ("replay_epoch" if st["replay_epoch"] != book.epoch else "book_map_sha256")),
                }
            )
        independent = book
    assert mismatches == []
    assert independent.top()[0] is not None


def test_14_no_clickhouse_writes():
    assert ALLOW_CLICKHOUSE_WRITES is False
    assert ALLOW_ARCHIVE_REPLAY is True
    import obfull_research_engine.level_first_episode1_corrected_sms1_persist_v1.runner as runner

    src = Path(runner.__file__).read_text(encoding="utf-8")
    assert "INSERT" not in src
    assert "open_client" not in src


def test_15_no_outcomes_and_frozen_sms1():
    states = _build(start=T0, end=T0 + timedelta(milliseconds=100), changes=[])
    for row in states:
        for field in FORBIDDEN_FIELDS:
            assert field not in row
    path = PKG_ROOT / FROZEN["sms1"] / "episodes/ep_pc_7775a856ab22f006_1788725942/states_100ms.jsonl.zst"
    assert path.is_file()
    assert file_sha256(path) == SMS1_EP1_STATES_SHA256


def test_16_episode1_run_artifacts_if_present():
    root = PKG_ROOT / "results" / "level_first_episode1_corrected_sms1_persist_v1" / "BTCUSDT"
    if not root.is_dir():
        return
    runs = sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith("csp1_"))
    if not runs:
        return
    status = (runs[-1] / "STATUS").read_text(encoding="utf-8").strip()
    manifest = json.loads((runs[-1] / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["golden"]["n_mismatch"] == 0
    assert manifest["partial_first_bucket"]["pre_evidence_level_changes_count"] == 0
    assert manifest["partial_first_bucket"]["first_bucket_available_at_ok"] is True
    assert status == "EPISODE1_CORRECTED_SMS1_PERSISTENCE_AND_DERIVED_EVENTS_PARITY_PROVEN"

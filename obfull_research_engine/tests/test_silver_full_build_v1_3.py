"""Safety and resumability tests for the inert-by-default Silver v1.3 full-build CLI."""

from __future__ import annotations

import json
import os
import signal
from types import SimpleNamespace

import pytest

import obfull_research_engine.clickhouse_research_store_v1.silver_full_build_v1_3 as runner
from obfull_research_engine.clickhouse_research_store_v1.epoch_aware_silver_v1_3 import (
    EpochDefinition,
    discover_epochs,
    split_resume_and_replay_stream,
)
from obfull_research_engine.clickhouse_research_store_v1.silver_replay import BronzeRecord


CHAIN_VERSION = "canonical_segment_chain_v1_3_BTCUSDT_20260912T060011Z_f666e592a0bef459"
CHAIN_HASH = "f666e592a0bef4598545b3f247cf5dd97dd9c53017028e3c33ddfce5e0d15333"
SHA_A = "f" * 64


class FakeClient:
    def __init__(
        self,
        *,
        free_bytes: int = 500 * 1024**3,
        bronze_running: bool = False,
        bronze_complete: bool = True,
        records: int = runner.EXPECTED_BRONZE_RECORDS,
        utc_bad: int = 0,
        bad_record_ids: int = 0,
        schema_tables: int | None = None,
    ):
        self.free_bytes = free_bytes
        self.bronze_running = bronze_running
        self.bronze_complete = bronze_complete
        self.records = records
        self.utc_bad = utc_bad
        self.bad_record_ids = bad_record_ids
        self.schema_tables = schema_tables
        self.commands: list[str] = []
        self.inserts: list[str] = []
        self.chunks: dict[str, tuple[str, str, str]] = {}
        self.chunk_rows: dict[str, dict[str, int]] = {}
        self.level_changes: set[str] = set()
        self.metrics: set[str] = set()
        self.epochs: dict[str, str] = {}
        self.tables = set()

    def command(self, sql):
        self.commands.append(str(sql))
        if "CREATE TABLE" in str(sql):
            for table in (
                runner.EPOCHS_TABLE,
                runner.GAPS_TABLE,
                runner.LEVEL_CHANGES_TABLE,
                runner.METRICS_TABLE,
                runner.CHUNKS_TABLE,
                runner.RUNS_TABLE,
            ):
                if table in str(sql):
                    self.tables.add(table)
        return 1

    def insert(self, table, rows, column_names):
        self.inserts.append(table)
        if table.endswith(runner.CHUNKS_TABLE):
            chunk_key = rows[-1][column_names.index("chunk_key")]
            status = rows[-1][column_names.index("status")]
            epoch_hash = rows[-1][column_names.index("epoch_hash")]
            plan_hash = rows[-1][column_names.index("epoch_plan_hash")]
            self.chunks[str(chunk_key)] = (str(status), str(epoch_hash), str(plan_hash))
        elif table.endswith(runner.LEVEL_CHANGES_TABLE):
            for row in rows:
                self.level_changes.add(str(row[0]))
        elif table.endswith(runner.METRICS_TABLE):
            for row in rows:
                self.metrics.add(str(row[0]))
        elif table.endswith(runner.EPOCHS_TABLE):
            for row in rows:
                self.epochs[str(row[0])] = str(row[1])

    def query(self, sql, parameters=None):
        text = str(sql)
        parameters = parameters or {}
        if "system.disks" in text:
            return SimpleNamespace(result_rows=[(self.free_bytes,)])
        if "system.tables" in text:
            if self.schema_tables is not None:
                return SimpleNamespace(result_rows=[(self.schema_tables,)])
            count = sum(
                1
                for table in (
                    runner.EPOCHS_TABLE,
                    runner.GAPS_TABLE,
                    runner.LEVEL_CHANGES_TABLE,
                    runner.METRICS_TABLE,
                    runner.CHUNKS_TABLE,
                    runner.RUNS_TABLE,
                )
                if table in self.tables
            )
            return SimpleNamespace(result_rows=[(count,)])
        if f"{runner.BRONZE_LEDGER_TABLE}" in text and "GROUP BY status" in text:
            if not self.bronze_complete:
                return SimpleNamespace(result_rows=[("RUNNING", 1)])
            return SimpleNamespace(result_rows=[("COMPLETE", runner.EXPECTED_SEGMENT_COUNT)])
        if f"{runner.BRONZE_EVENTS_TABLE}" in text and "uniqExact(record_id)" in text:
            total = self.records if self.bronze_complete else 0
            return SimpleNamespace(
                result_rows=[
                    (
                        total,
                        total,
                        total,
                        164,
                        164,
                        self.utc_bad,
                        self.bad_record_ids,
                    )
                ]
            )
        if f"{runner.BRONZE_SEGMENTS_TABLE}" in text and "canonical_segment_chain_index" in text:
            rows = []
            for rank in range(runner.EXPECTED_SEGMENT_COUNT):
                rows.append(
                    (
                        rank,
                        SHA_A,
                        f"/seg/{rank}",
                        1,
                        2,
                        1,
                        2,
                        1,
                        2,
                        1,
                        2,
                        "inst",
                        runner.EXPECTED_BRONZE_RECORDS // runner.EXPECTED_SEGMENT_COUNT,
                        "UNIQUE_HOUR_CANONICAL",
                        1,
                        "",
                        "segment_start",
                        "in_memory_snapshot_continuation",
                        "COMPLETE",
                        CHAIN_HASH,
                    )
                )
            return SimpleNamespace(result_rows=rows)
        if f"{runner.CHUNKS_TABLE}" in text and "SELECT status" in text:
            chunk_key = str(parameters.get("chunk_key", ""))
            value = self.chunks.get(chunk_key)
            return SimpleNamespace(result_rows=[value] if value else [])
        if f"{runner.CHUNKS_TABLE}" in text and "SELECT version_ms" in text:
            return SimpleNamespace(result_rows=[])
        if f"{runner.CHUNKS_TABLE}" in text and "RUNNING" in text:
            bad = sum(1 for status, _, _ in self.chunks.values() if status in {"RUNNING", "INTERRUPTED", "FAILED"})
            return SimpleNamespace(result_rows=[(bad,)])
        if f"{runner.LEVEL_CHANGES_TABLE}" in text and "uniqExact(row_id)" in text:
            return SimpleNamespace(result_rows=[(len(self.level_changes), len(self.level_changes))])
        if f"{runner.METRICS_TABLE}" in text and "uniqExact(row_id)" in text:
            return SimpleNamespace(result_rows=[(len(self.metrics), len(self.metrics))])
        if f"{runner.LEVEL_CHANGES_TABLE}" in text and "toUnixTimestamp64Nano(event_time)" in text:
            return SimpleNamespace(result_rows=[(0,)])
        if f"{runner.EPOCHS_TABLE}" in text and "SELECT epoch_id" in text:
            return SimpleNamespace(result_rows=[(k, v) for k, v in self.epochs.items()])
        if f"{runner.RUNS_TABLE}" in text:
            return SimpleNamespace(result_rows=[])
        raise AssertionError(f"unexpected query: {text}")

    def query_row_block_stream(self, sql, parameters=None, settings=None):
        self.last_stream_sql = str(sql)
        self.last_stream_parameters = parameters or {}
        self.last_stream_settings = settings or {}
        self.commands.append(f"STREAM:{sql[:120]}")
        class _Stream:
            def __init__(_self, rows):
                _self.rows = rows

            def __enter__(_self):
                return _self

            def __exit__(_self, *_args):
                return False

            def __iter__(_self):
                return iter(_self.rows)

        return _Stream(getattr(self, "stream_blocks", []))


def _config(tmp_path, **overrides):
    values = {
        "symbol": "BTCUSDT",
        "input_database": runner.DEFAULT_INPUT_DATABASE,
        "output_database": runner.DEFAULT_OUTPUT_DATABASE,
        "chain_version": CHAIN_VERSION,
        "expected_chain_hash": CHAIN_HASH,
        "expected_bronze_records": runner.EXPECTED_BRONZE_RECORDS,
        "resume": False,
        "start_chain_index": 0,
        "end_chain_index": 163,
        "chunk_market_minutes": 15,
        "warmup_minutes": 5,
        "max_rss_mib": 1536,
        "min_free_disk_gib": 200,
        "min_available_memory_mib": 1,
        "progress_every_chunks": 1,
        "report_path": tmp_path / "report.json",
        "lock_path": tmp_path / "build.lock",
        "enforce_canonical_lock_path": False,
    }
    values.update(overrides)
    return runner.BuildConfig(**values)


def _args(tmp_path, *extra: str):
    return runner.build_parser().parse_args(
        [
            "--chain-version",
            CHAIN_VERSION,
            "--expected-chain-hash",
            CHAIN_HASH,
            "--report-path",
            str(tmp_path / "report.json"),
            "--lock-path",
            str(tmp_path / "build.lock"),
            "--allow-noncanonical-lock-path",
            *extra,
        ]
    )


def _record(*, rank: int, ordinal: int, kind: str, event_ns: int) -> BronzeRecord:
    payload: dict
    if kind == "snapshot":
        payload = {"data": {"u": 1, "seq": 1, "b": [["100", "1"]], "a": [["101", "1"]]}}
    elif kind == "gap_marker":
        payload = {"details": {"reason": "transport_reconnect"}}
    else:
        payload = {"data": {"u": 1, "seq": 1, "b": [], "a": []}}
    return BronzeRecord(
        record_id=f"{rank:04d}{ordinal:060d}",
        symbol="BTCUSDT",
        message_type=kind,
        event_time_ns=event_ns,
        receive_time_ns=event_ns,
        update_id=1,
        seq=1,
        update_id_present=1,
        seq_present=1,
        source_segment_sha256=SHA_A,
        record_ordinal=ordinal,
        payload_sha256="d" * 64,
        original_payload=payload,
        canonical_segment_chain_index=rank,
    )


def test_without_explicit_mode_or_run_never_touches_clickhouse(tmp_path):
    client = FakeClient()
    with pytest.raises(runner.SilverBuildError, match="EXPLICIT_MODE_REQUIRED"):
        runner.execute(_args(tmp_path), client=client)
    assert client.commands == [] and client.inserts == []


def test_bronze_still_running_blocks_check_only(tmp_path):
    client = FakeClient()
    with pytest.raises(runner.SilverBuildError, match="BRONZE_INPUT_NOT_READY"):
        runner.execute(
            _args(tmp_path, "--check-only"),
            client=client,
            bronze_probe={"active": True, "running_pids": [1941605], "lock": None},
        )


def test_incomplete_bronze_blocks_check_only(tmp_path):
    client = FakeClient(bronze_complete=False)
    with pytest.raises(runner.SilverBuildError, match="BRONZE_INPUT_NOT_READY"):
        runner.execute(
            _args(tmp_path, "--check-only"),
            client=client,
            bronze_probe={"active": False, "running_pids": [], "lock": None},
        )


def test_wrong_bronze_record_count_blocks(tmp_path):
    client = FakeClient(records=123)
    with pytest.raises(runner.SilverBuildError, match="BRONZE_INPUT_NOT_READY"):
        runner.bronze_hard_preflight(
            client,
            _config(tmp_path),
            bronze_probe={"active": False, "running_pids": [], "lock": None},
        )


def test_wrong_chain_hash_rejected(tmp_path):
    config = _config(tmp_path, expected_chain_hash="a" * 64)
    with pytest.raises(runner.SilverBuildError, match="CHAIN_HASH"):
        runner._sha_text("bad", label="CHAIN_HASH")
    client = FakeClient()
    with pytest.raises(runner.SilverBuildError, match="BRONZE_MAPPING_INVALID"):
        runner.bronze_hard_preflight(
            client,
            config,
            bronze_probe={"active": False, "running_pids": [], "lock": None},
        )


def test_init_schema_and_verify_only_do_not_require_bronze_ready(tmp_path, monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(
        runner,
        "preflight",
        lambda *_args, **_kwargs: {"status": "PASS"},
    )
    monkeypatch.setattr(runner, "init_schema", lambda *_args: None)
    result = runner.execute(_args(tmp_path, "--init-schema"), client=client)
    assert result["status"] == "SCHEMA_INITIALIZED"
    assert client.inserts == []

    monkeypatch.setattr(
        runner,
        "verify_build",
        lambda *_args: {"status": "VERIFIED"},
    )
    verified = runner.execute(_args(tmp_path, "--verify-only"), client=client)
    assert verified["verify"]["status"] == "VERIFIED"


def test_active_and_stale_locks_require_explicit_handling(tmp_path, monkeypatch):
    path = tmp_path / "lock"
    path.write_text(json.dumps({"pid": os.getpid()}))
    with pytest.raises(runner.SilverBuildError, match="ALREADY_RUNNING"):
        runner.BuildLock.inspect_existing(path)
    path.write_text(json.dumps({"pid": 99999999}))
    monkeypatch.setattr(runner, "_pid_alive", lambda _pid: False)
    with pytest.raises(runner.SilverBuildError, match="STALE_LOCK"):
        runner.BuildLock.inspect_existing(path)


def test_epoch_plan_is_deterministic():
    records = [
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=1_000_000_000),
        _record(rank=1, ordinal=2, kind="delta", event_ns=1_100_000_000),
    ]
    left = discover_epochs(
        records,
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        scan_end_ns=2_000_000_000,
        clean_segment_start_ranks={1},
    )
    right = discover_epochs(
        records,
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        scan_end_ns=2_000_000_000,
        clean_segment_start_ranks={1},
    )
    assert left.epochs[0].epoch_id == right.epochs[0].epoch_id
    assert left.epochs[0].epoch_hash == right.epochs[0].epoch_hash


def test_periodic_checkpoint_does_not_open_epoch():
    records = [
        _record(rank=1, ordinal=1, kind="gap_marker", event_ns=1_000_000_000),
        _record(rank=1, ordinal=2, kind="snapshot", event_ns=1_100_000_000),
        _record(
            rank=1,
            ordinal=3,
            kind="checkpoint",
            event_ns=1_200_000_000,
        ),
    ]
    records[2].original_payload = {
        "checkpoint_reason": "periodic_5m",
        "bids": [["100", "1"]],
        "asks": [["101", "1"]],
    }
    discovery = discover_epochs(
        records,
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        scan_end_ns=2_000_000_000,
        clean_segment_start_ranks={1},
    )
    assert len(discovery.epochs) == 1
    assert discovery.epochs[0].anchor_type == "exchange_snapshot"


def test_chunk_plan_stays_inside_epoch():
    epoch = EpochDefinition(
        epoch_id="e" * 64,
        epoch_hash="h" * 64,
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        symbol="BTCUSDT",
        anchor_type="exchange_snapshot",
        anchor_provenance="exchange_websocket_original_payload",
        anchor_event_time_ns=0,
        anchor_receive_time_ns=0,
        anchor_u=1,
        anchor_seq=1,
        anchor_segment_chain_index=1,
        anchor_record_ordinal=1,
        safe_start_ns=0,
        safe_end_ns=60 * 60 * 1_000_000_000,
        terminating_reason="COMPLETE",
        preceding_gap_id="",
        status="COMPLETE",
        apply_end_segment_chain_index=1,
        apply_end_record_ordinal=100,
    )
    chunks = runner.plan_epoch_chunks([epoch], chunk_market_minutes=15, warmup_minutes=5)
    assert chunks
    for chunk in chunks:
        assert chunk.analysis_start_ns >= epoch.safe_start_ns
        assert chunk.analysis_end_ns <= epoch.safe_end_ns


def test_signal_state_requests_controlled_interrupt():
    state = runner.StopState()
    state.request(signal.SIGTERM)
    with pytest.raises(runner.ControlledInterrupt, match="15"):
        state.check()


def test_resource_limits_reject_disabled_thresholds(tmp_path):
    config = _config(tmp_path, max_rss_mib=0)
    with pytest.raises(runner.SilverBuildError, match="RESOURCE_LIMIT_INVALID"):
        runner._check_resources(FakeClient(), config)


def test_target_database_must_be_exact_production_name():
    with pytest.raises(runner.SilverBuildError, match="OUTPUT_DATABASE"):
        runner.validate_output_database("research_full_ob_silver_pilot_v1_3")


def test_schema_ddl_is_new_only_and_ns_materialized():
    ddl = "\n".join(runner._table_ddls(runner.DEFAULT_OUTPUT_DATABASE))
    assert runner.EPOCHS_TABLE in ddl
    assert runner.METRICS_TABLE in ddl
    assert runner.GAPS_TABLE in ddl
    assert runner.CHUNKS_TABLE in ddl
    assert runner.RUNS_TABLE in ddl
    assert "fromUnixTimestamp64Nano(event_time_ns, 'UTC')" in ddl
    assert "ALTER " not in ddl and "DROP " not in ddl and "TRUNCATE " not in ddl


def test_noncanonical_lock_path_rejected_before_queries(tmp_path):
    config = _config(tmp_path, enforce_canonical_lock_path=True)
    client = FakeClient()
    with pytest.raises(runner.SilverBuildError, match="NONCANONICAL_LOCK_PATH"):
        runner.preflight(client, config)
    assert client.commands == []


def test_utc_ns_mismatch_blocks_bronze_preflight(tmp_path):
    client = FakeClient(utc_bad=1)
    with pytest.raises(runner.SilverBuildError, match="BRONZE_INPUT_NOT_READY"):
        runner.bronze_hard_preflight(
            client,
            _config(tmp_path),
            bronze_probe={"active": False, "running_pids": [], "lock": None},
        )


def test_duplicate_record_ids_block_bronze_preflight(tmp_path):
    client = FakeClient(bad_record_ids=3)
    with pytest.raises(runner.SilverBuildError, match="BRONZE_INPUT_NOT_READY"):
        runner.bronze_hard_preflight(
            client,
            _config(tmp_path),
            bronze_probe={"active": False, "running_pids": [], "lock": None},
        )


def test_resume_requires_run_flag(tmp_path):
    with pytest.raises(runner.SilverBuildError, match="RESUME_REQUIRES_RUN"):
        runner.execute(
            _args(tmp_path, "--check-only", "--resume"),
            client=FakeClient(),
        )


def test_epoch_plan_hash_is_stable():
    records = [
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=1_000_000_000),
        _record(rank=1, ordinal=2, kind="delta", event_ns=1_100_000_000),
    ]
    left = discover_epochs(
        records,
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        scan_end_ns=2_000_000_000,
        clean_segment_start_ranks={1},
    )
    right = discover_epochs(
        records,
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        scan_end_ns=2_000_000_000,
        clean_segment_start_ranks={1},
    )
    left_plan = runner.BuildPlan(epochs=list(left.epochs), gaps=list(left.gaps))
    right_plan = runner.BuildPlan(epochs=list(right.epochs), gaps=list(right.gaps))
    left_plan.finalize()
    right_plan.finalize()
    assert left_plan.epoch_plan_hash == right_plan.epoch_plan_hash


def test_epoch_plan_changed_on_stored_epoch_hash_mismatch(tmp_path):
    config = _config(tmp_path)
    client = FakeClient()
    epoch = discover_epochs(
        [_record(rank=1, ordinal=1, kind="snapshot", event_ns=1_000_000_000)],
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        scan_end_ns=2_000_000_000,
        clean_segment_start_ranks={1},
    ).epochs[0]
    client.epochs = {epoch.epoch_id: "b" * 64}
    plan = runner.BuildPlan(epochs=[epoch], gaps=[])
    plan.finalize()
    with pytest.raises(runner.SilverBuildError, match="EPOCH_PLAN_CHANGED"):
        runner.persist_epochs_and_gaps(client, config, plan)


def test_chunk_crossing_gap_is_rejected():
    epoch = EpochDefinition(
        epoch_id="e" * 64,
        epoch_hash="h" * 64,
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        symbol="BTCUSDT",
        anchor_type="exchange_snapshot",
        anchor_provenance="exchange_websocket_original_payload",
        anchor_event_time_ns=0,
        anchor_receive_time_ns=0,
        anchor_u=1,
        anchor_seq=1,
        anchor_segment_chain_index=1,
        anchor_record_ordinal=1,
        safe_start_ns=0,
        safe_end_ns=10 * 60 * 1_000_000_000,
        terminating_reason="GAP",
        preceding_gap_id="g" * 64,
        status="COMPLETE",
        apply_end_segment_chain_index=1,
        apply_end_record_ordinal=100,
    )
    decision = runner.validate_epoch_window(
        [epoch],
        analysis_start_ns=0,
        analysis_end_ns=20 * 60 * 1_000_000_000,
        warmup_ns=0,
    )
    assert decision.status != "OK"


def test_complete_chunk_is_skipped_without_replay(tmp_path, monkeypatch):
    config = _config(tmp_path, resume=True)
    client = FakeClient()
    epoch = discover_epochs(
        [_record(rank=1, ordinal=1, kind="snapshot", event_ns=1_000_000_000)],
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        scan_end_ns=2_000_000_000,
        clean_segment_start_ranks={1},
    ).epochs[0]
    chunk = runner.ChunkPlan(
        epoch_index=1,
        chunk_index=1,
        epoch=epoch,
        analysis_start_ns=epoch.safe_start_ns,
        analysis_end_ns=min(epoch.safe_end_ns, epoch.safe_start_ns + 60_000_000_000),
        warmup_ns=0,
    )
    chunk.materialize_ids()
    plan = runner.BuildPlan(epochs=[epoch], gaps=[])
    plan.finalize()
    plan_hash = plan.epoch_plan_hash
    client.chunks[chunk.chunk_key] = ("COMPLETE", epoch.epoch_hash, plan_hash)
    monkeypatch.setattr(runner, "iter_bronze_records", lambda *_args, **_kwargs: iter([]))
    result = runner.build_one_chunk(
        client,
        config,
        run_id="r" * 64,
        epoch_plan_hash=plan_hash,
        chunk=chunk,
        stop=runner.StopState(),
    )
    assert result["status"] == "SKIPPED_ALREADY_COMPLETE"
    assert client.inserts == []


def test_incomplete_chunk_requires_resume_flag(tmp_path):
    config = _config(tmp_path, resume=False)
    client = FakeClient()
    epoch = discover_epochs(
        [_record(rank=1, ordinal=1, kind="snapshot", event_ns=1_000_000_000)],
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        scan_end_ns=2_000_000_000,
        clean_segment_start_ranks={1},
    ).epochs[0]
    chunk = runner.ChunkPlan(
        epoch_index=1,
        chunk_index=1,
        epoch=epoch,
        analysis_start_ns=epoch.safe_start_ns,
        analysis_end_ns=min(epoch.safe_end_ns, epoch.safe_start_ns + 60_000_000_000),
        warmup_ns=0,
    )
    chunk.materialize_ids()
    plan = runner.BuildPlan(epochs=[epoch], gaps=[])
    plan.finalize()
    plan_hash = plan.epoch_plan_hash
    client.chunks[chunk.chunk_key] = ("INTERRUPTED", epoch.epoch_hash, plan_hash)
    with pytest.raises(runner.SilverBuildError, match="RESUME_INCOMPLETE_REQUIRES_RESUME"):
        runner.build_one_chunk(
            client,
            config,
            run_id="r" * 64,
            epoch_plan_hash=plan_hash,
            chunk=chunk,
            stop=runner.StopState(),
        )


def test_verify_only_performs_no_dml(tmp_path, monkeypatch):
    client = FakeClient(schema_tables=6)
    monkeypatch.setattr(
        runner,
        "preflight",
        lambda *_args, **_kwargs: {"status": "PASS"},
    )
    runner.execute(_args(tmp_path, "--verify-only"), client=client)
    assert client.inserts == []
    assert not any("INSERT" in cmd for cmd in client.commands)


def test_verify_build_rejects_duplicate_rows(tmp_path):
    config = _config(tmp_path)
    client = FakeClient(schema_tables=6)
    client.level_changes = {"a", "a"}
    with pytest.raises(runner.SilverBuildError, match="VERIFY_OUTPUT_MISMATCH"):
        runner.verify_build(client, config)


def test_verify_build_rejects_incomplete_chunks(tmp_path):
    config = _config(tmp_path)
    client = FakeClient(schema_tables=6)
    client.chunks["k"] = ("RUNNING", "h" * 64, "p" * 64)
    with pytest.raises(runner.SilverBuildError, match="VERIFY_INCOMPLETE_CHUNKS"):
        runner.verify_build(client, config)


def test_free_disk_limit_blocks_start(tmp_path, monkeypatch):
    config = _config(tmp_path, min_free_disk_gib=1000)
    monkeypatch.setattr(runner, "current_rss_bytes", lambda: 1024)
    monkeypatch.setattr(runner, "_available_memory_bytes", lambda: 1024**4)
    with pytest.raises(runner.SilverBuildError, match="FREE_DISK_LIMIT"):
        runner._check_resources(FakeClient(free_bytes=1024**3), config)


def test_available_memory_limit_blocks_start(tmp_path, monkeypatch):
    config = _config(tmp_path, min_available_memory_mib=4096)
    monkeypatch.setattr(runner, "current_rss_bytes", lambda: 1024)
    monkeypatch.setattr(runner, "_available_memory_bytes", lambda: 1024)
    with pytest.raises(runner.SilverBuildError, match="MEMORY_LIMIT"):
        runner._check_resources(FakeClient(), config)


def test_run_build_interrupts_on_signal(tmp_path, monkeypatch):
    config = _config(tmp_path, resume=True, warmup_minutes=0)
    client = FakeClient(schema_tables=6)
    epoch = EpochDefinition(
        epoch_id="e" * 64,
        epoch_hash="h" * 64,
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        symbol="BTCUSDT",
        anchor_type="exchange_snapshot",
        anchor_provenance="exchange_websocket_original_payload",
        anchor_event_time_ns=0,
        anchor_receive_time_ns=0,
        anchor_u=1,
        anchor_seq=1,
        anchor_segment_chain_index=1,
        anchor_record_ordinal=1,
        safe_start_ns=0,
        safe_end_ns=60 * 60 * 1_000_000_000,
        terminating_reason="COMPLETE",
        preceding_gap_id="",
        status="COMPLETE",
        apply_end_segment_chain_index=1,
        apply_end_record_ordinal=100,
    )
    chunk = runner.ChunkPlan(
        epoch_index=1,
        chunk_index=1,
        epoch=epoch,
        analysis_start_ns=0,
        analysis_end_ns=15 * 60 * 1_000_000_000,
        warmup_ns=0,
    )
    chunk.materialize_ids()
    plan = runner.BuildPlan(epochs=[epoch], gaps=[], chunks=[chunk])
    plan.finalize()
    stop = runner.StopState()
    stop.request(signal.SIGTERM)

    def _boom(*_args, **_kwargs):
        raise runner.ControlledInterrupt("STOP_SILVER_BUILD_INTERRUPTED_SIGNAL_15")

    monkeypatch.setattr(runner, "build_one_chunk", _boom)
    with pytest.raises(runner.ControlledInterrupt):
        runner.run_build(client, config, plan, stop=stop)


def test_episode1_replay_parity_remains_exact():
    from obfull_research_engine.clickhouse_research_store_v1.epoch_aware_silver_pilot_v1_3 import (
        EPISODE_ANALYSIS_START,
        EPISODE_END,
        EPISODE_SCAN_START,
    )

    records = [
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=EPISODE_SCAN_START),
        _record(rank=1, ordinal=2, kind="delta", event_ns=EPISODE_ANALYSIS_START + 1),
    ]
    discovery = discover_epochs(
        records,
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        scan_end_ns=EPISODE_END,
        clean_segment_start_ranks={1},
    )
    epoch = discovery.epochs[0]
    decision = runner.validate_epoch_window(
        [epoch],
        analysis_start_ns=EPISODE_ANALYSIS_START,
        analysis_end_ns=EPISODE_END,
        warmup_ns=0,
    )
    assert decision.status == "OK"


def test_epoch_plan_stream_uses_lifecycle_payload_and_pk_order(tmp_path):
    client = FakeClient()
    list(
        runner.iter_bronze_epoch_plan_records(
            client,
            _config(tmp_path),
        )
    )
    sql = client.last_stream_sql
    assert "INNER JOIN" not in sql
    assert "multiIf(" in sql
    assert "ORDER BY e.canonical_segment_chain_index, e.record_ordinal" in sql
    assert client.last_stream_settings["max_memory_usage"] == runner.BRONZE_STREAM_MAX_MEMORY_BYTES
    assert client.last_stream_settings["max_block_size"] == runner.BRONZE_STREAM_MAX_BLOCK_SIZE


def test_chunk_stream_query_is_apply_bounded_and_full_payload(tmp_path):
    client = FakeClient()
    epoch = EpochDefinition(
        epoch_id="e" * 64,
        epoch_hash="h" * 64,
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        symbol="BTCUSDT",
        anchor_type="exchange_snapshot",
        anchor_provenance="exchange_websocket_original_payload",
        anchor_event_time_ns=0,
        anchor_receive_time_ns=0,
        anchor_u=1,
        anchor_seq=1,
        anchor_segment_chain_index=1,
        anchor_record_ordinal=1,
        safe_start_ns=0,
        safe_end_ns=60 * 60 * 1_000_000_000,
        terminating_reason="COMPLETE",
        preceding_gap_id="",
        status="COMPLETE",
        apply_end_segment_chain_index=1,
        apply_end_record_ordinal=100,
    )
    chunk = runner.ChunkPlan(
        epoch_index=1,
        chunk_index=1,
        epoch=epoch,
        analysis_start_ns=0,
        analysis_end_ns=15 * 60 * 1_000_000_000,
        warmup_ns=0,
    )
    chunk.materialize_ids()
    plan = runner.BuildPlan(epochs=[epoch], gaps=[])
    plan.finalize()
    client.stream_blocks = [[
        (
            "r" * 64,
            "BTCUSDT",
            "snapshot",
            0,
            0,
            1,
            1,
            1,
            1,
            SHA_A,
            1,
            "d" * 64,
            json.dumps(
                {
                    "data": {
                        "u": 1,
                        "seq": 1,
                        "b": [["100", "1"]],
                        "a": [["101", "1"]],
                    }
                }
            ),
            1,
        )
    ]]
    result = runner.build_one_chunk(
        client,
        _config(tmp_path, resume=True),
        run_id="r" * 64,
        epoch_plan_hash=plan.epoch_plan_hash,
        chunk=chunk,
        stop=runner.StopState(),
    )
    assert result["status"] == "COMPLETE"
    assert "canonical_segment_chain_index, e.record_ordinal" in client.last_stream_sql
    assert "original_payload AS original_payload" in client.last_stream_sql
    assert client.last_stream_parameters["start_rank"] == 1


def test_clickhouse_memory_error_maps_to_stop_verdict():
    with pytest.raises(runner.SilverBuildError, match="STOP_SILVER_CH_MEMORY_LIMIT"):
        runner._raise_clickhouse_stream_error(
            RuntimeError("Code: 241. DB::Exception: MEMORY_LIMIT_EXCEEDED")
        )


def test_epoch_plan_hash_matches_full_payload_reference():
    records = [
        _record(rank=1, ordinal=1, kind="gap_marker", event_ns=1_000_000_000),
        _record(rank=1, ordinal=2, kind="snapshot", event_ns=1_100_000_000),
        _record(rank=1, ordinal=3, kind="delta", event_ns=1_200_000_000),
        _record(rank=1, ordinal=4, kind="delta", event_ns=1_300_000_000),
    ]
    records[2].original_payload = {
        "data": {"u": 11, "seq": 11, "b": [], "a": []},
    }
    records[2].update_id = 11
    records[2].seq = 11
    records[3].original_payload = {
        "data": {"u": 20, "seq": 20, "b": [], "a": []},
    }
    records[3].update_id = 20
    records[3].seq = 20
    full = discover_epochs(
        records,
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        scan_end_ns=2_000_000_000,
        clean_segment_start_ranks={1},
    )
    lightweight = list(records)
    lightweight[2].original_payload = {}
    lightweight[3].original_payload = {}
    streamed = discover_epochs(
        lightweight,
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        scan_end_ns=2_000_000_000,
        clean_segment_start_ranks={1},
    )
    assert len(full.epochs) == len(streamed.epochs)
    assert full.epochs[0].epoch_id == streamed.epochs[0].epoch_id
    assert full.epochs[0].epoch_hash == streamed.epochs[0].epoch_hash


def test_split_resume_stream_avoids_full_materialization():
    records = [
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=1_000_000_000),
        _record(rank=1, ordinal=2, kind="delta", event_ns=1_100_000_000),
        _record(rank=1, ordinal=3, kind="delta", event_ns=1_200_000_000),
    ]
    records[0].original_payload = {
        "data": {"u": 1, "seq": 1, "b": [["100", "1"]], "a": [["101", "1"]]},
    }
    epoch = discover_epochs(
        records,
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        scan_end_ns=2_000_000_000,
        clean_segment_start_ranks={1},
    ).epochs[0]
    resume, tail = split_resume_and_replay_stream(
        iter(records),
        epoch=epoch,
        analysis_start_ns=1_050_000_000,
    )
    assert resume.message_type == "snapshot"
    assert sum(1 for _ in tail) == 2


def test_multi_epoch_plan_keeps_independent_safe_windows():
    hour = 3_600_000_000_000
    records = [
        _record(rank=1, ordinal=1, kind="gap_marker", event_ns=hour),
        _record(rank=1, ordinal=2, kind="snapshot", event_ns=hour + 1_000_000_000),
        _record(rank=1, ordinal=3, kind="delta", event_ns=hour + 2_000_000_000),
        _record(rank=1, ordinal=4, kind="gap_marker", event_ns=3 * hour),
        _record(rank=1, ordinal=5, kind="snapshot", event_ns=3 * hour + 1_000_000_000),
        _record(rank=1, ordinal=6, kind="delta", event_ns=3 * hour + 2_000_000_000),
    ]
    discovery = discover_epochs(
        records,
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        scan_end_ns=5 * hour,
        clean_segment_start_ranks={1},
    )
    assert len(discovery.epochs) == 2
    chunks = runner.plan_epoch_chunks(
        discovery.epochs,
        chunk_market_minutes=15,
        warmup_minutes=5,
    )
    assert chunks
    for chunk in chunks:
        assert chunk.analysis_end_ns <= chunk.epoch.safe_end_ns

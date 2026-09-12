"""Safety and resumability tests for the inert-by-default Bronze v1.3 CLI."""

from __future__ import annotations

import hashlib
import json
import os
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest
import zstandard as zstd

import obfull_research_engine.clickhouse_research_store_v1.bronze_full_import_v1_3 as runner


class FakeClient:
    def __init__(self, *, free_bytes: int = 500 * 1024**3):
        self.free_bytes = free_bytes
        self.commands: list[str] = []
        self.inserts: list[str] = []
        self.ledger: dict[str, str] = {}
        self.ledger_versions: dict[str, int] = {}
        self.ledger_version_history: list[int] = []
        self.events: dict[tuple[int, int], tuple] = {}
        self.event_insert_rows = 0
        self.schema_exists = True

    def command(self, sql):
        self.commands.append(str(sql))
        return 1

    def insert(self, table, rows, column_names):
        self.inserts.append(table)
        if table.endswith(runner.LEDGER_TABLE):
            import_id = rows[-1][column_names.index("segment_import_id")]
            status = rows[-1][column_names.index("status")]
            self.ledger[str(import_id)] = str(status)
            self.ledger_versions[str(import_id)] = int(
                rows[-1][column_names.index("ledger_version_ms")]
            )
            self.ledger_version_history.append(self.ledger_versions[str(import_id)])
        elif table.endswith(runner.EVENTS_TABLE):
            self.event_insert_rows += len(rows)
            rank_index = column_names.index("canonical_segment_chain_index")
            ordinal_index = column_names.index("record_ordinal")
            for row in rows:
                self.events[(int(row[rank_index]), int(row[ordinal_index]))] = tuple(row)

    def query(self, sql, parameters=None):
        text = str(sql)
        parameters = parameters or {}
        if "system.disks" in text:
            return SimpleNamespace(result_rows=[(self.free_bytes,)])
        if "system.tables" in text:
            return SimpleNamespace(result_rows=[(3 if self.schema_exists else 0,)])
        if runner.LEDGER_TABLE in text and "SELECT status" in text:
            status = self.ledger.get(str(parameters["import_id"]))
            version = self.ledger_versions.get(str(parameters["import_id"]), 1)
            return SimpleNamespace(
                result_rows=[(status, version)] if status else []
            )
        if runner.EVENTS_TABLE in text and "uniqExact(record_id)" in text:
            rank = int(parameters["chain_index"])
            rows = [row for (row_rank, _), row in self.events.items() if row_rank == rank]
            ordinals = {int(row[runner.EVENT_COLUMNS.index("record_ordinal")]) for row in rows}
            ids = {str(row[runner.EVENT_COLUMNS.index("record_id")]) for row in rows}
            invalid = 0
            for row in rows:
                sha = str(row[runner.EVENT_COLUMNS.index("source_segment_sha256")])
                ordinal = int(row[runner.EVENT_COLUMNS.index("record_ordinal")])
                expected = hashlib.sha256(f"{sha}:{ordinal}".encode()).hexdigest()
                invalid += str(row[runner.EVENT_COLUMNS.index("record_id")]) != expected
            return SimpleNamespace(
                result_rows=[
                    (
                        len(rows),
                        len(ids),
                        len(ordinals),
                        invalid,
                        min(ordinals) if ordinals else 0,
                        max(ordinals) if ordinals else 0,
                    )
                ]
            )
        raise AssertionError(f"unexpected query: {text}")


def _fixture(tmp_path: Path, *, records: int = 2):
    root = tmp_path / "archive"
    directory = root / "BTCUSDT" / "2026" / "09" / "06"
    directory.mkdir(parents=True)
    path = directory / "BTCUSDT_20260906T070000Z_fixture_full_ob_continuous_raw_archive_v1.ndjson.zst"
    payloads = []
    for ordinal in range(1, records + 1):
        payloads.append(
            json.dumps(
                {
                    "message_type": "delta",
                    "event_time_ns": ordinal,
                    "receive_time_ns": ordinal,
                    "archive_time_ns": ordinal,
                    "u": ordinal,
                    "seq": ordinal,
                    "payload_sha256": "d" * 64,
                    "original_payload": {
                        "data": {"u": ordinal, "seq": ordinal, "b": [], "a": []}
                    },
                }
            )
        )
    path.write_bytes(
        zstd.ZstdCompressor().compress(("\n".join(payloads) + "\n").encode())
    )
    segment_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    Path(str(path) + ".manifest.json").write_text(
        json.dumps(
            {
                "segment_sha256": segment_sha,
                "status": "CLOSED",
                "completion_status": "COMPLETE",
                "utc_hour": "2026-09-06T07:00:00Z",
            }
        )
    )
    segment = runner.SegmentSpec(
        symbol="BTCUSDT",
        chain_version="fixture-chain",
        canonical_segment_chain_index=0,
        source_segment_sha256=segment_sha,
        source_path=str(path),
        segment_start_ns=1,
        segment_end_ns=records + 1,
        first_archive_time_ns=1,
        last_archive_time_ns=records,
        first_event_time_ns=1,
        last_event_time_ns=records,
        first_receive_time_ns=1,
        last_receive_time_ns=records,
        archive_instance_id="fixture",
        record_count=records,
        resolution_status="UNIQUE_HOUR_CANONICAL",
        is_canonical=1,
        predecessor_segment_sha256="",
        anchor_type="segment_start",
        anchor_provenance="fixture",
        continuity_status="COMPLETE",
        canonical_chain_hash="",
    )
    chain_hash = hashlib.sha256(
        runner._canonical_json([segment.contract_payload()])
    ).hexdigest()
    segment.canonical_chain_hash = chain_hash
    config = runner.ImportConfig(
        symbol="BTCUSDT",
        chain_version="fixture-chain",
        expected_chain_hash=chain_hash,
        archive_root=root,
        database="research_fixture_v1_3",
        resume=False,
        start_chain_index=0,
        end_chain_index=0,
        max_rss_mib=1536,
        min_free_disk_gib=200,
        progress_every_segments=1,
        report_path=tmp_path / "report.json",
        lock_path=tmp_path / "import.lock",
        expected_segment_count=1,
    )
    return config, segment, path


def _args(tmp_path: Path, *extra: str):
    return runner.build_parser().parse_args(
        [
            "--chain-version",
            "fixture-chain",
            "--expected-chain-hash",
            "a" * 64,
            "--archive-root",
            str(tmp_path),
            "--report-path",
            str(tmp_path / "report.json"),
            "--lock-path",
            str(tmp_path / "runner.lock"),
            *extra,
        ]
    )


def test_without_explicit_mode_or_run_never_touches_clickhouse(tmp_path: Path):
    client = FakeClient()
    with pytest.raises(runner.BronzeImportError, match="EXPLICIT_MODE_REQUIRED"):
        runner.execute(_args(tmp_path), client=client)
    assert client.commands == [] and client.inserts == []


def test_check_only_and_init_schema_never_import_events(
    tmp_path: Path, monkeypatch
):
    client = FakeClient()
    monkeypatch.setattr(
        runner,
        "preflight",
        lambda *_args, **_kwargs: ([], {"status": "PASS"}),
    )
    checked = runner.execute(_args(tmp_path, "--check-only"), client=client)
    assert checked["status"] == "PASS"
    assert client.inserts == []

    initialized = {"called": False}
    monkeypatch.setattr(
        runner,
        "init_schema",
        lambda *_args: initialized.__setitem__("called", True),
    )
    monkeypatch.setattr(runner, "validate_target_schema", lambda *_args: None)
    monkeypatch.setattr(
        runner,
        "_register_target_contract",
        lambda *_args: pytest.fail("init-schema must not insert registry rows"),
    )
    result = runner.execute(_args(tmp_path, "--init-schema"), client=client)
    assert initialized["called"] is True
    assert result["status"] == "SCHEMA_INITIALIZED"
    assert not any(table.endswith(runner.EVENTS_TABLE) for table in client.inserts)


def test_verify_only_is_read_only(tmp_path: Path, monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(
        runner,
        "preflight",
        lambda *_args, **_kwargs: ([], {"status": "PASS"}),
    )
    monkeypatch.setattr(
        runner,
        "verify_target",
        lambda *_args: {"status": "VERIFIED", "segments": 0},
    )
    result = runner.execute(_args(tmp_path, "--verify-only"), client=client)
    assert result["verify"]["status"] == "VERIFIED"
    assert client.inserts == []
    assert not (tmp_path / "report.json").exists()


def test_wrong_chain_hash_and_version_stop():
    config = runner.ImportConfig(
        "BTCUSDT",
        "wrong",
        "a" * 64,
        Path("/tmp"),
        "research_x_v1_3",
        False,
        0,
        0,
        1536,
        0,
        1,
        Path("/tmp/r"),
        Path("/tmp/l"),
        1,
    )
    segment = runner.SegmentSpec(
        "BTCUSDT", "right", 0, "b" * 64, "/x", 1, 2, 1, 2, 1, 2, 1, 2,
        "i", 1, "OK", 1, "", "segment_start", "test", "COMPLETE", "a" * 64,
    )
    with pytest.raises(runner.BronzeImportError, match="CHAIN_MAPPING_INVALID"):
        runner.validate_contract([segment], config)
    segment.chain_version = "wrong"
    with pytest.raises(runner.BronzeImportError, match="CHAIN_HASH_MISMATCH"):
        runner.validate_contract([segment], config)


def test_live_open_segment_is_rejected(tmp_path: Path):
    config, segment, path = _fixture(tmp_path)
    manifest = Path(str(path) + ".manifest.json")
    value = json.loads(manifest.read_text())
    value["status"] = "LIVE_OPEN"
    manifest.write_text(json.dumps(value))
    with pytest.raises(runner.BronzeImportError, match="LIVE_OPEN"):
        runner.verify_archives([segment], config)


def test_unknown_or_missing_closed_status_is_rejected(tmp_path: Path):
    config, segment, path = _fixture(tmp_path)
    manifest = Path(str(path) + ".manifest.json")
    value = json.loads(manifest.read_text())
    value["completion_status"] = "UNKNOWN"
    manifest.write_text(json.dumps(value))
    with pytest.raises(runner.BronzeImportError, match="CONTRADICTORY"):
        runner.verify_archives([segment], config)
    value["completion_status"] = "COMPLETE"
    value["status"] = "OPEN"
    manifest.write_text(json.dumps(value))
    with pytest.raises(runner.BronzeImportError, match="CONTRADICTORY"):
        runner.verify_archives([segment], config)


def test_active_and_stale_locks_require_explicit_handling(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "lock"
    path.write_text(json.dumps({"pid": os.getpid()}))
    with pytest.raises(runner.BronzeImportError, match="ALREADY_RUNNING"):
        runner.ImportLock.inspect_existing(path)
    path.write_text(json.dumps({"pid": 99999999}))
    monkeypatch.setattr(runner.ImportLock, "pid_alive", staticmethod(lambda _pid: False))
    with pytest.raises(runner.BronzeImportError, match="STALE_LOCK"):
        runner.ImportLock.inspect_existing(path)
    assert path.exists()


def test_lock_contains_contract_and_releases_only_own_lock(tmp_path: Path):
    path = tmp_path / "lock"
    metadata = {
        "pid": 1,
        "started_at": "now",
        "host": "host",
        "chain_version": "v",
        "chain_hash": "h",
        "database": "db",
    }
    with runner.ImportLock(path, metadata):
        payload = json.loads(path.read_text())
        assert all(payload[key] == value for key, value in metadata.items())
    assert not path.exists()


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_signal_state_requests_controlled_interrupt(signum):
    state = runner.StopState()
    state.request(signum)
    with pytest.raises(runner.ControlledInterrupt, match=str(int(signum))):
        state.check()


def test_import_resume_skip_and_idempotency(tmp_path: Path, capsys):
    config, segment, _ = _fixture(tmp_path)
    client = FakeClient()
    result = runner.run_import(client, config, [segment])
    assert result["completed_segments"] == 1
    assert len(client.events) == segment.record_count
    assert client.ledger_version_history == sorted(
        set(client.ledger_version_history)
    )
    second = runner.run_import(client, config, [segment])
    assert second["skipped_segments"] == 1
    assert len(client.events) == segment.record_count
    output = capsys.readouterr().out
    assert '"eta_s"' in output and '"segment": "1/1"' in output


def test_incomplete_segment_requires_resume_then_repeats_safely(tmp_path: Path):
    config, segment, _ = _fixture(tmp_path)
    client = FakeClient()
    import_id = runner._segment_import_id(config, segment)
    client.ledger[import_id] = "INTERRUPTED"
    with pytest.raises(runner.BronzeImportError, match="REQUIRES_RESUME"):
        runner.import_one_segment(client, config, segment, runner.StopState())
    resumed = runner.ImportConfig(**{**config.__dict__, "resume": True})
    inserted, logical = runner.import_one_segment(
        client, resumed, segment, runner.StopState()
    )
    assert inserted == logical == segment.record_count
    assert client.ledger[import_id] == "COMPLETE"


def test_interrupt_inside_segment_persists_interrupted(
    tmp_path: Path, monkeypatch
):
    config, segment, _ = _fixture(tmp_path)
    client = FakeClient()
    state = runner.StopState()
    original = runner.iter_records_stream
    monkeypatch.setattr(runner, "BATCH_SIZE", 1)

    def interrupted(path):
        iterator = iter(original(path))
        yield next(iterator)
        state.request(signal.SIGTERM)
        yield next(iterator)

    monkeypatch.setattr(runner, "iter_records_stream", interrupted)
    with pytest.raises(runner.ControlledInterrupt):
        runner.import_one_segment(client, config, segment, state)
    assert client.ledger[runner._segment_import_id(config, segment)] == "INTERRUPTED"
    assert client.event_insert_rows == 1
    resumed = runner.ImportConfig(**{**config.__dict__, "resume": True})
    runner.import_one_segment(client, resumed, segment, runner.StopState())
    assert client.event_insert_rows == segment.record_count
    assert len(client.events) == segment.record_count


def test_duplicate_record_id_hard_stops(tmp_path: Path, monkeypatch):
    config, segment, _ = _fixture(tmp_path)
    client = FakeClient()
    original = runner._event_row

    def duplicate_id(*args, **kwargs):
        row = list(original(*args, **kwargs))
        row[0] = "f" * 64
        return tuple(row)

    monkeypatch.setattr(runner, "_event_row", duplicate_id)
    with pytest.raises(runner.BronzeImportError, match="DUPLICATE_RECORD_ID"):
        runner.import_one_segment(client, config, segment, runner.StopState())


def test_missing_rank_and_resource_limits_stop(tmp_path: Path):
    config, segment, _ = _fixture(tmp_path)
    segment.canonical_segment_chain_index = 1
    with pytest.raises(runner.BronzeImportError, match="SEGMENT_RANKS_INVALID"):
        runner.validate_contract([segment], config)
    low_disk = FakeClient(free_bytes=1)
    with pytest.raises(runner.BronzeImportError, match="FREE_DISK_LIMIT"):
        runner._check_resources(low_disk, config)
    tiny_rss = runner.ImportConfig(**{**config.__dict__, "max_rss_mib": 0})
    with pytest.raises(runner.BronzeImportError, match="RESOURCE_LIMIT_INVALID"):
        runner._check_resources(FakeClient(), tiny_rss)
    exceeded_rss = runner.ImportConfig(**{**config.__dict__, "max_rss_mib": 1})
    with pytest.raises(runner.BronzeImportError, match="RSS_LIMIT"):
        runner._check_resources(FakeClient(), exceeded_rss)
    disabled_disk = runner.ImportConfig(
        **{**config.__dict__, "min_free_disk_gib": -1}
    )
    with pytest.raises(runner.BronzeImportError, match="RESOURCE_LIMIT_INVALID"):
        runner._check_resources(FakeClient(), disabled_disk)


@pytest.mark.parametrize(
    "database",
    [
        "research_full_ob_continuous_v1",
        "research_full_ob_continuous_v1_2",
        "research_full_ob_continuous_pilot_v1_3",
        "not-versioned",
        "customer_data_v1_3",
    ],
)
def test_target_database_must_be_production_v1_3(database):
    with pytest.raises(runner.BronzeImportError, match="DATABASE"):
        runner.validate_target_database(database)


def test_exact_productive_target_database_is_accepted():
    runner.validate_target_database(runner.DEFAULT_DATABASE)


def test_schema_is_new_v13_only_and_utc_ns_materialized():
    ddl = "\n".join(runner._table_ddls("research_full_ob_continuous_v1_3"))
    assert runner.SEGMENTS_TABLE in ddl
    assert runner.EVENTS_TABLE in ddl
    assert runner.LEDGER_TABLE in ddl
    assert "fromUnixTimestamp64Nano(event_time_ns, 'UTC')" in ddl
    assert "_pilot_" not in ddl
    assert "ALTER " not in ddl and "DROP " not in ddl and "TRUNCATE " not in ddl


def test_target_schema_validation_checks_engines_and_keys():
    tables, columns = runner._schema_contract()
    table_rows = [
        (table, engine, engine_full, partition, order, primary)
        for table, (engine, engine_full, partition, order, primary) in tables.items()
    ]
    column_rows = [
        (table, name, kind, default_kind, expression)
        for table, rows in columns.items()
        for name, kind, default_kind, expression in rows
    ]
    runner.validate_schema_metadata(table_rows, column_rows)
    broken = list(table_rows)
    broken[0] = (
        broken[0][0],
        broken[0][1],
        broken[0][2].replace("(created_at_ms)", "()"),
        *broken[0][3:],
    )
    with pytest.raises(runner.BronzeImportError, match="SCHEMA_MISMATCH"):
        runner.validate_schema_metadata(broken, column_rows)


def test_fixture_archive_sha_and_contract_pass(tmp_path: Path):
    config, segment, _ = _fixture(tmp_path)
    runner.validate_contract([segment], config)
    verified = runner.verify_archives([segment], config)
    assert verified[0].source_segment_sha256 == segment.source_segment_sha256


def test_code_identity_and_empty_target_registration(tmp_path: Path):
    identity = runner.code_identity()
    assert identity["branch"] == runner.EXPECTED_BRANCH
    assert len(identity["head"]) == 40
    config, segment, _ = _fixture(tmp_path)

    class RegistryClient:
        def __init__(self):
            self.rows = []

        def query(self, *_args, **_kwargs):
            return SimpleNamespace(result_rows=[])

        def insert(self, table, rows, column_names):
            self.rows.extend(rows)
            assert table.endswith(runner.SEGMENTS_TABLE)
            assert "created_at_ms" in column_names

    client = RegistryClient()
    assert (
        runner._register_target_contract(client, config, [segment])
        == "INSERTED"
    )
    assert len(client.rows) == 1


def test_noncanonical_lock_path_is_rejected_before_queries(tmp_path: Path):
    config, _, _ = _fixture(tmp_path)
    config = runner.ImportConfig(
        **{**config.__dict__, "database": runner.DEFAULT_DATABASE}
    )
    client = FakeClient()
    with pytest.raises(runner.BronzeImportError, match="NONCANONICAL_LOCK_PATH"):
        runner.preflight(client, config)
    assert client.commands == []

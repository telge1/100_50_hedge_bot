"""Unit tests for explicit historical source-run resolution."""

from __future__ import annotations

import csv
import shutil
import sys
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE_ROOT.parent
_shadow = str(REPO_ROOT / "src")
while _shadow in sys.path:
    sys.path.remove(_shadow)
sys.path.insert(0, str(ENGINE_ROOT / "src"))
_ORDERBOOK_SRC = Path("/home/telgenbuescher/projects/orderbook_analyse/src")
if _ORDERBOOK_SRC.is_dir():
    sys.path.insert(0, str(_ORDERBOOK_SRC))

import pytest  # noqa: E402

from obfull_research_engine.mp_qdh_first_touch_study_v1.source_run import (  # noqa: E402
    SOURCE_RUN_ENV,
    SourceRunResolutionError,
    content_id_sha256,
    resolve_source_run_dir,
)
from obfull_research_engine.mp_qdh_first_touch_study_v1.universe import (  # noqa: E402
    build_first_touch_universe_from_batch,
    select_first_touch_universe,
)


def _write_mini_batch(path: Path, *, n_events: int = 3) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    events = []
    episodes = []
    for i in range(n_events):
        eid = f"e{i:03d}"
        ft = i < 2
        lab = "ABSORB" if i != 1 else "UNRESOLVED"
        side = "LONG" if i != 1 else "LONG"
        events.append(
            {
                "event_id": eid,
                "window_id": "w0",
                "zone_id": "z",
                "label_price_only": lab,
                "trade_side": side,
                "fade_side": "LONG",
                "break_side": "SHORT",
                "trigger_ts_ns": str(1000 + i),
                "trigger_price": "1",
                "first_touch_ts_ns": str(100 + i),
                "touch_price": "1",
                "confluence_class": "C",
                "event_role": "UPPER",
            }
        )
        episodes.append(
            {
                "event_id": eid,
                "episode_id": f"ep{i}",
                "zone_id": "z",
                "window_id": "w0",
                "is_first_touch_of_zone_version": "true" if ft else "false",
                "selected_first_touch_zone_version": "true" if ft else "false",
            }
        )
    for name, rows in (
        ("events_all.csv", events),
        ("episodes.csv", episodes),
        ("batch_windows.csv", [{"window_id": "w0", "replay_epoch": "1"}]),
    ):
        with (path / name).open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    return path


def test_cli_overrides_env(tmp_path):
    a = _write_mini_batch(tmp_path / "a")
    b = _write_mini_batch(tmp_path / "b", n_events=4)
    r = resolve_source_run_dir(
        source_run_dir=a,
        repo_root=tmp_path / "repo",
        env={SOURCE_RUN_ENV: str(b)},
    )
    assert r is not None
    assert r.resolution_source == "cli"
    assert r.source_run_dir == a.resolve()


def test_env_overrides_missing_default(tmp_path):
    batch = _write_mini_batch(tmp_path / "ext")
    repo = tmp_path / "repo"
    repo.mkdir()
    r = resolve_source_run_dir(source_run_dir=None, repo_root=repo, env={SOURCE_RUN_ENV: str(batch)})
    assert r is not None
    assert r.resolution_source == "env"
    assert r.source_is_external is True


def test_missing_path_raises(tmp_path):
    with pytest.raises(SourceRunResolutionError):
        resolve_source_run_dir(source_run_dir=tmp_path / "nope", repo_root=tmp_path)


def test_missing_required_file_raises(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    (d / "events_all.csv").write_text("event_id\n", encoding="utf-8")
    with pytest.raises(SourceRunResolutionError):
        resolve_source_run_dir(source_run_dir=d, repo_root=tmp_path)


def test_identical_content_different_path_same_content_id(tmp_path):
    a = _write_mini_batch(tmp_path / "a")
    b = tmp_path / "b"
    shutil.copytree(a, b)
    ra = resolve_source_run_dir(source_run_dir=a, repo_root=tmp_path)
    rb = resolve_source_run_dir(source_run_dir=b, repo_root=tmp_path)
    assert ra is not None and rb is not None
    assert ra.source_event_file_sha256 == rb.source_event_file_sha256
    ida = content_id_sha256(
        ra.source_event_file_sha256, ra.source_episode_file_sha256, ra.source_windows_file_sha256
    )
    idb = content_id_sha256(
        rb.source_event_file_sha256, rb.source_episode_file_sha256, rb.source_windows_file_sha256
    )
    assert ida == idb
    assert str(ra.source_run_dir) != str(rb.source_run_dir)


def test_different_content_different_hash(tmp_path):
    a = _write_mini_batch(tmp_path / "a", n_events=3)
    b = _write_mini_batch(tmp_path / "b", n_events=4)
    ra = resolve_source_run_dir(source_run_dir=a, repo_root=tmp_path)
    rb = resolve_source_run_dir(source_run_dir=b, repo_root=tmp_path)
    assert ra is not None and rb is not None
    assert ra.source_event_file_sha256 != rb.source_event_file_sha256


def test_build_from_batch_uses_production_filter(tmp_path):
    batch = _write_mini_batch(tmp_path / "batch")
    resolved = resolve_source_run_dir(source_run_dir=batch, repo_root=tmp_path)
    uni = build_first_touch_universe_from_batch(batch, source_meta=resolved)
    # e000 ABSORB FT included; e001 UNRESOLVED excluded; e002 not FT
    assert uni["manifest"]["n_included"] == 1
    assert uni["included"][0]["event_id"] == "e000"
    assert "source_run" in uni
    assert uni["source_run"]["source_read_only_expected"] is True


def test_select_ignores_outcome_and_feature_columns():
    events = {
        "a": {
            "event_id": "a",
            "window_id": "w",
            "zone_id": "z",
            "label_price_only": "ABSORB",
            "trade_side": "LONG",
            "fade_side": "LONG",
            "break_side": "SHORT",
            "trigger_ts_ns": "1",
            "trigger_price": "1",
            "first_touch_ts_ns": "1",
            "touch_price": "1",
            "reached_0_41": "false",
            "qdh_at_decision": "0",
            "mfe_pct": "0",
        }
    }
    episodes = [
        {
            "event_id": "a",
            "episode_id": "ep",
            "zone_id": "z",
            "window_id": "w",
            "is_first_touch_of_zone_version": "true",
            "selected_first_touch_zone_version": "true",
        }
    ]
    uni = select_first_touch_universe(events=events, episodes=episodes, windows={"w": {}})
    assert uni["manifest"]["n_included"] == 1

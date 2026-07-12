from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from rem_card.services import persistent_snapshot_cache as cache


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    assert cache.flush(timeout_sec=5.0)
    monkeypatch.setattr(cache, "PERSISTENT_SNAPSHOT_CACHE_DIR", tmp_path / "patient_snapshots")
    cache._LAST_PRUNE_MONOTONIC.clear()
    cache._MANIFEST_STATES.clear()
    yield
    assert cache.flush(timeout_sec=5.0)
    cache._MANIFEST_STATES.clear()


def _wait_for_path(path: Path, *, timeout_sec: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_sec
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"marker was not created: {path}")
        time.sleep(0.01)


def test_sync_store_remains_immediately_visible(isolated_cache):
    key = ("live", 1, "2026-07-12T08:00:00")
    assert cache.store_snapshot("sync", key, {"version": 1})
    assert cache.load_snapshot("sync", key) == {"version": 1}


def test_async_store_freezes_payload_and_coalesces_latest(isolated_cache):
    key = ("live", 2, "2026-07-12T08:00:00")
    snapshot = {"version": 1, "items": [1]}
    assert cache.schedule_store_snapshot("coalesce", key, snapshot)
    snapshot["items"].append(999)
    assert cache.schedule_store_snapshot("coalesce", key, {"version": 2, "items": [2]})
    assert cache.schedule_store_snapshot("coalesce", key, {"version": 3, "items": [3]})
    assert cache.flush(timeout_sec=5.0)
    assert cache.load_snapshot("coalesce", key) == {"version": 3, "items": [3]}


def test_async_delete_tombstone_prevents_stale_write_resurrection(isolated_cache):
    key = ("live", 3, "2026-07-12T08:00:00")
    assert cache.schedule_store_snapshot("delete_race", key, {"version": 1})
    cache.delete_snapshot("delete_race", key)
    assert cache.flush(timeout_sec=5.0)
    assert cache.load_snapshot("delete_race", key) is None

    assert cache.schedule_store_snapshot("delete_race", key, {"version": 2})
    assert cache.flush(timeout_sec=5.0)
    assert cache.load_snapshot("delete_race", key) == {"version": 2}


def test_admission_delete_cancels_pending_write(isolated_cache):
    key = ("live", 29, "2026-07-12T08:00:00", "nurse", "live", "card", "hash")
    assert cache.schedule_store_snapshot("patient_card_pending", key, {"version": 10})
    assert cache.delete_snapshots_for_admission("patient_card_pending", 29) >= 1
    assert cache.flush(timeout_sec=5.0)
    assert cache.load_snapshot("patient_card_pending", key) is None


def test_sync_store_wins_over_older_scheduled_write(isolated_cache):
    key = ("live", 4, "2026-07-12T08:00:00")
    assert cache.schedule_store_snapshot("sync_wins", key, {"version": 1})
    assert cache.store_snapshot("sync_wins", key, {"version": 2})
    assert cache.flush(timeout_sec=5.0)
    assert cache.load_snapshot("sync_wins", key) == {"version": 2}


def test_sync_store_does_not_resurrect_snapshot_deleted_before_replace(isolated_cache, monkeypatch):
    namespace = "sync_delete_race"
    key = ("live", 41, "2026-07-12T08:00:00")
    generation_check_started = threading.Event()
    allow_generation_check = threading.Event()
    invalidation_finished = threading.Event()
    store_result: dict[str, bool] = {}
    delete_result: dict[str, bool] = {}
    thread_errors: list[BaseException] = []

    original_commit = cache._ASYNC_WRITER.commit_if_current
    original_invalidate_key = cache._ASYNC_WRITER.invalidate_key

    def blocked_commit(writer_key, generation, callback):
        generation_check_started.set()
        if not allow_generation_check.wait(5.0):
            raise TimeoutError("generation check was not released")
        return original_commit(writer_key, generation, callback)

    def tracked_invalidate_key(target_namespace, target_key):
        original_invalidate_key(target_namespace, target_key)
        invalidation_finished.set()

    monkeypatch.setattr(cache._ASYNC_WRITER, "commit_if_current", blocked_commit)
    monkeypatch.setattr(cache._ASYNC_WRITER, "invalidate_key", tracked_invalidate_key)

    def run_store():
        try:
            store_result["value"] = cache.store_snapshot(namespace, key, {"version": 1})
        except BaseException as exc:  # pragma: no cover - asserted in the parent thread
            thread_errors.append(exc)

    def run_delete():
        try:
            delete_result["value"] = cache.delete_snapshot(namespace, key)
        except BaseException as exc:  # pragma: no cover - asserted in the parent thread
            thread_errors.append(exc)

    store_thread = threading.Thread(target=run_store, daemon=True)
    delete_thread = threading.Thread(target=run_delete, daemon=True)
    store_thread.start()
    try:
        assert generation_check_started.wait(5.0)
        delete_thread.start()
        assert invalidation_finished.wait(5.0)
    finally:
        allow_generation_check.set()

    store_thread.join(5.0)
    delete_thread.join(5.0)
    assert not store_thread.is_alive()
    assert not delete_thread.is_alive()
    assert thread_errors == []
    assert store_result == {"value": False}
    assert delete_result == {"value": False}
    assert cache.load_snapshot(namespace, key) is None
    assert list(cache._namespace_dir(namespace).glob("*.tmp")) == []


def test_admission_delete_cancels_inflight_sync_store(isolated_cache, monkeypatch):
    namespace = "sync_admission_delete_race"
    key = ("live", 42, "2026-07-12T08:00:00")
    commit_started = threading.Event()
    allow_commit = threading.Event()
    store_result: dict[str, bool] = {}

    original_commit = cache._ASYNC_WRITER.commit_if_current

    def blocked_commit(writer_key, generation, callback):
        commit_started.set()
        assert allow_commit.wait(5.0)
        return original_commit(writer_key, generation, callback)

    monkeypatch.setattr(cache._ASYNC_WRITER, "commit_if_current", blocked_commit)

    store_thread = threading.Thread(
        target=lambda: store_result.setdefault(
            "value",
            cache.store_snapshot(namespace, key, {"version": 1}),
        ),
        daemon=True,
    )
    store_thread.start()
    try:
        assert commit_started.wait(5.0)
        assert cache.delete_snapshots_for_admission(namespace, 42) >= 1
    finally:
        allow_commit.set()

    store_thread.join(5.0)
    assert not store_thread.is_alive()
    assert store_result == {"value": False}
    assert cache.flush(timeout_sec=5.0)
    assert cache.load_snapshot(namespace, key) is None


def test_identical_async_content_is_not_rewritten(isolated_cache):
    key = ("live", 5, "2026-07-12T08:00:00")
    snapshot = {"version": 7, "saved_at": datetime(2026, 7, 12, 9, 0, 0)}
    assert cache.schedule_store_snapshot("dedupe", key, snapshot)
    assert cache.flush(timeout_sec=5.0)
    path = cache._cache_path("dedupe", key)
    first_mtime = path.stat().st_mtime_ns

    assert cache.schedule_store_snapshot("dedupe", key, snapshot)
    assert cache.flush(timeout_sec=5.0)
    assert path.stat().st_mtime_ns == first_mtime


def test_prune_is_throttled_per_namespace(isolated_cache, monkeypatch):
    calls: list[str] = []
    original = cache.prune_namespace

    def tracked(namespace: str, *, now=None):
        calls.append(namespace)
        return original(namespace, now=now)

    monkeypatch.setattr(cache, "prune_namespace", tracked)
    assert cache.schedule_store_snapshot("prune_once", (1, "a"), {"value": 1})
    assert cache.schedule_store_snapshot("prune_once", (2, "b"), {"value": 2})
    assert cache.flush(timeout_sec=5.0)
    assert calls == ["prune_once"]


def test_admission_delete_uses_manifest_without_unpickling_catalog(isolated_cache, monkeypatch):
    namespace = "manifest_delete"
    matching = [("live", 77, f"shift-{index}") for index in range(12)]
    unrelated = [("live", 88, f"shift-{index}") for index in range(12)]
    for index, key in enumerate(matching + unrelated):
        assert cache.store_snapshot(namespace, key, {"value": index})

    original_pickle_load = cache.pickle.load
    pickle_load_calls = []

    def tracked_pickle_load(*args, **kwargs):
        pickle_load_calls.append(threading.current_thread().name)
        return original_pickle_load(*args, **kwargs)

    monkeypatch.setattr(cache.pickle, "load", tracked_pickle_load)
    assert cache.delete_snapshots_for_admission(namespace, 77) == len(matching)
    assert cache.flush(timeout_sec=5.0)
    assert pickle_load_calls == []
    assert all(cache.load_snapshot(namespace, key) is None for key in matching)
    assert all(cache.load_snapshot(namespace, key) is not None for key in unrelated)

    manifest = json.loads(cache._manifest_path(namespace).read_text(encoding="utf-8"))
    assert manifest["version"] == cache._MANIFEST_VERSION
    assert len(manifest["entries"]) == len(unrelated)


def test_legacy_admission_delete_tombstone_blocks_read_before_background_scan(
    isolated_cache,
    monkeypatch,
):
    namespace = "legacy_manifest_delete"
    key = ("live", 91, "2026-07-12T08:00:00")
    serialized, _content_hash = cache._serialize_snapshot(key, {"version": 1}, expires_at=None)
    path = cache._cache_path(namespace, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(serialized)
    cache._MANIFEST_STATES.clear()

    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()
    original_cleanup = cache._cleanup_admission_files

    def blocked_cleanup(*args, **kwargs):
        cleanup_started.set()
        assert allow_cleanup.wait(5.0)
        return original_cleanup(*args, **kwargs)

    monkeypatch.setattr(cache, "_cleanup_admission_files", blocked_cleanup)
    assert cache.delete_snapshots_for_admission(namespace, 91) == 0
    assert cleanup_started.wait(5.0)
    assert path.exists()
    assert cache.load_snapshot(namespace, key) is None

    allow_cleanup.set()
    assert cache.flush(timeout_sec=5.0)
    assert not path.exists()


def test_fresh_store_survives_legacy_cleanup_for_same_digest(isolated_cache, monkeypatch):
    namespace = "legacy_store_race"
    key = ("live", 92, "2026-07-12T08:00:00")
    serialized, _content_hash = cache._serialize_snapshot(key, {"version": 1}, expires_at=None)
    path = cache._cache_path(namespace, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(serialized)
    cache._MANIFEST_STATES.clear()

    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()
    original_cleanup = cache._cleanup_admission_files

    def blocked_cleanup(*args, **kwargs):
        cleanup_started.set()
        assert allow_cleanup.wait(5.0)
        return original_cleanup(*args, **kwargs)

    monkeypatch.setattr(cache, "_cleanup_admission_files", blocked_cleanup)
    cache.delete_snapshots_for_admission(namespace, 92)
    assert cleanup_started.wait(5.0)
    assert cache.store_snapshot(namespace, key, {"version": 2})

    allow_cleanup.set()
    assert cache.flush(timeout_sec=5.0)
    assert cache.load_snapshot(namespace, key) == {"version": 2}


def test_overlapping_admission_invalidations_cannot_resurrect_manifest_entry(
    isolated_cache,
    monkeypatch,
):
    namespace = "overlapping_admission_delete"
    key = ("live", 93, "2026-07-12T08:00:00")
    assert cache.store_snapshot(namespace, key, {"version": 1})
    path = cache._cache_path(namespace, key)
    assert path.exists()

    first_cleanup_started = threading.Event()
    allow_first_cleanup = threading.Event()
    original_cleanup = cache._cleanup_admission_files
    calls = 0

    def blocked_first_cleanup(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_cleanup_started.set()
            assert allow_first_cleanup.wait(5.0)
        return original_cleanup(*args, **kwargs)

    monkeypatch.setattr(cache, "_cleanup_admission_files", blocked_first_cleanup)
    cache.delete_snapshots_for_admission(namespace, 93)
    assert first_cleanup_started.wait(5.0)
    cache.delete_snapshots_for_admission(namespace, 93)
    allow_first_cleanup.set()

    assert cache.flush(timeout_sec=5.0)
    assert calls == 2
    assert not path.exists()
    assert cache.load_snapshot(namespace, key) is None


def test_fresh_replace_between_cleanup_check_and_unlink_keeps_manifest(
    isolated_cache,
    monkeypatch,
):
    namespace = "cleanup_replace_race"
    key = ("live", 94, "2026-07-12T08:00:00")
    assert cache.store_snapshot(namespace, key, {"version": 1})

    unlink_started = threading.Event()
    allow_unlink = threading.Event()
    original_delete = cache._delete_manifest_entries_batch

    def blocked_delete(target_namespace, entries):
        unlink_started.set()
        assert allow_unlink.wait(5.0)
        return original_delete(target_namespace, entries)

    monkeypatch.setattr(cache, "_delete_manifest_entries_batch", blocked_delete)
    cache.delete_snapshots_for_admission(namespace, 94)
    assert unlink_started.wait(5.0)
    assert cache.store_snapshot(namespace, key, {"version": 2})
    allow_unlink.set()

    assert cache.flush(timeout_sec=5.0)
    assert cache.load_snapshot(namespace, key) == {"version": 2}
    manifest = json.loads(cache._manifest_path(namespace).read_text(encoding="utf-8"))
    assert cache._cache_digest(key) in manifest["entries"]

    cache.delete_snapshots_for_admission(namespace, 94)
    assert cache.flush(timeout_sec=5.0)
    assert cache.load_snapshot(namespace, key) is None


def test_same_content_resave_during_cleanup_is_not_false_deduplicated(
    isolated_cache,
    monkeypatch,
):
    namespace = "cleanup_same_content_resave"
    key = ("live", 95, "2026-07-12T08:00:00")
    snapshot = {"version": 1}
    assert cache.store_snapshot(namespace, key, snapshot)

    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()
    original_cleanup = cache._cleanup_admission_files

    def blocked_cleanup(*args, **kwargs):
        cleanup_started.set()
        assert allow_cleanup.wait(5.0)
        return original_cleanup(*args, **kwargs)

    monkeypatch.setattr(cache, "_cleanup_admission_files", blocked_cleanup)
    cache.delete_snapshots_for_admission(namespace, 95)
    assert cleanup_started.wait(5.0)
    assert cache.schedule_store_snapshot(namespace, key, snapshot)
    allow_cleanup.set()

    assert cache.flush(timeout_sec=5.0)
    assert cache.load_snapshot(namespace, key) == snapshot


def test_late_read_cannot_cross_completed_admission_invalidation(isolated_cache, monkeypatch):
    namespace = "late_read_admission_delete"
    key = ("live", 96, "2026-07-12T08:00:00")
    assert cache.store_snapshot(namespace, key, {"version": 1})

    second_check_started = threading.Event()
    allow_second_check = threading.Event()
    original_check = cache._manifest_cache_key_blocked
    call_count = 0
    result: dict[str, object] = {}

    def blocked_second_check(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            second_check_started.set()
            assert allow_second_check.wait(5.0)
        return original_check(*args, **kwargs)

    monkeypatch.setattr(cache, "_manifest_cache_key_blocked", blocked_second_check)
    load_thread = threading.Thread(
        target=lambda: result.setdefault("value", cache.load_snapshot(namespace, key)),
        daemon=True,
    )
    load_thread.start()
    assert second_check_started.wait(5.0)
    cache.delete_snapshots_for_admission(namespace, 96)
    assert cache.flush(timeout_sec=5.0)
    allow_second_check.set()
    load_thread.join(5.0)

    assert not load_thread.is_alive()
    assert result == {"value": None}


def test_expired_read_does_not_delete_fresh_replacement(isolated_cache, monkeypatch):
    namespace = "expired_fresh_replace"
    key = ("live", 97, "2026-07-12T08:00:00")
    assert cache.store_snapshot(
        namespace,
        key,
        {"version": 1},
        expires_at=datetime(2020, 1, 1),
    )

    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()
    original_cleanup = cache._delete_invalid_snapshot_if_unchanged
    first_call = True

    def blocked_cleanup(*args, **kwargs):
        nonlocal first_call
        if first_call:
            first_call = False
            cleanup_started.set()
            assert allow_cleanup.wait(5.0)
        return original_cleanup(*args, **kwargs)

    monkeypatch.setattr(cache, "_delete_invalid_snapshot_if_unchanged", blocked_cleanup)
    load_result: dict[str, object] = {}
    load_thread = threading.Thread(
        target=lambda: load_result.setdefault("value", cache.load_snapshot(namespace, key)),
        daemon=True,
    )
    load_thread.start()
    assert cleanup_started.wait(5.0)
    assert cache.store_snapshot(namespace, key, {"version": 2})
    allow_cleanup.set()
    load_thread.join(5.0)

    assert load_result == {"value": None}
    assert cache.load_snapshot(namespace, key) == {"version": 2}


def test_expired_snapshot_removes_manifest_entry(isolated_cache):
    namespace = "expired_manifest_cleanup"
    key = ("live", 98, "2026-07-12T08:00:00")
    assert cache.store_snapshot(
        namespace,
        key,
        {"version": 1},
        expires_at=datetime(2020, 1, 1),
    )
    assert cache.load_snapshot(namespace, key) is None
    manifest = json.loads(cache._manifest_path(namespace).read_text(encoding="utf-8"))
    assert cache._cache_digest(key) not in manifest["entries"]


def test_corrupt_snapshot_removes_manifest_entry(isolated_cache):
    namespace = "corrupt_manifest_cleanup"
    key = ("live", 99, "2026-07-12T08:00:00")
    assert cache.store_snapshot(namespace, key, {"version": 1})
    cache._cache_path(namespace, key).write_bytes(b"not-a-pickle")
    assert cache.load_snapshot(namespace, key) is None
    manifest = json.loads(cache._manifest_path(namespace).read_text(encoding="utf-8"))
    assert cache._cache_digest(key) not in manifest["entries"]


def test_cross_process_manifest_writers_merge_without_lost_entries(isolated_cache, tmp_path):
    namespace = "multiprocess_manifest"
    cache_root = cache.PERSISTENT_SNAPSHOT_CACHE_DIR
    go_path = tmp_path / "go"
    ready_paths = [tmp_path / f"ready-{index}" for index in (1, 2)]
    script = r"""
import sys, time
from pathlib import Path
from rem_card.services import persistent_snapshot_cache as cache
cache.PERSISTENT_SNAPSHOT_CACHE_DIR = Path(sys.argv[1])
cache._MANIFEST_STATES.clear()
identifier = int(sys.argv[2])
Path(sys.argv[3]).write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 10
while not Path(sys.argv[4]).exists():
    if time.monotonic() >= deadline:
        raise TimeoutError("go marker")
    time.sleep(0.005)
key = ("live", identifier, "2026-07-12T08:00:00")
if not cache.store_snapshot("multiprocess_manifest", key, {"id": identifier}):
    raise RuntimeError("store failed")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.fspath(Path(__file__).resolve().parents[2])
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                os.fspath(cache_root),
                str(identifier),
                os.fspath(ready_path),
                os.fspath(go_path),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for identifier, ready_path in zip((101, 102), ready_paths)
    ]
    try:
        deadline = time.monotonic() + 10.0
        while not all(path.exists() for path in ready_paths):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        go_path.write_text("go", encoding="utf-8")
        outputs = [process.communicate(timeout=20) for process in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    assert all(process.returncode == 0 for process in processes), outputs
    cache._MANIFEST_STATES.clear()
    manifest = json.loads(cache._manifest_path(namespace).read_text(encoding="utf-8"))
    assert len(manifest["entries"]) == 2
    for identifier in (101, 102):
        key = ("live", identifier, "2026-07-12T08:00:00")
        assert cache.load_snapshot(namespace, key) == {"id": identifier}


@pytest.mark.parametrize("invalidation_kind", ["admission", "exact"])
def test_cross_process_stale_writer_cannot_cross_persisted_invalidation(
    isolated_cache,
    tmp_path,
    invalidation_kind,
):
    namespace = f"cross_process_stale_{invalidation_kind}"
    admission_id = 201 if invalidation_kind == "admission" else 202
    key = ("live", admission_id, "2026-07-12T08:00:00")
    ready_path = tmp_path / f"{invalidation_kind}-ready"
    go_path = tmp_path / f"{invalidation_kind}-go"
    script = r"""
import sys, time
from pathlib import Path
from rem_card.services import persistent_snapshot_cache as cache
cache.PERSISTENT_SNAPSHOT_CACHE_DIR = Path(sys.argv[1])
cache._MANIFEST_STATES.clear()
namespace = sys.argv[2]
admission_id = int(sys.argv[3])
ready_path = Path(sys.argv[4])
go_path = Path(sys.argv[5])
key = ("live", admission_id, "2026-07-12T08:00:00")
original_commit = cache._commit_snapshot_file
def blocked_commit(*args, **kwargs):
    ready_path.write_text("ready", encoding="utf-8")
    deadline = time.monotonic() + 15.0
    while not go_path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("commit release marker")
        time.sleep(0.01)
    return original_commit(*args, **kwargs)
cache._commit_snapshot_file = blocked_commit
if not cache.schedule_store_snapshot(namespace, key, {"stale": True}):
    raise RuntimeError("schedule failed")
if not cache.flush(20.0):
    raise RuntimeError("flush timed out")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.fspath(Path(__file__).resolve().parents[2])
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            os.fspath(cache.PERSISTENT_SNAPSHOT_CACHE_DIR),
            namespace,
            str(admission_id),
            os.fspath(ready_path),
            os.fspath(go_path),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    outputs = ("", "")
    try:
        _wait_for_path(ready_path)
        if invalidation_kind == "admission":
            cache.delete_snapshots_for_admission(namespace, admission_id)
            assert cache.flush(timeout_sec=5.0)
        else:
            cache.delete_snapshot(namespace, key)
        go_path.write_text("go", encoding="utf-8")
        outputs = process.communicate(timeout=20)
    finally:
        go_path.write_text("go", encoding="utf-8")
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert process.returncode == 0, outputs
    cache._MANIFEST_STATES.clear()
    assert cache.load_snapshot(namespace, key) is None
    assert not cache._cache_path(namespace, key).exists()


def test_cross_process_legacy_registration_cannot_launder_newer_admission_epoch(
    isolated_cache,
    tmp_path,
    monkeypatch,
):
    namespace = "cross_process_legacy_epoch"
    first_key = ("live", 211, "2026-07-12T08:00:00")
    second_key = ("live", 212, "2026-07-12T08:00:00")
    for key in (first_key, second_key):
        serialized, _content_hash = cache._serialize_snapshot(
            key,
            {"legacy": key[1]},
            expires_at=None,
        )
        path = cache._cache_path(namespace, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(serialized)
    cache._MANIFEST_STATES.clear()

    registration_ready = tmp_path / "legacy-registration-ready"
    allow_registration = tmp_path / "legacy-registration-go"
    script = r"""
import sys, time
from pathlib import Path
from rem_card.services import persistent_snapshot_cache as cache
cache.PERSISTENT_SNAPSHOT_CACHE_DIR = Path(sys.argv[1])
cache._MANIFEST_STATES.clear()
ready_path = Path(sys.argv[2])
go_path = Path(sys.argv[3])
original_register = cache._manifest_register_legacy_entries
def blocked_register(*args, **kwargs):
    ready_path.write_text("ready", encoding="utf-8")
    deadline = time.monotonic() + 15.0
    while not go_path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("registration release marker")
        time.sleep(0.01)
    return original_register(*args, **kwargs)
cache._manifest_register_legacy_entries = blocked_register
cache.delete_snapshots_for_admission("cross_process_legacy_epoch", 211)
if not cache.flush(20.0):
    raise RuntimeError("flush timed out")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.fspath(Path(__file__).resolve().parents[2])
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            os.fspath(cache.PERSISTENT_SNAPSHOT_CACHE_DIR),
            os.fspath(registration_ready),
            os.fspath(allow_registration),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()
    original_cleanup = cache._cleanup_admission_files

    def blocked_cleanup(*args, **kwargs):
        cleanup_started.set()
        assert allow_cleanup.wait(10.0)
        return original_cleanup(*args, **kwargs)

    outputs = ("", "")
    try:
        _wait_for_path(registration_ready)
        monkeypatch.setattr(cache, "_cleanup_admission_files", blocked_cleanup)
        cache.delete_snapshots_for_admission(namespace, 212)
        assert cleanup_started.wait(10.0)

        # The first process now attempts to register the old admission 212
        # snapshot after the second process has persisted its newer epoch.
        allow_registration.write_text("go", encoding="utf-8")
        outputs = process.communicate(timeout=20)
        allow_cleanup.set()
        assert cache.flush(timeout_sec=20.0)
    finally:
        allow_registration.write_text("go", encoding="utf-8")
        allow_cleanup.set()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert process.returncode == 0, outputs
    cache._MANIFEST_STATES.clear()
    assert cache.load_snapshot(namespace, second_key) is None
    assert not cache._cache_path(namespace, second_key).exists()


def test_file_identity_detects_same_size_same_mtime_replacement(isolated_cache, tmp_path):
    path = tmp_path / "identity.pkl"
    path.write_bytes(b"old!")
    original_stat = path.stat()
    fingerprint = cache._path_fingerprint(path)
    replacement = tmp_path / "replacement.tmp"
    replacement.write_bytes(b"new!")
    os.replace(replacement, path)
    os.utime(
        path,
        ns=(int(original_stat.st_atime_ns), int(original_stat.st_mtime_ns)),
    )
    assert path.stat().st_size == original_stat.st_size
    assert path.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert cache._same_path_fingerprint(path, fingerprint) is False


def test_external_replacement_invalidates_process_local_content_dedupe(isolated_cache):
    namespace = "external_content_dedupe"
    key = ("live", 103, "2026-07-12T08:00:00")
    assert cache.store_snapshot(namespace, key, {"version": "A"})
    script = r"""
import sys
from pathlib import Path
from rem_card.services import persistent_snapshot_cache as cache
cache.PERSISTENT_SNAPSHOT_CACHE_DIR = Path(sys.argv[1])
cache._MANIFEST_STATES.clear()
key = ("live", 103, "2026-07-12T08:00:00")
if not cache.store_snapshot("external_content_dedupe", key, {"version": "B"}):
    raise RuntimeError("external store failed")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.fspath(Path(__file__).resolve().parents[2])
    completed = subprocess.run(
        [sys.executable, "-c", script, os.fspath(cache.PERSISTENT_SNAPSHOT_CACHE_DIR)],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)

    # _last_written_hash still contains A in this process.  The manifest hash
    # must prevent a false dedupe against externally written B.
    assert cache.schedule_store_snapshot(namespace, key, {"version": "A"})
    assert cache.flush(timeout_sec=5.0)
    assert cache.load_snapshot(namespace, key) == {"version": "A"}

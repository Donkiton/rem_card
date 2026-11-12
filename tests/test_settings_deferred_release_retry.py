from __future__ import annotations

import json
import multiprocessing
import os
import socket
import sqlite3
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from contextlib import contextmanager

import pytest

from rem_card.app import sqlite_shared
from rem_card.app.sqlite_shared import FileWriteLock
from rem_card.data.settings.settings_db import SettingsDatabase
from rem_card.data.settings.settings_release import export_settings_release_snapshot
from rem_card.data.settings.settings_schema import now_text
from rem_card.services.settings import settings_service as settings


def _seed_setting(db, key, value, role="system"):
    with db.transaction("test_seed") as cursor:
        cursor.execute(
            "INSERT INTO app_settings(scope,key,value_json,revision,updated_at,updated_by_role) "
            "VALUES('shared',?,?,1,?,?)",
            (key, json.dumps(value), now_text(), role),
        )


def _setup_retry(tmp_path, monkeypatch, *, delay=0.05):
    source = SettingsDatabase(baza_dir=str(tmp_path / "source"))
    source.ensure_ready()
    _seed_setting(source, "release_retry_test", "packaged")
    _seed_setting(source, "release_retry_manual", "packaged default")
    snapshot = tmp_path / "package" / "settings_release_snapshot.json"
    export_settings_release_snapshot(str(tmp_path / "source"), str(snapshot), release_version="5.1.11", release_commit="test")
    db = SettingsDatabase(baza_dir=str(tmp_path / "target"))
    db.ensure_ready()
    _seed_setting(db, "release_retry_manual", "clinician's choice", role="doctor")
    service = settings.SettingsService(db)
    monkeypatch.setattr(service, "_ensure_legacy_import", lambda: None)
    monkeypatch.setattr(service, "_should_prepare_operblock_on_startup", lambda: False)
    monkeypatch.setattr(service, "_should_repair_backgrounds_on_startup", lambda: False)
    monkeypatch.setenv("REMCARD_APPLY_SETTINGS_RELEASE_SNAPSHOT", "1")
    monkeypatch.setenv("REMCARD_SETTINGS_RELEASE_SNAPSHOT", str(snapshot))
    monkeypatch.setattr(settings, "_RELEASE_RETRY_DELAY_SEC", delay)
    monkeypatch.setattr(settings, "_RELEASE_RETRY_INTERVAL_SEC", 0.05)
    monkeypatch.setattr(settings, "_RELEASE_RETRY_TIMEOUT_SEC", 2.0)
    holder = FileWriteLock(db.lock_path)
    assert holder.acquire("test-live-editor", "settings_editor")
    started = time.monotonic()
    info = service.ensure_ready()
    assert time.monotonic() - started < 2
    assert info["settings_release_snapshot"]["reason"] == settings.SETTINGS_STARTUP_WRITE_BUSY_REASON
    return service, holder, snapshot


def _wait_until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate(), "background settings operation did not settle"


def _applied_hash(db):
    with db.read_connection() as connection:
        row = connection.execute("SELECT value FROM settings_meta WHERE key='settings_release_snapshot_applied_hash'").fetchone()
    return row[0] if row else None


def test_busy_startup_retries_in_real_child_after_unlock_and_preserves_user_rows(tmp_path, monkeypatch):
    service, holder, _snapshot = _setup_retry(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "_prime_app_setting_cache", lambda: pytest.fail("coordinator must not read SMB"))
    original_lock = Path(service.db.lock_path).read_bytes()
    try:
        _wait_until(lambda: service.get_deferred_release_retry_probe()["status"] == settings.SETTINGS_STARTUP_WRITE_BUSY_REASON)
        assert Path(service.db.lock_path).read_bytes() == original_lock
        assert not _applied_hash(service.db)
        holder.release()
        _wait_until(lambda: bool(_applied_hash(service.db)))
        _wait_until(lambda: not service.get_deferred_release_retry_probe()["running"])
        # No read getter starts/retries the write. Completion itself replaces
        # the old primed cache, including defaults and preserved user choices.
        assert service.get_app_setting("shared", "release_retry_test") == "packaged"
        assert service.get_app_setting("shared", "release_retry_manual") == "clinician's choice"
        assert service._ready_info["settings_startup_warnings"] == []
        assert service._ready_info["settings_release_snapshot"]["applied"]
    finally:
        holder.release()
        assert service.stop_deferred_release_retry(3)


def test_unavailable_database_identity_keeps_successful_startup_without_retry(tmp_path, monkeypatch):
    def unavailable_identity(_path):
        raise ValueError("SMB file identity unavailable")

    monkeypatch.setattr(settings, "file_identity", unavailable_identity)
    service, holder, _snapshot = _setup_retry(tmp_path, monkeypatch)
    try:
        assert service._ready
        assert service.get_deferred_release_retry_probe() == {
            "running": False, "status": "not_started", "pending": False,
        }
        assert Path(service.db.lock_path).exists()
    finally:
        holder.release()
        assert service.stop_deferred_release_retry(0.1)


@pytest.mark.parametrize("change", ["release", "target", "readonly", "schema"])
def test_deferred_retry_cannot_apply_to_changed_release_target_or_schema(tmp_path, monkeypatch, change):
    service, holder, snapshot = _setup_retry(tmp_path, monkeypatch, delay=0.15)
    try:
        holder.release()
        if change == "release":
            manifest = snapshot.with_name("settings_release_manifest.json")
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["release_commit"] = "another-release"
            manifest.write_text(json.dumps(data), encoding="utf-8")
        elif change == "target":
            replacement = SettingsDatabase(baza_dir=str(tmp_path / "replacement"))
            replacement.ensure_ready()
            os.replace(replacement.db_path, service.db.db_path)
        elif change == "readonly":
            service.db.settings_readonly = True
        else:
            monkeypatch.setenv("REMCARD_BUILD_NO_DATABASE_UPGRADES", "1")
            with sqlite3.connect(service.db.db_path) as connection:
                connection.execute("DROP TABLE order_templates")
        _wait_until(lambda: not service.get_deferred_release_retry_probe()["running"])
        assert not _applied_hash(service.db)
        if change == "schema":
            with sqlite3.connect(service.db.db_path) as connection:
                assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='order_templates'").fetchone()
    finally:
        holder.release()
        assert service.stop_deferred_release_retry(3)


def test_schema_changed_in_place_after_precheck_is_rechecked_under_writer_lock(tmp_path, monkeypatch):
    from rem_card.data.settings import settings_schema

    service, holder, _snapshot = _setup_retry(tmp_path, monkeypatch, delay=60)
    config = dict(service._deferred_release_retry.config)
    assert service.stop_deferred_release_retry(3)
    holder.release()
    original = settings.SettingsService._settings_startup_write_precheck_report

    def schema_changed_after_precheck(self, **kwargs):
        report = original(self, **kwargs)
        assert report is None
        # A second client can update schema metadata without replacing the file.
        with sqlite3.connect(self.db.db_path) as connection:
            connection.execute("UPDATE settings_meta SET value=? WHERE key=?", (
                str(settings_schema.SCHEMA_VERSION + 1), settings_schema.SCHEMA_VERSION_KEY,
            ))
        return report

    class Result:
        def send(self, result):
            self.result = result

        def close(self):
            pass

    monkeypatch.setattr(settings.SettingsService, "_settings_startup_write_precheck_report", schema_changed_after_precheck)
    result = Result()
    settings._deferred_settings_release_worker({**config, "job_token": uuid.uuid4().hex}, result)
    assert result.result["reason"] == "settings_retry_schema_changed"
    assert not _applied_hash(service.db)
    with service.db.read_connection() as connection:
        assert not connection.execute("SELECT 1 FROM app_settings WHERE key='release_retry_test'").fetchone()
        assert connection.execute("SELECT value FROM settings_meta WHERE key=?", (
            settings_schema.SCHEMA_VERSION_KEY,
        )).fetchone()[0] == str(settings_schema.SCHEMA_VERSION + 1)


def test_startup_precheck_recovers_only_a_confirmed_dead_local_pid(tmp_path):
    db = SettingsDatabase(baza_dir=str(tmp_path))
    db.ensure_ready()
    child = multiprocessing.get_context("spawn").Process(target=time.sleep, args=(0,))
    child.start()
    child.join(5)
    assert child.exitcode == 0
    pid = child.pid
    child.close()
    Path(db.lock_path).write_text(json.dumps({
        "host": socket.gethostname(), "pid": pid, "source": settings._RELEASE_APPLY_SOURCE,
        "user_id": "old-settings-process", "timestamp": time.time() - 400000,
        "lock_token": uuid.uuid4().hex,
    }), encoding="utf-8")
    report = settings.SettingsService(db)._settings_startup_write_precheck_report(
        source=settings._RELEASE_APPLY_SOURCE, operation_label="test",
    )
    assert report is None
    assert not Path(db.lock_path).exists()


@pytest.mark.parametrize("kind", ["live", "foreign", "unknown", "unreadable"])
def test_old_settings_lock_is_preserved_without_proof_of_abandonment(tmp_path, monkeypatch, kind):
    db = SettingsDatabase(baza_dir=str(tmp_path))
    db.ensure_ready()
    holder = FileWriteLock(db.lock_path)
    assert holder.acquire("protected-editor", "settings_editor")
    try:
        payload = json.loads(Path(db.lock_path).read_text(encoding="utf-8"))
        payload["timestamp"] = time.time() - 400000
        if kind == "foreign":
            payload["host"] = "another-workstation"
        elif kind == "unknown":
            monkeypatch.setattr(sqlite_shared, "is_local_pid_alive", lambda _pid: None)
        Path(db.lock_path).write_text("{" if kind == "unreadable" else json.dumps(payload), encoding="utf-8")
        before = Path(db.lock_path).read_bytes()
        report = settings.SettingsService(db)._settings_startup_write_precheck_report(
            source=settings._RELEASE_APPLY_SOURCE, operation_label="test",
        )
        assert report["reason"] == settings.SETTINGS_STARTUP_WRITE_BUSY_REASON
        assert Path(db.lock_path).read_bytes() == before
    finally:
        Path(db.lock_path).unlink(missing_ok=True)


def _hold_settings_writer(job, _sender):
    lock = FileWriteLock(job["lock_path"])
    assert lock.acquire(f"settings-release:{job['job_token']}", settings._RELEASE_APPLY_SOURCE)
    while not Path(job["database_path"] + ".exit-child").exists():
        time.sleep(0.01)
    os._exit(0)


def test_unstoppable_settings_writer_is_retained_and_blocks_context_replacement(tmp_path, monkeypatch):
    context = multiprocessing.get_context("spawn")
    owners = []

    class StopDenied:
        def __init__(self, process):
            self.process = process

        def __getattr__(self, name):
            return getattr(self.process, name)

        def terminate(self):
            raise PermissionError("simulated protected process")

        kill = terminate

    class Context:
        Pipe = staticmethod(context.Pipe)

        @staticmethod
        def Process(**kwargs):
            if kwargs.get("name") == "RemCardSettingsReleaseWorker":
                kwargs["target"] = _hold_settings_writer
                process = StopDenied(context.Process(**kwargs))
                owners.append(process)
                return process
            return context.Process(**kwargs)

    service, holder, _snapshot = _setup_retry(tmp_path, monkeypatch)
    monkeypatch.setattr(settings.multiprocessing, "get_context", lambda *_args: Context())
    monkeypatch.setattr(settings, "_RELEASE_RETRY_TIMEOUT_SEC", 0.3)
    monkeypatch.setattr(settings, "_DEFAULT_SERVICE", service)
    try:
        holder.release()
        _wait_until(lambda: service.get_deferred_release_retry_probe()["status"] == "owned_worker_pending")
        assert not service.stop_deferred_release_retry(0.01)
        new_path = tmp_path / "another-db" / "settings.db"
        with pytest.raises(RuntimeError, match="ещё завершается"):
            settings.configure_settings_service(settings_db_path=str(new_path))
        assert settings._DEFAULT_SERVICE is service
        assert not new_path.exists()
        assert len(owners) == 1
        assert Path(service.db.lock_path).exists()
        assert not _applied_hash(service.db)
        Path(service.db.db_path + ".exit-child").touch()
        _wait_until(lambda: service.stop_deferred_release_retry(1.5))
        assert not Path(service.db.lock_path).exists()
    finally:
        holder.release()
        Path(service.db.db_path + ".exit-child").touch()
        assert service.stop_deferred_release_retry(3)


class _WorkerResult:
    def send(self, result):
        self.result = result

    def close(self):
        pass


@pytest.mark.parametrize("change", ["local_write", "monitor_before_gui", "monitor_after_gui", "invalidate", "close"])
def test_late_release_cache_result_cannot_replace_a_newer_cache(tmp_path, monkeypatch, change):
    from rem_card.services.data_service import DataService

    service, holder, _snapshot = _setup_retry(tmp_path, monkeypatch, delay=60)
    config = dict(service._deferred_release_retry.config)
    assert service.stop_deferred_release_retry(3)
    service._deferred_release_stop_requested = False
    holder.release()
    epoch = service._app_setting_cache_epoch
    result = _WorkerResult()
    settings._deferred_settings_release_worker({**config, "job_token": uuid.uuid4().hex}, result)
    assert result.result["status"] == "success"
    if change == "close":
        service._deferred_release_stop_requested = True
    elif change == "invalidate":
        service.invalidate_cache()
    elif change == "local_write":
        service.set_app_setting("shared", "release_retry_manual", "new local value")
    else:
        other = settings.SettingsService(service.db)
        other._ready = True
        other.set_app_setting("shared", "release_retry_manual", "new monitor value")
        service.fetch_changes_since(0, refresh_app_settings=True)
    receiver = SimpleNamespace(
        _shutting_down=False, _network_outage_detected=False,
        _sync_coordinator=SimpleNamespace(classify=lambda payload: payload),
        changes_detected=SimpleNamespace(emit=lambda payload: None),
    )
    monkeypatch.setattr(settings, "get_settings_service", lambda: service)

    def gui_emit():
        DataService._emit_coordinated_changes(receiver, {
            "changes": [{"settings_change": True}],
            "last_settings_change_id": service._app_setting_cache_change_id,
        })

    if change == "monitor_before_gui":
        gui_emit()
    accepted = service._release_retry_applied(
        config, result.result["report"], result.result["app_setting_rows"],
        change_id=result.result["app_setting_change_id"], expected_epoch=epoch,
    )
    assert accepted is False
    if change == "monitor_after_gui":
        gui_emit()
    if change == "invalidate":
        assert service._app_setting_cache_complete is False
    elif change != "close":
        monkeypatch.setattr(service.db, "read_connection", lambda: pytest.fail("GUI must use the winning cache"))
        expected = "new local value" if change == "local_write" else "new monitor value"
        assert service.get_app_setting("shared", "release_retry_manual") == expected


def test_superseded_release_result_refreshes_in_readonly_child_without_poll_or_reapply(tmp_path, monkeypatch):
    service, holder, _snapshot = _setup_retry(tmp_path, monkeypatch, delay=0.15)
    context = multiprocessing.get_context("spawn")
    jobs, names, reports, cursors = [], [], [], []
    original = service._release_retry_applied

    class Context:
        Pipe = staticmethod(context.Pipe)

        @staticmethod
        def Process(**kwargs):
            names.append(kwargs["name"])
            if kwargs["name"] == "RemCardSettingsReleaseWorker":
                jobs.append(kwargs["args"][0])
            return context.Process(**kwargs)

    def raced_result(config, report, rows, **kwargs):
        reports.append(report)
        if len(reports) == 1:
            service.set_app_setting("shared", "release_retry_manual", "late local choice")
            cursors.append(service.latest_change_id())
        return original(config, report, rows, **kwargs)

    monkeypatch.setenv("REMCARD_SETTINGS_MONITOR_POLL_INTERVAL_SEC", "0")
    monkeypatch.setattr(settings.multiprocessing, "get_context", lambda *_args: Context())
    monkeypatch.setattr(service, "_release_retry_applied", raced_result)
    try:
        holder.release()
        _wait_until(lambda: not service.get_deferred_release_retry_probe()["running"])
        assert [job["refresh_only"] for job in jobs] == [False, True]
        assert names.count("RemCardSettingsReleaseCleanup") == 1
        assert reports[0]["applied"] and reports[1] is None
        assert service._ready_info["settings_release_snapshot"] == reports[0]
        assert service.latest_change_id() == cursors[0]
        assert service.app_setting_cache_covers(cursors[0])
        monkeypatch.setattr(service.db, "read_connection", lambda: pytest.fail("final cache must include all package values"))
        assert service.get_app_setting("shared", "release_retry_test") == "packaged"
        assert service.get_app_setting("shared", "release_retry_manual") == "late local choice"
        assert service.get_app_setting("shared", "missing", default=17) == 17
        assert not Path(service.db.lock_path).exists()
    finally:
        holder.release()
        assert service.stop_deferred_release_retry(3)


def test_refresh_only_child_never_applies_release_or_requests_writer_lock(tmp_path, monkeypatch):
    from rem_card.data.settings import settings_release

    service, holder, _snapshot = _setup_retry(tmp_path, monkeypatch, delay=60)
    config = dict(service._deferred_release_retry.config)
    assert service.stop_deferred_release_retry(3)
    before_lock = Path(service.db.lock_path).read_bytes()
    before_cursor = service.latest_change_id()
    monkeypatch.setattr(settings_release, "apply_settings_release_snapshot", lambda *_args, **_kwargs: pytest.fail("refresh attempted a release write"))
    monkeypatch.setattr(settings.SettingsService, "_settings_startup_write_precheck_report", lambda *_args, **_kwargs: pytest.fail("refresh inspected a writer lock"))
    result = _WorkerResult()
    try:
        settings._deferred_settings_release_worker({**config, "job_token": uuid.uuid4().hex, "refresh_only": True}, result)
        assert result.result["status"] == "success"
        assert result.result["report"] is None and result.result["write_attempted"] is False
        assert result.result["app_setting_change_id"] == before_cursor
        assert Path(service.db.lock_path).read_bytes() == before_lock
        assert not _applied_hash(service.db)
    finally:
        holder.release()


@pytest.mark.parametrize("conflicts", [1, 2])
def test_monitor_cache_refresh_retries_at_most_two_complete_reads(tmp_path, monkeypatch, conflicts):
    db = SettingsDatabase(baza_dir=str(tmp_path / "settings"))
    service = settings.SettingsService(db)
    service.ensure_ready()
    service.set_app_setting("shared", "monitor_test", "initial")
    install = service._install_app_setting_cache
    epochs = []

    def race(rows, **kwargs):
        epochs.append(kwargs["expected_epoch"])
        if len(epochs) <= conflicts:
            service.set_app_setting("shared", "monitor_test", f"new-{len(epochs)}")
        return install(rows, **kwargs)

    monkeypatch.setattr(service, "_install_app_setting_cache", race)
    if conflicts == 1:
        changes = service.fetch_changes_since(0, refresh_app_settings=True)
        assert changes and service.app_setting_cache_covers(max(row["id"] for row in changes))
    else:
        with pytest.raises(settings.SettingsDbError):
            service.fetch_changes_since(0, refresh_app_settings=True)
    assert len(epochs) == 2
    assert service.get_app_setting("shared", "monitor_test") == f"new-{conflicts}"


def test_complete_worker_values_and_cursor_are_read_in_one_transaction(tmp_path, monkeypatch):
    db = SettingsDatabase(baza_dir=str(tmp_path / "settings"))
    db.ensure_ready()
    statements = []
    read = db.read_connection

    @contextmanager
    def traced_read():
        with read() as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(db, "read_connection", traced_read)
    rows, change_id = settings._read_complete_app_settings(db)
    assert isinstance(rows, list) and isinstance(change_id, int)
    assert statements[0] == "BEGIN"
    assert "MAX(id)" in statements[1] and "settings_change_log" in statements[1]
    assert "FROM app_settings" in statements[2]
    assert len(statements) == 3


def test_readonly_worker_timeout_never_starts_writer_lock_cleanup(monkeypatch):
    retry = settings._SettingsReleaseRetry(None, {})
    retry.process = SimpleNamespace(close=lambda: None)
    retry.job = {"refresh_only": True}
    monkeypatch.setattr(settings, "_stop_process_bounded", lambda *_args, **_kwargs: ("stopped", []))
    monkeypatch.setattr(settings.multiprocessing, "get_context", lambda *_args: pytest.fail("readonly job requested lock cleanup"))
    assert retry._finish_owned()
    assert retry.process is retry.job is None

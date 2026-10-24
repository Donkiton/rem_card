from __future__ import annotations

import json
import multiprocessing
import os
import socket
import sqlite3
import time
import uuid
from pathlib import Path

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

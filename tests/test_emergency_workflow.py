import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from rem_card.app.emergency_store import EmergencyLocalStore, EmergencyStoreError
from rem_card.app.emergency_workflow import (
    authorization_path, authorize_patient_merge, resume_local_work,
    validate_emergency_patient_source,
)
from rem_card.app.emergency_remote_identity import validate_remote_identity_error


def _source(path, *, cycle="cycle-1", populated=True):
    with sqlite3.connect(path) as conn:
        conn.executescript("CREATE TABLE patients(id INTEGER PRIMARY KEY);"
                           "CREATE TABLE admissions(id INTEGER PRIMARY KEY, patient_id INTEGER);"
                           "CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);")
        conn.execute("INSERT INTO meta VALUES('db_cycle_started_at',?)", (cycle,))
        if populated:
            conn.execute("INSERT INTO patients VALUES(1)")
            conn.execute("INSERT INTO admissions VALUES(11,1)")


def test_empty_emergency_creation_is_denied_before_any_files(tmp_path):
    root = tmp_path / "emergency"
    store = EmergencyLocalStore(root=str(root))
    with pytest.raises(EmergencyStoreError, match="пустой"):
        store.create_active_session_from_empty_database()
    assert not root.exists()


def test_source_requires_real_patient_admission_and_never_creates_missing_db(tmp_path):
    path = tmp_path / "base.db"
    assert validate_emergency_patient_source(str(path))
    assert not path.exists()
    _source(path, populated=False)
    assert "нет пациентов" in validate_emergency_patient_source(str(path))
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO patients VALUES(1)")
    assert validate_emergency_patient_source(str(path))
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO admissions VALUES(11,1)")
    assert validate_emergency_patient_source(str(path)) == ""


def test_same_path_after_rotation_is_a_different_database(tmp_path):
    base, remote = tmp_path / "base.db", tmp_path / "remote.db"
    _source(base)
    _source(remote)
    session = SimpleNamespace(base_remote_fingerprint={"path": str(remote)},
                              base_remote_db_path=str(remote), base_snapshot_path=str(base))
    assert validate_remote_identity_error(session, str(remote)) == ""
    with sqlite3.connect(remote) as conn:
        conn.execute("UPDATE meta SET value='cycle-2'")
    assert "cycle changed" in validate_remote_identity_error(session, str(remote))


def test_review_authorization_is_explicit_and_resuming_invalidates_it(tmp_path):
    statuses = []
    store = SimpleNamespace(resolve_root=lambda: str(tmp_path),
                            mark_session_status=lambda *args: statuses.append(args))
    with pytest.raises(ValueError):
        authorize_patient_merge(store, "session1", {"selected_admission_ids": []})
    assert not Path(authorization_path(store, "session1")).exists()
    selected = {"selected_admission_ids": [12, 11], "plan_digest": "approved-digest"}
    path = authorize_patient_merge(store, "session1", selected)
    assert json.loads(Path(path).read_text(encoding="utf-8"))["selected_admission_ids"] == [11, 12]
    resume_local_work(store, "session1")
    assert not Path(path).exists()
    assert statuses == [("session1", "active")]


def test_waiting_session_blocks_rotation():
    from rem_card.app.db_lifecycle import ROTATION_BLOCKING_EMERGENCY_STATUSES
    from rem_card.app.emergency_metadata import SESSION_STATUSES

    assert "waiting" in SESSION_STATUSES & ROTATION_BLOCKING_EMERGENCY_STATUSES


@pytest.mark.parametrize("role", ["doctor", "nurse"])
def test_network_start_keeps_waiting_session_for_review(monkeypatch, tmp_path, role):
    from rem_card.app import main, emergency_startup

    metadata = SimpleNamespace(status="waiting", emergency_session_id="session1",
                               local_db_path=str(tmp_path / "session1" / "local.db"))
    decision = SimpleNamespace(active_session_metadata=metadata, allowed=True,
                               root=str(tmp_path), status="active_session_available")
    monkeypatch.setattr(emergency_startup, "prepare_emergency_startup", lambda role: decision)
    monkeypatch.setattr(emergency_startup, "record_emergency_startup_metric", lambda *a, **kw: None)
    runtime = object()
    monkeypatch.setattr(main, "_start_active_emergency_session_for_startup", lambda value: runtime)
    monkeypatch.setattr(main, "_show_active_emergency_startup_choice", lambda *a, **kw: pytest.fail("legacy choice"))
    assert main._resolve_active_emergency_session_before_network_start(role) is runtime


def test_discovery_failure_stops_startup_instead_of_opening_network(monkeypatch):
    from rem_card.app import main, emergency_startup

    monkeypatch.setattr(emergency_startup, "prepare_emergency_startup",
                        lambda role: (_ for _ in ()).throw(OSError("metadata inaccessible")))
    notices = []
    monkeypatch.setattr(main, "_show_startup_warning_without_settings", lambda *args: notices.append(args))
    assert main._resolve_active_emergency_session_before_network_start("nurse") is False
    assert len(notices) == 1


@pytest.mark.parametrize("role", ["doctor", "nurse"])
def test_waiting_session_is_selected_before_network_startup_guard(monkeypatch, role):
    from rem_card.app import main

    runtime = SimpleNamespace(mode="emergency")
    calls = []
    monkeypatch.setattr(main, "_start_preselected_operblock_offline_context", lambda *a, **kw: (None, ""))
    def discover(*args, **kwargs):
        calls.append("saved_session")
        return runtime
    def guard(role, selected, **kwargs):
        assert selected is runtime
        assert calls == ["saved_session"]
        calls.append("guard")
        return True
    monkeypatch.setattr(main, "_resolve_active_emergency_session_before_network_start", discover)
    monkeypatch.setattr(main, "_validate_compiled_startup_unless_runtime_preselected", guard)
    monkeypatch.setattr(main, "_resolve_startup_runtime_context_after_guard", lambda role, state, close: state["runtime_context"])
    monkeypatch.setattr(main, "_run_pending_emergency_merge_after_startup_guard", lambda *a: None)
    monkeypatch.setattr(main, "_acquire_role_lock_for_startup", lambda *a: "local-role-lock")
    args = SimpleNamespace(role=role, emergency_startup_request="")
    result = main._prepare_runtime_context_for_startup(args, False, SimpleNamespace(close=lambda: None))
    assert result == (runtime, "saved_emergency_session", "local-role-lock")


@pytest.mark.parametrize("status,recovery", [("merging", False), ("merge_pending", False), ("merge_failed", True)])
def test_resume_refuses_unresolved_commit_before_removing_approval(tmp_path, status, recovery):
    metadata = SimpleNamespace(status=status, merge_recovery_required=recovery)
    store = SimpleNamespace(resolve_root=lambda: str(tmp_path), read_active_session=lambda _: metadata)
    path = Path(authorize_patient_merge(store, "session1", {"selected_admission_ids": [11], "plan_digest": "digest"}))
    with pytest.raises(ValueError, match="результат"):
        resume_local_work(store, "session1")
    assert path.is_file()

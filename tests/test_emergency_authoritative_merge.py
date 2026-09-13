from __future__ import annotations

import shutil
import sqlite3
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import rem_card.app.emergency_row_level_merge as row_merge
from rem_card.app.emergency_row_level_merge import (
    EmergencyRowLevelMergeService,
    apply_row_merge_plan,
    build_row_merge_plan,
    has_merge_receipt,
    plan_digest,
)


SCHEMA = """
CREATE TABLE patients (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE admissions (id INTEGER PRIMARY KEY, patient_id INTEGER, bed_number INTEGER, note TEXT);
CREATE TABLE beds (id INTEGER PRIMARY KEY, current_admission_id INTEGER, status TEXT);
CREATE TABLE vitals (id INTEGER PRIMARY KEY, admission_id INTEGER, value TEXT);
CREATE TABLE orders (id INTEGER PRIMARY KEY, admission_id INTEGER, text TEXT);
CREATE TABLE administrations (id INTEGER PRIMARY KEY, order_id INTEGER, status TEXT);
CREATE TABLE procedures (id INTEGER PRIMARY KEY, admission_id INTEGER, procedure_type TEXT);
CREATE TABLE procedure_consents (id INTEGER PRIMARY KEY, procedure_id INTEGER, consent_mode TEXT);
"""


def _db(path):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO patients VALUES (1, 'Иванов')")
    conn.execute("INSERT INTO patients VALUES (2, 'Петров')")
    conn.execute("INSERT INTO admissions VALUES (10, 1, 1, 'base')")
    conn.execute("INSERT INTO admissions VALUES (20, 2, 2, 'base')")
    conn.execute("INSERT INTO beds VALUES (1, 10, 'occupied')")
    conn.execute("INSERT INTO beds VALUES (2, 20, 'occupied')")
    conn.execute("INSERT INTO vitals VALUES (100, 10, 'base')")
    conn.execute("INSERT INTO vitals VALUES (200, 20, 'base')")
    conn.execute("INSERT INTO orders VALUES (300, 10, 'base')")
    conn.execute("INSERT INTO administrations VALUES (400, 300, 'planned')")
    conn.execute("INSERT INTO procedures VALUES (500, 10, 'cvc')")
    conn.execute("INSERT INTO procedure_consents VALUES (600, 500, 'patient')")
    conn.commit()
    conn.close()


def _copies(tmp_path):
    base = tmp_path / "base.db"
    local = tmp_path / "local.db"
    remote = tmp_path / "remote.db"
    _db(base)
    shutil.copyfile(base, local)
    shutil.copyfile(base, remote)
    return base, local, remote


def _plan(base, local, remote, selected=None):
    return build_row_merge_plan(
        base_db_path=str(base), local_db_path=str(local), remote_db_path=str(remote),
        selected_admission_ids=selected, authoritative=True,
    )


def test_authoritative_scope_overrides_changed_only_remote_and_deletes_remote_only_row(tmp_path):
    base, local, remote = _copies(tmp_path)
    conn = sqlite3.connect(remote)
    conn.execute("UPDATE vitals SET value = 'remote' WHERE id = 100")
    conn.execute("INSERT INTO vitals VALUES (101, 10, 'remote only')")
    conn.commit(); conn.close()

    plan = _plan(base, local, remote, [10])

    assert plan.ok
    assert {(op.action, op.pk_values, op.admission_id) for op in plan.operations} == {
        ("update", (100,), 10), ("delete", (101,), 10),
    }
    apply_row_merge_plan(str(remote), plan)
    conn = sqlite3.connect(remote)
    assert conn.execute("SELECT value FROM vitals WHERE id = 100").fetchone()[0] == "base"
    assert conn.execute("SELECT COUNT(*) FROM vitals WHERE id = 101").fetchone()[0] == 0
    conn.close()


def test_authoritative_subset_preserves_unselected_remote_patient(tmp_path):
    base, local, remote = _copies(tmp_path)
    conn = sqlite3.connect(remote)
    conn.execute("UPDATE vitals SET value = 'remote selected' WHERE id = 100")
    conn.execute("UPDATE vitals SET value = 'remote other' WHERE id = 200")
    conn.commit(); conn.close()

    plan = _plan(base, local, remote, [10])
    assert plan.ok
    apply_row_merge_plan(str(remote), plan)
    conn = sqlite3.connect(remote)
    assert conn.execute("SELECT value FROM vitals WHERE id = 100").fetchone()[0] == "base"
    assert conn.execute("SELECT value FROM vitals WHERE id = 200").fetchone()[0] == "remote other"
    conn.close()


def test_preview_auto_selects_local_change_and_digest_is_stable(tmp_path):
    base, local, remote = _copies(tmp_path)
    conn = sqlite3.connect(local)
    conn.execute("UPDATE vitals SET value = 'local' WHERE id = 100")
    conn.commit(); conn.close()
    plan = _plan(base, local, remote)
    assert plan.ok
    assert any(op.admission_id == 10 for op in plan.operations)
    assert plan_digest(plan) == plan_digest(plan)


def test_selected_subset_with_shared_patient_or_bed_is_rejected(tmp_path):
    base, local, remote = _copies(tmp_path)
    conn = sqlite3.connect(local)
    conn.execute("INSERT INTO admissions VALUES (11, 1, 1, 'same patient and bed')")
    conn.execute("UPDATE patients SET name = 'Иванов изменён' WHERE id = 1")
    conn.commit(); conn.close()
    plan = _plan(base, local, remote, [10])
    codes = {item["code"] for item in plan.blockers}
    assert "selected_scope_shared_patient" in codes
    assert "selected_scope_shared_bed" in codes
    assert any("Нельзя выбрать" in item["reason"] for item in plan.blockers)


def test_selected_card_deletion_and_bed_reset_do_not_touch_unselected_bed(tmp_path):
    base, local, remote = _copies(tmp_path)
    conn = sqlite3.connect(local)
    conn.execute("DELETE FROM vitals WHERE id = 100")
    conn.commit(); conn.close()
    conn = sqlite3.connect(remote)
    conn.execute("UPDATE beds SET status = 'remote changed' WHERE id = 1")
    conn.execute("UPDATE beds SET status = 'other changed' WHERE id = 2")
    conn.execute("UPDATE vitals SET value = 'remote changed' WHERE id = 100")
    conn.commit(); conn.close()

    plan = _plan(base, local, remote, [10])
    assert plan.ok
    assert any(op.table == "vitals" and op.action == "delete" for op in plan.operations)
    assert any(op.table == "beds" and op.action == "update" for op in plan.operations)
    apply_row_merge_plan(str(remote), plan)
    conn = sqlite3.connect(remote)
    assert conn.execute("SELECT COUNT(*) FROM vitals WHERE id = 100").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM beds WHERE id = 1").fetchone()[0] == "occupied"
    assert conn.execute("SELECT status FROM beds WHERE id = 2").fetchone()[0] == "other changed"
    conn.close()


def test_indirect_order_and_procedure_children_follow_selected_admission(tmp_path):
    base, local, remote = _copies(tmp_path)
    conn = sqlite3.connect(remote)
    conn.execute("UPDATE administrations SET status = 'done remotely' WHERE id = 400")
    conn.execute("UPDATE procedure_consents SET consent_mode = 'representative' WHERE id = 600")
    conn.commit(); conn.close()

    plan = _plan(base, local, remote, [10])
    affected = {(op.table, op.admission_id) for op in plan.operations}
    assert ("administrations", 10) in affected
    assert ("procedure_consents", 10) in affected
    apply_row_merge_plan(str(remote), plan)
    conn = sqlite3.connect(remote)
    assert conn.execute("SELECT status FROM administrations WHERE id = 400").fetchone()[0] == "planned"
    assert conn.execute("SELECT consent_mode FROM procedure_consents WHERE id = 600").fetchone()[0] == "patient"
    conn.close()


def test_new_patient_and_admission_id_collision_are_remapped_not_overwritten(tmp_path):
    base, local, remote = _copies(tmp_path)
    conn = sqlite3.connect(local)
    conn.execute("INSERT INTO patients VALUES (3, 'Новый')")
    conn.execute("INSERT INTO admissions VALUES (30, 3, 3, 'local')")
    conn.execute("INSERT INTO vitals VALUES (300, 30, 'local')")
    conn.commit(); conn.close()
    conn = sqlite3.connect(remote)
    conn.execute("INSERT INTO patients VALUES (3, 'Другой')")
    conn.execute("INSERT INTO admissions VALUES (30, 2, 3, 'remote')")
    conn.execute("INSERT INTO vitals VALUES (300, 30, 'remote')")
    conn.commit(); conn.close()

    plan = _plan(base, local, remote, [30])
    assert plan.ok
    assert any(op.table == "patients" and op.insert_new_primary_key for op in plan.operations)
    apply_row_merge_plan(str(remote), plan)
    conn = sqlite3.connect(remote)
    assert conn.execute("SELECT name FROM patients WHERE id = 3").fetchone()[0] == "Другой"
    assert conn.execute("SELECT COUNT(*) FROM patients WHERE name = 'Новый'").fetchone()[0] == 1
    conn.close()


def test_receipt_makes_apply_idempotent(tmp_path):
    base, local, remote = _copies(tmp_path)
    plan = _plan(base, local, remote, [10])
    digest = plan_digest(plan)
    first = apply_row_merge_plan(str(remote), plan, session_id="session-1", approved_plan_digest=digest)
    second = apply_row_merge_plan(str(remote), plan, session_id="session-1", approved_plan_digest=digest)
    assert first["applied_operations"] == 0
    assert second["receipt_reused"] is True
    assert has_merge_receipt(str(remote), "session-1", digest)


@pytest.mark.parametrize("interruption", [None, "before_finalize", "archive"])
def test_successful_merge_clears_persisted_recovery_flag(tmp_path, monkeypatch, interruption):
    from scripts.regression_checks.emergency_merge import _prepare_mode_a_merge_fixture, _run_merge_dry_run_fixture
    from rem_card.app.emergency_startup import prepare_emergency_startup
    from rem_card.app.emergency_store import EmergencyLocalStore

    fixture = _prepare_mode_a_merge_fixture(str(tmp_path), local_change=False)
    session = fixture["session"]
    with sqlite3.connect(session.local_db_path) as conn:
        conn.execute("UPDATE admissions SET history_number = 'EMERGENCY-EDIT' WHERE id = 1")
    conn.close()
    fixture["dry_run_result"] = _run_merge_dry_run_fixture(fixture)
    service = EmergencyRowLevelMergeService(
        store=fixture["store"],
        runtime_context=fixture["store"].build_active_runtime_context(session.emergency_session_id),
        source_medical_db_path=fixture["medical_path"],
        source_settings_db_path=fixture["settings_path"],
        network_baza_dir=fixture["network_baza"],
    )
    preview = build_row_merge_plan(
        base_db_path=session.base_snapshot_path, local_db_path=session.local_db_path,
        remote_db_path=fixture["medical_path"], authoritative=True,
    )
    selected = sorted({op.admission_id for op in preview.operations if op.admission_id is not None})
    assert selected
    plan = build_row_merge_plan(
        base_db_path=session.base_snapshot_path, local_db_path=session.local_db_path,
        remote_db_path=fixture["medical_path"], authoritative=True, selected_admission_ids=selected,
    )
    assert plan.ok
    args = (session.emergency_session_id, fixture["dry_run_result"].report_path,
            fixture["marker_path"], selected, plan_digest(plan))
    if interruption:
        def fail_finalization(*_args):
            raise OSError("simulated interruption after commit")

        with monkeypatch.context() as patch:
            if interruption == "archive":
                patch.setattr(service._legacy, "archive_emergency_session", fail_finalization)
            else:
                patch.setattr(service, "_finalize_success", fail_finalization)
            failed = service.run_merge(*args)
        assert not failed.ok
        assert fixture["store"].read_active_session(session.emergency_session_id).merge_recovery_required
        assert has_merge_receipt(fixture["medical_path"], session.emergency_session_id, plan_digest(plan))
        root = fixture["store"].resolve_root()
        for role in ("doctor", "nurse"):
            startup = prepare_emergency_startup(role, root=root)
            assert startup.allowed and startup.status == "active_session_available", startup
            assert startup.active_session_metadata.merge_recovery_required
        restarted_store = EmergencyLocalStore(root=root)
        service = EmergencyRowLevelMergeService(
            store=restarted_store,
            runtime_context=restarted_store.build_active_runtime_context(session.emergency_session_id),
            source_medical_db_path=fixture["medical_path"],
            source_settings_db_path=fixture["settings_path"],
            network_baza_dir=fixture["network_baza"],
        )

    result = service.run_merge(*args)
    assert result.ok, result.to_dict()
    archived = json.loads((Path(result.archive_path) / "emergency_session.json").read_text(encoding="utf-8"))
    assert archived["status"] == "merged"
    assert archived["merge_result"] == "success"
    assert archived["merge_recovery_required"] is False
    assert result.requires_recovery is False
    assert not (Path(fixture["store"].resolve_root()) / "active" / session.emergency_session_id).exists()


def test_failed_commit_rolls_back_rows_and_does_not_write_receipt(tmp_path):
    base, local, remote = _copies(tmp_path)
    conn = sqlite3.connect(remote)
    conn.execute("UPDATE vitals SET value = 'remote' WHERE id = 100")
    conn.execute("CREATE TRIGGER reject_vital BEFORE UPDATE ON vitals BEGIN SELECT RAISE(ABORT, 'forced commit failure'); END")
    conn.commit(); conn.close()
    plan = _plan(base, local, remote, [10])
    digest = plan_digest(plan)
    try:
        apply_row_merge_plan(str(remote), plan, session_id="broken", approved_plan_digest=digest)
    except sqlite3.DatabaseError:
        pass
    else:
        raise AssertionError("forced failure did not abort the transaction")
    conn = sqlite3.connect(remote)
    assert conn.execute("SELECT value FROM vitals WHERE id = 100").fetchone()[0] == "remote"
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='emergency_merge_receipts'").fetchone()[0] == 0
    conn.close()
    assert not has_merge_receipt(str(remote), "broken", digest)


def test_service_retry_uses_receipt_before_digest_rebuild(tmp_path, monkeypatch):
    base, local, remote = _copies(tmp_path)
    plan = _plan(base, local, remote, [10])
    digest = plan_digest(plan)
    apply_row_merge_plan(str(remote), plan, session_id="retry-session", approved_plan_digest=digest)
    session = SimpleNamespace(emergency_session_id="retry-session", base_last_change_id=0, status="merge_pending")
    context = SimpleNamespace(medical_db_path=str(remote))
    prereq = SimpleNamespace(
        ok=True, session=session, context=context, marker_path="marker", dry_run_report_path="dry-run",
        warnings=[], blockers=[], local_validation={"local": {"last_change_id": 0}},
    )
    legacy = SimpleNamespace(
        load_and_validate_prerequisites=lambda *_args: prereq,
        acquire_merge_locks=lambda *_args: {"ok": True, "_locks": []},
        _recheck_session_locks=lambda *_args: {"ok": True},
        _validate_remote_unchanged=lambda *_args: {"ok": True, "remote_last_change_id": 0, "local_last_change_id": 0},
        create_fresh_standby_after_merge=lambda *_args: {"ok": True},
    )
    service = EmergencyRowLevelMergeService.__new__(EmergencyRowLevelMergeService)
    service._legacy = legacy
    service.store = SimpleNamespace(resolve_root=lambda: str(tmp_path), read_active_session=lambda _: session)
    service.write_merge_report = lambda result: result.report_path
    service.validate_final_remote_db = lambda _path: {"ok": True, "file_hash": "hash", "quick_check": "ok", "integrity_check": "ok", "foreign_key_check": "ok", "last_change_id": 0}
    finalized = []
    service._finalize_success = lambda _session, result: finalized.append(result) or result
    monkeypatch.setattr(row_merge, "build_row_merge_plan", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("receipt retry rebuilt plan")))

    result = service.run_merge("retry-session", "dry-run", "marker", [10], digest)

    assert result.ok
    assert finalized


def test_historical_bed_and_repeated_patient_do_not_expand_selected_scope(tmp_path):
    base, local, remote = _copies(tmp_path)
    for path in (base, local, remote):
        conn = sqlite3.connect(path)
        conn.execute("ALTER TABLE admissions ADD COLUMN is_active INTEGER DEFAULT 1")
        conn.execute("UPDATE admissions SET is_active = 0, bed_number = 1, patient_id = 1 WHERE id = 20")
        conn.commit(); conn.close()
    conn = sqlite3.connect(remote)
    conn.execute("UPDATE vitals SET value = 'remote historical' WHERE id = 200")
    conn.commit(); conn.close()
    plan = _plan(base, local, remote, [10])
    assert not plan.blockers
    assert not any(op.pk_values == (200,) for op in plan.operations)


def test_preview_includes_network_only_difference_for_untouched_local_card(tmp_path):
    base, local, remote = _copies(tmp_path)
    conn = sqlite3.connect(remote)
    conn.execute("UPDATE vitals SET value = 'remote only' WHERE id = 200")
    conn.commit(); conn.close()
    plan = _plan(base, local, remote)
    assert any(op.pk_values == (200,) and op.admission_id == 20 for op in plan.operations)


def test_rao_arrival_from_operating_room_is_not_excluded(tmp_path):
    base, local, remote = _copies(tmp_path)
    for path in (base, local, remote):
        with sqlite3.connect(path) as conn:
            conn.execute("ALTER TABLE admissions ADD COLUMN unit_scope TEXT DEFAULT 'rao'")
            conn.execute("ALTER TABLE admissions ADD COLUMN source_department TEXT DEFAULT 'Операционная'")
    with sqlite3.connect(remote) as conn:
        conn.execute("UPDATE vitals SET value='remote' WHERE id=100")
    assert any(op.pk_values == (100,) for op in _plan(base, local, remote).operations)


def test_shared_patient_change_checks_remote_only_admission(tmp_path):
    base, local, remote = _copies(tmp_path)
    with sqlite3.connect(remote) as conn:
        conn.execute("INSERT INTO admissions(id,patient_id,bed_number) VALUES(30,1,3)")
        conn.execute("UPDATE patients SET name='Remote corrected name' WHERE id=1")
    assert "selected_scope_shared_patient" in {item["code"] for item in _plan(base, local, remote, [10]).blockers}


def test_remote_bed_occupied_by_unselected_patient_is_not_overwritten(tmp_path):
    base, local, remote = _copies(tmp_path)
    with sqlite3.connect(remote) as conn:
        conn.execute("UPDATE beds SET current_admission_id=20 WHERE id=1")
    plan = _plan(base, local, remote, [10])
    assert "authoritative_bed_conflict" in {item["code"] for item in plan.blockers}


def test_remote_selected_record_with_local_unselected_id_collision_is_explicit(tmp_path):
    base, local, remote = _copies(tmp_path)
    with sqlite3.connect(local) as conn:
        conn.execute("INSERT INTO vitals VALUES(999,10,'local new')")
    with sqlite3.connect(remote) as conn:
        conn.execute("INSERT INTO vitals VALUES(999,20,'remote new')")
    partial = _plan(base, local, remote, [20])
    assert "selected_scope_identity_collision" in {item["code"] for item in partial.blockers}
    assert not _plan(base, local, remote, [10,20]).blockers


def test_full_preview_excludes_explicit_operblock_admission(tmp_path):
    base, local, remote = _copies(tmp_path)
    for path in (base, local, remote):
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE operation_cases (id INTEGER PRIMARY KEY, admission_id INTEGER, patient_id INTEGER)")
        conn.execute("INSERT INTO operation_cases VALUES (1, 20, 2)")
        conn.commit(); conn.close()
    conn = sqlite3.connect(remote)
    conn.execute("UPDATE vitals SET value = 'operblock remote' WHERE id = 200")
    conn.commit(); conn.close()
    plan = _plan(base, local, remote)
    assert not any(op.pk_values == (200,) for op in plan.operations)


def test_shared_template_blocks_only_when_template_write_is_needed(tmp_path):
    base, local, remote = _copies(tmp_path)
    for path in (base, local, remote):
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE diet_templates (id INTEGER PRIMARY KEY, title TEXT)")
        conn.execute("CREATE TABLE diet_plan (id INTEGER PRIMARY KEY, admission_id INTEGER, template_id INTEGER)")
        conn.execute("INSERT INTO diet_templates VALUES (1, 'base')")
        conn.execute("INSERT INTO diet_plan VALUES (1, 10, 1)")
        conn.execute("INSERT INTO diet_plan VALUES (2, 20, 1)")
        conn.commit(); conn.close()
    unchanged = _plan(base, local, remote, [10])
    assert "selected_scope_shared_template" not in {item["code"] for item in unchanged.blockers}
    conn = sqlite3.connect(remote)
    conn.execute("UPDATE diet_templates SET title = 'remote change' WHERE id = 1")
    conn.commit(); conn.close()
    changed = _plan(base, local, remote, [10])
    assert "selected_scope_shared_template" in {item["code"] for item in changed.blockers}
    conn = sqlite3.connect(local)
    conn.execute("DELETE FROM diet_plan WHERE admission_id = 20")
    conn.commit(); conn.close()
    remote_only_reference = _plan(base, local, remote, [10])
    assert "selected_scope_shared_template" in {item["code"] for item in remote_only_reference.blockers}


def test_committed_merge_survives_disconnect_before_local_finalization(tmp_path, monkeypatch):
    from scripts.emergency_db_acceptance_runner import (
        _apply_controlled_local_emergency_writes, _build_network_fixture, _run_dry_run,
        _run_mode_a_merge, _run_restore_probe, _start_nurse_emergency,
    )

    paths = _build_network_fixture(tmp_path, "commit_recovery")
    store, _, startup = _start_nurse_emergency(paths)
    _apply_controlled_local_emergency_writes(startup.metadata.local_db_path)
    session_id = startup.metadata.emergency_session_id
    probe = _run_restore_probe(paths, store, startup)["probe"]
    marker = probe.mark_merge_ready()
    dry_run = _run_dry_run(paths, store, session_id, marker)
    session = store.read_active_session(session_id)
    plan = build_row_merge_plan(base_db_path=session.base_snapshot_path,
        local_db_path=session.local_db_path, remote_db_path=paths["medical_path"],
        selected_admission_ids=[1], authoritative=True)
    digest = plan_digest(plan)
    captured = []

    def break_finalization(service):
        captured.append(service)
        service._finalize_success = lambda *args: (_ for _ in ()).throw(OSError("simulated process loss after COMMIT"))

    failed = _run_mode_a_merge(paths, store, session_id, dry_run.report_path, marker,
                               service_mutator=break_finalization)
    assert not failed.ok
    assert failed.requires_recovery is True
    assert store.read_active_session(session_id).merge_recovery_required
    assert has_merge_receipt(paths["medical_path"], session_id, digest)
    service = captured[0]
    # Unreachable receipt must never be mistaken for an absent receipt.
    with monkeypatch.context() as patch:
        patch.setattr(service._legacy, "_network_context", lambda: (_ for _ in ()).throw(OSError("offline")))
        unknown = service.run_merge(session_id, selected_admission_ids=[1], expected_plan_digest=digest)
        assert unknown.error_code == "merge_outcome_unknown"
        assert store.read_active_session(session_id).merge_recovery_required
    del service._finalize_success
    recovered = service.run_merge(session_id, selected_admission_ids=[1], expected_plan_digest=digest)
    assert recovered.ok and recovered.archive_path
    with sqlite3.connect(paths["medical_path"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM emergency_merge_receipts WHERE session_id=?", (session_id,)).fetchone()[0] == 1


def test_backup_failure_before_transaction_allows_review_and_local_resume(tmp_path):
    from scripts.emergency_db_acceptance_runner import (
        _apply_controlled_local_emergency_writes, _build_network_fixture, _file_hash,
        _run_dry_run, _run_mode_a_merge, _run_restore_probe, _start_nurse_emergency,
    )
    from rem_card.app.emergency_pending_merge import _merge_details_with_commit_state
    from rem_card.app.emergency_workflow import resume_local_work

    paths = _build_network_fixture(tmp_path, "backup_failure")
    store, _, startup = _start_nurse_emergency(paths)
    _apply_controlled_local_emergency_writes(startup.metadata.local_db_path)
    session_id = startup.metadata.emergency_session_id
    before = _file_hash(paths["medical_path"])
    marker = _run_restore_probe(paths, store, startup)["probe"].mark_merge_ready()
    dry_run = _run_dry_run(paths, store, session_id, marker)
    def fail_backup(service):
        service._legacy.create_pre_merge_backups = lambda *args: (_ for _ in ()).throw(OSError("backup disk unavailable"))
    failed = _run_mode_a_merge(paths, store, session_id, dry_run.report_path, marker, service_mutator=fail_backup)
    assert not failed.ok and failed.requires_recovery is False
    assert _merge_details_with_commit_state(failed)["requires_recovery"] is False
    assert _file_hash(paths["medical_path"]) == before
    resume_local_work(store, session_id)
    assert store.read_active_session(session_id).status == "active"

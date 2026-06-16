#!/usr/bin/env python
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
try:
    from _local_rem_card_bootstrap import bootstrap_local_rem_card

    bootstrap_local_rem_card()
except Exception:
    pass


@dataclass
class ScenarioResult:
    name: str
    ok: bool
    details: str = ""
    artifacts: dict[str, Any] = field(default_factory=dict)


def _require(condition: bool, message: str, **artifacts) -> None:
    if not condition:
        raise AssertionError(f"{message} | {artifacts}" if artifacts else message)


def _network_context(baza_dir: Path):
    from rem_card.app.db_runtime_context import DbRuntimeContext

    root = baza_dir.resolve()
    archive = root / "archiv"
    settings = root / "settings"
    backup_health = root / "backup_health"
    backups = root / "backups"
    for part in (
        archive,
        settings,
        settings / "backups",
        settings / "backup_health",
        backups / "valid",
        backup_health / "invalid_backups",
        root / "locks",
        root / "logs",
        root / "quarantine",
        root / "session_locks",
        root / "snapshots",
        root / "config",
    ):
        part.mkdir(parents=True, exist_ok=True)
    return DbRuntimeContext(
        mode="network",
        medical_db_path=str(archive / "rao_journal.db"),
        medical_db_lock_path=str(archive / "db.lock"),
        medical_backups_valid_dir=str(backups / "valid"),
        medical_backup_health_dir=str(backup_health),
        medical_quarantine_dir=str(root / "quarantine"),
        medical_snapshots_dir=str(root / "snapshots"),
        medical_logs_dir=str(root / "logs"),
        recovery_lock_path=str(root / "locks" / "recovery.lock"),
        session_locks_dir=str(root / "session_locks"),
        settings_db_path=str(settings / "remcard_settings.db"),
        settings_db_lock_path=str(settings / "settings.db.lock"),
        settings_backups_dir=str(settings / "backups"),
        settings_backup_health_dir=str(settings / "backup_health"),
        settings_readonly=False,
        source_label="acceptance_network",
        is_network=True,
        is_emergency=False,
        is_snapshot=False,
        baza_dir=str(root),
        medical_backups_root_dir=str(backups),
        medical_invalid_backups_dir=str(backup_health / "invalid_backups"),
        medical_db_rotation_lock_path=str(archive / "db_rotation.lock"),
        medical_client_policy_path=str(root / "config" / "client_policy.json"),
        medical_startup_quickcheck_state_path=str(backup_health / "startup_quick_check_state.json"),
    )


def _make_manager(context):
    from rem_card.app.operblock_schema import ensure_operblock_schema
    from rem_card.data.dao.db_manager import DatabaseManager

    manager = DatabaseManager(context.medical_db_path, context.medical_db_path, runtime_context=context)
    ensure_operblock_schema(manager)
    return manager


def _make_local_manager(root: Path):
    from rem_card.app.operblock_offline_store import start_or_resume_operblock_offline_session

    session = start_or_resume_operblock_offline_session(reason="acceptance", root=str(root))
    return _make_manager(session.runtime_context)


def _case_payload(table_code: str, suffix: str = "1") -> dict[str, Any]:
    return {
        "table_code": table_code,
        "history_number": f"{suffix}/26",
        "full_name": f"Пациент Тестовый {suffix}",
        "gender": "м",
        "birth_date": date(1980, 1, 1),
        "diagnosis_code": "S82.0",
        "diagnosis_text": "Тестовый диагноз",
        "department_profile": "Хирургия",
        "operation_name": "Тестовая операция",
        "surgeons": ("Иванов И.И.",),
        "anesthesiologist": "Петров П.П.",
        "anesthetist": "Сидорова С.С.",
        "preop_sys": 120,
        "preop_dia": 70,
        "preop_pulse": 80,
        "preop_spo2": 98,
    }


def _create_closed_case(manager, *, table_code: str = "emergency", suffix: str = "1", transfer: str = "Хирургия") -> int:
    from rem_card.services.operblock_service import OperBlockService

    service = OperBlockService(manager)
    result = service.create_operation_case(_case_payload(table_code, suffix))
    case_id = int(result["operation_case_id"])
    row = _query_one(manager.db_path, "SELECT started_at FROM operation_cases WHERE id = ?", (case_id,))
    started_at = datetime.fromisoformat(str(row[0]).replace(" ", "T")) if row and row[0] else datetime.now()
    base = started_at + timedelta(minutes=int(suffix) * 10 if str(suffix).isdigit() else 0)
    service.start_anesthesia(case_id, event_time=base + timedelta(minutes=1), assistance_type="ЭТН")
    service.start_surgery(case_id, event_time=base + timedelta(minutes=2), operation_name="Тестовая операция")
    service.end_surgery(case_id, event_time=base + timedelta(minutes=3))
    service.end_anesthesia_with_transfer(case_id, transfer, event_time=base + timedelta(minutes=4))
    service.release_operation_table(case_id)
    return case_id


def _create_active_case(manager, *, table_code: str = "emergency", suffix: str = "1") -> int:
    from rem_card.services.operblock_service import OperBlockService

    result = OperBlockService(manager).create_operation_case(_case_payload(table_code, suffix))
    return int(result["operation_case_id"])


def _query_one(db_path: str, query: str, params: tuple[Any, ...] = ()) -> Any:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(query, params).fetchone()
    finally:
        conn.close()


def _run_with_managers(temp_root: Path, name: str, func: Callable[[Any, Any, Path, Path], None]) -> ScenarioResult:
    network_root = temp_root / name / "network"
    offline_root = temp_root / name / "offline"
    network_manager = None
    local_manager = None
    try:
        network_manager = _make_manager(_network_context(network_root))
        local_manager = _make_local_manager(offline_root)
        func(network_manager, local_manager, network_root, offline_root)
        return ScenarioResult(name, True, "ok")
    except Exception as exc:
        return ScenarioResult(name, False, str(exc), {"traceback": traceback.format_exc()})
    finally:
        for manager in (local_manager, network_manager):
            if manager is not None:
                try:
                    manager.close(timeout_sec=1.0)
                except Exception:
                    pass


def _scenario_initial_offline(temp_root: Path) -> ScenarioResult:
    name = "initial_network_missing"
    offline_root = temp_root / name / "offline"
    manager = None
    try:
        manager = _make_local_manager(offline_root)
        from rem_card.services.operblock_service import OperBlockService

        service = OperBlockService(manager)
        _require(service.list_archived_operation_cases() == [], "initial local archive must be empty")
        _create_closed_case(manager, suffix="1", transfer="РАО")
        archived = service.list_archived_operation_cases()
        _require(len(archived) == 1, "local archive must show completed local case", archived=archived)
        manager.close(timeout_sec=1.0)
        manager = _make_local_manager(offline_root)
        archived_after_restart = OperBlockService(manager).list_archived_operation_cases()
        _require(len(archived_after_restart) == 1, "repeat offline start must load previous local case")
        row = _query_one(
            manager.db_path,
            "SELECT future_rao_admission_id FROM operation_cases WHERE status = 'closed' LIMIT 1",
        )
        _require(row and row[0] is None, "offline RAO transfer must not create recovery admission")
        return ScenarioResult(name, True, "ok")
    except Exception as exc:
        return ScenarioResult(name, False, str(exc), {"traceback": traceback.format_exc()})
    finally:
        if manager is not None:
            try:
                manager.close(timeout_sec=1.0)
            except Exception:
                pass


def _scenario_migration(network_manager, local_manager, _network_root: Path, offline_root: Path) -> None:
    from rem_card.app.operblock_offline_migration import run_pending_operblock_offline_migration

    _create_closed_case(local_manager, suffix="2", transfer="РАО")
    result = run_pending_operblock_offline_migration(network_manager, root=str(offline_root))
    _require(result.ok and result.migrated_cases == 1, "migration must import one completed case", result=result)
    row = _query_one(
        network_manager.db_path,
        "SELECT COUNT(*), SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) FROM operation_cases",
    )
    _require(row and row[0] == 1 and row[1] == 1, "network archive must contain migrated closed case", row=row)
    active_rao = _query_one(
        network_manager.db_path,
        """
        SELECT COUNT(*)
        FROM admissions
        WHERE is_active = 1
          AND COALESCE(unit_scope, '') <> 'operblock'
        """,
    )
    _require(active_rao and int(active_rao[0] or 0) == 0, "offline RAO migration must not occupy recovery bed")
    _require(os.path.isfile(result.network_backup_path), "network backup must exist", backup=result.network_backup_path)
    _require(os.path.isfile(result.local_backup_path), "local backup must exist", backup=result.local_backup_path)


def _scenario_migration_non_rao_department(network_manager, local_manager, _network_root: Path, offline_root: Path) -> None:
    from rem_card.app.operblock_offline_migration import run_pending_operblock_offline_migration

    _create_closed_case(local_manager, suffix="22", transfer="Травматология")
    result = run_pending_operblock_offline_migration(network_manager, root=str(offline_root))
    _require(result.ok and result.migrated_cases == 1, "non-RAO migration must import one completed case", result=result)
    row = _query_one(
        network_manager.db_path,
        """
        SELECT status, transfer_department, future_rao_admission_id
        FROM operation_cases
        LIMIT 1
        """,
    )
    _require(
        row and row[0] == "closed" and row[1] == "Травматология" and row[2] is None,
        "non-RAO offline migration must remain archived without recovery admission",
        row=row,
    )
    active_rao = _query_one(
        network_manager.db_path,
        """
        SELECT COUNT(*)
        FROM admissions
        WHERE is_active = 1
          AND COALESCE(unit_scope, '') <> 'operblock'
        """,
    )
    _require(active_rao and int(active_rao[0] or 0) == 0, "non-RAO migration must not occupy recovery bed")


def _scenario_active_blocks_migration(network_manager, local_manager, _network_root: Path, offline_root: Path) -> None:
    from rem_card.app.operblock_offline_migration import run_pending_operblock_offline_migration

    _create_active_case(local_manager, suffix="3")
    result = run_pending_operblock_offline_migration(network_manager, root=str(offline_root))
    _require(result.ok and result.blocked and not result.attempted, "active local case must block migration", result=result)


def _scenario_table_conflict(network_manager, local_manager, _network_root: Path, offline_root: Path) -> None:
    from rem_card.app.operblock_offline_migration import run_pending_operblock_offline_migration

    _create_active_case(network_manager, table_code="emergency", suffix="4")
    _create_closed_case(local_manager, table_code="emergency", suffix="5")
    result = run_pending_operblock_offline_migration(network_manager, root=str(offline_root))
    _require(not result.ok and result.blocked, "different active network case must block migration", result=result)


def _scenario_protocol_conflict(network_manager, local_manager, _network_root: Path, offline_root: Path) -> None:
    from rem_card.app.operblock_offline_migration import run_pending_operblock_offline_migration

    _create_closed_case(network_manager, table_code="planned", suffix="6")
    _create_closed_case(local_manager, table_code="planned", suffix="7")
    result = run_pending_operblock_offline_migration(network_manager, root=str(offline_root))
    _require(result.ok and result.migrated_cases == 1, "protocol conflict migration must succeed", result=result)
    with sqlite3.connect(network_manager.db_path) as conn:
        rows = conn.execute(
            """
            SELECT anesthesia_protocol_number, original_protocol_number
            FROM operation_cases
            WHERE original_protocol_number IS NOT NULL
            """
        ).fetchall()
    _require(rows and rows[0][0] != rows[0][1], "protocol conflict must assign new number and preserve original", rows=rows)


def _scenario_cancelled_excluded(network_manager, local_manager, _network_root: Path, offline_root: Path) -> None:
    from rem_card.app.operblock_offline_migration import run_pending_operblock_offline_migration

    case_id = _create_active_case(local_manager, suffix="8")
    local_manager.execute_remcard(
        """
        UPDATE operation_cases
        SET status = 'cancelled',
            excluded_from_migration = 1,
            migration_status = 'discarded'
        WHERE id = ?
        """,
        (case_id,),
        source="acceptance_cancel_case",
    )
    result = run_pending_operblock_offline_migration(network_manager, root=str(offline_root))
    _require(result.ok and not result.attempted, "cancelled case must not be migrated", result=result)
    row = _query_one(network_manager.db_path, "SELECT COUNT(*) FROM operation_cases")
    _require(row and int(row[0] or 0) == 0, "network archive must not show cancelled case", row=row)


def _scenario_runtime_drop_same_case(network_manager, local_manager, _network_root: Path, offline_root: Path) -> None:
    from rem_card.app.operblock_offline_migration import run_pending_operblock_offline_migration
    from rem_card.app.operblock_offline_store import mirror_active_operblock_cases_from_network_db
    from rem_card.services.operblock_service import OperBlockService

    remote_case_id = _create_active_case(network_manager, table_code="planned", suffix="9")
    mirrored = mirror_active_operblock_cases_from_network_db(
        network_manager,
        reason="acceptance_runtime_drop",
        root=str(offline_root),
    )
    _require(mirrored == 1, "runtime drop mirror must copy active online case", mirrored=mirrored)
    row = _query_one(local_manager.db_path, "SELECT id, offline_case_uuid FROM operation_cases WHERE status = 'active'")
    _require(row and row[0], "local DB must contain mirrored active case", row=row)

    service = OperBlockService(local_manager)
    local_case_id = int(row[0])
    base = datetime.now() + timedelta(minutes=90)
    service.start_anesthesia(local_case_id, event_time=base + timedelta(minutes=1), assistance_type="ЭТН")
    service.start_surgery(local_case_id, event_time=base + timedelta(minutes=2), operation_name="Тестовая операция")
    service.end_surgery(local_case_id, event_time=base + timedelta(minutes=3))
    service.end_anesthesia_with_transfer(local_case_id, "Хирургия", event_time=base + timedelta(minutes=4))
    service.release_operation_table(local_case_id)

    result = run_pending_operblock_offline_migration(network_manager, root=str(offline_root))
    _require(result.ok and result.migrated_cases == 1, "same-case remote active table must allow migration", result=result)
    remote = _query_one(
        network_manager.db_path,
        "SELECT COUNT(*), MAX(status), MAX(id) FROM operation_cases WHERE offline_case_uuid = ?",
        (str(row[1]),),
    )
    _require(
        remote and int(remote[0] or 0) == 1 and remote[1] == "closed" and int(remote[2] or 0) == remote_case_id,
        "migration must update same remote case without duplicates",
        remote=remote,
    )


def _read_shadow_journal(root: Path) -> list[dict[str, Any]]:
    path = root / "active" / "shadow_journal.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def _scenario_precommit_journal_runtime_drop(network_manager, local_manager, _network_root: Path, offline_root: Path) -> None:
    from rem_card.services.data_service import DataService
    from rem_card.services.operblock_service import OperBlockService

    old_root = os.environ.get("REMCARD_OPERBLOCK_OFFLINE_ROOT")
    os.environ["REMCARD_OPERBLOCK_OFFLINE_ROOT"] = str(offline_root)
    data_service = DataService(network_manager, monitor_enabled=False)
    data_service.set_runtime_role("operblock")
    try:
        service = OperBlockService(network_manager)
        accepted = data_service.enqueue_write(
            "operblock_create_operation_case",
            lambda: service.create_operation_case(_case_payload("planned", "11")),
        )
        _require(accepted, "opblock write must be accepted")
        data_service.prepare_runtime_outage_shutdown(timeout=5.0)
        events = _read_shadow_journal(offline_root)
        intent_index = next(
            (index for index, event in enumerate(events) if event.get("event") == "opblock_write_intent"),
            None,
        )
        committed_index = next(
            (index for index, event in enumerate(events) if event.get("event") == "opblock_write_remote_committed"),
            None,
        )
        _require(intent_index is not None, "pre-commit intent must be durable before queued write runs", events=events)
        _require(committed_index is not None, "confirmed commit must be marked in local journal", events=events)
        _require(intent_index < committed_index, "intent must be written before remote_committed marker", events=events)
        local_active = _query_one(local_manager.db_path, "SELECT COUNT(*) FROM operation_cases WHERE status = 'active'")
        _require(local_active and int(local_active[0] or 0) == 1, "runtime-drop committed write must be mirrored locally")
    finally:
        data_service.shutdown()
        if old_root is None:
            os.environ.pop("REMCARD_OPERBLOCK_OFFLINE_ROOT", None)
        else:
            os.environ["REMCARD_OPERBLOCK_OFFLINE_ROOT"] = old_root


def _scenario_unconfirmed_write_not_marked_saved(network_manager, _local_manager, _network_root: Path, offline_root: Path) -> None:
    from rem_card.services.data_service import DataService

    old_root = os.environ.get("REMCARD_OPERBLOCK_OFFLINE_ROOT")
    os.environ["REMCARD_OPERBLOCK_OFFLINE_ROOT"] = str(offline_root)
    data_service = DataService(network_manager, monitor_enabled=False)
    data_service.set_runtime_role("operblock")
    success_callbacks: list[Any] = []
    error_callbacks: list[str] = []
    try:
        accepted = data_service.enqueue_write(
            "operblock_add_vitals",
            lambda: (_ for _ in ()).throw(sqlite3.OperationalError("unable to open database file")),
            on_success=lambda result: success_callbacks.append(result),
            on_error=lambda exc: error_callbacks.append(str(exc)),
        )
        _require(accepted, "failing opblock write must be accepted into queue before execution")
        data_service.prepare_runtime_outage_shutdown(timeout=5.0)
        events = _read_shadow_journal(offline_root)
        _require(any(event.get("event") == "opblock_write_intent" for event in events), "failed write must have intent")
        _require(any(event.get("event") == "opblock_write_failed" for event in events), "failed write must be marked failed")
        _require(
            not any(event.get("event") == "opblock_write_remote_committed" for event in events),
            "unconfirmed failed write must not be marked remote_committed",
            events=events,
        )
        _require(not success_callbacks, "unconfirmed failed write must not call success callback", callbacks=success_callbacks)
    finally:
        data_service.shutdown()
        if old_root is None:
            os.environ.pop("REMCARD_OPERBLOCK_OFFLINE_ROOT", None)
        else:
            os.environ["REMCARD_OPERBLOCK_OFFLINE_ROOT"] = old_root


def _scenario_retention(network_manager, local_manager, _network_root: Path, offline_root: Path) -> None:
    from rem_card.app.operblock_offline_migration import run_pending_operblock_offline_migration
    from rem_card.app.operblock_offline_store import (
        cleanup_verified_operblock_offline_session,
        get_operblock_offline_metadata_path,
        read_operblock_offline_metadata,
        write_operblock_offline_metadata,
    )

    _create_closed_case(local_manager, suffix="10")
    result = run_pending_operblock_offline_migration(network_manager, root=str(offline_root))
    _require(result.ok and result.migrated_cases == 1, "retention setup migration must succeed", result=result)
    metadata = read_operblock_offline_metadata(str(offline_root))
    _require(metadata and metadata.migration_status == "verified", "session must be marked verified", metadata=metadata)
    metadata.retain_until = (datetime.now().astimezone() + timedelta(days=1)).isoformat(timespec="seconds")
    write_operblock_offline_metadata(metadata, str(offline_root))
    _require(
        not cleanup_verified_operblock_offline_session(network_manager, root=str(offline_root)),
        "verified session younger than retention must be preserved",
    )
    local_manager.close(timeout_sec=1.0)
    metadata.retain_until = (datetime.now().astimezone() - timedelta(days=1)).isoformat(timespec="seconds")
    write_operblock_offline_metadata(metadata, str(offline_root))
    _require(
        cleanup_verified_operblock_offline_session(network_manager, root=str(offline_root)),
        "verified session older than retention must be removed after network verification",
    )
    _require(not os.path.exists(get_operblock_offline_metadata_path(str(offline_root))), "metadata must be removed")


def _scenario_retention_preserves_unverified(network_manager, local_manager, _network_root: Path, offline_root: Path) -> None:
    from rem_card.app.operblock_offline_store import (
        cleanup_verified_operblock_offline_session,
        get_operblock_offline_metadata_path,
        read_operblock_offline_metadata,
        write_operblock_offline_metadata,
    )

    _create_closed_case(local_manager, suffix="12")
    metadata = read_operblock_offline_metadata(str(offline_root))
    _require(metadata is not None, "offline metadata must exist")
    metadata.migration_status = "verified"
    metadata.retain_until = (datetime.now().astimezone() - timedelta(days=1)).isoformat(timespec="seconds")
    write_operblock_offline_metadata(metadata, str(offline_root))
    _require(
        not cleanup_verified_operblock_offline_session(network_manager, root=str(offline_root)),
        "old session with pending/unverified case must not be deleted",
    )
    _require(os.path.exists(get_operblock_offline_metadata_path(str(offline_root))), "pending metadata must remain")


def main() -> int:
    temp_root = Path(tempfile.mkdtemp(prefix="remcard_opblock_offline_acceptance_"))
    results: list[ScenarioResult] = []
    try:
        os.environ["REMCARD_LOCAL_FIRST_SYNC"] = "0"
        os.environ["REMCARD_LOCAL_OUTBOX_SYNC"] = "0"
        scenarios = [
            lambda root: _scenario_initial_offline(root),
            lambda root: _run_with_managers(root, "migration", _scenario_migration),
            lambda root: _run_with_managers(root, "migration_non_rao_department", _scenario_migration_non_rao_department),
            lambda root: _run_with_managers(root, "active_blocks_migration", _scenario_active_blocks_migration),
            lambda root: _run_with_managers(root, "table_conflict", _scenario_table_conflict),
            lambda root: _run_with_managers(root, "protocol_conflict", _scenario_protocol_conflict),
            lambda root: _run_with_managers(root, "cancelled_excluded", _scenario_cancelled_excluded),
            lambda root: _run_with_managers(root, "runtime_drop_same_case", _scenario_runtime_drop_same_case),
            lambda root: _run_with_managers(root, "precommit_journal_runtime_drop", _scenario_precommit_journal_runtime_drop),
            lambda root: _run_with_managers(root, "unconfirmed_write_not_marked_saved", _scenario_unconfirmed_write_not_marked_saved),
            lambda root: _run_with_managers(root, "retention", _scenario_retention),
            lambda root: _run_with_managers(root, "retention_preserves_unverified", _scenario_retention_preserves_unverified),
        ]
        for scenario in scenarios:
            results.append(scenario(temp_root))
        ok = all(item.ok for item in results)
        summary = {
            "status": "passed" if ok else "failed",
            "temp_root": str(temp_root),
            "results": [item.__dict__ for item in results],
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return 0 if ok else 1
    finally:
        if os.environ.get("REMCARD_KEEP_OPERBLOCK_ACCEPTANCE_ARTIFACTS") != "1":
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import getpass
import os
import shutil
import socket
import sqlite3
import uuid
from dataclasses import replace
from datetime import datetime
from typing import Any

from rem_card.app.db_runtime_context import DbRuntimeContext
from rem_card.app.emergency_metadata import (
    EmergencyMetadataError,
    EmergencySessionMetadata,
    EmergencyStandbyMetadata,
    atomic_write_json,
    metadata_to_dict,
    read_json_file,
    session_metadata_from_dict,
    standby_metadata_from_dict,
)
from rem_card.app.emergency_paths import (
    active_base_snapshot_path,
    active_dir,
    active_medical_db_path,
    active_session_dir,
    active_session_metadata_path,
    active_settings_snapshot_path,
    archived_dir,
    archived_session_dir,
    emergency_client_id_path,
    resolve_emergency_root,
    standby_dir,
    standby_medical_db_path,
    standby_metadata_path,
    standby_settings_db_path,
)
from rem_card.app.emergency_validation import (
    compute_file_hash,
    validate_medical_db_snapshot,
    validate_settings_db_snapshot,
)
from rem_card.app.local_metrics import record_metric
from rem_card.app.version import APP_VERSION


class EmergencyStoreError(RuntimeError):
    pass


def _now_text() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _normalize_path(path: str) -> str:
    return os.path.abspath(os.path.normpath(str(path)))


def _map_session_path(path: str, active_dir_path: str, archive_dir_path: str) -> str:
    if not path:
        return path
    try:
        path_abs = os.path.abspath(os.path.normpath(path))
        active_abs = os.path.abspath(os.path.normpath(active_dir_path))
        if os.path.commonpath([os.path.normcase(path_abs), os.path.normcase(active_abs)]) == os.path.normcase(active_abs):
            rel_path = os.path.relpath(path_abs, active_abs)
            return _normalize_path(os.path.join(archive_dir_path, rel_path))
    except Exception:
        pass
    return path


def _map_optional_session_path(path: str | None, active_dir_path: str, archive_dir_path: str) -> str | None:
    if path is None:
        return None
    return _map_session_path(path, active_dir_path, archive_dir_path)


def _path_is_under(path: str, root: str) -> bool:
    try:
        path_abs = os.path.normcase(os.path.abspath(os.path.normpath(path)))
        root_abs = os.path.normcase(os.path.abspath(os.path.normpath(root)))
        return os.path.commonpath([path_abs, root_abs]) == root_abs
    except Exception:
        return False


def get_local_machine_name() -> str:
    return socket.gethostname() or "unknown"


def get_windows_user() -> str:
    try:
        return getpass.getuser() or os.environ.get("USERNAME") or "unknown"
    except Exception:
        return os.environ.get("USERNAME") or "unknown"


def get_or_create_local_emergency_client_id(root: str | None = None) -> str:
    client_id, _warning = get_or_create_local_emergency_client_id_with_warning(root)
    return client_id


def get_or_create_local_emergency_client_id_with_warning(root: str | None = None) -> tuple[str, str | None]:
    resolved_root = resolve_emergency_root(root)
    path = emergency_client_id_path(resolved_root)
    warning = None
    try:
        payload = read_json_file(path)
        client_id = str(payload.get("client_id") or "").strip()
        if client_id:
            return client_id, None
        warning = "empty emergency client id metadata"
    except EmergencyMetadataError as exc:
        if os.path.exists(path):
            warning = str(exc)
    client_id = f"emergency:{get_local_machine_name()}:{uuid.uuid4().hex}"
    atomic_write_json(
        path,
        {
            "client_id": client_id,
            "created_at": _now_text(),
            "source_machine": get_local_machine_name(),
            "source_windows_user": get_windows_user(),
            "regenerated_after_warning": warning,
        },
    )
    return client_id, warning


def _copy_file_once(source_path: str, target_path: str) -> None:
    if os.path.exists(target_path):
        raise EmergencyStoreError(f"Файл уже существует и не будет перезаписан: {target_path}")
    if not os.path.exists(source_path) or os.path.getsize(source_path) <= 0:
        raise EmergencyStoreError(f"Источник snapshot недоступен или пустой: {source_path}")
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    tmp_path = f"{target_path}.{uuid.uuid4().hex[:12]}.tmp"
    try:
        shutil.copy2(source_path, tmp_path)
        try:
            with open(tmp_path, "rb") as fh:
                os.fsync(fh.fileno())
        except OSError:
            pass
        if os.path.exists(target_path):
            raise EmergencyStoreError(f"Файл уже существует и не будет перезаписан: {target_path}")
        os.replace(tmp_path, target_path)
        tmp_path = ""
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _ensure_active_session_dirs(session_dir: str) -> None:
    for name in ("locks", "backups", "backup_health", "quarantine", "snapshots", "logs", "config"):
        os.makedirs(os.path.join(session_dir, name), exist_ok=True)
    os.makedirs(os.path.join(session_dir, "backups", "settings"), exist_ok=True)
    os.makedirs(os.path.join(session_dir, "backup_health", "settings"), exist_ok=True)
    os.makedirs(os.path.join(session_dir, "backup_health", "invalid_backups"), exist_ok=True)


class EmergencyLocalStore:
    def __init__(
        self,
        root: str | None = None,
        *,
        settings_required: bool = True,
        source_role: str = "nurse",
    ):
        self.root = resolve_emergency_root(root)
        self.settings_required = bool(settings_required)
        self.source_role = str(source_role or "nurse")

    def resolve_root(self) -> str:
        return self.root

    def ensure_root_dirs(self) -> None:
        for path in (standby_dir(self.root), active_dir(self.root), archived_dir(self.root)):
            os.makedirs(path, exist_ok=True)

    def write_standby_metadata(self, metadata: EmergencyStandbyMetadata) -> None:
        self.ensure_root_dirs()
        atomic_write_json(standby_metadata_path(self.root), metadata_to_dict(metadata))

    def read_standby_metadata(self) -> EmergencyStandbyMetadata:
        payload = read_json_file(standby_metadata_path(self.root))
        return standby_metadata_from_dict(payload)

    def list_valid_standby(self) -> list[EmergencyStandbyMetadata]:
        try:
            metadata = self.read_standby_metadata()
        except EmergencyMetadataError:
            return []
        if metadata.validation_status not in {"ok", "valid"}:
            return []
        if not os.path.exists(metadata.medical_db_path):
            return []
        if self.settings_required and (not metadata.settings_db_path or not os.path.exists(metadata.settings_db_path)):
            return []
        return [metadata]

    def get_latest_valid_standby(self) -> EmergencyStandbyMetadata | None:
        candidates = self.list_valid_standby()
        if not candidates:
            return None
        candidates.sort(key=lambda item: item.updated_at, reverse=True)
        return candidates[0]

    def delete_standby_files(self, metadata: EmergencyStandbyMetadata | None = None) -> int:
        directory = standby_dir(self.root)
        candidates = {
            standby_metadata_path(self.root),
            standby_medical_db_path(self.root),
            standby_settings_db_path(self.root),
        }
        if metadata is not None:
            for path in (metadata.medical_db_path, metadata.settings_db_path):
                if path and _path_is_under(str(path), directory):
                    candidates.add(str(path))
            generation_dir = str(getattr(metadata, "generation_dir", "") or "")
            if generation_dir and _path_is_under(generation_dir, directory):
                candidates.add(generation_dir)
        removed = 0
        for path in sorted(candidates, key=lambda item: len(str(item)), reverse=True):
            try:
                if os.path.isfile(path):
                    os.remove(path)
                    removed += 1
                elif os.path.isdir(path):
                    shutil.rmtree(path)
                    removed += 1
            except OSError:
                pass
        return removed

    def create_active_session_from_standby(
        self,
        standby_metadata: EmergencyStandbyMetadata,
        *,
        session_id: str | None = None,
    ) -> EmergencySessionMetadata:
        self.ensure_root_dirs()
        medical_validation = validate_medical_db_snapshot(standby_metadata.medical_db_path)
        if not medical_validation.ok:
            raise EmergencyStoreError(f"Standby medical DB не прошла validation: {medical_validation.reason}")

        settings_validation = None
        if self.settings_required:
            if not standby_metadata.settings_db_path:
                raise EmergencyStoreError("Standby settings DB обязательна, но путь не задан")
            settings_validation = validate_settings_db_snapshot(standby_metadata.settings_db_path)
            if not settings_validation.ok:
                raise EmergencyStoreError(f"Standby settings DB не прошла validation: {settings_validation.reason}")

        effective_session_id = session_id or f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:12]}"
        session_dir = active_session_dir(self.root, effective_session_id)
        base_snapshot_path = active_base_snapshot_path(self.root, effective_session_id)
        local_db_path = active_medical_db_path(self.root, effective_session_id)
        settings_snapshot_path = active_settings_snapshot_path(self.root, effective_session_id) if self.settings_required else None

        if os.path.exists(base_snapshot_path):
            raise EmergencyStoreError(f"base_snapshot.db уже существует для active session: {effective_session_id}")

        _ensure_active_session_dirs(session_dir)

        _copy_file_once(standby_metadata.medical_db_path, base_snapshot_path)
        _copy_file_once(standby_metadata.medical_db_path, local_db_path)
        if settings_snapshot_path:
            _copy_file_once(str(standby_metadata.settings_db_path), settings_snapshot_path)

        base_hash = compute_file_hash(base_snapshot_path)
        created_at = _now_text()
        source_client_id, client_id_warning = get_or_create_local_emergency_client_id_with_warning(self.root)
        validation_error = client_id_warning
        metadata = EmergencySessionMetadata(
            emergency_session_id=effective_session_id,
            status="active",
            created_at=created_at,
            started_at=created_at,
            ended_at=None,
            merged_at=None,
            source_machine=get_local_machine_name(),
            source_windows_user=get_windows_user(),
            source_client_id=source_client_id,
            source_role=self.source_role,
            app_version=APP_VERSION,
            schema_version=int(medical_validation.schema_version or standby_metadata.schema_version or 0),
            base_remote_db_path=standby_metadata.source_remote_db_path,
            base_remote_fingerprint=dict(standby_metadata.source_remote_fingerprint or medical_validation.fingerprint),
            base_last_change_id=int(standby_metadata.remote_last_change_id or medical_validation.last_change_id or 0),
            base_snapshot_hash=base_hash,
            base_snapshot_created_at=created_at,
            standby_last_change_id=int(standby_metadata.remote_last_change_id or medical_validation.last_change_id or 0),
            last_observed_remote_change_id=int(standby_metadata.remote_last_change_id or medical_validation.last_change_id or 0),
            local_db_path=_normalize_path(local_db_path),
            base_snapshot_path=_normalize_path(base_snapshot_path),
            settings_snapshot_path=None if settings_snapshot_path is None else _normalize_path(settings_snapshot_path),
            merge_attempt_count=0,
            last_merge_error=None,
            validation_status="ok",
            validation_error=validation_error,
        )
        self.write_active_session(metadata)
        return metadata

    def create_active_session_from_empty_database(
        self,
        *,
        session_id: str | None = None,
        settings_source_path: str | None = None,
        reason: str = "standby unavailable",
    ) -> EmergencySessionMetadata:
        self.ensure_root_dirs()
        effective_session_id = session_id or f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:12]}"
        session_dir = active_session_dir(self.root, effective_session_id)
        base_snapshot_path = active_base_snapshot_path(self.root, effective_session_id)
        local_db_path = active_medical_db_path(self.root, effective_session_id)
        settings_snapshot_path = active_settings_snapshot_path(self.root, effective_session_id) if self.settings_required else None

        if os.path.exists(base_snapshot_path):
            raise EmergencyStoreError(f"base_snapshot.db уже существует для active session: {effective_session_id}")
        if os.path.exists(local_db_path):
            raise EmergencyStoreError(f"rao_journal_emergency.db уже существует для active session: {effective_session_id}")

        _ensure_active_session_dirs(session_dir)
        self._create_empty_medical_snapshot(base_snapshot_path, session_dir)
        _copy_file_once(base_snapshot_path, local_db_path)
        if settings_snapshot_path:
            self._copy_or_create_settings_snapshot(
                settings_snapshot_path,
                session_dir,
                settings_source_path=settings_source_path,
            )

        base_validation = validate_medical_db_snapshot(base_snapshot_path)
        if not base_validation.ok:
            raise EmergencyStoreError(f"Пустая аварийная medical DB не прошла validation: {base_validation.reason}")
        local_validation = validate_medical_db_snapshot(local_db_path)
        if not local_validation.ok:
            raise EmergencyStoreError(f"Пустая локальная аварийная DB не прошла validation: {local_validation.reason}")
        settings_validation = None
        if settings_snapshot_path:
            settings_validation = validate_settings_db_snapshot(settings_snapshot_path)
            if not settings_validation.ok:
                raise EmergencyStoreError(f"Аварийная settings DB не прошла validation: {settings_validation.reason}")

        base_hash = compute_file_hash(base_snapshot_path)
        created_at = _now_text()
        source_client_id, client_id_warning = get_or_create_local_emergency_client_id_with_warning(self.root)
        metadata = EmergencySessionMetadata(
            emergency_session_id=effective_session_id,
            status="active",
            created_at=created_at,
            started_at=created_at,
            ended_at=None,
            merged_at=None,
            source_machine=get_local_machine_name(),
            source_windows_user=get_windows_user(),
            source_client_id=source_client_id,
            source_role=self.source_role,
            app_version=APP_VERSION,
            schema_version=int(base_validation.schema_version or 0),
            base_remote_db_path="",
            base_remote_fingerprint={},
            base_last_change_id=0,
            base_snapshot_hash=base_hash,
            base_snapshot_created_at=created_at,
            standby_last_change_id=0,
            last_observed_remote_change_id=0,
            local_db_path=_normalize_path(local_db_path),
            base_snapshot_path=_normalize_path(base_snapshot_path),
            settings_snapshot_path=None if settings_snapshot_path is None else _normalize_path(settings_snapshot_path),
            merge_attempt_count=0,
            last_merge_error=None,
            validation_status="ok",
            validation_error=client_id_warning,
        )
        self.write_active_session(metadata)
        return metadata

    def _create_empty_medical_snapshot(self, target_path: str, session_dir: str) -> None:
        if os.path.exists(target_path):
            raise EmergencyStoreError(f"Файл уже существует и не будет перезаписан: {target_path}")
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        conn = sqlite3.connect(target_path, check_same_thread=False, isolation_level=None, timeout=5.0)
        try:
            from rem_card.app.schema_migration_guard import ensure_unified_schema_with_migration_backup
            from rem_card.app.sqlite_shared import configure_connection, run_quick_check

            configure_connection(conn, profile="network")
            ensure_unified_schema_with_migration_backup(
                conn,
                db_path=target_path,
                backup_dir=os.path.join(session_dir, "backups"),
                invalid_dir=os.path.join(session_dir, "backup_health", "invalid_backups"),
                policy_path=os.path.join(session_dir, "config", "client_policy.json"),
                baza_dir=session_dir,
                lock_path=os.path.join(session_dir, "locks", "db.lock"),
                source="emergency_empty_schema_init",
            )
            ok, result = run_quick_check(conn)
            if not ok:
                raise EmergencyStoreError(f"Проверка пустой аварийной БД не пройдена: {result}")
        finally:
            conn.close()

    def _copy_or_create_settings_snapshot(
        self,
        target_path: str,
        session_dir: str,
        *,
        settings_source_path: str | None = None,
    ) -> None:
        source = str(settings_source_path or "").strip()
        if source:
            validation = validate_settings_db_snapshot(source)
            if validation.ok:
                _copy_file_once(source, target_path)
                return
        if os.path.exists(target_path):
            raise EmergencyStoreError(f"settings snapshot уже существует: {target_path}")
        from rem_card.data.settings.settings_db import SettingsDatabase
        from rem_card.services.settings.settings_service import SettingsService

        service = SettingsService(
            SettingsDatabase(
                settings_db_path=target_path,
                settings_db_lock_path=os.path.join(session_dir, "locks", "settings.db.lock"),
                settings_backups_dir=os.path.join(session_dir, "backups", "settings"),
                settings_backup_health_dir=os.path.join(session_dir, "backup_health", "settings"),
                readonly=False,
            )
        )
        service.ensure_ready()

    def rebuild_active_settings_snapshot_from_source(
        self,
        metadata: EmergencySessionMetadata,
        source_path: str,
        *,
        reason: str = "schema_drift",
    ) -> bool:
        target_path = str(metadata.settings_snapshot_path or "").strip()
        if not target_path:
            raise EmergencyStoreError("active settings snapshot path is missing")
        source = str(source_path or "").strip()
        source_validation = validate_settings_db_snapshot(source)
        if not source_validation.ok:
            raise EmergencyStoreError(f"Source settings snapshot is not valid: {source_validation.reason}")
        target_dir = os.path.dirname(target_path)
        os.makedirs(target_dir, exist_ok=True)
        tmp_path = os.path.join(target_dir, f".{os.path.basename(target_path)}.rebuild.{os.getpid()}.tmp")
        record_metric(
            "emergency_settings_snapshot_rebuild_started",
            1,
            reason=str(reason or "schema_drift"),
            emergency_session_id=metadata.emergency_session_id,
            expected_schema_version=source_validation.schema_version,
            source_settings_db_path=os.path.abspath(source),
            target_settings_db_path=os.path.abspath(target_path),
        )
        try:
            shutil.copy2(source, tmp_path)
            tmp_validation = validate_settings_db_snapshot(tmp_path)
            if not tmp_validation.ok:
                raise EmergencyStoreError(f"Rebuilt settings snapshot validation failed: {tmp_validation.reason}")
            os.replace(tmp_path, target_path)
            final_validation = validate_settings_db_snapshot(target_path)
            if not final_validation.ok:
                raise EmergencyStoreError(f"Final settings snapshot validation failed: {final_validation.reason}")
            record_metric(
                "emergency_settings_snapshot_rebuild_finished",
                1,
                reason=str(reason or "schema_drift"),
                emergency_session_id=metadata.emergency_session_id,
                expected_schema_version=source_validation.schema_version,
                actual_schema_version=final_validation.schema_version,
                settings_db_hash=final_validation.file_hash,
            )
            return True
        except Exception as exc:
            record_metric(
                "emergency_settings_snapshot_rebuild_failed",
                1,
                reason=str(reason or "schema_drift"),
                emergency_session_id=metadata.emergency_session_id,
                error=str(exc),
            )
            raise
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def read_active_session(self, session_id: str) -> EmergencySessionMetadata:
        payload = read_json_file(active_session_metadata_path(self.root, session_id))
        return session_metadata_from_dict(payload)

    def write_active_session(self, metadata: EmergencySessionMetadata) -> None:
        atomic_write_json(
            active_session_metadata_path(self.root, metadata.emergency_session_id),
            metadata_to_dict(metadata),
        )

    def mark_session_status(self, session_id: str, status: str, error: str | None = None) -> EmergencySessionMetadata:
        metadata = self.read_active_session(session_id)
        now = _now_text()
        metadata = replace(
            metadata,
            status=status,
            ended_at=now if status in {"merge_pending", "merged", "merge_failed", "archived", "discarded"} else metadata.ended_at,
            merged_at=now if status == "merged" else metadata.merged_at,
            last_merge_error=error if error is not None else metadata.last_merge_error,
            validation_status="error" if error else metadata.validation_status,
            validation_error=error if error else metadata.validation_error,
        )
        self.write_active_session(metadata)
        return metadata

    def archive_session(self, session_id: str) -> str:
        metadata = self.mark_session_status(session_id, "archived")
        source_dir = active_session_dir(self.root, metadata.emergency_session_id)
        target_dir = archived_session_dir(self.root, metadata.emergency_session_id)
        if not os.path.isdir(source_dir):
            raise EmergencyStoreError(f"Active session не найдена: {source_dir}")
        if os.path.exists(target_dir):
            raise EmergencyStoreError(f"Archived session уже существует: {target_dir}")
        os.makedirs(os.path.dirname(target_dir), exist_ok=True)
        shutil.move(source_dir, target_dir)
        return target_dir

    def mark_session_discarded(
        self,
        session_id: str,
        *,
        reason: str = "user_requested_without_merge",
        requested_by_role: str = "unknown",
    ) -> EmergencySessionMetadata:
        metadata = self.read_active_session(session_id)
        if metadata.status == "discarded":
            return metadata
        if metadata.status not in {"active", "merge_failed"}:
            raise EmergencyStoreError(f"Emergency session нельзя завершить без объединения из статуса: {metadata.status}")
        source_dir = active_session_dir(self.root, metadata.emergency_session_id)
        if not os.path.isdir(source_dir):
            raise EmergencyStoreError(f"Active session не найдена: {source_dir}")

        now = _now_text()
        report_path = os.path.join(source_dir, "logs", "emergency_discard_report.json")
        report = {
            "status": "discarded",
            "emergency_session_id": metadata.emergency_session_id,
            "discarded_at": now,
            "reason": str(reason or "user_requested_without_merge"),
            "requested_by_role": str(requested_by_role or "unknown"),
            "source_machine": metadata.source_machine,
            "source_client_id": metadata.source_client_id,
            "network_merge_performed": False,
            "local_emergency_db_preserved": True,
        }
        atomic_write_json(report_path, report)
        updated = replace(
            metadata,
            status="discarded",
            ended_at=now,
            discarded_at=now,
            discard_reason=str(reason or "user_requested_without_merge"),
            discard_report_path=_normalize_path(report_path),
            merge_result="discarded_without_merge",
            last_merge_error=None,
        )
        self.write_active_session(updated)
        return updated

    def archive_discarded_session(self, session_id: str) -> str:
        metadata = self.read_active_session(session_id)
        if metadata.status != "discarded":
            raise EmergencyStoreError(f"Emergency session не помечена как discarded: {metadata.status}")
        source_dir = active_session_dir(self.root, metadata.emergency_session_id)
        target_dir = archived_session_dir(self.root, metadata.emergency_session_id)
        if not os.path.isdir(source_dir):
            raise EmergencyStoreError(f"Active session не найдена: {source_dir}")
        if os.path.exists(target_dir):
            raise EmergencyStoreError(f"Archived session уже существует: {target_dir}")
        updated = replace(
            metadata,
            local_db_path=_map_session_path(metadata.local_db_path, source_dir, target_dir),
            base_snapshot_path=_map_session_path(metadata.base_snapshot_path, source_dir, target_dir),
            settings_snapshot_path=_map_optional_session_path(metadata.settings_snapshot_path, source_dir, target_dir),
            last_dry_run_report_path=_map_optional_session_path(metadata.last_dry_run_report_path, source_dir, target_dir),
            last_merge_report_path=_map_optional_session_path(metadata.last_merge_report_path, source_dir, target_dir),
            local_backup_path=_map_optional_session_path(metadata.local_backup_path, source_dir, target_dir),
            discard_report_path=_map_optional_session_path(metadata.discard_report_path, source_dir, target_dir),
        )
        self.write_active_session(updated)
        os.makedirs(os.path.dirname(target_dir), exist_ok=True)
        shutil.move(source_dir, target_dir)
        return target_dir

    def discard_active_session(
        self,
        session_id: str,
        *,
        reason: str = "user_requested_without_merge",
        requested_by_role: str = "unknown",
    ) -> str:
        self.mark_session_discarded(
            session_id,
            reason=reason,
            requested_by_role=requested_by_role,
        )
        return self.archive_discarded_session(session_id)

    def build_active_runtime_context(self, session_id: str) -> DbRuntimeContext:
        return self._build_session_runtime_context(session_id, mode="emergency", source_label="emergency")

    def build_settings_snapshot_context(self, session_id: str) -> DbRuntimeContext:
        return self._build_session_runtime_context(session_id, mode="snapshot", source_label="settings_snapshot")

    def build_readonly_settings_service_for_session(self, session_id: str):
        from rem_card.services.settings.settings_service import SettingsService

        return SettingsService(context=self.build_settings_snapshot_context(session_id))

    def _build_session_runtime_context(self, session_id: str, *, mode: str, source_label: str) -> DbRuntimeContext:
        session_dir = active_session_dir(self.root, session_id)
        locks_dir = os.path.join(session_dir, "locks")
        backups_dir = os.path.join(session_dir, "backups")
        backup_health_dir = os.path.join(session_dir, "backup_health")
        is_emergency = mode == "emergency"
        is_snapshot = mode == "snapshot"
        return DbRuntimeContext(
            mode=mode,
            medical_db_path=_normalize_path(active_medical_db_path(self.root, session_id)),
            medical_db_lock_path=_normalize_path(os.path.join(locks_dir, "db.lock")),
            medical_backups_valid_dir=_normalize_path(backups_dir),
            medical_backup_health_dir=_normalize_path(backup_health_dir),
            medical_quarantine_dir=_normalize_path(os.path.join(session_dir, "quarantine")),
            medical_snapshots_dir=_normalize_path(os.path.join(session_dir, "snapshots")),
            medical_logs_dir=_normalize_path(os.path.join(session_dir, "logs")),
            recovery_lock_path=_normalize_path(os.path.join(locks_dir, "recovery.lock")),
            session_locks_dir=_normalize_path(os.path.join(locks_dir, "session_locks")),
            settings_db_path=_normalize_path(active_settings_snapshot_path(self.root, session_id)),
            settings_db_lock_path=_normalize_path(os.path.join(locks_dir, "settings.db.lock")),
            settings_backups_dir=_normalize_path(os.path.join(backups_dir, "settings")),
            settings_backup_health_dir=_normalize_path(os.path.join(backup_health_dir, "settings")),
            settings_readonly=True,
            source_label=source_label,
            is_network=False,
            is_emergency=is_emergency,
            is_snapshot=is_snapshot,
            baza_dir=_normalize_path(session_dir),
            medical_backups_root_dir=_normalize_path(backups_dir),
            medical_invalid_backups_dir=_normalize_path(os.path.join(backup_health_dir, "invalid_backups")),
            medical_db_rotation_lock_path=_normalize_path(os.path.join(locks_dir, "db_rotation.lock")),
            medical_client_policy_path=_normalize_path(os.path.join(session_dir, "config", "client_policy.json")),
            medical_startup_quickcheck_state_path=_normalize_path(
                os.path.join(backup_health_dir, "startup_quick_check_state.json")
            ),
            emergency_session_id=str(session_id),
        )

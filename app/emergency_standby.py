from __future__ import annotations

import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable

from rem_card.app.db_runtime_context import build_network_runtime_context
from rem_card.app.emergency_metadata import (
    EmergencyMetadataError,
    EmergencyStandbyMetadata,
    atomic_write_json,
    metadata_to_dict,
)
from rem_card.app.emergency_paths import (
    resolve_emergency_root,
    standby_dir,
    standby_generation_dir,
    standby_generation_medical_db_path,
    standby_generation_metadata_path,
    standby_generation_settings_db_path,
    standby_generations_dir,
    standby_medical_db_path,
    standby_settings_db_path,
)
from rem_card.app.emergency_compatibility import emergency_metadata_compatibility_error, emergency_metadata_compatible
from rem_card.app.emergency_store import EmergencyLocalStore
from rem_card.app.emergency_validation import (
    SnapshotValidationResult,
    validate_medical_db_snapshot,
    validate_settings_db_snapshot,
)
from rem_card.app.local_metrics import record_metric
from rem_card.app.sqlite_shared import backup_connection, configure_connection
from rem_card.data.settings import settings_schema
from rem_card.app.version import APP_VERSION


DEFAULT_STANDBY_MAX_AGE_DAYS = max(1, int(float(os.environ.get("REMCARD_EMERGENCY_STANDBY_MAX_AGE_DAYS", "3"))))


class EmergencyStandbyError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmergencyStandbyRefreshResult:
    ok: bool
    status: str
    reason: str
    metadata: EmergencyStandbyMetadata | None = None
    medical_validation: SnapshotValidationResult | None = None
    settings_validation: SnapshotValidationResult | None = None


def _parse_metadata_time(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def standby_metadata_age_seconds(metadata: EmergencyStandbyMetadata, *, now: datetime | None = None) -> float:
    stamp = _parse_metadata_time(metadata.updated_at) or _parse_metadata_time(metadata.created_at)
    if stamp is None:
        return float("inf")
    current = now
    if current is None:
        current = datetime.now(stamp.tzinfo) if stamp.tzinfo is not None else datetime.now()
    elif stamp.tzinfo is not None and current.tzinfo is None:
        current = current.replace(tzinfo=stamp.tzinfo)
    elif stamp.tzinfo is None and current.tzinfo is not None:
        current = current.replace(tzinfo=None)
    return max(0.0, (current - stamp).total_seconds())


def standby_metadata_expired(
    metadata: EmergencyStandbyMetadata,
    *,
    max_age_days: int | float = DEFAULT_STANDBY_MAX_AGE_DAYS,
    now: datetime | None = None,
) -> bool:
    return standby_metadata_age_seconds(metadata, now=now) > float(max_age_days) * 86400.0


class EmergencyStandbyManager:
    def __init__(
        self,
        root: str | None = None,
        *,
        source_medical_db_path: str | None = None,
        source_settings_db_path: str | None = None,
        settings_required: bool = True,
        is_safe_to_refresh: Callable[[], bool] | None = None,
        store: EmergencyLocalStore | None = None,
    ):
        network_context = build_network_runtime_context()
        self.root = resolve_emergency_root(root)
        self.source_medical_db_path = os.path.abspath(
            os.path.normpath(source_medical_db_path or network_context.medical_db_path)
        )
        self.source_settings_db_path = os.path.abspath(
            os.path.normpath(source_settings_db_path or network_context.settings_db_path)
        )
        self.settings_required = bool(settings_required)
        self.is_safe_to_refresh = is_safe_to_refresh or (lambda: True)
        self.store = store or EmergencyLocalStore(root=self.root, settings_required=self.settings_required)

    def check_network_sources(self) -> EmergencyStandbyRefreshResult:
        medical_validation = validate_medical_db_snapshot(self.source_medical_db_path)
        if not medical_validation.ok:
            return EmergencyStandbyRefreshResult(
                ok=False,
                status="source_unavailable",
                reason=f"medical source is not ready: {medical_validation.reason}",
                medical_validation=medical_validation,
            )

        settings_validation = None
        if self.settings_required:
            settings_validation = validate_settings_db_snapshot(self.source_settings_db_path)
            if not settings_validation.ok:
                return EmergencyStandbyRefreshResult(
                    ok=False,
                    status="source_unavailable",
                    reason=f"settings source is not ready: {settings_validation.reason}",
                    medical_validation=medical_validation,
                    settings_validation=settings_validation,
                )

        return EmergencyStandbyRefreshResult(
            ok=True,
            status="ready",
            reason="ok",
            medical_validation=medical_validation,
            settings_validation=settings_validation,
        )

    def create_or_refresh_standby(self, *, forced: bool = False) -> EmergencyStandbyRefreshResult:
        self.store.ensure_root_dirs()
        if not self.is_safe_to_refresh():
            return EmergencyStandbyRefreshResult(ok=False, status="deferred", reason="refresh is not safe now")
        self.cleanup_expired_standby()

        source_status = self.check_network_sources()
        if not source_status.ok:
            return source_status

        remote_last_change_id = int(source_status.medical_validation.last_change_id if source_status.medical_validation else 0)
        settings_fingerprint = (
            None
            if source_status.settings_validation is None
            else dict(source_status.settings_validation.fingerprint)
        )
        if not self.should_refresh_standby(
            remote_last_change_id,
            settings_fingerprint=settings_fingerprint,
            source_schema_version=source_status.medical_validation.schema_version if source_status.medical_validation else None,
            forced=forced,
        ):
            return EmergencyStandbyRefreshResult(
                ok=True,
                status="current",
                reason="standby is already current",
                metadata=self.store.get_latest_valid_standby(),
                medical_validation=source_status.medical_validation,
                settings_validation=source_status.settings_validation,
            )

        return self._refresh_pair(source_status)

    def refresh_medical_standby(self) -> EmergencyStandbyRefreshResult:
        return self.create_or_refresh_standby(forced=True)

    def refresh_settings_standby(self) -> EmergencyStandbyRefreshResult:
        return self.create_or_refresh_standby(forced=True)

    def validate_standby(self) -> EmergencyStandbyRefreshResult:
        try:
            metadata = self.store.read_standby_metadata()
        except EmergencyMetadataError as exc:
            return EmergencyStandbyRefreshResult(ok=False, status="metadata_error", reason=str(exc))
        if standby_metadata_expired(metadata):
            removed = self.store.delete_standby_files(metadata)
            return EmergencyStandbyRefreshResult(
                ok=False,
                status="expired",
                reason=f"standby is older than {DEFAULT_STANDBY_MAX_AGE_DAYS} days; removed_files={removed}",
                metadata=metadata,
            )

        generation_error = self._standby_generation_error(metadata)
        if generation_error:
            return EmergencyStandbyRefreshResult(ok=False, status="invalid", reason=generation_error, metadata=metadata)

        compatibility_error = emergency_metadata_compatibility_error(metadata)
        if compatibility_error:
            return EmergencyStandbyRefreshResult(ok=False, status="invalid", reason=compatibility_error, metadata=metadata)

        medical_validation = validate_medical_db_snapshot(metadata.medical_db_path)
        settings_validation = None
        if self.settings_required:
            if not metadata.settings_db_path:
                return EmergencyStandbyRefreshResult(
                    ok=False,
                    status="invalid",
                    reason="settings standby path is missing",
                    metadata=metadata,
                    medical_validation=medical_validation,
                )
            settings_validation = validate_settings_db_snapshot(metadata.settings_db_path)

        ok = medical_validation.ok and (settings_validation.ok if settings_validation else True)
        hash_error = self._standby_hash_error(metadata, medical_validation, settings_validation)
        if hash_error:
            ok = False
        if metadata.validation_status not in {"ok", "valid"}:
            ok = False
        return EmergencyStandbyRefreshResult(
            ok=ok,
            status="valid" if ok else "invalid",
            reason="ok" if ok else hash_error or metadata.validation_error or medical_validation.reason,
            metadata=metadata,
            medical_validation=medical_validation,
            settings_validation=settings_validation,
        )

    def get_standby_status(self) -> EmergencyStandbyRefreshResult:
        return self.validate_standby()

    def _standby_generation_error(self, metadata: EmergencyStandbyMetadata) -> str:
        generation_id = str(getattr(metadata, "generation_id", "") or "").strip()
        generation_dir_value = str(getattr(metadata, "generation_dir", "") or "").strip()
        if not generation_id and not generation_dir_value:
            return ""
        expected_dir = os.path.abspath(standby_generation_dir(self.root, generation_id)) if generation_id else ""
        actual_dir = os.path.abspath(os.path.normpath(generation_dir_value)) if generation_dir_value else expected_dir
        if not generation_id:
            return "standby generation_id is missing"
        if os.path.normcase(actual_dir) != os.path.normcase(expected_dir):
            return "standby generation_dir mismatch"
        paths = [str(metadata.medical_db_path or "")]
        if self.settings_required:
            paths.append(str(metadata.settings_db_path or ""))
        for path in paths:
            try:
                path_abs = os.path.normcase(os.path.abspath(os.path.normpath(path)))
                root_abs = os.path.normcase(expected_dir)
                if os.path.commonpath([path_abs, root_abs]) != root_abs:
                    return "standby file is outside metadata generation"
            except Exception:
                return "standby generation path is invalid"
        generation_metadata = standby_generation_metadata_path(self.root, generation_id)
        if not os.path.isfile(generation_metadata):
            return "standby generation metadata is missing"
        return ""

    @staticmethod
    def _standby_hash_error(
        metadata: EmergencyStandbyMetadata,
        medical_validation: SnapshotValidationResult,
        settings_validation: SnapshotValidationResult | None,
    ) -> str:
        if medical_validation.ok:
            if metadata.medical_db_hash and metadata.medical_db_hash != medical_validation.file_hash:
                return "medical standby hash mismatch"
            if int(metadata.medical_db_size or 0) != int(medical_validation.file_size or 0):
                return "medical standby size mismatch"
        if settings_validation and settings_validation.ok:
            if metadata.settings_db_hash and metadata.settings_db_hash != settings_validation.file_hash:
                return "settings standby hash mismatch"
            if metadata.settings_db_size is not None and int(metadata.settings_db_size or 0) != int(settings_validation.file_size or 0):
                return "settings standby size mismatch"
        return ""

    def should_refresh_standby(
        self,
        current_remote_last_change_id: int,
        *,
        settings_fingerprint: dict | None = None,
        source_schema_version: int | None = None,
        forced: bool = False,
    ) -> bool:
        if forced:
            return True
        if not self.is_safe_to_refresh():
            return False
        metadata = self.store.get_latest_valid_standby()
        if metadata is None:
            return True
        if standby_metadata_expired(metadata):
            return True
        if int(current_remote_last_change_id or 0) > int(metadata.remote_last_change_id or 0):
            return True
        if source_schema_version is not None and int(source_schema_version or 0) != int(metadata.schema_version or 0):
            return True
        if not emergency_metadata_compatible(metadata):
            return True
        if settings_fingerprint is not None and dict(settings_fingerprint) != dict(metadata.source_settings_fingerprint or {}):
            return True
        return False

    def mark_standby_invalid(self, reason: str) -> EmergencyStandbyRefreshResult:
        try:
            metadata = self.store.read_standby_metadata()
        except EmergencyMetadataError as exc:
            return EmergencyStandbyRefreshResult(ok=False, status="metadata_error", reason=str(exc))
        invalid = replace(metadata, validation_status="invalid", validation_error=str(reason or "invalid"))
        self.store.write_standby_metadata(invalid)
        return EmergencyStandbyRefreshResult(ok=False, status="invalid", reason=str(reason or "invalid"), metadata=invalid)

    def cleanup_failed_temp_files(self) -> int:
        cleanup_count = 0
        directory = standby_dir(self.root)
        if not os.path.isdir(directory):
            return 0
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            try:
                if name.startswith(".staging.") and os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                    cleanup_count += 1
                elif ".tmp." in name and name.endswith(".db"):
                    os.remove(path)
                    cleanup_count += 1
            except OSError:
                pass
        return cleanup_count

    def cleanup_expired_standby(self) -> int:
        try:
            metadata = self.store.read_standby_metadata()
        except EmergencyMetadataError:
            return 0
        if not standby_metadata_expired(metadata):
            return 0
        return self.store.delete_standby_files(metadata)

    def _refresh_pair(self, source_status: EmergencyStandbyRefreshResult) -> EmergencyStandbyRefreshResult:
        generation_id = self._new_generation_id()
        staging_dir = self._new_staging_dir()
        final_generation_dir = standby_generation_dir(self.root, generation_id)
        staging_medical_path = os.path.join(staging_dir, os.path.basename(standby_medical_db_path(self.root)))
        staging_settings_path = (
            os.path.join(staging_dir, os.path.basename(standby_settings_db_path(self.root))) if self.settings_required else None
        )
        final_medical_path = standby_generation_medical_db_path(self.root, generation_id)
        final_settings_path = standby_generation_settings_db_path(self.root, generation_id) if self.settings_required else None
        committed = False
        generation_moved = False
        try:
            os.makedirs(staging_dir, exist_ok=False)
            self._backup_sqlite_to_temp(self.source_medical_db_path, staging_medical_path, source="emergency_medical_standby")
            medical_validation = validate_medical_db_snapshot(staging_medical_path)
            if not medical_validation.ok:
                return EmergencyStandbyRefreshResult(
                    ok=False,
                    status="validation_failed",
                    reason=f"medical temp validation failed: {medical_validation.reason}",
                    medical_validation=medical_validation,
                    settings_validation=source_status.settings_validation,
                )

            settings_validation = None
            if self.settings_required:
                record_metric(
                    "emergency_settings_snapshot_rebuild_started",
                    1,
                    reason="standby_refresh",
                    expected_schema_version=settings_schema.SCHEMA_VERSION,
                    source_settings_db_path=self.source_settings_db_path,
                    target_settings_db_path=str(staging_settings_path),
                    generation_id=generation_id,
                )
                self._backup_sqlite_to_temp(
                    self.source_settings_db_path,
                    str(staging_settings_path),
                    source="emergency_settings_standby",
                )
                settings_validation = validate_settings_db_snapshot(str(staging_settings_path))
                if not settings_validation.ok:
                    record_metric(
                        "emergency_settings_snapshot_rebuild_failed",
                        1,
                        reason="standby_refresh_validation_failed",
                        expected_schema_version=settings_schema.SCHEMA_VERSION,
                        actual_schema_version=settings_validation.schema_version,
                        detail=settings_validation.reason,
                        generation_id=generation_id,
                    )
                    return EmergencyStandbyRefreshResult(
                        ok=False,
                        status="validation_failed",
                        reason=f"settings temp validation failed: {settings_validation.reason}",
                        medical_validation=medical_validation,
                        settings_validation=settings_validation,
                    )

            metadata = self._build_success_metadata(
                medical_validation=medical_validation,
                settings_validation=settings_validation,
                source_medical_validation=source_status.medical_validation,
                source_settings_validation=source_status.settings_validation,
                medical_db_path=final_medical_path,
                settings_db_path=final_settings_path,
                generation_id=generation_id,
                generation_dir=final_generation_dir,
            )
            atomic_write_json(os.path.join(staging_dir, os.path.basename(standby_generation_metadata_path(self.root, generation_id))), metadata_to_dict(metadata))
            os.makedirs(standby_generations_dir(self.root), exist_ok=True)
            os.replace(staging_dir, final_generation_dir)
            generation_moved = True

            final_medical_validation = validate_medical_db_snapshot(final_medical_path)
            final_settings_validation = validate_settings_db_snapshot(final_settings_path) if final_settings_path else None
            final_error = self._final_generation_error(metadata, final_medical_validation, final_settings_validation)
            if final_error:
                if self.settings_required:
                    record_metric(
                        "emergency_settings_snapshot_rebuild_failed",
                        1,
                        reason="standby_refresh_final_validation_failed",
                        expected_schema_version=settings_schema.SCHEMA_VERSION,
                        actual_schema_version=0 if final_settings_validation is None else final_settings_validation.schema_version,
                        detail=final_error,
                        generation_id=generation_id,
                    )
                return EmergencyStandbyRefreshResult(
                    ok=False,
                    status="validation_failed",
                    reason=final_error,
                    medical_validation=final_medical_validation,
                    settings_validation=final_settings_validation,
                )

            self.store.write_standby_metadata(metadata)
            committed = True
            if self.settings_required and final_settings_validation is not None:
                record_metric(
                    "emergency_settings_snapshot_rebuild_finished",
                    1,
                    reason="standby_refresh",
                    expected_schema_version=settings_schema.SCHEMA_VERSION,
                    actual_schema_version=final_settings_validation.schema_version,
                    settings_db_hash=final_settings_validation.file_hash,
                    generation_id=generation_id,
                )
            return EmergencyStandbyRefreshResult(
                ok=True,
                status="valid",
                reason="ok",
                metadata=metadata,
                medical_validation=final_medical_validation,
                settings_validation=final_settings_validation,
            )
        except Exception as exc:
            if self.settings_required:
                record_metric(
                    "emergency_settings_snapshot_rebuild_failed",
                    1,
                    reason="standby_refresh_exception",
                    expected_schema_version=settings_schema.SCHEMA_VERSION,
                    detail=str(exc),
                    generation_id=generation_id,
                )
            return EmergencyStandbyRefreshResult(ok=False, status="error", reason=str(exc))
        finally:
            if os.path.isdir(staging_dir):
                shutil.rmtree(staging_dir, ignore_errors=True)
            if generation_moved and not committed and os.path.isdir(final_generation_dir):
                shutil.rmtree(final_generation_dir, ignore_errors=True)

    def _backup_sqlite_to_temp(self, source_path: str, temp_target_path: str, *, source: str) -> str:
        os.makedirs(os.path.dirname(temp_target_path), exist_ok=True)
        conn = None
        try:
            conn = sqlite3.connect(
                f"file:{os.path.abspath(source_path)}?mode=ro",
                uri=True,
                check_same_thread=False,
                isolation_level=None,
                timeout=5.0,
            )
            configure_connection(conn, readonly=True, profile="network")
            return backup_connection(
                conn,
                temp_target_path,
                validate=False,
                lock_path=None,
                source=source,
            )
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _new_generation_id(self) -> str:
        return f"gen_{uuid.uuid4().hex[:16]}"

    def _new_staging_dir(self) -> str:
        return os.path.join(standby_dir(self.root), f".staging.{uuid.uuid4().hex[:16]}")

    def _final_generation_error(
        self,
        metadata: EmergencyStandbyMetadata,
        medical_validation: SnapshotValidationResult,
        settings_validation: SnapshotValidationResult | None,
    ) -> str:
        if not medical_validation.ok:
            return f"final medical standby validation failed: {medical_validation.reason}"
        if settings_validation and not settings_validation.ok:
            return f"final settings standby validation failed: {settings_validation.reason}"
        return self._standby_hash_error(metadata, medical_validation, settings_validation)

    def _build_success_metadata(
        self,
        *,
        medical_validation: SnapshotValidationResult,
        settings_validation: SnapshotValidationResult | None,
        source_medical_validation: SnapshotValidationResult | None,
        source_settings_validation: SnapshotValidationResult | None,
        medical_db_path: str,
        settings_db_path: str | None,
        generation_id: str = "",
        generation_dir: str = "",
    ) -> EmergencyStandbyMetadata:
        now = datetime.now().replace(microsecond=0).isoformat()
        existing = self.store.get_latest_valid_standby()
        source_medical = source_medical_validation or medical_validation
        source_settings = source_settings_validation or settings_validation
        return EmergencyStandbyMetadata(
            standby_id=existing.standby_id if existing else f"standby_{uuid.uuid4().hex}",
            created_at=existing.created_at if existing else now,
            updated_at=now,
            source_remote_db_path=self.source_medical_db_path,
            source_remote_fingerprint=dict(source_medical.fingerprint),
            source_settings_db_path=self.source_settings_db_path if self.settings_required else None,
            source_settings_fingerprint=None if source_settings is None else dict(source_settings.fingerprint),
            remote_last_change_id=int(source_medical.last_change_id or medical_validation.last_change_id or 0),
            schema_version=int(source_medical.schema_version or medical_validation.schema_version or 0),
            app_version=APP_VERSION,
            medical_db_path=os.path.abspath(medical_db_path),
            medical_db_hash=medical_validation.file_hash,
            medical_db_size=medical_validation.file_size,
            medical_db_mtime=medical_validation.file_mtime,
            settings_db_path=None if settings_db_path is None else os.path.abspath(settings_db_path),
            settings_db_hash=None if settings_validation is None else settings_validation.file_hash,
            settings_db_size=None if settings_validation is None else settings_validation.file_size,
            settings_db_mtime=None if settings_validation is None else settings_validation.file_mtime,
            quick_check_status=medical_validation.reason,
            settings_quick_check_status=None if settings_validation is None else settings_validation.reason,
            validation_status="valid",
            validation_error=None,
            metadata_schema_version=1,
            generation_id=str(generation_id or ""),
            generation_dir="" if not generation_dir else os.path.abspath(generation_dir),
        )

    @staticmethod
    def _temp_standby_path(final_path: str) -> str:
        directory = os.path.dirname(final_path)
        base_name = os.path.basename(final_path)
        stem, ext = os.path.splitext(base_name)
        return os.path.join(directory, f"{stem}.tmp.{os.getpid()}{ext or '.db'}")

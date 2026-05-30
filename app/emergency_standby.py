from __future__ import annotations

import os
import sqlite3
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable

from rem_card.app.db_runtime_context import build_network_runtime_context
from rem_card.app.emergency_metadata import (
    EmergencyMetadataError,
    EmergencyStandbyMetadata,
)
from rem_card.app.emergency_paths import (
    resolve_emergency_root,
    standby_dir,
    standby_medical_db_path,
    standby_settings_db_path,
)
from rem_card.app.emergency_store import EmergencyLocalStore
from rem_card.app.emergency_validation import (
    SnapshotValidationResult,
    validate_medical_db_snapshot,
    validate_settings_db_snapshot,
)
from rem_card.app.sqlite_shared import backup_connection, configure_connection
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
        if metadata.validation_status not in {"ok", "valid"}:
            ok = False
        return EmergencyStandbyRefreshResult(
            ok=ok,
            status="valid" if ok else "invalid",
            reason="ok" if ok else metadata.validation_error or medical_validation.reason,
            metadata=metadata,
            medical_validation=medical_validation,
            settings_validation=settings_validation,
        )

    def get_standby_status(self) -> EmergencyStandbyRefreshResult:
        return self.validate_standby()

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
        if str(metadata.app_version or "") != str(APP_VERSION):
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
            if ".tmp." not in name or not name.endswith(".db"):
                continue
            path = os.path.join(directory, name)
            try:
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
        final_medical_path = standby_medical_db_path(self.root)
        final_settings_path = standby_settings_db_path(self.root) if self.settings_required else None
        temp_medical_path = self._temp_standby_path(final_medical_path)
        temp_settings_path = self._temp_standby_path(final_settings_path) if final_settings_path else None
        temp_paths = [path for path in (temp_medical_path, temp_settings_path) if path]

        try:
            self._backup_sqlite_to_temp(self.source_medical_db_path, temp_medical_path, source="emergency_medical_standby")
            medical_validation = validate_medical_db_snapshot(temp_medical_path)
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
                self._backup_sqlite_to_temp(
                    self.source_settings_db_path,
                    str(temp_settings_path),
                    source="emergency_settings_standby",
                )
                settings_validation = validate_settings_db_snapshot(str(temp_settings_path))
                if not settings_validation.ok:
                    return EmergencyStandbyRefreshResult(
                        ok=False,
                        status="validation_failed",
                        reason=f"settings temp validation failed: {settings_validation.reason}",
                        medical_validation=medical_validation,
                        settings_validation=settings_validation,
                    )

            os.replace(temp_medical_path, final_medical_path)
            if temp_settings_path and final_settings_path:
                os.replace(temp_settings_path, final_settings_path)

            final_medical_validation = validate_medical_db_snapshot(final_medical_path)
            final_settings_validation = (
                validate_settings_db_snapshot(final_settings_path) if final_settings_path else None
            )
            if not final_medical_validation.ok or (final_settings_validation and not final_settings_validation.ok):
                return EmergencyStandbyRefreshResult(
                    ok=False,
                    status="validation_failed",
                    reason="final standby validation failed",
                    medical_validation=final_medical_validation,
                    settings_validation=final_settings_validation,
                )

            metadata = self._build_success_metadata(
                medical_validation=final_medical_validation,
                settings_validation=final_settings_validation,
                source_medical_validation=source_status.medical_validation,
                source_settings_validation=source_status.settings_validation,
                medical_db_path=final_medical_path,
                settings_db_path=final_settings_path,
            )
            self.store.write_standby_metadata(metadata)
            return EmergencyStandbyRefreshResult(
                ok=True,
                status="valid",
                reason="ok",
                metadata=metadata,
                medical_validation=final_medical_validation,
                settings_validation=final_settings_validation,
            )
        except Exception as exc:
            return EmergencyStandbyRefreshResult(ok=False, status="error", reason=str(exc))
        finally:
            for path in temp_paths:
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass

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

    def _build_success_metadata(
        self,
        *,
        medical_validation: SnapshotValidationResult,
        settings_validation: SnapshotValidationResult | None,
        source_medical_validation: SnapshotValidationResult | None,
        source_settings_validation: SnapshotValidationResult | None,
        medical_db_path: str,
        settings_db_path: str | None,
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
        )

    @staticmethod
    def _temp_standby_path(final_path: str) -> str:
        directory = os.path.dirname(final_path)
        base_name = os.path.basename(final_path)
        stem, ext = os.path.splitext(base_name)
        return os.path.join(directory, f"{stem}.tmp.{os.getpid()}{ext or '.db'}")

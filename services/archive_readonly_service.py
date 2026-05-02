from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Callable, Optional, Sequence

from rem_card.app.sqlite_shared import configure_connection
from rem_card.data.dao.patient_status_dao import PatientStatusDAO
from rem_card.data.dao.remcard_dao import FluidsDAO, OrdersDAO, PatientDAO, VitalsDAO
from rem_card.services.patient_status_service import PatientStatusService
from rem_card.services.remcard_service import RemCardService


GLOBAL_CHANGELOG_ENTITIES = ("patients", "admissions", "beds", "operations")


class ReadOnlyArchiveDbError(RuntimeError):
    pass


class ArchiveReadOnlyDatabaseManager:
    """
    Минимальный DB-manager для чтения архивного sqlite-файла в режиме mode=ro.
    Интерфейс совместим с DAO-слоем RemCardService.
    """

    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(str(db_path or ""))
        if not self.db_path:
            raise ValueError("Archive DB path is empty")
        if not os.path.isfile(self.db_path):
            raise FileNotFoundError(self.db_path)

        uri = f"file:{self.db_path}?mode=ro"
        self._conn = sqlite3.connect(
            uri,
            uri=True,
            check_same_thread=False,
            isolation_level=None,
            timeout=5.0,
        )
        configure_connection(self._conn, readonly=True)

    def _has_table(self, table_name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ? LIMIT 1",
            (table_name,),
        ).fetchone()
        return bool(row)

    def fetch_all_remcard(self, query: str, params: Sequence = ()):
        cursor = self._conn.cursor()
        cursor.execute(query, tuple(params or ()))
        return cursor.fetchall()

    def fetch_one_remcard(self, query: str, params: Sequence = ()):
        cursor = self._conn.cursor()
        cursor.execute(query, tuple(params or ()))
        return cursor.fetchone()

    def fetch_all_journal(self, query: str, params: Sequence = ()):
        """Compatibility alias for legacy journal callers."""
        return self.fetch_all_remcard(query, params)

    def fetch_one_journal(self, query: str, params: Sequence = ()):
        """Compatibility alias for legacy journal callers."""
        return self.fetch_one_remcard(query, params)

    def get_data_version(self) -> int:
        row = self.fetch_one_remcard("SELECT value FROM meta WHERE key = 'app_data_version'")
        return int(row[0]) if row and row[0] is not None else 0

    def get_latest_change_id(self, admission_id: Optional[int] = None, include_global: bool = True) -> int:
        if not self._has_table("change_log"):
            return 0

        query = "SELECT COALESCE(MAX(id), 0) FROM change_log"
        params: list[object] = []
        if admission_id is not None:
            if include_global:
                placeholders = ",".join("?" for _ in GLOBAL_CHANGELOG_ENTITIES)
                query += f" WHERE admission_id = ? OR entity_name IN ({placeholders})"
                params.extend([admission_id, *GLOBAL_CHANGELOG_ENTITIES])
            else:
                query += " WHERE admission_id = ?"
                params.append(admission_id)

        row = self.fetch_one_remcard(query, tuple(params))
        return int(row[0]) if row and row[0] is not None else 0

    def fetch_changes_since(self, last_change_id: int, admission_id: Optional[int] = None, include_global: bool = True):
        if not self._has_table("change_log"):
            return []

        query = "SELECT * FROM change_log WHERE id > ?"
        params: list[object] = [last_change_id]
        if admission_id is not None:
            if include_global:
                placeholders = ",".join("?" for _ in GLOBAL_CHANGELOG_ENTITIES)
                query += f" AND (admission_id = ? OR entity_name IN ({placeholders}))"
                params.extend([admission_id, *GLOBAL_CHANGELOG_ENTITIES])
            else:
                query += " AND admission_id = ?"
                params.append(admission_id)
        query += " ORDER BY id ASC"
        return self.fetch_all_remcard(query, tuple(params))

    def get_changed_entities_since(
        self,
        last_change_id: int,
        admission_id: Optional[int] = None,
        include_global: bool = True,
    ) -> set[str]:
        if not self._has_table("change_log"):
            return set()

        query = "SELECT DISTINCT entity_name FROM change_log WHERE id > ?"
        params: list[object] = [last_change_id]
        if admission_id is not None:
            if include_global:
                placeholders = ",".join("?" for _ in GLOBAL_CHANGELOG_ENTITIES)
                query += f" AND (admission_id = ? OR entity_name IN ({placeholders}))"
                params.extend([admission_id, *GLOBAL_CHANGELOG_ENTITIES])
            else:
                query += " AND admission_id = ?"
                params.append(admission_id)

        rows = self.fetch_all_remcard(query, tuple(params))
        return {str(row[0]) for row in rows if row and row[0] is not None}

    @contextmanager
    def remcard_transaction(self, source: str = "archive_readonly_tx"):
        raise ReadOnlyArchiveDbError("Archive DB is opened in read-only mode")
        yield

    def run_write_operation(self, operation: Callable, source: str = "archive_readonly_write"):
        raise ReadOnlyArchiveDbError("Archive DB is opened in read-only mode")

    def execute_remcard(self, query, params=(), source: str = "archive_readonly_execute"):
        raise ReadOnlyArchiveDbError("Archive DB is opened in read-only mode")

    def close(self):
        if self._conn:
            self._conn.close()


def create_archive_readonly_service(db_path: str) -> tuple[RemCardService, ArchiveReadOnlyDatabaseManager]:
    from rem_card.services.read_coordinator import ReadCoordinator

    db_manager = ArchiveReadOnlyDatabaseManager(db_path)

    patient_dao = PatientDAO(db_manager)
    vitals_dao = VitalsDAO(db_manager)
    fluids_dao = FluidsDAO(db_manager)
    orders_dao = OrdersDAO(db_manager)
    status_dao = PatientStatusDAO(db_manager)
    status_service = PatientStatusService(status_dao, data_service=None, ventilation_dao=None)

    service = RemCardService(
        vitals_dao=vitals_dao,
        fluids_dao=fluids_dao,
        orders_dao=orders_dao,
        ventilation_dao=None,
        patient_dao=patient_dao,
        status_service=status_service,
        data_service=None,
    )
    service.status_service = status_service
    service.read_coordinator = ReadCoordinator(service)
    service.read_mode = "archive"
    service.source_db_path = db_manager.db_path
    return service, db_manager

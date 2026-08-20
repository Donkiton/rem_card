import os
import sqlite3
from contextlib import nullcontext
from typing import List, Optional
from datetime import datetime, timedelta
from ..dto.remcard_dto import PatientDTO
from .patient_status_dao import PatientStatusDAO
from ..dto.remcard_dto import PatientStatus
from rem_card.app.patient_age import parse_date_value
from ...app.logger import logger
from ...app.db_cycle_registry import discover_db_cycle_paths, select_db_paths_for_period
from ...app.archive_schema_cache import get_archive_schema
from ...app.sqlite_uri import build_sqlite_file_uri
from ...app.sqlite_shared import configure_connection

class PatientDAO:
    def __init__(self, db_manager):
        self.db = db_manager
        self.status_dao = PatientStatusDAO(db_manager)

    def sync_from_journal(self):
        """Compatibility no-op after journal -> remcard migration."""
        logger.debug("PatientDAO.sync_from_journal skipped: unified DB mode is active.")
        return None

    def get_active_patients(self) -> List[PatientDTO]:
        query = """
            SELECT 
                a.id as admission_id, p.last_name, p.first_name, p.middle_name, p.full_name, p.birth_date,
                a.history_number, a.bed_number, a.admission_datetime, COALESCE(a.transfer_datetime, a.death_datetime) as transfer_datetime,
                a.diagnosis_text, a.patient_age, a.patient_months, a.patient_age_unit, a.patient_gender,
                a.diagnosis_code as mkb_code,
                a.emergency_notice_number,
                a.emergency_notice_entered_at,
                COALESCE(
                    a.operation_description,
                    (SELECT o.description FROM operations o WHERE o.admission_id = a.id ORDER BY DATETIME(o.operation_datetime) DESC, o.id DESC LIMIT 1)
                ) as operation_info
            FROM admissions a
            JOIN patients p ON a.patient_id = p.id
            JOIN beds b ON a.id = b.current_admission_id
            WHERE b.status = 'OCCUPIED'
        """
        rows = self.db.fetch_all_remcard(query)
        return self._map_patients(rows)

    def get_active_patients_by_ids(self, admission_ids: List[int]) -> List[PatientDTO]:
        ids = [int(admission_id) for admission_id in (admission_ids or []) if admission_id is not None]
        if not ids:
            return []
        placeholders = ", ".join("?" for _ in ids)
        query = f"""
            SELECT
                a.id as admission_id, p.last_name, p.first_name, p.middle_name, p.full_name, p.birth_date,
                a.history_number, a.bed_number, a.admission_datetime, COALESCE(a.transfer_datetime, a.death_datetime) as transfer_datetime,
                a.diagnosis_text, a.patient_age, a.patient_months, a.patient_age_unit, a.patient_gender,
                a.diagnosis_code as mkb_code,
                a.emergency_notice_number,
                a.emergency_notice_entered_at,
                COALESCE(
                    a.operation_description,
                    (SELECT o.description FROM operations o WHERE o.admission_id = a.id ORDER BY DATETIME(o.operation_datetime) DESC, o.id DESC LIMIT 1)
                ) as operation_info
            FROM admissions a
            JOIN patients p ON a.patient_id = p.id
            JOIN beds b ON a.id = b.current_admission_id
            WHERE b.status = 'OCCUPIED'
              AND a.id IN ({placeholders})
            ORDER BY b.bed_number
        """
        rows = self.db.fetch_all_remcard(query, tuple(ids))
        return self._map_patients(rows)

    def get_archived_patients(self, start_dt: str | None = None, end_dt: str | None = None) -> List[PatientDTO]:
        current_db_path = os.path.abspath(str(getattr(self.db, "db_path", "") or ""))
        rows: list[dict] = []
        if start_dt and end_dt:
            db_paths = self.get_archive_db_paths_for_period(start_dt, end_dt)
        else:
            db_paths = self._iter_archived_db_paths(current_db_path, include_current=True)
        current_key = os.path.normcase(current_db_path)

        for archived_db_path in db_paths:
            try:
                abs_path = os.path.abspath(archived_db_path)
                if current_key and os.path.normcase(abs_path) == current_key:
                    tables = self._get_current_table_names()
                    query, params = self._build_archived_patients_query(
                        patient_columns=self._get_current_table_columns("patients"),
                        admission_columns=self._get_current_table_columns("admissions"),
                        has_operations_table="operations" in tables,
                        has_operation_cases_table="operation_cases" in tables,
                        start_dt=start_dt,
                        end_dt=end_dt,
                    )
                    archived_rows = [
                        dict(row)
                        for row in self.db.fetch_all_remcard(query, params)
                    ]
                    is_external = False
                else:
                    archived_rows = self._fetch_archived_rows_from_db(abs_path, start_dt=start_dt, end_dt=end_dt)
                    is_external = True
                for data in archived_rows:
                    data["source_db_path"] = abs_path
                    data["source_db_name"] = os.path.basename(archived_db_path)
                    data["source_admission_id"] = data.get("admission_id")
                    data["is_external_archive"] = is_external
                    rows.append(data)
            except Exception as exc:
                logger.warning("Skipping archived DB %s due to read error: %s", archived_db_path, exc)

        patients = self._map_patients(rows)
        patients.sort(key=self._archived_patient_sort_key, reverse=True)
        return patients

    def get_archived_patients_page(
        self,
        *,
        start_dt: str | None = None,
        end_dt: str | None = None,
        page: int = 1,
        page_size: int = 50,
        search_name: str = "",
        search_ib: str = "",
        search_diag: str = "",
    ) -> dict:
        page_size = max(1, int(page_size or 50))
        page = max(1, int(page or 1))
        offset = (page - 1) * page_size
        fetch_limit = offset + page_size

        current_db_path = os.path.abspath(str(getattr(self.db, "db_path", "") or ""))
        # The page queries already apply the period.  Preselecting through
        # select_db_paths_for_period would open and aggregate every archive
        # first, then open matching files again for count+rows.
        db_paths = self._iter_archived_db_paths(current_db_path, include_current=True)
        current_key = os.path.normcase(current_db_path)
        use_global_merge = len(db_paths) > 1

        rows: list[dict] = []
        total_count = 0
        for archived_db_path in db_paths:
            try:
                abs_path = os.path.abspath(archived_db_path)
                is_current = bool(current_key) and os.path.normcase(abs_path) == current_key
                if is_current:
                    scope_factory = getattr(self.db, "central_read_snapshot_scope", None)
                    if not callable(scope_factory):
                        scope_factory = getattr(self.db, "central_read_scope", None)
                    read_scope = (
                        scope_factory("patient_archive_page")
                        if callable(scope_factory)
                        else nullcontext()
                    )
                    with read_scope:
                        tables = self._get_current_table_names()
                        patient_columns = self._get_current_table_columns("patients")
                        admission_columns = self._get_current_table_columns("admissions")
                        count_query, count_params = self._build_archived_patients_query(
                            patient_columns=patient_columns,
                            admission_columns=admission_columns,
                            has_operations_table="operations" in tables,
                            has_operation_cases_table="operation_cases" in tables,
                            start_dt=start_dt,
                            end_dt=end_dt,
                            search_name=search_name,
                            search_ib=search_ib,
                            search_diag=search_diag,
                            count_only=True,
                            end_exclusive=True,
                        )
                        total_count += self._fetch_count_from_manager(count_query, count_params)
                        query, params = self._build_archived_patients_query(
                            patient_columns=patient_columns,
                            admission_columns=admission_columns,
                            has_operations_table="operations" in tables,
                            has_operation_cases_table="operation_cases" in tables,
                            start_dt=start_dt,
                            end_dt=end_dt,
                            search_name=search_name,
                            search_ib=search_ib,
                            search_diag=search_diag,
                            limit=fetch_limit if use_global_merge else page_size,
                            offset=0 if use_global_merge else offset,
                            end_exclusive=True,
                        )
                        archived_rows = [dict(row) for row in self.db.fetch_all_remcard(query, params)]
                    is_external = False
                else:
                    archived_count, archived_rows = self._fetch_archived_page_from_db(
                        abs_path,
                        start_dt=start_dt,
                        end_dt=end_dt,
                        search_name=search_name,
                        search_ib=search_ib,
                        search_diag=search_diag,
                        limit=fetch_limit if use_global_merge else page_size,
                        offset=0 if use_global_merge else offset,
                    )
                    total_count += archived_count
                    is_external = True
                for data in archived_rows:
                    data["source_db_path"] = abs_path
                    data["source_db_name"] = os.path.basename(archived_db_path)
                    data["source_admission_id"] = data.get("admission_id")
                    data["is_external_archive"] = is_external
                    rows.append(data)
            except Exception as exc:
                logger.warning("Skipping archived DB %s due to paged read error: %s", archived_db_path, exc)

        patients = self._map_patients(rows)
        patients.sort(key=self._archived_patient_sort_key, reverse=True)
        if use_global_merge:
            patients = patients[offset : offset + page_size]
        return {
            "records": patients,
            "total_count": int(total_count),
            "page": page,
            "page_size": page_size,
        }

    def _fetch_count_from_manager(self, query: str, params: tuple) -> int:
        rows = self.db.fetch_all_remcard(query, params)
        if not rows:
            return 0
        row = rows[0]
        try:
            return int(row["total_count"] or 0)
        except Exception:
            try:
                return int(row[0] or 0)
            except Exception:
                return 0

    @staticmethod
    def _archived_patient_sort_key(patient: PatientDTO) -> tuple:
        source_path = os.path.normcase(
            os.path.abspath(str(getattr(patient, "source_db_path", "") or ""))
        )
        source_id = getattr(patient, "source_admission_id", None)
        if source_id is None:
            source_id = getattr(patient, "id", 0)
        return (
            patient.admission_datetime or datetime.min,
            source_path,
            int(source_id or 0),
        )

    def _get_current_table_names(self) -> set[str]:
        try:
            rows = self.db.fetch_all_remcard("SELECT name FROM sqlite_master WHERE type='table'")
        except Exception:
            return set()
        tables: set[str] = set()
        for row in rows or []:
            try:
                name = row["name"]
            except Exception:
                try:
                    name = row[0]
                except Exception:
                    name = None
            if name:
                tables.add(str(name))
        return tables

    def _get_current_table_columns(self, table_name: str) -> Optional[set[str]]:
        if table_name not in {"patients", "admissions"}:
            return None
        try:
            rows = self.db.fetch_all_remcard(f"PRAGMA table_info({table_name})")
        except Exception:
            return None
        columns: set[str] = set()
        for row in rows or []:
            try:
                name = row["name"]
            except Exception:
                try:
                    name = row[1]
                except Exception:
                    name = None
            if name:
                columns.add(str(name))
        return columns or None

    def get_patient_by_id(self, admission_id: int) -> Optional[PatientDTO]:
        def build_query(*, include_operations: bool = True, include_emergency_notice: bool = True) -> str:
            operation_expr = (
                "COALESCE("
                "a.operation_description,"
                "(SELECT o.description FROM operations o WHERE o.admission_id = a.id ORDER BY DATETIME(o.operation_datetime) DESC, o.id DESC LIMIT 1)"
                ")"
                if include_operations
                else "a.operation_description"
            )
            emergency_number_expr = "a.emergency_notice_number" if include_emergency_notice else "NULL"
            emergency_entered_expr = "a.emergency_notice_entered_at" if include_emergency_notice else "NULL"
            return f"""
            SELECT 
                a.id as admission_id, p.last_name, p.first_name, p.middle_name, p.full_name, p.birth_date,
                a.history_number, a.bed_number, a.admission_datetime, COALESCE(a.transfer_datetime, a.death_datetime) as transfer_datetime,
                a.diagnosis_text, a.patient_age, a.patient_months, a.patient_age_unit, a.patient_gender,
                a.diagnosis_code as mkb_code,
                {emergency_number_expr} as emergency_notice_number,
                {emergency_entered_expr} as emergency_notice_entered_at,
                {operation_expr} as operation_info
            FROM admissions a
            JOIN patients p ON a.patient_id = p.id
            WHERE a.id = ?
            """

        include_operations = True
        include_emergency_notice = True
        for _attempt in range(3):
            try:
                rows = self.db.fetch_all_remcard(
                    build_query(
                        include_operations=include_operations,
                        include_emergency_notice=include_emergency_notice,
                    ),
                    (admission_id,),
                )
                break
            except sqlite3.OperationalError as exc:
                text = str(exc).lower()
                if "no such table: operations" in text and include_operations:
                    include_operations = False
                    continue
                if "emergency_notice_" in text and include_emergency_notice:
                    include_emergency_notice = False
                    continue
                raise
        else:
            rows = []
        if not rows: return None
        patients = self._map_patients(rows)
        return patients[0] if patients else None

    def release_due_outcome_beds(self, delay_minutes: int = 30) -> int:
        """
        Автоматически освобождает койки, если у пациента активный исход (TRANSFERRED/DEAD)
        и с момента исхода прошло не меньше delay_minutes.
        Возвращает количество освобожденных коек.
        """
        delay_minutes = max(0, int(delay_minutes))
        cutoff = (datetime.now() - timedelta(minutes=delay_minutes)).replace(microsecond=0).isoformat()
        now_iso = datetime.now().replace(microsecond=0).isoformat()
        released_counter = {"count": 0}
        due_query = """
            SELECT
                b.bed_number AS bed_number,
                b.current_admission_id AS admission_id,
                pse.status AS status,
                pse.start_time AS outcome_time
            FROM beds b
            JOIN patient_status_events pse
                ON pse.admission_id = b.current_admission_id
               AND pse.end_time IS NULL
            WHERE b.status = 'OCCUPIED'
              AND b.current_admission_id IS NOT NULL
              AND pse.status IN (?, ?)
              AND DATETIME(pse.start_time) <= DATETIME(?)
        """

        # Легкий read-only precheck, чтобы не открывать write-транзакцию без необходимости.
        due_rows = self.db.fetch_all_remcard(
            due_query,
            (PatientStatus.TRANSFERRED.value, PatientStatus.DEAD.value, cutoff),
        )
        if not due_rows:
            return 0

        def operation(cursor):
            cursor.execute(due_query, (PatientStatus.TRANSFERRED.value, PatientStatus.DEAD.value, cutoff))
            due_rows = cursor.fetchall()

            for row in due_rows:
                bed_number = int(row["bed_number"])
                admission_id = int(row["admission_id"])
                status = row["status"]
                outcome_time = row["outcome_time"]
                outcome_value = "переведен" if status == PatientStatus.TRANSFERRED.value else "умер"

                cursor.execute(
                    """
                    UPDATE beds
                    SET current_admission_id = NULL,
                        status = 'FREE'
                    WHERE bed_number = ?
                      AND current_admission_id = ?
                      AND status = 'OCCUPIED'
                    """,
                    (bed_number, admission_id),
                )
                if cursor.rowcount != 1:
                    continue

                if status == PatientStatus.TRANSFERRED.value:
                    cursor.execute(
                        """
                        UPDATE admissions
                        SET is_active = 0,
                            outcome = COALESCE(outcome, ?),
                            transfer_datetime = COALESCE(transfer_datetime, ?),
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (outcome_value, outcome_time, now_iso, admission_id),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE admissions
                        SET is_active = 0,
                            outcome = COALESCE(outcome, ?),
                            death_datetime = COALESCE(death_datetime, ?),
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (outcome_value, outcome_time, now_iso, admission_id),
                    )

                released_counter["count"] += 1

            return released_counter["count"]

        self.db.run_write_operation(operation, source="auto_release_outcome_beds")
        return released_counter["count"]

    def delete_admission(self, admission_id: int):
        """Удаляет госпитализацию вместе со связанными данными в unified DB."""
        logger.info(f"Deleting admission {admission_id}...")
        self.db.run_write_operation(
            lambda cursor: self._delete_admission_with_cursor(cursor, admission_id),
            source="delete_admission",
        )

    def delete_patient(self, patient_id: int):
        """Удаляет пациента и все его госпитализации в unified DB."""
        logger.info(f"Deleting patient {patient_id} and all related data...")
        def operation(cursor):
            cursor.execute("SELECT id FROM admissions WHERE patient_id = ?", (patient_id,))
            admission_rows = cursor.fetchall()
            for row in admission_rows:
                self._delete_admission_with_cursor(cursor, row["id"])
            cursor.execute("DELETE FROM patients WHERE id = ?", (patient_id,))

        self.db.run_write_operation(operation, source="delete_patient")

    def _delete_admission_with_cursor(self, cursor, admission_id: int):
        """
        Низкоуровневое удаление госпитализации в рамках текущей транзакции.
        Важно выполнять все шаги одной транзакцией, чтобы исключить частичное удаление.
        """
        cursor.execute("UPDATE beds SET current_admission_id = NULL, status = 'FREE' WHERE current_admission_id = ?", (admission_id,))
        cursor.execute("DELETE FROM administrations WHERE order_id IN (SELECT id FROM orders WHERE admission_id = ?)", (admission_id,))
        cursor.execute("DELETE FROM order_audit_log WHERE admission_id = ?", (admission_id,))
        cursor.execute("DELETE FROM patient_status_events WHERE admission_id = ?", (admission_id,))
        cursor.execute("DELETE FROM vital_settings WHERE admission_id = ?", (admission_id,))
        cursor.execute("DELETE FROM vitals WHERE admission_id = ?", (admission_id,))
        cursor.execute("DELETE FROM fluids WHERE admission_id = ?", (admission_id,))
        cursor.execute("DELETE FROM orders WHERE admission_id = ?", (admission_id,))
        cursor.execute("DELETE FROM operations WHERE admission_id = ?", (admission_id,))
        cursor.execute("DELETE FROM ivl_episodes WHERE admission_id = ?", (admission_id,))
        cursor.execute("DELETE FROM transfusions WHERE admission_id = ?", (admission_id,))
        cursor.execute(
            "DELETE FROM procedure_cvc WHERE procedure_id IN (SELECT id FROM procedures WHERE admission_id = ?)",
            (admission_id,),
        )
        cursor.execute(
            "DELETE FROM procedure_lumbar_puncture WHERE procedure_id IN (SELECT id FROM procedures WHERE admission_id = ?)",
            (admission_id,),
        )
        cursor.execute(
            "DELETE FROM procedure_transfusion WHERE procedure_id IN (SELECT id FROM procedures WHERE admission_id = ?)",
            (admission_id,),
        )
        cursor.execute(
            "DELETE FROM procedure_consents WHERE procedure_id IN (SELECT id FROM procedures WHERE admission_id = ?)",
            (admission_id,),
        )
        cursor.execute("DELETE FROM procedures WHERE admission_id = ?", (admission_id,))
        cursor.execute("DELETE FROM lab_orders WHERE admission_id = ?", (admission_id,))
        cursor.execute("DELETE FROM clinical_events WHERE admission_id = ?", (admission_id,))
        cursor.execute("DELETE FROM devices WHERE admission_id = ?", (admission_id,))
        cursor.execute("DELETE FROM respiratory_support WHERE admission_id = ?", (admission_id,))
        cursor.execute("DELETE FROM lab_data WHERE admission_id = ?", (admission_id,))
        cursor.execute("DELETE FROM admissions WHERE id = ?", (admission_id,))

    def _map_patients(self, rows) -> List[PatientDTO]:
        def _safe_parse_dt(value):
            if value is None or value == "":
                return None
            if isinstance(value, datetime):
                return value
            text = str(value).strip()
            if not text:
                return None
            try:
                return datetime.fromisoformat(text)
            except Exception:
                # На случай нестандартного формата даты просто игнорируем.
                return None

        def _row_get(row, key, default=None):
            if isinstance(row, dict):
                return row.get(key, default)
            try:
                return row[key]
            except Exception:
                return default

        patients = []
        for r in rows:
            admission_id = _row_get(r, "admission_id")
            if admission_id is None:
                continue

            last_name = _row_get(r, "last_name")
            first_name = _row_get(r, "first_name")
            middle_name = _row_get(r, "middle_name")
            full_name = _row_get(r, "full_name")
            if (not last_name and not first_name and not middle_name) and full_name:
                parts = str(full_name).split()
                last_name = parts[0] if len(parts) > 0 else ""
                first_name = parts[1] if len(parts) > 1 else ""
                middle_name = " ".join(parts[2:]) if len(parts) > 2 else ""

            source_db_path = _row_get(r, "source_db_path")
            source_admission_id = _row_get(r, "source_admission_id", admission_id)
            source_db_name = _row_get(r, "source_db_name") or (os.path.basename(source_db_path) if source_db_path else None)

            patients.append(PatientDTO(
                id=int(admission_id),
                last_name=last_name,
                first_name=first_name,
                middle_name=middle_name,
                history_number=_row_get(r, "history_number") or "",
                bed_number=_row_get(r, "bed_number"),
                admission_datetime=_safe_parse_dt(_row_get(r, "admission_datetime")),
                transfer_datetime=_safe_parse_dt(_row_get(r, "transfer_datetime")),
                diagnosis_text=_row_get(r, "diagnosis_text") or "",
                age=_row_get(r, "patient_age"),
                age_months=_row_get(r, "patient_months"),
                age_unit=_row_get(r, "patient_age_unit"),
                patient_gender=_row_get(r, "patient_gender"),
                birth_date=parse_date_value(_row_get(r, "birth_date")),
                mkb_code=_row_get(r, "mkb_code"),
                operation_info=_row_get(r, "operation_info"),
                emergency_notice_number=_row_get(r, "emergency_notice_number"),
                emergency_notice_entered_at=_safe_parse_dt(_row_get(r, "emergency_notice_entered_at")),
                full_name=full_name,
                source_db_path=source_db_path,
                source_db_name=source_db_name,
                source_admission_id=int(source_admission_id) if source_admission_id is not None else None,
                is_external_archive=bool(_row_get(r, "is_external_archive", False)),
            ))
        return patients

    @staticmethod
    def _build_archived_patients_query(
        patient_columns: Optional[set[str]] = None,
        admission_columns: Optional[set[str]] = None,
        has_operations_table: bool = True,
        has_operation_cases_table: bool = False,
        start_dt: str | None = None,
        end_dt: str | None = None,
        search_name: str = "",
        search_ib: str = "",
        search_diag: str = "",
        limit: int | None = None,
        offset: int = 0,
        count_only: bool = False,
        end_exclusive: bool = False,
    ) -> tuple[str, tuple]:
        patient_columns = patient_columns or {"last_name", "first_name", "middle_name", "full_name", "birth_date"}
        admission_columns = admission_columns or {
            "history_number",
            "bed_number",
            "admission_datetime",
            "transfer_datetime",
            "death_datetime",
            "diagnosis_text",
            "patient_age",
            "patient_months",
            "patient_age_unit",
            "patient_gender",
            "diagnosis_code",
            "operation_description",
            "emergency_notice_number",
            "emergency_notice_entered_at",
            "unit_scope",
            "admission_type",
            "merged_into_admission_id",
        }

        def p_col(name: str) -> str:
            return f"p.{name}" if name in patient_columns else "NULL"

        def a_col(name: str) -> str:
            return f"a.{name}" if name in admission_columns else "NULL"

        transfer_expr = "COALESCE(a.transfer_datetime, a.death_datetime)"
        if "transfer_datetime" not in admission_columns and "death_datetime" not in admission_columns:
            transfer_expr = "NULL"
        elif "transfer_datetime" not in admission_columns:
            transfer_expr = "a.death_datetime"
        elif "death_datetime" not in admission_columns:
            transfer_expr = "a.transfer_datetime"

        operation_subquery = "NULL"
        if has_operations_table:
            operation_subquery = (
                "(SELECT o.description FROM operations o "
                "WHERE o.admission_id = a.id "
                "ORDER BY DATETIME(o.operation_datetime) DESC, o.id DESC LIMIT 1)"
            )

        if "operation_description" in admission_columns:
            operation_expr = f"COALESCE(a.operation_description, {operation_subquery})"
        else:
            operation_expr = operation_subquery

        order_expr = (
            "DATETIME(a.admission_datetime) DESC, a.id DESC"
            if "admission_datetime" in admission_columns
            else "a.id DESC"
        )
        where_parts: list[str] = []
        params: list = []
        if start_dt and end_dt and "admission_datetime" in admission_columns:
            end_operator = "<" if end_exclusive else "<="
            where_parts.append(
                "/* REMCARD_MIXED_DATETIME_INDEXED_RANGE */ ("
                "(SUBSTR(a.admission_datetime, 11, 1) = ' ' "
                "AND /* REMCARD_MIXED_DATETIME_INDEXED_RANGE */ "
                "a.admission_datetime >= REPLACE(?1, 'T', ' ') "
                f"AND a.admission_datetime {end_operator} REPLACE(?2, 'T', ' ')) "
                "OR "
                "(SUBSTR(a.admission_datetime, 11, 1) = 'T' "
                "AND /* REMCARD_MIXED_DATETIME_INDEXED_RANGE */ "
                "a.admission_datetime >= REPLACE(?1, ' ', 'T') "
                f"AND a.admission_datetime {end_operator} REPLACE(?2, ' ', 'T'))"
                ")"
            )
            params.extend((start_dt, end_dt))

        if "unit_scope" in admission_columns:
            where_parts.append("LOWER(TRIM(COALESCE(a.unit_scope, ''))) <> 'operblock'")
        if "admission_type" in admission_columns:
            where_parts.append("LOWER(TRIM(COALESCE(a.admission_type, ''))) <> 'operblock'")
        if "merged_into_admission_id" in admission_columns:
            where_parts.append("a.merged_into_admission_id IS NULL")
        if has_operation_cases_table:
            where_parts.append(
                "NOT EXISTS (SELECT 1 FROM operation_cases oc WHERE oc.admission_id = a.id)"
            )

        clean_name = str(search_name or "").strip().casefold()
        if clean_name:
            name_expr = (
                f"COALESCE({p_col('full_name')}, '') || ' ' || "
                f"COALESCE({p_col('last_name')}, '') || ' ' || "
                f"COALESCE({p_col('first_name')}, '') || ' ' || "
                f"COALESCE({p_col('middle_name')}, '')"
            )
            where_parts.append(f"INSTR(CASEFOLD({name_expr}), ?) > 0")
            params.append(clean_name)

        clean_ib = str(search_ib or "").strip().casefold()
        if clean_ib and "history_number" in admission_columns:
            where_parts.append("INSTR(CASEFOLD(COALESCE(a.history_number, '')), ?) > 0")
            params.append(clean_ib)

        clean_diag = str(search_diag or "").strip().casefold()
        if clean_diag:
            diag_parts = []
            if "diagnosis_text" in admission_columns:
                diag_parts.append("COALESCE(a.diagnosis_text, '')")
            if "diagnosis_code" in admission_columns:
                diag_parts.append("COALESCE(a.diagnosis_code, '')")
            if diag_parts:
                diag_expr = " || ' ' || ".join(diag_parts)
                where_parts.append(f"INSTR(CASEFOLD({diag_expr}), ?) > 0")
                params.append(clean_diag)

        where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

        if count_only:
            query = f"""
                SELECT COUNT(*) AS total_count
                FROM admissions a
                JOIN patients p ON a.patient_id = p.id
                {where_sql}
            """
            return query, tuple(params)

        limit_sql = ""
        if limit is not None:
            limit_sql = "LIMIT ? OFFSET ?"
            params.extend((max(1, int(limit)), max(0, int(offset or 0))))

        query = f"""
            SELECT
                a.id as admission_id,
                {p_col('last_name')} as last_name,
                {p_col('first_name')} as first_name,
                {p_col('middle_name')} as middle_name,
                {p_col('full_name')} as full_name,
                {p_col('birth_date')} as birth_date,
                {a_col('history_number')} as history_number,
                {a_col('bed_number')} as bed_number,
                {a_col('admission_datetime')} as admission_datetime,
                {transfer_expr} as transfer_datetime,
                {a_col('diagnosis_text')} as diagnosis_text,
                {a_col('patient_age')} as patient_age,
                {a_col('patient_months')} as patient_months,
                {a_col('patient_age_unit')} as patient_age_unit,
                {a_col('patient_gender')} as patient_gender,
                {a_col('diagnosis_code')} as mkb_code,
                {a_col('emergency_notice_number')} as emergency_notice_number,
                {a_col('emergency_notice_entered_at')} as emergency_notice_entered_at,
                {operation_expr} as operation_info
            FROM admissions a
            JOIN patients p ON a.patient_id = p.id
            {where_sql}
            ORDER BY {order_expr}
            {limit_sql}
        """
        return query, tuple(params)

    @staticmethod
    def _iter_archived_db_paths(current_db_path: str, *, include_current: bool = False) -> list[str]:
        return discover_db_cycle_paths(current_db_path=current_db_path, include_current=include_current)

    def get_archive_db_paths_for_period(self, start_dt: str | None, end_dt: str | None) -> list[str]:
        current_db_path = os.path.abspath(str(getattr(self.db, "db_path", "") or ""))
        if not start_dt or not end_dt:
            return self._iter_archived_db_paths(current_db_path, include_current=True)
        return select_db_paths_for_period(
            current_db_path=current_db_path,
            start_dt=start_dt,
            end_dt=end_dt,
        )

    def _fetch_archived_rows_from_db(
        self,
        db_path: str,
        *,
        start_dt: str | None = None,
        end_dt: str | None = None,
        search_name: str = "",
        search_ib: str = "",
        search_diag: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict]:
        uri = build_sqlite_file_uri(db_path, mode="ro")
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=4.0)
        try:
            configure_connection(conn, readonly=True)
            schema = get_archive_schema(
                conn,
                db_path,
                inspect_tables=("patients", "admissions"),
            )
            tables = schema.tables
            if "patients" not in tables or "admissions" not in tables:
                return []

            patient_columns = schema.columns.get("patients", frozenset())
            admission_columns = schema.columns.get("admissions", frozenset())
            has_operations = "operations" in tables
            has_operation_cases = "operation_cases" in tables

            query, params = self._build_archived_patients_query(
                patient_columns=patient_columns,
                admission_columns=admission_columns,
                has_operations_table=has_operations,
                has_operation_cases_table=has_operation_cases,
                start_dt=start_dt,
                end_dt=end_dt,
                search_name=search_name,
                search_ib=search_ib,
                search_diag=search_diag,
                limit=limit,
                offset=offset,
            )
            return [dict(row) for row in conn.execute(query, params).fetchall()]
        finally:
            conn.close()

    def _fetch_archived_count_from_db(
        self,
        db_path: str,
        *,
        start_dt: str | None = None,
        end_dt: str | None = None,
        search_name: str = "",
        search_ib: str = "",
        search_diag: str = "",
    ) -> int:
        uri = build_sqlite_file_uri(db_path, mode="ro")
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=4.0)
        try:
            configure_connection(conn, readonly=True)
            schema = get_archive_schema(
                conn,
                db_path,
                inspect_tables=("patients", "admissions"),
            )
            tables = schema.tables
            if "patients" not in tables or "admissions" not in tables:
                return 0

            patient_columns = schema.columns.get("patients", frozenset())
            admission_columns = schema.columns.get("admissions", frozenset())
            query, params = self._build_archived_patients_query(
                patient_columns=patient_columns,
                admission_columns=admission_columns,
                has_operations_table="operations" in tables,
                has_operation_cases_table="operation_cases" in tables,
                start_dt=start_dt,
                end_dt=end_dt,
                search_name=search_name,
                search_ib=search_ib,
                search_diag=search_diag,
                count_only=True,
            )
            row = conn.execute(query, params).fetchone()
            return int(row["total_count"] or 0) if row else 0
        finally:
            conn.close()

    def _fetch_archived_page_from_db(
        self,
        db_path: str,
        *,
        start_dt: str | None = None,
        end_dt: str | None = None,
        search_name: str = "",
        search_ib: str = "",
        search_diag: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[int, list[dict]]:
        """Fetch count and rows from one readonly archive connection."""
        uri = build_sqlite_file_uri(db_path, mode="ro")
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=4.0)
        try:
            configure_connection(conn, readonly=True)
            schema = get_archive_schema(
                conn,
                db_path,
                inspect_tables=("patients", "admissions"),
            )
            tables = schema.tables
            if not {"patients", "admissions"}.issubset(tables):
                return 0, []
            conn.execute("BEGIN")
            conn.execute("SELECT name FROM main.sqlite_master LIMIT 1").fetchone()
            query_args = {
                "patient_columns": schema.columns.get("patients", frozenset()),
                "admission_columns": schema.columns.get("admissions", frozenset()),
                "has_operations_table": "operations" in tables,
                "has_operation_cases_table": "operation_cases" in tables,
                "start_dt": start_dt,
                "end_dt": end_dt,
                "search_name": search_name,
                "search_ib": search_ib,
                "search_diag": search_diag,
                "end_exclusive": True,
            }
            count_query, count_params = self._build_archived_patients_query(
                **query_args,
                count_only=True,
            )
            count_row = conn.execute(count_query, count_params).fetchone()
            total_count = int(count_row["total_count"] or 0) if count_row else 0
            if total_count <= 0:
                return 0, []
            rows_query, rows_params = self._build_archived_patients_query(
                **query_args,
                limit=limit,
                offset=offset,
            )
            rows = [dict(row) for row in conn.execute(rows_query, rows_params).fetchall()]
            return total_count, rows
        finally:
            if bool(getattr(conn, "in_transaction", False)):
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            conn.close()

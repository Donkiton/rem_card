import glob
import os
import sqlite3
from typing import List, Optional
from datetime import datetime, timedelta
from ..dto.remcard_dto import PatientDTO
from .patient_status_dao import PatientStatusDAO
from ..dto.remcard_dto import PatientStatus
from rem_card.app.patient_age import parse_date_value
from rem_card.services.shift_service import ShiftService
from ...app.logger import logger
from ...app.paths import ARCHIV_DIR, DB_CYCLE_ARCHIVE_DIR
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
                a.diagnosis_text, a.patient_age, a.patient_months, a.patient_age_unit,
                a.diagnosis_code as mkb_code,
                COALESCE(
                    a.operation_description,
                    (SELECT o.description FROM operations o WHERE o.admission_id = a.id ORDER BY o.operation_datetime DESC LIMIT 1)
                ) as operation_info
            FROM admissions a
            JOIN patients p ON a.patient_id = p.id
            JOIN beds b ON a.id = b.current_admission_id
            WHERE b.status = 'OCCUPIED'
        """
        rows = self.db.fetch_all_remcard(query)
        return self._map_patients(rows)

    def get_archived_patients(self) -> List[PatientDTO]:
        current_db_path = os.path.abspath(str(getattr(self.db, "db_path", "") or ""))
        rows: list[dict] = []

        # 1) Текущая рабочая БД.
        try:
            current_rows = self.db.fetch_all_remcard(self._build_archived_patients_query())
            for row in current_rows:
                data = dict(row)
                data["source_db_path"] = current_db_path
                data["source_db_name"] = os.path.basename(current_db_path) if current_db_path else "rao_journal.db"
                data["source_admission_id"] = data.get("admission_id")
                data["is_external_archive"] = False
                rows.append(data)
        except Exception as exc:
            logger.warning("Failed to load archived patients from current DB: %s", exc)

        # 2) Ротационные архивы (старые циклы БД).
        for archived_db_path in self._iter_archived_db_paths(current_db_path):
            try:
                archived_rows = self._fetch_archived_rows_from_db(archived_db_path)
                for data in archived_rows:
                    data["source_db_path"] = archived_db_path
                    data["source_db_name"] = os.path.basename(archived_db_path)
                    data["source_admission_id"] = data.get("admission_id")
                    data["is_external_archive"] = True
                    rows.append(data)
            except Exception as exc:
                logger.warning("Skipping archived DB %s due to read error: %s", archived_db_path, exc)

        patients = self._map_patients(rows)
        patients.sort(key=lambda p: p.admission_datetime or datetime.min, reverse=True)
        return patients

    def get_patient_by_id(self, admission_id: int) -> Optional[PatientDTO]:
        query = """
            SELECT 
                a.id as admission_id, p.last_name, p.first_name, p.middle_name, p.full_name, p.birth_date,
                a.history_number, a.bed_number, a.admission_datetime, COALESCE(a.transfer_datetime, a.death_datetime) as transfer_datetime,
                a.diagnosis_text, a.patient_age, a.patient_months, a.patient_age_unit,
                a.diagnosis_code as mkb_code,
                COALESCE(
                    a.operation_description,
                    (SELECT o.description FROM operations o WHERE o.admission_id = a.id ORDER BY o.operation_datetime DESC LIMIT 1)
                ) as operation_info
            FROM admissions a
            JOIN patients p ON a.patient_id = p.id
            WHERE a.id = ?
        """
        try:
            rows = self.db.fetch_all_remcard(query, (admission_id,))
        except sqlite3.OperationalError as exc:
            if "no such table: operations" not in str(exc).lower():
                raise
            fallback_query = """
                SELECT 
                    a.id as admission_id, p.last_name, p.first_name, p.middle_name, p.full_name, p.birth_date,
                    a.history_number, a.bed_number, a.admission_datetime, COALESCE(a.transfer_datetime, a.death_datetime) as transfer_datetime,
                    a.diagnosis_text, a.patient_age, a.patient_months, a.patient_age_unit,
                    a.diagnosis_code as mkb_code,
                    a.operation_description as operation_info
                FROM admissions a
                JOIN patients p ON a.patient_id = p.id
                WHERE a.id = ?
            """
            rows = self.db.fetch_all_remcard(fallback_query, (admission_id,))
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
                birth_date=parse_date_value(_row_get(r, "birth_date")),
                mkb_code=_row_get(r, "mkb_code"),
                operation_info=_row_get(r, "operation_info"),
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
    ) -> str:
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
            "diagnosis_code",
            "operation_description",
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
                "ORDER BY o.operation_datetime DESC LIMIT 1)"
            )

        if "operation_description" in admission_columns:
            operation_expr = f"COALESCE(a.operation_description, {operation_subquery})"
        else:
            operation_expr = operation_subquery

        order_expr = "a.admission_datetime DESC" if "admission_datetime" in admission_columns else "a.id DESC"

        return f"""
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
                {a_col('diagnosis_code')} as mkb_code,
                {operation_expr} as operation_info
            FROM admissions a
            JOIN patients p ON a.patient_id = p.id
            ORDER BY {order_expr}
        """

    @staticmethod
    def _iter_archived_db_paths(current_db_path: str) -> list[str]:
        patterns = [
            os.path.join(ARCHIV_DIR, "rao_journal_archived_*.db"),
            os.path.join(ARCHIV_DIR, "rao_journal_cycle_*.db"),
            os.path.join(ARCHIV_DIR, "rao-*.db"),
            os.path.join(DB_CYCLE_ARCHIVE_DIR, "*.db"),  # legacy path compatibility
        ]

        seen = set()
        found = []
        current_abs = os.path.abspath(current_db_path) if current_db_path else ""

        for pattern in patterns:
            for path in glob.glob(pattern):
                if not path.lower().endswith(".db"):
                    continue
                abs_path = os.path.abspath(path)
                if current_abs and abs_path == current_abs:
                    continue
                if abs_path in seen:
                    continue
                if not os.path.isfile(abs_path):
                    continue
                seen.add(abs_path)
                found.append(abs_path)

        found.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        return found

    def _fetch_archived_rows_from_db(self, db_path: str) -> list[dict]:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=4.0)
        try:
            configure_connection(conn)
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if "patients" not in tables or "admissions" not in tables:
                return []

            patient_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(patients)").fetchall()
            }
            admission_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(admissions)").fetchall()
            }
            has_operations = "operations" in tables

            query = self._build_archived_patients_query(
                patient_columns=patient_columns,
                admission_columns=admission_columns,
                has_operations_table=has_operations,
            )
            return [dict(row) for row in conn.execute(query).fetchall()]
        finally:
            conn.close()

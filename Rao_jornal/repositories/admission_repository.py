from typing import Optional, List
from rem_card.Rao_jornal.domain.admission import Admission
from rem_card.Rao_jornal.domain.transfusion import Transfusion
from rem_card.Rao_jornal.domain.operation import Operation
from rem_card.Rao_jornal.database.db_manager import DBManager

class AdmissionRepository:
    def __init__(self, db_manager: DBManager):
        self.db_manager = db_manager

    def add_admission(self, admission: Admission, cursor=None) -> int:
        if cursor is not None:
            cursor.execute(
                """INSERT INTO admissions (
                    patient_id, bed_number, history_number, admission_datetime, 
                    patient_age, patient_months, patient_age_unit, patient_gender,
                    diagnosis_code, diagnosis_text,
                    department_profile, source_department, transfer_datetime, transfer_department,
                    outcome, transfer_lpu, transfer_lpu_other, death_datetime, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    admission.patient_id, admission.bed_number, admission.history_number, admission.admission_datetime,
                    admission.patient_age, admission.patient_months, admission.patient_age_unit, admission.patient_gender,
                    admission.diagnosis_code, admission.diagnosis_text, admission.department_profile,
                    admission.source_department, admission.transfer_datetime,
                    admission.transfer_department, admission.outcome, admission.transfer_lpu,
                    admission.transfer_lpu_other, admission.death_datetime, admission.created_at, admission.updated_at
                )
            )
            return cursor.lastrowid
        with self.db_manager.write_transaction(source="journal_add_admission") as tx:
            tx.execute(
                """INSERT INTO admissions (
                    patient_id, bed_number, history_number, admission_datetime, 
                    patient_age, patient_months, patient_age_unit, patient_gender,
                    diagnosis_code, diagnosis_text,
                    department_profile, source_department, transfer_datetime, transfer_department,
                    outcome, transfer_lpu, transfer_lpu_other, death_datetime, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    admission.patient_id, admission.bed_number, admission.history_number, admission.admission_datetime,
                    admission.patient_age, admission.patient_months, admission.patient_age_unit, admission.patient_gender,
                    admission.diagnosis_code, admission.diagnosis_text, admission.department_profile,
                    admission.source_department, admission.transfer_datetime,
                    admission.transfer_department, admission.outcome, admission.transfer_lpu,
                    admission.transfer_lpu_other, admission.death_datetime, admission.created_at, admission.updated_at
                )
            )
            return tx.lastrowid

    def get_admission_by_id(self, admission_id: int) -> Optional[Admission]:
        conn = self.db_manager.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM admissions WHERE id = ?", (admission_id,))
        row = cursor.fetchone()
        if row:
            return Admission(**row)
        return None

    def get_current_admission_by_bed(self, bed_number: int) -> Optional[Admission]:
        conn = self.db_manager.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT a.* FROM admissions a JOIN beds b ON a.id = b.current_admission_id WHERE b.bed_number = ?",
            (bed_number,)
        )
        row = cursor.fetchone()
        if row:
            return Admission(**row)
        return None

    def update_admission(self, admission: Admission, cursor=None):
        if cursor is not None:
            cursor.execute(
                """UPDATE admissions SET
                    patient_id = ?, bed_number = ?, history_number = ?, admission_datetime = ?,
                    patient_age = ?, patient_months = ?, patient_age_unit = ?, patient_gender = ?,
                    diagnosis_code = ?, diagnosis_text = ?, department_profile = ?, source_department = ?,
                    transfer_datetime = ?, transfer_department = ?, outcome = ?,
                    transfer_lpu = ?, transfer_lpu_other = ?, death_datetime = ?, updated_at = ?
                WHERE id = ?""",
                (
                    admission.patient_id, admission.bed_number, admission.history_number, admission.admission_datetime,
                    admission.patient_age, admission.patient_months, admission.patient_age_unit, admission.patient_gender,
                    admission.diagnosis_code, admission.diagnosis_text, admission.department_profile,
                    admission.source_department, admission.transfer_datetime,
                    admission.transfer_department, admission.outcome, admission.transfer_lpu,
                    admission.transfer_lpu_other, admission.death_datetime, admission.updated_at, admission.id
                )
            )
            return
        with self.db_manager.write_transaction(source="journal_update_admission") as tx:
            tx.execute(
                """UPDATE admissions SET
                    patient_id = ?, bed_number = ?, history_number = ?, admission_datetime = ?,
                    patient_age = ?, patient_months = ?, patient_age_unit = ?, patient_gender = ?,
                    diagnosis_code = ?, diagnosis_text = ?, department_profile = ?, source_department = ?,
                    transfer_datetime = ?, transfer_department = ?, outcome = ?,
                    transfer_lpu = ?, transfer_lpu_other = ?, death_datetime = ?, updated_at = ?
                WHERE id = ?""",
                (
                    admission.patient_id, admission.bed_number, admission.history_number, admission.admission_datetime,
                    admission.patient_age, admission.patient_months, admission.patient_age_unit, admission.patient_gender,
                    admission.diagnosis_code, admission.diagnosis_text, admission.department_profile,
                    admission.source_department, admission.transfer_datetime,
                    admission.transfer_department, admission.outcome, admission.transfer_lpu,
                    admission.transfer_lpu_other, admission.death_datetime, admission.updated_at, admission.id
                )
            )

    # Operations
    def add_operation(self, operation: Operation, cursor=None):
        if cursor is not None:
            cursor.execute(
                "INSERT INTO operations (admission_id, operation_number, description, operation_datetime) VALUES (?, ?, ?, ?)",
                (operation.admission_id, operation.operation_number, operation.description, operation.operation_datetime)
            )
            return
        with self.db_manager.write_transaction(source="journal_add_operation") as cursor:
            cursor.execute(
                "INSERT INTO operations (admission_id, operation_number, description, operation_datetime) VALUES (?, ?, ?, ?)",
                (operation.admission_id, operation.operation_number, operation.description, operation.operation_datetime)
            )

    def get_operations_by_admission(self, admission_id: int) -> List[Operation]:
        conn = self.db_manager.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM operations WHERE admission_id = ? ORDER BY operation_number", (admission_id,))
        rows = cursor.fetchall()
        return [Operation(**row) for row in rows]

    def delete_operations_by_admission(self, admission_id: int, cursor=None):
        if cursor is not None:
            cursor.execute("DELETE FROM operations WHERE admission_id = ?", (admission_id,))
            return
        with self.db_manager.write_transaction(source="journal_delete_operations") as cursor:
            cursor.execute("DELETE FROM operations WHERE admission_id = ?", (admission_id,))

    # IVL Episodes
    def add_ivl_episode(self, episode, cursor=None):
        if cursor is not None:
            cursor.execute(
                "INSERT INTO ivl_episodes (admission_id, episode_number, start_time, end_time, type) VALUES (?, ?, ?, ?, ?)",
                (episode.admission_id, episode.episode_number, episode.start_time, episode.end_time, episode.type)
            )
            return
        with self.db_manager.write_transaction(source="journal_add_ivl") as cursor:
            cursor.execute(
                "INSERT INTO ivl_episodes (admission_id, episode_number, start_time, end_time, type) VALUES (?, ?, ?, ?, ?)",
                (episode.admission_id, episode.episode_number, episode.start_time, episode.end_time, episode.type)
            )

    def get_ivl_episodes_by_admission(self, admission_id: int):
        from rem_card.Rao_jornal.domain.ivl_episode import IVLEpisode
        conn = self.db_manager.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM ivl_episodes WHERE admission_id = ? ORDER BY episode_number", (admission_id,))
        rows = cursor.fetchall()
        return [IVLEpisode(**row) for row in rows]

    def delete_ivl_episodes_by_admission(self, admission_id: int, cursor=None):
        if cursor is not None:
            cursor.execute("DELETE FROM ivl_episodes WHERE admission_id = ?", (admission_id,))
            return
        with self.db_manager.write_transaction(source="journal_delete_ivl") as cursor:
            cursor.execute("DELETE FROM ivl_episodes WHERE admission_id = ?", (admission_id,))

    # Transfusions
    def add_transfusion(self, transfusion: Transfusion):
        with self.db_manager.write_transaction(source="journal_add_transfusion") as cursor:
            cursor.execute(
                "INSERT INTO transfusions (admission_id, type, volume_ml, datetime) VALUES (?, ?, ?, ?)",
                (transfusion.admission_id, transfusion.type, transfusion.volume_ml, transfusion.datetime)
            )

    def get_transfusions_by_admission(self, admission_id: int) -> List[Transfusion]:
        conn = self.db_manager.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM transfusions WHERE admission_id = ?", (admission_id,))
        rows = cursor.fetchall()
        return [Transfusion(**row) for row in rows]

    def delete_transfusion(self, transfusion_id: int):
        with self.db_manager.write_transaction(source="journal_delete_transfusion") as cursor:
            cursor.execute("DELETE FROM transfusions WHERE id = ?", (transfusion_id,))

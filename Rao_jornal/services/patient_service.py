from __future__ import annotations

from typing import Optional, List, TYPE_CHECKING
from rem_card.Rao_jornal.repositories.patient_repository import PatientRepository
from rem_card.Rao_jornal.repositories.admission_repository import AdmissionRepository

if TYPE_CHECKING:
    from rem_card.Rao_jornal.database.db_manager import DBManager
    from rem_card.Rao_jornal.domain.patient import Patient
    from rem_card.Rao_jornal.domain.admission import Admission
    from rem_card.Rao_jornal.domain.transfusion import Transfusion
    from rem_card.Rao_jornal.domain.operation import Operation
    from rem_card.Rao_jornal.domain.ivl_episode import IVLEpisode

class PatientService:
    def __init__(self, db_manager: DBManager):
        self.patient_repo = PatientRepository(db_manager)
        self.admission_repo = AdmissionRepository(db_manager)
        self.db_manager = db_manager

    def create_patient_and_admission(self, patient: Patient, admission: Admission) -> Optional[int]:
        try:
            import uuid
            if patient.admission_uid is None:
                patient.admission_uid = str(uuid.uuid4())

            with self.db_manager.write_transaction(source="journal_create_patient_and_admission") as cursor:
                patient_id = self.patient_repo.add_patient(patient, cursor=cursor)
                admission.patient_id = patient_id
                admission_id = self.admission_repo.add_admission(admission, cursor=cursor)
                cursor.execute(
                    """
                    UPDATE beds
                    SET status = ?, current_admission_id = ?
                    WHERE bed_number = ?
                      AND status = 'FREE'
                      AND current_admission_id IS NULL
                    """,
                    ("OCCUPIED", admission_id, admission.bed_number),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"Койка {admission.bed_number} уже занята другим пользователем.")
                return admission_id
        except Exception as e:
            print(f"Error creating patient and admission: {e}")
            raise e

    def get_patient_with_current_admission(self, bed_number: int):
        admission = self.admission_repo.get_current_admission_by_bed(bed_number)
        if admission:
            patient = self.patient_repo.get_patient_by_id(admission.patient_id)
            if patient:
                return patient, admission
        return None, None

    def update_patient_and_admission(self, patient: Patient, admission: Admission):
        try:
            with self.db_manager.write_transaction(source="journal_update_patient_and_admission") as cursor:
                self.patient_repo.update_patient(patient, cursor=cursor)
                self.admission_repo.update_admission(admission, cursor=cursor)
            if hasattr(self.db_manager, '_check_for_backup'):
                self.db_manager._check_for_backup()
            return True
        except Exception as e:
            print(f"Error updating patient and admission: {e}")
            raise e

    # Operations
    def add_operation(self, operation: Operation):
        return self.admission_repo.add_operation(operation)

    def get_operations_by_admission(self, admission_id: int) -> List[Operation]:
        return self.admission_repo.get_operations_by_admission(admission_id)

    def update_operations(self, admission_id: int, operations: List[Operation]):
        with self.db_manager.write_transaction(source="journal_update_operations") as cursor:
            self.admission_repo.delete_operations_by_admission(admission_id, cursor=cursor)
            for op in operations:
                op.admission_id = admission_id
                self.admission_repo.add_operation(op, cursor=cursor)

    # Transfusions
    def add_transfusion(self, transfusion: Transfusion):
        return self.admission_repo.add_transfusion(transfusion)

    def get_transfusions_by_admission(self, admission_id: int) -> List[Transfusion]:
        return self.admission_repo.get_transfusions_by_admission(admission_id)

    def delete_transfusion(self, transfusion_id: int):
        return self.admission_repo.delete_transfusion(transfusion_id)

    def update_transfusions(self, admission_id: int, transfusions: List[Transfusion]):
        # We don't necessarily want to delete all transfusions every time 
        # because they are usually historical events. 
        # But for new admissions, we need to save the pending ones.
        for t in transfusions:
            t.admission_id = admission_id
            self.add_transfusion(t)

    # IVL
    def get_ivl_episodes_by_admission(self, admission_id: int) -> List[IVLEpisode]:
        return self.admission_repo.get_ivl_episodes_by_admission(admission_id)

    def update_ivl_episodes(self, admission_id: int, episodes: List[IVLEpisode]):
        with self.db_manager.write_transaction(source="journal_update_ivl_episodes") as cursor:
            self.admission_repo.delete_ivl_episodes_by_admission(admission_id, cursor=cursor)
            for i, ep in enumerate(episodes):
                ep.admission_id = admission_id
                ep.episode_number = i + 1
                self.admission_repo.add_ivl_episode(ep, cursor=cursor)

    def release_bed(self, bed_number: int):
        self.db_manager.update_bed_status(bed_number, "FREE", None)

import os
import time
import threading
from typing import Any, Callable, List, Optional
from ..data.dto.remcard_dto import PatientDTO
from ..data.dao.patient_dao import PatientDAO
from rem_card.app.logger import logger

class PatientService:
    def __init__(self, dao: PatientDAO, data_service=None):
        self.dao = dao
        self.data_service = data_service
        self.outcome_release_delay_minutes = max(0, int(os.environ.get("REMCARD_OUTCOME_RELEASE_DELAY_MIN", "30")))
        self._outcome_release_check_interval_sec = max(5.0, float(os.environ.get("REMCARD_OUTCOME_RELEASE_CHECK_SEC", "15")))
        self._last_outcome_release_check_mono = 0.0
        self._outcome_release_guard = threading.Lock()
        self._outcome_release_worker_active = False

    def enqueue_write(
        self,
        description: str,
        operation: Callable[[], Any],
        on_success=None,
        on_error=None,
    ):
        if self.data_service:
            self.data_service.enqueue_write(
                description=description,
                operation=operation,
                on_success=on_success,
                on_error=on_error,
            )
            return
        try:
            result = operation()
        except Exception as exc:
            if on_error:
                on_error(exc)
                return
            raise
        if on_success:
            on_success(result)

    def sync_patients(self):
        self.dao.sync_from_journal()

    def _release_due_outcome_beds_impl(self) -> int:
        released = 0
        try:
            released = self._release_due_outcome_beds_operation()
        except Exception as exc:
            logger.warning("Auto-release check failed: %s", exc)
        return released

    def _release_due_outcome_beds_operation(self) -> int:
        """Run the mutation without hiding its outcome from a write owner."""
        released = self.dao.release_due_outcome_beds(
            delay_minutes=self.outcome_release_delay_minutes
        )
        if released > 0:
            logger.info(
                "Auto-release completed: %s patient(s) removed from beds after outcome timeout (%s min).",
                released,
                self.outcome_release_delay_minutes,
            )
        return released

    def maybe_release_due_outcome_beds(self, force: bool = False) -> int:
        if self.data_service is not None:
            self.maybe_release_due_outcome_beds_async(force=force)
            return 0
        now_mono = time.monotonic()
        if not force and (now_mono - self._last_outcome_release_check_mono) < self._outcome_release_check_interval_sec:
            return 0
        self._last_outcome_release_check_mono = now_mono
        return self._release_due_outcome_beds_impl()

    def maybe_release_due_outcome_beds_async(self, force: bool = False) -> bool:
        now_mono = time.monotonic()
        with self._outcome_release_guard:
            if self._outcome_release_worker_active:
                return False
            if not force and (now_mono - self._last_outcome_release_check_mono) < self._outcome_release_check_interval_sec:
                return False
            self._last_outcome_release_check_mono = now_mono
            self._outcome_release_worker_active = True

        if self.data_service is None:
            with self._outcome_release_guard:
                self._outcome_release_worker_active = False
            logger.warning("Auto-release check skipped: lifecycle DataService is unavailable")
            return False

        def _operation():
            try:
                return self._release_due_outcome_beds_operation()
            finally:
                with self._outcome_release_guard:
                    self._outcome_release_worker_active = False

        def _on_error(exc: Exception):
            logger.warning("Auto-release check failed: %s", exc)

        try:
            accepted = bool(
                self.data_service.enqueue_write(
                    description="auto_release_outcome_beds",
                    operation=_operation,
                    on_error=_on_error,
                )
            )
        except Exception as exc:
            with self._outcome_release_guard:
                self._outcome_release_worker_active = False
            logger.warning("Auto-release check could not be submitted: %s", exc)
            return False
        if not accepted:
            with self._outcome_release_guard:
                self._outcome_release_worker_active = False
        return accepted

    def get_active_patients(self) -> List[PatientDTO]:
        self.maybe_release_due_outcome_beds()
        result = self.dao.get_active_patients()
        return result

    def get_active_patients_by_ids(self, admission_ids: List[int]) -> List[PatientDTO]:
        getter = getattr(self.dao, "get_active_patients_by_ids", None)
        if callable(getter):
            return getter(admission_ids)
        requested = {int(admission_id) for admission_id in (admission_ids or []) if admission_id is not None}
        if not requested:
            return []
        return [
            patient
            for patient in self.dao.get_active_patients()
            if getattr(patient, "id", None) is not None and int(patient.id) in requested
        ]

    def get_archived_patients(self, start_dt: str | None = None, end_dt: str | None = None) -> List[PatientDTO]:
        return self.dao.get_archived_patients(start_dt=start_dt, end_dt=end_dt)

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
        if hasattr(self.dao, "get_archived_patients_page"):
            return self.dao.get_archived_patients_page(
                start_dt=start_dt,
                end_dt=end_dt,
                page=page,
                page_size=page_size,
                search_name=search_name,
                search_ib=search_ib,
                search_diag=search_diag,
            )
        records = self.get_archived_patients(start_dt=start_dt, end_dt=end_dt)
        return {"records": records, "total_count": len(records), "page": page, "page_size": page_size}

    def get_archive_db_paths_for_period(self, start_dt: str | None, end_dt: str | None) -> list[str]:
        if hasattr(self.dao, "get_archive_db_paths_for_period"):
            return self.dao.get_archive_db_paths_for_period(start_dt, end_dt)
        return []

    def get_patient(self, admission_id: int) -> Optional[PatientDTO]:
        return self.dao.get_patient_by_id(admission_id)

    def delete_admission(self, admission_id: int):
        with self.dao.db.remcard_transaction():
            self.dao.delete_admission(admission_id)

    def delete_patient(self, patient_id: int):
        with self.dao.db.remcard_transaction():
            self.dao.delete_patient(patient_id)

import os
import time
import threading
from datetime import datetime
from typing import Any, Callable, List, Optional
from ..data.dto.remcard_dto import PatientDTO
from ..data.dao.patient_dao import PatientDAO
from rem_card.app.logger import logger
from rem_card.app.local_metrics import record_metric
from rem_card.services.write_dispatch import enqueue_service_write

class PatientService:
    def __init__(self, dao: PatientDAO, data_service=None):
        self.dao = dao
        self.data_service = data_service
        self.outcome_release_delay_minutes = max(0, int(os.environ.get("REMCARD_OUTCOME_RELEASE_DELAY_MIN", "30")))
        self._outcome_release_check_interval_sec = max(5.0, float(os.environ.get("REMCARD_OUTCOME_RELEASE_CHECK_SEC", "15")))
        self._last_outcome_release_check_mono = 0.0
        self._outcome_release_guard = threading.Lock()
        self._outcome_release_worker_active = False
        self._outcome_release_reconcile_interval_sec = 60.0
        self._outcome_release_schedule_known = False
        self._outcome_release_schedule_generation = 0
        self._outcome_release_schedule_checked_mono = 0.0
        self._next_outcome_release_at = None

    def invalidate_outcome_release_schedule(self):
        with self._outcome_release_guard:
            self._outcome_release_schedule_generation += 1
            self._outcome_release_schedule_known = False

    def _outcome_release_check_needed(self, now_mono: float) -> bool:
        """Called under the guard; performs no database access."""
        return (
            not self._outcome_release_schedule_known
            or now_mono - self._outcome_release_schedule_checked_mono >= self._outcome_release_reconcile_interval_sec
            or (self._next_outcome_release_at is not None and datetime.now() >= self._next_outcome_release_at)
        )

    def enqueue_write(
        self,
        description: str,
        operation: Callable[[], Any],
        on_success=None,
        on_error=None,
    ):
        enqueue_service_write(self.data_service, description, operation, on_success, on_error)

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
        started = time.perf_counter()
        released = 0
        next_release = None
        status = "error"
        deadline_due = False
        with self._outcome_release_guard:
            generation = self._outcome_release_schedule_generation
        get_deadline = getattr(self.dao, "get_next_outcome_bed_release_at", None)
        try:
            if callable(get_deadline):
                next_release = get_deadline(delay_minutes=self.outcome_release_delay_minutes)
            deadline_due = next_release is not None and datetime.now() >= next_release
            if not callable(get_deadline) or deadline_due:
                released = self.dao.release_due_outcome_beds(delay_minutes=self.outcome_release_delay_minutes)
            with self._outcome_release_guard:
                if generation == self._outcome_release_schedule_generation and callable(get_deadline):
                    self._next_outcome_release_at = next_release
                    self._outcome_release_schedule_checked_mono = time.monotonic()
                    # Reconcile after a due attempt, without introducing a read
                    # failure after an already committed clinical mutation.
                    self._outcome_release_schedule_known = not deadline_due
            status = "ok"
        except Exception:
            self.invalidate_outcome_release_schedule()
            raise
        finally:
            record_metric(
                "outcome_bed_release_check_ms", round((time.perf_counter() - started) * 1000.0, 3),
                status=status, released_count=released,
                next_due_in_sec=None if next_release is None else round((next_release - datetime.now()).total_seconds(), 3),
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
        with self._outcome_release_guard:
            if self._outcome_release_worker_active:
                return 0
            if not force and (
                (now_mono - self._last_outcome_release_check_mono) < self._outcome_release_check_interval_sec
                or not self._outcome_release_check_needed(now_mono)
            ):
                return 0
            self._last_outcome_release_check_mono = now_mono
            self._outcome_release_worker_active = True
        try:
            return self._release_due_outcome_beds_impl()
        finally:
            with self._outcome_release_guard:
                self._outcome_release_worker_active = False

    def maybe_release_due_outcome_beds_async(self, force: bool = False) -> bool:
        if self.data_service is not None:
            outage_detected = getattr(self.data_service, "is_network_outage_detected", None)
            if callable(outage_detected) and outage_detected():
                return False
        now_mono = time.monotonic()
        with self._outcome_release_guard:
            if self._outcome_release_worker_active:
                return False
            if not force and (
                (now_mono - self._last_outcome_release_check_mono) < self._outcome_release_check_interval_sec
                or not self._outcome_release_check_needed(now_mono)
            ):
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

"""Deadline-driven reconciliation for visible RAO bed lists."""
from datetime import datetime, timedelta
import time

from PySide6.QtCore import QObject, QTimer


class OutcomeBedReleaseMonitor(QObject):
    """Use local deadlines; query only overdue rows, at most once per 15 s."""

    def __init__(self, beds):
        super().__init__(beds)
        self.beds = beds
        self._next_check = 0.0
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.check)
        self.timer.start()

    def stop(self):
        self.timer.stop()

    def check(self):
        beds = self.beds
        if beds._is_closing or not beds.isVisible():
            return
        now = datetime.now()
        due = []
        for admission_id, row in beds._rows_by_admission_id.items():
            sector = row.sector_4b
            status = sector._outcome_timer_status_dto
            if status is None or not status.status.is_outcome() or not status.start_time:
                continue
            if now >= status.start_time + timedelta(minutes=sector._outcome_timer_delay_minutes):
                due.append(admission_id)
        if not due:
            self._next_check = 0.0
            return
        if time.monotonic() < self._next_check:
            return
        data = getattr(beds.patient_service, 'data_service', None)
        outage = getattr(data, 'is_network_outage_detected', None)
        if callable(outage) and outage():
            return
        self._next_check = time.monotonic() + 15.0
        # The write owner rechecks current status in its transaction. The
        # asynchronous read can race that write; retry until the DB confirms
        # release, or until a cancelled outcome removes the local deadline.
        release = getattr(beds.patient_service, 'maybe_release_due_outcome_beds_async', None)
        if callable(release):
            release(force=True)
        beds.refresh_admissions(due, queue_if_running=False)


def remove_released_rows(beds, released_ids):
    """Remove only rows whose absence was confirmed by a successful DB read."""
    for admission_id in released_ids:
        row = beds._rows_by_admission_id.pop(admission_id)
        beds.list_layout.removeWidget(row)
        row.hide()
        row.deleteLater()
    beds._last_ordered_row_ids = tuple(
        admission_id for admission_id in beds._last_ordered_row_ids
        if admission_id in beds._rows_by_admission_id
    )
    if beds._last_ordered_row_ids:
        first = beds._rows_by_admission_id[beds._last_ordered_row_ids[0]]
        first.setContentsMargins(0, 5, 0, 0)

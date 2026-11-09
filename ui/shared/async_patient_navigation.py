"""Keep patient navigation reads and the add-patient lease off the Qt thread."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import threading
from typing import Callable
import weakref

from PySide6.QtCore import QObject, Signal, Slot
from shiboken6 import isValid

from rem_card.app.logger import logger
from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.custom_message_box import CustomMessageBox


def latest_card_date(service, admission_id: int, *, non_future_at: datetime | None = None):
    try:
        dates = service.get_all_card_dates(admission_id)
        if not dates:
            return None
        if non_future_at is None:
            return max(dates)

        def shift_start(value):
            try:
                return service.get_day_period(value)[0] if value is not None else None
            except Exception:
                return None

        current_start = shift_start(non_future_at)
        if current_start is not None:
            return max(
                (date for date in dates if (start := shift_start(date)) is not None and start <= current_start),
                default=None,
            )
    except Exception as exc:
        logger.warning("Failed to resolve latest card date: %s", exc)
    return None


def resolve_card_date(
    service, admission_id: int, now: datetime, *, exclude_future: bool,
    cancelled: Callable[[], bool] | None = None,
):
    if cancelled is not None and cancelled():
        return None
    try:
        if service.has_card(admission_id, now):
            return now
    except Exception as exc:
        logger.warning("Failed to resolve current card date: %s", exc)
    if cancelled is not None and cancelled():
        return None
    return latest_card_date(service, admission_id, non_future_at=now if exclude_future else None)


def _context(owner):
    layout = getattr(owner, "layout_manager", None)
    service = getattr(owner, "service", None) or getattr(owner, "remcard_service", None)
    return (
        id(layout),
        id(service),
        getattr(owner, "_selection_mode", None),
        getattr(layout, "current_admission_id", None),
        getattr(owner, "admission_id", None),
        getattr(owner, "_current_date", None),
        getattr(owner, "_patient_open_generation", None),
    )


@dataclass(eq=False)
class _CardRequest:
    context: tuple
    service: object
    admission_id: int
    now: datetime
    exclude_future: bool
    source: str
    deliver: Callable[[datetime | None], None]

    def same_action(self, other):
        return (self.context, self.admission_id, self.source) == (other.context, other.admission_id, other.source)


class _CardDateWorker(AsyncCallThread):
    def __init__(self, request):
        super().__init__(
            resolve_card_date, request.service, request.admission_id, request.now,
            exclude_future=request.exclude_future, cancelled=self.is_cancel_requested,
        )


class _AddPatientLease(AsyncCallThread):
    ready = Signal(bool, object)

    def __init__(self, lock):
        super().__init__(self._hold)
        self._lock = lock
        self._released = threading.Event()

    def quit(self):
        self._released.set()
        super().quit()

    def _hold(self):
        if self._released.is_set():
            return
        held = False
        try:
            held = self._lock.acquire()
            if self._released.is_set():
                return
            holder = None if held else self._lock.holder_owner_role()
            self.ready.emit(held, holder)
            if held:
                self._released.wait()
        finally:
            if held or self._lock.ownership_context():
                self._lock.release()


class _PatientNavigation(QObject):
    def __init__(self, owner):
        super().__init__(owner)
        self._owner = weakref.ref(owner)
        self._card_worker = None
        self._active_card = None
        self._wanted_card = None
        self._lease = None
        self._lease_context = None
        self._enter_management = None

    def _live_owner(self):
        owner = self._owner()
        if (
            owner is not None and isValid(owner)
            and not getattr(owner, "_is_closing", False)
            and not getattr(owner, "_patient_navigation_quiesced", False)
        ):
            return owner
        return None

    def request_card(self, service, admission_id, now, exclude_future, source, deliver):
        owner = self._live_owner()
        if owner is None or not admission_id or service is None:
            return
        # A newer click supersedes an acquisition that has not opened the view.
        # Once open, that view keeps its lease until the normal mode transition.
        if self._lease is not None and not owner._add_patient_lock_held:
            self.release_add_patient()
        request = _CardRequest(_context(owner), service, int(admission_id), now, exclude_future, source, deliver)
        if self._wanted_card is not None and self._wanted_card.same_action(request):
            return
        if self._card_worker is not None:
            self._card_worker.quit()
        self._wanted_card = request
        self._start_card()

    def _start_card(self):
        request = self._wanted_card
        owner = self._live_owner()
        if self._card_worker is not None or request is None:
            return
        if owner is None or request.context != _context(owner):
            self._wanted_card = None
            return
        worker = _CardDateWorker(request)
        worker.succeeded.connect(self._card_ready)
        worker.failed.connect(self._card_failed)
        worker.finished.connect(self._card_finished)
        owner.destroyed.connect(worker.quit)
        self._card_worker, self._active_card = worker, request
        try:
            worker.start()
        except Exception as exc:
            self._card_failed(exc)
            self._card_finished()

    def cancel_card(self):
        self._wanted_card = None
        if self._card_worker is not None:
            self._card_worker.quit()

    @Slot(object)
    def _card_ready(self, date):
        owner = self._live_owner()
        request = self._active_card
        if request is not None and request is self._wanted_card:
            self._wanted_card = None
            if owner is not None and request.context == _context(owner):
                request.deliver(date)

    @Slot(object)
    def _card_failed(self, exc):
        logger.warning("Card date lookup failed: %s", exc)
        self._card_ready(None)

    @Slot()
    def _card_finished(self):
        self._card_worker = None
        self._active_card = None
        self._start_card()

    @property
    def add_patient_busy(self):
        return self._lease is not None

    def request_add_patient(self, enter_management):
        owner = self._live_owner()
        if owner is None or self._lease is not None:
            return
        self.cancel_card()
        lock = owner._add_patient_lock
        if lock is None:
            enter_management()
            return
        self._lease_context = _context(owner)
        self._enter_management = enter_management
        lease = _AddPatientLease(lock)
        self._lease = lease
        lease.ready.connect(self._lease_ready)
        lease.failed.connect(self._lease_failed)
        lease.finished.connect(self._lease_finished)
        owner.destroyed.connect(lease.quit)
        try:
            lease.start()
        except Exception as exc:
            self._lease_failed(exc)
            self._lease_finished()
            return
        owner._apply_add_patient_button_state()

    def release_add_patient(self):
        owner = self._owner()
        if owner is not None:
            owner._add_patient_lock_held = False
        self._enter_management = None
        if self._lease is not None:
            self._lease.quit()

    @Slot(bool, object)
    def _lease_ready(self, acquired, holder):
        owner = self._live_owner()
        lease = self._lease
        if (owner is None or self._enter_management is None or self._lease_context != _context(owner)
                or lease is None or lease.is_cancel_requested()):
            self.release_add_patient()
            return
        if not acquired:
            holder_label = {"doctor": "врача", "nurse": "медсестры"}.get(holder)
            suffix = f" у {holder_label}" if holder_label else ""
            CustomMessageBox.warning(
                owner, "Добавление занято", f"Окно добавления пациента уже открыто{suffix}.\nПожалуйста, подождите.",
            )
            return
        owner._add_patient_lock_held = True
        try:
            self._enter_management()
        except Exception as exc:
            self.release_add_patient()
            logger.warning("Failed to open patient management: %s", exc, exc_info=True)
            if self._live_owner() is not None:
                CustomMessageBox.warning(owner, "Добавление пациента", "Не удалось открыть окно добавления пациента.")

    @Slot(object)
    def _lease_failed(self, exc):
        logger.warning("Add-patient lock operation failed: %s", exc, exc_info=exc)
        owner = self._live_owner()
        if owner is not None and self._enter_management is not None and self._lease_context == _context(owner):
            CustomMessageBox.warning(owner, "Добавление пациента", "Не удалось получить блокировку. Повторите попытку.")

    @Slot()
    def _lease_finished(self):
        self._lease = None
        self._enter_management = None
        owner = self._live_owner()
        if owner is not None:
            owner._add_patient_lock_held = False
            owner._apply_add_patient_button_state()


def patient_navigation(owner):
    navigation = getattr(owner, "_async_patient_navigation", None)
    if navigation is None:
        navigation = _PatientNavigation(owner)
        owner._async_patient_navigation = navigation
    return navigation


def cancel_card_date_lookup(owner):
    navigation = getattr(owner, "_async_patient_navigation", None)
    if navigation is not None:
        navigation.cancel_card()


def release_add_patient_lock(owner):
    navigation = getattr(owner, "_async_patient_navigation", None)
    if navigation is not None:
        navigation.release_add_patient()


def add_patient_busy(owner):
    navigation = getattr(owner, "_async_patient_navigation", None)
    return navigation is not None and navigation.add_patient_busy


def quiesce_patient_navigation(owner):
    """Discard pending UI results before the runtime outage dialog opens."""
    if owner is None:
        return
    owner._patient_navigation_quiesced = True
    cancel_card_date_lookup(owner)
    # Leave an already open management view's lease in place while its
    # accepted writes drain. The normal role shutdown releases that lease.
    if not getattr(owner, "_add_patient_lock_held", False):
        release_add_patient_lock(owner)

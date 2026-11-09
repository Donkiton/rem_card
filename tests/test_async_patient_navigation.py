from __future__ import annotations

from datetime import datetime
import threading
from types import MethodType, SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from rem_card.app.role_session_lock import RoleSessionLock
from rem_card.services.shift_service import ShiftService
from rem_card.ui.doctor_view.doctor_remcard_widget import DoctorRemCardWidget
from rem_card.ui.nurse_view.nurse_main_widget import NurseMainWidget
from rem_card.ui.shared.async_patient_navigation import add_patient_busy, quiesce_patient_navigation


HISTORICAL_DATE = datetime(2026, 6, 20, 8)
MANAGEMENT = "patient_bed_management"


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _until(condition, timeout_ms=3000):
    if condition():
        return
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: loop.quit() if condition() else None)
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(loop.quit)
    timer.start()
    timeout.start(timeout_ms)
    loop.exec()
    timer.stop()
    timeout.stop()
    assert condition(), "asynchronous navigation did not reach the expected state"


def _heartbeat():
    ticks = []
    QTimer.singleShot(0, lambda: ticks.append(True))
    _until(lambda: bool(ticks), timeout_ms=500)


class _Window(QWidget):
    def __init__(self, role, service=None, lock=None):
        super().__init__()
        self.role = role
        self.service = self.remcard_service = service
        self.admission_id = 1
        self._current_date = datetime(2026, 6, 19, 8)
        self._patient_open_generation = 0
        self._selection_mode = "card"
        self._is_closing = False
        self._full_layout_created = False
        self._archive_read_only_mode = False
        self._navigation_diagnostics = None
        self._add_patient_lock = lock
        self._add_patient_lock_held = False
        self.loads = []
        self.modes = []
        self.layout_manager = SimpleNamespace(
            current_admission_id=1, set_patient_selection_mode=self.set_mode,
            bottom_row=SimpleNamespace(hide=lambda: None),
        )
        methods = NurseMainWidget if role == "nurse" else DoctorRemCardWidget
        for name in (
            "on_show_card_clicked", "_show_resolved_card_date", "on_add_patient_clicked",
            "_enter_patient_management", "_release_add_patient_lock", "_on_selection_mode_changed",
        ):
            setattr(self, name, MethodType(getattr(methods, name), self))
        if role == "doctor":
            for name in ("on_patient_selected_from_list", "_open_resolved_w1_card"):
                setattr(self, name, MethodType(getattr(methods, name), self))

    def set_mode(self, mode):
        self.modes.append(mode)
        self._on_selection_mode_changed(mode)

    def load_patient_card(self, admission_id, date):
        self.loads.append((admission_id, date, threading.get_ident()))
        self._patient_open_generation += 1

    def safe_load_archived_card(self, date, **_kwargs):
        self.load_patient_card(self.admission_id, date)

    def _is_same_medical_day(self, left, right):
        return ShiftService.get_day_period(left)[0] == ShiftService.get_day_period(right)[0]

    def _apply_add_patient_button_state(self):
        pass

    def _apply_burn_calculator_button_state(self):
        pass

    def _refresh_add_patient_button_lock_state(self):
        pass

    def _exit_archive_read_only_mode(self):
        pass

    def _prime_patient_header_from_w1(self, *_args):
        pass


@pytest.mark.parametrize("route", ["nurse", "doctor", "doctor_w1"])
def test_card_date_lookup_keeps_gui_alive_and_coalesces_repeated_clicks(app, route):
    entered, release = threading.Event(), threading.Event()
    reads = []
    gui_thread = threading.get_ident()

    def has_card(admission, _date):
        reads.append(("has_card", admission, threading.get_ident()))
        entered.set()
        assert release.wait(3)
        return False

    def all_dates(admission):
        reads.append(("dates", admission, threading.get_ident()))
        return [HISTORICAL_DATE]

    service = SimpleNamespace(has_card=has_card, get_all_card_dates=all_dates, get_day_period=ShiftService.get_day_period)
    window = _Window("nurse" if route == "nurse" else "doctor", service)
    if route == "doctor_w1":
        window._selection_mode = "beds"
        patient = SimpleNamespace(id=1)
        def click():
            window.on_patient_selected_from_list(patient, "show")
    else:
        click = window.on_show_card_clicked
    try:
        click()
        _until(entered.is_set)
        click()
        _heartbeat()
        assert len(reads) == 1 and window.loads == []
        release.set()
        _until(lambda: bool(window.loads))
        assert window.loads == [(1, HISTORICAL_DATE, gui_thread)]
        assert [(kind, adm) for kind, adm, _ in reads] == [("has_card", 1), ("dates", 1)]
        assert all(thread != gui_thread for _, _, thread in reads)
    finally:
        release.set()


@pytest.mark.parametrize("role", ["nurse", "doctor"])
def test_changed_patient_replaces_pending_lookup_without_stale_fallback(app, role):
    entered, release = threading.Event(), threading.Event()
    reads = []

    def has_card(admission, _date):
        reads.append(("has_card", admission))
        if admission == 1:
            entered.set()
            assert release.wait(3)
        return False

    def all_dates(admission):
        reads.append(("dates", admission))
        return [HISTORICAL_DATE]

    service = SimpleNamespace(has_card=has_card, get_all_card_dates=all_dates, get_day_period=ShiftService.get_day_period)
    window = _Window(role, service)
    try:
        window.on_show_card_clicked()
        _until(entered.is_set)
        window.admission_id = window.layout_manager.current_admission_id = 2
        window._patient_open_generation += 1
        window.on_show_card_clicked()
        window.on_show_card_clicked()
        release.set()
        _until(lambda: bool(window.loads))
        assert window.loads == [(2, HISTORICAL_DATE, threading.get_ident())]
        assert reads == [("has_card", 1), ("has_card", 2), ("dates", 2)]
    finally:
        release.set()


@pytest.mark.parametrize("role", ["nurse", "doctor"])
@pytest.mark.parametrize("stop", ["mode_change", "runtime_outage"])
def test_cancelled_card_lookup_does_not_open_a_card(app, role, stop):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def has_card(_admission, _date):
        calls.append(True)
        entered.set()
        assert release.wait(3)
        return True

    window = _Window(role, SimpleNamespace(has_card=has_card))
    try:
        window.on_show_card_clicked()
        _until(entered.is_set)
        if stop == "runtime_outage":
            quiesce_patient_navigation(window)
            window.on_show_card_clicked()
        else:
            window.set_mode("beds")
            window.set_mode("card")
        release.set()
        _until(lambda: window._async_patient_navigation._card_worker is None)
        assert calls == [True] and window.loads == []
    finally:
        release.set()


@pytest.mark.parametrize("role", ["nurse", "doctor"])
def test_add_patient_keeps_exclusive_file_lease_and_releases_off_gui(app, tmp_path, monkeypatch, role):
    path = tmp_path / "add_patient.lock"
    lock = RoleSessionLock(str(path), "add_patient_button", role, owner_role=role)
    other = RoleSessionLock(str(path), "add_patient_button", "other")
    monkeypatch.setattr(other, "_is_local_host", lambda _host: True)
    entered, finish_acquire = threading.Event(), threading.Event()
    releasing, finish_release = threading.Event(), threading.Event()
    calls = []
    acquire, release = lock.acquire, lock.release
    gui_thread = threading.get_ident()

    def slow_acquire():
        calls.append(("acquire", threading.get_ident()))
        entered.set()
        assert finish_acquire.wait(3)
        return acquire()

    def slow_release():
        calls.append(("release", threading.get_ident()))
        releasing.set()
        assert finish_release.wait(3)
        release()

    monkeypatch.setattr(lock, "acquire", slow_acquire)
    monkeypatch.setattr(lock, "release", slow_release)
    window = _Window(role, lock=lock)
    window._selection_mode = "beds"
    try:
        window.on_add_patient_clicked()
        _until(entered.is_set)
        window.on_add_patient_clicked()
        _heartbeat()
        assert window.modes == [] and len(calls) == 1
        finish_acquire.set()
        _until(lambda: window._selection_mode == MANAGEMENT)
        assert window._add_patient_lock_held and lock.ownership_context()
        assert not other.acquire()
        assert other.holder_owner_role() == role
        window.set_mode("beds")
        _until(releasing.is_set)
        window.on_add_patient_clicked()
        _heartbeat()
        assert window.modes == [MANAGEMENT, "beds"]
        assert [name for name, _ in calls] == ["acquire", "release"]
        assert all(thread != gui_thread for _, thread in calls)
        finish_release.set()
        _until(lambda: not add_patient_busy(window))
        assert not path.exists() and not window._add_patient_lock_held
    finally:
        finish_acquire.set()
        finish_release.set()
        window._release_add_patient_lock()


@pytest.mark.parametrize("role", ["nurse", "doctor"])
@pytest.mark.parametrize("stop", ["destroyed", "runtime_outage"])
def test_inactive_window_releases_late_acquisition(app, tmp_path, monkeypatch, role, stop):
    path = tmp_path / "late.lock"
    lock = RoleSessionLock(str(path), "add_patient_button", role)
    entered, finish, released = threading.Event(), threading.Event(), threading.Event()
    acquire, release = lock.acquire, lock.release

    def slow_acquire():
        entered.set()
        assert finish.wait(3)
        return acquire()

    def observe_release():
        release()
        released.set()

    monkeypatch.setattr(lock, "acquire", slow_acquire)
    monkeypatch.setattr(lock, "release", observe_release)
    window = _Window(role, lock=lock)
    window._selection_mode = "beds"
    try:
        window.on_add_patient_clicked()
        _until(entered.is_set)
        if stop == "runtime_outage":
            quiesce_patient_navigation(window)
        else:
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            assert not isValid(window)
        finish.set()
        _until(released.is_set)
        if stop == "runtime_outage":
            _until(lambda: not add_patient_busy(window))
            window.on_add_patient_clicked()
            assert not add_patient_busy(window)
        assert not path.exists() and lock.ownership_context() is None
        assert window.modes == []
    finally:
        finish.set()


@pytest.mark.parametrize("role", ["nurse", "doctor"])
@pytest.mark.parametrize("latest", ["add_patient", "show_card"])
def test_latest_patient_navigation_click_wins_when_first_worker_finishes_first(
    app, tmp_path, monkeypatch, role, latest,
):
    path = tmp_path / "competing.lock"
    lock = RoleSessionLock(str(path), "add_patient_button", role)
    card_entered, finish_card = threading.Event(), threading.Event()
    lock_entered, finish_lock = threading.Event(), threading.Event()
    acquire = lock.acquire

    def has_card(_admission, _date):
        card_entered.set()
        assert finish_card.wait(3)
        return True

    def slow_acquire():
        lock_entered.set()
        assert finish_lock.wait(3)
        return acquire()

    monkeypatch.setattr(lock, "acquire", slow_acquire)
    window = _Window(role, SimpleNamespace(has_card=has_card), lock)
    window._selection_mode = "beds"
    def show_card():
        if role == "doctor":
            window.on_patient_selected_from_list(SimpleNamespace(id=1), "show")
        else:
            window.on_show_card_clicked()

    try:
        if latest == "add_patient":
            show_card()
            _until(card_entered.is_set)
            window.on_add_patient_clicked()
            _until(lock_entered.is_set)
            finish_card.set()
            _until(lambda: window._async_patient_navigation._card_worker is None)
            assert window.loads == []
            finish_lock.set()
            _until(lambda: bool(window.modes))
            assert window.modes == [MANAGEMENT] and path.exists()
        else:
            window.on_add_patient_clicked()
            _until(lock_entered.is_set)
            show_card()
            _until(card_entered.is_set)
            finish_lock.set()
            _until(lambda: not add_patient_busy(window) or bool(window.modes))
            assert window.modes == [] and not path.exists()
            finish_card.set()
            _until(lambda: bool(window.loads))
            assert len(window.loads) == 1 and MANAGEMENT not in window.modes
    finally:
        finish_card.set()
        finish_lock.set()
        window._release_add_patient_lock()
        _until(lambda: not add_patient_busy(window) and window._async_patient_navigation._card_worker is None)
    assert not path.exists()


@pytest.mark.parametrize("role", ["nurse", "doctor"])
@pytest.mark.parametrize("action", ["runtime_outage", "show_card"])
def test_open_management_keeps_lease_until_explicit_release(app, tmp_path, role, action):
    path = tmp_path / "active.lock"
    lock = RoleSessionLock(str(path), "add_patient_button", role)
    window = _Window(role, SimpleNamespace(has_card=lambda *_args: True), lock)
    window._selection_mode = "beds"
    try:
        window.on_add_patient_clicked()
        _until(lambda: window._selection_mode == MANAGEMENT)
        ownership = lock.ownership_context()
        assert ownership is not None
        if action == "runtime_outage":
            quiesce_patient_navigation(window)
        else:
            window.on_show_card_clicked()
            _until(lambda: window._async_patient_navigation._card_worker is None)
        _heartbeat()
        assert lock.ownership_context() == ownership
        assert path.exists() and window._add_patient_lock_held
    finally:
        window._release_add_patient_lock()
        _until(lambda: not add_patient_busy(window))
    assert not path.exists()

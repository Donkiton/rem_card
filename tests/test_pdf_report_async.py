from __future__ import annotations

import sys
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QThread, QTimer, Signal
from PySide6.QtWidgets import QApplication, QWidget
from pypdf import PdfReader
from shiboken6 import isValid

from rem_card.ui.shared import pdf_opener, report_controller
from rem_card.ui.shared.pdf_build_worker import PdfBuildWorker
from rem_card.ui.shared.pdf_report_jobs import ReportDataWorker, report_jobs, shutdown_report_jobs
from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.rem_card_sectors import sector_print
from rem_card.ui.rem_card_sectors.s_print.builder import ReportBuilder
from rem_card.ui.doctor_view.components import beds_selection_widget as doctor_beds
from rem_card.ui.nurse_view.components import nurse_beds_selection_widget as nurse_beds


def _pump_until(predicate, timeout_ms=4000):
    if predicate():
        return
    loop = QEventLoop()
    poll = QTimer()
    poll.setInterval(5)
    poll.timeout.connect(lambda: loop.quit() if predicate() else None)
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    poll.start()
    deadline.start(timeout_ms)
    loop.exec()
    poll.stop()
    deadline.stop()
    assert predicate(), "background PDF operation did not reach the expected state"


def _pump_for(milliseconds):
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def report_fixture(app, monkeypatch, tmp_path):
    del app
    data = {
        "patient_name": "Synthetic Report",
        "start_dt": datetime(2026, 10, 29, 8),
        "end_dt": datetime(2026, 10, 30, 8),
        "icu_day": 1,
    }
    requests = []
    built = []
    opened = []
    notices = []
    directory = tmp_path / "reports"
    service = SimpleNamespace(has_card=lambda *_: True, get_all_card_dates=lambda *_: [data["start_dt"]])

    class DailyCollector(QThread):
        finished = Signal(dict)
        error = Signal(str)

        def __init__(self, _service, admission_id, _date, _config):
            super().__init__()
            requests.append(admission_id)

        def run(self):
            self.finished.emit(dict(data))

    class FullCollector(QThread):
        finished = Signal(list)
        error = Signal(str)

        def __init__(self, _service, admission_id, _dates, _config):
            super().__init__()
            requests.append(admission_id)

        def run(self):
            self.finished.emit([dict(data)])

    def build(payload, config, path):
        built.append((payload, dict(config), Path(path), threading.get_ident()))
        Path(path).write_bytes(b"synthetic PDF payload")

    def open_file(path, *, parent=None):
        opened.append((Path(path), parent, threading.get_ident()))
        return True

    monkeypatch.setattr(sector_print, "DataCollectorWorker", DailyCollector)
    monkeypatch.setattr(sector_print, "FullReportWorker", FullCollector)
    monkeypatch.setattr(sector_print.PrintConfig, "load", lambda _self: {"synthetic": True})
    monkeypatch.setattr(ReportBuilder, "build_pdf", staticmethod(build))
    monkeypatch.setattr(report_controller, "REPORT_DIR", str(directory))
    from rem_card.app import paths

    monkeypatch.setattr(paths, "REPORT_DIR", str(directory))
    for module in (report_controller, doctor_beds, nurse_beds):
        monkeypatch.setattr(module, "open_pdf_file", open_file)
        for name in ("information", "warning", "critical"):
            monkeypatch.setattr(module.CustomMessageBox, name,
                                lambda *args, _kind=name: notices.append((_kind, threading.get_ident())))

    def make(route):
        if route == "controller":
            parent = QWidget()
            parent._is_closing = False
            target = report_controller.RemCardReportController(service, parent)
            parent.report_controller = target
        else:
            source = doctor_beds.BedsSelectionWidget if route == "doctor_w1" else nurse_beds.NurseBedsSelectionWidget
            names = (
                "on_daily_report_requested", "on_full_report_requested",
                "_on_daily_report_collected", "_on_full_report_collected",
                "_start_daily_pdf_worker", "_start_full_pdf_worker",
                "_on_daily_pdf_ready", "_on_full_pdf_ready",
                "_on_daily_pdf_error", "_on_full_pdf_error",
                "_on_daily_report_error", "_on_full_report_error",
                "_clear_daily_pdf_worker", "_clear_full_pdf_worker",
            )
            methods = {name: getattr(source, name) for name in names}
            methods["_pdf_patient_name"] = staticmethod(source._pdf_patient_name)
            cls = type(f"ReportHarness_{route}", (QWidget,), methods)
            target = parent = cls()
            target._is_closing = False
            target.remcard_service = service
            target.daily_worker = target.report_worker = None
            target.daily_pdf_worker = target.full_pdf_worker = None
            target._daily_report_config = target._full_report_config = None

        def request(kind, admission_id=1):
            if route == "controller":
                if kind == "daily":
                    target.run_daily_report(admission_id, data["start_dt"])
                else:
                    target.run_full_report(admission_id)
            else:
                method = target.on_daily_report_requested if kind == "daily" else target.on_full_report_requested
                method(SimpleNamespace(id=admission_id))

        return parent, target, request

    return SimpleNamespace(data=data, requests=requests, built=built, opened=opened,
                           notices=notices, directory=directory, make=make)


@pytest.mark.parametrize("route", ["controller", "doctor_w1", "nurse_w1"])
@pytest.mark.parametrize("kind", ["daily", "full"])
def test_report_filesystem_wait_keeps_gui_responsive_and_single_flight(report_fixture, monkeypatch, route, kind):
    fixture = report_fixture
    parent, target, request = fixture.make(route)
    entered, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    mkdir_calls = []
    metadata_calls = []
    original_mkdir = Path.mkdir
    original_exists, original_stat = Path.exists, Path.stat

    def mkdir(path, *args, **kwargs):
        if path == fixture.directory:
            mkdir_calls.append(threading.get_ident())
            entered.set()
            assert release.wait(3)
        return original_mkdir(path, *args, **kwargs)

    def exists(path):
        if path.suffix == ".pdf":
            metadata_calls.append(("exists", threading.get_ident()))
        return original_exists(path)

    def stat(path, *args, **kwargs):
        if path.suffix == ".pdf":
            metadata_calls.append(("stat", threading.get_ident()))
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(Path, "stat", stat)
    ticks = []
    heartbeat = QTimer()
    heartbeat.setInterval(5)
    heartbeat.timeout.connect(lambda: ticks.append(1))
    heartbeat.start()
    try:
        request(kind)
        _pump_until(entered.is_set)
        request(kind, admission_id=2)
        _pump_for(70)
        assert len(ticks) >= 3
        assert fixture.requests == [1]
        assert mkdir_calls == [mkdir_calls[0]] and mkdir_calls[0] != main_thread
        release.set()
        _pump_until(lambda: len(fixture.opened) == 1)
        _pump_until(lambda: not report_jobs(target).active(kind))
        _pump_until(lambda: not target.daily_worker.isRunning() if kind == "daily" else
                    not (getattr(target, "full_worker", None) or target.report_worker).isRunning())
        assert len(fixture.built) == 1
        assert fixture.built[0][3] != main_thread
        assert fixture.opened[0][2] == main_thread
        expected = fixture.data if kind == "daily" else [fixture.data]
        assert fixture.built[0][0] == expected
        assert metadata_calls == []  # Completed GUI slots must not probe SMB again.
    finally:
        release.set()
        heartbeat.stop()
        shutdown_report_jobs(parent)


@pytest.mark.parametrize("route", ["controller", "doctor_w1", "nurse_w1"])
@pytest.mark.parametrize("kind", ["daily", "full"])
def test_directory_error_is_delivered_on_gui_and_request_can_be_retried(report_fixture, monkeypatch, route, kind):
    fixture = report_fixture
    parent, target, request = fixture.make(route)
    original_mkdir = Path.mkdir
    attempts = []

    def mkdir(path, *args, **kwargs):
        if path == fixture.directory:
            attempts.append(threading.get_ident())
            if len(attempts) == 1:
                raise PermissionError("synthetic denied directory")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    request(kind)
    _pump_until(lambda: any(item[0] == "critical" for item in fixture.notices))
    assert fixture.opened == []
    assert not report_jobs(target).active(kind)
    request(kind)
    _pump_until(lambda: len(fixture.opened) == 1)
    assert len(attempts) == 2 and len(fixture.built) == 1
    assert all(thread != threading.get_ident() for thread in attempts)
    assert all(thread == threading.get_ident() for _, thread in fixture.notices)
    shutdown_report_jobs(parent)


@pytest.mark.parametrize("route", ["controller", "doctor_w1", "nurse_w1"])
@pytest.mark.parametrize("kind", ["daily", "full"])
def test_close_during_directory_wait_retains_worker_and_suppresses_late_result(report_fixture, monkeypatch, route, kind):
    fixture = report_fixture
    parent, target, request = fixture.make(route)
    entered, release = threading.Event(), threading.Event()
    original_mkdir = Path.mkdir

    def mkdir(path, *args, **kwargs):
        if path == fixture.directory:
            entered.set()
            assert release.wait(3)
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    try:
        request(kind)
        _pump_until(entered.is_set)
        worker = target.daily_pdf_worker if kind == "daily" else target.full_pdf_worker
        parent._is_closing = True
        shutdown_report_jobs(parent)
        parent.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        assert not isValid(parent)
        assert worker.isRunning()
        release.set()
        _pump_until(lambda: not worker.isRunning())
        _pump_for(25)
        assert fixture.built == [] and fixture.opened == [] and fixture.notices == []
    finally:
        release.set()


def test_open_pdf_checks_and_shell_launch_do_not_block_gui(app, monkeypatch, tmp_path):
    del app
    path = tmp_path / "report.pdf"
    path.write_bytes(b"pdf")
    parent = QWidget()
    entered, release = threading.Event(), threading.Event()
    launched = []
    calls = []
    original_exists = Path.exists

    def exists(value):
        if value == path:
            calls.append(threading.get_ident())
        return original_exists(value)

    def launch(value):
        launched.append((value, threading.get_ident()))
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(pdf_opener, "_launch_pdf", launch)
    try:
        assert pdf_opener.open_pdf_file(path, parent=parent) is True
        _pump_until(entered.is_set)
        marker = []
        QTimer.singleShot(0, lambda: marker.append(True))
        _pump_until(lambda: bool(marker))
        assert calls == [calls[0]] and calls[0] != threading.get_ident()
        assert launched[0][1] != threading.get_ident()
    finally:
        release.set()
        _pump_until(lambda: not pdf_opener._open_requests)


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows ShellExecute/COM integration")
def test_windows_shell_launch_uses_background_com_apartment(app, monkeypatch, tmp_path):
    del app
    path = tmp_path / "report.pdf"
    path.write_bytes(b"pdf")
    entered, release = threading.Event(), threading.Event()
    calls = []

    def startfile(value):
        calls.append((value, threading.get_ident()))
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(pdf_opener.os, "startfile", startfile)
    try:
        assert pdf_opener.open_pdf_file(path)
        _pump_until(entered.is_set)
        assert calls == [(str(path), calls[0][1])]
        assert calls[0][1] != threading.get_ident()
        marker = []
        QTimer.singleShot(0, lambda: marker.append(True))
        _pump_until(lambda: bool(marker))
    finally:
        release.set()
        _pump_until(lambda: not pdf_opener._open_requests)


def test_collector_adapter_is_retained_until_actual_run_returns(app):
    del app
    entered, release = threading.Event(), threading.Event()

    class Collector(QThread):
        finished = Signal(dict)
        error = Signal(str)

        def run(self):
            self.finished.emit({})
            entered.set()
            assert release.wait(3)

    parent = QWidget()
    worker = ReportDataWorker(Collector())
    jobs = report_jobs(parent)
    assert jobs.begin("daily")
    jobs.track("daily", worker)
    try:
        worker.start()
        _pump_until(entered.is_set)
        jobs.finish("daily")
        parent.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        assert worker in AsyncCallThread._keepalive_threads
        assert not worker.collector.isRunning()  # No native QThread was started.
        release.set()
        _pump_until(lambda: worker not in AsyncCallThread._keepalive_threads)
        assert not worker.isRunning()
    finally:
        release.set()
        worker.wait(3000)


@pytest.mark.parametrize("operation", ["collector", "mkdir", "shell"])
def test_process_exits_cleanly_while_report_operation_is_blocked(tmp_path, operation):
    # A QWidget-only close test does not catch native QThread destruction
    # during interpreter/Qt shutdown. Exercise the actual process boundary.
    code = r'''
import sys, threading
from pathlib import Path
from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import QApplication, QWidget
from rem_card.ui.shared.pdf_report_jobs import ReportDataWorker
from rem_card.ui.shared.pdf_build_worker import PdfBuildWorker
from rem_card.ui.shared import pdf_opener
app = QApplication([])
parent = QWidget()
entered = threading.Event()
blocked = threading.Event()
def block(*_args, **_kwargs):
    entered.set()
    blocked.wait(30)
mode, folder = sys.argv[1:]
if mode == 'collector':
    class Collector(QThread):
        finished = Signal(dict)
        error = Signal(str)
        def run(self):
            block()
    worker = ReportDataWorker(Collector())
    worker.start()
elif mode == 'mkdir':
    Path.mkdir = block
    worker = PdfBuildWorker({}, {}, Path(folder) / 'report.pdf', parent=parent)
    worker.start()
else:
    path = Path(folder) / 'report.pdf'
    path.write_bytes(b'pdf')
    pdf_opener._launch_pdf = block
    pdf_opener.open_pdf_file(path, parent=parent)
poll = QTimer()
def exit_when_blocked():
    if entered.is_set():
        print('EXIT_WHILE_BLOCKED', flush=True)
        app.quit()
poll.timeout.connect(exit_when_blocked)
poll.start(5)
QTimer.singleShot(5000, app.quit)
app.exec()
sys.exit(0 if entered.is_set() else 2)
'''
    result = subprocess.run([sys.executable, "-c", code, operation, str(tmp_path)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", timeout=12)
    assert result.returncode == 0, result.stdout
    assert "EXIT_WHILE_BLOCKED" in result.stdout


def test_close_during_file_check_cancels_open_and_missing_file_error_is_async(app, monkeypatch, tmp_path):
    del app
    parent = QWidget()
    path = tmp_path / "missing.pdf"
    entered, release = threading.Event(), threading.Event()
    launched, warnings = [], []
    original_exists = Path.exists

    def exists(value):
        if value == path:
            entered.set()
            assert release.wait(3)
            return True
        return original_exists(value)

    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(pdf_opener, "_launch_pdf", launched.append)
    monkeypatch.setattr(pdf_opener.CustomMessageBox, "warning", lambda *_: warnings.append(threading.get_ident()))
    try:
        assert pdf_opener.open_pdf_file(path, parent=parent)
        _pump_until(entered.is_set)
        shutdown_report_jobs(parent)
        release.set()
        _pump_until(lambda: not pdf_opener._open_requests)
        assert launched == [] and warnings == []
    finally:
        release.set()
    monkeypatch.setattr(Path, "exists", original_exists)
    assert pdf_opener.open_pdf_file(path, parent=parent)
    _pump_until(lambda: len(warnings) == 1)
    _pump_until(lambda: not pdf_opener._open_requests)
    assert warnings == [threading.get_ident()]


@pytest.mark.parametrize("full", [False, True])
def test_worker_preserves_real_pdf_content(app, tmp_path, full):
    del app
    data = {"patient_name": "Synthetic report", "start_dt": datetime(2026, 10, 29, 8),
            "end_dt": datetime(2026, 10, 30, 8), "icu_day": 1}
    payload = [data, {**data, "start_dt": data["start_dt"] + timedelta(days=1),
                       "end_dt": data["end_dt"] + timedelta(days=1)}] if full else data
    baseline, actual = tmp_path / "baseline.pdf", tmp_path / "actual.pdf"
    ReportBuilder.build_pdf(payload, {}, baseline)
    worker = PdfBuildWorker(payload, {}, actual)
    completed, errors = [], []
    worker.completed.connect(completed.append)
    worker.error.connect(errors.append)
    worker.start()
    _pump_until(lambda: bool(completed) or bool(errors))
    _pump_until(lambda: not worker.isRunning())
    assert not errors and completed == [actual]
    left, right = PdfReader(baseline), PdfReader(actual)
    assert len(left.pages) == len(right.pages)
    assert [page.extract_text() for page in left.pages] == [page.extract_text() for page in right.pages]

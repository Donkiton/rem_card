from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtCore import QEvent, QObject  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QStackedWidget, QWidget  # noqa: E402

from rem_card.ui.archive_center.archive_main_widget import ArchiveMainWidget  # noqa: E402
from rem_card.ui.analytics.graphs_catalog import GRAPH_GROUPS  # noqa: E402
from rem_card.ui.archive_center.statistics_page import ArchiveStatisticsPage  # noqa: E402
from rem_card.ui.doctor_view.archive_widget import ARCHIVE_MODE_OPERBLOCK, ARCHIVE_MODE_RAO, ArchiveWidget  # noqa: E402
from rem_card.ui.nurse_view.nurse_main_widget import NurseMainWidget  # noqa: E402
from rem_card.ui.operblock_view.operblock_main_widget import OperBlockMainWidget  # noqa: E402
from rem_card.ui.shared.lightweight_w1_shell import LightweightW1Shell  # noqa: E402


class _PatientService:
    def get_archived_patients_page(self, **_kwargs):
        return {"records": [], "total_count": 0, "page": 1, "page_size": 50}


class _OperblockService:
    db = object()

    def __init__(self):
        self.last_page_kwargs = None

    def list_archived_operation_cases_page(self, **_kwargs):
        self.last_page_kwargs = dict(_kwargs)
        return {"records": [], "total_count": 0, "page": 1, "page_size": 50}


def application() -> QApplication:
    return QApplication.instance() or QApplication([])


def _center(role: str) -> ArchiveMainWidget:
    app = application()
    widget = ArchiveMainWidget(
        _PatientService(),
        role=role,
        allow_edit=role == "doctor",
        operblock_service=_OperblockService(),
    )
    widget.resize(1280, 720)
    widget.show()
    app.processEvents()
    return widget


def test_archive_center_has_separate_fixed_destinations_and_shell():
    widget = _center("doctor")
    try:
        assert [button.text() for button in widget.navigation_buttons] == [
            "Архив реанимации",
            "Архив оперблока",
            "Статистика РАО",
            "Аналитика",
            "Графики реанимации",
            "Статистика оперблока",
        ]
        assert all(button.isVisible() for button in widget.navigation_buttons)
        assert widget.navigation_buttons[0].isChecked()
        assert not hasattr(widget, "btn_back")
        assert widget.page_title.text() == "Архив пациентов реанимации"
        assert widget.surface_frame.objectName() == "ArchiveCenterFrame"
        brand_card = widget.findChild(QWidget, "SettingsBrandCard")
        assert brand_card is not None
        assert brand_card.layout().spacing() == 10
        assert widget.layout().contentsMargins().left() == 0
        assert widget.layout().contentsMargins().top() == 5
        assert widget.layout().contentsMargins().right() == 5
        assert widget.layout().contentsMargins().bottom() == 4
        assert "border-radius: 5px" in widget.styleSheet()
        assert widget.minimumSizeHint().width() <= 1120
        assert widget.rao_archive.archive_source_mode == ARCHIVE_MODE_RAO
        assert widget.operblock_archive.archive_source_mode == ARCHIVE_MODE_OPERBLOCK
        assert widget.rao_archive.frame.objectName() == "ArchiveDataPanel"
        assert widget.rao_archive.table.objectName() == "ArchiveDataTable"
        assert widget.rao_archive.pagination_frame.objectName() == "ArchivePaginationBar"
        assert widget.rao_archive.actions_frame.objectName() == "ArchiveActionsBar"
        assert widget.rao_archive is not widget.operblock_archive
        assert widget.rao_statistics is not widget.operblock_statistics
        assert widget.rao_analysis.analytics_workspace is widget.rao_statistics.analytics_workspace
        assert widget.operblock_analysis.analytics_workspace is widget.operblock_statistics.analytics_workspace
        assert widget.width() >= 1280
    finally:
        widget.close()


def test_nurse_center_keeps_archive_read_only_but_statistics_available():
    widget = _center("nurse")
    try:
        for page in (widget.rao_archive, widget.operblock_archive):
            assert not page.allow_edit
            assert page.btn_edit.isHidden()
            assert page.btn_delete_last.isHidden()
            assert page.btn_delete.isHidden()
        loaded = []
        widget.operblock_statistics.ensure_loaded = lambda: loaded.append(True)
        widget.select_destination(5)
        assert widget.content_stack.currentWidget() is widget.operblock_statistics
        assert widget.page_title.text() == "Статистика оперблока"
        assert loaded == [True]
        assert widget.operblock_statistics.btn_refresh.isVisible()
        assert widget.operblock_statistics.btn_refresh.text() == "Отсортировать"
        assert widget.operblock_statistics.btn_save_pdf.text() == "Сохранить PDF"
        assert widget.operblock_statistics.date_from.date().year() == 2000
        assert all(checkbox.isChecked() for checkbox in widget.operblock_statistics.checkboxes.values())
    finally:
        widget.close()


def test_lightweight_shell_uses_the_same_center_for_both_roles():
    application()
    for role in ("doctor", "nurse"):
        shell = LightweightW1Shell(
            role=role,
            patient_service=_PatientService(),
            operblock_service=_OperblockService(),
        )
        try:
            archive = shell._ensure_archive_widget()
            assert isinstance(archive, ArchiveMainWidget)
            assert archive.operblock_service is not None
            assert archive.allow_edit is (role == "doctor")
        finally:
            shell.close()


def test_both_operating_room_roles_build_the_common_unfiltered_archive_center():
    application()

    class Host:
        def __init__(self, table_code):
            self.patient_service = _PatientService()
            self.remcard_service = SimpleNamespace(data_service=None)
            self.operblock_service = _OperblockService()
            self.stack = QStackedWidget()
            self._table_filter_code = table_code
            self._creating_lazy_archive_page = True

        @staticmethod
        def is_view_only_mode():
            return False

        @staticmethod
        def _open_case_from_unified_archive(_case):
            return None

        @staticmethod
        def _on_rao_case_selected_in_operblock_archive(_patient):
            return None

    hosts = []
    centers = []
    try:
        for table_code in ("emergency", "planned"):
            host = Host(table_code)
            hosts.append(host)
            center = OperBlockMainWidget._build_archive_page(host)
            centers.append(center)
            assert isinstance(center, ArchiveMainWidget)
            assert center.role == "operblock"
            margins = center.layout().contentsMargins()
            assert (margins.left(), margins.right()) == (5, 5)
            assert center.content_stack.currentWidget() is center.operblock_archive
            assert center.navigation_buttons[1].isChecked()
            assert center.operblock_archive.table_filter.currentData() is None
            assert not center.rao_archive.allow_edit
            assert center.operblock_archive.allow_edit
            badge = center.findChild(QLabel, "ArchiveCenterRoleBadge")
            assert badge is not None
            assert badge.text() == "Оперблок"
    finally:
        for center in centers:
            center.close()
        for host in hosts:
            host.stack.close()


def test_operblock_archive_case_returns_to_the_common_center_on_back():
    archive_page = QWidget()
    protocol_page = QWidget()
    board_page = QWidget()
    stack = QStackedWidget()
    for page in (board_page, archive_page, protocol_page):
        stack.addWidget(page)

    class Host:
        def __init__(self):
            self._archive_return_operation_case_id = 44
            self._protocol_opened_from_archive = False
            self.archive_page = archive_page
            self.protocol_page = protocol_page
            self.board_page = board_page
            self.settings_page = None
            self.stack = stack
            self.opened_case_id = None
            self.refreshed = False

        @staticmethod
        def is_view_only_mode():
            return False

        def _open_protocol(self, case_id):
            self.opened_case_id = case_id
            self.stack.setCurrentWidget(self.protocol_page)

        @staticmethod
        def _set_protocol_chrome(_enabled):
            return None

        def refresh_operblock_archive(self, **_kwargs):
            self.refreshed = True

        @staticmethod
        def _show_board():
            raise AssertionError("Возврат должен вести в единый архив, а не на доску операционной")

    host = Host()
    OperBlockMainWidget._open_case_from_unified_archive(host, {"operation_case_id": 17})
    assert host.opened_case_id == 17
    assert host._archive_return_operation_case_id is None
    assert host._protocol_opened_from_archive

    OperBlockMainWidget.on_back_clicked(host)
    assert stack.currentWidget() is archive_page
    assert host.refreshed
    assert not host._protocol_opened_from_archive
    stack.close()
    stack.deleteLater()
    application().processEvents()


def test_statistics_period_is_forwarded_to_archive_db_discovery():
    class PatientService:
        def __init__(self):
            self.calls = []

        def get_archive_db_paths_for_period(self, start_dt, end_dt):
            self.calls.append((start_dt, end_dt))
            return []

    application()
    service = PatientService()
    archive_page = ArchiveWidget(service, fixed_source_mode=ARCHIVE_MODE_RAO, embedded=True)
    archive_page.date_from.setDate(archive_page.date_from.date().addYears(-10))
    archive_page.date_to.setDate(archive_page.date_from.date().addDays(1))
    page = ArchiveStatisticsPage(source_mode="rao", archive_page=archive_page)
    page.date_from.setDate(page.date_from.date().addDays(7))
    page.date_to.setDate(page.date_from.date().addDays(3))
    start = page.date_from.date().toString("yyyy-MM-dd")
    end = page.date_to.date().toString("yyyy-MM-dd")
    try:
        assert page._archive_db_paths(f"{start} 00:00:00", f"{end} 23:59:59") == []
        assert service.calls == [(f"{start} 00:00:00", f"{end} 23:59:59")]
    finally:
        page.close()
        archive_page.close()


def test_archive_filters_are_compact_ordered_and_operblock_table_is_forwarded():
    application()
    service = _OperblockService()
    page = ArchiveWidget(
        _PatientService(),
        operblock_service=service,
        fixed_source_mode=ARCHIVE_MODE_OPERBLOCK,
        embedded=True,
    )
    page.show()
    application().processEvents()
    try:
        layout = page.filters_frame.layout()
        assert layout.getItemPosition(layout.indexOf(page.search_name)) == (0, 0, 1, 3)
        assert layout.getItemPosition(layout.indexOf(page.search_ib)) == (0, 3, 1, 1)
        assert layout.getItemPosition(layout.indexOf(page.search_diag)) == (0, 4, 1, 3)
        assert page.date_from.width() == page.date_to.width() == 132
        assert page.date_from.displayFormat() == "dd.MM.yyyy"
        assert page.date_from.calendarWidget().objectName() == "ArchiveCalendar"
        assert page.table_filter.isVisible()
        page.table_filter.setCurrentIndex(1)
        page._load_operblock_archive_page(
            "2026-01-01 00:00:00",
            "2026-02-01 00:00:00",
            1,
            page_size=50,
            search_name="",
            search_ib="",
            search_diag="",
            table_code=page.table_filter.currentData(),
        )
        assert service.last_page_kwargs["table_code"] == "emergency"
    finally:
        page.close()


def test_first_archive_open_does_not_show_transient_button_windows():
    class Watch(QObject):
        def __init__(self):
            super().__init__()
            self.transient_buttons = []

        def eventFilter(self, obj, event):
            if event.type() == QEvent.Show and isinstance(obj, QPushButton) and obj.isWindow():
                self.transient_buttons.append(obj)
            return False

    app = application()
    watch = Watch()
    app.installEventFilter(watch)
    shell = LightweightW1Shell(
        role="doctor",
        patient_service=_PatientService(),
        operblock_service=_OperblockService(),
    )
    shell.resize(1280, 720)
    shell.show()
    app.processEvents()
    try:
        shell.set_patient_selection_mode("archive")
        for _ in range(10):
            app.processEvents()
        assert watch.transient_buttons == []
    finally:
        shell.close()
        app.removeEventFilter(watch)


def test_statistics_first_open_builds_full_report_automatically(monkeypatch):
    from rem_card.ui.archive_center import statistics_page as module

    class ArchivePage:
        def get_analytics_db_paths(self, _start, _end):
            return []

    class RemcardService:
        pass

    selected_calls = []
    monkeypatch.setattr(module, "get_analytics_base_manager", lambda **_kwargs: object())
    monkeypatch.setattr(module, "resolve_readonly_analytics_manager", lambda *_args, **_kwargs: (object(), None))
    monkeypatch.setattr(
        module,
        "build_detailed_statistics_report_html",
        lambda _manager, _start, _end, selected, **kwargs: selected_calls.append(
            (list(selected), kwargs.get("include_recovery_beds"))
        )
        or "<html><body>READY</body></html>",
    )

    app = application()
    page = ArchiveStatisticsPage(
        source_mode="rao",
        remcard_service=RemcardService(),
        archive_page=ArchivePage(),
    )
    try:
        page.ensure_loaded()
        deadline = time.monotonic() + 2.0
        while not page._loaded and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert page._loaded
        assert selected_calls == [(list(page.checkboxes), False)]
        assert "READY" in page.report.toPlainText()
        assert page.date_from.date().toString("yyyy-MM-dd") == "2000-01-01"
        assert not page.chk_include_recovery.isChecked()
    finally:
        page.shutdown()
        page.close()


def test_graphs_are_a_separate_page_with_the_full_existing_catalog():
    widget = _center("doctor")
    try:
        widget.select_destination(4)
        assert widget.content_stack.currentWidget() is widget.rao_graphs
        assert widget.page_title.text() == "Графики реанимации"
        assert len(widget.rao_graphs.checkboxes) == sum(len(items) for items in GRAPH_GROUPS.values())
        assert len(widget.rao_graphs.checkboxes) >= 65
        assert widget.rao_graphs.btn_preview.text() == "Показать графики"
        assert not hasattr(widget.rao_statistics, "btn_graphs")
    finally:
        widget.close()


def test_analysis_has_separate_full_height_pages_and_does_not_squeeze_statistics():
    app = application()
    widget = _center("doctor")
    try:
        widget.select_destination(2)
        app.processEvents()
        assert widget.content_stack.currentWidget() is widget.rao_statistics
        assert not widget.rao_statistics.analytics_workspace.isVisible()
        assert widget.rao_statistics.report.height() > 350

        widget.select_destination(3)
        app.processEvents()
        assert widget.content_stack.currentWidget() is widget.analytics
        assert widget.page_title.text() == "Аналитика"
        assert widget.analytics.stack.currentWidget() is widget.rao_analysis
        assert widget.rao_analysis.include_recovery.isVisible()
        widget.rao_analysis.include_recovery.setChecked(True)
        assert widget.rao_statistics.chk_include_recovery.isChecked()
        assert widget.rao_analysis.analytics_workspace.isVisible()
        assert widget.rao_analysis.analytics_workspace.height() > 350

        widget.analytics.select_scope(1)
        app.processEvents()
        assert widget.content_stack.currentWidget() is widget.analytics
        assert widget.analytics.stack.currentWidget() is widget.operblock_analysis
        assert widget.analytics.btn_operblock.isChecked()
        assert not widget.operblock_analysis.include_recovery.isVisible()
        assert widget.operblock_analysis.analytics_workspace.isVisible()
        assert widget.operblock_analysis.analytics_workspace.height() > 350
    finally:
        widget.shutdown()
        widget.close()


def test_graphs_page_uses_existing_real_graph_builder_and_recovery_filter(monkeypatch):
    from rem_card.ui.archive_center import graphs_page as module

    calls = []
    cleaned = []
    monkeypatch.setattr(module, "get_analytics_base_manager", lambda **_kwargs: "base")
    monkeypatch.setattr(
        module,
        "resolve_readonly_analytics_manager",
        lambda *_args, **_kwargs: ("readonly", lambda: cleaned.append(True)),
    )
    monkeypatch.setattr(
        module,
        "build_graphs_html",
        lambda manager, start, end, selected, colors, **kwargs: calls.append(
            (manager, start, end, tuple(selected), bool(colors), kwargs["include_recovery_beds"])
        )
        or SimpleNamespace(html="", image_paths=[]),
    )

    page = module.ArchiveGraphsPage(remcard_service=object())
    try:
        selected = page._selected_keys()
        page._build_graphs("2026-01-01", "2026-01-31", selected, True)
        assert calls == [("readonly", "2026-01-01", "2026-01-31", tuple(selected), True, True)]
        assert cleaned == [True]
    finally:
        page.shutdown()
        page.close()


def test_nurse_partial_operblock_viewer_cleanup_is_non_navigating_and_single_close():
    class Viewer:
        def __init__(self):
            self.shutdown_calls = 0
            self.deleted = False

        def shutdown(self):
            self.shutdown_calls += 1

        def deleteLater(self):
            self.deleted = True

    class Stack:
        def __init__(self):
            self.removed = []

        def removeWidget(self, widget):
            self.removed.append(widget)

    class DbManager:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    owner = NurseMainWidget.__new__(NurseMainWidget)
    viewer = Viewer()
    db_manager = DbManager()
    owner._operblock_archive_viewer = viewer
    owner._operblock_archive_db_manager = db_manager
    owner.main_stack = Stack()

    NurseMainWidget._discard_partial_operblock_archive_viewer(owner, viewer, db_manager)

    assert owner._operblock_archive_viewer is None
    assert owner._operblock_archive_db_manager is None
    assert owner.main_stack.removed == [viewer]
    assert viewer.shutdown_calls == 1
    assert viewer.deleted
    assert db_manager.close_calls == 1

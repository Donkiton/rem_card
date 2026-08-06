from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from rem_card.app.db_cycle_registry import DbCycleInfo  # noqa: E402
from rem_card.services.database_info_service import (  # noqa: E402
    BackupInfo,
    DatabaseHistoryEvent,
    DatabaseInfo,
    DatabaseInfoCollectionCancelled,
    DatabaseInfoSnapshot,
)
from rem_card.ui.admin_view.admin_main_widget import AdminMainWidget  # noqa: E402
from rem_card.ui.admin_view.database_info_dialog import DatabaseInfoDialog  # noqa: E402


def application() -> QApplication:
    return QApplication.instance() or QApplication([])


def _snapshot() -> DatabaseInfoSnapshot:
    created = datetime(2026, 7, 1, 8, 0)
    database = DatabaseInfo(
        key="medical",
        title="Основная медицинская БД",
        kind="Рабочая БД",
        path=r"C:\data\rao_journal.db",
        exists=True,
        size_bytes=2 * 1024 * 1024,
        created_at=created,
        modified_at=datetime(2026, 7, 2, 9, 30),
        status="Доступна",
        schema_version="17",
        detail="Текущий цикл",
    )
    cycle = DbCycleInfo(
        path=database.path,
        display_name="Текущая БД",
        is_current=True,
        exists=True,
        size_bytes=database.size_bytes,
        modified_at=database.modified_at,
        created_at=database.created_at,
        cycle_started_at=database.created_at,
        min_admission_datetime=datetime(2026, 7, 1, 9, 0),
        max_admission_datetime=datetime(2026, 7, 2, 10, 0),
        patient_count=3,
        admission_count=4,
        transferred_count=1,
        death_count=0,
        active_beds=2,
        quick_check_ok=True,
        validation_message="ok",
        schema_revision="17",
        age_days=1.0,
        days_until_rotation=179.0,
    )
    backup = BackupInfo(
        scope="Основная БД",
        kind="Ручной",
        path=r"C:\data\backups\rao_journal_manual.db",
        size_bytes=1024 * 1024,
        created_at=datetime(2026, 7, 2, 11, 0),
        modified_at=datetime(2026, 7, 2, 11, 0),
        validation_status="Проверен",
        source="manual_primary_db_backup",
        sha256="abc",
        metadata_path=r"C:\data\backups\rao_journal_manual.db.meta.json",
        metadata_error="",
    )
    event = DatabaseHistoryEvent(
        occurred_at=backup.created_at,
        event_type="Бэкап",
        title="Ручной: Основная БД",
        description=backup.source,
        size_bytes=backup.size_bytes,
        status=backup.validation_status,
        path=backup.path,
    )
    return DatabaseInfoSnapshot(
        collected_at=datetime(2026, 7, 2, 11, 5),
        databases=(database,),
        cycles=(cycle,),
        backups=(backup,),
        events=(event,),
        warnings=(),
    )


def _process_events_until(app: QApplication, predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    app.processEvents()
    return bool(predicate())


def test_dialog_renders_summary_tabs_filters_and_paths():
    app = application()
    dialog = DatabaseInfoDialog(snapshot_loader=_snapshot, auto_load=False)
    dialog._apply_snapshot(_snapshot())

    assert dialog.tabs.count() == 4
    assert dialog.database_table.rowCount() == 1
    assert dialog.cycle_table.rowCount() == 1
    assert dialog.backup_table.rowCount() == 1
    assert dialog.history_table.rowCount() == 1
    assert dialog.database_count_value.text() == "1 из 1"
    assert dialog.backup_count_value.text() == "1"
    assert dialog.backup_size_value.text() == "1.0 МБ"
    assert dialog.latest_backup_value.text() == "02.07.2026"

    dialog.backup_filter.setCurrentIndex(2)
    assert dialog.backup_table.rowCount() == 0
    dialog.backup_filter.setCurrentIndex(1)
    assert dialog.backup_table.rowCount() == 1
    dialog.backup_table.setCurrentCell(0, 0)
    assert "rao_journal_manual.db" in dialog.path_label.text()
    dialog.close()
    app.processEvents()


def test_dialog_collects_snapshot_outside_gui_thread():
    app = application()
    main_thread_id = threading.get_ident()
    loader_thread_ids = []
    apply_thread_ids = []
    finished_workers = []

    def loader():
        loader_thread_ids.append(threading.get_ident())
        return _snapshot()

    dialog = DatabaseInfoDialog(snapshot_loader=loader, auto_load=False)
    original_apply_snapshot = dialog._apply_snapshot

    def apply_snapshot(snapshot):
        apply_thread_ids.append(threading.get_ident())
        original_apply_snapshot(snapshot)

    dialog._apply_snapshot = apply_snapshot
    # Намеренно не завершаем UI из finished: успешный результат обязан сам
    # перевести окно в готовое состояние, даже если finished придёт позднее.
    dialog._on_load_finished = finished_workers.append
    dialog.reload_info()

    assert _process_events_until(app, lambda: dialog._snapshot is not None)
    assert loader_thread_ids
    assert loader_thread_ids[0] != main_thread_id
    assert apply_thread_ids == [main_thread_id]
    assert dialog.refresh_button.isEnabled()
    assert dialog._worker is None
    assert _process_events_until(app, lambda: bool(finished_workers))
    dialog.close()
    app.processEvents()


def test_dialog_failed_load_is_ready_without_finished_callback():
    app = application()
    finished_workers = []

    def loader():
        raise RuntimeError("test failure")

    dialog = DatabaseInfoDialog(snapshot_loader=loader, auto_load=False)
    dialog._on_load_finished = finished_workers.append
    dialog.reload_info()

    assert _process_events_until(
        app,
        lambda: "test failure" in dialog.status_label.text(),
    )
    assert dialog.refresh_button.isEnabled()
    assert dialog._worker is None
    assert _process_events_until(app, lambda: bool(finished_workers))
    dialog.close()
    app.processEvents()


def test_admin_maintenance_has_database_info_button():
    app = application()
    widget = AdminMainWidget(role="admin")

    assert widget.btn_database_info.text() == "Информация о БД"
    assert any(
        entry["button"] is widget.btn_database_info
        for entry in widget.settings_action_cards
    )
    assert not widget.btn_database_info.isHidden()
    widget.deleteLater()
    app.processEvents()


def test_closing_dialog_cancels_default_service_collection():
    app = application()
    started = threading.Event()
    cancelled = threading.Event()

    class SlowService:
        @staticmethod
        def collect(*, cancel_check):
            started.set()
            while not cancel_check():
                time.sleep(0.005)
            cancelled.set()
            raise DatabaseInfoCollectionCancelled("cancelled")

    dialog = DatabaseInfoDialog(snapshot_loader=_snapshot, auto_load=False)
    dialog._service = SlowService()
    dialog.show()
    dialog.reload_info()
    assert started.wait(1.0)
    worker = dialog._worker
    assert worker is not None

    dialog.close()

    assert _process_events_until(app, cancelled.is_set)
    assert worker.wait(1000)
    app.processEvents()

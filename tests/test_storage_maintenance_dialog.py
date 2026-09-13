import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from rem_card.ui.admin_view.storage_maintenance_dialog import StorageMaintenanceDialog
from rem_card.ui.styles import theme_manager
from rem_card.services.storage_maintenance import Inspection


@pytest.fixture
def app(tmp_path, monkeypatch):
    # File-only theme storage; never initialize the real settings database.
    monkeypatch.setenv("REMCARD_BAZA_DIR", str(tmp_path / "synthetic-base"))
    monkeypatch.setenv("REMCARD_DEV_BAZA_DIR", str(tmp_path / "synthetic-base"))
    monkeypatch.setenv("REMCARD_LOCAL_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("REMCARD_STYLE_SETTINGS_PATH", str(tmp_path / "theme.json"))
    monkeypatch.setattr(theme_manager, "_THEME_MANAGER", None)
    application = QApplication.instance() or QApplication([])
    yield application


def wait(app, predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(.005)
    assert predicate()


def test_inspection_is_async_and_incomplete_result_disables_cleanup(app, tmp_path, monkeypatch):
    dialog = StorageMaintenanceDialog(tmp_path)
    release = threading.Event()
    started = threading.Event()
    def slow_inspection():
        started.set()
        release.wait(2)
        return Inspection(str(tmp_path), errors=["Нет доступа"])
    monkeypatch.setattr(dialog.service, "inspect", slow_inspection)
    dialog.inspect_files()
    assert started.wait(1)
    assert not dialog.inspect_button.isEnabled()
    assert not dialog.clean_button.isEnabled()
    dialog.reject()
    assert dialog.worker is not None
    release.set()
    wait(app, lambda: dialog.worker is None)
    assert dialog.inspect_button.isEnabled()
    assert not dialog.clean_button.isEnabled()
    assert "неполная" in dialog.status.text()


@pytest.mark.skipif(os.name != 'nt', reason='Windows deletion')
def test_full_manual_flow(app, tmp_path):
    (tmp_path / 'archiv').mkdir()
    (tmp_path / 'archiv/rao_journal.db').write_bytes(b'synthetic')
    (tmp_path / 'logs').mkdir()
    old = tmp_path / 'logs/audit_20260518.jsonl'
    old.write_text('{}', encoding='utf-8')
    stamp = time.time() - 120 * 86400
    os.utime(old, (stamp, stamp))
    dialog = StorageMaintenanceDialog(tmp_path)
    dialog.show()
    dialog.inspect_button.click()
    wait(app, lambda: dialog.worker is None)
    assert dialog.table.rowCount() == 1
    assert dialog.clean_button.isEnabled()
    dialog.clean_button.click()
    wait(app, lambda: dialog.worker is None)
    assert not old.exists()
    assert not dialog.clean_button.isEnabled()
    assert 'Удалено: 1' in dialog.status.text()


def test_action_is_doctor_only(app):
    from rem_card.ui.admin_view.admin_main_widget import AdminMainWidget
    for role in ('doctor', 'nurse', 'admin'):
        widget = AdminMainWidget(role=role)
        attached = {entry['button'] for entry in widget.settings_action_cards}
        assert (widget.btn_storage_maintenance in attached) == (role == 'doctor')

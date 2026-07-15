from __future__ import annotations

import os
import sys
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from rem_card.app.emergency_password import verify_emergency_password_for_offline_startup  # noqa: E402
from rem_card.app.emergency_password_storage import (  # noqa: E402
    create_emergency_password_record,
    is_emergency_password_record,
    verify_emergency_password_record,
)
from rem_card.ui.admin_view.admin_main_widget import AdminMainWidget  # noqa: E402


def test_emergency_password_record_is_salted_and_does_not_store_plaintext():
    first = create_emergency_password_record("new-safe-password")
    second = create_emergency_password_record("new-safe-password")

    assert is_emergency_password_record(first)
    assert first != second
    assert "new-safe-password" not in str(first)
    assert verify_emergency_password_record("new-safe-password", first)
    assert not verify_emergency_password_record("wrong-password", first)


def test_emergency_password_record_rejects_corrupted_or_excessive_work_factor():
    record = create_emergency_password_record("new-safe-password")
    record["iterations"] = 2_000_001
    assert not verify_emergency_password_record("new-safe-password", record)

    record = create_emergency_password_record("new-safe-password")
    record["digest"] = "not-base64!"
    assert not verify_emergency_password_record("new-safe-password", record)


def test_offline_password_cannot_fall_back_without_settings_snapshot():
    assert not verify_emergency_password_for_offline_startup("123456", settings_db_path="")


def test_first_change_gate_disables_other_settings_but_keeps_password_action():
    _app = QApplication.instance() or QApplication([])
    widget = AdminMainWidget(role="doctor")

    widget._set_emergency_password_gate(True)

    assert widget.btn_emergency_password.isEnabled()
    assert not widget.btn_drugs.isEnabled()
    assert widget.emergency_password_notice.isVisible() is False
    assert not widget.emergency_password_notice.isHidden()

    widget._set_emergency_password_gate(False)
    assert widget.btn_drugs.isEnabled()
    assert widget.emergency_password_notice.isHidden()
    widget.deleteLater()


def test_first_settings_entry_schedules_mandatory_password_dialog(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "rem_card.app.emergency_password.is_emergency_password_change_required",
        lambda: True,
    )
    widget = AdminMainWidget(role="doctor")
    prompts: list[bool] = []
    monkeypatch.setattr(
        widget,
        "_prompt_required_emergency_password_change",
        lambda: prompts.append(True),
    )

    widget.show()
    app.processEvents()

    assert prompts == [True]
    assert not widget.btn_drugs.isEnabled()
    assert widget.btn_emergency_password.isEnabled()
    widget.close()
    widget.deleteLater()

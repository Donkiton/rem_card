from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from rem_card.ui.admin_view.db_rotation_dialog import DbRotationDialog


class _DbManagerStub:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.runtime_context = SimpleNamespace(mode="network")

    @staticmethod
    def manual_rotation_undo_status() -> dict:
        return {"available": False, "reason": "not_available"}

    @staticmethod
    def active_rotation_role_locks(_owner_context=None) -> list:
        return []

    @staticmethod
    def active_rotation_emergency_sessions() -> list:
        return []


def test_undo_button_uses_rotation_style_when_disabled(tmp_path):
    app = QApplication.instance() or QApplication([])
    dialog = DbRotationDialog(_DbManagerStub(str(tmp_path / "rao_journal.db")))
    try:
        assert dialog.undo_btn.objectName() == dialog.rotate_btn.objectName()
        assert dialog.undo_btn.objectName() == "DbRotationPrimaryButton"
        assert not dialog.undo_btn.isEnabled()
        assert "QPushButton#DbRotationPrimaryButton:disabled" in dialog.styleSheet()
    finally:
        dialog.close()
        app.processEvents()

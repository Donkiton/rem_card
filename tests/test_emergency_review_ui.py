from __future__ import annotations

# Environment isolation must happen before importing Qt and project UI modules.
# ruff: noqa: E402

import os
from pathlib import Path

_RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "tmp" / "ui-emergency-tests"
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["REMCARD_BAZA_DIR"] = str(_RUNTIME_ROOT / "baza")
os.environ["REMCARD_CI_SETTINGS_DIR"] = str(_RUNTIME_ROOT / "settings")
os.environ["REMCARD_STYLE_SETTINGS_FILE"] = str(_RUNTIME_ROOT / "settings" / "style.ini")
os.environ["LOCALAPPDATA"] = str(_RUNTIME_ROOT / "local")
os.environ["REMCARD_EMERGENCY_DB_ROOT"] = str(_RUNTIME_ROOT / "emergency")
os.environ["REMCARD_LOCAL_LOGS_DIR"] = str(_RUNTIME_ROOT / "logs")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog

from rem_card.ui.shared.emergency_review_dialog import AdmissionReviewBlock, EmergencyReviewDialog


def _review(*, blockers=None):
    return {
        "summary": {"patient_count": 2, "operation_count": 3},
        "blockers": list(blockers or []),
        "patients": [
            {
                "admission_id": 12,
                "patient_id": 7,
                "title": "Иванов Иван Иванович · история № 456 · поступление 09.09.2026 11:30",
                "selected_default": False,
                "operation_count": 2,
                "sections": [
                    {"key": "vitals", "title": "Показатели состояния", "items": [{
                        "text": "Изменено: Показатели состояния · 10.09.2026 07:30. АД: 120/80 мм рт. ст. → 130/85 мм рт. ст.",
                    }]},
                    {"key": "orders", "title": "Назначения и выполнения", "items": [{
                        "text": "Добавлено: Назначение · 10.09.2026 08:15. Препарат: Норадреналин; доза: 0.1 мкг/кг/мин",
                    }]},
                ],
            },
            {
                "admission_id": 13,
                "patient_id": 8,
                "title": "Петров Пётр · история № 457 · поступление 10.09.2026 09:00",
                "selected_default": False,
                "operation_count": 1,
                "sections": [{"key": "admission", "title": "Пациент и госпитализация", "items": [{
                    "text": "Изменено: Госпитализация. Койка: 2 → 3",
                }]}],
            },
        ],
    }


def _app():
    return QApplication.instance() or QApplication([])


def test_admissions_are_collapsed_unchecked_and_keyboard_operable():
    app = _app()
    dialog = EmergencyReviewDialog(_review())
    try:
        blocks = dialog.findChildren(AdmissionReviewBlock)
        assert len(blocks) == 2
        assert dialog.selected_admission_ids == []
        assert all(not block.accept_checkbox.isChecked() for block in blocks)
        assert all(not block.details_widget.isVisible() for block in blocks)
        assert not dialog.accept_button.isEnabled()
        assert dialog.scroll_area.horizontalScrollBarPolicy() == Qt.ScrollBarAlwaysOff

        blocks[0].toggle_button.click()
        dialog.show()
        app.processEvents()
        assert blocks[0].details_widget.isVisible()
        assert blocks[0].toggle_button.arrowType() == Qt.DownArrow
        assert blocks[0].toggle_button.accessibleName().startswith("Скрыть")

        blocks[0].accept_checkbox.click()
        assert dialog.selected_admission_ids == [12]
        assert dialog.accept_button.isEnabled()
        assert "(1)" in dialog.accept_button.text()
    finally:
        dialog.deleteLater()
        app.processEvents()


def test_accepting_subset_warns_with_exact_omitted_count(monkeypatch):
    _app()
    dialog = EmergencyReviewDialog(_review())
    dialog._blocks[0].accept_checkbox.setChecked(True)
    calls = []

    def ask(_parent, title, message, actions, default_code=0):
        calls.append((title, message, tuple(actions), default_code))
        return QDialog.Rejected if len(calls) == 1 else QDialog.Accepted

    monkeypatch.setattr(
        "rem_card.ui.shared.emergency_review_dialog.EmergencyActionDialog.ask", ask
    )
    try:
        dialog._accept_selected()
        assert dialog.result() == 0
        assert "Не выбрано: 1 из 2" in calls[0][1]
        dialog._accept_selected()
        assert dialog.result() == QDialog.Accepted
        assert dialog.selected_admission_ids == [12]
    finally:
        dialog.deleteLater()


def test_explicit_finish_without_transfer_accepts_empty_selection(monkeypatch):
    _app()
    dialog = EmergencyReviewDialog(_review())
    monkeypatch.setattr(
        "rem_card.ui.shared.emergency_review_dialog.EmergencyActionDialog.ask",
        lambda *_args, **_kwargs: QDialog.Accepted,
    )
    try:
        dialog._finish_without_transfer()
        assert dialog.result() == QDialog.Accepted
        assert dialog.selected_admission_ids == []
    finally:
        dialog.deleteLater()


def test_cancel_rejects_without_selection_or_confirmation(monkeypatch):
    _app()
    dialog = EmergencyReviewDialog(_review())
    dialog._blocks[0].accept_checkbox.setChecked(True)
    monkeypatch.setattr(
        "rem_card.ui.shared.emergency_review_dialog.EmergencyActionDialog.ask",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("confirmation must not open")),
    )
    try:
        dialog.reject()
        assert dialog.result() == QDialog.Rejected
        assert dialog.selected_admission_ids == [12]
    finally:
        dialog.deleteLater()


def test_blockers_disable_all_acceptance_paths():
    _app()
    dialog = EmergencyReviewDialog(_review(blockers=[{"message": "Койка уже занята другим пациентом."}]))
    try:
        dialog._blocks[0].accept_checkbox.setChecked(True)
        assert not dialog.accept_button.isEnabled()
        assert not dialog.finish_without_button.isEnabled()
        assert "Койка уже занята" in dialog.blockers_label.text()
    finally:
        dialog.deleteLater()


def test_dialog_fits_available_screen_when_shown():
    app = _app()
    dialog = EmergencyReviewDialog(_review())
    try:
        dialog.show()
        app.processEvents()
        available = dialog.screen().availableGeometry()
        assert dialog.width() <= available.width()
        assert dialog.height() <= available.height()
        assert dialog.width() <= 900
        assert dialog.height() <= 680
    finally:
        dialog.deleteLater()
        app.processEvents()

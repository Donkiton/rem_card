from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR.parent) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR.parent))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QInputMethodEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from rem_card.services.mkb import MKBService  # noqa: E402
from rem_card.ui.patient_bed_management.diagnosis_search import MATCH_ROLE  # noqa: E402
from rem_card.ui.patient_bed_management.tabs.diagnosis_tab import DiagnosisTabWidget  # noqa: E402
from rem_card.ui.styles.diagnosis_styles import DARK_DIAGNOSIS_PALETTE, LIGHT_DIAGNOSIS_PALETTE  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def widget(tmp_path, app):
    db_path = tmp_path / "mkb.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE class_mkb (code TEXT, name TEXT)")
        conn.executemany("INSERT INTO class_mkb VALUES (?, ?)", [
            ("K42", "Пупочная грыжа"),
            ("K42.0", "Пупочная грыжа с непроходимостью без гангрены"),
            ("K42.1", "Пупочная грыжа с гангреной"),
            ("K42.9", "Пупочная грыжа без непроходимости или гангрены"),
            ("A00", "Холера"),
            ("A01.1+", "Диагноз с маркером"),
            ("T01.1", "Очень длинное название диагноза " * 8),
        ])
    conn.close()
    service = MKBService(str(db_path))
    view = DiagnosisTabWidget(service, show_operations=False)
    view.resize(520, 310)
    yield view
    view.hide()
    view._search_timer.stop()
    service.close_connection()


def reveal(widget, app):
    widget.show()
    widget.activateWindow()
    widget.diagnosis_code_input.setFocus()
    app.processEvents()


def results(widget):
    model = widget.completer.results_model
    return [model.item(row).data(MATCH_ROLE) for row in range(model.rowCount()) if model.item(row).data(MATCH_ROLE)]


def test_manual_text_is_available_immediately_without_code_or_counter(widget):
    assert not widget.diagnosis_text_input.isReadOnly()
    assert widget.diagnosis_code_input.placeholderText() == "Введите код или название"
    assert not any("/ 500" in label.text() for label in widget.findChildren(QLabel))
    widget.diagnosis_text_input.setPlainText("Свой диагноз")
    assert widget.get_data() == {"diagnosis_code": None, "diagnosis_text": "Свой диагноз"}


def test_exact_code_fills_diagnosis_without_focus_loss(widget):
    widget.diagnosis_code_input.setText("k421")
    assert widget.get_data() == {"diagnosis_code": "K42.1", "diagnosis_text": "Пупочная грыжа с гангреной"}
    assert widget.diagnosis_code_input.text() == "k421"
    assert not widget.diagnosis_text_input.isReadOnly()


def test_code_can_be_completed_after_parent_rubric(widget):
    widget.diagnosis_code_input.setText("K42")
    assert widget.get_data()["diagnosis_code"] == "K42"
    widget.diagnosis_code_input.setText("K42.")
    widget.diagnosis_code_input.setText("K42.1")
    assert widget.get_data()["diagnosis_code"] == "K42.1"


def test_cyrillic_name_and_cursor_are_not_reformatted(widget):
    widget.diagnosis_code_input.setText("пупочная грыжа")
    widget.diagnosis_code_input.setCursorPosition(4)
    widget._refresh_suggestions()
    assert widget.diagnosis_code_input.text() == "пупочная грыжа"
    assert widget.diagnosis_code_input.cursorPosition() == 4
    assert len(results(widget)) == 4


def test_ime_input_updates_live_suggestions(widget, app):
    reveal(widget, app)
    event = QInputMethodEvent()
    event.setCommitString("пупочная")
    QApplication.sendEvent(widget.diagnosis_code_input, event)
    QTest.qWait(150)
    assert widget.diagnosis_code_input.text() == "пупочная"
    assert len(results(widget)) == 4
    widget.diagnosis_code_input.setText("пупочная грыжа с гангреной")
    QTest.qWait(150)
    assert [match.code for match in results(widget)] == ["K42.1"]


def test_live_result_can_be_selected_with_mouse(widget, app):
    reveal(widget, app)
    widget.diagnosis_code_input.setText("пупочная грыжа с гангреной")
    QTest.qWait(150)
    popup = widget.completer.popup()
    assert popup.isVisible()
    index = popup.model().index(0, 0)
    QTest.mouseClick(popup.viewport(), Qt.LeftButton, pos=popup.visualRect(index).center())
    QTest.qWait(150)
    assert widget.get_data()["diagnosis_code"] == "K42.1"
    assert widget.diagnosis_code_input.text() == "K42.1"
    assert not popup.isVisible()


def test_live_result_can_be_selected_with_keyboard(widget, app):
    reveal(widget, app)
    widget.diagnosis_code_input.setText("пупочная грыжа с гангреной")
    QTest.qWait(150)
    QTest.keyClick(widget.diagnosis_code_input, Qt.Key_Down)
    assert widget.get_data()["diagnosis_code"] is None
    assert widget.diagnosis_code_input.text() == "пупочная грыжа с гангреной"
    QTest.keyClick(widget.diagnosis_code_input, Qt.Key_Return)
    QTest.qWait(150)
    assert widget.get_data()["diagnosis_code"] == "K42.1"
    assert not widget.completer.popup().isVisible()


def test_escape_dismisses_pending_search_and_does_not_reopen_popup(widget, app):
    reveal(widget, app)
    widget.diagnosis_code_input.setText("пупочная")
    QTest.keyClick(widget.diagnosis_code_input, Qt.Key_Escape)
    QTest.qWait(150)
    assert not widget.completer.popup().isVisible()


def test_unknown_query_can_be_explicitly_used_as_manual_diagnosis(widget):
    widget.diagnosis_code_input.setText("Собственная формулировка")
    widget._refresh_suggestions()
    model = widget.completer.results_model
    widget.completer.accept_index(model.index(model.rowCount() - 1, 0))
    assert widget.get_data() == {"diagnosis_code": None, "diagnosis_text": "Собственная формулировка"}
    assert widget.diagnosis_code_input.text() == ""


def test_unselected_search_text_is_not_saved_as_a_code_or_diagnosis(widget):
    widget.diagnosis_code_input.setText("пупочная")
    assert widget.get_data() == {"diagnosis_code": None, "diagnosis_text": ""}


def test_unknown_code_never_becomes_diagnosis_text(widget):
    widget.diagnosis_code_input.setText("Z99.9")
    widget._use_manual_query("Z99.9")
    assert widget.get_data() == {"diagnosis_code": None, "diagnosis_text": ""}


def test_search_and_focus_changes_preserve_manual_text(widget, app):
    widget.diagnosis_text_input.setPlainText("Важное клиническое уточнение")
    reveal(widget, app)
    widget.diagnosis_code_input.setText("K42.1")
    QTest.qWait(150)
    widget.diagnosis_text_input.setFocus()
    app.processEvents()
    assert widget.get_data() == {"diagnosis_code": None, "diagnosis_text": "Важное клиническое уточнение"}
    widget._select_diagnosis(widget.mkb_service.find_diagnosis_by_code("K42.1"))
    assert widget.get_data()["diagnosis_code"] == "K42.1"


def test_editing_selected_name_clears_code_and_does_not_rebind_on_focus(widget, app):
    widget.diagnosis_code_input.setText("K42.1")
    widget.diagnosis_text_input.setPlainText("Свой уточнённый диагноз")
    widget.diagnosis_code_input.setFocus()
    app.processEvents()
    assert widget.get_data() == {"diagnosis_code": None, "diagnosis_text": "Свой уточнённый диагноз"}
    assert widget.diagnosis_code_input.text() == ""


def test_clearing_search_preserves_selected_diagnosis(widget):
    widget.diagnosis_code_input.setText("K42.1")
    widget.diagnosis_code_input.clear()
    assert widget.get_data()["diagnosis_code"] == "K42.1"


@pytest.mark.parametrize("code", [None, "K42.1", "Z99.9"])
def test_loading_existing_card_preserves_saved_text_and_code(widget, code):
    admission = SimpleNamespace(diagnosis_code=code, diagnosis_text="Сохранённая формулировка врача")
    widget.set_data(admission, [])
    assert widget.get_data() == {"diagnosis_code": code, "diagnosis_text": admission.diagnosis_text}


def test_empty_saved_text_can_be_filled_from_known_code(widget):
    widget.set_data(SimpleNamespace(diagnosis_code="K42.1", diagnosis_text=None), [])
    assert widget.get_data()["diagnosis_text"] == "Пупочная грыжа с гангреной"


def test_marker_is_saved_as_part_of_selected_code(widget):
    widget.diagnosis_code_input.setText("A01.1")
    assert widget.get_data()["diagnosis_code"] == "A01.1+"


def test_manual_length_limit_remains_without_counter(widget):
    widget.diagnosis_text_input.setPlainText("а" * 520)
    assert len(widget.get_data()["diagnosis_text"]) == 500


def test_existing_long_text_is_not_truncated_when_opening(widget):
    widget.set_data(SimpleNamespace(diagnosis_code=None, diagnosis_text="а" * 550), [])
    assert len(widget.get_data()["diagnosis_text"]) == 550


def test_lookup_failure_keeps_manual_entry_available(widget, monkeypatch):
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("test dictionary unavailable")
    monkeypatch.setattr(widget.mkb_service, "find_diagnosis_by_code", unavailable)
    monkeypatch.setattr(widget.mkb_service, "search_diagnoses", unavailable)
    widget.diagnosis_code_input.setText("K42.1")
    widget._refresh_suggestions()
    assert "недоступен" in widget.info_text.text()
    widget.diagnosis_text_input.setPlainText("Ручной диагноз")
    assert widget.get_data()["diagnosis_text"] == "Ручной диагноз"


def test_dark_palette_can_be_previewed_without_changing_working_default(widget):
    assert LIGHT_DIAGNOSIS_PALETTE.surface in widget.styleSheet()
    widget.apply_palette(DARK_DIAGNOSIS_PALETTE)
    assert DARK_DIAGNOSIS_PALETTE.surface in widget.styleSheet()
    assert widget.completer.delegate.colors is DARK_DIAGNOSIS_PALETTE
    widget.apply_palette()
    assert widget.completer.delegate.colors is LIGHT_DIAGNOSIS_PALETTE


def test_long_suggestion_wraps_in_narrow_popup(widget, app):
    widget.resize(360, 310)
    reveal(widget, app)
    widget.diagnosis_code_input.setText("Очень длинное")
    QTest.qWait(150)
    popup = widget.completer.popup()
    row = popup.model().index(0, 0)
    assert popup.visualRect(row).height() > 38
    assert popup.width() <= widget.diagnosis_code_input.width()

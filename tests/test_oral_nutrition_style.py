import gc
import os
import threading
from datetime import datetime
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QSettings, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QMouseEvent, QWindow  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QLabel,
    QTableWidgetItem,
)

from rem_card.ui.shared.components.oral_nutrition_widget import (  # noqa: E402
    DietAssignmentDialog,
    OralFactDialog,
    OralNutritionWidget,
)
from rem_card.ui.nurse_view.sectors.nurse_sector_2b import NurseSector2b  # noqa: E402
from rem_card.ui.rem_card_sectors.sector_2b import Sector2b  # noqa: E402
from rem_card.ui.shared.display_settings_storage import REMCARD_TABS  # noqa: E402
from rem_card.ui.shared.custom_message_box import CustomMessageBox  # noqa: E402
from rem_card.ui.shared.window_state import SavedFramelessDialogMixin  # noqa: E402


def _application() -> QApplication:
    return QApplication.instance() or QApplication([])


def _template():
    return SimpleNamespace(
        id=1,
        name="Стол № 9",
        diet_text="Диета при сахарном диабете",
        is_default=True,
        schedule_json=[],
        details_json={},
    )


def _diet_version():
    return SimpleNamespace(
        id=17,
        admission_id=1,
        template_id=1,
        diet_name="Стол № 9",
        diet_text="Диета при сахарном диабете",
        effective_from=datetime(2026, 8, 30, 8, 0),
        schedule_json=[],
        details_json={},
        change_note="Первичное назначение",
        version=3,
    )


def test_oral_nutrition_tab_uses_remcard_cards_and_action_roles():
    _application()
    widget = OralNutritionWidget(role="doctor")

    assert widget.objectName() == "OralNutritionRoot"
    assert widget.outer_header.text() == "Пероральное питание"
    assert widget.outer_header.objectName() == "OralNutritionOuterHeader"
    assert widget.outer_body.objectName() == "OralNutritionOuterBody"
    assert widget.assign_btn.objectName() == "OralPrimaryButton"
    assert widget.undo_btn.objectName() == "OralSecondaryButton"
    assert widget.clear_btn.objectName() == "OralDangerButton"
    assert widget.intake_table.objectName() == "OralIntakeTable"
    assert len(widget.findChildren(QFrame, "OralNutritionSectionCard")) == 3
    assert 'QFrame#OralNutritionSummary[dietState="assigned"]' not in widget.styleSheet()
    assert "border-left: 4px solid" not in widget.styleSheet()
    assert "border-radius" in widget.styleSheet()
    assert widget.lower_layout.stretch(0) == 1
    assert widget.lower_layout.stretch(1) == 1


def test_diet_table_headers_match_ventilation_palette():
    _application()
    widget = OralNutritionWidget(role="doctor")
    page_style = widget.styleSheet().lower()
    dialog = DietAssignmentDialog([_template()])
    dialog_style = dialog.content_widget.styleSheet().lower()

    for style in (page_style, dialog_style):
        assert "background-color: #d9e2ec" in style
        assert "color: #243b53" in style
        assert "background-color: #cbd7e5" in style


def test_diet_buttons_match_procedure_dialog_visual_language():
    _application()
    widget = OralNutritionWidget(role="doctor")
    page_style = widget.styleSheet().lower()
    dialog = DietAssignmentDialog([_template()])
    dialog_style = dialog.content_widget.styleSheet().lower()

    for style in (page_style, dialog_style):
        assert "border-radius: 5px" in style
        assert "background-color: #2f80c0" in style
        assert "border: 1px solid #23689f" in style
        assert "background-color: #f4f7fb" in style
        assert "border: 1px solid #b9c5d3" in style
        assert "background-color: #9dbbd3" in style


def test_oral_nutrition_hides_redundant_plan_fact_footer_after_render():
    _application()
    widget = OralNutritionWidget(role="doctor")
    widget.admission_id = 1
    widget._snapshot = {"planned_rows": [], "events": [], "versions": [], "history": []}

    widget._render()

    assert widget.status_label.text() == ""
    assert widget.status_label.isHidden()


def test_oral_nutrition_coalesces_overlapping_snapshot_refreshes(monkeypatch):
    app = _application()

    class FakeWorker(QObject):
        succeeded = Signal(object)
        failed = Signal(object)
        finished = Signal()
        instances = []

        def __init__(self, operation, parent=None):
            super().__init__(parent)
            self.operation = operation
            self.running = False
            self.__class__.instances.append(self)

        def start(self):
            self.running = True

        def isRunning(self):
            return self.running

        def complete(self, result):
            self.running = False
            self.succeeded.emit(result)
            self.finished.emit()

    monkeypatch.setattr(
        "rem_card.ui.shared.components.oral_nutrition_widget.AsyncCallThread",
        FakeWorker,
    )
    service = SimpleNamespace(build_oral_nutrition_snapshot=lambda *_: {})
    widget = OralNutritionWidget(service=service, role="doctor")
    widget.admission_id = 1
    widget.shift_date = datetime(2026, 8, 30, 8, 0)

    widget.refresh_data()
    widget.refresh_data()

    assert len(FakeWorker.instances) == 1
    assert widget._refresh_pending is True

    FakeWorker.instances[0].complete({})
    app.processEvents()

    assert len(FakeWorker.instances) == 2
    assert widget._refresh_pending is False
    assert widget._refresh_worker is FakeWorker.instances[1]


def test_intake_double_click_edits_existing_fact_instead_of_creating_one(monkeypatch):
    _application()
    widget = OralNutritionWidget(role="doctor")
    widget.intake_table.insertRow(0)
    item = QTableWidgetItem("Вода")
    event = SimpleNamespace(id=1)
    item.setData(Qt.UserRole, {"facts": [event]})
    item.setData(Qt.UserRole + 1, event)
    widget.intake_table.setItem(0, 0, item)
    edited = []
    created = []
    monkeypatch.setattr(widget, "_edit_selected_fact", lambda: edited.append(True))
    monkeypatch.setattr(widget, "_add_planned_fact", lambda: created.append(True))

    widget.intake_table.itemDoubleClicked.emit(item)

    assert widget.intake_table.currentRow() == 0
    assert edited == [True]
    assert created == []


def test_intake_double_click_creates_fact_when_selected_meal_has_no_fact(monkeypatch):
    _application()
    widget = OralNutritionWidget(role="doctor")
    widget.intake_table.insertRow(0)
    item = QTableWidgetItem("Завтрак")
    item.setData(Qt.UserRole, {"meal": "Завтрак", "facts": []})
    item.setData(Qt.UserRole + 1, None)
    widget.intake_table.setItem(0, 0, item)
    edited = []
    created = []
    monkeypatch.setattr(widget, "_edit_selected_fact", lambda: edited.append(True))
    monkeypatch.setattr(widget, "_add_planned_fact", lambda: created.append(True))

    widget.intake_table.itemDoubleClicked.emit(item)

    assert edited == []
    assert created == [True]


def test_nurse_double_click_edits_existing_intake_fact(monkeypatch):
    _application()
    widget = OralNutritionWidget(role="nurse")
    widget.intake_table.insertRow(0)
    event = SimpleNamespace(id=7, version=2, plan_version_id=3, planned_item_key="breakfast")
    planned = {"meal": "Завтрак", "facts": [event], "plan_version_id": 3, "key": "breakfast"}
    item = QTableWidgetItem("Завтрак")
    item.setData(Qt.UserRole, planned)
    item.setData(Qt.UserRole + 1, event)
    widget.intake_table.setItem(0, 0, item)
    opened = []
    monkeypatch.setattr(widget, "_open_fact_dialog", lambda plan, fact: opened.append((plan, fact)))

    widget.intake_table.itemDoubleClicked.emit(item)

    assert opened == [(planned, event)]


def test_nurse_double_click_adds_missing_intake_fact(monkeypatch):
    _application()
    widget = OralNutritionWidget(role="nurse")
    widget.intake_table.insertRow(0)
    planned = {"meal": "Обед", "facts": [], "plan_version_id": 3, "key": "lunch"}
    item = QTableWidgetItem("Обед")
    item.setData(Qt.UserRole, planned)
    item.setData(Qt.UserRole + 1, None)
    widget.intake_table.setItem(0, 0, item)
    opened = []
    monkeypatch.setattr(widget, "_open_fact_dialog", lambda plan, fact: opened.append((plan, fact)))

    widget.intake_table.itemDoubleClicked.emit(item)

    assert opened == [(planned, None)]


def test_nurse_fact_edit_is_saved_with_nurse_actor(monkeypatch):
    _application()
    saved = []
    planned_time = datetime(2026, 8, 30, 12, 0)
    planned = {"planned_dt": planned_time}
    event = SimpleNamespace(id=9, version=4)

    class AcceptedFactDialog:
        def __init__(self, **_kwargs):
            pass

        @staticmethod
        def exec():
            return True

        @staticmethod
        def data():
            return {
                "event_time": planned_time,
                "amount_ml": 180,
                "meal_name": "Обед",
                "note": "съедено полностью",
            }

    service = SimpleNamespace(
        update_oral_intake_fact=lambda event_id, **kwargs: saved.append((event_id, kwargs))
        or SimpleNamespace(version=5)
    )
    widget = OralNutritionWidget(service=service, role="nurse")
    monkeypatch.setattr(
        "rem_card.ui.shared.components.oral_nutrition_widget.OralFactDialog",
        AcceptedFactDialog,
    )
    monkeypatch.setattr(widget, "_restrictions_at", lambda _moment: {})
    monkeypatch.setattr(
        widget,
        "_enqueue_write",
        lambda _description, operation, after_success=None: operation(),
    )

    widget._open_fact_dialog(planned, event)

    assert saved[0][0] == 9
    assert saved[0][1]["actor"] == "nurse"
    assert saved[0][1]["expected_version"] == 4
    assert saved[0][1]["planned_time"] == planned_time


def test_fact_dialog_open_error_is_reported_without_escaping(monkeypatch):
    _application()
    warnings = []

    class BrokenFactDialog:
        def __init__(self, **_kwargs):
            raise RuntimeError("тестовая ошибка окна")

    widget = OralNutritionWidget(role="nurse")
    monkeypatch.setattr(
        "rem_card.ui.shared.components.oral_nutrition_widget.OralFactDialog",
        BrokenFactDialog,
    )
    monkeypatch.setattr(
        CustomMessageBox,
        "warning",
        lambda *args, **kwargs: warnings.append((args, kwargs)),
    )

    widget._open_fact_dialog({"meal": "Ужин"}, None)

    assert warnings
    assert "Не удалось открыть окно фактического потребления" in warnings[0][0][2]


def test_oral_write_result_is_applied_on_gui_thread():
    app = _application()
    callback_threads = []

    class BackgroundCallbackService:
        @staticmethod
        def enqueue_write(_description, operation, on_success=None, on_error=None):
            def run():
                try:
                    result = operation()
                except Exception as exc:
                    on_error(exc)
                else:
                    on_success(result)

            thread = threading.Thread(target=run)
            thread.start()
            thread.join()

    widget = OralNutritionWidget(service=BackgroundCallbackService(), role="nurse")
    gui_thread_id = threading.get_ident()

    widget._enqueue_write(
        "oral_fact_thread_test",
        lambda: SimpleNamespace(version=1),
        after_success=lambda _result: callback_threads.append(threading.get_ident()),
    )
    app.processEvents()

    assert callback_threads == [gui_thread_id]
    assert widget._write_pending is False


def test_oral_nutrition_navigation_is_named_diet_for_both_roles():
    _application()
    doctor_tabs = Sector2b()
    nurse_tabs = NurseSector2b()

    assert doctor_tabs.btn_oral_nutrition.text() == "Диета"
    assert nurse_tabs.btn_oral_nutrition.text() == "Диета"
    assert next(item for item in REMCARD_TABS["doctor"] if item["id"] == "oral_nutrition")["label"] == "Диета"
    assert next(item for item in REMCARD_TABS["nurse"] if item["id"] == "oral_nutrition")["label"] == "Диета"


def test_diet_assignment_dialog_styles_inner_surface_and_custom_controls():
    _application()
    dialog = DietAssignmentDialog([_template()])

    style = dialog.content_widget.styleSheet()
    buttons = dialog.findChild(QDialogButtonBox)

    assert dialog.content_widget.objectName() == "OralNutritionDialogBody"
    assert dialog.title_bar.title_label.text() == "Назначение диеты"
    assert any(
        label.text() == "Основные параметры диеты"
        for label in dialog.findChildren(QLabel, "OralDialogSectionTitle")
    )
    assert len(dialog.findChildren(QFrame, "OralDialogSection")) == 3
    assert "combo_arrow_down.svg" in style
    assert "spin_arrow_up.svg" in style
    assert buttons.button(QDialogButtonBox.Save).objectName() == "OralDialogPrimaryButton"
    assert buttons.button(QDialogButtonBox.Cancel).objectName() == "OralDialogSecondaryButton"
    assert buttons.button(QDialogButtonBox.Save).text() == "Сохранить"
    assert buttons.button(QDialogButtonBox.Cancel).text() == "Отмена"
    assert dialog.remove_row_btn.objectName() == "OralDialogDangerButton"


def test_diet_assignment_dialog_is_resizable_and_restores_saved_geometry(tmp_path, monkeypatch):
    _application()
    settings = QSettings(str(tmp_path / "diet-assignment-dialog.ini"), QSettings.IniFormat)
    monkeypatch.setattr(DietAssignmentDialog, "_settings", lambda self: settings)

    first_dialog = DietAssignmentDialog([_template()])
    # Offscreen Qt exposes an 800 px-wide virtual screen, while the dialog's
    # required minimum width is 820 px. Keep the saved geometry within the
    # platform's normalized bounds so the persistence assertion is portable.
    first_dialog.setGeometry(1, 40, 820, 740)
    first_dialog._save_saved_geometry()

    restored_dialog = DietAssignmentDialog([_template()])

    assert isinstance(restored_dialog, SavedFramelessDialogMixin)
    assert restored_dialog.isSizeGripEnabled() is True
    assert restored_dialog.geometry().x() == 1
    assert restored_dialog.geometry().y() == 40
    assert restored_dialog.geometry().width() == 820
    assert restored_dialog.geometry().height() == 740


def test_existing_diet_assignment_dialog_offers_confirmed_delete(monkeypatch):
    _application()
    dialog = DietAssignmentDialog([_template()], version=_diet_version())
    confirmations = []
    monkeypatch.setattr(
        CustomMessageBox,
        "question",
        lambda *args, **kwargs: confirmations.append((args, kwargs)) or CustomMessageBox.Yes,
    )

    assert dialog.delete_button.text() == "Удалить"
    assert dialog.delete_button.objectName() == "OralDialogDangerButton"
    assert dialog.delete_button.isHidden() is False

    dialog.delete_button.click()

    assert confirmations
    assert dialog.delete_requested is True
    assert dialog.result() == QDialog.Accepted


def test_edit_selected_diet_deactivates_after_click_outside_changes_table():
    app = _application()
    widget = OralNutritionWidget(role="doctor")
    widget.admission_id = 1
    widget._snapshot = {
        "active": _diet_version(),
        "versions": [_diet_version()],
        "planned_rows": [],
        "events": [],
        "history": [],
    }
    widget.resize(1100, 760)
    widget.show()
    widget._render()
    app.processEvents()

    widget.version_table.selectRow(0)
    app.processEvents()
    assert widget.edit_version_btn.isEnabled() is True
    assert widget._selected_version() is not None

    QTest.mouseClick(widget.intake_table.viewport(), Qt.LeftButton, pos=QPoint(12, 12))
    app.processEvents()

    assert widget.version_table.selectionModel().hasSelection() is False
    assert widget._selected_version() is None
    assert widget.edit_version_btn.isEnabled() is False


def test_edit_selected_diet_button_keeps_selection_until_action_runs(monkeypatch):
    app = _application()
    selected = _diet_version()
    widget = OralNutritionWidget(role="doctor")
    widget.admission_id = 1
    widget.shift_date = datetime(2026, 8, 30, 8, 0)
    widget._snapshot = {
        "active": selected,
        "versions": [selected],
        "planned_rows": [],
        "events": [],
        "history": [],
    }
    opened = []
    monkeypatch.setattr(widget, "_open_assignment", lambda version: opened.append(version))
    widget.resize(1100, 760)
    widget.show()
    widget._render()
    app.processEvents()

    widget.version_table.selectRow(0)
    app.processEvents()
    QTest.mouseClick(widget.edit_version_btn, Qt.LeftButton)
    app.processEvents()

    assert opened == [selected]


def test_system_window_press_over_edit_button_does_not_clear_diet_selection():
    app = _application()
    widget = OralNutritionWidget(role="doctor")
    widget.admission_id = 1
    widget._snapshot = {
        "active": _diet_version(),
        "versions": [_diet_version()],
        "planned_rows": [],
        "events": [],
        "history": [],
    }
    widget.resize(1100, 760)
    widget.show()
    widget._render()
    app.processEvents()
    widget.version_table.selectRow(0)
    app.processEvents()

    global_position = widget.edit_version_btn.mapToGlobal(widget.edit_version_btn.rect().center())
    event = QMouseEvent(
        QEvent.MouseButtonPress,
        QPointF(0, 0),
        QPointF(global_position),
        Qt.LeftButton,
        Qt.LeftButton,
        Qt.NoModifier,
    )

    widget.eventFilter(widget.windowHandle(), event)

    assert widget.version_table.selectionModel().hasSelection() is True
    assert widget.edit_version_btn.isEnabled() is True


def test_version_selection_filter_accepts_system_window_mouse_events():
    _application()
    widget = OralNutritionWidget(role="doctor")
    system_window = QWindow()

    assert widget.eventFilter(system_window, QEvent(QEvent.MouseButtonPress)) is False


def test_edit_dialog_delete_request_removes_selected_diet_version(monkeypatch):
    _application()
    selected = _diet_version()
    deleted = []

    class DeleteDialog:
        delete_requested = True

        def __init__(self, _templates, version=None, parent=None):
            assert version is selected
            assert parent is widget

        @staticmethod
        def exec():
            return True

    service = SimpleNamespace(
        delete_diet_version=lambda admission_id, version_id, expected_version=None: deleted.append(
            (admission_id, version_id, expected_version)
        )
    )
    widget = OralNutritionWidget(service=service, role="doctor")
    widget.admission_id = 1
    widget._snapshot = {"templates": []}
    monkeypatch.setattr(
        "rem_card.ui.shared.components.oral_nutrition_widget.DietAssignmentDialog",
        DeleteDialog,
    )
    monkeypatch.setattr(
        widget,
        "_enqueue_write",
        lambda description, operation, after_success=None: operation(),
    )

    widget._open_assignment(selected)

    assert deleted == [(1, 17, 3)]


def test_diet_assignment_restrictions_cannot_be_selected_together():
    _application()
    dialog = DietAssignmentDialog([_template()])

    dialog.on_demand_check.setChecked(True)
    dialog.no_fluids_check.setChecked(True)
    assert dialog.no_fluids_check.isChecked() is True
    assert dialog.on_demand_check.isChecked() is False

    dialog.on_demand_check.setChecked(True)
    assert dialog.on_demand_check.isChecked() is True
    assert dialog.no_fluids_check.isChecked() is False


def test_hunger_disables_diet_parameters_and_meal_schedule_only():
    _application()
    dialog = DietAssignmentDialog([_template()])
    dialog.fractional_check.setChecked(True)
    schedule_rows = dialog.schedule_table.rowCount()

    dialog.no_food_check.setChecked(True)

    assert dialog.diet_parameters_widget.isEnabled() is False
    assert dialog.schedule_frame.isEnabled() is False
    assert dialog.fractional_check.isEnabled() is False
    assert dialog.fractional_check.isChecked() is False
    assert dialog.no_food_check.isEnabled() is True
    assert dialog.on_demand_check.isEnabled() is True
    assert dialog.no_fluids_check.isEnabled() is True

    dialog.on_demand_check.setChecked(True)
    dialog.no_fluids_check.setChecked(True)
    assert dialog.on_demand_check.isChecked() is False
    assert dialog.no_fluids_check.isChecked() is True

    data = dialog.data()
    assert data["template_id"] is None
    assert data["diet_name"] == "Голод"
    assert data["diet_text"] == "Пациент голодает"
    assert data["schedule_json"] == []
    assert data["details_json"]["fractional"] is False
    assert data["details_json"]["no_food"] is True
    assert data["details_json"]["no_fluids"] is True
    assert data["details_json"]["on_demand"] is False

    dialog.no_food_check.setChecked(False)
    assert dialog.diet_parameters_widget.isEnabled() is True
    assert dialog.schedule_frame.isEnabled() is True
    assert dialog.fractional_check.isEnabled() is True
    assert dialog.schedule_table.rowCount() == schedule_rows


def test_hunger_with_no_fluids_disables_unplanned_intake_for_both_roles(monkeypatch):
    _application()
    opened = []

    class UnexpectedFactDialog:
        def __init__(self, **_kwargs):
            opened.append(True)

    monkeypatch.setattr(
        "rem_card.ui.shared.components.oral_nutrition_widget.OralFactDialog",
        UnexpectedFactDialog,
    )
    for role in ("doctor", "nurse"):
        active = _diet_version()
        active.details_json = {"no_food": True, "no_fluids": True}
        widget = OralNutritionWidget(role=role)
        widget.admission_id = 1
        widget.shift_date = datetime(2026, 8, 30, 8, 0)
        widget._snapshot = {
            "active": active,
            "versions": [active],
            "planned_rows": [],
            "events": [],
            "history": [],
        }

        widget._render()

        assert widget.add_unplanned_btn.isEnabled() is False
        assert "голод" in widget.add_unplanned_btn.toolTip().lower()
        assert "жидкости" in widget.add_unplanned_btn.toolTip().lower()
        widget._open_fact_dialog(None, None)

    assert opened == []


def test_single_diet_restriction_does_not_disable_unplanned_intake():
    _application()
    for details in ({"no_food": True}, {"no_fluids": True}):
        active = _diet_version()
        active.details_json = details
        widget = OralNutritionWidget(role="nurse")
        widget.admission_id = 1
        widget.shift_date = datetime(2026, 8, 30, 8, 0)
        widget._snapshot = {
            "active": active,
            "versions": [active],
            "planned_rows": [],
            "events": [],
            "history": [],
        }

        widget._render()

        assert widget.add_unplanned_btn.isEnabled() is True
        assert widget.add_unplanned_btn.toolTip() == ""


def test_oral_fact_dialog_uses_same_dialog_design_language():
    _application()
    planned_time = datetime(2026, 8, 30, 12, 0)
    dialog = OralFactDialog(
        planned_item={"meal": "Завтрак", "amount": 250, "planned_dt": planned_time},
        shift_date=datetime(2026, 8, 30, 8, 0),
    )
    buttons = dialog.findChild(QDialogButtonBox)

    assert dialog.content_widget.objectName() == "OralNutritionDialogBody"
    assert len(dialog.findChildren(QFrame, "OralDialogSection")) == 1
    assert "combo_arrow_down.svg" in dialog.content_widget.styleSheet()
    assert buttons.button(QDialogButtonBox.Save).objectName() == "OralDialogPrimaryButton"
    assert buttons.button(QDialogButtonBox.Cancel).objectName() == "OralDialogSecondaryButton"
    assert buttons.button(QDialogButtonBox.Save).text() == "Сохранить"
    assert buttons.button(QDialogButtonBox.Cancel).text() == "Отмена"
    assert dialog.allowed_start == datetime(2026, 8, 30, 9, 0)
    assert dialog.allowed_end == datetime(2026, 8, 31, 7, 59)
    assert dialog.time_edit.minimumDateTime().toPython() == dialog.allowed_start
    assert dialog.time_edit.maximumDateTime().toPython() == dialog.allowed_end
    assert not hasattr(dialog, "time_window_label")
    assert dialog.time_edit.calendarWidget() in dialog._oral_popup_widgets


def test_oral_popup_wrappers_are_retained_for_dialog_lifetime():
    _application()
    dialog = DietAssignmentDialog([_template()])

    retained = dialog._oral_popup_widgets

    assert retained
    assert dialog.effective_edit.calendarWidget() in retained
    assert dialog.template_combo.view() in retained


def test_oral_modal_dialog_is_deleted_on_gui_event_loop():
    app = _application()
    widget = OralNutritionWidget(role="doctor")
    dialog = widget._own_modal_dialog(QDialog(widget))
    dialog_key = id(dialog)

    widget._dispose_modal_dialog(dialog)
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()

    assert dialog_key not in widget._owned_modal_dialogs


def test_oral_dialog_lifecycle_survives_background_gc_stress():
    app = _application()
    widget = OralNutritionWidget(role="doctor")
    planned_time = datetime(2026, 8, 30, 12, 0)

    for index in range(20):
        if index % 2:
            dialog = DietAssignmentDialog([_template()], parent=widget)
        else:
            dialog = OralFactDialog(
                planned_item={"meal": "Обед", "amount": 250, "planned_dt": planned_time},
                shift_date=datetime(2026, 8, 30, 8, 0),
                parent=widget,
            )
        widget._own_modal_dialog(dialog)
        QTimer.singleShot(0, dialog.reject)
        assert dialog.exec() == QDialog.Rejected

        collector = threading.Thread(target=gc.collect)
        collector.start()
        collector.join()

        widget._dispose_modal_dialog(dialog)
        app.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()

        collector = threading.Thread(target=gc.collect)
        collector.start()
        collector.join()

    assert widget._owned_modal_dialogs == {}


def test_oral_fact_early_window_does_not_cross_medical_day_start():
    _application()
    dialog = OralFactDialog(
        planned_item={"meal": "Завтрак", "amount": 250, "planned_dt": datetime(2026, 8, 30, 9, 0)},
        shift_date=datetime(2026, 8, 30, 8, 0),
    )

    assert dialog.allowed_start == datetime(2026, 8, 30, 8, 0)
    assert dialog.allowed_end == datetime(2026, 8, 31, 7, 59)

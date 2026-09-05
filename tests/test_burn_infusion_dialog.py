from __future__ import annotations

from datetime import datetime, timedelta
import os
from pathlib import Path
import sys
import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from PySide6.QtCore import QCoreApplication, QDateTime, QEvent, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from rem_card.ui.shared.components.burn_infusion_calculator import (  # noqa: E402
    BurnInfusionCalculatorDialog,
)


def application() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def isolated_burn_dialog_settings(tmp_path, monkeypatch):
    app = application()
    settings = QSettings(str(tmp_path / "burn.ini"), QSettings.IniFormat)
    monkeypatch.setattr(BurnInfusionCalculatorDialog, "_settings", lambda self: settings)
    yield
    for widget in app.topLevelWidgets():
        if isinstance(widget, BurnInfusionCalculatorDialog):
            widget.close()
            widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def filled_dialog(context=None):
    now = datetime(2026, 9, 5, 14)
    dialog = BurnInfusionCalculatorDialog(patient_context={
        "age_years": 42, "weight_kg": 80, "as_of": now, **(context or {}),
    })
    dialog.injury_datetime_edit.setDateTime(QDateTime(now - timedelta(hours=3)))
    dialog.total_tbsa_spin.setValue(35)
    dialog.superficial_tbsa_spin.setValue(20)
    dialog.deep_tbsa_spin.setValue(15)
    return dialog


def test_dialog_prefills_context_and_renders_reference_example():
    app = application()
    dialog = BurnInfusionCalculatorDialog(
        patient_context={
            "display_name": "Иванов Иван Иванович",
            "history_number": "12345",
            "mkb_code": "T31.5",
            "age_years": 42,
            "weight_kg": 80,
            "urine_last_hour_ml": 25,
            "urine_average_3h_ml": 31,
        }
    )
    dialog.injury_datetime_edit.setDateTime(QDateTime(datetime.now() - timedelta(hours=3)))
    dialog.total_tbsa_spin.setValue(35)
    dialog.superficial_tbsa_spin.setValue(20)
    dialog.deep_tbsa_spin.setValue(15)
    dialog.inhalation_check.setChecked(True)
    dialog.infused_spin.setValue(1000)

    dialog._calculate()
    app.processEvents()

    assert dialog.total_value_label.text() == "12 880 мл"
    assert dialog.current_card[1].text() == "5 440 мл"
    assert dialog.copy_button.isEnabled()
    assert not dialog.transfer_button.isEnabled()
    assert "T31.5" in dialog.patient_context_label.text()
    assert "ниже целевого" in dialog.warning_label.text()

    style = dialog.styleSheet()
    assert "QComboBox::down-arrow" in style
    assert "QDateTimeEdit::drop-down" in style
    assert "QDoubleSpinBox::up-button" in style
    assert "combo_arrow_down.svg" in style
    assert "spin_arrow_up.svg" in style
    assert "spin_arrow_down.svg" in style

    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_dialog_is_resizable_and_restores_saved_geometry(tmp_path, monkeypatch):
    app = application()
    settings = QSettings(str(tmp_path / "burn-calculator.ini"), QSettings.IniFormat)
    monkeypatch.setattr(BurnInfusionCalculatorDialog, "_settings", lambda _self: settings)

    first = BurnInfusionCalculatorDialog()
    first.resize(1040, 730)
    assert first.isSizeGripEnabled()
    assert first.maximumWidth() > first.minimumWidth()
    assert first.maximumHeight() > first.minimumHeight()
    saved_geometry = first.saveGeometry()
    first.accept()
    assert settings.value(first._GEOMETRY_SETTINGS_KEY) == saved_geometry

    restored = BurnInfusionCalculatorDialog()
    # Offscreen Qt clamps widths to its 800 px virtual screen, while the dialog
    # intentionally keeps a safe 900 px minimum. Height remains directly verifiable.
    assert restored.size().width() >= restored.minimumWidth()
    assert restored.size().height() == 730
    restored.close()

    settings.clear()
    app.processEvents()


def test_dialog_formats_age_and_uses_automatic_adult_reduction():
    app = application()
    dialog = BurnInfusionCalculatorDialog(patient_context={"age_years": 56.5, "weight_kg": 80})

    assert dialog.age_spin.text() == "56 лет"
    for value, expected in ((0.5, "6 месяцев"), (1, "1 год"), (2, "2 года"), (5, "5 лет"), (21, "21 год")):
        dialog.age_spin.setValue(value)
        assert dialog.age_spin.text() == expected
    assert dialog.age_spin.valueFromText("6 месяцев") == 0.5

    dialog.age_spin.setValue(56.5)
    assert "1,75 раза" in dialog.patient_profile_label.text()
    assert dialog.pediatric_details_label.isHidden()

    dialog.injury_datetime_edit.setDateTime(QDateTime(datetime.now() - timedelta(hours=3)))
    dialog.total_tbsa_spin.setValue(20)
    dialog.superficial_tbsa_spin.setValue(10)
    dialog.deep_tbsa_spin.setValue(10)
    dialog._calculate()

    assert dialog._last_result is not None
    assert dialog._last_result.age_reduction_divisor == 1.75
    dialog.close()
    app.processEvents()


def test_dialog_shows_complete_pediatric_profile_and_dashes_for_missing_data():
    app = application()
    empty = BurnInfusionCalculatorDialog()
    assert empty.age_spin.text() == "—"
    assert empty.weight_spin.text() == "—"
    assert empty.urine_last_hour_spin.text() == "—"
    assert empty.urine_average_spin.text() == "—"
    empty.close()

    child = BurnInfusionCalculatorDialog(patient_context={"age_years": 4.5, "weight_kg": 20})
    assert child.age_spin.text() == "4 года"
    assert child.patient_profile_label.text() == "Ребёнок · 3 мл/кг × площадь ожога"
    assert not child.pediatric_details_label.isHidden()
    assert "2–5 лет" in child.pediatric_details_label.text()
    assert "80 мл/кг/сут" in child.pediatric_details_label.text()
    assert "1 600 мл/сут" in child.pediatric_details_label.text()

    child.injury_datetime_edit.setDateTime(QDateTime(datetime.now() - timedelta(hours=2)))
    child.total_tbsa_spin.setValue(20)
    child.superficial_tbsa_spin.setValue(10)
    child.deep_tbsa_spin.setValue(10)
    child._calculate()
    assert child._last_result is not None
    assert child._last_result.burn_formula_ml == 1200
    assert child._last_result.maintenance_ml == 1600
    assert child._last_result.total_ml == 2800
    assert "Ожоговая в/в составляющая" in child.breakdown_label.text()
    assert "Физиологическая потребность ребёнка" in child.breakdown_label.text()
    assert "Суммарный ориентир жидкостной терапии" in child.current_card[2].text()
    assert "Суммарный ориентир жидкостной терапии" in child._result_as_text()

    child.close()
    app.processEvents()


def test_dialog_preserves_one_month_boundary_for_pediatric_formula():
    app = application()
    infant = BurnInfusionCalculatorDialog(patient_context={"age_years": 1 / 12, "weight_kg": 5})

    assert infant.age_spin.text() == "1 месяц"
    assert "1 месяц–1 год" in infant.pediatric_details_label.text()
    assert "120 мл/кг/сут" in infant.pediatric_details_label.text()

    infant.injury_datetime_edit.setDateTime(QDateTime(datetime.now() - timedelta(hours=1)))
    infant.total_tbsa_spin.setValue(10)
    infant.superficial_tbsa_spin.setValue(5)
    infant.deep_tbsa_spin.setValue(5)
    infant._calculate()
    assert infant._last_result is not None
    assert infant._last_result.maintenance_ml == 600

    infant.close()
    app.processEvents()


def test_dialog_prefills_and_resets_monitoring_from_card(tmp_path, monkeypatch):
    app = application()
    settings = QSettings(str(tmp_path / "monitoring.ini"), QSettings.IniFormat)
    monkeypatch.setattr(BurnInfusionCalculatorDialog, "_settings", lambda self: settings)
    dialog = BurnInfusionCalculatorDialog(patient_context={
        "age_years": 42, "weight_kg": 80,
        "infused_ml": 250, "urine_last_hour_ml": 100, "urine_average_3h_ml": 200,
        "infused_source": "Выполненные назначения: 04.09 08:00–04.09 13:30.",
    })
    assert dialog.infused_spin.value() == 250
    assert dialog.urine_last_hour_spin.value() == 100
    assert dialog.urine_average_spin.value() == 200
    assert "08:00" in dialog.infused_source_label.text()
    dialog.injury_datetime_edit.setDateTime(QDateTime(datetime.now() - timedelta(hours=3)))
    dialog.total_tbsa_spin.setValue(20)
    dialog._calculate()
    assert dialog._last_result is not None
    assert dialog._last_result.remaining_ml == dialog._last_result.total_ml - 250
    dialog.infused_spin.setValue(1000)
    dialog._reset_form()
    assert dialog.infused_spin.value() == 250
    assert dialog.urine_average_spin.value() == 200
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_dialog_requires_manual_volume_after_read_failure(tmp_path, monkeypatch):
    app = application()
    settings = QSettings(str(tmp_path / "monitoring-error.ini"), QSettings.IniFormat)
    monkeypatch.setattr(BurnInfusionCalculatorDialog, "_settings", lambda self: settings)
    dialog = BurnInfusionCalculatorDialog(patient_context={
        "age_years": 42, "weight_kg": 80, "infused_load_failed": True,
        "infused_source": "Не удалось загрузить введённый объём. Укажите вручную.",
    })
    assert dialog.infused_spin.text() == "—"
    dialog._calculate()
    assert dialog._last_result is None
    assert "не загружены" in dialog.validation_label.text()
    dialog.infused_spin.setValue(250)
    dialog.injury_datetime_edit.setDateTime(QDateTime(datetime.now() - timedelta(hours=3)))
    dialog.total_tbsa_spin.setValue(20)
    dialog._calculate()
    assert dialog._last_result is not None
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


@pytest.mark.parametrize("change", [
    lambda d: d.weight_spin.setValue(40), lambda d: d.age_spin.setValue(60),
    lambda d: d.total_tbsa_spin.setValue(40), lambda d: d.deep_tbsa_spin.setValue(10),
    lambda d: d.superficial_tbsa_spin.setValue(10), lambda d: d.infused_spin.setValue(500),
    lambda d: d.urine_last_hour_spin.setValue(100), lambda d: d.urine_average_spin.setValue(200),
    lambda d: d.inhalation_check.setChecked(True), lambda d: d.electrical_check.setChecked(True),
    lambda d: d.burn_shock_check.setChecked(False),
    lambda d: d.injury_datetime_edit.setDateTime(d.injury_datetime_edit.dateTime().addSecs(-3600)),
    lambda d: d.mode_buttons["day_2_3"].setChecked(True),
])
def test_every_input_change_clears_visible_and_copyable_result(change):
    dialog = filled_dialog()
    dialog._calculate()
    assert dialog._last_result is not None
    change(dialog)
    assert dialog._last_result is None
    assert dialog.total_value_label.text() == "—"
    assert dialog.current_card[1].text() == "—"
    assert not dialog.copy_button.isEnabled()
    assert dialog._result_as_text() == ""


def test_validation_failure_does_not_leave_previous_numbers():
    dialog = filled_dialog()
    dialog._calculate()
    assert dialog._last_result is not None
    dialog.total_tbsa_spin.blockSignals(True)
    dialog.total_tbsa_spin.setValue(0)
    dialog.total_tbsa_spin.blockSignals(False)
    dialog._calculate()
    assert dialog.validation_label.text()
    assert dialog.total_value_label.text() == "—"
    assert not dialog.copy_button.isEnabled()


def test_period_volume_load_is_lazy_cached_and_refresh_preserves_manual_values():
    calls = []
    now = datetime(2026, 9, 5, 14)
    def loader(**kwargs):
        calls.append(kwargs)
        return {"as_of": now, "infused_ml": 250, "period_infused_ml": 1000,
                "period_start": kwargs["injury"], "period_end": kwargs["injury"] + timedelta(days=1),
                "urine_last_hour_ml": 100, "urine_average_3h_ml": 200}
    dialog = filled_dialog({"monitoring_loader": loader, "infused_ml": 250})
    assert calls == []
    assert dialog.card_infused_label.text() == "250 мл"
    assert dialog.infused_spin.value() == -1
    dialog._calculate()
    assert len(calls) == 1
    assert calls[0]["now"] == now
    assert dialog._last_result.remaining_ml == 11200 - 1000
    assert dialog.infused_spin.value() == 1000
    dialog.weight_spin.setValue(70)
    dialog._calculate()
    assert len(calls) == 1
    dialog.infused_spin.setValue(1500)
    dialog.urine_average_spin.setValue(300)
    dialog._refresh_monitoring()
    assert len(calls) == 2
    assert dialog.infused_spin.value() == 1500
    assert dialog.urine_average_spin.value() == 300
    assert "Вручную" in dialog.infused_source_label.text()
    assert dialog._last_result is None
    dialog.injury_datetime_edit.setDateTime(QDateTime(now - timedelta(hours=4)))
    assert dialog.infused_spin.value() == -1
    dialog._calculate()
    assert len(calls) == 3
    assert dialog.infused_spin.value() == 1000


def test_undo_manual_monitoring_restores_snapshot_without_resetting_burn():
    dialog = filled_dialog({"infused_ml": 250, "urine_last_hour_ml": 100})
    dialog.infused_spin.setValue(1000)
    dialog.urine_last_hour_spin.setValue(200)
    assert dialog.restore_monitoring_button.isEnabled()
    dialog._restore_monitoring()
    assert dialog.infused_spin.value() == 250
    assert dialog.urine_last_hour_spin.value() == 100
    assert dialog.total_tbsa_spin.value() == 35
    assert not dialog.restore_monitoring_button.isEnabled()


def test_failed_refresh_clears_automatic_values_and_does_not_present_zero():
    now = datetime(2026, 9, 5, 14)
    def loader(**kwargs):
        return {"as_of": now, "infused_load_failed": True, "period_error": "Объём не загружен"}
    dialog = filled_dialog({"monitoring_loader": loader, "infused_ml": 250, "urine_last_hour_ml": 100})
    dialog._refresh_monitoring()
    assert dialog.card_infused_label.text() == "—"
    assert dialog.infused_spin.text() == "—"
    assert dialog.urine_last_hour_spin.text() == "—"
    dialog._calculate()
    assert dialog._last_result is None
    assert "не загружены" in dialog.validation_label.text()


def test_child_oral_intake_is_visible_required_and_invalidates_result():
    dialog = filled_dialog({"age_years": 4, "weight_kg": 20, "oral_ml": 400})
    assert not dialog.oral_spin.isHidden()
    dialog._calculate()
    assert dialog._last_result is not None
    assert dialog._last_result.remaining_ml == dialog._last_result.total_ml - 400
    dialog.oral_spin.setValue(500)
    assert dialog._last_result is None
    dialog.oral_spin.setValue(-1)
    dialog._calculate()
    assert "энтеральное" in dialog.validation_label.text()
    dialog.age_spin.setValue(42)
    assert dialog.oral_spin.isHidden()


@pytest.mark.parametrize("hours,start,end", [(30, "24 ч", "48 ч"), (54, "48 ч", "72 ч")])
def test_timeline_uses_selected_treatment_day(hours, start, end):
    dialog = filled_dialog()
    dialog.injury_datetime_edit.setDateTime(QDateTime(dialog._as_of - timedelta(hours=hours)))
    dialog.mode_buttons["day_2_3"].setChecked(True)
    dialog._calculate()
    assert dialog._last_result is not None
    assert dialog.timeline_progress.value() == 60
    assert dialog.timeline_start_label.text() == start
    assert dialog.timeline_end_label.text() == end

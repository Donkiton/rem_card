from datetime import datetime
from types import SimpleNamespace

from rem_card.ui.doctor_view.doctor_remcard_widget import DoctorRemCardWidget
from rem_card.ui.nurse_view.nurse_main_widget import NurseMainWidget


SHIFT_DATE = datetime(2026, 8, 30, 8, 0)


def _oral_widget():
    events = [SimpleNamespace(event_time=SHIFT_DATE, amount_ml=250)]
    planned_rows = [{"time": "09:00", "amount": 400}]
    return SimpleNamespace(
        admission_id=7,
        shift_date=SHIFT_DATE,
        _snapshot={"events": events, "planned_rows": planned_rows},
    ), events, planned_rows


def test_doctor_balance_reads_events_and_plan_from_current_oral_snapshot():
    widget, events, planned_rows = _oral_widget()
    owner = SimpleNamespace(
        admission_id=7,
        _current_date=SHIFT_DATE,
        diet_intake_widget=widget,
    )

    assert DoctorRemCardWidget._local_oral_state_for_balance(owner) == (events, planned_rows)


def test_nurse_balance_reads_events_and_plan_from_current_oral_snapshot():
    widget, events, planned_rows = _oral_widget()
    owner = SimpleNamespace(
        layout_manager=SimpleNamespace(current_admission_id=7),
        _current_date=SHIFT_DATE,
        diet_intake_widget=widget,
    )

    assert NurseMainWidget._local_oral_state_for_balance(owner) == (events, planned_rows)


def test_unloaded_oral_widget_does_not_override_service_balance_with_empty_state():
    widget = SimpleNamespace(admission_id=7, shift_date=SHIFT_DATE, _snapshot={})
    owner = SimpleNamespace(
        admission_id=7,
        _current_date=SHIFT_DATE,
        diet_intake_widget=widget,
    )

    assert DoctorRemCardWidget._local_oral_state_for_balance(owner) is None

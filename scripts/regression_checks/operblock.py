"""Safety-сценарии: operblock."""

from __future__ import annotations

from typing import Any
from pathlib import Path
from datetime import datetime
import json
import os
import re
import time


def _check_operblock_medication_aliases_quick_search(temp_root: str) -> tuple[bool, str]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from rem_card.services.operblock_medication_presets import (
        DILUENTS_SEED_FILE,
        DRUGS_SEED_FILE,
        OPERBLOCK_MEDICATION_PRESETS_FILE,
        OPERBLOCK_MEDICATION_PRESETS_OVERRIDE_KEY,
        load_operblock_medication_presets,
        save_operblock_medication_presets,
    )
    import rem_card.ui.operblock_view.operblock_main_widget as operblock_widget_module
    from rem_card.ui.operblock_view.operblock_main_widget import OperBlockMainWidget

    seed_dir = os.path.join(temp_root, "seed")
    user_dict_dir = os.path.join(temp_root, "user")
    os.makedirs(seed_dir, exist_ok=True)
    os.makedirs(user_dict_dir, exist_ok=True)
    for filename, payload in (
        (DRUGS_SEED_FILE, {}),
        (DILUENTS_SEED_FILE, {}),
        (OPERBLOCK_MEDICATION_PRESETS_FILE, {"items": []}),
    ):
        with open(os.path.join(seed_dir, filename), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
    with open(os.path.join(user_dict_dir, "user_overrides.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {
                OPERBLOCK_MEDICATION_PRESETS_OVERRIDE_KEY: {
                    "version": 1,
                    "include_opblock_seed": False,
                    "include_quick_orders_compat": False,
                    "items": [],
                }
            },
            fh,
            ensure_ascii=False,
        )

    save_operblock_medication_presets(
        [
            {
                "preset_id": "manual:bolus:propofol",
                "label": "Propofol",
                "display_name": "S. Propofoli",
                "aliases": ["диприван", "проф"],
                "kind": "bolus",
                "drug_group": "regression_anesthetics",
                "enabled": True,
                "sort_order": 10,
            },
            {
                "preset_id": "manual:timed_infusion:mezaton",
                "label": "Mezaton",
                "display_name": "S. Phenylephrini 1%",
                "aliases": ["меза"],
                "kind": "timed_infusion",
                "drug_group": "regression_vasopressors",
                "enabled": True,
                "sort_order": 20,
            }
        ],
        user_dict_dir=user_dict_dir,
    )
    presets = load_operblock_medication_presets(
        seed_dir=seed_dir,
        user_dict_dir=user_dict_dir,
        include_disabled=True,
    )
    aliases_by_id = {str(item.get("preset_id") or ""): item.get("aliases") for item in presets}
    if aliases_by_id.get("manual:bolus:propofol") != ["диприван", "проф"]:
        return False, f"aliases were not preserved: {presets!r}"
    if aliases_by_id.get("manual:timed_infusion:mezaton") != ["меза"]:
        return False, f"timed infusion aliases were not preserved: {presets!r}"

    widget = OperBlockMainWidget.__new__(OperBlockMainWidget)
    widget._medication_presets = presets
    widget._preset_search_text = ""
    widget._preset_kind_filter = "bolus"
    widget._quick_order_filter_buttons = [
        {"key": "bolus", "label": "Болюсы", "built_in": True, "sort_order": 10},
    ]
    group_load_calls = 0
    original_group_loader = operblock_widget_module.load_operblock_drug_groups

    def fake_group_loader():
        nonlocal group_load_calls
        group_load_calls += 1
        return [
            {"code": "regression_anesthetics", "label": "Анестетики"},
            {"code": "regression_vasopressors", "label": "Вазопрессоры"},
        ]

    try:
        operblock_widget_module.load_operblock_drug_groups = fake_group_loader
        OperBlockMainWidget._rebuild_quick_order_search_index(widget)
        if group_load_calls != 1:
            return False, f"search index should load drug groups once, got {group_load_calls}"
        group_load_calls = 0
        result = OperBlockMainWidget._filtered_medication_presets(widget)
        if [item.get("preset_id") for item in result] != ["manual:bolus:propofol"]:
            return False, f"empty search should keep active bolus tab filter: {result!r}"
        widget._preset_search_text = "диприв"
        result = OperBlockMainWidget._filtered_medication_presets(widget)
        if [item.get("preset_id") for item in result] != ["manual:bolus:propofol"]:
            return False, f"quick search did not find preset by alias: {result!r}"
        widget._preset_search_text = "меза"
        result = OperBlockMainWidget._filtered_medication_presets(widget)
        if [item.get("preset_id") for item in result] != ["manual:timed_infusion:mezaton"]:
            return False, f"quick search was limited by active bolus tab: {result!r}"
        if group_load_calls != 0:
            return False, f"quick search reloaded drug groups while typing: {group_load_calls}"
    finally:
        operblock_widget_module.load_operblock_drug_groups = original_group_loader
    return True, "ok"


def _prepare_operblock_operation_stage_case(context: dict[str, Any]) -> tuple[bool, str]:
    from datetime import date, timedelta

    service = context["service"]
    vital_dto_cls = context["vital_dto_cls"]
    widget_cls = context["widget_cls"]
    no_vitals_case = service.create_operation_case(
        {
            "table_code": "planned",
            "history_number": "REGSTAGE0",
            "full_name": "Без Виталов",
            "gender": "м",
            "birth_date": date(1980, 1, 1),
            "diagnosis_code": "K35",
            "diagnosis_text": "Острый аппендицит",
        }
    )
    no_vitals_case_id = int(no_vitals_case["operation_case_id"])
    no_vitals_defaults = service.build_operblock_patient_header_snapshot(no_vitals_case_id)
    no_vitals_started_at = datetime.fromisoformat(str(no_vitals_defaults["started_at"]).replace(" ", "T")).replace(
        second=0,
        microsecond=0,
    )
    no_vitals_anesthesia_time = no_vitals_started_at + timedelta(minutes=7)
    try:
        service.start_anesthesia(no_vitals_case_id, "ОА", event_time=no_vitals_anesthesia_time)
    except ValueError as exc:
        if "Перед началом пособия" not in str(exc):
            return False, f"unexpected anesthesia without vitals error: {exc}"
    else:
        return False, "anesthesia was started without initial vitals"

    case = service.create_operation_case(
        {
            "table_code": "emergency",
            "history_number": "REGSTAGE1",
            "full_name": "Тестов Пациент",
            "gender": "м",
            "birth_date": date(1980, 1, 1),
            "diagnosis_code": "K35",
            "diagnosis_text": "Острый аппендицит",
        }
    )
    admission_id = int(case["admission_id"])
    case_id = int(case["operation_case_id"])
    case_defaults = service.build_operblock_patient_header_snapshot(case_id)
    case_started_at = datetime.fromisoformat(str(case_defaults["started_at"]).replace(" ", "T")).replace(
        second=0,
        microsecond=0,
    )
    vital_time = case_started_at + timedelta(minutes=10)
    service.add_vital_record(
        vital_dto_cls(id=None, admission_id=admission_id, timestamp=vital_time, sys=120, dia=80, pulse=70, spo2=98)
    )
    default_widget = widget_cls.__new__(widget_cls)
    default_widget.operblock_service = service
    default_widget._current_operation_start = case_started_at
    default_widget._current_protocol_date = case_started_at
    default_anesthesia_time = widget_cls._default_anesthesia_start_datetime(default_widget, case_id)
    if default_anesthesia_time != vital_time + timedelta(minutes=5):
        return False, f"anesthesia default time is not latest vitals + 5 min: {default_anesthesia_time!r}"
    default_widget._current_anesthesia_start = case_started_at
    default_surgery_time = widget_cls._default_surgery_start_datetime(default_widget)
    if default_surgery_time != case_started_at + timedelta(minutes=5):
        return False, f"surgery default time is not anesthesia start + 5 min: {default_surgery_time!r}"
    service.start_anesthesia(case_id, "ОА", event_time=case_started_at)
    service.start_surgery(
        case_id,
        operation_name="Операция",
        surgeons=["Хирург"],
        event_time=default_surgery_time,
    )
    context.update(
        {
            "admission_id": admission_id,
            "case_id": case_id,
            "case_started_at": case_started_at,
            "default_anesthesia_time": default_anesthesia_time,
            "default_surgery_time": default_surgery_time,
        }
    )
    return True, "ok"


def _check_operblock_operation_stage_lifecycle(context: dict[str, Any]) -> tuple[bool, str]:
    from datetime import timedelta

    service = context["service"]
    case_id = context["case_id"]
    admission_id = context["admission_id"]
    default_surgery_time = context["default_surgery_time"]
    added = service.add_operation_stage(
        case_id,
        "Аппендэктомия",
        event_time=default_surgery_time + timedelta(minutes=30),
    )
    if added.get("display_label") != "Аппендэктомия" or (added.get("payload") or {}).get("stage_kind") != "custom":
        return False, f"custom stage insert returned unexpected payload: {added!r}"
    snapshot_after_add = service.build_operblock_timeline_snapshot(admission_id, operation_case_id=case_id).to_dict()
    added_events = list(snapshot_after_add.get("operation_events") or [])
    if [event.get("display_label") for event in added_events] != [
        "Начало пособия",
        "Начало операции",
        "Аппендэктомия",
    ]:
        return False, f"operation stage order after add is wrong: {added_events!r}"

    auto_event_id = int(added_events[0].get("source_id") or 0)
    try:
        service.update_operation_stage(
            auto_event_id,
            "Другое начало",
            expected_revision=int(added_events[0].get("revision") or 0),
        )
    except ValueError as exc:
        if "Автоматические этапы" not in str(exc):
            return False, f"unexpected auto-stage edit error: {exc}"
    else:
        return False, "automatic operation stage was editable"

    surgery_start_dt = datetime.fromisoformat(str(added_events[1].get("event_time")).replace(" ", "T"))
    edited = service.update_operation_stage(
        int(added["source_id"]),
        "Лапароскопическая аппендэктомия",
        expected_revision=int(added["revision"]),
        event_time=surgery_start_dt + timedelta(minutes=60),
    )
    if int(edited.get("revision") or 0) != int(added.get("revision") or 0) + 1:
        return False, f"custom stage revision did not increase: added={added!r}, edited={edited!r}"
    second_moved_later = service.add_operation_stage(
        case_id,
        "Ревизия брюшной полости",
        event_time=surgery_start_dt + timedelta(minutes=90),
    )
    if datetime.fromisoformat(str(second_moved_later.get("event_time")).replace(" ", "T")) != surgery_start_dt + timedelta(
        minutes=90
    ):
        return False, f"custom stage insert ignored explicit event_time: {second_moved_later!r}"
    snapshot_after_edit = service.build_operblock_timeline_snapshot(admission_id, operation_case_id=case_id).to_dict()
    edited_events = list(snapshot_after_edit.get("operation_events") or [])
    labels_after_edit = [event.get("display_label") for event in edited_events]
    if labels_after_edit != [
        "Начало пособия",
        "Начало операции",
        "Лапароскопическая аппендэктомия",
        "Ревизия брюшной полости",
    ]:
        return False, f"operation stage label was not updated in snapshot: {labels_after_edit!r}"

    second_moved_earlier = service.update_operation_stage(
        int(second_moved_later["source_id"]),
        "Ревизия брюшной полости",
        expected_revision=int(second_moved_later["revision"]),
        event_time=surgery_start_dt + timedelta(minutes=59),
    )
    snapshot_after_reorder = service.build_operblock_timeline_snapshot(
        admission_id,
        operation_case_id=case_id,
    ).to_dict()
    reordered_labels = [event.get("display_label") for event in snapshot_after_reorder.get("operation_events") or []]
    if reordered_labels != [
        "Начало пособия",
        "Начало операции",
        "Ревизия брюшной полости",
        "Лапароскопическая аппендэктомия",
    ]:
        return False, f"operation stage time edit did not reorder stages: {reordered_labels!r}"
    second_moved_before_surgery = service.update_operation_stage(
        int(second_moved_earlier["source_id"]),
        "Ревизия брюшной полости",
        expected_revision=int(second_moved_earlier["revision"]),
        event_time=surgery_start_dt - timedelta(minutes=1),
    )
    snapshot_before_surgery = service.build_operblock_timeline_snapshot(
        admission_id,
        operation_case_id=case_id,
    ).to_dict()
    labels_before_surgery = [
        event.get("display_label")
        for event in snapshot_before_surgery.get("operation_events") or []
    ]
    if labels_before_surgery != [
        "Начало пособия",
        "Ревизия брюшной полости",
        "Начало операции",
        "Лапароскопическая аппендэктомия",
    ]:
        return False, f"before-surgery custom stage order is wrong: {labels_before_surgery!r}"
    anesthesia_start_dt = datetime.fromisoformat(str(added_events[0].get("event_time")).replace(" ", "T"))
    try:
        service.update_operation_stage(
            int(second_moved_before_surgery["source_id"]),
            "Ревизия брюшной полости",
            expected_revision=int(second_moved_before_surgery["revision"]),
            event_time=anesthesia_start_dt - timedelta(minutes=1),
        )
    except ValueError as exc:
        if "раньше начала пособия" not in str(exc) and "раньше поступления пациента" not in str(exc):
            return False, f"unexpected before-anesthesia stage time error: {exc}"
    else:
        return False, "custom stage time was moved before anesthesia start"

    context.update(
        {
            "added_events": added_events,
            "surgery_start_dt": surgery_start_dt,
            "edited": edited,
            "second_moved_later": second_moved_later,
            "second_moved_before_surgery": second_moved_before_surgery,
            "snapshot_after_edit": snapshot_after_edit,
            "edited_events": edited_events,
            "labels_before_surgery": labels_before_surgery,
        }
    )
    return True, "ok"


def _check_operblock_stage_afternoon_dialog(context: dict[str, Any], app) -> tuple[bool, str]:
    surgery_start_dt = context["surgery_start_dt"]
    dialog = context["time_edit_dialog_cls"](
        surgery_start_dt.replace(hour=17, minute=40),
        min_datetime=surgery_start_dt.replace(hour=8, minute=0),
        stage_label="Проверка даты этапа",
    )
    try:
        dialog.time_input.setText("15:00")
        afternoon_dt = datetime.fromisoformat(dialog.datetime_text())
        if afternoon_dt.date() != surgery_start_dt.date() or afternoon_dt.hour != 15 or afternoon_dt.minute != 0:
            return False, f"operation stage afternoon time was moved to wrong date: {afternoon_dt!r}"
        return True, "ok"
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def _check_operblock_operation_stage_dialogs(context: dict[str, Any]) -> tuple[bool, str]:
    from datetime import timedelta

    surgery_start_dt = context["surgery_start_dt"]
    edited = context["edited"]
    second_moved_later = context["second_moved_later"]
    second_moved_before_surgery = context["second_moved_before_surgery"]
    added_events = context["added_events"]
    app = context["application_cls"].instance() or context["application_cls"]([])
    start_dialog = context["start_anesthesia_dialog_cls"](
        [{"label": "ОА"}],
        ["Анестезиолог"],
        ["Анестезист"],
        initial_start_datetime=context["default_anesthesia_time"],
        min_start_datetime=context["case_started_at"],
    )
    try:
        if start_dialog.time_input.text() != context["default_anesthesia_time"].strftime("%H:%M"):
            return False, "start anesthesia dialog did not show default time"
        edited_start_dt = context["default_anesthesia_time"] + timedelta(minutes=20)
        start_dialog.time_input.setText(edited_start_dt.strftime("%H:%M"))
        selected_start_dt = datetime.fromisoformat(start_dialog.start_datetime_text())
        if selected_start_dt != edited_start_dt:
            return False, f"start anesthesia dialog resolved edited time to wrong datetime: {selected_start_dt!r}"
    finally:
        start_dialog.close()
        start_dialog.deleteLater()
        app.processEvents()

    start_surgery_dialog = context["start_surgery_dialog_cls"](
        ["Хирург"],
        ["Операционная медсестра"],
        initial_operation_name="Операция",
        initial_surgeons=["Хирург"],
        initial_operating_nurse="Операционная медсестра",
        initial_start_datetime=surgery_start_dt,
        min_start_datetime=context["case_started_at"],
    )
    try:
        if start_surgery_dialog.time_input.text() != surgery_start_dt.strftime("%H:%M"):
            return False, "start surgery dialog did not show default time"
        edited_surgery_dt = surgery_start_dt + timedelta(minutes=20)
        start_surgery_dialog.time_input.setText(edited_surgery_dt.strftime("%H:%M"))
        selected_surgery_dt = datetime.fromisoformat(start_surgery_dialog.start_datetime_text())
        if selected_surgery_dt != edited_surgery_dt:
            return False, f"start surgery dialog resolved edited time to wrong datetime: {selected_surgery_dt!r}"
    finally:
        start_surgery_dialog.close()
        start_surgery_dialog.deleteLater()
        app.processEvents()

    dialog = context["operation_stages_dialog_cls"](
        [
            {
                "kind": "anesthesia_start",
                "label": "Начало пособия",
                "event_id": int(added_events[0].get("source_id") or 0),
                "event_time": added_events[0].get("event_time"),
                "revision": int(added_events[0].get("revision") or 0),
                "readonly": True,
            },
            {
                "kind": "surgery_start",
                "label": "Начало операции",
                "event_id": int(added_events[1].get("source_id") or 0),
                "event_time": added_events[1].get("event_time"),
                "revision": int(added_events[1].get("revision") or 0),
                "readonly": True,
            },
            {
                "kind": "custom",
                "label": "Лапароскопическая аппендэктомия",
                "event_id": int(edited["source_id"]),
                "event_time": edited.get("event_time"),
                "revision": int(edited["revision"]),
                "readonly": False,
                "payload": {"stage_kind": "custom", "label": "Лапароскопическая аппендэктомия"},
            },
            {
                "kind": "custom",
                "label": "Ревизия брюшной полости",
                "event_id": int(second_moved_before_surgery["source_id"]),
                "event_time": second_moved_before_surgery.get("event_time"),
                "revision": int(second_moved_before_surgery["revision"]),
                "readonly": False,
                "payload": {"stage_kind": "custom", "label": "Ревизия брюшной полости"},
            },
        ]
    )
    new_widgets = dialog._row_widgets.get("new") or {}
    new_row = new_widgets.get("row") or {}
    new_time_label = new_widgets.get("time_label")
    if not str(new_row.get("event_time") or ""):
        return False, "new operation stage row has no pending event_time"
    if new_time_label is None or not re.fullmatch(r"\d{2}:\d{2}", str(new_time_label.text() or "")):
        return False, "new operation stage row does not show editable current time"
    pending_time = (surgery_start_dt + timedelta(minutes=120)).isoformat(timespec="seconds")
    dialog.apply_pending_stage_time("new", pending_time)
    if (dialog._row_widgets.get("new") or {}).get("row", {}).get("event_time") != pending_time:
        return False, "new operation stage pending time was not updated in dialog row"
    if str(new_time_label.text() or "") != (surgery_start_dt + timedelta(minutes=120)).strftime("%H:%M"):
        return False, "new operation stage pending time label was not updated"

    ok, details = _check_operblock_stage_afternoon_dialog(context, app)
    if not ok:
        return False, details

    midnight_dialog = context["time_edit_dialog_cls"](
        surgery_start_dt.replace(hour=23, minute=40),
        min_datetime=surgery_start_dt.replace(hour=22, minute=0),
        stage_label="Переход через полночь",
    )
    try:
        midnight_dialog.time_input.setText("00:15")
        midnight_dt = datetime.fromisoformat(midnight_dialog.datetime_text())
        expected_midnight = (surgery_start_dt + timedelta(days=1)).date()
        if midnight_dt.date() != expected_midnight or midnight_dt.hour != 0 or midnight_dt.minute != 15:
            return False, f"operation stage midnight time did not resolve to next day: {midnight_dt!r}"
    finally:
        midnight_dialog.close()
        midnight_dialog.deleteLater()
        app.processEvents()

    render_calls = 0
    original_render_rows = dialog._render_rows

    def _count_render_rows():
        nonlocal render_calls
        render_calls += 1
        return original_render_rows()

    dialog._render_rows = _count_render_rows
    target_key = f"event:{int(edited['source_id'])}"
    second_key = f"event:{int(second_moved_later['source_id'])}"
    before_widget_ids = {
        key: id(widgets.get("frame"))
        for key, widgets in (dialog._row_widgets or {}).items()
        if key in {target_key, second_key}
    }
    dialog.apply_saved_stage(
        target_key,
        {
            "source_id": int(edited["source_id"]),
            "display_label": "Переименованный этап",
            "raw_text": "Переименованный этап",
            "event_time": edited.get("event_time"),
            "revision": int(edited["revision"]) + 1,
            "payload": {"stage_kind": "custom", "label": "Переименованный этап"},
        },
    )
    after_widget_ids = {
        key: id(widgets.get("frame"))
        for key, widgets in (dialog._row_widgets or {}).items()
        if key in {target_key, second_key}
    }
    if render_calls:
        return False, "operation stage rename rerendered all stage rows"
    if before_widget_ids != after_widget_ids:
        return False, "operation stage rename recreated unchanged row widgets"
    target_edit = (dialog._row_widgets.get(target_key) or {}).get("edit")
    second_edit = (dialog._row_widgets.get(second_key) or {}).get("edit")
    if target_edit is None or target_edit.text() != "Переименованный этап":
        return False, "operation stage rename did not update target row in place"
    if second_edit is None or second_edit.text() != "Ревизия брюшной полости":
        return False, "operation stage rename changed a different stage row"
    dialog.close()
    dialog.deleteLater()
    app.processEvents()
    return True, "ok"


def _check_operblock_operation_stage_ui(context: dict[str, Any]) -> tuple[bool, str]:
    import weakref

    widget_cls = context["widget_cls"]
    surgery_start_dt = context["surgery_start_dt"]
    second_moved_later = context["second_moved_later"]
    second_moved_before_surgery = context["second_moved_before_surgery"]
    snapshot_after_edit = context["snapshot_after_edit"]
    edited_events = context["edited_events"]
    labels_before_surgery = context["labels_before_surgery"]

    widget = widget_cls.__new__(widget_cls)
    widget._current_timeline_snapshot = dict(snapshot_after_edit)
    if not widget_cls._patch_operation_stage_event_locally(widget, second_moved_before_surgery):
        return False, "local UI stage patch returned false"
    patched_events = list((widget._current_timeline_snapshot or {}).get("operation_events") or [])
    patched_labels = [event.get("display_label") for event in patched_events]
    if patched_labels != labels_before_surgery:
        return False, f"local UI patch did not replace only target stage: {patched_labels!r}"
    if len(patched_events) != len(edited_events):
        return False, "local UI patch changed operation_events count"

    class _StageDialogSpy:
        def __init__(self):
            self.saved: list[tuple[str, dict]] = []

        def apply_saved_stage(self, row_key: str, stage: dict) -> None:
            self.saved.append((row_key, dict(stage or {})))

        def apply_save_error(self, row_key: str) -> None:
            raise AssertionError(f"unexpected stage save error for {row_key}")

    class _PatchOnlyChart:
        def __init__(self):
            self.start_time = surgery_start_dt
            self.calls: list[dict] = []

        def patch_operation_stage_marker(self, stage_event: dict, *, snapshot=None, start_time=None) -> bool:
            self.calls.append({"stage_event": dict(stage_event or {}), "snapshot": snapshot, "start_time": start_time})
            return True

    def _unexpected_refresh(*args, **kwargs):
        raise AssertionError("operation stage save caused full protocol/chart refresh")

    save_widget = widget_cls.__new__(widget_cls)
    save_widget._write_pending = True
    save_widget._current_timeline_snapshot = dict(snapshot_after_edit)
    save_widget._current_operation_start = surgery_start_dt
    save_widget._current_protocol_date = surgery_start_dt
    save_widget.vitals_chart = _PatchOnlyChart()
    save_widget._apply_protocol_controls_state = lambda: None
    save_widget.refresh_protocol = _unexpected_refresh
    save_widget._update_vitals_chart_order_markers = _unexpected_refresh
    dialog_spy = _StageDialogSpy()
    try:
        widget_cls._on_operation_stage_saved(
            save_widget,
            weakref.ref(dialog_spy),
            f"event:{int(second_moved_later['source_id'])}",
            second_moved_before_surgery,
        )
    except AssertionError as exc:
        return False, str(exc)
    if not dialog_spy.saved:
        return False, "operation stage save did not update dialog locally"
    if len(save_widget.vitals_chart.calls) != 1:
        return False, f"single chart marker patch expected, got {len(save_widget.vitals_chart.calls)}"

    return _check_operblock_operation_stage_dialogs(context)


def _check_operblock_operation_stages_custom_events(temp_root: str) -> tuple[bool, str]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from datetime import timedelta

    from rem_card.app.operblock_schema import _apply_operblock_schema
    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.data.dto.remcard_dto import VitalDTO
    from rem_card.services.operblock_service import OperBlockService
    from rem_card.ui.operblock_view.operblock_main_widget import (
        OperBlockMainWidget,
        OperationStageTimeEditDialog,
        StartAnesthesiaDialog,
        StartSurgeryDialog,
        OperationStagesDialog,
    )
    from PySide6.QtWidgets import QApplication

    db_path = os.path.join(temp_root, "operblock_operation_stages.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        manager.run_write_operation(_apply_operblock_schema, source="regression_operblock_schema")
        service = OperBlockService(manager)
        context = {
            "service": service,
            "vital_dto_cls": VitalDTO,
            "widget_cls": OperBlockMainWidget,
        }
        for check in (
            _prepare_operblock_operation_stage_case,
            _check_operblock_operation_stage_lifecycle,
        ):
            ok, details = check(context)
            if not ok:
                return False, details
        case_id = context["case_id"]
        surgery_start_dt = context["surgery_start_dt"]

        context.update(
            {
                "application_cls": QApplication,
                "start_anesthesia_dialog_cls": StartAnesthesiaDialog,
                "start_surgery_dialog_cls": StartSurgeryDialog,
                "operation_stages_dialog_cls": OperationStagesDialog,
                "time_edit_dialog_cls": OperationStageTimeEditDialog,
            }
        )
        ok, details = _check_operblock_operation_stage_ui(context)
        if not ok:
            return False, details


        surgery_end_dt = surgery_start_dt + timedelta(minutes=180)
        after_surgery_stage_dt = surgery_end_dt + timedelta(minutes=1)
        service.end_surgery(case_id, event_time=surgery_end_dt)
        late_stage = service.add_operation_stage(
            case_id,
            "Контроль после окончания операции",
            event_time=after_surgery_stage_dt,
        )
        if datetime.fromisoformat(str(late_stage.get("event_time")).replace(" ", "T")) != after_surgery_stage_dt:
            return False, f"post-surgery stage has wrong time: {late_stage!r}"
        updated_late_stage = service.update_operation_stage(
            int(late_stage["source_id"]),
            "Финальный контроль после окончания операции",
            expected_revision=int(late_stage["revision"]),
            event_time=after_surgery_stage_dt + timedelta(minutes=1),
        )
        if int(updated_late_stage.get("revision") or 0) != int(late_stage.get("revision") or 0) + 1:
            return False, f"post-surgery stage revision did not increase: {updated_late_stage!r}"

        anesthesia_end_dt = surgery_end_dt + timedelta(minutes=10)
        service.end_anesthesia_with_transfer(
            case_id,
            "Хирургия",
            event_time=anesthesia_end_dt,
        )
        try:
            service.add_operation_stage(
                case_id,
                "Этап после завершения пособия",
                event_time=anesthesia_end_dt + timedelta(minutes=1),
            )
        except ValueError as exc:
            if "до завершения пособия" not in str(exc):
                return False, f"unexpected closed-anesthesia stage error: {exc}"
        else:
            return False, "custom stage was added after anesthesia end"
        return True, "ok"
    finally:
        manager.close()


def _check_operblock_rao_auto_transfer_recovery_beds_and_vitals(temp_root: str) -> tuple[bool, str]:
    from datetime import date, timedelta

    from rem_card.app.operblock_schema import _apply_operblock_schema
    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.data.dao.vitals_dao import VitalsDAO
    from rem_card.data.dto.remcard_dto import VitalDTO
    from rem_card.services.operblock_service import OperBlockService

    def _occupy_recovery_beds(manager: DatabaseManager, bed_numbers: tuple[int, ...]) -> None:
        with manager.remcard_transaction(source="regression_seed_occupied_recovery_beds") as cursor:
            for bed_number in (10, 11, 12):
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO beds(bed_number, status, current_admission_id, revision)
                    VALUES (?, 'FREE', NULL, 0)
                    """,
                    (bed_number,),
                )
            for index, bed_number in enumerate(bed_numbers, start=1):
                cursor.execute("INSERT INTO patients(full_name) VALUES (?)", (f"Occupied Recovery {bed_number}",))
                patient_id = int(cursor.lastrowid)
                cursor.execute(
                    """
                    INSERT INTO admissions(patient_id, bed_number, history_number, admission_datetime, is_active)
                    VALUES (?, ?, ?, ?, 1)
                    """,
                    (patient_id, bed_number, f"OCC-{index}", "2026-06-01T08:00:00"),
                )
                admission_id = int(cursor.lastrowid)
                cursor.execute(
                    """
                    UPDATE beds
                    SET status = 'OCCUPIED',
                        current_admission_id = ?,
                        revision = COALESCE(revision, 0) + 1
                    WHERE bed_number = ?
                    """,
                    (admission_id, bed_number),
                )

    def _finish_case_to_rao(
        manager: DatabaseManager,
        service: OperBlockService,
        *,
        history_number: str,
        clear_source_diagnosis: bool = False,
    ) -> tuple[int, int, datetime]:
        case = service.create_operation_case(
            {
                "table_code": "emergency",
                "history_number": history_number,
                "full_name": "Рао Тест Пациент",
                "gender": "м",
                "birth_date": date(1980, 1, 1),
                "diagnosis_code": "K35",
                "diagnosis_text": "Острый аппендицит",
                "department_profile": "Хирургия",
                "operation_name": "Аппендэктомия",
                "surgeons": ["Хирург"],
                "anesthesiologist": "Анестезиолог",
                "anesthetist": "Анестезист",
                "preop_sys": 111,
                "preop_dia": 71,
                "preop_pulse": 81,
                "preop_spo2": 97,
            }
        )
        admission_id = int(case["admission_id"])
        case_id = int(case["operation_case_id"])
        started_row = manager.fetch_one_remcard(
            "SELECT started_at FROM operation_cases WHERE id = ?",
            (case_id,),
        )
        started_at = datetime.fromisoformat(str(started_row["started_at"]).replace(" ", "T")).replace(second=0, microsecond=0)
        service.add_vital_record(
            VitalDTO(
                id=None,
                admission_id=admission_id,
                timestamp=started_at + timedelta(minutes=25),
                sys=123,
                dia=77,
                pulse=88,
                temp=36.7,
                spo2=99,
                rr=15,
                cvp=4,
            )
        )
        if clear_source_diagnosis:
            with manager.remcard_transaction(source="regression_clear_source_diagnosis") as cursor:
                cursor.execute("UPDATE admissions SET diagnosis_text = NULL WHERE id = ?", (admission_id,))

        service.start_anesthesia(
            case_id,
            "ОА",
            anesthesiologist="Анестезиолог",
            anesthetist="Анестезист",
            event_time=started_at + timedelta(minutes=5),
        )
        service.start_surgery(
            case_id,
            operation_name="Аппендэктомия",
            surgeons=["Хирург"],
            event_time=started_at + timedelta(minutes=10),
        )
        service.end_surgery(case_id, event_time=started_at + timedelta(minutes=30))
        anesthesia_end = started_at + timedelta(minutes=40)
        service.end_anesthesia_with_transfer(case_id, "РАО", event_time=anesthesia_end)
        return case_id, admission_id, anesthesia_end

    def _run_transfer_scenario(
        name: str,
        index: int,
        occupied_beds: tuple[int, ...],
        *,
        expected_bed: int | None,
        clear_source_diagnosis: bool = False,
        check_vitals: bool = False,
    ) -> tuple[bool, str]:
        db_path = os.path.join(temp_root, f"operblock_rao_transfer_{name}.db")
        manager = DatabaseManager(db_path, db_path)
        try:
            manager.run_write_operation(_apply_operblock_schema, source="regression_operblock_schema")
            _occupy_recovery_beds(manager, occupied_beds)
            service = OperBlockService(manager)
            case_id, source_admission_id, anesthesia_end = _finish_case_to_rao(
                manager,
                service,
                history_number=f"RAO{index:03d}",
                clear_source_diagnosis=clear_source_diagnosis,
            )
            case_row = manager.fetch_one_remcard(
                "SELECT transfer_department, future_rao_admission_id FROM operation_cases WHERE id = ?",
                (case_id,),
            )
            if not case_row or case_row["transfer_department"] != "РАО":
                return False, f"{name}: transfer_department was not saved as RAO"
            future_rao_admission_id = case_row["future_rao_admission_id"]
            if expected_bed is None:
                if future_rao_admission_id is not None:
                    return False, f"{name}: RAO admission was created unexpectedly: {future_rao_admission_id}"
                created = manager.fetch_one_remcard(
                    """
                    SELECT COUNT(*) AS count
                    FROM admissions
                    WHERE intake_extra_json LIKE '%operblock_rao_transfer%'
                    """
                )
                if int(created["count"] or 0) != 0:
                    return False, f"{name}: RAO transfer admission exists despite blocked creation"
                return True, "ok"

            if future_rao_admission_id is None:
                return False, f"{name}: RAO admission was not linked to operation case"
            admission_row = manager.fetch_one_remcard(
                """
                SELECT a.*, p.full_name, p.birth_date
                FROM admissions a
                JOIN patients p ON p.id = a.patient_id
                WHERE a.id = ?
                """,
                (int(future_rao_admission_id),),
            )
            if not admission_row:
                return False, f"{name}: linked RAO admission was not found"
            if int(admission_row["bed_number"]) != expected_bed:
                return False, f"{name}: expected bed {expected_bed}, got {admission_row['bed_number']}"
            expected_admission_dt = (anesthesia_end + timedelta(minutes=10)).isoformat(timespec="seconds")
            if str(admission_row["admission_datetime"]) != expected_admission_dt:
                return False, f"{name}: wrong RAO admission time: {admission_row['admission_datetime']!r}"
            if admission_row["source_department"] != "Профильное отделение":
                return False, f"{name}: wrong source department: {admission_row['source_department']!r}"
            if admission_row["department_profile"] != "Хирургия":
                return False, f"{name}: wrong department profile: {admission_row['department_profile']!r}"
            if admission_row["diagnosis_text"] != "Острый аппендицит":
                return False, f"{name}: diagnosis was not copied"
            if int(admission_row["recovery_bed_stay"] or 0) != 1:
                return False, f"{name}: recovery_bed_stay was not set"

            bed_row = manager.fetch_one_remcard(
                "SELECT status, current_admission_id FROM beds WHERE bed_number = ?",
                (expected_bed,),
            )
            if not bed_row or bed_row["status"] != "OCCUPIED" or int(bed_row["current_admission_id"]) != int(future_rao_admission_id):
                return False, f"{name}: selected recovery bed was not occupied by new admission"

            if check_vitals:
                vitals_dao = VitalsDAO(manager)
                preview = vitals_dao.get_latest_vital_values_bulk([int(future_rao_admission_id)]).get(int(future_rao_admission_id))
                expected_preview = {"sys": 123, "dia": 77, "pulse": 88, "temp": 36.7, "spo2": 99, "rr": 15, "cvp": 4}
                if preview != expected_preview:
                    return False, f"{name}: copied vitals are not visible in preview: {preview!r}"
                vitals_dao.add_vital(
                    VitalDTO(
                        id=None,
                        admission_id=int(future_rao_admission_id),
                        timestamp=datetime.fromisoformat(expected_admission_dt) + timedelta(minutes=5),
                        sys=130,
                        pulse=91,
                    )
                )
                updated_preview = vitals_dao.get_latest_vital_values_bulk([int(future_rao_admission_id)]).get(int(future_rao_admission_id))
                if not updated_preview or updated_preview.get("sys") != 130 or updated_preview.get("pulse") != 91:
                    return False, f"{name}: new RAO vitals did not update preview: {updated_preview!r}"
                if updated_preview.get("dia") != 77 or updated_preview.get("spo2") != 99:
                    return False, f"{name}: old copied non-null vitals were lost after partial update: {updated_preview!r}"

            source_link = manager.fetch_one_remcard(
                """
                SELECT COUNT(*) AS count
                FROM admissions
                WHERE id = ?
                  AND intake_extra_json LIKE ?
                """,
                (int(future_rao_admission_id), f"%\"source_admission_id\": {int(source_admission_id)}%"),
            )
            if int(source_link["count"] or 0) != 1:
                return False, f"{name}: intake metadata does not reference source admission"
            return True, "ok"
        finally:
            manager.close()

    scenarios = [
        ("free_all", (), 10),
        ("bed_11_busy", (11,), 10),
        ("bed_12_busy", (12,), 10),
        ("bed_10_busy", (10,), 11),
        ("beds_11_12_busy", (11, 12), 10),
        ("beds_10_12_busy", (10, 12), 11),
        ("beds_10_11_busy", (10, 11), 12),
        ("all_busy", (10, 11, 12), None),
    ]
    for index, (name, occupied_beds, expected_bed) in enumerate(scenarios, start=1):
        ok, details = _run_transfer_scenario(
            name,
            index,
            occupied_beds,
            expected_bed=expected_bed,
            check_vitals=name == "beds_11_12_busy",
        )
        if not ok:
            return ok, details

    ok, details = _run_transfer_scenario(
        "missing_required_diagnosis",
        len(scenarios) + 1,
        (),
        expected_bed=None,
        clear_source_diagnosis=True,
    )
    if not ok:
        return ok, details
    return True, "ok"


def _check_operblock_occupy_dialog_manual_birth_date_and_plain_groups(temp_root: str) -> tuple[bool, str]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from datetime import date

    from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QDateEdit, QLineEdit, QPushButton, QWidget

    from rem_card.ui.operblock_view.operblock_main_widget import OccupyTableDialog
    from rem_card.ui.styles.theme import STYLE_PATIENT_FORM_CANCEL_BUTTON

    _ = temp_root
    app = QApplication.instance() or QApplication([])
    dialog = OccupyTableDialog("planned", "Плановая операционная")
    try:
        dialog.show()
        app.processEvents()
        if isinstance(dialog.birth_date_input, QDateEdit):
            return False, "occupy dialog birth date still uses QDateEdit calendar widget"
        if not isinstance(dialog.birth_date_input, QLineEdit):
            return False, "occupy dialog birth date is not a plain text input"
        scroll_style = dialog.form_scroll.styleSheet()
        if "border: none" not in scroll_style or "border: 1px" in scroll_style:
            return False, "occupy dialog form scroll area still has an outer border connecting section cards"
        birth_samples = {
            "01012000": date(2000, 1, 1),
            "01/01/00": date(2000, 1, 1),
            "1.1.00": date(2000, 1, 1),
            "03051986": date(1986, 5, 3),
        }
        for raw_text, expected in birth_samples.items():
            dialog.birth_date_input.setText(raw_text)
            dialog._normalize_birth_date_field()
            parsed = dialog._birth_date_value()
            if parsed != expected:
                return False, f"occupy dialog birth date parse mismatch for {raw_text!r}: {parsed} != {expected}"
            if dialog.birth_date_input.text() != expected.strftime("%d.%m.%Y"):
                return False, f"occupy dialog birth date was not normalized after parse: {dialog.birth_date_input.text()!r}"
        dialog.birth_date_input.clear()
        for char in "03051986":
            cursor_pos = dialog.birth_date_input.cursorPosition()
            new_text = (
                dialog.birth_date_input.text()[:cursor_pos]
                + char
                + dialog.birth_date_input.text()[cursor_pos:]
            )
            dialog.birth_date_input.setText(new_text)
            dialog.birth_date_input.setCursorPosition(cursor_pos + 1)
            dialog._on_birth_date_text_edited(new_text)
        if dialog.birth_date_input.text() != "03.05.1986" or dialog.birth_date_input.cursorPosition() != 10:
            return False, "occupy dialog birth date progressive numeric input does not keep cursor at the end"

        for object_name in ("OperBlockOccupyBloodFields", "OperBlockOccupyAnesthesiaFields"):
            group = dialog.findChild(QWidget, object_name)
            if group is None:
                return False, f"occupy dialog field group missing: {object_name}"
            stylesheet = group.styleSheet()
            if "background: transparent" not in stylesheet or "border: none" not in stylesheet:
                return False, f"occupy dialog field group is not transparent: {object_name}"

        checkbox = dialog.findChild(QCheckBox, "OperBlockSaveInitialVitalsCheckbox")
        if checkbox is not None:
            return False, "occupy dialog still shows initial vitals save checkbox"

        dialog._add_surgeon_row("Второй хирург")
        app.processEvents()
        remove_buttons = dialog.findChildren(QPushButton, "OperBlockOccupyRemoveSurgeonButton")
        visible_remove_buttons = [button for button in remove_buttons if not button.isHidden()]
        if not visible_remove_buttons:
            return False, "occupy dialog did not show remove surgeon buttons after adding a second surgeon"
        reference_remove_button = QPushButton("Удалить")
        reference_remove_button.setFixedHeight(32)
        reference_remove_button.setStyleSheet(STYLE_PATIENT_FORM_CANCEL_BUTTON)
        expected_remove_width = reference_remove_button.sizeHint().width() + 20
        for button in visible_remove_buttons:
            if button.width() < expected_remove_width:
                return False, f"remove surgeon button was not widened by 20px: {button.width()} < {expected_remove_width}"
        surgeon_combos = [
            combo
            for _row, combo in getattr(dialog, "_surgeon_rows", [])
            if isinstance(combo, QComboBox)
        ]
        if len(surgeon_combos) < 2:
            return False, "occupy dialog did not keep surgeon combo rows"
        return True, "ok"
    finally:
        dialog.close()
        app.processEvents()


def _check_operblock_operation_stage_chart_grouping(temp_root: str) -> tuple[bool, str]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from datetime import timedelta

    import pyqtgraph as pg
    from PySide6.QtWidgets import QApplication

    from rem_card.ui.operblock_view.operblock_chart_widget import OperBlockChartWidget

    app = QApplication.instance() or QApplication([])
    start = datetime(2026, 6, 8, 12, 0)
    chart = OperBlockChartWidget()
    try:
        chart.resize(900, 520)
        chart.show()
        app.processEvents()
        chart.start_time = start
        chart.visible_hours = 3
        chart.plot_widget.getViewBox().setRange(
            xRange=(0, 3),
            yRange=(chart.MEDICATION_BAND_MIN, chart.VITAL_AXIS_MAX),
            padding=0,
        )
        first = {
            "source_id": 10,
            "event_time": start.isoformat(timespec="seconds"),
            "display_label": "разрез",
            "raw_text": "разрез",
            "revision": 1,
            "payload": {"stage_kind": "custom", "label": "разрез"},
        }
        second = {
            "source_id": 11,
            "event_time": start.isoformat(timespec="seconds"),
            "display_label": "распил",
            "raw_text": "распил",
            "revision": 1,
            "payload": {"stage_kind": "custom", "label": "распил"},
        }
        snapshot = {"operation_events": [first, second]}
        chart.set_timeline_snapshot(snapshot, start, force=True)
        app.processEvents()
        labels = [item for item in chart._order_marker_items if isinstance(item, pg.TextItem)]
        texts = [getattr(getattr(item, "textItem", None), "toPlainText", lambda: "")() for item in labels]
        if texts != ["разрез, распил"]:
            return False, f"same-time operation stages were not grouped: {texts!r}"
        shared_ids = set(id(item) for item in chart._operation_stage_marker_items_by_key.get("timeline_event:10", []))
        shared_ids &= set(id(item) for item in chart._operation_stage_marker_items_by_key.get("timeline_event:11", []))
        if not shared_ids:
            return False, "grouped operation stage label was not indexed by both stage keys"

        moved_second = dict(second)
        moved_second["event_time"] = (start + timedelta(minutes=30)).isoformat(timespec="seconds")
        moved_second["revision"] = 2
        chart.patch_operation_stage_marker(
            moved_second,
            snapshot={"operation_events": [first, moved_second]},
            start_time=start,
        )
        app.processEvents()
        texts_after_patch = [
            getattr(getattr(item, "textItem", None), "toPlainText", lambda: "")()
            for item in chart._order_marker_items
            if isinstance(item, pg.TextItem)
        ]
        if sorted(texts_after_patch) != ["разрез", "распил"]:
            return False, f"operation stage group was not redrawn after patch: {texts_after_patch!r}"
        return True, "ok"
    finally:
        chart.deleteLater()
        app.processEvents()


def _find_operblock_board_label(root, text: str):
    from PySide6.QtWidgets import QLabel

    return next((label for label in root.findChildren(QLabel) if label.text() == text), None)


def _find_operblock_board_owner_block(label):
    from PySide6.QtWidgets import QFrame

    block = label.parentWidget() if label is not None else None
    while block is not None and not (
        isinstance(block, QFrame) and block.objectName() == "OperBlockStartBlock"
    ):
        block = block.parentWidget()
    return block if isinstance(block, QFrame) else None


def _operblock_board_block_bottom(full_card, block) -> int:
    from PySide6.QtCore import QPoint

    return block.mapTo(full_card, QPoint(0, 0)).y() + block.height()


def _check_operblock_board_preview_stages_layout(
    full_card,
    full_stage_title,
    vitals_block,
    meds_block,
    operation_events: list[dict],
) -> tuple[bool, str]:
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QLabel, QFrame

    stage_block = _find_operblock_board_owner_block(full_stage_title)
    if stage_block is None:
        return False, "full board preview operation stages block frame was not rendered"
    if "#F8FBFF" in stage_block.styleSheet() or "#CFE3FF" in stage_block.styleSheet():
        return False, "operation stages title is still inside the blue framed block"
    if "#1F2D3D" not in full_stage_title.styleSheet() or "#2563EB" in full_stage_title.styleSheet():
        return False, "operation stages title does not use the default black style"
    stages_panel = stage_block.findChild(QFrame, "OperBlockBoardStagesPanel")
    if stages_panel is None or "#F8FBFF" not in stages_panel.styleSheet() or "#CFE3FF" not in stages_panel.styleSheet():
        return False, "operation stages rows are not inside the blue framed panel"
    target_bottom = max(
        _operblock_board_block_bottom(full_card, vitals_block),
        _operblock_board_block_bottom(full_card, meds_block),
    )
    stage_bottom = _operblock_board_block_bottom(full_card, stage_block)
    if abs(stage_bottom - target_bottom) > 2:
        return False, f"board operation stages block is not stretched to lower row bottom: {stage_bottom} != {target_bottom}"
    empty_stage_notice = stages_panel.findChild(QFrame, "OperBlockStagesEmptyNotice")
    if not operation_events and empty_stage_notice is None:
        return False, "empty operation stages notice was not moved into the stages block"
    if empty_stage_notice is not None:
        if "#EFF6FF" not in empty_stage_notice.styleSheet() or "#8FBEFF" not in empty_stage_notice.styleSheet():
            return False, "empty operation stages notice does not keep the progress notice styling"
        empty_stage_notice_text = next(
            (
                label
                for label in empty_stage_notice.findChildren(QLabel)
                if label.text() == "Операция еще не начата"
            ),
            None,
        )
        if empty_stage_notice_text is None:
            return False, "empty operation stages notice does not render its text"
        stage_notice_gap = (
            empty_stage_notice.mapTo(full_card, QPoint(0, 0)).y()
            - full_stage_title.mapTo(full_card, QPoint(0, 0)).y()
            - full_stage_title.height()
        )
        if stage_notice_gap < 6 or stage_notice_gap > 24:
            return False, f"empty operation stages content is not kept near the title: {stage_notice_gap}"
    for stage_index in range(1, len(operation_events) + 1):
        stage_label = next(
            (label for label in stages_panel.findChildren(QLabel) if label.text() == f"Этап {stage_index:02d}"),
            None,
        )
        if stage_label is None:
            return False, f"operation stage label missed in rows panel: {stage_index:02d}"
        if "#1F2D3D" not in stage_label.styleSheet() or "#2563EB" in stage_label.styleSheet():
            return False, f"operation stage label is not rendered in the unified black style: {stage_label.styleSheet()!r}"
    return True, "ok"


def _check_operblock_board_preview_action_buttons(full_card) -> tuple[bool, str]:
    from PySide6.QtWidgets import QPushButton

    open_buttons = [
        button
        for button in full_card.findChildren(QPushButton)
        if "ОТКРЫТЬ КАРТОЧКУ" in button.text()
    ]
    if not open_buttons:
        return False, "board preview open button was not rendered"
    open_style = open_buttons[0].styleSheet()
    if "QPushButton:hover" not in open_style or "background-color: #E2E8F0" not in open_style:
        return False, "board preview open button hover does not fill the button background"
    edit_buttons = [
        button
        for button in full_card.findChildren(QPushButton)
        if "РЕДАКТИРОВАТЬ" in button.text()
    ]
    if not edit_buttons:
        return False, "board preview edit button was not rendered"
    if any("✎" in button.text() for button in edit_buttons):
        return False, "board preview edit button still uses a text pencil"
    if edit_buttons[0].icon().isNull():
        return False, "board preview edit button did not load edit.png icon"
    return True, "ok"


def _check_operblock_board_progress_stepper_centered(app, widget, base_dt: datetime) -> tuple[bool, str]:
    from PySide6.QtCore import QPoint

    from rem_card.ui.operblock_view.operblock_main_widget import _OperBlockBoardProgressStepper

    block = widget._board_progress_block(
        {
            "started_at": base_dt.isoformat(timespec="seconds"),
            "operation_events": [],
        }
    )
    try:
        block.resize(840, 236)
        block.show()
        app.processEvents()
        app.processEvents()
        title = _find_operblock_board_label(block, "Ход операции")
        stepper = block.findChild(_OperBlockBoardProgressStepper)
        if title is None or stepper is None:
            return False, "board progress preview did not render title or stepper"
        space_above = (
            stepper.mapTo(block, QPoint(0, 0)).y()
            - title.mapTo(block, QPoint(0, 0)).y()
            - title.height()
        )
        space_below = block.height() - stepper.mapTo(block, QPoint(0, 0)).y() - stepper.height() - 16
        if space_below < 8:
            return False, f"board progress stepper is still pressed to the bottom: {space_below}"
        if abs((space_above - 12) - space_below) > 4:
            return False, f"board progress stepper is not vertically centered: above={space_above}, below={space_below}"
        return True, "ok"
    finally:
        block.deleteLater()
        app.processEvents()


def _check_operblock_board_progress_preview_content(
    app,
    widget,
    base_dt: datetime,
    operation_events: list[dict],
) -> tuple[bool, str]:
    from PySide6.QtWidgets import QLabel

    progress_block = widget._board_progress_block(
        {
            "operation_name": "Лапароскопическая холецистэктомия",
            "started_at": base_dt.isoformat(timespec="seconds"),
            "operation_events": operation_events,
        }
    )
    try:
        progress_texts = [label.text() for label in progress_block.findChildren(QLabel)]
        if "Ход операции: Лапароскопическая холецистэктомия" not in progress_texts:
            return False, f"board progress title does not include operation name: {progress_texts!r}"
        if any(f"Этап {index:02d}" in progress_texts for index in range(1, 9)):
            return False, f"board progress preview still contains detailed operation stages: {progress_texts!r}"
        ok, details = _check_operblock_board_progress_stepper_centered(app, widget, base_dt)
        if not ok:
            return False, details
        return True, "ok"
    finally:
        progress_block.deleteLater()
        app.processEvents()


def _check_operblock_board_medication_empty_notice(app, widget) -> tuple[bool, str]:
    from PySide6.QtWidgets import QLabel, QFrame

    meds_block = widget._board_medications_block({"medication_history": []})
    try:
        meds_block.resize(360, 180)
        meds_block.show()
        app.processEvents()
        app.processEvents()
        empty_notice = meds_block.findChild(QFrame, "OperBlockMedicationsEmptyNotice")
        if empty_notice is None:
            return False, "empty medication preview does not render styled notice"
        if "#EFF6FF" not in empty_notice.styleSheet() or "#8FBEFF" not in empty_notice.styleSheet():
            return False, "empty medication preview notice does not use the operation stages notice style"
        notice_text = next(
            (label for label in empty_notice.findChildren(QLabel) if label.text() == "Нет введённых препаратов"),
            None,
        )
        if notice_text is None:
            return False, "empty medication preview notice text was not rendered"
        plain_empty_labels = [
            label
            for label in meds_block.findChildren(QLabel)
            if label.text() == "Нет введённых препаратов" and label.parentWidget() is not empty_notice
        ]
        if plain_empty_labels:
            return False, "empty medication preview still renders the old plain label"
        return True, "ok"
    finally:
        meds_block.deleteLater()
        app.processEvents()


def _check_operblock_empty_table_card_layout(temp_root: str) -> tuple[bool, str]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication, QFrame, QLabel, QPushButton, QWidget

    from rem_card.ui.operblock_view.operblock_main_widget import OperBlockMainWidget

    _ = temp_root
    app = QApplication.instance() or QApplication([])
    widget = OperBlockMainWidget.__new__(OperBlockMainWidget)
    widget._current_board_apply_metrics = None
    card = widget._make_empty_table_card("planned", "Плановая операционная")
    try:
        card.resize(1200, 720)
        card.show()
        app.processEvents()
        app.processEvents()

        header = next((label for label in card.findChildren(QLabel) if label.text() == "Плановая операционная"), None)
        if header is None:
            return False, "empty table card did not render the table title"
        empty_state = card.findChild(QFrame, "OperBlockEmptyStateCard")
        if empty_state is None:
            return False, "empty table card did not render the central empty-state card"
        width_ratio = empty_state.width() / max(1, card.width())
        if width_ratio < 0.78 or width_ratio > 0.92:
            return False, f"empty-state card width ratio is outside the requested range: {width_ratio:.3f}"
        if "#FFFFFF" not in empty_state.styleSheet() or "#DDE5EE" not in empty_state.styleSheet():
            return False, "empty-state card does not keep the white card and thin border styling"
        illustration = card.findChild(QWidget, "OperBlockEmptyStateIllustration")
        if illustration is None:
            return False, "empty table card did not render the round operating-room illustration"
        if isinstance(illustration, QLabel):
            return False, "empty-state illustration must not be the old QLabel pixmap"
        if illustration.width() < 120 or illustration.height() < 120:
            return False, f"empty-state illustration is too small: {illustration.size()}"
        status = next((label for label in card.findChildren(QLabel) if label.text() == "МЕСТО СВОБОДНО"), None)
        if status is None or "#16A34A" not in status.styleSheet():
            return False, "empty table card did not render the green free-status text"
        description = card.findChild(QLabel, "OperBlockEmptyStateDescription")
        if description is None or "нет активной операции" not in description.text():
            return False, "empty table card did not render the explanatory text"
        separator = card.findChild(QFrame, "OperBlockEmptyStateSeparator")
        if separator is None or separator.height() != 1:
            return False, "empty table card did not render the thin separator"
        button = card.findChild(QPushButton, "OperBlockEmptyStateOccupyButton")
        if button is None:
            return False, "empty table card did not render the primary occupy button"
        if button.text() != "ЗАНЯТЬ СТОЛ" or button.height() < 52 or button.maximumWidth() > 520:
            return False, f"empty occupy button has unexpected geometry or text: {button.text()} {button.size()}"
        if "#16A34A" not in button.styleSheet():
            return False, "empty occupy button is not styled as the green primary action"
        pixmap_labels = [
            label
            for label in card.findChildren(QLabel)
            if label.pixmap() is not None and not label.pixmap().isNull()
        ]
        if pixmap_labels:
            return False, "empty table card still renders a pixmap label from the old placeholder"
        info = card.findChild(QFrame, "OperBlockEmptyStateInfo")
        if info is None or "#EFF6FF" not in info.styleSheet() or "#BBD7FF" not in info.styleSheet():
            return False, "empty table card did not render the blue informational block"
        info_text = card.findChild(QLabel, "OperBlockEmptyStateInfoText")
        if info_text is None or "После занятия стола" not in info_text.text():
            return False, "empty table card did not render the informational text"
        return True, "ok"
    finally:
        card.deleteLater()
        app.processEvents()


def _check_operblock_board_gender_photo_uses_operating_room_asset(
    app,
    widget,
    *,
    gender: str,
    asset_name: str,
    description: str,
) -> tuple[bool, str]:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImageReader
    from PySide6.QtWidgets import QLabel

    from rem_card.app.paths import get_icon_dir

    asset_path = os.path.join(get_icon_dir(), asset_name)
    if not os.path.isfile(asset_path):
        return False, f"operating room {description} patient photo asset is missing"

    label = QLabel()
    label.setFixedSize(232, 268)
    widget._current_board_apply_metrics = None
    widget._board_photo_thumbnail_cache = {}
    widget._set_patient_photo(label, gender)
    pixmap = label.pixmap()
    if pixmap is None or pixmap.isNull():
        return False, f"{description} operating room patient photo was not rendered"
    if pixmap.width() < label.width() - 4 or pixmap.height() < label.height() - 4:
        return False, f"{description} operating room patient photo is too small for the slot: {pixmap.size()}"

    reader = QImageReader(asset_path)
    reader.setAutoTransform(True)
    source_size = reader.size()
    if source_size.isValid():
        reader.setScaledSize(source_size.scaled(label.size(), Qt.KeepAspectRatio))
    expected = reader.read()
    if expected.isNull():
        return False, f"operating room {description} patient photo asset could not be decoded"
    actual = pixmap.toImage()
    if actual.size() != expected.size():
        return False, f"{description} patient photo does not use operating room asset size: {actual.size()} != {expected.size()}"
    for x, y in (
        (actual.width() // 2, actual.height() // 2),
        (actual.width() // 3, actual.height() // 3),
        (actual.width() * 2 // 3, actual.height() * 2 // 3),
    ):
        actual_color = actual.pixelColor(x, y)
        expected_color = expected.pixelColor(x, y)
        channel_delta = (
            abs(actual_color.red() - expected_color.red())
            + abs(actual_color.green() - expected_color.green())
            + abs(actual_color.blue() - expected_color.blue())
            + abs(actual_color.alpha() - expected_color.alpha())
        )
        if channel_delta > 12:
            return False, f"{description} patient photo pixels do not match operating room asset"
    return True, "ok"


def _check_operblock_board_operating_room_photos(app, widget) -> tuple[bool, str]:
    checks = (
        ("Мужской", "man_in_oper_extr.png", "male"),
        ("Женский", "woman_in_oper_extr.png", "female"),
    )
    for gender, asset_name, description in checks:
        ok, details = _check_operblock_board_gender_photo_uses_operating_room_asset(
            app,
            widget,
            gender=gender,
            asset_name=asset_name,
            description=description,
        )
        if not ok:
            return False, details
    return True, "ok"


def _check_operblock_board_preview_full_card_layout(
    app,
    widget,
    base_dt: datetime,
    operation_events: list[dict],
    medication_history: list[dict],
) -> tuple[bool, str]:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtWidgets import QLabel, QScrollArea, QWidget

    widget._current_board_apply_metrics = None
    widget._board_photo_thumbnail_cache = {}
    full_card = widget._make_occupied_table_card(
        {
            "display_name": "Экстренная операционная",
            "patient": {
                "operation_case_id": 1,
                "history_number": "REGBOARD1",
                "full_name": "Тестов Пациент",
                "age": "46 лет",
                "gender": "м",
                "diagnosis_code": "K35.8",
                "diagnosis_text": "Острый аппендицит. Перитонит.",
                "operation_name": "Лапароскопическая холецистэктомия",
                "started_at": base_dt.isoformat(timespec="seconds"),
                "operation_events": operation_events,
                "medication_history": medication_history,
                "latest": {"ad": "120/80", "pulse": 70, "spo2": 98, "source": "current"},
            },
        },
    )
    try:
        full_card.resize(1380, 780)
        full_card.show()
        app.processEvents()
        app.processEvents()

        patient_label = _find_operblock_board_label(full_card, "ИБ № REGBOARD1")
        admission_title = _find_operblock_board_label(full_card, "Диагноз при поступлении")
        vitals_title = _find_operblock_board_label(full_card, "Текущие показатели")
        meds_title = _find_operblock_board_label(full_card, "Назначения и препараты")
        allergies_title = _find_operblock_board_label(full_card, "Аллергии")
        if patient_label is None or admission_title is None or vitals_title is None or meds_title is None or allergies_title is None:
            return False, "board preview did not render patient, admission, vitals, medication or allergies title"
        vitals_y = vitals_title.mapTo(full_card, QPoint(0, 0)).y()
        meds_y = meds_title.mapTo(full_card, QPoint(0, 0)).y()
        if abs(vitals_y - meds_y) > 1:
            return False, f"board medication title is not aligned with vitals title: {meds_y} != {vitals_y}"
        full_scroll = full_card.findChild(QScrollArea, "OperBlockBoardMedicationsScroll")
        if full_scroll is None:
            return False, "full board preview medication scroll was not rendered"
        scroll_y = full_scroll.mapTo(full_card, QPoint(0, 0)).y()
        title_bottom = meds_title.mapTo(full_card, QPoint(0, 0)).y() + meds_title.height()
        scroll_gap = scroll_y - title_bottom
        if scroll_gap < 4 or scroll_gap > 10:
            return False, f"board medication list gap is unstable: {scroll_gap}"
        if full_scroll.verticalScrollBar().value() != full_scroll.verticalScrollBar().maximum():
            return False, "full board preview medication scroll is not positioned at latest rows"
        progress_title = _find_operblock_board_label(full_card, "Ход операции: Лапароскопическая холецистэктомия")
        patient_block = _find_operblock_board_owner_block(patient_label)
        admission_block = _find_operblock_board_owner_block(admission_title)
        progress_block = _find_operblock_board_owner_block(progress_title)
        vitals_block = _find_operblock_board_owner_block(vitals_title)
        meds_block = _find_operblock_board_owner_block(meds_title)
        allergies_block = _find_operblock_board_owner_block(allergies_title)
        if (
            patient_block is None
            or admission_block is None
            or progress_block is None
            or vitals_block is None
            or meds_block is None
            or allergies_block is None
        ):
            return False, "board preview did not render patient, admission, progress, vitals, medication or allergies block frame"
        for guarded_block, description in (
            (patient_block, "patient info"),
            (admission_block, "admission diagnosis"),
            (vitals_block, "vitals"),
            (allergies_block, "allergies"),
        ):
            context_widgets = [guarded_block, *guarded_block.findChildren(QWidget)]
            if any(widget.contextMenuPolicy() != Qt.NoContextMenu for widget in context_widgets):
                return False, f"board {description} block allows a context menu on right click"
        if not operation_events:
            progress_empty_labels = [
                label.text()
                for label in progress_block.findChildren(QLabel)
                if label.text() == "Операция еще не начата"
            ]
            if progress_empty_labels:
                return False, "empty operation stages notice is still rendered in the progress block"
        progress_bottom = _operblock_board_block_bottom(full_card, progress_block)
        meds_top = meds_block.mapTo(full_card, QPoint(0, 0)).y()
        progress_to_meds_gap = meds_top - progress_bottom
        if progress_to_meds_gap < 8 or progress_to_meds_gap > 18:
            return False, f"board progress block does not fill the gap above medications: {progress_to_meds_gap}"
        full_stage_title = _find_operblock_board_label(full_card, "Этапы операции")
        if full_stage_title is None:
            return False, "full board preview operation stages block was not rendered"
        ok, details = _check_operblock_board_preview_stages_layout(
            full_card,
            full_stage_title,
            vitals_block,
            meds_block,
            operation_events,
        )
        if not ok:
            return False, details
        ok, details = _check_operblock_board_preview_action_buttons(full_card)
        if not ok:
            return False, details
        return True, "ok"
    finally:
        full_card.deleteLater()
        app.processEvents()


def _check_operblock_board_vitals_header_icon_alignment(app, widget, operblock_widget_cls) -> tuple[bool, str]:
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QLabel

    for source, title_text in (("current", "Текущие показатели"), ("initial", "Исходные показатели")):
        vitals_block = operblock_widget_cls._board_vitals_block(
            widget,
            {"latest": {"ad": "120/80", "pulse": 70, "spo2": 98, "source": source}},
        )
        try:
            vitals_block.resize(248, 220)
            vitals_block.show()
            app.processEvents()
            title_label = _find_operblock_board_label(vitals_block, title_text)
            header_icon = vitals_block.findChild(QLabel, "OperBlockBoardBlockHeaderIcon")
            if title_label is None or header_icon is None:
                return False, f"board vitals {source} header did not render title or icon"
            title_y = title_label.mapTo(vitals_block, QPoint(0, 0)).y()
            icon_y = header_icon.mapTo(vitals_block, QPoint(0, 0)).y()
            if abs(icon_y - title_y) > 2:
                return False, f"board vitals {source} header icon is shifted: {icon_y} != {title_y}"
            title_label.setMinimumHeight(title_label.sizeHint().height() + 32)
            vitals_block.layout().activate()
            app.processEvents()
            title_y = title_label.mapTo(vitals_block, QPoint(0, 0)).y()
            icon_y = header_icon.mapTo(vitals_block, QPoint(0, 0)).y()
            if abs(icon_y - title_y) > 2:
                return False, f"board vitals {source} header icon shifts when title row grows: {icon_y} != {title_y}"
        finally:
            vitals_block.deleteLater()
            app.processEvents()
    return True, "ok"


def _check_operblock_board_preview_bounded_history(temp_root: str) -> tuple[bool, str]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from datetime import timedelta

    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtWidgets import QApplication, QLabel, QScrollArea

    from rem_card.services.operblock_service import OperBlockService
    from rem_card.ui.operblock_view.operblock_main_widget import (
        OPERBLOCK_BOARD_MEDICATION_SCROLL_MAX_HEIGHT,
        OperBlockMainWidget,
    )

    app = QApplication.instance() or QApplication([])
    widget = OperBlockMainWidget.__new__(OperBlockMainWidget)
    base_dt = datetime(2026, 6, 8, 12, 0)

    ok, details = _check_operblock_board_vitals_header_icon_alignment(app, widget, OperBlockMainWidget)
    if not ok:
        return False, details

    timeline = {
        "bolus_events": [
            {
                "source_id": index,
                "event_time": (base_dt + timedelta(minutes=index)).isoformat(timespec="seconds"),
                "display_label": f"Болюс {index:02d}",
            }
            for index in range(1, 9)
        ]
    }
    service_items = OperBlockService._board_medication_history_from_timeline(timeline)
    if len(service_items) != 8:
        return False, f"board medication history was truncated before UI scroll: {len(service_items)}"

    medication_history = [
        {
            "time": (base_dt + timedelta(minutes=index)).isoformat(timespec="seconds"),
            "label": f"Препарат {index:02d}",
            "kind_label": "Болюс",
        }
        for index in range(1, 13)
    ]
    meds_block = OperBlockMainWidget._board_medications_block(widget, {"medication_history": medication_history})
    meds_scroll = meds_block.findChild(QScrollArea, "OperBlockBoardMedicationsScroll")
    if meds_scroll is None:
        return False, "board medication preview did not create a scroll area"
    if meds_scroll.maximumHeight() != OPERBLOCK_BOARD_MEDICATION_SCROLL_MAX_HEIGHT:
        return False, f"unexpected medication scroll maximum height: {meds_scroll.maximumHeight()}"
    if meds_scroll.verticalScrollBarPolicy() != Qt.ScrollBarAsNeeded:
        return False, "board medication scroll bar is not set to appear as needed"
    med_texts = [label.text() for label in meds_block.findChildren(QLabel)]
    if "Препарат 01" not in med_texts or "Препарат 12" not in med_texts:
        return False, f"medication preview scroll did not keep all rows: {med_texts!r}"
    meds_block.resize(360, 260)
    meds_block.show()
    app.processEvents()
    app.processEvents()
    meds_bar = meds_scroll.verticalScrollBar()
    if meds_bar.maximum() <= 0:
        return False, "board medication preview did not overflow in the scroll area"
    if meds_bar.value() != meds_bar.maximum():
        return False, f"board medication preview did not auto-scroll to latest row: {meds_bar.value()} != {meds_bar.maximum()}"
    meds_title = next((label for label in meds_block.findChildren(QLabel) if label.text() == "Назначения и препараты"), None)
    if meds_title is None:
        return False, "board medication preview did not render title"
    meds_gap = (
        meds_scroll.mapTo(meds_block, QPoint(0, 0)).y()
        - meds_title.mapTo(meds_block, QPoint(0, 0)).y()
        - meds_title.height()
    )
    if meds_gap < 4 or meds_gap > 10:
        return False, f"board medication preview leaves a large gap before rows: {meds_gap}"

    ok, details = _check_operblock_board_operating_room_photos(app, widget)
    if not ok:
        return False, details

    ok, details = _check_operblock_board_medication_empty_notice(app, widget)
    if not ok:
        return False, details

    operation_events = [
        {
            "source_id": index,
            "event_time": (base_dt + timedelta(minutes=index)).isoformat(timespec="seconds"),
            "kind": "custom",
            "label": f"Этап {index:02d}",
        }
        for index in range(1, 9)
    ]
    ok, details = _check_operblock_board_progress_preview_content(app, widget, base_dt, operation_events)
    if not ok:
        return False, details

    stages_block = OperBlockMainWidget._board_operation_stages_block(
        widget,
        {"started_at": base_dt.isoformat(timespec="seconds"), "operation_events": operation_events},
    )
    stage_texts = [label.text() for label in stages_block.findChildren(QLabel)]
    for index in range(1, 9):
        if f"Этап {index:02d}" not in stage_texts:
            return False, f"board operation history missed stage {index:02d}: {stage_texts!r}"
    if "Подготовка пациента" in stage_texts:
        return False, "board operation history mixed case started_at into timeline stages"

    admission_block = OperBlockMainWidget._board_admission_block(
        widget,
        {"display_name": "Экстренная операционная"},
        {
            "diagnosis_code": "K35.8",
            "diagnosis_text": "Острый аппендицит. Перитонит.",
            "started_at": base_dt.isoformat(timespec="seconds"),
        },
    )
    diagnosis_labels = [
        label
        for label in admission_block.findChildren(QLabel)
        if "Острый аппендицит" in label.text()
    ]
    if not diagnosis_labels:
        return False, "board admission preview did not render diagnosis text"
    if admission_block.graphicsEffect() is not None:
        return False, "board admission preview keeps a graphics effect around selectable diagnosis text"
    if not (diagnosis_labels[0].textInteractionFlags() & Qt.TextSelectableByMouse):
        return False, "board admission diagnosis text is not selectable"
    default_block, _default_layout = OperBlockMainWidget._board_block("Проверка тени")
    if default_block.graphicsEffect() is None:
        return False, "default board block shadow was disabled globally"

    allergy_icon = OperBlockMainWidget._board_allergy_status_icon(has_allergies=True)
    if allergy_icon.text():
        return False, "board allergy danger icon is still rendered as font text"
    allergy_pixmap = allergy_icon.pixmap()
    if allergy_pixmap is None or allergy_pixmap.isNull():
        return False, "board allergy danger icon pixmap was not rendered"
    allergy_image = allergy_pixmap.toImage()

    def has_red_pixel_near(x: int, y: int) -> bool:
        for dx in range(-1, 2):
            for dy in range(-1, 2):
                color = allergy_image.pixelColor(x + dx, y + dy)
                if color.alpha() > 80 and color.red() > 180 and color.green() < 120 and color.blue() < 120:
                    return True
        return False

    if not all(has_red_pixel_near(x, y) for x, y in ((8, 8), (14, 14), (14, 8), (8, 14))):
        return False, "board allergy danger icon cross is not drawn on both diagonals"

    for preview_events in (operation_events, operation_events[:1], []):
        ok, details = _check_operblock_board_preview_full_card_layout(
            app,
            widget,
            base_dt,
            preview_events,
            medication_history,
        )
        if not ok:
            return False, details

    meds_block.deleteLater()
    stages_block.deleteLater()
    admission_block.deleteLater()
    default_block.deleteLater()
    allergy_icon.deleteLater()
    app.processEvents()
    return True, "ok"


def _check_operblock_quick_order_local_updates(temp_root: str) -> tuple[bool, str]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from datetime import date

    from PySide6.QtWidgets import QApplication

    from rem_card.app.operblock_schema import _apply_operblock_schema
    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.services.operblock_service import OperBlockService
    from rem_card.ui.operblock_view.operblock_main_widget import OperBlockMainWidget

    class _ChartSpy:
        def __init__(self, start_time):
            self.start_time = start_time
            self.calls: list[dict] = []

        def set_timeline_snapshot(self, snapshot, start_time, *, force: bool = False):
            self.calls.append({"snapshot": dict(snapshot or {}), "start_time": start_time, "force": bool(force)})

        def update_data(self, *args, **kwargs):
            raise AssertionError("quick order save updated vitals data")

    def _unexpected_refresh(*args, **kwargs):
        raise AssertionError("quick order save caused full protocol/vitals refresh")

    db_path = os.path.join(temp_root, "operblock_quick_order_local_updates.db")
    manager = DatabaseManager(db_path, db_path)
    app = QApplication.instance() or QApplication([])
    try:
        manager.run_write_operation(_apply_operblock_schema, source="regression_operblock_schema")
        service = OperBlockService(manager)
        case = service.create_operation_case(
            {
                "table_code": "emergency",
                "history_number": "REGQORDER1",
                "full_name": "Тестов Пациент",
                "gender": "м",
                "birth_date": date(1980, 1, 1),
                "diagnosis_code": "K35",
                "diagnosis_text": "Острый аппендицит",
            }
        )
        admission_id = int(case["admission_id"])
        case_id = int(case["operation_case_id"])
        service.add_vitals(admission_id, sys=120, dia=80, pulse=70, spo2=98)
        service.start_anesthesia(case_id, "ОА")
        snapshot = service.build_operblock_timeline_snapshot(admission_id, operation_case_id=case_id).to_dict()
        orders_snapshot = service.build_operblock_orders_snapshot(admission_id, operation_case_id=case_id)
        anesthesia_start = datetime.fromisoformat(str((snapshot.get("operation_events") or [])[0].get("event_time")).replace(" ", "T"))

        widget = OperBlockMainWidget.__new__(OperBlockMainWidget)
        widget._current_admission_id = admission_id
        widget._current_operation_case_id = case_id
        widget._current_operation_start = anesthesia_start
        widget._current_protocol_date = anesthesia_start
        widget._current_orders_rows = [dict(row or {}) for row in orders_snapshot.get("orders") or []]
        widget._current_timeline_snapshot = dict(snapshot)
        widget._pending_quick_orders_scroll_state = None
        widget._write_pending = True
        widget._orders_force_top_on_next_apply = False
        widget._orders_render_signature = ""
        widget._orders_source_signature = ""
        widget._orders_filter_kind = "all"
        widget._orders_hide_deleted = True
        widget._local_write_refresh_suppressions = {}
        widget.vitals_chart = _ChartSpy(anesthesia_start)
        widget._set_protocol_write_controls_enabled = lambda *_args, **_kwargs: None
        widget._restore_quick_orders_scroll_state_later = lambda *_args, **_kwargs: None
        widget.refresh_protocol = _unexpected_refresh
        widget._update_vitals_chart = _unexpected_refresh

        order_row = service.add_order(admission_id, "Фентанил 0,1 мг", return_row=True)
        if not isinstance(order_row, dict) or not int(order_row.get("id") or 0):
            return False, f"add_order(return_row=True) returned unexpected result: {order_row!r}"
        try:
            OperBlockMainWidget._on_quick_order_saved(widget, order_row)
        except AssertionError as exc:
            return False, str(exc)
        order_id = int(order_row["id"])
        if order_id not in {int((row or {}).get("id") or 0) for row in widget._current_orders_rows}:
            return False, "quick bolus was not added to local orders rows"
        bolus_ids = {
            int((event or {}).get("source_id") or 0)
            for event in (widget._current_timeline_snapshot or {}).get("bolus_events") or []
        }
        if order_id not in bolus_ids:
            return False, "quick bolus was not added to local timeline snapshot"
        if len(widget.vitals_chart.calls) != 1:
            return False, f"quick bolus should patch chart markers once, got {len(widget.vitals_chart.calls)}"

        write_source = f"operblock_quick_bolus_order:{admission_id}"
        OperBlockMainWidget._remember_local_write_refresh_suppression(widget, write_source, {"orders"})
        if not OperBlockMainWidget._should_skip_local_write_refresh(
            widget,
            {"force_sources": [write_source], "changes": [{"entity_name": "orders"}]},
        ):
            return False, "quick bolus local write did not suppress own orders refresh"

        event_row = service.start_infusion(
            admission_id,
            case_id,
            "Пропофол",
            "5",
            "мл/час",
            anesthesia_start.isoformat(timespec="seconds"),
            return_event=True,
        )
        if not isinstance(event_row, dict) or not int(event_row.get("id") or 0):
            return False, f"start_infusion(return_event=True) returned unexpected result: {event_row!r}"
        widget._write_pending = True
        before_chart_calls = len(widget.vitals_chart.calls)
        try:
            OperBlockMainWidget._on_infusion_mutation_saved(widget, event_row)
        except AssertionError as exc:
            return False, str(exc)
        interval_ids = {
            str((interval or {}).get("interval_id") or "")
            for interval in (widget._current_timeline_snapshot or {}).get("infusion_intervals") or []
        }
        if f"infusion:{int(event_row['id'])}" not in interval_ids:
            return False, "quick infusion was not added to local timeline snapshot"
        if len(widget.vitals_chart.calls) != before_chart_calls + 1:
            return False, "quick infusion should patch chart markers once"

        infusion_source = f"operblock_start_infusion:{admission_id}"
        OperBlockMainWidget._remember_local_write_refresh_suppression(widget, infusion_source, {"operblock_timeline_events"})
        if not OperBlockMainWidget._should_skip_local_write_refresh(
            widget,
            {"force_sources": [infusion_source], "changes": [{"entity_name": "operblock_timeline_events"}]},
        ):
            return False, "quick infusion local write did not suppress own timeline refresh"
        return True, "ok"
    finally:
        manager.close()
        app.processEvents()


def _check_print_and_background_settings_from_db(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.services.settings.settings_service import SettingsService

    service = SettingsService(SettingsDatabase(baza_dir=os.path.join(temp_root, "Baza")))
    service.ensure_ready()
    print_config = service.get_app_setting("doctor", "print_config", default={})
    background = service.get_app_setting("shared", "background_settings", default={})
    display = service.get_app_setting("shared", "display_settings", default={})
    if not isinstance(print_config, dict) or "vitals" not in print_config:
        return False, "doctor print settings were not imported into settings DB"
    if not isinstance(background, dict) or not background.get("backgrounds"):
        return False, "background settings were not imported into settings DB"
    if not isinstance(display, dict) or not display.get("active"):
        return False, "display settings were not imported into settings DB"
    return True, "ok"


def _check_operblock_icon_defaults(context: dict[str, Any]) -> tuple[bool, str]:
    records = context["service"].list_operblock_icons()
    remcard_records = context["service"].list_remcard_icons()
    for definition in context["remcard_icon_definitions"]:
        record = remcard_records.get(definition.icon_key)
        if not record:
            return False, f"иконка ремкарты {definition.icon_key} не получила стартовую запись"
        source_path = os.path.join(context["icon_dir"], definition.source_file or definition.default_file)
        if record.get("image_blob") != Path(source_path).read_bytes():
            return False, f"иконка ремкарты {definition.icon_key} должна быть загружена из {os.path.basename(source_path)}"
        if record.get("source") != "seed":
            return False, f"иконка ремкарты {definition.icon_key} должна быть seed-строкой"

    sevo_record = records.get("drug:manual:gas:sevoflurane")
    if not sevo_record:
        return False, "севофлюран не получил стартовую пользовательскую иконку"
    if sevo_record.get("default_file") != "gas_izm.png":
        return False, "севофлюран должен иметь стандартную иконку gas_izm.png"
    if sevo_record.get("image_blob") != context["sevo_source_blob"]:
        return False, "стартовая иконка севофлюрана должна быть sevodrag.png из БД"
    if sevo_record.get("source") != "seed":
        return False, "стартовая иконка севофлюрана должна быть seed-строкой"
    if records.get("drug:manual:gas:desflurane") is not None:
        return False, "десфлюран не должен наследовать пользовательскую иконку севофлюрана"
    if context["default_drug_icon_file"]("gas") != "gas_izm.png":
        return False, "стандартная иконка препарата-газа должна быть gas_izm.png"

    type_key = context["type_icon_key"]("operation_stage")
    edit_key = context["edit_icon_key"]("operation_stage")
    if type_key != "type:operation_stage":
        return False, f"неверный ключ иконки этапа операции: {type_key}"
    if edit_key != "edit:operation_stage":
        return False, f"неверный ключ иконки изменения этапа операции: {edit_key}"
    if context["default_icon_file_for_key"](type_key) != "etap1.png":
        return False, "иконка этапа операции по умолчанию должна быть etap1.png"
    if context["default_icon_file_for_key"](edit_key) != "etap2.png":
        return False, "иконка окна изменения этапа операции по умолчанию должна быть etap2.png"

    definitions_by_key = context["definitions_by_key"]
    male_key = context["male_key"]
    female_key = context["female_key"]
    for icon_key, message in (
        (type_key, "иконка этапа операции не попала в список настраиваемых иконок"),
        (edit_key, "иконка изменения этапа операции не попала в список настраиваемых иконок"),
        (male_key, "фото пациента-мужчины оперблока не попало в список настраиваемых иконок"),
        (female_key, "фото пациента-женщины оперблока не попало в список настраиваемых иконок"),
    ):
        if icon_key not in definitions_by_key:
            return False, message
    if context["default_icon_file_for_key"](male_key) != "man_in_oper_extr.png":
        return False, "фото пациента-мужчины оперблока должно иметь fallback man_in_oper_extr.png"
    if context["default_icon_file_for_key"](female_key) != "woman_in_oper_extr.png":
        return False, "фото пациента-женщины оперблока должно иметь fallback woman_in_oper_extr.png"
    if not os.path.isfile(os.path.join(context["icon_dir"], "etap1.png")):
        return False, "файл стандартной иконки этапа операции etap1.png не найден"
    if not os.path.isfile(os.path.join(context["icon_dir"], "etap2.png")):
        return False, "файл стандартной иконки изменения этапа операции etap2.png не найден"

    sevo_candidates = context["candidate_keys_from_payload"]({"kind": "gas"}, "Севофлюран")
    if "drug:manual:gas:sevoflurane" not in sevo_candidates:
        return False, "севофлюран без preset_id не ищет сохраненную иконку севофлюрана"
    des_candidates = context["candidate_keys"](preset_id="manual:gas:desflurane", label="Desflurane")
    if "drug:manual:gas:sevoflurane" in des_candidates:
        return False, "десфлюран не должен искать иконку севофлюрана"
    noisy_candidates = context["candidate_keys_from_payload"](
        {"kind": "gas", "label": "Десфлюран", "display_name": "Десфлюран"},
        "Десфлюран 0,7 МАК",
    )
    if "drug-label:десфлюран" not in noisy_candidates:
        return False, "окно изменения газа должно искать иконку по чистому названию из payload"
    context["noisy_des_candidates"] = noisy_candidates
    return True, "ok"


def _check_operblock_icons_settings_db(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.paths import get_icon_dir
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.data.settings.settings_release import (
        apply_settings_release_snapshot,
        export_settings_release_snapshot,
    )
    from rem_card.services.operblock_icon_defaults import (
        DEFAULT_ICON_DEFINITION_BY_KEY,
        OPERBLOCK_PATIENT_FEMALE_ICON_KEY,
        OPERBLOCK_PATIENT_MALE_ICON_KEY,
        OPERBLOCK_ICONS_KEY,
        default_icon_file_for_key,
        default_drug_icon_file,
        drug_icon_candidate_keys,
        drug_icon_candidate_keys_from_payload,
        edit_icon_key,
        type_icon_key,
    )
    from rem_card.services.remcard_icon_defaults import REMCARD_ICON_DEFINITIONS
    from rem_card.services.settings.settings_service import (
        SettingsService,
        configure_settings_service,
        reset_settings_service,
    )
    from rem_card.ui.shared.operblock_icon_settings import (
        current_operblock_icon_source,
        invalidate_operblock_icon_cache,
    )

    icon_dir = get_icon_dir()
    sevo_source_path = os.path.join(icon_dir, "sevodrag.png")
    des_source_path = os.path.join(icon_dir, "gas.png")
    sevo_source_blob = Path(sevo_source_path).read_bytes()
    des_source_blob = Path(des_source_path).read_bytes()

    source_baza_dir = os.path.join(temp_root, "Baza")
    service = SettingsService(SettingsDatabase(baza_dir=source_baza_dir))
    service.ensure_ready()

    with service.db.read_connection() as conn:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'operblock_icons'"
        ).fetchone()
        catalog_row = conn.execute(
            "SELECT version FROM settings_catalog_versions WHERE catalog_key = ?",
            (OPERBLOCK_ICONS_KEY,),
        ).fetchone()
    if not table_exists:
        return False, "settings schema did not create operblock_icons table"
    if catalog_row is None:
        return False, "settings schema did not create operblock_icons catalog version"

    defaults_context = {
        "service": service,
        "icon_dir": icon_dir,
        "sevo_source_blob": sevo_source_blob,
        "remcard_icon_definitions": REMCARD_ICON_DEFINITIONS,
        "definitions_by_key": DEFAULT_ICON_DEFINITION_BY_KEY,
        "male_key": OPERBLOCK_PATIENT_MALE_ICON_KEY,
        "female_key": OPERBLOCK_PATIENT_FEMALE_ICON_KEY,
        "default_icon_file_for_key": default_icon_file_for_key,
        "default_drug_icon_file": default_drug_icon_file,
        "candidate_keys": drug_icon_candidate_keys,
        "candidate_keys_from_payload": drug_icon_candidate_keys_from_payload,
        "type_icon_key": type_icon_key,
        "edit_icon_key": edit_icon_key,
    }
    ok, details = _check_operblock_icon_defaults(defaults_context)
    if not ok:
        return False, details
    noisy_des_candidates = defaults_context["noisy_des_candidates"]

    before_version, before_hash = service.get_catalog_version(OPERBLOCK_ICONS_KEY)
    service.save_operblock_icon(
        icon_key="drug:manual:gas:desflurane",
        category="drug",
        target_key="manual:gas:desflurane",
        name="Иконка препарата: Десфлюран",
        default_file="gas_izm.png",
        image_path=des_source_path,
        sort_order=10020,
        changed_by_role="regression",
    )
    after_version, after_hash = service.get_catalog_version(OPERBLOCK_ICONS_KEY)
    if after_version <= before_version or after_hash == before_hash:
        return False, "сохранение иконки препарата не обновило каталог operblock_icons"
    des_record = service.list_operblock_icons().get("drug:manual:gas:desflurane")
    if not des_record or des_record.get("image_blob") != des_source_blob:
        return False, "иконка десфлюрана не загрузилась из файлового хранилища настроек"
    if des_record.get("source") != "manual":
        return False, "пользовательская иконка десфлюрана должна быть manual-строкой"
    if des_record.get("default_file") != "gas_izm.png":
        return False, "десфлюран должен оставаться на стандартном fallback gas_izm.png"
    try:
        configure_settings_service(settings_db_path=service.db.db_path)
        invalidate_operblock_icon_cache()
        label_source = current_operblock_icon_source(["drug-label:десфлюран"], fallback_file="gas_izm.png")
        if "gas.png" not in label_source:
            return False, "назначение десфлюрана без preset_id не нашло сохраненную иконку по названию"
        noisy_label_source = current_operblock_icon_source(noisy_des_candidates, fallback_file="gas_izm.png")
        if "gas.png" not in noisy_label_source:
            return False, "окно изменения десфлюрана с дозой в названии не нашло сохраненную иконку"
    finally:
        reset_settings_service()

    snapshot_path = os.path.join(temp_root, "settings_release_snapshot.json")
    export_report = export_settings_release_snapshot(
        source_baza_dir,
        snapshot_path,
        release_version="operblock-icons-regression",
        release_commit="regression",
    )
    if export_report.get("row_counts", {}).get("operblock_icons", 0) < 2:
        return False, "release snapshot не экспортировал иконки оперблока"

    target_baza_dir = os.path.join(temp_root, "TargetBaza")
    target_service = SettingsService(SettingsDatabase(baza_dir=target_baza_dir))
    target_service.ensure_ready()
    apply_report = apply_settings_release_snapshot(
        target_service.db,
        snapshot_path,
        bump_catalog_version=target_service._bump_catalog_version,
    )
    if not apply_report.get("applied"):
        return False, f"release snapshot с иконками не применился: {apply_report}"
    if OPERBLOCK_ICONS_KEY not in apply_report.get("changed_catalogs", []):
        return False, "release snapshot не обновил каталог operblock_icons"
    _target_version, target_records = target_service.get_operblock_icon_records(
        ["drug:manual:gas:desflurane"], include_blob=True, ensure_defaults=False
    )
    target_row = target_records.get("drug:manual:gas:desflurane")
    if not target_row or target_row.get("image_blob") != des_source_blob:
        return False, "иконка десфлюрана не перенеслась в файловое хранилище целевых настроек"
    if target_row.get("default_file") != "gas_izm.png":
        return False, "fallback десфлюрана изменился после переноса release snapshot"

    preserved_baza_dir = os.path.join(temp_root, "PreservedBaza")
    preserved_service = SettingsService(SettingsDatabase(baza_dir=preserved_baza_dir))
    preserved_service.ensure_ready()
    preserved_source_path = os.path.join(icon_dir, "bolus.png")
    preserved_blob = Path(preserved_source_path).read_bytes()
    time.sleep(1.1)
    preserved_service.save_operblock_icon(
        icon_key="drug:manual:gas:desflurane",
        category="drug",
        target_key="manual:gas:desflurane",
        name="Иконка препарата: Десфлюран",
        default_file="gas_izm.png",
        image_path=preserved_source_path,
        sort_order=10020,
        changed_by_role="regression",
    )
    preserved_report = apply_settings_release_snapshot(
        preserved_service.db,
        snapshot_path,
        bump_catalog_version=preserved_service._bump_catalog_version,
    )
    preserved_table = preserved_report.get("tables", {}).get("operblock_icons", {})
    if int(preserved_table.get("preserved") or 0) < 1:
        return False, "release snapshot не сохранил более свежую ручную иконку"
    _preserved_version, preserved_records = preserved_service.get_operblock_icon_records(
        ["drug:manual:gas:desflurane"], include_blob=True, ensure_defaults=False
    )
    preserved_row = preserved_records.get("drug:manual:gas:desflurane")
    if not preserved_row or preserved_row.get("source") != "manual" or preserved_row.get("image_blob") != preserved_blob:
        return False, "release snapshot перезаписал ручную иконку десфлюрана"
    return True, "ok"


def _check_background_files_use_shared_settings_folder(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.settings_db_paths import get_settings_backgrounds_dir
    from rem_card.data.settings.settings_release import (
        apply_settings_release_snapshot,
        export_settings_release_snapshot,
    )
    from rem_card.services.settings.settings_service import (
        BACKGROUND_SETTINGS_KEY,
        configure_settings_service,
        reset_settings_service,
    )
    from rem_card.ui.shared import background_settings as bg

    saved_baza_dir = os.environ.get("REMCARD_BAZA_DIR")
    original_get_icon_dir = bg.get_icon_dir
    original_materialize = bg._materialize_background_from_db
    try:
        baza_dir = os.path.join(temp_root, "Baza")
        os.environ["REMCARD_BAZA_DIR"] = baza_dir
        reset_settings_service()
        service = configure_settings_service(
            settings_db_path=os.path.join(baza_dir, "settings", "remcard_settings.db")
        )
        service.ensure_ready()

        source_bytes = b"\x89PNG\r\n\x1a\n" + (b"x" * ((2 * 1024 * 1024) + 256))
        source_dir = os.path.join(temp_root, "source")
        os.makedirs(source_dir, exist_ok=True)
        source_path = os.path.join(source_dir, "new_background.png")
        Path(source_path).write_bytes(source_bytes)

        file_name = bg.copy_background_to_backgrounds_dir(source_path)
        expected_dir = get_settings_backgrounds_dir(baza_dir)
        expected_path = os.path.join(expected_dir, file_name)
        if not os.path.isfile(expected_path):
            return False, "загруженный фон не скопирован в settings/backgrounds"
        if os.path.normcase(bg.background_storage_file_path(file_name)) != os.path.normcase(expected_path):
            return False, "background_storage_file_path не указывает на settings/backgrounds"
        if os.path.normcase(bg.background_file_path(file_name)) != os.path.normcase(expected_path):
            return False, "background_file_path не выбирает settings/backgrounds первым"

        background_key = "background_shared_storage_regression"
        payload = bg.normalize_background_settings_payload(
            {
                "backgrounds": [
                    {
                        "id": background_key,
                        "name": "Регрессионный фон",
                        "file": file_name,
                        "start": "01-01",
                        "end": "12-31",
                    }
                ]
            }
        )
        service.set_app_setting(
            "shared",
            "background_settings",
            payload,
            catalog_key=BACKGROUND_SETTINGS_KEY,
            entity_type="background_settings",
            operation="regression",
        )
        with service.db.read_connection() as conn:
            row = conn.execute(
                "SELECT image_blob FROM ui_backgrounds WHERE background_key = ?",
                (background_key,),
            ).fetchone()
        if not row or row["image_blob"] is not None:
            return False, "ui_backgrounds сохранил лишний BLOB загруженного изображения"

        materialized = bg.ensure_background_file_available(
            {"id": background_key, "file": file_name, "start": "01-01", "end": "12-31"}
        )
        if os.path.normcase(materialized) != os.path.normcase(expected_path):
            return False, "фон не читается из settings/backgrounds"
        if not os.path.isfile(expected_path) or Path(expected_path).read_bytes() != source_bytes:
            return False, "файл фона в общей папке отсутствует или поврежден"

        snapshot_path = os.path.join(temp_root, "settings_release_snapshot.json")
        export_settings_release_snapshot(
            baza_dir,
            snapshot_path,
            release_version="background-regression",
            release_commit="regression",
        )
        target_baza_dir = os.path.join(temp_root, "TargetBaza")
        os.environ["REMCARD_BAZA_DIR"] = target_baza_dir
        reset_settings_service()
        target_service = configure_settings_service(
            settings_db_path=os.path.join(target_baza_dir, "settings", "remcard_settings.db")
        )
        target_service.ensure_ready()
        apply_report = apply_settings_release_snapshot(
            target_service.db,
            snapshot_path,
            bump_catalog_version=target_service._bump_catalog_version,
        )
        if not apply_report.get("applied"):
            return False, f"release snapshot с фоном не применился: {apply_report}"
        target_path = os.path.join(get_settings_backgrounds_dir(target_baza_dir), file_name)
        target_materialized = bg.ensure_background_file_available(
            {"id": background_key, "file": file_name, "start": "01-01", "end": "12-31"}
        )
        if os.path.normcase(target_materialized) != os.path.normcase(target_path):
            return False, "release snapshot восстановил фон не в settings/backgrounds целевой базы"
        if not os.path.isfile(target_path) or Path(target_path).read_bytes() != source_bytes:
            return False, "release snapshot не перенес файл фона на целевую базу"

        legacy_dir = os.path.join(temp_root, "legacy_icon")
        os.makedirs(legacy_dir, exist_ok=True)
        legacy_bytes = b"legacy-background"
        legacy_file = os.path.join(legacy_dir, "legacy_background.png")
        Path(legacy_file).write_bytes(legacy_bytes)
        bg.get_icon_dir = lambda: legacy_dir
        bg._materialize_background_from_db = lambda _entry, _path: False
        legacy_result = bg.ensure_background_file_available({"id": "legacy", "file": "legacy_background.png"})
        expected_legacy_path = os.path.join(get_settings_backgrounds_dir(target_baza_dir), "legacy_background.png")
        if os.path.normcase(legacy_result) != os.path.normcase(expected_legacy_path):
            return False, "старый фон из icon не перенесен в settings/backgrounds"
        if not os.path.isfile(expected_legacy_path) or Path(expected_legacy_path).read_bytes() != legacy_bytes:
            return False, "копия старого фона из icon отсутствует или повреждена"

        return True, "ok"
    finally:
        bg.get_icon_dir = original_get_icon_dir
        bg._materialize_background_from_db = original_materialize
        bg.invalidate_background_settings_cache()
        reset_settings_service()
        if saved_baza_dir is None:
            os.environ.pop("REMCARD_BAZA_DIR", None)
        else:
            os.environ["REMCARD_BAZA_DIR"] = saved_baza_dir


def _check_background_release_preserves_user_settings(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.data.settings.settings_release import apply_settings_release_snapshot, export_settings_release_snapshot
    from rem_card.services.settings.settings_service import BACKGROUND_SETTINGS_KEY, SettingsService
    from rem_card.ui.shared import background_settings as bg

    source_baza = os.path.join(temp_root, "SourceBaza")
    target_baza = os.path.join(temp_root, "TargetBaza")
    background_key = "background_release_conflict_regression"

    source_service = SettingsService(SettingsDatabase(baza_dir=source_baza))
    source_service.ensure_ready()
    release_payload = bg.normalize_background_settings_payload(
        {
            "backgrounds": [
                {
                    "id": background_key,
                    "name": "Релизный фон",
                    "file": "",
                    "start": "01-01",
                    "end": "12-31",
                }
            ]
        }
    )
    source_service.set_app_setting(
        "shared",
        "background_settings",
        release_payload,
        catalog_key=BACKGROUND_SETTINGS_KEY,
        entity_type="background_settings",
        operation="release_probe",
        changed_by_role="system",
    )
    snapshot_path = os.path.join(temp_root, "settings_release_snapshot.json")
    export_settings_release_snapshot(
        source_baza,
        snapshot_path,
        release_version="background-preserve-regression",
        release_commit="regression",
    )

    target_service = SettingsService(SettingsDatabase(baza_dir=target_baza))
    target_service.ensure_ready()
    user_payload = bg.normalize_background_settings_payload(
        {
            "backgrounds": [
                {
                    "id": background_key,
                    "name": "Пользовательский фон",
                    "file": "",
                    "start": "02-01",
                    "end": "02-02",
                }
            ]
        }
    )
    target_service.set_app_setting(
        "shared",
        "background_settings",
        user_payload,
        catalog_key=BACKGROUND_SETTINGS_KEY,
        entity_type="background_settings",
        operation="user_probe",
        changed_by_role="doctor",
    )
    with target_service.db.transaction("regression_old_background_user_edit") as cursor:
        cursor.execute(
            """
            UPDATE app_settings
            SET updated_at = '2000-01-01 00:00:00'
            WHERE scope = 'shared' AND key = 'background_settings'
            """
        )
        cursor.execute(
            "UPDATE ui_backgrounds SET updated_at = '2000-01-01 00:00:00' WHERE background_key = ?",
            (background_key,),
        )

    apply_report = apply_settings_release_snapshot(
        target_service.db,
        snapshot_path,
        bump_catalog_version=target_service._bump_catalog_version,
    )
    table_reports = apply_report.get("tables") or {}
    if int((table_reports.get("app_settings") or {}).get("preserved") or 0) < 1:
        return False, f"background_settings app row was not preserved: {apply_report}"
    if int((table_reports.get("ui_backgrounds") or {}).get("preserved") or 0) < 1:
        return False, f"background row was not preserved: {apply_report}"

    target_service.invalidate_cache()
    final_payload = target_service.get_app_setting("shared", "background_settings", default={})
    final_names = [str(item.get("name") or "") for item in final_payload.get("backgrounds") or []]
    if "Пользовательский фон" not in final_names or "Релизный фон" in final_names:
        return False, f"release snapshot overwrote user background app setting: {final_names}"
    with target_service.db.read_connection() as conn:
        row = conn.execute(
            "SELECT value_json FROM ui_backgrounds WHERE background_key = ?",
            (background_key,),
        ).fetchone()
    row_payload = json.loads(row["value_json"]) if row and row["value_json"] else {}
    if row_payload.get("name") != "Пользовательский фон":
        return False, f"release snapshot overwrote user background row: {row_payload}"

    repair_baza = os.path.join(temp_root, "RepairBaza")
    repair_service = SettingsService(SettingsDatabase(baza_dir=repair_baza))
    repair_service.ensure_ready()
    user_background_file = "background_release_user.png"
    user_background_dir = os.path.join(repair_service.db.settings_dir, "backgrounds")
    os.makedirs(user_background_dir, exist_ok=True)
    with open(os.path.join(user_background_dir, user_background_file), "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n")
    repair_user_payload = bg.normalize_background_settings_payload(
        {
            "backgrounds": [
                {
                    "id": background_key,
                    "name": "Пользовательский фон",
                    "file": user_background_file,
                    "start": "02-01",
                    "end": "02-02",
                }
            ]
        }
    )
    repair_service.set_app_setting(
        "shared",
        "background_settings",
        repair_user_payload,
        catalog_key=BACKGROUND_SETTINGS_KEY,
        entity_type="background_settings",
        operation="user_probe",
        changed_by_role="doctor",
    )
    default_only_payload = bg.normalize_background_settings_payload({"backgrounds": []})
    with repair_service.db.transaction("regression_broken_background_app_setting") as cursor:
        cursor.execute(
            """
            UPDATE app_settings
            SET value_json = ?, updated_by_role = 'system', updated_at = '2000-01-01 00:00:00'
            WHERE scope = 'shared' AND key = 'background_settings'
            """,
            (json.dumps(default_only_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),),
        )
    restarted = SettingsService(SettingsDatabase(baza_dir=repair_baza))
    repair_info = restarted.ensure_ready()
    repair_report = repair_info.get("background_settings_repair") or {}
    if int(repair_report.get("restored_rows") or 0) < 1:
        return False, f"background repair did not restore missing rows: {repair_info}"
    repaired_payload = restarted.get_app_setting("shared", "background_settings", default={})
    repaired_names = [str(item.get("name") or "") for item in repaired_payload.get("backgrounds") or []]
    if "Пользовательский фон" not in repaired_names:
        return False, f"background repair did not restore user background: {repaired_names}"
    return True, "ok"


def _check_operblock_precommit_shadow_journal_contract(temp_root: str) -> tuple[bool, str]:
    data_service_source = Path("services/data_service.py").read_text(encoding="utf-8")
    store_source = Path("app/operblock_offline_store.py").read_text(encoding="utf-8")
    if "def record_operblock_write_intent" not in store_source:
        return False, "record_operblock_write_intent is missing"
    for marker in (
        "opblock_write_intent",
        "opblock_write_remote_committed",
        "opblock_write_failed",
        "operation_uuid",
        "remote_commit_state",
    ):
        if marker not in store_source:
            return False, f"shadow journal marker is missing: {marker}"
    enqueue_idx = data_service_source.find("def enqueue_write(")
    intent_idx = data_service_source.find("operation_uuid = self._record_operblock_write_intent(description)", enqueue_idx)
    submit_idx = data_service_source.find("self._queue.submit(", enqueue_idx)
    if enqueue_idx < 0 or intent_idx < 0 or submit_idx < 0 or not intent_idx < submit_idx:
        return False, "enqueue_write must record durable opblock intent before queue submit"
    success_idx = data_service_source.find("def handle_success(result):", enqueue_idx)
    success_emit_idx = data_service_source.find("self._success_callback_requested.emit(on_success, result)", success_idx)
    mirror_idx = data_service_source.find("self._mirror_operblock_write_after_commit(", success_emit_idx)
    if success_idx < 0 or success_emit_idx < 0 or mirror_idx < 0 or not success_emit_idx < mirror_idx:
        return False, "queued committed writes must post user success before post-commit shadow mirror scheduling"
    if "opblock_shadow_mirror_decoupled_from_write" not in data_service_source:
        return False, "post-commit shadow mirror decoupling metric is missing"
    if "raise RuntimeError(\"Не удалось сохранить локальный журнал записи оперблока.\")" not in data_service_source:
        return False, "opblock network write must not continue after pre-commit journal failure"
    return True, "ok"


def _check_operblock_migration_dialog_non_closable_contract(temp_root: str) -> tuple[bool, str]:
    main_window_source = Path("ui/main_window.py").read_text(encoding="utf-8")
    main_source = Path("app/main.py").read_text(encoding="utf-8")
    for source_name, source in (("ui/main_window.py", main_window_source), ("app/main.py", main_source)):
        if "Не выключайте ПК. Идёт перенос данных оперблока." not in source:
            return False, f"migration progress text missing in {source_name}"
        if "~Qt.WindowCloseButtonHint" not in source:
            return False, f"migration dialog close button is not disabled in {source_name}"
    if "_close_operblock_migration_dialog(dialog)" not in main_window_source:
        return False, "post-release migration dialog is not closed in finally"
    if "_close_operblock_migration_progress_dialog(dialog, app)" not in main_source:
        return False, "pre-window migration dialog is not closed in finally"
    if "_run_pending_operblock_offline_migration_before_window(" not in main_source:
        return False, "pre-window migration hook is missing"
    return True, "ok"


def _check_operblock_offline_acceptance_runner_rc_scenarios(temp_root: str) -> tuple[bool, str]:
    source = Path("scripts/operblock_offline_acceptance_runner.py").read_text(encoding="utf-8")
    required = (
        "initial_network_missing",
        "migration",
        "migration_non_rao_department",
        "active_blocks_migration",
        "table_conflict",
        "protocol_conflict",
        "cancelled_excluded",
        "runtime_drop_same_case",
        "precommit_journal_runtime_drop",
        "unconfirmed_write_not_marked_saved",
        "retention",
        "retention_preserves_unverified",
    )
    missing = [name for name in required if name not in source]
    if missing:
        return False, f"acceptance runner is missing RC scenarios: {missing}"
    return True, "ok"

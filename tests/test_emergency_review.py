from __future__ import annotations

import json
import shutil
import sqlite3

import pytest

from rem_card.app import emergency_review
from rem_card.app.emergency_row_level_merge import RowMergePlan, RowOperation, plan_digest
from rem_card.app.unified_db_schema import ensure_unified_schema


def _context_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE patients (
            id INTEGER PRIMARY KEY, full_name TEXT, last_name TEXT, first_name TEXT, middle_name TEXT
        );
        CREATE TABLE admissions (
            id INTEGER PRIMARY KEY, patient_id INTEGER, history_number TEXT,
            admission_datetime TEXT, transfer_datetime TEXT, death_datetime TEXT
        );
        CREATE TABLE drugs (id INTEGER PRIMARY KEY, code TEXT, name TEXT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, admission_id INTEGER, text TEXT, drug_key TEXT);
        """
    )
    conn.execute("INSERT INTO patients VALUES (7, 'Иванов Иван', 'Иванов', 'Иван', 'Иванович')")
    conn.execute("INSERT INTO admissions VALUES (12, 7, '456', '2026-09-09 11:30', NULL, NULL)")
    conn.execute("INSERT INTO drugs VALUES (3, 'noradrenaline', 'Норадреналин')")
    conn.commit()
    conn.close()


def _paths(tmp_path):
    paths = [tmp_path / name for name in ("base.db", "local.db", "remote.db")]
    for path in paths:
        _context_db(path)
    return paths


def _review_plan() -> RowMergePlan:
    return RowMergePlan(
        operations=[
            RowOperation(
                table="orders", action="insert", pk_columns=("id",), pk_values=(4,),
                row={
                    "id": 4, "admission_id": 12, "datetime": "2026-09-10 08:15",
                    "text": "Норадреналин", "drug_key": "noradrenaline", "dose_value": 0.1,
                    "dose_unit": "мкг/кг/мин", "rate_ml_h": 4.0,
                },
                admission_id=12, patient_id=7,
            ),
            RowOperation(
                table="vitals", action="update", pk_columns=("id",), pk_values=(8,),
                row={
                    "id": 8, "admission_id": 12, "datetime": "2026-09-10 07:30",
                    "sys": 130, "dia": 85, "pulse": 76, "temp": 37.2, "spo2": 96,
                },
                remote_row={
                    "id": 8, "admission_id": 12, "datetime": "2026-09-10 07:30",
                    "sys": 120, "dia": 80, "pulse": 70, "temp": 36.8, "spo2": 98,
                },
                admission_id=12, patient_id=7,
            ),
        ],
        changed_tables_summary={"orders": {"insert": 1}, "vitals": {"update": 1}},
    )


@pytest.mark.parametrize("mark,expected", [
    ("nurse_executed", "выполнено"),
    ("nurse_not_executed", "не выполнено"),
    ("Отложено до осмотра", "Отложено до осмотра"),
])
def test_review_explains_nurse_marks_and_preserves_free_text(tmp_path, monkeypatch, mark, expected):
    base, local, remote = _paths(tmp_path)
    plan = RowMergePlan(operations=[RowOperation(
        table="administrations", action="insert", pk_columns=("id",), pk_values=(1,),
        row={"id": 1, "admission_id": 12, "status": "planned", "comment": mark,
             "executed_time": "2026-09-10 20:00"}, admission_id=12, patient_id=7,
    )], changed_tables_summary={"administrations": {"insert": 1}})
    monkeypatch.setattr(emergency_review, "build_row_merge_plan", lambda **_: plan)
    review = emergency_review.build_emergency_review(
        base_db_path=str(base), local_db_path=str(local), remote_db_path=str(remote))
    assert not review["blockers"]
    text = review["patients"][0]["sections"][0]["items"][0]["text"]
    assert f"Отметка / комментарий медсестры: {expected}" in text
    assert "Время отметки:" in text
    assert "Состояние записи в плане: запланировано" in text
    assert "nurse_" not in text


def test_shared_patient_identity_is_reviewed_on_each_admission(tmp_path):
    base, local, remote = _paths(tmp_path)
    for path in (base, local, remote):
        with sqlite3.connect(path) as conn:
            conn.execute("INSERT INTO admissions VALUES (13, 7, '457', '2026-09-10 11:30', NULL, NULL)")
    with sqlite3.connect(local) as conn:
        conn.execute("UPDATE patients SET last_name='Исправленная фамилия' WHERE id=7")
    review = emergency_review.build_emergency_review(
        base_db_path=str(base), local_db_path=str(local), remote_db_path=str(remote))
    assert not review["blockers"]
    assert {item["admission_id"] for item in review["patients"]} == {12, 13}
    assert emergency_review.review_selection(review, [12])["blockers"]
    assert not emergency_review.review_selection(review, [12, 13])["blockers"]


def test_review_is_serializable_and_puts_all_vitals_before_orders(tmp_path, monkeypatch):
    base, local, remote = _paths(tmp_path)
    plan = _review_plan()
    monkeypatch.setattr(emergency_review, "build_row_merge_plan", lambda **_kwargs: plan)

    review = emergency_review.build_emergency_review(
        base_db_path=str(base), local_db_path=str(local), remote_db_path=str(remote)
    )

    assert json.loads(json.dumps(review, ensure_ascii=False))["schema_version"] == 1
    assert review["blockers"] == []
    assert review["patients"][0]["selected_default"] is False
    assert "Иванов Иван Иванович" in review["patients"][0]["title"]
    assert "история № 456" in review["patients"][0]["title"]
    assert [section["key"] for section in review["patients"][0]["sections"]] == ["vitals", "orders"]

    vital_text = review["patients"][0]["sections"][0]["items"][0]["text"]
    assert "АД: 120/80 мм рт. ст. → 130/85 мм рт. ст." in vital_text
    assert "Пульс: 70 уд/мин → 76 уд/мин" in vital_text
    assert "Температура: 36.8 °C → 37.2 °C" in vital_text
    assert "SpO₂: 98 % → 96 %" in vital_text

    order_text = review["patients"][0]["sections"][1]["items"][0]["text"]
    assert "Назначение «Норадреналин, 0.1 мкг/кг/мин»" in order_text
    assert "Препарат: Норадреналин" in order_text
    assert "Доза: 0.1 мкг/кг/мин" in order_text
    assert "Скорость: 4 мл/ч" in order_text
    assert "{\"" not in order_text
    assert "admission_id" not in order_text


def test_unknown_meaningful_field_blocks_review(tmp_path, monkeypatch):
    base, local, remote = _paths(tmp_path)
    plan = RowMergePlan(operations=[RowOperation(
        table="vitals", action="insert", pk_columns=("id",), pk_values=(1,),
        row={"id": 1, "admission_id": 12, "datetime": "2026-09-10 08:00", "new_clinical_value": 9},
        admission_id=12, patient_id=7,
    )])
    monkeypatch.setattr(emergency_review, "build_row_merge_plan", lambda **_kwargs: plan)

    review = emergency_review.build_emergency_review(
        base_db_path=str(base), local_db_path=str(local), remote_db_path=str(remote)
    )

    assert review["blockers"][0]["code"] == "review_description_unsupported"
    assert review["patients"][0]["operation_count"] == 0


def _review_text(review) -> str:
    return "\n".join(
        item["text"]
        for patient in review["patients"]
        for section in patient["sections"]
        for item in section["items"]
    )


def test_real_admission_and_death_payloads_are_fully_human_readable(tmp_path, monkeypatch):
    base, local, remote = _paths(tmp_path)
    intake = {
        "source": "operblock_rao_transfer",
        "operation_case_id": 81,
        "source_patient_id": 91,
        "source_admission_id": 92,
        "table_code": "Стол 2",
        "operation_finished_at": "2026-09-10T11:20:00",
        "transfer_department": "РАО",
        "operation_name": "Лапаротомия",
        "anesthesia_assistance_type": "Общая анестезия",
        "surgeons": ["Иванов И.И.", "Петров П.П."],
        "anesthesiologist": "Сидоров С.С.",
        "anesthetist": "Орлова О.О.",
        "height_cm": 172,
        "weight_kg": 70.5,
        "allergies": "Пенициллин",
        "blood_group": "A(II)",
        "blood_rh": "Rh+",
    }
    death = {
        "outcome_type": "biological_death",
        "clinical_death_datetime": "2026-09-10T12:00:00",
        "biological_death_datetime": "2026-09-10T12:30:00",
        "cardiac_arrest_cause": "Асистолия",
        "measures": [{"name": "СЛР", "value": "Компрессии 100–120 в минуту"}],
        "comment": "Без эффекта",
        "death_protocol": {
            "doctor": "Иванов И.И.", "position": "врач анестезиолог-реаниматолог",
            "workplace": "Городская больница", "patient": "Иванов Иван", "gender": "мужской",
            "age": "55 лет", "history_number": "456", "other": "",
            "cpr_stop_reason": "Неэффективность в течение 30 минут",
            "biological_death_date": "10.09.2026", "biological_death_time": "12:30",
            "signature_doctor": "Иванов И.И.",
        },
    }
    plan = RowMergePlan(operations=[
        RowOperation(
            table="admissions", action="update", pk_columns=("id",), pk_values=(12,),
            row={"id": 12, "patient_id": 7, "intake_extra_json": json.dumps(intake, ensure_ascii=False)},
            remote_row={"id": 12, "patient_id": 7, "intake_extra_json": None},
            admission_id=12, patient_id=7,
        ),
        RowOperation(
            table="admissions", action="update", pk_columns=("id",), pk_values=(12,),
            row={"id": 12, "patient_id": 7, "cardiac_arrest_measures_json": json.dumps(death, ensure_ascii=False)},
            remote_row={"id": 12, "patient_id": 7, "cardiac_arrest_measures_json": None},
            admission_id=12, patient_id=7,
        ),
    ])
    monkeypatch.setattr(emergency_review, "build_row_merge_plan", lambda **_kwargs: plan)

    review = emergency_review.build_emergency_review(
        base_db_path=str(base), local_db_path=str(local), remote_db_path=str(remote)
    )
    text = _review_text(review)

    assert review["blockers"] == []
    assert "Масса тела: 70.5 кг" in text
    assert "Рост: 172 см" in text
    assert "Хирурги: Иванов И.И., Петров П.П." in text
    assert "Исход: биологическая смерть" in text
    assert "Реанимационные мероприятия: Мероприятие: СЛР; Описание: Компрессии 100–120 в минуту" in text
    assert "Причина прекращения СЛР: Неэффективность в течение 30 минут" in text
    assert "operation_case_id" not in text
    assert "source_admission_id" not in text
    assert "operblock_rao_transfer" not in text
    assert "biological_death" not in text


def test_real_ventilation_payload_and_codes_have_labels_and_units(tmp_path, monkeypatch):
    base, local, remote = _paths(tmp_path)
    parameters = {"RR": 16, "TV": 450, "PEEP": 5, "FiO2": 0.4}
    plan = RowMergePlan(operations=[
        RowOperation(
            table="ivl_episodes", action="insert", pk_columns=("id",), pk_values=(20,),
            row={
                "id": 20, "admission_id": 12, "episode_number": 1, "start_time": "2026-09-10T08:00:00",
                "type": "delivery", "start_type": "ADMISSION", "delivery_type": "APPARATUS", "is_active": 1,
            }, admission_id=12, patient_id=7,
        ),
        RowOperation(
            table="clinical_events", action="insert", pk_columns=("id",), pk_values=(21,),
            row={
                "id": 21, "admission_id": 12, "timestamp": "2026-09-10T08:00:00",
                "event_type": "START_VENT", "mode": "CONTROLLED_VCV",
                "parameters_json": json.dumps(parameters), "data": json.dumps(parameters),
            }, admission_id=12, patient_id=7,
        ),
        RowOperation(
            table="devices", action="insert", pk_columns=("id",), pk_values=(22,),
            row={
                "id": 22, "admission_id": 12, "device_type": "ENDOTRACHEAL_TUBE",
                "insertion_date": "2026-09-10T08:00:00", "location": "22 см от резцов",
            }, admission_id=12, patient_id=7,
        ),
    ])
    monkeypatch.setattr(emergency_review, "build_row_merge_plan", lambda **_kwargs: plan)

    review = emergency_review.build_emergency_review(
        base_db_path=str(base), local_db_path=str(local), remote_db_path=str(remote)
    )
    text = _review_text(review)

    assert review["blockers"] == []
    assert "Вид начала: с поступления" in text
    assert "Способ проведения: аппарат ИВЛ" in text
    assert "Тип события: старт ИВЛ" in text
    assert "Частота дыхания: 16 в мин" in text
    assert "Дыхательный объём: 450 мл" in text
    assert "ПДКВ: 5 см вод. ст." in text
    assert "Кислород во вдыхаемой смеси: 0.4 (40 %)" in text
    assert "Устройство: эндотрахеальная трубка" in text
    assert "START_VENT" not in text
    assert "ENDOTRACHEAL_TUBE" not in text


def test_real_procedure_payloads_and_codes_are_translated(tmp_path, monkeypatch):
    base, local, remote = _paths(tmp_path)
    observation = {
        "before": {"bp": "120/80", "pulse": "75", "temp": "36.6", "diuresis": "сохранен, желтая"},
        "hour1": {"bp": "118/76", "pulse": "78", "temp": "36.7", "diuresis": "сохранен, желтая"},
        "hour2": {"bp": "116/74", "pulse": "76", "temp": "36.7", "diuresis": "сохранен, желтая"},
    }
    plan = RowMergePlan(operations=[
        RowOperation(
            table="procedures", action="insert", pk_columns=("id",), pk_values=(30,),
            row={
                "id": 30, "admission_id": 12, "patient_id": 7, "procedure_type": "CVC",
                "status": "active", "started_at": "2026-09-10T09:00:00",
                "patient_snapshot_json": json.dumps({
                    "patient_id": 7, "admission_id": 12, "admission_datetime": "2026-09-09T11:30:00",
                    "full_name": "Иванов Иван Иванович", "sex": "male", "age": 55, "age_months": 660,
                    "age_unit": "л", "birth_date": "1971-01-01", "history_number": "456",
                    "department": "РАО", "bed_number": 2, "diagnosis": "Пневмония", "diagnosis_code": "J18.9",
                }, ensure_ascii=False),
            }, admission_id=12, patient_id=7,
        ),
        RowOperation(
            table="procedure_consents", action="insert", pk_columns=("id",), pk_values=(32,),
            row={
                "id": 32, "procedure_id": 30, "consent_kind": "CVC_CONSENT", "consent_mode": "consilium",
                "patient_signed": 0,
                "consilium_json": json.dumps({
                    "doctor_1": "Иванов И.И.", "doctor_2": "Петров П.П.", "doctor_3": "Сидоров С.С.",
                    "notes": "По жизненным показаниям",
                }, ensure_ascii=False),
            }, admission_id=12, patient_id=7,
        ),
        RowOperation(
            table="procedure_cvc", action="insert", pk_columns=("procedure_id",), pk_values=(30,),
            row={
                "procedure_id": 30,
                "indications_json": json.dumps({"selected": ["vasopressors"], "other": ""}),
                "procedure_place_code": "icu_room", "anesthesia_code": "local", "access_code": "ijv_right",
                "method_code": "seldinger", "ultrasound_control_json": json.dumps(["dynamic"]),
                "fixation_json": json.dumps(["ligature"]),
                "position_confirmation_json": json.dumps({"selected": ["xray"], "comment": "Кончик в ВПВ"}),
                "technical_difficulty_code": "none", "catheter_status": "active",
                "usage_complications_code": "none",
            }, admission_id=12, patient_id=7,
        ),
        RowOperation(
            table="procedure_transfusion", action="insert", pk_columns=("procedure_id",), pk_values=(31,),
            row={
                "procedure_id": 31, "indication_code": "voce", "alloimmune_antibodies": "negative",
                "transfusions_history": "yes", "reactions_history": "no", "individual_selection_history": "no",
                "observation_json": json.dumps(observation, ensure_ascii=False),
            }, admission_id=12, patient_id=7,
        ),
    ])
    monkeypatch.setattr(emergency_review, "build_row_merge_plan", lambda **_kwargs: plan)

    review = emergency_review.build_emergency_review(
        base_db_path=str(base), local_db_path=str(local), remote_db_path=str(remote)
    )
    text = _review_text(review)

    assert review["blockers"] == []
    assert "Процедура «ЦВК»" in text
    assert "Статус: Активна" in text
    assert "ФИО: Иванов Иван Иванович" in text
    assert "Пол: мужской" in text
    assert "Вид согласия: согласие на ЦВК" in text
    assert "Кто дал согласие: консилиум" in text
    assert "Врач 1: Иванов И.И." in text
    assert "Проведение вазопрессорной терапии" in text
    assert "Место проведения: Палата реанимации и интенсивной терапии" in text
    assert "Доступ: Внутренняя яремная вена правая" in text
    assert "УЗ-контроль: Динамический УЗ-контроль" in text
    assert "Фиксация: Лигатурой" in text
    assert "Способы подтверждения: Обзорная рентгенография" in text
    assert "Показание: ВОЦЭ - восполнение объема циркулирующих эритроцитов" in text
    assert "Перед началом переливания: Артериальное давление: 120/80" in text
    assert "vasopressors" not in text
    assert "icu_room" not in text
    assert "CVC_CONSENT" not in text


def test_real_diet_payload_has_human_labels_and_units(tmp_path, monkeypatch):
    base, local, remote = _paths(tmp_path)
    schedule = [{"key": "breakfast", "meal": "Завтрак", "time": "09:00", "amount": 200, "note": ""}]
    details = {
        "consistency": "Протёртая", "temperature": "Тёплая", "salt_limit": "3 г",
        "fractional": True, "daily_fluid_ml": 1200, "special_instructions": "Малыми порциями",
        "comment": "", "no_food": False, "no_fluids": False, "on_demand": False,
    }
    plan = RowMergePlan(operations=[RowOperation(
        table="diet_plan_versions", action="insert", pk_columns=("id",), pk_values=(40,),
        row={
            "id": 40, "admission_id": 12, "diet_name": "Стол № 1",
            "effective_from": "2026-09-10T08:00:00", "schedule_json": json.dumps(schedule, ensure_ascii=False),
            "details_json": json.dumps(details, ensure_ascii=False),
        }, admission_id=12, patient_id=7,
    )])
    monkeypatch.setattr(emergency_review, "build_row_merge_plan", lambda **_kwargs: plan)

    review = emergency_review.build_emergency_review(
        base_db_path=str(base), local_db_path=str(local), remote_db_path=str(remote)
    )
    text = _review_text(review)

    assert review["blockers"] == []
    assert "Приём пищи: Завтрак" in text
    assert "Время: 09:00" in text
    assert "Объём: 200 мл" in text
    assert "Консистенция: Протёртая" in text
    assert "Дробное питание: да" in text
    assert "Жидкость за сутки: 1200 мл" in text
    assert "breakfast" not in text


@pytest.mark.parametrize(
    ("table", "field", "value"),
    [
        ("clinical_events", "event_type", "NEW_VENT_EVENT"),
        ("procedure_cvc", "access_code", "new_access"),
    ],
)
def test_unknown_enum_code_blocks_review(tmp_path, monkeypatch, table, field, value):
    base, local, remote = _paths(tmp_path)
    plan = RowMergePlan(operations=[RowOperation(
        table=table, action="insert", pk_columns=("id",), pk_values=(1,),
        row={"id": 1, "admission_id": 12, field: value}, admission_id=12, patient_id=7,
    )])
    monkeypatch.setattr(emergency_review, "build_row_merge_plan", lambda **_kwargs: plan)

    review = emergency_review.build_emergency_review(
        base_db_path=str(base), local_db_path=str(local), remote_db_path=str(remote)
    )

    assert review["blockers"][0]["code"] == "review_description_unsupported"
    assert review["patients"][0]["operation_count"] == 0


def test_unknown_nested_clinical_key_blocks_review(tmp_path, monkeypatch):
    base, local, remote = _paths(tmp_path)
    plan = RowMergePlan(operations=[RowOperation(
        table="respiratory_support", action="insert", pk_columns=("id",), pk_values=(1,),
        row={
            "id": 1, "admission_id": 12, "datetime": "2026-09-10T08:00:00",
            "parameters_json": json.dumps({"RR": 16, "new_pressure": 20}),
        }, admission_id=12, patient_id=7,
    )])
    monkeypatch.setattr(emergency_review, "build_row_merge_plan", lambda **_kwargs: plan)

    review = emergency_review.build_emergency_review(
        base_db_path=str(base), local_db_path=str(local), remote_db_path=str(remote)
    )

    assert review["blockers"][0]["code"] == "review_description_unsupported"


def test_review_selection_rebuilds_exact_subset_and_returns_its_digest(tmp_path, monkeypatch):
    base, local, remote = _paths(tmp_path)
    full = _review_plan()
    selected_plan = RowMergePlan(operations=full.operations[:1])
    calls = []

    def fake_builder(**kwargs):
        calls.append(kwargs["selected_admission_ids"])
        return full if kwargs["selected_admission_ids"] is None else selected_plan

    monkeypatch.setattr(emergency_review, "build_row_merge_plan", fake_builder)
    review = {
        "patients": [{"admission_id": 12}],
        "plan_digest": plan_digest(full),
        "session_id": "session-1",
        "_source_paths": {"base": str(base), "local": str(local), "remote": str(remote)},
    }

    authorization = emergency_review.review_selection(review, [12])

    assert calls == [None, [12]]
    assert authorization["ok"] is True
    assert authorization["selected_admission_ids"] == [12]
    assert authorization["plan_digest"] == plan_digest(selected_plan)
    assert authorization["operation_count"] == 1
    assert authorization["session_id"] == "session-1"
    assert authorization["approved_at"]


def test_review_selection_rejects_stale_preview(tmp_path, monkeypatch):
    base, local, remote = _paths(tmp_path)
    changed = _review_plan()
    review = {
        "patients": [{"admission_id": 12}],
        "plan_digest": "old-digest",
        "_source_paths": {"base": str(base), "local": str(local), "remote": str(remote)},
    }
    monkeypatch.setattr(emergency_review, "build_row_merge_plan", lambda **_kwargs: changed)

    authorization = emergency_review.review_selection(review, [12])

    assert authorization["ok"] is False
    assert authorization["plan_digest"] == ""
    assert authorization["blockers"] == [{
        "code": "review_stale",
        "message": "Данные изменились после открытия окна. Обновите просмотр перед подтверждением.",
    }]


def test_review_selection_does_not_authorize_a_preview_format_blocker(tmp_path, monkeypatch):
    base, local, remote = _paths(tmp_path)
    full = _review_plan()
    blocker = {"code": "review_description_unsupported", "message": "Нельзя описать изменение."}
    review = {
        "patients": [{"admission_id": 12}],
        "plan_digest": plan_digest(full),
        "blockers": [blocker],
        "_source_paths": {"base": str(base), "local": str(local), "remote": str(remote)},
    }
    monkeypatch.setattr(emergency_review, "build_row_merge_plan", lambda **_kwargs: full)

    authorization = emergency_review.review_selection(review, [12])

    assert authorization["ok"] is False
    assert authorization["plan_digest"] == ""
    assert authorization["blockers"] == [blocker]


def test_review_selection_rejects_unknown_admission(tmp_path):
    base, local, remote = _paths(tmp_path)
    review = {
        "patients": [{"admission_id": 12}],
        "_source_paths": {"base": str(base), "local": str(local), "remote": str(remote)},
    }
    with pytest.raises(ValueError, match="нет в предварительном просмотре"):
        emergency_review.review_selection(review, [99])


def test_unified_database_preview_and_selection_use_the_same_real_plan(tmp_path):
    base, local, remote = (tmp_path / name for name in ("base-full.db", "local-full.db", "remote-full.db"))
    conn = sqlite3.connect(base)
    ensure_unified_schema(conn)
    conn.execute(
        "INSERT INTO patients(id, full_name, last_name, first_name) VALUES(1, 'Иванов Иван', 'Иванов', 'Иван')"
    )
    conn.execute(
        "INSERT INTO admissions(id, patient_id, bed_number, history_number, admission_datetime) "
        "VALUES(10, 1, 1, '456', '2026-09-09 11:30')"
    )
    conn.execute("INSERT INTO beds(bed_number, status, current_admission_id) VALUES(1, 'occupied', 10)")
    conn.execute(
        "INSERT INTO vitals(id, admission_id, datetime, sys, dia, pulse) "
        "VALUES(100, 10, '2026-09-10 07:30', 120, 80, 70)"
    )
    conn.commit()
    conn.close()
    shutil.copyfile(base, local)
    shutil.copyfile(base, remote)
    conn = sqlite3.connect(local)
    conn.execute("UPDATE vitals SET sys=130, dia=85, pulse=76 WHERE id=100")
    conn.commit()
    conn.close()

    review = emergency_review.build_emergency_review(
        base_db_path=str(base), local_db_path=str(local), remote_db_path=str(remote)
    )
    authorization = emergency_review.review_selection(review, [10])

    assert review["summary"]["operation_count"] == 1
    assert review["patients"][0]["sections"][0]["key"] == "vitals"
    assert authorization["ok"] is True
    assert authorization["operation_count"] == 1
    assert len(authorization["plan_digest"]) == 64

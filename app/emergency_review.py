from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Iterable, Mapping

from rem_card.app.emergency_row_level_merge import (
    REMCARD_MERGE_TABLE_ORDER,
    RowMergePlan,
    RowOperation,
    build_row_merge_plan,
    plan_digest as merge_plan_digest,
)
from rem_card.app.sqlite_uri import build_sqlite_file_uri
from rem_card.app.sqlite_shared import configure_connection
from rem_card.data.dto.lab_orders_dto import LAB_MATERIAL_LABELS, LAB_ORDER_STATUS_LABELS
from rem_card.data.dto.procedures_dto import PROCEDURE_STATUS_LABELS, PROCEDURE_TYPE_LABELS
from rem_card.services.procedures_print_service import (
    CVC_ACCESS_LABELS,
    CVC_ANESTHESIA_LABELS,
    CVC_CONFIRMATION_LABELS,
    CVC_FIXATION_LABELS,
    CVC_INDICATION_LABELS,
    CVC_METHOD_LABELS,
    CVC_PLACE_LABELS,
    CVC_ULTRASOUND_LABELS,
    LP_ACCESS_LABELS,
    LP_INDICATION_LABELS,
    LP_LEVEL_LABELS,
    LP_RESULT_LABELS,
    TRANSFUSION_ALLOIMMUNE_LABELS,
    TRANSFUSION_INDICATION_LABELS,
    TRANSFUSION_WAS_NOT_WAS,
    TRANSFUSION_WERE_NOT_WERE,
    TRANSFUSION_YES_NO,
)


EMERGENCY_REVIEW_SCHEMA_VERSION = 1

_SECTION_TABLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("vitals", "Показатели состояния", ("vitals", "vital_settings")),
    ("orders", "Назначения и выполнения", ("orders", "administrations", "order_audit_log", "drugs")),
    ("admission", "Пациент и госпитализация", ("patients", "admissions", "beds", "patient_status_events")),
    ("balance", "Жидкости и питание", ("fluids", "diet_plan", "diet_plan_versions", "diet_templates", "oral_intake_events")),
    ("respiratory", "Дыхательная поддержка", ("ivl_episodes", "respiratory_support")),
    ("laboratory", "Анализы", ("lab_data", "lab_orders")),
    (
        "procedures",
        "Операции и процедуры",
        (
            "operations", "procedures", "procedure_consents", "procedure_cvc",
            "procedure_lumbar_puncture", "procedure_transfusion", "transfusions",
        ),
    ),
    ("other", "Другие клинические записи", ("clinical_events", "devices")),
)

_TABLE_TITLES = {
    "patients": "Данные пациента", "admissions": "Госпитализация", "beds": "Койка",
    "operations": "Операция", "ivl_episodes": "Эпизод ИВЛ", "transfusions": "Трансфузия",
    "clinical_events": "Клиническое событие", "devices": "Устройство",
    "respiratory_support": "Параметры дыхательной поддержки", "lab_data": "Лабораторные показатели",
    "drugs": "Справочник препарата", "vitals": "Показатели состояния",
    "vital_settings": "Набор показателей", "fluids": "Баланс жидкости", "orders": "Назначение",
    "administrations": "Выполнение назначения", "patient_status_events": "Статус пациента",
    "order_audit_log": "Изменение назначения", "diet_templates": "Шаблон питания",
    "diet_plan": "План питания", "diet_plan_versions": "Версия плана питания",
    "oral_intake_events": "Приём питания", "procedures": "Процедура",
    "lab_orders": "Назначенный анализ", "procedure_consents": "Согласие на процедуру",
    "procedure_cvc": "Центральный венозный доступ",
    "procedure_lumbar_puncture": "Люмбальная пункция",
    "procedure_transfusion": "Протокол трансфузии",
}

_HIDDEN_FIELDS = {
    "id", "patient_id", "admission_id", "current_admission_id", "order_id", "source_order_id",
    "source_admin_id", "procedure_id", "template_id", "ivl_episode_id", "event_id", "plan_version_id",
    "doctor_id", "performer_id", "admission_uid", "chain_id", "big_chain_id", "action_id", "card_day_id",
    "revision", "version", "sort_order", "draft_sort_order", "created_at", "created_at_db", "updated_at",
    "last_modified_by", "created_by", "updated_by", "created_by_role", "created_by_user",
    "completed_by_role", "completed_by_user", "source", "ui_color", "ui_color_until",
}

_NURSE_MARK_LABELS = {"nurse_executed": "выполнено", "nurse_not_executed": "не выполнено"}
_ADMINISTRATION_FIELD_LABELS = {
    "status": "Состояние записи в плане",
    "comment": "Отметка / комментарий медсестры",
    "executed_time": "Время отметки",
}

# Labels cover the clinical allow-list.  An unexpected meaningful field becomes a
# blocker instead of disappearing from the physician's review.
_FIELD_LABELS = {
    "full_name": "ФИО", "birth_date": "Дата рождения", "last_name": "Фамилия", "first_name": "Имя",
    "middle_name": "Отчество", "bed_number": "Койка", "status": "Статус", "history_number": "№ истории",
    "admission_datetime": "Поступление", "patient_age": "Возраст", "patient_months": "Возраст в месяцах",
    "patient_age_unit": "Единица возраста", "patient_gender": "Пол", "diagnosis_code": "Код диагноза",
    "diagnosis_text": "Диагноз", "department_profile": "Профиль отделения",
    "source_department": "Откуда поступил", "transfer_datetime": "Перевод", "transfer_department": "Куда переведён",
    "outcome": "Исход", "transfer_lpu": "Медицинская организация перевода",
    "transfer_lpu_other": "Другая организация перевода", "death_datetime": "Дата смерти",
    "operation_description": "Операция", "operation_description_2": "Вторая операция", "is_active": "Активная госпитализация",
    "intake_extra_json": "Дополнительные данные поступления", "clinical_death_datetime": "Клиническая смерть",
    "cardiac_arrest_cause": "Причина остановки кровообращения", "cardiac_arrest_measures_json": "Реанимационные мероприятия",
    "emergency_notice_number": "Номер экстренного извещения", "emergency_notice_entered_at": "Извещение внесено",
    "recovery_bed_stay": "Нахождение на койке пробуждения", "operation_number": "Номер операции",
    "description": "Описание", "operation_datetime": "Время операции", "episode_number": "Номер эпизода",
    "start_time": "Начало", "end_time": "Окончание", "type": "Тип", "start_type": "Вид начала",
    "delivery_type": "Способ проведения", "volume_ml": "Объём", "datetime": "Время", "timestamp": "Время",
    "event_type": "Тип события", "author": "Автор", "data": "Подробности", "mode": "Режим",
    "parameters_json": "Параметры", "extubation_reason": "Причина экстубации", "o2_flow": "Поток кислорода",
    "device_type": "Устройство", "insertion_date": "Установлено", "removal_date": "Удалено",
    "replacement_time": "Заменено", "location": "Расположение", "fio2": "Кислород во вдыхаемой смеси",
    "peep": "ПДКВ", "tv": "Дыхательный объём", "rr": "Частота дыхания", "platelets": "Тромбоциты",
    "bilirubin": "Билирубин", "creatinine": "Креатинин", "lactate": "Лактат", "pao2": "PaO₂",
    "code": "Код", "name": "Название", "template": "Шаблон", "sys": "Систолическое АД",
    "dia": "Диастолическое АД", "pulse": "Пульс", "temp": "Температура", "spo2": "SpO₂", "gcs": "ШКГ",
    "cvp": "ЦВД", "date": "Дата", "ad": "Показывать АД", "iv_input": "Внутривенно",
    "oral_input": "Через рот", "food": "Питание", "urine": "Диурез", "ng_output": "По зонду",
    "drain_output": "По дренажам", "stool": "Стул", "other_output": "Другие потери", "text": "Назначение",
    "drug_key": "Препарат", "latin": "Латинское название", "dose_value": "Доза", "dose_unit": "Единица дозы",
    "is_per_kg": "На килограмм", "frequency": "Кратность", "specific_times": "Время выполнения",
    "rate_ml_h": "Скорость", "volume_total": "Общий объём", "duration_min": "Длительность",
    "is_finalized": "Назначение завершено", "is_committed": "Подтверждено", "comment": "Комментарий",
    "cell_role": "Этап выполнения", "planned_time": "Запланировано", "actual_time": "Выполнено",
    "dose_given": "Введённая доза", "reason_type": "Вид причины", "reason_text": "Причина",
    "action_type": "Действие", "payload": "Подробности", "is_undone": "Отменено", "diet_text": "Рацион",
    "schedule_json": "Расписание", "details_json": "Состав рациона", "is_default": "Шаблон по умолчанию",
    "shift_start": "Начало суток", "effective_from": "Действует с", "diet_name": "Название рациона",
    "change_note": "Причина изменения", "event_time": "Время", "amount_ml": "Объём",
    "planned_item_key": "Пункт плана", "entry_kind": "Вид записи", "meal_name": "Приём пищи", "note": "Примечание",
    "procedure_type": "Вид процедуры", "started_at": "Начало", "finished_at": "Окончание",
    "duration_minutes": "Длительность", "doctor_name_snapshot": "Врач", "department_snapshot": "Отделение",
    "patient_snapshot_json": "Данные пациента", "diagnosis_snapshot": "Диагноз", "notes": "Примечания",
    "is_deleted": "Удалена", "protocol_printed_at": "Протокол напечатан", "analysis_code": "Код анализа",
    "analysis_name": "Анализ", "material": "Материал", "scheduled_at": "Назначено на", "completed_at": "Выполнено",
    "consent_kind": "Вид согласия", "consent_mode": "Кто дал согласие", "patient_signed": "Подписано пациентом",
    "representative_name": "Представитель", "representative_details": "Данные представителя",
    "consilium_json": "Консилиум", "emergency_reason": "Причина экстренности", "printed_at": "Напечатано",
    "cvc_code_main_selected": "Основной код ЦВК", "cvc_code_tunneled_selected": "Туннелируемый катетер",
    "indications_json": "Показания", "procedure_place_code": "Место проведения", "procedure_place_other": "Другое место",
    "anesthesia_code": "Анестезия", "anesthesia_other": "Другая анестезия", "access_code": "Доступ",
    "access_other": "Другой доступ", "method_code": "Метод", "method_other": "Другой метод",
    "ultrasound_control_json": "УЗ-контроль", "attempts_count": "Число попыток", "diameter_f": "Диаметр",
    "length_cm": "Длина", "lumens_count": "Число просветов", "fixation_json": "Фиксация",
    "fixation_other": "Другая фиксация", "position_confirmed_at": "Положение подтверждено",
    "position_confirmation_json": "Подтверждение положения", "technical_difficulty_code": "Техническая сложность",
    "technical_difficulty_description": "Описание сложности", "actions_taken": "Принятые меры",
    "catheter_status": "Состояние катетера", "removed_or_replaced": "Удаление или замена", "removed_at": "Удалено",
    "usage_complications_code": "Осложнение", "usage_complications_description": "Описание осложнения",
    "additional_treatment": "Дополнительное лечение", "operator_doctor_name": "Врач-оператор",
    "removal_doctor_name": "Врач при удалении", "level_code": "Уровень пункции", "level_other": "Другой уровень",
    "result_code": "Результат", "csf_characteristics": "Характеристика ликвора", "result_notes": "Описание результата",
    "request_at": "Заявка", "indication_code": "Показание", "recipient_abo": "Группа крови пациента",
    "recipient_rh": "Резус-фактор пациента", "recipient_antigens": "Антигены пациента",
    "alloimmune_antibodies": "Аллоиммунные антитела", "transfusions_history": "Трансфузии в анамнезе",
    "reactions_history": "Реакции в анамнезе", "reactions_history_details": "Описание реакций",
    "individual_selection_history": "Индивидуальный подбор", "donor_component_name": "Компонент крови",
    "procurement_org": "Организация заготовки", "donor_abo": "Группа крови донора", "donor_rh": "Резус-фактор донора",
    "donor_antigens": "Антигены донора", "donor_code": "Код донора", "unit_number": "Номер единицы",
    "collection_date": "Дата заготовки", "expiration_date": "Срок годности", "selection_medical_org": "Место подбора",
    "selection_study_date": "Дата подбора", "selection_responsible_name": "Ответственный за подбор",
    "selection_conclusion": "Заключение", "reagent_anti_a_series": "Серия анти-A",
    "reagent_anti_a_expiration": "Срок анти-A", "reagent_anti_b_series": "Серия анти-B",
    "reagent_anti_b_expiration": "Срок анти-B", "reagent_anti_d_series": "Серия анти-D",
    "reagent_anti_d_expiration": "Срок анти-D", "plane_compatibility": "Проба на совместимость",
    "biological_test": "Биологическая проба", "reaction_symptoms": "Симптомы реакции",
    "reaction_severity": "Тяжесть реакции", "observation_json": "Наблюдение",
}

_UNIT_BY_FIELD = {
    "sys": "мм рт. ст.", "dia": "мм рт. ст.", "pulse": "уд/мин", "temp": "°C", "spo2": "%",
    "rr": "в мин", "cvp": "см вод. ст.", "peep": "см вод. ст.", "tv": "мл", "fio2": "%",
    "o2_flow": "л/мин", "volume_ml": "мл", "amount_ml": "мл", "iv_input": "мл", "oral_input": "мл",
    "food": "мл", "urine": "мл", "ng_output": "мл", "drain_output": "мл", "stool": "мл",
    "other_output": "мл", "rate_ml_h": "мл/ч", "volume_total": "мл", "duration_min": "мин",
    "duration_minutes": "мин", "dose_given": "", "length_cm": "см", "diameter_f": "Fr",
}

_BOOLEAN_FIELDS = {
    "is_active", "is_finalized", "is_committed", "is_undone", "is_default", "is_per_kg", "is_deleted",
    "patient_signed", "recovery_bed_stay", "cvc_code_main_selected", "cvc_code_tunneled_selected", "ad",
}

_JSON_KEY_LABELS = {
    "name": "название", "time": "время", "times": "время", "value": "значение", "unit": "единица",
    "text": "текст", "note": "примечание", "comment": "комментарий", "enabled": "включено",
    "selected": "выбрано", "type": "тип", "code": "код", "dose": "доза", "volume": "объём",
    "amount": "количество", "duration": "длительность", "method": "метод", "location": "место",
}

_STRUCTURED_KEY_LABELS = {
    "parameters_json": {
        "RR": "Частота дыхания", "TV": "Дыхательный объём", "Pinsp": "Давление вдоха",
        "PEEP": "ПДКВ", "FiO2": "Кислород во вдыхаемой смеси", "PS": "Поддержка давлением",
        "Phigh": "Высокое давление", "Plow": "Низкое давление",
        "Thigh": "Время высокого давления", "Tlow": "Время низкого давления",
    },
    "data": {
        "RR": "Частота дыхания", "TV": "Дыхательный объём", "Pinsp": "Давление вдоха",
        "PEEP": "ПДКВ", "FiO2": "Кислород во вдыхаемой смеси", "PS": "Поддержка давлением",
        "Phigh": "Высокое давление", "Plow": "Низкое давление",
        "Thigh": "Время высокого давления", "Tlow": "Время низкого давления",
    },
    "intake_extra_json": {
        "table_code": "Операционный стол", "operation_finished_at": "Операция завершена",
        "transfer_department": "Отделение перевода", "operation_name": "Операция",
        "anesthesia_assistance_type": "Анестезиологическое пособие", "surgeons": "Хирурги",
        "anesthesiologist": "Анестезиолог", "anesthetist": "Медсестра-анестезист",
        "height_cm": "Рост", "weight_kg": "Масса тела", "allergies": "Аллергологический анамнез",
        "blood_group": "Группа крови", "blood_rh": "Резус-фактор",
    },
    "patient_snapshot_json": {
        "admission_datetime": "Поступление", "full_name": "ФИО", "sex": "Пол", "age": "Возраст",
        "age_months": "Возраст в месяцах", "age_unit": "Единица возраста", "birth_date": "Дата рождения",
        "history_number": "№ истории", "department": "Отделение", "bed_number": "Койка",
        "diagnosis": "Диагноз", "diagnosis_code": "Код диагноза",
    },
    "cardiac_arrest_measures_json": {
        "outcome_type": "Исход", "clinical_death_datetime": "Клиническая смерть",
        "recovery_datetime": "Восстановление кровообращения",
        "biological_death_datetime": "Биологическая смерть", "cardiac_arrest_cause": "Причина остановки сердца",
        "measures": "Реанимационные мероприятия", "comment": "Комментарий",
        "cpr_duration_minutes": "Длительность СЛР", "doctor": "Врач", "death_protocol": "Протокол смерти",
        "position": "Должность", "workplace": "Место работы", "patient": "Пациент", "gender": "Пол",
        "age": "Возраст", "history_number": "№ истории", "other": "Дополнительно",
        "cpr_stop_reason": "Причина прекращения СЛР", "biological_death_date": "Дата биологической смерти",
        "biological_death_time": "Время биологической смерти", "signature_doctor": "Врач, подписавший протокол",
        "name": "Мероприятие", "value": "Описание",
    },
    "observation_json": {
        "before": "Перед началом переливания", "hour1": "Через 1 час", "hour2": "Через 2 часа",
        "bp": "Артериальное давление", "pulse": "ЧСС", "temp": "Температура",
        "diuresis": "Диурез, цвет мочи",
    },
    "schedule_json": {
        "key": "Пункт плана", "meal": "Приём пищи", "name": "Приём пищи", "time": "Время",
        "amount": "Объём", "note": "Примечание",
    },
    "details_json": {
        "consistency": "Консистенция", "temperature": "Температура пищи", "salt_limit": "Ограничение соли",
        "fractional": "Дробное питание", "daily_fluid_ml": "Жидкость за сутки",
        "special_instructions": "Особые указания", "comment": "Комментарий", "no_food": "Без питания",
        "no_fluids": "Без жидкости", "on_demand": "По требованию",
    },
    "consilium_json": {
        "doctor_1": "Врач 1", "doctor_2": "Врач 2", "doctor_3": "Врач 3", "notes": "Примечание",
    },
    "indications_json": {"selected": "Выбранные показания", "other": "Другие показания"},
    "position_confirmation_json": {"selected": "Способы подтверждения", "comment": "Комментарий"},
}

_STRUCTURED_HIDDEN_KEYS = {
    "intake_extra_json": {"source", "operation_case_id", "source_patient_id", "source_admission_id"},
    "patient_snapshot_json": {"patient_id", "admission_id"},
    "schedule_json": {"key"},
}

_STRUCTURED_ROOT_TYPES = {
    "intake_extra_json": dict,
    "patient_snapshot_json": dict,
    "cardiac_arrest_measures_json": dict,
    "parameters_json": dict,
    "data": dict,
    "observation_json": dict,
    "schedule_json": list,
    "details_json": dict,
    "consilium_json": dict,
    "indications_json": dict,
    "ultrasound_control_json": list,
    "fixation_json": list,
    "position_confirmation_json": dict,
}

_STRUCTURED_UNITS = {
    ("intake_extra_json", "height_cm"): "см",
    ("intake_extra_json", "weight_kg"): "кг",
    ("cardiac_arrest_measures_json", "cpr_duration_minutes"): "мин",
    ("schedule_json", "amount"): "мл",
    ("details_json", "daily_fluid_ml"): "мл",
    ("parameters_json", "RR"): "в мин",
    ("parameters_json", "TV"): "мл",
    ("parameters_json", "Pinsp"): "см вод. ст.",
    ("parameters_json", "PEEP"): "см вод. ст.",
    ("parameters_json", "PS"): "см вод. ст.",
    ("parameters_json", "Phigh"): "см вод. ст.",
    ("parameters_json", "Plow"): "см вод. ст.",
    ("parameters_json", "Thigh"): "с",
    ("parameters_json", "Tlow"): "с",
    ("data", "RR"): "в мин",
    ("data", "TV"): "мл",
    ("data", "Pinsp"): "см вод. ст.",
    ("data", "PEEP"): "см вод. ст.",
    ("data", "PS"): "см вод. ст.",
    ("data", "Phigh"): "см вод. ст.",
    ("data", "Plow"): "см вод. ст.",
    ("data", "Thigh"): "с",
    ("data", "Tlow"): "с",
}

_STRUCTURED_VALUE_LABELS = {
    ("admissions", "cardiac_arrest_measures_json", "outcome_type"): {
        "biological_death": "биологическая смерть", "cpr_recovery": "восстановление кровообращения",
    },
    ("procedure_cvc", "indications_json", "selected"): CVC_INDICATION_LABELS,
    ("procedure_lumbar_puncture", "indications_json", "selected"): LP_INDICATION_LABELS,
    ("procedure_cvc", "ultrasound_control_json", ""): CVC_ULTRASOUND_LABELS,
    ("procedure_cvc", "fixation_json", ""): CVC_FIXATION_LABELS,
    ("procedure_cvc", "position_confirmation_json", "selected"): CVC_CONFIRMATION_LABELS,
}

_VENTILATION_MODE_LABELS = {
    "CONTROLLED_VCV": "Controlled VCV", "CONTROLLED_PCV": "Controlled PCV", "SIMV_VC": "SIMV VC",
    "SIMV_PC": "SIMV PC", "PSV": "PSV", "CPAP": "CPAP", "BIPAP": "BIPAP",
    "SPONTANEOUS": "самостоятельное дыхание",
}

_ENUM_LABELS_BY_FIELD = {
    ("procedures", "procedure_type"): PROCEDURE_TYPE_LABELS,
    ("procedures", "status"): PROCEDURE_STATUS_LABELS,
    ("procedure_consents", "consent_kind"): {
        "CVC_CONSENT": "согласие на ЦВК", "LUMBAR_PUNCTURE_CONSENT": "согласие на люмбальную пункцию",
        "TRANSFUSION_CONSENT": "согласие на гемотрансфузию",
    },
    ("procedure_consents", "consent_mode"): {
        "patient": "пациент", "representative": "законный представитель", "consilium": "консилиум",
        "emergency_doctor_decision": "решение врача по экстренным показаниям",
    },
    ("procedure_cvc", "procedure_place_code"): CVC_PLACE_LABELS,
    ("procedure_lumbar_puncture", "procedure_place_code"): CVC_PLACE_LABELS,
    ("procedure_cvc", "anesthesia_code"): CVC_ANESTHESIA_LABELS,
    ("procedure_lumbar_puncture", "anesthesia_code"): CVC_ANESTHESIA_LABELS,
    ("procedure_cvc", "access_code"): CVC_ACCESS_LABELS,
    ("procedure_lumbar_puncture", "access_code"): LP_ACCESS_LABELS,
    ("procedure_cvc", "method_code"): CVC_METHOD_LABELS,
    ("procedure_lumbar_puncture", "level_code"): LP_LEVEL_LABELS,
    ("procedure_lumbar_puncture", "result_code"): LP_RESULT_LABELS,
    ("procedure_cvc", "technical_difficulty_code"): {"none": "не выявлено", "complications": "сложности / осложнения"},
    ("procedure_lumbar_puncture", "technical_difficulty_code"): {"none": "не выявлено", "complications": "сложности / осложнения"},
    ("procedure_cvc", "catheter_status"): {
        "active": "установлен", "removed": "удалён", "replaced": "переустановлен",
        "transferred_with_catheter": "переведён с катетером", "dead_with_catheter": "умер с катетером",
    },
    ("procedure_cvc", "removed_or_replaced"): {"removed": "катетер удалён", "replaced": "катетер переустановлен"},
    ("procedure_cvc", "usage_complications_code"): {"none": "не отмечались", "present": "отмечались"},
    ("procedure_transfusion", "indication_code"): TRANSFUSION_INDICATION_LABELS,
    ("procedure_transfusion", "alloimmune_antibodies"): TRANSFUSION_ALLOIMMUNE_LABELS,
    ("procedure_transfusion", "transfusions_history"): TRANSFUSION_WERE_NOT_WERE,
    ("procedure_transfusion", "reactions_history"): TRANSFUSION_YES_NO,
    ("procedure_transfusion", "individual_selection_history"): TRANSFUSION_WAS_NOT_WAS,
    ("clinical_events", "event_type"): {
        "START_VENT": "старт ИВЛ", "MODE_CHANGE": "смена режима", "EXTUBATION": "экстубация",
        "TRACHEOSTOMY": "трахеостомия", "TUBE_REPLACEMENT": "замена трубки",
    },
    ("clinical_events", "mode"): _VENTILATION_MODE_LABELS,
    ("respiratory_support", "mode"): _VENTILATION_MODE_LABELS,
    ("ivl_episodes", "type"): {"delivery": "с поступления", "transfer": "начата в отделении"},
    ("ivl_episodes", "start_type"): {"ADMISSION": "с поступления", "IN_DEPARTMENT": "в отделении"},
    ("ivl_episodes", "delivery_type"): {
        "SELF": "самостоятельное дыхание", "AMBU": "мешок Амбу", "APPARATUS": "аппарат ИВЛ",
        "UNKNOWN": "не указан",
    },
    ("devices", "device_type"): {"ENDOTRACHEAL_TUBE": "эндотрахеальная трубка", "TRACHEOSTOMY_TUBE": "трахеостомическая трубка"},
    ("patient_status_events", "status"): {
        "ACTIVE": "в отделении", "OUT": "вне отделения", "OR": "операционная", "CPR": "СЛР",
        "TRANSFERRED": "переведён", "DEAD": "умер",
    },
    ("patient_status_events", "reason_type"): {"cpr": "СЛР", "outcome": "исход", "operblock": "операционный блок"},
    ("beds", "status"): {"FREE": "свободна", "OCCUPIED": "занята"},
    ("orders", "type"): {
        "medication": "лекарственный препарат", "infusion_cont": "непрерывная инфузия",
        "infusion_inter": "прерывистая инфузия", "procedure": "процедура", "observation": "наблюдение",
    },
    ("orders", "status"): {
        "active": "активно", "held": "приостановлено", "completed": "выполнено",
        "cancelled": "отменено", "deleted": "удалено",
    },
    ("administrations", "status"): {
        "planned": "запланировано", "done": "выполнено", "cancelled": "отменено", "deleted": "удалено",
    },
    ("administrations", "cell_role"): {
        "single": "однократное выполнение", "start": "начало", "body": "продолжение", "end": "окончание",
    },
    ("lab_orders", "status"): LAB_ORDER_STATUS_LABELS,
    ("lab_orders", "material"): LAB_MATERIAL_LABELS,
    ("oral_intake_events", "entry_kind"): {
        "planned": "по плану", "unplanned": "вне плана", "legacy": "ранее внесённая запись",
    },
}

_VALUE_TRANSLATIONS = {
    "active": "активно", "inactive": "неактивно", "completed": "выполнено", "cancelled": "отменено",
    "canceled": "отменено", "assigned": "назначено", "draft": "черновик", "planned": "запланировано",
    "done": "выполнено", "pending": "ожидает", "transfer": "перевод", "transferred": "переведён",
    "death": "смерть", "died": "умер", "discharged": "выписан", "discharge": "выписка",
    "male": "мужской", "female": "женский", "m": "мужской", "f": "женский",
    "patient": "пациент", "representative": "представитель", "doctor": "врач", "nurse": "медсестра",
    "yes": "да", "no": "нет", "unplanned": "вне плана", "journal": "журнал",
}


class _ReviewContext:
    def __init__(self, paths: Iterable[str]):
        self.connections: list[sqlite3.Connection] = []
        for path in paths:
            conn = sqlite3.connect(build_sqlite_file_uri(path, mode="ro"), uri=True, isolation_level=None, timeout=5.0)
            configure_connection(conn, readonly=True, profile="network")
            conn.row_factory = sqlite3.Row
            self.connections.append(conn)

    def close(self) -> None:
        for conn in self.connections:
            conn.close()

    def row(self, table: str, key: Any, *, column: str = "id") -> dict[str, Any]:
        if table not in REMCARD_MERGE_TABLE_ORDER or column not in {"id", "code", "bed_number"}:
            return {}
        for conn in self.connections:
            try:
                found = conn.execute(f'SELECT * FROM "{table}" WHERE "{column}" = ? LIMIT 1', (key,)).fetchone()
            except sqlite3.DatabaseError:
                continue
            if found is not None:
                return dict(found)
        return {}

    def admissions_for_patient(self, patient_id: int) -> list[int]:
        for conn in self.connections:
            try:
                rows = conn.execute("SELECT id FROM admissions WHERE patient_id = ?", (patient_id,)).fetchall()
            except sqlite3.DatabaseError:
                continue
            result = {int(row[0]) for row in rows if row[0] is not None}
            if result:
                return sorted(result)
        return []

    def drug_name(self, code: Any) -> str:
        row = self.row("drugs", code, column="code")
        return str(row.get("name") or code or "не указан")

    def order_name(self, order_id: Any) -> str:
        row = self.row("orders", order_id)
        return str(row.get("text") or self.drug_name(row.get("drug_key")) or "назначение")

    def order_description(self, order_id: Any) -> str:
        return _order_description(self.row("orders", order_id), self)

    def template_name(self, template_id: Any) -> str:
        return str(self.row("diet_templates", template_id).get("name") or "шаблон питания")


def build_emergency_review(*, base_db_path: str, local_db_path: str, remote_db_path: str) -> dict[str, Any]:
    """Build a read-only, JSON-serializable physician review."""
    paths = tuple(os.path.abspath(os.path.normpath(str(path))) for path in (base_db_path, local_db_path, remote_db_path))
    plan = build_row_merge_plan(
        base_db_path=paths[0], local_db_path=paths[1], remote_db_path=paths[2],
        selected_admission_ids=None, authoritative=True,
    )
    context = _ReviewContext((paths[1], paths[2], paths[0]))
    try:
        patients, format_blockers = _patients_from_plan(plan, context)
    finally:
        context.close()
    blockers = [_public_blocker(item) for item in plan.blockers]
    blockers.extend(format_blockers)
    return {
        "schema_version": EMERGENCY_REVIEW_SCHEMA_VERSION,
        "patients": patients,
        "blockers": blockers,
        "summary": {
            "patient_count": len(patients),
            "operation_count": len(plan.operations),
            "conflict_count": len(plan.conflicts),
        },
        "plan_digest": merge_plan_digest(plan),
        "_source_paths": {"base": paths[0], "local": paths[1], "remote": paths[2]},
    }


def review_selection(
    review: Mapping[str, Any], selected_admission_ids: Iterable[int], *,
    base_db_path: str | None = None, local_db_path: str | None = None, remote_db_path: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Rebuild and authorize exactly the selected admission scope."""
    known = {int(item["admission_id"]) for item in review.get("patients", [])}
    selected = sorted({int(value) for value in selected_admission_ids})
    unknown = [value for value in selected if value not in known]
    if unknown:
        raise ValueError("Выбрана госпитализация, которой нет в предварительном просмотре.")
    stored_paths = dict(review.get("_source_paths") or {})
    base = os.path.abspath(os.path.normpath(base_db_path or stored_paths.get("base") or ""))
    local = os.path.abspath(os.path.normpath(local_db_path or stored_paths.get("local") or ""))
    remote = os.path.abspath(os.path.normpath(remote_db_path or stored_paths.get("remote") or ""))
    if not all((base_db_path or stored_paths.get("base"), local_db_path or stored_paths.get("local"), remote_db_path or stored_paths.get("remote"))):
        raise ValueError("Не заданы пути к трём снимкам базы данных.")
    current_full_plan = build_row_merge_plan(
        base_db_path=base, local_db_path=local, remote_db_path=remote,
        selected_admission_ids=None, authoritative=True,
    )
    current_full_digest = merge_plan_digest(current_full_plan)
    if current_full_digest != str(review.get("plan_digest") or ""):
        return {
            "schema_version": EMERGENCY_REVIEW_SCHEMA_VERSION,
            "session_id": str(session_id if session_id is not None else review.get("session_id") or ""),
            "selected_admission_ids": selected,
            "plan_digest": "",
            "patient_count": len(selected),
            "operation_count": 0,
            "blockers": [{
                "code": "review_stale",
                "message": "Данные изменились после открытия окна. Обновите просмотр перед подтверждением.",
            }],
            "ok": False,
            "approved_at": "",
        }
    preview_blockers = [
        dict(item) if isinstance(item, Mapping) else {"code": "review_blocked", "message": str(item)}
        for item in (review.get("blockers") or [])
    ]
    if preview_blockers:
        return {
            "schema_version": EMERGENCY_REVIEW_SCHEMA_VERSION,
            "session_id": str(session_id if session_id is not None else review.get("session_id") or ""),
            "selected_admission_ids": selected,
            "plan_digest": "",
            "patient_count": len(selected),
            "operation_count": 0,
            "blockers": preview_blockers,
            "ok": False,
            "approved_at": "",
        }
    plan = build_row_merge_plan(
        base_db_path=base, local_db_path=local, remote_db_path=remote,
        selected_admission_ids=selected, authoritative=True,
    )
    blockers = [_public_blocker(item) for item in plan.blockers]
    authorization = {
        "schema_version": EMERGENCY_REVIEW_SCHEMA_VERSION,
        "session_id": str(session_id if session_id is not None else review.get("session_id") or ""),
        "selected_admission_ids": selected,
        "plan_digest": merge_plan_digest(plan) if not blockers else "",
        "patient_count": len(selected),
        "operation_count": len(plan.operations),
        "blockers": blockers,
        "ok": not blockers,
        "approved_at": datetime.now().astimezone().isoformat(timespec="seconds") if not blockers else "",
    }
    return authorization


def _patients_from_plan(plan: RowMergePlan, context: _ReviewContext) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    grouped: dict[int, list[RowOperation]] = {}
    blockers: list[dict[str, str]] = []
    for op in plan.operations:
        admission_ids = _operation_admission_ids(op, context)
        if op.table == "patients" and len(admission_ids) > 1:
            # Shared identity changes must be visible on each affected card.
            # The selected-plan validator requires choosing them together.
            for admission_id in admission_ids:
                grouped.setdefault(admission_id, []).append(op)
            continue
        if len(admission_ids) != 1:
            blockers.append({
                "code": "review_scope_unknown",
                "message": f"Невозможно однозначно отнести изменение «{_TABLE_TITLES.get(op.table, 'клиническая запись')}» к одной госпитализации.",
            })
            continue
        grouped.setdefault(admission_ids[0], []).append(op)

    patients: list[dict[str, Any]] = []
    for admission_id in sorted(grouped, key=lambda value: _admission_sort_key(value, context)):
        admission = context.row("admissions", admission_id)
        patient_id = _safe_int(admission.get("patient_id"))
        if patient_id is None:
            patient_id = next((op.patient_id for op in grouped[admission_id] if op.patient_id is not None), None)
        patient = context.row("patients", patient_id) if patient_id is not None else {}
        title = _patient_title(patient, admission, admission_id)
        section_items: dict[str, list[dict[str, Any]]] = {key: [] for key, _, _ in _SECTION_TABLES}
        for op in grouped[admission_id]:
            try:
                item = _format_operation(op, context)
            except _UnsupportedReviewData:
                blockers.append({
                    "code": "review_description_unsupported",
                    "message": f"Невозможно безопасно и полностью описать изменение «{_TABLE_TITLES.get(op.table, 'клиническая запись')}» для {title}.",
                })
                continue
            section_items[_section_for_table(op.table)].append(item)
        sections = [
            {"key": key, "title": title_text, "items": section_items[key]}
            for key, title_text, _ in _SECTION_TABLES if section_items[key]
        ]
        patients.append({
            "admission_id": admission_id, "patient_id": patient_id, "title": title,
            "selected_default": False, "sections": sections,
            "operation_count": sum(len(section["items"]) for section in sections),
        })
    return patients, _dedupe_blockers(blockers)


def _operation_admission_ids(op: RowOperation, context: _ReviewContext) -> list[int]:
    if op.admission_id is not None:
        return [int(op.admission_id)]
    row = op.row or op.remote_row or op.base_row
    direct = _safe_int(row.get("admission_id"))
    if direct is not None:
        return [direct]
    current = _safe_int(row.get("current_admission_id"))
    if current is not None:
        return [current]
    if op.table == "admissions":
        value = _safe_int(row.get("id"))
        return [value] if value is not None else []
    patient_id = op.patient_id if op.patient_id is not None else _safe_int(row.get("patient_id"))
    if patient_id is None and op.table == "patients":
        patient_id = _safe_int(row.get("id"))
    return context.admissions_for_patient(int(patient_id)) if patient_id is not None else []


def _format_operation(op: RowOperation, context: _ReviewContext) -> dict[str, Any]:
    before = dict(op.remote_row or {})
    after = dict(op.row or {})
    if op.action == "insert":
        kind, prefix, source = "addition", "Добавлено", after
    elif op.action == "delete":
        kind, prefix, source = "deletion", "Удалено", before
    else:
        kind, prefix, source = "change", "Изменено", after
    table_title = _TABLE_TITLES.get(op.table)
    if table_title is None:
        raise _UnsupportedReviewData
    time_text = _operation_time(source or before)
    heading = _record_heading(op.table, table_title, source or before, context)
    heading += f" · {time_text}" if time_text else ""

    fields = _changed_fields(op.action, before, after)
    lines: list[str] = []
    if op.table == "vitals":
        fields, pressure_line = _combine_pressure(op.action, fields, before, after)
        if pressure_line:
            lines.append(pressure_line)
    for field in fields:
        if field in _HIDDEN_FIELDS:
            continue
        label = _FIELD_LABELS.get(field)
        if op.table == "administrations":
            label = _ADMINISTRATION_FIELD_LABELS.get(field, label)
        if label is None:
            raise _UnsupportedReviewData
        if op.action == "update":
            left = _format_value(field, before.get(field), before, context, table=op.table)
            right = _format_value(field, after.get(field), after, context, table=op.table)
            lines.append(f"{label}: {left} → {right}")
        else:
            value = _format_value(field, source.get(field), source, context, table=op.table)
            lines.append(f"{label}: {value}")
    if not lines:
        lines.append("Запись целиком")
    text = f"{prefix}: {heading}. " + "; ".join(lines)
    return {"kind": kind, "title": heading, "lines": lines, "text": text, "conflict": bool(op.conflicts)}


def _changed_fields(action: str, before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    if action == "update":
        names = [name for name in after if before.get(name) != after.get(name)]
    else:
        source = after if action == "insert" else before
        names = [name for name, value in source.items() if value not in (None, "")]
    return [name for name in names if name not in _HIDDEN_FIELDS]


def _combine_pressure(
    action: str, fields: list[str], before: Mapping[str, Any], after: Mapping[str, Any]
) -> tuple[list[str], str | None]:
    if "sys" not in fields and "dia" not in fields:
        return fields, None
    remaining = [field for field in fields if field not in {"sys", "dia"}]
    source = after if action == "insert" else before
    current = f"{_plain_number(source.get('sys'))}/{_plain_number(source.get('dia'))} мм рт. ст."
    if action != "update":
        return remaining, f"АД: {current}"
    result = f"{_plain_number(after.get('sys'))}/{_plain_number(after.get('dia'))} мм рт. ст."
    return remaining, f"АД: {current} → {result}"


def _format_value(
    field: str, value: Any, row: Mapping[str, Any], context: _ReviewContext, *, table: str = ""
) -> str:
    if value is None or value == "":
        return "не указано"
    if table == "administrations" and field == "comment" and str(value) in _NURSE_MARK_LABELS:
        return _NURSE_MARK_LABELS[str(value)]
    if field == "drug_key":
        return context.drug_name(value)
    if field == "dose_value":
        unit = str(row.get("dose_unit") or "").strip()
        suffix = "/кг" if row.get("is_per_kg") and "/кг" not in unit.lower() else ""
        return " ".join(part for part in (_plain_number(value), f"{unit}{suffix}".strip()) if part)
    if field == "dose_given":
        unit = str(row.get("dose_unit") or "").strip()
        return " ".join(part for part in (_plain_number(value), unit) if part)
    if field in _BOOLEAN_FIELDS or (table == "vital_settings" and field in {"pulse", "temp", "spo2", "rr", "cvp"}):
        return "да" if bool(value) else "нет"
    enum_labels = _ENUM_LABELS_BY_FIELD.get((table, field))
    if enum_labels is not None:
        return _strict_label(value, enum_labels)
    if field.endswith("_json") or field in {"data", "payload", "template"}:
        return _format_structured(value, field=field, table=table)
    if field == "specific_times":
        parsed = _try_json(value)
        if isinstance(parsed, list):
            return ", ".join(str(item) for item in parsed) or "не указано"
    if field == "frequency":
        return f"{_plain_number(value)} раз/сут"
    translated = _VALUE_TRANSLATIONS.get(str(value).strip().lower())
    if translated is not None:
        return translated
    unit = _UNIT_BY_FIELD.get(field, "")
    rendered = _plain_number(value)
    return f"{rendered} {unit}".strip()


def _format_structured(value: Any, *, field: str, table: str) -> str:
    parsed = _try_json(value)
    if parsed is None:
        if field in {"payload", "template"} and isinstance(value, str) and value.strip():
            return value.strip()
        raise _UnsupportedReviewData
    expected_type = _STRUCTURED_ROOT_TYPES.get(field)
    if expected_type is not None and not isinstance(parsed, expected_type):
        raise _UnsupportedReviewData
    return _format_structured_value(parsed, field=field, table=table, path=())


def _record_heading(table: str, fallback: str, row: Mapping[str, Any], context: _ReviewContext) -> str:
    if table == "orders":
        return f"Назначение «{_order_description(row, context)}»"
    if table == "administrations":
        return f"Выполнение «{context.order_description(row.get('order_id'))}»"
    if table == "lab_orders" and row.get("analysis_name"):
        return f"Анализ «{row['analysis_name']}»"
    if table in {"operations", "procedures"}:
        description = row.get("description") or row.get("procedure_type")
        if description:
            if table == "procedures" and row.get("procedure_type"):
                description = _strict_label(row["procedure_type"], PROCEDURE_TYPE_LABELS)
            return f"{fallback} «{description}»"
    if table in {"diet_plan", "diet_plan_versions"}:
        diet = row.get("diet_name") or row.get("diet_text")
        if diet:
            return f"{fallback} «{diet}»"
    if table == "oral_intake_events" and row.get("meal_name"):
        return f"Приём питания «{row['meal_name']}»"
    if table in {"transfusions", "procedure_transfusion"}:
        component = row.get("donor_component_name") or row.get("type")
        if component:
            return f"{fallback} «{component}»"
    return fallback


def _order_description(row: Mapping[str, Any], context: _ReviewContext) -> str:
    name = str(row.get("text") or context.drug_name(row.get("drug_key")) or "назначение").strip()
    dose = row.get("dose_value")
    if dose in (None, ""):
        return name
    rendered_dose = _format_value("dose_value", dose, row, context, table="orders")
    return f"{name}, {rendered_dose}"


def _format_structured_value(value: Any, *, field: str, table: str, path: tuple[str, ...]) -> str:
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            key_text = str(key)
            if key_text in _STRUCTURED_HIDDEN_KEYS.get(field, set()):
                continue
            label = _structured_key_label(field, key_text)
            if label is None:
                raise _UnsupportedReviewData
            rendered = _format_structured_value(item, field=field, table=table, path=(*path, key_text))
            parts.append(f"{label}: {rendered}")
        return "; ".join(parts) or "служебные данные не отображаются"
    if isinstance(value, list):
        return ", ".join(
            _format_structured_value(item, field=field, table=table, path=path) for item in value
        ) or "нет"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if value is None:
        return "не указано"
    value_labels = _STRUCTURED_VALUE_LABELS.get((table, field, path[-1] if path else ""))
    if value_labels is not None:
        return _strict_label(value, value_labels)
    translated = _VALUE_TRANSLATIONS.get(str(value).strip().lower())
    if translated is not None:
        return translated
    if path and path[-1].endswith(("_datetime", "_at")):
        return _display_datetime(value)
    rendered = _plain_number(value)
    if field in {"parameters_json", "data"} and path and path[-1] == "FiO2":
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise _UnsupportedReviewData from None
        if 0 < number <= 1:
            return f"{rendered} ({int(round(number * 100))} %)"
        if 1 < number <= 100:
            return f"{rendered} ({int(round(number))} %)"
        raise _UnsupportedReviewData
    unit = _STRUCTURED_UNITS.get((field, path[-1] if path else ""), "")
    return f"{rendered} {unit}".strip()


def _structured_key_label(field: str, key: str) -> str | None:
    configured = _STRUCTURED_KEY_LABELS.get(field)
    if configured is not None:
        return configured.get(key)
    if key in _HIDDEN_FIELDS:
        return None
    return _FIELD_LABELS.get(key) or _JSON_KEY_LABELS.get(key)


def _strict_label(value: Any, labels: Mapping[str, str]) -> str:
    text = str(value).strip()
    if text in labels:
        return str(labels[text])
    folded = text.casefold()
    for code, label in labels.items():
        if str(code).casefold() == folded:
            return str(label)
    raise _UnsupportedReviewData


def _try_json(value: Any) -> Any:
    if isinstance(value, (dict, list, bool, int, float)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _patient_title(patient: Mapping[str, Any], admission: Mapping[str, Any], admission_id: int) -> str:
    name = " ".join(str(patient.get(key) or "").strip() for key in ("last_name", "first_name", "middle_name")).strip()
    name = name or str(patient.get("full_name") or "Пациент без указанного ФИО").strip()
    history = str(admission.get("history_number") or "не указан")
    dates = [f"поступление {_display_datetime(admission.get('admission_datetime'))}"]
    if admission.get("transfer_datetime"):
        dates.append(f"перевод {_display_datetime(admission.get('transfer_datetime'))}")
    if admission.get("death_datetime"):
        dates.append(f"смерть {_display_datetime(admission.get('death_datetime'))}")
    return f"{name} · история № {history} · " + " · ".join(dates)


def _display_datetime(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "не указано"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return text


def _operation_time(row: Mapping[str, Any]) -> str:
    for field in ("datetime", "timestamp", "event_time", "planned_time", "actual_time", "scheduled_at", "start_time", "shift_start", "date"):
        if row.get(field):
            return _display_datetime(row[field])
    return ""


def _plain_number(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _section_for_table(table: str) -> str:
    for key, _, tables in _SECTION_TABLES:
        if table in tables:
            return key
    raise _UnsupportedReviewData


def _admission_sort_key(admission_id: int, context: _ReviewContext) -> tuple[str, int]:
    row = context.row("admissions", admission_id)
    return str(row.get("admission_datetime") or ""), admission_id


def _public_blocker(blocker: Mapping[str, Any]) -> dict[str, str]:
    code = str(blocker.get("code") or "merge_blocked")
    known = {
        "selected_admission_not_found": "Выбранная госпитализация отсутствует в одном из снимков базы.",
        "protected_common_row_update": "Изменение затрагивает защищённые данные операционного блока.",
        "protected_common_row_delete": "Удаление затрагивает защищённые данные операционного блока.",
        "local_foreign_key_violation": "В локальных данных нарушена связь между клиническими записями.",
        "remote_foreign_key_violation": "В сетевых данных нарушена связь между клиническими записями.",
        "authoritative_bed_conflict": "Выбранные локальные данные занимают койку, уже занятую другим пациентом.",
        "selected_scope_shared_bed": "Нельзя перенести только часть выбранных данных: одна койка связана с несколькими госпитализациями.",
        "selected_scope_shared_patient": "Нельзя перенести только часть выбранных данных: госпитализации относятся к одному пациенту.",
        "unknown_clinical_table_changed": "Обнаружены изменённые клинические данные, которые эта версия RemCard не умеет безопасно описать.",
    }
    return {"code": code, "message": known.get(code, "Объединение заблокировано: данные требуют ручной проверки.")}


def _dedupe_blockers(blockers: list[dict[str, str]]) -> list[dict[str, str]]:
    result = []
    seen: set[tuple[str, str]] = set()
    for blocker in blockers:
        key = (blocker["code"], blocker["message"])
        if key not in seen:
            seen.add(key)
            result.append(blocker)
    return result


def _safe_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class _UnsupportedReviewData(Exception):
    pass

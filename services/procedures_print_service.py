from __future__ import annotations

import re
import json
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from rem_card.app.paths import REPORT_DIR
from rem_card.data.dao.procedures_dao import ProceduresDAO
from rem_card.data.dto.procedures_dto import ProcedureBundle, PROCEDURE_TYPE_LABELS, ProcedureType
from rem_card.services.procedures_reportlab_builder import ProcedureReportLabBuilder


CVC_INDICATION_LABELS = {
    "infusion_need": "Потребность во введении инфузионных сред или лекарственных препаратов при невозможности обеспечения требуемого объема, скорости, эффективности и безопасности инфузионной и/или медикаментозной терапии за счет катетеризации периферических вен и/или внутрикостного введения",
    "cvp_monitoring": "Контроль центрального венозного давления и прочие виды гемодинамического мониторинга",
    "vasopressors": "Проведение вазопрессорной терапии",
    "renal_replacement": "Проведение заместительной почечной терапии",
    "extracorporeal_support": "Проведение экстракорпоральных методов поддержки кровообращения и дыхания",
    "pacing": "Эндокардиальная кардиостимуляция",
    "parenteral_nutrition": "Парентеральное питание",
    "hyperosmolar": "Введение гиперосмолярных и/или обладающих раздражающим действием на интиму кровеносных сосудов растворов",
    "catheter_replacement": "Замена катетера",
}

CVC_INDICATION_CODES = {
    "infusion_need": "1",
    "cvp_monitoring": "2",
    "vasopressors": "3",
    "renal_replacement": "4",
    "extracorporeal_support": "5",
    "pacing": "6",
    "parenteral_nutrition": "7",
    "hyperosmolar": "8",
    "catheter_replacement": "9",
}

CVC_PLACE_CODES = {
    "operating_room": "1",
    "icu_room": "2",
    "icu_block": "3",
    "other": "4",
}

CVC_PLACE_LABELS = {
    "operating_room": "Операционная",
    "icu_room": "Палата реанимации и интенсивной терапии",
    "icu_block": "Блок/палата интенсивной терапии",
    "other": "Прочее",
}

CVC_ANESTHESIA_CODES = {
    "local": "1",
    "general": "2",
    "other": "3",
}

CVC_ANESTHESIA_LABELS = {
    "local": "Местная",
    "general": "Общая",
    "other": "Прочее",
}

CVC_ACCESS_LABELS = {
    "ijv_right": "Внутренняя яремная вена правая",
    "ijv_left": "Внутренняя яремная вена левая",
    "subclavian_right": "Подключичная вена правая",
    "subclavian_left": "Подключичная вена левая",
    "femoral_right": "Бедренная вена правая",
    "femoral_left": "Бедренная вена левая",
    "other": "Прочие локализации",
}

CVC_ACCESS_CODES = {
    "ijv_right": "1",
    "ijv_left": "2",
    "subclavian_right": "3",
    "subclavian_left": "4",
    "femoral_right": "5",
    "femoral_left": "6",
    "other": "7",
}

CVC_METHOD_CODES = {
    "seldinger": "1",
    "other": "2",
}

CVC_METHOD_LABELS = {
    "seldinger": "По Сельдингеру",
    "other": "Прочие методы",
}

CVC_ULTRASOUND_CODES = {
    "marking": "1",
    "dynamic": "2",
    "unavailable": "3",
}

CVC_ULTRASOUND_LABELS = {
    "marking": "Разметка под УЗ-контролем",
    "dynamic": "Динамический УЗ-контроль",
    "unavailable": "УЗ-контроль не доступен и/или отсутствует опыт",
}

CVC_FIXATION_CODES = {
    "ligature": "лигатура",
    "plaster": "лейкопластырь",
    "sutureless": "бесшовное устройство",
}

CVC_FIXATION_LABELS = {
    "ligature": "Лигатурой",
    "plaster": "Лейкопластырем",
    "sutureless": "Бесшовным устройством",
}

CVC_CONFIRMATION_CODES = {
    "blood_return": "1",
    "xray": "2",
    "ultrasound": "3",
    "ct": "4",
}

CVC_CONFIRMATION_LABELS = {
    "blood_return": "Свободный обратный ток венозной крови",
    "xray": "Обзорная рентгенография",
    "ultrasound": "УЗ-контроль",
    "ct": "РКТ",
}

LP_INDICATION_LABELS = {
    "cns_infection": "Подозрение на инфекционное поражение ЦНС",
    "subarachnoid_hemorrhage": "Подозрение на субарахноидальное кровоизлияние",
    "cns_autoimmune": "Подозрение на воспалительные и аутоиммунные заболевания ЦНС",
    "pns_demyelinating": "Диагностика демиелинизирующих и воспалительных заболеваний периферической нервной системы",
    "cns_neoplastic": "Подозрение на неопластическое поражение ЦНС и мозговых оболочек",
    "csf_pressure": "Оценка ликворного давления и ликвородинамики",
    "metabolic_degenerative_prion": "Диагностика метаболических, дегенеративных и прионных заболеваний ЦНС",
    "paraneoplastic_autoantibody": "Подозрение на паранеопластическое или аутоантительное поражение нервной системы",
    "unclear_cns_lesion": "Уточнение характера поражения ЦНС неясной этиологии",
    "therapeutic": "Лечебные и процедурные показания",
}

LP_INDICATION_CODES = {
    "cns_infection": "1",
    "subarachnoid_hemorrhage": "2",
    "cns_autoimmune": "3",
    "pns_demyelinating": "4",
    "cns_neoplastic": "5",
    "csf_pressure": "6",
    "metabolic_degenerative_prion": "7",
    "paraneoplastic_autoantibody": "8",
    "unclear_cns_lesion": "9",
    "therapeutic": "10",
}

LP_ACCESS_LABELS = {
    "midline": "Срединный доступ",
    "paramedian": "Парамедианный доступ",
    "taylor": "Доступ Тейлора",
}

LP_ACCESS_CODES = {
    "midline": "1",
    "paramedian": "2",
    "taylor": "3",
}

LP_LEVEL_LABELS = {
    "L1-L2": "L1–L2",
    "L2-L3": "L2–L3",
    "L3-L4": "L3–L4",
    "L4-L5": "L4–L5",
    "L5-S1": "L5–S1",
}

LP_LEVEL_CODES = {
    "L1-L2": "1",
    "L2-L3": "2",
    "L3-L4": "3",
    "L4-L5": "4",
    "L5-S1": "5",
}

LP_RESULT_LABELS = {
    "csf_not_obtained": "Ликвор не получен",
    "csf_obtained": "Ликвор получен",
}

TRANSFUSION_INDICATION_LABELS = {
    "voce": "ВОЦЭ - восполнение объема циркулирующих эритроцитов",
    "vpfs": "ВПФС - восполнение факторов свертываемости крови",
}

TRANSFUSION_INDICATION_PRINT_LABELS = {
    "voce": "Восполнение объема циркулирующих эритроцитов",
    "vpfs": "Восполнение факторов свертываемости крови",
}

TRANSFUSION_SCENARIO_SUFFIX = {
    "voce": "ВОЦЭ",
    "vpfs": "ВПФС",
}

TRANSFUSION_ALLOIMMUNE_LABELS = {
    "negative": "отрицательные",
    "erythrocyte": "Эритроцитарные аллоантитела",
    "leukocyte": "Лейкоцитарные аллоантитела",
    "platelet": "Тромбоцитарные аллоантитела",
    "plasma_protein": "Антитела против плазменных белков",
    "other": "другие",
}

TRANSFUSION_YES_NO = {
    "no": "нет",
    "yes": "да",
}

TRANSFUSION_WERE_NOT_WERE = {
    "no": "не были",
    "yes": "были",
}

TRANSFUSION_WAS_NOT_WAS = {
    "no": "не было",
    "yes": "было",
}


class ProceduresPrintService:
    def __init__(self, dao: ProceduresDAO):
        self.dao = dao
        self.template_dir = Path(__file__).resolve().parents[1] / "procedure_templates"

    def render_document(self, procedure_id: int, document_kind: str) -> str:
        bundle = self.dao.get_bundle(int(procedure_id))
        if not bundle:
            raise ValueError("Процедура не найдена.")
        kind = str(document_kind or "").strip()
        if kind == "cvc_protocol":
            return self._render_cvc_protocol(bundle)
        if kind == "cvc_removal":
            return self._render_cvc_removal(bundle)
        if kind == "cvc_consent":
            return self._render_cvc_consent(bundle)
        if kind == "lp_protocol":
            return self._render_lp_protocol(bundle)
        if kind == "lp_consent":
            return self._render_lp_consent(bundle)
        if kind == "transfusion_protocol":
            return self._render_transfusion_protocol(bundle)
        if kind == "transfusion_consent":
            raise ValueError("Шаблон согласия на гемотрансфузию будет добавлен позже.")
        raise ValueError(f"Неизвестный тип печати процедуры: {kind}")

    def build_pdf(self, procedure_id: int, document_kind: str, pdf_path) -> Path:
        bundle = self.dao.get_bundle(int(procedure_id))
        if not bundle:
            raise ValueError("Процедура не найдена.")
        kind = str(document_kind or "").strip()
        if kind == "cvc_protocol":
            context = self._cvc_protocol_context(bundle)
        elif kind == "cvc_removal":
            context = self._cvc_protocol_context(bundle)
        elif kind == "cvc_consent":
            context = self._cvc_consent_context(bundle)
        elif kind == "lp_protocol":
            context = self._lp_protocol_context(bundle)
        elif kind == "lp_consent":
            context = self._lp_consent_context(bundle)
        elif kind == "transfusion_protocol":
            context = self._transfusion_protocol_context(bundle)
        elif kind == "transfusion_consent":
            raise ValueError("Шаблон согласия на гемотрансфузию будет добавлен позже.")
        else:
            raise ValueError(f"Неизвестный тип печати процедуры: {kind}")
        output_path = Path(pdf_path)
        ProcedureReportLabBuilder.build_pdf(kind, context, output_path)
        return output_path

    def build_pdf_path(self, procedure_id: int, document_kind: str) -> Path:
        procedure = self.dao.get_procedure(int(procedure_id))
        if not procedure:
            raise ValueError("Процедура не найдена.")
        try:
            snapshot = json.loads(procedure.patient_snapshot_json or "{}")
        except Exception:
            snapshot = {}
        patient_name = self._safe_filename(snapshot.get("full_name") or "patient")
        proc_label = PROCEDURE_TYPE_LABELS.get(procedure.procedure_type, procedure.procedure_type)
        if procedure.procedure_type == ProcedureType.TRANSFUSION.value:
            transfusion = self.dao.get_transfusion(int(procedure_id))
            suffix = TRANSFUSION_SCENARIO_SUFFIX.get(getattr(transfusion, "indication_code", "") if transfusion else "", "")
            if suffix:
                proc_label = f"{proc_label}_{suffix}"
        proc_safe = self._safe_filename(proc_label)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_dir = Path(REPORT_DIR) / "procedures"
        return report_dir / f"{patient_name}_{proc_safe}_{document_kind}_{procedure.id}_{stamp}.pdf"

    def _render_cvc_protocol(self, bundle: ProcedureBundle) -> str:
        template = self._load_template("cvc_protocol_v1.html")
        return self._fill(template, self._cvc_protocol_context(bundle))

    def _render_cvc_consent(self, bundle: ProcedureBundle) -> str:
        template = self._load_template("cvc_consent_v1.html")
        return self._fill(template, self._cvc_consent_context(bundle))

    def _render_lp_protocol(self, bundle: ProcedureBundle) -> str:
        context = self._lp_protocol_context(bundle)
        return (
            "<html><body>"
            f"<h1>Протокол люмбальной пункции</h1>"
            f"<p>Пациент: {context.get('patient_name', '')}</p>"
            f"<p>Показания: {context.get('indications_text', '')}</p>"
            f"<p>Доступ: {context.get('access_text', '')}; уровень: {context.get('level_text', '')}</p>"
            f"<p>Результат: {context.get('result_text', '')}</p>"
            "</body></html>"
        )

    def _render_lp_consent(self, bundle: ProcedureBundle) -> str:
        context = self._lp_consent_context(bundle)
        return (
            "<html><body>"
            f"<h1>Добровольное информированное согласие пациента на выполнение люмбальной пункции</h1>"
            f"<p>ИБ № {context.get('history_number', '')}</p>"
            f"<p>{context.get('patient_name', '')}</p>"
            "</body></html>"
        )

    def _render_cvc_removal(self, bundle: ProcedureBundle) -> str:
        context = self._cvc_protocol_context(bundle)
        return (
            "<html><body>"
            f"<p>1. Катетер удален / переустановлен: {context.get('removal_summary', '')}</p>"
            f"<p>2. Осложнения во время использования катетера: {context.get('usage_complications_text', '')}</p>"
            f"<p>3. Отмеченные осложнения в виде: {context.get('usage_complications_description', '')}. "
            f"Требовали дополнительных лечебных мероприятий: {context.get('additional_treatment', '')}</p>"
            f"<p>ФИО врача: {context.get('removal_doctor_or_operator', '')}. Подпись ____________________</p>"
            "</body></html>"
        )

    def _render_transfusion_protocol(self, bundle: ProcedureBundle) -> str:
        context = self._transfusion_protocol_context(bundle)
        return (
            "<html><body>"
            f"<h1>Протокол трансфузии</h1>"
            f"<p>Пациент: {context.get('patient_name', '')}</p>"
            f"<p>Показание: {context.get('indication_print', '')}</p>"
            f"<p>Компонент: {context.get('donor_component_name', '')}</p>"
            "</body></html>"
        )

    def _cvc_protocol_context(self, bundle: ProcedureBundle) -> dict[str, str]:
        procedure = bundle.procedure
        cvc = bundle.cvc
        if cvc is None:
            raise ValueError("Данные ЦВК не найдены.")
        context = self._base_context(bundle)
        context.update(
            {
                "procedure_date": self._format_date(procedure.started_at),
                "start_time": self._format_time(procedure.started_at),
                "finish_time": self._format_time(procedure.finished_at),
                "duration": self._plain(procedure.duration_minutes),
                "doctor": self._plain(procedure.doctor_name_snapshot),
                "notes": self._plain(procedure.notes),
                "procedure_code_mark": "A11.12.001.001" if cvc.cvc_code_tunneled_selected else "A11.12.001",
                "procedure_title": self._plain(
                    "катетеризация подключичной и других центральных вен с использованием туннельного катетера, A11.12.001.001"
                    if cvc.cvc_code_tunneled_selected
                    else "катетеризация подключичной и других центральных вен, А11.12.001"
                ),
                "procedure_code_text": "туннельный катетер" if cvc.cvc_code_tunneled_selected else "центральная вена",
                "indications_codes": self._codes(cvc.indications, CVC_INDICATION_CODES, "10" if cvc.indications_other else ""),
                "indications_text": self._list_labels(cvc.indications, CVC_INDICATION_LABELS, cvc.indications_other),
                "indications_other": self._plain(cvc.indications_other),
                "place_code": self._code(cvc.procedure_place_code, CVC_PLACE_CODES),
                "place_text": self._choice_label(cvc.procedure_place_code, CVC_PLACE_LABELS, cvc.procedure_place_other),
                "place_other": self._plain(cvc.procedure_place_other),
                "anesthesia_code": self._code(cvc.anesthesia_code, CVC_ANESTHESIA_CODES),
                "anesthesia_text": self._choice_label(cvc.anesthesia_code, CVC_ANESTHESIA_LABELS, cvc.anesthesia_other),
                "anesthesia_other": self._plain(cvc.anesthesia_other),
                "access_code": self._code(cvc.access_code, CVC_ACCESS_CODES),
                "access_text": self._choice_label(cvc.access_code, CVC_ACCESS_LABELS, cvc.access_other),
                "access_other": self._plain(cvc.access_other),
                "method_code": self._code(cvc.method_code, CVC_METHOD_CODES),
                "method_text": self._choice_label(cvc.method_code, CVC_METHOD_LABELS, cvc.method_other),
                "method_other": self._plain(cvc.method_other),
                "ultrasound_codes": self._codes(cvc.ultrasound_control, CVC_ULTRASOUND_CODES),
                "ultrasound_text": self._list_labels(cvc.ultrasound_control, CVC_ULTRASOUND_LABELS, ""),
                "attempts": self._format_number(cvc.attempts_count),
                "diameter": self._format_number(cvc.diameter_f),
                "length": self._format_number(cvc.length_cm),
                "lumens": self._format_number(cvc.lumens_count),
                "catheter_params_text": self._catheter_params_text(cvc),
                "fixation_codes": self._codes(cvc.fixation, CVC_FIXATION_CODES),
                "fixation_text": self._list_labels(cvc.fixation, CVC_FIXATION_LABELS, cvc.fixation_other),
                "fixation_other": self._plain(cvc.fixation_other),
                "confirmation_time": self._format_time(cvc.position_confirmed_at),
                "confirmation_codes": self._codes(cvc.position_confirmation, CVC_CONFIRMATION_CODES),
                "confirmation_text": self._list_labels(cvc.position_confirmation, CVC_CONFIRMATION_LABELS, ""),
                "confirmation_comment": self._plain(cvc.position_confirmation_comment),
                "difficulty_code": "2" if cvc.technical_difficulty_code == "complications" else "1",
                "difficulty_text": self._difficulty_text(cvc),
                "difficulty_description": self._plain(cvc.technical_difficulty_description),
                "actions_taken": self._plain(cvc.actions_taken),
                **self._removal_context(cvc),
            }
        )
        return context

    def _cvc_consent_context(self, bundle: ProcedureBundle) -> dict[str, str]:
        context = self._base_context(bundle)
        consent = bundle.consent
        consilium = self._consilium_dict(getattr(consent, "consilium_json", "") if consent else "")
        print_dt = datetime.now().replace(second=0, microsecond=0)
        created_at = getattr(consent, "created_at", None) if consent else None
        if not created_at:
            created_at = bundle.procedure.started_at
        context.update(
            {
                "doctor": self._plain(bundle.procedure.doctor_name_snapshot),
                "consent_mode": self._plain(getattr(consent, "consent_mode", "patient") if consent else "patient"),
                "representative_name": self._plain(getattr(consent, "representative_name", "") if consent else ""),
                "representative_details": self._plain(getattr(consent, "representative_details", "") if consent else ""),
                "emergency_reason": self._plain(getattr(consent, "emergency_reason", "") if consent else ""),
                "consent_date": self._format_date(created_at),
                "consent_time": self._format_time(created_at),
                "print_datetime": self._format_dt(print_dt),
                "consilium_doctor_1": self._plain(consilium.get("doctor_1")),
                "consilium_doctor_2": self._plain(consilium.get("doctor_2")),
                "consilium_doctor_3": self._plain(consilium.get("doctor_3")),
                "consilium_notes": self._plain(consilium.get("notes")),
            }
        )
        return context

    def _lp_protocol_context(self, bundle: ProcedureBundle) -> dict[str, str]:
        procedure = bundle.procedure
        lp = bundle.lumbar_puncture
        if lp is None:
            raise ValueError("Данные люмбальной пункции не найдены.")
        context = self._base_context(bundle)
        context.update(
            {
                "procedure_date": self._format_date(procedure.started_at),
                "start_time": self._format_time(procedure.started_at),
                "finish_time": "",
                "duration": "",
                "doctor": self._plain(procedure.doctor_name_snapshot),
                "notes": self._plain(procedure.notes),
                "indications_codes": self._codes(lp.indications, LP_INDICATION_CODES, "11" if lp.indications_other else ""),
                "indications_text": self._list_labels(lp.indications, LP_INDICATION_LABELS, lp.indications_other),
                "indications_other": self._plain(lp.indications_other),
                "place_code": self._code(lp.procedure_place_code, CVC_PLACE_CODES),
                "place_text": self._choice_label(lp.procedure_place_code, CVC_PLACE_LABELS, lp.procedure_place_other),
                "place_other": self._plain(lp.procedure_place_other),
                "anesthesia_code": self._code(lp.anesthesia_code, CVC_ANESTHESIA_CODES),
                "anesthesia_text": self._choice_label(lp.anesthesia_code, CVC_ANESTHESIA_LABELS, lp.anesthesia_other),
                "anesthesia_other": self._plain(lp.anesthesia_other),
                "access_code": self._code(lp.access_code, LP_ACCESS_CODES),
                "access_text": self._choice_label(lp.access_code, LP_ACCESS_LABELS, lp.access_other),
                "access_other": self._plain(lp.access_other),
                "level_code": self._code(lp.level_code, LP_LEVEL_CODES),
                "level_text": self._choice_label(lp.level_code, LP_LEVEL_LABELS, lp.level_other),
                "level_other": self._plain(lp.level_other),
                "difficulty_code": "2" if lp.technical_difficulty_code == "complications" else "1",
                "difficulty_text": self._lp_difficulty_text(lp),
                "difficulty_description": self._plain(lp.technical_difficulty_description),
                "actions_taken": self._plain(lp.actions_taken),
                "result_text": self._plain(LP_RESULT_LABELS.get(lp.result_code, lp.result_code or "")),
                "csf_characteristics": self._plain(lp.csf_characteristics),
                "result_notes": self._plain(lp.result_notes),
            }
        )
        return context

    def _lp_consent_context(self, bundle: ProcedureBundle) -> dict[str, str]:
        context = self._base_context(bundle)
        consent = bundle.consent
        consilium = self._consilium_dict(getattr(consent, "consilium_json", "") if consent else "")
        print_dt = datetime.now().replace(second=0, microsecond=0)
        created_at = getattr(consent, "created_at", None) if consent else None
        if not created_at:
            created_at = bundle.procedure.started_at
        context.update(
            {
                "doctor": self._plain(bundle.procedure.doctor_name_snapshot),
                "consent_mode": self._plain(getattr(consent, "consent_mode", "patient") if consent else "patient"),
                "representative_name": self._plain(getattr(consent, "representative_name", "") if consent else ""),
                "representative_details": self._plain(getattr(consent, "representative_details", "") if consent else ""),
                "emergency_reason": self._plain(getattr(consent, "emergency_reason", "") if consent else ""),
                "consent_date": self._format_date(created_at),
                "consent_time": self._format_time(created_at),
                "print_datetime": self._format_dt(print_dt),
                "consilium_doctor_1": self._plain(consilium.get("doctor_1")),
                "consilium_doctor_2": self._plain(consilium.get("doctor_2")),
                "consilium_doctor_3": self._plain(consilium.get("doctor_3")),
                "consilium_notes": self._plain(consilium.get("notes")),
            }
        )
        return context

    def _transfusion_protocol_context(self, bundle: ProcedureBundle) -> dict[str, str]:
        procedure = bundle.procedure
        transfusion = bundle.transfusion
        if transfusion is None:
            raise ValueError("Данные гемотрансфузии не найдены.")
        context = self._base_context(bundle)
        observation = self._observation_dict(transfusion.observation_json)
        scenario = transfusion.indication_code or ""
        context.update(
            {
                "print_datetime": self._format_dt(datetime.now().replace(second=0, microsecond=0)),
                "request_datetime": self._format_dt(transfusion.request_at),
                "transfusion_date": self._format_date_long(procedure.started_at),
                "transfusion_date_short": self._format_date(procedure.started_at),
                "start_time": self._format_time(procedure.started_at),
                "finish_time": self._format_time(procedure.finished_at),
                "duration": self._plain(procedure.duration_minutes),
                "doctor": self._plain(procedure.doctor_name_snapshot),
                "notes": self._plain(procedure.notes),
                "scenario_suffix": self._plain(TRANSFUSION_SCENARIO_SUFFIX.get(scenario, "")),
                "indication_text": self._plain(TRANSFUSION_INDICATION_LABELS.get(scenario, "")),
                "indication_print": self._plain(TRANSFUSION_INDICATION_PRINT_LABELS.get(scenario, "")),
                "recipient_abo": self._plain(transfusion.recipient_abo),
                "recipient_rh": self._plain(transfusion.recipient_rh),
                "recipient_antigens": self._plain(transfusion.recipient_antigens),
                "alloimmune_antibodies": self._plain(
                    TRANSFUSION_ALLOIMMUNE_LABELS.get(transfusion.alloimmune_antibodies, transfusion.alloimmune_antibodies)
                ),
                "transfusions_history": self._plain(
                    TRANSFUSION_WERE_NOT_WERE.get(transfusion.transfusions_history, transfusion.transfusions_history)
                ),
                "reactions_history": self._plain(
                    TRANSFUSION_YES_NO.get(transfusion.reactions_history, transfusion.reactions_history)
                ),
                "reactions_history_details": self._plain(transfusion.reactions_history_details),
                "individual_selection_history": self._plain(
                    TRANSFUSION_WAS_NOT_WAS.get(
                        transfusion.individual_selection_history,
                        transfusion.individual_selection_history,
                    )
                ),
                "donor_component_name": self._plain(transfusion.donor_component_name),
                "procurement_org": self._plain(transfusion.procurement_org),
                "donor_abo": self._plain(transfusion.donor_abo),
                "donor_rh": self._plain(transfusion.donor_rh),
                "donor_antigens": self._plain(transfusion.donor_antigens),
                "unit_number": self._plain(transfusion.unit_number),
                "volume_ml": self._plain(transfusion.volume_ml),
                "collection_date": self._plain(transfusion.collection_date),
                "expiration_date": self._plain(transfusion.expiration_date),
                "selection_medical_org": self._plain(transfusion.selection_medical_org),
                "selection_study_date": self._plain(transfusion.selection_study_date),
                "selection_responsible_name": self._plain(transfusion.selection_responsible_name),
                "selection_conclusion": self._plain(transfusion.selection_conclusion),
                "reagent_series_text": self._plain(
                    "N серии реагента "
                    f"Анти-А:{transfusion.reagent_anti_a_series}, "
                    f"Анти-B:{transfusion.reagent_anti_b_series}, "
                    f"Анти-D:{transfusion.reagent_anti_d_series}"
                ),
                "reagent_expiration_text": self._plain(
                    "Срок годности "
                    f"Анти-А до {transfusion.reagent_anti_a_expiration}, "
                    f"Анти-B до {transfusion.reagent_anti_b_expiration}, "
                    f"Анти-D до {transfusion.reagent_anti_d_expiration}"
                ),
                "plane_compatibility": self._plain(transfusion.plane_compatibility if scenario != "vpfs" else "не применяется"),
                "biological_test": self._plain(transfusion.biological_test),
                "reaction_symptoms": self._plain(transfusion.reaction_symptoms),
                "reaction_severity": self._plain(transfusion.reaction_severity),
                "obs_before_bp": self._plain(observation.get("before", {}).get("bp")),
                "obs_before_pulse": self._plain(observation.get("before", {}).get("pulse")),
                "obs_before_temp": self._plain(observation.get("before", {}).get("temp")),
                "obs_before_diuresis": self._plain(observation.get("before", {}).get("diuresis")),
                "obs_hour1_bp": self._plain(observation.get("hour1", {}).get("bp")),
                "obs_hour1_pulse": self._plain(observation.get("hour1", {}).get("pulse")),
                "obs_hour1_temp": self._plain(observation.get("hour1", {}).get("temp")),
                "obs_hour1_diuresis": self._plain(observation.get("hour1", {}).get("diuresis")),
                "obs_hour2_bp": self._plain(observation.get("hour2", {}).get("bp")),
                "obs_hour2_pulse": self._plain(observation.get("hour2", {}).get("pulse")),
                "obs_hour2_temp": self._plain(observation.get("hour2", {}).get("temp")),
                "obs_hour2_diuresis": self._plain(observation.get("hour2", {}).get("diuresis")),
            }
        )
        return context

    def _base_context(self, bundle: ProcedureBundle) -> dict[str, str]:
        snapshot = bundle.patient_snapshot or {}
        procedure = bundle.procedure
        return {
            "patient_name": self._plain(snapshot.get("full_name")),
            "history_number": self._plain(snapshot.get("history_number")),
            "sex": self._plain(snapshot.get("sex")),
            "age": self._plain(snapshot.get("age")),
            "birth_date": self._plain(snapshot.get("birth_date")),
            "department": self._plain(procedure.department_snapshot or snapshot.get("department")),
            "bed_number": self._plain(snapshot.get("bed_number")),
            "diagnosis": self._plain(procedure.diagnosis_snapshot or snapshot.get("diagnosis")),
            "procedure_id": self._plain(procedure.id),
        }

    def _removal_context(self, cvc) -> dict[str, str]:
        action = ""
        if cvc.removed_or_replaced == "removed":
            action = "катетер удалён"
        elif cvc.removed_or_replaced == "replaced":
            action = "катетер переустановлен"
        removal_summary = action
        date_text = self._format_date(cvc.removed_at)
        time_text = self._format_time(cvc.removed_at)
        if date_text:
            removal_summary = f"{removal_summary}; дата {date_text}" if removal_summary else f"Дата {date_text}"
        if time_text:
            removal_summary = f"{removal_summary}; время {time_text}" if removal_summary else f"Время {time_text}"
        complications_text = "Отмечались" if cvc.usage_complications_code == "present" else "Не отмечались"
        removal_doctor = cvc.removal_doctor_name or cvc.operator_doctor_name
        block_present = bool(
            cvc.removed_or_replaced
            or cvc.usage_complications_description
            or cvc.additional_treatment
            or cvc.removal_doctor_name
        )
        return {
            "removal_action": self._plain(action),
            "removal_date": date_text,
            "removal_time": time_text,
            "removal_summary": self._plain(removal_summary),
            "usage_complications_code": "2" if cvc.usage_complications_code == "present" else "1",
            "usage_complications_text": self._plain(complications_text),
            "usage_complications_description": self._plain(cvc.usage_complications_description),
            "additional_treatment": self._plain(cvc.additional_treatment),
            "removal_doctor": self._plain(cvc.removal_doctor_name),
            "removal_doctor_or_operator": self._plain(removal_doctor),
            "removal_block_present": "1" if block_present else "",
        }

    def _load_template(self, name: str) -> str:
        path = self.template_dir / name
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _fill(template: str, context: dict[str, str]) -> str:
        result = template
        for key, value in context.items():
            result = result.replace("{{" + key + "}}", value)
        return result

    @staticmethod
    def _plain(value: Any) -> str:
        if value is None:
            return ""
        return escape(str(value))

    @staticmethod
    def _format_dt(value: Any) -> str:
        if isinstance(value, datetime):
            return escape(value.strftime("%d.%m.%Y %H:%M"))
        return ""

    @staticmethod
    def _format_date(value: Any) -> str:
        if isinstance(value, datetime):
            return escape(value.strftime("%d.%m.%Y"))
        return ""

    @staticmethod
    def _format_date_long(value: Any) -> str:
        if not isinstance(value, datetime):
            return ""
        months = {
            1: "января",
            2: "февраля",
            3: "марта",
            4: "апреля",
            5: "мая",
            6: "июня",
            7: "июля",
            8: "августа",
            9: "сентября",
            10: "октября",
            11: "ноября",
            12: "декабря",
        }
        return escape(f"{value.day} {months[value.month]} {value.year} г.")

    @staticmethod
    def _format_time(value: Any) -> str:
        if isinstance(value, datetime):
            return escape(value.strftime("%H:%M"))
        return ""

    def _list_labels(self, values: list[str], labels: dict[str, str], other: str) -> str:
        items = [labels.get(str(value), str(value)) for value in values or []]
        if other:
            items.append(f"Прочее: {other}")
        return "; ".join(self._plain(item) for item in items)

    def _choice_label(self, value: Any, labels: dict[str, str], other: str) -> str:
        key = str(value or "").strip()
        other_text = str(other or "").strip()
        items: list[str] = []
        if key and key != "other":
            items.append(labels.get(key, key))
        if other_text:
            label = labels.get("other", "Прочее")
            items.append(f"{label}: {other_text}")
        elif key == "other":
            items.append(labels.get("other", "Прочее"))
        return "; ".join(self._plain(item) for item in items)

    def _with_other(self, value: Any, other: str) -> str:
        text = str(value or "").strip()
        if other:
            text = f"{text}; прочее: {other}" if text else f"Прочее: {other}"
        return self._plain(text or "Не указано")

    def _yes_no_lines(self, items: list[tuple[str, bool]]) -> str:
        return "<br>".join(f"{self._plain(label)}: {'да' if selected else 'нет'}" for label, selected in items)

    def _code(self, value: str, mapping: dict[str, str]) -> str:
        return self._plain(mapping.get(str(value or ""), ""))

    def _codes(self, values: list[str], mapping: dict[str, str], extra: str = "") -> str:
        result = [mapping.get(str(value), "") for value in values or []]
        if extra:
            result.append(str(extra))
        return self._plain(", ".join(item for item in result if item) or "")

    def _catheter_params_text(self, cvc) -> str:
        parts: list[str] = []
        if cvc.attempts_count:
            parts.append(self._format_number(cvc.attempts_count))
        if cvc.diameter_f:
            parts.append(f"диаметр: {self._format_number(cvc.diameter_f)} F")
        if cvc.length_cm:
            parts.append(f"длина: {self._format_number(cvc.length_cm)} см")
        if cvc.lumens_count:
            parts.append(f"количество просветов: {self._format_number(cvc.lumens_count)}")
        return "; ".join(parts)

    def _difficulty_text(self, cvc) -> str:
        if cvc.technical_difficulty_code == "complications":
            text = "Сложности / осложнения"
            if cvc.technical_difficulty_description:
                text = f"{text}: {cvc.technical_difficulty_description}"
            return self._plain(text)
        if cvc.technical_difficulty_description:
            return self._plain(cvc.technical_difficulty_description)
        return "Не выявлено"

    def _lp_difficulty_text(self, lp) -> str:
        if lp.technical_difficulty_code == "complications":
            text = "Сложности / осложнения"
            if lp.technical_difficulty_description:
                text = f"{text}: {lp.technical_difficulty_description}"
            return self._plain(text)
        if lp.technical_difficulty_description:
            return self._plain(lp.technical_difficulty_description)
        return "Не выявлено"

    def _format_number(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, float) and value.is_integer():
            return self._plain(int(value))
        return self._plain(value)

    @staticmethod
    def _consilium_dict(raw: str) -> dict[str, str]:
        try:
            data = json.loads(raw or "{}")
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _observation_dict(raw: str) -> dict[str, dict[str, str]]:
        default = {
            "before": {"bp": "", "pulse": "", "temp": "", "diuresis": "сохранен, желтая"},
            "hour1": {"bp": "", "pulse": "", "temp": "", "diuresis": "сохранен, желтая"},
            "hour2": {"bp": "", "pulse": "", "temp": "", "diuresis": "сохранен, желтая"},
        }
        try:
            data = json.loads(raw or "{}")
        except Exception:
            return default
        if not isinstance(data, dict):
            return default
        for slot, values in default.items():
            incoming = data.get(slot)
            if isinstance(incoming, dict):
                values.update({key: str(incoming.get(key) or "") for key in ("bp", "pulse", "temp", "diuresis")})
        return default

    @staticmethod
    def _safe_filename(value: str) -> str:
        safe = re.sub(r"[^0-9A-Za-zА-Яа-я_.-]+", "_", str(value or "").strip())
        return safe.strip("._") or "procedure"

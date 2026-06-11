from __future__ import annotations

import json
from datetime import datetime, timedelta
from html import escape
from statistics import median
from typing import Iterable

from rem_card.services.analytics.constants import STATISTICAL_BED_COUNT, STATISTICAL_HIGH_LOAD_THRESHOLD
from rem_card.services.analytics.graphs_service import _thread_local_manager
from rem_card.services.analytics.recovery_filter import recovery_bed_analytics_filter
from rem_card.services.analytics.recovery_summary import (
    build_recovery_bed_summary,
    build_recovery_summary_rows,
)
from rem_card.ui.styles.theme import (
    BG_CARD,
    BG_LIGHT,
    BORDER_COLOR,
    COLOR_PRIMARY_DARK,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)


RECOVERY_SECTION_KEY = "s_recovery"

SECTION_GROUPS = {
    "Основная деятельность": {
        "s1": "1. Общая деятельность отделения",
        "s2": "2. Использование коечного фонда",
        "s3": "3. Демография",
        "s4": "4. Поток пациентов",
        "s5": "5. Диагностическая структура",
        "s6": "6. Исходы лечения",
        "s7": "7. Время до смерти",
        "s8": "8. Летальность по группам",
        RECOVERY_SECTION_KEY: "9. Пробуждение",
    },
    "Интенсивная терапия и вмешательства": {
        "s9": "10. ИВЛ",
        "s10": "11. Операции",
        "s11": "12. Переливания",
        "s12": "13. Центральные венозные катетеры",
        "s13": "14. Люмбальные пункции",
        "s14": "15. Осложнения процедур",
        "s16": "17. Индексы интенсивности",
        "s17": "18. Индексы нагрузки",
        "s18": "19. Специальные показатели",
        "s19": "20. Нагрузка персонала",
        "sx": "➕ Дополнительные показатели",
    },
}
TOP_SECTIONS = [
    "s1",
    "s2",
    "s6",
    "s7",
    RECOVERY_SECTION_KEY,
    "s9",
    "s10",
    "s11",
    "s12",
    "s13",
    "s14",
    "s18",
    "s19",
    "sx",
]

PERFORMED_PROCEDURE_STATUSES = {
    "active",
    "completed",
    "catheter_removed",
    "catheter_replaced",
    "catheter_transferred",
    "catheter_dead",
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

CVC_STATUS_LABELS = {
    "active": "Катетер активен",
    "completed": "Процедура завершена",
    "catheter_removed": "Катетер удален",
    "catheter_replaced": "Катетер переустановлен",
    "removed": "Катетер удален",
    "replaced": "Катетер переустановлен",
    "catheter_transferred": "Переведен с кат.",
    "catheter_dead": "Умер с кат.",
    "transferred_with_catheter": "Переведен с кат.",
    "dead_with_catheter": "Умер с кат.",
}

LP_ACCESS_LABELS = {
    "midline": "Срединный доступ",
    "paramedian": "Парамедианный доступ",
    "taylor": "Доступ Тейлора",
}

LP_LEVEL_LABELS = {
    "L1-L2": "L1-L2",
    "L2-L3": "L2-L3",
    "L3-L4": "L3-L4",
    "L4-L5": "L4-L5",
    "L5-S1": "L5-S1",
}

LP_RESULT_LABELS = {
    "csf_obtained": "Ликвор получен",
    "csf_not_obtained": "Ликвор не получен",
}

TRANSFUSION_INDICATION_LABELS = {
    "voce": "ВОЦЭ",
    "vpfs": "ВПФС",
}


def parse_statistics_datetime(value):
    return DetailedStatisticsReportBuilder._parse_datetime(value)


def build_detailed_statistics_report_html(
    db_manager,
    start_date_str: str,
    end_date_str: str,
    selected_sections: Iterable[str],
    *,
    include_recovery_beds: bool = False,
) -> str:
    builder = DetailedStatisticsReportBuilder(
        db_manager,
        start_date_str,
        end_date_str,
        include_recovery_beds=include_recovery_beds,
    )
    return builder.generate_report_html(list(selected_sections or []))


class DetailedStatisticsReportBuilder:
    def __init__(
        self,
        db_manager,
        start_date_str: str,
        end_date_str: str,
        *,
        include_recovery_beds: bool = False,
    ):
        self.db_manager = db_manager
        self._start_dt = self._parse_datetime(start_date_str) or (datetime.now() - timedelta(days=30))
        self._end_dt = self._parse_datetime(end_date_str) or datetime.now()
        if self._end_dt < self._start_dt:
            self._start_dt, self._end_dt = self._end_dt, self._start_dt
        self.start_date_str = self._start_dt.strftime("%Y-%m-%d 00:00:00")
        self.end_date_str = self._end_dt.strftime("%Y-%m-%d 23:59:59")
        self.section_groups = SECTION_GROUPS
        self.include_recovery_beds = bool(include_recovery_beds)

    @staticmethod
    def _parse_datetime(value):
        if isinstance(value, datetime):
            return value
        text = str(value or "").strip()
        if not text:
            return None
        text = text.replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
            try:
                parsed = datetime.strptime(text, fmt)
                if fmt in ("%Y-%m-%d", "%d.%m.%Y"):
                    return parsed.replace(hour=0, minute=0, second=0, microsecond=0)
                return parsed
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None

    @staticmethod
    def _safe_float(value, default=0.0):
        try:
            if value is None:
                return float(default)
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _safe_int(value, default=0):
        try:
            if value is None:
                return int(default)
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    @staticmethod
    def _safe_div(num: float, den: float) -> float:
        if not den:
            return 0.0
        return float(num) / float(den)

    @staticmethod
    def _pct(num: float, den: float) -> float:
        if not den:
            return 0.0
        return (float(num) / float(den)) * 100.0

    @staticmethod
    def _fmt_num(value, digits=2):
        if value is None:
            return "н/д"
        if isinstance(value, int):
            return str(value)
        return f"{float(value):.{digits}f}"

    @staticmethod
    def _fmt_pct(num: float, den: float, digits=1):
        return f"{DetailedStatisticsReportBuilder._pct(num, den):.{digits}f}%"

    @staticmethod
    def _normalize_text(value, fallback="Не указано"):
        text = str(value or "").strip()
        return text if text else fallback

    @staticmethod
    def _normalize_outcome(raw_outcome: str, transfer_dt, death_dt) -> str:
        text = str(raw_outcome or "").strip().lower()
        if death_dt is not None or text == "умер":
            return "умер"
        if transfer_dt is not None or text == "переведен":
            return "переведен"
        if not text or text == "в отделении":
            return "в отделении"
        return text

    @staticmethod
    def _age_to_years(age_value, age_unit: str):
        if age_value is None:
            return None
        try:
            age = float(age_value)
        except (TypeError, ValueError):
            return None
        unit = str(age_unit or "").strip().lower()
        if "меся" in unit:
            return age / 12.0
        return age

    @staticmethod
    def _age_group(age_years):
        if age_years is None:
            return "Не указан"
        if age_years < 1:
            return "до 1 года"
        if age_years < 18:
            return "1–17"
        if age_years <= 44:
            return "18–44"
        if age_years <= 60:
            return "45–60"
        if age_years <= 75:
            return "61–75"
        return "75+"

    @staticmethod
    def _weekday_name(value: datetime):
        names = [
            "Понедельник",
            "Вторник",
            "Среда",
            "Четверг",
            "Пятница",
            "Суббота",
            "Воскресенье",
        ]
        return names[value.weekday()]

    @staticmethod
    def _mkb_class(code: str):
        text = str(code or "").strip()
        if not text:
            return "Не указан"
        return text[:3].upper()

    @staticmethod
    def _diagnosis_key(code: str, diagnosis_text: str):
        clean_code = str(code or "").strip() or "—"
        clean_text = str(diagnosis_text or "").strip() or "Без уточнения"
        return f"{clean_code} {clean_text}"

    @staticmethod
    def _distribution_lines(counter: dict[str, int], total: int, *, limit=12, forced_order=None):
        if not counter:
            return "н/д"
        if forced_order:
            items = [(name, counter.get(name, 0)) for name in forced_order if counter.get(name, 0) > 0]
            tail = [(k, v) for k, v in counter.items() if k not in forced_order]
            tail.sort(key=lambda x: (-x[1], x[0]))
            items.extend(tail)
        else:
            items = sorted(counter.items(), key=lambda x: (-x[1], x[0]))
        visible = items[:limit]
        lines = [
            f"{escape(str(name))}: {count} ({DetailedStatisticsReportBuilder._fmt_pct(count, total)})"
            for name, count in visible
        ]
        hidden_count = len(items) - len(visible)
        if hidden_count > 0:
            lines.append(f"... еще {hidden_count}")
        return "<br/>".join(lines) if lines else "н/д"

    @staticmethod
    def _distribution_mortality_lines(total_counter: dict[str, int], deaths_counter: dict[str, int], *, limit=12):
        if not total_counter:
            return "н/д"
        items = sorted(total_counter.items(), key=lambda x: (-x[1], x[0]))[:limit]
        lines = []
        for group_name, total in items:
            deaths = deaths_counter.get(group_name, 0)
            rate = DetailedStatisticsReportBuilder._fmt_pct(deaths, total)
            lines.append(f"{escape(str(group_name))}: {deaths}/{total} ({rate})")
        return "<br/>".join(lines) if lines else "н/д"

    @staticmethod
    def _table_exists(cursor, table_name: str) -> bool:
        cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table_name,),
        )
        return cursor.fetchone() is not None

    @classmethod
    def _table_columns(cls, cursor, table_name: str) -> set[str]:
        if not cls._table_exists(cursor, table_name):
            return set()
        cursor.execute(f'PRAGMA table_info("{table_name}")')
        return {str(row[1]) for row in cursor.fetchall() if row and row[1]}

    @staticmethod
    def _select_expr(columns: set[str], column: str, fallback: str = "NULL") -> str:
        if column in columns:
            return column
        return f"{fallback} AS {column}"

    @staticmethod
    def _json_dict(value):
        if isinstance(value, dict):
            return dict(value)
        text = str(value or "").strip()
        if not text:
            return {}
        try:
            decoded = json.loads(text)
        except Exception:
            return {}
        return decoded if isinstance(decoded, dict) else {}

    @classmethod
    def _death_doctor_from_payload(cls, value) -> str:
        payload = cls._json_dict(value)
        protocol = payload.get("death_protocol")
        protocol = protocol if isinstance(protocol, dict) else {}
        doctor = str(
            protocol.get("doctor")
            or protocol.get("signature_doctor")
            or payload.get("doctor")
            or payload.get("signature_doctor")
            or ""
        ).strip()
        return doctor or "Не указан"

    @staticmethod
    def _clean_label(value, fallback="Не указано") -> str:
        text = str(value or "").strip()
        return text if text else fallback

    @classmethod
    def _transfusion_type_label(cls, value) -> str:
        text = str(value or "").strip()
        normalized = text.lower().replace("ё", "е")
        if normalized in {"blood", "rbc"} or any(token in normalized for token in ("кров", "эритро", "эрмас")):
            return "Кровь"
        if normalized in {"plasma", "ffp"} or any(token in normalized for token in ("плазм", "сзп")):
            return "Плазма"
        return cls._clean_label(text)

    @classmethod
    def _procedure_transfusion_type_label(cls, indication_code, component_name) -> str:
        indication = str(indication_code or "").strip().lower()
        component = str(component_name or "").strip().lower().replace("ё", "е")
        if indication == "vpfs" or "плазм" in component or "сзп" in component or "ffp" in component:
            return "Плазма"
        if indication == "voce" or any(token in component for token in ("эритро", "эр.", "rbc", "кров")):
            return "Кровь"
        return "Прочее"

    @classmethod
    def _cvc_access_label(cls, access_code, access_other="") -> str:
        code = str(access_code or "").strip()
        other = str(access_other or "").strip()
        if code == "other" and other:
            return f"Прочие локализации: {other}"
        return CVC_ACCESS_LABELS.get(code, cls._clean_label(other or code))

    @classmethod
    def _cvc_status_label(cls, procedure_status, catheter_status="", action="") -> str:
        for raw in (action, catheter_status, procedure_status):
            text = str(raw or "").strip()
            if text in CVC_STATUS_LABELS:
                return CVC_STATUS_LABELS[text]
        return cls._clean_label(procedure_status)

    @classmethod
    def _lp_access_label(cls, access_code, access_other="") -> str:
        code = str(access_code or "").strip()
        other = str(access_other or "").strip()
        return LP_ACCESS_LABELS.get(code, cls._clean_label(other or code))

    @classmethod
    def _lp_level_label(cls, level_code, level_other="") -> str:
        code = str(level_code or "").strip()
        other = str(level_other or "").strip()
        return LP_LEVEL_LABELS.get(code, cls._clean_label(other or code))

    @classmethod
    def _lp_result_label(cls, result_code) -> str:
        code = str(result_code or "").strip()
        return LP_RESULT_LABELS.get(code, cls._clean_label(code))

    @staticmethod
    def _is_performed_procedure_status(status) -> bool:
        return str(status or "").strip().lower() in PERFORMED_PROCEDURE_STATUSES

    @staticmethod
    def _has_complication(code, description="") -> bool:
        normalized = str(code or "").strip().lower()
        if normalized in {"complications", "present", "yes", "true"}:
            return True
        if normalized in {"none", "no", "false"}:
            return False
        return bool(str(description or "").strip())

    @classmethod
    def _count_volume_text(cls, count: int, volume_ml: float) -> str:
        return f"{cls._fmt_num(count, 0)} / {cls._fmt_num(volume_ml, 0)} мл"

    @classmethod
    def _count_volume_distribution_lines(cls, stats: dict[str, dict[str, float]], total_count: int, *, limit=12):
        if not stats:
            return "н/д"
        items = sorted(stats.items(), key=lambda item: (-item[1].get("count", 0), item[0]))[:limit]
        lines = []
        for name, values in items:
            count = int(values.get("count") or 0)
            volume = float(values.get("volume") or 0.0)
            lines.append(
                f"{escape(str(name))}: {count} ({cls._fmt_pct(count, total_count)}), {cls._fmt_num(volume, 0)} мл"
            )
        return "<br/>".join(lines) if lines else "н/д"

    @classmethod
    def _death_doctor_lines(cls, counter: dict[str, int], total_deaths: int, total_admissions: int, *, limit=12):
        if not counter:
            return "н/д"
        items = sorted(counter.items(), key=lambda x: (-x[1], x[0]))[:limit]
        lines = []
        for doctor, count in items:
            lines.append(
                f"{escape(str(doctor))}: {count} "
                f"({cls._fmt_pct(count, total_deaths)} от смертей; "
                f"{cls._fmt_pct(count, total_admissions)} от госпитализаций)"
            )
        return "<br/>".join(lines) if lines else "н/д"

    def _fetch_context(self):
        manager, cleanup = _thread_local_manager(self.db_manager)
        conn = manager.get_connection()
        cursor = conn.cursor()
        try:
            with recovery_bed_analytics_filter(conn, include_recovery_beds=self.include_recovery_beds):
                period_params = (self.start_date_str, self.end_date_str)
                admission_columns = self._table_columns(cursor, "admissions")
                cardiac_measures_expr = self._select_expr(admission_columns, "cardiac_arrest_measures_json")

                cursor.execute(
                    f"""
                    SELECT
                        id,
                        patient_id,
                        admission_datetime,
                        transfer_datetime,
                        death_datetime,
                        outcome,
                        patient_age,
                        patient_age_unit,
                        patient_gender,
                        source_department,
                        diagnosis_code,
                        diagnosis_text,
                        {cardiac_measures_expr}
                    FROM admissions
                    WHERE admission_datetime BETWEEN ? AND ?
                    """,
                    period_params,
                )
                columns = [column[0] for column in cursor.description]
                raw_admissions = [dict(zip(columns, row)) for row in cursor.fetchall()]

                admissions = []
                for row in raw_admissions:
                    admission_id = self._safe_int(row.get("id"), default=0)
                    patient_id = row.get("patient_id")
                    adm_dt = self._parse_datetime(row.get("admission_datetime"))
                    if adm_dt is None:
                        continue

                    transfer_dt = self._parse_datetime(row.get("transfer_datetime"))
                    death_dt = self._parse_datetime(row.get("death_datetime"))

                    raw_end_candidates = [dt for dt in (death_dt, transfer_dt, self._end_dt) if dt is not None]
                    los_end_dt = min(raw_end_candidates) if raw_end_candidates else self._end_dt
                    if los_end_dt < adm_dt:
                        los_end_dt = adm_dt

                    los_days = max(0.0, (los_end_dt - adm_dt).total_seconds() / 86400.0)

                    outcome = self._normalize_outcome(row.get("outcome"), transfer_dt, death_dt)
                    is_death = outcome == "умер"
                    death_time_hours = None
                    if is_death and death_dt is not None:
                        death_time_hours = max(0.0, (death_dt - adm_dt).total_seconds() / 3600.0)
                    death_doctor = self._death_doctor_from_payload(row.get("cardiac_arrest_measures_json")) if is_death else ""

                    age_years = self._age_to_years(row.get("patient_age"), row.get("patient_age_unit"))
                    gender = self._normalize_text(row.get("patient_gender"))
                    source = self._normalize_text(row.get("source_department"))
                    diagnosis_code = str(row.get("diagnosis_code") or "").strip()
                    diagnosis_text = str(row.get("diagnosis_text") or "").strip()

                    admissions.append(
                        {
                            "admission_id": admission_id,
                            "patient_id": patient_id,
                            "admission_dt": adm_dt,
                            "transfer_dt": transfer_dt,
                            "death_dt": death_dt,
                            "outcome": outcome,
                            "los_days": los_days,
                            "age_years": age_years,
                            "age_group": self._age_group(age_years),
                            "gender": gender,
                            "source": source,
                            "diagnosis_code": diagnosis_code,
                            "diagnosis_text": diagnosis_text,
                            "diagnosis_key": self._diagnosis_key(diagnosis_code, diagnosis_text),
                            "mkb_class": self._mkb_class(diagnosis_code),
                            "weekday_name": self._weekday_name(adm_dt),
                            "month_label": adm_dt.strftime("%Y-%m"),
                            "is_death": is_death,
                            "death_time_hours": death_time_hours,
                            "death_doctor": death_doctor,
                        }
                    )

                admission_ids = {row["admission_id"] for row in admissions if row["admission_id"]}

                cursor.execute(
                    """
                    SELECT admission_id
                    FROM operations
                    WHERE operation_datetime BETWEEN ? AND ?
                    """,
                    period_params,
                )
                raw_ops = [self._safe_int(r[0], default=0) for r in cursor.fetchall()]
                operations_adm_ids = [aid for aid in raw_ops if aid and aid in admission_ids]

                cursor.execute(
                    """
                    SELECT admission_id, type, volume_ml
                    FROM transfusions
                    WHERE datetime BETWEEN ? AND ?
                    """,
                    period_params,
                )
                transfusions = []
                for admission_id, transf_type, volume_ml in cursor.fetchall():
                    aid = self._safe_int(admission_id, default=0)
                    if not aid or aid not in admission_ids:
                        continue
                    transfusions.append(
                        {
                            "admission_id": aid,
                            "type": self._transfusion_type_label(transf_type),
                            "volume_ml": self._safe_float(volume_ml, default=0.0),
                        }
                    )

                cursor.execute(
                    """
                    SELECT admission_id, start_time, end_time
                    FROM ivl_episodes
                    WHERE start_time BETWEEN ? AND ?
                    """,
                    period_params,
                )
                ivl_episodes = []
                for admission_id, start_time, end_time in cursor.fetchall():
                    aid = self._safe_int(admission_id, default=0)
                    if not aid or aid not in admission_ids:
                        continue
                    start_dt = self._parse_datetime(start_time)
                    if start_dt is None:
                        continue
                    end_dt = self._parse_datetime(end_time) or self._end_dt
                    if end_dt < start_dt:
                        end_dt = start_dt
                    end_dt = min(end_dt, self._end_dt)
                    duration_hours = max(0.0, (end_dt - start_dt).total_seconds() / 3600.0)
                    ivl_episodes.append(
                        {
                            "admission_id": aid,
                            "duration_hours": duration_hours,
                        }
                    )

                cvc_procedures = self._fetch_cvc_procedures(cursor, admission_ids)
                lumbar_punctures = self._fetch_lumbar_punctures(cursor, admission_ids)
                procedure_transfusions = self._fetch_procedure_transfusions(cursor, admission_ids)

                return {
                    "admissions": admissions,
                    "operations_adm_ids": operations_adm_ids,
                    "transfusions": transfusions,
                    "ivl_episodes": ivl_episodes,
                    "cvc_procedures": cvc_procedures,
                    "lumbar_punctures": lumbar_punctures,
                    "procedure_transfusions": procedure_transfusions,
                }
        finally:
            if cleanup:
                cleanup()

    @staticmethod
    def _cursor_dict_rows(cursor) -> list[dict]:
        columns = [column[0] for column in (cursor.description or [])]
        rows = []
        for row in cursor.fetchall():
            if hasattr(row, "keys"):
                rows.append(dict(row))
            else:
                rows.append(dict(zip(columns, row)))
        return rows

    def _fetch_cvc_procedures(self, cursor, admission_ids: set[int]) -> list[dict]:
        if not admission_ids or not (
            self._table_exists(cursor, "procedures") and self._table_exists(cursor, "procedure_cvc")
        ):
            return []

        cursor.execute(
            """
            SELECT
                p.id AS procedure_id,
                p.admission_id,
                p.status,
                p.started_at,
                p.finished_at,
                p.duration_minutes,
                p.created_at,
                p.doctor_name_snapshot,
                c.access_code,
                c.access_other,
                c.attempts_count,
                c.diameter_f,
                c.length_cm,
                c.lumens_count,
                c.technical_difficulty_code,
                c.technical_difficulty_description,
                c.usage_complications_code,
                c.usage_complications_description,
                c.catheter_status,
                c.removed_or_replaced,
                c.removed_at,
                c.operator_doctor_name,
                c.removal_doctor_name
            FROM procedures p
            JOIN procedure_cvc c ON c.procedure_id = p.id
            WHERE p.procedure_type = 'CVC'
              AND COALESCE(p.is_deleted, 0) = 0
              AND DATETIME(COALESCE(p.started_at, p.created_at)) BETWEEN DATETIME(?) AND DATETIME(?)
            """,
            (self.start_date_str, self.end_date_str),
        )

        result = []
        for row in self._cursor_dict_rows(cursor):
            aid = self._safe_int(row.get("admission_id"), default=0)
            status = str(row.get("status") or "").strip().lower()
            if not aid or aid not in admission_ids or not self._is_performed_procedure_status(status):
                continue
            doctor = self._normalize_text(row.get("operator_doctor_name") or row.get("doctor_name_snapshot"))
            removal_doctor = self._normalize_text(row.get("removal_doctor_name"), fallback="")
            technical_description = str(row.get("technical_difficulty_description") or "").strip()
            usage_description = str(row.get("usage_complications_description") or "").strip()
            catheter_status = str(row.get("catheter_status") or "").strip().lower()
            action = str(row.get("removed_or_replaced") or "").strip().lower()
            has_catheter_endpoint = (
                status in {"catheter_removed", "catheter_replaced", "catheter_transferred", "catheter_dead"}
                or catheter_status in {"removed", "replaced", "transferred_with_catheter", "dead_with_catheter"}
                or action in {"removed", "replaced"}
            )
            start_dt = self._parse_datetime(row.get("started_at") or row.get("created_at"))
            finish_dt = self._parse_datetime(row.get("finished_at") or row.get("removed_at"))
            dwell_minutes = None
            if has_catheter_endpoint and start_dt is not None and finish_dt is not None:
                dwell_minutes = max(0.0, (finish_dt - start_dt).total_seconds() / 60.0)
            elif has_catheter_endpoint and row.get("duration_minutes") is not None:
                dwell_minutes = self._safe_float(row.get("duration_minutes"), default=0.0)
            result.append(
                {
                    "admission_id": aid,
                    "doctor": doctor,
                    "removal_doctor": removal_doctor,
                    "access": self._cvc_access_label(row.get("access_code"), row.get("access_other")),
                    "status": self._cvc_status_label(
                        status,
                        row.get("catheter_status"),
                        row.get("removed_or_replaced"),
                    ),
                    "attempts_count": row.get("attempts_count"),
                    "diameter_f": row.get("diameter_f"),
                    "length_cm": row.get("length_cm"),
                    "lumens_count": row.get("lumens_count"),
                    "technical_complication": self._has_complication(
                        row.get("technical_difficulty_code"),
                        technical_description,
                    ),
                    "technical_description": technical_description,
                    "usage_complication": self._has_complication(
                        row.get("usage_complications_code"),
                        usage_description,
                    ),
                    "usage_description": usage_description,
                    "dwell_minutes": dwell_minutes,
                }
            )
        return result

    def _fetch_lumbar_punctures(self, cursor, admission_ids: set[int]) -> list[dict]:
        if not admission_ids or not (
            self._table_exists(cursor, "procedures") and self._table_exists(cursor, "procedure_lumbar_puncture")
        ):
            return []

        cursor.execute(
            """
            SELECT
                p.id AS procedure_id,
                p.admission_id,
                p.status,
                p.started_at,
                p.created_at,
                p.doctor_name_snapshot,
                lp.access_code,
                lp.access_other,
                lp.level_code,
                lp.level_other,
                lp.technical_difficulty_code,
                lp.technical_difficulty_description,
                lp.result_code,
                lp.operator_doctor_name
            FROM procedures p
            JOIN procedure_lumbar_puncture lp ON lp.procedure_id = p.id
            WHERE p.procedure_type = 'LUMBAR_PUNCTURE'
              AND COALESCE(p.is_deleted, 0) = 0
              AND DATETIME(COALESCE(p.started_at, p.created_at)) BETWEEN DATETIME(?) AND DATETIME(?)
            """,
            (self.start_date_str, self.end_date_str),
        )

        result = []
        for row in self._cursor_dict_rows(cursor):
            aid = self._safe_int(row.get("admission_id"), default=0)
            status = str(row.get("status") or "").strip().lower()
            if not aid or aid not in admission_ids or not self._is_performed_procedure_status(status):
                continue
            description = str(row.get("technical_difficulty_description") or "").strip()
            result.append(
                {
                    "admission_id": aid,
                    "doctor": self._normalize_text(row.get("operator_doctor_name") or row.get("doctor_name_snapshot")),
                    "access": self._lp_access_label(row.get("access_code"), row.get("access_other")),
                    "level": self._lp_level_label(row.get("level_code"), row.get("level_other")),
                    "result": self._lp_result_label(row.get("result_code")),
                    "complication": self._has_complication(row.get("technical_difficulty_code"), description),
                    "complication_description": description,
                }
            )
        return result

    def _fetch_procedure_transfusions(self, cursor, admission_ids: set[int]) -> list[dict]:
        if not admission_ids or not (
            self._table_exists(cursor, "procedures") and self._table_exists(cursor, "procedure_transfusion")
        ):
            return []

        cursor.execute(
            """
            SELECT
                p.id AS procedure_id,
                p.admission_id,
                p.status,
                p.started_at,
                p.created_at,
                p.doctor_name_snapshot,
                t.indication_code,
                t.donor_component_name,
                t.volume_ml,
                t.reaction_symptoms,
                t.reaction_severity,
                t.operator_doctor_name
            FROM procedures p
            JOIN procedure_transfusion t ON t.procedure_id = p.id
            WHERE p.procedure_type = 'TRANSFUSION'
              AND COALESCE(p.is_deleted, 0) = 0
              AND DATETIME(COALESCE(p.started_at, p.created_at)) BETWEEN DATETIME(?) AND DATETIME(?)
            """,
            (self.start_date_str, self.end_date_str),
        )

        result = []
        for row in self._cursor_dict_rows(cursor):
            aid = self._safe_int(row.get("admission_id"), default=0)
            status = str(row.get("status") or "").strip().lower()
            if not aid or aid not in admission_ids or not self._is_performed_procedure_status(status):
                continue
            indication_code = str(row.get("indication_code") or "").strip()
            component = str(row.get("donor_component_name") or "").strip()
            reaction_symptoms = str(row.get("reaction_symptoms") or "").strip()
            reaction_severity = str(row.get("reaction_severity") or "").strip()
            result.append(
                {
                    "admission_id": aid,
                    "doctor": self._normalize_text(row.get("operator_doctor_name") or row.get("doctor_name_snapshot")),
                    "type": self._procedure_transfusion_type_label(indication_code, component),
                    "indication": TRANSFUSION_INDICATION_LABELS.get(indication_code, self._clean_label(indication_code)),
                    "component": self._clean_label(component),
                    "volume_ml": self._safe_float(row.get("volume_ml"), default=0.0),
                    "reaction": bool(reaction_symptoms or reaction_severity),
                }
            )
        return result

    def _population_stats(self, admissions):
        return {
            "total_n": len(admissions),
            "unique_patients": len({a["patient_id"] for a in admissions if a["patient_id"] is not None}),
            "death_ids": {a["admission_id"] for a in admissions if a["is_death"] and a["admission_id"]},
        }

    def _los_stats(self, admissions, total_n):
        los_values = [a["los_days"] for a in admissions]
        bed_days = sum(los_values)
        return {
            "bed_days": bed_days,
            "alos": self._safe_div(bed_days, total_n),
            "los_median": median(los_values) if los_values else None,
            "los_min": min(los_values) if los_values else None,
            "los_max": max(los_values) if los_values else None,
            "long_stay_count": sum(1 for x in los_values if x > 7.0),
        }

    def _age_stats(self, admissions):
        ages = [a["age_years"] for a in admissions if a["age_years"] is not None]
        return {
            "mean_age": self._safe_div(sum(ages), len(ages)) if ages else None,
            "median_age": median(ages) if ages else None,
        }

    def _death_timing_stats(self, admissions):
        death_times_hours = [a["death_time_hours"] for a in admissions if a["death_time_hours"] is not None]
        return {
            "mean_time_to_death_days": self._safe_div(sum(death_times_hours), len(death_times_hours)) / 24.0 if death_times_hours else None,
            "median_time_to_death_days": (median(death_times_hours) / 24.0) if death_times_hours else None,
            "early_deaths": sum(1 for h in death_times_hours if h < 24.0),
            "deaths_1_3_days": sum(1 for h in death_times_hours if 24.0 <= h < 72.0),
            "deaths_3_7_days": sum(1 for h in death_times_hours if 72.0 <= h < 168.0),
            "deaths_ge_7_days": sum(1 for h in death_times_hours if h >= 168.0),
        }

    @staticmethod
    def _inc_counter(counter, key):
        counter[key] = counter.get(key, 0) + 1

    def _distribution_stats(self, admissions):
        age_groups = {}
        age_groups_deaths = {}
        genders = {}
        sources = {}
        months = {}
        weekdays = {}
        diagnoses = {}
        mkb_classes = {}
        outcomes = {}
        death_doctors = {}

        for admission in admissions:
            self._inc_counter(age_groups, admission["age_group"])
            self._inc_counter(genders, admission["gender"])
            self._inc_counter(sources, admission["source"])
            self._inc_counter(months, admission["month_label"])
            self._inc_counter(weekdays, admission["weekday_name"])
            self._inc_counter(diagnoses, admission["diagnosis_key"])
            self._inc_counter(mkb_classes, admission["mkb_class"])
            outcome_label = admission["outcome"].capitalize()
            self._inc_counter(outcomes, outcome_label)
            if admission["is_death"]:
                self._inc_counter(age_groups_deaths, admission["age_group"])
                self._inc_counter(death_doctors, admission.get("death_doctor") or "Не указан")

        return {
            "age_groups": age_groups,
            "age_groups_deaths": age_groups_deaths,
            "genders": genders,
            "sources": sources,
            "months": months,
            "weekdays": weekdays,
            "diagnoses": diagnoses,
            "mkb_classes": mkb_classes,
            "outcomes": outcomes,
            "death_doctors": death_doctors,
        }

    def _operation_stats(self, operations_adm_ids, death_ids):
        surg_adm_ids = set(operations_adm_ids)
        return {
            "operations_count": len(operations_adm_ids),
            "surg_adm_ids": surg_adm_ids,
            "n_surg": len(surg_adm_ids),
            "deaths_surg": len(death_ids.intersection(surg_adm_ids)),
        }

    def _transfusion_stats(self, transfusions, death_ids):
        transf_adm_ids = {t["admission_id"] for t in transfusions}
        transf_by_type = {}
        transf_volume_by_type = {}
        for t in transfusions:
            self._inc_counter(transf_by_type, t["type"])
            bucket = transf_volume_by_type.setdefault(t["type"], {"count": 0, "volume": 0.0})
            bucket["count"] += 1
            bucket["volume"] += self._safe_float(t.get("volume_ml"), default=0.0)
        blood_bucket = transf_volume_by_type.get("Кровь", {})
        plasma_bucket = transf_volume_by_type.get("Плазма", {})
        other_units = sum(
            int(values.get("count") or 0)
            for name, values in transf_volume_by_type.items()
            if name not in {"Кровь", "Плазма"}
        )
        other_volume = sum(
            float(values.get("volume") or 0.0)
            for name, values in transf_volume_by_type.items()
            if name not in {"Кровь", "Плазма"}
        )
        return {
            "transfusion_units": len(transfusions),
            "transf_adm_ids": transf_adm_ids,
            "n_transf": len(transf_adm_ids),
            "volume_total": sum(t["volume_ml"] for t in transfusions),
            "transf_by_type": transf_by_type,
            "transf_volume_by_type": transf_volume_by_type,
            "blood_units": int(blood_bucket.get("count") or 0),
            "blood_volume": float(blood_bucket.get("volume") or 0.0),
            "plasma_units": int(plasma_bucket.get("count") or 0),
            "plasma_volume": float(plasma_bucket.get("volume") or 0.0),
            "other_transfusion_units": other_units,
            "other_transfusion_volume": other_volume,
            "deaths_transf": len(death_ids.intersection(transf_adm_ids)),
        }

    def _procedure_stats(self, cvc_procedures, lumbar_punctures, procedure_transfusions):
        cvc_accesses = {}
        cvc_statuses = {}
        cvc_doctors = {}
        cvc_comp_by_access = {}
        cvc_comp_by_doctor = {}
        cvc_usage_comp_by_doctor = {}
        cvc_attempts = []
        cvc_lumens = []
        cvc_lengths = []
        cvc_diameters = []
        cvc_dwell_minutes = []
        cvc_technical_complications = 0
        cvc_usage_complications = 0
        cvc_any_complications = 0

        for cvc in cvc_procedures:
            self._inc_counter(cvc_accesses, cvc["access"])
            self._inc_counter(cvc_statuses, cvc["status"])
            self._inc_counter(cvc_doctors, cvc["doctor"])

            for target, key in (
                (cvc_attempts, "attempts_count"),
                (cvc_lumens, "lumens_count"),
                (cvc_lengths, "length_cm"),
                (cvc_diameters, "diameter_f"),
            ):
                value = cvc.get(key)
                if value is not None:
                    parsed = self._safe_float(value, default=0.0)
                    if parsed > 0:
                        target.append(parsed)
            dwell_minutes = cvc.get("dwell_minutes")
            if dwell_minutes is not None:
                parsed_dwell = self._safe_float(dwell_minutes, default=0.0)
                if parsed_dwell >= 0:
                    cvc_dwell_minutes.append(parsed_dwell)

            has_technical = bool(cvc.get("technical_complication"))
            has_usage = bool(cvc.get("usage_complication"))
            if has_technical:
                cvc_technical_complications += 1
            if has_usage:
                cvc_usage_complications += 1
                self._inc_counter(cvc_usage_comp_by_doctor, cvc.get("removal_doctor") or cvc["doctor"])
            if has_technical or has_usage:
                cvc_any_complications += 1
                self._inc_counter(cvc_comp_by_access, cvc["access"])
                self._inc_counter(cvc_comp_by_doctor, cvc["doctor"])

        lp_accesses = {}
        lp_levels = {}
        lp_results = {}
        lp_doctors = {}
        lp_comp_by_access = {}
        lp_comp_by_level = {}
        lp_comp_by_doctor = {}
        lp_complications = 0
        for lp in lumbar_punctures:
            self._inc_counter(lp_accesses, lp["access"])
            self._inc_counter(lp_levels, lp["level"])
            self._inc_counter(lp_results, lp["result"])
            self._inc_counter(lp_doctors, lp["doctor"])
            if lp.get("complication"):
                lp_complications += 1
                self._inc_counter(lp_comp_by_access, lp["access"])
                self._inc_counter(lp_comp_by_level, lp["level"])
                self._inc_counter(lp_comp_by_doctor, lp["doctor"])

        protocol_types = {}
        protocol_indications = {}
        protocol_components = {}
        protocol_doctors = {}
        procedure_transfusion_volume_by_type = {}
        procedure_transfusion_reactions = 0
        procedure_transfusion_reactions_by_doctor = {}
        for transfusion in procedure_transfusions:
            transf_type = transfusion["type"]
            volume = self._safe_float(transfusion.get("volume_ml"), default=0.0)
            self._inc_counter(protocol_types, transf_type)
            self._inc_counter(protocol_indications, transfusion["indication"])
            self._inc_counter(protocol_components, transfusion["component"])
            self._inc_counter(protocol_doctors, transfusion["doctor"])
            bucket = procedure_transfusion_volume_by_type.setdefault(transf_type, {"count": 0, "volume": 0.0})
            bucket["count"] += 1
            bucket["volume"] += volume
            if transfusion.get("reaction"):
                procedure_transfusion_reactions += 1
                self._inc_counter(procedure_transfusion_reactions_by_doctor, transfusion["doctor"])

        protocol_blood = procedure_transfusion_volume_by_type.get("Кровь", {})
        protocol_plasma = procedure_transfusion_volume_by_type.get("Плазма", {})
        protocol_other_units = sum(
            int(values.get("count") or 0)
            for name, values in procedure_transfusion_volume_by_type.items()
            if name not in {"Кровь", "Плазма"}
        )
        protocol_other_volume = sum(
            float(values.get("volume") or 0.0)
            for name, values in procedure_transfusion_volume_by_type.items()
            if name not in {"Кровь", "Плазма"}
        )

        cvc_count = len(cvc_procedures)
        lp_count = len(lumbar_punctures)
        procedure_transfusion_count = len(procedure_transfusions)
        return {
            "cvc_count": cvc_count,
            "cvc_patients": len({row["admission_id"] for row in cvc_procedures}),
            "cvc_accesses": cvc_accesses,
            "cvc_statuses": cvc_statuses,
            "cvc_doctors": cvc_doctors,
            "cvc_avg_attempts": self._safe_div(sum(cvc_attempts), len(cvc_attempts)) if cvc_attempts else None,
            "cvc_avg_lumens": self._safe_div(sum(cvc_lumens), len(cvc_lumens)) if cvc_lumens else None,
            "cvc_avg_length": self._safe_div(sum(cvc_lengths), len(cvc_lengths)) if cvc_lengths else None,
            "cvc_avg_diameter": self._safe_div(sum(cvc_diameters), len(cvc_diameters)) if cvc_diameters else None,
            "cvc_closed_count": len(cvc_dwell_minutes),
            "cvc_total_dwell_days": sum(cvc_dwell_minutes) / 1440.0,
            "cvc_avg_dwell_days": self._safe_div(sum(cvc_dwell_minutes), len(cvc_dwell_minutes)) / 1440.0 if cvc_dwell_minutes else None,
            "cvc_technical_complications": cvc_technical_complications,
            "cvc_technical_no_complications": max(0, cvc_count - cvc_technical_complications),
            "cvc_usage_complications": cvc_usage_complications,
            "cvc_usage_no_complications": max(0, cvc_count - cvc_usage_complications),
            "cvc_any_complications": cvc_any_complications,
            "cvc_comp_by_access": cvc_comp_by_access,
            "cvc_comp_by_doctor": cvc_comp_by_doctor,
            "cvc_usage_comp_by_doctor": cvc_usage_comp_by_doctor,
            "lp_count": lp_count,
            "lp_patients": len({row["admission_id"] for row in lumbar_punctures}),
            "lp_accesses": lp_accesses,
            "lp_levels": lp_levels,
            "lp_results": lp_results,
            "lp_doctors": lp_doctors,
            "lp_complications": lp_complications,
            "lp_no_complications": max(0, lp_count - lp_complications),
            "lp_comp_by_access": lp_comp_by_access,
            "lp_comp_by_level": lp_comp_by_level,
            "lp_comp_by_doctor": lp_comp_by_doctor,
            "procedure_transfusion_count": procedure_transfusion_count,
            "procedure_transfusion_patients": len({row["admission_id"] for row in procedure_transfusions}),
            "procedure_transfusion_volume": sum(
                self._safe_float(row.get("volume_ml"), default=0.0) for row in procedure_transfusions
            ),
            "procedure_transfusion_types": protocol_types,
            "procedure_transfusion_indications": protocol_indications,
            "procedure_transfusion_components": protocol_components,
            "procedure_transfusion_doctors": protocol_doctors,
            "procedure_transfusion_volume_by_type": procedure_transfusion_volume_by_type,
            "procedure_blood_units": int(protocol_blood.get("count") or 0),
            "procedure_blood_volume": float(protocol_blood.get("volume") or 0.0),
            "procedure_plasma_units": int(protocol_plasma.get("count") or 0),
            "procedure_plasma_volume": float(protocol_plasma.get("volume") or 0.0),
            "procedure_other_transfusion_units": protocol_other_units,
            "procedure_other_transfusion_volume": protocol_other_volume,
            "procedure_transfusion_reactions": procedure_transfusion_reactions,
            "procedure_transfusion_no_reactions": max(0, procedure_transfusion_count - procedure_transfusion_reactions),
            "procedure_transfusion_reactions_by_doctor": procedure_transfusion_reactions_by_doctor,
        }

    def _ivl_stats(self, ivl_episodes, death_ids):
        ivl_adm_ids = {e["admission_id"] for e in ivl_episodes}
        ivl_hours = sum(e["duration_hours"] for e in ivl_episodes)
        return {
            "ivl_adm_ids": ivl_adm_ids,
            "n_ivl": len(ivl_adm_ids),
            "ivl_episodes_count": len(ivl_episodes),
            "ivl_days": ivl_hours / 24.0,
            "deaths_ivl": len(death_ids.intersection(ivl_adm_ids)),
        }

    def _period_bed_stats(self, bed_days, total_n):
        period_days = max(1, (self._end_dt.date() - self._start_dt.date()).days + 1)
        beds = self._statistics_beds()
        bed_fund = beds * period_days
        return {
            "period_days": period_days,
            "beds": beds,
            "bed_fund": bed_fund,
            "occupancy": self._pct(bed_days, bed_fund),
            "bed_utilization_days": self._safe_div(bed_days, beds),
            "turnover": self._safe_div(total_n, beds),
            "bti": self._safe_div((bed_fund - bed_days), total_n),
        }

    def _statistics_beds(self):
        return STATISTICAL_BED_COUNT

    def _admission_active_on_day(self, admission, day_start, day_end):
        adm_start = admission["admission_dt"]
        end_candidates = [self._end_dt]
        if admission["transfer_dt"] is not None:
            end_candidates.append(admission["transfer_dt"])
        if admission["death_dt"] is not None:
            end_candidates.append(admission["death_dt"])
        adm_end = min(end_candidates)
        return adm_start < day_end and adm_end > day_start

    def _daily_load_stats(self, admissions, bed_days, period_days, beds):
        mean_patients = self._safe_div(bed_days, period_days)
        daily_counts = []
        for day_idx in range(period_days):
            day_start = datetime.combine(self._start_dt.date(), datetime.min.time()) + timedelta(days=day_idx)
            day_end = day_start + timedelta(days=1)
            count = 0
            for admission in admissions:
                if self._admission_active_on_day(admission, day_start, day_end):
                    count += 1
            daily_counts.append(count)

        max_patients = max(daily_counts) if daily_counts else 0
        threshold = STATISTICAL_HIGH_LOAD_THRESHOLD
        high_load_periods = sum(1 for c in daily_counts if c >= threshold)
        return {
            "mean_patients": mean_patients,
            "utilization": self._safe_div(mean_patients, beds),
            "max_patients": max_patients,
            "load_threshold": threshold,
            "load_time_pct": self._pct(high_load_periods, period_days),
        }

    def _intensity_indexes(self, *, n_ivl, n_surg, n_transf, total_n, early_deaths, deaths, n_with_interventions, ivl_days, operations_count, transfusion_units, bed_days, period_days, max_patients, beds):
        return {
            "intensity_index": self._safe_div((n_ivl + n_surg + n_transf), total_n),
            "severity_index": self._safe_div(early_deaths, deaths),
            "technology_index": self._pct(n_with_interventions, total_n),
            "resource_use_index": self._safe_div((ivl_days + operations_count + transfusion_units), bed_days),
            "throughput": self._safe_div(total_n, period_days),
            "load_coefficient": self._safe_div(max_patients, beds),
        }

    def _calculate_statistics(self):
        context = self._fetch_context()
        admissions = context["admissions"]
        operations_adm_ids = context["operations_adm_ids"]
        transfusions = context["transfusions"]
        ivl_episodes = context["ivl_episodes"]
        cvc_procedures = context["cvc_procedures"]
        lumbar_punctures = context["lumbar_punctures"]
        procedure_transfusions = context["procedure_transfusions"]

        population = self._population_stats(admissions)
        total_n = population["total_n"]
        death_ids = population["death_ids"]
        deaths = len(death_ids)

        los = self._los_stats(admissions, total_n)
        age = self._age_stats(admissions)
        death_timing = self._death_timing_stats(admissions)
        distributions = self._distribution_stats(admissions)
        operations = self._operation_stats(operations_adm_ids, death_ids)
        transfusion = self._transfusion_stats(transfusions, death_ids)
        procedures = self._procedure_stats(cvc_procedures, lumbar_punctures, procedure_transfusions)
        ivl = self._ivl_stats(ivl_episodes, death_ids)
        bed_period = self._period_bed_stats(los["bed_days"], total_n)
        load = self._daily_load_stats(admissions, los["bed_days"], bed_period["period_days"], bed_period["beds"])

        n_with_interventions = len(
            ivl["ivl_adm_ids"].union(operations["surg_adm_ids"]).union(transfusion["transf_adm_ids"])
        )
        indexes = self._intensity_indexes(
            n_ivl=ivl["n_ivl"],
            n_surg=operations["n_surg"],
            n_transf=transfusion["n_transf"],
            total_n=total_n,
            early_deaths=death_timing["early_deaths"],
            deaths=deaths,
            n_with_interventions=n_with_interventions,
            ivl_days=ivl["ivl_days"],
            operations_count=operations["operations_count"],
            transfusion_units=transfusion["transfusion_units"],
            bed_days=los["bed_days"],
            period_days=bed_period["period_days"],
            max_patients=load["max_patients"],
            beds=bed_period["beds"],
        )

        return {
            "N": total_n,
            "N_unique": population["unique_patients"],
            "bed_days": los["bed_days"],
            "alos": los["alos"],
            "los_median": los["los_median"],
            "los_min": los["los_min"],
            "los_max": los["los_max"],
            "beds": bed_period["beds"],
            "period_days": bed_period["period_days"],
            "bed_fund": bed_period["bed_fund"],
            "occupancy": bed_period["occupancy"],
            "bed_utilization_days": bed_period["bed_utilization_days"],
            "turnover": bed_period["turnover"],
            "bti": bed_period["bti"],
            "mean_age": age["mean_age"],
            "median_age": age["median_age"],
            "age_groups": distributions["age_groups"],
            "genders": distributions["genders"],
            "months": distributions["months"],
            "weekdays": distributions["weekdays"],
            "sources": distributions["sources"],
            "diagnoses": distributions["diagnoses"],
            "mkb_classes": distributions["mkb_classes"],
            "deaths": deaths,
            "mortality_pct": self._pct(deaths, total_n),
            "mortality_per_1000_bed_days": self._safe_div(deaths, los["bed_days"]) * 1000.0,
            "outcomes": distributions["outcomes"],
            "mean_time_to_death_days": death_timing["mean_time_to_death_days"],
            "median_time_to_death_days": death_timing["median_time_to_death_days"],
            "early_deaths": death_timing["early_deaths"],
            "deaths_1_3_days": death_timing["deaths_1_3_days"],
            "deaths_3_7_days": death_timing["deaths_3_7_days"],
            "deaths_ge_7_days": death_timing["deaths_ge_7_days"],
            "age_groups_deaths": distributions["age_groups_deaths"],
            "death_doctors": distributions["death_doctors"],
            "N_IVL": ivl["n_ivl"],
            "ivl_episodes_count": ivl["ivl_episodes_count"],
            "ivl_days": ivl["ivl_days"],
            "deaths_ivl": ivl["deaths_ivl"],
            "N_surg": operations["n_surg"],
            "operations_count": operations["operations_count"],
            "deaths_surg": operations["deaths_surg"],
            "N_transf": transfusion["n_transf"],
            "transfusion_units": transfusion["transfusion_units"],
            "volume_total": transfusion["volume_total"],
            "transf_by_type": transfusion["transf_by_type"],
            "transf_volume_by_type": transfusion["transf_volume_by_type"],
            "blood_units": transfusion["blood_units"],
            "blood_volume": transfusion["blood_volume"],
            "plasma_units": transfusion["plasma_units"],
            "plasma_volume": transfusion["plasma_volume"],
            "other_transfusion_units": transfusion["other_transfusion_units"],
            "other_transfusion_volume": transfusion["other_transfusion_volume"],
            "deaths_transf": transfusion["deaths_transf"],
            "cvc_count": procedures["cvc_count"],
            "cvc_patients": procedures["cvc_patients"],
            "cvc_accesses": procedures["cvc_accesses"],
            "cvc_statuses": procedures["cvc_statuses"],
            "cvc_doctors": procedures["cvc_doctors"],
            "cvc_avg_attempts": procedures["cvc_avg_attempts"],
            "cvc_avg_lumens": procedures["cvc_avg_lumens"],
            "cvc_avg_length": procedures["cvc_avg_length"],
            "cvc_avg_diameter": procedures["cvc_avg_diameter"],
            "cvc_closed_count": procedures["cvc_closed_count"],
            "cvc_total_dwell_days": procedures["cvc_total_dwell_days"],
            "cvc_avg_dwell_days": procedures["cvc_avg_dwell_days"],
            "cvc_technical_complications": procedures["cvc_technical_complications"],
            "cvc_technical_no_complications": procedures["cvc_technical_no_complications"],
            "cvc_usage_complications": procedures["cvc_usage_complications"],
            "cvc_usage_no_complications": procedures["cvc_usage_no_complications"],
            "cvc_any_complications": procedures["cvc_any_complications"],
            "cvc_comp_by_access": procedures["cvc_comp_by_access"],
            "cvc_comp_by_doctor": procedures["cvc_comp_by_doctor"],
            "cvc_usage_comp_by_doctor": procedures["cvc_usage_comp_by_doctor"],
            "lp_count": procedures["lp_count"],
            "lp_patients": procedures["lp_patients"],
            "lp_accesses": procedures["lp_accesses"],
            "lp_levels": procedures["lp_levels"],
            "lp_results": procedures["lp_results"],
            "lp_doctors": procedures["lp_doctors"],
            "lp_complications": procedures["lp_complications"],
            "lp_no_complications": procedures["lp_no_complications"],
            "lp_comp_by_access": procedures["lp_comp_by_access"],
            "lp_comp_by_level": procedures["lp_comp_by_level"],
            "lp_comp_by_doctor": procedures["lp_comp_by_doctor"],
            "procedure_transfusion_count": procedures["procedure_transfusion_count"],
            "procedure_transfusion_patients": procedures["procedure_transfusion_patients"],
            "procedure_transfusion_volume": procedures["procedure_transfusion_volume"],
            "procedure_transfusion_types": procedures["procedure_transfusion_types"],
            "procedure_transfusion_indications": procedures["procedure_transfusion_indications"],
            "procedure_transfusion_components": procedures["procedure_transfusion_components"],
            "procedure_transfusion_doctors": procedures["procedure_transfusion_doctors"],
            "procedure_transfusion_volume_by_type": procedures["procedure_transfusion_volume_by_type"],
            "procedure_blood_units": procedures["procedure_blood_units"],
            "procedure_blood_volume": procedures["procedure_blood_volume"],
            "procedure_plasma_units": procedures["procedure_plasma_units"],
            "procedure_plasma_volume": procedures["procedure_plasma_volume"],
            "procedure_other_transfusion_units": procedures["procedure_other_transfusion_units"],
            "procedure_other_transfusion_volume": procedures["procedure_other_transfusion_volume"],
            "procedure_transfusion_reactions": procedures["procedure_transfusion_reactions"],
            "procedure_transfusion_no_reactions": procedures["procedure_transfusion_no_reactions"],
            "procedure_transfusion_reactions_by_doctor": procedures["procedure_transfusion_reactions_by_doctor"],
            "IVL_index": self._safe_div(ivl["n_ivl"], total_n),
            "Surgery_index": self._safe_div(operations["n_surg"], total_n),
            "Transfusion_index": self._safe_div(transfusion["n_transf"], total_n),
            "mean_patients": load["mean_patients"],
            "utilization": load["utilization"],
            "max_patients": load["max_patients"],
            "load_threshold": load["load_threshold"],
            "load_time_pct": load["load_time_pct"],
            "intensity_index": indexes["intensity_index"],
            "severity_index": indexes["severity_index"],
            "long_stay_pct": self._pct(los["long_stay_count"], total_n),
            "technology_index": indexes["technology_index"],
            "resource_use_index": indexes["resource_use_index"],
            "throughput": indexes["throughput"],
            "load_coefficient": indexes["load_coefficient"],
        }

    def _build_recovery_summary_rows(self):
        manager, cleanup = _thread_local_manager(self.db_manager)
        conn = manager.get_connection()
        try:
            summary = build_recovery_bed_summary(conn, self.start_date_str, self.end_date_str)
            return build_recovery_summary_rows(
                summary,
                include_recovery_beds=self.include_recovery_beds,
            )
        finally:
            if cleanup:
                cleanup()

    def _section_rows(self, section_key: str, s: dict):
        total_n = s["N"]
        deaths = s["deaths"]

        if section_key == RECOVERY_SECTION_KEY:
            return self._build_recovery_summary_rows()

        if section_key == "s1":
            return [
                ("1.1 Уникальные пациенты", "Число уникальных пациентов", self._fmt_num(s["N_unique"], 0)),
                ("1.2 Госпитализации", "Число госпитализаций", self._fmt_num(total_n, 0)),
                ("1.3 Койко-дни", "Сумма дней пребывания всех госпитализаций", self._fmt_num(s["bed_days"])),
                ("1.4 Средняя длительность лечения", "Средняя длительность = Койко-дни / Госпитализации", self._fmt_num(s["alos"])),
                ("1.5 Медиана длительности лечения", "Медиана длительности лечения", self._fmt_num(s["los_median"])),
                ("1.6 Минимальная длительность лечения", "Минимальная длительность лечения", self._fmt_num(s["los_min"])),
                ("1.7 Максимальная длительность лечения", "Максимальная длительность лечения", self._fmt_num(s["los_max"])),
            ]

        if section_key == "s2":
            return [
                ("2.1 Количество коек", "Койки", self._fmt_num(s["beds"], 0)),
                ("2.2 Коечный фонд", "Коечный фонд = Койки × Дни", self._fmt_num(s["bed_fund"])),
                ("2.3 Занятость (%)", "Занятость = Койко-дни / Коечный фонд × 100%", f"{self._fmt_num(s['occupancy'])}%"),
                ("2.4 Средняя занятость койки", "Средняя занятость койки (дни) = Койко-дни / Койки", self._fmt_num(s["bed_utilization_days"])),
                ("2.5 Оборот койки", "Оборот койки = Госпитализации / Койки", self._fmt_num(s["turnover"])),
                ("2.6 Простой койки", "Простой койки = (Коечный фонд − Койко-дни) / Госпитализации", self._fmt_num(s["bti"])),
            ]

        if section_key == "s3":
            age_lines = self._distribution_lines(
                s["age_groups"],
                total_n,
                forced_order=["до 1 года", "1–17", "18–44", "45–60", "61–75", "75+", "Не указан"],
            )
            gender_lines = self._distribution_lines(s["genders"], total_n)
            return [
                ("3.1 Средний возраст", "Средний возраст = Сумма возрастов / Госпитализации", self._fmt_num(s["mean_age"])),
                ("3.2 Медианный возраст", "Медианный возраст", self._fmt_num(s["median_age"])),
                ("3.3 Возрастные группы", "Доля = Число пациентов в группе / Госпитализации × 100%", age_lines),
                ("3.4 Пол", "Доля = Число пациентов в группе / Госпитализации × 100%", gender_lines),
            ]

        if section_key == "s4":
            weekday_order = [
                "Понедельник",
                "Вторник",
                "Среда",
                "Четверг",
                "Пятница",
                "Суббота",
                "Воскресенье",
            ]
            return [
                ("4.1 По месяцам", "Доля = Число госпитализаций в группе / Госпитализации × 100%", self._distribution_lines(s["months"], total_n)),
                ("4.2 По дням недели", "Доля = Число госпитализаций в группе / Госпитализации × 100%", self._distribution_lines(s["weekdays"], total_n, forced_order=weekday_order)),
                ("4.3 По источнику", "Доля = Число госпитализаций в группе / Госпитализации × 100%", self._distribution_lines(s["sources"], total_n)),
            ]

        if section_key == "s5":
            return [
                ("5.1 Частота диагнозов", "Частота = Число диагнозов в группе / Госпитализации × 100%", self._distribution_lines(s["diagnoses"], total_n)),
                ("5.2 По классам МКБ", "Частота = Число диагнозов в группе / Госпитализации × 100%", self._distribution_lines(s["mkb_classes"], total_n)),
            ]

        if section_key == "s6":
            return [
                ("6.1 Абсолютная летальность", "Умершие", self._fmt_num(deaths, 0)),
                ("6.2 Летальность (%)", "Летальность = Умершие / Госпитализации × 100%", f"{self._fmt_num(s['mortality_pct'])}%"),
                ("6.3 На 1000 койко-дней", "Летальность = Умершие / Койко-дни × 1000", self._fmt_num(s["mortality_per_1000_bed_days"])),
                ("6.4 Исходы", "Доля = Число исходов в группе / Госпитализации × 100%", self._distribution_lines(s["outcomes"], total_n)),
                ("6.5 Смерти по врачу", "Врач из протокола установления смерти: смерти врача / все смерти; смерти врача / госпитализации", self._death_doctor_lines(s["death_doctors"], deaths, total_n)),
            ]

        if section_key == "s7":
            return [
                ("7.1 Среднее время до смерти", "Среднее время = Сумма времени до смерти / Умершие", self._fmt_num(s["mean_time_to_death_days"])),
                ("7.2 Медиана времени до смерти", "Медиана времени", self._fmt_num(s["median_time_to_death_days"])),
                ("7.3 Ранняя летальность", "Ранняя летальность = Смерти <24ч / Умершие × 100%", self._fmt_pct(s["early_deaths"], deaths)),
                ("7.4 1–3 суток", "Доля = Число смертей в интервале / Умершие × 100%", self._fmt_pct(s["deaths_1_3_days"], deaths)),
                ("7.5 3–7 суток", "Доля = Число смертей в интервале / Умершие × 100%", self._fmt_pct(s["deaths_3_7_days"], deaths)),
                ("7.6 ≥7 суток", "Доля = Число смертей в интервале / Умершие × 100%", self._fmt_pct(s["deaths_ge_7_days"], deaths)),
            ]

        if section_key == "s8":
            lines = self._distribution_mortality_lines(s["age_groups"], s["age_groups_deaths"])
            return [
                ("8.1 Летальность по группам", "Летальность группы = Умершие в группе / Пациенты в группе × 100%", lines),
                ("8.2 Смертность по врачу протокола", "Врач из протокола установления смерти: смерти врача / все смерти; смерти врача / госпитализации", self._death_doctor_lines(s["death_doctors"], deaths, total_n)),
            ]

        if section_key == "s9":
            return [
                ("10.1 Пациенты на ИВЛ", "Пациенты с ИВЛ", self._fmt_num(s["N_IVL"], 0)),
                ("10.2 Доля пациентов на ИВЛ", "Доля ИВЛ = Пациенты с ИВЛ / Госпитализации × 100%", self._fmt_pct(s["N_IVL"], total_n)),
                ("10.3 Эпизоды ИВЛ", "Эпизоды", self._fmt_num(s["ivl_episodes_count"], 0)),
                ("10.4 Средняя длительность ИВЛ", "Средняя ИВЛ = ИВЛ-дни / Пациенты с ИВЛ", self._fmt_num(self._safe_div(s["ivl_days"], s["N_IVL"]))),
                ("10.5 ИВЛ-дни", "ИВЛ-дни", self._fmt_num(s["ivl_days"])),
                ("10.6 Летальность на ИВЛ", "Летальность ИВЛ = Умершие на ИВЛ / Пациенты с ИВЛ × 100%", self._fmt_pct(s["deaths_ivl"], s["N_IVL"])),
            ]

        if section_key == "s10":
            return [
                ("11.1 Пациенты с операциями", "Пациенты с операциями", self._fmt_num(s["N_surg"], 0)),
                ("11.2 Операции", "Операции", self._fmt_num(s["operations_count"], 0)),
                ("11.3 Частота операций", "Частота = Пациенты с операциями / Госпитализации", self._fmt_num(self._safe_div(s["N_surg"], total_n))),
                ("11.4 Летальность у оперированных", "Летальность = Умершие после операций / Пациенты с операциями × 100%", self._fmt_pct(s["deaths_surg"], s["N_surg"])),
            ]

        if section_key == "s11":
            return [
                ("12.1 Число переливаний", "Переливания", self._fmt_num(s["transfusion_units"], 0)),
                ("12.2 Пациенты с переливаниями", "Пациенты с переливаниями", self._fmt_num(s["N_transf"], 0)),
                ("12.3 Общий объем, мл", "Общий объем переливаний", self._fmt_num(s["volume_total"])),
                ("12.4 Средний объем дозы, мл", "Средняя доза = Общий объем переливаний / Число доз", self._fmt_num(self._safe_div(s["volume_total"], s["transfusion_units"]))),
                ("12.5 Летальность при переливаниях", "Летальность = Умершие после переливаний / Пациенты с переливаниями × 100%", self._fmt_pct(s["deaths_transf"], s["N_transf"])),
                ("12.6 Перелито крови", "Количество доз / суммарный объем крови", self._count_volume_text(s["blood_units"], s["blood_volume"])),
                ("12.7 Перелито плазмы", "Количество доз / суммарный объем плазмы", self._count_volume_text(s["plasma_units"], s["plasma_volume"])),
                ("12.8 Прочие компоненты", "Количество доз / суммарный объем прочих компонентов", self._count_volume_text(s["other_transfusion_units"], s["other_transfusion_volume"])),
                ("12.9 По типам", "Доля = Число доз данного типа / Число доз × 100%; указан суммарный объем", self._count_volume_distribution_lines(s["transf_volume_by_type"], s["transfusion_units"])),
                ("12.10 Протоколы гемотрансфузий", "Сохраненные выполненные протоколы гемотрансфузий", self._fmt_num(s["procedure_transfusion_count"], 0)),
                ("12.11 Кровь по протоколам", "Количество протоколов / суммарный объем крови", self._count_volume_text(s["procedure_blood_units"], s["procedure_blood_volume"])),
                ("12.12 Плазма по протоколам", "Количество протоколов / суммарный объем плазмы", self._count_volume_text(s["procedure_plasma_units"], s["procedure_plasma_volume"])),
                ("12.13 Реакции при гемотрансфузиях", "Реакции = протоколы с симптомами/тяжестью реакции", f"{self._fmt_num(s['procedure_transfusion_reactions'], 0)} / {self._fmt_num(s['procedure_transfusion_count'], 0)}"),
                ("12.14 Реакции по врачам", "Врач-исполнитель протокола гемотрансфузии", self._distribution_lines(s["procedure_transfusion_reactions_by_doctor"], s["procedure_transfusion_reactions"])),
            ]

        if section_key == "s12":
            return [
                ("13.1 Установлено ЦВК", "Выполненные/активные процедуры ЦВК", self._fmt_num(s["cvc_count"], 0)),
                ("13.2 Пациенты с ЦВК", "Уникальные госпитализации с ЦВК", self._fmt_num(s["cvc_patients"], 0)),
                ("13.3 Локализации ЦВК", "Доля = число ЦВК в локализации / все ЦВК × 100%", self._distribution_lines(s["cvc_accesses"], s["cvc_count"])),
                ("13.4 Статус катетера", "Доля = число катетеров в статусе / все ЦВК × 100%", self._distribution_lines(s["cvc_statuses"], s["cvc_count"])),
                ("13.5 Врачи-исполнители", "Доля = число ЦВК врача / все ЦВК × 100%", self._distribution_lines(s["cvc_doctors"], s["cvc_count"])),
                ("13.6 Среднее число попыток", "Среднее число попыток среди заполненных протоколов", self._fmt_num(s["cvc_avg_attempts"])),
                ("13.7 Среднее число просветов", "Среднее число просветов среди заполненных протоколов", self._fmt_num(s["cvc_avg_lumens"])),
                ("13.8 Средняя длина катетера, см", "Средняя длина среди заполненных протоколов", self._fmt_num(s["cvc_avg_length"])),
                ("13.9 Средний диаметр, F", "Средний диаметр среди заполненных протоколов", self._fmt_num(s["cvc_avg_diameter"])),
                ("13.10 ЦВК с рассчитанной длительностью", "Катетеры с временем окончания пребывания", self._fmt_num(s["cvc_closed_count"], 0)),
                ("13.11 Суммарная длительность ЦВК, дней", "Сумма времени от установки до удаления/переустановки/исхода", self._fmt_num(s["cvc_total_dwell_days"])),
                ("13.12 Средняя длительность ЦВК, дней", "Среднее время от установки до удаления/переустановки/исхода", self._fmt_num(s["cvc_avg_dwell_days"])),
            ]

        if section_key == "s13":
            return [
                ("14.1 Выполнено люмбальных пункций", "Выполненные процедуры люмбальной пункции", self._fmt_num(s["lp_count"], 0)),
                ("14.2 Пациенты с пункциями", "Уникальные госпитализации с люмбальной пункцией", self._fmt_num(s["lp_patients"], 0)),
                ("14.3 Результат пункции", "Доля = число результатов / все пункции × 100%", self._distribution_lines(s["lp_results"], s["lp_count"])),
                ("14.4 Доступ", "Доля = число доступов / все пункции × 100%", self._distribution_lines(s["lp_accesses"], s["lp_count"])),
                ("14.5 Уровень пункции", "Доля = число уровней / все пункции × 100%", self._distribution_lines(s["lp_levels"], s["lp_count"])),
                ("14.6 Врачи-исполнители", "Доля = число пункций врача / все пункции × 100%", self._distribution_lines(s["lp_doctors"], s["lp_count"])),
            ]

        if section_key == "s14":
            return [
                ("15.1 Осложнения при постановке ЦВК", "Осложнения / все ЦВК", f"{self._fmt_num(s['cvc_technical_complications'], 0)} / {self._fmt_num(s['cvc_count'], 0)}"),
                ("15.2 Без осложнений при постановке ЦВК", "Без осложнений / все ЦВК", f"{self._fmt_num(s['cvc_technical_no_complications'], 0)} / {self._fmt_num(s['cvc_count'], 0)}"),
                ("15.3 Осложнения при использовании/удалении ЦВК", "Осложнения / все ЦВК", f"{self._fmt_num(s['cvc_usage_complications'], 0)} / {self._fmt_num(s['cvc_count'], 0)}"),
                ("15.4 ЦВК с любыми осложнениями", "Уникальные ЦВК с осложнениями постановки или использования", self._fmt_num(s["cvc_any_complications"], 0)),
                ("15.5 Осложнения ЦВК по локализации", "Локализация выбранного доступа при осложнении", self._distribution_lines(s["cvc_comp_by_access"], s["cvc_any_complications"])),
                ("15.6 Осложнения ЦВК по врачу-исполнителю", "Врач-исполнитель ЦВК, где отмечено осложнение", self._distribution_lines(s["cvc_comp_by_doctor"], s["cvc_any_complications"])),
                ("15.7 Осложнения удаления/использования ЦВК по врачу", "Врач удаления/переустановки; если не указан, врач-исполнитель ЦВК", self._distribution_lines(s["cvc_usage_comp_by_doctor"], s["cvc_usage_complications"])),
                ("15.8 Осложнения люмбальных пункций", "Осложнения / все люмбальные пункции", f"{self._fmt_num(s['lp_complications'], 0)} / {self._fmt_num(s['lp_count'], 0)}"),
                ("15.9 Без осложнений люмбальных пункций", "Без осложнений / все люмбальные пункции", f"{self._fmt_num(s['lp_no_complications'], 0)} / {self._fmt_num(s['lp_count'], 0)}"),
                ("15.10 Осложнения пункций по доступу", "Доступ при осложненной люмбальной пункции", self._distribution_lines(s["lp_comp_by_access"], s["lp_complications"])),
                ("15.11 Осложнения пункций по уровню", "Уровень при осложненной люмбальной пункции", self._distribution_lines(s["lp_comp_by_level"], s["lp_complications"])),
                ("15.12 Осложнения пункций по врачу", "Врач-исполнитель люмбальной пункции", self._distribution_lines(s["lp_comp_by_doctor"], s["lp_complications"])),
                ("15.13 Реакции гемотрансфузий", "Реакции / все выполненные протоколы гемотрансфузии", f"{self._fmt_num(s['procedure_transfusion_reactions'], 0)} / {self._fmt_num(s['procedure_transfusion_count'], 0)}"),
                ("15.14 Реакции гемотрансфузий по врачу", "Врач-исполнитель протокола гемотрансфузии", self._distribution_lines(s["procedure_transfusion_reactions_by_doctor"], s["procedure_transfusion_reactions"])),
            ]

        if section_key == "s16":
            return [
                ("17.1 ИВЛ на пациента", "Индекс ИВЛ = Пациенты с ИВЛ / Госпитализации", self._fmt_num(s["IVL_index"])),
                ("17.2 Операции на пациента", "Индекс операций = Пациенты с операциями / Госпитализации", self._fmt_num(s["Surgery_index"])),
                ("17.3 Переливания на пациента", "Индекс переливаний = Пациенты с переливаниями / Госпитализации", self._fmt_num(s["Transfusion_index"])),
            ]

        if section_key == "s17":
            return [
                ("18.1 Оборот койки", "Оборот койки = Госпитализации / Койки", self._fmt_num(s["turnover"])),
                ("18.2 Койко-дни на пациента", "Средняя длительность лечения", self._fmt_num(s["alos"])),
            ]

        if section_key == "s18":
            return [
                ("19.1 Доля ранней летальности", "Ранняя летальность = Смерти <24ч / Умершие × 100%", self._fmt_pct(s["early_deaths"], deaths)),
                ("19.2 Индекс интенсивности лечения", "Индекс интенсивности = (Пациенты с ИВЛ + Пациенты с операциями + Пациенты с переливаниями) / Госпитализации", self._fmt_num(s["intensity_index"])),
                ("19.3 Индекс тяжести потока", "Индекс тяжести = Ранние смерти / Умершие", self._fmt_num(s["severity_index"])),
                ("19.4 Длительное пребывание", "Длительное пребывание = Госпитализации дольше 7 суток / Госпитализации × 100%", f"{self._fmt_num(s['long_stay_pct'])}%"),
                ("19.5 Индекс технологичности", "Индекс технологичности = Госпитализации с вмешательствами / Госпитализации × 100%", f"{self._fmt_num(s['technology_index'])}%"),
            ]

        if section_key == "s19":
            return [
                ("20.1 Среднесуточная занятость", "Среднее число пациентов в отделении за сутки = Койко-дни / Дни периода", self._fmt_num(s["mean_patients"])),
                ("20.2 Использование коек", "Использование коек = Среднесуточная занятость / Количество коек", self._fmt_num(s["utilization"])),
                ("20.3 Максимальная загрузка", "Максимум пациентов одновременно", self._fmt_num(s["max_patients"], 0)),
                (
                    "20.4 Высокая нагрузка",
                    "Время высокой нагрузки = Время ≥ порог / Общее время × 100%",
                    f"{self._fmt_num(s['load_time_pct'])}% (порог: ≥{self._fmt_num(s['load_threshold'], 0)} пациентов)",
                ),
            ]

        if section_key == "sx":
            return [
                ("Индекс использования ресурсов", "(ИВЛ-дни + Операции + Переливания) / Койко-дни", self._fmt_num(s["resource_use_index"])),
                ("Пропускная способность", "Госпитализации / Дни периода", self._fmt_num(s["throughput"])),
                ("Косвенный показатель тяжести", "Ранняя летальность = Смерти <24ч / Умершие × 100%", self._fmt_pct(s["early_deaths"], deaths)),
                ("Коэффициент пиковой нагрузки", "Пик пациентов / Койки", self._fmt_num(s["load_coefficient"])),
            ]

        return []

    def _section_title(self, section_key: str):
        for _group, items in self.section_groups.items():
            if section_key in items:
                return items[section_key]
        return section_key

    def _render_sections_html(self, selected_sections: list[str], s: dict):
        sections_html = []

        def _format_value_cell(value):
            if value is None:
                return "—"
            text = str(value).strip()
            if not text:
                return "—"
            # Для распределений оставляем переносы строк в HTML.
            if "<br/>" in text:
                return text
            return escape(text)

        for section_key in selected_sections:
            rows = self._section_rows(section_key, s)
            if not rows:
                continue

            rows_html = []
            for name, formula, value in rows:
                rows_html.append(
                    f"""
                    <tr>
                        <td>{escape(str(name))}</td>
                        <td>{escape(str(formula))}</td>
                        <td class="value">{_format_value_cell(value)}</td>
                    </tr>
                    """
                )

            section_html = f"""
                <h2>{escape(self._section_title(section_key))}</h2>
                <table>
                    <tr>
                        <th>Показатель</th>
                        <th>Формула</th>
                        <th class="value">Значение</th>
                    </tr>
                    {''.join(rows_html)}
                </table>
            """
            sections_html.append(section_html)
        return "\n".join(sections_html)

    def generate_report_html(self, selected_sections):
        selected = [key for key in selected_sections if key]
        if not selected:
            raise ValueError("Выберите хотя бы один раздел статистики.")

        try:
            stats = self._calculate_statistics()

            html_body = self._render_sections_html(selected, stats)
            generated_at = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
            period = f"{self._start_dt.strftime('%d.%m.%Y')} - {self._end_dt.strftime('%d.%m.%Y')}"

            full_html = f"""
            <html>
            <head>
                <meta charset="UTF-8">
                <style>
                    html, body {{
                        font-family: 'Arial', sans-serif;
                        color: {TEXT_PRIMARY};
                        background: {BG_CARD};
                        margin: 0;
                        padding: 0;
                    }}
                    .page {{
                        background: {BG_CARD};
                        padding: 18px 20px;
                    }}
                    h1 {{
                        margin: 0 0 6px 0;
                        font-size: 20px;
                        color: {TEXT_PRIMARY};
                    }}
                    h2 {{
                        margin: 16px 0 7px 0;
                        font-size: 14px;
                        color: {COLOR_PRIMARY_DARK};
                        text-transform: uppercase;
                    }}
                    .period {{
                        margin: 0 0 12px 0;
                        color: {TEXT_SECONDARY};
                        font-weight: 600;
                    }}
                    table {{
                        width: 100%;
                        border-collapse: collapse;
                        margin-bottom: 12px;
                    }}
                    th, td {{
                        border: 1px solid {BORDER_COLOR};
                        padding: 6px 8px;
                        text-align: left;
                        font-size: 12px;
                        vertical-align: top;
                        color: {TEXT_PRIMARY};
                    }}
                    th {{
                        background: {BG_LIGHT};
                        font-weight: 700;
                    }}
                    td.value, th.value {{
                        text-align: right;
                        width: 32%;
                    }}
                    .footnote {{
                        margin-top: 12px;
                        color: {TEXT_SECONDARY};
                        font-size: 11px;
                    }}
                </style>
            </head>
            <body>
                <div class="page">
                    <h1>Статистический отчет ОАР №3</h1>
                    <p class="period">Период: {period}</p>
                    {html_body}
                    <p class="footnote">Сформировано автоматически: {generated_at}</p>
                </div>
            </body>
            </html>
            """

            return full_html
        except Exception:
            raise

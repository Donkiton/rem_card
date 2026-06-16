from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from html import escape
from statistics import median
from typing import Any, Iterable, Mapping

from rem_card.app.logger import logger
from rem_card.services.analytics.graphs_service import _thread_local_manager
from rem_card.services.operblock_route_settings import operblock_route_from_comment
from rem_card.services.operblock_timeline import parse_operblock_medication_text
from rem_card.ui.styles.theme import (
    BG_CARD,
    BG_LIGHT,
    BORDER_COLOR,
    COLOR_PRIMARY_DARK,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)


OPERBLOCK_SECTION_GROUPS = {
    "Объем и поток": {
        "ob1": "1. Всего случаев",
        "ob2": "2. Закрытые случаи",
        "ob3": "3. Активные случаи",
        "ob4": "4. Экстренные операции",
        "ob5": "5. Плановые операции",
        "ob6": "6. Доля экстренных",
        "ob7": "7. Доля плановых",
        "ob8": "8. Среднее операций в день",
        "ob9": "9. Максимум операций за день",
        "ob10": "10. Самая загруженная дата",
        "ob11": "11. Ночные операции",
    },
    "Время и этапы": {
        "ob12": "12. Суммарная занятость столов",
        "ob13": "13. Средняя длительность в операционной",
        "ob14": "14. Медиана длительности в операционной",
        "ob15": "15. Минимальная длительность в операционной",
        "ob16": "16. Максимальная длительность в операционной",
        "ob17": "17. Полный набор этапов",
        "ob18": "18. Неполный набор этапов",
        "ob19": "19. Средняя длительность анестезии",
        "ob20": "20. Медиана длительности анестезии",
        "ob21": "21. Средняя длительность операции",
        "ob22": "22. Медиана длительности операции",
        "ob23": "23. От входа до начала анестезии",
        "ob24": "24. От начала анестезии до начала операции",
        "ob25": "25. От конца операции до конца анестезии",
    },
    "Анестезия и витальные": {
        "ob26": "26. Распределение по виду анестезии",
        "ob27": "27. Самый частый вид анестезии",
        "ob28": "28. Длительность анестезии по виду",
        "ob29": "29. Есть исходные витальные",
        "ob30": "30. Нет исходных витальных",
        "ob31": "31. Среднее число измерений витальных",
        "ob32": "32. Средний интервал между витальными",
        "ob33": "33. Средний минимум SpO2",
        "ob34": "34. Операции со SpO2 < 90",
        "ob35": "35. Операции с АДс < 90",
        "ob36": "36. Операции с пульсом > 120",
        "ob37": "37. Нет витальных после начала анестезии",
    },
    "Медикаменты, инфузии и газы": {
        "ob38": "38. Болюсных введений",
        "ob39": "39. Операций с болюсами",
        "ob40": "40. Среднее болюсов на операцию",
        "ob41": "41. Топ болюсных препаратов",
        "ob42": "42. Болюсы по путям введения",
        "ob43": "43. Суммарный распознанный объем болюсов",
        "ob44": "44. Инфузий запущено",
        "ob45": "45. Операций с инфузиями",
        "ob46": "46. Среднее инфузий на операцию",
        "ob47": "47. Неостановленные инфузии",
        "ob48": "48. Средняя длительность инфузии",
        "ob49": "49. Топ инфузионных препаратов",
        "ob50": "50. Суммарный заявленный объем инфузий",
        "ob51": "51. Газы и кислород",
    },
    "Персонал": {
        "ob52": "52. Операции по анестезиологам",
        "ob53": "53. Операции по анестезисткам",
        "ob54": "54. Операции по хирургам",
        "ob55": "55. Операции по операционным сестрам",
        "ob56": "56. Пары анестезиолог + анестезистка",
        "ob57": "57. Пары хирург + анестезиолог",
        "ob58": "58. Ночные операции по анестезиологам",
        "ob59": "59. Экстренные операции по анестезиологам",
        "ob60": "60. Плановые операции по анестезиологам",
        "ob61": "61. Длительность анестезии по анестезиологу",
        "ob62": "62. Длительность операции по хирургу",
        "ob63": "63. Случаи без ключевого персонала",
    },
    "Пациенты, маршруты и диагнозы": {
        "ob64": "64. Пол пациентов",
        "ob65": "65. Возрастные группы",
        "ob66": "66. Дети / взрослые",
        "ob67": "67. Профили/отделения поступления",
        "ob68": "68. Отделение перевода",
        "ob69": "69. Топ диагнозов",
        "ob70": "70. Топ МКБ",
        "ob71": "71. Пациенты с несколькими операциями",
        "ob72": "72. Повторные операции в один день",
        "ob73": "73. Группа крови и Rh",
    },
    "Качество заполнения": {
        "ob74": "74. Без названия операции",
        "ob75": "75. Без анестезиолога",
        "ob76": "76. Без хирурга",
        "ob77": "77. Без анестезистки",
        "ob78": "78. Без отделения перевода",
        "ob79": "79. Без роста или веса",
    },
}

OPERBLOCK_TOP_INDICATORS = [
    "ob1",
    "ob2",
    "ob4",
    "ob5",
    "ob6",
    "ob7",
    "ob8",
    "ob9",
    "ob11",
    "ob12",
    "ob13",
    "ob17",
    "ob18",
    "ob19",
    "ob21",
    "ob23",
    "ob24",
    "ob26",
    "ob27",
    "ob29",
    "ob30",
    "ob31",
    "ob34",
    "ob35",
    "ob37",
    "ob38",
    "ob39",
    "ob44",
    "ob45",
    "ob47",
    "ob51",
    "ob52",
    "ob54",
    "ob63",
    "ob68",
    "ob69",
    "ob74",
    "ob75",
    "ob76",
    "ob78",
]


def build_operblock_statistics_report_html(
    db_manager,
    start_date_str: str,
    end_date_str: str,
    selected_indicators: Iterable[str] | None = None,
    db_paths: Iterable[str] | None = None,
) -> str:
    selected = None if selected_indicators is None else list(selected_indicators)
    return OperBlockStatisticsReportBuilder(
        db_manager,
        start_date_str,
        end_date_str,
        db_paths=db_paths,
    ).generate_report_html(selected)


class OperBlockStatisticsReportBuilder:
    def __init__(
        self,
        db_manager,
        start_date_str: str,
        end_date_str: str,
        *,
        db_paths: Iterable[str] | None = None,
    ):
        self.db_manager = db_manager
        self._start_dt = self._parse_datetime(start_date_str) or (datetime.now() - timedelta(days=30))
        self._end_dt = self._parse_datetime(end_date_str) or datetime.now()
        if self._end_dt < self._start_dt:
            self._start_dt, self._end_dt = self._end_dt, self._start_dt
        self.start_date_str = self._start_dt.strftime("%Y-%m-%d 00:00:00")
        self.end_date_str = self._end_dt.strftime("%Y-%m-%d 23:59:59")
        self.db_paths = self._normalize_db_paths(db_paths or [])

    @staticmethod
    def _normalize_db_paths(db_paths: Iterable[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in db_paths or []:
            if not raw:
                continue
            path = os.path.abspath(str(raw))
            if not os.path.isfile(path):
                continue
            key = os.path.normcase(path)
            if key in seen:
                continue
            seen.add(key)
            result.append(path)
        return result

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value.replace(second=0, microsecond=0)
        text = str(value or "").strip().replace("T", " ")
        if not text:
            return None
        if "." in text:
            text = text.split(".", 1)[0]
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%d.%m.%Y %H:%M:%S",
            "%d.%m.%Y %H:%M",
            "%d.%m.%Y",
        ):
            try:
                parsed = datetime.strptime(text, fmt)
                if fmt in ("%Y-%m-%d", "%d.%m.%Y"):
                    return parsed.replace(hour=0, minute=0, second=0, microsecond=0)
                return parsed.replace(second=0, microsecond=0)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(text.replace(" ", "T")).replace(second=0, microsecond=0)
        except ValueError:
            return None

    @staticmethod
    def _json_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping):
            return dict(value)
        text = str(value or "").strip()
        if not text:
            return {}
        try:
            decoded = json.loads(text)
        except Exception:
            return {}
        return decoded if isinstance(decoded, dict) else {}

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            if value is None or value == "":
                return float(default)
            return float(str(value).replace(",", "."))
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _decimal(value: Any) -> Decimal | None:
        text = str(value or "").strip().replace(",", ".")
        if not text:
            return None
        try:
            return Decimal(text)
        except (InvalidOperation, ValueError):
            return None

    @classmethod
    def _decimal_ml(cls, *values: Any) -> Decimal | None:
        for value in values:
            parsed = cls._decimal(value)
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def _fmt_num(value: Any, digits: int = 1) -> str:
        if value is None:
            return "н/д"
        try:
            number = float(value)
        except (TypeError, ValueError):
            return str(value)
        if digits <= 0:
            return str(int(round(number)))
        text = f"{number:.{digits}f}".rstrip("0").rstrip(".")
        return text.replace(".", ",")

    @classmethod
    def _fmt_minutes(cls, value: Any) -> str:
        if value is None:
            return "н/д"
        return f"{cls._fmt_num(value, 0)} мин"

    @classmethod
    def _fmt_hours(cls, value: Any) -> str:
        if value is None:
            return "н/д"
        return f"{cls._fmt_num(value, 1)} ч"

    @staticmethod
    def _pct(num: float, den: float) -> float:
        return (float(num) / float(den) * 100.0) if den else 0.0

    @classmethod
    def _fmt_pct(cls, num: float, den: float) -> str:
        return f"{cls._fmt_num(cls._pct(num, den), 1)}%"

    @staticmethod
    def _clean_text(value: Any, fallback: str = "Не указано") -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        return text or fallback

    @staticmethod
    def _weekday_name(value: datetime) -> str:
        return (
            "Понедельник",
            "Вторник",
            "Среда",
            "Четверг",
            "Пятница",
            "Суббота",
            "Воскресенье",
        )[value.weekday()]

    @staticmethod
    def _mkb_class(value: Any) -> str:
        text = str(value or "").strip().upper()
        return text[:3] if text else "Не указан"

    @staticmethod
    def _table_label(value: Any) -> str:
        text = str(value or "").strip().lower()
        if text == "emergency" or "экстр" in text:
            return "Экстренная"
        if text == "planned" or "план" in text:
            return "Плановая"
        return str(value or "Не указано")

    @staticmethod
    def _status_label(value: Any) -> str:
        status = str(value or "").strip().lower()
        return {
            "active": "Активные",
            "closed": "Закрытые",
            "transferred_to_rao": "Переведены в РАО",
        }.get(status, status or "Не указан")

    @classmethod
    def _age_years(cls, row: Mapping[str, Any], ref_dt: datetime | None) -> float | None:
        birth_dt = cls._parse_datetime(row.get("birth_date"))
        if birth_dt is not None and ref_dt is not None:
            years = ref_dt.year - birth_dt.year
            if (ref_dt.month, ref_dt.day) < (birth_dt.month, birth_dt.day):
                years -= 1
            return max(0.0, float(years))
        months = row.get("patient_months")
        if months not in (None, ""):
            return cls._safe_float(months) / 12.0
        age = row.get("patient_age")
        if age in (None, ""):
            return None
        unit = str(row.get("patient_age_unit") or "").casefold()
        age_value = cls._safe_float(age)
        return age_value / 12.0 if "меся" in unit else age_value

    @classmethod
    def _age_group(cls, age_years: float | None) -> str:
        if age_years is None:
            return "Не указан"
        if age_years < 1:
            return "до 1 года"
        if age_years < 18:
            return "1-17"
        if age_years <= 44:
            return "18-44"
        if age_years <= 60:
            return "45-60"
        if age_years <= 75:
            return "61-75"
        return "75+"

    @staticmethod
    def _duration_minutes(start: datetime | None, end: datetime | None) -> float | None:
        if start is None or end is None or end < start:
            return None
        return (end - start).total_seconds() / 60.0

    @classmethod
    def _median_or_none(cls, values: Iterable[float]) -> float | None:
        clean = [float(value) for value in values if value is not None]
        return median(clean) if clean else None

    @classmethod
    def _avg_or_none(cls, values: Iterable[float]) -> float | None:
        clean = [float(value) for value in values if value is not None]
        return (sum(clean) / len(clean)) if clean else None

    @classmethod
    def _distribution_lines(cls, counter: Counter | dict[str, int], total: int, *, limit: int = 12) -> str:
        if not counter:
            return "н/д"
        items = sorted(counter.items(), key=lambda item: (-int(item[1] or 0), str(item[0])))[:limit]
        return "<br/>".join(
            f"{escape(str(name))}: {int(count)} ({cls._fmt_pct(int(count), total)})"
            for name, count in items
        ) or "н/д"

    @classmethod
    def _duration_distribution_lines(cls, values: dict[str, list[float]], *, limit: int = 10) -> str:
        rows = []
        for name, durations in values.items():
            avg = cls._avg_or_none(durations)
            if avg is None:
                continue
            rows.append((name, len(durations), avg))
        rows.sort(key=lambda item: (-item[1], str(item[0])))
        if not rows:
            return "н/д"
        return "<br/>".join(
            f"{escape(str(name))}: {count}, среднее {cls._fmt_minutes(avg)}"
            for name, count, avg in rows[:limit]
        )

    @staticmethod
    def _top_label(counter: Counter | dict[str, int]) -> str:
        if not counter:
            return "н/д"
        name, count = sorted(counter.items(), key=lambda item: (-int(item[1] or 0), str(item[0])))[0]
        return f"{name}: {int(count)}"

    @staticmethod
    def _cursor_dict_rows(cursor) -> list[dict[str, Any]]:
        columns = [column[0] for column in (cursor.description or [])]
        rows = []
        for row in cursor.fetchall():
            rows.append(dict(row) if hasattr(row, "keys") else dict(zip(columns, row)))
        return rows

    @staticmethod
    def _table_exists(cursor, table_name: str) -> bool:
        cursor.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table_name,))
        return cursor.fetchone() is not None

    @staticmethod
    def _surgeons_from_json(value: Any) -> list[str]:
        if isinstance(value, list):
            raw_items = value
        else:
            try:
                raw_items = json.loads(str(value or "[]"))
            except Exception:
                raw_items = []
        if isinstance(raw_items, str):
            raw_items = [raw_items]
        if not isinstance(raw_items, list):
            return []
        result = []
        seen = set()
        for item in raw_items:
            text = re.sub(r"\s+", " ", str(item or "").strip())
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                result.append(text)
        return result

    @classmethod
    def _stage_text_list(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            raw_items = re.split(r"\s*,\s*", value)
        elif isinstance(value, list):
            raw_items = []
            for item in value:
                if isinstance(item, str):
                    raw_items.extend(re.split(r"\s*,\s*", item))
                else:
                    raw_items.append(item)
        else:
            raw_items = []
        result = []
        seen = set()
        for item in raw_items:
            text = re.sub(r"\s+", " ", str(item or "").strip())
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                result.append(text)
        return result

    @classmethod
    def _stage_state(cls, rows: list[dict[str, Any]]) -> dict[str, Any]:
        stage_rows = []
        for row in rows:
            payload = cls._json_dict(row.get("payload_json"))
            kind = str(payload.get("stage_kind") or payload.get("operation_stage") or "").strip()
            if kind not in {"anesthesia_start", "anesthesia_end", "surgery_start", "surgery_end", "custom"}:
                continue
            event_dt = cls._parse_datetime(row.get("event_time"))
            if event_dt is None:
                continue
            stage_rows.append({"kind": kind, "event_dt": event_dt, "payload": payload})
        stage_rows.sort(key=lambda item: item["event_dt"])

        anesthesia_intervals = []
        surgery_intervals = []
        anesthesia_start = None
        surgery_start = None
        current_anesthesia_type = ""
        current_anesthesiologist = ""
        current_anesthetist = ""
        current_operation_name = ""
        current_surgeons = []
        current_nurse = ""
        first_anesthesia_type = ""
        first_anesthesiologist = ""
        first_anesthetist = ""
        first_operation_name = ""
        first_surgeons = []
        first_nurse = ""
        last_anesthesia_type = ""
        last_anesthesiologist = ""
        last_anesthetist = ""
        last_operation_name = ""
        last_surgeons = []
        last_nurse = ""
        transfer_department = ""
        custom_events = 0

        for row in stage_rows:
            payload = row["payload"]
            kind = row["kind"]
            event_dt = row["event_dt"]
            if kind == "custom":
                custom_events += 1
                continue
            if kind == "anesthesia_start":
                if anesthesia_start is None:
                    anesthesia_start = event_dt
                    current_anesthesia_type = cls._clean_text(
                        payload.get("anesthesia_assistance_type")
                        or payload.get("assistance_type")
                        or payload.get("anesthesia_type"),
                        fallback="",
                    )
                    current_anesthesiologist = cls._clean_text(
                        payload.get("anesthesiologist") or payload.get("anesthesia_doctor"),
                        fallback="",
                    )
                    current_anesthetist = cls._clean_text(
                        payload.get("anesthetist") or payload.get("anesthesia_nurse"),
                        fallback="",
                    )
                    last_anesthesia_type = current_anesthesia_type
                    last_anesthesiologist = current_anesthesiologist
                    last_anesthetist = current_anesthetist
                    first_anesthesia_type = first_anesthesia_type or current_anesthesia_type
                    first_anesthesiologist = first_anesthesiologist or current_anesthesiologist
                    first_anesthetist = first_anesthetist or current_anesthetist
            elif kind == "anesthesia_end":
                transfer_department = cls._clean_text(payload.get("transfer_department"), fallback="") or transfer_department
                if anesthesia_start is not None and event_dt >= anesthesia_start:
                    anesthesia_intervals.append(
                        {
                            "start": anesthesia_start,
                            "end": event_dt,
                            "type": current_anesthesia_type,
                            "anesthesiologist": current_anesthesiologist,
                            "anesthetist": current_anesthetist,
                        }
                    )
                    anesthesia_start = None
                    current_anesthesia_type = ""
                    current_anesthesiologist = ""
                    current_anesthetist = ""
            elif kind == "surgery_start":
                if surgery_start is None:
                    surgery_start = event_dt
                    current_operation_name = cls._clean_text(
                        payload.get("operation_name") or payload.get("surgery_name"),
                        fallback="",
                    )
                    current_surgeons = cls._stage_text_list(payload.get("surgeons") or payload.get("surgeon"))
                    current_nurse = cls._clean_text(
                        payload.get("operating_nurse") or payload.get("surgery_nurse"),
                        fallback="",
                    )
                    last_operation_name = current_operation_name
                    last_surgeons = list(current_surgeons)
                    last_nurse = current_nurse
                    first_operation_name = first_operation_name or current_operation_name
                    first_surgeons = first_surgeons or list(current_surgeons)
                    first_nurse = first_nurse or current_nurse
            elif kind == "surgery_end":
                if surgery_start is not None and event_dt >= surgery_start:
                    surgery_intervals.append(
                        {
                            "start": surgery_start,
                            "end": event_dt,
                            "operation_name": current_operation_name,
                            "surgeons": list(current_surgeons),
                            "operating_nurse": current_nurse,
                        }
                    )
                    surgery_start = None
                    current_operation_name = ""
                    current_surgeons = []
                    current_nurse = ""

        if anesthesia_start is not None:
            anesthesia_intervals.append(
                {
                    "start": anesthesia_start,
                    "end": None,
                    "type": current_anesthesia_type,
                    "anesthesiologist": current_anesthesiologist,
                    "anesthetist": current_anesthetist,
                }
            )
        if surgery_start is not None:
            surgery_intervals.append(
                {
                    "start": surgery_start,
                    "end": None,
                    "operation_name": current_operation_name,
                    "surgeons": list(current_surgeons),
                    "operating_nurse": current_nurse,
                }
            )

        return {
            "events": stage_rows,
            "custom_events": custom_events,
            "anesthesia_intervals": anesthesia_intervals,
            "surgery_intervals": surgery_intervals,
            "first_anesthesia_start": anesthesia_intervals[0]["start"] if anesthesia_intervals else None,
            "last_anesthesia_end": next((row["end"] for row in reversed(anesthesia_intervals) if row.get("end")), None),
            "first_surgery_start": surgery_intervals[0]["start"] if surgery_intervals else None,
            "last_surgery_end": next((row["end"] for row in reversed(surgery_intervals) if row.get("end")), None),
            "anesthesia_type": last_anesthesia_type or first_anesthesia_type,
            "anesthesiologist": last_anesthesiologist or first_anesthesiologist,
            "anesthetist": last_anesthetist or first_anesthetist,
            "operation_name": last_operation_name or first_operation_name,
            "surgeons": last_surgeons or first_surgeons,
            "operating_nurse": last_nurse or first_nurse,
            "transfer_department": transfer_department,
        }

    @staticmethod
    def _in_intervals(value: datetime, intervals: list[dict[str, Any]]) -> bool:
        for interval in intervals:
            start = interval.get("start")
            end = interval.get("end")
            if start is None:
                continue
            if value < start:
                continue
            if end is not None and value > end:
                continue
            return True
        return False

    @staticmethod
    def _text_is_oxygen(value: Any) -> bool:
        text = str(value or "").strip().casefold().replace("ё", "е")
        if not text:
            return False
        return bool(
            "кислород" in text
            or "oxygen" in text
            or re.search(r"(?<![0-9a-zа-я])(?:o|о)\s*2(?![0-9a-zа-я])", text)
        )

    @classmethod
    def _payload_is_gas(cls, payload: Mapping[str, Any], *labels: Any) -> bool:
        if str(payload.get("kind") or "").strip().casefold() == "gas":
            return True
        kind_text = " ".join(str(label or "") for label in labels).casefold().replace("ё", "е")
        return any(token in kind_text for token in ("газ", "десф", "сево", "sevo", "desfl", "кислород", "oxygen"))

    @classmethod
    def _fetch_context(cls, db_manager, start_date_str: str, end_date_str: str) -> dict[str, Any]:
        manager, cleanup = _thread_local_manager(db_manager)
        try:
            return cls._fetch_context_from_connection(manager.get_connection(), start_date_str, end_date_str)
        finally:
            if cleanup:
                cleanup()

    @classmethod
    def _fetch_context_from_connection(cls, conn, start_date_str: str, end_date_str: str) -> dict[str, Any]:
        cursor = conn.cursor()
        if not cls._table_exists(cursor, "operation_cases"):
            return {"cases": [], "timeline": [], "orders": [], "vitals": []}

        cursor.execute(
            """
            SELECT
                oc.id AS operation_case_id,
                oc.patient_id,
                oc.admission_id,
                oc.table_code,
                oc.status,
                oc.created_at,
                oc.started_at,
                oc.ended_at,
                oc.planned_operation_name,
                oc.planned_surgeons_json,
                oc.planned_operating_nurse,
                oc.planned_anesthesiologist,
                oc.planned_anesthetist,
                oc.height_cm,
                oc.weight_kg,
                oc.allergies,
                oc.blood_group,
                oc.blood_rh,
                oc.preop_sys,
                oc.preop_dia,
                oc.preop_pulse,
                oc.preop_spo2,
                oc.anesthesia_protocol_number,
                oc.anesthesia_protocol_date,
                oc.transfer_department,
                t.display_name AS table_display_name,
                p.full_name,
                p.birth_date,
                a.history_number,
                a.patient_gender,
                a.patient_age,
                a.patient_months,
                a.patient_age_unit,
                a.diagnosis_code,
                a.diagnosis_text,
                a.department_profile,
                a.source_department
            FROM operation_cases oc
            LEFT JOIN operating_tables t ON t.code = oc.table_code
            LEFT JOIN admissions a ON a.id = oc.admission_id
            LEFT JOIN patients p ON p.id = oc.patient_id
            WHERE DATETIME(oc.started_at) BETWEEN DATETIME(?) AND DATETIME(?)
              AND COALESCE(oc.status, '') NOT IN ('cancelled', 'deleted')
            ORDER BY DATETIME(oc.started_at), oc.id
            """,
            (start_date_str, end_date_str),
        )
        cases = cls._cursor_dict_rows(cursor)
        if not cases:
            return {"cases": [], "timeline": [], "orders": [], "vitals": []}

        if cls._table_exists(cursor, "operblock_timeline_events"):
            cursor.execute(
                """
                SELECT e.*
                FROM operblock_timeline_events e
                JOIN operation_cases oc ON oc.id = e.operation_case_id
                WHERE DATETIME(oc.started_at) BETWEEN DATETIME(?) AND DATETIME(?)
                  AND COALESCE(oc.status, '') NOT IN ('cancelled', 'deleted')
                  AND COALESCE(e.status, '') NOT IN ('deleted', 'cancelled')
                ORDER BY e.operation_case_id, DATETIME(e.event_time), e.id
                """,
                (start_date_str, end_date_str),
            )
            timeline = cls._cursor_dict_rows(cursor)
        else:
            timeline = []

        if cls._table_exists(cursor, "orders"):
            cursor.execute(
                """
                SELECT
                    oc.id AS operation_case_id,
                    o.id,
                    o.admission_id,
                    o.datetime,
                    o.text,
                    o.drug_key,
                    o.status,
                    o.comment
                FROM operation_cases oc
                JOIN orders o ON o.admission_id = oc.admission_id
                WHERE DATETIME(oc.started_at) BETWEEN DATETIME(?) AND DATETIME(?)
                  AND COALESCE(oc.status, '') NOT IN ('cancelled', 'deleted')
                  AND COALESCE(o.status, '') NOT IN ('deleted', 'cancelled')
                ORDER BY oc.id, DATETIME(o.datetime), o.id
                """,
                (start_date_str, end_date_str),
            )
            orders = cls._cursor_dict_rows(cursor)
        else:
            orders = []

        if cls._table_exists(cursor, "vitals"):
            cursor.execute(
                """
                SELECT
                    oc.id AS operation_case_id,
                    v.id,
                    v.admission_id,
                    v.datetime,
                    v.sys,
                    v.dia,
                    v.pulse,
                    v.temp,
                    v.spo2,
                    v.rr,
                    v.cvp
                FROM operation_cases oc
                JOIN vitals v ON v.admission_id = oc.admission_id
                WHERE DATETIME(oc.started_at) BETWEEN DATETIME(?) AND DATETIME(?)
                  AND COALESCE(oc.status, '') NOT IN ('cancelled', 'deleted')
                ORDER BY oc.id, DATETIME(v.datetime), v.id
                """,
                (start_date_str, end_date_str),
            )
            vitals = cls._cursor_dict_rows(cursor)
        else:
            vitals = []

        return {"cases": cases, "timeline": timeline, "orders": orders, "vitals": vitals}

    @classmethod
    def _fetch_multi_db_context(
        cls,
        db_paths: Iterable[str],
        start_date_str: str,
        end_date_str: str,
    ) -> dict[str, Any]:
        from rem_card.services.analytics.multi_db_analytics import create_readonly_analytics_manager

        merged: dict[str, list[dict[str, Any]]] = {
            "cases": [],
            "timeline": [],
            "orders": [],
            "vitals": [],
        }
        next_case_id = 1

        for db_path in cls._normalize_db_paths(db_paths):
            manager = None
            try:
                manager = create_readonly_analytics_manager(db_path)
                context = cls._fetch_context_from_connection(
                    manager.get_connection(),
                    start_date_str,
                    end_date_str,
                )
            except Exception as exc:
                logger.warning("Skipping operblock statistics DB %s: %s", db_path, exc)
                continue
            finally:
                if manager is not None:
                    manager.close_connection()

            case_id_map: dict[int, int] = {}
            for case in context.get("cases") or []:
                item = dict(case or {})
                source_case_id = cls._safe_int(item.get("operation_case_id"))
                if not source_case_id:
                    continue
                remapped_case_id = next_case_id
                next_case_id += 1
                case_id_map[source_case_id] = remapped_case_id
                item["source_db_path"] = db_path
                item["source_operation_case_id"] = source_case_id
                item["operation_case_id"] = remapped_case_id
                merged["cases"].append(item)

            for section in ("timeline", "orders", "vitals"):
                for row in context.get(section) or []:
                    item = dict(row or {})
                    source_case_id = cls._safe_int(item.get("operation_case_id"))
                    remapped_case_id = case_id_map.get(source_case_id)
                    if not remapped_case_id:
                        continue
                    item["source_db_path"] = db_path
                    item["source_operation_case_id"] = source_case_id
                    item["operation_case_id"] = remapped_case_id
                    merged[section].append(item)

        return merged

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _calculate_statistics(self) -> dict[str, Any]:
        if self.db_paths:
            context = self._fetch_multi_db_context(self.db_paths, self.start_date_str, self.end_date_str)
        else:
            context = self._fetch_context(self.db_manager, self.start_date_str, self.end_date_str)
        timeline_by_case: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in context["timeline"]:
            timeline_by_case[int(row.get("operation_case_id") or 0)].append(row)

        orders_by_case: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in context["orders"]:
            orders_by_case[int(row.get("operation_case_id") or 0)].append(row)

        vitals_by_case: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in context["vitals"]:
            vitals_by_case[int(row.get("operation_case_id") or 0)].append(row)

        now = datetime.now().replace(second=0, microsecond=0)
        period_days = max(1, (self._end_dt.date() - self._start_dt.date()).days + 1)
        cases = []
        for raw in context["cases"]:
            case_id = int(raw.get("operation_case_id") or 0)
            started_at = self._parse_datetime(raw.get("started_at") or raw.get("created_at"))
            if started_at is None:
                continue
            ended_at = self._parse_datetime(raw.get("ended_at"))
            effective_end = ended_at or min(now, self._end_dt if self._end_dt < now else now)
            if effective_end < started_at:
                effective_end = started_at
            stage_state = self._stage_state(timeline_by_case.get(case_id, []))
            surgeons = stage_state["surgeons"] or self._surgeons_from_json(raw.get("planned_surgeons_json"))
            case = dict(raw)
            case.update(
                {
                    "operation_case_id": case_id,
                    "started_dt": started_at,
                    "ended_dt": ended_at,
                    "effective_end_dt": effective_end,
                    "status_label": self._status_label(raw.get("status")),
                    "table_label": self._table_label(raw.get("table_code") or raw.get("table_display_name")),
                    "stage_state": stage_state,
                    "operation_name": stage_state["operation_name"]
                    or self._clean_text(raw.get("planned_operation_name"), fallback=""),
                    "surgeons": surgeons,
                    "anesthesiologist": stage_state["anesthesiologist"]
                    or self._clean_text(raw.get("planned_anesthesiologist"), fallback=""),
                    "anesthetist": stage_state["anesthetist"]
                    or self._clean_text(raw.get("planned_anesthetist"), fallback=""),
                    "operating_nurse": stage_state["operating_nurse"]
                    or self._clean_text(raw.get("planned_operating_nurse"), fallback=""),
                    "anesthesia_type": stage_state["anesthesia_type"] or "Не указан",
                    "transfer_target": self._clean_text(
                        raw.get("transfer_department") or stage_state["transfer_department"],
                        fallback="",
                    ),
                }
            )
            cases.append(case)

        counted_cases = cases
        total = len(counted_cases)
        case_ids = {int(case["operation_case_id"]) for case in counted_cases}

        by_status = Counter(case["status_label"] for case in cases)
        by_table = Counter(case["table_label"] for case in counted_cases)
        by_date = Counter(case["started_dt"].strftime("%d.%m.%Y") for case in counted_cases)
        by_weekday = Counter(self._weekday_name(case["started_dt"]) for case in counted_cases)
        by_hour = Counter(case["started_dt"].strftime("%H:00") for case in counted_cases)
        night_cases = [
            case for case in counted_cases if case["started_dt"].hour >= 22 or case["started_dt"].hour < 6
        ]

        room_durations = []
        anesthesia_durations = []
        surgery_durations = []
        room_to_anesthesia = []
        anesthesia_to_surgery = []
        surgery_end_to_anesthesia_end = []
        full_stage_count = 0
        repeated_anesthesia = 0
        repeated_surgery = 0
        anesthesia_type_durations: dict[str, list[float]] = defaultdict(list)
        anesthesiologist_durations: dict[str, list[float]] = defaultdict(list)
        surgeon_durations: dict[str, list[float]] = defaultdict(list)

        anesthesiologists = Counter()
        anesthetists = Counter()
        surgeons_counter = Counter()
        nurses = Counter()
        anesthesia_pairs = Counter()
        surgeon_anesthesia_pairs = Counter()
        anesth_night = Counter()
        anesth_emergency = Counter()
        anesth_planned = Counter()

        genders = Counter()
        age_groups = Counter()
        child_adult = Counter()
        departments = Counter()
        transfers = Counter()
        diagnoses = Counter()
        mkb_classes = Counter()
        blood_groups = Counter()
        blood_rh = Counter()
        repeated_by_patient = Counter()
        repeated_by_patient_day = Counter()

        missing_operation_name = 0
        missing_anesthesiologist = 0
        missing_surgeon = 0
        missing_anesthetist = 0
        missing_transfer = 0
        missing_height_weight = 0
        missing_key_personnel = 0

        for case in counted_cases:
            room_duration = self._duration_minutes(case["started_dt"], case["effective_end_dt"])
            if room_duration is not None:
                room_durations.append(room_duration)

            stage = case["stage_state"]
            anesthesia_intervals = stage["anesthesia_intervals"]
            surgery_intervals = stage["surgery_intervals"]
            if len(anesthesia_intervals) > 1:
                repeated_anesthesia += 1
            if len(surgery_intervals) > 1:
                repeated_surgery += 1

            case_anesthesia_minutes = sum(
                duration
                for duration in (
                    self._duration_minutes(row.get("start"), row.get("end") or case["effective_end_dt"])
                    for row in anesthesia_intervals
                )
                if duration is not None
            )
            case_surgery_minutes = sum(
                duration
                for duration in (
                    self._duration_minutes(row.get("start"), row.get("end") or case["effective_end_dt"])
                    for row in surgery_intervals
                )
                if duration is not None
            )
            if case_anesthesia_minutes > 0:
                anesthesia_durations.append(case_anesthesia_minutes)
                anesthesia_type_durations[case["anesthesia_type"]].append(case_anesthesia_minutes)
                if case["anesthesiologist"]:
                    anesthesiologist_durations[case["anesthesiologist"]].append(case_anesthesia_minutes)
            if case_surgery_minutes > 0:
                surgery_durations.append(case_surgery_minutes)
                for surgeon in case["surgeons"]:
                    surgeon_durations[surgeon].append(case_surgery_minutes)

            if (
                stage["first_anesthesia_start"]
                and stage["last_anesthesia_end"]
                and stage["first_surgery_start"]
                and stage["last_surgery_end"]
            ):
                full_stage_count += 1
            value = self._duration_minutes(case["started_dt"], stage["first_anesthesia_start"])
            if value is not None:
                room_to_anesthesia.append(value)
            value = self._duration_minutes(stage["first_anesthesia_start"], stage["first_surgery_start"])
            if value is not None:
                anesthesia_to_surgery.append(value)
            value = self._duration_minutes(stage["last_surgery_end"], stage["last_anesthesia_end"])
            if value is not None:
                surgery_end_to_anesthesia_end.append(value)

            if case["anesthesiologist"]:
                anesthesiologists[case["anesthesiologist"]] += 1
                if case in night_cases:
                    anesth_night[case["anesthesiologist"]] += 1
                if case["table_label"] == "Экстренная":
                    anesth_emergency[case["anesthesiologist"]] += 1
                if case["table_label"] == "Плановая":
                    anesth_planned[case["anesthesiologist"]] += 1
            if case["anesthetist"]:
                anesthetists[case["anesthetist"]] += 1
            if case["operating_nurse"]:
                nurses[case["operating_nurse"]] += 1
            for surgeon in case["surgeons"]:
                surgeons_counter[surgeon] += 1
                if case["anesthesiologist"]:
                    surgeon_anesthesia_pairs[f"{surgeon} + {case['anesthesiologist']}"] += 1
            if case["anesthesiologist"] and case["anesthetist"]:
                anesthesia_pairs[f"{case['anesthesiologist']} + {case['anesthetist']}"] += 1

            age_years = self._age_years(case, case["started_dt"])
            genders[self._clean_text(case.get("patient_gender"))] += 1
            age_groups[self._age_group(age_years)] += 1
            if age_years is None:
                child_adult["Возраст не указан"] += 1
            elif age_years < 18:
                child_adult["Дети"] += 1
            else:
                child_adult["Взрослые"] += 1
            departments[self._clean_text(case.get("department_profile") or case.get("source_department"))] += 1
            transfers[self._clean_text(case.get("transfer_target"))] += 1
            diagnosis = self._clean_text(
                f"{str(case.get('diagnosis_code') or '').strip()} {str(case.get('diagnosis_text') or '').strip()}".strip()
            )
            diagnoses[diagnosis] += 1
            mkb_classes[self._mkb_class(case.get("diagnosis_code"))] += 1
            blood_groups[self._clean_text(case.get("blood_group"))] += 1
            blood_rh[self._clean_text(case.get("blood_rh"))] += 1
            patient_key = str(case.get("patient_id") or case.get("history_number") or "")
            if patient_key:
                repeated_by_patient[patient_key] += 1
                repeated_by_patient_day[f"{patient_key}:{case['started_dt'].date().isoformat()}"] += 1

            if not case["operation_name"]:
                missing_operation_name += 1
            if not case["anesthesiologist"]:
                missing_anesthesiologist += 1
            if not case["surgeons"]:
                missing_surgeon += 1
            if not case["anesthetist"]:
                missing_anesthetist += 1
            if not case["anesthesiologist"] or not case["surgeons"] or not case["anesthetist"]:
                missing_key_personnel += 1
            if not case["transfer_target"]:
                missing_transfer += 1
            if not case.get("height_cm") or not case.get("weight_kg"):
                missing_height_weight += 1

        bolus_count = 0
        bolus_cases = set()
        bolus_drugs = Counter()
        bolus_routes = Counter()
        bolus_volume_ml = Decimal("0")
        vitals_count_by_case = Counter()
        vital_interval_values = []
        min_spo2_values = []
        cases_with_initial_vitals = set()
        cases_with_vitals_after_anesthesia = set()
        cases_spo2_low = set()
        cases_sys_low = set()
        cases_pulse_high = set()

        case_by_id = {int(case["operation_case_id"]): case for case in counted_cases}
        for case_id, vital_rows in vitals_by_case.items():
            case = case_by_id.get(case_id)
            if case is None:
                continue
            stage = case["stage_state"]
            rows = []
            for raw in vital_rows:
                event_dt = self._parse_datetime(raw.get("datetime"))
                if event_dt is None or event_dt < case["started_dt"] or event_dt > case["effective_end_dt"]:
                    continue
                rows.append((event_dt, raw))
            rows.sort(key=lambda item: item[0])
            if not rows:
                continue
            vitals_count_by_case[case_id] = len(rows)
            for index in range(1, len(rows)):
                vital_interval_values.append((rows[index][0] - rows[index - 1][0]).total_seconds() / 60.0)
            first_anesthesia_start = stage.get("first_anesthesia_start")
            if any(
                value not in (None, "")
                for value in (case.get("preop_sys"), case.get("preop_dia"), case.get("preop_pulse"), case.get("preop_spo2"))
            ):
                cases_with_initial_vitals.add(case_id)
            if first_anesthesia_start is not None:
                if any(event_dt <= first_anesthesia_start for event_dt, _raw in rows):
                    cases_with_initial_vitals.add(case_id)
                if any(event_dt >= first_anesthesia_start for event_dt, _raw in rows):
                    cases_with_vitals_after_anesthesia.add(case_id)
            spo2_values = [self._safe_float(raw.get("spo2"), default=-1.0) for _dt, raw in rows if raw.get("spo2") not in (None, "")]
            if spo2_values:
                min_spo2 = min(spo2_values)
                min_spo2_values.append(min_spo2)
                if min_spo2 < 90:
                    cases_spo2_low.add(case_id)
            if any(raw.get("sys") not in (None, "") and self._safe_float(raw.get("sys")) < 90 for _dt, raw in rows):
                cases_sys_low.add(case_id)
            if any(raw.get("pulse") not in (None, "") and self._safe_float(raw.get("pulse")) > 120 for _dt, raw in rows):
                cases_pulse_high.add(case_id)

        for case_id, order_rows in orders_by_case.items():
            case = case_by_id.get(case_id)
            if case is None:
                continue
            intervals = case["stage_state"]["anesthesia_intervals"]
            for raw in order_rows:
                event_dt = self._parse_datetime(raw.get("datetime"))
                if event_dt is None:
                    continue
                if intervals:
                    if not self._in_intervals(event_dt, intervals):
                        continue
                elif event_dt < case["started_dt"] or event_dt > case["effective_end_dt"]:
                    continue
                parsed = parse_operblock_medication_text(str(raw.get("text") or ""))
                drug = self._clean_text(parsed.get("drug_label") or raw.get("text"))
                route = self._clean_text(operblock_route_from_comment(raw.get("comment")), fallback="Не указан")
                bolus_count += 1
                bolus_cases.add(case_id)
                bolus_drugs[drug] += 1
                bolus_routes[route] += 1
                volume = self._decimal_ml(parsed.get("volume_ml"))
                if volume is not None:
                    bolus_volume_ml += volume

        timeline_children: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for raw in context["timeline"]:
            parent_id = raw.get("parent_event_id")
            if parent_id not in (None, ""):
                timeline_children[int(parent_id)].append(raw)

        infusion_count = 0
        infusion_cases = set()
        open_infusions = 0
        auto_stopped = 0
        infusion_durations = []
        infusion_drugs = Counter()
        infusion_volume_ml = Decimal("0")
        gas_starts = 0
        oxygen_starts = 0
        gas_changes = 0

        for raw in context["timeline"]:
            if str(raw.get("event_type") or "") == "bolus":
                case_id = int(raw.get("operation_case_id") or 0)
                if case_id in case_ids:
                    bolus_count += 1
                    bolus_cases.add(case_id)
                    bolus_drugs[self._clean_text(raw.get("drug_label") or raw.get("display_label"))] += 1
                    bolus_routes[self._clean_text(raw.get("route"), fallback="Не указан")] += 1
                    volume = self._decimal_ml(raw.get("volume_ml"))
                    if volume is not None:
                        bolus_volume_ml += volume
                continue

            if str(raw.get("event_type") or "") != "infusion_start":
                continue
            case_id = int(raw.get("operation_case_id") or 0)
            case = case_by_id.get(case_id)
            if case is None:
                continue
            payload = self._json_dict(raw.get("payload_json"))
            start_dt = self._parse_datetime(raw.get("event_time"))
            if start_dt is None:
                continue
            children = sorted(
                timeline_children.get(int(raw.get("id") or 0), []),
                key=lambda item: self._parse_datetime(item.get("event_time")) or datetime.max,
            )
            stop_row = next((row for row in reversed(children) if str(row.get("event_type") or "") == "infusion_stop"), None)
            stop_dt = self._parse_datetime(stop_row.get("event_time")) if stop_row else self._parse_datetime(raw.get("end_time"))
            effective_stop = stop_dt or case["effective_end_dt"]
            duration = self._duration_minutes(start_dt, effective_stop)
            is_gas = self._payload_is_gas(payload, raw.get("drug_label"), raw.get("display_label"))
            if is_gas:
                gas_starts += 1
                gas_changes += sum(1 for row in children if str(row.get("event_type") or "") == "infusion_change")
                if self._text_is_oxygen(raw.get("drug_label")) or self._text_is_oxygen(raw.get("display_label")):
                    oxygen_starts += 1
                continue
            infusion_count += 1
            infusion_cases.add(case_id)
            infusion_drugs[self._clean_text(payload.get("display_name") or payload.get("label") or raw.get("drug_label"))] += 1
            if duration is not None:
                infusion_durations.append(duration)
            if str(raw.get("status") or "").strip().lower() == "active" and stop_dt is None:
                open_infusions += 1
            stop_payload = self._json_dict(stop_row.get("payload_json")) if stop_row else {}
            if str(stop_payload.get("auto_stopped_by") or "") == "anesthesia_end":
                auto_stopped += 1
            volume = self._decimal_ml(raw.get("volume_ml"), payload.get("declared_total_volume_ml"), payload.get("volume_ml"))
            if volume is not None:
                infusion_volume_ml += volume

        return {
            "period_days": period_days,
            "total": total,
            "by_status": by_status,
            "by_table": by_table,
            "by_date": by_date,
            "by_weekday": by_weekday,
            "by_hour": by_hour,
            "night_count": len(night_cases),
            "room_durations": room_durations,
            "room_hours": sum(room_durations) / 60.0,
            "full_stage_count": full_stage_count,
            "repeated_anesthesia": repeated_anesthesia,
            "repeated_surgery": repeated_surgery,
            "anesthesia_durations": anesthesia_durations,
            "surgery_durations": surgery_durations,
            "room_to_anesthesia": room_to_anesthesia,
            "anesthesia_to_surgery": anesthesia_to_surgery,
            "surgery_end_to_anesthesia_end": surgery_end_to_anesthesia_end,
            "anesthesia_types": Counter(case["anesthesia_type"] for case in counted_cases),
            "anesthesia_type_durations": anesthesia_type_durations,
            "initial_vitals": len(cases_with_initial_vitals),
            "vitals_by_case": vitals_count_by_case,
            "vital_intervals": vital_interval_values,
            "min_spo2_values": min_spo2_values,
            "spo2_low_cases": len(cases_spo2_low),
            "sys_low_cases": len(cases_sys_low),
            "pulse_high_cases": len(cases_pulse_high),
            "vitals_after_anesthesia": len(cases_with_vitals_after_anesthesia),
            "bolus_count": bolus_count,
            "bolus_cases": len(bolus_cases),
            "bolus_drugs": bolus_drugs,
            "bolus_routes": bolus_routes,
            "bolus_volume_ml": bolus_volume_ml,
            "infusion_count": infusion_count,
            "infusion_cases": len(infusion_cases),
            "open_infusions": open_infusions,
            "auto_stopped": auto_stopped,
            "infusion_durations": infusion_durations,
            "infusion_drugs": infusion_drugs,
            "infusion_volume_ml": infusion_volume_ml,
            "gas_starts": gas_starts,
            "oxygen_starts": oxygen_starts,
            "gas_changes": gas_changes,
            "anesthesiologists": anesthesiologists,
            "anesthetists": anesthetists,
            "surgeons": surgeons_counter,
            "nurses": nurses,
            "anesthesia_pairs": anesthesia_pairs,
            "surgeon_anesthesia_pairs": surgeon_anesthesia_pairs,
            "anesth_night": anesth_night,
            "anesth_emergency": anesth_emergency,
            "anesth_planned": anesth_planned,
            "anesthesiologist_durations": anesthesiologist_durations,
            "surgeon_durations": surgeon_durations,
            "genders": genders,
            "age_groups": age_groups,
            "child_adult": child_adult,
            "departments": departments,
            "transfers": transfers,
            "diagnoses": diagnoses,
            "mkb_classes": mkb_classes,
            "repeated_patients": sum(1 for count in repeated_by_patient.values() if count > 1),
            "same_day_repeats": sum(1 for count in repeated_by_patient_day.values() if count > 1),
            "blood_groups": blood_groups,
            "blood_rh": blood_rh,
            "missing_operation_name": missing_operation_name,
            "missing_anesthesiologist": missing_anesthesiologist,
            "missing_surgeon": missing_surgeon,
            "missing_anesthetist": missing_anesthetist,
            "missing_key_personnel": missing_key_personnel,
            "missing_transfer": missing_transfer,
            "missing_height_weight": missing_height_weight,
        }

    def _section_rows(self, stats: dict[str, Any]) -> list[tuple[str, list[tuple[str, str, str]]]]:
        total = int(stats["total"] or 0)
        emergency = int(stats["by_table"].get("Экстренная", 0))
        planned = int(stats["by_table"].get("Плановая", 0))
        busiest_day = self._top_label(stats["by_date"])
        busiest_weekday = self._top_label(stats["by_weekday"])
        busiest_hour = self._top_label(stats["by_hour"])
        avg_vitals = self._avg_or_none(stats["vitals_by_case"].values())
        mean_min_spo2 = self._avg_or_none(stats["min_spo2_values"])
        incomplete_stage = max(0, total - int(stats["full_stage_count"] or 0))
        no_initial_vitals = max(0, total - int(stats["initial_vitals"] or 0))
        no_vitals_after_anesthesia = max(0, total - int(stats["vitals_after_anesthesia"] or 0))

        return [
            (
                "Объем и поток",
                [
                    ("1. Всего случаев", "Случаи оперблока с началом в выбранном периоде, без удаленных/отмененных", self._fmt_num(total, 0)),
                    ("2. Закрытые случаи", "Статус - операция окончена", self._fmt_num(stats["by_status"].get("Закрытые", 0), 0)),
                    ("3. Активные случаи", "Статус - операция в процессе", self._fmt_num(stats["by_status"].get("Активные", 0), 0)),
                    ("4. Экстренные операции", "Стол: экстренная операционная", self._fmt_num(emergency, 0)),
                    ("5. Плановые операции", "Стол: плановая операционная", self._fmt_num(planned, 0)),
                    ("6. Доля экстренных", "Экстренные / операции", self._fmt_pct(emergency, total)),
                    ("7. Доля плановых", "Плановые / операции", self._fmt_pct(planned, total)),
                    ("8. Среднее операций в день", "Операции / дни периода", self._fmt_num(total / stats["period_days"] if stats["period_days"] else 0)),
                    ("9. Максимум операций за день", "Максимальное число стартов в одну дату", self._fmt_num(max(stats["by_date"].values(), default=0), 0)),
                    ("10. Самая загруженная дата", "Дата с максимальным числом стартов", busiest_day),
                    ("11. Ночные операции", "Начало операции с 22:00 до 05:59", f"{self._fmt_num(stats['night_count'], 0)} ({self._fmt_pct(stats['night_count'], total)})"),
                ],
            ),
            (
                "Время и этапы",
                [
                    ("12. Суммарная занятость столов", "Сумма времени от входа до закрытия/текущего времени", self._fmt_hours(stats["room_hours"])),
                    ("13. Средняя длительность в операционной", "Среднее время от начала случая до завершения", self._fmt_minutes(self._avg_or_none(stats["room_durations"]))),
                    ("14. Медиана длительности в операционной", "Типичная длительность случая: половина короче, половина дольше", self._fmt_minutes(self._median_or_none(stats["room_durations"]))),
                    ("15. Минимальная длительность в операционной", "Самый короткий случай за период", self._fmt_minutes(min(stats["room_durations"], default=None))),
                    ("16. Максимальная длительность в операционной", "Самый длинный случай за период", self._fmt_minutes(max(stats["room_durations"], default=None))),
                    ("17. Полный набор этапов", "Есть начало/конец пособия и начало/конец операции", self._fmt_num(stats["full_stage_count"], 0)),
                    ("18. Неполный набор этапов", "Операции без одного из обязательных этапов", self._fmt_num(incomplete_stage, 0)),
                    ("19. Средняя длительность анестезии", "Сумма интервалов пособия по операции, среднее", self._fmt_minutes(self._avg_or_none(stats["anesthesia_durations"]))),
                    ("20. Медиана длительности анестезии", "Сумма интервалов пособия по операции, медиана", self._fmt_minutes(self._median_or_none(stats["anesthesia_durations"]))),
                    ("21. Средняя длительность операции", "Сумма интервалов операции, среднее", self._fmt_minutes(self._avg_or_none(stats["surgery_durations"]))),
                    ("22. Медиана длительности операции", "Сумма интервалов операции, медиана", self._fmt_minutes(self._median_or_none(stats["surgery_durations"]))),
                    ("23. От входа до начала анестезии", "Среднее время от начала случая до первого начала анестезии", self._fmt_minutes(self._avg_or_none(stats["room_to_anesthesia"]))),
                    ("24. От начала анестезии до начала операции", "Среднее время от первого начала анестезии до первого начала операции", self._fmt_minutes(self._avg_or_none(stats["anesthesia_to_surgery"]))),
                    ("25. От конца операции до конца анестезии", "Среднее время от последнего окончания операции до последнего окончания анестезии", self._fmt_minutes(self._avg_or_none(stats["surgery_end_to_anesthesia_end"]))),
                ],
            ),
            (
                "Анестезия и витальные",
                [
                    ("26. Распределение по виду анестезии", "Доля каждого вида анестезии", self._distribution_lines(stats["anesthesia_types"], total)),
                    ("27. Самый частый вид анестезии", "Максимум по виду анестезии", self._top_label(stats["anesthesia_types"])),
                    ("28. Длительность анестезии по виду", "Средняя длительность пособия по виду", self._duration_distribution_lines(stats["anesthesia_type_durations"])),
                    ("29. Есть исходные витальные", "Есть показатели состояния до или на старте анестезии", self._fmt_num(stats["initial_vitals"], 0)),
                    ("30. Нет исходных витальных", "Операции без исходных витальных", self._fmt_num(no_initial_vitals, 0)),
                    ("31. Среднее число измерений витальных", "Среднее число записей показателей состояния на одну операцию", self._fmt_num(avg_vitals)),
                    ("32. Средний интервал между витальными", "Среднее время между соседними записями показателей состояния", self._fmt_minutes(self._avg_or_none(stats["vital_intervals"]))),
                    ("33. Средний минимум SpO2", "Среднее значение минимального SpO2 по операциям", self._fmt_num(mean_min_spo2)),
                    ("34. Операции со SpO2 < 90", "Есть хотя бы одно значение SpO2 ниже 90", self._fmt_num(stats["spo2_low_cases"], 0)),
                    ("35. Операции с АДс < 90", "Есть хотя бы одно систолическое АД ниже 90", self._fmt_num(stats["sys_low_cases"], 0)),
                    ("36. Операции с пульсом > 120", "Есть хотя бы один пульс выше 120", self._fmt_num(stats["pulse_high_cases"], 0)),
                    ("37. Нет витальных после начала анестезии", "Нет записей показателей состояния после начала анестезии", self._fmt_num(no_vitals_after_anesthesia, 0)),
                ],
            ),
            (
                "Медикаменты, инфузии и газы",
                [
                    ("38. Болюсных введений", "Болюсные введения во время анестезии", self._fmt_num(stats["bolus_count"], 0)),
                    ("39. Операций с болюсами", "Уникальные случаи с болюсами", self._fmt_num(stats["bolus_cases"], 0)),
                    ("40. Среднее болюсов на операцию", "Болюсы / операции", self._fmt_num(stats["bolus_count"] / total if total else 0)),
                    ("41. Топ болюсных препаратов", "Самые часто использованные препараты для болюсного введения", self._distribution_lines(stats["bolus_drugs"], stats["bolus_count"])),
                    ("42. Болюсы по путям введения", "Путь введения из комментария назначения", self._distribution_lines(stats["bolus_routes"], stats["bolus_count"])),
                    ("43. Суммарный распознанный объем болюсов", "Сумма указанного объема болюсных введений", f"{self._fmt_num(stats['bolus_volume_ml'], 1)} мл"),
                    ("44. Инфузий запущено", "Количество начатых инфузий, без газов и кислорода", self._fmt_num(stats["infusion_count"], 0)),
                    ("45. Операций с инфузиями", "Операции, где была начата хотя бы одна инфузия", self._fmt_num(stats["infusion_cases"], 0)),
                    ("46. Среднее инфузий на операцию", "Инфузии / операции", self._fmt_num(stats["infusion_count"] / total if total else 0)),
                    ("47. Неостановленные инфузии", "Начатые инфузии без отметки об остановке", self._fmt_num(stats["open_infusions"], 0)),
                    ("48. Средняя длительность инфузии", "Среднее время от начала инфузии до её остановки", self._fmt_minutes(self._avg_or_none(stats["infusion_durations"]))),
                    ("49. Топ инфузионных препаратов", "Самые часто начатые инфузионные препараты", self._distribution_lines(stats["infusion_drugs"], stats["infusion_count"])),
                    ("50. Суммарный заявленный объем инфузий", "Сумма указанного объема инфузий", f"{self._fmt_num(stats['infusion_volume_ml'], 1)} мл"),
                    ("51. Газы и кислород", "Запуски газов / кислорода / изменения дозы или потока", f"{stats['gas_starts']} / {stats['oxygen_starts']} / {stats['gas_changes']}"),
                ],
            ),
            (
                "Персонал",
                [
                    ("52. Операции по анестезиологам", "Доля операций врача / операции", self._distribution_lines(stats["anesthesiologists"], total)),
                    ("53. Операции по анестезисткам", "Доля операций анестезистки / операции", self._distribution_lines(stats["anesthetists"], total)),
                    ("54. Операции по хирургам", "Доля операций хирурга / операции", self._distribution_lines(stats["surgeons"], total)),
                    ("55. Операции по операционным сестрам", "Доля операций операционной сестры / операции", self._distribution_lines(stats["nurses"], total)),
                    ("56. Пары анестезиолог + анестезистка", "Частота сочетаний в протоколе", self._distribution_lines(stats["anesthesia_pairs"], total)),
                    ("57. Пары хирург + анестезиолог", "Частота сочетаний в протоколе", self._distribution_lines(stats["surgeon_anesthesia_pairs"], total)),
                    ("58. Ночные операции по анестезиологам", "Операции 22:00-05:59 по врачу", self._distribution_lines(stats["anesth_night"], max(1, stats["night_count"]))),
                    ("59. Экстренные операции по анестезиологам", "Экстренная операционная по врачу", self._distribution_lines(stats["anesth_emergency"], max(1, emergency))),
                    ("60. Плановые операции по анестезиологам", "Плановая операционная по врачу", self._distribution_lines(stats["anesth_planned"], max(1, planned))),
                    ("61. Длительность анестезии по анестезиологу", "Средняя длительность пособия по врачу", self._duration_distribution_lines(stats["anesthesiologist_durations"])),
                    ("62. Длительность операции по хирургу", "Средняя длительность операции по хирургу", self._duration_distribution_lines(stats["surgeon_durations"])),
                    ("63. Случаи без ключевого персонала", "Нет анестезиолога, хирурга или анестезистки", self._fmt_num(stats["missing_key_personnel"], 0)),
                ],
            ),
            (
                "Пациенты, маршруты и диагнозы",
                [
                    ("64. Пол пациентов", "Распределение пациентов по полу", self._distribution_lines(stats["genders"], total)),
                    ("65. Возрастные группы", "Возраст на момент операции", self._distribution_lines(stats["age_groups"], total)),
                    ("66. Дети / взрослые", "Возраст <18 / >=18", self._distribution_lines(stats["child_adult"], total)),
                    ("67. Профили/отделения поступления", "Откуда пациент поступил или к какому профилю относится", self._distribution_lines(stats["departments"], total)),
                    ("68. Отделение перевода", "Куда пациент переведен после окончания анестезии", self._distribution_lines(stats["transfers"], total)),
                    ("69. Топ диагнозов", "Самые частые диагнозы по коду и тексту", self._distribution_lines(stats["diagnoses"], total)),
                    ("70. Топ МКБ", "Самые частые трехзначные группы МКБ", self._distribution_lines(stats["mkb_classes"], total)),
                    ("71. Пациенты с несколькими операциями", "Один пациент или одна история болезни встречается больше одного раза", self._fmt_num(stats["repeated_patients"], 0)),
                    ("72. Повторные операции в один день", "Один пациент или одна история болезни встречается несколько раз в одну дату", self._fmt_num(stats["same_day_repeats"], 0)),
                    ("73. Группа крови и Rh", "Распределение по группе крови и резус-фактору", f"Группа: {self._distribution_lines(stats['blood_groups'], total)}<br/>Rh: {self._distribution_lines(stats['blood_rh'], total)}"),
                ],
            ),
            (
                "Качество заполнения",
                [
                    ("74. Без названия операции", "Не указано название операции", self._fmt_num(stats["missing_operation_name"], 0)),
                    ("75. Без анестезиолога", "Не указан анестезиолог", self._fmt_num(stats["missing_anesthesiologist"], 0)),
                    ("76. Без хирурга", "Не указан хирург", self._fmt_num(stats["missing_surgeon"], 0)),
                    ("77. Без анестезистки", "Не указана анестезистка", self._fmt_num(stats["missing_anesthetist"], 0)),
                    ("78. Без отделения перевода", "Не указано отделение перевода", self._fmt_num(stats["missing_transfer"], 0)),
                    ("79. Без роста или веса", "Не заполнен рост или вес пациента", self._fmt_num(stats["missing_height_weight"], 0)),
                ],
            ),
        ]

    @staticmethod
    def _format_value_cell(value: Any) -> str:
        if value is None:
            return "—"
        text = str(value).strip()
        if not text:
            return "—"
        if "<br/>" in text:
            return text
        return escape(text)

    @staticmethod
    def _indicator_key(row_name: str) -> str:
        match = re.match(r"\s*(\d+)\.", str(row_name or ""))
        if not match:
            return ""
        return f"ob{match.group(1)}"

    @classmethod
    def _filter_sections(
        cls,
        sections: list[tuple[str, list[tuple[str, str, str]]]],
        selected_indicators: Iterable[str] | None,
    ) -> list[tuple[str, list[tuple[str, str, str]]]]:
        if selected_indicators is None:
            return sections
        selected = {str(key or "").strip() for key in selected_indicators if str(key or "").strip()}
        if not selected:
            return []
        filtered_sections = []
        for title, rows in sections:
            filtered_rows = [row for row in rows if cls._indicator_key(row[0]) in selected]
            if filtered_rows:
                filtered_sections.append((title, filtered_rows))
        return filtered_sections

    def _render_sections_html(self, sections: list[tuple[str, list[tuple[str, str, str]]]]) -> str:
        blocks = []
        for title, rows in sections:
            rows_html = []
            for name, formula, value in rows:
                rows_html.append(
                    f"""
                    <tr>
                        <td>{escape(str(name))}</td>
                        <td>{escape(str(formula))}</td>
                        <td class="value">{self._format_value_cell(value)}</td>
                    </tr>
                    """
                )
            blocks.append(
                f"""
                <h2>{escape(title)}</h2>
                <table>
                    <tr>
                        <th>Показатель</th>
                        <th>Расчет</th>
                        <th class="value">Значение</th>
                    </tr>
                    {''.join(rows_html)}
                </table>
                """
            )
        return "\n".join(blocks)

    def generate_report_html(self, selected_indicators: Iterable[str] | None = None) -> str:
        stats = self._calculate_statistics()
        sections = self._filter_sections(self._section_rows(stats), selected_indicators)
        indicators_count = sum(len(rows) for _title, rows in sections)
        generated_at = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        period = f"{self._start_dt.strftime('%d.%m.%Y')} - {self._end_dt.strftime('%d.%m.%Y')}"
        html_body = self._render_sections_html(sections) if sections else "<p>Показатели не выбраны.</p>"

        return f"""
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
                <h1>Статистический отчет оперблока</h1>
                <p class="period">Период: {period}. Показателей: {indicators_count}</p>
                {html_body}
                <p class="footnote">Сформировано автоматически: {generated_at}</p>
            </div>
        </body>
        </html>
        """

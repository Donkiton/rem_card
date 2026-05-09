from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional


@dataclass(frozen=True)
class PatientAgeComponents:
    years: int
    months: int
    days: int
    total_months: int


def parse_date_value(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("T", " ")
    text = text.replace(",", ".").replace("/", ".")
    if text.isdigit() and len(text) == 8:
        text = f"{text[:2]}.{text[2:4]}.{text[4:]}"
    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y.%m.%d",
        "%Y.%m.%d %H:%M",
        "%Y.%m.%d %H:%M:%S",
        "%d.%m.%Y",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y %H:%M:%S",
    ):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def calculate_age_components(birth_date: Any, reference_date: Any = None) -> Optional[PatientAgeComponents]:
    birth = parse_date_value(birth_date)
    if birth is None:
        return None

    reference = parse_date_value(reference_date) if reference_date is not None else date.today()
    if reference is None or birth > reference:
        return None

    years = reference.year - birth.year
    if (reference.month, reference.day) < (birth.month, birth.day):
        years -= 1
    years = max(0, years)

    total_months = (reference.year - birth.year) * 12 + reference.month - birth.month
    if reference.day < birth.day:
        total_months -= 1
    total_months = max(0, total_months)

    months = max(0, total_months - years * 12)
    month_anniversary = _add_months(birth, total_months)
    days = max(0, (reference - month_anniversary).days)
    return PatientAgeComponents(years=years, months=months, days=days, total_months=total_months)


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + max(0, int(months))
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _pluralize(value: int, form1: str, form2: str, form5: str) -> str:
    number = abs(int(value)) % 100
    last_digit = number % 10
    if 11 <= number <= 19:
        return form5
    if last_digit == 1:
        return form1
    if 2 <= last_digit <= 4:
        return form2
    return form5


def format_patient_age_from_birth_date(birth_date: Any, reference_date: Any = None) -> str:
    components = calculate_age_components(birth_date, reference_date)
    if components is None:
        return ""
    return format_patient_age_components(components)


def format_patient_age_components(components: PatientAgeComponents) -> str:
    if components.total_months == 0:
        return f"{components.days} {_pluralize(components.days, 'день', 'дня', 'дней')}"

    if components.total_months < 12:
        months = components.total_months
        text = f"{months} {_pluralize(months, 'месяц', 'месяца', 'месяцев')}"
        if components.days > 0:
            text += f", {components.days} {_pluralize(components.days, 'день', 'дня', 'дней')}"
        return text

    years_text = f"{components.years} {_pluralize(components.years, 'год', 'года', 'лет')}"
    if components.total_months < 36 and components.months > 0:
        months_text = f"{components.months} {_pluralize(components.months, 'месяц', 'месяца', 'месяцев')}"
        return f"{years_text}, {months_text}"
    return years_text


def storage_age_from_birth_date(birth_date: Any, reference_date: Any = None) -> dict[str, Optional[int | str]]:
    components = calculate_age_components(birth_date, reference_date)
    if components is None:
        return {
            "patient_age": None,
            "patient_months": None,
            "patient_age_unit": None,
        }

    if components.total_months < 12:
        return {
            "patient_age": components.total_months,
            "patient_months": None,
            "patient_age_unit": "месяцы",
        }

    if components.total_months < 36:
        return {
            "patient_age": components.years,
            "patient_months": components.months,
            "patient_age_unit": "годы",
        }

    return {
        "patient_age": components.years,
        "patient_months": None,
        "patient_age_unit": "годы",
    }


def format_patient_age(age_value: Any, age_unit: Any = None, months: Any = None) -> str:
    if age_value in (None, ""):
        return ""
    try:
        age_number = int(age_value)
    except (TypeError, ValueError):
        return str(age_value).strip()

    unit = str(age_unit or "").strip().lower()
    if "меся" in unit:
        return f"{age_number} {_pluralize(age_number, 'месяц', 'месяца', 'месяцев')}"

    text = f"{age_number} {_pluralize(age_number, 'год', 'года', 'лет')}"
    try:
        months_number = int(months or 0)
    except (TypeError, ValueError):
        months_number = 0
    if 0 <= age_number < 3 and months_number > 0:
        text += f", {months_number} {_pluralize(months_number, 'месяц', 'месяца', 'месяцев')}"
    return text

from __future__ import annotations

from datetime import datetime, timedelta
import json

from rem_card.app.logger import logger


def patient_value(patient, key: str):
    if patient is None:
        return None
    if isinstance(patient, dict):
        return patient.get(key)
    return getattr(patient, key, None)


def patient_diagnosis_value(patient) -> str:
    return " ".join(
        filter(
            None,
            (
                str(patient_value(patient, "mkb_code") or "").strip(),
                str(patient_value(patient, "diagnosis_text") or "").strip(),
            ),
        )
    )


def patient_age_years(patient, *, exact: bool) -> float | int | None:
    if patient is None:
        return None
    try:
        from rem_card.app.patient_age import calculate_age_components

        components = calculate_age_components(patient_value(patient, "birth_date"), datetime.now())
        if components is not None:
            if exact:
                return round(
                    float(components.years)
                    + float(components.months) / 12.0
                    + float(components.days) / 365.25,
                    6,
                )
            return int(components.years)
    except Exception:
        pass

    age = patient_value(patient, "age")
    if age in (None, ""):
        return None
    try:
        number = float(age)
    except Exception:
        return None
    unit = str(patient_value(patient, "age_unit") or "").casefold()
    months = patient_value(patient, "age_months")
    if "меся" in unit:
        years = number / 12.0
    else:
        try:
            years = number + float(months or 0) / 12.0
        except Exception:
            years = number
    return round(years, 6) if exact else int(years)


def patient_sex(patient) -> str | None:
    value = str(patient_value(patient, "patient_gender") or "").strip().casefold()
    if value in {"м", "муж", "мужчина", "male", "m"}:
        return "male"
    if value in {"ж", "жен", "женщина", "female", "f"}:
        return "female"
    return None


def patient_weight_kg(service, admission_id: int) -> float | None:
    db = getattr(getattr(service, "patient_dao", None), "db", None)
    if db is None:
        db = getattr(getattr(service, "orders_dao", None), "db", None)
    if db is None or not hasattr(db, "fetch_one_remcard"):
        return None

    try:
        row = db.fetch_one_remcard(
            "SELECT intake_extra_json FROM admissions WHERE id = ?",
            (int(admission_id),),
        )
        raw = _row_value(row, "intake_extra_json")
        payload = json.loads(str(raw)) if raw else {}
        weight = _positive_float(payload.get("weight_kg") if isinstance(payload, dict) else None)
        if weight is not None:
            return weight
    except Exception as exc:
        logger.warning("Calculator context: failed to read RAO transfer weight: %s", exc)

    for field_name in ("future_rao_admission_id", "admission_id"):
        try:
            row = db.fetch_one_remcard(
                f"""
                SELECT weight_kg
                FROM operation_cases
                WHERE {field_name} = ?
                  AND weight_kg IS NOT NULL
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(admission_id),),
            )
            weight = _positive_float(_row_value(row, "weight_kg"))
            if weight is not None:
                return weight
        except Exception as exc:
            logger.debug("Calculator context: operation weight lookup failed: %s", exc)
    return None


def build_electrolyte_context(service, admission_id: int, patient) -> dict:
    context: dict = {}
    age_years = patient_age_years(patient, exact=False)
    if age_years is not None:
        context["age_years"] = age_years
    sex = patient_sex(patient)
    if sex:
        context["sex"] = sex
    weight = patient_weight_kg(service, admission_id)
    if weight is not None:
        context["weight_kg"] = weight
    urine = _recent_urine(service, patient, admission_id, hours=24, require_full_period=True)
    if urine is not None:
        context["urine_ml_day"] = urine
    return context


def build_burn_context(service, admission_id: int, patient, *, shift_date=None, now=None) -> dict:
    context: dict = {}
    display_name = ""
    if hasattr(patient, "get_display_name"):
        try:
            display_name = str(patient.get_display_name() or "").strip()
        except Exception:
            display_name = ""
    if not display_name:
        display_name = str(patient_value(patient, "full_name") or "").strip()
    if display_name:
        context["display_name"] = display_name

    for key in ("history_number", "mkb_code", "diagnosis_text"):
        value = patient_value(patient, key)
        if value not in (None, ""):
            context[key] = value
    age_years = patient_age_years(patient, exact=True)
    if age_years is not None:
        context["age_years"] = age_years
    weight = patient_weight_kg(service, admission_id)
    if weight is not None:
        context["weight_kg"] = weight
        context["weight_source"] = "карты поступления/перевода"

    now = now or datetime.now()
    context.update(burn_recent_diuresis(service, admission_id, now=now))
    from rem_card.services.burn_monitoring import load_burn_infused_volume
    from rem_card.services.shift_service import ShiftService

    start, end = ShiftService.get_day_period(shift_date or now)
    end = min(end, now)
    admission = patient_value(patient, "admission_datetime")
    if isinstance(admission, datetime):
        start = max(start, admission)
    try:
        context["infused_ml"] = load_burn_infused_volume(service, admission_id, start, end)
        context["infused_source"] = (
            f"Выполненные назначения: {start:%d.%m %H:%M}–{end:%d.%m %H:%M}. "
            "Если период расчёта отличается, скорректируйте объём."
        )
    except Exception as exc:
        logger.warning("Burn calculator: failed to load infused volume: %s", exc)
        context["infused_load_failed"] = True
        context["infused_source"] = "Не удалось загрузить введённый объём из назначений. Укажите вручную."
    return context


def burn_recent_diuresis(service, admission_id: int, *, now: datetime | None = None) -> dict:
    context: dict = {}
    fluid_service = getattr(service, "fluid_service", None)
    if fluid_service is None or not hasattr(fluid_service, "get_fluids_in_bounds"):
        return context
    now = now or datetime.now()
    try:
        fluids = fluid_service.get_fluids_in_bounds(admission_id, now - timedelta(hours=3), now) or []
    except Exception as exc:
        logger.warning("Calculator context: failed to load recent diuresis: %s", exc)
        return context
    total = sum(float(patient_value(item, "urine") or 0.0) for item in fluids)
    # Для этого блока отсутствие записи за час считается нулевым диурезом.
    # Делитель всегда 3, а не количество заполненных часов.
    context["urine_average_3h_ml"] = round(total / 3.0, 1)
    last_hour = []
    for item in fluids:
        timestamp = patient_value(item, "timestamp")
        if timestamp is None:
            continue
        try:
            if timestamp >= now - timedelta(hours=1):
                last_hour.append(item)
        except TypeError:
            continue
    context["urine_last_hour_ml"] = round(
        sum(float(patient_value(item, "urine") or 0.0) for item in last_hour),
        1,
    )
    return context


def _recent_urine(
    service,
    patient,
    admission_id: int,
    *,
    hours: int,
    require_full_period: bool,
) -> float | None:
    now = datetime.now()
    if require_full_period:
        admission_dt = patient_value(patient, "admission_datetime")
        if admission_dt is None:
            return None
        try:
            if (now - admission_dt).total_seconds() < hours * 3600:
                return None
        except Exception:
            return None
    fluid_service = getattr(service, "fluid_service", None)
    if fluid_service is None or not hasattr(fluid_service, "get_fluids_in_bounds"):
        return None
    try:
        fluids = fluid_service.get_fluids_in_bounds(
            admission_id,
            now - timedelta(hours=hours),
            now,
        )
        return round(sum(float(patient_value(item, "urine") or 0.0) for item in fluids or []), 1)
    except Exception as exc:
        logger.warning("Calculator context: failed to load %sh diuresis: %s", hours, exc)
        return None


def _row_value(row, key: str):
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except Exception:
        return None


def _positive_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(str(value).replace(",", "."))
    except Exception:
        return None
    return number if number > 0 else None

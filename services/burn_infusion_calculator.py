from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re


CLINICAL_RECOMMENDATION_ID = "687_3"
CLINICAL_RECOMMENDATION_YEAR = 2024

# Острые состояния из области применения КР, для которых калькулятор может быть
# предложен в карте пациента. L55 (солнечный ожог) и T95 (последствия ожогов)
# намеренно не включены в автоматическую активацию острого расчета.
ACUTE_BURN_MKB_FAMILIES = (
    "T20",
    "T21",
    "T22",
    "T23",
    "T24",
    "T25",
    "T27",
    "T29",
    "T30",
    "T31",
    "T32",
)

MODE_FIRST_24H = "first_24h"
MODE_DAY_2_3 = "day_2_3"
MODE_POST_SHOCK = "post_shock"
SUPPORTED_MODES = (MODE_FIRST_24H, MODE_DAY_2_3, MODE_POST_SHOCK)


def normalize_mkb_text(value: object) -> str:
    text = str(value or "").strip().upper()
    return text.translate(str.maketrans({"Т": "T", "С": "C"}))


def extract_mkb_families(value: object) -> tuple[str, ...]:
    text = normalize_mkb_text(value)
    families = re.findall(r"(?<![A-Z0-9])(T\d{2})(?:(?:\.|-)?\d+)?(?![A-Z0-9])", text)
    return tuple(dict.fromkeys(families))


def is_acute_burn_mkb(value: object) -> bool:
    return any(family in ACUTE_BURN_MKB_FAMILIES for family in extract_mkb_families(value))


def pediatric_maintenance_ml_per_kg(age_years: float) -> float:
    age = float(age_years)
    if age < (1.0 / 12.0) or age >= 18.0:
        raise ValueError("Физиологическая потребность по детской таблице задана для возраста от 1 месяца до 18 лет.")
    if age < 1.0:
        return 120.0
    if age < 2.0:
        return 100.0
    if age < 5.0:
        return 80.0
    if age < 10.0:
        return 60.0
    return 50.0


@dataclass(frozen=True)
class BurnInfusionInput:
    age_years: float
    weight_kg: float
    injury_datetime: datetime
    total_tbsa_percent: float
    superficial_tbsa_percent: float = 0.0
    deep_tbsa_percent: float = 0.0
    inhalation_injury: bool = False
    electrical_burn: bool = False
    burn_shock: bool = True
    infused_ml: float = 0.0
    urine_last_hour_ml: float | None = None
    urine_average_3h_ml: float | None = None
    older_age_reduction_divisor: float | None = None


@dataclass(frozen=True)
class BurnInfusionResult:
    mode: str
    period_label: str
    total_ml: float
    remaining_ml: float
    recommended_rate_ml_h: float
    burn_formula_ml: float
    maintenance_ml: float
    inhalation_extra_ml: float
    electrical_extra_ml: float
    age_reduction_divisor: float | None
    first_8h_ml: float | None
    next_16h_ml: float | None
    current_interval_label: str
    current_interval_remaining_ml: float
    next_interval_rate_ml_h: float | None
    elapsed_hours: float
    urine_target_min_ml_kg_h: float
    urine_target_max_ml_kg_h: float | None
    warnings: tuple[str, ...]
    calculation_trace: tuple[str, ...]


def _validate_input(data: BurnInfusionInput, *, now: datetime, mode: str) -> None:
    if not (1.0 / 12.0 <= float(data.age_years) <= 120.0):
        raise ValueError("Возраст должен быть от 1 месяца до 120 лет.")
    if not (0.5 <= float(data.weight_kg) <= 500.0):
        raise ValueError("Масса должна быть от 0,5 до 500 кг.")
    if data.injury_datetime > now:
        raise ValueError("Дата и время травмы не могут быть позже текущего времени.")
    for label, value in (
        ("Общая площадь ожога", data.total_tbsa_percent),
        ("Поверхностный ожог", data.superficial_tbsa_percent),
        ("Глубокий ожог", data.deep_tbsa_percent),
    ):
        if not (0.0 <= float(value) <= 100.0):
            raise ValueError(f"{label}: допустимо значение от 0 до 100%.")
    if float(data.total_tbsa_percent) <= 0.0:
        raise ValueError("Укажите общую площадь ожога больше 0%.")
    if float(data.superficial_tbsa_percent) + float(data.deep_tbsa_percent) > float(data.total_tbsa_percent) + 0.05:
        raise ValueError("Сумма поверхностного и глубокого ожога не может превышать общую площадь.")
    if float(data.infused_ml) < 0.0:
        raise ValueError("Введенный объем не может быть отрицательным.")
    for value in (data.urine_last_hour_ml, data.urine_average_3h_ml):
        if value is not None and float(value) < 0.0:
            raise ValueError("Диурез не может быть отрицательным.")
    if float(data.age_years) > 50.0 and mode in {MODE_FIRST_24H, MODE_DAY_2_3}:
        divisor = data.older_age_reduction_divisor
        if divisor is None or not (1.5 <= float(divisor) <= 2.0):
            raise ValueError("Для пациента старше 50 лет выберите коэффициент снижения объема от 1,5 до 2,0.")


def _first_day_components(data: BurnInfusionInput) -> tuple[float, float, float, float, float, float | None, tuple[str, ...]]:
    age = float(data.age_years)
    weight = float(data.weight_kg)
    tbsa_for_formula = min(float(data.total_tbsa_percent), 50.0)
    pediatric = age < 18.0
    factor = 3.0 if pediatric else 4.0
    burn_formula = factor * weight * tbsa_for_formula
    maintenance = pediatric_maintenance_ml_per_kg(age) * weight if pediatric else 0.0
    formula_subtotal = burn_formula + maintenance
    inhalation_extra = formula_subtotal * 0.15 if data.inhalation_injury else 0.0
    electrical_extra = formula_subtotal * 0.50 if data.electrical_burn else 0.0
    total_before_age_reduction = formula_subtotal + inhalation_extra + electrical_extra
    age_divisor = float(data.older_age_reduction_divisor) if age > 50.0 else None
    total = total_before_age_reduction / age_divisor if age_divisor else total_before_age_reduction

    trace = [
        f"Площадь для формулы: {tbsa_for_formula:g}% (максимум 50%; эритема не учитывается)",
        f"Ожоговая составляющая: {factor:g} мл × {weight:g} кг × {tbsa_for_formula:g}% = {burn_formula:.0f} мл",
    ]
    if pediatric:
        trace.append(f"Физиологическая потребность ребенка: {maintenance:.0f} мл/сут")
    if data.inhalation_injury:
        trace.append(f"Ингаляционная травма: +15% = {inhalation_extra:.0f} мл")
    if data.electrical_burn:
        trace.append(f"Электротравма: +50% = {electrical_extra:.0f} мл")
    if age_divisor:
        trace.append(f"Возраст старше 50 лет: объем уменьшен в {age_divisor:g} раза")
    trace.append(f"Расчетный объем первых суток: {total:.0f} мл")
    return total, burn_formula, maintenance, inhalation_extra, electrical_extra, age_divisor, tuple(trace)


def _urine_targets(data: BurnInfusionInput) -> tuple[float, float | None]:
    if float(data.age_years) >= 18.0:
        return 0.5, 1.0
    if float(data.weight_kg) <= 30.0:
        return 1.0, 2.0
    return 1.0, None


def _monitoring_warnings(
    data: BurnInfusionInput,
    *,
    target_min: float,
    target_max: float | None,
) -> list[str]:
    warnings: list[str] = []
    weight = float(data.weight_kg)
    upper_for_reduction = 1.0 if float(data.age_years) >= 18.0 else 2.0
    for label, urine_ml in (
        ("за последний час", data.urine_last_hour_ml),
        ("в среднем за 3 часа", data.urine_average_3h_ml),
    ):
        if urine_ml is None:
            continue
        normalized = float(urine_ml) / weight
        if normalized < target_min:
            warnings.append(
                f"Диурез {label} {normalized:.2f} мл/кг/ч ниже целевого; требуется клиническая оценка темпа инфузии."
            )
        elif normalized > upper_for_reduction:
            warnings.append(
                f"Диурез {label} {normalized:.2f} мл/кг/ч выше контрольного уровня; КР рекомендуют уменьшить темп/объем."
            )
        elif target_max is not None and normalized > target_max:
            warnings.append(f"Диурез {label} {normalized:.2f} мл/кг/ч выше целевого диапазона.")
    return warnings


def calculate_burn_infusion(
    data: BurnInfusionInput,
    *,
    mode: str = MODE_FIRST_24H,
    now: datetime | None = None,
) -> BurnInfusionResult:
    if mode not in SUPPORTED_MODES:
        raise ValueError("Неизвестный режим расчета инфузии.")
    current_time = now or datetime.now()
    _validate_input(data, now=current_time, mode=mode)

    elapsed_hours = max(0.0, (current_time - data.injury_datetime).total_seconds() / 3600.0)
    target_min, target_max = _urine_targets(data)
    warnings = _monitoring_warnings(data, target_min=target_min, target_max=target_max)

    if not data.burn_shock and mode in {MODE_FIRST_24H, MODE_DAY_2_3}:
        warnings.append("Ожоговый шок не отмечен: проверьте клинические показания к формульной инфузии.")
    threshold = 10.0 if float(data.age_years) < 18.0 else 15.0
    if float(data.total_tbsa_percent) <= threshold and mode in {MODE_FIRST_24H, MODE_DAY_2_3}:
        warnings.append(
            f"Площадь не превышает порог плановой внутривенной терапии по КР ({threshold:g}%); оцените возможность оральной регидратации."
        )
    if float(data.superficial_tbsa_percent) + float(data.deep_tbsa_percent) < float(data.total_tbsa_percent) - 0.05:
        warnings.append("Поверхностная и глубокая площади не покрывают всю общую площадь ожога.")

    if mode == MODE_POST_SHOCK:
        total = (
            1.5 * float(data.deep_tbsa_percent) * float(data.weight_kg)
            + 0.5 * float(data.superficial_tbsa_percent) * float(data.weight_kg)
        )
        remaining = max(0.0, total - float(data.infused_ml))
        if float(data.infused_ml) > total:
            warnings.append("Введенный объем превышает расчетный суточный объем выбранного периода.")
        trace = (
            f"Глубокий ожог: 1,5 мл × {data.deep_tbsa_percent:g}% × {data.weight_kg:g} кг",
            f"Поверхностный ожог: 0,5 мл × {data.superficial_tbsa_percent:g}% × {data.weight_kg:g} кг",
            f"Расчетный суточный объем после выхода из шока: {total:.0f} мл",
        )
        return BurnInfusionResult(
            mode=mode,
            period_label="После выхода из ожогового шока",
            total_ml=total,
            remaining_ml=remaining,
            recommended_rate_ml_h=remaining / 24.0,
            burn_formula_ml=total,
            maintenance_ml=0.0,
            inhalation_extra_ml=0.0,
            electrical_extra_ml=0.0,
            age_reduction_divisor=None,
            first_8h_ml=None,
            next_16h_ml=None,
            current_interval_label="Суточный ориентир",
            current_interval_remaining_ml=remaining,
            next_interval_rate_ml_h=None,
            elapsed_hours=elapsed_hours,
            urine_target_min_ml_kg_h=target_min,
            urine_target_max_ml_kg_h=target_max,
            warnings=tuple(warnings),
            calculation_trace=trace,
        )

    first_day_total, burn_formula, maintenance, inhalation_extra, electrical_extra, age_divisor, trace = (
        _first_day_components(data)
    )

    if mode == MODE_DAY_2_3:
        if elapsed_hours < 24.0:
            raise ValueError("Вторые сутки еще не наступили. Используйте режим первых 24 часов.")
        if elapsed_hours >= 72.0:
            raise ValueError("Третьи сутки завершены. Используйте режим после выхода из ожогового шока.")
        day_number = 2 if elapsed_hours < 48.0 else 3
        fraction = 0.5 if day_number == 2 else (1.0 / 3.0)
        period_start = 24.0 if day_number == 2 else 48.0
        hours_left = max(0.01, 24.0 - (elapsed_hours - period_start))
        total = first_day_total * fraction
        remaining = max(0.0, total - float(data.infused_ml))
        if float(data.infused_ml) > total:
            warnings.append("Введенный объем превышает расчетный объем выбранных суток.")
        fraction_label = "1/2" if day_number == 2 else "1/3"
        day_trace = trace + (
            f"{day_number}-и сутки: {fraction_label} от расчетного объема первых суток = {total:.0f} мл",
            f"Осталось {remaining:.0f} мл на {hours_left:.1f} ч",
        )
        return BurnInfusionResult(
            mode=mode,
            period_label=f"{day_number}-и сутки ожоговой болезни",
            total_ml=total,
            remaining_ml=remaining,
            recommended_rate_ml_h=remaining / hours_left,
            burn_formula_ml=burn_formula,
            maintenance_ml=maintenance,
            inhalation_extra_ml=inhalation_extra,
            electrical_extra_ml=electrical_extra,
            age_reduction_divisor=age_divisor,
            first_8h_ml=None,
            next_16h_ml=None,
            current_interval_label=f"Оставшаяся часть {day_number}-х суток",
            current_interval_remaining_ml=remaining,
            next_interval_rate_ml_h=None,
            elapsed_hours=elapsed_hours,
            urine_target_min_ml_kg_h=target_min,
            urine_target_max_ml_kg_h=target_max,
            warnings=tuple(warnings),
            calculation_trace=day_trace,
        )

    if elapsed_hours >= 24.0:
        raise ValueError("Первые 24 часа после травмы завершены. Выберите режим 2–3-х суток.")

    first_8h = first_day_total / 2.0
    next_16h = first_day_total / 2.0
    infused = float(data.infused_ml)
    if elapsed_hours < 8.0:
        hours_left = max(0.01, 8.0 - elapsed_hours)
        interval_label = "До конца первых 8 часов"
        interval_remaining = max(0.0, first_8h - infused)
        current_rate = interval_remaining / hours_left
        next_rate = next_16h / 16.0
        if infused > first_8h:
            warnings.append("Введенный объем превышает расчетную половину первых 8 часов.")
    else:
        hours_left = max(0.01, 24.0 - elapsed_hours)
        interval_label = "До конца первых 24 часов"
        interval_remaining = max(0.0, first_day_total - infused)
        current_rate = interval_remaining / hours_left
        next_rate = None
    remaining = max(0.0, first_day_total - infused)
    if infused > first_day_total:
        warnings.append("Введенный объем превышает расчетный объем первых суток.")
    first_day_trace = trace + (
        f"Первые 8 часов от момента травмы: {first_8h:.0f} мл",
        f"Следующие 16 часов: {next_16h:.0f} мл",
        f"Введено: {infused:.0f} мл; осталось: {remaining:.0f} мл",
    )
    return BurnInfusionResult(
        mode=mode,
        period_label="Первые 24 часа",
        total_ml=first_day_total,
        remaining_ml=remaining,
        recommended_rate_ml_h=current_rate,
        burn_formula_ml=burn_formula,
        maintenance_ml=maintenance,
        inhalation_extra_ml=inhalation_extra,
        electrical_extra_ml=electrical_extra,
        age_reduction_divisor=age_divisor,
        first_8h_ml=first_8h,
        next_16h_ml=next_16h,
        current_interval_label=interval_label,
        current_interval_remaining_ml=interval_remaining,
        next_interval_rate_ml_h=next_rate,
        elapsed_hours=elapsed_hours,
        urine_target_min_ml_kg_h=target_min,
        urine_target_max_ml_kg_h=target_max,
        warnings=tuple(warnings),
        calculation_trace=first_day_trace,
    )


__all__ = [
    "ACUTE_BURN_MKB_FAMILIES",
    "BurnInfusionInput",
    "BurnInfusionResult",
    "CLINICAL_RECOMMENDATION_ID",
    "CLINICAL_RECOMMENDATION_YEAR",
    "MODE_DAY_2_3",
    "MODE_FIRST_24H",
    "MODE_POST_SHOCK",
    "calculate_burn_infusion",
    "extract_mkb_families",
    "is_acute_burn_mkb",
    "pediatric_maintenance_ml_per_kg",
]

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


KCL_4_PERCENT_MMOL_PER_ML = 0.536
NACL_09_MMOL_PER_L = 154.0
NACL_3_PERCENT_MMOL_PER_L = 513.0
CREATININE_MMOL_PER_MG_DL = 0.0884

STATUS_ORDER = {
    "green": 0,
    "yellow": 1,
    "red": 2,
}

STATUS_LABELS = {
    "green": "зеленый",
    "yellow": "желтый",
    "red": "красный",
}


@dataclass(frozen=True)
class KidneyRisk:
    code: str
    label: str
    risk_level: str
    status: str
    allow_potassium_auto: bool
    allow_furosemide_adjustment: bool
    max_auto_potassium_mmol: float | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class FurosemideAdjustment:
    dose_mg_per_day: float
    suggested_mmol_per_day: float
    applied_mmol_per_day: float
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PotassiumResult:
    current: float
    target: float
    deficit_mmol: float
    deficit_kcl4_ml: float
    furosemide_suggested_mmol: float
    furosemide_applied_mmol: float
    total_daily_mmol: float | None
    total_kcl4_ml: float | None
    auto_recommendation_allowed: bool
    warnings: tuple[str, ...]
    messages: tuple[str, ...]


@dataclass(frozen=True)
class SodiumResult:
    current: float
    final_target: float
    daily_target: float
    tbw_l: float
    final_deficit_mmol: float
    daily_deficit_mmol: float
    nacl09_volume_l: float
    nacl3_volume_l: float
    warnings: tuple[str, ...]
    messages: tuple[str, ...]


@dataclass(frozen=True)
class ChlorideResult:
    current: float
    target: float
    deficit_mmol: float
    covered_by_kcl_mmol: float
    residual_deficit_mmol: float
    nacl09_volume_l: float
    nacl3_volume_l: float
    kcl4_volume_ml: float
    warnings: tuple[str, ...]
    messages: tuple[str, ...]


@dataclass(frozen=True)
class ElectrolyteRecommendation:
    kidney: KidneyRisk
    potassium: PotassiumResult | None
    sodium: SodiumResult | None
    chloride: ChlorideResult | None
    status: str
    status_reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    control_recommendations: tuple[str, ...]
    copy_text: str


KIDNEY_RISKS: dict[str, KidneyRisk] = {
    "normal": KidneyRisk(
        code="normal",
        label="Норма / ХБП нет",
        risk_level="normal",
        status="green",
        allow_potassium_auto=True,
        allow_furosemide_adjustment=True,
        max_auto_potassium_mmol=None,
    ),
    "ckd_c1_c2": KidneyRisk(
        code="ckd_c1_c2",
        label="ХБП C1-C2",
        risk_level="mild_ckd",
        status="green",
        allow_potassium_auto=True,
        allow_furosemide_adjustment=True,
        max_auto_potassium_mmol=None,
        warnings=("ХБП C1-C2: нужен контроль динамики K+, креатинина и диуреза.",),
    ),
    "ckd_c3": KidneyRisk(
        code="ckd_c3",
        label="ХБП C3",
        risk_level="moderate_ckd",
        status="yellow",
        allow_potassium_auto=True,
        allow_furosemide_adjustment=True,
        max_auto_potassium_mmol=None,
        warnings=("ХБП C3: контроль K+ и креатинина нужен чаще.",),
    ),
    "ckd_c4": KidneyRisk(
        code="ckd_c4",
        label="ХБП C4",
        risk_level="severe_ckd",
        status="yellow",
        allow_potassium_auto=True,
        allow_furosemide_adjustment=False,
        max_auto_potassium_mmol=40.0,
        warnings=(
            "ХБП C4: фуросемидная поправка не добавляется автоматически.",
            "ХБП C4: автоматический расчет K+ ограничен, нужна ручная клиническая оценка.",
        ),
    ),
    "ckd_c5": KidneyRisk(
        code="ckd_c5",
        label="ХБП C5 без диализа",
        risk_level="kidney_failure",
        status="red",
        allow_potassium_auto=False,
        allow_furosemide_adjustment=False,
        max_auto_potassium_mmol=None,
        warnings=("ХБП C5: автоматическое восполнение K+ не рекомендуется.",),
    ),
    "aki_diuresis": KidneyRisk(
        code="aki_diuresis",
        label="ОПН с сохраненным диурезом",
        risk_level="kidney_failure",
        status="yellow",
        allow_potassium_auto=True,
        allow_furosemide_adjustment=False,
        max_auto_potassium_mmol=40.0,
        warnings=(
            "ОПН с сохраненным диурезом: расчет показывается как математический, итог требует ручного подтверждения.",
        ),
    ),
    "oliguria": KidneyRisk(
        code="oliguria",
        label="Олигурия",
        risk_level="oliguria",
        status="red",
        allow_potassium_auto=False,
        allow_furosemide_adjustment=False,
        max_auto_potassium_mmol=None,
        warnings=("Олигурия: KCl автоматически не рекомендуется из-за риска гиперкалиемии.",),
    ),
    "anuria": KidneyRisk(
        code="anuria",
        label="Анурия",
        risk_level="anuria",
        status="red",
        allow_potassium_auto=False,
        allow_furosemide_adjustment=False,
        max_auto_potassium_mmol=None,
        warnings=("Анурия: высокий риск гиперкалиемии. Автоматическое восполнение K+ заблокировано.",),
    ),
    "dialysis": KidneyRisk(
        code="dialysis",
        label="Диализный пациент",
        risk_level="dialysis",
        status="red",
        allow_potassium_auto=False,
        allow_furosemide_adjustment=False,
        max_auto_potassium_mmol=None,
        warnings=(
            "Диализный пациент: коррекция K+/Na+/Cl- зависит от диализной программы и междиализного периода.",
        ),
    ),
}

KIDNEY_ALIASES = {
    "норма": "normal",
    "норма / хбп нет": "normal",
    "хбп нет": "normal",
    "хбп c1-c2": "ckd_c1_c2",
    "хбп c1–c2": "ckd_c1_c2",
    "хбп c3": "ckd_c3",
    "хбп c4": "ckd_c4",
    "хбп c5": "ckd_c5",
    "хбп c5 без диализа": "ckd_c5",
    "опн с сохраненным диурезом": "aki_diuresis",
    "опн с сохранённым диурезом": "aki_diuresis",
    "олигурия": "oliguria",
    "анурия": "anuria",
    "диализ": "dialysis",
    "диализный пациент": "dialysis",
}

GI_LOSS_LABELS = {
    "none": "нет",
    "vomiting": "рвота",
    "diarrhea": "диарея",
    "fistula_drainage": "свищи/дренажи",
}


def calculate_potassium_deficit(k_current: float, k_target: float, weight_kg: float) -> float:
    k_current = _non_negative_float(k_current, "Текущий K+")
    k_target = _non_negative_float(k_target, "Целевой K+")
    weight_kg = _positive_float(weight_kg, "Масса")
    return max(0.0, (k_target - k_current) * weight_kg * 0.4)


def calculate_kcl_4_percent_volume(potassium_mmol: float) -> float:
    potassium_mmol = _non_negative_float(potassium_mmol, "K+")
    return potassium_mmol / KCL_4_PERCENT_MMOL_PER_ML


def calculate_furosemide_potassium_adjustment(
    furosemide_mg_per_day: float,
    *,
    active_diuresis: bool = True,
) -> FurosemideAdjustment:
    dose = _non_negative_float(furosemide_mg_per_day, "Фуросемид")
    warnings: list[str] = []
    if dose <= 0:
        suggested = 0.0
    elif dose <= 20:
        suggested = 10.0
    elif dose <= 40:
        suggested = 20.0
    elif dose <= 80:
        suggested = 30.0
    else:
        suggested = 40.0
        warnings.append(
            "Высокая доза диуретика. Потери K+ считать по динамике анализов и диурезу."
        )

    applied = suggested if active_diuresis else 0.0
    if dose > 0 and not active_diuresis:
        warnings.append(
            "Активный диурез не указан: фуросемидная поправка не добавлена автоматически."
        )
    return FurosemideAdjustment(
        dose_mg_per_day=dose,
        suggested_mmol_per_day=suggested,
        applied_mmol_per_day=applied,
        warnings=tuple(warnings),
    )


def calculate_total_body_water(weight_kg: float, age_years: float, sex: str) -> float:
    weight_kg = _positive_float(weight_kg, "Масса")
    age_years = _non_negative_float(age_years, "Возраст")
    normalized_sex = _normalize_sex(sex)
    if normalized_sex == "male":
        factor = 0.6 if age_years < 65 else 0.5
    else:
        factor = 0.5 if age_years < 65 else 0.45
    return weight_kg * factor


def calculate_sodium_deficit(
    na_current: float,
    na_target: float,
    weight_kg: float,
    age_years: float,
    sex: str,
) -> float:
    na_current = _non_negative_float(na_current, "Текущий Na+")
    na_target = _non_negative_float(na_target, "Целевой Na+")
    tbw_l = calculate_total_body_water(weight_kg, age_years, sex)
    return max(0.0, tbw_l * (na_target - na_current))


def calculate_chloride_deficit(cl_current: float, cl_target: float, weight_kg: float) -> float:
    cl_current = _non_negative_float(cl_current, "Текущий Cl-")
    cl_target = _non_negative_float(cl_target, "Целевой Cl-")
    weight_kg = _positive_float(weight_kg, "Масса")
    return max(0.0, weight_kg * 0.2 * (cl_target - cl_current))


def calculate_egfr_ckd_epi_2021(creatinine_mmol_l: float, age_years: float, sex: str) -> float:
    creatinine_mmol_l = _positive_float(creatinine_mmol_l, "Креатинин")
    age_years = _non_negative_float(age_years, "Возраст")
    normalized_sex = _normalize_sex(sex)
    scr_mg_dl = creatinine_mmol_l / CREATININE_MMOL_PER_MG_DL
    if normalized_sex == "female":
        k_value = 0.7
        alpha = -0.241
        sex_factor = 1.012
    else:
        k_value = 0.9
        alpha = -0.302
        sex_factor = 1.0
    ratio = scr_mg_dl / k_value
    return 142.0 * (min(ratio, 1.0) ** alpha) * (max(ratio, 1.0) ** -1.200) * (0.9938 ** age_years) * sex_factor


def classify_egfr_kidney_state(egfr_ml_min: float | None) -> tuple[str, str]:
    if egfr_ml_min is None:
        return "normal", "не рассчитана"
    egfr = _non_negative_float(egfr_ml_min, "СКФ")
    if egfr >= 90:
        return "ckd_c1_c2", "ХБП C1 / СКФ G1"
    if egfr >= 60:
        return "ckd_c1_c2", "ХБП C2 / СКФ G2"
    if egfr >= 45:
        return "ckd_c3", "ХБП C3a / СКФ G3a"
    if egfr >= 30:
        return "ckd_c3", "ХБП C3b / СКФ G3b"
    if egfr >= 15:
        return "ckd_c4", "ХБП C4 / СКФ G4"
    return "ckd_c5", "ХБП C5 / СКФ G5"


def calculate_kidney_risk(kidney_state: str | None) -> KidneyRisk:
    key = str(kidney_state or "normal").strip()
    normalized = KIDNEY_ALIASES.get(key.casefold(), key)
    return KIDNEY_RISKS.get(normalized, KIDNEY_RISKS["normal"])


def build_electrolyte_recommendation(
    *,
    weight_kg: float,
    age_years: float | None = None,
    sex: str = "male",
    k_current: float | None = None,
    k_target: float = 4.0,
    na_current: float | None = None,
    na_target: float = 140.0,
    cl_current: float | None = None,
    cl_target: float = 100.0,
    kidney_state: str | None = "normal",
    furosemide_mg_per_day: float = 0.0,
    active_diuresis: bool = True,
    spironolactone: bool = False,
    potassium_sparing: bool = False,
    gi_losses: str = "none",
    magnesium_mmol_l: float | None = None,
    creatinine_mmol_l: float | None = None,
    egfr_ml_min: float | None = None,
    urine_ml_day: float | None = None,
    hco3_mmol_l: float | None = None,
    potassium_symptoms_or_arrhythmia: bool = False,
) -> ElectrolyteRecommendation:
    weight = _positive_float(weight_kg, "Масса")
    age = _optional_non_negative_float(age_years, "Возраст")
    normalized_sex = _normalize_sex(sex)
    kidney = calculate_kidney_risk(kidney_state)
    furosemide = _non_negative_float(furosemide_mg_per_day, "Фуросемид")
    mg = _optional_non_negative_float(magnesium_mmol_l, "Mg2+")
    _optional_non_negative_float(creatinine_mmol_l, "Креатинин")
    _optional_non_negative_float(egfr_ml_min, "СКФ")
    _optional_non_negative_float(urine_ml_day, "Диурез")
    _optional_non_negative_float(hco3_mmol_l, "HCO3-")

    status = kidney.status
    status_reasons: list[str] = []
    warnings: list[str] = list(kidney.warnings)
    if kidney.status != "green":
        status_reasons.append(f"Функция почек: {kidney.label}.")

    potassium = _build_potassium_result(
        weight=weight,
        k_current=k_current,
        k_target=k_target,
        kidney=kidney,
        furosemide_mg_per_day=furosemide,
        active_diuresis=active_diuresis,
        spironolactone=bool(spironolactone),
        potassium_sparing=bool(potassium_sparing),
        magnesium_mmol_l=mg,
        symptoms_or_arrhythmia=bool(potassium_symptoms_or_arrhythmia),
    )
    if potassium is not None:
        warnings.extend(potassium.warnings)
        if potassium.current < 2.1:
            status = _max_status(status, "red")
            status_reasons.append("K+ ниже 2,1 ммоль/л.")
        elif potassium.current < 2.5:
            severity = "red" if potassium_symptoms_or_arrhythmia else "yellow"
            status = _max_status(status, severity)
            if potassium_symptoms_or_arrhythmia:
                status_reasons.append("K+ ниже 2,5 ммоль/л с симптомами/аритмией.")
            else:
                status_reasons.append("K+ 2,1-2,5 ммоль/л.")
        if furosemide > 40:
            status = _max_status(status, "yellow")
            status_reasons.append("Фуросемид больше 40 мг/сут.")
        if mg is None and potassium.deficit_mmol > 0:
            status = _max_status(status, "yellow")
            status_reasons.append("Mg2+ не введен при дефиците K+.")
        if not potassium.auto_recommendation_allowed:
            status = _max_status(status, "red")
            status_reasons.append("Автоматический итоговый объем KCl заблокирован.")

    severe_spironolactone_combo = bool(spironolactone) and kidney.risk_level in {
        "severe_ckd",
        "kidney_failure",
        "oliguria",
        "anuria",
        "dialysis",
    }
    if severe_spironolactone_combo:
        status = _max_status(status, "red")
        status_reasons.append("Спиронолактон на фоне тяжелого почечного риска.")

    sodium = _build_sodium_result(
        weight=weight,
        age=age,
        sex=normalized_sex,
        na_current=na_current,
        na_target=na_target,
    )
    if sodium is not None:
        warnings.extend(sodium.warnings)
        if sodium.current < 120:
            status = _max_status(status, "red")
            status_reasons.append("Na+ ниже 120 ммоль/л.")
        elif sodium.current < 125:
            status = _max_status(status, "yellow")
            status_reasons.append("Na+ ниже 125 ммоль/л.")
        if sodium.final_target - sodium.current > 8:
            status = _max_status(status, "yellow")
            status_reasons.append("Конечная цель Na+ выше безопасного суточного шага.")
        if kidney.status != "green":
            warnings.append(
                "При почечной недостаточности расчет Na+ не учитывает риск перегрузки объемом."
            )

    kcl_for_chloride_mmol = 0.0
    if potassium is not None and potassium.auto_recommendation_allowed:
        kcl_for_chloride_mmol = max(0.0, potassium.total_daily_mmol or 0.0)

    chloride = _build_chloride_result(
        weight=weight,
        cl_current=cl_current,
        cl_target=cl_target,
        kcl_for_chloride_mmol=kcl_for_chloride_mmol,
    )
    if chloride is not None:
        warnings.extend(chloride.warnings)
        if chloride.current < 90:
            status = _max_status(status, "yellow")
            status_reasons.append("Cl- выраженно снижен.")
        if kidney.status != "green":
            warnings.append(
                "При почечной недостаточности расчет Cl- не учитывает риск перегрузки объемом."
            )

    gi_loss_key = str(gi_losses or "none")
    if gi_loss_key != "none":
        warnings.append(f"Потери ЖКТ: {GI_LOSS_LABELS.get(gi_loss_key, gi_loss_key)}. Нужна оценка текущих потерь.")

    status_reasons = _deduplicate(status_reasons) or ["Критичных ограничений по введенным данным не найдено."]
    warnings = _deduplicate(warnings)
    control_recommendations = _build_control_recommendations(potassium)
    recommendation = ElectrolyteRecommendation(
        kidney=kidney,
        potassium=potassium,
        sodium=sodium,
        chloride=chloride,
        status=status,
        status_reasons=tuple(status_reasons),
        warnings=tuple(warnings),
        control_recommendations=control_recommendations,
        copy_text="",
    )
    return ElectrolyteRecommendation(
        kidney=recommendation.kidney,
        potassium=recommendation.potassium,
        sodium=recommendation.sodium,
        chloride=recommendation.chloride,
        status=recommendation.status,
        status_reasons=recommendation.status_reasons,
        warnings=recommendation.warnings,
        control_recommendations=recommendation.control_recommendations,
        copy_text=_build_copy_text(
            recommendation,
            age_years=age,
            sex=normalized_sex,
            weight_kg=weight,
            furosemide_mg_per_day=furosemide,
            spironolactone=bool(spironolactone),
        ),
    )


def _build_potassium_result(
    *,
    weight: float,
    k_current: float | None,
    k_target: float,
    kidney: KidneyRisk,
    furosemide_mg_per_day: float,
    active_diuresis: bool,
    spironolactone: bool,
    potassium_sparing: bool,
    magnesium_mmol_l: float | None,
    symptoms_or_arrhythmia: bool,
) -> PotassiumResult | None:
    if k_current is None:
        return None
    current = _non_negative_float(k_current, "Текущий K+")
    target = _non_negative_float(k_target, "Целевой K+")
    deficit = calculate_potassium_deficit(current, target, weight)
    deficit_volume = calculate_kcl_4_percent_volume(deficit)
    messages: list[str] = []
    warnings: list[str] = []
    if deficit <= 0:
        messages.append("Дефицита K+ до выбранной цели нет.")
    if current < 2.5:
        warnings.append("Тяжелая гипокалиемия: нужен контроль ЭКГ и K+ через 4-6 часов.")
    if symptoms_or_arrhythmia:
        warnings.append("Симптомы/аритмия: коррекция K+ требует неотложной клинической оценки.")
    if magnesium_mmol_l is None and deficit > 0:
        warnings.append("Mg2+ не введен: при гипомагниемии коррекция K+ может быть неэффективной.")

    furosemide_adjustment = calculate_furosemide_potassium_adjustment(
        furosemide_mg_per_day,
        active_diuresis=active_diuresis,
    )
    warnings.extend(furosemide_adjustment.warnings)
    applied_furosemide = furosemide_adjustment.applied_mmol_per_day

    if not kidney.allow_furosemide_adjustment and applied_furosemide > 0:
        applied_furosemide = 0.0
        warnings.append("Почечный риск: фуросемидная поправка не добавлена автоматически.")
    if spironolactone:
        if applied_furosemide > 0:
            applied_furosemide = 0.0
        warnings.append(
            "Спиронолактон повышает риск гиперкалиемии. Восполнение K+ только с контролем K+, креатинина, диуреза и ЭКГ."
        )
    if potassium_sparing:
        warnings.append("иАПФ/БРА/калийсберегающие препараты повышают риск гиперкалиемии.")

    severe_spironolactone_combo = spironolactone and kidney.risk_level in {
        "severe_ckd",
        "kidney_failure",
        "oliguria",
        "anuria",
        "dialysis",
    }
    auto_allowed = kidney.allow_potassium_auto and not severe_spironolactone_combo

    total_daily: float | None = None
    total_volume: float | None = None
    if auto_allowed:
        total_daily = deficit + applied_furosemide
        if kidney.max_auto_potassium_mmol is not None and total_daily > kidney.max_auto_potassium_mmol:
            total_daily = kidney.max_auto_potassium_mmol
            warnings.append(
                f"{kidney.label}: автоматический итог K+ ограничен {_fmt_mmol(total_daily)} ммоль/сут."
            )
        total_volume = calculate_kcl_4_percent_volume(total_daily)
    else:
        warnings.append("Автоматический итоговый объем KCl не рекомендован.")

    return PotassiumResult(
        current=current,
        target=target,
        deficit_mmol=deficit,
        deficit_kcl4_ml=deficit_volume,
        furosemide_suggested_mmol=furosemide_adjustment.suggested_mmol_per_day,
        furosemide_applied_mmol=applied_furosemide,
        total_daily_mmol=total_daily,
        total_kcl4_ml=total_volume,
        auto_recommendation_allowed=auto_allowed,
        warnings=tuple(warnings),
        messages=tuple(messages),
    )


def _build_sodium_result(
    *,
    weight: float,
    age: float | None,
    sex: str,
    na_current: float | None,
    na_target: float,
) -> SodiumResult | None:
    if na_current is None:
        return None
    if age is None:
        raise ValueError("Для расчета Na+ укажите возраст.")
    current = _non_negative_float(na_current, "Текущий Na+")
    final_target = _non_negative_float(na_target, "Целевой Na+")
    tbw = calculate_total_body_water(weight, age, sex)
    daily_target = min(final_target, current + 8.0) if final_target > current else final_target
    final_deficit = max(0.0, tbw * (final_target - current))
    daily_deficit = max(0.0, tbw * (daily_target - current))
    warnings: list[str] = []
    messages: list[str] = []
    if final_deficit <= 0:
        messages.append("Дефицита Na+ до выбранной цели нет.")
    if current < 120:
        warnings.append("Na+ <120 ммоль/л: высокий риск, нужна ручная клиническая оценка.")
    if final_target - current > 8:
        warnings.append(
            "Конечная цель Na+ выше безопасной суточной цели. Расчет растворов выполнен к цели на ближайшие 24 часа."
        )
    warnings.append(
        "Объем NaCl является математическим расчетом, не готовой схемой инфузии. Для Na+ критична скорость коррекции."
    )
    return SodiumResult(
        current=current,
        final_target=final_target,
        daily_target=daily_target,
        tbw_l=tbw,
        final_deficit_mmol=final_deficit,
        daily_deficit_mmol=daily_deficit,
        nacl09_volume_l=daily_deficit / NACL_09_MMOL_PER_L if daily_deficit > 0 else 0.0,
        nacl3_volume_l=daily_deficit / NACL_3_PERCENT_MMOL_PER_L if daily_deficit > 0 else 0.0,
        warnings=tuple(warnings),
        messages=tuple(messages),
    )


def _build_chloride_result(
    *,
    weight: float,
    cl_current: float | None,
    cl_target: float,
    kcl_for_chloride_mmol: float,
) -> ChlorideResult | None:
    if cl_current is None:
        return None
    current = _non_negative_float(cl_current, "Текущий Cl-")
    target = _non_negative_float(cl_target, "Целевой Cl-")
    deficit = calculate_chloride_deficit(current, target, weight)
    covered = min(deficit, max(0.0, kcl_for_chloride_mmol))
    residual = max(0.0, deficit - covered)
    warnings: list[str] = []
    messages: list[str] = []
    if deficit <= 0:
        messages.append("Дефицита Cl- до выбранной цели нет.")
    if covered > 0:
        messages.append("Часть дефицита Cl- будет закрыта при восполнении KCl.")
    if current < 90:
        warnings.append("Cl- выраженно снижен: оцените КОС/HCO3- и текущие потери.")
    return ChlorideResult(
        current=current,
        target=target,
        deficit_mmol=deficit,
        covered_by_kcl_mmol=covered,
        residual_deficit_mmol=residual,
        nacl09_volume_l=residual / NACL_09_MMOL_PER_L if residual > 0 else 0.0,
        nacl3_volume_l=residual / NACL_3_PERCENT_MMOL_PER_L if residual > 0 else 0.0,
        kcl4_volume_ml=deficit / KCL_4_PERCENT_MMOL_PER_ML if deficit > 0 else 0.0,
        warnings=tuple(warnings),
        messages=tuple(messages),
    )


def _build_control_recommendations(potassium: PotassiumResult | None) -> tuple[str, ...]:
    recommendations = [
        "K+, Na+, Cl-",
        "Mg2+",
        "креатинин/СКФ",
        "диурез",
    ]
    if potassium is not None and (potassium.current < 2.5 or not potassium.auto_recommendation_allowed):
        recommendations.append("ЭКГ при тяжелой гипокалиемии или высоком риске коррекции K+")
    return tuple(recommendations)


def _build_copy_text(
    recommendation: ElectrolyteRecommendation,
    *,
    age_years: float | None,
    sex: str,
    weight_kg: float,
    furosemide_mg_per_day: float,
    spironolactone: bool,
) -> str:
    lines: list[str] = [
        f"Пациент: {_sex_display(sex)}, { _fmt_mmol(age_years) if age_years is not None else '-' } лет, {_fmt_mmol(weight_kg)} кг.",
        f"Функция почек: {recommendation.kidney.label}.",
        f"Фуросемид: {_fmt_mmol(furosemide_mg_per_day)} мг/сут.",
        f"Спиронолактон: {'да' if spironolactone else 'нет'}.",
        "",
    ]
    if recommendation.potassium is not None:
        k = recommendation.potassium
        lines.extend(
            [
                f"K+ {_fmt_mmol(k.current)} -> цель {_fmt_mmol(k.target)}:",
                f"дефицит {_fmt_mmol(k.deficit_mmol)} ммоль, 4% KCl ~= {_fmt_ml(k.deficit_kcl4_ml)} мл.",
                f"С учетом фуросемида: +{_fmt_mmol(k.furosemide_applied_mmol)} ммоль/сут.",
            ]
        )
        if k.auto_recommendation_allowed and k.total_daily_mmol is not None and k.total_kcl4_ml is not None:
            lines.append(
                f"Итого: ~{_fmt_mmol(k.total_daily_mmol)} ммоль, 4% KCl ~={_fmt_ml(k.total_kcl4_ml)} мл/сут."
            )
        else:
            lines.append("Итого: автоматический объем KCl не рекомендован, показан математический расчет.")
        lines.append("")
    if recommendation.sodium is not None:
        na = recommendation.sodium
        lines.extend(
            [
                f"Na+ {_fmt_mmol(na.current)} -> суточная цель {_fmt_mmol(na.daily_target)}, конечная цель {_fmt_mmol(na.final_target)}:",
                f"TBW {_fmt_mmol(na.tbw_l)} л, дефицит до суточной цели {_fmt_mmol(na.daily_deficit_mmol)} ммоль.",
                f"NaCl 0,9% математически ~{_fmt_l(na.nacl09_volume_l)} л, NaCl 3% ~{_fmt_l(na.nacl3_volume_l)} л.",
                "",
            ]
        )
    if recommendation.chloride is not None:
        cl = recommendation.chloride
        lines.extend(
            [
                f"Cl- {_fmt_mmol(cl.current)} -> цель {_fmt_mmol(cl.target)}:",
                f"дефицит {_fmt_mmol(cl.deficit_mmol)} ммоль.",
                f"Покрывается через KCl: {_fmt_mmol(cl.covered_by_kcl_mmol)} ммоль.",
                f"Остаточный дефицит Cl-: {_fmt_mmol(cl.residual_deficit_mmol)} ммоль.",
                f"NaCl 0,9% математически ~{_fmt_l(cl.nacl09_volume_l)} л, NaCl 3% ~{_fmt_l(cl.nacl3_volume_l)} л.",
                "",
            ]
        )
    lines.extend(
        [
            f"Безопасность: {STATUS_LABELS.get(recommendation.status, recommendation.status)}.",
            "Причины статуса:",
            *[f"- {reason}" for reason in recommendation.status_reasons],
        ]
    )
    if recommendation.warnings:
        lines.extend(["", "Предупреждения:", *[f"- {warning}" for warning in recommendation.warnings]])
    lines.extend(["", "Рекомендованный контроль:", *[f"- {item}" for item in recommendation.control_recommendations]])
    return "\n".join(lines)


def _normalize_sex(sex: str | None) -> str:
    value = str(sex or "male").strip().casefold()
    if value in {"female", "woman", "женщина", "ж", "f"}:
        return "female"
    return "male"


def _sex_display(sex: str) -> str:
    return "женщина" if _normalize_sex(sex) == "female" else "мужчина"


def _non_negative_float(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}: нужно числовое значение.") from exc
    if result < 0:
        raise ValueError(f"{label}: отрицательное значение запрещено.")
    return result


def _positive_float(value: Any, label: str) -> float:
    result = _non_negative_float(value, label)
    if result <= 0:
        raise ValueError(f"{label}: значение должно быть больше 0.")
    return result


def _optional_non_negative_float(value: Any, label: str) -> float | None:
    if value is None or value == "":
        return None
    return _non_negative_float(value, label)


def _max_status(left: str, right: str) -> str:
    return left if STATUS_ORDER.get(left, 0) >= STATUS_ORDER.get(right, 0) else right


def _deduplicate(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        result.append(text)
        seen.add(text)
    return result


def _fmt_mmol(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.1f}".replace(".", ",")


def _fmt_ml(value: float | None) -> str:
    if value is None:
        return "-"
    return str(int(round(value)))


def _fmt_l(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}".replace(".", ",")

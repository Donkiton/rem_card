from __future__ import annotations

import math
from typing import Any


VITAL_RANGES: dict[str, tuple[str, float, float, bool]] = {
    "sys": ("АД систолическое", 0, 300, True),
    "dia": ("АД диастолическое", 0, 300, True),
    "pulse": ("Пульс", 0, 300, True),
    "temp": ("Температура", 0.0, 45.0, False),
    "spo2": ("SpO2", 0, 100, True),
    "rr": ("ЧДД", 0, 100, True),
    "cvp": ("ЦВД", -1, 50, True),
}


def _validate_value(name: str, value: Any) -> None:
    if value is None:
        return
    label, minimum, maximum, integer_only = VITAL_RANGES[name]
    if isinstance(value, bool):
        raise ValueError(f"Некорректное значение показателя «{label}».")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Некорректное значение показателя «{label}».") from exc
    if not math.isfinite(numeric):
        raise ValueError(f"Некорректное значение показателя «{label}».")
    if integer_only and not numeric.is_integer():
        raise ValueError(f"Показатель «{label}» должен быть целым числом.")
    if not minimum <= numeric <= maximum:
        raise ValueError(
            f"Значение показателя «{label}» должно быть в диапазоне "
            f"от {minimum:g} до {maximum:g}."
        )


def validate_vital_values(
    *,
    sys: Any = None,
    dia: Any = None,
    pulse: Any = None,
    temp: Any = None,
    spo2: Any = None,
    rr: Any = None,
    cvp: Any = None,
) -> None:
    values = {
        "sys": sys,
        "dia": dia,
        "pulse": pulse,
        "temp": temp,
        "spo2": spo2,
        "rr": rr,
        "cvp": cvp,
    }
    for name, value in values.items():
        _validate_value(name, value)
    if sys is not None and dia is not None and float(dia) > float(sys):
        raise ValueError("АД диастолическое не может быть выше систолического.")


def validate_vital_dto(dto: Any) -> None:
    validate_vital_values(
        sys=getattr(dto, "sys", None),
        dia=getattr(dto, "dia", None),
        pulse=getattr(dto, "pulse", None),
        temp=getattr(dto, "temp", None),
        spo2=getattr(dto, "spo2", None),
        rr=getattr(dto, "rr", None),
        cvp=getattr(dto, "cvp", None),
    )

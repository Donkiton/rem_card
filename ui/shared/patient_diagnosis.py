def format_patient_diagnosis(patient, *, empty_text: str = "-") -> str:
    """Формирует диагноз пациента с сохранённым кодом МКБ, если он указан."""
    diagnosis_text = str(getattr(patient, "diagnosis_text", None) or "").strip()
    diagnosis_code = str(
        getattr(patient, "mkb_code", None)
        or getattr(patient, "diagnosis_code", None)
        or ""
    ).strip()

    if diagnosis_code and diagnosis_text:
        return f"{diagnosis_code} - {diagnosis_text}"
    return diagnosis_text or diagnosis_code or empty_text

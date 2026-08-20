def build_status_signature(status_dto):
    if status_dto is None:
        return None
    status = getattr(status_dto, "status", None)
    status_value = getattr(status, "value", status)
    return (
        status_value,
        getattr(status_dto, "reason_type", None),
        getattr(status_dto, "reason_text", None),
        str(getattr(status_dto, "start_time", None)),
        str(getattr(status_dto, "end_time", None)),
        getattr(status_dto, "revision", None),
    )


def build_w1_bed_row_signature(patient, runtime_snapshot=None):
    runtime = dict(runtime_snapshot or {})
    latest_values = runtime.get("latest_values") or {}
    settings = runtime.get("settings") or {}
    return (
        getattr(patient, "id", None),
        getattr(patient, "patient_id", None),
        getattr(patient, "bed_number", None),
        getattr(patient, "full_name", None),
        getattr(patient, "history_number", None),
        getattr(patient, "diagnosis_text", None),
        getattr(patient, "patient_age", None),
        getattr(patient, "patient_age_unit", None),
        str(getattr(patient, "admission_datetime", None)),
        build_status_signature(runtime.get("status")),
        bool(runtime.get("card_exists", False)),
        bool(runtime.get("yest_exists", False)),
        bool(runtime.get("plan_card_available", False)),
        bool(runtime.get("plan_card_exists", False)),
        str(runtime.get("plan_card_target_date")),
        tuple(sorted(dict(latest_values).items())),
        tuple(sorted(dict(settings).items())),
        int(runtime.get("outcome_delay_min") or 30),
    )

from typing import Any, Mapping, Sequence

from rem_card.data.dto.remcard_dto import PatientStatus


_STATUS_GROUP_ORDER = {
    PatientStatus.ACTIVE: 0,
    PatientStatus.OUT: 1,
    PatientStatus.OR: 2,
    PatientStatus.TRANSFERRED: 3,
    PatientStatus.DEAD: 4,
}


def sort_patients_for_w1(
    patients: Sequence[Any],
    runtime_snapshot: Mapping[Any, Mapping[str, Any]],
) -> list[Any]:
    """Return patients grouped by current W1 status, then by bed number."""
    indexed_patients = list(enumerate(patients))
    indexed_patients.sort(
        key=lambda item: _patient_sort_key(
            patient=item[1],
            runtime_snapshot=runtime_snapshot,
            original_index=item[0],
        )
    )
    return [patient for _, patient in indexed_patients]


def _patient_sort_key(
    patient: Any,
    runtime_snapshot: Mapping[Any, Mapping[str, Any]],
    original_index: int,
) -> tuple[int, int, int]:
    status = _extract_status(_runtime_entry(runtime_snapshot, getattr(patient, "id", None)))
    group_index = _STATUS_GROUP_ORDER.get(status, _STATUS_GROUP_ORDER[PatientStatus.ACTIVE])
    return (group_index, _bed_number(patient), original_index)


def _runtime_entry(
    runtime_snapshot: Mapping[Any, Mapping[str, Any]],
    admission_id: Any,
) -> Mapping[str, Any]:
    if admission_id is None:
        return {}

    keys = [admission_id]
    try:
        keys.append(int(admission_id))
    except (TypeError, ValueError):
        pass
    keys.append(str(admission_id))

    for key in keys:
        entry = runtime_snapshot.get(key)
        if entry is not None:
            return entry
    return {}


def _extract_status(runtime_entry: Mapping[str, Any]) -> PatientStatus | None:
    status_event = runtime_entry.get("status") if runtime_entry else None
    status = getattr(status_event, "status", status_event)

    if isinstance(status, PatientStatus):
        return status

    if status is None:
        return None

    try:
        return PatientStatus(str(status))
    except ValueError:
        return None


def _bed_number(patient: Any) -> int:
    try:
        return int(getattr(patient, "bed_number", 999_999) or 999_999)
    except (TypeError, ValueError):
        return 999_999

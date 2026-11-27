"""Apply committed bed changes without allowing older reads to undo them."""
from datetime import datetime, timedelta
from functools import partial

from rem_card.data.dto.remcard_dto import PatientDTO, PatientStatus, PatientStatusEventDTO
from rem_card.services.shift_service import ShiftService


def require_beds_change(beds, change_id, *, reset=False):
    if reset:
        beds._beds_change_floor = 0
        beds._beds_read_epoch = getattr(beds, "_beds_read_epoch", 0) + 1
    beds._beds_change_floor = max(getattr(beds, "_beds_change_floor", 0), int(change_id or 0))


def accept_beds_snapshot(beds, snapshot):
    snapshot = snapshot or {}
    current_epoch = getattr(beds, "_beds_read_epoch", 0)
    if snapshot.get("_read_epoch", current_epoch) != current_epoch:
        return False
    change_id = int(snapshot.get("change_id") or 0)
    if change_id < getattr(beds, "_beds_change_floor", 0):
        return False
    require_beds_change(beds, change_id)
    return True


def load_beds_snapshot(beds, builder, *args):
    epoch = getattr(beds, "_beds_read_epoch", 0)
    snapshot = builder(*args, include_change_cursor=True)
    if int(snapshot.get("change_id") or 0) < getattr(beds, "_beds_change_floor", 0):
        snapshot = builder(*args, include_change_cursor=True, force_central=True)
    return dict(snapshot, _read_epoch=epoch)


def bind_admission_commits(journal, beds):
    journal.admissionCommitted.connect(partial(show_committed_admission, beds))


def show_committed_admission(beds, receipt):
    if beds._is_closing or not accept_beds_snapshot(beds, receipt):
        return
    bed = receipt["bed"]
    admission_id = int(bed["current_admission_id"])
    now = datetime.now()
    admitted_at = datetime.fromisoformat(bed["admission_datetime"])
    birth_date = datetime.fromisoformat(bed["birth_date"]).date() if bed["birth_date"] else None
    name = bed["full_name"].split(maxsplit=2)
    name += [""] * (3 - len(name))
    patient = PatientDTO(
        id=admission_id,
        last_name=name[0],
        first_name=name[1],
        middle_name=name[2],
        full_name=bed["full_name"],
        history_number=bed["history_number"],
        bed_number=bed["bed_number"],
        admission_datetime=admitted_at,
        diagnosis_text=bed["diagnosis_text"],
        age=bed["patient_age"],
        age_months=bed["patient_months"],
        age_unit=bed["patient_age_unit"],
        patient_gender=bed["patient_gender"],
        birth_date=birth_date,
        mkb_code=bed["diagnosis_code"],
    )
    patients = []
    runtime = {}
    for row in beds._rows_by_admission_id.values():
        visible = row.patient
        if visible.id != admission_id and visible.bed_number != patient.bed_number:
            patients.append(visible)
            runtime[visible.id] = getattr(visible, "_w1_runtime_snapshot", {})
    patients.append(patient)
    plan_window = ShiftService.is_plan_card_window(now)
    runtime[admission_id] = {
        "card_exists": False,
        "has_any_card": False,
        "plan_card_available": plan_window,
        "plan_card_window_active": plan_window,
        "plan_card_target_date": ShiftService.get_next_shift_start(now),
    }
    if bed["current_status"] == PatientStatus.ACTIVE.value:
        runtime[admission_id]["status"] = PatientStatusEventDTO(
            id=None, admission_id=admission_id, status=PatientStatus.ACTIVE, start_time=admitted_at,
        )
    beds._apply_beds_snapshot({
        "patients": patients, "runtime_snapshot": runtime,
        "now": now, "yesterday": now - timedelta(days=1),
        "change_id": receipt["change_id"],
        "_read_epoch": getattr(beds, "_beds_read_epoch", 0),
    })
    beds.refresh()

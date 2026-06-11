from rem_card.services.patient_bed_management.recovery_beds import is_recovery_bed_number


SPECIAL_BED_LABELS = {
    11: "Койка пробуждения",
    12: "Койка пробуждения",
}


def is_recovery_bed(bed_number: int) -> bool:
    return is_recovery_bed_number(bed_number)


def format_patient_bed_label(bed_number: int, *, numbered: bool = False, uppercase: bool = False) -> str:
    try:
        bed_number = int(bed_number)
    except (TypeError, ValueError):
        label = "Койка"
    else:
        label = SPECIAL_BED_LABELS.get(bed_number)
        if label is None:
            label = f"Койка № {bed_number}" if numbered else f"Койка {bed_number}"

    return label.upper() if uppercase else label

SPECIAL_BED_LABELS = {
    11: "Койка пробуждения",
    12: "Койка пробуждения",
}


def is_recovery_bed(bed_number: int) -> bool:
    try:
        return int(bed_number) in SPECIAL_BED_LABELS
    except (TypeError, ValueError):
        return False


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

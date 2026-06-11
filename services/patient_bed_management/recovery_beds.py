RECOVERY_BED_NUMBERS = frozenset({11, 12})


def is_recovery_bed_number(bed_number: int) -> bool:
    try:
        return int(bed_number) in RECOVERY_BED_NUMBERS
    except (TypeError, ValueError):
        return False

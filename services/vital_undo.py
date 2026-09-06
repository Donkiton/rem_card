"""Undo receipts captured inside the original clinical write transaction."""

from datetime import datetime

from rem_card.services.concurrency import DataConflictError, DATA_CONFLICT_MESSAGE


VITAL_FIELDS = ("sys", "dia", "pulse", "temp", "spo2", "rr", "cvp")


def vital_change(dto, before):
    return {
        "admission_id": int(dto.admission_id),
        "vital_id": int(dto.id),
        "revision": int(dto.revision or 0),
        "before": {key: before[key] for key in VITAL_FIELDS} if before is not None else None,
    }


def undo_vital_change(cursor, change):
    """Undo this write only; never look up the latest row of an admission."""
    vital_id = int(change["vital_id"])
    admission_id = int(change["admission_id"])
    expected = int(change["revision"])
    row = cursor.execute(
        "SELECT * FROM vitals WHERE id = ? AND admission_id = ?",
        (vital_id, admission_id),
    ).fetchone()
    if row is None or int(row["revision"] or 0) != expected:
        raise DataConflictError(DATA_CONFLICT_MESSAGE)
    before = change["before"]
    if before is None:
        cursor.execute(
            "DELETE FROM vitals WHERE id = ? AND admission_id = ? AND COALESCE(revision, 0) = ?",
            (vital_id, admission_id, expected),
        )
        result = {"action": "delete", "vital_id": vital_id}
    else:
        cursor.execute(
            "UPDATE vitals SET " + ", ".join(f"{key} = ?" for key in VITAL_FIELDS)
            + ", revision = COALESCE(revision, 0) + 1, last_modified_by = 'vital_undo',"
            " updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')"
            " WHERE id = ? AND admission_id = ? AND COALESCE(revision, 0) = ?",
            (*[before[key] for key in VITAL_FIELDS], vital_id, admission_id, expected),
        )
        result = {
            "action": "upsert", "vital_id": vital_id, "revision": expected + 1,
            "vital": dict(
                id=vital_id, admission_id=admission_id,
                timestamp=datetime.fromisoformat(row["datetime"]),
                revision=expected + 1, last_modified_by="vital_undo", **before,
            ),
        }
    if cursor.rowcount != 1:
        raise DataConflictError(DATA_CONFLICT_MESSAGE)
    return result

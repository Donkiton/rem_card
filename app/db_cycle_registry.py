from __future__ import annotations

import glob
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from rem_card.app.db_lifecycle import DB_CYCLE_META_KEY
from rem_card.app.sqlite_shared import configure_connection, validate_sqlite_file


DB_CYCLE_MAX_AGE_DAYS = 180


@dataclass(frozen=True)
class DbCycleInfo:
    path: str
    display_name: str
    is_current: bool
    exists: bool
    size_bytes: int
    modified_at: datetime | None
    created_at: datetime | None
    cycle_started_at: datetime | None
    min_admission_datetime: datetime | None
    max_admission_datetime: datetime | None
    patient_count: int
    admission_count: int
    transferred_count: int
    death_count: int
    active_beds: int
    quick_check_ok: bool
    validation_message: str
    schema_revision: str | None
    age_days: float | None
    days_until_rotation: float | None


def discover_db_cycle_paths(
    *,
    current_db_path: str,
    archive_dir: str | None = None,
    include_current: bool = True,
) -> list[str]:
    current = os.path.abspath(str(current_db_path or ""))
    root = os.path.abspath(str(archive_dir or os.path.dirname(current)))
    patterns = [
        os.path.join(root, "rao_journal_archived_*.db"),
        os.path.join(root, "rao_journal_cycle_*.db"),
        os.path.join(root, "rao-*.db"),
        os.path.join(root, "db_cycle_archive", "*.db"),
    ]

    paths: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        if not path:
            return
        abs_path = os.path.abspath(path)
        key = os.path.normcase(abs_path)
        if key in seen:
            return
        if not abs_path.lower().endswith(".db"):
            return
        if not os.path.isfile(abs_path):
            return
        seen.add(key)
        paths.append(abs_path)

    if include_current:
        add(current)

    for pattern in patterns:
        for path in glob.glob(pattern):
            add(path)

    current_key = os.path.normcase(current)
    paths.sort(
        key=lambda path: (
            0 if os.path.normcase(path) == current_key else 1,
            -_safe_mtime(path),
            os.path.basename(path).lower(),
        )
    )
    return paths


def list_db_cycles(
    *,
    current_db_path: str,
    archive_dir: str | None = None,
    include_current: bool = True,
    validate: bool = False,
    max_age_days: int = DB_CYCLE_MAX_AGE_DAYS,
) -> list[DbCycleInfo]:
    return [
        inspect_db_cycle(
            path,
            current_db_path=current_db_path,
            validate=validate,
            max_age_days=max_age_days,
        )
        for path in discover_db_cycle_paths(
            current_db_path=current_db_path,
            archive_dir=archive_dir,
            include_current=include_current,
        )
    ]


def select_db_paths_for_period(
    *,
    current_db_path: str,
    start_dt: str | datetime | None,
    end_dt: str | datetime | None,
    archive_dir: str | None = None,
) -> list[str]:
    start = _parse_datetime(start_dt)
    end = _parse_datetime(end_dt)
    selected: list[str] = []

    for info in list_db_cycles(
        current_db_path=current_db_path,
        archive_dir=archive_dir,
        include_current=True,
        validate=False,
    ):
        if not info.exists or not info.quick_check_ok:
            continue
        if _cycle_overlaps_period(info, start, end):
            selected.append(info.path)
    return selected


def inspect_db_cycle(
    db_path: str,
    *,
    current_db_path: str = "",
    validate: bool = False,
    max_age_days: int = DB_CYCLE_MAX_AGE_DAYS,
) -> DbCycleInfo:
    path = os.path.abspath(str(db_path or ""))
    current = os.path.abspath(str(current_db_path or ""))
    is_current = bool(current) and os.path.normcase(path) == os.path.normcase(current)
    exists = os.path.isfile(path)
    size_bytes = os.path.getsize(path) if exists else 0
    modified_at = _timestamp_to_datetime(os.path.getmtime(path)) if exists else None
    created_at = _timestamp_to_datetime(os.path.getctime(path)) if exists else None

    base = _empty_info(
        path=path,
        is_current=is_current,
        exists=exists,
        size_bytes=size_bytes,
        modified_at=modified_at,
        created_at=created_at,
        max_age_days=max_age_days,
    )
    if not exists:
        return base

    quick_check_ok = True
    validation_message = "ok"
    if validate:
        quick_check_ok, validation_message = validate_sqlite_file(path)
        if not quick_check_ok:
            return _replace_info(base, quick_check_ok=False, validation_message=validation_message)

    conn = None
    try:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False, isolation_level=None, timeout=5.0)
        configure_connection(conn, readonly=True)
        tables = _table_names(conn)

        cycle_started_at = _read_meta_datetime(conn, DB_CYCLE_META_KEY) if "meta" in tables else None
        schema_revision = _read_meta_value(conn, "schema_fastpath_revision") if "meta" in tables else None
        min_dt = None
        max_dt = None
        admission_count = 0
        patient_count = 0
        transferred_count = 0
        death_count = 0
        active_beds = 0

        if "admissions" in tables:
            row = conn.execute(
                """
                SELECT
                    MIN(admission_datetime),
                    MAX(admission_datetime),
                    COUNT(*),
                    COUNT(DISTINCT patient_id),
                    SUM(CASE WHEN lower(TRIM(COALESCE(outcome, ''))) = 'переведен' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN lower(TRIM(COALESCE(outcome, ''))) = 'умер' THEN 1 ELSE 0 END)
                FROM admissions
                """
            ).fetchone()
            if row:
                min_dt = _parse_datetime(row[0])
                max_dt = _parse_datetime(row[1])
                admission_count = int(row[2] or 0)
                patient_count = int(row[3] or 0)
                transferred_count = int(row[4] or 0)
                death_count = int(row[5] or 0)

        if patient_count <= 0 and "patients" in tables:
            row = conn.execute("SELECT COUNT(*) FROM patients").fetchone()
            patient_count = int(row[0] or 0) if row else 0

        if "beds" in tables:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM beds
                WHERE status = 'OCCUPIED' OR current_admission_id IS NOT NULL
                """
            ).fetchone()
            active_beds = int(row[0] or 0) if row else 0

        age_days, days_until = _rotation_age(cycle_started_at, max_age_days) if is_current else (None, None)
        return _replace_info(
            base,
            cycle_started_at=cycle_started_at,
            min_admission_datetime=min_dt,
            max_admission_datetime=max_dt,
            patient_count=patient_count,
            admission_count=admission_count,
            transferred_count=transferred_count,
            death_count=death_count,
            active_beds=active_beds,
            quick_check_ok=quick_check_ok,
            validation_message=validation_message,
            schema_revision=schema_revision,
            age_days=age_days,
            days_until_rotation=days_until,
        )
    except Exception as exc:
        return _replace_info(base, quick_check_ok=False, validation_message=str(exc))
    finally:
        if conn is not None:
            conn.close()


def _empty_info(
    *,
    path: str,
    is_current: bool,
    exists: bool,
    size_bytes: int,
    modified_at: datetime | None,
    created_at: datetime | None,
    max_age_days: int,
) -> DbCycleInfo:
    cycle_started_at = None
    if is_current and modified_at:
        cycle_started_at = modified_at
    age_days, days_until = _rotation_age(cycle_started_at, max_age_days) if is_current else (None, None)
    return DbCycleInfo(
        path=path,
        display_name="Текущая БД" if is_current else os.path.basename(path),
        is_current=is_current,
        exists=exists,
        size_bytes=size_bytes,
        modified_at=modified_at,
        created_at=created_at,
        cycle_started_at=cycle_started_at,
        min_admission_datetime=None,
        max_admission_datetime=None,
        patient_count=0,
        admission_count=0,
        transferred_count=0,
        death_count=0,
        active_beds=0,
        quick_check_ok=exists,
        validation_message="ok" if exists else "file does not exist",
        schema_revision=None,
        age_days=age_days,
        days_until_rotation=days_until,
    )


def _replace_info(info: DbCycleInfo, **updates) -> DbCycleInfo:
    data = info.__dict__.copy()
    data.update(updates)
    return DbCycleInfo(**data)


def _cycle_overlaps_period(info: DbCycleInfo, start: datetime | None, end: datetime | None) -> bool:
    min_dt = info.min_admission_datetime
    max_dt = info.max_admission_datetime
    if min_dt is None or max_dt is None:
        return False
    if start is not None and max_dt < start:
        return False
    if end is not None and min_dt > end:
        return False
    return True


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if row and row[0]
    }


def _read_meta_value(conn: sqlite3.Connection, key: str) -> str | None:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    except Exception:
        return None
    if not row or row[0] is None:
        return None
    return str(row[0])


def _read_meta_datetime(conn: sqlite3.Connection, key: str) -> datetime | None:
    value = _read_meta_value(conn, key)
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(float(value)))
    except Exception:
        return _parse_datetime(value)


def _rotation_age(started_at: datetime | None, max_age_days: int) -> tuple[float | None, float | None]:
    if started_at is None:
        return None, None
    age_days = max(0.0, (datetime.now() - started_at).total_seconds() / 86400.0)
    days_until = max(0.0, float(max_age_days) - age_days)
    return round(age_days, 2), round(days_until, 2)


def _parse_datetime(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("T", " ")
    text = text.split(".")[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[: len(datetime.now().strftime(fmt))], fmt)
        except Exception:
            pass
    try:
        return datetime.fromisoformat(str(value).strip())
    except Exception:
        return None


def _timestamp_to_datetime(value: float) -> datetime:
    return datetime.fromtimestamp(float(value))


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except Exception:
        return 0.0

from __future__ import annotations

import re
import sqlite3
from typing import Any, Mapping, Optional
from rem_card.services.concurrency import DATA_CONFLICT_MESSAGE, DataConflictError, assert_revision_matches
from .common import (
    OperBlockConflictError,
    validate_operblock_runtime_path,
    _parse_dt,
    _minute_floor,
    _format_bound_time,
    _parse_json_dict,
    _row_to_dict,
)


class OperBlockInfusionsMixin:
    def start_infusion(
        self,
        admission_id: int,
        operation_case_id: int,
        drug_label: str,
        rate_value: Any = None,
        rate_unit: str = "",
        event_time: Any = None,
        *,
        concentration_text: str | None = None,
        volume_ml: Any = None,
        route: str | None = None,
        payload: Optional[dict[str, Any]] = None,
        return_event: bool = False,
    ) -> int | dict[str, Any]:
        validate_operblock_runtime_path(self.db)
        clean_drug = str(drug_label or "").strip()
        if not clean_drug:
            raise ValueError("Укажите препарат для инфузии.")
        clean_rate_value = str(rate_value or "").strip()
        clean_rate_unit = str(rate_unit or "").strip()
        clean_volume_ml = re.sub(r"\s*мл\s*$", "", str(volume_ml or "").strip(), flags=re.IGNORECASE).strip()
        payload_data = dict(payload or {}) if isinstance(payload, Mapping) else {}
        payload_dose_text = re.sub(
            r"\s+",
            " ",
            str(payload_data.get("display_dose_text") or payload_data.get("dose_text") or "").strip(),
        )
        is_gas_infusion = self._payload_is_gas(payload_data)
        if is_gas_infusion:
            payload_data["kind"] = "gas"
        if (clean_rate_value and not clean_rate_unit) or (clean_rate_unit and not clean_rate_value):
            raise ValueError("Укажите скорость инфузии полностью.")
        if clean_volume_ml and not re.fullmatch(r"\d+(?:[,.]\d+)?", clean_volume_ml):
            raise ValueError("Укажите объем инфузии в мл.")
        if not clean_rate_value and not clean_volume_ml and not payload_dose_text:
            raise ValueError("Укажите скорость или объем инфузии.")
        event_dt = self._normalize_timeline_event_datetime(event_time)
        payload_json = self._timeline_payload_json(payload_data)

        def operation(cursor: sqlite3.Cursor):
            case = self._assert_active_operation_for_case_admission(cursor, admission_id, operation_case_id)
            self._assert_datetime_in_active_anesthesia_bounds(cursor, event_dt, case, entity_label="Время инфузии")
            if is_gas_infusion:
                is_oxygen = self._payload_is_oxygen(payload_data, clean_drug)
                if self._active_gas_start_for_case(
                    cursor,
                    admission_id,
                    operation_case_id,
                    oxygen=is_oxygen,
                ):
                    if is_oxygen:
                        raise OperBlockConflictError(
                            "Кислород уже идет. Измените поток активного кислорода, второй кислород запустить нельзя."
                        )
                    raise OperBlockConflictError(
                        "Газ уже идет. Измените дозу активного газа, второй ингаляционный газ запустить нельзя."
                    )
            rate_tail = f"{clean_rate_value} {clean_rate_unit}".strip()
            volume_tail = f"{clean_volume_ml} мл".strip() if clean_volume_ml else ""
            tail = rate_tail or volume_tail or payload_dose_text
            if tail and tail.casefold() in clean_drug.casefold():
                tail = ""
            display_label = f"{clean_drug} {tail}".strip()
            cursor.execute(
                """
                INSERT INTO operblock_timeline_events (
                    operation_case_id, admission_id, table_code, event_type, event_time,
                    drug_label, display_label, raw_text, volume_ml, concentration_text, rate_value,
                    rate_unit, route, status, revision, payload_json,
                    created_by_role, created_by_client_id, last_modified_by
                ) VALUES (?, ?, ?, 'infusion_start', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?,
                          'operblock', ?, 'operblock')
                """,
                (
                    int(operation_case_id),
                    int(admission_id),
                    case.get("table_code"),
                    event_dt.isoformat(timespec="seconds"),
                    clean_drug,
                    display_label,
                    display_label,
                    clean_volume_ml or None,
                    str(concentration_text or "").strip() or None,
                    clean_rate_value or None,
                    clean_rate_unit or None,
                    str(route or "").strip() or None,
                    payload_json,
                    self.client_id,
                ),
            )
            event_id = int(cursor.lastrowid)
            if not return_event:
                return event_id
            row = cursor.execute(
                """
                SELECT
                    id,
                    operation_case_id,
                    admission_id,
                    table_code,
                    event_type,
                    event_time,
                    end_time,
                    drug_label,
                    display_label,
                    raw_text,
                    dose_value,
                    dose_unit,
                    volume_ml,
                    concentration_text,
                    rate_value,
                    rate_unit,
                    route,
                    status,
                    COALESCE(revision, 0) AS revision,
                    created_at,
                    updated_at,
                    payload_json,
                    parent_event_id
                FROM operblock_timeline_events
                WHERE id = ?
                """,
                (event_id,),
            ).fetchone()
            return _row_to_dict(row)

        result = self.db.run_write_operation(operation, source="operblock_start_infusion")
        return dict(result or {}) if return_event else int(result)

    def change_infusion_rate(
        self,
        start_event_id: int,
        *,
        expected_revision: Optional[int],
        new_rate_value: Any,
        new_rate_unit: str,
        event_time: Any,
        start_event_time: Any = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> int:
        validate_operblock_runtime_path(self.db)
        if expected_revision is None:
            raise ValueError("Не удалось проверить актуальность инфузии. Обновите протокол.")
        clean_rate_value = str(new_rate_value or "").strip()
        clean_rate_unit = str(new_rate_unit or "").strip()
        if not clean_rate_value or not clean_rate_unit:
            raise ValueError("Укажите новую скорость инфузии.")
        event_dt = self._normalize_timeline_event_datetime(event_time)
        start_event_dt = self._normalize_timeline_event_datetime(start_event_time) if start_event_time is not None else None
        payload_json = self._timeline_payload_json(payload)

        def operation(cursor: sqlite3.Cursor):
            start = self._get_active_infusion_start_for_update(cursor, start_event_id)
            assert_revision_matches(start["revision"], expected_revision)
            if self._payload_is_gas(_parse_json_dict(start["payload_json"])):
                raise ValueError("Для газа измените дозу или поток. Скорость в мл/час для газа не применяется.")
            case = self._assert_active_operation_for_case_admission(
                cursor,
                int(start["admission_id"]),
                int(start["operation_case_id"]),
            )
            self._assert_datetime_in_active_anesthesia_bounds(
                cursor,
                event_dt,
                case,
                entity_label="Время изменения инфузии",
            )
            if start_event_dt is not None:
                self._assert_datetime_in_active_anesthesia_bounds(
                    cursor,
                    start_event_dt,
                    case,
                    entity_label="Время начала инфузии",
                )
                first_child = cursor.execute(
                    """
                    SELECT event_time
                    FROM operblock_timeline_events
                    WHERE parent_event_id = ?
                      AND event_type IN ('infusion_change', 'infusion_stop')
                      AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                    ORDER BY datetime(event_time) ASC, id ASC
                    LIMIT 1
                    """,
                    (int(start_event_id),),
                ).fetchone()
                first_child_dt = _parse_dt(first_child["event_time"]) if first_child else None
                if first_child_dt is not None and _minute_floor(start_event_dt) > _minute_floor(first_child_dt):
                    raise ValueError(
                        "Время начала инфузии не может быть позже первого события инфузии: "
                        f"{_minute_floor(first_child_dt).strftime('%d.%m.%Y %H:%M')}."
                    )
                if _minute_floor(event_dt) < _minute_floor(start_event_dt):
                    raise ValueError(
                        f"Событие инфузии не может быть раньше начала инфузии: "
                        f"{_format_bound_time(_minute_floor(start_event_dt))}."
                    )
            else:
                self._assert_infusion_event_not_before_start(event_dt, start)
            display_label = f"{start['drug_label']} {clean_rate_value} {clean_rate_unit}".strip()
            cursor.execute(
                """
                INSERT INTO operblock_timeline_events (
                    operation_case_id, admission_id, table_code, event_type, event_time,
                    drug_label, display_label, raw_text, concentration_text, rate_value,
                    rate_unit, route, status, revision, parent_event_id, payload_json,
                    created_by_role, created_by_client_id, last_modified_by
                ) VALUES (?, ?, ?, 'infusion_change', ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?,
                          'operblock', ?, 'operblock')
                """,
                (
                    int(start["operation_case_id"]),
                    int(start["admission_id"]),
                    start["table_code"],
                    event_dt.isoformat(timespec="seconds"),
                    start["drug_label"],
                    display_label,
                    display_label,
                    start["concentration_text"],
                    clean_rate_value,
                    clean_rate_unit,
                    start["route"],
                    int(start_event_id),
                    payload_json,
                    self.client_id,
                ),
            )
            event_id = int(cursor.lastrowid)
            cursor.execute(
                """
                UPDATE operblock_timeline_events
                SET event_time = COALESCE(?, event_time),
                    revision = COALESCE(revision, 0) + 1,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                  AND event_type = 'infusion_start'
                  AND status = 'active'
                  AND COALESCE(revision, 0) = ?
                """,
                (
                    start_event_dt.isoformat(timespec="seconds") if start_event_dt is not None else None,
                    int(start_event_id),
                    int(expected_revision),
                ),
            )
            if cursor.rowcount != 1:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            return event_id

        return int(self.db.run_write_operation(operation, source="operblock_change_infusion_rate"))

    def change_gas_dose(
        self,
        start_event_id: int,
        *,
        expected_revision: Optional[int],
        dose_text: str,
        event_time: Any,
        start_event_time: Any = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> int:
        validate_operblock_runtime_path(self.db)
        if expected_revision is None:
            raise ValueError("Не удалось проверить актуальность газа. Обновите протокол.")
        clean_dose = re.sub(r"\s+", " ", str(dose_text or "").strip())
        if not clean_dose:
            raise ValueError("Укажите дозу газа или поток кислорода.")
        event_dt = self._normalize_timeline_event_datetime(event_time)
        start_event_dt = self._normalize_timeline_event_datetime(start_event_time) if start_event_time is not None else None

        effective_payload = dict(payload or {}) if isinstance(payload, Mapping) else {}
        effective_payload["kind"] = "gas"
        effective_payload["dose_text"] = clean_dose
        effective_payload["display_dose_text"] = clean_dose
        payload_json = self._timeline_payload_json(effective_payload)

        def operation(cursor: sqlite3.Cursor):
            start = self._get_active_infusion_start_for_update(cursor, start_event_id)
            assert_revision_matches(start["revision"], expected_revision)
            if not self._payload_is_gas(_parse_json_dict(start["payload_json"])):
                raise ValueError("Для дозатора изменяется скорость, для газа - доза или поток.")
            case = self._assert_active_operation_for_case_admission(
                cursor,
                int(start["admission_id"]),
                int(start["operation_case_id"]),
            )
            self._assert_datetime_in_active_anesthesia_bounds(
                cursor,
                event_dt,
                case,
                entity_label="Время изменения газа",
            )
            if start_event_dt is not None:
                self._assert_datetime_in_active_anesthesia_bounds(
                    cursor,
                    start_event_dt,
                    case,
                    entity_label="Время начала газа",
                )
                first_child = cursor.execute(
                    """
                    SELECT event_time
                    FROM operblock_timeline_events
                    WHERE parent_event_id = ?
                      AND event_type IN ('infusion_change', 'infusion_stop')
                      AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                    ORDER BY datetime(event_time) ASC, id ASC
                    LIMIT 1
                    """,
                    (int(start_event_id),),
                ).fetchone()
                first_child_dt = _parse_dt(first_child["event_time"]) if first_child else None
                if first_child_dt is not None and _minute_floor(start_event_dt) > _minute_floor(first_child_dt):
                    raise ValueError(
                        "Время начала газа не может быть позже первого события газа: "
                        f"{_minute_floor(first_child_dt).strftime('%d.%m.%Y %H:%M')}."
                    )
                if _minute_floor(event_dt) < _minute_floor(start_event_dt):
                    raise ValueError(
                        f"Изменение газа не может быть раньше начала газа: "
                        f"{_format_bound_time(_minute_floor(start_event_dt))}."
                    )
            else:
                self._assert_infusion_event_not_before_start(event_dt, start)

            display_label = f"{start['drug_label']} {clean_dose}".strip()
            cursor.execute(
                """
                INSERT INTO operblock_timeline_events (
                    operation_case_id, admission_id, table_code, event_type, event_time,
                    drug_label, display_label, raw_text, concentration_text, route, status,
                    revision, parent_event_id, payload_json,
                    created_by_role, created_by_client_id, last_modified_by
                ) VALUES (?, ?, ?, 'infusion_change', ?, ?, ?, ?, ?, ?, 'active',
                          1, ?, ?, 'operblock', ?, 'operblock')
                """,
                (
                    int(start["operation_case_id"]),
                    int(start["admission_id"]),
                    start["table_code"],
                    event_dt.isoformat(timespec="seconds"),
                    start["drug_label"],
                    display_label,
                    display_label,
                    start["concentration_text"],
                    start["route"],
                    int(start_event_id),
                    payload_json,
                    self.client_id,
                ),
            )
            event_id = int(cursor.lastrowid)
            cursor.execute(
                """
                UPDATE operblock_timeline_events
                SET event_time = COALESCE(?, event_time),
                    rate_value = NULL,
                    rate_unit = NULL,
                    revision = COALESCE(revision, 0) + 1,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                  AND event_type = 'infusion_start'
                  AND status = 'active'
                  AND COALESCE(revision, 0) = ?
                """,
                (
                    start_event_dt.isoformat(timespec="seconds") if start_event_dt is not None else None,
                    int(start_event_id),
                    int(expected_revision),
                ),
            )
            if cursor.rowcount != 1:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            return event_id

        return int(self.db.run_write_operation(operation, source="operblock_change_gas_dose"))

    def update_infusion_start_time(
        self,
        start_event_id: int,
        *,
        expected_revision: Optional[int],
        event_time: Any,
    ) -> int:
        validate_operblock_runtime_path(self.db)
        if expected_revision is None:
            raise ValueError("Не удалось проверить актуальность инфузии. Обновите протокол.")
        event_dt = self._normalize_timeline_event_datetime(event_time)

        def operation(cursor: sqlite3.Cursor):
            start = self._get_infusion_start_for_update(
                cursor,
                start_event_id,
                allowed_statuses={"active", "stopped"},
            )
            assert_revision_matches(start["revision"], expected_revision)
            case = self._assert_active_operation_for_case_admission(
                cursor,
                int(start["admission_id"]),
                int(start["operation_case_id"]),
            )
            self._assert_datetime_in_active_anesthesia_bounds(
                cursor,
                event_dt,
                case,
                entity_label="Время начала инфузии",
            )
            first_child = cursor.execute(
                """
                SELECT event_time
                FROM operblock_timeline_events
                WHERE parent_event_id = ?
                  AND event_type IN ('infusion_change', 'infusion_stop')
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                ORDER BY datetime(event_time) ASC, id ASC
                LIMIT 1
                """,
                (int(start_event_id),),
            ).fetchone()
            first_child_dt = _parse_dt(first_child["event_time"]) if first_child else None
            if first_child_dt is not None and _minute_floor(event_dt) > _minute_floor(first_child_dt):
                raise ValueError(
                    "Время начала инфузии не может быть позже первого события инфузии: "
                    f"{_minute_floor(first_child_dt).strftime('%d.%m.%Y %H:%M')}."
                )
            cursor.execute(
                """
                UPDATE operblock_timeline_events
                SET event_time = ?,
                    revision = COALESCE(revision, 0) + 1,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                  AND event_type = 'infusion_start'
                  AND status IN ('active', 'stopped')
                  AND COALESCE(revision, 0) = ?
                """,
                (event_dt.isoformat(timespec="seconds"), int(start_event_id), int(expected_revision)),
            )
            if cursor.rowcount != 1:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            return int(start_event_id)

        return int(self.db.run_write_operation(operation, source="operblock_update_infusion_start_time"))

    def update_infusion_volume(
        self,
        start_event_id: int,
        *,
        expected_revision: Optional[int],
        volume_ml: Any,
        event_time: Any = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> int:
        validate_operblock_runtime_path(self.db)
        if expected_revision is None:
            raise ValueError("Не удалось проверить актуальность инфузии. Обновите протокол.")
        clean_volume_ml = re.sub(r"\s*мл\s*$", "", str(volume_ml or "").strip(), flags=re.IGNORECASE).strip()
        if not clean_volume_ml or not re.fullmatch(r"\d+(?:[,.]\d+)?", clean_volume_ml):
            raise ValueError("Укажите объем инфузии в мл.")
        try:
            if float(clean_volume_ml.replace(",", ".")) <= 0:
                raise ValueError
        except Exception as exc:
            raise ValueError("Укажите объем инфузии в мл.") from exc
        event_dt = self._normalize_timeline_event_datetime(event_time) if event_time is not None else None

        def operation(cursor: sqlite3.Cursor):
            start = self._get_infusion_start_for_update(
                cursor,
                start_event_id,
                allowed_statuses={"active", "stopped"},
            )
            assert_revision_matches(start["revision"], expected_revision)
            effective_payload = _parse_json_dict(start["payload_json"])
            if isinstance(payload, dict):
                effective_payload.update({key: value for key, value in payload.items() if value not in (None, "", [])})
            effective_payload["volume_ml"] = clean_volume_ml
            effective_payload["declared_total_volume_ml"] = clean_volume_ml
            payload_json = self._timeline_payload_json(effective_payload)
            display_label = f"{start['drug_label']} {clean_volume_ml} мл".strip()
            if event_dt is not None:
                case = self._assert_active_operation_for_case_admission(
                    cursor,
                    int(start["admission_id"]),
                    int(start["operation_case_id"]),
                )
                self._assert_datetime_in_active_anesthesia_bounds(
                    cursor,
                    event_dt,
                    case,
                    entity_label="Время начала инфузии",
                )
                first_child = cursor.execute(
                    """
                    SELECT event_time
                    FROM operblock_timeline_events
                    WHERE parent_event_id = ?
                      AND event_type IN ('infusion_change', 'infusion_stop')
                      AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                    ORDER BY datetime(event_time) ASC, id ASC
                    LIMIT 1
                    """,
                    (int(start_event_id),),
                ).fetchone()
                first_child_dt = _parse_dt(first_child["event_time"]) if first_child else None
                if first_child_dt is not None and _minute_floor(event_dt) > _minute_floor(first_child_dt):
                    raise ValueError(
                        "Время начала инфузии не может быть позже первого события инфузии: "
                        f"{_minute_floor(first_child_dt).strftime('%d.%m.%Y %H:%M')}."
                    )
            cursor.execute(
                """
                UPDATE operblock_timeline_events
                SET display_label = ?,
                    raw_text = ?,
                    event_time = COALESCE(?, event_time),
                    volume_ml = ?,
                    payload_json = ?,
                    revision = COALESCE(revision, 0) + 1,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                  AND event_type = 'infusion_start'
                  AND status IN ('active', 'stopped')
                  AND COALESCE(revision, 0) = ?
                """,
                (
                    display_label,
                    display_label,
                    event_dt.isoformat(timespec="seconds") if event_dt is not None else None,
                    clean_volume_ml,
                    payload_json,
                    int(start_event_id),
                    int(expected_revision),
                ),
            )
            if cursor.rowcount != 1:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            return int(start_event_id)

        return int(self.db.run_write_operation(operation, source="operblock_update_infusion_volume"))

    def update_infusion_dose_text(
        self,
        start_event_id: int,
        *,
        expected_revision: Optional[int],
        dose_text: str,
        event_time: Any = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> int:
        validate_operblock_runtime_path(self.db)
        if expected_revision is None:
            raise ValueError("Не удалось проверить актуальность инфузии. Обновите протокол.")
        clean_dose = re.sub(r"\s+", " ", str(dose_text or "").strip())
        if not clean_dose:
            raise ValueError("Укажите дозу назначения.")
        event_dt = self._normalize_timeline_event_datetime(event_time) if event_time is not None else None

        def operation(cursor: sqlite3.Cursor):
            start = self._get_infusion_start_for_update(
                cursor,
                start_event_id,
                allowed_statuses={"active", "stopped"},
            )
            assert_revision_matches(start["revision"], expected_revision)
            effective_payload = _parse_json_dict(start["payload_json"])
            if isinstance(payload, dict):
                effective_payload.update({key: value for key, value in payload.items() if value not in (None, "", [])})
            effective_payload["dose_text"] = clean_dose
            effective_payload["display_dose_text"] = clean_dose
            is_gas_infusion = self._payload_is_gas(effective_payload)
            if is_gas_infusion:
                effective_payload["kind"] = "gas"
            payload_json = self._timeline_payload_json(effective_payload)
            display_label = f"{start['drug_label']} {clean_dose}".strip()
            if event_dt is not None:
                case = self._assert_active_operation_for_case_admission(
                    cursor,
                    int(start["admission_id"]),
                    int(start["operation_case_id"]),
                )
                self._assert_datetime_in_active_anesthesia_bounds(
                    cursor,
                    event_dt,
                    case,
                    entity_label="Время начала назначения",
                )
                first_child = cursor.execute(
                    """
                    SELECT event_time
                    FROM operblock_timeline_events
                    WHERE parent_event_id = ?
                      AND event_type IN ('infusion_change', 'infusion_stop')
                      AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                    ORDER BY datetime(event_time) ASC, id ASC
                    LIMIT 1
                    """,
                    (int(start_event_id),),
                ).fetchone()
                first_child_dt = _parse_dt(first_child["event_time"]) if first_child else None
                if first_child_dt is not None and _minute_floor(event_dt) > _minute_floor(first_child_dt):
                    raise ValueError(
                        "Время начала назначения не может быть позже первого события назначения: "
                        f"{_minute_floor(first_child_dt).strftime('%d.%m.%Y %H:%M')}."
                    )
            cursor.execute(
                """
                UPDATE operblock_timeline_events
                SET display_label = ?,
                    raw_text = ?,
                    event_time = COALESCE(?, event_time),
                    rate_value = CASE WHEN ? THEN NULL ELSE rate_value END,
                    rate_unit = CASE WHEN ? THEN NULL ELSE rate_unit END,
                    payload_json = ?,
                    revision = COALESCE(revision, 0) + 1,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                  AND event_type = 'infusion_start'
                  AND status IN ('active', 'stopped')
                  AND COALESCE(revision, 0) = ?
                """,
                (
                    display_label,
                    display_label,
                    event_dt.isoformat(timespec="seconds") if event_dt is not None else None,
                    1 if is_gas_infusion else 0,
                    1 if is_gas_infusion else 0,
                    payload_json,
                    int(start_event_id),
                    int(expected_revision),
                ),
            )
            if cursor.rowcount != 1:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            if is_gas_infusion:
                cursor.execute(
                    """
                    UPDATE operblock_timeline_events
                    SET status = 'cancelled',
                        revision = COALESCE(revision, 0) + 1,
                        last_modified_by = 'operblock',
                        updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                    WHERE parent_event_id = ?
                      AND event_type = 'infusion_change'
                      AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                    """,
                    (int(start_event_id),),
                )
            return int(start_event_id)

        return int(self.db.run_write_operation(operation, source="operblock_update_infusion_dose_text"))

    def stop_infusion(
        self,
        start_event_id: int,
        *,
        expected_revision: Optional[int],
        event_time: Any,
        payload: Optional[dict[str, Any]] = None,
    ) -> int:
        validate_operblock_runtime_path(self.db)
        if expected_revision is None:
            raise ValueError("Не удалось проверить актуальность инфузии. Обновите протокол.")
        event_dt = self._normalize_timeline_event_datetime(event_time)
        payload_json = self._timeline_payload_json(payload)

        def operation(cursor: sqlite3.Cursor):
            start = self._get_active_infusion_start_for_update(cursor, start_event_id)
            assert_revision_matches(start["revision"], expected_revision)
            case = self._assert_active_operation_for_case_admission(
                cursor,
                int(start["admission_id"]),
                int(start["operation_case_id"]),
            )
            self._assert_datetime_in_active_anesthesia_bounds(
                cursor,
                event_dt,
                case,
                entity_label="Время остановки инфузии",
            )
            self._assert_infusion_event_not_before_start(event_dt, start)
            self._assert_infusion_stop_not_before_latest_change(cursor, int(start_event_id), event_dt)
            display_label = f"{start['drug_label']} стоп".strip()
            cursor.execute(
                """
                INSERT INTO operblock_timeline_events (
                    operation_case_id, admission_id, table_code, event_type, event_time,
                    drug_label, display_label, raw_text, volume_ml, concentration_text, rate_value,
                    rate_unit, route, status, revision, parent_event_id, payload_json,
                    created_by_role, created_by_client_id, last_modified_by
                ) VALUES (?, ?, ?, 'infusion_stop', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?,
                          'operblock', ?, 'operblock')
                """,
                (
                    int(start["operation_case_id"]),
                    int(start["admission_id"]),
                    start["table_code"],
                    event_dt.isoformat(timespec="seconds"),
                    start["drug_label"],
                    display_label,
                    display_label,
                    start["volume_ml"],
                    start["concentration_text"],
                    start["rate_value"],
                    start["rate_unit"],
                    start["route"],
                    int(start_event_id),
                    payload_json,
                    self.client_id,
                ),
            )
            event_id = int(cursor.lastrowid)
            cursor.execute(
                """
                UPDATE operblock_timeline_events
                SET status = 'stopped',
                    end_time = ?,
                    revision = COALESCE(revision, 0) + 1,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                  AND event_type = 'infusion_start'
                  AND status = 'active'
                  AND COALESCE(revision, 0) = ?
                """,
                (event_dt.isoformat(timespec="seconds"), int(start_event_id), int(expected_revision)),
            )
            if cursor.rowcount != 1:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            return event_id

        return int(self.db.run_write_operation(operation, source="operblock_stop_infusion"))

    def delete_infusion(self, start_event_id: int, *, expected_revision: Optional[int]) -> int:
        validate_operblock_runtime_path(self.db)
        if expected_revision is None:
            raise ValueError("Не удалось проверить актуальность инфузии. Обновите протокол.")

        def operation(cursor: sqlite3.Cursor):
            start = self._get_infusion_start_for_update(
                cursor,
                start_event_id,
                allowed_statuses={"active", "stopped"},
            )
            assert_revision_matches(start["revision"], expected_revision)
            case = self._assert_active_operation_for_case_admission(
                cursor,
                int(start["admission_id"]),
                int(start["operation_case_id"]),
            )
            self._require_active_anesthesia_interval(cursor, case, entity_label="Удаление инфузии")
            cursor.execute(
                """
                UPDATE operblock_timeline_events
                SET status = 'deleted',
                    revision = COALESCE(revision, 0) + 1,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
                  AND event_type = 'infusion_start'
                  AND status IN ('active', 'stopped')
                  AND COALESCE(revision, 0) = ?
                """,
                (int(start_event_id), int(expected_revision)),
            )
            if cursor.rowcount != 1:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            cursor.execute(
                """
                UPDATE operblock_timeline_events
                SET status = 'deleted',
                    revision = COALESCE(revision, 0) + 1,
                    last_modified_by = 'operblock',
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE parent_event_id = ?
                  AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
                """,
                (int(start_event_id),),
            )
            return int(start_event_id)

        return int(self.db.run_write_operation(operation, source="operblock_delete_infusion"))

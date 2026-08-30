from datetime import datetime
from typing import List, Optional

from rem_card.data.dao.exceptions import OptimisticLockError
from rem_card.data.dto.remcard_dto import (
    DietPlanDTO,
    DietPlanVersionDTO,
    DietTemplateDTO,
    OralIntakeEventDTO,
)


def _dt_to_db(value: datetime) -> str:
    return value.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")


def _parse_dt(value) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.fromtimestamp(0)
    return datetime.fromisoformat(text.replace(" ", "T"))


class DietTemplateDAO:
    def __init__(self, db_manager):
        self.db = db_manager

    def list_templates(self) -> List[DietTemplateDTO]:
        rows = self.db.fetch_all_remcard(
            """
            SELECT *
            FROM diet_templates
            ORDER BY is_default DESC, LOWER(name) ASC, id ASC
            """
        )
        return [self._map(row) for row in rows]

    def get_template(self, template_id: int) -> Optional[DietTemplateDTO]:
        row = self.db.fetch_one_remcard("SELECT * FROM diet_templates WHERE id = ?", (int(template_id),))
        return self._map(row) if row else None

    def create_template(self, dto: DietTemplateDTO, cursor=None) -> int:
        cur = cursor or self.db.execute_remcard(
            """
            INSERT INTO diet_templates (
                name, diet_text, schedule_json, details_json, is_default, version, last_modified_by, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 1, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
            """,
            (
                dto.name,
                dto.diet_text,
                dto.schedule_json,
                dto.details_json,
                int(dto.is_default or 0),
                dto.last_modified_by or "doctor",
            ),
        )
        if cursor:
            cur = cursor.execute(
                """
                INSERT INTO diet_templates (
                    name, diet_text, schedule_json, details_json, is_default, version, last_modified_by, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
                """,
                (
                    dto.name,
                    dto.diet_text,
                    dto.schedule_json,
                    dto.details_json,
                    int(dto.is_default or 0),
                    dto.last_modified_by or "doctor",
                ),
            )
        return int(cur.lastrowid)

    def update_template(self, dto: DietTemplateDTO, expected_version: Optional[int] = None, cursor=None):
        if dto.id is None:
            raise ValueError("Template id is required")
        expected = int(expected_version if expected_version is not None else dto.version or 0)
        params = [
            dto.name,
            dto.diet_text,
            dto.schedule_json,
            dto.details_json,
            int(dto.is_default or 0),
            dto.last_modified_by or "doctor",
            int(dto.id),
        ]
        where_version = ""
        if expected > 0:
            where_version = " AND version = ?"
            params.append(expected)
        query = f"""
            UPDATE diet_templates
            SET name = ?,
                diet_text = ?,
                schedule_json = ?,
                details_json = ?,
                is_default = ?,
                version = COALESCE(version, 0) + 1,
                last_modified_by = ?,
                updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
            WHERE id = ?{where_version}
        """
        cur = cursor.execute(query, tuple(params)) if cursor else self.db.execute_remcard(query, tuple(params))
        if expected > 0 and cur.rowcount == 0:
            raise OptimisticLockError("Шаблон питания был изменен другим пользователем")

    def delete_template(self, template_id: int, expected_version: Optional[int] = None, cursor=None):
        params = [int(template_id)]
        where_version = ""
        if expected_version is not None and int(expected_version) > 0:
            where_version = " AND version = ?"
            params.append(int(expected_version))
        query = f"DELETE FROM diet_templates WHERE id = ?{where_version}"
        cur = cursor.execute(query, tuple(params)) if cursor else self.db.execute_remcard(query, tuple(params))
        if expected_version is not None and int(expected_version) > 0 and cur.rowcount == 0:
            raise OptimisticLockError("Шаблон питания был изменен другим пользователем")

    @staticmethod
    def _map(row) -> DietTemplateDTO:
        rd = dict(row)
        return DietTemplateDTO(
            id=rd.get("id"),
            name=rd.get("name") or "",
            diet_text=rd.get("diet_text") or "",
            schedule_json=rd.get("schedule_json") or "[]",
            details_json=rd.get("details_json") or "{}",
            is_default=int(rd.get("is_default") or 0),
            version=int(rd.get("version") or 0),
            created_at=rd.get("created_at"),
            updated_at=rd.get("updated_at"),
            last_modified_by=rd.get("last_modified_by"),
        )


class DietPlanDAO:
    def __init__(self, db_manager):
        self.db = db_manager

    def get_plan(self, admission_id: int, shift_start: datetime) -> Optional[DietPlanDTO]:
        row = self.db.fetch_one_remcard(
            """
            SELECT *
            FROM diet_plan
            WHERE admission_id = ? AND shift_start = ?
            """,
            (int(admission_id), _dt_to_db(shift_start)),
        )
        return self._map(row) if row else None

    def upsert_plan(self, dto: DietPlanDTO, expected_version: Optional[int] = None, cursor=None) -> DietPlanDTO:
        if cursor is None:
            with self.db.remcard_transaction(source="diet_plan_upsert") as cur:
                return self.upsert_plan(dto, expected_version=expected_version, cursor=cur)

        shift_start = _dt_to_db(dto.shift_start)
        cursor.execute(
            """
            SELECT *
            FROM diet_plan
            WHERE admission_id = ? AND shift_start = ?
            """,
            (int(dto.admission_id), shift_start),
        )
        row = cursor.fetchone()
        if row:
            current = self._map(row)
            expected = int(expected_version if expected_version is not None else current.version or 0)
            cursor.execute(
                """
                UPDATE diet_plan
                SET template_id = ?,
                    diet_text = ?,
                    schedule_json = ?,
                    details_json = ?,
                    version = COALESCE(version, 0) + 1,
                    last_modified_by = ?,
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ? AND version = ?
                """,
                (
                    dto.template_id,
                    dto.diet_text,
                    dto.schedule_json,
                    dto.details_json,
                    dto.last_modified_by or "doctor",
                    int(current.id),
                    expected,
                ),
            )
            if cursor.rowcount == 0:
                raise OptimisticLockError("План питания был изменен другим пользователем")
            return self.get_plan_with_cursor(cursor, int(dto.admission_id), dto.shift_start)

        cursor.execute(
            """
            INSERT INTO diet_plan (
                admission_id, shift_start, template_id, diet_text, schedule_json, details_json,
                version, last_modified_by, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
            """,
            (
                int(dto.admission_id),
                shift_start,
                dto.template_id,
                dto.diet_text,
                dto.schedule_json,
                dto.details_json,
                dto.last_modified_by or "doctor",
            ),
        )
        return self.get_plan_with_cursor(cursor, int(dto.admission_id), dto.shift_start)

    def delete_plan(self, admission_id: int, shift_start: datetime, expected_version: Optional[int] = None, cursor=None):
        params = [int(admission_id), _dt_to_db(shift_start)]
        where_version = ""
        if expected_version is not None and int(expected_version) > 0:
            where_version = " AND version = ?"
            params.append(int(expected_version))
        query = f"DELETE FROM diet_plan WHERE admission_id = ? AND shift_start = ?{where_version}"
        cur = cursor.execute(query, tuple(params)) if cursor else self.db.execute_remcard(query, tuple(params))
        if expected_version is not None and int(expected_version) > 0 and cur.rowcount == 0:
            raise OptimisticLockError("План питания был изменен другим пользователем")

    def get_plan_with_cursor(self, cursor, admission_id: int, shift_start: datetime) -> Optional[DietPlanDTO]:
        cursor.execute(
            """
            SELECT *
            FROM diet_plan
            WHERE admission_id = ? AND shift_start = ?
            """,
            (int(admission_id), _dt_to_db(shift_start)),
        )
        row = cursor.fetchone()
        return self._map(row) if row else None

    @staticmethod
    def _map(row) -> DietPlanDTO:
        rd = dict(row)
        return DietPlanDTO(
            id=rd.get("id"),
            admission_id=int(rd.get("admission_id") or 0),
            shift_start=_parse_dt(rd.get("shift_start")),
            template_id=rd.get("template_id"),
            diet_text=rd.get("diet_text") or "",
            schedule_json=rd.get("schedule_json") or "[]",
            details_json=rd.get("details_json") or "{}",
            version=int(rd.get("version") or 0),
            created_at=rd.get("created_at"),
            updated_at=rd.get("updated_at"),
            last_modified_by=rd.get("last_modified_by"),
        )


class DietPlanVersionDAO:
    def __init__(self, db_manager):
        self.db = db_manager

    def list_versions(self, admission_id: int, start: datetime, end: datetime) -> List[DietPlanVersionDTO]:
        rows = self.db.fetch_all_remcard(
            """
            SELECT *
            FROM diet_plan_versions
            WHERE admission_id = ?
              AND DATETIME(effective_from) < DATETIME(?)
              AND (
                    DATETIME(effective_from) >= DATETIME(?)
                    OR id = (
                        SELECT id FROM diet_plan_versions
                        WHERE admission_id = ? AND DATETIME(effective_from) < DATETIME(?)
                        ORDER BY DATETIME(effective_from) DESC, id DESC LIMIT 1
                    )
              )
            ORDER BY DATETIME(effective_from) ASC, id ASC
            """,
            (int(admission_id), _dt_to_db(end), _dt_to_db(start), int(admission_id), _dt_to_db(start)),
        )
        return [self._map(row) for row in rows]

    def list_all(self, admission_id: int) -> List[DietPlanVersionDTO]:
        rows = self.db.fetch_all_remcard(
            """
            SELECT * FROM diet_plan_versions
            WHERE admission_id = ?
            ORDER BY DATETIME(effective_from) ASC, id ASC
            """,
            (int(admission_id),),
        )
        return [self._map(row) for row in rows]

    def get_active_at(self, admission_id: int, moment: datetime, cursor=None) -> Optional[DietPlanVersionDTO]:
        query = """
            SELECT * FROM diet_plan_versions
            WHERE admission_id = ? AND DATETIME(effective_from) <= DATETIME(?)
            ORDER BY DATETIME(effective_from) DESC, id DESC LIMIT 1
        """
        params = (int(admission_id), _dt_to_db(moment))
        if cursor is not None:
            cursor.execute(query, params)
            row = cursor.fetchone()
        else:
            row = self.db.fetch_one_remcard(query, params)
        return self._map(row) if row else None

    def upsert_version(
        self,
        dto: DietPlanVersionDTO,
        expected_version: Optional[int] = None,
        cursor=None,
    ) -> DietPlanVersionDTO:
        if cursor is None:
            with self.db.remcard_transaction(source="diet_plan_version_upsert") as cur:
                return self.upsert_version(dto, expected_version=expected_version, cursor=cur)
        effective_from = _dt_to_db(dto.effective_from)
        cursor.execute(
            "SELECT * FROM diet_plan_versions WHERE admission_id = ? AND effective_from = ?",
            (int(dto.admission_id), effective_from),
        )
        row = cursor.fetchone()
        if row:
            current = self._map(row)
            expected = int(expected_version if expected_version is not None else current.version or 0)
            cursor.execute(
                """
                UPDATE diet_plan_versions
                SET shift_start = ?, template_id = ?, diet_name = ?, diet_text = ?,
                    schedule_json = ?, details_json = ?, change_note = ?,
                    version = COALESCE(version, 0) + 1, last_modified_by = ?,
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ? AND version = ?
                """,
                (
                    _dt_to_db(dto.shift_start), dto.template_id, dto.diet_name, dto.diet_text,
                    dto.schedule_json, dto.details_json, dto.change_note,
                    dto.last_modified_by or "doctor", int(current.id), expected,
                ),
            )
            if cursor.rowcount != 1:
                raise OptimisticLockError("Назначение диеты было изменено другим пользователем")
            return self.get_active_at(int(dto.admission_id), dto.effective_from, cursor=cursor)
        cursor.execute(
            """
            INSERT INTO diet_plan_versions (
                admission_id, shift_start, effective_from, template_id, diet_name,
                diet_text, schedule_json, details_json, change_note,
                version, last_modified_by, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
            """,
            (
                int(dto.admission_id), _dt_to_db(dto.shift_start), effective_from,
                dto.template_id, dto.diet_name, dto.diet_text, dto.schedule_json,
                dto.details_json, dto.change_note, dto.last_modified_by or "doctor",
            ),
        )
        cursor.execute("SELECT * FROM diet_plan_versions WHERE id = ?", (int(cursor.lastrowid),))
        return self._map(cursor.fetchone())

    def update_version_by_id(
        self,
        dto: DietPlanVersionDTO,
        expected_version: Optional[int] = None,
        cursor=None,
    ) -> DietPlanVersionDTO:
        if dto.id is None:
            raise ValueError("Diet plan version id is required")
        if cursor is None:
            with self.db.remcard_transaction(source="diet_plan_version_update") as cur:
                return self.update_version_by_id(dto, expected_version=expected_version, cursor=cur)
        current = self.get_by_id(int(dto.id), cursor=cursor)
        if current is None:
            raise ValueError("Назначение диеты не найдено")
        expected = int(expected_version if expected_version is not None else current.version or 0)
        try:
            cursor.execute(
                """
                UPDATE diet_plan_versions
                SET shift_start = ?, effective_from = ?, template_id = ?, diet_name = ?,
                    diet_text = ?, schedule_json = ?, details_json = ?, change_note = ?,
                    version = COALESCE(version, 0) + 1, last_modified_by = ?,
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ? AND version = ?
                """,
                (
                    _dt_to_db(dto.shift_start), _dt_to_db(dto.effective_from), dto.template_id,
                    dto.diet_name, dto.diet_text, dto.schedule_json, dto.details_json,
                    dto.change_note, dto.last_modified_by or "doctor", int(dto.id), expected,
                ),
            )
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise ValueError("На указанное время уже существует изменение диеты") from exc
            raise
        if cursor.rowcount != 1:
            raise OptimisticLockError("Назначение диеты было изменено другим пользователем")
        return self.get_by_id(int(dto.id), cursor=cursor)

    def get_by_id(self, version_id: int, cursor=None) -> Optional[DietPlanVersionDTO]:
        query = "SELECT * FROM diet_plan_versions WHERE id = ?"
        params = (int(version_id),)
        if cursor is not None:
            cursor.execute(query, params)
            row = cursor.fetchone()
        else:
            row = self.db.fetch_one_remcard(query, params)
        return self._map(row) if row else None

    def delete_by_id(
        self,
        version_id: int,
        *,
        expected_version: Optional[int] = None,
        cursor=None,
    ) -> None:
        if cursor is None:
            with self.db.remcard_transaction(source="diet_plan_version_delete") as cur:
                self.delete_by_id(
                    version_id,
                    expected_version=expected_version,
                    cursor=cur,
                )
                return

        params = [int(version_id)]
        where_version = ""
        if expected_version is not None and int(expected_version) > 0:
            where_version = " AND version = ?"
            params.append(int(expected_version))

        # Фактическое потребление является самостоятельной медицинской записью
        # и при удалении назначения должно сохраниться без устаревшей ссылки.
        cursor.execute(
            "UPDATE oral_intake_events SET plan_version_id = NULL WHERE plan_version_id = ?",
            (int(version_id),),
        )
        cursor.execute(
            f"DELETE FROM diet_plan_versions WHERE id = ?{where_version}",
            tuple(params),
        )
        if cursor.rowcount != 1:
            raise OptimisticLockError("Назначение диеты было изменено или удалено другим пользователем")

    @staticmethod
    def _map(row) -> DietPlanVersionDTO:
        rd = dict(row)
        return DietPlanVersionDTO(
            id=rd.get("id"), admission_id=int(rd.get("admission_id") or 0),
            shift_start=_parse_dt(rd.get("shift_start")),
            effective_from=_parse_dt(rd.get("effective_from")),
            template_id=rd.get("template_id"), diet_name=rd.get("diet_name") or "",
            diet_text=rd.get("diet_text") or "", schedule_json=rd.get("schedule_json") or "[]",
            details_json=rd.get("details_json") or "{}", change_note=rd.get("change_note") or "",
            version=int(rd.get("version") or 0), created_at=rd.get("created_at"),
            updated_at=rd.get("updated_at"), last_modified_by=rd.get("last_modified_by"),
        )


class OralIntakeDAO:
    def __init__(self, db_manager):
        self.db = db_manager

    def get_events(self, admission_id: int, start: datetime, end: datetime) -> List[OralIntakeEventDTO]:
        rows = self.db.fetch_all_remcard(
            """
            SELECT *
            FROM oral_intake_events
            WHERE admission_id = ?
              AND DATETIME(event_time) >= DATETIME(?)
              AND DATETIME(event_time) < DATETIME(?)
            ORDER BY DATETIME(event_time) ASC, id ASC
            """,
            (int(admission_id), _dt_to_db(start), _dt_to_db(end)),
        )
        return [self._map(row) for row in rows]

    def get_event_at(self, admission_id: int, event_time: datetime, cursor=None) -> Optional[OralIntakeEventDTO]:
        query = """
            SELECT *
            FROM oral_intake_events
            WHERE admission_id = ? AND DATETIME(event_time) = DATETIME(?)
        """
        params = (int(admission_id), _dt_to_db(event_time))
        if cursor:
            cursor.execute(query, params)
            row = cursor.fetchone()
        else:
            row = self.db.fetch_one_remcard(query, params)
        return self._map(row) if row else None

    def get_unplanned_event_at(
        self,
        admission_id: int,
        event_time: datetime,
        cursor=None,
    ) -> Optional[OralIntakeEventDTO]:
        query = """
            SELECT *
            FROM oral_intake_events
            WHERE admission_id = ?
              AND DATETIME(event_time) = DATETIME(?)
              AND COALESCE(entry_kind, 'unplanned') = 'unplanned'
            ORDER BY id DESC
            LIMIT 1
        """
        params = (int(admission_id), _dt_to_db(event_time))
        if cursor is not None:
            cursor.execute(query, params)
            row = cursor.fetchone()
        else:
            row = self.db.fetch_one_remcard(query, params)
        return self._map(row) if row else None

    def upsert_unplanned_event(self, dto: OralIntakeEventDTO, cursor=None) -> OralIntakeEventDTO:
        if cursor is None:
            with self.db.remcard_transaction(source="oral_intake_unplanned_upsert") as cur:
                return self.upsert_unplanned_event(dto, cursor=cur)

        existing = self.get_unplanned_event_at(dto.admission_id, dto.event_time, cursor=cursor)
        if existing is None:
            return self.create_event(dto, cursor=cursor)

        dto.id = existing.id
        return self.update_event_by_id(
            dto,
            expected_version=existing.version,
            cursor=cursor,
        )

    def upsert_event(
        self,
        dto: OralIntakeEventDTO,
        expected_version: Optional[int] = None,
        cursor=None,
    ) -> Optional[OralIntakeEventDTO]:
        if cursor is None:
            with self.db.remcard_transaction(source="oral_intake_upsert") as cur:
                return self.upsert_event(dto, expected_version=expected_version, cursor=cur)

        event_time = _dt_to_db(dto.event_time)
        shift_start = _dt_to_db(dto.shift_start)
        existing = self.get_event_at(dto.admission_id, dto.event_time, cursor=cursor)
        if existing:
            expected = int(expected_version if expected_version is not None else existing.version or 0)
            cursor.execute(
                """
                UPDATE oral_intake_events
                SET amount_ml = ?,
                    shift_start = ?,
                    plan_version_id = ?,
                    planned_item_key = ?,
                    entry_kind = ?,
                    meal_name = ?,
                    note = ?,
                    action_id = ?,
                    version = COALESCE(version, 0) + 1,
                    last_modified_by = ?,
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ? AND version = ?
                """,
                (
                    float(dto.amount_ml),
                    shift_start,
                    dto.plan_version_id,
                    dto.planned_item_key,
                    dto.entry_kind or "unplanned",
                    dto.meal_name,
                    dto.note,
                    dto.action_id,
                    dto.last_modified_by or "nurse",
                    int(existing.id),
                    expected,
                ),
            )
            if cursor.rowcount == 0:
                raise OptimisticLockError("Факт перорального ввода был изменен другим пользователем")
            return self.get_event_at(dto.admission_id, dto.event_time, cursor=cursor)

        cursor.execute(
            """
            INSERT INTO oral_intake_events (
                admission_id, shift_start, event_time, amount_ml,
                plan_version_id, planned_item_key, entry_kind, meal_name, note, action_id,
                version, last_modified_by, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
            """,
            (
                int(dto.admission_id),
                shift_start,
                event_time,
                float(dto.amount_ml),
                dto.plan_version_id,
                dto.planned_item_key,
                dto.entry_kind or "unplanned",
                dto.meal_name,
                dto.note,
                dto.action_id,
                dto.last_modified_by or "nurse",
            ),
        )
        return self.get_event_at(dto.admission_id, dto.event_time, cursor=cursor)

    def delete_event(
        self,
        admission_id: int,
        event_time: datetime,
        expected_version: Optional[int] = None,
        cursor=None,
    ):
        params = [int(admission_id), _dt_to_db(event_time)]
        where_version = ""
        if expected_version is not None and int(expected_version) > 0:
            where_version = " AND version = ?"
            params.append(int(expected_version))
        query = f"DELETE FROM oral_intake_events WHERE admission_id = ? AND DATETIME(event_time) = DATETIME(?){where_version}"
        cur = cursor.execute(query, tuple(params)) if cursor else self.db.execute_remcard(query, tuple(params))
        if expected_version is not None and int(expected_version) > 0 and cur.rowcount == 0:
            raise OptimisticLockError("Факт перорального ввода был изменен другим пользователем")

    def get_event(self, event_id: int, cursor=None) -> Optional[OralIntakeEventDTO]:
        query = "SELECT * FROM oral_intake_events WHERE id = ?"
        params = (int(event_id),)
        if cursor is not None:
            cursor.execute(query, params)
            row = cursor.fetchone()
        else:
            row = self.db.fetch_one_remcard(query, params)
        return self._map(row) if row else None

    def create_event(self, dto: OralIntakeEventDTO, cursor=None) -> OralIntakeEventDTO:
        if cursor is None:
            with self.db.remcard_transaction(source="oral_intake_create") as cur:
                return self.create_event(dto, cursor=cur)
        cursor.execute(
            """
            INSERT INTO oral_intake_events (
                admission_id, shift_start, event_time, amount_ml,
                plan_version_id, planned_item_key, entry_kind, meal_name, note, action_id,
                version, last_modified_by, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
            """,
            (
                int(dto.admission_id), _dt_to_db(dto.shift_start), _dt_to_db(dto.event_time),
                float(dto.amount_ml), dto.plan_version_id, dto.planned_item_key,
                dto.entry_kind or "unplanned", dto.meal_name, dto.note, dto.action_id,
                dto.last_modified_by or "nurse",
            ),
        )
        return self.get_event(int(cursor.lastrowid), cursor=cursor)

    def update_event_by_id(
        self,
        dto: OralIntakeEventDTO,
        expected_version: Optional[int] = None,
        cursor=None,
    ) -> OralIntakeEventDTO:
        if dto.id is None:
            raise ValueError("Event id is required")
        if cursor is None:
            with self.db.remcard_transaction(source="oral_intake_update") as cur:
                return self.update_event_by_id(dto, expected_version=expected_version, cursor=cur)
        current = self.get_event(int(dto.id), cursor=cursor)
        if current is None:
            raise ValueError("Факт питания не найден")
        expected = int(expected_version if expected_version is not None else current.version or 0)
        cursor.execute(
            """
            UPDATE oral_intake_events
            SET shift_start = ?, event_time = ?, amount_ml = ?, plan_version_id = ?,
                planned_item_key = ?, entry_kind = ?, meal_name = ?, note = ?, action_id = ?,
                version = COALESCE(version, 0) + 1, last_modified_by = ?,
                updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
            WHERE id = ? AND version = ?
            """,
            (
                _dt_to_db(dto.shift_start), _dt_to_db(dto.event_time), float(dto.amount_ml),
                dto.plan_version_id, dto.planned_item_key, dto.entry_kind or "unplanned",
                dto.meal_name, dto.note, dto.action_id, dto.last_modified_by or "doctor",
                int(dto.id), expected,
            ),
        )
        if cursor.rowcount != 1:
            raise OptimisticLockError("Факт питания был изменен другим пользователем")
        return self.get_event(int(dto.id), cursor=cursor)

    def delete_event_by_id(self, event_id: int, expected_version: Optional[int] = None, cursor=None):
        params = [int(event_id)]
        where_version = ""
        if expected_version is not None and int(expected_version) > 0:
            where_version = " AND version = ?"
            params.append(int(expected_version))
        query = f"DELETE FROM oral_intake_events WHERE id = ?{where_version}"
        cur = cursor.execute(query, tuple(params)) if cursor else self.db.execute_remcard(query, tuple(params))
        if expected_version is not None and int(expected_version) > 0 and cur.rowcount != 1:
            raise OptimisticLockError("Факт питания был изменен другим пользователем")

    def last_action_id(self, admission_id: int, actor: str) -> Optional[str]:
        row = self.db.fetch_one_remcard(
            """
            SELECT action_id FROM oral_intake_events
            WHERE admission_id = ? AND last_modified_by = ? AND COALESCE(action_id, '') <> ''
            ORDER BY DATETIME(created_at) DESC, id DESC LIMIT 1
            """,
            (int(admission_id), str(actor or "nurse")),
        )
        return str(row["action_id"]) if row and row["action_id"] else None

    def delete_action(self, admission_id: int, action_id: str, actor: Optional[str] = None) -> int:
        params: list = [int(admission_id), str(action_id)]
        actor_clause = ""
        if actor:
            actor_clause = " AND last_modified_by = ?"
            params.append(str(actor))
        with self.db.remcard_transaction(source="oral_intake_undo_action") as cur:
            cur.execute(
                f"DELETE FROM oral_intake_events WHERE admission_id = ? AND action_id = ?{actor_clause}",
                tuple(params),
            )
            return int(cur.rowcount or 0)

    def clear_events(self, admission_id: int, before: Optional[datetime] = None) -> int:
        params: list = [int(admission_id)]
        time_clause = ""
        if before is not None:
            time_clause = " AND DATETIME(event_time) < DATETIME(?)"
            params.append(_dt_to_db(before))
        with self.db.remcard_transaction(source="oral_intake_doctor_clear") as cur:
            cur.execute(f"DELETE FROM oral_intake_events WHERE admission_id = ?{time_clause}", tuple(params))
            return int(cur.rowcount or 0)

    def get_totals(self, admission_id: int, start: datetime, end: datetime, current_time: datetime) -> dict:
        rows = self.db.fetch_all_remcard(
            """
            SELECT event_time, amount_ml
            FROM oral_intake_events
            WHERE admission_id = ?
              AND DATETIME(event_time) >= DATETIME(?)
              AND DATETIME(event_time) < DATETIME(?)
            """,
            (int(admission_id), _dt_to_db(start), _dt_to_db(end)),
        )
        current_limit = _dt_to_db(current_time)
        current = 0.0
        daily = 0.0
        for row in rows:
            amount = float(row["amount_ml"] or 0.0)
            daily += amount
            if _parse_dt(row["event_time"]) <= _parse_dt(current_limit):
                current += amount
        return {"current": round(current, 1), "daily": round(daily, 1)}

    @staticmethod
    def _map(row) -> OralIntakeEventDTO:
        rd = dict(row)
        return OralIntakeEventDTO(
            id=rd.get("id"),
            admission_id=int(rd.get("admission_id") or 0),
            shift_start=_parse_dt(rd.get("shift_start")),
            event_time=_parse_dt(rd.get("event_time")),
            amount_ml=float(rd.get("amount_ml") or 0.0),
            plan_version_id=rd.get("plan_version_id"),
            planned_item_key=rd.get("planned_item_key"),
            entry_kind=rd.get("entry_kind") or "unplanned",
            meal_name=rd.get("meal_name") or "",
            note=rd.get("note") or "",
            action_id=rd.get("action_id"),
            version=int(rd.get("version") or 0),
            created_at=rd.get("created_at"),
            updated_at=rd.get("updated_at"),
            last_modified_by=rd.get("last_modified_by"),
        )

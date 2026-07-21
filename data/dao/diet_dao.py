from datetime import datetime
from typing import List, Optional

from rem_card.data.dao.exceptions import OptimisticLockError
from rem_card.data.dto.remcard_dto import DietPlanDTO, DietTemplateDTO, OralIntakeEventDTO


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
                name, diet_text, schedule_json, is_default, version, last_modified_by, updated_at
            )
            VALUES (?, ?, ?, ?, 1, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
            """,
            (
                dto.name,
                dto.diet_text,
                dto.schedule_json,
                int(dto.is_default or 0),
                dto.last_modified_by or "doctor",
            ),
        )
        if cursor:
            cur = cursor.execute(
                """
                INSERT INTO diet_templates (
                    name, diet_text, schedule_json, is_default, version, last_modified_by, updated_at
                )
                VALUES (?, ?, ?, ?, 1, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
                """,
                (
                    dto.name,
                    dto.diet_text,
                    dto.schedule_json,
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
                    version = COALESCE(version, 0) + 1,
                    last_modified_by = ?,
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ? AND version = ?
                """,
                (
                    dto.template_id,
                    dto.diet_text,
                    dto.schedule_json,
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
                admission_id, shift_start, template_id, diet_text, schedule_json,
                version, last_modified_by, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 1, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
            """,
            (
                int(dto.admission_id),
                shift_start,
                dto.template_id,
                dto.diet_text,
                dto.schedule_json,
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
            version=int(rd.get("version") or 0),
            created_at=rd.get("created_at"),
            updated_at=rd.get("updated_at"),
            last_modified_by=rd.get("last_modified_by"),
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
                    version = COALESCE(version, 0) + 1,
                    last_modified_by = ?,
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ? AND version = ?
                """,
                (
                    float(dto.amount_ml),
                    shift_start,
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
                version, last_modified_by, updated_at
            )
            VALUES (?, ?, ?, ?, 1, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
            """,
            (
                int(dto.admission_id),
                shift_start,
                event_time,
                float(dto.amount_ml),
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
            version=int(rd.get("version") or 0),
            created_at=rd.get("created_at"),
            updated_at=rd.get("updated_at"),
            last_modified_by=rd.get("last_modified_by"),
        )

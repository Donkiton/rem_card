import json
from datetime import datetime
from typing import Any, Optional, List

from ..dto.remcard_dto import (
    VentilationCaseDTO,
    VentilationDeliveryType,
    VentilationEventDTO,
    VentilationEventType,
    VentilationMode,
    VentilationStartType,
    VentilationTubeDTO,
)
from rem_card.services.concurrency import assert_revision_matches


class VentilationDAO:
    def __init__(self, db_manager):
        self.db = db_manager

    @staticmethod
    def _row_value(row, key: str, index: int = 0):
        if row is None:
            return None
        try:
            return row[key]
        except Exception:
            pass
        try:
            return row[index]
        except Exception:
            return None

    @staticmethod
    def _parse_dt(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        raw = str(value).replace(" ", "T")
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            # Fallback для старых форматов с лишней точностью дробной части
            if "." in raw:
                head, frac = raw.split(".", 1)
                return datetime.fromisoformat(f"{head}.{frac[:6]}")
            raise

    @staticmethod
    def _safe_enum(enum_cls, value: Optional[str], default):
        if value is None:
            return default
        try:
            return enum_cls(value)
        except Exception:
            return default

    @staticmethod
    def _safe_json_load(value: Optional[str]) -> dict[str, Any]:
        if not value:
            return {}
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except Exception:
            return {}

    def get_admission_datetime(self, admission_id: int) -> Optional[datetime]:
        row = self.db.fetch_one_remcard(
            "SELECT admission_datetime FROM admissions WHERE id = ?",
            (admission_id,),
        )
        if not row or not row["admission_datetime"]:
            return None
        return self._parse_dt(row["admission_datetime"])

    def get_next_episode_number(self, admission_id: int) -> int:
        row = self.db.fetch_one_remcard(
            "SELECT COALESCE(MAX(episode_number), 0) AS max_n FROM ivl_episodes WHERE admission_id = ?",
            (admission_id,),
        )
        return int((row["max_n"] if row and row["max_n"] is not None else 0) + 1)

    def get_case_by_id(self, case_id: int) -> Optional[VentilationCaseDTO]:
        row = self.db.fetch_one_remcard(
            "SELECT * FROM ivl_episodes WHERE id = ?",
            (case_id,),
        )
        return self._map_case(row) if row else None

    def get_latest_case(self, admission_id: int) -> Optional[VentilationCaseDTO]:
        row = self.db.fetch_one_remcard(
            """
            SELECT * FROM ivl_episodes
            WHERE admission_id = ?
            ORDER BY start_time DESC, id DESC
            LIMIT 1
            """,
            (admission_id,),
        )
        return self._map_case(row) if row else None

    def get_active_case(self, admission_id: int) -> Optional[VentilationCaseDTO]:
        row = self.db.fetch_one_remcard(
            """
            SELECT * FROM ivl_episodes
            WHERE admission_id = ?
              AND (is_active = 1 OR (is_active IS NULL AND end_time IS NULL))
            ORDER BY start_time DESC, id DESC
            LIMIT 1
            """,
            (admission_id,),
        )
        return self._map_case(row) if row else None

    def get_cases(self, admission_id: int) -> List[VentilationCaseDTO]:
        rows = self.db.fetch_all_remcard(
            """
            SELECT * FROM ivl_episodes
            WHERE admission_id = ?
            ORDER BY start_time ASC, id ASC
            """,
            (admission_id,),
        )
        return [self._map_case(row) for row in rows]

    def get_last_event(self, case_id: int) -> Optional[VentilationEventDTO]:
        row = self.db.fetch_one_remcard(
            """
            SELECT * FROM clinical_events
            WHERE ivl_episode_id = ?
            ORDER BY timestamp DESC, id DESC
            LIMIT 1
            """,
            (case_id,),
        )
        return self._map_event(row) if row else None

    def get_outcome_extubation_event(
        self,
        case_id: int,
        *,
        author: str = "SYSTEM_OUTCOME",
    ) -> Optional[VentilationEventDTO]:
        row = self.db.fetch_one_remcard(
            """
            SELECT * FROM clinical_events
            WHERE ivl_episode_id = ?
              AND event_type = 'EXTUBATION'
              AND author = ?
            ORDER BY timestamp DESC, id DESC
            LIMIT 1
            """,
            (case_id, author),
        )
        return self._map_event(row) if row else None

    def get_case_events(self, case_id: int) -> List[VentilationEventDTO]:
        rows = self.db.fetch_all_remcard(
            """
            SELECT * FROM clinical_events
            WHERE ivl_episode_id = ?
            ORDER BY timestamp ASC, id ASC
            """,
            (case_id,),
        )
        return [self._map_event(row) for row in rows]

    def get_active_tube(self, case_id: int) -> Optional[VentilationTubeDTO]:
        row = self.db.fetch_one_remcard(
            """
            SELECT * FROM devices
            WHERE ivl_episode_id = ?
              AND device_type IN ('ENDOTRACHEAL_TUBE', 'TRACHEOSTOMY_TUBE')
              AND removal_date IS NULL
            ORDER BY insertion_date DESC, id DESC
            LIMIT 1
            """,
            (case_id,),
        )
        return self._map_tube(row) if row else None

    def get_case_tubes(self, case_id: int) -> List[VentilationTubeDTO]:
        rows = self.db.fetch_all_remcard(
            """
            SELECT * FROM devices
            WHERE ivl_episode_id = ?
              AND device_type IN ('ENDOTRACHEAL_TUBE', 'TRACHEOSTOMY_TUBE')
            ORDER BY insertion_date ASC, id ASC
            """,
            (case_id,),
        )
        return [self._map_tube(row) for row in rows]

    def insert_case(
        self,
        cursor,
        *,
        admission_id: int,
        episode_number: int,
        start_time: datetime,
        case_type: str,
        start_type: VentilationStartType,
        delivery_type: VentilationDeliveryType,
    ) -> int:
        cursor.execute(
            """
            INSERT INTO ivl_episodes (
                admission_id, episode_number, start_time, end_time, type, start_type, delivery_type, is_active
            )
            VALUES (?, ?, ?, NULL, ?, ?, ?, 1)
            """,
            (
                admission_id,
                episode_number,
                start_time.isoformat(),
                case_type,
                start_type.value,
                delivery_type.value,
            ),
        )
        return int(cursor.lastrowid)

    def close_case(self, cursor, case_id: int, end_time: datetime):
        cursor.execute(
            """
            UPDATE ivl_episodes
            SET end_time = ?,
                is_active = 0,
                revision = COALESCE(revision, 0) + 1
            WHERE id = ?
            """,
            (end_time.isoformat(), case_id),
        )

    def set_case_end_time(self, cursor, case_id: int, end_time: datetime):
        cursor.execute(
            """
            UPDATE ivl_episodes
            SET end_time = ?,
                is_active = 0,
                revision = COALESCE(revision, 0) + 1
            WHERE id = ?
            """,
            (end_time.isoformat(), case_id),
        )

    def reopen_case(self, cursor, case_id: int):
        cursor.execute(
            """
            UPDATE ivl_episodes
            SET end_time = NULL,
                is_active = 1,
                revision = COALESCE(revision, 0) + 1
            WHERE id = ?
            """,
            (case_id,),
        )

    def bump_case_revision(self, cursor, case_id: int):
        cursor.execute(
            """
            UPDATE ivl_episodes
            SET revision = COALESCE(revision, 0) + 1
            WHERE id = ?
            """,
            (case_id,),
        )

    def assert_case_revision(self, cursor, case_id: int, expected_revision):
        if expected_revision is None:
            return
        cursor.execute("SELECT COALESCE(revision, 0) AS revision FROM ivl_episodes WHERE id = ?", (case_id,))
        row = cursor.fetchone()
        assert_revision_matches(row["revision"] if row else None, expected_revision)

    def assert_event_revision(self, cursor, event_id: int, expected_revision):
        if expected_revision is None:
            return
        cursor.execute("SELECT COALESCE(revision, 0) AS revision FROM clinical_events WHERE id = ?", (event_id,))
        row = cursor.fetchone()
        assert_revision_matches(row["revision"] if row else None, expected_revision)

    def insert_event(
        self,
        cursor,
        *,
        admission_id: int,
        case_id: int,
        event_time: datetime,
        event_type: VentilationEventType,
        mode: Optional[VentilationMode],
        parameters_json: Optional[str],
        extubation_reason: Optional[str],
        o2_flow: Optional[float],
        author: Optional[str],
    ) -> int:
        cursor.execute(
            """
            INSERT INTO clinical_events (
                admission_id, ivl_episode_id, timestamp, event_type, mode,
                data, parameters_json, extubation_reason, o2_flow, author, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
            """,
            (
                admission_id,
                case_id,
                event_time.isoformat(),
                event_type.value,
                mode.value if mode else None,
                parameters_json,
                parameters_json,
                extubation_reason,
                o2_flow,
                author,
            ),
        )
        return int(cursor.lastrowid)

    def update_extubation_event(
        self,
        cursor,
        *,
        event_id: int,
        event_time: datetime,
        extubation_reason: Optional[str],
        author: Optional[str],
    ):
        cursor.execute(
            """
            UPDATE clinical_events
            SET timestamp = ?,
                extubation_reason = ?,
                author = ?,
                revision = COALESCE(revision, 0) + 1
            WHERE id = ?
              AND event_type = 'EXTUBATION'
            """,
            (
                event_time.isoformat(),
                extubation_reason,
                author,
                event_id,
            ),
        )

    def insert_respiratory_support(
        self,
        cursor,
        *,
        admission_id: int,
        case_id: int,
        event_id: int,
        event_time: datetime,
        mode: Optional[VentilationMode],
        parameters: dict[str, Any],
        parameters_json: Optional[str],
    ) -> int:
        cursor.execute(
            """
            INSERT INTO respiratory_support (
                admission_id, ivl_episode_id, event_id, datetime, mode, fio2, peep, tv, rr, parameters_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                admission_id,
                case_id,
                event_id,
                event_time.isoformat(),
                mode.value if mode else None,
                parameters.get("FiO2"),
                parameters.get("PEEP"),
                parameters.get("TV"),
                parameters.get("RR"),
                parameters_json,
            ),
        )
        return int(cursor.lastrowid)

    def insert_tube(
        self,
        cursor,
        *,
        admission_id: int,
        case_id: int,
        insertion_time: datetime,
        device_type: str = "ENDOTRACHEAL_TUBE",
        location: Optional[str] = None,
    ) -> int:
        cursor.execute(
            """
            INSERT INTO devices (
                admission_id, ivl_episode_id, device_type, insertion_date, location, created_at
            )
            VALUES (?, ?, ?, ?, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
            """,
            (
                admission_id,
                case_id,
                device_type,
                insertion_time.isoformat(),
                location,
            ),
        )
        return int(cursor.lastrowid)

    def close_tube(self, cursor, tube_id: int, at_time: datetime):
        cursor.execute(
            """
            UPDATE devices
            SET removal_date = ?, replacement_time = ?
            WHERE id = ?
            """,
            (at_time.isoformat(), at_time.isoformat(), tube_id),
        )

    def close_active_tubes_for_case(self, cursor, case_id: int, at_time: datetime):
        cursor.execute(
            """
            UPDATE devices
            SET removal_date = COALESCE(removal_date, ?),
                replacement_time = COALESCE(replacement_time, ?)
            WHERE ivl_episode_id = ?
              AND device_type IN ('ENDOTRACHEAL_TUBE', 'TRACHEOSTOMY_TUBE')
              AND removal_date IS NULL
            """,
            (at_time.isoformat(), at_time.isoformat(), case_id),
        )

    def move_tube_closure_time(self, cursor, case_id: int, from_time: datetime, to_time: datetime):
        cursor.execute(
            """
            UPDATE devices
            SET removal_date = ?,
                replacement_time = ?
            WHERE ivl_episode_id = ?
              AND device_type IN ('ENDOTRACHEAL_TUBE', 'TRACHEOSTOMY_TUBE')
              AND removal_date = ?
            """,
            (
                to_time.isoformat(),
                to_time.isoformat(),
                case_id,
                from_time.isoformat(),
            ),
        )

    def reopen_tubes_closed_at(self, cursor, case_id: int, at_time: datetime):
        cursor.execute(
            """
            UPDATE devices
            SET removal_date = NULL,
                replacement_time = NULL
            WHERE ivl_episode_id = ?
              AND device_type IN ('ENDOTRACHEAL_TUBE', 'TRACHEOSTOMY_TUBE')
              AND removal_date = ?
            """,
            (case_id, at_time.isoformat()),
        )

    def get_latest_tube_id(self, cursor, case_id: int) -> Optional[int]:
        cursor.execute(
            """
            SELECT id FROM devices
            WHERE ivl_episode_id = ?
              AND device_type IN ('ENDOTRACHEAL_TUBE', 'TRACHEOSTOMY_TUBE')
            ORDER BY insertion_date DESC, id DESC
            LIMIT 1
            """,
            (case_id,),
        )
        row = cursor.fetchone()
        tube_id = self._row_value(row, "id")
        return int(tube_id) if tube_id is not None else None

    def delete_tube(self, cursor, tube_id: int):
        cursor.execute("DELETE FROM devices WHERE id = ?", (tube_id,))

    def delete_case_tubes(self, cursor, case_id: int):
        cursor.execute(
            """
            DELETE FROM devices
            WHERE ivl_episode_id = ?
              AND device_type IN ('ENDOTRACHEAL_TUBE', 'TRACHEOSTOMY_TUBE')
            """,
            (case_id,),
        )

    def delete_case(self, cursor, case_id: int):
        cursor.execute("DELETE FROM ivl_episodes WHERE id = ?", (case_id,))

    def delete_respiratory_support_by_event(self, cursor, event_id: int):
        cursor.execute("DELETE FROM respiratory_support WHERE event_id = ?", (event_id,))

    def delete_event(self, cursor, event_id: int):
        cursor.execute("DELETE FROM clinical_events WHERE id = ?", (event_id,))

    def count_case_events(self, cursor, case_id: int) -> int:
        cursor.execute(
            "SELECT COUNT(*) AS n FROM clinical_events WHERE ivl_episode_id = ?",
            (case_id,),
        )
        row = cursor.fetchone()
        n = self._row_value(row, "n")
        return int(n or 0)

    def _map_case(self, row) -> VentilationCaseDTO:
        r = dict(row)
        legacy_type = str(r.get("type") or "transfer")
        inferred_start_type = (
            VentilationStartType.ADMISSION
            if legacy_type.lower() == "delivery"
            else VentilationStartType.IN_DEPARTMENT
        )
        start_type = self._safe_enum(
            VentilationStartType,
            r.get("start_type"),
            inferred_start_type,
        )
        delivery_type = self._safe_enum(
            VentilationDeliveryType,
            r.get("delivery_type"),
            VentilationDeliveryType.UNKNOWN,
        )
        is_active_raw = r.get("is_active")
        if is_active_raw is None:
            is_active = r.get("end_time") is None
        else:
            is_active = bool(is_active_raw)

        return VentilationCaseDTO(
            id=r.get("id"),
            admission_id=r.get("admission_id") or 0,
            episode_number=r.get("episode_number") or 0,
            start_time=self._parse_dt(r.get("start_time")) or datetime.now(),
            end_time=self._parse_dt(r.get("end_time")),
            type=legacy_type,
            start_type=start_type,
            delivery_type=delivery_type,
            is_active=is_active,
            revision=int(r.get("revision") or 0),
        )

    def _map_event(self, row) -> VentilationEventDTO:
        r = dict(row)
        params = self._safe_json_load(r.get("parameters_json"))
        if not params and r.get("data"):
            params = self._safe_json_load(r.get("data"))
        author = r.get("author")
        if isinstance(author, str) and author.strip().lower() == "doctor":
            author = "Доктор"

        return VentilationEventDTO(
            id=r.get("id"),
            admission_id=r.get("admission_id") or 0,
            ivl_episode_id=r.get("ivl_episode_id") or 0,
            timestamp=self._parse_dt(r.get("timestamp")) or datetime.now(),
            event_type=self._safe_enum(
                VentilationEventType,
                r.get("event_type"),
                VentilationEventType.MODE_CHANGE,
            ),
            mode=self._safe_enum(VentilationMode, r.get("mode"), None),
            parameters=params,
            extubation_reason=r.get("extubation_reason"),
            o2_flow=r.get("o2_flow"),
            author=author,
            revision=int(r.get("revision") or 0),
        )

    def _map_tube(self, row) -> VentilationTubeDTO:
        r = dict(row)
        return VentilationTubeDTO(
            id=r.get("id"),
            admission_id=r.get("admission_id") or 0,
            ivl_episode_id=r.get("ivl_episode_id") or 0,
            device_type=r.get("device_type") or "ENDOTRACHEAL_TUBE",
            insertion_time=self._parse_dt(r.get("insertion_date")) or datetime.now(),
            replacement_time=self._parse_dt(r.get("replacement_time")),
            removal_time=self._parse_dt(r.get("removal_date")),
            location=r.get("location"),
        )

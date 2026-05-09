import json
from typing import Any, Dict, List, Optional
from datetime import datetime
from ..dto.remcard_dto import PatientStatus, PatientStatusEventDTO
from rem_card.app.logger import logger
from rem_card.services.concurrency import DataConflictError, DATA_CONFLICT_MESSAGE, assert_revision_matches
from .sync_cursor import is_cursor_newer, make_sync_cursor, normalize_sync_cursor

class PatientStatusDAO:
    def __init__(self, db_manager):
        self.db = db_manager

    def get_event_by_id(self, event_id: int) -> Optional[PatientStatusEventDTO]:
        row = self.db.fetch_one_remcard(
            "SELECT * FROM patient_status_events WHERE id = ?",
            (event_id,),
        )
        return self._map_row(row) if row else None

    def get_events(self, admission_id: int) -> List[PatientStatusEventDTO]:
        """Возвращает все события статуса для госпитализации, отсортированные по времени."""
        query = "SELECT * FROM patient_status_events WHERE admission_id = ? ORDER BY start_time ASC"
        rows = self.db.fetch_all_remcard(query, (admission_id,))
        return [self._map_row(r) for r in rows]

    def get_active_event(self, admission_id: int) -> Optional[PatientStatusEventDTO]:
        """Возвращает текущее активное событие (end_time IS NULL)."""
        query = "SELECT * FROM patient_status_events WHERE admission_id = ? AND end_time IS NULL"
        row = self.db.fetch_one_remcard(query, (admission_id,))
        return self._map_row(row) if row else None

    def get_admission_outcome_context(self, admission_id: int) -> Dict[str, Any]:
        """Возвращает поля госпитализации, нужные для диалогов исхода."""
        row = self.db.fetch_one_remcard(
            """
            SELECT
                a.id,
                a.admission_datetime,
                a.department_profile,
                a.source_department,
                a.history_number,
                a.patient_age,
                a.patient_months,
                a.patient_age_unit,
                a.patient_gender,
                p.birth_date,
                p.last_name,
                p.first_name,
                p.middle_name,
                p.full_name,
                TRIM(
                    COALESCE(p.last_name, '') || ' ' ||
                    COALESCE(p.first_name, '') || ' ' ||
                    COALESCE(p.middle_name, '')
                ) AS patient_name,
                a.transfer_datetime,
                a.transfer_department,
                a.transfer_lpu,
                a.transfer_lpu_other,
                a.death_datetime,
                a.clinical_death_datetime,
                a.cardiac_arrest_cause,
                a.cardiac_arrest_measures_json,
                a.outcome,
                COALESCE(a.revision, 0) AS revision
            FROM admissions a
            JOIN patients p ON a.patient_id = p.id
            WHERE a.id = ?
            """,
            (admission_id,),
        )
        if not row:
            return {}

        context = dict(row)
        context["active_ventilation"] = self._get_active_ventilation_context(admission_id)
        return context

    def _get_active_ventilation_context(self, admission_id: int) -> Optional[Dict[str, Any]]:
        """Возвращает текущий режим/параметры активного случая ИВЛ для окна исхода смерти."""
        active_case = self._fetch_one_optional(
            """
            SELECT id, episode_number, start_time
            FROM ivl_episodes
            WHERE admission_id = ?
              AND (is_active = 1 OR (is_active IS NULL AND end_time IS NULL))
            ORDER BY start_time DESC, id DESC
            LIMIT 1
            """,
            (admission_id,),
            "active ventilation case with is_active",
        )
        if not active_case:
            active_case = self._fetch_one_optional(
                """
                SELECT id, episode_number, start_time
                FROM ivl_episodes
                WHERE admission_id = ?
                  AND end_time IS NULL
                ORDER BY start_time DESC, id DESC
                LIMIT 1
                """,
                (admission_id,),
                "active ventilation case legacy",
            )
        if not active_case:
            return None

        case = dict(active_case)
        case_id = case.get("id")
        case_start = case.get("start_time")
        support = self._fetch_latest_respiratory_support(
            int(case_id) if case_id is not None else None,
            admission_id,
            case_start,
        )
        event = self._fetch_latest_ventilation_event(
            int(case_id) if case_id is not None else None,
            admission_id,
            case_start,
        )

        mode = (support or {}).get("mode") or (event or {}).get("mode")
        if not mode and event:
            event_data = self._safe_json_dict(event.get("data"))
            mode = event_data.get("mode") or event_data.get("ventilation_mode")

        parameters = self._extract_ventilation_parameters(support)
        event_parameters = self._extract_ventilation_parameters(event)
        for key, value in event_parameters.items():
            parameters.setdefault(key, value)

        if not mode and not parameters:
            return None

        source_time = (support or {}).get("datetime") or (event or {}).get("timestamp")
        return {
            "case_id": case_id,
            "episode_number": case.get("episode_number"),
            "start_time": case_start,
            "source_time": source_time,
            "mode": str(mode).strip() if mode is not None else "",
            "parameters": parameters,
        }

    def _fetch_latest_respiratory_support(
        self,
        case_id: Optional[int],
        admission_id: int,
        case_start: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        queries = []
        if case_id is not None:
            queries.append(
                (
                    """
                    SELECT mode, parameters_json, fio2, peep, tv, rr, datetime
                    FROM respiratory_support
                    WHERE ivl_episode_id = ?
                    ORDER BY datetime DESC, id DESC
                    LIMIT 1
                    """,
                    (case_id,),
                    "latest respiratory support by case",
                )
            )
        queries.extend(
            [
                (
                    """
                    SELECT mode, parameters_json, fio2, peep, tv, rr, datetime
                    FROM respiratory_support
                    WHERE admission_id = ?
                      AND (? IS NULL OR DATETIME(datetime) >= DATETIME(?))
                    ORDER BY datetime DESC, id DESC
                    LIMIT 1
                    """,
                    (admission_id, case_start, case_start),
                    "latest respiratory support by admission",
                ),
                (
                    """
                    SELECT mode, fio2, peep, tv, rr, datetime
                    FROM respiratory_support
                    WHERE admission_id = ?
                      AND (? IS NULL OR DATETIME(datetime) >= DATETIME(?))
                    ORDER BY datetime DESC, id DESC
                    LIMIT 1
                    """,
                    (admission_id, case_start, case_start),
                    "latest respiratory support legacy",
                ),
            ]
        )

        for query, params, label in queries:
            row = self._fetch_one_optional(query, params, label)
            if row:
                return dict(row)
        return None

    def _fetch_latest_ventilation_event(
        self,
        case_id: Optional[int],
        admission_id: int,
        case_start: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        queries = []
        if case_id is not None:
            queries.append(
                (
                    """
                    SELECT mode, parameters_json, data, timestamp
                    FROM clinical_events
                    WHERE ivl_episode_id = ?
                      AND event_type IN ('START_VENT', 'MODE_CHANGE')
                      AND (mode IS NOT NULL OR parameters_json IS NOT NULL OR data IS NOT NULL)
                    ORDER BY timestamp DESC, id DESC
                    LIMIT 1
                    """,
                    (case_id,),
                    "latest ventilation event by case",
                )
            )
        queries.extend(
            [
                (
                    """
                    SELECT mode, parameters_json, data, timestamp
                    FROM clinical_events
                    WHERE admission_id = ?
                      AND (? IS NULL OR DATETIME(timestamp) >= DATETIME(?))
                      AND event_type IN ('START_VENT', 'MODE_CHANGE')
                    ORDER BY timestamp DESC, id DESC
                    LIMIT 1
                    """,
                    (admission_id, case_start, case_start),
                    "latest ventilation event by admission",
                ),
                (
                    """
                    SELECT data, timestamp, event_type
                    FROM clinical_events
                    WHERE admission_id = ?
                      AND (? IS NULL OR DATETIME(timestamp) >= DATETIME(?))
                      AND event_type IN ('START_VENT', 'MODE_CHANGE')
                    ORDER BY timestamp DESC, id DESC
                    LIMIT 1
                    """,
                    (admission_id, case_start, case_start),
                    "latest ventilation event legacy",
                ),
            ]
        )

        for query, params, label in queries:
            row = self._fetch_one_optional(query, params, label)
            if row:
                return dict(row)
        return None

    def _fetch_one_optional(self, query: str, params, label: str):
        try:
            return self.db.fetch_one_remcard(query, params)
        except Exception as exc:
            logger.debug("[StatusDAO] Optional outcome context query failed (%s): %s", label, exc)
            return None

    def _extract_ventilation_parameters(self, row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not row:
            return {}

        parameters = self._safe_json_dict(row.get("parameters_json"))
        data = self._safe_json_dict(row.get("data"))
        if not parameters and isinstance(data.get("parameters"), dict):
            parameters = dict(data.get("parameters") or {})
        if not parameters and isinstance(data.get("params"), dict):
            parameters = dict(data.get("params") or {})
        if not parameters:
            known_from_data = {
                self._normalize_vent_parameter_key(key): value
                for key, value in data.items()
                if self._normalize_vent_parameter_key(key)
                in {"RR", "TV", "Pinsp", "PEEP", "FiO2", "PS", "Flow", "Phigh", "Plow", "Thigh", "Tlow"}
            }
            parameters.update(known_from_data)

        standalone = {
            "FiO2": row.get("fio2"),
            "PEEP": row.get("peep"),
            "TV": row.get("tv"),
            "RR": row.get("rr"),
        }
        for key, value in standalone.items():
            if value is not None and str(value).strip() != "":
                parameters.setdefault(key, value)

        normalized: Dict[str, Any] = {}
        for key, value in parameters.items():
            normalized_key = self._normalize_vent_parameter_key(str(key))
            if value is None or str(value).strip() == "":
                continue
            normalized[normalized_key] = value
        return normalized

    @staticmethod
    def _safe_json_dict(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        if not value:
            return {}
        try:
            decoded = json.loads(value)
        except Exception:
            return {}
        return decoded if isinstance(decoded, dict) else {}

    @staticmethod
    def _normalize_vent_parameter_key(key: str) -> str:
        lut = {
            "fio2": "FiO2",
            "peep": "PEEP",
            "rr": "RR",
            "tv": "TV",
            "pinsp": "Pinsp",
            "ps": "PS",
            "flow": "Flow",
            "phigh": "Phigh",
            "plow": "Plow",
            "thigh": "Thigh",
            "tlow": "Tlow",
        }
        return lut.get(key.lower(), key)

    def fetch_updated_events(self, admission_id: int, last_sync_time):
        """Возвращает измененные события статуса для поллинга."""
        last_sync_ts, last_sync_id = normalize_sync_cursor(last_sync_time)
        query = """
            SELECT *
            FROM patient_status_events
            WHERE admission_id = ?
              AND (
                  COALESCE(STRFTIME('%Y-%m-%d %H:%M:%f', updated_at), '') > ?
                  OR (
                      COALESCE(STRFTIME('%Y-%m-%d %H:%M:%f', updated_at), '') = ?
                      AND id > ?
                  )
              )
            ORDER BY COALESCE(STRFTIME('%Y-%m-%d %H:%M:%f', updated_at), '') ASC, id ASC
        """
        rows = self.db.fetch_all_remcard(query, (admission_id, last_sync_ts, last_sync_ts, last_sync_id))
        
        events = []
        new_sync_cursor = make_sync_cursor(last_sync_ts, last_sync_id)
        if rows:
            for row in rows:
                rd = dict(row)
                row_updated_at = str(rd.get("updated_at") or new_sync_cursor["updated_at"])
                row_id = int(rd.get("id") or 0)
                if is_cursor_newer(row_updated_at, row_id, new_sync_cursor["updated_at"], new_sync_cursor["id"]):
                    new_sync_cursor = make_sync_cursor(row_updated_at, row_id)
                events.append(self._map_row(rd))
        if isinstance(last_sync_time, dict):
            return events, new_sync_cursor
        return events, new_sync_cursor["updated_at"]

    def change_status(self, admission_id: int, new_status: PatientStatus, 
                      reason_type: Optional[str] = None, reason_text: Optional[str] = None, 
                      user_id: Optional[str] = None,
                      expected_active_event_id: Optional[int] = None,
                      expected_active_revision: Optional[int] = None) -> bool:
        """
        Атомарно меняет статус пациента: закрывает текущий и открывает новый.
        Реализует концепцию полуоткрытых интервалов [start, end).
        """
        # Если пришел только reason_text (после упрощения UI), используем его
        now_str = datetime.now().isoformat()
        
        try:
            with self.db.remcard_transaction() as cursor:
                # 1. Проверяем, не закрыта ли уже госпитализация (опционально, зависит от бизнес-логики)
                
                # 2. Ищем текущее активное событие
                cursor.execute(
                    "SELECT id, status, COALESCE(revision, 0) AS revision FROM patient_status_events WHERE admission_id = ? AND end_time IS NULL",
                    (admission_id,)
                )
                current_active = cursor.fetchone()
                
                if current_active:
                    if expected_active_event_id is not None and int(current_active["id"]) != int(expected_active_event_id):
                        raise DataConflictError(DATA_CONFLICT_MESSAGE)
                    assert_revision_matches(current_active["revision"], expected_active_revision)
                    old_status = current_active['status']
                    if old_status == new_status.value:
                        logger.debug(f"[StatusDAO] Status for admission {admission_id} is already {old_status}. No change needed.")
                        return True # Статус уже такой, ничего не меняем
                    
                    # Закрываем старое событие
                    cursor.execute(
                        """
                        UPDATE patient_status_events
                        SET end_time = ?,
                            updated_at = ?,
                            last_modified_by = ?,
                            revision = COALESCE(revision, 0) + 1
                        WHERE id = ?
                        """,
                        (now_str, now_str, user_id, current_active['id'])
                    )
                    if cursor.rowcount != 1:
                        raise DataConflictError(DATA_CONFLICT_MESSAGE)
                    logger.info(f"[StatusDAO] Admission {admission_id}: status changed {old_status} -> {new_status.value} by {user_id}")
                else:
                    logger.warning(f"[StatusDAO] Admission {admission_id} had no active status! Creating new {new_status.value}")

                # 3. Создаем новое событие
                cursor.execute("""
                    INSERT INTO patient_status_events 
                    (admission_id, status, reason_type, reason_text, start_time, created_by, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    admission_id, new_status.value, reason_type, reason_text, 
                    now_str, user_id, now_str, now_str
                ))
                
                # При смене статуса на DEAD/TRANSFERRED мы НЕ трогаем is_active, 
                # так как за список коек (W1) отвечает Журнал.
                return True
        except DataConflictError:
            raise
        except Exception as e:
            logger.error(f"[StatusDAO] Error changing status for admission {admission_id}: {e}", exc_info=True)
            return False

    def change_status_with_outcome_details(
        self,
        admission_id: int,
        new_status: PatientStatus,
        event_time: datetime,
        reason_type: Optional[str] = None,
        reason_text: Optional[str] = None,
        user_id: Optional[str] = None,
        admission_details: Optional[Dict[str, Any]] = None,
        expected_active_event_id: Optional[int] = None,
        expected_active_revision: Optional[int] = None,
        expected_admission_revision: Optional[int] = None,
    ) -> bool:
        """
        Меняет статус на финальный исход и в той же транзакции записывает
        структурированные поля исхода в admissions.
        """
        if new_status not in (PatientStatus.TRANSFERRED, PatientStatus.DEAD):
            return self.change_status(
                admission_id,
                new_status,
                reason_type,
                reason_text,
                user_id,
                expected_active_event_id=expected_active_event_id,
                expected_active_revision=expected_active_revision,
            )

        details = dict(admission_details or {})
        event_dt = (event_time or datetime.now()).replace(second=0, microsecond=0)
        event_time_str = event_dt.isoformat()
        now_str = datetime.now().replace(microsecond=0).isoformat()

        if new_status == PatientStatus.TRANSFERRED:
            transfer_department = str(details.get("transfer_department") or "").strip()
            transfer_lpu = str(details.get("transfer_lpu") or "").strip() or None
            transfer_lpu_other = str(details.get("transfer_lpu_other") or "").strip() or None
            if not transfer_department:
                logger.warning("[StatusDAO] Transfer outcome rejected: no transfer_department")
                return False
            if transfer_department == "Другое ЛПУ" and not transfer_lpu:
                logger.warning("[StatusDAO] Transfer outcome rejected: no transfer_lpu")
                return False
            if transfer_lpu == "Другое ЛПУ" and not transfer_lpu_other:
                logger.warning("[StatusDAO] Transfer outcome rejected: no transfer_lpu_other")
                return False
        else:
            transfer_department = None
            transfer_lpu = None
            transfer_lpu_other = None

        clinical_death = details.get("clinical_death_datetime")
        if isinstance(clinical_death, datetime):
            clinical_death = clinical_death.replace(second=0, microsecond=0).isoformat()
        elif clinical_death is not None:
            clinical_death = str(clinical_death)

        measures_json = details.get("cardiac_arrest_measures_json")
        cardiac_arrest_cause = details.get("cardiac_arrest_cause")

        try:
            with self.db.remcard_transaction(source="status_outcome_details") as cursor:
                cursor.execute(
                    "SELECT id, status, start_time, COALESCE(revision, 0) AS revision FROM patient_status_events WHERE admission_id = ? AND end_time IS NULL",
                    (admission_id,),
                )
                current_active = cursor.fetchone()

                if current_active:
                    if expected_active_event_id is not None and int(current_active["id"]) != int(expected_active_event_id):
                        raise DataConflictError(DATA_CONFLICT_MESSAGE)
                    assert_revision_matches(current_active["revision"], expected_active_revision)
                    old_status = current_active["status"]
                    if old_status == new_status.value:
                        logger.debug(
                            "[StatusDAO] Admission %s is already in final status %s",
                            admission_id,
                            old_status,
                        )
                        return False

                    current_start = self._parse_sqlite_dt(current_active["start_time"])
                    if current_start and event_dt < current_start.replace(second=0, microsecond=0):
                        logger.warning(
                            "[StatusDAO] Outcome time %s is earlier than current status start %s for admission %s",
                            event_dt,
                            current_start,
                            admission_id,
                        )
                        return False

                    cursor.execute(
                        """
                        UPDATE patient_status_events
                        SET end_time = ?,
                            updated_at = ?,
                            last_modified_by = ?,
                            revision = COALESCE(revision, 0) + 1
                        WHERE id = ?
                        """,
                        (event_time_str, now_str, user_id, current_active["id"]),
                    )
                    if cursor.rowcount != 1:
                        raise DataConflictError(DATA_CONFLICT_MESSAGE)
                    logger.info(
                        "[StatusDAO] Admission %s: status changed %s -> %s at %s by %s",
                        admission_id,
                        old_status,
                        new_status.value,
                        event_time_str,
                        user_id,
                    )
                else:
                    logger.warning(
                        "[StatusDAO] Admission %s had no active status. Creating final %s at %s",
                        admission_id,
                        new_status.value,
                        event_time_str,
                    )

                cursor.execute(
                    """
                    INSERT INTO patient_status_events
                    (admission_id, status, reason_type, reason_text, start_time, created_by, created_at, updated_at, last_modified_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        admission_id,
                        new_status.value,
                        reason_type,
                        reason_text,
                        event_time_str,
                        user_id,
                        now_str,
                        now_str,
                        user_id,
                    ),
                )

                if new_status == PatientStatus.TRANSFERRED:
                    cursor.execute(
                        """
                        UPDATE admissions
                        SET outcome = ?,
                            transfer_datetime = ?,
                            transfer_department = ?,
                            transfer_lpu = ?,
                            transfer_lpu_other = ?,
                            death_datetime = NULL,
                            clinical_death_datetime = NULL,
                            cardiac_arrest_cause = NULL,
                            cardiac_arrest_measures_json = NULL,
                            updated_at = ?,
                            revision = COALESCE(revision, 0) + 1
                        WHERE id = ?
                          AND (? IS NULL OR COALESCE(revision, 0) = ?)
                        """,
                        (
                            "переведен",
                            event_time_str,
                            transfer_department,
                            transfer_lpu,
                            transfer_lpu_other,
                            now_str,
                            admission_id,
                            expected_admission_revision,
                            expected_admission_revision,
                        ),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE admissions
                        SET outcome = ?,
                            transfer_datetime = NULL,
                            transfer_department = NULL,
                            transfer_lpu = NULL,
                            transfer_lpu_other = NULL,
                            death_datetime = ?,
                            clinical_death_datetime = ?,
                            cardiac_arrest_cause = ?,
                            cardiac_arrest_measures_json = ?,
                            updated_at = ?,
                            revision = COALESCE(revision, 0) + 1
                        WHERE id = ?
                          AND (? IS NULL OR COALESCE(revision, 0) = ?)
                        """,
                        (
                            "умер",
                            event_time_str,
                            clinical_death,
                            cardiac_arrest_cause,
                            measures_json,
                            now_str,
                            admission_id,
                            expected_admission_revision,
                            expected_admission_revision,
                        ),
                    )
                if expected_admission_revision is not None and cursor.rowcount != 1:
                    raise DataConflictError(DATA_CONFLICT_MESSAGE)

                return True
        except DataConflictError:
            raise
        except Exception as e:
            logger.error(
                "[StatusDAO] Error changing final status for admission %s: %s",
                admission_id,
                e,
                exc_info=True,
            )
            return False

    def rollback_last_status(
        self,
        admission_id: int,
        expected_active_event_id: Optional[int] = None,
        expected_active_revision: Optional[int] = None,
    ) -> bool:
        """
        Откатывает последнее изменение статуса:
        удаляет текущее активное событие и открывает предыдущее.
        """
        now_str = datetime.now().replace(microsecond=0).isoformat()
        try:
            with self.db.remcard_transaction() as cursor:
                # 1. Ищем текущее активное событие
                cursor.execute(
                    "SELECT id, status, COALESCE(revision, 0) AS revision FROM patient_status_events WHERE admission_id = ? AND end_time IS NULL",
                    (admission_id,)
                )
                current = cursor.fetchone()
                if not current:
                    return False
                if expected_active_event_id is not None and int(current["id"]) != int(expected_active_event_id):
                    raise DataConflictError(DATA_CONFLICT_MESSAGE)
                assert_revision_matches(current["revision"], expected_active_revision)
                current_status = current["status"]

                # 2. Проверяем, сколько всего событий. Нельзя удалять единственное (начальное).
                cursor.execute(
                    "SELECT count(*) as cnt FROM patient_status_events WHERE admission_id = ?",
                    (admission_id,)
                )
                if cursor.fetchone()['cnt'] <= 1:
                    logger.warning(f"[StatusDAO] Cannot rollback: only one status exists for admission {admission_id}")
                    return False

                # 3. Находим предыдущее событие до удаления текущего.
                cursor.execute("""
                    SELECT id, status FROM patient_status_events 
                    WHERE admission_id = ?
                      AND id != ?
                    ORDER BY start_time DESC, id DESC LIMIT 1
                """, (admission_id, current['id']))
                prev = cursor.fetchone()
                if prev:
                    current_is_outcome = current_status in (
                        PatientStatus.TRANSFERRED.value,
                        PatientStatus.DEAD.value,
                    )
                    previous_is_outcome = prev["status"] in (
                        PatientStatus.TRANSFERRED.value,
                        PatientStatus.DEAD.value,
                    )
                    should_restore_bed = current_is_outcome and not previous_is_outcome
                    bed_number = None
                    bed_row = None

                    if should_restore_bed:
                        cursor.execute(
                            "SELECT bed_number FROM admissions WHERE id = ?",
                            (admission_id,),
                        )
                        admission_row = cursor.fetchone()
                        if not admission_row:
                            logger.warning(
                                "[StatusDAO] Cannot rollback outcome: admission %s not found",
                                admission_id,
                            )
                            return False
                        bed_number = int(admission_row["bed_number"])
                        cursor.execute(
                            "SELECT bed_number, current_admission_id FROM beds WHERE bed_number = ?",
                            (bed_number,),
                        )
                        bed_row = cursor.fetchone()
                        occupied_by = int(bed_row["current_admission_id"]) if bed_row and bed_row["current_admission_id"] is not None else None
                        if occupied_by is not None and occupied_by != int(admission_id):
                            raise DataConflictError(
                                f"Койка {bed_number} уже занята другим пациентом. Откат исхода невозможен без переноса койки."
                            )

                    # 4. Удаляем текущее событие
                    cursor.execute("DELETE FROM patient_status_events WHERE id = ?", (current['id'],))

                    # 5. Открываем предыдущее событие.
                    cursor.execute(
                        """
                        UPDATE patient_status_events
                        SET end_time = NULL,
                            updated_at = ?,
                            revision = COALESCE(revision, 0) + 1
                        WHERE id = ?
                        """,
                        (now_str, prev['id'])
                    )

                    if should_restore_bed:
                        cursor.execute(
                            """
                            UPDATE admissions
                            SET outcome = NULL,
                                is_active = 1,
                                transfer_datetime = NULL,
                                transfer_department = NULL,
                                transfer_lpu = NULL,
                                transfer_lpu_other = NULL,
                                death_datetime = NULL,
                                clinical_death_datetime = NULL,
                                cardiac_arrest_cause = NULL,
                                cardiac_arrest_measures_json = NULL,
                                updated_at = ?,
                                revision = COALESCE(revision, 0) + 1
                            WHERE id = ?
                            """,
                            (now_str, admission_id),
                        )

                        if bed_row:
                            cursor.execute(
                                """
                                UPDATE beds
                                SET current_admission_id = ?,
                                    status = 'OCCUPIED',
                                    revision = COALESCE(revision, 0) + 1
                                WHERE bed_number = ?
                                  AND (current_admission_id IS NULL OR current_admission_id = ?)
                                """,
                                (admission_id, bed_number, admission_id),
                            )
                            if cursor.rowcount != 1:
                                raise DataConflictError(DATA_CONFLICT_MESSAGE)
                        else:
                            cursor.execute(
                                """
                                INSERT INTO beds (bed_number, status, current_admission_id, revision)
                                VALUES (?, 'OCCUPIED', ?, 1)
                                """,
                                (bed_number, admission_id),
                            )
                    
                return True
        except DataConflictError:
            raise
        except Exception as e:
            logger.error(f"[StatusDAO] Error rolling back status for admission {admission_id}: {e}")
            return False

    def ensure_initial_status(self, admission_id: int, shift_start: datetime, admission_datetime: Optional[datetime], user_id: str = "SYSTEM"):
        """
        Обеспечивает наличие начального статуса для карты.
        Учитывает время поступления и преемственность с прошлыми сутками.
        """
        try:
            with self.db.remcard_transaction() as cursor:
                # 1. Проверяем, есть ли события вообще
                cursor.execute("SELECT count(*) as cnt FROM patient_status_events WHERE admission_id = ?", (admission_id,))
                total_cnt = cursor.fetchone()['cnt']
                
                # Минимально разрешенное время (время поступления)
                min_allowed = admission_datetime if admission_datetime else shift_start
                # Очищаем от микросекунд для сравнения в SQLite
                min_allowed_str = min_allowed.replace(microsecond=0).isoformat()
                shift_start_str = shift_start.replace(microsecond=0).isoformat()

                if total_cnt == 0:
                    # САМАЯ ПЕРВАЯ ЗАПИСЬ СТАТУСА для пациента в системе
                    # Таймлайн должен начинаться С МОМЕНТА ПОСТУПЛЕНИЯ
                    cursor.execute("""
                        INSERT INTO patient_status_events 
                        (admission_id, status, start_time, created_by, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (admission_id, PatientStatus.ACTIVE.value, min_allowed_str, user_id, min_allowed_str, min_allowed_str))
                    logger.info(f"[StatusDAO] Created first status for {admission_id} at {min_allowed_str}")
                    return

                # 2. Проверяем, покрыто ли начало текущей смены (shift_start)
                # Ищем событие, которое началось <= shift_start и (не закончилось ИЛИ закончилось >= shift_start)
                # Используем DATETIME() для надежного сравнения разных форматов SQLite
                cursor.execute("""
                    SELECT id FROM patient_status_events 
                    WHERE admission_id = ? 
                    AND DATETIME(start_time) <= DATETIME(?) 
                    AND (end_time IS NULL OR DATETIME(end_time) >= DATETIME(?))
                    LIMIT 1
                """, (admission_id, shift_start_str, shift_start_str))
                
                if cursor.fetchone():
                    return # Начало смены уже покрыто историей

                # 3. Если не покрыто, ищем последнее событие СТРОГО ДО начала смены
                cursor.execute("""
                    SELECT status, end_time FROM patient_status_events 
                    WHERE admission_id = ? AND DATETIME(start_time) < DATETIME(?) 
                    ORDER BY start_time DESC LIMIT 1
                """, (admission_id, shift_start_str))
                last_before = cursor.fetchone()
                
                if last_before:
                    # Проверяем, не существует ли уже записи на это точное время (мостик)
                    cursor.execute("SELECT 1 FROM patient_status_events WHERE admission_id = ? AND DATETIME(start_time) = DATETIME(?)", (admission_id, shift_start_str))
                    if not cursor.fetchone():
                        cursor.execute("""
                            INSERT INTO patient_status_events 
                            (admission_id, status, start_time, created_by, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (admission_id, last_before['status'], shift_start_str, user_id, shift_start_str, shift_start_str))
                        logger.info(f"[StatusDAO] Bridged status for {admission_id} at {shift_start_str}")
                else:
                    # Событий ДО начала смены нет. Это значит, что все существующие события
                    # начинаются ПОЗЖЕ shift_start (например, пациент поступил сегодня в 10:40).
                    # В этом случае НИКАКИХ мостиков на 08:00 строить нельзя.
                    pass

        except Exception as e:
            logger.error(f"[StatusDAO] Error ensuring initial status for {admission_id}: {e}")

    def update_event_bounds(
        self,
        event_id: int,
        new_start: datetime,
        new_end: Optional[datetime],
        new_reason: Optional[str] = None,
        expected_revision: Optional[int] = None,
    ) -> bool:
        """
        Обновляет границы события (и опционально комментарий) и каскадно правит соседние.
        Реализует строгую логику линейности: обновление происходит по ИНДЕКСУ в отсортированном списке событий.
        """
        now_str = datetime.now().isoformat()
        try:
            with self.db.remcard_transaction() as cursor:
                # 1. Получаем текущее событие
                cursor.execute("SELECT admission_id, COALESCE(revision, 0) AS revision FROM patient_status_events WHERE id = ?", (event_id,))
                curr_adm = cursor.fetchone()
                if not curr_adm: return False
                assert_revision_matches(curr_adm["revision"], expected_revision)
                admission_id = curr_adm['admission_id']

                # 2. Получаем ВСЕ события пациента в хронологическом порядке (с нужными полями)
                cursor.execute("SELECT id, start_time, end_time FROM patient_status_events WHERE admission_id = ? ORDER BY start_time ASC", (admission_id,))
                all_events = cursor.fetchall()
                
                # Находим индекс редактируемого события
                curr_idx = -1
                for i, ev in enumerate(all_events):
                    if ev['id'] == event_id:
                        curr_idx = i
                        break
                        
                if curr_idx == -1: return False
                
                # 3. ВАЛИДАЦИЯ ЛИНЕЙНОСТИ ВРЕМЕНИ НАЧАЛА
                # Начало текущего события не может "схлопнуть" предыдущее
                if curr_idx > 0:
                    prev_ev = all_events[curr_idx - 1]
                    prev_start = datetime.fromisoformat(prev_ev['start_time'].replace(' ', 'T'))
                    if new_start <= prev_start:
                        logger.warning(f"[StatusDAO] Linear validation failed: new_start {new_start} <= prev_start {prev_start}")
                        return False

                # ВАЛИДАЦИЯ ЛИНЕЙНОСТИ ВРЕМЕНИ КОНЦА (если передано из UI)
                # Конец текущего события не может "схлопнуть" следующее событие
                curr_end_raw = all_events[curr_idx]['end_time']
                
                if new_end and curr_idx < len(all_events) - 1:
                    next_ev = all_events[curr_idx + 1]
                    if next_ev['end_time']:
                        next_end = datetime.fromisoformat(next_ev['end_time'].replace(' ', 'T'))
                        if new_end >= next_end:
                            logger.warning(f"[StatusDAO] Linear validation failed: new_end {new_end} >= next_end {next_end}")
                            return False

                # 4. Обновляем само событие
                final_end = new_end.isoformat() if new_end else curr_end_raw
                
                if new_reason is not None:
                    cursor.execute(
                        """
                        UPDATE patient_status_events
                        SET start_time = ?,
                            end_time = ?,
                            reason_text = ?,
                            updated_at = ?,
                            revision = COALESCE(revision, 0) + 1
                        WHERE id = ?
                        """,
                        (new_start.isoformat(), final_end, new_reason, now_str, event_id)
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE patient_status_events
                        SET start_time = ?,
                            end_time = ?,
                            updated_at = ?,
                            revision = COALESCE(revision, 0) + 1
                        WHERE id = ?
                        """,
                        (new_start.isoformat(), final_end, now_str, event_id)
                    )

                # 5. КАСКАД НАЗАД
                if curr_idx > 0:
                    prev_id = all_events[curr_idx - 1]['id']
                    cursor.execute(
                        """
                        UPDATE patient_status_events
                        SET end_time = ?,
                            updated_at = ?,
                            revision = COALESCE(revision, 0) + 1
                        WHERE id = ?
                        """,
                        (new_start.isoformat(), now_str, prev_id)
                    )

                # 6. КАСКАД ВПЕРЕД
                if new_end and curr_idx < len(all_events) - 1:
                    next_id = all_events[curr_idx + 1]['id']
                    cursor.execute(
                        """
                        UPDATE patient_status_events
                        SET start_time = ?,
                            updated_at = ?,
                            revision = COALESCE(revision, 0) + 1
                        WHERE id = ?
                        """,
                        (new_end.isoformat(), now_str, next_id)
                    )
                
                logger.info(f"[StatusDAO] Event {event_id} bounds updated: {new_start} - {final_end}")
                return True
        except DataConflictError:
            raise
        except Exception as e:
            logger.error(f"[StatusDAO] Error updating event bounds: {e}", exc_info=True)
            return False

    def _parse_sqlite_dt(self, dt_str: Optional[str]) -> Optional[datetime]:
        if not dt_str: return None
        try:
            raw = dt_str.replace(' ', 'T')
            if '.' in raw:
                parts = raw.split('.')
                raw = parts[0] + '.' + parts[1][:6]
            return datetime.fromisoformat(raw)
        except Exception as e:
            logger.error(f"[StatusDAO] Error parsing date '{dt_str}': {e}")
            return None

    def _map_row(self, r) -> PatientStatusEventDTO:
        return PatientStatusEventDTO(
            id=r['id'],
            admission_id=r['admission_id'],
            status=PatientStatus(r['status']),
            reason_type=r['reason_type'],
            reason_text=r['reason_text'],
            start_time=self._parse_sqlite_dt(r['start_time']),
            end_time=self._parse_sqlite_dt(r['end_time']),
            created_by=r['created_by'],
            created_at=self._parse_sqlite_dt(r['created_at']),
            updated_at=self._parse_sqlite_dt(r['updated_at']),
            revision=int(r['revision'] if 'revision' in r.keys() and r['revision'] is not None else 0)
        )

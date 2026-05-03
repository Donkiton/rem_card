from typing import Any, Dict, List, Optional, Sequence
from datetime import datetime

from ..dto.remcard_dto import VitalDTO
from rem_card.app.logger import logger
from rem_card.services.concurrency import DataConflictError, DATA_CONFLICT_MESSAGE, assert_revision_matches
from .sync_cursor import is_cursor_newer, make_sync_cursor, normalize_sync_cursor


class VitalsDAO:
    def __init__(self, db_manager):
        self.db = db_manager

    def add_vital(self, dto: VitalDTO, expected_revision: Optional[int] = None):
        """Insert or update vitals row for the same minute (admission_id + minute)."""
        target_minute = dto.timestamp.strftime("%Y-%m-%d %H:%M")
        check_query = """
            SELECT id, COALESCE(revision, 0) AS revision FROM vitals
            WHERE admission_id = ?
              AND STRFTIME('%Y-%m-%d %H:%M', datetime) = ?
        """
        row = self.db.fetch_one_remcard(check_query, (dto.admission_id, target_minute))

        last_modified_by = dto.last_modified_by if dto.last_modified_by else "doctor"

        if row:
            old_revision = int(row["revision"] or 0)
            assert_revision_matches(old_revision, expected_revision)
            logger.debug(
                "Updating vitals for admission_id=%s at %s. Values: sys=%s, dia=%s, pulse=%s, temp=%s, spo2=%s, rr=%s, cvp=%s",
                dto.admission_id,
                target_minute,
                dto.sys,
                dto.dia,
                dto.pulse,
                dto.temp,
                dto.spo2,
                dto.rr,
                dto.cvp,
            )
            query = """
                UPDATE vitals
                SET sys = COALESCE(?, sys),
                    dia = COALESCE(?, dia),
                    pulse = COALESCE(?, pulse),
                    temp = COALESCE(?, temp),
                    spo2 = COALESCE(?, spo2),
                    rr = COALESCE(?, rr),
                    cvp = COALESCE(?, cvp),
                    last_modified_by = ?,
                    revision = COALESCE(revision, 0) + 1,
                    updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
                WHERE id = ?
            """
            cursor = self.db.execute_remcard(
                query,
                (
                    dto.sys,
                    dto.dia,
                    dto.pulse,
                    dto.temp,
                    dto.spo2,
                    dto.rr,
                    dto.cvp,
                    last_modified_by,
                    row["id"],
                ),
            )
            if cursor.rowcount != 1:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            dto.id = row["id"]
            dto.revision = old_revision + 1
        else:
            if expected_revision is not None:
                raise DataConflictError(DATA_CONFLICT_MESSAGE)
            logger.debug(
                "Inserting new vitals for admission_id=%s at %s. Values: sys=%s, dia=%s, pulse=%s, temp=%s, spo2=%s, rr=%s, cvp=%s",
                dto.admission_id,
                target_minute,
                dto.sys,
                dto.dia,
                dto.pulse,
                dto.temp,
                dto.spo2,
                dto.rr,
                dto.cvp,
            )
            query = """
                INSERT INTO vitals (admission_id, datetime, sys, dia, pulse, temp, spo2, rr, cvp, last_modified_by, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
            """
            cursor = self.db.execute_remcard(
                query,
                (
                    dto.admission_id,
                    dto.timestamp.isoformat(),
                    dto.sys,
                    dto.dia,
                    dto.pulse,
                    dto.temp,
                    dto.spo2,
                    dto.rr,
                    dto.cvp,
                    last_modified_by,
                ),
            )
            dto.id = cursor.lastrowid
            dto.revision = 0

    def get_vitals(self, admission_id: int, start: datetime, end: datetime) -> List[VitalDTO]:
        query = """
            SELECT id, admission_id, datetime, sys, dia, pulse, temp, spo2, rr, cvp, last_modified_by, updated_at, COALESCE(revision, 0) AS revision
            FROM vitals
            WHERE admission_id = ? AND datetime >= ? AND datetime <= ?
            ORDER BY datetime ASC
        """
        rows = self.db.fetch_all_remcard(query, (admission_id, start.isoformat(), end.isoformat()))
        return [
            VitalDTO(
                id=r["id"],
                admission_id=r["admission_id"],
                timestamp=datetime.fromisoformat(r["datetime"]),
                sys=r["sys"],
                dia=r["dia"],
                pulse=r["pulse"],
                temp=r["temp"],
                spo2=r["spo2"],
                rr=r["rr"],
                cvp=r["cvp"],
                last_modified_by=r["last_modified_by"],
                updated_at=r["updated_at"],
                revision=r["revision"],
            )
            for r in rows
        ]

    def fetch_updated_vitals(self, admission_id: int, last_sync_time):
        """Return changed vitals for polling and a new sync cursor."""
        last_sync_ts, last_sync_id = normalize_sync_cursor(last_sync_time)
        query = """
            SELECT *
            FROM vitals
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

        vitals = []
        new_sync_cursor = make_sync_cursor(last_sync_ts, last_sync_id)

        if rows:
            for r in rows:
                rd = dict(r)
                row_updated_at = str(rd.get("updated_at") or new_sync_cursor["updated_at"])
                row_id = int(rd.get("id") or 0)
                if is_cursor_newer(row_updated_at, row_id, new_sync_cursor["updated_at"], new_sync_cursor["id"]):
                    new_sync_cursor = make_sync_cursor(row_updated_at, row_id)
                vitals.append(
                    VitalDTO(
                        id=rd["id"],
                        admission_id=rd["admission_id"],
                        timestamp=datetime.fromisoformat(rd["datetime"]),
                        sys=rd["sys"],
                        dia=rd["dia"],
                        pulse=rd["pulse"],
                        temp=rd["temp"],
                        spo2=rd["spo2"],
                        rr=rd["rr"],
                        cvp=rd["cvp"],
                        last_modified_by=rd["last_modified_by"],
                        updated_at=rd["updated_at"],
                        revision=rd["revision"] if "revision" in rd.keys() else 0,
                    )
                )

        if isinstance(last_sync_time, dict):
            return vitals, new_sync_cursor
        return vitals, new_sync_cursor["updated_at"]

    def get_latest_vital_datetime(self, admission_id: int) -> Optional[datetime]:
        query = "SELECT MAX(datetime) as dt FROM vitals WHERE admission_id = ?"
        row = self.db.fetch_one_remcard(query, (admission_id,))
        return datetime.fromisoformat(row["dt"]) if row and row["dt"] else None

    def get_latest_vital(self, admission_id: int) -> Optional[VitalDTO]:
        query = """
            SELECT id, admission_id, datetime, sys, dia, pulse, temp, spo2, rr, cvp, last_modified_by, updated_at, COALESCE(revision, 0) AS revision
            FROM vitals
            WHERE admission_id = ?
              AND (sys IS NOT NULL OR dia IS NOT NULL OR pulse IS NOT NULL OR temp IS NOT NULL OR spo2 IS NOT NULL OR rr IS NOT NULL OR cvp IS NOT NULL)
            ORDER BY datetime DESC, id DESC
            LIMIT 1
        """
        r = self.db.fetch_one_remcard(query, (admission_id,))
        if not r:
            return None

        return VitalDTO(
            id=r["id"],
            admission_id=r["admission_id"],
            timestamp=datetime.fromisoformat(r["datetime"]),
            sys=r["sys"],
            dia=r["dia"],
            pulse=r["pulse"],
            temp=r["temp"],
            spo2=r["spo2"],
            rr=r["rr"],
            cvp=r["cvp"],
            last_modified_by=r["last_modified_by"],
            updated_at=r["updated_at"],
            revision=r["revision"],
        )

    def get_all_vital_dates(self, admission_id: int) -> List[datetime]:
        query = "SELECT DISTINCT date(datetime) as d FROM vitals WHERE admission_id = ? ORDER BY d ASC"
        rows = self.db.fetch_all_remcard(query, (admission_id,))
        dates = []
        for r in rows:
            if r["d"]:
                dt = datetime.strptime(r["d"], "%Y-%m-%d").replace(hour=12, minute=0)
                dates.append(dt)
        return dates

    def clear_vitals(self, admission_id: int, start: datetime, end: datetime):
        query = "DELETE FROM vitals WHERE admission_id = ? AND datetime >= ? AND datetime <= ?"
        self.db.execute_remcard(query, (admission_id, start.isoformat(), end.isoformat()))

    def delete_all_for_admission(self, admission_id: int):
        self.db.execute_remcard("DELETE FROM vitals WHERE admission_id = ?", (admission_id,))

    def delete_vital(self, vital_id: int, expected_revision: Optional[int] = None):
        query = "DELETE FROM vitals WHERE id = ?"
        params = [vital_id]
        if expected_revision is not None:
            query += " AND COALESCE(revision, 0) = ?"
            params.append(int(expected_revision))
        cursor = self.db.execute_remcard(query, tuple(params))
        if expected_revision is not None and cursor.rowcount != 1:
            raise DataConflictError(DATA_CONFLICT_MESSAGE)

    def get_vital_settings(self, admission_id: int, date: str) -> Optional[Dict[str, Any]]:
        query = """
            SELECT ad, pulse, temp, spo2, rr, cvp
            FROM vital_settings
            WHERE admission_id = ? AND date <= ?
            ORDER BY date DESC LIMIT 1
        """
        r = self.db.fetch_one_remcard(query, (admission_id, date))
        if r:
            logger.debug(
                "Fetched vital settings for admission_id=%s, date=%s: ad=%s, pulse=%s, temp=%s, spo2=%s, rr=%s, cvp=%s",
                admission_id,
                date,
                r["ad"],
                r["pulse"],
                r["temp"],
                r["spo2"],
                r["rr"],
                r["cvp"],
            )
            return dict(r)
        return None

    def save_vital_settings(self, admission_id: int, date: str, settings: Dict[str, Any]):
        logger.debug("Saving vital settings for admission_id=%s, date=%s: %s", admission_id, date, settings)
        query = """
            INSERT INTO vital_settings (admission_id, date, ad, pulse, temp, spo2, rr, cvp, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
            ON CONFLICT(admission_id, date) DO UPDATE SET
                ad = excluded.ad,
                pulse = excluded.pulse,
                temp = excluded.temp,
                spo2 = excluded.spo2,
                rr = excluded.rr,
                cvp = excluded.cvp,
                updated_at = excluded.updated_at
        """
        self.db.execute_remcard(
            query,
            (
                admission_id,
                date,
                settings.get("ad", 1),
                settings.get("pulse", 1),
                settings.get("temp", 1),
                settings.get("spo2", 1),
                settings.get("rr", 0),
                settings.get("cvp", 0),
            ),
        )

    def get_latest_vital_values(self, admission_id: int) -> Dict[str, Any]:
        """Return latest non-null vitals for one admission by a single query."""
        query = """
            SELECT
                (SELECT v.sys   FROM vitals v WHERE v.admission_id = ? AND v.sys  IS NOT NULL ORDER BY v.datetime DESC, v.id DESC LIMIT 1) AS sys,
                (SELECT v.dia   FROM vitals v WHERE v.admission_id = ? AND v.dia  IS NOT NULL ORDER BY v.datetime DESC, v.id DESC LIMIT 1) AS dia,
                (SELECT v.pulse FROM vitals v WHERE v.admission_id = ? AND v.pulse IS NOT NULL ORDER BY v.datetime DESC, v.id DESC LIMIT 1) AS pulse,
                (SELECT v.temp  FROM vitals v WHERE v.admission_id = ? AND v.temp IS NOT NULL ORDER BY v.datetime DESC, v.id DESC LIMIT 1) AS temp,
                (SELECT v.spo2  FROM vitals v WHERE v.admission_id = ? AND v.spo2 IS NOT NULL ORDER BY v.datetime DESC, v.id DESC LIMIT 1) AS spo2,
                (SELECT v.rr    FROM vitals v WHERE v.admission_id = ? AND v.rr   IS NOT NULL ORDER BY v.datetime DESC, v.id DESC LIMIT 1) AS rr,
                (SELECT v.cvp   FROM vitals v WHERE v.admission_id = ? AND v.cvp  IS NOT NULL ORDER BY v.datetime DESC, v.id DESC LIMIT 1) AS cvp
        """
        row = self.db.fetch_one_remcard(query, (admission_id,) * 7)
        if not row:
            return {
                "sys": None,
                "dia": None,
                "pulse": None,
                "temp": None,
                "spo2": None,
                "rr": None,
                "cvp": None,
            }
        return dict(row)

    def get_latest_vital_values_bulk(self, admission_ids: Sequence[int]) -> Dict[int, Dict[str, Any]]:
        ids = [int(adm_id) for adm_id in admission_ids if adm_id is not None]
        if not ids:
            return {}

        placeholders = ",".join(["(?)"] * len(ids))
        query = f"""
            WITH ids(admission_id) AS (VALUES {placeholders})
            SELECT
                ids.admission_id AS admission_id,
                (SELECT v.sys   FROM vitals v WHERE v.admission_id = ids.admission_id AND v.sys  IS NOT NULL ORDER BY v.datetime DESC, v.id DESC LIMIT 1) AS sys,
                (SELECT v.dia   FROM vitals v WHERE v.admission_id = ids.admission_id AND v.dia  IS NOT NULL ORDER BY v.datetime DESC, v.id DESC LIMIT 1) AS dia,
                (SELECT v.pulse FROM vitals v WHERE v.admission_id = ids.admission_id AND v.pulse IS NOT NULL ORDER BY v.datetime DESC, v.id DESC LIMIT 1) AS pulse,
                (SELECT v.temp  FROM vitals v WHERE v.admission_id = ids.admission_id AND v.temp IS NOT NULL ORDER BY v.datetime DESC, v.id DESC LIMIT 1) AS temp,
                (SELECT v.spo2  FROM vitals v WHERE v.admission_id = ids.admission_id AND v.spo2 IS NOT NULL ORDER BY v.datetime DESC, v.id DESC LIMIT 1) AS spo2,
                (SELECT v.rr    FROM vitals v WHERE v.admission_id = ids.admission_id AND v.rr   IS NOT NULL ORDER BY v.datetime DESC, v.id DESC LIMIT 1) AS rr,
                (SELECT v.cvp   FROM vitals v WHERE v.admission_id = ids.admission_id AND v.cvp  IS NOT NULL ORDER BY v.datetime DESC, v.id DESC LIMIT 1) AS cvp
            FROM ids
        """
        rows = self.db.fetch_all_remcard(query, tuple(ids))
        result: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            result[int(row["admission_id"])] = {
                "sys": row["sys"],
                "dia": row["dia"],
                "pulse": row["pulse"],
                "temp": row["temp"],
                "spo2": row["spo2"],
                "rr": row["rr"],
                "cvp": row["cvp"],
            }
        return result

    def get_vital_settings_bulk(self, admission_ids: Sequence[int], date: str) -> Dict[int, Dict[str, Any]]:
        ids = [int(adm_id) for adm_id in admission_ids if adm_id is not None]
        if not ids:
            return {}

        placeholders = ",".join(["(?)"] * len(ids))
        query = f"""
            WITH ids(admission_id) AS (VALUES {placeholders})
            SELECT
                ids.admission_id AS admission_id,
                (SELECT vs.ad    FROM vital_settings vs WHERE vs.admission_id = ids.admission_id AND vs.date <= ? ORDER BY vs.date DESC LIMIT 1) AS ad,
                (SELECT vs.pulse FROM vital_settings vs WHERE vs.admission_id = ids.admission_id AND vs.date <= ? ORDER BY vs.date DESC LIMIT 1) AS pulse,
                (SELECT vs.temp  FROM vital_settings vs WHERE vs.admission_id = ids.admission_id AND vs.date <= ? ORDER BY vs.date DESC LIMIT 1) AS temp,
                (SELECT vs.spo2  FROM vital_settings vs WHERE vs.admission_id = ids.admission_id AND vs.date <= ? ORDER BY vs.date DESC LIMIT 1) AS spo2,
                (SELECT vs.rr    FROM vital_settings vs WHERE vs.admission_id = ids.admission_id AND vs.date <= ? ORDER BY vs.date DESC LIMIT 1) AS rr,
                (SELECT vs.cvp   FROM vital_settings vs WHERE vs.admission_id = ids.admission_id AND vs.date <= ? ORDER BY vs.date DESC LIMIT 1) AS cvp
            FROM ids
        """
        params = tuple(ids) + (date, date, date, date, date, date)
        rows = self.db.fetch_all_remcard(query, params)
        result: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            result[int(row["admission_id"])] = {
                "ad": row["ad"],
                "pulse": row["pulse"],
                "temp": row["temp"],
                "spo2": row["spo2"],
                "rr": row["rr"],
                "cvp": row["cvp"],
            }
        return result

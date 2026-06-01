from typing import List, Optional
from datetime import datetime, timedelta
from ..dto.remcard_dto import FluidDTO
from rem_card.services.concurrency import DataConflictError, DATA_CONFLICT_MESSAGE
from .sync_cursor import is_cursor_newer, make_sync_cursor, normalize_sync_cursor

class FluidsDAO:
    def __init__(self, db_manager):
        self.db = db_manager

    def add_fluid(self, dto: FluidDTO):
        query = """
            INSERT INTO fluids (admission_id, datetime, iv_input, oral_input, food, urine, ng_output, drain_output, stool, other_output, last_modified_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
        """
        last_modified_by = dto.last_modified_by if dto.last_modified_by else 'doctor'
        res = self.db.execute_remcard(query, (
            dto.admission_id, dto.timestamp.isoformat(),
            dto.iv_input, dto.oral_input, dto.food, 
            dto.urine, dto.ng_output, dto.drain_output, dto.stool, dto.other_output,
            last_modified_by
        ))
        dto.id = res.lastrowid
        return dto.id

    def update_fluid(self, dto: FluidDTO, expected_revision: Optional[int] = None):
        last_modified_by = dto.last_modified_by if dto.last_modified_by else 'doctor'
        query = """
            UPDATE fluids SET 
                iv_input=?, oral_input=?, food=?, urine=?, ng_output=?, 
                drain_output=?, stool=?, other_output=?, last_modified_by=?, 
                revision=COALESCE(revision, 0) + 1,
                updated_at=STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
            WHERE id=?
        """
        params = [
            dto.iv_input, dto.oral_input, dto.food,
            dto.urine, dto.ng_output, dto.drain_output, dto.stool, dto.other_output,
            last_modified_by, dto.id,
        ]
        if expected_revision is not None:
            query += " AND COALESCE(revision, 0) = ?"
            params.append(int(expected_revision))
        cursor = self.db.execute_remcard(query, tuple(params))
        if expected_revision is not None and cursor.rowcount != 1:
            raise DataConflictError(DATA_CONFLICT_MESSAGE)

    def delete_fluid(self, fluid_id: int, expected_revision: Optional[int] = None):
        query = "DELETE FROM fluids WHERE id=?"
        params = [fluid_id]
        if expected_revision is not None:
            query += " AND COALESCE(revision, 0) = ?"
            params.append(int(expected_revision))
        cursor = self.db.execute_remcard(query, tuple(params))
        if expected_revision is not None and cursor.rowcount != 1:
            raise DataConflictError(DATA_CONFLICT_MESSAGE)

    def get_fluids(self, admission_id: int, start: datetime, end: datetime) -> List[FluidDTO]:
        query = "SELECT * FROM fluids WHERE admission_id = ? AND datetime >= ? AND datetime < ? ORDER BY datetime ASC"
        rows = self.db.fetch_all_remcard(query, (admission_id, start.isoformat(), end.isoformat()))
        return [FluidDTO(
            id=r['id'],
            admission_id=r['admission_id'],
            timestamp=datetime.fromisoformat(r['datetime']),
            iv_input=r['iv_input'],
            oral_input=r['oral_input'],
            food=r['food'],
            urine=r['urine'],
            ng_output=r['ng_output'],
            drain_output=r['drain_output'],
            stool=r['stool'],
            other_output=r['other_output'] if 'other_output' in r.keys() else 0.0,
            last_modified_by=r['last_modified_by'] if 'last_modified_by' in r.keys() else None,
            updated_at=r['updated_at'] if 'updated_at' in r.keys() else None,
            revision=r['revision'] if 'revision' in r.keys() else 0
        ) for r in rows]

    def get_latest_urine_before(self, admission_id: int, target: datetime) -> Optional[float]:
        row = self.db.fetch_one_remcard(
            """
            SELECT urine
            FROM fluids
            WHERE admission_id = ?
              AND urine IS NOT NULL
              AND urine > 0
              AND CAST(STRFTIME('%s', datetime) AS INTEGER) <= CAST(STRFTIME('%s', ?) AS INTEGER)
            ORDER BY CAST(STRFTIME('%s', datetime) AS INTEGER) DESC, id DESC
            LIMIT 1
            """,
            (int(admission_id), target.isoformat()),
        )
        return float(row["urine"]) if row and row["urine"] is not None else None

    def get_transfusion_followup_urine(
        self,
        admission_id: int,
        target: datetime,
        now: datetime,
        *,
        before_window_minutes: int = 10,
    ) -> Optional[float]:
        if now < target:
            return None
        start = (target - timedelta(minutes=before_window_minutes)).isoformat()
        end = now.isoformat()
        target_value = target.isoformat()
        row = self.db.fetch_one_remcard(
            """
            SELECT urine
            FROM fluids
            WHERE admission_id = ?
              AND urine IS NOT NULL
              AND urine > 0
              AND CAST(STRFTIME('%s', datetime) AS INTEGER) >= CAST(STRFTIME('%s', ?) AS INTEGER)
              AND CAST(STRFTIME('%s', datetime) AS INTEGER) <= CAST(STRFTIME('%s', ?) AS INTEGER)
            ORDER BY ABS(
                         CAST(STRFTIME('%s', datetime) AS INTEGER)
                         - CAST(STRFTIME('%s', ?) AS INTEGER)
                     ) ASC,
                     CAST(STRFTIME('%s', datetime) AS INTEGER) DESC,
                     id DESC
            LIMIT 1
            """,
            (int(admission_id), start, end, target_value),
        )
        return float(row["urine"]) if row and row["urine"] is not None else None

    def fetch_updated_fluids(self, admission_id: int, last_sync_time):
        """Возвращает измененные жидкости для поллинга и новое время синхронизации."""
        last_sync_ts, last_sync_id = normalize_sync_cursor(last_sync_time)
        query = """
            SELECT *
            FROM fluids
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
        
        fluids = []
        new_sync_cursor = make_sync_cursor(last_sync_ts, last_sync_id)
        
        if rows:
            for r in rows:
                rd = dict(r)
                row_updated_at = str(rd.get('updated_at') or new_sync_cursor["updated_at"])
                row_id = int(rd.get('id') or 0)
                if is_cursor_newer(row_updated_at, row_id, new_sync_cursor["updated_at"], new_sync_cursor["id"]):
                    new_sync_cursor = make_sync_cursor(row_updated_at, row_id)
                fluids.append(FluidDTO(
                    id=rd['id'],
                    admission_id=rd['admission_id'],
                    timestamp=datetime.fromisoformat(rd['datetime']),
                    iv_input=rd['iv_input'],
                    oral_input=rd['oral_input'],
                    food=rd['food'],
                    urine=rd['urine'],
                    ng_output=rd['ng_output'],
                    drain_output=rd['drain_output'],
                    stool=rd['stool'],
                    other_output=rd['other_output'] if 'other_output' in rd.keys() else 0.0,
                    last_modified_by=rd['last_modified_by'] if 'last_modified_by' in rd.keys() else None,
                    updated_at=rd['updated_at'] if 'updated_at' in rd.keys() else None,
                    revision=rd['revision'] if 'revision' in rd.keys() else 0
                ))

        if isinstance(last_sync_time, dict):
            return fluids, new_sync_cursor
        return fluids, new_sync_cursor["updated_at"]

    def delete_all_for_admission(self, admission_id: int):
        self.db.execute_remcard("DELETE FROM fluids WHERE admission_id = ?", (admission_id,))

    def get_all_dates(self, admission_id: int) -> List[datetime]:
        """Возвращает все уникальные даты для записей о жидкостях пациента."""
        query = "SELECT DISTINCT datetime as dt FROM fluids WHERE admission_id = ? ORDER BY datetime ASC"
        rows = self.db.fetch_all_remcard(query, (admission_id,))
        dates = []
        for r in rows:
            if r['dt']:
                dates.append(datetime.fromisoformat(str(r['dt']).replace(" ", "T")))
        return dates

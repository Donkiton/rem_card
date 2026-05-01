from typing import List
from datetime import datetime
from ..dto.remcard_dto import FluidDTO
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

    def update_fluid(self, dto: FluidDTO):
        query = """
            UPDATE fluids SET 
                iv_input=?, oral_input=?, food=?, urine=?, ng_output=?, 
                drain_output=?, stool=?, other_output=?, last_modified_by=?, 
                updated_at=STRFTIME('%Y-%m-%d %H:%M:%f', 'now')
            WHERE id=?
        """
        last_modified_by = dto.last_modified_by if dto.last_modified_by else 'doctor'
        self.db.execute_remcard(query, (
            dto.iv_input, dto.oral_input, dto.food, 
            dto.urine, dto.ng_output, dto.drain_output, dto.stool, dto.other_output,
            last_modified_by, dto.id
        ))

    def delete_fluid(self, fluid_id: int):
        self.db.execute_remcard("DELETE FROM fluids WHERE id=?", (fluid_id,))

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
            updated_at=r['updated_at'] if 'updated_at' in r.keys() else None
        ) for r in rows]

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
                    updated_at=rd['updated_at'] if 'updated_at' in rd.keys() else None
                ))

        if isinstance(last_sync_time, dict):
            return fluids, new_sync_cursor
        return fluids, new_sync_cursor["updated_at"]

    def delete_all_for_admission(self, admission_id: int):
        self.db.execute_remcard("DELETE FROM fluids WHERE admission_id = ?", (admission_id,))

    def get_all_dates(self, admission_id: int) -> List[datetime]:
        """Возвращает все уникальные даты для записей о жидкостях пациента."""
        query = "SELECT DISTINCT date(datetime) as d FROM fluids WHERE admission_id = ? ORDER BY d ASC"
        rows = self.db.fetch_all_remcard(query, (admission_id,))
        dates = []
        for r in rows:
            if r['d']:
                dt = datetime.strptime(r['d'], '%Y-%m-%d').replace(hour=12, minute=0)
                dates.append(dt)
        return dates

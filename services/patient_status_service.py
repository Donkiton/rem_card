from typing import Dict, List, Tuple, Optional, Callable, Any, Sequence
from datetime import datetime
from ..data.dao.patient_status_dao import PatientStatusDAO
from ..data.dto.remcard_dto import PatientStatus, PatientStatusEventDTO
from .ventilation_service import VentilationService
from rem_card.app.logger import logger

class PatientStatusService:
    def __init__(
        self,
        status_dao: PatientStatusDAO,
        *,
        data_service=None,
        ventilation_service: Optional[VentilationService] = None,
        ventilation_dao=None,
    ):
        self.status_dao = status_dao
        self.data_service = data_service
        if ventilation_service is not None:
            self.ventilation_service = ventilation_service
        elif ventilation_dao is not None:
            self.ventilation_service = VentilationService(ventilation_dao, data_service=data_service)
        else:
            self.ventilation_service = None

    def change_status(self, admission_id: int, new_status: PatientStatus, 
                      reason_type: Optional[str] = None, reason_text: Optional[str] = None, 
                      user_id: Optional[str] = None) -> bool:
        """РЎРјРµРЅР° СЃС‚Р°С‚СѓСЃР° РїР°С†РёРµРЅС‚Р°."""
        ok = self.status_dao.change_status(admission_id, new_status, reason_type, reason_text, user_id)
        if ok:
            self._sync_ventilation_for_admission(admission_id)
        return ok

    def change_status_with_outcome_details(
        self,
        admission_id: int,
        new_status: PatientStatus,
        event_time: datetime,
        reason_type: Optional[str] = None,
        reason_text: Optional[str] = None,
        user_id: Optional[str] = None,
        admission_details: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Смена финального статуса вместе с записью структурированных деталей исхода."""
        ok = self.status_dao.change_status_with_outcome_details(
            admission_id,
            new_status,
            event_time,
            reason_type,
            reason_text,
            user_id,
            admission_details,
        )
        if ok:
            self._sync_ventilation_for_admission(admission_id)
        return ok

    def get_admission_outcome_context(self, admission_id: int) -> Dict[str, Any]:
        return self.status_dao.get_admission_outcome_context(admission_id)

    def rollback_last_status(self, admission_id: int) -> bool:
        """РћС‚РєР°С‚ РїРѕСЃР»РµРґРЅРµРіРѕ РёР·РјРµРЅРµРЅРёСЏ СЃС‚Р°С‚СѓСЃР°."""
        ok = self.status_dao.rollback_last_status(admission_id)
        if ok:
            self._sync_ventilation_for_admission(admission_id)
        return ok

    def ensure_initial_status(self, admission_id: int, shift_start: datetime, admission_datetime: Optional[datetime], user_id: str = "SYSTEM"):
        """РћР±РµСЃРїРµС‡РёРІР°РµС‚ РЅР°Р»РёС‡РёРµ РЅР°С‡Р°Р»СЊРЅРѕРіРѕ СЃС‚Р°С‚СѓСЃР°."""
        return self.status_dao.ensure_initial_status(admission_id, shift_start, admission_datetime, user_id)

    def get_current_status(self, admission_id: int) -> Optional[PatientStatusEventDTO]:
        """Р’РѕР·РІСЂР°С‰Р°РµС‚ С‚РµРєСѓС‰РёР№ Р°РєС‚РёРІРЅС‹Р№ СЃС‚Р°С‚СѓСЃ РїР°С†РёРµРЅС‚Р°."""
        return self.status_dao.get_active_event(admission_id)

    def get_current_statuses_bulk(self, admission_ids: Sequence[int]) -> Dict[int, Optional[PatientStatusEventDTO]]:
        ids = [int(adm_id) for adm_id in admission_ids if adm_id is not None]
        if not ids:
            return {}

        placeholders = ",".join("?" for _ in ids)
        query = f"""
            SELECT *
            FROM patient_status_events
            WHERE end_time IS NULL
              AND admission_id IN ({placeholders})
        """
        rows = self.status_dao.db.fetch_all_remcard(query, tuple(ids))
        result: Dict[int, Optional[PatientStatusEventDTO]] = {adm_id: None for adm_id in ids}
        for row in rows:
            mapped = self.status_dao._map_row(row)
            result[int(mapped.admission_id)] = mapped
        return result

    def is_active_at(self, admission_id: int, timestamp: datetime) -> bool:
        """
        РџСЂРѕРІРµСЂСЏРµС‚, Р±С‹Р» Р»Рё РїР°С†РёРµРЅС‚ РІ СЃС‚Р°С‚СѓСЃРµ ACTIVE РІ СѓРєР°Р·Р°РЅРЅС‹Р№ РјРѕРјРµРЅС‚ РІСЂРµРјРµРЅРё.
        """
        event = self.get_event_at(admission_id, timestamp)
        # Р›РћР“ Р”Р›РЇ РћРўР›РђР”РљР (Р±СѓРґРµС‚ РІРёРґРµРЅ РІ С‚РµСЂРјРёРЅР°Р»Рµ РїСЂРё Р·Р°РїСѓСЃРєРµ С‡РµСЂРµР· -m)
        logger.debug(f"[StatusService] Checking status at {timestamp.strftime('%H:%M:%S')}. Found event: {event.status.value if event else 'None'}")
        
        if event:
            return event.status == PatientStatus.ACTIVE
        return False

    def get_event_at(self, admission_id: int, timestamp: datetime) -> Optional[PatientStatusEventDTO]:
        """
        Р’РѕР·РІСЂР°С‰Р°РµС‚ СЃРѕР±С‹С‚РёРµ, РєРѕС‚РѕСЂРѕРµ РїРѕРєСЂС‹РІР°РµС‚ СѓРєР°Р·Р°РЅРЅС‹Р№ РјРѕРјРµРЅС‚ РІСЂРµРјРµРЅРё.
        РћР±СЂРµР·Р°РµС‚ СЃРµРєСѓРЅРґС‹ Рё РјРёРєСЂРѕСЃРµРєСѓРЅРґС‹ РґР»СЏ СЃСЂР°РІРЅРµРЅРёСЏ РјРёРЅСѓС‚ РІ РјРёРЅСѓС‚Сѓ,
        С‡С‚РѕР±С‹ РІСЂР°С‡ РјРѕРі РІРІРѕРґРёС‚СЊ РґР°РЅРЅС‹Рµ СЂРѕРІРЅРѕ РЅР° РіСЂР°РЅРёС†Рµ СЃРѕР±С‹С‚РёР№ (РЅР°РїСЂРёРјРµСЂ, 15:04).
        """
        query = """
            SELECT * FROM patient_status_events 
            WHERE admission_id = ? 
            ORDER BY start_time DESC
        """
        rows = self.status_dao.db.fetch_all_remcard(query, (admission_id,))
        
        # РћР±СЂРµР·Р°РµРј СЃРµРєСѓРЅРґС‹ Рё РјРёРєСЂРѕСЃРµРєСѓРЅРґС‹ Сѓ РёСЃРєРѕРјРѕРіРѕ РІСЂРµРјРµРЅРё
        target_dt = timestamp.replace(second=0, microsecond=0)
        
        found_event = None
        candidates = []
        
        for r in rows:
            event = self.status_dao._map_row(r)
            # РћР±СЂРµР·Р°РµРј СЃРµРєСѓРЅРґС‹ Рё РјРёРєСЂРѕСЃРµРєСѓРЅРґС‹ Сѓ РіСЂР°РЅРёС† СЃРѕР±С‹С‚РёСЏ
            start_dt = event.start_time.replace(second=0, microsecond=0)
            end_dt = event.end_time.replace(second=0, microsecond=0) if event.end_time else None
            
            # РЎРѕР±РёСЂР°РµРј РІСЃРµ СЃРѕР±С‹С‚РёСЏ, РєРѕС‚РѕСЂС‹Рµ РЅР°С‡Р°Р»РёСЃСЊ <= target_dt Рё Р·Р°РєРѕРЅС‡РёР»РёСЃСЊ >= target_dt
            if start_dt <= target_dt:
                if end_dt is None or end_dt >= target_dt:
                    candidates.append(event)
        
        if candidates:
            # Р•СЃР»Рё РЅР° РѕРґРЅСѓ РјРёРЅСѓС‚Сѓ (СЃС‚С‹Рє) РїСЂРµС‚РµРЅРґСѓСЋС‚ РґРІР° СЃРѕР±С‹С‚РёСЏ, РїСЂРёРѕСЂРёС‚РµС‚ РѕС‚РґР°РµС‚СЃСЏ ACTIVE,
            # С‡С‚РѕР±С‹ РІСЂР°С‡ РјРѕРі РІРЅРµСЃС‚Рё РїРѕРєР°Р·Р°С‚РµР»Рё СЂРѕРІРЅРѕ РІ РјРёРЅСѓС‚Сѓ СѓС…РѕРґР° РёР»Рё РїСЂРёР±С‹С‚РёСЏ.
            active_candidate = next((c for c in candidates if c.status == PatientStatus.ACTIVE), None)
            if active_candidate:
                found_event = active_candidate
            else:
                found_event = candidates[0] # Р‘РµСЂРµРј СЃР°РјРѕРµ СЃРІРµР¶РµРµ, РµСЃР»Рё РЅРµС‚ ACTIVE

        logger.debug(f"[StatusService] get_event_at({target_dt.strftime('%H:%M')}): Found {found_event.status.value if found_event else 'None'}")
        return found_event

    def get_next_active_event_start(self, admission_id: int, after_time: datetime) -> Optional[datetime]:
        """РќР°С…РѕРґРёС‚ РЅР°С‡Р°Р»Рѕ Р±Р»РёР¶Р°Р№С€РµРіРѕ СЃР»РµРґСѓСЋС‰РµРіРѕ СЃРѕР±С‹С‚РёСЏ ACTIVE РїРѕСЃР»Рµ СѓРєР°Р·Р°РЅРЅРѕРіРѕ РІСЂРµРјРµРЅРё."""
        ts_str = after_time.isoformat().replace('T', ' ')
        query = """
            SELECT start_time FROM patient_status_events 
            WHERE admission_id = ? 
            AND status = ?
            AND start_time > ?
            ORDER BY start_time ASC
            LIMIT 1
        """
        row = self.status_dao.db.fetch_one_remcard(query, (admission_id, PatientStatus.ACTIVE.value, ts_str))
        if not row:
            row = self.status_dao.db.fetch_one_remcard(query, (admission_id, PatientStatus.ACTIVE.value, after_time.isoformat()))
            
        if row:
            return datetime.fromisoformat(row['start_time'].replace(' ', 'T'))
        return None

    def get_active_intervals(self, admission_id: int, start_limit: datetime, end_limit: datetime) -> List[Tuple[datetime, datetime]]:
        """
        Р’РѕР·РІСЂР°С‰Р°РµС‚ СЃРїРёСЃРѕРє РёРЅС‚РµСЂРІР°Р»РѕРІ (start, end) РґР»СЏ СЃС‚Р°С‚СѓСЃР° ACTIVE РІ Р·Р°РґР°РЅРЅРѕРј РґРёР°РїР°Р·РѕРЅРµ РІСЂРµРјРµРЅРё.
        РСЃРїРѕР»СЊР·СѓРµС‚СЃСЏ РґР»СЏ СЂР°СЃС‡С‘С‚Р° РёРЅС„СѓР·РёР№ Рё Р±Р°Р»Р°РЅСЃР°.
        """
        query = """
            SELECT *
            FROM patient_status_events
            WHERE admission_id = ?
              AND status = ?
              AND datetime(start_time) <= datetime(?)
              AND (end_time IS NULL OR datetime(end_time) >= datetime(?))
            ORDER BY start_time ASC
        """
        rows = self.status_dao.db.fetch_all_remcard(
            query,
            (
                admission_id,
                PatientStatus.ACTIVE.value,
                end_limit.isoformat(),
                start_limit.isoformat(),
            ),
        )

        active_intervals = []
        for row in rows:
            ev = self.status_dao._map_row(row)
            int_start = ev.start_time.replace(second=0, microsecond=0)
            int_end = ev.end_time.replace(second=0, microsecond=0) if ev.end_time else end_limit

            overlap_start = max(int_start, start_limit)
            overlap_end = min(int_end, end_limit)
            if overlap_start <= overlap_end:
                active_intervals.append((overlap_start, overlap_end))

        return active_intervals

    def get_events(self, admission_id: int) -> List[PatientStatusEventDTO]:
        """Р’РѕР·РІСЂР°С‰Р°РµС‚ РёСЃС‚РѕСЂРёСЋ СЃРѕР±С‹С‚РёР№."""
        return self.status_dao.get_events(admission_id)

    def get_events_in_range(self, admission_id: int, start: datetime, end: datetime) -> List[PatientStatusEventDTO]:
        """Р’РѕР·РІСЂР°С‰Р°РµС‚ СЃРѕР±С‹С‚РёСЏ, РїРµСЂРµСЃРµРєР°СЋС‰РёРµСЃСЏ СЃ Р·Р°РґР°РЅРЅС‹Рј РёРЅС‚РµСЂРІР°Р»РѕРј РІСЂРµРјРµРЅРё."""
        all_events = self.get_events(admission_id)
        filtered = []
        
        # РќРѕСЂРјР°Р»РёР·СѓРµРј РіСЂР°РЅРёС†С‹ РґР»СЏ СЃСЂР°РІРЅРµРЅРёСЏ
        range_start = start.replace(microsecond=0)
        range_end = end.replace(microsecond=0)
        
        for ev in all_events:
            # РќРѕСЂРјР°Р»РёР·СѓРµРј РіСЂР°РЅРёС†С‹ СЃРѕР±С‹С‚РёСЏ
            ev_start = ev.start_time.replace(microsecond=0)
            ev_end = ev.end_time.replace(microsecond=0) if ev.end_time else None
            
            # РџРµСЂРµСЃРµС‡РµРЅРёРµ РёРЅС‚РµСЂРІР°Р»РѕРІ [ev.start, ev.end] Рё [start, end]
            # ev.start < end AND (ev.end IS NULL OR ev.end > start)
            if ev_start < range_end and (ev_end is None or ev_end > range_start):
                filtered.append(ev)
        return filtered

    def update_event_bounds(self, event_id: int, new_start: datetime, new_end: Optional[datetime], new_reason: Optional[str] = None) -> bool:
        """РћР±РЅРѕРІР»РµРЅРёРµ РіСЂР°РЅРёС† СЃРѕР±С‹С‚РёСЏ С‡РµСЂРµР· DAO."""
        event = self.status_dao.get_event_by_id(event_id)
        ok = self.status_dao.update_event_bounds(event_id, new_start, new_end, new_reason)
        if ok and event:
            self._sync_ventilation_for_admission(event.admission_id)
        return ok

    def _sync_ventilation_for_admission(self, admission_id: int):
        if not self.ventilation_service:
            return
        try:
            current = self.status_dao.get_active_event(admission_id)
            if not current:
                self.ventilation_service.sync_outcome_status(
                    admission_id,
                    status_value=None,
                    status_time=None,
                )
                return
            self.ventilation_service.sync_outcome_status(
                admission_id,
                status_value=current.status.value,
                status_time=current.start_time,
            )
        except Exception as exc:
            logger.warning(
                "[StatusService] Failed to synchronize ventilation outcome for admission_id=%s: %s",
                admission_id,
                exc,
            )

    # --- Async Write Orchestration ---
    def enqueue_write(
        self,
        description: str,
        operation: Callable[[], Any],
        on_success: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ):
        if self.data_service:
            self.data_service.enqueue_write(
                description=description,
                operation=operation,
                on_success=on_success,
                on_error=on_error,
            )
            return

        try:
            result = operation()
            if on_success:
                on_success(result)
        except Exception as exc:
            if on_error:
                on_error(exc)
            else:
                raise

    def enqueue_change_status(
        self,
        admission_id: int,
        new_status: PatientStatus,
        reason_type: Optional[str] = None,
        reason_text: Optional[str] = None,
        user_id: Optional[str] = None,
        on_success: Optional[Callable[[bool], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ):
        self.enqueue_write(
            description=f"status_change:{admission_id}:{new_status.value}",
            operation=lambda: self.change_status(admission_id, new_status, reason_type, reason_text, user_id),
            on_success=on_success,
            on_error=on_error,
        )

    def enqueue_change_status_with_outcome_details(
        self,
        admission_id: int,
        new_status: PatientStatus,
        event_time: datetime,
        reason_type: Optional[str] = None,
        reason_text: Optional[str] = None,
        user_id: Optional[str] = None,
        admission_details: Optional[Dict[str, Any]] = None,
        on_success: Optional[Callable[[bool], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ):
        self.enqueue_write(
            description=f"status_outcome:{admission_id}:{new_status.value}",
            operation=lambda: self.change_status_with_outcome_details(
                admission_id,
                new_status,
                event_time,
                reason_type,
                reason_text,
                user_id,
                admission_details,
            ),
            on_success=on_success,
            on_error=on_error,
        )

    def enqueue_rollback_last_status(
        self,
        admission_id: int,
        on_success: Optional[Callable[[bool], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ):
        self.enqueue_write(
            description=f"status_rollback:{admission_id}",
            operation=lambda: self.rollback_last_status(admission_id),
            on_success=on_success,
            on_error=on_error,
        )

    def enqueue_update_event_bounds(
        self,
        event_id: int,
        new_start: datetime,
        new_end: Optional[datetime],
        new_reason: Optional[str] = None,
        on_success: Optional[Callable[[bool], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ):
        self.enqueue_write(
            description=f"status_update_bounds:{event_id}",
            operation=lambda: self.update_event_bounds(event_id, new_start, new_end, new_reason),
            on_success=on_success,
            on_error=on_error,
        )

    def get_non_active_intervals_longer_than(self, admission_id: int, start_limit: datetime, end_limit: datetime, min_minutes: int = 60) -> List[Tuple[datetime, datetime]]:
        """
        Р’РѕР·РІСЂР°С‰Р°РµС‚ РёРЅС‚РµСЂРІР°Р»С‹ РќР•-ACTIVE (OUT, OR), РґР»РёС‚РµР»СЊРЅРѕСЃС‚СЊ РєРѕС‚РѕСЂС‹С… >= min_minutes.
        РСЃРїРѕР»СЊР·СѓРµС‚СЃСЏ РґР»СЏ РѕС‚СЂРёСЃРѕРІРєРё СЂР°Р·СЂС‹РІРѕРІ РЅР° РіСЂР°С„РёРєР°С….
        """
        events = self.status_dao.get_events(admission_id)
        gaps = []
        
        for ev in events:
            if ev.status == PatientStatus.ACTIVE:
                continue
                
            int_start = ev.start_time
            int_end = ev.end_time if ev.end_time else datetime.now()
            
            overlap_start = max(int_start, start_limit)
            overlap_end = min(int_end, end_limit)
            
            if overlap_start < overlap_end:
                duration = (overlap_end - overlap_start).total_seconds() / 60
                if duration >= min_minutes:
                    gaps.append((overlap_start, overlap_end))
                    
        return gaps



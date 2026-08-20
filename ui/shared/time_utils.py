from datetime import datetime, timedelta
from PySide6.QtCore import QDateTime

class TimeClamper:
    """Логика ограничения времени рамками смены и госпитализации."""
    
    @staticmethod
    def clamp_to_bounds(new_dt: QDateTime, s_start: datetime, s_end: datetime, patient=None) -> datetime:
        # Преобразование QDateTime в python datetime
        py_dt = datetime(
            new_dt.date().year(), new_dt.date().month(), new_dt.date().day(),
            new_dt.time().hour(), new_dt.time().minute(), new_dt.time().second()
        )
        
        target_dt = py_dt

        # 1. Ограничение рамками смены
        if py_dt < s_start:
            target_dt = s_start
        elif py_dt >= s_end:
            target_dt = s_end - timedelta(seconds=1)

        # 2. Ограничение рамками госпитализации
        if patient:
            if patient.admission_datetime and target_dt < patient.admission_datetime:
                target_dt = patient.admission_datetime
            
            # Учитываем как transfer_datetime так и death_datetime (если они есть)
            # В DTO они часто объединяются в transfer_datetime
            if patient.transfer_datetime and target_dt > patient.transfer_datetime:
                target_dt = patient.transfer_datetime

        return target_dt

    @staticmethod
    def get_next_full_hour(current_dt: datetime) -> datetime:
        """Расчет следующего целого часа для автоподстановки."""
        if current_dt.minute == 0 and current_dt.second == 0:
            return current_dt + timedelta(hours=1)
        return current_dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

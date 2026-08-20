from .patient_dao import PatientDAO
from .vitals_dao import VitalsDAO
from .fluids_dao import FluidsDAO
from .orders_dao import OrdersDAO
from .ventilation_dao import VentilationDAO
from .diet_dao import DietPlanDAO, DietTemplateDAO, OralIntakeDAO

# Оставляем этот файл для обратной совместимости, пока не обновим все импорты
__all__ = [
    'PatientDAO',
    'VitalsDAO',
    'FluidsDAO',
    'OrdersDAO',
    'VentilationDAO',
    'DietTemplateDAO',
    'DietPlanDAO',
    'OralIntakeDAO',
]

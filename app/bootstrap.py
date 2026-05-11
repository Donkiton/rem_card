import os
from typing import TYPE_CHECKING

from rem_card.app.logger import logger
from rem_card.app.paths import (
    BAZA_DIR,
    JOURNAL_DB_PATH,
    NETWORK_ROOT,
    REMCARD_DB_PATH,
    ensure_directories,
)

if TYPE_CHECKING:
    from rem_card.data.dao.db_manager import DatabaseManager


class Container:
    def __init__(self, db_manager: "DatabaseManager"):
        from rem_card.data.dao.patient_status_dao import PatientStatusDAO
        from rem_card.data.dao.remcard_dao import (
            FluidsDAO,
            OrdersDAO,
            PatientDAO,
            VentilationDAO,
            VitalsDAO,
        )
        from rem_card.services.data_service import DataService
        from rem_card.services.patient_status_service import PatientStatusService
        from rem_card.services.read_coordinator import ReadCoordinator
        from rem_card.services.remcard_service import PatientService, RemCardService

        self.db_manager = db_manager
        self.data_service = DataService(db_manager)
        if hasattr(self.db_manager, "set_write_queue_idle_probe"):
            self.db_manager.set_write_queue_idle_probe(self.data_service.is_write_queue_idle)

        # DAOs
        self.patient_dao = PatientDAO(db_manager)
        self.vitals_dao = VitalsDAO(db_manager)
        self.fluids_dao = FluidsDAO(db_manager)
        self.orders_dao = OrdersDAO(db_manager)
        self.ventilation_dao = VentilationDAO(db_manager)
        self.status_dao = PatientStatusDAO(db_manager)

        # Services
        self.patient_status_service = PatientStatusService(
            self.status_dao,
            data_service=self.data_service,
            ventilation_dao=self.ventilation_dao,
        )
        self.patient_service = PatientService(self.patient_dao, data_service=self.data_service)
        self.remcard_service = RemCardService(
            self.vitals_dao,
            self.fluids_dao,
            self.orders_dao,
            self.ventilation_dao,
            self.patient_dao,
            status_service=self.patient_status_service,
            data_service=self.data_service,
        )
        # Прокидываем сервис статусов в основной фасад (RemCardService)
        self.remcard_service.status_service = self.patient_status_service
        self.data_service.add_poll_maintenance_task(self.remcard_service.maybe_release_due_outcome_beds)
        self.read_coordinator = ReadCoordinator(self.remcard_service)
        self.remcard_service.read_coordinator = self.read_coordinator
        self.remcard_service.read_mode = "live"
        self.remcard_service.source_db_path = "live"


def bootstrap() -> Container:
    from rem_card.data.dao.db_manager import DatabaseManager

    logger.info("Bootstrapping container...")

    ensure_directories()

    logger.info(f"[NETWORK ROOT] {NETWORK_ROOT}")
    logger.info(f"[BAZA PATH] {BAZA_DIR}")
    logger.info(f"[DB PATH] rao_journal.db -> {JOURNAL_DB_PATH}")
    logger.info(f"[DB PATH] unified_runtime.db -> {REMCARD_DB_PATH}")

    if os.environ.get("REMCARD_BOOTSTRAP_DEBUG") == "1":
        print(f"[NETWORK ROOT] {NETWORK_ROOT}")
        print(f"[BAZA PATH] {BAZA_DIR}")
        print(f"[DB PATH] rao_journal.db -> {JOURNAL_DB_PATH}")
        print(f"[DB PATH] unified_runtime.db -> {REMCARD_DB_PATH}")

    db_manager = DatabaseManager(JOURNAL_DB_PATH, REMCARD_DB_PATH)
    return Container(db_manager)

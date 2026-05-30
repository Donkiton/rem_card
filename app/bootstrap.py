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
    def __init__(self, db_manager: "DatabaseManager", role: str | None = None):
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
        self.runtime_context = getattr(db_manager, "runtime_context", None)
        self.runtime_mode = getattr(self.runtime_context, "mode", "network")
        self.data_service = DataService(db_manager)
        self.data_service.set_runtime_role(role)
        if hasattr(self.db_manager, "set_write_queue_idle_probe"):
            self.db_manager.set_write_queue_idle_probe(self.data_service.is_write_queue_idle)
        self.emergency_standby_scheduler = self._create_emergency_standby_scheduler(role)

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

    def _create_emergency_standby_scheduler(self, role: str | None):
        from rem_card.app.emergency_standby_scheduler import create_emergency_standby_scheduler_for_runtime

        runtime_context = getattr(self.db_manager, "runtime_context", None)
        mode = getattr(runtime_context, "mode", "network")
        scheduler = create_emergency_standby_scheduler_for_runtime(
            role=role,
            mode=mode,
            is_write_queue_idle=self.data_service.is_write_queue_idle,
            is_shutdown=lambda: bool(
                getattr(self.data_service, "_shutting_down", False)
                or getattr(self.db_manager, "_closed", False)
                or getattr(self.db_manager, "_closing", False)
            ),
        )
        if scheduler is None:
            return None
        self.data_service.set_emergency_standby_scheduler(scheduler)
        if scheduler.start():
            scheduler.request_refresh("startup")
            logger.info("Emergency standby scheduler started for role=%s mode=%s", role, mode)
        return scheduler


def bootstrap(role: str | None = None, runtime_context=None) -> Container:
    from rem_card.data.dao.db_manager import DatabaseManager

    logger.info("Bootstrapping container...")

    runtime_mode = getattr(runtime_context, "mode", "network") if runtime_context is not None else "network"
    if runtime_context is None:
        ensure_directories()
        medical_db_path = JOURNAL_DB_PATH
        remcard_db_path = REMCARD_DB_PATH
        logger.info(f"[NETWORK ROOT] {NETWORK_ROOT}")
        logger.info(f"[BAZA PATH] {BAZA_DIR}")
        logger.info(f"[DB PATH] rao_journal.db -> {JOURNAL_DB_PATH}")
        logger.info(f"[DB PATH] unified_runtime.db -> {REMCARD_DB_PATH}")
    else:
        medical_db_path = runtime_context.medical_db_path
        remcard_db_path = runtime_context.medical_db_path
        logger.info("[RUNTIME MODE] %s", runtime_mode)
        logger.info("[DB PATH] runtime medical -> %s", runtime_context.medical_db_path)
        logger.info("[SETTINGS DB] runtime settings -> %s", runtime_context.settings_db_path)

    from rem_card.services.settings.settings_service import configure_settings_service, get_settings_service

    if runtime_context is not None:
        settings_service = configure_settings_service(
            runtime_context=runtime_context,
            readonly=bool(getattr(runtime_context, "settings_readonly", False)),
        )
    else:
        settings_service = get_settings_service()
    settings_info = settings_service.ensure_ready()
    logger.info(
        "[SETTINGS DB] remcard_settings.db -> %s",
        settings_info.get("settings_db_path"),
    )

    if os.environ.get("REMCARD_BOOTSTRAP_DEBUG") == "1":
        if runtime_context is None:
            print(f"[NETWORK ROOT] {NETWORK_ROOT}")
            print(f"[BAZA PATH] {BAZA_DIR}")
            print(f"[DB PATH] rao_journal.db -> {JOURNAL_DB_PATH}")
            print(f"[DB PATH] unified_runtime.db -> {REMCARD_DB_PATH}")
        else:
            print(f"[RUNTIME MODE] {runtime_mode}")
            print(f"[DB PATH] runtime medical -> {runtime_context.medical_db_path}")
        print(f"[SETTINGS DB] remcard_settings.db -> {settings_info.get('settings_db_path')}")

    db_manager = DatabaseManager(medical_db_path, remcard_db_path, runtime_context=runtime_context)
    return Container(db_manager, role=role)

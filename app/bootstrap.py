import os
from typing import TYPE_CHECKING

from rem_card.app.logger import logger
from rem_card.app.roles import is_operblock_role
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
        from rem_card.services.operblock_service import OperBlockService
        from rem_card.services.patient_status_service import PatientStatusService
        from rem_card.services.read_coordinator import ReadCoordinator
        from rem_card.services.remcard_service import PatientService, RemCardService

        self.db_manager = db_manager
        self.runtime_context = getattr(db_manager, "runtime_context", None)
        self.runtime_mode = getattr(self.runtime_context, "mode", "network")
        role_key = str(role or os.environ.get("REMCARD_UI_ROLE", "")).strip().lower()
        monitor_enabled = not is_operblock_role(role_key)
        self.data_service = DataService(db_manager, monitor_enabled=monitor_enabled)
        self.data_service.set_runtime_role(role)
        if not monitor_enabled:
            logger.info("[OPERBLOCK DB] background change monitor disabled for writer role")
        if hasattr(self.db_manager, "set_write_queue_idle_probe"):
            self.db_manager.set_write_queue_idle_probe(self.data_service.is_write_queue_idle)
        self.emergency_standby_scheduler = self._create_emergency_standby_scheduler(role)
        self.emergency_restore_probe_scheduler = self._create_emergency_restore_probe_scheduler(role)

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
        self.operblock_service = OperBlockService(db_manager)

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

    def _create_emergency_restore_probe_scheduler(self, role: str | None):
        from rem_card.app.emergency_restore_probe import create_emergency_restore_probe_scheduler_for_runtime

        runtime_context = getattr(self.db_manager, "runtime_context", None)
        mode = getattr(runtime_context, "mode", "network")
        scheduler = create_emergency_restore_probe_scheduler_for_runtime(
            role=role,
            runtime_context=runtime_context,
            is_local_write_idle=self.data_service.is_write_queue_idle,
            is_shutdown=lambda: bool(
                getattr(self.data_service, "_shutting_down", False)
                or getattr(self.db_manager, "_closed", False)
                or getattr(self.db_manager, "_closing", False)
            ),
            on_status=self.data_service.emit_restore_probe_status,
        )
        if scheduler is None:
            return None
        self.data_service.set_emergency_restore_probe_scheduler(scheduler)
        if scheduler.start():
            logger.info("Emergency restore probe scheduler started for role=%s mode=%s", role, mode)
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

    if os.environ.get("REMCARD_BOOTSTRAP_DEBUG") == "1":
        if runtime_context is None:
            print(f"[NETWORK ROOT] {NETWORK_ROOT}")
            print(f"[BAZA PATH] {BAZA_DIR}")
            print(f"[DB PATH] rao_journal.db -> {JOURNAL_DB_PATH}")
            print(f"[DB PATH] unified_runtime.db -> {REMCARD_DB_PATH}")
        else:
            print(f"[RUNTIME MODE] {runtime_mode}")
            print(f"[DB PATH] runtime medical -> {runtime_context.medical_db_path}")

    db_manager = DatabaseManager(medical_db_path, remcard_db_path, runtime_context=runtime_context)
    from rem_card.services.settings.settings_service import configure_settings_service, get_settings_service

    if runtime_context is not None:
        settings_service = configure_settings_service(
            runtime_context=runtime_context,
            readonly=bool(getattr(runtime_context, "settings_readonly", False)),
        )
    else:
        settings_service = get_settings_service()
    from rem_card.app import operblock_startup_metrics

    with operblock_startup_metrics.measure("settings_ensure_ready_ms", source="bootstrap"):
        settings_info = settings_service.ensure_ready()
    logger.info(
        "[SETTINGS DB] remcard_settings.db -> %s",
        settings_info.get("settings_db_path"),
    )
    if os.environ.get("REMCARD_BOOTSTRAP_DEBUG") == "1":
        print(f"[SETTINGS DB] remcard_settings.db -> {settings_info.get('settings_db_path')}")

    if is_operblock_role(str(role or os.environ.get("REMCARD_UI_ROLE", "")).strip().lower()):
        local_db_used = str(bool(runtime_mode == "opblock_offline")).lower()
        db_profile = "local_network_safe" if runtime_mode == "opblock_offline" else "network"
        logger.info(
            "[OPERBLOCK DB] role=%s data_root=%s db_path=%s db_profile=%s local_db_used=%s",
            str(role or os.environ.get("REMCARD_UI_ROLE", "")).strip().lower() or "operblock",
            getattr(db_manager, "baza_dir", BAZA_DIR),
            getattr(db_manager, "db_path", medical_db_path),
            db_profile,
            local_db_used,
        )
        from rem_card.app.operblock_schema import ensure_operblock_schema

        with operblock_startup_metrics.measure("ensure_operblock_schema_ms", source="bootstrap"):
            result = ensure_operblock_schema(db_manager)
        logger.info(
            "[OPERBLOCK SCHEMA] migrated=%s backup_path=%s quick_check=%s integrity_check=%s",
            result.migrated,
            result.backup_path or "",
            result.quick_check or "",
            result.integrity_check or "",
        )

    return Container(db_manager, role=role)

"""Safety-сценарии: clinical."""

from __future__ import annotations

from .common import PROJECT_ROOT
from pathlib import Path
from .common import _cached_source_segment
import ast
import json
import os
import time


def _check_local_metrics_are_buffered(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    root = PROJECT_ROOT
    source_path = root / "app/local_metrics.py"
    source_text = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text)
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    required = {"record_metric", "flush_metrics", "shutdown_metrics", "_metrics_worker", "_write_payloads"}
    missing = sorted(required - set(functions))
    if missing:
        return False, f"local_metrics missing buffered helpers: {missing}"

    record_source = _cached_source_segment(source_text, functions["record_metric"]) or ""
    if "put_nowait" not in record_source:
        return False, "record_metric must enqueue without blocking the read path"
    if "_write_payloads([payload])" not in record_source:
        return False, "record_metric must keep a sync/forced-flush escape hatch"
    if "open(" in record_source or "_metrics_path()" in record_source:
        return False, "record_metric hot path must not open metrics files directly"
    if "REMCARD_LOCAL_METRICS_SYNC" not in source_text:
        return False, "local metrics sync fallback env flag is missing"
    if "RemCardLocalMetricsWriter" not in source_text:
        return False, "local metrics background writer thread is missing"

    return True, "ok"


def _check_latest_change_metric_throttles_unchanged_values(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from rem_card.app import local_metrics

    saved_sync = os.environ.get("REMCARD_LOCAL_METRICS_SYNC")
    saved_interval = os.environ.get("REMCARD_LATEST_CHANGE_METRIC_MIN_INTERVAL_SEC")
    try:
        os.environ["REMCARD_LOCAL_METRICS_SYNC"] = "1"
        os.environ["REMCARD_LATEST_CHANGE_METRIC_MIN_INTERVAL_SEC"] = "999"
        local_metrics._LATEST_CHANGE_METRIC_STATE.clear()  # type: ignore[attr-defined]
        for _idx in range(5):
            local_metrics.record_metric(
                "latest_change_id",
                100,
                component="regression_throttle",
                admission_id=None,
                include_global=True,
                source="central",
            )
        local_metrics.record_metric(
            "latest_change_id",
            101,
            component="regression_throttle",
            admission_id=None,
            include_global=True,
            source="central",
        )
        local_metrics.record_metric(
            "latest_change_id",
            101,
            component="regression_throttle",
            admission_id=None,
            include_global=True,
            source="fallback",
        )
        local_metrics.flush_metrics(timeout=1.0)

        metrics_dir = os.environ["REMCARD_LOCAL_LOGS_DIR"]
        files = [
            os.path.join(metrics_dir, name)
            for name in os.listdir(metrics_dir)
            if name.startswith("metrics_") and name.endswith(".jsonl")
        ]
        if not files:
            return False, "metrics file was not created for throttle check"
        newest = max(files, key=os.path.getmtime)
        records = []
        with open(newest, "r", encoding="utf-8") as fh:
            for line in fh:
                if "regression_throttle" in line:
                    records.append(json.loads(line))
        if len(records) != 3:
            return False, f"latest_change_id throttle wrote {len(records)} records instead of 3: {records}"
        if [record.get("value") for record in records] != [100, 101, 101]:
            return False, f"latest_change_id throttle preserved wrong values: {records}"
        if records[-1].get("source") != "fallback":
            return False, "fallback latest_change_id metric must bypass throttle"
        return True, "ok"
    finally:
        if saved_sync is None:
            os.environ.pop("REMCARD_LOCAL_METRICS_SYNC", None)
        else:
            os.environ["REMCARD_LOCAL_METRICS_SYNC"] = saved_sync
        if saved_interval is None:
            os.environ.pop("REMCARD_LATEST_CHANGE_METRIC_MIN_INTERVAL_SEC", None)
        else:
            os.environ["REMCARD_LATEST_CHANGE_METRIC_MIN_INTERVAL_SEC"] = saved_interval
        local_metrics._LATEST_CHANGE_METRIC_STATE.clear()  # type: ignore[attr-defined]


def _check_crash_handler_clean_finalize_removes_session_files(temp_root: str) -> tuple[bool, str]:
    from rem_card.app import logger as logger_module

    spool = os.path.join(temp_root, "crash-outbox")
    saved_spool = os.environ.get("REMCARD_CRASH_OUTBOX_DIR")
    try:
        os.environ["REMCARD_CRASH_OUTBOX_DIR"] = spool
        session_id = logger_module.init_crash_handler(role="nurse")
        if not session_id:
            return False, "crash session was not initialized"
        logger_module.finalize_crash_handler(exit_code=0)
        remaining = [
            path.relative_to(spool).as_posix()
            for path in Path(spool).rglob("*")
            if path.is_file()
        ]
        if remaining:
            return False, f"clean crash session left files behind: {remaining}"
        return True, "ok"
    finally:
        logger_module.finalize_crash_handler(exit_code=0)
        if saved_spool is None:
            os.environ.pop("REMCARD_CRASH_OUTBOX_DIR", None)
        else:
            os.environ["REMCARD_CRASH_OUTBOX_DIR"] = saved_spool


def _check_sector_ivl_enqueue_error_refreshes(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication

    from rem_card.ui.rem_card_sectors import sector_ivl

    _ = temp_root
    app = QApplication.instance() or QApplication([])

    class FakeIvlService:
        def __init__(self):
            self.enqueue_called = False
            self.summary_reads = 0

        def enqueue_write(self, description, operation, on_success=None, on_error=None, write_metadata=None):
            self.enqueue_called = True
            if on_error:
                on_error(RuntimeError("forced ivl write failure"))

        def get_ventilation_summary(self, admission_id):
            self.summary_reads += 1
            return {"active_case": None, "total_duration_seconds": 0}

        def get_ventilation_timeline(self, admission_id):
            return []

        def get_latest_ventilation_case(self, admission_id):
            return None

        def get_latest_change_id(self, admission_id=None, include_global=True):
            return self.summary_reads

        def get_patient(self, admission_id):
            return SimpleNamespace(admission_datetime=datetime(2026, 5, 3, 8, 0))

    warnings: list[str] = []
    original_warning = sector_ivl.CustomMessageBox.warning
    sector_ivl.CustomMessageBox.warning = lambda parent, title, message: warnings.append(f"{title}: {message}")
    widget = sector_ivl.SectorIvl()
    service = FakeIvlService()
    try:
        widget.set_runtime_context(service, 1)
        widget._enqueue_ivl_write(
            "regression_ivl_error",
            lambda: None,
            pending_text="Случай: сохранение...",
            error_title="Ошибка ИВЛ",
        )
        app.processEvents()
        if not service.enqueue_called:
            return False, "SectorIvl did not use enqueue_write"
        if widget._ivl_write_pending:
            return False, "SectorIvl kept pending state after write error"
        if not warnings or "forced ivl write failure" not in warnings[-1]:
            return False, f"SectorIvl did not show write error warning: {warnings}"
        if service.summary_reads < 2:
            return False, "SectorIvl did not refresh from DB/service after write error"
        return True, "ok"
    finally:
        sector_ivl.CustomMessageBox.warning = original_warning
        widget.close()


def _check_balance_controller_enqueue_error_refreshes(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime, timedelta

    from PySide6.QtWidgets import QApplication

    from rem_card.ui.shared.components import balance_controller as balance_module
    from rem_card.ui.shared.components.balance_controller import BalanceController

    _ = temp_root
    app = QApplication.instance() or QApplication([])

    class FakeButton:
        def __init__(self):
            self.enabled = True

        def setEnabled(self, enabled):
            self.enabled = bool(enabled)

        def isEnabled(self):
            return self.enabled

    class FakeLabel:
        def __init__(self):
            self.text = ""

        def setText(self, text):
            self.text = text

    class FakePanel:
        def __init__(self):
            self.edit_input = FakeButton()
            self.btn_save = FakeButton()
            self.btn_delete = FakeButton()
            self.btn_undo = FakeButton()
            self.status_lbl = FakeLabel()

        def set_selection(self, label_text, current_val=None, keep_focus=True):
            self.last_selection = (label_text, current_val, keep_focus)

        def set_undo_active(self, active):
            self.btn_undo.setEnabled(active)

    class FakeGrid:
        def __init__(self):
            self.enabled = True
            self.rows_map = ["urine", "drain_output", "ng_output", "stool", "other_output"]
            self.row_labels = ["Диурез", "Дренажи", "ЖКТ (зонд)", "Рвота", "Другое"]

        def setEnabled(self, enabled):
            self.enabled = bool(enabled)

        def update_data(self, hourly_data):
            self.hourly_data = hourly_data

        def currentRow(self):
            return 0

        def currentColumn(self):
            return 0

        def get_selected_info(self):
            return "urine", 8, 0

    class FakeVitalService:
        def get_effective_bounds(self, admission_id, shift_date):
            return shift_date - timedelta(hours=1), shift_date + timedelta(hours=23)

    class FakeFluidService:
        def __init__(self):
            self.vital_service = FakeVitalService()
            self.enqueue_called = False
            self.refresh_reads = 0
            self.on_error = None

        def enqueue_write(self, description, operation, on_success=None, on_error=None, write_metadata=None):
            self.enqueue_called = True
            self.description = description
            self.operation = operation
            self.on_error = on_error

        def upsert_hourly_output(self, **kwargs):
            raise AssertionError("queued write operation should not run in UI thread")

        def get_fluids(self, admission_id, shift_date):
            self.refresh_reads += 1
            return []

    critical_messages: list[str] = []
    original_critical = balance_module.CustomMessageBox.critical
    balance_module.CustomMessageBox.critical = lambda parent, title, message: critical_messages.append(f"{title}: {message}")
    try:
        shift_date = datetime(2026, 5, 3, 8, 0)
        service = FakeFluidService()
        controller = BalanceController(service, admission_id=1, shift_date=shift_date)
        refresh_requests = []
        controller.refresh_requested.connect(lambda: refresh_requests.append(True))
        controller.grid = FakeGrid()
        controller.panel_2d = FakePanel()
        controller._effective_bounds_cache = (shift_date - timedelta(hours=1), shift_date + timedelta(hours=23))

        controller._process_update("urine", 8, 100, is_sum=False)
        app.processEvents()
        if not service.enqueue_called:
            return False, "BalanceController did not use enqueue_write"
        if not controller._write_pending:
            return False, "BalanceController did not enter pending state"
        if controller.grid.enabled or controller.panel_2d.btn_save.enabled:
            return False, "BalanceController did not disable write UI while pending"
        if not service.on_error:
            return False, "BalanceController did not register error callback"

        service.on_error(RuntimeError("forced balance write failure"))
        app.processEvents()
        if controller._write_pending:
            return False, "BalanceController kept pending state after write error"
        if not controller.grid.enabled:
            return False, "BalanceController did not re-enable UI after write error"
        if controller._undo_stack:
            return False, f"BalanceController added undo state after failed write: {controller._undo_stack}"
        if not refresh_requests or service.refresh_reads != 0:
            return False, "BalanceController must request background refresh without reading DB on the UI thread"
        if not critical_messages or "forced balance write failure" not in critical_messages[-1]:
            return False, f"BalanceController did not show write error: {critical_messages}"
        return True, "ok"
    finally:
        balance_module.CustomMessageBox.critical = original_critical


def _check_diet_intake_enqueue_error_refreshes(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from PySide6.QtWidgets import QApplication

    from rem_card.ui.shared.components import diet_intake_widget as diet_module
    from rem_card.ui.shared.components.diet_intake_widget import DietIntakeWidget

    _ = temp_root
    app = QApplication.instance() or QApplication([])

    class FakeDietService:
        def __init__(self):
            self.enqueue_called = False
            self.on_error = None
            self.refresh_reads = 0

        def enqueue_write(self, description, operation, on_success=None, on_error=None, write_metadata=None):
            self.enqueue_called = True
            self.description = description
            self.operation = operation
            self.on_error = on_error

        def list_diet_templates(self):
            self.refresh_reads += 1
            return []

        def get_diet_plan(self, admission_id, shift_date):
            return None

        def get_oral_intake_events(self, admission_id, shift_date):
            return []

        def get_latest_change_id(self, admission_id=None, include_global=True):
            return self.refresh_reads

        def current_shift_time(self, shift_date):
            return "08:00"

    warnings: list[str] = []
    original_warning = diet_module.CustomMessageBox.warning
    diet_module.CustomMessageBox.warning = lambda parent, title, message: warnings.append(f"{title}: {message}")
    try:
        service = FakeDietService()
        widget = DietIntakeWidget(service=service, role="nurse")
        widget.admission_id = 1
        widget.shift_date = datetime(2026, 5, 3, 8, 0)
        widget.btn_save.setEnabled(True)
        widget.btn_cancel.setEnabled(True)

        widget._enqueue_write("regression_diet_error", lambda: None)
        app.processEvents()
        if not service.enqueue_called:
            return False, "DietIntakeWidget did not use enqueue_write"
        if not widget._write_pending:
            return False, "DietIntakeWidget did not enter pending state"
        if widget.btn_save.isEnabled():
            return False, "DietIntakeWidget did not disable save while pending"
        if not service.on_error:
            return False, "DietIntakeWidget did not register error callback"

        service.on_error(RuntimeError("forced diet write failure"))
        app.processEvents()
        if widget._write_pending:
            return False, "DietIntakeWidget kept pending state after write error"
        if not widget.btn_save.isEnabled():
            return False, "DietIntakeWidget did not re-enable save after write error"
        if service.refresh_reads < 1:
            return False, "DietIntakeWidget did not refresh from service after write error"
        if not warnings or "forced diet write failure" not in warnings[-1]:
            return False, f"DietIntakeWidget did not show write error: {warnings}"
        return True, "ok"
    finally:
        diet_module.CustomMessageBox.warning = original_warning
        try:
            widget.close()
        except Exception:
            pass


def _check_diet_intake_cached_snapshot_refreshes_templates(temp_root: str) -> tuple[bool, str]:
    from collections import OrderedDict
    from datetime import datetime

    from PySide6.QtWidgets import QApplication

    from rem_card.data.dto.remcard_dto import DietTemplateDTO
    from rem_card.ui.shared.components.diet_intake_widget import DietIntakeWidget

    _ = temp_root
    app = QApplication.instance() or QApplication([])
    _ = app

    old_template = DietTemplateDTO(id=1, name="ОВД", schedule_json="[]", version=1)
    new_template = DietTemplateDTO(id=5, name="Питье по требованию", schedule_json="[]", version=1)

    class FakeDietService:
        def __init__(self):
            self.templates = [old_template]
            self.template_reads = 0

        def list_diet_templates(self):
            self.template_reads += 1
            return list(self.templates)

        def get_diet_plan(self, admission_id, shift_date):
            return None

        def get_oral_intake_events(self, admission_id, shift_date):
            return []

        def get_latest_change_id(self, admission_id=None, include_global=True):
            _ = admission_id, include_global
            return 10

    service = FakeDietService()
    widget = DietIntakeWidget(service=service, role="doctor")
    try:
        widget.admission_id = 917001
        widget.shift_date = datetime(2026, 5, 19, 8, 0)
        cache_key = widget._cache_key()
        widget._snapshot_cache = OrderedDict(
            [
                (
                    cache_key,
                    {
                        "version": 10,
                        "templates": [old_template],
                        "plan": None,
                        "events": [],
                    },
                )
            ]
        )

        service.templates = [old_template, new_template]
        widget.refresh_data(force=False)
        names = [template.name for template in widget._templates]
        if "Питье по требованию" not in names:
            return False, f"diet templates stayed stale after cached snapshot hit: {names}"
        if service.template_reads != 1:
            return False, f"diet template list should be reread once on cache hit: {service.template_reads}"
        return True, "ok"
    finally:
        try:
            widget.close()
        except Exception:
            pass


def _check_diet_templates_manual_order_persists(temp_root: str) -> tuple[bool, str]:
    from rem_card.services.diet_service import DietTemplateFileStore, DietTemplateService

    path = os.path.join(temp_root, "diet_templates.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "next_id": 4,
                "templates": [
                    {"id": 1, "name": "Второй", "schedule": [], "version": 1},
                    {"id": 2, "name": "Первый", "schedule": [], "version": 1},
                    {"id": 3, "name": "Третий", "schedule": [], "version": 1},
                ],
            },
            fh,
            ensure_ascii=False,
        )

    service = DietTemplateService(DietTemplateFileStore(path=path))
    if [int(t.id) for t in service.list_templates()] != [1, 2, 3]:
        return False, "diet templates should preserve file order instead of sorting by name"

    service.reorder_templates([3, 1, 2])
    if [int(t.id) for t in service.list_templates()] != [3, 1, 2]:
        return False, "diet template reorder did not persist requested order"

    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if [int(item["id"]) for item in payload.get("templates", [])] != [3, 1, 2]:
        return False, f"stored diet template order mismatch: {payload}"

    current = service.get_template(1)
    service.update_template(
        1,
        name="Второй измененный",
        diet_text=current.diet_text,
        schedule_json=current.schedule_json,
        is_default=bool(current.is_default),
        expected_version=current.version,
    )
    if [int(t.id) for t in service.list_templates()] != [3, 1, 2]:
        return False, "diet template update changed manual order"

    new_id = service.create_template("Новый", schedule_json=[])
    if [int(t.id) for t in service.list_templates()] != [3, 1, 2, int(new_id)]:
        return False, "new diet template should be appended after manual order"

    return True, "ok"


def _check_diet_templates_widget_reorder_updates_service(temp_root: str) -> tuple[bool, str]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from rem_card.data.dto.remcard_dto import DietTemplateDTO
    from rem_card.ui.admin_view.diet_templates_widget import DietTemplatesWidget

    _ = temp_root
    app = QApplication.instance() or QApplication([])
    _ = app

    class FakeDietTemplateService:
        def __init__(self):
            self.templates = [
                DietTemplateDTO(id=1, name="Первый", schedule_json="[]"),
                DietTemplateDTO(id=2, name="Второй", schedule_json="[]"),
                DietTemplateDTO(id=3, name="Третий", schedule_json="[]"),
            ]
            self.reorder_calls = []

        def list_diet_templates(self):
            return list(self.templates)

        def reorder_diet_templates(self, ordered_template_ids):
            self.reorder_calls.append([int(item) for item in ordered_template_ids])
            by_id = {int(template.id): template for template in self.templates}
            self.templates = [by_id[int(template_id)] for template_id in ordered_template_ids]

    service = FakeDietTemplateService()
    widget = DietTemplatesWidget(service=service, role="admin")
    try:
        widget.table.setCurrentCell(1, 0)
        widget.move_selected_template_up()
        if service.reorder_calls != [[2, 1, 3]]:
            return False, f"widget did not pass moved template order to service: {service.reorder_calls}"
        if [int(widget.table.item(row, 0).data(Qt.UserRole)) for row in range(widget.table.rowCount())] != [2, 1, 3]:
            return False, "widget table did not reload in reordered order"
        if int(widget.current_template().id) != 2:
            return False, "widget did not keep moved template selected"
        return True, "ok"
    finally:
        try:
            widget.close()
        except Exception:
            pass


def _check_oral_intake_batch_rolls_back(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.data.dao.diet_dao import OralIntakeDAO
    from rem_card.data.dao.exceptions import OptimisticLockError
    from rem_card.data.dao.patient_dao import PatientDAO
    from rem_card.services.diet_service import OralIntakeService
    from rem_card.services.vital_service import VitalService

    db_path = os.path.join(temp_root, "oral_intake_batch.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        admission_dt = datetime(2026, 4, 24, 8, 0, 0)
        existing_dt = datetime(2026, 4, 24, 10, 0, 0)
        new_dt = datetime(2026, 4, 24, 9, 0, 0)
        with manager.remcard_transaction(source="regression_seed_oral_batch") as cursor:
            cursor.execute(
                """
                INSERT INTO patients (full_name, last_name, first_name, middle_name)
                VALUES (?, ?, ?, ?)
                """,
                ("Петров Петр", "Петров", "Петр", None),
            )
            patient_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO admissions (
                    patient_id,
                    bed_number,
                    history_number,
                    admission_datetime,
                    is_active
                )
                VALUES (?, ?, ?, ?, 1)
                """,
                (patient_id, 1, "REG-DIET-001", admission_dt.isoformat()),
            )
            admission_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO oral_intake_events (
                    admission_id, shift_start, event_time, amount_ml, version, last_modified_by, updated_at
                )
                VALUES (?, ?, ?, 50, 1, 'nurse', STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
                """,
                (
                    admission_id,
                    admission_dt.strftime("%Y-%m-%d %H:%M"),
                    existing_dt.strftime("%Y-%m-%d %H:%M"),
                ),
            )

        vital_service = VitalService(vitals_dao=None, patient_dao=PatientDAO(manager), status_service=None)
        oral_service = OralIntakeService(OralIntakeDAO(manager), vital_service)

        try:
            oral_service.apply_changes(
                admission_id,
                [
                    {"event_dt": new_dt, "amount": 100, "expected_version": None},
                    {"event_dt": existing_dt, "amount": 250, "expected_version": 999},
                ],
            )
        except OptimisticLockError:
            pass
        else:
            return False, "batch did not raise optimistic lock conflict"

        inserted = oral_service.dao.get_event_at(admission_id, new_dt)
        if inserted is not None:
            return False, "first batch change was committed despite later failure"
        existing = oral_service.dao.get_event_at(admission_id, existing_dt)
        if existing is None or int(existing.amount_ml) != 50 or int(existing.version) != 1:
            return False, f"existing oral event changed despite rollback: {existing}"
        return True, "ok"
    finally:
        manager.close()


def _check_patient_form_enqueue_error_keeps_dialog(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from PySide6.QtWidgets import QApplication

    from rem_card.ui.patient_bed_management import patient_form as patient_form_module
    from rem_card.ui.patient_bed_management.patient_form import PatientForm

    _ = temp_root
    app = QApplication.instance() or QApplication([])

    class FakePatientBedService:
        def __init__(self):
            self.enqueue_called = False
            self.on_error = None

        def enqueue_write(self, description, operation, on_success=None, on_error=None, write_metadata=None):
            self.enqueue_called = True
            self.description = description
            self.operation = operation
            self.on_error = on_error

    class FakeGeneralTab:
        def get_data(self):
            return {
                "history_number": "REG-PAT-001",
                "full_name": "Иванов Иван",
                "birth_date": datetime(1986, 5, 3).date(),
                "birth_date_text": "03.05.1986",
                "admission_datetime": datetime(2026, 5, 3, 8, 0),
                "age_value": 40,
                "months": None,
                "age_unit": "лет",
                "gender": "М",
                "department_profile": "ОАР",
                "source_department": "Приемное",
            }

    class FakeDiagnosisTab:
        def get_data(self):
            return {"diagnosis_code": "A00", "diagnosis_text": "Тестовый диагноз"}

    warnings: list[str] = []
    original_warning = patient_form_module.CustomMessageBox.warning
    original_warning_with_actions = patient_form_module.CustomMessageBox.warning_with_actions
    patient_form_module.CustomMessageBox.warning = lambda parent, title, message: warnings.append(f"{title}: {message}")
    patient_form_module.CustomMessageBox.warning_with_actions = (
        lambda parent, title, message, action_buttons: patient_form_module.CustomMessageBox.Yes
    )
    form = None
    try:
        service = FakePatientBedService()
        form = PatientForm(service, 1)
        form.general_tab = FakeGeneralTab()
        form.diagnosis_tab = FakeDiagnosisTab()
        form._save_data()
        app.processEvents()
        if not service.enqueue_called:
            return False, "PatientForm did not use enqueue_write"
        if not form._write_pending:
            return False, "PatientForm did not enter pending state"
        if form.save_button.isEnabled() or form.save_button.text() != "Сохранение...":
            return False, "PatientForm did not show pending save state"
        if not service.on_error:
            return False, "PatientForm did not register error callback"
        service.on_error(RuntimeError("forced patient form failure"))
        app.processEvents()
        if form._write_pending:
            return False, "PatientForm kept pending state after error"
        if not form.save_button.isEnabled() or form.save_button.text() != "Сохранить карточку":
            return False, "PatientForm did not restore save button after error"
        if not warnings or "forced patient form failure" not in warnings[-1]:
            return False, f"PatientForm did not show write error: {warnings}"
        return True, "ok"
    finally:
        patient_form_module.CustomMessageBox.warning = original_warning
        patient_form_module.CustomMessageBox.warning_with_actions = original_warning_with_actions
        if form is not None:
            form.close()


def _check_side_patient_card_child_photo_uses_gender_assets(temp_root: str) -> tuple[bool, str]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from datetime import datetime
    from types import SimpleNamespace

    from PySide6.QtCore import QSize, Qt
    from PySide6.QtGui import QPixmap
    from PySide6.QtWidgets import QApplication

    from rem_card.app.paths import get_icon_dir
    from rem_card.ui.patient_bed_management import side_patient_card as side_module
    from rem_card.ui.patient_bed_management.side_patient_card import PATIENT_PHOTO_SIZE, SidePatientCard

    _ = temp_root
    app = QApplication.instance() or QApplication([])
    checks = (
        ("Мужской", "man_in_oper_extr.png", side_module.REMCARD_MALE_PATIENT_ICON_KEY, None),
        ("Женский", "woman_in_oper_extr.png", side_module.REMCARD_FEMALE_PATIENT_ICON_KEY, None),
        ("Умер", "deadman.png", side_module.REMCARD_DEAD_PATIENT_ICON_KEY, "DEAD"),
    )
    asset_by_key = {}
    for _label, asset_name, icon_key, _status in checks:
        asset_path = os.path.join(get_icon_dir(), asset_name)
        if not os.path.isfile(asset_path):
            return False, f"side patient card asset is missing: {asset_name}"
        asset_by_key[icon_key] = asset_path

    requested_keys = []
    original_loader = side_module.request_remcard_icon_pixmap

    def fake_request_remcard_icon_pixmap(_label, icon_key, *, target_size=None, **kwargs):
        requested_keys.append(icon_key)
        pixmap = QPixmap(asset_by_key.get(icon_key, ""))
        if target_size is not None and not pixmap.isNull():
            size = target_size if isinstance(target_size, QSize) else QSize(*target_size)
            pixmap = pixmap.scaled(
                size,
                kwargs.get("aspect_mode", Qt.KeepAspectRatio),
                kwargs.get("transformation_mode", Qt.SmoothTransformation),
            )
        return pixmap

    side_module.request_remcard_icon_pixmap = fake_request_remcard_icon_pixmap
    card = SidePatientCard()
    try:
        for label, asset_name, icon_key, current_status in checks:
            asset_path = asset_by_key[icon_key]
            patient = SimpleNamespace(full_name=f"Тест {label}", birth_date=datetime(2022, 1, 1).date())
            admission = SimpleNamespace(
                history_number="REG-SIDE-001",
                patient_age=4,
                patient_months=None,
                patient_age_unit="годы",
                patient_gender="Мужской" if current_status else label,
                current_status=current_status,
                admission_datetime=datetime(2026, 1, 1, 9, 0),
                diagnosis_text="Тестовый диагноз",
            )
            card.update_info(1, patient, admission)
            app.processEvents()
            if requested_keys[-1:] != [icon_key]:
                return False, f"side patient card requested wrong photo key for {label}: {requested_keys[-1:]}"
            actual = card.photo_label.pixmap()
            if actual is None or actual.isNull():
                return False, f"side patient card did not render photo for {label}"
            expected = card._circular_patient_photo(QPixmap(asset_path))
            if actual.size() != expected.size():
                return False, f"side patient card photo size mismatch for {label}: {actual.size()} != {expected.size()}"
            if actual.size().width() != PATIENT_PHOTO_SIZE or actual.size().height() != PATIENT_PHOTO_SIZE:
                return False, f"side patient card photo frame size changed for {label}: {actual.size()}"
            actual_image = actual.toImage()
            expected_image = expected.toImage()
            for x, y in (
                (actual_image.width() // 2, actual_image.height() // 2),
                (actual_image.width() // 3, actual_image.height() // 3),
                (actual_image.width() * 2 // 3, actual_image.height() * 2 // 3),
            ):
                actual_color = actual_image.pixelColor(x, y)
                expected_color = expected_image.pixelColor(x, y)
                channel_delta = (
                    abs(actual_color.red() - expected_color.red())
                    + abs(actual_color.green() - expected_color.green())
                    + abs(actual_color.blue() - expected_color.blue())
                    + abs(actual_color.alpha() - expected_color.alpha())
                )
                if channel_delta > 12:
                    return False, f"side patient card photo pixels do not match expected asset for {label}"
        return True, "ok"
    finally:
        side_module.request_remcard_icon_pixmap = original_loader
        card.close()
        app.processEvents()


def _check_patient_bed_move_enqueue_error_refreshes(temp_root: str) -> tuple[bool, str]:
    from PySide6.QtWidgets import QApplication

    from rem_card.ui.patient_bed_management import management_widget as management_module
    from rem_card.ui.patient_bed_management.management_widget import NUM_BEDS, PatientBedManagementWidget

    _ = temp_root
    app = QApplication.instance() or QApplication([])

    class FakePatientBedService:
        def __init__(self):
            self.enqueue_called = False
            self.on_error = None
            self.refresh_reads = 0

        def get_bed_by_number(self, bed_number):
            if int(bed_number) == 1:
                return {"bed_number": 1, "status": "OCCUPIED", "current_admission_id": 10}
            return {"bed_number": int(bed_number), "status": "FREE", "current_admission_id": None}

        def enqueue_write(self, description, operation, on_success=None, on_error=None, write_metadata=None):
            self.enqueue_called = True
            self.description = description
            self.operation = operation
            self.on_error = on_error

        def get_beds_snapshot(self):
            self.refresh_reads += 1
            return [
                {
                    "bed_number": idx,
                    "status": "FREE",
                    "current_admission_id": None,
                    "full_name": "",
                    "history_number": "",
                    "diagnosis_text": "",
                }
                for idx in range(1, NUM_BEDS + 1)
            ]

        def get_patient_with_current_admission(self, bed_number):
            return None, None

    warnings: list[str] = []
    original_question = management_module.CustomMessageBox.question
    original_warning = management_module.CustomMessageBox.warning
    management_module.CustomMessageBox.question = lambda *args, **kwargs: management_module.CustomMessageBox.Yes
    management_module.CustomMessageBox.warning = lambda parent, title, message: warnings.append(f"{title}: {message}")
    widget = None
    try:
        widget = PatientBedManagementWidget(db_manager=object())
        service = FakePatientBedService()
        widget.patient_bed_service = service
        widget.move_patient(1, 2)
        app.processEvents()
        if not service.enqueue_called:
            return False, "PatientBedManagementWidget did not use enqueue_write"
        if not widget._move_pending:
            return False, "PatientBedManagementWidget did not enter move pending state"
        if any(bed.isEnabled() for bed in widget.bed_widgets):
            return False, "PatientBedManagementWidget did not disable bed widgets while pending"
        if not service.on_error:
            return False, "PatientBedManagementWidget did not register error callback"
        service.on_error(RuntimeError("forced bed move failure"))
        app.processEvents()
        if widget._move_pending:
            return False, "PatientBedManagementWidget kept pending state after error"
        if not all(bed.isEnabled() for bed in widget.bed_widgets):
            return False, "PatientBedManagementWidget did not re-enable bed widgets after error"
        if service.refresh_reads < 1:
            return False, "PatientBedManagementWidget did not refresh beds after error"
        if not warnings or "forced bed move failure" not in warnings[-1]:
            return False, f"PatientBedManagementWidget did not show move error: {warnings}"
        return True, "ok"
    finally:
        management_module.CustomMessageBox.question = original_question
        management_module.CustomMessageBox.warning = original_warning
        if widget is not None:
            widget.close()


def _check_archive_delete_enqueue_error_refreshes(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication

    from rem_card.ui.doctor_view import archive_widget as archive_module
    from rem_card.ui.doctor_view.archive_widget import ArchiveWidget

    _ = temp_root
    app = QApplication.instance() or QApplication([])

    class FakeArchivePatient(SimpleNamespace):
        def get_display_name(self):
            return self.full_name

    class FakeWriteService:
        def __init__(self, result=None):
            self.result = result
            self.enqueue_called = False
            self.on_error = None
            self.on_success = None

        def enqueue_write(self, description, operation, on_success=None, on_error=None, write_metadata=None):
            self.enqueue_called = True
            self.description = description
            self.operation = operation
            self.on_success = on_success
            self.on_error = on_error

        def get_archived_patients(self):
            return []

        def delete_admission(self, admission_id):
            return self.result

        def delete_last_card(self, admission_id):
            return self.result

    warnings: list[str] = []
    original_question = archive_module.CustomMessageBox.question
    original_warning = archive_module.CustomMessageBox.warning
    archive_module.CustomMessageBox.question = lambda *args, **kwargs: archive_module.CustomMessageBox.Yes
    archive_module.CustomMessageBox.warning = lambda parent, title, message: warnings.append(f"{title}: {message}")
    widget = None
    try:
        patient_service = FakeWriteService()
        remcard_service = FakeWriteService(result=(True, None, "ok"))
        widget = ArchiveWidget(
            patient_service,
            remcard_service=remcard_service,
            allow_edit=True,
        )
        load_calls = []
        patient = FakeArchivePatient(
            id=1,
            full_name="Иванов Иван",
            history_number="REG-ARCH-001",
            diagnosis_text="Тест",
            admission_datetime=datetime(2026, 5, 3, 8, 0),
            transfer_datetime=datetime(2026, 5, 4, 8, 0),
            is_external_archive=False,
            source_db_path=None,
            source_admission_id=None,
        )
        def fake_load_data(*args, **kwargs):
            load_calls.append((args, kwargs))
            widget._apply_loaded_records(
                widget._load_token,
                widget.archive_source_mode,
                {"records": [patient], "total_count": 1, "page": 1, "page_size": widget.page_size},
            )

        widget.load_data = fake_load_data
        widget.all_archived_patients = [patient]
        widget.filter_data()
        widget.table.selectRow(0)

        widget.on_delete_clicked()
        app.processEvents()
        if not patient_service.enqueue_called:
            return False, "ArchiveWidget delete-all did not use enqueue_write"
        if not widget._delete_pending or widget.table.isEnabled():
            return False, "ArchiveWidget did not enter pending state for delete-all"
        if not patient_service.on_error:
            return False, "ArchiveWidget did not register delete-all error callback"
        patient_service.on_error(RuntimeError("forced archive delete failure"))
        app.processEvents()
        if widget._delete_pending or not widget.table.isEnabled():
            return False, "ArchiveWidget did not restore UI after delete-all error"
        if not load_calls:
            return False, "ArchiveWidget did not refresh after delete-all error"
        if not warnings or "forced archive delete failure" not in warnings[-1]:
            return False, f"ArchiveWidget did not show delete-all error: {warnings}"

        warnings.clear()
        load_calls.clear()
        widget.table.selectRow(0)
        widget.on_delete_last_clicked()
        app.processEvents()
        if not remcard_service.enqueue_called:
            return False, "ArchiveWidget delete-last did not use enqueue_write"
        if not widget._delete_pending or widget.table.isEnabled():
            return False, "ArchiveWidget did not enter pending state for delete-last"
        if not remcard_service.on_error:
            return False, "ArchiveWidget did not register delete-last error callback"
        remcard_service.on_error(RuntimeError("forced archive last-card failure"))
        app.processEvents()
        if widget._delete_pending or not widget.table.isEnabled():
            return False, "ArchiveWidget did not restore UI after delete-last error"
        if not load_calls:
            return False, "ArchiveWidget did not refresh after delete-last error"
        if not warnings or "forced archive last-card failure" not in warnings[-1]:
            return False, f"ArchiveWidget did not show delete-last error: {warnings}"
        return True, "ok"
    finally:
        archive_module.CustomMessageBox.question = original_question
        archive_module.CustomMessageBox.warning = original_warning
        if widget is not None:
            widget.close()


def _check_process_launch_hides_console_windows(temp_root: str) -> tuple[bool, str]:
    from rem_card.app import process_launch

    _ = temp_root
    calls = []
    original_popen = process_launch.subprocess.Popen

    class FakeProcess:
        pass

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess()

    process_launch.subprocess.Popen = fake_popen
    try:
        result = process_launch.popen_hidden(["probe.exe"], cwd=temp_root)
    finally:
        process_launch.subprocess.Popen = original_popen

    if not isinstance(result, FakeProcess):
        return False, "popen_hidden не вернул результат Popen"
    if len(calls) != 1:
        return False, f"popen_hidden вызвал Popen неверное число раз: {len(calls)}"

    kwargs = calls[0][1]
    if os.name == "nt":
        flags = int(kwargs.get("creationflags") or 0)
        expected_flag = int(getattr(process_launch.subprocess, "CREATE_NO_WINDOW", 0) or 0)
        if expected_flag and not (flags & expected_flag):
            return False, f"popen_hidden не добавил CREATE_NO_WINDOW: flags={flags}"
        if kwargs.get("startupinfo") is None:
            return False, "popen_hidden не добавил startupinfo для скрытия окна"
    else:
        if "creationflags" in kwargs or "startupinfo" in kwargs:
            return False, f"popen_hidden добавил Windows-параметры вне Windows: {kwargs}"
    return True, "ok"


def _check_archive_first_load_does_not_spawn_process(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime
    from types import SimpleNamespace

    import subprocess as subprocess_module
    from PySide6.QtWidgets import QApplication

    from rem_card.ui.doctor_view.archive_widget import ArchiveWidget
    from rem_card.ui.shared.patient_archive_dialog import PatientArchiveDialog

    _ = temp_root
    app = QApplication.instance() or QApplication([])

    class FakePatientService:
        def __init__(self):
            self.archive_calls = 0

        def get_archived_patients(self, start_dt=None, end_dt=None):
            self.archive_calls += 1
            _ = start_dt, end_dt
            return []

        def get_archive_db_paths_for_period(self, start_dt, end_dt):
            _ = start_dt, end_dt
            return []

    class FakeRemCardService:
        def __init__(self):
            self.card_date_calls = 0

        def get_all_card_dates(self, patient_id):
            self.card_date_calls += 1
            _ = patient_id
            return [datetime(2026, 5, 3, 8, 0)]

    class FakePatient(SimpleNamespace):
        def get_display_name(self):
            return "Иванов Иван"

    popen_calls = []
    original_popen = subprocess_module.Popen

    def fake_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        raise AssertionError(f"Первое открытие архива не должно запускать внешний процесс: {args}")

    archive_widget = None
    card_dialog = None
    subprocess_module.Popen = fake_popen
    try:
        patient_service = FakePatientService()
        archive_widget = ArchiveWidget(patient_service, remcard_service=object())
        archive_widget.load_data()
        deadline = time.monotonic() + 3.0
        while getattr(archive_widget, "_load_worker", None) is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        app.processEvents()
        if patient_service.archive_calls != 1:
            return False, f"ArchiveWidget load_data не вызвал сервис ровно один раз: {patient_service.archive_calls}"
        if getattr(archive_widget, "_load_worker", None) is not None:
            return False, "ArchiveWidget load_data не завершился"

        remcard_service = FakeRemCardService()
        patient = FakePatient(id=1)
        card_dialog = PatientArchiveDialog(remcard_service, patient)
        deadline = time.monotonic() + 3.0
        while getattr(card_dialog.card_list_widget, "_load_worker", None) is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        app.processEvents()
        if remcard_service.card_date_calls != 1:
            return False, f"PatientArchiveDialog не загрузил список карт один раз: {remcard_service.card_date_calls}"
    finally:
        subprocess_module.Popen = original_popen
        if card_dialog is not None:
            card_dialog.close()
        if archive_widget is not None:
            archive_widget.close()

    if popen_calls:
        return False, f"первое открытие архива запустило процесс: {popen_calls}"
    return True, "ok"


def _check_doctor_create_card_enqueue_error_refreshes(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime, timedelta
    from types import MethodType, SimpleNamespace

    from PySide6.QtWidgets import QApplication

    from rem_card.ui.doctor_view import doctor_remcard_widget as doctor_module
    from rem_card.ui.doctor_view.doctor_remcard_widget import DoctorRemCardWidget

    _ = temp_root
    app = QApplication.instance() or QApplication([])

    class FakeButton:
        def __init__(self):
            self.enabled = True

        def setEnabled(self, enabled):
            self.enabled = bool(enabled)

    class FakeStatusService:
        def __init__(self):
            self.ensure_calls = 0

        def ensure_initial_status(self, admission_id, start, admission_datetime):
            self.ensure_calls += 1

    class FakeService:
        def __init__(self):
            self.status_service = FakeStatusService()
            self.enqueue_called = False
            self.operation = None
            self.on_error = None

        def get_day_period(self, now):
            start = now.replace(hour=8, minute=0, second=0, microsecond=0)
            return start, start + timedelta(days=1)

        def get_patient(self, admission_id):
            return SimpleNamespace(admission_datetime=datetime(2026, 5, 3, 8, 0))

        def enqueue_write(self, description, operation, on_success=None, on_error=None, write_metadata=None):
            self.enqueue_called = True
            self.description = description
            self.operation = operation
            self.on_error = on_error

        def add_vital(self, dto, shift_date=None, force=False):
            raise AssertionError("queued create-card write should not run in UI thread")

    class FakeLayoutManager:
        def __init__(self):
            self.sector_4v = SimpleNamespace(btn_new_card=FakeButton())
            self.status_refreshes = 0

        def refresh_current_status(self):
            self.status_refreshes += 1

    warnings: list[str] = []
    original_warning = doctor_module.CustomMessageBox.warning
    original_information = doctor_module.CustomMessageBox.information
    doctor_module.CustomMessageBox.warning = lambda parent, title, message: warnings.append(f"{title}: {message}")
    doctor_module.CustomMessageBox.information = lambda *args, **kwargs: None
    try:
        service = FakeService()
        layout_manager = FakeLayoutManager()
        widget = SimpleNamespace(
            _archive_read_only_mode=False,
            _create_card_write_pending=False,
            _snapshot_worker=None,
            _create_card_after_snapshot=False,
            _snapshot_pending=None,
            _card_snapshot_cache={},
            admission_id=1,
            service=service,
            layout_manager=layout_manager,
            refresh_calls=0,
        )
        widget._current_status_is_outcome = MethodType(DoctorRemCardWidget._current_status_is_outcome, widget)
        widget._begin_create_card_pending = MethodType(DoctorRemCardWidget._begin_create_card_pending, widget)
        widget._finish_create_card_pending = MethodType(DoctorRemCardWidget._finish_create_card_pending, widget)
        widget._set_create_card_controls_enabled = MethodType(
            DoctorRemCardWidget._set_create_card_controls_enabled,
            widget,
        )
        widget.force_reload_all = lambda: setattr(widget, "refresh_calls", widget.refresh_calls + 1)
        widget.update_patient_info = lambda: None
        widget._show_read_only_hint = lambda: None

        DoctorRemCardWidget.on_create_card_clicked(widget)
        app.processEvents()
        if not service.enqueue_called:
            return False, "DoctorRemCardWidget did not use enqueue_write for create-card"
        if not widget._create_card_write_pending:
            return False, "DoctorRemCardWidget did not enter create-card pending state"
        if layout_manager.sector_4v.btn_new_card.enabled:
            return False, "DoctorRemCardWidget did not disable create-card button while pending"
        if service.status_service.ensure_calls:
            return False, "create-card write operation ran before queued worker callback"
        if not service.on_error:
            return False, "DoctorRemCardWidget did not register create-card error callback"

        service.on_error(RuntimeError("forced create-card failure"))
        app.processEvents()
        if widget._create_card_write_pending:
            return False, "DoctorRemCardWidget kept create-card pending state after error"
        if not layout_manager.sector_4v.btn_new_card.enabled:
            return False, "DoctorRemCardWidget did not re-enable create-card button after error"
        if widget.refresh_calls != 1:
            return False, "DoctorRemCardWidget did not refresh after create-card error"
        if not warnings or "forced create-card failure" not in warnings[-1]:
            return False, f"DoctorRemCardWidget did not show create-card error: {warnings}"
        return True, "ok"
    finally:
        doctor_module.CustomMessageBox.warning = original_warning
        doctor_module.CustomMessageBox.information = original_information


def _check_doctor_archive_outcome_blocks_new_card_before_snapshot(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime
    from types import MethodType, SimpleNamespace

    from rem_card.data.dto.remcard_dto import PatientStatus, PatientStatusEventDTO
    from rem_card.ui.doctor_view.doctor_remcard_widget import DoctorRemCardWidget

    _ = temp_root

    class DummyButton:
        def __init__(self):
            self.enabled = True

        def setEnabled(self, enabled):
            self.enabled = bool(enabled)

    button = DummyButton()
    status = PatientStatusEventDTO(
        admission_id=1,
        status=PatientStatus.DEAD,
        start_time=datetime(2026, 5, 3, 12, 0),
    )
    widget = SimpleNamespace(
        _card_snapshot_cache=None,
        layout_manager=SimpleNamespace(
            _current_status_dto=status,
            sector_4v=SimpleNamespace(btn_new_card=button),
        ),
        admission_id=1,
        service=SimpleNamespace(get_current_status=lambda _admission_id: None),
    )
    widget._current_status_is_outcome = MethodType(DoctorRemCardWidget._current_status_is_outcome, widget)
    widget._set_create_card_controls_enabled = MethodType(
        DoctorRemCardWidget._set_create_card_controls_enabled,
        widget,
    )

    if not widget._current_status_is_outcome():
        return False, "outcome status from layout was not detected before snapshot"
    widget._set_create_card_controls_enabled(True)
    if button.enabled:
        return False, "new-card button stayed enabled for outcome before snapshot"
    return True, "ok"


def _check_patient_status_error_refreshes_checked_state(temp_root: str) -> tuple[bool, str]:
    from .orders import _wait_for_movement_snapshot
    from datetime import datetime

    from PySide6.QtWidgets import QApplication

    from rem_card.data.dto.remcard_dto import PatientStatus, PatientStatusEventDTO
    from rem_card.ui.rem_card_sectors import sector_events as events_module
    from rem_card.ui.rem_card_sectors.sector_events import SectorEvents

    _ = temp_root
    app = QApplication.instance() or QApplication([])

    class FakeStatusService:
        def __init__(self):
            self.enqueue_called = False
            self.on_error = None
            self.reads = 0

        def get_events(self, admission_id):
            self.reads += 1
            return [
                PatientStatusEventDTO(
                    id=1,
                    admission_id=admission_id,
                    status=PatientStatus.ACTIVE,
                    start_time=datetime(2026, 5, 3, 8, 0),
                    created_by="USER",
                )
            ]

        def get_movement_snapshot(self, admission_id, shift_start, shift_end):
            events = self.get_events(admission_id)
            return {
                "admission_id": admission_id, "events": events, "version": self.reads,
                "is_archive": False, "late_state": {}, "total_events": len(events),
                "current_status": events[0],
            }

        def get_latest_change_id(self, admission_id=None, include_global=True):
            return self.reads

        def enqueue_change_status(
            self,
            admission_id,
            new_status,
            reason_type=None,
            reason_text=None,
            user_id=None,
            on_success=None,
            on_error=None,
        ):
            self.enqueue_called = True
            self.on_error = on_error

    warnings: list[str] = []
    original_warning = events_module.CustomMessageBox.warning
    events_module.CustomMessageBox.warning = lambda parent, title, message: warnings.append(f"{title}: {message}")
    widget = SectorEvents()
    service = FakeStatusService()
    try:
        widget.set_patient(1, service)
        _wait_for_movement_snapshot(widget, app)
        if not widget.btn_active.isChecked() or widget.btn_out.isChecked():
            return False, "initial status buttons did not reflect active DB status"

        widget.btn_out.setChecked(True)
        widget.on_status_btn_clicked(PatientStatus.OUT)
        app.processEvents()
        if not service.enqueue_called or not service.on_error:
            return False, "SectorEvents did not enqueue status change"
        if widget.btn_out.isChecked() or not widget.btn_active.isChecked():
            return False, "SectorEvents showed final status before commit"
        if widget.content_area.isEnabled():
            return False, "SectorEvents did not enter pending disabled state"

        service.on_error(RuntimeError("forced status failure"))
        _wait_for_movement_snapshot(widget, app)
        if not widget.content_area.isEnabled():
            return False, "SectorEvents did not re-enable after status write error"
        if not widget.btn_active.isChecked() or widget.btn_out.isChecked():
            return False, "SectorEvents did not refresh/rollback checked state after error"
        if service.reads < 2:
            return False, "SectorEvents did not refresh from DB/service after status write error"
        if not warnings or "forced status failure" not in warnings[-1]:
            return False, f"SectorEvents did not show status write error: {warnings}"
        return True, "ok"
    finally:
        events_module.CustomMessageBox.warning = original_warning
        widget.close()


def _assert_stale_snapshot_preserves_cell_delete_draft(doctor_widget, doctor_model, index, shift, doctor_order):
    from PySide6.QtCore import Qt

    stale_snapshot = {
        "admission_id": 1,
        "shift_date": shift,
        "only_committed": False,
        "orders": [doctor_order],
        "admin_rows": [
            {
                "id": 10,
                "order_id": 1,
                "big_chain_id": None,
                "cell_role": "single",
                "planned_time": doctor_model.time_slots[0].isoformat(),
                "actual_time": None,
                "performer_id": None,
                "status": "planned",
                "is_committed": 1,
                "comment": "",
                "volume_ml": 0.0,
                "updated_at": "2026-05-03 08:00:00.000",
                "last_modified_by": "doctor",
            }
        ],
        "has_any_draft": False,
        "has_any_administrations": True,
        "has_any_orders": True,
        "change_id": 1,
        "source": "refresh",
        "load_trace_id": "regression-stale-no-draft",
    }
    if not doctor_widget._apply_snapshot_data(snapshot=stale_snapshot, admission_id=1, shift_date=shift):
        return False, "doctor committed cell delete stale snapshot was rejected instead of guarded"
    guarded_admin = doctor_model.data(index, Qt.UserRole)
    if guarded_admin is None or guarded_admin.status != "deleted" or not doctor_widget.has_drafts():
        return False, "stale no-draft snapshot cleared committed cell delete draft state"
    return True, "ok"


def _assert_committed_long_infusion_delete_marks_draft(doctor_widget, doctor_model, long_index, long_order):
    from rem_card.data.dto.remcard_dto import AdministrationDTO

    long_chain_id = "long-committed-chain"
    committed_chain = []
    for offset, role in enumerate(("start", "body", "end")):
        slot = doctor_model.time_slots[offset]
        admin_row = AdministrationDTO(
            id=30 + offset,
            order_id=3,
            planned_time=slot,
            status="planned",
            cell_role=role,
            big_chain_id=long_chain_id,
            is_committed=1,
            comment="",
        )
        doctor_model.admin_map[(3, slot.isoformat())] = admin_row
        committed_chain.append(admin_row)

    doctor_widget._draft_baseline_admin_map.update(
        {
            (3, admin_row.planned_time.isoformat()): admin_row
            for admin_row in committed_chain
        }
    )

    doctor_model.has_any_draft = False
    doctor_widget._cached_has_drafts = False
    committed_long_delete = doctor_widget._apply_optimistic_cell(
        long_index,
        long_order,
        committed_chain[0],
        doctor_model.time_slots[0],
        "orders_left_click",
    )
    deleted_roles = [
        getattr(doctor_model.admin_map.get((3, doctor_model.time_slots[offset].isoformat())), "status", None)
        for offset in range(3)
    ]
    if deleted_roles != ["deleted", "deleted", "deleted"]:
        return False, f"committed long infusion delete did not tombstone all cells: {deleted_roles}"
    if not doctor_widget.has_drafts():
        return False, "committed long infusion delete did not activate save draft state"

    doctor_widget._restore_admin_cells(committed_long_delete)
    if any(
        getattr(doctor_model.admin_map.get((3, doctor_model.time_slots[offset].isoformat())), "status", None) != "planned"
        for offset in range(3)
    ):
        return False, "committed long infusion tombstones were not restored on error"
    return True, "ok"


def _assert_orders_same_cell_fast_click_guard(
    *,
    base_service_cls,
    orders_widget_cls,
    orders_model_cls,
    order_dto_cls,
    qt,
    shift,
) -> tuple[bool, str]:
    import time

    from rem_card.ui.doctor_view.orders_widget import ORDERS_CELL_REPEAT_GUARD_SEC

    class DeferredOrdersService(base_service_cls):
        def __init__(self):
            super().__init__()
            self.queued_writes = []
            self.left_click_calls = []

        def enqueue_write(self, description, operation, on_success=None, on_error=None, write_metadata=None):
            self.queued_writes.append(
                {
                    "description": description,
                    "operation": operation,
                    "on_success": on_success,
                    "on_error": on_error,
                }
            )

        def apply_order_left_click(self, order, admin, planned_time):
            self.left_click_calls.append(
                (
                    int(getattr(order, "id", 0) or 0),
                    planned_time.isoformat(),
                    str(getattr(admin, "status", "") or ""),
                )
            )

    deferred_service = DeferredOrdersService()
    widget = orders_widget_cls(service=deferred_service, admission_id=1, shift_date=shift, defer_ui=True)
    try:
        model = orders_model_cls(deferred_service, admission_id=1, shift_date=shift)
        order = order_dto_cls(id=71, admission_id=1, latin="Debounce", is_committed=1)
        model.orders = [order]
        widget.model = model
        index = model.index(0, 1)
        slot = model.time_slots[0]

        widget._handle_cell_action(
            index,
            "orders_left_click",
            deferred_service.apply_order_left_click,
        )
        pending_admin = model.data(index, qt.UserRole)
        if deferred_service.queued_writes:
            return False, f"local draft click must not enqueue writes: {len(deferred_service.queued_writes)}"
        if pending_admin is None or pending_admin.status != "planned":
            return False, f"first click did not leave planned optimistic cell: {pending_admin}"

        widget._handle_cell_action(
            index,
            "orders_left_click",
            deferred_service.apply_order_left_click,
        )
        if deferred_service.queued_writes:
            return False, "repeat local click unexpectedly enqueued a write"
        pending_admin = model.data(index, qt.UserRole)
        if pending_admin is None or pending_admin.status != "planned":
            return False, "second click on pending cell removed optimistic value"

        if deferred_service.left_click_calls:
            return False, f"local draft click called persistence service: {deferred_service.left_click_calls}"

        widget._handle_cell_action(
            index,
            "orders_left_click",
            deferred_service.apply_order_left_click,
        )
        if deferred_service.queued_writes:
            return False, "repeat-click debounce must suppress immediate post-success click"

        cell_key = widget._admin_cell_write_key(order.id, slot)
        widget._recent_admin_cell_clicks[cell_key] = time.monotonic() - ORDERS_CELL_REPEAT_GUARD_SEC - 0.05
        widget._handle_cell_action(
            index,
            "orders_left_click",
            deferred_service.apply_order_left_click,
        )
        if deferred_service.queued_writes:
            return False, "later local click unexpectedly enqueued a write"
        if model.data(index, qt.UserRole) is not None:
            return False, "later local click should toggle the cell back to baseline"
        if widget.has_drafts():
            return False, "toggle back to baseline must collapse the local draft to a no-op"
    finally:
        widget.close()

    return True, "ok"


def _check_doctor_order_mark_cycle(
    doctor_widget,
    doctor_model,
    index,
    committed_admin,
    service,
    *,
    qt,
    executed_mark: str,
    not_executed_mark: str,
) -> tuple[bool, str]:
    committed_admin.comment = ""
    doctor_model.admin_map[(1, doctor_model.time_slots[0].isoformat())] = committed_admin
    doctor_widget._cached_has_drafts = False
    doctor_model.has_any_draft = False
    service.mark_calls.clear()
    doctor_widget._handle_doctor_order_mark(index)
    marked_admin = doctor_model.data(index, qt.UserRole)
    if getattr(marked_admin, "comment", "") != executed_mark:
        return False, "doctor right click did not mark cell as executed"
    doctor_widget._handle_doctor_order_mark(index)
    marked_admin = doctor_model.data(index, qt.UserRole)
    if getattr(marked_admin, "comment", "") != not_executed_mark:
        return False, "doctor right click did not switch executed mark to not executed"
    doctor_widget._handle_doctor_order_mark(index)
    marked_admin = doctor_model.data(index, qt.UserRole)
    if getattr(marked_admin, "comment", ""):
        return False, "doctor right click did not clear not executed mark"
    expected_calls = [
        ("set", 10, executed_mark),
        ("set", 10, not_executed_mark),
        ("cancel", 10, ""),
    ]
    if service.mark_calls != expected_calls:
        return False, f"doctor right click service calls mismatch: {service.mark_calls}"
    if doctor_widget.has_drafts():
        return False, "doctor order mark must not create a prescription draft"
    return True, "ok"


def _check_doctor_long_infusion_pending(doctor_widget, doctor_model, order_dto_cls) -> tuple[bool, str]:
    long_order = order_dto_cls(id=3, admission_id=1, latin="Long", is_committed=1, duration_min=180)
    doctor_model.orders.append(long_order)
    long_index = doctor_model.index(1, 1)
    long_previous = doctor_widget._apply_optimistic_cell(
        long_index,
        long_order,
        None,
        doctor_model.time_slots[0],
        "orders_left_click",
    )
    actual_roles = [
        getattr(doctor_model.admin_map.get((3, doctor_model.time_slots[offset].isoformat())), "cell_role", None)
        for offset in range(3)
    ]
    if actual_roles != ["start", "body", "end"]:
        return False, f"long infusion optimistic roles mismatch: {actual_roles}"
    if not all(
        getattr(doctor_model.admin_map[(3, doctor_model.time_slots[offset].isoformat())], "_pending_cell_action", None)
        for offset in range(3)
    ):
        return False, "long infusion optimistic cells did not keep pending markers"
    doctor_widget._restore_admin_cells(long_previous)
    if any(key[0] == 3 for key in doctor_model.admin_map):
        return False, "long infusion optimistic state was not restored on error"
    ok, details = _assert_committed_long_infusion_delete_marks_draft(
        doctor_widget,
        doctor_model,
        long_index,
        long_order,
    )
    doctor_model.orders.pop()
    return (ok, details) if not ok else (True, "ok")


def _check_nurse_pending_mark_cycle(
    nurse_widget,
    service,
    *,
    orders_model_cls,
    order_dto_cls,
    administration_dto_cls,
    shift,
    qt,
    executed_mark: str,
) -> tuple[bool, str]:
    nurse_model = orders_model_cls(service, admission_id=1, shift_date=shift)
    nurse_order = order_dto_cls(id=2, admission_id=1, latin="Nurse")
    nurse_model.orders = [nurse_order]
    nurse_slot = nurse_model.time_slots[0]
    nurse_admin = administration_dto_cls(
        id=20,
        order_id=2,
        planned_time=nurse_slot,
        status="planned",
        cell_role="single",
        comment="",
    )
    nurse_model.admin_map[(2, nurse_slot.isoformat())] = nurse_admin
    nurse_widget.model = nurse_model
    nurse_index = nurse_model.index(0, 1)

    nurse_widget._apply_pending_nurse_mark(nurse_index, nurse_admin, executed_mark)
    pending_admin = nurse_model.data(nurse_index, qt.UserRole)
    if getattr(pending_admin, "comment", ""):
        return False, "nurse mark became final before commit"
    if not hasattr(pending_admin, "_pending_mark"):
        return False, "nurse mark did not enter pending state"

    nurse_widget._apply_committed_nurse_mark(nurse_index, nurse_admin, executed_mark)
    committed_admin = nurse_model.data(nurse_index, qt.UserRole)
    if getattr(committed_admin, "comment", "") != executed_mark:
        return False, "nurse mark did not become final after success"
    if hasattr(committed_admin, "_pending_mark"):
        return False, "nurse pending marker remained after success"
    return True, "ok"


def _check_orders_pending_states_before_commit(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime, timedelta

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from rem_card.data.dto.remcard_dto import AdministrationDTO, OrderDTO
    from rem_card.ui.doctor_view.orders_widget import OrdersWidget
    from rem_card.ui.nurse_view.components.nurse_orders_widget import NurseOrdersWidget
    from rem_card.ui.shared.orders_model import OrdersModel
    from rem_card.services.order_domain_service import NURSE_MARK_EXECUTED, NURSE_MARK_NOT_EXECUTED

    _ = temp_root
    app = QApplication.instance() or QApplication([])

    class FakeOrdersService:
        def __init__(self):
            self.mark_calls = []

        def get_day_period(self, shift_date):
            start = shift_date.replace(hour=8, minute=0, second=0, microsecond=0)
            return start, start + timedelta(days=1)

        def enqueue_write(self, description, operation, on_success=None, on_error=None, write_metadata=None):
            try:
                result = operation()
            except Exception as exc:
                if on_error:
                    on_error(exc)
                return
            if on_success:
                on_success(result)

        def set_doctor_order_mark(self, admin_id: int, mark: str, *, expected_version=None):
            self.mark_calls.append(("set", int(admin_id), mark))

        def cancel_doctor_order_mark(self, admin_id: int, *, expected_version=None):
            self.mark_calls.append(("cancel", int(admin_id), ""))

    shift = datetime(2026, 5, 3, 8, 0)
    service = FakeOrdersService()
    doctor_widget = OrdersWidget(service=service, admission_id=1, shift_date=shift, defer_ui=True)
    nurse_widget = NurseOrdersWidget(service=service, admission_id=1, shift_date=shift, defer_ui=True)
    try:
        doctor_model = OrdersModel(service, admission_id=1, shift_date=shift)
        doctor_order = OrderDTO(id=1, admission_id=1, latin="Test", is_committed=1)
        doctor_model.orders = [doctor_order]
        doctor_widget.model = doctor_model

        doctor_widget._mark_local_order_row_deleted(0, doctor_order, was_committed=True)
        app.processEvents()
        if len(doctor_model.orders) != 0:
            return False, "doctor order row did not disappear from the local overlay"
        deleted_entry = doctor_widget._local_deleted_orders.get(1)
        if not deleted_entry or not getattr(deleted_entry[1], "_pending_delete", False):
            return False, "doctor order tombstone was not retained for Save"
        doctor_widget._clear_local_order_row_pending_delete(1)
        if len(doctor_model.orders) != 1 or getattr(doctor_model.orders[0], "_pending_delete", False):
            return False, "doctor order row was not restored after local delete rollback"

        index = doctor_model.index(0, 1)
        pending = doctor_widget._apply_pending_cell(index, doctor_order, None, doctor_model.time_slots[0], "orders_left_click")
        admin = doctor_model.data(index, Qt.UserRole)
        if not pending:
            return False, "doctor order cell did not capture previous state"
        if admin is None or not getattr(admin, "_pending_cell_action", None):
            return False, "doctor order cell did not show pending state before commit"
        doctor_widget._restore_admin_cells(pending)
        if doctor_model.data(index, Qt.UserRole) is not None:
            return False, "doctor order cell pending state was not restored on error"

        optimistic = doctor_widget._apply_optimistic_cell(
            index,
            doctor_order,
            None,
            doctor_model.time_slots[0],
            "orders_left_click",
        )
        admin = doctor_model.data(index, Qt.UserRole)
        if not optimistic:
            return False, "doctor order cell did not capture optimistic previous state"
        if admin is None or admin.status != "planned" or admin.cell_role != "single":
            return False, "doctor order cell did not show final mark immediately"
        if not getattr(admin, "_pending_cell_action", None):
            return False, "doctor order optimistic mark did not keep pending marker"
        doctor_widget._restore_admin_cells(optimistic)
        if doctor_model.data(index, Qt.UserRole) is not None:
            return False, "doctor order optimistic state was not restored on error"

        ok, details = _assert_orders_same_cell_fast_click_guard(
            base_service_cls=FakeOrdersService,
            orders_widget_cls=OrdersWidget,
            orders_model_cls=OrdersModel,
            order_dto_cls=OrderDTO,
            qt=Qt,
            shift=shift,
        )
        if not ok:
            return False, details

        committed_admin = AdministrationDTO(
            id=10,
            order_id=1,
            planned_time=doctor_model.time_slots[0],
            status="planned",
            cell_role="single",
            is_committed=1,
            comment="",
        )
        committed_key = (1, doctor_model.time_slots[0].isoformat())
        doctor_model.admin_map[committed_key] = committed_admin
        doctor_widget._draft_baseline_admin_map = {committed_key: committed_admin}
        doctor_model.has_any_draft = False
        doctor_widget._cached_has_drafts = False
        repaint_events = []
        doctor_model.dataChanged.connect(
            lambda top_left, bottom_right, _roles: repaint_events.append(
                (top_left.row(), top_left.column(), bottom_right.row(), bottom_right.column())
            )
        )
        committed_delete = doctor_widget._apply_optimistic_cell(
            index,
            doctor_order,
            committed_admin,
            doctor_model.time_slots[0],
            "orders_left_click",
        )
        deleted_admin = doctor_model.data(index, Qt.UserRole)
        if not committed_delete:
            return False, "doctor committed cell delete did not capture previous state"
        if deleted_admin is None or deleted_admin.status != "deleted" or int(deleted_admin.is_committed or 0) != 0:
            return False, f"doctor committed cell delete did not create draft tombstone: {deleted_admin}"
        if not doctor_widget.has_drafts():
            return False, "doctor committed cell delete did not activate save draft state"
        if not any(left_col <= 0 <= right_col for _top, left_col, _bottom, right_col in repaint_events):
            return False, f"doctor draft-state change did not repaint order column: {repaint_events}"
        if not any(left_col <= index.column() <= right_col for _top, left_col, _bottom, right_col in repaint_events):
            return False, f"doctor cell change did not repaint target cell: {repaint_events}"
        ok, details = _assert_stale_snapshot_preserves_cell_delete_draft(
            doctor_widget,
            doctor_model,
            index,
            shift,
            doctor_order,
        )
        if not ok:
            return False, details
        doctor_widget._restore_admin_cells(committed_delete)
        if doctor_model.data(index, Qt.UserRole) != committed_admin:
            return False, "doctor committed cell tombstone was not restored on error"

        snapshot_model = OrdersModel(service, admission_id=1, shift_date=shift)
        snapshot_order = OrderDTO(id=90, admission_id=1, latin="Snapshot", is_committed=1)
        snapshot_model.orders = [snapshot_order]
        snapshot_events = []
        snapshot_model.dataChanged.connect(
            lambda top_left, bottom_right, _roles: snapshot_events.append(
                (top_left.row(), top_left.column(), bottom_right.row(), bottom_right.column())
            )
        )
        snapshot = {
            "admission_id": 1,
            "shift_date": shift,
            "only_committed": False,
            "orders": [snapshot_order],
            "admin_rows": [
                {
                    "id": 900,
                    "order_id": 90,
                    "planned_time": snapshot_model.time_slots[0].isoformat(),
                    "actual_time": None,
                    "cell_role": "single",
                    "status": "planned",
                    "is_committed": 0,
                    "comment": "",
                    "volume_ml": 0.0,
                    "updated_at": "2026-05-03 08:00:00.000",
                }
            ],
            "has_any_draft": True,
        }
        if not snapshot_model.apply_admin_rows_snapshot(snapshot):
            return False, "admin-only snapshot was not applied to matching order model"
        if not any(left_col <= 0 <= right_col for _top, left_col, _bottom, right_col in snapshot_events):
            return False, f"admin-only draft-state change did not repaint order column: {snapshot_events}"

        ok, details = _check_doctor_order_mark_cycle(
            doctor_widget,
            doctor_model,
            index,
            committed_admin,
            service,
            qt=Qt,
            executed_mark=NURSE_MARK_EXECUTED,
            not_executed_mark=NURSE_MARK_NOT_EXECUTED,
        )
        if not ok:
            return False, details
        ok, details = _check_doctor_long_infusion_pending(
            doctor_widget,
            doctor_model,
            OrderDTO,
        )
        if not ok:
            return False, details
        return _check_nurse_pending_mark_cycle(
            nurse_widget,
            service,
            orders_model_cls=OrdersModel,
            order_dto_cls=OrderDTO,
            administration_dto_cls=AdministrationDTO,
            shift=shift,
            qt=Qt,
            executed_mark=NURSE_MARK_EXECUTED,
        )
    finally:
        doctor_widget.close()
        nurse_widget.close()

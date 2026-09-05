"""Safety-сценарии: ui."""

from __future__ import annotations

from .common import PROJECT_ROOT
from pathlib import Path
from .common import _cached_source_segment
import ast
import json
import os
import subprocess
import sys
import textwrap


def _check_doctor_orders_late_model_binding(temp_root: str) -> tuple[bool, str]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from datetime import datetime, timedelta

    from PySide6.QtCore import QObject
    from PySide6.QtWidgets import QApplication

    from rem_card.data.dto.remcard_dto import OrderDTO, OrderStatus, OrderType
    from rem_card.ui.doctor_view.orders_widget import OrdersWidget

    class DummyOrdersService(QObject):
        def get_day_period(self, shift_date):
            start = shift_date.replace(hour=8, minute=0, second=0, microsecond=0)
            return start, start + timedelta(hours=24)

    app = QApplication.instance() or QApplication([])
    service = DummyOrdersService()
    widget = OrdersWidget(service=service, admission_id=1, shift_date=datetime(2026, 4, 24, 12), defer_ui=True)
    try:
        widget._ensure_model_initialized()
        if widget.model is None:
            return False, "model was not initialized before UI setup"
        widget.model.orders = [object()]

        widget.setup_ui()
        widget.show()
        app.processEvents()

        if widget.table_view.model() is not widget.model:
            return False, "late-created table did not bind existing orders model"
        if widget.table_view.verticalHeader().count() != 1:
            return False, f"table header row count mismatch: {widget.table_view.verticalHeader().count()}"
        if widget.table_view.rowHeight(0) <= 0:
            return False, f"first row is collapsed: height={widget.table_view.rowHeight(0)}"

        draft_events = []
        widget.draftStatusChanged.connect(lambda active: draft_events.append(bool(active)))
        order = OrderDTO(
            id=10,
            admission_id=1,
            drug_key="local_delete_probe",
            latin="Local Delete Probe",
            type=OrderType.MEDICATION,
            status=OrderStatus.ACTIVE,
            is_committed=1,
            created_at=datetime(2026, 4, 24, 9),
        )
        widget.model.orders = [order]
        widget.model.admin_map = {}
        widget.model.has_any_draft = False
        widget._cached_has_drafts = False
        widget._mark_local_order_row_deleted(0, order, was_committed=True)
        if not widget.has_drafts() or not draft_events or draft_events[-1] is not True:
            return False, "local row delete did not emit active draft state"
        if widget.model.rowCount() != 0 or int(order.id) not in widget._local_deleted_orders:
            return False, "local row delete did not hide the row while retaining its tombstone"
        return True, "ok"
    finally:
        widget.close()


def _check_orders_widget_skips_duplicate_snapshot(temp_root: str) -> tuple[bool, str]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from datetime import datetime, timedelta

    from PySide6.QtCore import QObject
    from PySide6.QtWidgets import QApplication

    from rem_card.data.dto.remcard_dto import OrderDTO, OrderStatus, OrderType
    from rem_card.services.read_coordinator import ReadCoordinator
    from rem_card.ui.doctor_view.orders_widget import OrdersWidget

    class DummyOrdersService(QObject):
        def get_day_period(self, shift_date):
            start = shift_date.replace(hour=8, minute=0, second=0, microsecond=0)
            return start, start + timedelta(hours=24)

    app = QApplication.instance() or QApplication([])
    shift_date = datetime(2026, 4, 24, 12)
    service = DummyOrdersService()
    service.read_coordinator = ReadCoordinator(service)
    widget = OrdersWidget(service=service, admission_id=1, shift_date=shift_date, defer_ui=True)
    try:
        widget._ensure_model_initialized()
        if widget.model is None:
            return False, "model was not initialized"
        context = service.read_coordinator.make_orders_context(
            source_db="live",
            admission_id=1,
            shift_date=shift_date,
            role="doctor",
            mode="live",
            variant="committed",
        )
        context_key = context.cache_key()
        context_hash = context.hash()

        original_apply_snapshot = widget.model.apply_snapshot
        apply_count = 0

        def counted_apply_snapshot(snapshot):
            nonlocal apply_count
            apply_count += 1
            return original_apply_snapshot(snapshot)

        widget.model.apply_snapshot = counted_apply_snapshot
        snapshot = {
            "admission_id": 1,
            "shift_date": shift_date,
            "only_committed": True,
            "orders": [
                OrderDTO(
                    id=10,
                    admission_id=1,
                    drug_key="duplicate_snapshot_probe",
                    latin="Duplicate Snapshot Probe",
                    type=OrderType.MEDICATION,
                    status=OrderStatus.ACTIVE,
                    is_committed=1,
                    created_at=datetime(2026, 4, 24, 9),
                )
            ],
            "admin_rows": [],
            "has_any_draft": False,
            "has_any_administrations": False,
            "has_any_orders": True,
            "change_id": 7,
            "version": 7,
            "context_hash": context_hash,
            "load_trace_id": "orders-duplicate-000001",
            "source": "refresh",
        }

        first_ok = widget._apply_snapshot_data(
            snapshot=snapshot,
            admission_id=1,
            shift_date=shift_date,
            context_key=context_key,
        )
        second_ok = widget._apply_snapshot_data(
            snapshot=snapshot,
            admission_id=1,
            shift_date=shift_date,
            context_key=context_key,
        )
        app.processEvents()

        if not first_ok or not second_ok:
            return False, f"snapshot apply returned first={first_ok} second={second_ok}"
        if apply_count != 1:
            return False, f"duplicate snapshot reset was not skipped, apply_count={apply_count}"
        if len(widget.model.orders) != 1:
            return False, f"unexpected model rows after duplicate skip: {len(widget.model.orders)}"

        previous_context = service.read_coordinator.make_orders_context(
            source_db="live",
            admission_id=7,
            shift_date=shift_date,
            role="doctor",
            mode="live",
            variant="committed",
        )
        current_context = service.read_coordinator.make_orders_context(
            source_db="live",
            admission_id=5,
            shift_date=shift_date,
            role="doctor",
            mode="live",
            variant="committed",
        )
        widget.admission_id = 5
        widget.shift_date = shift_date
        widget._last_polled_change_id = 49793
        widget._last_polled_context_key = previous_context.cache_key()
        widget._last_applied_snapshot_signature = None
        drift_snapshot = {
            "admission_id": 5,
            "shift_date": shift_date,
            "only_committed": True,
            "orders": [],
            "admin_rows": [],
            "has_any_draft": False,
            "has_any_administrations": False,
            "has_any_orders": False,
            "change_id": 49781,
            "version": 49781,
            "context_hash": current_context.hash(),
            "load_trace_id": "orders-context-drift",
            "source": "refresh",
        }
        drift_ok = widget._apply_snapshot_data(
            snapshot=drift_snapshot,
            admission_id=5,
            shift_date=shift_date,
            context_key=current_context.cache_key(),
        )
        if not drift_ok or widget._snapshot_stale:
            return False, "context-drift cursor caused stale snapshot loop"
        if int(widget._last_polled_change_id or 0) != 49781:
            return False, f"context-drift cursor was not reset: {widget._last_polled_change_id}"
        return True, "ok"
    finally:
        widget.close()


def _check_order_row_delete_without_times_marks_draft(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.data.dao.remcard_dao import FluidsDAO, OrdersDAO, PatientDAO, VentilationDAO, VitalsDAO
    from rem_card.data.dto.remcard_dto import OrderDTO, OrderStatus, OrderType
    from rem_card.services.remcard_service import RemCardService

    db_path = os.path.join(temp_root, "orders_no_times_delete.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        with manager.remcard_transaction(source="regression_seed_patient") as cursor:
            cursor.execute("INSERT INTO patients(full_name) VALUES (?)", ("Regression Patient",))
            patient_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO admissions(patient_id, bed_number, history_number, admission_datetime)
                VALUES (?, ?, ?, ?)
                """,
                (patient_id, 1, "REG-1", "2026-04-24T08:00:00"),
            )
            admission_id = int(cursor.lastrowid)

        service = RemCardService(
            VitalsDAO(manager),
            FluidsDAO(manager),
            OrdersDAO(manager),
            VentilationDAO(manager),
            PatientDAO(manager),
        )
        shift_date = datetime(2026, 4, 24, 12, 0, 0)
        order = OrderDTO(
            admission_id=admission_id,
            drug_key="regression_empty_schedule",
            latin="Regression Empty Schedule",
            type=OrderType.MEDICATION,
            status=OrderStatus.ACTIVE,
            dose_value=1.0,
            dose_unit="mg",
            is_per_kg=False,
            frequency=1,
            specific_times=[],
            duration_min=0,
            is_committed=0,
            created_at=datetime(2026, 4, 24, 9, 0, 0),
            comment="",
            last_modified_by="doctor",
        )

        service.add_order(order)
        if order.id is None:
            return False, "order insert did not return id"
        service.finalize_order_card(admission_id, shift_date=shift_date)

        saved_snapshot = service.build_orders_snapshot(admission_id, shift_date, only_committed=False)
        if len(saved_snapshot["orders"]) != 1 or saved_snapshot["has_any_draft"]:
            return False, f"unexpected saved snapshot: orders={len(saved_snapshot['orders'])}, draft={saved_snapshot['has_any_draft']}"
        if len(service.get_orders(admission_id, shift_date, only_committed=True)) != 1:
            return False, "saved no-time order is not visible to committed reader"

        service.soft_delete_order_row(order.id, True)
        deleted_snapshot = service.build_orders_snapshot(admission_id, shift_date, only_committed=False)
        if deleted_snapshot["orders"]:
            return False, "deleted no-time order is still visible in doctor snapshot"
        if not deleted_snapshot["has_any_draft"]:
            return False, "deleted no-time order did not mark doctor snapshot as draft"
        if not service.has_order_drafts(admission_id, shift_date):
            return False, "shift-scoped draft query missed deleted no-time order"
        if service.get_orders(admission_id, shift_date, only_committed=True):
            return False, "deleted no-time order is still visible to committed reader before save"

        service.finalize_order_card(admission_id, shift_date=shift_date)
        if service.has_order_drafts(admission_id, shift_date):
            return False, "draft flag remained after finalizing deleted no-time order"
        if service.get_orders(admission_id, shift_date, only_committed=False):
            return False, "deleted no-time order is visible to doctor after final save"
        if service.get_orders(admission_id, shift_date, only_committed=True):
            return False, "deleted no-time order is visible to committed reader after final save"
        return True, "ok"
    finally:
        manager.close()


def _check_orders_cell_delete_draft_and_noop_toggle(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from PySide6.QtCore import Qt

    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.data.dao.remcard_dao import FluidsDAO, OrdersDAO, PatientDAO, VentilationDAO, VitalsDAO
    from rem_card.data.dto.remcard_dto import OrderDTO, OrderStatus, OrderType
    from rem_card.services.order_domain_service import NURSE_MARK_EXECUTED
    from rem_card.services.read_coordinator import ReadCoordinator
    from rem_card.services.remcard_service import RemCardService
    from rem_card.ui.shared.orders_model import OrdersModel

    db_path = os.path.join(temp_root, "orders_cell_delete_draft.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        with manager.remcard_transaction(source="regression_seed_patient") as cursor:
            cursor.execute("INSERT INTO patients(full_name) VALUES (?)", ("Regression Patient",))
            patient_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO admissions(patient_id, bed_number, history_number, admission_datetime)
                VALUES (?, ?, ?, ?)
                """,
                (patient_id, 1, "REG-CELL", "2026-04-24T08:00:00"),
            )
            admission_id = int(cursor.lastrowid)

        service = RemCardService(
            VitalsDAO(manager),
            FluidsDAO(manager),
            OrdersDAO(manager),
            VentilationDAO(manager),
            PatientDAO(manager),
        )
        shift_date = datetime(2026, 4, 24, 12, 0, 0)
        order = OrderDTO(
            admission_id=admission_id,
            drug_key="regression_cell",
            latin="Regression Cell",
            type=OrderType.MEDICATION,
            status=OrderStatus.ACTIVE,
            dose_value=1.0,
            dose_unit="mg",
            is_per_kg=False,
            frequency=1,
            specific_times=[],
            duration_min=0,
            is_committed=0,
            created_at=datetime(2026, 4, 24, 9, 0, 0),
            comment="",
            last_modified_by="doctor",
        )
        service.add_order(order)
        service.finalize_order_card(admission_id, shift_date=shift_date)

        saved_slot = datetime(2026, 4, 24, 10, 0, 0)
        empty_slot = datetime(2026, 4, 24, 11, 0, 0)
        service.apply_order_left_click(order, None, saved_slot)
        service.finalize_order_card(admission_id, shift_date=shift_date)
        saved_snapshot = service.build_orders_snapshot(admission_id, shift_date, only_committed=False)
        if saved_snapshot["has_any_draft"]:
            return False, "saved baseline unexpectedly has drafts"
        baseline_rows = [
            dict(row)
            for row in saved_snapshot["admin_rows"]
            if int(dict(row).get("order_id") or 0) == int(order.id)
            and str(dict(row).get("planned_time") or "") == saved_slot.isoformat()
        ]
        if not baseline_rows:
            return False, "saved baseline committed cell row is missing"
        baseline_admin_id = int(baseline_rows[-1]["id"])

        service.apply_order_left_click(order, None, saved_slot)
        deleted_snapshot = service.build_orders_snapshot(admission_id, shift_date, only_committed=False)
        if not deleted_snapshot["has_any_draft"]:
            return False, "deleted saved cell did not keep draft flag"
        latest_deleted = [
            dict(row)
            for row in deleted_snapshot["admin_rows"]
            if int(dict(row).get("order_id") or 0) == int(order.id)
            and str(dict(row).get("planned_time") or "") == saved_slot.isoformat()
        ][-1]
        if latest_deleted.get("status") != "deleted" or int(latest_deleted.get("is_committed") or 0) != 0:
            return False, f"saved-cell delete did not produce uncommitted tombstone: {latest_deleted}"

        model = OrdersModel(service, admission_id=admission_id, shift_date=shift_date)
        model.apply_snapshot(deleted_snapshot)
        deleted_admin = model.data(model.index(0, 3), Qt.UserRole)
        if deleted_admin is None or deleted_admin.status != "deleted" or not model.has_any_draft:
            return False, "OrdersModel dropped deleted draft tombstone"

        try:
            service.set_nurse_order_mark(baseline_admin_id, NURSE_MARK_EXECUTED)
        except RuntimeError as exc:
            return False, f"nurse mark was blocked by unsaved doctor cell draft: {exc}"
        nurse_rows = service.get_nurse_orders_data(admission_id, shift_date)
        nurse_row = next((dict(row) for row in nurse_rows if int(dict(row).get("id") or 0) == baseline_admin_id), None)
        if nurse_row is None or nurse_row.get("comment") != NURSE_MARK_EXECUTED:
            return False, f"nurse mark did not apply to committed baseline during doctor draft: {nurse_rows}"

        coordinator = ReadCoordinator(service)
        context = coordinator.make_orders_context(
            source_db="live",
            admission_id=admission_id,
            shift_date=shift_date,
            role="doctor",
            mode="live",
            variant="full",
        )
        delta_snapshot = coordinator._change_log_applier.apply_orders_delta(
            context=context,
            base_snapshot=saved_snapshot,
            latest_change_id=service.get_latest_change_id(admission_id),
        )
        if not delta_snapshot.get("has_any_draft"):
            return False, "ReadCoordinator delta lost deleted draft flag"
        if not any(str(dict(row).get("status") or "") == "deleted" for row in delta_snapshot.get("admin_rows") or []):
            return False, "ReadCoordinator delta removed deleted tombstone row"

        service.apply_order_left_click(order, None, saved_slot)
        restored_snapshot = service.build_orders_snapshot(admission_id, shift_date, only_committed=False)
        if restored_snapshot["has_any_draft"]:
            return False, "delete-then-restore saved cell left a no-op draft"

        service.apply_order_left_click(order, None, empty_slot)
        if not service.has_order_drafts(admission_id, shift_date):
            return False, "new draft cell did not mark card dirty"
        service.apply_order_left_click(order, None, empty_slot)
        if service.has_order_drafts(admission_id, shift_date):
            return False, "quick add-then-remove empty cell left a no-op draft"
        empty_rows = [
            dict(row)
            for row in service.get_latest_administrations(
                admission_id=admission_id,
                shift_date=shift_date,
                only_committed=False,
                include_deleted=True,
                include_cancelled=True,
                include_deleted_orders=True,
            )
            if int(dict(row).get("order_id") or 0) == int(order.id)
            and str(dict(row).get("planned_time") or "") == empty_slot.isoformat()
        ]
        if empty_rows:
            return False, f"quick add-then-remove left effective rows: {empty_rows}"

        return True, "ok"
    finally:
        manager.close()


def _check_order_row_edit_updates_existing_order(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.data.dao.remcard_dao import FluidsDAO, OrdersDAO, PatientDAO, VentilationDAO, VitalsDAO
    from rem_card.data.dto.remcard_dto import OrderDTO, OrderStatus, OrderType
    from rem_card.services.order_service import OrderConflictError
    from rem_card.services.remcard_service import RemCardService

    db_path = os.path.join(temp_root, "orders_row_edit.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        with manager.remcard_transaction(source="regression_seed_patient") as cursor:
            cursor.execute("INSERT INTO patients(full_name) VALUES (?)", ("Regression Patient",))
            patient_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO admissions(patient_id, bed_number, history_number, admission_datetime)
                VALUES (?, ?, ?, ?)
                """,
                (patient_id, 1, "REG-ORDER-EDIT", "2026-04-24T08:00:00"),
            )
            admission_id = int(cursor.lastrowid)

        service = RemCardService(
            VitalsDAO(manager),
            FluidsDAO(manager),
            OrdersDAO(manager),
            VentilationDAO(manager),
            PatientDAO(manager),
        )
        shift_date = datetime(2026, 4, 24, 12, 0, 0)
        order = OrderDTO(
            admission_id=admission_id,
            drug_key="regression_edit_original",
            latin="Regression Original",
            type=OrderType.MEDICATION,
            status=OrderStatus.ACTIVE,
            dose_value=1.0,
            dose_unit="mg",
            is_per_kg=False,
            frequency=1,
            specific_times=[],
            duration_min=0,
            is_committed=0,
            created_at=datetime(2026, 4, 24, 9, 0, 0),
            comment="",
            last_modified_by="doctor",
        )
        service.add_order(order)
        service.apply_order_left_click(order, None, datetime(2026, 4, 24, 10, 0, 0))
        service.finalize_order_card(admission_id, shift_date=shift_date)

        baseline = next(item for item in service.get_orders(admission_id, shift_date) if item.id == order.id)
        noop_edit = OrderDTO(
            admission_id=admission_id,
            drug_key=baseline.drug_key,
            latin=baseline.latin,
            type=baseline.type,
            status=baseline.status,
            dose_value=baseline.dose_value,
            dose_unit=baseline.dose_unit,
            is_per_kg=baseline.is_per_kg,
            frequency=baseline.frequency,
            specific_times=list(baseline.specific_times or []),
            duration_min=baseline.duration_min,
            rate_ml_h=baseline.rate_ml_h,
            volume_total=baseline.volume_total,
            created_at=baseline.created_at,
            comment=baseline.comment,
            last_modified_by="doctor",
        )
        noop_result = service.update_order(order.id, noop_edit, expected_revision=baseline.revision)
        noop_snapshot = service.build_orders_snapshot(admission_id, shift_date, only_committed=False)
        if noop_result.id != order.id:
            return False, f"no-op edit changed order id: {noop_result.id}"
        if int(noop_result.is_committed or 0) != 1 or noop_snapshot["has_any_draft"]:
            return False, "no-op edit must not create an unsaved draft"

        edited = OrderDTO(
            admission_id=admission_id,
            drug_key="regression_edit_updated",
            latin="Regression Updated",
            type=OrderType.INFUSION_CONTINUOUS,
            status=OrderStatus.ACTIVE,
            dose_value=2.5,
            dose_unit="mg",
            is_per_kg=False,
            frequency=2,
            specific_times=["08:00", "20:00"],
            duration_min=30,
            is_committed=0,
            created_at=datetime(2026, 4, 24, 9, 30, 0),
            comment="S. NaCl 0.9% - 100мл [ROUTE:В/в капельно] [DUR:30]",
            last_modified_by="doctor",
        )
        service.update_order(order.id, edited, expected_revision=baseline.revision)

        draft_snapshot = service.build_orders_snapshot(admission_id, shift_date, only_committed=False)
        visible_orders = draft_snapshot["orders"]
        if [item.id for item in visible_orders] != [order.id]:
            return False, f"edit must keep the same visible order id, got {[item.id for item in visible_orders]}"
        updated_order = visible_orders[0]
        if updated_order.latin != "Regression Updated" or updated_order.dose_value != 2.5:
            return False, f"order fields were not updated: {updated_order}"
        if int(updated_order.is_committed or 0) != 0 or not draft_snapshot["has_any_draft"]:
            return False, "edited committed order must become an unsaved draft"

        active_rows = [
            dict(row)
            for row in draft_snapshot["admin_rows"]
            if int(dict(row).get("order_id") or 0) == int(order.id)
            and str(dict(row).get("planned_time") or "") == "2026-04-24T10:00:00"
            and str(dict(row).get("status") or "") == "planned"
        ]
        if not active_rows:
            return False, "edit detached or removed existing administration cells"

        try:
            service.update_order(order.id, edited, expected_revision=baseline.revision)
            return False, "stale order edit did not raise conflict"
        except OrderConflictError:
            pass

        latest = next(item for item in service.get_orders(admission_id, shift_date) if item.id == order.id)
        service.finalize_order_card(admission_id, shift_date=shift_date, expected_revisions={order.id: latest.revision})
        nurse_rows = service.get_nurse_orders_data(admission_id, shift_date)
        nurse_row = next((dict(row) for row in nurse_rows if int(dict(row).get("order_id") or 0) == int(order.id)), None)
        if nurse_row is None:
            return False, f"edited order disappeared from nurse read model: {nurse_rows}"
        if nurse_row.get("latin") != "Regression Updated" or float(nurse_row.get("dose_value") or 0) != 2.5:
            return False, f"nurse read model did not get edited order fields: {nurse_row}"

        source = (PROJECT_ROOT / "ui" / "doctor_view" / "orders_widget.py").read_text(encoding="utf-8")
        if "index.column() == 0 and event.button() == Qt.RightButton" not in source:
            return False, "doctor order column right click branch is missing"
        if "_open_order_edit_dialog(index)" not in source:
            return False, "doctor order column right click does not open edit dialog"

        return True, "ok"
    finally:
        manager.close()


def _check_orders_optimistic_lock_conflicts(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.data.dao.remcard_dao import FluidsDAO, OrdersDAO, PatientDAO, VentilationDAO, VitalsDAO
    from rem_card.data.dto.remcard_dto import OrderDTO, OrderStatus, OrderType
    from rem_card.services.order_service import ORDER_CONFLICT_MESSAGE, OrderConflictError
    from rem_card.services.remcard_service import RemCardService

    db_path = os.path.join(temp_root, "orders_optimistic_lock.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        with manager.remcard_transaction(source="regression_seed_patient") as cursor:
            cursor.execute("INSERT INTO patients(full_name) VALUES (?)", ("Regression Patient",))
            patient_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO admissions(patient_id, bed_number, history_number, admission_datetime)
                VALUES (?, ?, ?, ?)
                """,
                (patient_id, 1, "REG-LOCK", "2026-04-24T08:00:00"),
            )
            admission_id = int(cursor.lastrowid)

        service = RemCardService(
            VitalsDAO(manager),
            FluidsDAO(manager),
            OrdersDAO(manager),
            VentilationDAO(manager),
            PatientDAO(manager),
        )
        shift_date = datetime(2026, 4, 24, 12, 0, 0)

        def new_order(name: str) -> OrderDTO:
            return OrderDTO(
                admission_id=admission_id,
                drug_key=name.lower(),
                latin=name,
                type=OrderType.MEDICATION,
                status=OrderStatus.ACTIVE,
                dose_value=1.0,
                dose_unit="mg",
                is_per_kg=False,
                frequency=1,
                specific_times=[],
                duration_min=0,
                is_committed=0,
                created_at=datetime(2026, 4, 24, 9, 0, 0),
                comment="",
                last_modified_by="doctor",
            )

        first = new_order("Lock One")
        second = new_order("Lock Two")
        service.add_order(first)
        service.add_order(second)
        if first.id is None or second.id is None:
            return False, "order insert did not return ids"

        initial = {order.id: order.revision for order in service.get_orders(admission_id, shift_date)}
        if initial.get(first.id) != 0 or initial.get(second.id) != 0:
            return False, f"unexpected initial revisions: {initial}"

        service.update_order_status(first.id, "held", expected_revision=initial[first.id])
        changed_first = next(order for order in service.get_orders(admission_id, shift_date) if order.id == first.id)
        if int(changed_first.revision or 0) != 1:
            return False, f"order revision did not increment after update: {changed_first.revision}"

        try:
            service.update_order_status(first.id, "active", expected_revision=initial[first.id])
            return False, "stale order update did not raise conflict"
        except OrderConflictError as exc:
            if ORDER_CONFLICT_MESSAGE not in str(exc):
                return False, f"unexpected conflict message: {exc}"

        try:
            service.save_order_draft_sort(admission_id, shift_date, [first.id, second.id], expected_revisions=initial)
            return False, "stale order sort did not raise conflict"
        except OrderConflictError:
            pass

        latest = {order.id: order.revision for order in service.get_orders(admission_id, shift_date)}
        service.save_order_draft_sort(admission_id, shift_date, [second.id, first.id], expected_revisions=latest)
        after_sort = {order.id: order.revision for order in service.get_orders(admission_id, shift_date)}
        if int(after_sort.get(second.id, 0)) <= int(latest.get(second.id, 0)):
            return False, "order sort did not increment revision"

        try:
            service.finalize_order_card(admission_id, shift_date=shift_date, expected_revisions=latest)
            return False, "stale order finalize did not raise conflict"
        except OrderConflictError:
            pass

        latest = {order.id: order.revision for order in service.get_orders(admission_id, shift_date)}
        service.soft_delete_order_row(second.id, False, expected_revision=latest[second.id])
        try:
            service.soft_delete_order_row(first.id, False, expected_revision=initial[first.id])
            return False, "stale order soft-delete did not raise conflict"
        except OrderConflictError:
            pass

        return True, "ok"
    finally:
        manager.close()


def _check_remaining_clinical_optimistic_lock_conflicts(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime, timedelta

    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.data.dao.fluids_dao import FluidsDAO
    from rem_card.data.dao.patient_dao import PatientDAO
    from rem_card.data.dao.patient_status_dao import PatientStatusDAO
    from rem_card.data.dao.ventilation_dao import VentilationDAO
    from rem_card.data.dao.vitals_dao import VitalsDAO
    from rem_card.data.dto.remcard_dto import PatientStatus, VentilationEventType, VentilationMode, VitalDTO
    from rem_card.services.concurrency import DATA_CONFLICT_MESSAGE, DataConflictError
    from rem_card.services.fluid_service import FluidService
    from rem_card.services.patient_bed_management.service import PatientBedManagementService
    from rem_card.services.patient_status_service import PatientStatusService
    from rem_card.services.ventilation_service import VentilationService
    from rem_card.services.vital_service import VitalService

    saved_local_first = os.environ.get("REMCARD_LOCAL_FIRST_SYNC")
    os.environ["REMCARD_LOCAL_FIRST_SYNC"] = "0"
    db_path = os.path.join(temp_root, "remaining_optimistic_lock.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        with manager.remcard_transaction(source="regression_seed_remaining_locks") as cursor:
            cursor.execute("INSERT INTO beds(bed_number, status, current_admission_id) VALUES (1, 'FREE', NULL)")
            cursor.execute("INSERT INTO beds(bed_number, status, current_admission_id) VALUES (2, 'FREE', NULL)")
            cursor.execute("INSERT INTO patients(full_name) VALUES (?)", ("Clinical Lock Patient",))
            patient_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO admissions(patient_id, bed_number, history_number, admission_datetime)
                VALUES (?, ?, ?, ?)
                """,
                (patient_id, 1, "REG-CLIN-LOCK", "2026-04-24T08:00:00"),
            )
            admission_id = int(cursor.lastrowid)
            cursor.execute(
                """
                UPDATE beds
                SET status = 'OCCUPIED',
                    current_admission_id = ?,
                    revision = COALESCE(revision, 0) + 1
                WHERE bed_number = 1
                """,
                (admission_id,),
            )
            cursor.execute(
                """
                INSERT INTO patient_status_events(admission_id, status, start_time, created_by, created_at, updated_at)
                VALUES (?, ?, ?, 'test', ?, ?)
                """,
                (admission_id, PatientStatus.ACTIVE.value, "2026-04-24T08:00:00", "2026-04-24T08:00:00", "2026-04-24T08:00:00"),
            )

        patient_dao = PatientDAO(manager)
        vitals_dao = VitalsDAO(manager)
        vital_service = VitalService(vitals_dao, patient_dao)
        fluid_service = FluidService(FluidsDAO(manager), vital_service)
        shift_date = datetime(2026, 4, 24, 12, 0, 0)

        fluid_service.upsert_hourly_output(admission_id, shift_date, 10, "urine", 100)
        fluid = fluid_service.get_fluids(admission_id, shift_date)[0]
        fluid_service.upsert_hourly_output(admission_id, shift_date, 10, "urine", 120, expected_revision=fluid.revision)
        try:
            fluid_service.upsert_hourly_output(admission_id, shift_date, 10, "urine", 140, expected_revision=fluid.revision)
            return False, "stale fluids update did not raise conflict"
        except DataConflictError as exc:
            if DATA_CONFLICT_MESSAGE not in str(exc):
                return False, f"unexpected fluids conflict message: {exc}"

        vital_time = datetime(2026, 4, 24, 10, 30, 0)
        vital_service.add_vital(
            VitalDTO(id=None, admission_id=admission_id, timestamp=vital_time, sys=120, dia=70, pulse=80),
            shift_date=shift_date,
            force=True,
        )
        vital = vital_service.get_vitals(admission_id, shift_date)[0]
        vital_service.add_vital(
            VitalDTO(id=None, admission_id=admission_id, timestamp=vital_time, sys=121),
            shift_date=shift_date,
            force=True,
            expected_revision=vital.revision,
        )
        try:
            vital_service.add_vital(
                VitalDTO(id=None, admission_id=admission_id, timestamp=vital_time, sys=122),
                shift_date=shift_date,
                force=True,
                expected_revision=vital.revision,
            )
            return False, "stale vitals update did not raise conflict"
        except DataConflictError:
            pass

        bed_service = PatientBedManagementService(manager)
        patient, admission = bed_service.get_patient_with_current_admission(1)
        if not patient or not admission:
            return False, "seeded bed/admission was not visible"
        bed_service.update_patient_and_admission(
            patient.id,
            admission.id,
            {"full_name": "Clinical Lock Patient"},
            {
                "bed_number": 1,
                "history_number": "REG-CLIN-LOCK-2",
                "admission_datetime": admission.admission_datetime,
            },
            expected_admission_revision=admission.revision,
        )
        try:
            bed_service.update_patient_and_admission(
                patient.id,
                admission.id,
                {"full_name": "Clinical Lock Patient"},
                {
                    "bed_number": 1,
                    "history_number": "REG-CLIN-LOCK-3",
                    "admission_datetime": admission.admission_datetime,
                },
                expected_admission_revision=admission.revision,
            )
            return False, "stale admission update did not raise conflict"
        except DataConflictError:
            pass

        source_bed = bed_service.get_bed_by_number(1)
        target_bed = bed_service.get_bed_by_number(2)
        _patient, latest_admission = bed_service.get_patient_with_current_admission(1)
        bed_service.move_patient(
            1,
            2,
            expected_source_bed_revision=int(source_bed["revision"] or 0),
            expected_target_bed_revision=int(target_bed["revision"] or 0),
            expected_source_admission_revision=latest_admission.revision,
        )
        try:
            bed_service.move_patient(2, 1, expected_source_bed_revision=0)
            return False, "stale bed move did not raise conflict"
        except DataConflictError:
            pass

        status_service = PatientStatusService(PatientStatusDAO(manager))
        current = status_service.get_current_status(admission_id)
        status_service.change_status(
            admission_id,
            PatientStatus.OUT,
            reason_text="test",
            user_id="test",
            expected_active_event_id=current.id,
            expected_active_revision=current.revision,
        )
        try:
            status_service.change_status(
                admission_id,
                PatientStatus.OR,
                reason_text="stale",
                user_id="test",
                expected_active_event_id=current.id,
                expected_active_revision=current.revision,
            )
            return False, "stale status change did not raise conflict"
        except DataConflictError:
            pass

        vent_service = VentilationService(VentilationDAO(manager))
        start_time = datetime(2026, 4, 24, 9, 0, 0)
        case = vent_service.create_case(
            admission_id,
            start_time=start_time,
            initial_mode=VentilationMode.CONTROLLED_VCV,
            initial_parameters={"RR": 12, "TV": 500, "PEEP": 5, "FiO2": 50},
        )
        vent_service.add_event(
            case.id,
            event_time=start_time + timedelta(minutes=10),
            event_type=VentilationEventType.MODE_CHANGE,
            mode=VentilationMode.CONTROLLED_VCV,
            parameters={"RR": 13, "TV": 500, "PEEP": 5, "FiO2": 50},
            expected_case_revision=case.revision,
        )
        try:
            vent_service.add_event(
                case.id,
                event_time=start_time + timedelta(minutes=20),
                event_type=VentilationEventType.MODE_CHANGE,
                mode=VentilationMode.CONTROLLED_VCV,
                parameters={"RR": 14, "TV": 500, "PEEP": 5, "FiO2": 50},
                expected_case_revision=case.revision,
            )
            return False, "stale ventilation event did not raise conflict"
        except DataConflictError:
            pass

        quick = manager.fetch_one_remcard("PRAGMA quick_check")
        if not quick or str(quick[0]).lower() != "ok":
            return False, f"quick_check failed after optimistic lock checks: {quick}"
        return True, "ok"
    finally:
        manager.close()
        if saved_local_first is None:
            os.environ.pop("REMCARD_LOCAL_FIRST_SYNC", None)
        else:
            os.environ["REMCARD_LOCAL_FIRST_SYNC"] = saved_local_first


def _check_analytics_runs_outside_ui_callbacks(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    graphs_source = (PROJECT_ROOT / "ui" / "analytics" / "graphs_dialog.py").read_text(encoding="utf-8")
    report_source = (PROJECT_ROOT / "ui" / "analytics" / "report_dialog.py").read_text(encoding="utf-8")
    detailed_report_source = (PROJECT_ROOT / "ui" / "analytics" / "statistics_dialog.py").read_text(encoding="utf-8")
    worker_source = (PROJECT_ROOT / "ui" / "shared" / "analytics_worker.py").read_text(encoding="utf-8")
    pdf_worker_source = (PROJECT_ROOT / "ui" / "shared" / "html_pdf_worker.py").read_text(encoding="utf-8")
    graph_service_source = (PROJECT_ROOT / "services" / "analytics" / "graphs_service.py").read_text(encoding="utf-8")
    statistics_service_source = (PROJECT_ROOT / "services" / "analytics" / "statistics_service.py").read_text(encoding="utf-8")
    detailed_statistics_service_source = (
        PROJECT_ROOT / "services" / "analytics" / "detailed_statistics_service.py"
    ).read_text(encoding="utf-8")

    forbidden_ui_tokens = ("cursor.execute", "pd.read_sql", "matplotlib", "QPdfWriter", "QTextDocument", "generate_g")
    for label, source in (
        ("graphs_dialog", graphs_source),
        ("report_dialog", report_source),
        ("statistics_dialog", detailed_report_source),
    ):
        for token in forbidden_ui_tokens:
            if token in source:
                return False, f"{label} still contains heavy analytics token: {token}"

    if "class AnalyticsWorker(QThread)" not in worker_source or "self._operation()" not in worker_source:
        return False, "AnalyticsWorker does not own callable execution"
    if "class HtmlPdfWorker(QThread)" not in pdf_worker_source or "QPdfWriter" not in pdf_worker_source:
        return False, "HtmlPdfWorker does not own HTML PDF generation"
    for label, source in (
        ("graphs dialog", graphs_source),
        ("report dialog", report_source),
        ("statistics dialog", detailed_report_source),
    ):
        if "def reject(self):" not in source or "def closeEvent(self, event):" not in source:
            return False, f"{label} must cancel/ignore worker callbacks on reject and closeEvent"
        if "self._closing = True" not in source:
            return False, f"{label} must ignore worker callbacks after close/reject"
    if "build_graphs_html" not in graph_service_source or "generate_g1_g5" not in graph_service_source:
        return False, "graphs service does not own graph generation"
    if "build_statistical_report_html" not in statistics_service_source or "cursor.execute" not in statistics_service_source:
        return False, "statistics service does not own SQL report generation"
    if (
        "build_detailed_statistics_report_html" not in detailed_statistics_service_source
        or "cursor.execute" not in detailed_statistics_service_source
    ):
        return False, "detailed statistics service does not own detailed SQL report generation"
    return True, "ok"


def _check_medical_audit_log_triggers(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.app.unified_db_schema import SCHEMA_MIN_MIGRATION_VERSION

    db_path = os.path.join(temp_root, "medical_audit_log.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        with manager.remcard_transaction(source="regression_seed_medical_audit") as cursor:
            cursor.execute("INSERT INTO patients(full_name) VALUES (?)", ("Audit Patient",))
            patient_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO admissions(patient_id, bed_number, history_number, admission_datetime)
                VALUES (?, ?, ?, ?)
                """,
                (patient_id, 1, "REG-AUDIT-001", "2026-05-03 08:00:00"),
            )
            admission_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO orders(
                    admission_id, datetime, text, drug_key, latin, type, status,
                    is_committed, revision, last_modified_by, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, 'doctor', STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))
                """,
                (
                    admission_id,
                    "2026-05-03 08:00:00",
                    "Audit Drug",
                    "audit_drug",
                    "Audit Drug",
                    "medication",
                    "active",
                ),
            )
            order_id = int(cursor.lastrowid)
            cursor.execute("UPDATE orders SET status = 'held' WHERE id = ?", (order_id,))

        row = manager.fetch_one_remcard(
            """
            SELECT MAX(version) AS version
            FROM schema_migrations
            """
        )
        if not row or int(row["version"] or 0) < SCHEMA_MIN_MIGRATION_VERSION:
            return False, "medical audit migration did not advance schema_migrations"

        audit_rows = manager.fetch_all_remcard(
            """
            SELECT table_name, row_id, admission_id, action_type, changed_by, operation_id, before_json, after_json
            FROM medical_audit_log
            WHERE table_name = 'orders' AND row_id = ?
            ORDER BY id
            """,
            (order_id,),
        )
        actions = [dict(row)["action_type"] for row in audit_rows]
        if actions != ["insert", "update"]:
            return False, f"unexpected order audit actions: {actions}"

        update_row = dict(audit_rows[-1])
        if update_row.get("changed_by") != "doctor":
            return False, f"unexpected audit changed_by: {update_row.get('changed_by')}"
        if not update_row.get("operation_id"):
            return False, "medical audit operation_id is empty"
        if int(update_row.get("admission_id") or 0) != admission_id:
            return False, "medical audit admission_id mismatch"

        before_payload = json.loads(update_row["before_json"])
        after_payload = json.loads(update_row["after_json"])
        if before_payload.get("status") != "active" or after_payload.get("status") != "held":
            return False, f"medical audit before/after payload mismatch: {before_payload} -> {after_payload}"

        quick = manager.fetch_one_remcard("PRAGMA quick_check")
        if not quick or str(quick[0]).lower() != "ok":
            return False, f"quick_check failed after audit trigger writes: {quick}"
        return True, "ok"
    finally:
        manager.close()


def _check_lab_orders_are_scoped_to_card_day(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime, timedelta

    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.data.dao.lab_orders_dao import LabOrdersDAO
    from rem_card.services.lab_orders_service import LabOrdersService

    db_path = os.path.join(temp_root, "lab_orders_scope.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        with manager.remcard_transaction(source="regression_seed_lab_orders_scope") as cursor:
            cursor.execute("INSERT INTO patients(full_name) VALUES (?)", ("Lab Scope Patient",))
            patient_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO admissions(patient_id, bed_number, history_number, admission_datetime)
                VALUES (?, ?, ?, ?)
                """,
                (patient_id, 1, "REG-LAB-SCOPE", "2026-05-24 08:00:00"),
            )
            admission_id = int(cursor.lastrowid)

        service = LabOrdersService(LabOrdersDAO(manager))
        yesterday_start = datetime(2026, 5, 24, 8, 0, 0)
        today_start = datetime(2026, 5, 25, 8, 0, 0)
        yesterday_end = yesterday_start + timedelta(days=1)
        today_end = today_start + timedelta(days=1)
        yesterday_card_day_id = service.card_day_id_from_shift_start(yesterday_start)
        today_card_day_id = service.card_day_id_from_shift_start(today_start)

        service.create_lab_orders(
            admission_id=admission_id,
            card_day_id=yesterday_card_day_id,
            orders=[
                {
                    "analysis_name": "Вчерашний анализ",
                    "material": "venous_blood",
                    "scheduled_at": datetime(2026, 5, 24, 10, 0, 0),
                }
            ],
        )
        service.create_lab_orders(
            admission_id=admission_id,
            card_day_id=today_card_day_id,
            orders=[
                {
                    "analysis_name": "Сегодняшний анализ",
                    "material": "urine",
                    "scheduled_at": datetime(2026, 5, 25, 10, 0, 0),
                }
            ],
        )
        with manager.remcard_transaction(source="regression_seed_legacy_lab_order") as cursor:
            cursor.execute(
                """
                INSERT INTO lab_orders(
                    patient_id, admission_id, card_day_id,
                    analysis_code, analysis_name, material, status,
                    created_at, scheduled_at, completed_at, comment,
                    created_by_role, revision
                )
                VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    patient_id,
                    admission_id,
                    "legacy_yesterday",
                    "Legacy yesterday",
                    "venous_blood",
                    "completed",
                    "2026-05-25 09:30:00",
                    "2026-05-24 10:00:00",
                    "2026-05-25 10:30:00",
                    "",
                    "doctor",
                    0,
                ),
            )

        today_rows = service.build_snapshot(
            admission_id,
            card_day_id=today_card_day_id,
            start_dt=today_start,
            end_dt=today_end,
        )["rows"]
        yesterday_rows = service.build_snapshot(
            admission_id,
            card_day_id=yesterday_card_day_id,
            start_dt=yesterday_start,
            end_dt=yesterday_end,
        )["rows"]
        today_names = {row.get("analysis_name") for row in today_rows}
        yesterday_names = {row.get("analysis_name") for row in yesterday_rows}
        if "Сегодняшний анализ" not in today_names:
            return False, f"today lab order missing: {today_names}"
        if "Вчерашний анализ" in today_names or "Legacy yesterday" in today_names:
            return False, f"today snapshot contains another card day: {today_names}"
        if {"Вчерашний анализ", "Legacy yesterday"} - yesterday_names:
            return False, f"yesterday lab orders missing: {yesterday_names}"
        if "Сегодняшний анализ" in yesterday_names:
            return False, f"yesterday snapshot contains today order: {yesterday_names}"

        today_id = next((row.get("id") for row in today_rows if row.get("analysis_name") == "Сегодняшний анализ"), None)
        if today_id is None:
            return False, "today lab order id missing"
        deleted_count = service.delete_lab_orders(admission_id, order_ids=[today_id])
        if deleted_count != 1:
            return False, f"unexpected deleted lab orders count: {deleted_count}"
        today_after_delete = service.build_snapshot(
            admission_id,
            card_day_id=today_card_day_id,
            start_dt=today_start,
            end_dt=today_end,
        )["rows"]
        yesterday_after_delete = service.build_snapshot(
            admission_id,
            card_day_id=yesterday_card_day_id,
            start_dt=yesterday_start,
            end_dt=yesterday_end,
        )["rows"]
        if today_after_delete:
            return False, f"today snapshot still has deleted rows: {today_after_delete}"
        yesterday_after_delete_names = {row.get("analysis_name") for row in yesterday_after_delete}
        if {"Вчерашний анализ", "Legacy yesterday"} - yesterday_after_delete_names:
            return False, f"delete affected another card day: {yesterday_after_delete_names}"
        return True, "ok"
    finally:
        manager.close()


def _check_doctor_create_card_avoids_open_snapshot_race(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    source_path = PROJECT_ROOT / "ui" / "doctor_view" / "doctor_remcard_widget.py"
    source_text = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text)

    class_defs = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DoctorRemCardWidget"]
    if not class_defs:
        return False, "DoctorRemCardWidget class not found"
    methods = {node.name: node for node in class_defs[0].body if isinstance(node, ast.FunctionDef)}

    load_method = methods.get("load_patient_card")
    if load_method is None:
        return False, "load_patient_card not found"
    load_source = _cached_source_segment(source_text, load_method) or ""
    orders_context_source = _cached_source_segment(
        source_text,
        methods.get("_sync_orders_widget_context_for_patient_open"),
    ) or ""
    if "ow.set_context" not in f"{load_source}\n{orders_context_source}":
        return False, "load_patient_card must update OrdersWidget through set_context"
    request_snapshot_kw = [
        (arg, default)
        for arg, default in zip(load_method.args.kwonlyargs, load_method.args.kw_defaults)
        if arg.arg == "request_snapshot"
    ]
    if (
        not request_snapshot_kw
        or not isinstance(request_snapshot_kw[0][1], ast.Constant)
        or request_snapshot_kw[0][1].value is not True
    ):
        return False, "load_patient_card must accept request_snapshot=True keyword"

    select_method = methods.get("on_patient_selected_from_list")
    if select_method is None:
        return False, "on_patient_selected_from_list not found"
    create_branch_uses_deferred_snapshot = False
    for node in ast.walk(select_method):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "load_patient_card":
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "request_snapshot"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is False
            ):
                create_branch_uses_deferred_snapshot = True
                break
    if not create_branch_uses_deferred_snapshot:
        return False, "create action should load patient card with request_snapshot=False"

    create_method = methods.get("on_create_card_clicked")
    if create_method is None:
        return False, "on_create_card_clicked not found"
    create_source = _cached_source_segment(source_text, create_method) or ""
    if "_create_card_after_snapshot" not in create_source or "_snapshot_worker is not None" not in create_source:
        return False, "create-card write is not deferred while snapshot worker is pending"

    return True, "ok"


def _check_doctor_load_patient_card_refactor_path(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    source_path = PROJECT_ROOT / "ui" / "doctor_view" / "doctor_remcard_widget.py"
    source_text = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text)
    class_defs = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DoctorRemCardWidget"]
    if not class_defs:
        return False, "DoctorRemCardWidget class not found"
    methods = {node.name: node for node in class_defs[0].body if isinstance(node, ast.FunctionDef)}
    load_source = _cached_source_segment(source_text, methods.get("load_patient_card")) or ""
    if not load_source:
        return False, "load_patient_card not found"

    ordered_markers = (
        "orders_context_unchanged = self._prepare_patient_card_orders_context(admission_id, date)",
        ") = self._reset_patient_card_context_state(",
        "self._sync_patient_card_layout_context(",
        "self._last_change_id = 0",
        "self._apply_archive_read_only_state()",
        "if not cached_card_snapshot:",
        "self._reset_balance_view_state()",
        "if cached_vitals_snapshot:",
        "self._apply_patient_open_cache(admission_id, date, cached_vitals_snapshot)",
        "self._schedule_patient_card_snapshots(",
        "self._activate_patient_card_vitals_tab()",
        "self._sync_patient_card_auxiliary_contexts(admission_id, date)",
        "self._schedule_nurse_orders_context_for_patient_open(",
        "QTimer.singleShot(0, self.start_polling)",
        "hide_app_loading(self, open_loading_key, delay_ms=600)",
    )
    marker_positions = [load_source.find(marker) for marker in ordered_markers]
    if any(position < 0 for position in marker_positions):
        missing = [marker for marker, position in zip(ordered_markers, marker_positions) if position < 0]
        return False, f"load_patient_card refactor path markers missing: {missing}"
    if marker_positions != sorted(marker_positions):
        return False, "load_patient_card refactor path order changed"

    helper_tokens = {
        "_prepare_patient_card_orders_context": (
            "_ensure_orders_widget()",
            "orders_context_unchanged = False",
            "orders_widget.clear_drafts()",
            "return orders_context_unchanged",
        ),
        "_reset_patient_card_context_state": (
            "self.admission_id = admission_id",
            "self.current_date = date",
            "self._card_snapshot_cache = None",
            "_get_cached_patient_card_snapshot(admission_id, date)",
            "_get_cached_patient_vitals_snapshot(admission_id, date)",
        ),
        "_sync_patient_card_layout_context": (
            "self.layout_manager.current_admission_id = admission_id",
            "self.layout_manager.current_date = date",
            "self._sync_lab_orders_context()",
            "self._update_emergency_notice_sector()",
            "self._update_chart_context_for_patient_open(admission_id, card_start_dt)",
            "self.layout_manager.set_events_context(",
            "self.vitals_input.mark_dirty()",
            "self._sync_orders_widget_context_for_patient_open(admission_id, date, orders_context_unchanged)",
        ),
        "_sync_orders_widget_context_for_patient_open": (
            "ow.set_context(",
            "ow.service = self.service",
            "ow.admission_id = admission_id",
            "ow.shift_date = date",
        ),
        "_schedule_patient_card_snapshots": (
            "if not request_snapshot:",
            "self._should_ensure_initial_status_for_date(date)",
            "self._request_card_snapshot(",
            'load_scope="patient_open_vitals"',
            "self._schedule_card_hydration_snapshot(",
        ),
        "_activate_patient_card_vitals_tab": (
            'set_active_tab("Витальные функции", source="refresh")',
            "select_tab(active_tab, emit=False)",
            "self.on_tab_changed(active_tab)",
        ),
        "_sync_patient_card_auxiliary_contexts": (
            "self.balance_controller.admission_id = admission_id",
            "self.balance_controller.shift_date = date",
            "set_patient_period_manual_mode",
            "_ensure_diet_widget()",
            "diet_widget.set_context(admission_id, date)",
        ),
        "_schedule_nurse_orders_context_for_patient_open": (
            "ensure_nurse_orders_manager",
            "_bind_nurse_orders_balance_signals()",
            "QTimer.singleShot(",
            "_set_nurse_orders_context_if_current(mgr, aid, d, gen)",
        ),
    }
    for helper_name, tokens in helper_tokens.items():
        helper_source = _cached_source_segment(source_text, methods.get(helper_name)) or ""
        if not helper_source:
            return False, f"{helper_name} helper not found"
        missing = [token for token in tokens if token not in helper_source]
        if missing:
            return False, f"{helper_name} lost patient-open side effects: {missing}"

    chart_source = _cached_source_segment(source_text, methods.get("_update_chart_context_for_patient_open")) or ""
    match_pos = chart_source.find("chart_matches_target = self._chart_matches_context")
    clear_pos = chart_source.find("self.chart.clear_for_context")
    assign_pos = chart_source.find("self.chart.admission_id = admission_id")
    if min(match_pos, clear_pos, assign_pos) < 0 or not (match_pos < clear_pos and match_pos < assign_pos):
        return False, "chart context must still be checked before clear/assign side effects"

    return True, "ok"


def _check_orders_widgets_defer_snapshot_reload_thread_creation(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    cases = [
        ("doctor", "ui/doctor_view/orders_widget.py", "OrdersWidget"),
        ("nurse", "ui/nurse_view/components/nurse_orders_widget.py", "NurseOrdersWidget"),
    ]
    root = PROJECT_ROOT
    for role, relative_path, class_name in cases:
        source_path = root / relative_path
        source_text = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source_text)
        class_defs = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name]
        if not class_defs:
            return False, f"{role}: {class_name} class not found"
        methods = {node.name: node for node in class_defs[0].body if isinstance(node, ast.FunctionDef)}
        for method_name in (
            "_request_snapshot",
            "_queue_forced_reload_after_stale_snapshot",
            "_on_snapshot_finished",
            "_defer_snapshot_request",
        ):
            if method_name not in methods:
                return False, f"{role}: {method_name} not found"

        request_source = _cached_source_segment(source_text, methods["_request_snapshot"]) or ""
        if "self._snapshot_worker is not None" not in request_source:
            return False, f"{role}: snapshot worker must stay busy until finished signal"

        stale_source = _cached_source_segment(source_text, methods["_queue_forced_reload_after_stale_snapshot"]) or ""
        enqueue_method = methods.get("_enqueue_forced_reload")
        enqueue_source = _cached_source_segment(source_text, enqueue_method) if enqueue_method else ""
        if "_defer_snapshot_request" not in stale_source and "_defer_snapshot_request" not in enqueue_source:
            return False, f"{role}: stale snapshot reload must be deferred"

        finished_source = _cached_source_segment(source_text, methods["_on_snapshot_finished"]) or ""
        if "_defer_snapshot_request" not in finished_source:
            return False, f"{role}: pending reload after worker finish must be deferred"

        defer_source = _cached_source_segment(source_text, methods["_defer_snapshot_request"]) or ""
        if "QTimer.singleShot" not in defer_source:
            return False, f"{role}: deferred reload helper must use QTimer.singleShot"

    return True, "ok"


def _check_targeted_async_workers_are_parentless_and_guarded(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    cases = [
        (
            "doctor_card",
            PROJECT_ROOT / "ui" / "doctor_view" / "doctor_remcard_widget.py",
            "DoctorRemCardWidget",
            "_request_card_snapshot",
            ("_apply_card_snapshot", "_on_card_snapshot_failed", "_on_card_snapshot_finished", "shutdown"),
        ),
        (
            "nurse_card",
            PROJECT_ROOT / "ui" / "nurse_view" / "nurse_main_widget.py",
            "NurseMainWidget",
            "_request_card_snapshot",
            ("_apply_card_snapshot", "_on_card_snapshot_failed", "_on_card_snapshot_finished", "shutdown"),
        ),
        (
            "doctor_orders",
            PROJECT_ROOT / "ui" / "doctor_view" / "orders_widget.py",
            "OrdersWidget",
            "_request_snapshot",
            ("_apply_snapshot", "_apply_snapshot_data", "_on_snapshot_failed", "_on_snapshot_finished", "shutdown"),
        ),
        (
            "nurse_orders",
            PROJECT_ROOT / "ui" / "nurse_view" / "components" / "nurse_orders_widget.py",
            "NurseOrdersWidget",
            "_request_snapshot",
            ("_apply_snapshot", "_apply_snapshot_data", "_on_snapshot_failed", "_on_snapshot_finished", "shutdown"),
        ),
        (
            "doctor_beds",
            PROJECT_ROOT / "ui" / "doctor_view" / "components" / "beds_selection_widget.py",
            "BedsSelectionWidget",
            "refresh",
            ("_apply_beds_snapshot", "_on_refresh_failed", "_on_refresh_finished", "shutdown"),
        ),
        (
            "nurse_beds",
            PROJECT_ROOT / "ui" / "nurse_view" / "components" / "nurse_beds_selection_widget.py",
            "NurseBedsSelectionWidget",
            "refresh",
            ("_apply_beds_snapshot", "_on_refresh_failed", "_on_refresh_finished", "shutdown"),
        ),
    ]

    def _async_call_uses_parent_self(method: ast.FunctionDef) -> bool:
        for node in ast.walk(method):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            func_name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if func_name != "AsyncCallThread":
                continue
            for keyword in node.keywords:
                if keyword.arg == "parent" and isinstance(keyword.value, ast.Name) and keyword.value.id == "self":
                    return True
        return False

    for role, path, class_name, request_method_name, guarded_method_names in cases:
        source_text = path.read_text(encoding="utf-8")
        tree = ast.parse(source_text)
        class_defs = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name]
        if not class_defs:
            return False, f"{role}: {class_name} class not found"
        methods = {node.name: node for node in class_defs[0].body if isinstance(node, ast.FunctionDef)}
        request_method = methods.get(request_method_name)
        if request_method is None:
            return False, f"{role}: {request_method_name} not found"
        request_source = _cached_source_segment(source_text, request_method) or ""
        if "AsyncCallThread" not in request_source:
            return False, f"{role}: request method does not start AsyncCallThread"
        if _async_call_uses_parent_self(request_method):
            return False, f"{role}: snapshot worker still uses Qt parent=self"
        if "_is_closing" not in request_source:
            return False, f"{role}: request method must guard _is_closing"

        for method_name in guarded_method_names:
            method = methods.get(method_name)
            if method is None:
                return False, f"{role}: {method_name} not found"
            method_source = _cached_source_segment(source_text, method) or ""
            if "_is_closing" not in method_source:
                return False, f"{role}: {method_name} must guard _is_closing"

        shutdown_source = _cached_source_segment(source_text, methods["shutdown"]) or ""
        helper_source = ""
        helper = methods.get("_shutdown_snapshot_worker")
        if helper is not None:
            helper_source = _cached_source_segment(source_text, helper) or ""
        lifecycle_source = shutdown_source + "\n" + helper_source
        if "disconnect" not in lifecycle_source:
            return False, f"{role}: shutdown must disconnect active snapshot workers"
        if role == "nurse_card":
            if ".wait(" in lifecycle_source:
                return False, "nurse: shutdown must not block the UI thread waiting for a snapshot worker"
            if "_snapshot_request_id += 1" not in lifecycle_source:
                return False, "nurse: shutdown must invalidate queued snapshot results"
        elif ".wait(" not in lifecycle_source:
            return False, f"{role}: shutdown must wait active snapshot workers"
        if role.endswith("_card") and "clear_drafts()" in shutdown_source:
            return False, f"{role}: shutdown must not enqueue clear_drafts during app close"

    return True, "ok"


def _check_async_call_worker_avoids_qthread(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    path = PROJECT_ROOT / "ui" / "shared" / "async_call.py"
    source_text = path.read_text(encoding="utf-8")
    if "QThread" in source_text:
        return False, "AsyncCallThread must not use Qt QThread for snapshot workers"
    if "threading.Thread" not in source_text:
        return False, "AsyncCallThread must use a Python worker thread"
    for marker in ("succeeded = Signal(object)", "failed = Signal(object)", "finished = Signal()"):
        if marker not in source_text:
            return False, f"AsyncCallThread signal API changed: missing {marker}"
    for marker in ("def start(", "def isRunning(", "def quit(", "def wait("):
        if marker not in source_text:
            return False, f"AsyncCallThread compatibility API missing: {marker}"

    tree = ast.parse(source_text)
    class_defs = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AsyncCallThread"]
    if not class_defs:
        return False, "AsyncCallThread class not found"
    bases = [getattr(base, "id", getattr(base, "attr", "")) for base in class_defs[0].bases]
    if "QObject" not in bases:
        return False, "AsyncCallThread should stay a QObject signal emitter"
    return True, "ok"


def _check_patient_open_cache_snapshot_bypasses_worker_request_id(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    cases = [
        (
            "doctor",
            PROJECT_ROOT / "ui" / "doctor_view" / "doctor_remcard_widget.py",
            "DoctorRemCardWidget",
        ),
        (
            "nurse",
            PROJECT_ROOT / "ui" / "nurse_view" / "nurse_main_widget.py",
            "NurseMainWidget",
        ),
    ]
    for role, path, class_name in cases:
        source_text = path.read_text(encoding="utf-8")
        tree = ast.parse(source_text)
        class_defs = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name]
        if not class_defs:
            return False, f"{role}: {class_name} class not found"
        methods = {node.name: node for node in class_defs[0].body if isinstance(node, ast.FunctionDef)}
        cache_method = methods.get("_apply_patient_open_cache")
        apply_method = methods.get("_apply_card_snapshot")
        if cache_method is None or apply_method is None:
            return False, f"{role}: patient-open cache/apply methods not found"
        cache_source = _cached_source_segment(source_text, cache_method) or ""
        apply_source = _cached_source_segment(source_text, apply_method) or ""
        if '"from_cache": True' not in cache_source:
            return False, f"{role}: patient-open cache request must be marked from_cache"
        if 'request_id is None and not request.get("from_cache")' not in apply_source:
            return False, f"{role}: from_cache snapshots without worker request_id must pass request-id guard"
        if "request_id is not None and request_id != self._snapshot_request_id" not in apply_source:
            return False, f"{role}: worker snapshots must still reject stale request_id"
    return True, "ok"


def _check_patient_form_open_is_deferred_from_callback(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    path = PROJECT_ROOT / "ui" / "patient_bed_management" / "management_widget.py"
    source_text = path.read_text(encoding="utf-8")
    tree = ast.parse(source_text)
    class_defs = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PatientBedManagementWidget"
    ]
    if not class_defs:
        return False, "PatientBedManagementWidget class not found"
    methods = {node.name: node for node in class_defs[0].body if isinstance(node, ast.FunctionDef)}
    open_method = methods.get("_open_patient_card_by_number")
    safe_method = methods.get("_open_patient_form_safe")
    if open_method is None or safe_method is None:
        return False, "deferred patient form helpers not found"
    open_source = _cached_source_segment(source_text, open_method) or ""
    safe_source = _cached_source_segment(source_text, safe_method) or ""
    if "QTimer.singleShot" not in open_source:
        return False, "PatientForm opening must be deferred with QTimer.singleShot"
    if "dialog.exec" in open_source:
        return False, "PatientForm.dialog.exec must not run in the original callback"
    if "dialog.exec" in safe_source:
        return False, "PatientForm.dialog.exec must not run in the deferred helper"
    if "dialog.open" not in safe_source:
        return False, "deferred helper must still open PatientForm"
    if "finished.connect" not in safe_source or "_finish_patient_form_dialog" not in source_text:
        return False, "PatientForm nonblocking open must handle finished signal"
    for guard in ("_opening_patient_form", "_is_closing"):
        if guard not in open_source + safe_source:
            return False, f"PatientForm deferred open missing {guard} guard"
    return True, "ok"


def _check_shutdown_queue_db_ordering_guards(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    data_service_text = (PROJECT_ROOT / "services" / "data_service.py").read_text(encoding="utf-8")
    for marker in (
        "_shutting_down",
        "set_shutting_down",
        "Queued write rejected during shutdown",
        "return False",
    ):
        if marker not in data_service_text:
            return False, f"DataService missing shutdown guard marker: {marker}"

    main_window_text = (PROJECT_ROOT / "ui" / "main_window.py").read_text(encoding="utf-8")
    main_tree = ast.parse(main_window_text)
    main_classes = [node for node in main_tree.body if isinstance(node, ast.ClassDef) and node.name == "MainWindow"]
    if not main_classes:
        return False, "MainWindow class not found"
    main_methods = {node.name: node for node in main_classes[0].body if isinstance(node, ast.FunctionDef)}
    close_method = main_methods.get("closeEvent")
    if close_method is None:
        return False, "MainWindow.closeEvent not found"
    close_source = _cached_source_segment(main_window_text, close_method) or ""
    if "set_shutting_down" not in close_source:
        return False, "MainWindow.closeEvent must mark DataService shutting down before UI shutdown"
    if "db_manager.close(" in close_source or "data_service.shutdown()" in close_source:
        return False, "MainWindow.closeEvent must defer data resource shutdown until after Qt loop exits"
    if "clear_drafts()" in close_source:
        return False, "MainWindow.closeEvent must not enqueue clear_drafts during shutdown"

    main_app_text = (PROJECT_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    app_tree = ast.parse(main_app_text)
    shutdown_func = next(
        (
            node
            for node in app_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_shutdown_window_resources"
        ),
        None,
    )
    if shutdown_func is None:
        return False, "app.main._shutdown_window_resources not found"
    shutdown_source = _cached_source_segment(main_app_text, shutdown_func) or ""
    data_shutdown_idx = shutdown_source.find("data_service.shutdown()")
    db_close_idx = shutdown_source.find("db_manager.close()")
    if data_shutdown_idx < 0 or db_close_idx < 0:
        return False, "_shutdown_window_resources must drain DataService and close DB"
    if data_shutdown_idx > db_close_idx:
        return False, "_shutdown_window_resources must drain DataService before DB close"
    for marker in ("data_service_shutdown_ok", "DB manager close skipped", "DB manager close did not complete cleanly"):
        if marker not in shutdown_source:
            return False, f"_shutdown_window_resources missing shutdown ordering marker: {marker}"

    sqlite_text = (PROJECT_ROOT / "app" / "sqlite_shared.py").read_text(encoding="utf-8")
    for marker in ("DatabaseClosedError", "conn is None", "def shutdown(self, timeout: float = 1.0) -> bool"):
        if marker not in sqlite_text:
            return False, f"sqlite_shared missing controlled shutdown marker: {marker}"

    db_text = (PROJECT_ROOT / "data" / "dao" / "db_manager.py").read_text(encoding="utf-8")
    if "DatabaseClosedError" not in db_text or "self._closed or self._remcard_conn is None" not in db_text:
        return False, "DatabaseManager must raise controlled DatabaseClosedError after close"
    return True, "ok"


def _regression_class_methods(
    root: Path,
    relative_path: str,
    class_name: str,
) -> tuple[str, dict[str, ast.FunctionDef]]:
    source_path = root / relative_path
    source_text = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text)
    class_defs = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name]
    if not class_defs:
        return source_text, {}
    methods = {node.name: node for node in class_defs[0].body if isinstance(node, ast.FunctionDef)}
    return source_text, methods


def _assert_no_viewport_update(
    methods: dict[str, ast.FunctionDef],
    source_text: str,
    method_name: str,
    label: str,
) -> tuple[bool, str]:
    method = methods.get(method_name)
    if method is None:
        return False, f"{label}: {method_name} not found"
    method_source = _cached_source_segment(source_text, method) or ""
    if ".viewport().update(" in method_source or "viewport().update()" in method_source:
        return False, f"{label}: {method_name} must use targeted dataChanged, not full viewport repaint"
    return True, "ok"


def _check_doctor_fast_click_source(root: Path) -> tuple[bool, str]:
    source_text, methods = _regression_class_methods(
        root,
        "ui/doctor_view/orders_widget.py",
        "OrdersWidget",
    )
    if not methods:
        return False, "doctor: OrdersWidget class not found"
    for method_name in ("_handle_cell_action", "_emit_admin_cell_changes"):
        if method_name not in methods:
            return False, f"doctor: {method_name} not found"

    click_source = _cached_source_segment(source_text, methods["_handle_cell_action"]) or ""
    if "_apply_optimistic_cell" not in click_source:
        return False, "doctor: cell click must update the local overlay"
    if "_enqueue_cell_write" in click_source or "_enqueue_write(" in click_source:
        return False, "doctor: draft cell click must not enqueue persistence"
    if "service_action(" in click_source:
        return False, "doctor: draft cell click must not call the persistence service"
    if "_request_snapshot" in click_source or "_schedule_fast_sync" in click_source:
        return False, "doctor: draft cell click must not start a network refresh"

    emit_source = _cached_source_segment(source_text, methods["_emit_admin_cell_changes"]) or ""
    if ".viewport().update(" in emit_source or "viewport().update()" in emit_source:
        return False, "doctor: local cell changes must not repaint the whole orders viewport"

    for guarded_method in (
        "_try_apply_admin_only_snapshot",
        "_mark_local_order_row_deleted",
        "_clear_local_order_row_pending_delete",
        "_replace_local_order_after_edit",
    ):
        ok, details = _assert_no_viewport_update(methods, source_text, guarded_method, "doctor")
        if not ok:
            return False, details
    return True, "ok"


def _check_nurse_fast_click_source(root: Path) -> tuple[bool, str]:
    nurse_text, nurse_methods = _regression_class_methods(
        root,
        "ui/nurse_view/components/nurse_orders_widget.py",
        "NurseOrdersWidget",
    )
    if not nurse_methods:
        return False, "nurse: NurseOrdersWidget class not found"
    for guarded_method in (
        "_try_apply_admin_only_snapshot",
        "_restore_admin_cell",
        "_apply_pending_nurse_mark",
        "_apply_committed_nurse_mark",
        "_on_table_clicked",
        "_on_mark_updated",
    ):
        ok, details = _assert_no_viewport_update(nurse_methods, nurse_text, guarded_method, "nurse")
        if not ok:
            return False, details
    return True, "ok"


def _check_orders_model_fast_click_source(root: Path) -> tuple[bool, str]:
    model_text, model_methods = _regression_class_methods(
        root,
        "ui/shared/orders_model.py",
        "OrdersModel",
    )
    if not model_methods:
        return False, "shared: OrdersModel class not found"
    apply_admin_method = model_methods.get("apply_admin_rows_snapshot")
    if apply_admin_method is None:
        return False, "shared: OrdersModel.apply_admin_rows_snapshot not found"
    apply_admin_source = _cached_source_segment(model_text, apply_admin_method) or ""
    if "_set_has_any_draft(" not in apply_admin_source or "emit_order_column=True" not in apply_admin_source:
        return False, "shared: admin-only snapshot must repaint order column when draft state changes"
    return True, "ok"


def _check_orders_delegate_fast_click_source(root: Path) -> tuple[bool, str]:
    delegate_text, delegate_methods = _regression_class_methods(
        root,
        "ui/shared/orders_delegate.py",
        "OrdersDelegate",
    )
    if not delegate_methods:
        return False, "shared: OrdersDelegate class not found"
    if "_is_admin_pending" not in delegate_methods:
        return False, "shared: OrdersDelegate._is_admin_pending not found"
    pending_source = _cached_source_segment(delegate_text, delegate_methods["_is_admin_pending"]) or ""
    if "_pending_cell_action" in pending_source:
        return False, "shared: ordinary planned X must not be drawn as pending"
    return True, "ok"


def _check_orders_fast_click_path_stays_local(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    root = PROJECT_ROOT
    for check in (
        _check_doctor_fast_click_source,
        _check_nurse_fast_click_source,
        _check_orders_model_fast_click_source,
        _check_orders_delegate_fast_click_source,
    ):
        ok, details = check(root)
        if not ok:
            return False, details
    return True, "ok"


def _check_doctor_performance_guards(root: Path) -> tuple[bool, str]:
    from .ui_layout import _method_source
    doctor_text, doctor_methods = _regression_class_methods(
        root,
        "ui/doctor_view/doctor_remcard_widget.py",
        "DoctorRemCardWidget",
    )
    if not doctor_methods:
        return False, "DoctorRemCardWidget class not found"
    readonly_method = doctor_methods.get("_apply_archive_read_only_state")
    if readonly_method is None:
        return False, "DoctorRemCardWidget._apply_archive_read_only_state not found"
    readonly_source = _cached_source_segment(doctor_text, readonly_method) or ""
    if "_read_only_widget_signature" not in doctor_text or "apply_widget_state" not in readonly_source:
        return False, "doctor read-only state must be idempotent for child widgets"
    if "self.controls" not in readonly_source or "set_save_active" not in readonly_source:
        return False, "doctor read-only guard must keep controls refresh outside the child-widget skip"
    load_patient_card = doctor_methods.get("load_patient_card")
    if load_patient_card is None:
        return False, "DoctorRemCardWidget.load_patient_card not found"
    load_patient_source = _cached_source_segment(doctor_text, load_patient_card) or ""
    prepare_orders_source = _method_source(
        doctor_text,
        doctor_methods,
        "_prepare_patient_card_orders_context",
    )
    sync_orders_source = _method_source(
        doctor_text,
        doctor_methods,
        "_sync_orders_widget_context_for_patient_open",
    )
    patient_open_orders_source = "\n".join((load_patient_source, prepare_orders_source, sync_orders_source))
    if "orders_context_unchanged" not in patient_open_orders_source:
        return False, "doctor patient open must track unchanged orders context"
    if "if not self._archive_read_only_mode:\n                ow.clear_drafts()" in patient_open_orders_source:
        return False, "doctor patient reopen must not clear drafts again for unchanged orders context"
    if "if orders_widget is not None and not self._archive_read_only_mode and not orders_context_unchanged:" not in patient_open_orders_source:
        return False, "doctor patient open clear_drafts must be guarded by orders_context_unchanged"
    if "orders_widget.has_drafts()" not in patient_open_orders_source or "CustomMessageBox.No" not in patient_open_orders_source:
        return False, "doctor patient switch must protect a local draft from silent discard"
    return True, "ok"


def _check_orders_performance_guards(root: Path) -> tuple[bool, str]:
    orders_text, orders_methods = _regression_class_methods(
        root,
        "ui/doctor_view/orders_widget.py",
        "OrdersWidget",
    )
    if not orders_methods:
        return False, "OrdersWidget class not found"
    clear_method = orders_methods.get("clear_drafts")
    restore_method = orders_methods.get("_restore_local_draft_baseline")
    if restore_method is None or clear_method is None:
        return False, "orders local draft restore methods are missing"
    clear_source = _cached_source_segment(orders_text, clear_method) or ""
    if "_has_local_draft_changes" not in clear_source:
        return False, "clear_drafts must skip a clean local overlay"
    if "_restore_local_draft_baseline" not in clear_source:
        return False, "clear_drafts must restore the immutable local baseline"
    if "clear_order_drafts" in clear_source or "_enqueue_write" in clear_source:
        if "_legacy_central_draft_detected" not in clear_source or "orders_discard_legacy" not in clear_source:
            return False, "clear_drafts may write only for an explicitly detected legacy central draft"
    return True, "ok"


def _check_diet_performance_guards(root: Path) -> tuple[bool, str]:
    diet_text, diet_methods = _regression_class_methods(
        root,
        "ui/shared/components/diet_intake_widget.py",
        "DietIntakeWidget",
    )
    if not diet_methods:
        return False, "DietIntakeWidget class not found"
    set_read_only = _cached_source_segment(diet_text, diet_methods.get("set_read_only")) if diet_methods.get("set_read_only") else ""
    if "self.read_only == bool(read_only)" not in (set_read_only or ""):
        return False, "DietIntakeWidget.set_read_only must skip unchanged state"
    return True, "ok"


def _check_performance_a_guards_present(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    root = PROJECT_ROOT
    for check in (
        _check_doctor_performance_guards,
        _check_orders_performance_guards,
        _check_diet_performance_guards,
    ):
        ok, details = check(root)
        if not ok:
            return False, details
    return True, "ok"


def _check_report_pdf_callbacks_are_qobject_slots(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    cases = [
        ("doctor", "ui/doctor_view/components/beds_selection_widget.py", "BedsSelectionWidget"),
        ("nurse", "ui/nurse_view/components/nurse_beds_selection_widget.py", "NurseBedsSelectionWidget"),
        ("shared", "ui/shared/report_controller.py", "RemCardReportController"),
    ]
    root = PROJECT_ROOT
    for role, relative_path, class_name in cases:
        source_path = root / relative_path
        source_text = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source_text)
        class_defs = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name]
        if not class_defs:
            return False, f"{role}: {class_name} class not found"
        if class_name == "RemCardReportController":
            base_names = [
                base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
                for base in class_defs[0].bases
            ]
            if "QObject" not in base_names:
                return False, "shared: RemCardReportController must inherit QObject for queued report callbacks"
        methods = {node.name: node for node in class_defs[0].body if isinstance(node, ast.FunctionDef)}

        required_slots = {
            "_on_daily_report_collected": "dict",
            "_on_daily_report_error": "str",
            "_on_full_report_collected": "list",
            "_on_full_report_error": "str",
        }
        def has_slot_decorator(method: ast.FunctionDef, slot_arg: str) -> bool:
            for decorator in method.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                if not isinstance(decorator.func, ast.Name) or decorator.func.id != "Slot":
                    continue
                if not decorator.args:
                    continue
                arg = decorator.args[0]
                if isinstance(arg, ast.Name) and arg.id == slot_arg:
                    return True
            return False

        for method_name, slot_arg in required_slots.items():
            method = methods.get(method_name)
            if method is None:
                return False, f"{role}: {method_name} not found"
            if not has_slot_decorator(method, slot_arg):
                return False, f"{role}: {method_name} must be a Qt Slot({slot_arg})"

        daily_method_name = "run_daily_report" if class_name == "RemCardReportController" else "on_daily_report_requested"
        full_method_name = "run_full_report" if class_name == "RemCardReportController" else "on_full_report_requested"
        daily_method = methods.get(daily_method_name)
        full_method = methods.get(full_method_name)
        if daily_method is None or full_method is None:
            return False, f"{role}: report request methods not found"
        daily_source = _cached_source_segment(source_text, daily_method) or ""
        full_source = _cached_source_segment(source_text, full_method) or ""
        if "def on_finished" in daily_source or "def on_error" in daily_source:
            return False, f"{role}: daily report must not use nested callbacks"
        if "def on_finished" in full_source or "def on_error" in full_source:
            return False, f"{role}: full report must not use nested callbacks"
        if "finished.connect(self._on_daily_report_collected)" not in daily_source:
            return False, f"{role}: daily report must connect to QObject slot"
        if "error.connect(self._on_daily_report_error)" not in daily_source:
            return False, f"{role}: daily report error must connect to QObject slot"
        if "finished.connect(self._on_full_report_collected)" not in full_source:
            return False, f"{role}: full report must connect to QObject slot"
        if "error.connect(self._on_full_report_error)" not in full_source:
            return False, f"{role}: full report error must connect to QObject slot"

    return True, "ok"


def _check_pdf_build_runs_in_worker(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    worker_source = (PROJECT_ROOT / "ui" / "shared" / "pdf_build_worker.py").read_text(encoding="utf-8")
    if "class PdfBuildWorker(QThread)" not in worker_source or "ReportBuilder.build_pdf" not in worker_source:
        return False, "PdfBuildWorker must own ReportBuilder.build_pdf"

    checked_methods = {
        "ui/shared/report_controller.py": [
            "_on_daily_report_collected",
            "_on_full_report_collected",
        ],
        "ui/doctor_view/components/beds_selection_widget.py": [
            "_on_daily_report_collected",
            "_on_full_report_collected",
        ],
        "ui/nurse_view/components/nurse_beds_selection_widget.py": [
            "_on_daily_report_collected",
            "_on_full_report_collected",
        ],
        "ui/rem_card_sectors/sector_print.py": [
            "on_data_collected",
            "on_full_data_collected",
        ],
        "ui/nurse_view/sectors/nurse_sector_print.py": [
            "on_data",
            "on_full",
        ],
    }
    for relative_path, method_names in checked_methods.items():
        source_path = PROJECT_ROOT / relative_path
        source_text = source_path.read_text(encoding="utf-8")
        if "PdfBuildWorker" not in source_text or "pdf_worker" not in source_text:
            return False, f"{relative_path}: PdfBuildWorker is not retained by the widget"
        tree = ast.parse(source_text)
        methods = {
            node.name: _cached_source_segment(source_text, node) or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        for method_name in method_names:
            method_source = methods.get(method_name, "")
            if not method_source:
                return False, f"{relative_path}: {method_name} not found"
            if "ReportBuilder.build_pdf" in method_source:
                return False, f"{relative_path}: {method_name} still builds PDF in UI callback"
            if "_start" not in method_source:
                return False, f"{relative_path}: {method_name} does not delegate PDF build"
    return True, "ok"


def _check_report_pdf_opening_uses_shared_helper(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    opener_source = (PROJECT_ROOT / "ui" / "shared" / "pdf_opener.py").read_text(encoding="utf-8")
    if "def open_pdf_file" not in opener_source or "os.startfile" not in opener_source:
        return False, "shared PDF opener must use os.startfile on Windows"

    checked_files = (
        "ui/shared/report_controller.py",
        "ui/doctor_view/components/beds_selection_widget.py",
        "ui/nurse_view/components/nurse_beds_selection_widget.py",
        "ui/rem_card_sectors/sector_print.py",
        "ui/nurse_view/sectors/nurse_sector_print.py",
    )
    for relative_path in checked_files:
        source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        if "QDesktopServices.openUrl" in source or "QUrl.fromLocalFile" in source:
            return False, f"{relative_path}: PDF opening must use shared helper"
        if "open_pdf_file" not in source:
            return False, f"{relative_path}: shared PDF opener not used"
    return True, "ok"


def _ast_contains_name(node, name: str) -> bool:
    return any(isinstance(child, ast.Name) and child.id == name for child in ast.walk(node))


def _w1_helper_has_initial_status_day_guard(helper_method: ast.FunctionDef) -> bool:
    for node in ast.walk(helper_method):
        if not isinstance(node, ast.Compare):
            continue
        if not isinstance(node.left, ast.Name) or node.left.id != "current_start":
            continue
        if len(node.ops) != 2 or not isinstance(node.ops[0], ast.LtE) or not isinstance(node.ops[1], ast.Lt):
            continue
        if len(node.comparators) != 2:
            continue
        left_bound, right_bound = node.comparators
        if (
            isinstance(left_bound, ast.Name)
            and left_bound.id == "value"
            and isinstance(right_bound, ast.Name)
            and right_bound.id == "current_end"
        ):
            return True
    return False


def _w1_archive_assigns_initial_status_helper_result(archive_method: ast.FunctionDef) -> bool:
    for node in ast.walk(archive_method):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "should_ensure_initial_status" for target in node.targets):
            continue
        value = node.value
        if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Attribute):
            continue
        if value.func.attr != "_should_ensure_initial_status_for_date":
            continue
        if len(value.args) == 1 and isinstance(value.args[0], ast.Name) and value.args[0].id == "selected_date":
            return True
    return False


def _w1_archive_defers_initial_status_write(archive_method: ast.FunctionDef) -> bool:
    ensure_calls = [
        node
        for node in ast.walk(archive_method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "ensure_initial_status"
    ]
    if ensure_calls:
        return False

    for node in ast.walk(archive_method):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "force_reload_all":
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "ensure_initial_status"
                and isinstance(keyword.value, ast.Name)
                and keyword.value.id == "should_ensure_initial_status"
            ):
                return True
    return False


def _check_w1_yesterday_card_skips_status_write_and_defers(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    source_path = PROJECT_ROOT / "ui" / "doctor_view" / "doctor_remcard_widget.py"
    source_text = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text)

    class_defs = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DoctorRemCardWidget"]
    if not class_defs:
        return False, "DoctorRemCardWidget class not found"
    methods = {node.name: node for node in class_defs[0].body if isinstance(node, ast.FunctionDef)}

    load_method = methods.get("load_patient_card")
    if load_method is None:
        return False, "load_patient_card not found"
    ensure_kw = [
        (arg, default)
        for arg, default in zip(load_method.args.kwonlyargs, load_method.args.kw_defaults)
        if arg.arg == "ensure_initial_status"
    ]
    if not ensure_kw or not isinstance(ensure_kw[0][1], ast.Constant) or ensure_kw[0][1].value is not None:
        return False, "load_patient_card must accept ensure_initial_status=None keyword"

    yest_clicked_source = _cached_source_segment(source_text, methods.get("on_yest_card_clicked")) or ""
    if "QTimer.singleShot" not in yest_clicked_source or "safe_load_archived_card" not in yest_clicked_source:
        return False, "open-card yesterday action must defer archive loading through QTimer.singleShot"

    select_source = _cached_source_segment(source_text, methods.get("on_patient_selected_from_list")) or ""
    if "QTimer.singleShot" not in select_source or "_open_w1_yesterday_card" not in select_source:
        return False, "W1 yesterday action must defer loading through QTimer.singleShot"

    open_w1_source = _cached_source_segment(source_text, methods.get("_open_w1_yesterday_card")) or ""
    if "ensure_initial_status=False" not in open_w1_source:
        return False, "W1 yesterday card must skip initial status writes"

    helper_method = methods.get("_should_ensure_initial_status_for_date")
    if helper_method is None:
        return False, "_should_ensure_initial_status_for_date helper not found"
    if not _w1_helper_has_initial_status_day_guard(helper_method):
        return False, "_should_ensure_initial_status_for_date must guard current_start <= value < current_end"

    archive_method = methods.get("safe_load_archived_card")
    if archive_method is None:
        return False, "safe_load_archived_card not found"
    archive_source = _cached_source_segment(source_text, archive_method) or ""

    if not _w1_archive_assigns_initial_status_helper_result(archive_method):
        return False, "safe_load_archived_card must assign helper result for selected_date"
    if not _w1_archive_defers_initial_status_write(archive_method):
        return False, "safe_load_archived_card must defer initial status writes to force_reload_all"

    if "skip initial status write for historical card" not in archive_source:
        return False, "safe_load_archived_card must log skipped historical status writes"

    return True, "ok"


def _check_chart_clears_on_card_context_change(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    root = PROJECT_ROOT

    chart_source = (root / "ui" / "shared" / "chart_widget.py").read_text(encoding="utf-8")
    if "def clear_for_context" not in chart_source:
        return False, "ChartWidget.clear_for_context not found"
    if "self.scatter_vitals.setData([])" not in chart_source:
        return False, "ChartWidget.clear_for_context must clear previous vital markers"

    cases = [
        ("doctor", "ui/doctor_view/doctor_remcard_widget.py", "DoctorRemCardWidget"),
        ("nurse", "ui/nurse_view/nurse_main_widget.py", "NurseMainWidget"),
    ]
    for role, relative_path, class_name in cases:
        source_path = root / relative_path
        source_text = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source_text)
        class_defs = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name]
        if not class_defs:
            return False, f"{role}: {class_name} class not found"
        methods = {node.name: node for node in class_defs[0].body if isinstance(node, ast.FunctionDef)}
        load_method = methods.get("load_patient_card")
        if load_method is None:
            return False, f"{role}: load_patient_card not found"
        load_source = _cached_source_segment(source_text, load_method) or ""
        chart_context_source = load_source
        if role == "doctor":
            chart_context_source = "\n".join(
                (
                    load_source,
                    _cached_source_segment(
                        source_text,
                        methods.get("_update_chart_context_for_patient_open"),
                    ) or "",
                )
            )
        if "clear_for_context" not in chart_context_source:
            return False, f"{role}: chart must be cleared immediately on patient card switch"
        if role == "doctor":
            match_pos = chart_context_source.find("chart_matches_target = self._chart_matches_context")
            assign_pos = chart_context_source.find("self.chart.admission_id = admission_id")
            if match_pos < 0 or assign_pos < 0 or match_pos > assign_pos:
                return False, "doctor: chart context must be checked before assigning the new admission_id"

    probe = textwrap.dedent(
        """
        import os
        from datetime import datetime, timedelta

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

        from _local_rem_card_bootstrap import bootstrap_local_rem_card

        bootstrap_local_rem_card()

        from PySide6.QtWidgets import QApplication
        from rem_card.ui.shared.chart_widget import ChartWidget


        class Vital:
            def __init__(self, idx: int, timestamp: datetime, sys_value: int, dia_value: int):
                self.id = idx
                self.timestamp = timestamp
                self.sys = sys_value
                self.dia = dia_value
                self.pulse = 70 + idx
                self.temp = 36.5
                self.spo2 = 98
                self.rr = None
                self.cvp = None
                self.updated_at = f"2026-01-01T00:00:{idx:02d}"


        app = QApplication.instance() or QApplication([])
        chart = ChartWidget()
        start = datetime(2026, 1, 1, 8, 0, 0)
        vitals = [
            Vital(1, start + timedelta(hours=1), 120, 70),
            Vital(2, start + timedelta(hours=2), 125, 75),
            Vital(3, start + timedelta(hours=3), 118, 68),
        ]
        try:
            chart.update_data(vitals, start, active_intervals=[])
            app.processEvents()
            fill = chart.fill_items[0]
            if fill.path().isEmpty():
                raise AssertionError("chart fill path was not created for blood-pressure data")

            chart.clear_for_context(admission_id=999, start_time=start + timedelta(days=1))
            app.processEvents()
            if not fill.path().isEmpty():
                raise AssertionError(
                    "ChartWidget.clear_for_context must clear stale blood-pressure fill path, "
                    f"elements={fill.path().elementCount()} bounds={fill.boundingRect()}"
                )

            chart.update_data(vitals, start, active_intervals=[])
            app.processEvents()
            chart.update_data([], start + timedelta(days=1), active_intervals=[])
            app.processEvents()
            if not fill.path().isEmpty():
                raise AssertionError(
                    "ChartWidget.update_data must clear stale blood-pressure fill path for empty vitals, "
                    f"elements={fill.path().elementCount()} bounds={fill.boundingRect()}"
                )
        finally:
            chart.deleteLater()
            app.processEvents()
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(PROJECT_ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=45,
    )
    if result.returncode != 0:
        return False, f"ChartWidget runtime probe failed rc={result.returncode}: {(result.stderr or result.stdout)[-800:]}"

    return True, "ok"


def _check_chart_heavy_redraw_performance(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    probe = textwrap.dedent(
        """
        import json
        import os
        import time
        from datetime import datetime, timedelta

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

        from _local_rem_card_bootstrap import bootstrap_local_rem_card

        bootstrap_local_rem_card()

        from PySide6.QtWidgets import QApplication
        from rem_card.ui.shared.chart_widget import ChartWidget


        class Vital:
            def __init__(self, idx: int, timestamp: datetime, updated_at: str):
                self.id = idx
                self.timestamp = timestamp
                self.sys = 110 + (idx % 25)
                self.dia = 65 + (idx % 15)
                self.pulse = 70 + (idx % 20)
                self.temp = 36.2 + ((idx % 7) * 0.1)
                self.spo2 = 95 + (idx % 4)
                self.rr = 15 + (idx % 6)
                self.cvp = 5 + (idx % 3)
                self.updated_at = updated_at

            def clone(self):
                copied = Vital(self.id, self.timestamp, self.updated_at)
                copied.sys = self.sys
                copied.dia = self.dia
                copied.pulse = self.pulse
                copied.temp = self.temp
                copied.spo2 = self.spo2
                copied.rr = self.rr
                copied.cvp = self.cvp
                return copied


        def percentile(values: list[float], p: float) -> float:
            arr = sorted(values)
            k = (len(arr) - 1) * p
            f = int(k)
            c = min(f + 1, len(arr) - 1)
            if f == c:
                return arr[f]
            return arr[f] + (arr[c] - arr[f]) * (k - f)


        app = QApplication.instance() or QApplication([])
        chart = ChartWidget()
        start = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
        base = start - timedelta(hours=24)
        vitals = [
            Vital(i + 1, base + timedelta(minutes=15 * i), f"2026-01-01T00:00:{i % 60:02d}")
            for i in range(220)
        ]
        intervals = []
        current = (start - timedelta(hours=36)).replace(second=0, microsecond=0)
        for _idx in range(180):
            active_start = current
            active_end = active_start + timedelta(minutes=15)
            intervals.append((active_start, active_end))
            current = active_end + timedelta(minutes=5)

        try:
            chart.update_data(vitals, start, active_intervals=intervals)
            app.processEvents()

            samples = []
            for idx in range(5):
                mutated = [vital.clone() for vital in vitals]
                mutated[-1].pulse += idx + 1
                mutated[-1].updated_at = f"2030-01-01T00:00:{idx:02d}"
                started = time.perf_counter()
                chart.update_data(mutated, start, active_intervals=intervals)
                app.processEvents()
                samples.append((time.perf_counter() - started) * 1000.0)

            p95 = percentile(samples, 0.95)
            limit_ms = float(os.environ.get("REMCARD_CHART_HEAVY_REDRAW_LIMIT_MS", "200"))
            rendered_curves = len(chart.curve_items)
            rendered_fills = len(chart.fill_items)
            if p95 > limit_ms:
                raise AssertionError(
                    f"heavy chart redraw p95={p95:.1f}ms > {limit_ms:.1f}ms; "
                    f"samples={[round(v, 1) for v in samples]}"
                )
            if rendered_curves > 20 or rendered_fills > 4:
                raise AssertionError(f"chart must reuse plot items, got curves={rendered_curves}, fills={rendered_fills}")
            print(json.dumps({"details": f"p95={p95:.1f}ms samples={[round(v, 1) for v in samples]}"}))
        finally:
            chart.deleteLater()
            app.processEvents()
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(PROJECT_ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    if result.returncode != 0:
        return False, f"ChartWidget heavy redraw probe failed rc={result.returncode}: {(result.stderr or result.stdout)[-800:]}"
    details = "ok"
    try:
        details = str(json.loads((result.stdout or "{}").splitlines()[-1]).get("details") or "ok")
    except Exception:
        details = "ok"
    return True, details


def _check_chart_snapshot_dedupes_unchanged_payload(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from datetime import datetime, timedelta
    from types import MethodType, SimpleNamespace

    from rem_card.ui.doctor_view.doctor_remcard_widget import DoctorRemCardWidget
    from rem_card.ui.nurse_view.nurse_main_widget import NurseMainWidget
    from rem_card.ui.shared.chart_widget import ChartWidget

    class Vital:
        def __init__(self, idx: int, timestamp: datetime, updated_at: str):
            self.id = idx
            self.timestamp = timestamp
            self.sys = 120 + idx
            self.dia = 70 + idx
            self.pulse = 80 + idx
            self.temp = 36.5
            self.spo2 = 98
            self.updated_at = updated_at

    class FakeChart:
        def __init__(self):
            self.calls = 0
            self.calls_payload = []

        @staticmethod
        def _normalize_key_dt(value):
            return ChartWidget._normalize_key_dt(value)

        @classmethod
        def _build_vitals_key(cls, vitals):
            return ChartWidget._build_vitals_key(vitals)

        @classmethod
        def _build_intervals_key(cls, active_intervals):
            return ChartWidget._build_intervals_key(active_intervals)

        def update_data(self, vitals, start_time, active_intervals=None):
            self.calls += 1
            self.calls_payload.append((len(vitals or []), start_time, tuple(active_intervals or ())))

    start = datetime(2026, 5, 3, 8, 0, 0)
    vitals = [
        Vital(1, start - timedelta(hours=2), "2026-05-03T08:01:00"),
        Vital(2, start + timedelta(hours=1), "2026-05-03T09:01:00"),
    ]
    intervals = [(start - timedelta(hours=1), start + timedelta(hours=2))]
    vitals_snapshot = {
        "admission_id": 77,
        "scope": "patient_vitals",
        "version": 10,
        "start_dt": start,
        "vitals_extended": vitals,
        "chart_active_intervals": intervals,
    }
    full_snapshot = {
        **vitals_snapshot,
        "scope": "patient_card",
        "balance_runtime": {"active_intervals": intervals, "totals": {}},
    }
    changed_snapshot = {
        **full_snapshot,
        "version": 11,
        "vitals_extended": [
            vitals[0],
            Vital(2, start + timedelta(hours=1), "2026-05-03T09:02:00"),
        ],
    }

    cases = [
        ("doctor", DoctorRemCardWidget, SimpleNamespace(current_admission_id=77)),
        ("nurse", NurseMainWidget, SimpleNamespace(current_admission_id=77)),
    ]
    for role, widget_cls, layout_manager in cases:
        fake = SimpleNamespace(
            admission_id=77,
            layout_manager=layout_manager,
            chart=FakeChart(),
            _last_applied_chart_signature=None,
        )
        fake._chart_snapshot_signature = MethodType(widget_cls._chart_snapshot_signature, fake)
        widget_cls._update_chart_from_snapshot(fake, vitals_snapshot)
        widget_cls._update_chart_from_snapshot(fake, full_snapshot)
        if fake.chart.calls != 1:
            return False, f"{role}: unchanged full snapshot must not call chart.update_data twice"
        widget_cls._update_chart_from_snapshot(fake, changed_snapshot)
        if fake.chart.calls != 2:
            return False, f"{role}: changed vitals payload must redraw chart"

    return True, "ok"


def _check_journal_prewarm_is_opt_in(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    root = PROJECT_ROOT
    cases = [
        ("doctor", root / "ui" / "doctor_view" / "doctor_remcard_widget.py"),
        ("nurse", root / "ui" / "nurse_view" / "nurse_main_widget.py"),
    ]
    for role, source_path in cases:
        source = source_path.read_text(encoding="utf-8")
        if 'JOURNAL_PREWARM_ENABLED = os.environ.get("REMCARD_JOURNAL_PREWARM", "0") == "1"' not in source:
            return False, f"{role}: journal prewarm must be disabled by default"
        if 'JOURNAL_WIDGET_PREWARM_ENABLED = os.environ.get("REMCARD_JOURNAL_WIDGET_PREWARM", "0") == "1"' not in source:
            return False, f"{role}: journal widget prewarm must be disabled by default"
        if "if JOURNAL_PREWARM_ENABLED:" not in source:
            return False, f"{role}: startup journal prewarm timer must be gated"

    return True, "ok"


def _check_w1_beds_refreshes_on_vitals_change(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    root = PROJECT_ROOT
    cases = [
        ("doctor", root / "ui" / "doctor_view" / "doctor_main_widget.py"),
        ("nurse", root / "ui" / "nurse_view" / "nurse_main_widget.py"),
    ]
    required_entities = {
        "vitals",
        "vital_settings",
        "patient_status_events",
        "fluids",
        "orders",
        "administrations",
    }
    for role, source_path in cases:
        source = source_path.read_text(encoding="utf-8")
        if "W1_REFRESH_ENTITIES" not in source:
            return False, f"{role}: W1 refresh entity set not found"
        if "queue_if_running=False" not in source:
            return False, f"{role}: startup W1 refresh should not queue a duplicate refresh"
        missing = [entity for entity in required_entities if f'"{entity}"' not in source]
        if missing:
            return False, f"{role}: W1 refresh entities missing {missing}"
        tree = ast.parse(source)
        methods = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_on_data_changes"
        ]
        if not methods:
            return False, f"{role}: _on_data_changes not found"
        method_source = _cached_source_segment(source, methods[0]) or ""
        if "W1_REFRESH_ENTITIES" not in method_source:
            return False, f"{role}: W1 beds refresh must use W1_REFRESH_ENTITIES"

    widget_cases = [
        ("doctor", root / "ui" / "doctor_view" / "components" / "beds_selection_widget.py"),
        ("nurse", root / "ui" / "nurse_view" / "components" / "nurse_beds_selection_widget.py"),
    ]
    for role, source_path in widget_cases:
        source = source_path.read_text(encoding="utf-8")
        if "def refresh(self, *, queue_if_running: bool = True)" not in source:
            return False, f"{role}: W1 refresh must support non-queued startup refresh"
        if "if queue_if_running:" not in source:
            return False, f"{role}: W1 refresh must respect queue_if_running"
        for marker in (
            "QCoreApplication.closingDown()",
            "QThread.currentThread() is not self.thread()",
            "QTimer.singleShot(0, lambda: self.refresh(queue_if_running=queue_if_running))",
            "not _qt_is_valid(self)",
        ):
            if marker not in source:
                return False, f"{role}: W1 beds refresh missing lifecycle guard marker: {marker}"

    return True, "ok"


def _check_lazy_w1_shell_contract(root: Path) -> tuple[bool, str]:
    shell_path = root / "ui" / "shared" / "lightweight_w1_shell.py"
    if not shell_path.exists():
        return False, "Lightweight W1 shell module not found"
    shell_source = shell_path.read_text(encoding="utf-8")
    for marker in (
        "class LightweightW1Shell",
        "selection_mode_changed = Signal(str)",
        "beds_selection_widget",
        "def set_patient_selection_mode",
        "def refresh_beds",
        "def refresh_w1a",
        "def shutdown",
    ):
        if marker not in shell_source:
            return False, f"Lightweight W1 shell missing contract marker: {marker}"
    if "self.bottom_row = QWidget(self)" not in shell_source:
        return False, "Lightweight W1 shell bottom_row must not be a top-level QWidget"
    if "RemCardLayoutManager" in shell_source or "NurseRemCardLayoutManager" in shell_source:
        return False, "Lightweight W1 shell must not instantiate full card layouts"
    return True, "ok"

from __future__ import annotations

import os
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from rem_card.data.dto.remcard_dto import OrderDTO, OrderStatus, OrderType
from rem_card.services.order_service import OrderConflictError
from rem_card.services.read_coordinator import OrdersContext
from rem_card.ui.doctor_view.components.order_template_builder import build_orders_from_template
from rem_card.ui.doctor_view.orders_widget import OrdersWidget
from rem_card.ui.shared.components.vital_settings_dialog import VitalSettingsDialog
from rem_card.ui.shared.orders_model import OrdersModel


class FakeOrdersService:
    def __init__(self):
        self.cell_write_calls = 0
        self.commit_calls = 0
        self.commit_payloads = []
        self.write_descriptions = []
        self.commit_result = None
        self.vital_settings = {"ad": 1, "pulse": 1, "temp": 1, "spo2": 1, "rr": 0, "cvp": 0}

    @staticmethod
    def get_day_period(value):
        start = value.replace(hour=8, minute=0, second=0, microsecond=0)
        if value.hour < 8:
            start -= timedelta(days=1)
        return start, start + timedelta(days=1)

    def forbidden_cell_write(self, *_args, **_kwargs):
        self.cell_write_calls += 1
        raise AssertionError("cell draft reached persistence")

    def commit_local_order_draft(self, *_args, **_kwargs):
        self.commit_calls += 1
        self.commit_payloads.append(_kwargs)
        return self.commit_result

    def get_vital_settings_cached(self, *_args, **_kwargs):
        return dict(self.vital_settings)

    def save_vital_settings(self, _admission_id, _date, settings):
        self.vital_settings.update({key: value for key, value in settings.items() if not key.startswith("__")})

    def enqueue_write(self, *, description, operation, on_success=None, on_error=None, **_kwargs):
        self.write_descriptions.append(description)
        try:
            result = operation()
        except Exception as exc:
            if on_error:
                on_error(exc)
            return
        if on_success:
            on_success(result)


def _make_widget():
    app = QApplication.instance() or QApplication([])
    del app
    shift = datetime(2026, 7, 13, 8, 0)
    service = FakeOrdersService()
    widget = OrdersWidget(service=service, admission_id=1, shift_date=shift, defer_ui=True)
    widget.model = OrdersModel(service, 1, shift)
    order = OrderDTO(
        id=1,
        admission_id=1,
        drug_key="nacl",
        latin="NaCl",
        type=OrderType.MEDICATION,
        status=OrderStatus.ACTIVE,
        dose_value=500,
        dose_unit="ml",
        duration_min=0,
        is_committed=1,
        revision=4,
        created_at=shift,
    )
    snapshot = {
        "admission_id": 1,
        "shift_date": shift,
        "only_committed": True,
        "orders": [order],
        "admin_rows": [],
        "has_any_draft": False,
        "has_any_orders": True,
        "has_any_administrations": False,
    }
    widget.model.apply_snapshot(snapshot)
    widget._capture_local_draft_baseline(snapshot)
    return widget, service


def _committed_snapshot_for_current_mark(widget, *, change_id=5):
    order = deepcopy(widget.model.orders[0])
    order.is_committed = 1
    admin = next(iter(widget.model.admin_map.values()))
    return {
        "admission_id": widget.admission_id,
        "shift_date": widget.shift_date,
        "only_committed": True,
        "orders": [order],
        "admin_rows": [
            {
                "id": 101,
                "order_id": order.id,
                "chain_id": admin.chain_id,
                "big_chain_id": admin.big_chain_id,
                "cell_role": admin.cell_role,
                "planned_time": admin.planned_time.isoformat(),
                "actual_time": None,
                "performer_id": None,
                "status": admin.status,
                "version": 1,
                "is_committed": 1,
                "comment": admin.comment,
                "volume_ml": admin.volume_ml,
                "updated_at": "2026-07-13T08:01:00",
                "last_modified_by": None,
            }
        ],
        "has_any_draft": False,
        "has_any_orders": True,
        "has_any_administrations": True,
        "change_id": change_id,
        "version": change_id,
        "source": "post_finalize",
        "load_trace_id": "test-write-result",
    }


def test_cell_click_is_local_and_toggle_back_collapses_to_noop():
    widget, service = _make_widget()
    try:
        widget._enqueue_write = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("draft click enqueued a write")
        )
        index = widget.model.index(0, 1)
        widget._handle_cell_action(index, "orders_left_click", service.forbidden_cell_write)

        assert service.cell_write_calls == 0
        assert len(widget.model.admin_map) == 1
        assert len(widget._local_draft_dirty_admin_keys) == 1
        assert widget.has_drafts()

        widget._recent_admin_cell_clicks.clear()
        widget._handle_cell_action(index, "orders_left_click", service.forbidden_cell_write)

        assert service.cell_write_calls == 0
        assert widget.model.admin_map == {}
        assert widget._local_draft_dirty_admin_keys == set()
        assert not widget.has_drafts()
    finally:
        widget.shutdown()


def test_discard_restores_baseline_without_queue_or_database():
    widget, service = _make_widget()
    try:
        queued = []
        widget._enqueue_write = lambda *args, **kwargs: queued.append((args, kwargs))
        widget._handle_cell_action(
            widget.model.index(0, 1),
            "orders_left_click",
            service.forbidden_cell_write,
        )
        assert widget.has_drafts()

        widget.clear_drafts()

        assert queued == []
        assert widget.model.admin_map == {}
        assert len(widget.model.orders) == 1
        assert widget.model.orders[0].is_committed == 1
        assert not widget.has_drafts()
    finally:
        widget.shutdown()


def test_save_submits_exactly_one_write_task():
    widget, service = _make_widget()
    try:
        widget._handle_cell_action(
            widget.model.index(0, 1),
            "orders_left_click",
            service.forbidden_cell_write,
        )
        queued = []

        def capture(description, operation, **kwargs):
            queued.append((description, operation, kwargs))

        widget._enqueue_write = capture
        widget.finalize_card()

        assert len(queued) == 1
        assert queued[0][0] == "orders_finalize:1"
        queued[0][1]()
        assert service.commit_calls == 1
    finally:
        widget.shutdown()


def test_post_finalize_snapshot_emits_committed_orders_for_balance_before_refresh():
    widget, service = _make_widget()
    try:
        widget._handle_cell_action(
            widget.model.index(0, 1),
            "orders_left_click",
            service.forbidden_cell_write,
        )
        service.commit_result = {
            "order_id_map": {},
            "snapshot": _committed_snapshot_for_current_mark(widget, change_id=17),
        }
        payloads = []
        events = []
        widget.committedOrdersBalanceReady.connect(
            lambda payload: (payloads.append(payload), events.append("committed"))
        )
        widget.localBalanceChanged.connect(lambda: events.append("recalculate"))

        widget.finalize_card()

        assert len(payloads) == 1
        assert payloads[0]["change_id"] == 17
        assert payloads[0]["source"] == "post_finalize"
        assert len(payloads[0]["orders"]) == 1
        assert len(payloads[0]["orders"][0].administrations) == 1
        assert events[-2:] == ["committed", "recalculate"]
    finally:
        widget.shutdown()


def test_double_save_queues_once_and_error_keeps_overlay_for_retry():
    widget, service = _make_widget()
    try:
        widget._handle_cell_action(
            widget.model.index(0, 1),
            "orders_left_click",
            service.forbidden_cell_write,
        )
        queued = []

        def capture(description, operation, **kwargs):
            queued.append((description, operation, kwargs))

        widget._enqueue_write = capture
        widget.finalize_card()
        widget.finalize_card()

        assert len(queued) == 1
        assert widget.has_drafts()
        assert len(widget.model.admin_map) == 1

        queued[0][2]["on_error"](OrderConflictError("concurrent nurse change"))

        assert widget.has_drafts()
        assert len(widget.model.admin_map) == 1
        widget.finalize_card()
        assert len(queued) == 2
    finally:
        widget.shutdown()


def test_burst_of_40_distinct_cells_never_queues_persistence():
    widget, service = _make_widget()
    try:
        second = deepcopy(widget.model.orders[0])
        second.id = 2
        second.latin = "Glucose"
        widget.model.beginInsertRows(widget.model.index(-1, -1), 1, 1)
        widget.model.orders.append(second)
        widget.model.endInsertRows()
        widget._enqueue_write = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("burst draft enqueued a write")
        )

        for row in range(2):
            for column in range(1, 21):
                widget._handle_cell_action(
                    widget.model.index(row, column),
                    "orders_left_click",
                    service.forbidden_cell_write,
                )

        assert service.cell_write_calls == 0
        assert len(widget.model.admin_map) == 40
        assert len(widget._local_draft_dirty_admin_keys) == 40
    finally:
        widget.shutdown()


def test_24_hour_chain_is_built_entirely_in_memory():
    widget, service = _make_widget()
    try:
        widget.model.orders[0].duration_min = 24 * 60
        widget._enqueue_write = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("long chain enqueued a write")
        )

        widget._handle_cell_action(
            widget.model.index(0, 1),
            "orders_left_click",
            service.forbidden_cell_write,
        )

        assert service.cell_write_calls == 0
        assert len(widget.model.admin_map) == 24
        assert len(widget._local_draft_dirty_admin_keys) == 24
        roles = [
            admin.cell_role
            for _key, admin in sorted(widget.model.admin_map.items(), key=lambda item: item[0][1])
        ]
        assert roles[0] == "start"
        assert roles[-1] == "end"
        assert set(roles[1:-1]) == {"body"}
    finally:
        widget.shutdown()


def test_save_applies_snapshot_from_write_result_without_async_refresh():
    widget, service = _make_widget()
    try:
        widget._handle_cell_action(
            widget.model.index(0, 1),
            "orders_left_click",
            service.forbidden_cell_write,
        )
        service.commit_result = {
            "order_id_map": {},
            "snapshot": _committed_snapshot_for_current_mark(widget),
        }
        refresh_sources = []
        widget._refresh_model = lambda *, source="refresh": refresh_sources.append(source)

        widget.finalize_card()

        assert service.commit_calls == 1
        assert refresh_sources == []
        assert not widget.has_drafts()
        assert len(widget.model.admin_map) == 1
        committed_admin = next(iter(widget.model.admin_map.values()))
        assert committed_admin.id == 101
        assert committed_admin.is_committed == 1
    finally:
        widget.shutdown()


def test_save_publishes_write_snapshot_to_read_coordinator():
    widget, service = _make_widget()
    accepted = []

    class Coordinator:
        @staticmethod
        def make_orders_context(**kwargs):
            return OrdersContext(**kwargs)

        @staticmethod
        def accept_committed_orders_snapshot(context, snapshot):
            accepted.append((context, snapshot))
            return snapshot

    service.read_coordinator = Coordinator()
    try:
        widget._handle_cell_action(
            widget.model.index(0, 1),
            "orders_left_click",
            service.forbidden_cell_write,
        )
        service.commit_result = {
            "order_id_map": {},
            "snapshot": _committed_snapshot_for_current_mark(widget, change_id=28),
        }
        widget._refresh_model = lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("authoritative write snapshot triggered reload")
        )

        widget.finalize_card()

        assert len(accepted) == 1
        assert accepted[0][0].role == "doctor"
        assert accepted[0][0].variant == "committed"
        assert accepted[0][1]["change_id"] == 28
    finally:
        widget.shutdown()


def test_conflict_invalidates_cache_and_forces_authoritative_reload():
    widget, service = _make_widget()
    invalidations = []
    refreshes = []

    class Coordinator:
        @staticmethod
        def make_orders_context(**kwargs):
            return OrdersContext(**kwargs)

        @staticmethod
        def invalidate_orders_for_admission(admission_id, *, reason, shift_date=None):
            invalidations.append((admission_id, reason, shift_date))

    service.read_coordinator = Coordinator()
    try:
        widget._handle_cell_action(
            widget.model.index(0, 1),
            "orders_left_click",
            service.forbidden_cell_write,
        )
        queued = []
        widget._enqueue_write = lambda *args, **kwargs: queued.append((args, kwargs))
        widget._request_snapshot = lambda **kwargs: refreshes.append(kwargs)

        widget.finalize_card()
        queued[0][1]["on_error"](
            OrderConflictError(reason="administration_version_mismatch")
        )

        assert invalidations == [
            (1, "write_conflict:administration_version_mismatch", widget.shift_date)
        ]
        assert refreshes == [
            {
                "force": True,
                "source": "write_conflict",
                "priority": "HIGH",
                "invalidate_reason": "write_conflict",
            }
        ]
        assert not widget.has_drafts()
    finally:
        widget.shutdown()


def test_save_falls_back_to_async_refresh_when_write_snapshot_is_missing():
    widget, service = _make_widget()
    try:
        widget._handle_cell_action(
            widget.model.index(0, 1),
            "orders_left_click",
            service.forbidden_cell_write,
        )
        service.commit_result = {"order_id_map": {}, "snapshot": None}
        refresh_sources = []
        widget._refresh_model = lambda *, source="refresh": refresh_sources.append(source)

        widget.finalize_card()

        assert service.commit_calls == 1
        assert refresh_sources == ["post_finalize"]
        assert not widget.has_drafts()
    finally:
        widget.shutdown()


def test_template_batch_accepts_orders_without_source_ids():
    widget, service = _make_widget()
    try:
        template_engine = SimpleNamespace(
            drugs={
                "template_drug": {
                    "latin": "Medicamentum",
                    "default_dose": 10,
                    "unit": "mg",
                    "admin_type": "bolus",
                }
            },
            forms={},
            admin_types={"bolus": {"name_ru": "в/в болюсно"}},
            dilutions={},
        )
        template_orders = build_orders_from_template(
            template={
                "template_type": "simple",
                "drugs": [{"drug": "template_drug", "dose": 20, "unit": "mg"}],
            },
            engine=template_engine,
            admission_id=1,
            base_time=widget.shift_date,
        )

        assert len(template_orders) == 1
        assert template_orders[0].id is None
        assert widget._insert_local_orders_batch(template_orders)

        inserted = widget.model.orders[-1]
        assert inserted.id < 0
        assert inserted.drug_key == "template_drug"
        assert inserted.dose_value == 20
        assert inserted.is_committed == 0
        assert template_orders[0].id is None
        assert widget.has_drafts()
        assert service.commit_calls == 0
    finally:
        widget.shutdown()


def test_empty_batch_does_not_replace_existing_orders():
    widget, _service = _make_widget()
    try:
        original_order_ids = [order.id for order in widget.model.orders]

        assert not widget._insert_local_orders_batch([], replace_existing=True)

        assert [order.id for order in widget.model.orders] == original_order_ids
        assert widget._local_deleted_orders == {}
        assert not widget.has_drafts()
    finally:
        widget.shutdown()


def test_yesterday_copy_preserves_shifted_schedule_and_chain_locally():
    widget, _service = _make_widget()
    try:
        source_shift = widget.shift_date - timedelta(days=1)
        source = deepcopy(widget.model.orders[0])
        source.id = 77
        source.created_at = source_shift
        source.duration_min = 180
        source_rows = []
        for offset, role in enumerate(("start", "body", "end")):
            source_rows.append(
                {
                    "id": 100 + offset,
                    "order_id": 77,
                    "big_chain_id": "source-chain",
                    "cell_role": role,
                    "planned_time": (source_shift + timedelta(hours=2 + offset)).isoformat(),
                    "status": "planned",
                    "volume_ml": 25.0,
                }
            )
        widget._enqueue_write = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("yesterday copy enqueued a write")
        )

        assert widget._insert_local_orders_batch(
            [source],
            replace_existing=True,
            source_admin_rows=source_rows,
            source_shift_date=source_shift,
        )

        copied_orders = [order for order in widget.model.orders if int(order.id) < 0 and order.status == OrderStatus.ACTIVE]
        assert len(copied_orders) == 1
        copied_order_id = copied_orders[0].id
        copied = sorted(
            (admin for (order_id, _), admin in widget.model.admin_map.items() if order_id == copied_order_id),
            key=lambda admin: admin.planned_time,
        )
        assert [admin.planned_time for admin in copied] == [
            widget.shift_date + timedelta(hours=2 + offset) for offset in range(3)
        ]
        assert [admin.cell_role for admin in copied] == ["start", "body", "end"]
        assert len({admin.big_chain_id for admin in copied}) == 1
        assert copied[0].big_chain_id.startswith("local-copy:")
        assert all(admin.is_committed == 0 for admin in copied)
        assert widget.has_drafts()
    finally:
        widget.shutdown()


def test_legacy_central_draft_switches_doctor_reader_to_full_review_snapshot():
    widget, service = _make_widget()
    try:
        captured = {}

        class Coordinator:
            def make_orders_context(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(cache_key=lambda: (kwargs["variant"], kwargs["admission_id"]))

        service.read_coordinator = Coordinator()
        widget._legacy_central_draft_detected = True

        widget._build_orders_context()

        assert captured["role"] == "doctor"
        assert captured["variant"] == "full"
        assert widget._is_read_only()
    finally:
        widget.shutdown()


def test_legacy_central_draft_save_uses_explicit_legacy_resolution_task():
    widget, _service = _make_widget()
    try:
        widget._legacy_central_draft_detected = True
        widget.model._set_has_any_draft(True, emit_order_column=False)
        queued = []
        widget._enqueue_write = lambda description, operation, **kwargs: queued.append(
            (description, operation, kwargs)
        )

        widget.finalize_card()

        assert len(queued) == 1
        assert queued[0][0] == "orders_finalize_legacy:1"
        assert widget.is_draft_save_pending()
    finally:
        widget.shutdown()


def test_committed_order_delete_disappears_immediately_and_cancel_restores_it():
    widget, service = _make_widget()
    try:
        widget._enqueue_write = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("local delete enqueued a write")
        )
        order = widget.model.orders[0]

        widget._mark_local_order_row_deleted(0, order, was_committed=True)

        assert widget.model.rowCount() == 0
        assert widget.has_drafts()
        assert order.id in widget._local_deleted_orders
        payload = widget._build_local_draft_payload()
        assert len(payload["orders"]) == 1
        assert getattr(payload["orders"][0], "_pending_delete", False)
        assert payload["expected_revisions"] == {order.id: order.revision}
        assert service.commit_calls == 0

        widget.clear_drafts()

        assert widget.model.rowCount() == 1
        assert widget.model.orders[0].id == order.id
        assert widget._local_deleted_orders == {}
        assert not widget.has_drafts()
    finally:
        widget.shutdown()


def test_edited_existing_order_still_becomes_delete_tombstone():
    widget, _service = _make_widget()
    try:
        order = widget.model.orders[0]
        order.is_committed = 0
        order.dose_value = 750

        widget._mark_local_order_row_deleted(0, order, was_committed=False)

        assert widget.model.rowCount() == 0
        assert order.id in widget._local_deleted_orders
        tombstone = widget._local_deleted_orders[order.id][1]
        assert getattr(tombstone, "_pending_delete", False)
        assert widget.has_drafts()
    finally:
        widget.shutdown()


def test_deleting_new_local_cvp_collapses_to_clean_baseline_without_save():
    widget, service = _make_widget()
    try:
        widget._enqueue_write = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("local CVP action enqueued a write")
        )

        cvp_order, created = widget.add_cvp_order_if_missing()

        assert created
        assert cvp_order.id < 0
        assert widget.has_cvp_order()
        assert widget.model.rowCount() == 2
        assert widget.has_drafts()
        assert service.commit_calls == 0

        widget._mark_local_order_row_deleted(1, widget.model.orders[1], was_committed=False)

        assert widget.model.rowCount() == 1
        assert not widget.has_cvp_order()
        assert widget._local_deleted_orders == {}
        assert not widget.has_drafts()
        assert service.commit_calls == 0
    finally:
        widget.shutdown()


def test_deleted_order_is_sent_as_tombstone_only_when_orders_sheet_is_saved():
    widget, service = _make_widget()
    try:
        order = widget.model.orders[0]
        widget._mark_local_order_row_deleted(0, order, was_committed=True)
        queued = []
        widget._enqueue_write = lambda description, operation, **kwargs: queued.append(
            (description, operation, kwargs)
        )

        widget.finalize_card()

        assert len(queued) == 1
        assert service.commit_calls == 0
        queued[0][1]()
        assert service.commit_calls == 1
        saved_orders = service.commit_payloads[0]["orders"]
        assert len(saved_orders) == 1
        assert getattr(saved_orders[0], "_pending_delete", False)
    finally:
        widget.shutdown()


def test_vital_settings_cvp_button_adds_visible_local_order_but_saves_only_setting():
    widget, service = _make_widget()
    try:
        dialog = VitalSettingsDialog(
            service,
            1,
            "2026-07-13",
            cvp_order_exists=widget.has_cvp_order,
            cvp_order_adder=widget.add_cvp_order_if_missing,
        )
        dialog.switches["cvp"].setChecked(True)

        dialog.btn_cvp_order.click()

        assert widget.model.rowCount() == 2
        assert widget.has_cvp_order()
        cvp_order = widget.model.orders[-1]
        assert cvp_order.id < 0
        assert cvp_order.is_committed == 0
        assert widget.has_drafts()
        assert service.write_descriptions == []
        assert service.commit_calls == 0

        dialog.save_settings()

        assert service.write_descriptions == ["save_vital_settings:1:2026-07-13"]
        assert service.vital_settings["cvp"] == 1
        assert service.commit_calls == 0
        assert widget.has_cvp_order()
        assert widget.has_drafts()
    finally:
        widget.shutdown()


def test_manual_cvp_name_variant_is_not_duplicated_by_quick_button():
    widget, _service = _make_widget()
    try:
        manual_cvp = widget.model.orders[0]
        manual_cvp.drug_key = "manual"
        manual_cvp.latin = "ЦВД (см. вод. ст.)"

        existing, created = widget.add_cvp_order_if_missing()

        assert widget.has_cvp_order()
        assert existing is manual_cvp
        assert not created
        assert widget.model.rowCount() == 1
    finally:
        widget.shutdown()

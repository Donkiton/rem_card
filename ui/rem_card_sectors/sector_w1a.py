import time
from datetime import datetime, timedelta

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QLabel, QFrame, QScrollArea, QSizePolicy, QWidget, QVBoxLayout

from rem_card.app.logger import logger
from rem_card.services.order_domain_service import NURSE_MARK_EXECUTED, NURSE_MARK_NOT_EXECUTED
from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.base_sector import BaseSectorWidget
from rem_card.ui.shared.components.nurse_order_card import NurseOrderCard
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from rem_card.ui.shared.display_settings_storage import (
    DisplaySettingsStorage,
    normalize_display_role,
    w1a_upcoming_orders_enabled,
)


W1A_BEFORE_MIN = 60
W1A_AFTER_MIN = 180
W1A_PENDING_MARK_TTL_SEC = 8.0
W1A_TIME_RECOMPUTE_MAX_MS = 60 * 1000
W1A_REFRESH_DEBOUNCE_MS = 150
W1A_REFRESH_ENTITIES = {
    "orders",
    "administrations",
    "patients",
    "admissions",
    "beds",
    "patient_status_events",
}
W1A_REFRESH_SOURCE_PREFIXES = (
    "orders_",
    "doctor_order_mark:",
    "nurse_order_mark:",
    "nurse_order_panel_mark:",
    "patient_bed",
    "status_",
    "archive_",
)


class SectorW1a(BaseSectorWidget):
    """Сектор W1a со списком ближайших назначений по активным пациентам."""

    def __init__(self, service=None, parent=None, role: str | None = "doctor"):
        super().__init__("W1a", parent)
        self.service = service
        self.role = normalize_display_role(role)
        self._display_enabled = self._read_display_enabled()
        self.label.hide()
        self.setFrameStyle(BaseSectorWidget.NoFrame)
        self.setStyleSheet("background: transparent;")

        self.cards = {}
        self._card_signatures = {}
        self.groups = {}
        self._all_data = []
        self._pending_marks = {}
        self._last_content_hash = None
        self._last_change_id = 0
        self._refresh_worker = None
        self._refresh_pending = False
        self._is_shutting_down = False
        self._last_error_ts = 0.0
        self._group_pin_pending = False
        self._group_pin_rerun_requested = False
        self._last_render_signature = None

        self._time_timer = QTimer(self)
        self._time_timer.setSingleShot(True)
        self._time_timer.timeout.connect(self._render_from_cache)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(lambda: self.refresh_data(force=True))

        self.init_ui()
        if self._display_enabled:
            QTimer.singleShot(0, self.refresh_data)

    def init_ui(self):
        if self.layout():
            self.layout().setContentsMargins(3, 5, 5, 4)

        self.main_container = QWidget()
        self.main_container.setObjectName("sector_w1a_main_container")
        self.main_layout_v = QVBoxLayout(self.main_container)
        self.main_layout_v.setContentsMargins(2, 2, 2, 2)
        self.main_layout_v.setSpacing(0)

        self.header_lbl = QLabel("Ближайшие назначения")
        self.header_lbl.setObjectName("sector_header")
        self.header_lbl.setAlignment(Qt.AlignCenter)
        self.header_lbl.setFixedHeight(28)
        self.main_layout_v.addWidget(self.header_lbl)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setStyleSheet("background: transparent; border: none;")

        self.scroll_content = QWidget()
        self.scroll_content.setObjectName("sector_w1a_scroll_content")
        self.scroll_content.setMinimumWidth(0)
        self.scroll_content.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setContentsMargins(0, 3, 0, 0)
        self.scroll_layout.setSpacing(0)
        self.scroll_layout.setAlignment(Qt.AlignTop)

        self.cards_container = QWidget()
        self.cards_container.setMinimumWidth(0)
        self.cards_container.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Maximum)
        self.content_layout = QVBoxLayout(self.cards_container)
        self.content_layout.setContentsMargins(2, 0, 2, 0)
        self.content_layout.setSpacing(3)
        self.content_layout.setAlignment(Qt.AlignTop)

        self.empty_label = QLabel("Нет ближайших назначений")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setStyleSheet("color: #7f8c8d; font-style: italic; padding: 10px 4px;")
        self.content_layout.addWidget(self.empty_label)

        self.scroll_layout.addWidget(self.cards_container, 0, Qt.AlignTop)
        self.scroll_layout.addStretch(1)
        self.scroll_area.setWidget(self.scroll_content)
        self.main_layout_v.addWidget(self.scroll_area)

        self.main_container.setStyleSheet(
            """
            QWidget#sector_w1a_main_container {
                background-color: #f8f9fa;
                border: 1.5px solid #bdc3c7;
                border-radius: 5px;
            }
            QLabel#sector_header {
                font-weight: bold;
                font-size: 13px;
                color: #2c3e50;
                background-color: #e9ecef;
                border: none;
                border-bottom: 1px solid #bdc3c7;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
            }
            QWidget#sector_w1a_scroll_content {
                background-color: transparent;
            }
            QFrame#w1a_patient_group_card {
                background-color: #ffffff;
                border: 1.6px solid #7f9fbd;
                border-radius: 5px;
            }
            QLabel#w1a_patient_group_header {
                background-color: #d7eaf8;
                color: #173b57;
                font-size: 12px;
                font-weight: bold;
                border: none;
                border-bottom: 1.6px solid #7f9fbd;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                padding: 5px 6px;
            }
            QFrame#w1a_patient_group_body {
                background-color: #ffffff;
                border: none;
                border-bottom-left-radius: 4px;
                border-bottom-right-radius: 4px;
            }
            """
        )

        self.set_content(self.main_container)
        self.main_container.setVisible(self._display_enabled)
        self.empty_label.setVisible(False if not self._display_enabled else self.empty_label.isVisible())

    def set_service(self, service):
        if self.service is service:
            return
        self.service = service
        if self._display_enabled:
            self.refresh_data(force=True)

    def apply_display_settings(self):
        next_enabled = self._read_display_enabled()
        if next_enabled == self._display_enabled:
            return
        self._display_enabled = next_enabled
        self.main_container.setVisible(self._display_enabled)
        if self._display_enabled:
            self.refresh_data(force=True)
        else:
            self._sleep_display_disabled()

    def _read_display_enabled(self) -> bool:
        try:
            payload = DisplaySettingsStorage().load()
            return w1a_upcoming_orders_enabled(payload, self.role)
        except Exception:
            return True

    def _sleep_display_disabled(self):
        self._time_timer.stop()
        self._refresh_timer.stop()
        self._refresh_pending = False
        self._group_pin_pending = False
        self._group_pin_rerun_requested = False
        worker = self._refresh_worker
        self._refresh_worker = None
        if worker is not None:
            self._disconnect_worker(worker)
        self._pending_marks.clear()
        self._all_data = []
        self._last_content_hash = None
        self._last_render_signature = None
        self._clear_cards_and_groups()
        self.empty_label.setVisible(False)
        self.cards_container.adjustSize()
        self.cards_container.updateGeometry()

    def _clear_cards_and_groups(self):
        for card in list(self.cards.values()):
            card.setParent(None)
            card.deleteLater()
        self.cards.clear()
        self._card_signatures.clear()
        for group in list(self.groups.values()):
            frame = group.get("frame")
            if frame is not None:
                frame.setParent(None)
                frame.deleteLater()
        self.groups.clear()

    def refresh_data(self, force: bool = False):
        if self._is_shutting_down or not self._display_enabled or not self.service:
            return
        # force is accepted for API compatibility; content hash still gates rendering.
        if self._refresh_worker is not None and self._refresh_worker.isRunning():
            self._refresh_pending = True
            return

        loader = getattr(self.service, "build_w1a_upcoming_orders_snapshot", None)
        if not callable(loader):
            return

        worker = AsyncCallThread(loader, datetime.now())
        self._refresh_worker = worker
        worker.succeeded.connect(self._apply_snapshot)
        worker.failed.connect(self._on_refresh_failed)
        worker.finished.connect(self._on_refresh_finished)
        worker.start()

    def handle_data_changes(self, payload: dict):
        if self._is_shutting_down or not self._display_enabled:
            return
        if not self._payload_should_refresh(payload or {}):
            return
        self._refresh_timer.start(W1A_REFRESH_DEBOUNCE_MS)

    def _payload_should_refresh(self, payload: dict) -> bool:
        if payload.get("forced") or payload.get("gap_detected"):
            return True
        changed_entities = {
            str(entity)
            for entity in (payload.get("changed_entities") or [])
            if entity is not None
        }
        if not changed_entities:
            changed_entities = {
                str(change.get("entity_name") or "")
                for change in (payload.get("changes") or [])
                if change.get("entity_name")
            }
        if changed_entities.intersection(W1A_REFRESH_ENTITIES):
            return True
        force_sources = list(payload.get("force_sources") or [])
        if payload.get("force_source"):
            force_sources.append(str(payload.get("force_source")))
        return any(
            str(source or "").startswith(W1A_REFRESH_SOURCE_PREFIXES)
            for source in force_sources
        )

    def _apply_snapshot(self, snapshot):
        if self._is_shutting_down or not self._display_enabled:
            return
        sender = self.sender()
        if sender is not None and sender is not self._refresh_worker:
            return
        snapshot = dict(snapshot or {})
        content_hash = str(snapshot.get("content_hash") or "")
        self._last_change_id = int(snapshot.get("change_id") or self._last_change_id or 0)

        if content_hash and content_hash == self._last_content_hash and not self._pending_marks:
            self._schedule_next_time_tick()
            return

        self._last_content_hash = content_hash or self._last_content_hash
        self._all_data = [dict(item) for item in (snapshot.get("rows") or [])]
        self._render_from_cache()

    def _on_refresh_failed(self, exc):
        sender = self.sender()
        if sender is not None and sender is not self._refresh_worker:
            return
        now = time.monotonic()
        if now - self._last_error_ts > 15.0:
            self._last_error_ts = now
            logger.warning("W1a upcoming orders refresh failed: %s", exc, exc_info=True)

    def _on_refresh_finished(self):
        worker = self.sender() or self._refresh_worker
        if worker is not None and worker is not self._refresh_worker:
            self._disconnect_worker(worker)
            return
        if worker is not None:
            self._disconnect_worker(worker)
        self._refresh_worker = None
        if self._refresh_pending and not self._is_shutting_down and self._display_enabled:
            self._refresh_pending = False
            QTimer.singleShot(0, lambda: self.refresh_data(force=True))

    def _render_from_cache(self):
        if self._is_shutting_down or not self._display_enabled:
            return
        now = datetime.now()
        visible_data = [
            item
            for item in self._apply_pending_marks(self._all_data)
            if self._is_visible(item, now)
        ]
        render_signature = self._visible_render_signature(visible_data)
        if render_signature == self._last_render_signature and not self._pending_marks:
            self._refresh_visible_card_signals(render_signature[0])
            self._schedule_next_time_tick(now)
            return
        self._last_render_signature = render_signature
        self._sync_cards(visible_data)
        self._schedule_next_time_tick(now)
        self.update()
        self.scroll_area.viewport().update()

    def _sync_cards(self, data_list):
        started_mono = time.monotonic()
        stats = {"created": 0, "updated": 0, "removed": 0, "moved": 0, "groups_removed": 0}
        main_updates_enabled = self.main_container.updatesEnabled()
        cards_updates_enabled = self.cards_container.updatesEnabled()
        if main_updates_enabled:
            self.main_container.setUpdatesEnabled(False)
        if cards_updates_enabled:
            self.cards_container.setUpdatesEnabled(False)
        groups = self._build_patient_groups(data_list)
        new_ids = {int(item["id"]) for item in data_list if item.get("id") is not None}
        try:
            for admin_id in list(self.cards.keys()):
                if admin_id not in new_ids:
                    card = self.cards.pop(admin_id)
                    self._card_signatures.pop(admin_id, None)
                    card.setParent(None)
                    card.deleteLater()
                    stats["removed"] += 1

            new_group_keys = {group["key"] for group in groups}
            for group_key in list(self.groups.keys()):
                if group_key not in new_group_keys:
                    group = self.groups.pop(group_key)
                    group["frame"].setParent(None)
                    group["frame"].deleteLater()
                    stats["groups_removed"] += 1

            if self.empty_label.isVisible() == bool(data_list):
                self.empty_label.setVisible(not data_list)
            for group_index, group_data in enumerate(groups):
                group = self._ensure_patient_group(group_data)
                current_group_index = self.content_layout.indexOf(group["frame"])
                if current_group_index != group_index:
                    self.content_layout.insertWidget(group_index, group["frame"])
                    stats["moved"] += 1

                for order_index, item in enumerate(group_data["items"]):
                    admin_id = int(item["id"])
                    card_data = dict(item)
                    card_data["defer_mark_visual"] = True
                    card_data.pop("patient_name", None)
                    card_data.pop("patient_full_name", None)
                    card_signature = self._card_data_signature(card_data)
                    if admin_id in self.cards:
                        card = self.cards[admin_id]
                        if self._card_signatures.get(admin_id) != card_signature:
                            card.update_data(card_data)
                            self._card_signatures[admin_id] = card_signature
                            stats["updated"] += 1
                    else:
                        card = NurseOrderCard(card_data)
                        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
                        card.statusChanged.connect(self.handle_status_change)
                        card.contentHeightChanged.connect(self._queue_group_frame_pin)
                        self.cards[admin_id] = card
                        self._card_signatures[admin_id] = card_signature
                        stats["created"] += 1

                    current_index = group["layout"].indexOf(card)
                    if current_index != order_index:
                        group["layout"].insertWidget(order_index, card)
                        stats["moved"] += 1
                self._pin_group_frame_height(group)
        finally:
            if cards_updates_enabled:
                self.cards_container.setUpdatesEnabled(True)
            if main_updates_enabled:
                self.main_container.setUpdatesEnabled(True)

        self._refresh_visible_card_signals(new_ids)
        self._queue_group_frame_pin()
        if any(stats.values()):
            logger.debug(
                "W1a sync cards created=%s updated=%s removed=%s moved=%s groups_removed=%s rows=%s elapsed_ms=%.1f",
                stats["created"],
                stats["updated"],
                stats["removed"],
                stats["moved"],
                stats["groups_removed"],
                len(data_list),
                (time.monotonic() - started_mono) * 1000.0,
            )

    def _build_patient_groups(self, data_list):
        grouped = {}
        for item in sorted(data_list, key=self._order_sort_key):
            key = self._patient_group_key(item)
            group = grouped.setdefault(
                key,
                {
                    "key": key,
                    "patient_name": str(item.get("patient_name") or "Пациент").strip() or "Пациент",
                    "bed_number": item.get("bed_number"),
                    "items": [],
                },
            )
            group["items"].append(item)

        groups = list(grouped.values())
        groups.sort(
            key=lambda group: (
                self._group_sort_key(group)
            )
        )
        return groups

    def _group_sort_key(self, group):
        items = group.get("items") or []
        first_item = items[0] if items else {}
        return (
            self._bed_sort_key(group.get("bed_number")),
            self._order_sort_key(first_item),
        )

    def _bed_sort_key(self, bed_number):
        value = str(bed_number if bed_number is not None else "").strip()
        if not value:
            return (1, 999999, "")
        try:
            return (0, int(value), value)
        except ValueError:
            return (0, 999999, value.lower())

    def _patient_group_key(self, item):
        admission_id = item.get("admission_id")
        patient_id = item.get("patient_id")
        if admission_id is not None:
            return f"admission:{admission_id}"
        if patient_id is not None:
            return f"patient:{patient_id}"
        return f"patient-name:{str(item.get('patient_name') or '').strip().lower()}"

    def _ensure_patient_group(self, group_data):
        group_key = group_data["key"]
        patient_name = group_data["patient_name"]
        if group_key in self.groups:
            group = self.groups[group_key]
            if group["header"].text() != patient_name:
                group["header"].setText(patient_name)
            group["bed_number"] = group_data.get("bed_number")
            return group

        frame = QFrame()
        frame.setObjectName("w1a_patient_group_card")
        frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        frame.setStyleSheet(
            """
            QFrame#w1a_patient_group_card {
                background-color: #ffffff;
                border: 1.6px solid #7f9fbd;
                border-radius: 5px;
            }
            """
        )

        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        frame_layout.setSpacing(0)

        header = QLabel(patient_name)
        header.setObjectName("w1a_patient_group_header")
        header.setWordWrap(True)
        header.setAlignment(Qt.AlignCenter)
        header.setFixedHeight(26)
        header.setStyleSheet(
            """
            QLabel#w1a_patient_group_header {
                background-color: #d7eaf8;
                color: #173b57;
                font-size: 12px;
                font-weight: bold;
                border: none;
                border-bottom: 1.6px solid #7f9fbd;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                padding: 5px 6px;
            }
            """
        )
        frame_layout.addWidget(header)

        body = QFrame()
        body.setObjectName("w1a_patient_group_body")
        body.setStyleSheet(
            """
            QFrame#w1a_patient_group_body {
                background-color: #ffffff;
                border: none;
                border-bottom-left-radius: 4px;
                border-bottom-right-radius: 4px;
            }
            """
        )
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(5, 5, 5, 5)
        body_layout.setSpacing(4)
        frame_layout.addWidget(body)

        group = {
            "frame": frame,
            "header": header,
            "body": body,
            "layout": body_layout,
            "bed_number": group_data.get("bed_number"),
        }
        self.groups[group_key] = group
        return group

    def _pin_group_frame_heights(self):
        rerun_requested = self._group_pin_rerun_requested
        self._group_pin_rerun_requested = False
        self._group_pin_pending = False
        if self._is_shutting_down:
            return
        for group in list(self.groups.values()):
            self._pin_group_frame_height(group)
        self.content_layout.invalidate()
        self.content_layout.activate()
        self.cards_container.updateGeometry()
        if rerun_requested or self._group_pin_rerun_requested:
            self._group_pin_rerun_requested = False
            self._group_pin_pending = True
            QTimer.singleShot(0, self._pin_group_frame_heights)

    def _queue_group_frame_pin(self):
        if self._is_shutting_down:
            return
        if self._group_pin_pending:
            self._group_pin_rerun_requested = True
            return
        self._group_pin_pending = True
        QTimer.singleShot(0, self._pin_group_frame_heights)

    def _pin_group_frame_height(self, group):
        frame = (group or {}).get("frame")
        if frame is None:
            return
        if frame.minimumHeight() == frame.maximumHeight():
            frame.setMinimumHeight(0)
            frame.setMaximumHeight(16777215)
        required_height = max(
            frame.minimumSizeHint().height(),
            frame.sizeHint().height(),
            self._patient_group_natural_height(group),
        )
        if required_height > 0:
            frame.setFixedHeight(required_height)
            frame.updateGeometry()

    @staticmethod
    def _patient_group_natural_height(group):
        frame = (group or {}).get("frame")
        header = (group or {}).get("header")
        body = (group or {}).get("body")
        if frame is None or header is None or body is None:
            return 0

        frame_layout = frame.layout()
        margins = frame_layout.contentsMargins() if frame_layout is not None else None
        spacing = max(0, frame_layout.spacing()) if frame_layout is not None else 0
        vertical_margins = margins.top() + margins.bottom() if margins is not None else 0

        if header.minimumHeight() == header.maximumHeight():
            header_height = header.maximumHeight()
        else:
            header_width = max(1, header.width() or frame.width() or header.sizeHint().width())
            header_for_width = header.heightForWidth(header_width) if header.hasHeightForWidth() else -1
            header_height = max(
                header.minimumHeight(),
                header.minimumSizeHint().height(),
                header_for_width,
            )
        body_height = max(body.minimumSizeHint().height(), body.sizeHint().height())
        return int(vertical_margins + header_height + spacing + body_height + frame.frameWidth() * 2)

    def _visible_render_signature(self, data_list):
        return (
            tuple(int(item["id"]) for item in data_list if item.get("id") is not None),
            tuple(self._card_data_signature(self._card_payload_for_signature(item)) for item in data_list),
        )

    def _card_payload_for_signature(self, item):
        card_data = dict(item)
        card_data["defer_mark_visual"] = True
        card_data.pop("patient_name", None)
        card_data.pop("patient_full_name", None)
        return card_data

    @staticmethod
    def _card_data_signature(item):
        keys = (
            "id",
            "planned_time",
            "actual_time",
            "status",
            "comment",
            "cell_role",
            "expected_revision",
            "order_id",
            "order_title",
            "latin",
            "drug_key",
            "dose_value",
            "dose_unit",
            "order_comment",
            "order_type",
            "duration_min",
            "order_revision",
            "defer_mark_visual",
        )
        return tuple((key, item.get(key)) for key in keys)

    def _refresh_visible_card_signals(self, visible_ids):
        for admin_id in visible_ids or ():
            card = self.cards.get(int(admin_id))
            if card is not None and hasattr(card, "refresh_time_state"):
                card.refresh_time_state()

    def _order_sort_key(self, item):
        try:
            planned_dt = datetime.fromisoformat(str(item.get("planned_time")).replace(" ", "T"))
        except Exception:
            planned_dt = datetime.max
        return (
            planned_dt.isoformat(),
            int(item.get("priority") or 999),
            str(item.get("latin") or "").lower(),
            int(item.get("id") or 0),
        )

    def _is_visible(self, item, now: datetime) -> bool:
        if item.get("comment"):
            return False
        try:
            planned_dt = datetime.fromisoformat(str(item.get("planned_time")).replace(" ", "T"))
        except Exception:
            return False
        return (
            planned_dt - timedelta(minutes=W1A_BEFORE_MIN)
            <= now
            < planned_dt + timedelta(minutes=W1A_AFTER_MIN)
        )

    def _schedule_next_time_tick(self, now: datetime | None = None):
        self._time_timer.stop()
        if not self._display_enabled or not self._all_data:
            return
        now = now or datetime.now()
        next_minute = (now.replace(second=0, microsecond=0) + timedelta(minutes=1))
        delay_ms = max(1000, int((next_minute - now).total_seconds() * 1000) + 25)
        self._time_timer.start(min(delay_ms, W1A_TIME_RECOMPUTE_MAX_MS))

    def _set_pending_mark(self, admin_id: int, mark: str):
        self._pending_marks[int(admin_id)] = {
            "mark": str(mark or ""),
            "started_mono": time.monotonic(),
            "actual_time": datetime.now().isoformat(),
        }

    def _get_pending_mark(self, admin_id: int):
        if admin_id is None:
            return None
        pending = self._pending_marks.get(int(admin_id))
        if not pending:
            return None
        if (time.monotonic() - float(pending.get("started_mono") or 0.0)) > W1A_PENDING_MARK_TTL_SEC:
            self._pending_marks.pop(int(admin_id), None)
            return None
        return pending

    def _apply_pending_marks(self, data_list):
        if not self._pending_marks:
            return data_list

        for admin_id, pending in list(self._pending_marks.items()):
            if (time.monotonic() - float(pending.get("started_mono") or 0.0)) > W1A_PENDING_MARK_TTL_SEC:
                self._pending_marks.pop(admin_id, None)

        patched = []
        for item in data_list:
            patched_item = dict(item)
            pending = self._get_pending_mark(patched_item.get("id"))
            if pending:
                patched_item["comment"] = pending.get("mark") or ""
                patched_item["actual_time"] = patched_item.get("actual_time") or pending.get("actual_time")
            patched.append(patched_item)
        return patched

    def _enqueue_write(self, description: str, operation, *, on_success, on_error):
        if hasattr(self.service, "enqueue_write"):
            self.service.enqueue_write(
                description=description,
                operation=operation,
                on_success=on_success,
                on_error=on_error,
            )
            return
        try:
            result = operation()
        except Exception as exc:
            on_error(exc)
            return
        on_success(result)

    def handle_status_change(self, admin_id, mark):
        if not self._display_enabled:
            return
        if mark not in (NURSE_MARK_EXECUTED, NURSE_MARK_NOT_EXECUTED):
            return

        def operation(aid=admin_id, value=mark):
            if hasattr(self.service, "set_nurse_order_mark"):
                return self.service.set_nurse_order_mark(aid, value)
            return self.service.set_nurse_status(aid, value)

        self._set_pending_mark(admin_id, mark)
        self._render_from_cache()
        self._enqueue_write(
            f"nurse_order_panel_mark:w1a:{admin_id}",
            operation,
            on_success=lambda _result=None, aid=admin_id: self._on_mark_write_success(aid),
            on_error=lambda exc, aid=admin_id: self._on_mark_write_error(aid, exc),
        )

    def _on_mark_write_success(self, admin_id: int):
        pending = self._pending_marks.get(int(admin_id))
        if pending:
            pending["started_mono"] = time.monotonic()
        self.refresh_data(force=True)

    def _on_mark_write_error(self, admin_id: int, exc: Exception):
        self._pending_marks.pop(int(admin_id), None)
        self._render_from_cache()
        self.refresh_data(force=True)
        CustomMessageBox.warning(self, "Предупреждение", f"Ошибка сохранения: {exc}")

    def _disconnect_worker(self, worker):
        for signal, slot in (
            (worker.succeeded, self._apply_snapshot),
            (worker.failed, self._on_refresh_failed),
            (worker.finished, self._on_refresh_finished),
        ):
            try:
                signal.disconnect(slot)
            except Exception:
                pass

    def shutdown(self):
        self._is_shutting_down = True
        self._time_timer.stop()
        self._refresh_timer.stop()
        worker = self._refresh_worker
        self._refresh_worker = None
        if worker is None:
            return
        self._disconnect_worker(worker)
        if worker.isRunning():
            worker.quit()
            worker.wait(1200)

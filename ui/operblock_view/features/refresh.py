from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget
from math import ceil
from rem_card.app import operblock_startup_metrics
from rem_card.app.logger import logger
from rem_card.services.operblock_service import OPERBLOCK_TABLES
from rem_card.services.operblock_service import OperBlockConflictError
from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.custom_message_box import CustomMessageBox
import time
from rem_card.ui.operblock_view.operblock_helpers import (
    _safe_int,
    _stable_ui_hash,
)
from rem_card.ui.operblock_view.operblock_visual_primitives import (
    OPERBLOCK_LOCAL_WRITE_REFRESH_SUPPRESS_SECONDS,
    OPERBLOCK_ROUTE_ONLY_REFRESH_SUPPRESS_SECONDS,
)


class OperBlockRefreshMixin:
    def _visible_operblock_tables(self) -> list[dict]:
        if not self._table_filter_code:
            return [dict(table) for table in OPERBLOCK_TABLES]
        return [dict(table) for table in OPERBLOCK_TABLES if str(table.get("code") or "") == self._table_filter_code]

    def _filter_board_snapshot(self, snapshot: dict) -> dict:
        if not self._table_filter_code:
            return snapshot
        filtered = dict(snapshot or {})
        filtered["tables"] = [
            dict(table or {})
            for table in list((snapshot or {}).get("tables") or [])
            if str((table or {}).get("code") or "") == self._table_filter_code
        ]
        filtered["table_filter_code"] = self._table_filter_code
        filtered["content_hash"] = _stable_ui_hash(filtered)
        return filtered

    def _connect_updates(self):
        if self.data_service and hasattr(self.data_service, "changes_detected"):
            self.data_service.changes_detected.connect(self._on_changes_detected)

    def start_auto_refresh(self, *, wake_monitor: bool = True):
        if self._is_closing:
            return
        metric_started = operblock_startup_metrics.timer_start()
        operblock_startup_metrics.record_value(
            "start_auto_refresh_nested_phase",
            "wrapper:refresh_board",
            source="operblock_widget",
        )
        try:
            if self.data_service and hasattr(self.data_service, "set_change_monitor_enabled"):
                self.data_service.set_change_monitor_enabled(False)
            self.refresh_board(
                force=True,
                refresh_reason="start_auto_refresh",
                loading_message="Загрузка операционной...",
            )
            if self.data_service and wake_monitor:
                self.data_service.request_immediate_refresh(force_emit=False, source="operblock_start")
        finally:
            operblock_startup_metrics.record_since(
                "start_auto_refresh_ms",
                metric_started,
                source="operblock_widget",
                nested_role="wrapper",
                child_phase="first_refresh_board_ms",
            )

    def auto_refresh(self, force: bool = False):
        if self._is_closing:
            return
        action_info = self._start_opblock_action_diagnostics("operblock_user_refresh") if force else None
        current_widget = self.stack.currentWidget()
        try:
            if self.protocol_page is not None and current_widget == self.protocol_page and self._current_operation_case_id:
                self.refresh_protocol(
                    force=force,
                    loading_message="Обновление протокола операции..." if force else None,
                )
            elif self.archive_page is not None and current_widget == self.archive_page:
                self.refresh_operblock_archive(
                    force=force,
                    loading_message="Обновление архива оперблока..." if force else None,
                )
            else:
                self.refresh_board(
                    force=force,
                    refresh_reason="auto_refresh",
                    loading_message="Обновление операционной..." if force else None,
                )
        finally:
            self._finish_opblock_action_diagnostics(action_info, "success")

    def apply_operblock_icon_settings(self):
        self._orders_render_signature = ""
        self._active_infusions_render_signature = ""
        self._rendered_medication_group_signatures.clear()
        self._rendered_active_infusion_signatures.clear()
        self._board_photo_thumbnail_cache.clear()
        current_widget = self.stack.currentWidget()
        if self.protocol_page is not None and current_widget == self.protocol_page and self._current_operation_case_id:
            self.refresh_protocol(force=True)
            return
        if getattr(self, "quick_orders_list", None) is not None:
            try:
                self._refresh_quick_orders()
            except Exception:
                pass
        if self.archive_page is not None and current_widget == self.archive_page:
            self.refresh_operblock_archive(force=True)
        else:
            self.refresh_board(force=True, refresh_reason="operblock_icon_settings")

    def refresh_board(
        self,
        *,
        force: bool = False,
        refresh_reason: str = "refresh_board",
        loading_message: str | None = None,
    ):
        if self._is_closing:
            return
        worker = getattr(self, "_board_refresh_worker", None)
        if worker is not None and worker.isRunning():
            pending = dict(self._board_refresh_pending or {})
            self._board_refresh_pending = {
                "force": bool(force) or bool(pending.get("force")),
                "refresh_reason": str(refresh_reason or pending.get("refresh_reason") or "refresh_board"),
                "loading_message": loading_message or pending.get("loading_message"),
            }
            return
        loading_key = self._show_operblock_loading(
            loading_message,
            key="board-refresh",
            auto_hide_ms=30000,
        )
        enter_started = operblock_startup_metrics.timer_start()
        self._board_refresh_seq += 1
        refresh_seq = self._board_refresh_seq
        self._board_refresh_count_before_ready += 1
        is_first_refresh = not getattr(self, "_operblock_startup_first_refresh_recorded", False)
        refresh_fields = {
            "refresh_seq": refresh_seq,
            "refresh_reason": str(refresh_reason or "refresh_board"),
            "force": bool(force),
            "is_first": bool(is_first_refresh),
            "is_first_refresh": bool(is_first_refresh),
        }
        operblock_startup_metrics.record_value("refresh_seq", refresh_seq, source="operblock_widget", **refresh_fields)
        operblock_startup_metrics.record_value(
            "refresh_count_before_ready",
            self._board_refresh_count_before_ready,
            source="operblock_widget",
            **refresh_fields,
        )
        operblock_startup_metrics.record_value(
            "refresh_reason",
            refresh_fields["refresh_reason"],
            source="operblock_widget",
            **refresh_fields,
        )
        operblock_startup_metrics.record_value("refresh_force", bool(force), source="operblock_widget", **refresh_fields)
        operblock_startup_metrics.record_value(
            "refresh_is_first_refresh",
            bool(is_first_refresh),
            source="operblock_widget",
            **refresh_fields,
        )
        operblock_startup_metrics.record_value(
            "refresh_is_first",
            bool(is_first_refresh),
            source="operblock_widget",
            **refresh_fields,
        )
        operblock_startup_metrics.record_since("refresh_board_enter_ms", enter_started, source="operblock_widget", **refresh_fields)
        first_refresh_started = None
        if not getattr(self, "_operblock_startup_first_refresh_recorded", False):
            first_refresh_started = operblock_startup_metrics.timer_start()
            self._operblock_startup_first_refresh_recorded = True
        snapshot_started = operblock_startup_metrics.timer_start()
        worker = AsyncCallThread(
            self.operblock_service.build_operblock_board_snapshot,
            table_code=self._table_filter_code,
            parent=self,
        )
        self._board_refresh_worker = worker
        finalized = {"done": False}

        def finalize_refresh():
            if finalized["done"]:
                return
            finalized["done"] = True
            operblock_startup_metrics.record_since(
                "first_refresh_board_ms",
                first_refresh_started,
                source="operblock_widget",
                nested_role="child",
                parent_phase="start_auto_refresh_ms",
                **refresh_fields,
            )
            self._hide_operblock_loading(loading_key)
            if getattr(self, "_board_refresh_worker", None) is worker:
                self._board_refresh_worker = None
            pending = self._board_refresh_pending
            self._board_refresh_pending = None
            if pending and not self._is_closing:
                QTimer.singleShot(0, lambda pending=pending: self.refresh_board(**pending))
            elif is_first_refresh and not self._is_closing:
                QTimer.singleShot(300, self._preload_operblock_chart_module)

        def on_snapshot_ready(snapshot):
            try:
                operblock_startup_metrics.record_since(
                    "refresh_board_snapshot_ms",
                    snapshot_started,
                    source="operblock_widget",
                    nested_role="child",
                    parent_phase="first_refresh_board_ms",
                    **refresh_fields,
                )
                snapshot = self._filter_board_snapshot(snapshot)
                if not force and snapshot.get("content_hash") == self._board_hash:
                    return
                self._board_hash = snapshot.get("content_hash") or ""
                apply_started = operblock_startup_metrics.timer_start()
                self._apply_board_snapshot(snapshot, refresh_context=refresh_fields)
                operblock_startup_metrics.record_since(
                    "refresh_board_apply_total_ms",
                    apply_started,
                    source="operblock_widget",
                    nested_role="child",
                    parent_phase="first_refresh_board_ms",
                    **refresh_fields,
                )
            except Exception as exc:
                logger.error("operblock board refresh apply failed: %s", exc, exc_info=True)
                CustomMessageBox.warning(self, "Ошибка чтения БД", str(exc))
            finally:
                finalize_refresh()

        def on_snapshot_failed(exc):
            try:
                operblock_startup_metrics.record_since(
                    "refresh_board_snapshot_ms",
                    snapshot_started,
                    source="operblock_widget",
                    nested_role="child",
                    parent_phase="first_refresh_board_ms",
                    status="error",
                    **refresh_fields,
                )
                logger.error("operblock board refresh failed: %s", exc, exc_info=True)
                CustomMessageBox.warning(self, "Ошибка чтения БД", str(exc))
            finally:
                finalize_refresh()

        worker.succeeded.connect(on_snapshot_ready)
        worker.failed.connect(on_snapshot_failed)
        worker.start()

    def refresh_operblock_archive(self, *, force: bool = False, loading_message: str | None = None, page: int | None = None):
        if self._is_closing:
            return
        archive_page = getattr(self, "archive_page", None)
        if archive_page is not None and archive_page.objectName() == "ArchiveCenter":
            archive_page.load_data(reset_page=bool(page == 1))
            return
        if page is not None:
            self._archive_current_page = max(1, int(page or 1))
        loading_key = self._show_operblock_loading(
            loading_message,
            key="archive-refresh",
            auto_hide_ms=30000,
        )
        try:
            try:
                search_query = str(
                    getattr(self, "archive_search_input", None).text()
                    if hasattr(self, "archive_search_input")
                    else ""
                ).strip()
                if hasattr(self.operblock_service, "list_archived_operation_cases_page"):
                    payload = self.operblock_service.list_archived_operation_cases_page(
                        page=self._archive_current_page,
                        page_size=self._archive_page_size,
                        table_code=self._table_filter_code,
                        search_query=search_query,
                    )
                    cases = [dict(item or {}) for item in (payload or {}).get("records") or []]
                    total_count = int((payload or {}).get("total_count") or len(cases))
                    loaded_page = int((payload or {}).get("page") or self._archive_current_page or 1)
                else:
                    all_cases = self._filter_archive_cases_by_table(self.operblock_service.list_archived_operation_cases())
                    cases = all_cases
                    total_count = len(all_cases)
                    loaded_page = 1
            except Exception as exc:
                logger.error("operblock archive refresh failed: %s", exc, exc_info=True)
                CustomMessageBox.warning(self, "Архив оперблока", f"Не удалось обновить архив:\n{exc}")
                return
            total_pages = max(1, int(ceil(total_count / self._archive_page_size))) if total_count else 1
            if loaded_page > total_pages:
                self._archive_current_page = total_pages
                QTimer.singleShot(0, lambda: self.refresh_operblock_archive(force=True, page=total_pages))
                return
            self._archive_current_page = max(1, min(loaded_page, total_pages))
            self._archive_total_records = max(0, total_count)
            self._archive_total_pages = total_pages
            source_hash = _stable_ui_hash(cases)
            if not force and source_hash == getattr(self, "_archive_cases_hash", ""):
                self._refresh_operblock_archive_pagination_ui()
                return
            self._archive_cases_hash = source_hash
            self._archive_cases = [dict(item or {}) for item in cases]
            self._apply_operblock_archive_cases()
        finally:
            self._hide_operblock_loading(loading_key)

    def refresh_protocol(self, *, force: bool = False, loading_message: str | None = None):
        if self._is_closing or not self._current_operation_case_id:
            return
        worker = getattr(self, "_protocol_refresh_worker", None)
        if worker is not None and worker.isRunning():
            self._refresh_generation += 1
            pending = dict(self._protocol_refresh_pending or {})
            self._protocol_refresh_pending = {
                "force": bool(force) or bool(pending.get("force")),
                "loading_message": loading_message or pending.get("loading_message"),
            }
            return
        loading_key = self._show_operblock_loading(
            loading_message,
            key="protocol-refresh",
            auto_hide_ms=30000,
        )
        generation = self._refresh_generation = self._refresh_generation + 1
        operation_case_id = int(self._current_operation_case_id)
        snapshot_started = operblock_startup_metrics.timer_start()
        worker = AsyncCallThread(
            self.operblock_service.build_operblock_protocol_snapshot,
            operation_case_id,
            parent=self,
        )
        self._protocol_refresh_worker = worker
        finalized = {"done": False}

        def finalize_refresh():
            if finalized["done"]:
                return
            finalized["done"] = True
            self._hide_operblock_loading(loading_key)
            if getattr(self, "_protocol_refresh_worker", None) is worker:
                self._protocol_refresh_worker = None
            pending = self._protocol_refresh_pending
            self._protocol_refresh_pending = None
            if pending and not self._is_closing:
                QTimer.singleShot(0, lambda pending=pending: self.refresh_protocol(**pending))

        def is_stale_result() -> bool:
            return (
                self._is_closing
                or generation != self._refresh_generation
                or int(self._current_operation_case_id or 0) != operation_case_id
            )

        def on_snapshot_ready(snapshot):
            try:
                operblock_startup_metrics.record_since(
                    "refresh_protocol_snapshot_ms",
                    snapshot_started,
                    source="operblock_widget",
                    operation_case_id=operation_case_id,
                )
                if is_stale_result():
                    return
                if not force and snapshot.get("content_hash") == self._protocol_hash:
                    return
                self._protocol_hash = snapshot.get("content_hash") or ""
                apply_started = operblock_startup_metrics.timer_start()
                self._apply_protocol_snapshot(snapshot)
                operblock_startup_metrics.record_since(
                    "refresh_protocol_apply_ms",
                    apply_started,
                    source="operblock_widget",
                    operation_case_id=operation_case_id,
                )
                self._schedule_current_protocol_tab_ready(120)
            except Exception as exc:
                logger.error("operblock protocol refresh apply failed: %s", exc, exc_info=True)
                CustomMessageBox.warning(self, "Ошибка", f"Не удалось обновить протокол:\n{exc}")
            finally:
                finalize_refresh()

        def on_snapshot_failed(exc):
            try:
                operblock_startup_metrics.record_since(
                    "refresh_protocol_snapshot_ms",
                    snapshot_started,
                    source="operblock_widget",
                    operation_case_id=operation_case_id,
                    status="error",
                )
                if is_stale_result():
                    return
                if isinstance(exc, OperBlockConflictError):
                    CustomMessageBox.warning(self, "Оперблок", str(exc))
                    self._show_board()
                    return
                logger.error("operblock protocol refresh failed: %s", exc, exc_info=True)
                CustomMessageBox.warning(self, "Ошибка", f"Не удалось обновить протокол:\n{exc}")
            finally:
                finalize_refresh()

        worker.succeeded.connect(on_snapshot_ready)
        worker.failed.connect(on_snapshot_failed)
        worker.start()

    @staticmethod
    def _empty_board_table_payload(table: dict) -> dict:
        return {
            "code": str(table.get("code") or ""),
            "display_name": table.get("display_name") or "",
            "sort_order": table.get("sort_order"),
            "occupied": False,
            "patient": None,
        }

    @staticmethod
    def _board_table_content_hash(table: dict) -> str:
        return _stable_ui_hash(
            {
                "code": str(table.get("code") or ""),
                "display_name": table.get("display_name") or "",
                "sort_order": table.get("sort_order"),
                "occupied": bool(table.get("occupied")),
                "patient": table.get("patient") or None,
            }
        )

    def _board_card_layout_index(self, widget: QWidget | None) -> int:
        if widget is None:
            return -1
        layout = getattr(self, "cards_layout", None)
        if layout is None:
            return -1
        for index in range(layout.count()):
            item = layout.itemAt(index)
            if item is not None and item.widget() is widget:
                return index
        return -1

    def _remove_board_card_widget(self, widget: QWidget | None) -> bool:
        if widget is None:
            return False
        if getattr(self, "cards_layout", None) is not None:
            self.cards_layout.removeWidget(widget)
        widget.setParent(None)
        return True

    def _apply_board_snapshot(self, snapshot: dict, *, refresh_context: dict | None = None):
        metric_started = operblock_startup_metrics.timer_start()
        refresh_fields = dict(refresh_context or {})
        tables = list(snapshot.get("tables", []) or [])
        occupied_count = sum(1 for table in tables if table.get("occupied"))
        empty_count = max(0, len(tables) - occupied_count)
        apply_fields = {
            **refresh_fields,
            "nested_role": "child",
            "parent_phase": "apply_board_snapshot_ms",
        }
        apply_metrics = {
            "photo_count": 0,
            "missing_photo_count": 0,
            "current_card_fields": {},
            "photo_cache_hit_count": 0,
            "photo_cache_miss_count": 0,
        }
        previous_apply_metrics = self._current_board_apply_metrics
        self._current_board_apply_metrics = apply_metrics
        try:
            operblock_startup_metrics.record_value("board_apply_table_count", len(tables), source="operblock_widget", **apply_fields)
            operblock_startup_metrics.record_value("board_apply_occupied_count", occupied_count, source="operblock_widget", **apply_fields)
            operblock_startup_metrics.record_value("board_apply_empty_count", empty_count, source="operblock_widget", **apply_fields)
            snapshot_codes = {str(table.get("code") or "") for table in tables}
            clear_started = operblock_startup_metrics.timer_start()
            remove_started = operblock_startup_metrics.timer_start()
            removed_count = 0
            for table_code, widget in list(self._table_cards.items()):
                if table_code in snapshot_codes:
                    continue
                if self._remove_board_card_widget(widget):
                    removed_count += 1
                self._table_cards.pop(table_code, None)
                self._board_card_hashes.pop(table_code, None)
                self._board_card_states.pop(table_code, None)
            operblock_startup_metrics.record_since(
                "board_apply_clear_remove_widgets_ms",
                remove_started,
                source="operblock_widget",
                removed_count=removed_count,
                **apply_fields,
            )
            operblock_startup_metrics.record_duration(
                "board_apply_clear_delete_later_ms",
                0.0,
                source="operblock_widget",
                reason="delete_later_not_used",
                **apply_fields,
            )
            operblock_startup_metrics.record_since("board_apply_clear_total_ms", clear_started, source="operblock_widget", **apply_fields)

            loop_started = operblock_startup_metrics.timer_start()
            recreated_count = 0
            reused_count = 0
            skipped_unchanged_count = 0
            replaced_count = 0
            updated_count = 0
            relayout_count = 0
            relayout_elapsed_recorded = False
            for target_index, table in enumerate(tables):
                table_code = str(table.get("code") or "")
                table_hash = self._board_table_content_hash(table)
                card_kind = "occupied" if table.get("occupied") else "empty"
                previous_card = self._table_cards.get(table_code)
                previous_hash = self._board_card_hashes.get(table_code, "")
                previous_state = self._board_card_states.get(table_code) or {}
                card_fields = {
                    **refresh_fields,
                    "table_code": table_code,
                    "card_kind": card_kind,
                    "nested_role": "child",
                    "parent_phase": "board_apply_card_loop_total_ms",
                }
                card_inner_fields = {
                    **refresh_fields,
                    "table_code": table_code,
                    "card_kind": card_kind,
                    "nested_role": "child",
                    "parent_phase": (
                        "board_apply_make_occupied_card_ms" if table.get("occupied") else "board_apply_make_empty_card_ms"
                    ),
                }
                if previous_card is not None and previous_hash == table_hash:
                    reused_count += 1
                    skipped_unchanged_count += 1
                    if previous_state.get("has_photo"):
                        apply_metrics["photo_cache_hit_count"] = int(apply_metrics.get("photo_cache_hit_count") or 0) + 1
                    current_index = self._board_card_layout_index(previous_card)
                    if current_index != target_index:
                        move_started = operblock_startup_metrics.timer_start()
                        if current_index >= 0:
                            self.cards_layout.removeWidget(previous_card)
                        self.cards_layout.insertWidget(target_index, previous_card, 1)
                        operblock_startup_metrics.record_since(
                            "board_apply_order_relayout_ms",
                            move_started,
                            source="operblock_widget",
                            **card_fields,
                        )
                        relayout_elapsed_recorded = True
                        relayout_count += 1
                    continue

                apply_metrics["current_card_fields"] = card_inner_fields
                if previous_card is not None:
                    self._remove_board_card_widget(previous_card)
                    replaced_count += 1
                    updated_count += 1
                if table.get("occupied"):
                    card_started = operblock_startup_metrics.timer_start()
                    card = self._make_occupied_table_card(table)
                    operblock_startup_metrics.record_since(
                        "board_apply_make_occupied_card_ms",
                        card_started,
                        source="operblock_widget",
                        **card_fields,
                    )
                else:
                    card_started = operblock_startup_metrics.timer_start()
                    card = self._make_empty_table_card(table["code"], table["display_name"])
                    operblock_startup_metrics.record_since(
                        "board_apply_make_empty_card_ms",
                        card_started,
                        source="operblock_widget",
                        **card_fields,
                    )
                self._table_cards[table_code] = card
                self._board_card_hashes[table_code] = table_hash
                self._board_card_states[table_code] = {
                    "kind": card_kind,
                    "content_hash": table_hash,
                    "has_photo": True,
                }
                layout_add_started = operblock_startup_metrics.timer_start()
                self.cards_layout.insertWidget(target_index, card, 1)
                operblock_startup_metrics.record_since("board_apply_layout_add_ms", layout_add_started, source="operblock_widget", **card_fields)
                recreated_count += 1
            apply_metrics["current_card_fields"] = {}
            if not relayout_elapsed_recorded:
                operblock_startup_metrics.record_duration(
                    "board_apply_order_relayout_ms",
                    0.0,
                    source="operblock_widget",
                    **apply_fields,
                )
            operblock_startup_metrics.record_since("board_apply_card_loop_total_ms", loop_started, source="operblock_widget", **apply_fields)

            after_loop_started = operblock_startup_metrics.timer_start()
            operblock_startup_metrics.record_value(
                "board_apply_card_recreated_count",
                recreated_count,
                source="operblock_widget",
                **apply_fields,
            )
            operblock_startup_metrics.record_value(
                "board_apply_card_reused_count",
                reused_count,
                source="operblock_widget",
                **apply_fields,
            )
            operblock_startup_metrics.record_value(
                "board_apply_card_skipped_unchanged_count",
                skipped_unchanged_count,
                source="operblock_widget",
                **apply_fields,
            )
            operblock_startup_metrics.record_value(
                "board_apply_card_replaced_count",
                replaced_count,
                source="operblock_widget",
                **apply_fields,
            )
            operblock_startup_metrics.record_value(
                "board_apply_card_updated_count",
                updated_count,
                source="operblock_widget",
                **apply_fields,
            )
            operblock_startup_metrics.record_value(
                "board_apply_order_relayout_count",
                relayout_count,
                source="operblock_widget",
                **apply_fields,
            )
            operblock_startup_metrics.record_value(
                "board_apply_photo_count",
                int(apply_metrics.get("photo_count") or 0),
                source="operblock_widget",
                **apply_fields,
            )
            operblock_startup_metrics.record_value(
                "board_apply_missing_photo_count",
                int(apply_metrics.get("missing_photo_count") or 0),
                source="operblock_widget",
                **apply_fields,
            )
            operblock_startup_metrics.record_value(
                "board_apply_photo_cache_hit_count",
                int(apply_metrics.get("photo_cache_hit_count") or 0),
                source="operblock_widget",
                **apply_fields,
            )
            operblock_startup_metrics.record_value(
                "board_apply_photo_cache_miss_count",
                int(apply_metrics.get("photo_cache_miss_count") or 0),
                source="operblock_widget",
                **apply_fields,
            )
            operblock_startup_metrics.record_since("board_apply_after_loop_ms", after_loop_started, source="operblock_widget", **apply_fields)
        finally:
            self._current_board_apply_metrics = previous_apply_metrics
            operblock_startup_metrics.record_since(
                "apply_board_snapshot_ms",
                metric_started,
                source="operblock_widget",
                nested_role="child",
                parent_phase="first_refresh_board_ms",
                **refresh_fields,
            )

    def _cleanup_route_only_write_suppressions(self) -> None:
        now = time.monotonic()
        ttl = float(OPERBLOCK_ROUTE_ONLY_REFRESH_SUPPRESS_SECONDS)
        self._route_only_write_suppressions = {
            key: started
            for key, started in (getattr(self, "_route_only_write_suppressions", {}) or {}).items()
            if now - float(started or 0.0) <= ttl
        }

    def _remember_local_write_refresh_suppression(self, description: str, entities) -> None:
        source = str(description or "").strip()
        if not source:
            return
        self._local_write_refresh_suppressions[source] = {
            "started": time.monotonic(),
            "entities": {str(entity) for entity in (entities or []) if entity},
        }

    def _cleanup_local_write_refresh_suppressions(self) -> None:
        now = time.monotonic()
        ttl = float(OPERBLOCK_LOCAL_WRITE_REFRESH_SUPPRESS_SECONDS)
        self._local_write_refresh_suppressions = {
            source: data
            for source, data in (getattr(self, "_local_write_refresh_suppressions", {}) or {}).items()
            if now - float((data or {}).get("started") or 0.0) <= ttl
        }

    def _should_skip_local_write_refresh(self, payload: dict) -> bool:
        self._cleanup_local_write_refresh_suppressions()
        suppressions = dict(getattr(self, "_local_write_refresh_suppressions", {}) or {})
        if not suppressions:
            return False
        sources = set(self._payload_force_sources(payload or {}))
        matched_sources = [source for source in sources if source in suppressions]
        if not matched_sources:
            return False
        expected_entities: set[str] = set()
        for source in matched_sources:
            expected_entities.update(set((suppressions.get(source) or {}).get("entities") or set()))

        changes = list((payload or {}).get("changes") or [])
        changed_entities = {
            str((change or {}).get("entity_name") or "")
            for change in changes
            if (change or {}).get("entity_name")
        }
        if not changed_entities:
            changed_entities = {
                str(entity)
                for entity in ((payload or {}).get("entities") or (payload or {}).get("changed_entities") or [])
                if entity
            }
        can_skip = not changed_entities or changed_entities.issubset(expected_entities)
        if can_skip:
            for source in matched_sources:
                self._local_write_refresh_suppressions.pop(source, None)
        return can_skip

    @staticmethod
    def _payload_force_sources(payload: dict) -> list[str]:
        sources: list[str] = []
        raw_many = (payload or {}).get("force_sources") or []
        if isinstance(raw_many, (list, tuple, set)):
            sources.extend(str(item) for item in raw_many if item)
        raw_one = (payload or {}).get("force_source")
        if raw_one:
            sources.append(str(raw_one))
        return list(dict.fromkeys(sources))

    @staticmethod
    def _route_only_keys_from_sources(sources: list[str]) -> set[tuple[int, int]]:
        prefix = "operblock_update_order_route:"
        keys: set[tuple[int, int]] = set()
        for source in sources:
            text = str(source or "")
            if not text.startswith(prefix):
                continue
            parts = text[len(prefix) :].split(":")
            if len(parts) < 2:
                continue
            admission_id = _safe_int(parts[0])
            order_id = _safe_int(parts[1])
            if admission_id and order_id:
                keys.add((int(admission_id), int(order_id)))
        return keys

    def _should_skip_route_only_refresh(self, payload: dict) -> bool:
        self._cleanup_route_only_write_suppressions()
        pending = set((getattr(self, "_route_only_write_suppressions", {}) or {}).keys())
        source_keys = self._route_only_keys_from_sources(self._payload_force_sources(payload or {}))
        candidate_keys = pending | source_keys
        if not candidate_keys:
            return False

        changes = list((payload or {}).get("changes") or [])
        if changes:
            changed_keys: set[tuple[int, int]] = set()
            for change in changes:
                entity = str((change or {}).get("entity_name") or "")
                if entity != "orders":
                    return False
                admission_id = _safe_int((change or {}).get("admission_id"))
                order_id = _safe_int((change or {}).get("entity_id"))
                if not admission_id or not order_id:
                    return False
                key = (int(admission_id), int(order_id))
                if key not in candidate_keys:
                    return False
                changed_keys.add(key)
            return True

        entities = {
            str(entity)
            for entity in ((payload or {}).get("entities") or (payload or {}).get("changed_entities") or [])
            if entity
        }
        if entities and entities - {"orders"}:
            return False
        if source_keys or pending:
            return True
        return False

    def _on_changes_detected(self, payload: dict):
        if self._is_closing:
            return
        if self._should_skip_route_only_refresh(payload):
            return
        if self._should_skip_local_write_refresh(payload):
            return
        entities = set(payload.get("entities") or payload.get("changed_entities") or [])
        watched = {
            "operation_cases",
            "operation_table_assignments",
            "operating_tables",
            "vitals",
            "orders",
            "operblock_timeline_events",
            "administrations",
        }
        if not entities or entities.intersection(watched):
            QTimer.singleShot(0, lambda: self.auto_refresh(force=False))

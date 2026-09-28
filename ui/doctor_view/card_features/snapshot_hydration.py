from __future__ import annotations

from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from PySide6.QtCore import QTimer
from PySide6.QtCore import Qt
from datetime import datetime
from rem_card.ui.shared.loading_overlay import hide_app_loading
from rem_card.app.logger import logger
import os
from rem_card.app.local_metrics import record_metric
from rem_card.app.foreground_activity import should_defer_background_io
from rem_card.ui.shared.loading_overlay import show_app_loading
from .constants import CARD_HYDRATION_FOREGROUND_IDLE_SEC, CARD_HYDRATION_MAX_DEFER_ATTEMPTS, CARD_OPEN_HYDRATE_DELAY_MS

class DoctorSnapshotHydrationMixin:
    def _get_data_service(self):
        return getattr(self.service, "data_service", None)

    def _get_read_coordinator(self):
        return getattr(self.service, "read_coordinator", None)

    def _get_cached_patient_vitals_snapshot(self, admission_id, shift_date):
        if self._archive_read_only_mode:
            return None
        coordinator = self._get_read_coordinator()
        if coordinator is None or not admission_id or shift_date is None:
            return None
        try:
            context = coordinator.make_patient_snapshot_context(
                source_db="live",
                admission_id=int(admission_id),
                shift_date=shift_date,
                role="doctor",
                mode="live",
                variant="vitals",
            )
            if hasattr(coordinator, "get_cached_vitals"):
                return coordinator.get_cached_vitals(context.cache_key())
            if hasattr(coordinator, "get_current_cached_vitals"):
                return coordinator.get_current_cached_vitals(context.cache_key())
        except Exception as exc:
            logger.debug("Doctor vitals cache lookup failed: %s", exc)
            return None

    def _get_cached_patient_card_snapshot(self, admission_id, shift_date):
        if self._archive_read_only_mode:
            return None
        coordinator = self._get_read_coordinator()
        if coordinator is None or not admission_id or shift_date is None:
            return None
        try:
            context = coordinator.make_patient_snapshot_context(
                source_db="live",
                admission_id=int(admission_id),
                shift_date=shift_date,
                role="doctor",
                mode="live",
                variant="card_full",
            )
            if hasattr(coordinator, "get_cached_card"):
                return coordinator.get_cached_card(context.cache_key())
        except Exception as exc:
            logger.debug("Doctor card cache lookup failed: %s", exc)
        return None

    def _apply_patient_open_cache(self, admission_id, shift_date, snapshot):
        if not snapshot:
            return False
        load_scope = (
            "patient_open_card"
            if ("balance_runtime" in snapshot or "fluids" in snapshot)
            else "patient_open_vitals"
        )
        request = {
            "admission_id": int(admission_id),
            "shift_date": shift_date,
            "ensure_initial_status": False,
            "show_empty_message": False,
            "load_scope": load_scope,
            "context_key": self._current_snapshot_context_key(
                admission_id=admission_id,
                shift_date=shift_date,
                load_scope=load_scope,
            ),
            "snapshot": snapshot,
            "from_cache": True,
        }
        self._apply_card_snapshot(request)
        logger.info(
            "DoctorRemCardWidget applied cached vitals snapshot admission_id=%s version=%s",
            admission_id,
            snapshot.get("version"),
        )
        return True

    @staticmethod
    def _card_snapshot_apply_signature(snapshot: dict):
        if not snapshot:
            return None
        content_hash = snapshot.get("content_hash")
        cache_key = snapshot.get("cache_key")
        if cache_key is not None and content_hash:
            try:
                version = int(snapshot.get("version") or snapshot.get("change_id") or 0)
            except Exception:
                version = 0
            return (
                cache_key,
                str(snapshot.get("scope") or ""),
                version,
                str(content_hash),
            )
        dedup_signature = snapshot.get("dedup_signature")
        if dedup_signature is not None:
            return ("dedup", tuple(dedup_signature))
        return None

    def _chart_matches_context(self, admission_id, start_dt):
        chart = getattr(self, "chart", None)
        if chart is None:
            return False
        return (
            int(getattr(chart, "admission_id", 0) or 0) == int(admission_id or 0)
            and getattr(chart, "start_time", None) == start_dt
            and bool(getattr(chart, "vitals_data", None))
        )

    def _chart_snapshot_signature(self, snapshot: dict):
        chart = getattr(self, "chart", None)
        if not snapshot or chart is None:
            return None
        chart_cls = chart.__class__
        normalize_dt = getattr(chart_cls, "_normalize_key_dt", None)
        build_vitals_key = getattr(chart_cls, "_build_vitals_key", None)
        build_intervals_key = getattr(chart_cls, "_build_intervals_key", None)
        if not (normalize_dt and build_vitals_key and build_intervals_key):
            return None
        runtime = snapshot.get("balance_runtime") or {}
        active_intervals = snapshot.get("chart_active_intervals") or runtime.get("active_intervals")
        try:
            return (
                int(snapshot.get("admission_id") or self.admission_id or 0),
                normalize_dt(snapshot.get("start_dt")),
                build_vitals_key(snapshot.get("vitals_extended") or []),
                build_intervals_key(active_intervals or []),
            )
        except Exception as exc:
            logger.debug("Doctor chart snapshot signature failed: %s", exc)
            return None

    def _current_snapshot_context_key(
        self,
        *,
        admission_id=None,
        shift_date=None,
        load_scope: str = "full",
    ):
        target_admission_id = int(admission_id if admission_id is not None else (self.admission_id or 0))
        target_shift_date = shift_date if shift_date is not None else self._current_date
        source_db = (
            os.path.abspath(str(self._archive_source_db_path))
            if self._archive_read_only_mode and self._archive_source_db_path
            else "live"
        )
        coordinator = self._get_read_coordinator()
        if coordinator is not None and target_shift_date is not None:
            try:
                context = coordinator.make_patient_snapshot_context(
                    source_db=source_db,
                    admission_id=target_admission_id,
                    shift_date=target_shift_date,
                    role="doctor",
                    mode="archive" if self._archive_read_only_mode else "live",
                    variant=str(load_scope or "full"),
                )
                context_hash = context.hash()
            except Exception:
                context_hash = "unavailable"
        else:
            context_hash = "unavailable"
        return (
            target_admission_id,
            target_shift_date.isoformat() if target_shift_date else None,
            "doctor",
            "archive" if self._archive_read_only_mode else "live",
            source_db,
            str(load_scope or "full"),
            context_hash,
        )

    def _schedule_card_hydration_snapshot(
        self,
        admission_id: int,
        shift_date: datetime,
        *,
        ensure_initial_status: bool,
    ):
        context_key = self._current_snapshot_context_key(
            admission_id=admission_id,
            shift_date=shift_date,
            load_scope="patient_open_card",
        )
        QTimer.singleShot(
            CARD_OPEN_HYDRATE_DELAY_MS,
            lambda: self._request_card_hydration_if_current(
                admission_id,
                shift_date,
                context_key,
                ensure_initial_status=ensure_initial_status,
            ),
        )

    def _request_card_hydration_if_current(
        self,
        admission_id: int,
        shift_date: datetime,
        context_key,
        *,
        ensure_initial_status: bool,
        defer_attempts: int = 0,
    ):
        if int(admission_id or 0) != int(self.admission_id or 0):
            return
        if shift_date != self._current_date:
            return
        if context_key != self._current_snapshot_context_key(load_scope="patient_open_card"):
            return

        should_defer, reason, age_sec = should_defer_background_io(
            idle_window_sec=CARD_HYDRATION_FOREGROUND_IDLE_SEC,
            names={"orders", "orders_show"},
        )
        active_foreground = str(reason or "").startswith("active:")
        if should_defer and (active_foreground or defer_attempts < CARD_HYDRATION_MAX_DEFER_ATTEMPTS):
            delay_ms = max(1000, CARD_OPEN_HYDRATE_DELAY_MS)
            logger.info(
                "[DOCTOR_VIEW] card_hydration_deferred_for_foreground admission_id=%s reason=%s age_sec=%s attempt=%s delay_ms=%s",
                admission_id,
                reason,
                None if age_sec is None else round(age_sec, 3),
                defer_attempts + 1,
                delay_ms,
            )
            record_metric(
                "card_hydration_deferred_for_foreground",
                1,
                admission_id=admission_id,
                reason=reason,
                age_sec=None if age_sec is None else round(age_sec, 3),
                attempt=defer_attempts + 1,
                source="refresh",
            )
            QTimer.singleShot(
                delay_ms,
                lambda: self._request_card_hydration_if_current(
                    admission_id,
                    shift_date,
                    context_key,
                    ensure_initial_status=ensure_initial_status,
                    defer_attempts=defer_attempts + 1,
                ),
            )
            return

        self._request_card_snapshot(
            ensure_initial_status=ensure_initial_status,
            show_empty_message=False,
            load_scope="patient_open_card",
        )

    def _ensure_monitor_subscription(self):
        data_service = self._get_data_service()
        if not data_service or self._monitor_connected:
            return
        data_service.changes_detected.connect(self._on_data_changes, Qt.QueuedConnection)
        self._monitor_connected = True

    def _disconnect_monitor(self):
        data_service = self._get_data_service()
        if not data_service or not self._monitor_connected:
            return
        try:
            data_service.changes_detected.disconnect(self._on_data_changes)
        except Exception:
            pass
        self._monitor_connected = False

    def _disconnect_snapshot_worker(self, worker):
        if worker is None:
            return
        for signal, slot in (
            (worker.succeeded, self._apply_card_snapshot),
            (worker.failed, self._on_card_snapshot_failed),
            (worker.finished, self._on_card_snapshot_finished),
        ):
            try:
                signal.disconnect(slot)
            except Exception:
                pass

    def _shutdown_snapshot_worker(self, timeout_ms: int = 1200):
        self._snapshot_pending = None
        self._create_card_after_snapshot = False
        worker = self._snapshot_worker
        self._snapshot_worker = None
        if worker is None:
            return
        self._disconnect_snapshot_worker(worker)
        if worker.isRunning():
            worker.quit()
            worker.wait(timeout_ms)

    def _request_pending_card_snapshot(self):
        if self._is_closing:
            self._snapshot_pending = None
            return
        pending = self._snapshot_pending
        self._snapshot_pending = None
        if not pending:
            return
        self._request_card_snapshot(
            ensure_initial_status=pending["ensure_initial_status"],
            show_empty_message=pending["show_empty_message"],
            load_scope=pending.get("load_scope", "full"),
        )

    def _request_card_snapshot(
        self,
        *,
        ensure_initial_status: bool = False,
        show_empty_message: bool = False,
        force_emit: bool = False,
        load_scope: str = "full",
    ):
        if self._is_closing:
            return
        if not self.admission_id:
            return
        ensure_initial_status = bool(ensure_initial_status) and self._should_ensure_initial_status_for_date(
            self._current_date
        )

        if self._snapshot_worker is not None:
            self._snapshot_pending = {
                "ensure_initial_status": ensure_initial_status,
                "show_empty_message": bool(show_empty_message),
                "load_scope": str(load_scope or "full"),
            }
            return

        self._snapshot_request_id += 1
        request = {
            "request_id": self._snapshot_request_id,
            "admission_id": int(self.admission_id),
            "shift_date": self._current_date,
            "ensure_initial_status": ensure_initial_status,
            "show_empty_message": bool(show_empty_message),
            "load_scope": str(load_scope or "full"),
            "context_key": self._current_snapshot_context_key(load_scope=load_scope),
        }

        worker = AsyncCallThread(self._build_card_snapshot_job, request)
        self._snapshot_worker = worker
        worker.succeeded.connect(self._apply_card_snapshot)
        worker.failed.connect(self._on_card_snapshot_failed)
        worker.finished.connect(self._on_card_snapshot_finished)
        message = (
            "Загрузка карты пациента..."
            if str(load_scope or "").startswith("patient_open")
            else "Обновление данных карты..."
        )
        show_app_loading(
            self,
            message,
            key=f"doctor-card-snapshot:{id(self)}",
            auto_hide_ms=20000,
        )
        worker.start()

        data_service = self._get_data_service()
        if data_service:
            data_service.request_immediate_refresh(
                force_emit=force_emit,
                source="card_snapshot:doctor",
            )

    def _build_card_snapshot_job(self, request: dict):
        load_scope = str(request.get("load_scope") or "full")
        if load_scope == "patient_open_vitals":
            coordinator = self._get_read_coordinator()
            if coordinator is not None:
                snapshot = coordinator.load_patient_vitals_snapshot(
                    request["admission_id"],
                    request["shift_date"],
                    role="doctor",
                    mode="archive" if self._archive_read_only_mode else "live",
                    source_db=self._archive_source_db_path if self._archive_read_only_mode else "live",
                    ensure_initial_status=request["ensure_initial_status"],
                    force_refresh=False,
                )
            else:
                logger.warning(
                    "DoctorRemCardWidget: ReadCoordinator unavailable, using build_full_card_snapshot for patient open"
                )
                snapshot = self.service.build_full_card_snapshot(
                    request["admission_id"],
                    request["shift_date"],
                    include_change_cursor=True,
                    include_balance=True,
                    balance_only_committed=True,
                    ensure_initial_status=request["ensure_initial_status"],
                )
        elif load_scope in {"patient_open_card", "full"}:
            coordinator = self._get_read_coordinator()
            if coordinator is not None and hasattr(coordinator, "load_patient_card_snapshot"):
                snapshot = coordinator.load_patient_card_snapshot(
                    request["admission_id"],
                    request["shift_date"],
                    role="doctor",
                    mode="archive" if self._archive_read_only_mode else "live",
                    source_db=self._archive_source_db_path if self._archive_read_only_mode else "live",
                    ensure_initial_status=request["ensure_initial_status"],
                    balance_only_committed=True,
                    force_refresh=False,
                )
            else:
                snapshot = self.service.build_full_card_snapshot(
                    request["admission_id"],
                    request["shift_date"],
                    include_change_cursor=True,
                    include_balance=True,
                    balance_only_committed=True,
                    ensure_initial_status=request["ensure_initial_status"],
                )
        else:
            snapshot = self.service.build_full_card_snapshot(
                request["admission_id"],
                request["shift_date"],
                include_change_cursor=True,
                include_balance=True,
                balance_only_committed=True,
                ensure_initial_status=request["ensure_initial_status"],
            )
        request["snapshot"] = snapshot
        return request

    def _apply_card_snapshot(self, request: dict):
        if self._is_closing:
            return
        request_id = request.get("request_id")
        if request_id is None and not request.get("from_cache"):
            logger.info(
                "DoctorRemCardWidget discarded snapshot without request_id current_request_id=%s",
                self._snapshot_request_id,
            )
            return
        if request_id is not None and request_id != self._snapshot_request_id:
            logger.info(
                "DoctorRemCardWidget discarded stale snapshot request_id=%s current_request_id=%s",
                request_id,
                self._snapshot_request_id,
            )
            return
        if (
            int(request["admission_id"]) != int(self.admission_id or 0)
            or request["shift_date"] != self._current_date
        ):
            return
        if request.get("context_key") != self._current_snapshot_context_key(
            load_scope=request.get("load_scope", "full")
        ):
            logger.info(
                "DoctorRemCardWidget discarded stale snapshot admission_id=%s load_scope=%s request_context=%s current_context=%s",
                request.get("admission_id"),
                request.get("load_scope"),
                request.get("context_key"),
                self._current_snapshot_context_key(load_scope=request.get("load_scope", "full")),
            )
            return

        snapshot = dict(request.get("snapshot") or {})
        previous_snapshot = self._card_snapshot_cache or {}
        snapshot = self._balance_snapshot_sync.merge_card_snapshot(snapshot)
        snapshot_signature = self._card_snapshot_apply_signature(snapshot)
        if (
            snapshot_signature is not None
            and snapshot_signature == self._last_applied_card_snapshot_signature
        ):
            self._card_snapshot_cache = snapshot
            self._sync_burn_patient_from_snapshot(snapshot)
            if snapshot.get("balance_runtime") is not None:
                self._balance_runtime_cache = snapshot.get("balance_runtime")
                self._balance_runtime_provisional = False
            self._last_change_id = max(
                int(self._last_change_id or 0),
                int(snapshot.get("change_id") or 0),
            )
            logger.info(
                "DoctorRemCardWidget skipped unchanged card snapshot admission_id=%s scope=%s version=%s",
                request.get("admission_id"),
                snapshot.get("scope"),
                snapshot.get("version"),
            )
            return
        if (
            previous_snapshot
            and not request.get("from_cache")
            and previous_snapshot.get("cache_key") == snapshot.get("cache_key")
            and int(previous_snapshot.get("version") or 0) == int(snapshot.get("version") or 0)
            and previous_snapshot.get("scope") == snapshot.get("scope")
            and previous_snapshot.get("load_trace_id") == snapshot.get("load_trace_id")
        ):
            logger.info(
                "DoctorRemCardWidget skipped unchanged cached snapshot admission_id=%s scope=%s version=%s",
                request.get("admission_id"),
                snapshot.get("scope"),
                snapshot.get("version"),
            )
            return
        self._card_snapshot_cache = snapshot
        self._sync_burn_patient_from_snapshot(snapshot)
        self._last_applied_card_snapshot_signature = snapshot_signature
        if snapshot.get("balance_runtime") is not None:
            self._balance_runtime_cache = snapshot.get("balance_runtime")
            self._balance_runtime_provisional = False
        effective_bounds = snapshot.get("effective_bounds")

        self._ensure_card_widgets_initialized()
        self._bind_balance_widgets_if_ready()

        if hasattr(self, "chart"):
            self._update_chart_from_snapshot(snapshot)
        else:
            self._schedule_chart_init()

        self._apply_vitals_input_snapshot(snapshot, effective_bounds)

        if hasattr(self, 'layout_manager') and hasattr(self.layout_manager, 'sector_2a'):
            self.layout_manager.sector_2a.update_period(snapshot.get("start_dt"))

        if hasattr(self, "balance_controller") and effective_bounds and snapshot.get("fluids") is not None:
            self.balance_controller.apply_loaded_data(
                snapshot.get("fluids") or [],
                effective_bounds,
            )

        self.update_patient_info()
        self._update_emergency_notice_sector(snapshot)
        self.update_latest_indicators()
        self.update_balance_data()

        if hasattr(self.layout_manager, "set_current_status_dto"):
            self.layout_manager.set_current_status_dto(snapshot.get("status"))
        if snapshot.get("status"):
            self._last_status = snapshot["status"].status
            self._update_ui_accessibility(snapshot["status"])

        self._last_change_id = max(
            int(self._last_change_id or 0),
            int(snapshot.get("change_id") or 0),
        )

        if request.get("show_empty_message") and not snapshot.get("vitals"):
            CustomMessageBox.information(
                self,
                "Пусто",
                f"Нет данных за {self._current_date.strftime('%d.%m.%Y')}",
            )

    def _apply_vitals_input_snapshot(self, snapshot: dict, effective_bounds):
        if not hasattr(self, "vitals_input") or not effective_bounds:
            return
        self.vitals_input.admission_id = self.admission_id
        self.vitals_input.shift_date = self._current_date
        self.vitals_input.apply_context_snapshot(
            patient=snapshot.get("patient"),
            settings=snapshot.get("settings") or {},
            effective_bounds=effective_bounds,
            has_vitals=bool(snapshot.get("has_vitals")),
            vitals=snapshot.get("vitals") or [],
        )

    def _on_card_snapshot_failed(self, exc: Exception):
        if self._is_closing:
            return
        exc_info = (type(exc), exc, exc.__traceback__) if isinstance(exc, BaseException) else None
        logger.error("DoctorRemCardWidget snapshot load failed: %s", exc, exc_info=exc_info)

        from rem_card.services.balance_errors import IncompleteBalanceError
        if isinstance(exc, IncompleteBalanceError):
            self._balance_snapshot_sync.report_error(str(exc))
            self._balance_snapshot_sync.schedule()
            self._request_card_snapshot(show_empty_message=False, load_scope="patient_open_vitals")

    def _on_card_snapshot_finished(self):
        worker = self.sender()
        if self._snapshot_worker is worker:
            self._snapshot_worker = None
        elif self._snapshot_worker is not None:
            return
        if self._is_closing:
            self._snapshot_pending = None
            self._create_card_after_snapshot = False
            return
        pending_create = self._create_card_after_snapshot
        if pending_create:
            self._create_card_after_snapshot = False
            self._snapshot_pending = None
            hide_app_loading(self, f"doctor-card-snapshot:{id(self)}", delay_ms=350)
            if isinstance(pending_create, dict):
                QTimer.singleShot(0, lambda data=pending_create: self.on_create_card_clicked(**data))
            else:
                QTimer.singleShot(0, self.on_create_card_clicked)
            return
        if self._snapshot_pending:
            QTimer.singleShot(0, self._request_pending_card_snapshot)
            return
        hide_app_loading(self, f"doctor-card-snapshot:{id(self)}", delay_ms=350)

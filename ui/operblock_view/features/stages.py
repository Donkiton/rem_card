from __future__ import annotations

from PySide6.QtWidgets import QDialog
from datetime import datetime
from datetime import timedelta
from rem_card.app.local_metrics import record_metric
from rem_card.app.logger import logger
from rem_card.services.concurrency import DataConflictError
from rem_card.services.operblock_anesthesia_prep import load_start_anesthesia_options
from rem_card.services.operblock_service import OPERBLOCK_TRANSFER_DEPARTMENT_OPTIONS
from rem_card.services.operblock_service import OperBlockConflictError
from rem_card.services.operblock_service import normalize_operblock_transfer_department
from rem_card.services.operblock_team import load_operblock_anesthesiologists
from rem_card.services.operblock_team import load_operblock_anesthetists
from rem_card.services.operblock_team import load_operblock_operating_nurses
from rem_card.services.operblock_team import load_operblock_surgeons
from rem_card.services.operblock_team import normalize_operblock_team_text
from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from typing import Any
import re
import time
import weakref
from rem_card.ui.operblock_view.operblock_helpers import (
    _minute_floor_dt,
    _parse_datetime_value,
    _safe_int,
)
from rem_card.ui.operblock_view.operblock_medication_edit_dialogs import (
    OperationStageTimeEditDialog,
)
from rem_card.ui.operblock_view.operblock_staff_dialog import (
    EditOperBlockStaffDialog,
)
from rem_card.ui.operblock_view.operblock_stage_dialogs import (
    EndAnesthesiaTransferDialog,
    EndSurgeryDialog,
    OperationStagesDialog,
    StartAnesthesiaDialog,
    StartSurgeryDialog,
)


class OperBlockStagesMixin:
    def _open_operblock_staff_editor(self) -> None:
        if self.is_view_only_mode():
            return
        if self._write_pending:
            return
        if not self._current_operation_case_id:
            return
        state = getattr(self, "_current_stage_state", {}) or {}
        surgery_enabled = bool(state.get("surgery_active"))
        anesthesia_enabled = bool(state.get("anesthesia_active"))
        if not surgery_enabled and not anesthesia_enabled:
            CustomMessageBox.warning(
                self,
                "Изменить состав",
                "Изменять состав можно только во время операции или анестезиологического пособия.",
            )
            self.refresh_protocol(force=True)
            return

        loading_key = self._show_operblock_loading(
            "Загрузка состава бригады...",
            key="staff-editor",
            auto_hide_ms=20000,
        )
        try:
            try:
                surgeon_options = load_operblock_surgeons()
                operating_nurse_options = load_operblock_operating_nurses()
                anesthesiologist_options = load_operblock_anesthesiologists()
                anesthetist_options = load_operblock_anesthetists()
            except Exception as exc:
                CustomMessageBox.warning(self, "Изменить состав", f"Не удалось загрузить сотрудников: {exc}")
                return
        finally:
            self._hide_operblock_loading(loading_key, delay_ms=0)

        current_surgeons = self._normal_staff_name_list(state.get("current_surgeons"), split_commas=True)
        if not current_surgeons:
            current_surgeons = self._normal_staff_name_list(state.get("current_surgeon"), split_commas=True)
        dialog = EditOperBlockStaffDialog(
            surgeon_options=surgeon_options,
            operating_nurse_options=operating_nurse_options,
            anesthesiologist_options=anesthesiologist_options,
            anesthetist_options=anesthetist_options,
            current_surgeons=current_surgeons,
            current_operating_nurse=normalize_operblock_team_text(state.get("current_operating_nurse")),
            current_anesthesiologist=normalize_operblock_team_text(state.get("current_anesthesiologist")),
            current_anesthetist=normalize_operblock_team_text(state.get("current_anesthetist")),
            surgery_enabled=surgery_enabled,
            anesthesia_enabled=anesthesia_enabled,
            parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return

        case_id = int(self._current_operation_case_id)
        payload: dict[str, object] = {}
        if surgery_enabled:
            payload["surgeons"] = dialog.selected_surgeons()
            payload["operating_nurse"] = dialog.selected_operating_nurse()
        if anesthesia_enabled:
            payload["anesthesiologist"] = dialog.selected_anesthesiologist()
            payload["anesthetist"] = dialog.selected_anesthetist()
        if not payload:
            return

        self._run_stage_action(
            f"operblock_update_staff:{case_id}",
            lambda: self.operblock_service.update_operation_staff(case_id, **payload),
        )

    def _run_stage_action(self, action_name: str, operation, success_message: str = ""):
        if self._write_pending:
            return
        self._write_pending = True
        self._apply_protocol_controls_state()
        self._enqueue_write(
            action_name,
            operation,
            on_success=lambda _result: self._on_stage_action_success(success_message),
            on_error=lambda exc: self._on_stage_action_error(exc),
        )

    def _on_stage_action_success(self, message: str = ""):
        self._write_pending = False
        if message:
            CustomMessageBox.information(self, "Оперблок", message)
        self.refresh_protocol(force=True)
        self.refresh_board(force=True)

    def _on_stage_action_error(self, exc: Exception):
        self._write_pending = False
        title = "Конфликт данных" if isinstance(exc, (DataConflictError, OperBlockConflictError)) else "Оперблок"
        CustomMessageBox.warning(self, title, str(exc))
        if self._current_operation_case_id:
            self.refresh_protocol(force=True)
        self.refresh_board(force=True)

    def _operation_case_defaults(self, operation_case_id: int) -> dict:
        try:
            return dict(self.operblock_service.get_operation_case_form_data(int(operation_case_id)) or {})
        except Exception as exc:
            logger.error("operblock operation case defaults load failed: %s", exc, exc_info=True)
            return {}

    def _default_anesthesia_start_datetime(
        self,
        operation_case_id: int | None = None,
        *,
        latest_vital_at: datetime | None = None,
    ) -> datetime:
        fallback = (
            _minute_floor_dt(self._current_operation_start)
            or _minute_floor_dt(self._current_protocol_date)
            or datetime.now().replace(second=0, microsecond=0)
        )
        latest_vital_dt = _minute_floor_dt(latest_vital_at)
        if latest_vital_dt is None and operation_case_id:
            try:
                vitals = self.operblock_service.list_operation_vitals(int(operation_case_id))
            except Exception as exc:
                logger.error("operblock anesthesia default time vitals load failed: %s", exc, exc_info=True)
                vitals = []
            for vital in vitals or []:
                timestamp = _minute_floor_dt(getattr(vital, "timestamp", None))
                if timestamp is not None and (latest_vital_dt is None or timestamp > latest_vital_dt):
                    latest_vital_dt = timestamp
        if latest_vital_dt is not None:
            return latest_vital_dt + timedelta(minutes=5)
        return fallback

    def _prepare_start_anesthesia_dialog_data(
        self,
        operation_case_id: int,
        fallback_start_datetime: datetime,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        status = "error"
        try:
            context = dict(
                self.operblock_service.get_start_anesthesia_context(int(operation_case_id))
                or {}
            )
            options = load_start_anesthesia_options()
            latest_vital_at = _minute_floor_dt(context.get("latest_vital_at"))
            initial_start_datetime = (
                latest_vital_at + timedelta(minutes=5)
                if latest_vital_at is not None
                else _minute_floor_dt(fallback_start_datetime)
            )
            status = "ok"
            return {
                **context,
                **options,
                "initial_start_datetime": initial_start_datetime,
            }
        finally:
            record_metric(
                "operblock_start_anesthesia_prepare_worker_ms",
                round((time.perf_counter() - started) * 1000.0, 3),
                operation_case_id=int(operation_case_id),
                status=status,
            )

    def _show_start_anesthesia_dialog(self, case_id: int, payload: dict[str, Any]) -> None:
        has_initial_vitals = bool(payload.get("has_initial_vitals"))
        self._current_operation_has_vitals = has_initial_vitals
        self._apply_protocol_controls_state()
        if not has_initial_vitals:
            CustomMessageBox.warning(
                self,
                "Начать пособие",
                "Перед началом пособия введите исходные витальные показатели.",
            )
            return

        defaults = dict(payload.get("defaults") or {})
        dialog = StartAnesthesiaDialog(
            list(payload.get("anesthesia_types") or []),
            list(payload.get("anesthesiologists") or []),
            list(payload.get("anesthetists") or []),
            self,
            initial_assistance_type=str(defaults.get("anesthesia_assistance_type") or ""),
            initial_anesthesiologist=str(defaults.get("anesthesiologist") or ""),
            initial_anesthetist=str(defaults.get("anesthetist") or ""),
            initial_start_datetime=_minute_floor_dt(payload.get("initial_start_datetime"))
            or self._default_anesthesia_start_datetime(),
            min_start_datetime=self._current_operation_start,
            max_start_datetime=self._current_operation_end,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        assistance_type = dialog.selected_assistance_type()
        anesthesiologist = dialog.selected_anesthesiologist()
        anesthetist = dialog.selected_anesthetist()
        event_time = dialog.start_datetime_text()
        self._run_stage_action(
            f"operblock_start_anesthesia:{case_id}",
            lambda: self.operblock_service.start_anesthesia(
                case_id,
                assistance_type,
                anesthesiologist=anesthesiologist,
                anesthetist=anesthetist,
                event_time=event_time,
            ),
            "Анестезиологическое пособие начато.",
        )

    def _default_surgery_start_datetime(self) -> datetime:
        base_dt = (
            _minute_floor_dt(self._current_anesthesia_start)
            or _minute_floor_dt(self._current_operation_start)
            or _minute_floor_dt(self._current_protocol_date)
            or datetime.now().replace(second=0, microsecond=0)
        )
        default_dt = base_dt + timedelta(minutes=5)
        latest_stage_dt = self._latest_stage_before_surgery_datetime()
        return max(default_dt, latest_stage_dt) if latest_stage_dt is not None else default_dt

    def _latest_stage_before_surgery_datetime(self) -> datetime | None:
        stage_times = [
            _minute_floor_dt(_parse_datetime_value(row.get("event_time")))
            for row in self._operation_stage_dialog_rows()
            if str(row.get("kind") or "") in {"anesthesia_start", "custom"}
        ]
        return max((value for value in stage_times if value is not None), default=None)

    @staticmethod
    def _clamp_stage_datetime(
        value: datetime | None,
        *,
        min_datetime: datetime | None = None,
        max_datetime: datetime | None = None,
    ) -> datetime:
        result = _minute_floor_dt(value) or datetime.now().replace(second=0, microsecond=0)
        min_dt = _minute_floor_dt(min_datetime)
        max_dt = _minute_floor_dt(max_datetime)
        if min_dt is not None and max_dt is not None and max_dt < min_dt:
            max_dt = None
        if min_dt is not None and result < min_dt:
            result = min_dt
        if max_dt is not None and result > max_dt:
            result = max_dt
        return result

    def _default_surgery_end_datetime(self) -> datetime:
        min_dt = _minute_floor_dt(self._current_surgery_start) or _minute_floor_dt(self._current_anesthesia_start)
        return self._clamp_stage_datetime(datetime.now(), min_datetime=min_dt, max_datetime=self._current_anesthesia_end)

    def _default_anesthesia_end_datetime(self) -> datetime:
        lower_bounds = [
            _minute_floor_dt(self._current_operation_start),
            _minute_floor_dt(self._current_anesthesia_start),
            _minute_floor_dt(self._current_surgery_end),
        ]
        min_dt = max((dt for dt in lower_bounds if dt is not None), default=None)
        return self._clamp_stage_datetime(datetime.now(), min_datetime=min_dt, max_datetime=self._current_operation_end)

    def _start_anesthesia(self):
        if self.is_view_only_mode():
            return
        if not self._current_operation_case_id:
            return
        active_worker = getattr(self, "_start_anesthesia_prep_worker", None)
        if active_worker is not None and active_worker.isRunning():
            return

        case_id = int(self._current_operation_case_id)
        fallback_start_datetime = self._default_anesthesia_start_datetime()
        generation = int(getattr(self, "_start_anesthesia_prep_generation", 0)) + 1
        self._start_anesthesia_prep_generation = generation
        self._start_anesthesia_prep_pending = True
        self._apply_protocol_controls_state()
        loading_key = self._show_operblock_loading(
            "Подготовка начала пособия...",
            key="start-anesthesia",
            auto_hide_ms=30000,
        )
        total_started = time.perf_counter()
        worker = AsyncCallThread(
            self._prepare_start_anesthesia_dialog_data,
            case_id,
            fallback_start_datetime,
            parent=self,
        )
        self._start_anesthesia_prep_worker = worker
        finalized = {"done": False}

        def is_stale_result() -> bool:
            return (
                bool(getattr(self, "_is_closing", False))
                or generation != int(getattr(self, "_start_anesthesia_prep_generation", 0))
                or case_id != int(getattr(self, "_current_operation_case_id", 0) or 0)
            )

        def finalize_preparation(status: str) -> None:
            if finalized["done"]:
                return
            finalized["done"] = True
            self._hide_operblock_loading(loading_key, delay_ms=0)
            if getattr(self, "_start_anesthesia_prep_worker", None) is worker:
                self._start_anesthesia_prep_worker = None
            if generation == int(getattr(self, "_start_anesthesia_prep_generation", 0)):
                self._start_anesthesia_prep_pending = False
                self._apply_protocol_controls_state()
            record_metric(
                "operblock_start_anesthesia_prepare_total_ms",
                round((time.perf_counter() - total_started) * 1000.0, 3),
                operation_case_id=case_id,
                status=status,
                ui_sync_reads=0,
            )

        def on_preparation_ready(payload):
            stale = is_stale_result()
            finalize_preparation("stale" if stale else "ok")
            if stale:
                return
            self._show_start_anesthesia_dialog(case_id, dict(payload or {}))

        def on_preparation_failed(exc):
            stale = is_stale_result()
            finalize_preparation("stale" if stale else "error")
            if stale:
                return
            logger.error("operblock start anesthesia preparation failed: %s", exc, exc_info=True)
            CustomMessageBox.warning(
                self,
                "Начать пособие",
                f"Не удалось подготовить начало пособия: {exc}",
            )

        worker.succeeded.connect(on_preparation_ready)
        worker.failed.connect(on_preparation_failed)
        worker.start()

    def _end_anesthesia(self):
        if self.is_view_only_mode():
            return
        if not self._current_operation_case_id:
            return
        case_id = int(self._current_operation_case_id)
        defaults = self._operation_case_defaults(case_id)
        initial_department = normalize_operblock_transfer_department(defaults.get("department_profile") or "")
        departments = list(OPERBLOCK_TRANSFER_DEPARTMENT_OPTIONS)
        if initial_department and initial_department.casefold() not in {item.casefold() for item in departments}:
            departments.insert(1, initial_department)
        dialog = EndAnesthesiaTransferDialog(
            departments,
            self,
            initial_department=initial_department,
            initial_end_datetime=self._default_anesthesia_end_datetime(),
            min_end_datetime=max(
                (
                    dt
                    for dt in (
                        _minute_floor_dt(self._current_operation_start),
                        _minute_floor_dt(self._current_anesthesia_start),
                        _minute_floor_dt(self._current_surgery_end),
                    )
                    if dt is not None
                ),
                default=None,
            ),
            max_end_datetime=self._current_operation_end,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        transfer_department = dialog.selected_department()
        event_time = dialog.end_datetime_text()
        handoff_id = None
        if normalize_operblock_transfer_department(transfer_department).casefold() == "рао":
            try:
                candidates = self.operblock_service.find_late_binding_candidates(
                    case_id,
                    target_department=transfer_department,
                )
            except Exception as exc:
                logger.error(
                    "operblock late handoff lookup failed case_id=%s: %s",
                    case_id,
                    exc,
                    exc_info=True,
                )
                candidates = []
            if len(candidates) == 1:
                candidate = candidates[0]
                patient = dict(candidate.get("patient_snapshot") or {})
                reply = CustomMessageBox.question(
                    self,
                    "Связать с картой РАО",
                    "В очереди РАО найден пациент с теми же номером истории, ФИО "
                    "и датой рождения:\n\n"
                    f"{patient.get('full_name') or 'ФИО не указано'}\n"
                    f"История: {patient.get('history_number') or 'не указана'}\n\n"
                    "Связать операционный случай с исходной картой и вернуть пациента "
                    "на зарезервированную койку?",
                    CustomMessageBox.Yes | CustomMessageBox.No,
                    CustomMessageBox.Yes,
                )
                if reply == CustomMessageBox.Yes:
                    handoff_id = int(candidate["id"])
        self._run_stage_action(
            f"operblock_end_anesthesia:{case_id}",
            lambda: self.operblock_service.end_anesthesia_with_transfer(
                case_id,
                transfer_department,
                event_time=event_time,
                handoff_id=handoff_id,
            ),
            "Анестезиологическое пособие завершено.",
        )

    def _start_surgery(self):
        if self.is_view_only_mode():
            return
        if not self._current_operation_case_id:
            return
        case_id = int(self._current_operation_case_id)
        try:
            surgeons = load_operblock_surgeons()
            operating_nurses = load_operblock_operating_nurses()
        except Exception as exc:
            CustomMessageBox.warning(self, "Начать операцию", f"Не удалось загрузить сотрудников для операции: {exc}")
            return
        defaults = self._operation_case_defaults(case_id)
        initial_start_dt = self._default_surgery_start_datetime()
        min_start_dt = max(
            (
                value
                for value in (
                    _minute_floor_dt(self._current_anesthesia_start),
                    _minute_floor_dt(self._current_operation_start),
                    self._latest_stage_before_surgery_datetime(),
                )
                if value is not None
            ),
            default=None,
        )
        dialog = StartSurgeryDialog(
            surgeons,
            operating_nurses,
            self,
            initial_operation_name=str(defaults.get("operation_name") or ""),
            initial_surgeons=list(defaults.get("surgeons") or []),
            initial_operating_nurse=str(defaults.get("operating_nurse") or ""),
            initial_start_datetime=initial_start_dt,
            min_start_datetime=min_start_dt,
            max_start_datetime=self._current_anesthesia_end,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        operation_name = dialog.operation_name()
        surgeons = dialog.selected_surgeons()
        operating_nurse = dialog.selected_operating_nurse()
        event_time = dialog.start_datetime_text()
        self._run_stage_action(
            f"operblock_start_surgery:{case_id}",
            lambda: self.operblock_service.start_surgery(
                case_id,
                operation_name=operation_name,
                surgeons=surgeons,
                operating_nurse=operating_nurse,
                event_time=event_time,
            ),
        )

    def _end_surgery(self):
        if self.is_view_only_mode():
            return
        if not self._current_operation_case_id:
            return
        case_id = int(self._current_operation_case_id)
        dialog = EndSurgeryDialog(
            self,
            initial_end_datetime=self._default_surgery_end_datetime(),
            min_end_datetime=self._current_surgery_start or self._current_anesthesia_start or self._current_operation_start,
            max_end_datetime=self._current_anesthesia_end,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        event_time = dialog.end_datetime_text()
        self._run_stage_action(
            f"operblock_end_surgery:{case_id}",
            lambda: self.operblock_service.end_surgery(case_id, event_time=event_time),
        )

    def _operation_stage_dialog_rows(self) -> list[dict]:
        snapshot = getattr(self, "_current_timeline_snapshot", None) or {}
        rows: list[dict] = []
        for event in snapshot.get("operation_events") or []:
            data = dict(event or {})
            payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
            kind = str((payload or {}).get("stage_kind") or (payload or {}).get("operation_stage") or "").strip()
            if kind not in {"anesthesia_start", "surgery_start", "custom"}:
                continue
            label = str(
                (payload or {}).get("label")
                or data.get("display_label")
                or data.get("raw_text")
                or ""
            ).strip()
            rows.append(
                {
                    "kind": kind,
                    "label": label,
                    "event_id": _safe_int(data.get("source_id")),
                    "source_id": _safe_int(data.get("source_id")),
                    "event_time": data.get("event_time"),
                    "revision": int(data.get("revision") or 0),
                    "payload": payload,
                    "display_label": data.get("display_label"),
                    "raw_text": data.get("raw_text"),
                }
            )

        existing_auto = {str(row.get("kind") or "") for row in rows}
        for event in (getattr(self, "_current_stage_state", {}) or {}).get("events") or []:
            kind = str((event or {}).get("kind") or "").strip()
            if kind not in {"anesthesia_start", "surgery_start"} or kind in existing_auto:
                continue
            rows.append(
                {
                    "kind": kind,
                    "label": str((event or {}).get("label") or ""),
                    "event_id": _safe_int((event or {}).get("id")),
                    "source_id": _safe_int((event or {}).get("id")),
                    "event_time": (event or {}).get("event_time"),
                    "revision": int((event or {}).get("revision") or 0),
                    "payload": {"stage_kind": kind, "label": str((event or {}).get("label") or "")},
                }
            )
        return rows

    def _operation_stages_available(self) -> bool:
        state = dict(getattr(self, "_current_stage_state", {}) or {})
        return bool(state.get("anesthesia_active"))

    def _open_operation_stages_dialog(self):
        if self.is_view_only_mode():
            return
        if self._write_pending:
            return
        if not self._current_operation_case_id:
            return
        if not self._operation_stages_available():
            CustomMessageBox.warning(
                self,
                "Этапы",
                "Этапы доступны после начала и до завершения пособия.",
            )
            return
        dialog = OperationStagesDialog(self._operation_stage_dialog_rows(), self)
        dialog_ref = weakref.ref(dialog)
        dialog.saveRequested.connect(lambda payload, ref=dialog_ref: self._save_operation_stage_from_dialog(ref, payload))
        dialog.timeEditRequested.connect(lambda payload, ref=dialog_ref: self._edit_operation_stage_time_from_dialog(ref, payload))
        dialog.exec()

    def _validate_operation_stage_datetime_or_warn(self, value: str | None) -> bool:
        event_dt = _minute_floor_dt(_parse_datetime_value(value))
        if event_dt is None:
            CustomMessageBox.warning(self, "Время этапа", "Укажите корректное время этапа.")
            return False
        anesthesia_start = _minute_floor_dt(self._current_anesthesia_start)
        if anesthesia_start is None:
            CustomMessageBox.warning(self, "Время этапа", "Не удалось определить начало пособия. Обновите протокол.")
            return False
        if event_dt < anesthesia_start:
            CustomMessageBox.warning(
                self,
                "Время этапа",
                f"Этап не может быть раньше начала пособия: {anesthesia_start.strftime('%d.%m.%Y %H:%M')}.",
            )
            return False
        anesthesia_end = _minute_floor_dt(self._current_anesthesia_end) if not self._current_anesthesia_active else None
        if anesthesia_end is not None and event_dt > anesthesia_end:
            CustomMessageBox.warning(
                self,
                "Время этапа",
                f"Этап операции не может быть позже окончания пособия: {anesthesia_end.strftime('%d.%m.%Y %H:%M')}.",
            )
            return False
        return True

    def _edit_operation_stage_time_from_dialog(self, dialog_ref, payload: dict):
        dialog = dialog_ref()
        row_key = str((payload or {}).get("row_key") or "")
        if dialog is None or self._write_pending:
            if dialog is not None:
                dialog.apply_save_error(row_key)
            return
        event_id = _safe_int((payload or {}).get("event_id"))
        is_new = bool((payload or {}).get("is_new"))
        old_event_dt = _minute_floor_dt(_parse_datetime_value((payload or {}).get("event_time")))
        label = re.sub(r"\s+", " ", str((payload or {}).get("label") or "").strip())
        if old_event_dt is None:
            dialog.apply_save_error(row_key)
            CustomMessageBox.warning(self, "Время этапа", "Не удалось определить этап. Обновите протокол.")
            return
        if not is_new and (not event_id or not label):
            dialog.apply_save_error(row_key)
            CustomMessageBox.warning(self, "Время этапа", "Не удалось определить этап. Обновите протокол.")
            return
        time_dialog = OperationStageTimeEditDialog(
            old_event_dt,
            self,
            stage_label=label or "Новый этап",
            field_label="Время этапа",
            min_datetime=self._current_anesthesia_start,
            max_datetime=self._current_anesthesia_end if not self._current_anesthesia_active else None,
        )
        if time_dialog.exec() != QDialog.Accepted:
            return
        event_time = time_dialog.datetime_text()
        new_event_dt = _minute_floor_dt(_parse_datetime_value(event_time))
        if new_event_dt == old_event_dt:
            return
        if not self._validate_operation_stage_datetime_or_warn(event_time):
            return
        if is_new:
            dialog.apply_pending_stage_time(row_key, event_time)
            return
        self._save_operation_stage_from_dialog(
            dialog_ref,
            {
                "row_key": row_key,
                "event_id": event_id,
                "expected_revision": int((payload or {}).get("expected_revision") or 0),
                "label": label,
                "event_time": event_time,
                "is_new": False,
            },
        )

    def _save_operation_stage_from_dialog(self, dialog_ref, payload: dict):
        dialog = dialog_ref()
        row_key = str((payload or {}).get("row_key") or "")
        if dialog is None:
            return
        if self._write_pending:
            dialog.apply_save_error(row_key)
            return
        if not self._current_operation_case_id:
            dialog.apply_save_error(row_key)
            return
        label = re.sub(r"\s+", " ", str((payload or {}).get("label") or "").strip())
        if not label:
            dialog.apply_save_error(row_key)
            CustomMessageBox.warning(self, "Этапы", "Укажите название этапа.")
            return
        case_id = int(self._current_operation_case_id)
        is_new = bool((payload or {}).get("is_new"))
        event_id = _safe_int((payload or {}).get("event_id"))
        expected_revision = int((payload or {}).get("expected_revision") or 0)
        event_time = str((payload or {}).get("event_time") or "").strip() or None
        if not is_new and not event_id:
            dialog.apply_save_error(row_key)
            CustomMessageBox.warning(self, "Этапы", "Не удалось определить этап. Обновите протокол.")
            return
        if event_time is not None and not self._validate_operation_stage_datetime_or_warn(event_time):
            dialog.apply_save_error(row_key)
            return

        write_description = (
            f"operblock_add_operation_stage:{case_id}"
            if is_new
            else f"operblock_update_operation_stage:{int(event_id)}"
        )
        self._write_pending = True
        self._apply_protocol_controls_state()
        self._remember_local_write_refresh_suppression(write_description, {"operblock_timeline_events"})

        def operation():
            if is_new:
                return self.operblock_service.add_operation_stage(case_id, label, event_time=event_time)
            return self.operblock_service.update_operation_stage(
                int(event_id),
                label,
                expected_revision=expected_revision,
                event_time=event_time,
            )

        self._enqueue_write(
            write_description,
            operation,
            on_success=lambda result, ref=dialog_ref, key=row_key: self._on_operation_stage_saved(ref, key, result),
            on_error=lambda exc, ref=dialog_ref, key=row_key: self._on_operation_stage_save_error(ref, key, exc),
        )

    @staticmethod
    def _normalized_operation_stage_event(stage: dict) -> dict:
        data = dict(stage or {})
        source_id = _safe_int(data.get("source_id") or data.get("event_id"))
        if source_id:
            data["id"] = f"timeline_event:{int(source_id)}"
            data["source"] = "timeline_event"
            data["source_id"] = int(source_id)
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        payload = dict(payload or {})
        payload["stage_kind"] = str(payload.get("stage_kind") or "custom").strip() or "custom"
        label = re.sub(
            r"\s+",
            " ",
            str(payload.get("label") or data.get("display_label") or data.get("raw_text") or "").strip(),
        )
        if label:
            payload["label"] = label
            data["drug_label"] = label
            data["display_label"] = label
            data["raw_text"] = label
        data["payload"] = payload
        data["event_type"] = "clinical_event"
        data["status"] = str(data.get("status") or "active")
        data["revision"] = int(data.get("revision") or 0)
        return data

    def _patch_operation_stage_event_locally(self, stage: dict) -> bool:
        snapshot = dict(getattr(self, "_current_timeline_snapshot", None) or {})
        if not snapshot:
            return False
        stage_event = self._normalized_operation_stage_event(stage)
        source_id = _safe_int(stage_event.get("source_id"))
        if not source_id:
            return False
        events = []
        replaced = False
        for event in list(snapshot.get("operation_events") or []):
            data = dict(event or {})
            if _safe_int(data.get("source_id")) == int(source_id):
                if not replaced:
                    events.append(stage_event)
                    replaced = True
                continue
            events.append(data)
        if not replaced:
            events.append(stage_event)
        events.sort(
            key=lambda item: (
                _parse_datetime_value((item or {}).get("event_time")) or datetime.min,
                _safe_int((item or {}).get("source_id")) or 0,
            )
        )
        snapshot["operation_events"] = events
        self._current_timeline_snapshot = self._refresh_timeline_snapshot_hash(snapshot)
        return True

    def _update_single_operation_stage_marker(self, stage: dict) -> bool:
        chart = getattr(self, "vitals_chart", None)
        if chart is None or not hasattr(chart, "patch_operation_stage_marker"):
            return False
        start_dt = getattr(chart, "start_time", None)
        if not isinstance(start_dt, datetime):
            transform = getattr(chart, "_timeline_transform", None)
            start_dt = getattr(transform, "display_origin_at", None)
        if not isinstance(start_dt, datetime):
            start_dt = self._current_operation_start or self._current_protocol_date
        return bool(
            chart.patch_operation_stage_marker(
                self._normalized_operation_stage_event(stage),
                snapshot=getattr(self, "_current_timeline_snapshot", None),
                start_time=start_dt,
            )
        )

    def _on_operation_stage_saved(self, dialog_ref, row_key: str, result):
        self._write_pending = False
        self._apply_protocol_controls_state()
        stage = self._normalized_operation_stage_event(dict(result or {}))
        patched = self._patch_operation_stage_event_locally(stage)
        dialog = dialog_ref()
        if patched:
            if dialog is not None:
                dialog.apply_saved_stage(row_key, stage)
            if not self._update_single_operation_stage_marker(stage):
                self._update_vitals_chart_order_markers()
            return
        if dialog is not None:
            dialog.apply_save_error(row_key)
        self.refresh_protocol(force=True)

    def _on_operation_stage_save_error(self, dialog_ref, row_key: str, exc: Exception):
        self._write_pending = False
        self._apply_protocol_controls_state()
        dialog = dialog_ref()
        if dialog is not None:
            dialog.apply_save_error(row_key)
        title = "Конфликт данных" if isinstance(exc, (DataConflictError, OperBlockConflictError)) else "Этапы"
        CustomMessageBox.warning(self, title, str(exc))
        if isinstance(exc, (DataConflictError, OperBlockConflictError)) and self._current_operation_case_id:
            self.refresh_protocol(force=True)

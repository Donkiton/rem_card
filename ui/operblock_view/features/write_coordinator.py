from __future__ import annotations

from PySide6.QtCore import QTimer
from rem_card.app.foreground_activity import finish_foreground_resume_lease
from rem_card.app.foreground_activity import mark_foreground_activity
from rem_card.app.foreground_activity import start_foreground_resume_lease
from rem_card.app.local_metrics import record_metric
from rem_card.app.logger import logger
from rem_card.app.sqlite_shared import OPBLOCK_INTERACTIVE_WRITE_LOCK_TIMEOUT_MS
from rem_card.app.sqlite_shared import OpBlockInteractiveWriteBusyTimeout
from typing import Any
import time
import uuid
from rem_card.ui.operblock_view.operblock_visual_primitives import (
    OPERBLOCK_IDLE_DIAGNOSTIC_THRESHOLD_SEC,
)
from rem_card.ui.operblock_view.operblock_vitals_adapter import (
    _sanitize_diagnostic_message,
)


class OperBlockWriteCoordinatorMixin:
    def _diagnostic_role(self) -> str:
        code = str(self._table_filter_code or "").strip()
        return f"operblock_{code}" if code else "operblock"

    def _diagnostic_current_screen(self) -> str:
        stack = getattr(self, "stack", None)
        current = stack.currentWidget() if stack is not None else None
        if current == getattr(self, "protocol_page", None):
            return "protocol"
        if current == getattr(self, "archive_page", None):
            return "archive"
        if current == getattr(self, "settings_page", None):
            return "settings"
        return "board"

    def _diagnostic_table_code(self) -> str:
        if self._table_filter_code:
            return str(self._table_filter_code)
        for source in (
            getattr(self, "_current_timeline_snapshot", None),
            getattr(self, "_current_stage_state", None),
        ):
            if isinstance(source, dict):
                table_code = str(source.get("table_code") or "").strip()
                if table_code:
                    return table_code
        return ""

    def _record_user_idle_diagnostics(self, action: str) -> dict[str, Any]:
        now = time.monotonic()
        previous = float(getattr(self, "_opblock_idle_last_activity_monotonic", now) or now)
        idle_ms = max(0.0, (now - previous) * 1000.0)
        threshold_ms = OPERBLOCK_IDLE_DIAGNOSTIC_THRESHOLD_SEC * 1000.0
        returned_from_idle = False
        if idle_ms >= threshold_ms and not bool(getattr(self, "_opblock_idle_period_reported", False)):
            common = {
                "role": self._diagnostic_role(),
                "current_screen": self._diagnostic_current_screen(),
                "current_admission_id": self._current_admission_id,
                "current_operation_case_id": self._current_operation_case_id,
                "idle_ms": round(idle_ms, 3),
                "timestamp_ms": int(time.time() * 1000.0),
            }
            record_metric("user_idle_detected", 1, **common)
            record_metric(
                "user_return_from_idle",
                1,
                role=common["role"],
                idle_ms=common["idle_ms"],
                first_action=str(action or ""),
                current_screen=common["current_screen"],
                admission_id=self._current_admission_id,
                operation_case_id=self._current_operation_case_id,
                timestamp_ms=common["timestamp_ms"],
            )
            self._opblock_idle_period_reported = True
            returned_from_idle = True
        elif idle_ms < threshold_ms:
            self._opblock_idle_period_reported = False
        self._opblock_idle_last_activity_monotonic = now
        return {"idle_ms": idle_ms, "returned_from_idle": returned_from_idle}

    def _finish_foreground_resume_lease_later(self, lease_id: str, result: str = "expired") -> None:
        finished = finish_foreground_resume_lease(lease_id, result=result)
        active = dict(getattr(self, "_active_foreground_resume_lease", None) or {})
        if active.get("lease_id") == lease_id:
            self._active_foreground_resume_lease = None
        if finished:
            last = dict(getattr(self, "_last_opblock_action", None) or {})
            if last.get("foreground_lease_id") == lease_id:
                last["foreground_lease_finished"] = True
                self._last_opblock_action = last

    def _maybe_start_foreground_resume_lease(self, action: str, idle_info: dict[str, Any]) -> dict[str, Any] | None:
        if not bool((idle_info or {}).get("returned_from_idle")):
            return None
        idle_ms = float((idle_info or {}).get("idle_ms") or 0.0)
        lease = start_foreground_resume_lease(
            role=self._diagnostic_role(),
            idle_ms=idle_ms,
            first_action=str(action or ""),
            current_screen=self._diagnostic_current_screen(),
            admission_id=self._current_admission_id,
            operation_case_id=self._current_operation_case_id,
            table_code=self._diagnostic_table_code(),
        )
        if not lease:
            return None
        self._active_foreground_resume_lease = dict(lease)
        mark_foreground_activity(
            "opblock_resume",
            admission_id=self._current_admission_id,
            source="click",
            ttl_sec=max(1.0, float(lease.get("remaining_ms") or 0.0) / 1000.0),
            request_id=str(lease.get("lease_id") or ""),
            operation_case_id=self._current_operation_case_id,
        )
        remaining_ms = int(max(1000.0, float(lease.get("remaining_ms") or 0.0)))
        QTimer.singleShot(
            remaining_ms,
            lambda lease_id=str(lease.get("lease_id") or ""): self._finish_foreground_resume_lease_later(lease_id),
        )
        return dict(lease)

    def _start_opblock_action_diagnostics(self, description: str) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        action = str(description or "")
        idle_info = self._record_user_idle_diagnostics(action)
        idle_ms = float((idle_info or {}).get("idle_ms") or 0.0)
        lease = self._maybe_start_foreground_resume_lease(action, idle_info)
        started = time.monotonic()
        payload = {
            "action": action,
            "source": "foreground_resume" if lease else action,
            "priority": "user_visible" if lease else "",
            "foreground_lease_id": str((lease or {}).get("lease_id") or ""),
            "operation_case_id": self._current_operation_case_id,
            "admission_id": self._current_admission_id,
            "table_code": self._diagnostic_table_code(),
            "request_id": request_id,
            "idle_before_action_ms": round(idle_ms, 3),
            "timestamp_ms": int(time.time() * 1000.0),
            "started_monotonic": started,
            "pending_since_monotonic": started if self._write_pending else None,
            "screen": self._diagnostic_current_screen(),
            "first_action_after_idle": bool(lease),
        }
        self._active_opblock_action = dict(payload)
        self._last_opblock_action = dict(payload)
        record_metric("opblock_action_started", 1, **{k: v for k, v in payload.items() if not k.endswith("_monotonic")})
        if self._write_pending:
            record_metric(
                "ui_pending_state_observed",
                1,
                active_opblock_action=action,
                request_id=request_id,
                pending_since_ms=0,
                widget_alive=True,
                case_still_current=True,
                source="opblock_action_started",
            )
        return payload

    @staticmethod
    def _diagnostic_result_for_error(exc: Exception) -> str:
        if isinstance(exc, OpBlockInteractiveWriteBusyTimeout):
            return "busy_timeout"
        text = str(exc or "").lower()
        if "timeout" in text or "timed out" in text:
            return "timeout"
        if "busy" in text or "locked" in text or "занят" in text:
            return "busy"
        return "error"

    @staticmethod
    def _is_interactive_busy_timeout(exc: Exception) -> bool:
        if isinstance(exc, OpBlockInteractiveWriteBusyTimeout):
            return True
        text = f"{type(exc).__name__} {exc}".lower()
        return "interactive" in text and "busy" in text and "timeout" in text

    def _finish_opblock_action_diagnostics(self, action_info: dict[str, Any] | None, result: str, exc: Exception | None = None):
        if not action_info:
            return
        duration_ms = round((time.monotonic() - float(action_info.get("started_monotonic") or time.monotonic())) * 1000.0, 3)
        payload = {
            "action": str(action_info.get("action") or ""),
            "request_id": str(action_info.get("request_id") or ""),
            "result": result,
            "duration_ms": duration_ms,
            "error_class": type(exc).__name__ if exc is not None else "",
            "error_message_sanitized": _sanitize_diagnostic_message(exc) if exc is not None else "",
        }
        record_metric("opblock_action_finished", duration_ms, **payload)
        if (
            self._active_opblock_action
            and self._active_opblock_action.get("request_id") == action_info.get("request_id")
        ):
            self._active_opblock_action = None
        self._last_opblock_action = {**dict(action_info), **payload, "finished_monotonic": time.monotonic()}

    def diagnostic_snapshot(self) -> dict[str, Any]:
        active = dict(getattr(self, "_active_opblock_action", None) or {})
        last = dict(getattr(self, "_last_opblock_action", None) or {})
        lease = dict(getattr(self, "_active_foreground_resume_lease", None) or {})
        pending_since = active.get("pending_since_monotonic")
        pending_since_ms = None
        if isinstance(pending_since, (int, float)):
            pending_since_ms = round(max(0.0, (time.monotonic() - float(pending_since)) * 1000.0), 3)
        lease_age_ms = None
        if lease:
            lease_age_ms = max(0.0, float(lease.get("age_ms") or 0.0))
            started = active.get("started_monotonic") if active.get("foreground_lease_id") == lease.get("lease_id") else None
            if isinstance(started, (int, float)):
                lease_age_ms = round(max(lease_age_ms, (time.monotonic() - float(started)) * 1000.0), 3)
        return {
            "active_opblock_action": str(active.get("action") or ""),
            "active_opblock_request_id": str(active.get("request_id") or ""),
            "last_user_action": str(last.get("action") or ""),
            "first_action_after_idle": str(lease.get("first_action") or last.get("action") or ""),
            "idle_before_action_ms": active.get("idle_before_action_ms") or last.get("idle_before_action_ms"),
            "current_operation_case_id": self._current_operation_case_id,
            "current_admission_id": self._current_admission_id,
            "current_table_code": self._diagnostic_table_code(),
            "active_foreground_resume_lease": str(lease.get("lease_id") or ""),
            "foreground_lease_age_ms": lease_age_ms,
            "foreground_lease_reason": str(lease.get("reason") or ""),
            "ui_pending_action": str(active.get("action") or "") if self._write_pending else "",
            "ui_pending_since_ms": pending_since_ms if self._write_pending else None,
            "widget_alive": not bool(getattr(self, "_is_closing", False)),
            "case_still_current": (
                not active
                or not active.get("operation_case_id")
                or active.get("operation_case_id") == self._current_operation_case_id
            ),
        }

    def _enqueue_write(self, description: str, operation, on_success, on_error):
        if self.is_view_only_mode():
            logger.info("Operblock view-only write skipped: %s", description)
            self._write_pending = False
            self._apply_protocol_controls_state()
            return
        action_info = self._start_opblock_action_diagnostics(description)

        def diagnostic_success(result):
            try:
                on_success(result)
            finally:
                self._finish_opblock_action_diagnostics(action_info, "success")

        def diagnostic_error(exc: Exception):
            busy_timeout = self._is_interactive_busy_timeout(exc)
            pending_before = bool(getattr(self, "_write_pending", False))
            try:
                on_error(exc)
            finally:
                pending_after_handler = bool(getattr(self, "_write_pending", False))
                if busy_timeout and pending_after_handler:
                    self._write_pending = False
                    try:
                        self._set_protocol_write_controls_enabled(True)
                    except Exception:
                        pass
                if busy_timeout and (pending_before or pending_after_handler or not getattr(self, "_write_pending", False)):
                    record_metric(
                        "ui_pending_cleared_after_busy_timeout",
                        1,
                        action=str(action_info.get("action") or description),
                        request_id=str(action_info.get("request_id") or ""),
                        operation_case_id=action_info.get("operation_case_id"),
                        admission_id=action_info.get("admission_id"),
                        foreground_lease_id=str(action_info.get("foreground_lease_id") or ""),
                        timeout_ms=getattr(exc, "timeout_ms", OPBLOCK_INTERACTIVE_WRITE_LOCK_TIMEOUT_MS),
                    )
                self._finish_opblock_action_diagnostics(
                    action_info,
                    self._diagnostic_result_for_error(exc),
                    exc,
                )

        if not self.data_service:
            try:
                result = operation()
            except Exception as exc:
                diagnostic_error(exc)
            else:
                diagnostic_success(result)
            return
        write_metadata = {
            "interactive": True,
            "role": self._diagnostic_role(),
            "request_id": str(action_info.get("request_id") or ""),
            "idle_before_action_ms": action_info.get("idle_before_action_ms"),
            "foreground_lease_id": str(action_info.get("foreground_lease_id") or ""),
            "admission_id": action_info.get("admission_id"),
            "operation_case_id": action_info.get("operation_case_id"),
            "table_code": str(action_info.get("table_code") or ""),
            "timeout_ms": OPBLOCK_INTERACTIVE_WRITE_LOCK_TIMEOUT_MS,
        }
        self.data_service.enqueue_write(
            description,
            operation,
            on_success=diagnostic_success,
            on_error=diagnostic_error,
            write_metadata=write_metadata,
        )

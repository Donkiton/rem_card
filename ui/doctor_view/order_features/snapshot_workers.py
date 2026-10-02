"""SnapshotWorkerLifecycleMixin for the doctor orders widget."""

from datetime import datetime
from rem_card.app.local_metrics import record_metric
from rem_card.app.logger import logger
import time


class SnapshotWorkerLifecycleMixin:
    def _reset_pending_snapshot_request(self):
        self._snapshot_pending = False
        self._snapshot_force_pending = False
        self._snapshot_pending_source = "refresh"
        self._snapshot_pending_priority = "MEDIUM"
        self._snapshot_pending_reason = None
        self._forced_reload_pending_key = None

    def _disconnect_snapshot_worker(self, worker):
        if worker is None:
            return
        for signal, slot in (
            (worker.succeeded, self._apply_snapshot),
            (worker.failed, self._on_snapshot_failed),
            (worker.finished, self._on_snapshot_finished),
        ):
            try:
                signal.disconnect(slot)
            except Exception:
                pass

    def _retire_snapshot_worker_state(
        self,
        *,
        state: str,
        reason: str,
        replacement_request_id: str | None = None,
    ):
        active = dict(getattr(self, "_active_snapshot_worker_state", {}) or {})
        if not active and self._active_request_id:
            active = {
                "request_id": self._active_request_id,
                "generation": self._active_request_generation,
                "source": self._active_request_source,
                "priority": self._active_request_priority,
                "admission_id": self.admission_id,
                "started_monotonic": self._active_request_started_monotonic,
                "context_key": self._active_request_context_key,
                "seq": self._active_request_seq,
                "force": self._active_request_force,
            }
        if not active:
            return {}
        active["state"] = state
        active["retired_reason"] = reason
        active["retired_at"] = datetime.now().isoformat(timespec="milliseconds")
        active["replacement_request_id"] = replacement_request_id
        request_id = str(active.get("request_id") or "")
        if request_id:
            self._retired_snapshot_worker_states[request_id] = active
            while len(self._retired_snapshot_worker_states) > 50:
                oldest_key = next(iter(self._retired_snapshot_worker_states))
                self._retired_snapshot_worker_states.pop(oldest_key, None)
        self._active_snapshot_worker_state = {}
        return active

    def _reset_active_snapshot_request(self):
        self._active_request_context_key = None
        self._active_request_force = False
        self._active_request_priority = "MEDIUM"
        self._active_request_seq = 0
        self._active_request_id = ""
        self._active_request_generation = 0
        self._active_request_source = "refresh"
        self._active_request_started_monotonic = 0.0
        self._forced_reload_active_key = None
        self._active_snapshot_worker_state = {}

    def _detach_snapshot_worker(
        self,
        worker,
        *,
        state: str,
        reason: str,
        replacement_request_id: str | None = None,
    ):
        retired = self._retire_snapshot_worker_state(
            state=state,
            reason=reason,
            replacement_request_id=replacement_request_id,
        )
        self._disconnect_snapshot_worker(worker)
        try:
            worker.quit()
        except Exception:
            pass
        if self._snapshot_worker is worker:
            self._snapshot_worker = None
        self._post_finalize_watchdog_timer.stop()
        self._reset_active_snapshot_request()
        logger.warning(
            "[OrdersWidget] snapshot_worker_detached admission_id=%s request_id=%s source=%s priority=%s state=%s reason=%s replacement_request_id=%s",
            self.admission_id,
            retired.get("request_id"),
            retired.get("source"),
            retired.get("priority"),
            state,
            reason,
            replacement_request_id,
        )
        record_metric(
            "orders_snapshot_worker_detached",
            1,
            admission_id=self.admission_id,
            source=str(retired.get("source") or "refresh"),
            priority=str(retired.get("priority") or "MEDIUM"),
            request_id=retired.get("request_id"),
            generation=retired.get("generation"),
            state=state,
            reason=reason,
            replacement_request_id=replacement_request_id,
        )
        return retired

    def _active_snapshot_context_key(self):
        state = getattr(self, "_active_snapshot_worker_state", {}) or {}
        return state.get("context_key") or self._active_request_context_key

    def _record_snapshot_worker_superseded_context_switch(
        self,
        *,
        retired: dict,
        new_context_key,
        new_context_hash: str | None,
        replacement_request_id: str | None,
    ) -> None:
        logger.warning(
            "[OrdersWidget] orders_snapshot_worker_superseded_context_switch old_admission_id=%s new_admission_id=%s request_id=%s replacement_request_id=%s old_context=%s new_context=%s",
            retired.get("admission_id"),
            self.admission_id,
            retired.get("request_id"),
            replacement_request_id,
            retired.get("context_key"),
            new_context_key,
        )
        record_metric(
            "orders_snapshot_worker_superseded_context_switch",
            1,
            role="doctor",
            admission_id=int(self.admission_id or 0),
            old_admission_id=retired.get("admission_id"),
            request_id=retired.get("request_id"),
            generation=retired.get("generation"),
            replacement_request_id=replacement_request_id,
            old_context_key=str(retired.get("context_key") or ""),
            new_context_key=str(new_context_key or ""),
            context_hash=str(new_context_hash or ""),
            source=str(retired.get("source") or "refresh"),
            priority=str(retired.get("priority") or "MEDIUM"),
        )

    def _detach_active_snapshot_for_context_switch(
        self,
        *,
        new_context_key,
        new_context_hash: str | None = None,
        replacement_request_id: str | None = None,
    ) -> bool:
        worker = self._snapshot_worker
        if worker is None:
            return False
        try:
            worker_running = bool(worker.isRunning())
        except Exception:
            worker_running = False
        if not worker_running:
            return False
        old_context_key = self._active_snapshot_context_key()
        if old_context_key == new_context_key:
            return False
        retired = self._detach_snapshot_worker(
            worker,
            state="superseded",
            reason="context_switch",
            replacement_request_id=replacement_request_id,
        )
        self._record_snapshot_worker_superseded_context_switch(
            retired=retired,
            new_context_key=new_context_key,
            new_context_hash=new_context_hash,
            replacement_request_id=replacement_request_id,
        )
        return True

    def _disconnect_load_yesterday_worker(self, worker):
        if worker is None:
            return
        for signal, slot in (
            (worker.succeeded, self._on_load_yesterday_ready),
            (worker.failed, self._on_load_yesterday_failed),
            (worker.finished, self._on_load_yesterday_finished),
        ):
            try:
                signal.disconnect(slot)
            except Exception:
                pass

    def shutdown(self, timeout_ms: int = 1200):
        self._is_closing = True
        self._reset_pending_snapshot_request()
        for timer_name in (
            "_fast_sync_timer",
            "_state_sync_timer",
            "_change_batch_timer",
            "_soft_update_timer",
            "_post_finalize_watchdog_timer",
            "timer",
        ):
            timer = getattr(self, timer_name, None)
            if timer is not None:
                timer.stop()

        worker = self._snapshot_worker
        self._snapshot_worker = None
        self._post_finalize_retry_after_cancel = False
        self._post_finalize_retry_context_hash = None
        self._reset_active_snapshot_request()
        self._disconnect_snapshot_worker(worker)
        if worker is not None and worker.isRunning():
            worker.quit()
            worker.wait(timeout_ms)

        load_worker = self._load_yesterday_worker
        self._load_yesterday_worker = None
        self._disconnect_load_yesterday_worker(load_worker)
        if load_worker is not None and load_worker.isRunning():
            load_worker.quit()
            load_worker.wait(timeout_ms)

    def _clear_local_cell_draft_guard(self):
        self._local_cell_draft_guard = False
        self._local_cell_draft_guard_signatures = {}
        self._flush_deferred_forced_reload_after_guard()

    def _discard_deferred_forced_reload_after_guard(self, *, discard_reason: str):
        key = self._forced_reload_after_guard_key
        reason = self._forced_reload_after_guard_reason
        if key is None:
            return
        self._forced_reload_after_guard_key = None
        self._forced_reload_after_guard_reason = None
        reload_reason = str(reason or key[2])
        logger.info(
            "[OrdersSync] orders_deferred_reload_discarded_context_reset role=doctor old_admission_id=%s current_admission_id=%s reason=%s context_hash=%s discard_reason=%s",
            key[0],
            self.admission_id,
            reload_reason,
            key[1],
            discard_reason,
        )
        record_metric(
            "orders_deferred_reload_discarded_context_reset",
            1,
            role="doctor",
            admission_id=key[0],
            current_admission_id=int(self.admission_id or 0),
            context_hash=key[1],
            reason=reload_reason,
            discard_reason=discard_reason,
        )

    def _mark_local_cell_draft_guard(self, changed_keys=None):
        if self.has_drafts():
            self._local_cell_draft_guard = True
            if changed_keys and self.model is not None:
                for key in changed_keys:
                    self._local_cell_draft_guard_signatures[key] = self._admin_guard_signature(
                        self.model.admin_map.get(key)
                    )
            self._admin_only_snapshot_until = max(
                self._admin_only_snapshot_until,
                time.monotonic() + self._admin_only_snapshot_window_sec,
            )

    @staticmethod
    def _admin_guard_signature(admin):
        if admin is None:
            return None
        return (
            str(getattr(admin, "status", "") or ""),
            str(getattr(admin, "cell_role", "") or ""),
            int(getattr(admin, "is_committed", 0) or 0),
        )

    @staticmethod
    def _admin_row_guard_signature(row):
        if row is None:
            return None
        return (
            str(row.get("status") or ""),
            str(row.get("cell_role") or ""),
            int(row.get("is_committed") or 0),
        )

    @staticmethod
    def _admin_row_key(row):
        try:
            planned_time = datetime.fromisoformat(str(row.get("planned_time")))
            planned_key = planned_time.isoformat()
        except Exception:
            planned_key = str(row.get("planned_time") or "")
        try:
            order_id = int(row.get("order_id"))
        except Exception:
            order_id = row.get("order_id")
        return (order_id, planned_key)

    def _snapshot_matches_local_cell_guard(self, snapshot) -> bool:
        guard_signatures = dict(self._local_cell_draft_guard_signatures or {})
        if not guard_signatures:
            return bool(snapshot.get("has_any_draft", False))

        snapshot_by_key = {}
        for row in snapshot.get("admin_rows") or []:
            if not isinstance(row, dict):
                row = dict(row)
            snapshot_by_key[self._admin_row_key(row)] = row

        return all(
            self._admin_row_guard_signature(snapshot_by_key.get(key)) == signature
            for key, signature in guard_signatures.items()
        )

    def _should_preserve_local_cell_draft(self, snapshot) -> bool:
        if self._has_local_draft_changes():
            self._local_cell_draft_guard = True
            return True
        if not self._local_cell_draft_guard:
            return False
        if self._snapshot_matches_local_cell_guard(snapshot):
            self._clear_local_cell_draft_guard()
            return False
        if not self.has_drafts():
            self._clear_local_cell_draft_guard()
            return False
        if time.monotonic() >= self._admin_only_snapshot_until:
            self._clear_local_cell_draft_guard()
            return False
        return True

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer

from rem_card.app.logger import logger
from rem_card.ui.shared.async_call import AsyncCallThread


def show_balance_sync_status(layout, message):
    """Use existing headings so failure state does not change card geometry."""
    panel = getattr(layout, "sector_2b_g", None)
    header = getattr(panel, "header_lbl", None)
    if header is not None:
        header.setText("Баланс не обновлён" if message else "Введено:")
        header.setToolTip(message)
    editor = getattr(layout, "sector_2d", None)
    label = getattr(editor, "status_lbl", None)
    if label is not None:
        label.setText(message)


class BalanceSnapshotSync(QObject):
    """Coalesce authoritative balance reads without blocking the UI thread."""

    def __init__(
        self,
        parent: QObject,
        *,
        context_provider: Callable[[], Any],
        load_snapshot: Callable[[dict], dict],
        apply_snapshot: Callable[[dict, dict], None],
        overlay_sequence_provider: Callable[[], int] | None = None,
        role: str,
        delay_ms: int = 120,
        status_callback: Callable[[str], None] | None = None,
    ):
        super().__init__(parent)
        self._context_provider = context_provider
        self._load_snapshot = load_snapshot
        self._apply_snapshot = apply_snapshot
        self._overlay_sequence_provider = overlay_sequence_provider
        self._role = str(role or "unknown")
        self._delay_ms = max(0, int(delay_ms))
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._start_if_ready)
        self._recovery_timer = QTimer(self)
        self._recovery_timer.setSingleShot(True)
        self._recovery_timer.timeout.connect(self._retry_after_backoff)
        self._worker = None
        self._generation = 0
        self._pending = False
        self._required_change_id = 0
        self._retry_count = 0
        self._closing = False
        self._dirty = False
        self._active_request = None
        self._accepted_snapshot = {}
        self._status_callback = status_callback
        self.status_text = ""

    def report_error(self, message):
        self.status_text = str(message or "")
        if self._status_callback is not None:
            self._status_callback(self.status_text)

    BALANCE_FIELDS = ("effective_bounds", "fluids", "balance_runtime", "balance_calc")

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    @property
    def required_change_id(self) -> int:
        return int(self._required_change_id or 0)

    def schedule(self, required_change_id: int = 0) -> None:
        if self._closing:
            return
        try:
            required = max(0, int(required_change_id or 0))
        except (TypeError, ValueError):
            required = 0
        self._required_change_id = max(self._required_change_id, required)
        self._recovery_timer.stop()
        self._generation += 1
        self._dirty = True
        self._pending = True
        self._retry_count = 0
        self._timer.start(self._delay_ms)

    def ensure_current(self) -> None:
        if self._closing:
            return
        if self.is_dirty and self._worker is None and not self._timer.isActive():
            self._pending = True
            self._retry_count = 0
            self._recovery_timer.stop()
            self._timer.start(0)

    def _retry_after_backoff(self) -> None:
        # After three quick attempts, at most one recovery read per 30 seconds.
        if not self._closing and self.is_dirty and self._context_provider() is not None:
            self._pending = True
            self._timer.start(0)

    def reset(self) -> None:
        self._generation += 1
        self._pending = False
        self._required_change_id = 0
        self._retry_count = 0
        self._timer.stop()
        self._recovery_timer.stop()
        self._dirty = False
        self._accepted_snapshot = {}
        self.report_error("")

    def shutdown(self) -> None:
        self._closing = True
        self.reset()
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        self._disconnect_worker(worker)
        if worker.isRunning():
            worker.quit()

    def _start_if_ready(self) -> None:
        if self._closing or not self._pending:
            return
        if self._worker is not None:
            return
        context = self._context_provider()
        if context is None:
            return

        self._pending = False
        request = {
            "generation": int(self._generation),
            "context": context,
            "required_change_id": int(self._required_change_id or 0),
            "overlay_sequence": int(
                self._overlay_sequence_provider() if self._overlay_sequence_provider is not None else 0
            ),
        }
        self._active_request = request
        worker = AsyncCallThread(self._load_job, request)
        self._worker = worker
        worker.succeeded.connect(self._on_succeeded)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(self._on_finished)
        worker.start()

    def _load_job(self, request: dict) -> dict:
        return {**request, "snapshot": self._load_snapshot(request)}

    @staticmethod
    def _snapshot_change_id(snapshot: dict) -> int:
        try:
            return max(
                int(snapshot.get("change_id") or 0),
                int(snapshot.get("version") or 0),
                int(snapshot.get("last_change_id") or 0),
            )
        except (TypeError, ValueError):
            return 0

    def _request_is_current(self, request: dict) -> bool:
        return bool(
            request.get("generation") == self._generation
            and request.get("context") == self._context_provider()
        )

    def _on_succeeded(self, request: dict) -> None:
        if self._closing or not self._request_is_current(request):
            return
        snapshot = dict(request.get("snapshot") or {})
        if snapshot.get("balance_runtime") is None:
            self._on_failed(ValueError("Balance snapshot has no runtime"))
            return
        snapshot_change_id = self._snapshot_change_id(snapshot)
        required = max(
            int(request.get("required_change_id") or 0),
            int(self._required_change_id or 0),
            self._snapshot_change_id(self._accepted_snapshot),
        )
        if required > 0 and snapshot_change_id < required:
            self._retry_count += 1
            self._pending = self._retry_count <= 2
            logger.info(
                "[BalanceSync] stale snapshot discarded role=%s snapshot_change_id=%s required_change_id=%s retry=%s",
                self._role,
                snapshot_change_id,
                required,
                self._retry_count,
            )
            return

        # Clear before applying: calculation must use the accepted baseline.
        self._dirty = False
        self._recovery_timer.stop()
        self._accepted_snapshot = snapshot
        if required == 0 or snapshot_change_id >= required:
            self._required_change_id = 0
        try:
            self._apply_snapshot(snapshot, request)
        except Exception as exc:
            self._dirty = True
            self._on_failed(exc)
        else:
            self._retry_count = 0
            self.report_error("")

    def merge_card_snapshot(self, snapshot: dict) -> dict:
        """An older/full or vitals-only response cannot roll balance back."""
        accepted = self._accepted_snapshot
        if accepted and (
            snapshot.get("balance_runtime") is None
            or self._snapshot_change_id(snapshot) < self._snapshot_change_id(accepted)
            or (self.is_dirty and self._snapshot_change_id(snapshot) < self.required_change_id)
        ):
            snapshot = dict(snapshot)
            for key in self.BALANCE_FIELDS:
                if key in accepted:
                    snapshot[key] = accepted[key]
        elif snapshot.get("balance_runtime") is not None:
            self._accepted_snapshot = dict(snapshot)
        return snapshot

    def _on_failed(self, exc: Exception) -> None:
        if self._closing or not self._request_is_current(self._active_request or {}):
            return
        self._retry_count += 1
        self._dirty = True
        self.report_error(
            "Баланс не обновлён. Показаны последние загруженные данные; повторяем чтение."
            if self._accepted_snapshot else "Баланс не загружен. Повторяем чтение."
        )
        logger.warning(
            "[BalanceSync] snapshot load failed role=%s retry=%s error=%s",
            self._role,
            self._retry_count,
            exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        if self._retry_count <= 2:
            self._pending = True

    def _on_finished(self) -> None:
        worker = self.sender()
        if self._worker is worker:
            self._worker = None
        elif self._worker is not None:
            return
        if self._closing:
            return
        if not self._pending:
            if self.is_dirty and self._retry_count > 2:
                self._recovery_timer.start(30000)
            return
        retry_delay = min(1000, self._delay_ms * (2 ** max(0, self._retry_count)))
        self._timer.start(retry_delay)

    def _disconnect_worker(self, worker) -> None:
        for signal, slot in (
            (worker.succeeded, self._on_succeeded),
            (worker.failed, self._on_failed),
            (worker.finished, self._on_finished),
        ):
            try:
                signal.disconnect(slot)
            except Exception:
                pass

"""Responsive GUI-thread preparation for the doctor and nurse card layouts."""

from __future__ import annotations

import time
import weakref
from collections.abc import Callable, Iterable
from typing import Any

from PySide6.QtCore import QTimer
from shiboken6 import isValid

from rem_card.app.local_metrics import record_metric


class StagedUiPrewarm:
    """Run bounded Qt creation steps with an event-loop turn between them."""

    def __init__(
        self,
        owner,
        *,
        role: str,
        steps: Iterable[tuple[str, Callable[[], Any]]],
        stagger_ms: int,
        on_done: Callable[[], None] | None = None,
        on_failed: Callable[[BaseException], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
        metric_recorder: Callable[..., Any] = record_metric,
    ):
        self._owner_ref = weakref.ref(owner)
        self._role = str(role)
        self._steps = list(steps)
        self._stagger_ms = max(0, int(stagger_ms))
        self._on_done = on_done
        self._on_failed = on_failed
        self._on_cancel = on_cancel
        self._metric = metric_recorder
        self._index = 0
        self._generation = 0
        self._started_at: float | None = None
        self._work_ms = 0.0
        self._mode = "idle"
        self.active = False
        self.done = False
        self.cancelled = False
        self.failed = False
        self.error: BaseException | None = None

    @property
    def completed_steps(self) -> int:
        return self._index

    @property
    def total_steps(self) -> int:
        return len(self._steps)

    def start(self) -> bool:
        if self.active or self.done or self.cancelled or self.failed:
            return False
        self.active = True
        self._mode = "idle"
        self._started_at = time.perf_counter()
        self._generation += 1
        self._schedule(self._generation, 0)
        return True

    def finish_now(self, *, reason: str = "patient_open") -> bool:
        """Invalidate queued callbacks and synchronously finish required structure."""
        if self.done:
            return True
        if self.cancelled:
            return False
        if self.failed:
            if self.error is not None:
                raise self.error
            return False
        if self._started_at is None:
            self._started_at = time.perf_counter()
        self.active = True
        self._mode = str(reason or "immediate")
        self._generation += 1
        generation = self._generation
        while self._index < len(self._steps):
            index_before = self._index
            self._run_one(generation, schedule_next=False)
            if self.failed:
                if self.error is not None:
                    raise self.error
                return False
            if self.done:
                return True
            if (
                self.cancelled
                or not self.active
                or generation != self._generation
                or self._index == index_before
            ):
                return False
        return self.done

    def cancel(self, *, reason: str) -> bool:
        if self.done or self.cancelled:
            return False
        self._generation += 1
        self.active = False
        self.cancelled = True
        self._metric(
            "card_ui_prewarm_cancelled",
            1,
            role=self._role,
            reason=str(reason or "cancelled"),
            completed_steps=self._index,
            total_steps=len(self._steps),
        )
        if self._on_cancel is not None:
            self._on_cancel()
        return True

    def _owner(self):
        owner = self._owner_ref()
        if owner is None:
            return None
        try:
            if not isValid(owner):
                return None
        except Exception:
            pass
        if getattr(owner, "_is_closing", False):
            return None
        return owner

    def _schedule(self, generation: int, delay_ms: int) -> None:
        owner = self._owner()
        if owner is None:
            self.cancel(reason="owner_closed")
            return
        QTimer.singleShot(
            max(0, int(delay_ms)),
            owner,
            lambda expected=generation: self._run_one(expected, schedule_next=True),
        )

    def _run_one(self, generation: int, *, schedule_next: bool) -> None:
        if generation != self._generation or not self.active or self.done:
            return
        if self._owner() is None:
            self.cancel(reason="owner_closed")
            return
        if self._index >= len(self._steps):
            self._complete()
            return

        step_name, callback = self._steps[self._index]
        started = time.perf_counter()
        try:
            callback()
        except BaseException as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self._work_ms += elapsed_ms
            self._metric(
                "card_ui_prewarm_step_ms",
                round(elapsed_ms, 2),
                role=self._role,
                step=step_name,
                mode=self._mode,
                result="failed",
            )
            self._fail(exc, step_name)
            return

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self._work_ms += elapsed_ms
        if generation != self._generation or self.cancelled or not self.active:
            self._metric(
                "card_ui_prewarm_step_ms",
                round(elapsed_ms, 2),
                role=self._role,
                step=step_name,
                mode=self._mode,
                result="cancelled",
            )
            return
        self._metric(
            "card_ui_prewarm_step_ms",
            round(elapsed_ms, 2),
            role=self._role,
            step=step_name,
            mode=self._mode,
            result="ok",
        )
        self._index += 1
        if self._index >= len(self._steps):
            self._complete()
        elif schedule_next:
            self._schedule(generation, self._stagger_ms)

    def _complete(self) -> None:
        if self.done:
            return
        self.active = False
        self.done = True
        elapsed_ms = (
            (time.perf_counter() - self._started_at) * 1000.0
            if self._started_at is not None
            else self._work_ms
        )
        self._metric(
            "card_ui_prewarm_total_ms",
            round(elapsed_ms, 2),
            role=self._role,
            mode=self._mode,
            work_ms=round(self._work_ms, 2),
            steps=len(self._steps),
        )
        if self._on_done is not None:
            self._on_done()

    def _fail(self, exc: BaseException, step_name: str) -> None:
        self._generation += 1
        self.active = False
        self.failed = True
        self.error = exc
        self._metric(
            "card_ui_prewarm_failed",
            1,
            role=self._role,
            step=step_name,
            error_type=type(exc).__name__,
        )
        if self._on_cancel is not None:
            self._on_cancel()
        if self._on_failed is not None:
            self._on_failed(exc)


class StagedCardSectors:
    """Create the common full-card sectors over several Qt event-loop turns."""

    def __init__(self, role: str):
        normalized = str(role or "").strip().lower()
        if normalized not in {"doctor", "nurse"}:
            raise ValueError(f"Unsupported card role: {role}")
        self.role = normalized
        self.sectors: dict[str, Any] = {}
        self._created: list[Any] = []
        self._consumed = False

    def steps(self) -> list[tuple[str, Callable[[], None]]]:
        return [
            ("sectors_header", self._create_header),
            ("sectors_vitals", self._create_vitals),
            ("sectors_summary", self._create_summary),
            ("sectors_lower_primary", self._create_lower_primary),
            ("sectors_lower_secondary", self._create_lower_secondary),
        ]

    def _remember(self, name: str, widget: Any) -> Any:
        self.sectors[name] = widget
        if widget is not None:
            self._created.append(widget)
        return widget

    def _create_header(self) -> None:
        from rem_card.ui.rem_card_sectors.sector_1a import Sector1a
        from rem_card.ui.rem_card_sectors.sector_1b import Sector1b
        from rem_card.ui.rem_card_sectors.sector_4_sub import Sector4b, Sector4v
        from rem_card.ui.rem_card_sectors.sector_8 import Sector8

        self._remember("sector_8", Sector8()).setFixedHeight(38)
        self._remember("sector_1a", Sector1a()).setMinimumHeight(50)
        self.sectors["sector_1a"].setFixedWidth(250)
        self._remember("sector_1b", Sector1b()).setFixedWidth(250)
        self._remember("sector_4b", Sector4b()).setFixedHeight(56)
        self._remember("sector_4v", Sector4v()).setFixedHeight(42)

    def _create_vitals(self) -> None:
        from rem_card.ui.rem_card_sectors.sector_2a import Sector2a
        from rem_card.ui.rem_card_sectors.sector_2b import Sector2b
        from rem_card.ui.rem_card_sectors.sector_2g import Sector2g
        from rem_card.ui.rem_card_sectors.sector_2v import Sector2v

        self._remember("sector_2a", Sector2a()).setFixedHeight(30)
        self._remember("sector_2b", Sector2b()).setFixedHeight(37)
        self._remember("sector_2g", Sector2g()).setFixedWidth(140)
        self._remember("sector_2v", Sector2v()).setMinimumWidth(50)

    def _create_summary(self) -> None:
        from rem_card.ui.rem_card_sectors.sector_3a import Sector3a
        from rem_card.ui.rem_card_sectors.sector_3b import Sector3b
        from rem_card.ui.rem_card_sectors.sector_4a import Sector4a

        self._remember("sector_3a", Sector3a()).setFixedHeight(186)
        self._remember("sector_3b", Sector3b()).setFixedHeight(204)
        self._remember("sector_4a", Sector4a()).setFixedHeight(65)

    def _create_lower_primary(self) -> None:
        from rem_card.ui.rem_card_sectors.sector_5 import Sector5
        from rem_card.ui.rem_card_sectors.sector_6 import Sector6
        from rem_card.ui.rem_card_sectors.sector_7na_b import Sector7na_b

        self._remember("sector_5", Sector5()).setMinimumWidth(50)
        self._remember("sector_6", Sector6()).setMinimumWidth(50)
        self._remember("sector_7na_b", Sector7na_b()).setMinimumHeight(120)

    def _create_lower_secondary(self) -> None:
        from rem_card.ui.rem_card_sectors.sector_7bal_a import Sector7bal_a
        from rem_card.ui.rem_card_sectors.sector_7bal_b import Sector7bal_b
        from rem_card.ui.rem_card_sectors.sector_7vit_a import Sector7vit_a
        from rem_card.ui.rem_card_sectors.sector_7vit_b import Sector7vit_b

        self._remember("sector_7vit_a", Sector7vit_a())
        self._remember("sector_7vit_b", Sector7vit_b(role=self.role))
        self._remember("sector_7bal_a", Sector7bal_a())
        self._remember("sector_7bal_b", Sector7bal_b())
        self.sectors.update(
            sector_7=None,
            sector_7na_a=None,
            sector_2b_g=None,
            sector_2b_v=None,
            sector_2d=None,
            balance_grid=None,
            sector_ivl=None,
            sector_proc=None,
            sector_anal=None,
            sector_print=None,
            sector_w1b=None,
            sector_w1b_nurse=None,
        )

    def build_layout(self, callback: Callable[[dict], Any]) -> Any:
        """Transfer this preparation explicitly; never mutate a global factory."""
        if self._consumed:
            raise RuntimeError("Prepared card sectors were already consumed")
        try:
            result = callback(dict(self.sectors))
        except BaseException:
            self.dispose()
            raise
        self._consumed = True
        self._created.clear()
        self.sectors.clear()
        return result

    def dispose(self) -> None:
        if self._consumed:
            return
        for widget in self._created:
            try:
                if isValid(widget):
                    widget.deleteLater()
            except Exception:
                pass
        self._created.clear()
        self.sectors.clear()

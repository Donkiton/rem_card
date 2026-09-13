"""Native-Qt recovery smoke used only by the isolated compiled test bundle."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from rem_card.app.isolated_test_runtime import PURPOSE, sandbox_paths, test_share_name


SMOKE_SCHEMA_VERSION = 1
SMOKE_PURPOSE = "RemCard isolated emergency recovery UI smoke"
SMOKE_TIMEOUT_MS = 180_000
SMOKE_POLL_MS = 100


def _resolved_environment_path(name: str) -> Path:
    value = str(os.environ.get(name, "") or "").strip()
    if not value:
        raise RuntimeError(f"Не задан изолированный путь {name}.")
    return Path(value).resolve()


def _validated_smoke_root(root: str | Path) -> Path:
    """Fail closed unless every writable runtime path belongs to a marked bundle."""
    bundle = Path(root).resolve()
    if str(bundle).startswith("\\\\"):
        raise RuntimeError("Проверка интерфейса разрешена только в локальной тестовой сборке.")
    try:
        marker = json.loads((bundle / "TEST_SANDBOX.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("Не найден корректный маркер изолированной тестовой сборки.") from exc
    if marker.get("schema_version") != 1 or marker.get("purpose") != PURPOSE:
        raise RuntimeError("Проверка интерфейса отклонена: неверный маркер тестовой сборки.")
    paths = sandbox_paths(bundle)

    namespace = _resolved_environment_path("REMCARD_TEST_INSTANCE_NAMESPACE")
    emergency = _resolved_environment_path("REMCARD_EMERGENCY_DB_ROOT")
    config = _resolved_environment_path("REMCARD_DATA_PATH_CONFIG")
    if namespace != bundle:
        raise RuntimeError("Пространство тестового процесса не совпадает с папкой сборки.")
    if not emergency.is_relative_to(bundle) or not config.is_relative_to(bundle):
        raise RuntimeError("Пути тестового процесса выходят за пределы сборки.")
    if Path(paths["State"]).resolve() != (bundle / "State").resolve():
        raise RuntimeError("Папка отчёта перенаправлена за пределы тестовой сборки.")
    expected_share = "\\\\localhost\\" + test_share_name(bundle)
    if os.path.normcase(os.environ.get("REMCARD_BAZA_DIR", "").rstrip("\\")) != os.path.normcase(expected_share):
        raise RuntimeError("Проверка интерфейса отказалась использовать нетестовую базу.")
    try:
        configured_share = json.loads(config.read_text(encoding="utf-8-sig")).get("baza_dir", "")
    except (OSError, ValueError, AttributeError) as exc:
        raise RuntimeError("Не удалось проверить конфигурацию тестовой базы.") from exc
    if os.path.normcase(str(configured_share).rstrip("\\")) != os.path.normcase(expected_share):
        raise RuntimeError("Конфигурация тестового процесса указывает на другую базу.")
    return bundle


class _SmokeReport:
    def __init__(self, root: Path, mode: str):
        self.path = root / "State" / f"emergency-smoke-{mode}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.sequence = 0
        self.payload: dict[str, Any] = {
            "schema_version": SMOKE_SCHEMA_VERSION,
            "purpose": SMOKE_PURPOSE,
            "mode": mode,
            "status": "running",
            "stage": "installed",
        }
        self.write()

    def update(self, stage: str, **details: Any) -> None:
        self.sequence += 1
        self.payload.update(details)
        self.payload.update({"stage": str(stage), "sequence": self.sequence})
        self.write()

    def write(self) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)


class _EmergencyRecoverySmoke(QObject):
    def __init__(self, app: QApplication, *, mode: str, root: Path):
        super().__init__(app)
        self.app = app
        self.mode = mode
        self.root = root
        self.report = _SmokeReport(root, mode)
        self.stage = "await_window"
        self.window = None
        self.review = None
        self.finished = False

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(SMOKE_POLL_MS)
        self.poll_timer.timeout.connect(self._tick)
        self.timeout_timer = QTimer(self)
        self.timeout_timer.setSingleShot(True)
        self.timeout_timer.setInterval(SMOKE_TIMEOUT_MS)
        self.timeout_timer.timeout.connect(self._timed_out)
        self.poll_timer.start()
        self.timeout_timer.start()
        self.report.update("await_window")

    def _stop(self) -> None:
        self.finished = True
        self.poll_timer.stop()
        self.timeout_timer.stop()

    def _fail(self, message: str) -> None:
        if self.finished:
            return
        self.report.update("failed", status="failed", error=str(message))
        self._stop()

    def _timed_out(self) -> None:
        self._fail(f"Таймаут {SMOKE_TIMEOUT_MS // 1000} с на этапе {self.stage}.")

    def _tick(self) -> None:
        if self.finished:
            return
        try:
            if self.window is None:
                self.window = self._find_main_window()
                if self.window is None:
                    return
                runtime = getattr(getattr(self.window, "container", None), "runtime_context", None)
                if str(getattr(runtime, "mode", "") or "") != "emergency":
                    raise AssertionError("Главное окно запущено не в аварийном режиме.")
                self.stage = "await_initial_review" if self.mode == "owner" else "peer_ready"
                self.report.update("window_ready", runtime_mode="emergency")
            if self.mode == "peer":
                self._advance_peer()
            else:
                self._advance_owner()
        except Exception as exc:
            self._fail(f"{type(exc).__name__}: {exc}")

    def _find_main_window(self):
        candidates = [
            widget for widget in self.app.topLevelWidgets()
            if hasattr(widget, "_emergency_workflow") and getattr(widget, "_emergency_workflow", None) is not None
        ]
        visible = [widget for widget in candidates if widget.isVisible()]
        return visible[0] if visible else (candidates[0] if candidates else None)

    def _visible_review(self):
        for widget in self.app.topLevelWidgets():
            if (
                widget.isVisible()
                and widget.objectName() == "EmergencyReviewDialog"
                and hasattr(widget, "accept_button")
                and hasattr(widget, "cancel_button")
            ):
                return widget
        return None

    def _controller(self):
        controller = getattr(self.window, "_emergency_workflow", None)
        if controller is None:
            raise AssertionError("В главном окне отсутствует аварийный workflow.")
        return controller

    @staticmethod
    def _metadata_status(controller) -> str:
        metadata = controller.store.read_active_session(controller.session_id)
        return str(getattr(metadata, "status", "") or "")

    def _advance_peer(self) -> None:
        controller = self._controller()
        notice = getattr(controller, "recovery_notice", None)
        dismissed = False
        if notice is not None and notice.isVisible():
            button = getattr(notice, "stay_button", None)
            if button is None or not button.isEnabled():
                raise AssertionError("В уведомлении о восстановлении сети нет доступной кнопки «Остаться».")
            button.click()
            dismissed = True
        self.report.update(
            "peer_ready",
            status="ready",
            runtime_mode="emergency",
            recovery_notice_dismissed=dismissed,
        )
        self._stop()

    def _advance_owner(self) -> None:
        handlers = {
            'await_initial_review': self._step_initial_review,
            'await_resume': self._step_resume,
            'await_active': self._step_active,
            'await_peer': self._step_peer,
            'await_emergency_button': self._step_emergency_button,
            'await_preflight': self._step_preflight,
            'await_probe_ready': self._step_probe_ready,
            'await_second_review': self._step_second_review,
            'await_review_expanded': self._step_review_expanded,
        }
        handlers[self.stage]()

    def _step_initial_review(self) -> None:
        dialog = self._visible_review()
        if dialog is None:
            return
        if not dialog.cancel_button.isVisible() or not dialog.cancel_button.isEnabled():
            raise AssertionError("Первый просмотр нельзя отменить штатной кнопкой.")
        self.stage = "await_resume"
        self.report.update("initial_review_cancelled")
        dialog.cancel_button.click()
        return

    def _step_resume(self) -> None:
        controller = self._controller()
        waiting = getattr(controller, "waiting", None)
        if waiting is None or self._visible_review() is not None:
            return
        button = getattr(waiting, "resume_button", None)
        if button is None or not button.isVisible() or not button.isEnabled():
            return
        if controller.pause_token is None:
            raise AssertionError("Первый просмотр завершён без сохранённого pause token.")
        self.stage = "await_active"
        self.report.update("resume_clicked")
        button.click()
        return

    def _step_active(self) -> None:
        controller = self._controller()
        if controller.pause_token is not None or controller.waiting is not None:
            return
        if self._metadata_status(controller) != "active":
            return
        self.stage = "await_peer"
        self.report.update("active_ready", runtime_mode="emergency", metadata_status="active")
        return

    def _step_peer(self) -> None:
        participant = getattr(self.window.container, "emergency_participant", None)
        if participant is None or participant.peers_ready():
            return
        self.stage = "await_emergency_button"
        self.report.update("peer_registered")
        return

    def _step_emergency_button(self) -> None:
        controller = self._controller()
        notice = getattr(controller, "recovery_notice", None)
        if notice is not None and notice.isVisible():
            button = getattr(notice, "stay_button", None)
            if button is None or not button.isEnabled():
                raise AssertionError("Уведомление о сети нельзя отложить штатной кнопкой.")
            self.report.update("owner_recovery_notice_dismissed")
            button.click()
            return
        button = self._visible_emergency_button()
        if button is None:
            return
        if controller.pause_token is not None or self._metadata_status(controller) != "active":
            raise AssertionError("Локальная работа была приостановлена до открытия preflight.")
        self.stage = "await_preflight"
        self.report.update("emergency_button_clicked")
        button.click()
        return

    def _step_preflight(self) -> None:
        controller = self._controller()
        dialog = getattr(controller, "preflight", None)
        if dialog is None or not dialog.isVisible():
            return
        if controller.pause_token is not None or self._metadata_status(controller) != "active":
            raise AssertionError("Preflight открылся после преждевременной приостановки работы.")
        dialog.password_edit.setText("test-2026")
        if dialog.password_edit.text() != "test-2026":
            raise AssertionError("Аварийный пароль не введён в штатное поле.")
        self.stage = "await_probe_ready"
        self.report.update(
            "password_entered_before_pause",
            pause_token_is_none=True,
            metadata_status="active",
        )
        return

    def _step_probe_ready(self) -> None:
        controller = self._controller()
        dialog = getattr(controller, "preflight", None)
        if dialog is None:
            raise AssertionError("Preflight закрылся до подтверждения.")
        if not dialog.probe_ready or not dialog.confirm_button.isEnabled():
            return
        if controller.pause_token is not None or self._metadata_status(controller) != "active":
            raise AssertionError("Локальная работа приостановлена до подтверждения preflight.")
        if dialog.password_edit.text() != "test-2026":
            raise AssertionError("Поле аварийного пароля изменилось до подтверждения.")
        self.stage = "await_second_review"
        self.report.update(
            "probe_ready_before_pause",
            probe_ready=True,
            confirm_enabled=True,
            pause_token_is_none=True,
            metadata_status="active",
        )
        dialog.confirm_button.click()
        return

    def _step_second_review(self) -> None:
        dialog = self._visible_review()
        if dialog is None:
            return
        participant = getattr(self.window.container, "emergency_participant", None)
        if participant is None or not participant.peers_ready():
            raise AssertionError("Просмотр открылся до штатного закрытия второго аварийного окна.")
        peer = self._read_peer_report()
        if peer.get("stage") != "peer_ready":
            raise AssertionError("Второе окно не подтвердило готовность до просмотра переноса.")
        review = dict(dialog.review)
        summary = dict(review.get("summary") or {})
        patients = list(review.get("patients") or [])
        operation_count = int(summary.get("operation_count") or 0)
        if summary.get("patient_count") != 1 or operation_count <= 0:
            raise AssertionError(f"Неожиданный объём просмотра: {summary}.")
        if review.get("blockers") or len(patients) != 1:
            raise AssertionError("Синтетический просмотр содержит блокировки или неверное число пациентов.")
        patient = dict(patients[0])
        title = str(patient.get("title") or "")
        if "Тестов Пациент Учебный" not in title or "ТЕСТ-001" not in title:
            raise AssertionError("Просмотр содержит данные, не похожие на разрешённую синтетическую фикстуру.")
        section_titles = {str(section.get("title") or "") for section in patient.get("sections") or []}
        required_sections = {"Показатели состояния", "Назначения и выполнения"}
        if not required_sections.issubset(section_titles):
            raise AssertionError(f"В просмотре отсутствуют клинические разделы: {section_titles}.")
        if len(dialog._blocks) != 1:
            raise AssertionError("В диалоге просмотра должно быть ровно одно карточное представление.")
        self.review = dialog
        self.stage = "await_review_expanded"
        self.report.update(
            "second_review_opened",
            patient_count=1,
            operation_count=operation_count,
        )
        dialog._blocks[0].toggle_button.click()
        return

    def _step_review_expanded(self) -> None:
        dialog = self.review
        if dialog is None or not dialog.isVisible():
            raise AssertionError("Второй просмотр закрылся до выбора пациента.")
        block = dialog._blocks[0]
        if not block.toggle_button.isChecked() or not block.details_widget.isVisible():
            return
        section_labels = [
            label.text() for label in block.findChildren(QLabel)
            if label.objectName() == "EmergencyReviewSectionTitle" and label.isVisible()
        ]
        item_labels = [
            label.text() for label in block.findChildren(QLabel)
            if label.objectName() == "EmergencyReviewItem" and label.isVisible()
        ]
        operation_count = int(dict(dialog.review.get("summary") or {}).get("operation_count") or 0)
        if not section_labels or len(item_labels) != operation_count:
            raise AssertionError("Развёрнутый просмотр не показал все синтетические изменения.")
        block.accept_checkbox.click()
        if not block.accept_checkbox.isChecked() or not dialog.accept_button.isEnabled():
            raise AssertionError("Пациент не выбран штатным флажком.")
        self.report.update(
            "authorized_final",
            status="authorized",
            expected_mode="network",
            selected_admission_count=1,
            review_sections=section_labels,
            review_items=item_labels,
        )
        self._stop()
        dialog.accept_button.click()


    def _visible_emergency_button(self):
        for button in self.window.findChildren(QPushButton):
            if button.objectName() == "emergencyModeButton" and button.isVisible() and button.isEnabled():
                return button
        return None

    def _read_peer_report(self) -> dict[str, Any]:
        path = self.root / "State" / "emergency-smoke-peer.json"
        try:
            return dict(json.loads(path.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError, TypeError):
            return {}


def install_emergency_recovery_smoke(
    app: QApplication,
    *,
    mode: str,
    root: str | Path,
) -> QObject:
    """Install the deterministic native-Qt smoke controller after QApplication exists."""
    if mode not in {"owner", "peer"}:
        raise ValueError("mode must be 'owner' or 'peer'")
    if not isinstance(app, QApplication):
        raise TypeError("app must be QApplication")
    bundle = _validated_smoke_root(root)
    return _EmergencyRecoverySmoke(app, mode=mode, root=bundle)

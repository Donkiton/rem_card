import argparse
import ctypes
import json
import os
import socket
import sys
import time
from functools import partial
from typing import Callable, Optional

if __package__ in (None, ""):
    _repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    from _local_rem_card_bootstrap import bootstrap_local_rem_card

    bootstrap_local_rem_card()

from rem_card.app.runtime_paths import (
    DataPathConfigurationError,
    cleanup_old_local_logs,
    configure_operblock_runtime_path,
    create_baza_structure_and_db,
    get_local_logs_dir,
    is_compiled,
    write_configured_baza_dir,
)
from rem_card.app.roles import (
    ROLE_KEYS,
    ROLE_OPERBLOCK,
    ROLE_OPERBLOCK_EMERGENCY,
    ROLE_OPERBLOCK_PLANNED,
    is_operblock_role,
    role_display_name,
)
from rem_card.app.version import APP_DISPLAY_TITLE, APP_VERSION


STARTUP_TRACE_ENV = "REMCARD_STARTUP_TRACE"
STARTUP_W1_WAIT_MS_ENV = "REMCARD_STARTUP_W1_WAIT_MS"
STARTUP_W1_WAIT_DEFAULT_MS = 300
FULL_RUNTIME_THEME_ENV = "REMCARD_FULL_RUNTIME_THEME"
STARTUP_GUARD_QUICKCHECK_ENV = "REMCARD_STARTUP_GUARD_QUICKCHECK_OK"
EMERGENCY_STARTUP_ENTER_PROGRAM_TEXT = "Войти в программу"
EMERGENCY_STARTUP_PASSWORD_TEXT = "Ввести аварийный пароль"
EMERGENCY_STARTUP_SWITCH_TO_NETWORK_TEXT = "Перейти на основную БД"
EMERGENCY_STARTUP_CANCEL_TEXT = "Отмена"

ACTIVE_EMERGENCY_SESSION_NETWORK_AVAILABLE_MESSAGE = (
    "На этом ПК есть активная аварийная сессия RemCard.\n\n"
    "Сетевая база доступна, но обычный запуск заблокирован до решения по аварийной сессии.\n\n"
    "Можно войти в программу на локальной аварийной базе или после ввода аварийного пароля "
    "перейти на основную БД с объединением аварийных изменений."
)


def _show_native_warning(title: str, message: str):
    try:
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)
    except Exception:
        print(f"{title}: {message}")


def _env_flag_enabled(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _startup_w1_wait_ms() -> int:
    raw = str(os.environ.get(STARTUP_W1_WAIT_MS_ENV, STARTUP_W1_WAIT_DEFAULT_MS)).strip()
    try:
        value = int(float(raw))
    except Exception:
        value = STARTUP_W1_WAIT_DEFAULT_MS
    return max(0, min(value, 3000))


def _startup_trace(logger, started_at: float, phase: str, **fields):
    if not logger or not _env_flag_enabled(STARTUP_TRACE_ENV):
        return
    elapsed_ms = (time.perf_counter() - started_at) * 1000.0
    details = " ".join(f"{key}={value}" for key, value in fields.items())
    suffix = f" {details}" if details else ""
    logger.info("[StartupTrace] phase=%s t_ms=%.1f%s", phase, elapsed_ms, suffix)


def _opblock_startup_metrics_reset(role: Optional[str], started_at: float) -> None:
    if not is_operblock_role(role):
        return
    from rem_card.app import operblock_startup_metrics

    operblock_startup_metrics.reset(started_at=started_at)


def _opblock_startup_timer_start(role: Optional[str]) -> float | None:
    if not is_operblock_role(role):
        return None
    from rem_card.app import operblock_startup_metrics

    return operblock_startup_metrics.timer_start()


def _opblock_startup_record_duration(role: Optional[str], name: str, elapsed_ms: float, *, source: str) -> None:
    if not is_operblock_role(role):
        return
    from rem_card.app import operblock_startup_metrics

    operblock_startup_metrics.record_duration(name, elapsed_ms, source=source)


def _opblock_startup_record_since(role: Optional[str], name: str, started_at: float | None, *, source: str) -> None:
    if not is_operblock_role(role):
        return
    from rem_card.app import operblock_startup_metrics

    operblock_startup_metrics.record_since(name, started_at or 0.0, source=source)


def _opblock_startup_record_window_shown(role: Optional[str], initial_role_prepared: bool, QTimer) -> None:
    if not is_operblock_role(role):
        return
    from rem_card.app import operblock_startup_metrics

    operblock_startup_metrics.record_elapsed("time_to_window_visible_ms", source="app_main")
    if not initial_role_prepared:
        return

    def _record_operblock_board_ready_after_paint():
        operblock_startup_metrics.record_elapsed("board_ready_after_paint_ms", source="app_main")
        operblock_startup_metrics.record_elapsed("time_to_operblock_board_ready_ms", source="app_main")

    QTimer.singleShot(0, _record_operblock_board_ready_after_paint)


def _install_startup_trace_hooks(container, logger, started_at: float):
    if not _env_flag_enabled(STARTUP_TRACE_ENV):
        return
    service = getattr(container, "remcard_service", None)
    if service is None:
        return

    def _wrap(name: str):
        original = getattr(service, name, None)
        if not callable(original) or getattr(original, "_startup_trace_wrapped", False):
            return

        def wrapped(*args, **kwargs):
            load_started = time.perf_counter()
            result = None
            error = None
            try:
                result = original(*args, **kwargs)
                return result
            except Exception as exc:
                error = exc
                raise
            finally:
                fields = {
                    "loader": name,
                    "elapsed_ms": f"{(time.perf_counter() - load_started) * 1000.0:.1f}",
                }
                if isinstance(result, dict):
                    if "patients" in result:
                        fields["patients"] = len(result.get("patients") or [])
                    if "rows" in result:
                        fields["rows"] = len(result.get("rows") or [])
                    if result.get("content_hash"):
                        fields["content_hash"] = str(result.get("content_hash"))[:12]
                if error is not None:
                    fields["error"] = repr(error)
                _startup_trace(logger, started_at, "loader_done", **fields)

        setattr(wrapped, "_startup_trace_wrapped", True)
        setattr(service, name, wrapped)

    _wrap("build_beds_snapshot")
    _wrap("build_w1a_upcoming_orders_snapshot")


def _log_startup_phase_metrics(
    container,
    logger,
    started_at: float,
    *,
    theme_ui_init_ms: float,
    total_bootstrap_ms: float,
):
    metrics = dict(getattr(getattr(container, "db_manager", None), "startup_metrics", {}) or {})
    metrics["theme_ui_init_ms"] = round(float(theme_ui_init_ms), 3)
    metrics["total_bootstrap_ms"] = round(float(total_bootstrap_ms), 3)
    _startup_trace(
        logger,
        started_at,
        "startup_phases",
        **{key: f"{float(value):.1f}" for key, value in sorted(metrics.items())},
    )


def _initial_role_layout(window):
    role = getattr(window, "_initial_role", None)
    role_widget = None
    if role == "doctor":
        role_widget = getattr(window, "doctor_main", None)
    elif role == "nurse":
        role_widget = getattr(window, "nurse_main", None)
    elif is_operblock_role(role):
        role_widget = getattr(window, "operblock_main", None)
    if role_widget is None:
        return None
    layout = getattr(role_widget, "layout_manager", None)
    if layout is not None:
        return layout
    remcard_widget = getattr(role_widget, "remcard_widget", None)
    return getattr(remcard_widget, "layout_manager", None)


def _initial_w1_state(window) -> dict:
    if is_operblock_role(getattr(window, "_initial_role", None)):
        return {"ready": True, "role_ui": True, "operblock": True}

    layout = _initial_role_layout(window)
    if layout is None:
        return {"ready": False, "role_ui": False}

    beds_widget = getattr(layout, "beds_selection_widget", None)
    beds_apply_count = int(getattr(beds_widget, "_refresh_apply_count", 0) or 0) if beds_widget is not None else 0
    beds_ready = beds_widget is None or beds_apply_count > 0
    beds_rows = len(getattr(beds_widget, "_rows_by_admission_id", {}) or {}) if beds_widget is not None else 0

    w1a = getattr(layout, "sector_w1a", None)
    w1a_enabled = bool(getattr(w1a, "_display_enabled", True)) if w1a is not None else False
    w1a_apply_count = int(getattr(w1a, "_refresh_apply_count", 0) or 0) if w1a is not None else 0
    w1a_ready = w1a is None or not w1a_enabled or w1a_apply_count > 0
    w1a_rows = len(getattr(w1a, "_all_data", []) or []) if w1a is not None else 0

    return {
        "ready": bool(beds_ready and w1a_ready),
        "role_ui": True,
        "beds_ready": bool(beds_ready),
        "beds_apply_count": beds_apply_count,
        "beds_rows": beds_rows,
        "w1a_ready": bool(w1a_ready),
        "w1a_enabled": bool(w1a_enabled),
        "w1a_apply_count": w1a_apply_count,
        "w1a_rows": w1a_rows,
    }


def _wait_for_initial_w1(app, window, logger, started_at: float, timeout_ms: int) -> dict:
    if timeout_ms <= 0:
        state = _initial_w1_state(window)
        _startup_trace(logger, started_at, "initial_w1_wait_skipped", **state)
        return state

    wait_started = time.perf_counter()
    deadline = wait_started + (timeout_ms / 1000.0)
    state = _initial_w1_state(window)
    _startup_trace(logger, started_at, "initial_w1_wait_start", timeout_ms=timeout_ms, **state)
    while time.perf_counter() < deadline:
        try:
            app.processEvents()
        except Exception:
            break
        state = _initial_w1_state(window)
        if state.get("ready"):
            break
        time.sleep(0.005)

    state = _initial_w1_state(window)
    state["waited_ms"] = f"{(time.perf_counter() - wait_started) * 1000.0:.1f}"
    _startup_trace(logger, started_at, "initial_w1_wait_done", timeout_ms=timeout_ms, **state)
    return state


def _apply_basic_app_theme(app):
    """Cheap baseline styling for startup without global QSS repolish costs."""
    if app is None:
        return
    _install_no_button_focus_rect_style(app)
    try:
        from PySide6.QtGui import QColor, QFont, QPalette
        from rem_card.ui.styles.tooltip_style import apply_tooltip_palette

        app.setFont(QFont("Segoe UI", 10))
        palette = app.palette()
        colors = {
            QPalette.ColorRole.Window: "#f8f9fa",
            QPalette.ColorRole.WindowText: "#2c3e50",
            QPalette.ColorRole.Base: "#ffffff",
            QPalette.ColorRole.AlternateBase: "#fdfdfd",
            QPalette.ColorRole.Text: "#2c3e50",
            QPalette.ColorRole.Button: "#e9ecef",
            QPalette.ColorRole.ButtonText: "#2c3e50",
            QPalette.ColorRole.ToolTipBase: "#ffffff",
            QPalette.ColorRole.ToolTipText: "#000000",
        }
        for group in (
            QPalette.ColorGroup.Active,
            QPalette.ColorGroup.Inactive,
            QPalette.ColorGroup.Disabled,
        ):
            for role, color in colors.items():
                palette.setColor(group, role, QColor(color))
        app.setPalette(palette)
        apply_tooltip_palette(app)
    except Exception:
        pass
    try:
        from rem_card.ui.styles.context_menu_style import install_global_text_edit_context_menus

        install_global_text_edit_context_menus(app)
    except Exception:
        pass


def _install_no_button_focus_rect_style(app):
    try:
        from rem_card.ui.styles.focus_rect_style import install_no_button_focus_rect_style

        install_no_button_focus_rect_style(app)
    except Exception:
        pass


def _apply_app_theme(app, role: Optional[str] = None):
    _install_no_button_focus_rect_style(app)
    if not _env_flag_enabled(FULL_RUNTIME_THEME_ENV):
        _apply_basic_app_theme(app)
        return
    try:
        from rem_card.ui.styles.theme_manager import get_theme_manager

        manager = get_theme_manager()
        manager.load(role or "system")
        manager.apply_to_app(app, role or "system")
    except Exception:
        try:
            from rem_card.ui.styles.theme import GLOBAL_STYLE
            from rem_card.ui.styles.context_menu_style import install_global_text_edit_context_menus
            from rem_card.ui.styles.tooltip_style import apply_tooltip_palette

            app.setStyleSheet(GLOBAL_STYLE)
            apply_tooltip_palette(app)
            install_global_text_edit_context_menus(app)
        except Exception:
            pass


def _write_startup_local_log(message: str):
    try:
        log_dir = get_local_logs_dir()
        os.makedirs(log_dir, exist_ok=True)
        cleanup_old_local_logs(log_dir)
        path = os.path.join(log_dir, "startup.log")
        from datetime import datetime

        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')} | {message}\n")
    except Exception:
        pass


def _sync_release_settings_if_needed() -> None:
    if not is_compiled():
        return
    try:
        from rem_card.app.runtime_paths import sync_external_settings_from_bundle

        copied = sync_external_settings_from_bundle()
        if copied:
            _write_startup_local_log(f"release settings synced: files={copied}")
    except Exception as exc:
        _write_startup_local_log(f"release settings sync failed: {exc}")


def _show_custom_warning(title: str, message: str):
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication(sys.argv)
        _apply_app_theme(app, "system")
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        CustomMessageBox.warning(None, title, message)
    except Exception:
        _show_native_warning(title, message)


def _show_startup_warning_without_settings(title: str, message: str):
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        QApplication.instance() or QApplication(sys.argv)
        QMessageBox.warning(None, title, message)
    except Exception:
        _show_native_warning(title, message)


def _show_startup_action_without_settings(
    title: str,
    message: str,
    actions: list[tuple[str, int]],
    *,
    default_code: int = 0,
) -> int:
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        QApplication.instance() or QApplication(sys.argv)
        box = QMessageBox()
        box.setWindowTitle(title)
        box.setText(message)
        button_codes = {}
        for index, (button_text, result_code) in enumerate(actions):
            role = QMessageBox.AcceptRole if index == 0 else QMessageBox.RejectRole
            button = box.addButton(str(button_text), role)
            button_codes[button] = int(result_code)
        box.exec()
        return int(button_codes.get(box.clickedButton(), default_code))
    except Exception:
        _show_native_warning(title, message)
        return int(default_code)


def _show_custom_info(title: str, message: str):
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication(sys.argv)
        _apply_app_theme(app, "system")
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        CustomMessageBox.information(None, title, message)
    except Exception:
        print(f"{title}: {message}")


def _active_update_lock_payload_for_startup() -> Optional[dict]:
    try:
        from rem_card.app.update_launcher import get_active_update_lock_payload

        return get_active_update_lock_payload()
    except Exception:
        return None


def _describe_startup_update_lock(payload: dict) -> str:
    host = payload.get("host") or "неизвестно"
    started_at = payload.get("started_at") or ""
    version = payload.get("target_version") or ""
    details = []
    if version:
        details.append(f"версия {version}")
    details.append(f"компьютер: {host}")
    if started_at:
        details.append(f"начато: {started_at}")
    return "Обновление программы уже выполняется.\n\n" + "\n".join(details)


def _show_update_in_progress_if_needed() -> bool:
    try:
        payload = _active_update_lock_payload_for_startup()
        if not payload:
            return False
        _show_startup_warning_without_settings("Обновление программы", _describe_startup_update_lock(payload))
        return True
    except Exception:
        return False


def _launch_exit_update_if_needed() -> bool:
    if not is_compiled():
        return False
    try:
        from rem_card.app.update_checker import find_best_update
        from rem_card.app.update_launcher import is_update_in_progress, launch_update

        if is_update_in_progress():
            return False
        candidate = find_best_update()
        if not candidate:
            return False
        _write_startup_local_log(
            "exit update found: "
            f"current={APP_VERSION}; target={candidate.version}; source={candidate.prog_dir}"
        )
        launched = launch_update(candidate, restart_exe=None, wait_for_parent=True)
        _write_startup_local_log(
            f"exit update launch {'ok' if launched else 'failed'}: "
            f"target={candidate.version}; source={candidate.prog_dir}"
        )
        return launched
    except Exception as exc:
        _write_startup_local_log(f"exit update check failed: {exc}")
        return False


def _startup_block_requires_update(message: str, technical_reason: str = "") -> bool:
    text = f"{message}\n{technical_reason}".lower()
    markers = (
        "обновите программу",
        "версия программы устарела",
        "требуется обнов",
        "профиль доступа к базе не соответствует требованиям",
    )
    return any(marker in text for marker in markers)


def _find_startup_update_candidate():
    if not is_compiled():
        return None
    try:
        from rem_card.app.update_checker import find_best_update
        from rem_card.app.update_launcher import is_update_in_progress

        if is_update_in_progress():
            _write_startup_local_log("startup update required, but update is already in progress")
            return None

        candidate = find_best_update()
        if candidate:
            _write_startup_local_log(
                "startup update found: "
                f"current={APP_VERSION}; target={candidate.version}; source={candidate.prog_dir}"
            )
        else:
            _write_startup_local_log(
                f"startup update required, but no update package found: current={APP_VERSION}"
            )
        return candidate
    except Exception as exc:
        _write_startup_local_log(f"startup update check failed: {exc}")
        return None


def _launch_startup_update(candidate, *, role: Optional[str], reason: str) -> bool:
    if not candidate or not is_compiled():
        return False
    try:
        from rem_card.app.update_launcher import launch_update

        launched = launch_update(candidate, restart_exe=None, wait_for_parent=True)
        _write_startup_local_log(
            f"startup update launch {'ok' if launched else 'failed'}: "
            f"role={role}; current={APP_VERSION}; target={candidate.version}; "
            f"source={candidate.prog_dir}; reason={reason}"
        )
        if not launched:
            _show_custom_warning(
                "Обновление программы",
                "Не удалось запустить обновление автоматически.\n\n"
                "Закройте это окно и повторите запуск программы через минуту. "
                "Если сообщение повторяется, сообщите ответственному.",
            )
        return launched
    except Exception as exc:
        _write_startup_local_log(f"startup update launch crashed: {exc}")
        _show_custom_warning(
            "Обновление программы",
            "Не удалось запустить обновление автоматически.\n\n"
            "Повторите запуск программы через минуту. Если сообщение повторяется, сообщите ответственному.",
        )
        return False


def _run_path_setup():
    os.environ["REMCARD_PATH_SETUP_MODE"] = "1"

    from PySide6.QtWidgets import QApplication, QFileDialog

    app = QApplication.instance() or QApplication(sys.argv)
    _apply_app_theme(app, "system")

    while True:
        selected = QFileDialog.getExistingDirectory(
            None,
            "Выберите папку базы RemCard",
            os.path.expanduser("~"),
            QFileDialog.Option.ShowDirsOnly,
        )
        if not selected:
            return 0

        ok, message = create_baza_structure_and_db(selected)
        if not ok:
            _show_custom_warning("Путь не сохранен", message)
            continue

        try:
            config_path = write_configured_baza_dir(selected)
        except Exception as exc:
            _show_custom_warning("Путь не сохранен", str(exc))
            continue

        _show_custom_info(
            "Путь сохранен",
            f"Путь к папке базы сохранен.\n\n{selected}\n\nФайл настроек:\n{config_path}",
        )
        return 0


def _remember_startup_guard_quickcheck_ok(result):
    try:
        if not result or not result.ok or not result.baza_dir:
            os.environ.pop(STARTUP_GUARD_QUICKCHECK_ENV, None)
            return

        from rem_card.app.runtime_paths import get_journal_db_path
        from rem_card.app.sqlite_shared import NETWORK_SAFE_DB_PROFILE

        db_path = get_journal_db_path(result.baza_dir)
        stat_result = os.stat(db_path)
        payload = {
            "result": "ok",
            "source": "startup_db_guard",
            "pid": os.getpid(),
            "checked_at_epoch": int(time.time()),
            "db_path_norm": os.path.normcase(os.path.abspath(str(db_path))),
            "size_bytes": int(stat_result.st_size),
            "mtime_ns": int(getattr(stat_result, "st_mtime_ns", int(stat_result.st_mtime * 1_000_000_000))),
            "db_profile": NETWORK_SAFE_DB_PROFILE,
        }
        os.environ[STARTUP_GUARD_QUICKCHECK_ENV] = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    except Exception:
        os.environ.pop(STARTUP_GUARD_QUICKCHECK_ENV, None)


def _call_startup_message_callback(callback: Optional[Callable[[], None]]):
    if not callback:
        return
    try:
        callback()
    except Exception:
        pass


def _call_startup_failure_callback(callback: Optional[Callable[[object], object]], failure: object) -> Optional[bool]:
    if not callback:
        return None
    try:
        outcome = callback(failure)
    except Exception:
        return None
    if outcome is None:
        return None
    return bool(outcome)


def _show_emergency_startup_offer(
    message: str,
    *,
    open_text: str = EMERGENCY_STARTUP_PASSWORD_TEXT,
) -> bool:
    if os.environ.get("REMCARD_EMERGENCY_STARTUP_AUTO_ACCEPT") == "1":
        return True
    open_code = 1
    close_code = 0
    try:
        from PySide6.QtWidgets import QApplication

        QApplication.instance() or QApplication(sys.argv)
        from rem_card.ui.shared.emergency_dialogs import EmergencyActionDialog

        result = EmergencyActionDialog.ask(
            None,
            "Аварийный режим",
            message,
            [
                (str(open_text or EMERGENCY_STARTUP_PASSWORD_TEXT), open_code),
                (EMERGENCY_STARTUP_CANCEL_TEXT, close_code),
            ],
            default_code=close_code,
        )
    except Exception:
        result = _show_startup_action_without_settings(
            "Аварийный режим",
            message,
            [
                (str(open_text or EMERGENCY_STARTUP_PASSWORD_TEXT), open_code),
                (EMERGENCY_STARTUP_CANCEL_TEXT, close_code),
            ],
            default_code=close_code,
        )
    return int(result) == open_code


def _call_emergency_startup_offer(message: str, *, open_text: str) -> bool:
    try:
        return _show_emergency_startup_offer(message, open_text=open_text)
    except TypeError as exc:
        if "open_text" not in str(exc):
            raise
        return _show_emergency_startup_offer(message)


def _active_emergency_startup_auto_choice(network_available: bool) -> int | None:
    raw = str(os.environ.get("REMCARD_EMERGENCY_ACTIVE_SESSION_AUTO_CHOICE") or "").strip().lower()
    if raw:
        mapping = {
            "enter": 1,
            "resume": 1,
            "program": 1,
            "войти": 1,
            "network": 2,
            "main": 2,
            "switch": 2,
            "основная": 2,
            "cancel": 0,
            "отмена": 0,
        }
        value = mapping.get(raw)
        if value == 2 and not network_available:
            return 1
        if value is not None:
            return value
    if os.environ.get("REMCARD_EMERGENCY_STARTUP_AUTO_ACCEPT") == "1":
        return 1
    return None


def _active_emergency_startup_actions(network_available: bool) -> list[tuple[str, int]]:
    actions = [(EMERGENCY_STARTUP_ENTER_PROGRAM_TEXT, 1)]
    if network_available:
        actions.append((EMERGENCY_STARTUP_SWITCH_TO_NETWORK_TEXT, 2))
    actions.append((EMERGENCY_STARTUP_CANCEL_TEXT, 0))
    return actions


def _show_active_emergency_startup_choice(message: str, *, network_available: bool) -> int:
    auto_choice = _active_emergency_startup_auto_choice(network_available)
    if auto_choice is not None:
        return int(auto_choice)
    close_code = 0
    try:
        from PySide6.QtWidgets import QApplication

        QApplication.instance() or QApplication(sys.argv)
        from rem_card.ui.shared.emergency_dialogs import EmergencyActionDialog

        return int(
            EmergencyActionDialog.ask(
                None,
                "Аварийный режим",
                message,
                _active_emergency_startup_actions(network_available),
                default_code=close_code,
            )
        )
    except Exception:
        return int(
            _show_startup_action_without_settings(
                "Аварийный режим",
                message,
                _active_emergency_startup_actions(network_available),
                default_code=close_code,
            )
        )


def _show_emergency_startup_password(settings_db_path: str | None) -> bool:
    if os.environ.get("REMCARD_EMERGENCY_PASSWORD_AUTO_ACCEPT") == "1":
        return True
    if os.environ.get("REMCARD_EMERGENCY_STARTUP_AUTO_ACCEPT") == "1":
        return True
    try:
        from PySide6.QtWidgets import QApplication

        QApplication.instance() or QApplication(sys.argv)
        from rem_card.app.emergency_password import verify_emergency_password_for_offline_startup
        from rem_card.ui.shared.emergency_dialogs import EmergencyPasswordDialog

        return EmergencyPasswordDialog.verify(
            None,
            "Аварийный пароль",
            "Для перехода в аварийный режим медсестра должна ввести аварийный пароль.",
            lambda value: verify_emergency_password_for_offline_startup(value, settings_db_path=settings_db_path),
            confirm_text="Перейти в аварийный режим",
            cancel_text=EMERGENCY_STARTUP_CANCEL_TEXT,
            error_text="Пароль неверный. Аварийный режим не будет открыт без подтверждения.",
        )
    except Exception:
        return False


def _show_emergency_network_transition_password(settings_db_path: str | None) -> bool:
    if os.environ.get("REMCARD_EMERGENCY_PASSWORD_AUTO_ACCEPT") == "1":
        return True
    try:
        from PySide6.QtWidgets import QApplication

        QApplication.instance() or QApplication(sys.argv)
        from rem_card.app.emergency_password import verify_emergency_password
        from rem_card.ui.shared.emergency_dialogs import EmergencyPasswordDialog

        return EmergencyPasswordDialog.verify(
            None,
            "Аварийный пароль",
            "Для перехода на основную БД и объединения аварийной сессии введите аварийный пароль.",
            lambda value: verify_emergency_password(
                value,
                settings_db_path=str(settings_db_path or ""),
                readonly=True,
            ),
            confirm_text=EMERGENCY_STARTUP_SWITCH_TO_NETWORK_TEXT,
            cancel_text=EMERGENCY_STARTUP_CANCEL_TEXT,
            error_text="Пароль неверный. Переход на основную БД не будет выполнен без подтверждения.",
        )
    except Exception:
        return False


def _configure_emergency_settings_for_startup(runtime_context) -> None:
    try:
        from rem_card.services.settings.settings_service import configure_settings_service

        configure_settings_service(
            runtime_context=runtime_context,
            readonly=bool(getattr(runtime_context, "settings_readonly", True)),
        )
    except Exception:
        pass


def _startup_failure_category(failure: object) -> str:
    try:
        from rem_card.app.db_access_classifier import classify_database_access_error

        technical = str(getattr(failure, "technical_reason", "") or failure or "")
        if "database file does not exist" in technical.lower() or "missing_db" in technical.lower():
            return "missing_db"
        category = classify_database_access_error(RuntimeError(technical))
        if category:
            return str(category)
    except Exception:
        pass
    return ""


def _try_operblock_offline_startup_after_network_failure(
    role: Optional[str],
    failure: object,
    *,
    before_user_message: Optional[Callable[[], None]] = None,
    reason: str = "",
):
    if not is_operblock_role(role):
        return None

    category = _startup_failure_category(failure)
    technical = str(getattr(failure, "technical_reason", "") or failure or "")
    allowed_categories = {"network_unavailable", "network_unavailable_or_missing", "missing_db", "path_inaccessible"}
    if category not in allowed_categories:
        text = technical.lower()
        if not any(marker in text for marker in ("network", "unable to open", "no such file", "path not found", "недоступ", "сетев")):
            return None

    try:
        from rem_card.app.operblock_offline_store import (
            OPERBLOCK_OFFLINE_WARNING,
            start_or_resume_operblock_offline_session,
        )
        from rem_card.app.runtime_paths import get_journal_db_path, resolve_baza_dir
    except Exception:
        return None

    network_db_path = None
    try:
        network_db_path = get_journal_db_path(resolve_baza_dir())
    except Exception:
        network_db_path = None

    _call_startup_message_callback(before_user_message)
    _show_custom_warning("Оперблок: локальный режим", OPERBLOCK_OFFLINE_WARNING)
    return start_or_resume_operblock_offline_session(
        reason=reason or technical or category or "network_unavailable",
        network_db_path=network_db_path,
    )


def _try_emergency_startup_after_network_failure(
    role: Optional[str],
    failure: object,
    *,
    before_user_message: Optional[Callable[[], None]] = None,
    emergency_startup_request: str | None = None,
):
    if role not in ("doctor", "nurse"):
        return None

    try:
        from rem_card.app.emergency_startup import (
            DOCTOR_NETWORK_UNAVAILABLE_MESSAGE,
            classify_startup_failure,
            prepare_emergency_startup,
            record_emergency_startup_metric,
            start_or_resume_emergency_session,
        )
        from rem_card.app.runtime_outage import (
            startup_request_stale_warning,
            validate_runtime_outage_startup_request_marker,
        )
    except Exception:
        return None

    classification = classify_startup_failure(failure)
    if classification != "network_unavailable":
        return None

    record_emergency_startup_metric("emergency_startup_network_unavailable", role=role or "")
    _call_startup_message_callback(before_user_message)

    startup_request_payload = None
    if emergency_startup_request:
        validation = validate_runtime_outage_startup_request_marker(emergency_startup_request)
        if not validation.ok:
            record_emergency_startup_metric(
                "emergency_startup_failed",
                status="runtime_startup_request_invalid",
                reason=validation.reason,
            )
            _show_startup_warning_without_settings(
                "Аварийный режим недоступен",
                "Запрос аварийного запуска устарел или повреждён. Запустите RemCard заново с рабочего места медсестры.",
            )
            return False
        startup_request_payload = validation.payload

    decision = prepare_emergency_startup(role)
    if not decision.allowed:
        _show_startup_warning_without_settings("Аварийный режим недоступен", decision.user_message)
        return False

    record_emergency_startup_metric("emergency_startup_offered", status=decision.status)
    offer_message = decision.user_message
    stale_warning = startup_request_stale_warning(startup_request_payload)
    if stale_warning and stale_warning not in offer_message:
        offer_message = f"{offer_message}\n\n{stale_warning}"
    open_text = (
        EMERGENCY_STARTUP_ENTER_PROGRAM_TEXT
        if decision.active_session_metadata is not None
        else EMERGENCY_STARTUP_PASSWORD_TEXT
    )
    if not _call_emergency_startup_offer(offer_message, open_text=open_text):
        record_emergency_startup_metric("emergency_startup_user_cancelled", status=decision.status)
        return False

    if decision.active_session_metadata is None:
        settings_db_path = str(decision.password_settings_db_path or "")
        if not _show_emergency_startup_password(settings_db_path):
            record_emergency_startup_metric("emergency_startup_password_rejected", status=decision.status)
            return False

    record_emergency_startup_metric("emergency_startup_user_accepted", status=decision.status)
    try:
        session = start_or_resume_emergency_session(decision, startup_request=startup_request_payload)
        _configure_emergency_settings_for_startup(session.runtime_context)
        return session
    except Exception as exc:
        record_emergency_startup_metric("emergency_startup_failed", status="session_start_failed", reason=str(exc))
        _show_startup_warning_without_settings(
            "Аварийный режим недоступен",
            "Не удалось открыть аварийный режим на этом ПК. RemCard будет закрыт.",
        )
        return False


def _active_emergency_session_network_start_message() -> str:
    return ACTIVE_EMERGENCY_SESSION_NETWORK_AVAILABLE_MESSAGE


def _mark_active_emergency_session_merge_pending_for_network_start(
    decision,
    *,
    source_medical_db_path: str | None = None,
    source_settings_db_path: str | None = None,
    network_baza_dir: str | None = None,
) -> tuple[bool, str]:
    try:
        from rem_card.app.emergency_startup import (
            mark_active_emergency_session_merge_pending_for_network_start,
        )

        return mark_active_emergency_session_merge_pending_for_network_start(
            decision,
            source_medical_db_path=source_medical_db_path,
            source_settings_db_path=source_settings_db_path,
            network_baza_dir=network_baza_dir,
        )
    except Exception as exc:
        return False, f"Переход на основную БД сейчас невозможен.\n\nТехническая причина: {exc}"


def _start_active_emergency_session_for_startup(decision):
    from rem_card.app.emergency_startup import start_or_resume_emergency_session

    session = start_or_resume_emergency_session(decision)
    _configure_emergency_settings_for_startup(session.runtime_context)
    return session.runtime_context


def _resolve_active_emergency_session_before_network_start(
    role: Optional[str],
    *,
    before_user_message: Optional[Callable[[], None]] = None,
):
    if role != "nurse":
        return None

    try:
        from rem_card.app.emergency_startup import prepare_emergency_startup, record_emergency_startup_metric
    except Exception:
        return None

    try:
        decision = prepare_emergency_startup(role)
    except Exception:
        return None
    if getattr(decision, "active_session_metadata", None) is None:
        return None
    if not getattr(decision, "allowed", False):
        return None

    record_emergency_startup_metric("emergency_startup_active_session_network_available", status=decision.status)
    _call_startup_message_callback(before_user_message)
    choice = _show_active_emergency_startup_choice(
        _active_emergency_session_network_start_message(),
        network_available=True,
    )
    if int(choice or 0) == 0:
        record_emergency_startup_metric("emergency_startup_user_cancelled", status=decision.status)
        return False
    if int(choice or 0) == 1:
        record_emergency_startup_metric("emergency_startup_user_accepted", status=decision.status, action="resume")
        return _start_active_emergency_session_for_startup(decision)
    if int(choice or 0) != 2:
        return False

    settings_db_path = str(getattr(decision, "password_settings_db_path", "") or "")
    if not _show_emergency_network_transition_password(settings_db_path):
        record_emergency_startup_metric("emergency_startup_password_rejected", status=decision.status, action="network")
        return False

    ok, details = _mark_active_emergency_session_merge_pending_for_network_start(decision)
    if not ok:
        _show_custom_warning("Аварийное объединение недоступно", details)
        record_emergency_startup_metric(
            "emergency_startup_network_switch_blocked",
            status=decision.status,
            reason=details,
        )
        return _start_active_emergency_session_for_startup(decision)

    record_emergency_startup_metric("emergency_startup_network_switch_requested", status=decision.status)
    _run_pending_emergency_merge_before_startup(before_user_message or (lambda: None))
    return None


def _handle_emergency_startup_guard_failure(
    role: Optional[str],
    failure: object,
    *,
    before_user_message: Optional[Callable[[], None]] = None,
    emergency_startup_request: str | None = None,
) -> tuple[Optional[bool], object | None]:
    opblock_outcome = _try_operblock_offline_startup_after_network_failure(
        role,
        failure,
        before_user_message=before_user_message,
        reason="startup_guard",
    )
    if opblock_outcome is not None:
        return True, opblock_outcome.runtime_context

    outcome = _try_emergency_startup_after_network_failure(
        role,
        failure,
        before_user_message=before_user_message,
        emergency_startup_request=emergency_startup_request,
    )
    if outcome is None:
        return None, None
    if outcome is False:
        return False, None
    return True, outcome.runtime_context


def _compiled_startup_failure_handler(
    failure: object,
    *,
    role: Optional[str],
    before_user_message: Optional[Callable[[], None]],
    state: dict,
    emergency_startup_request: str | None = None,
):
    handled, runtime_context = _handle_emergency_startup_guard_failure(
        role,
        failure,
        before_user_message=before_user_message,
        emergency_startup_request=emergency_startup_request,
    )
    if runtime_context is not None:
        state["runtime_context"] = runtime_context
    return handled


def _bootstrap_container_with_emergency_fallback(
    bootstrap_func,
    *,
    role: Optional[str],
    emergency_runtime_context,
    before_user_message: Optional[Callable[[], None]] = None,
    role_lock=None,
    emergency_startup_request: str | None = None,
):
    if emergency_runtime_context is not None:
        return bootstrap_func(role=role, runtime_context=emergency_runtime_context), emergency_runtime_context, role_lock
    try:
        return bootstrap_func(role=role), emergency_runtime_context, role_lock
    except Exception as exc:
        opblock_outcome = _try_operblock_offline_startup_after_network_failure(
            role,
            exc,
            before_user_message=before_user_message,
            reason="bootstrap",
        )
        if opblock_outcome is not None:
            if role_lock:
                try:
                    role_lock.release()
                except Exception:
                    pass
                role_lock = None
            return bootstrap_func(role=role, runtime_context=opblock_outcome.runtime_context), opblock_outcome.runtime_context, role_lock

        outcome = _try_emergency_startup_after_network_failure(
            role,
            exc,
            before_user_message=before_user_message,
            emergency_startup_request=emergency_startup_request,
        )
        if outcome is None:
            raise
        if outcome is False:
            sys.exit(1)
        if role_lock:
            try:
                role_lock.release()
            except Exception:
                pass
            role_lock = None
        return bootstrap_func(role=role, runtime_context=outcome.runtime_context), outcome.runtime_context, role_lock


def _validate_compiled_role_startup(
    role: Optional[str],
    *,
    before_user_message: Optional[Callable[[], None]] = None,
    on_success: Optional[Callable[[object], None]] = None,
    on_failure: Optional[Callable[[object], object]] = None,
) -> bool:
    if not is_compiled() or role not in ROLE_KEYS:
        return True

    try:
        from rem_card.app.startup_db_guard import run_startup_db_guard

        result = run_startup_db_guard(role=role)
    except Exception as exc:
        _write_startup_local_log(f"startup db guard crashed for role={role}: {exc}")
        handled = _call_startup_failure_callback(on_failure, exc) if role in ("doctor", "nurse") or is_operblock_role(role) else None
        if handled is not None:
            return handled
        _call_startup_message_callback(before_user_message)
        _show_startup_warning_without_settings(
            "База данных недоступна",
            "Не удалось проверить базу данных. Работа временно недоступна. Сообщите ответственному.",
        )
        return False

    if result.ok:
        if on_success:
            try:
                on_success(result)
            except Exception:
                pass
        if result.recovered:
            _write_startup_local_log(
                f"startup db auto-recovered for role={role}: "
                f"restored_from={result.restored_from}; quarantine={result.quarantine_path}"
            )
            _call_startup_message_callback(before_user_message)
            _show_custom_info("База восстановлена", result.user_message)
        return True

    _write_startup_local_log(
        f"startup blocked for role={role}: {result.user_message}; technical={result.technical_reason}"
    )
    handled = _call_startup_failure_callback(on_failure, result) if role in ("doctor", "nurse") or is_operblock_role(role) else None
    if handled is not None:
        return handled
    update_candidate = None
    if _startup_block_requires_update(result.user_message, result.technical_reason):
        update_candidate = _find_startup_update_candidate()

    if update_candidate:
        message = (
            f"{result.user_message}\n\n"
            f"После нажатия \"Понятно\" будет запущено обновление до версии {update_candidate.version}."
        )
        _call_startup_message_callback(before_user_message)
        _show_startup_warning_without_settings("Требуется обновление", message)
        _launch_startup_update(update_candidate, role=role, reason=result.technical_reason)
    else:
        _call_startup_message_callback(before_user_message)
        _show_startup_warning_without_settings("База данных недоступна", result.user_message)
    return False


def _show_compiled_startup_splash(app, QPixmap, QSplashScreen, Qt):
    if not is_compiled():
        return None
    try:
        from rem_card.app.paths import get_icon_dir

        icon_path = os.path.join(get_icon_dir(), "remcardicon.ico")
        splash_pix = QPixmap(icon_path).scaled(128, 128, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        splash = QSplashScreen(splash_pix, Qt.WindowStaysOnTopHint)
        splash.show()
        app.processEvents()
        return splash
    except Exception:
        return None


def _create_startup_qt_context(role: Optional[str]):
    from PySide6.QtNetwork import QLocalSocket, QLocalServer
    from PySide6.QtWidgets import QApplication, QSplashScreen
    from PySide6.QtGui import QPixmap
    from PySide6.QtCore import Qt, QTimer

    app = QApplication.instance() or QApplication(sys.argv)
    splash = _show_compiled_startup_splash(app, QPixmap, QSplashScreen, Qt)
    theme_started = time.perf_counter()
    _apply_app_theme(app, role or "system")
    theme_ui_init_ms = (time.perf_counter() - theme_started) * 1000.0
    return app, splash, QLocalSocket, QLocalServer, Qt, QTimer, theme_ui_init_ms


def _close_startup_splash(app, splash):
    if splash is None:
        return None
    try:
        splash.close()
        app.processEvents()
    except Exception:
        pass
    return None


def _notify_existing_instance(QLocalSocket, server_name: str, role_suffix: str) -> bool:
    socket_client = QLocalSocket()
    socket_client.connectToServer(server_name)
    if not socket_client.waitForConnected(500):
        return False
    socket_client.write(b"SHOW")
    if not socket_client.waitForBytesWritten(500):
        return False
    print(f"Приложение с ролью '{role_suffix}' уже запущено. Окно будет развернуто.")
    return True


def _acquire_initial_role_lock(role: Optional[str]):
    if role not in ROLE_KEYS:
        return None

    from rem_card.app.paths import get_role_lock_path
    from rem_card.app.role_session_lock import RoleSessionLock

    owner_id = f"{socket.gethostname()}:{os.getpid()}:rem_card_startup"
    lock = RoleSessionLock(
        lock_path=get_role_lock_path(role),
        role=role,
        owner_id=owner_id,
        stale_timeout_sec=60.0,
        heartbeat_sec=8.0,
    )
    if lock.acquire():
        return lock

    role_name = role_display_name(role)
    holder = lock.describe_holder()
    _show_native_warning(
        "Роль занята",
        f"Роль \"{role_name}\" уже открыта на другом ПК.\n\n{holder}",
    )
    return None


def _configure_operblock_startup_path(role: Optional[str], path_setup: bool) -> bool:
    if is_operblock_role(role):
        os.environ["REMCARD_UI_ROLE"] = ROLE_OPERBLOCK
        os.environ["REMCARD_LOCAL_FIRST_SYNC"] = "0"
        os.environ["REMCARD_LOCAL_OUTBOX_SYNC"] = "0"
        if path_setup:
            return path_setup

    try:
        operblock_path_info = configure_operblock_runtime_path(role)
    except DataPathConfigurationError as exc:
        _show_native_warning("База данных недоступна", str(exc))
        sys.exit(1)

    if not operblock_path_info:
        return path_setup

    _write_startup_local_log(
        "operblock path configured: "
        f"role={operblock_path_info['role']} "
        f"data_root={operblock_path_info['data_root']} "
        f"db_path={operblock_path_info['db_path']} "
        f"db_profile={operblock_path_info['db_profile']} "
        f"local_db_used={operblock_path_info['local_db_used']}"
    )
    if path_setup:
        return path_setup
    return path_setup


def _infer_compiled_role_from_executable() -> Optional[str]:
    exe_name = os.path.basename(str(sys.executable or "")).lower()
    argv0_name = os.path.basename(str(sys.argv[0] if sys.argv else "")).lower()
    source_name = f"{exe_name} {argv0_name}"
    if "remcarddoctor" in source_name:
        return "doctor"
    if "remcardnurse" in source_name:
        return "nurse"
    if "remcardoperblockemergency" in source_name:
        return ROLE_OPERBLOCK_EMERGENCY
    if "remcardoperblockplanned" in source_name:
        return ROLE_OPERBLOCK_PLANNED
    if "remcardoperblock" in source_name:
        return ROLE_OPERBLOCK
    return None


def _resolve_startup_role(parsed_role: Optional[str], forced_role: Optional[str]) -> Optional[str]:
    compiled_role = _infer_compiled_role_from_executable()
    if compiled_role:
        return compiled_role
    if forced_role:
        return forced_role
    return parsed_role or None


def _preselect_operblock_offline_context_before_network_probe(role: Optional[str]) -> tuple[object | None, str]:
    if not is_operblock_role(role):
        return None, ""
    try:
        from rem_card.app.operblock_offline_store import (
            has_active_local_operblock_case,
            start_or_resume_operblock_offline_session,
        )

        if not has_active_local_operblock_case():
            return None, ""
        session = start_or_resume_operblock_offline_session(reason="active_local_case_blocks_network_probe")
        return session.runtime_context, "active_local_case"
    except Exception:
        return None, ""


def _show_preselected_operblock_offline_notice(
    reason: str,
    close_startup_splash: Callable[[], None],
) -> None:
    if reason != "active_local_case":
        return
    close_startup_splash()
    _show_custom_warning(
        "Оперблок: локальный режим",
        "На этом ПК есть незавершённый локальный случай оперблока. "
        "Оперблок открыт в локальном режиме без проверки сетевой базы.",
    )


def _validate_compiled_startup_unless_runtime_preselected(
    role: Optional[str],
    preselected_runtime_context,
    *,
    close_startup_splash: Callable[[], None],
    emergency_startup_state: dict,
    emergency_startup_request: str | None,
) -> bool:
    if preselected_runtime_context is not None:
        return True
    return _validate_compiled_role_startup(
        role,
        before_user_message=close_startup_splash,
        on_success=_remember_startup_guard_quickcheck_ok,
        on_failure=partial(
            _compiled_startup_failure_handler,
            role=role,
            before_user_message=close_startup_splash,
            state=emergency_startup_state,
            emergency_startup_request=emergency_startup_request,
        ),
    )


def _acquire_role_lock_for_startup(
    role: Optional[str],
    emergency_runtime_context,
    close_startup_splash: Callable[[], None],
):
    if role not in ROLE_KEYS or emergency_runtime_context is not None:
        return None
    role_lock = _acquire_initial_role_lock(role)
    if role_lock is None:
        close_startup_splash()
        sys.exit(0)
    return role_lock


def _shutdown_window_resources(window, logger):
    if not window:
        return True
    iter_containers = getattr(window, "iter_runtime_containers", None)
    if callable(iter_containers):
        containers = list(iter_containers())
    else:
        container = getattr(window, "container", None)
        containers = [container] if container else []
    if not containers:
        return True

    logger.info("Application resource shutdown started")
    data_service_shutdown_ok = True
    db_shutdown_ok = True
    for container in containers:
        container_label = str(getattr(getattr(container, "runtime_context", None), "source_label", "") or "network")
        container_data_ok = True
        data_service = getattr(container, "data_service", None)
        if data_service:
            try:
                logger.info("DataService shutdown started (%s)", container_label)
                container_data_ok = bool(data_service.shutdown())
                logger.info(
                    "DataService shutdown finished (%s) result=%s",
                    container_label,
                    "ok" if container_data_ok else "incomplete",
                )
            except Exception as exc:
                container_data_ok = False
                logger.warning("DataService shutdown failed (%s): %s", container_label, exc)
        data_service_shutdown_ok = bool(data_service_shutdown_ok and container_data_ok)

        db_manager = getattr(container, "db_manager", None)
        if db_manager and container_data_ok:
            try:
                container_db_ok = bool(db_manager.close())
                db_shutdown_ok = bool(db_shutdown_ok and container_db_ok)
                if not container_db_ok:
                    logger.warning("DB manager close did not complete cleanly (%s)", container_label)
            except Exception as exc:
                db_shutdown_ok = False
                logger.warning("DB manager close failed (%s): %s", container_label, exc)
        elif db_manager:
            db_shutdown_ok = False
            logger.warning(
                "DB manager close skipped because DataService shutdown did not complete cleanly (%s)",
                container_label,
            )
    if hasattr(window, "finalize_pending_emergency_discard"):
        if db_shutdown_ok:
            try:
                if not window.finalize_pending_emergency_discard():
                    logger.warning("Pending emergency discard finalization did not complete")
            except Exception as exc:
                logger.warning("Pending emergency discard finalization failed: %s", exc)
        else:
            logger.warning("Pending emergency discard finalization skipped because DB shutdown was incomplete")
    resources_ok = bool(data_service_shutdown_ok and db_shutdown_ok)
    logger.info("Application resource shutdown finished result=%s", "ok" if resources_ok else "incomplete")
    return resources_ok


def _window_exit_role(window, startup_role: Optional[str]) -> str:
    if startup_role in ("doctor", "nurse"):
        return str(startup_role)
    if window is None:
        return ""
    role = getattr(window, "_exit_role_key", None)
    if role:
        return str(role)
    current_role = getattr(window, "_current_role_key", None)
    if callable(current_role):
        try:
            return str(current_role())
        except Exception:
            return ""
    return ""


def _run_doctor_exit_db_rotation(window, startup_role: Optional[str], logger) -> None:
    if _window_exit_role(window, startup_role) != "doctor":
        return
    container = getattr(window, "container", None) if window is not None else None
    db_manager = getattr(container, "db_manager", None) if container is not None else None
    if db_manager is None or not hasattr(db_manager, "maybe_rotate_database_after_doctor_exit"):
        return
    try:
        result = db_manager.maybe_rotate_database_after_doctor_exit()
    except Exception as exc:
        logger.warning("Doctor-exit DB rotation check failed: %s", exc, exc_info=True)
        return

    status = result.get("status") if isinstance(result, dict) else None
    if status == "rotated":
        logger.warning("Doctor-exit DB rotation completed: %s", result)
    elif status in ("deferred_active_beds", "deferred_active_role_lock"):
        logger.warning("Doctor-exit DB rotation deferred: %s", result)
    elif status not in ("missing", "not_due", "rotation_lock_busy", "rotation_forbidden_runtime"):
        logger.info("Doctor-exit DB rotation status: %s | %s", status, result)


def _run_pending_emergency_merge_before_startup(close_startup_splash, logger=None) -> None:
    if logger is None:
        import logging

        logger = logging.getLogger("RemCard")
    try:
        from rem_card.app.emergency_pending_merge import run_pending_emergency_merge

        result = run_pending_emergency_merge()
    except Exception as exc:
        close_startup_splash()
        logger.warning("Pending emergency merge check failed: %s", exc, exc_info=True)
        _show_custom_warning(
            "Аварийное объединение",
            f"Не удалось проверить pending merge аварийной сессии:\n{exc}",
        )
        sys.exit(1)
    _handle_pending_emergency_merge_startup_result(result, close_startup_splash, logger)


def _handle_pending_emergency_merge_startup_result(result, close_startup_splash, logger) -> None:
    if result.ok and not result.attempted:
        return
    if result.ok:
        logger.info(
            "Pending emergency merge completed session_id=%s report=%s",
            result.session_id,
            result.merge_report_path,
        )
        return
    close_startup_splash()
    logger.warning(
        "Pending emergency merge failed session_id=%s error=%s details=%s",
        result.session_id,
        result.error,
        result.details,
    )
    message = result.user_message or "Аварийное объединение не завершено. Аварийная база сохранена."
    if result.error:
        message = f"{message}\n\nТехническая причина: {result.error}"
    _show_custom_warning("Аварийное объединение не завершено", message)
    sys.exit(1)


def _should_run_pending_emergency_merge_before_runtime(emergency_runtime_context) -> bool:
    return emergency_runtime_context is None


def _run_pending_emergency_merge_after_startup_guard(emergency_runtime_context, close_startup_splash) -> None:
    if not _should_run_pending_emergency_merge_before_runtime(emergency_runtime_context):
        return
    _run_pending_emergency_merge_before_startup(close_startup_splash)


def _resolve_startup_runtime_context_after_guard(
    role: Optional[str],
    emergency_startup_state: dict,
    close_startup_splash: Callable[[], None],
):
    emergency_runtime_context = emergency_startup_state.get("runtime_context")
    if emergency_runtime_context is not None:
        return emergency_runtime_context

    active_session_start = _resolve_active_emergency_session_before_network_start(
        role,
        before_user_message=close_startup_splash,
    )
    if active_session_start is False:
        close_startup_splash()
        sys.exit(0)
    if active_session_start is not None:
        emergency_startup_state["runtime_context"] = active_session_start
        return active_session_start
    return None


def _show_operblock_migration_progress_dialog(app, close_startup_splash):
    close_startup_splash()
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout

        dialog = QDialog()
        dialog.setWindowTitle("Перенос данных оперблока")
        dialog.setModal(True)
        dialog.setWindowFlags(
            (dialog.windowFlags() | Qt.CustomizeWindowHint | Qt.WindowTitleHint)
            & ~Qt.WindowCloseButtonHint
        )
        layout = QVBoxLayout(dialog)
        label = QLabel("Не выключайте ПК. Идёт перенос данных оперблока.")
        label.setWordWrap(True)
        label.setMinimumWidth(360)
        layout.addWidget(label)
        dialog.show()
        app.processEvents()
        return dialog
    except Exception:
        return None


def _close_operblock_migration_progress_dialog(dialog, app) -> None:
    if dialog is None:
        return
    try:
        dialog.close()
        app.processEvents()
    except Exception:
        pass


def _run_pending_operblock_offline_migration_before_window(
    *,
    role: Optional[str],
    container,
    app,
    close_startup_splash: Callable[[], None],
    logger,
) -> None:
    if not is_operblock_role(role):
        return
    runtime_context = getattr(getattr(container, "db_manager", None), "runtime_context", None)
    if getattr(runtime_context, "mode", "") != "network":
        return
    try:
        from rem_card.app.operblock_offline_migration import run_pending_operblock_offline_migration
        from rem_card.app.operblock_offline_store import (
            has_active_local_operblock_case,
            pending_completed_local_cases_count,
        )
    except Exception as exc:
        logger.warning("Operblock offline migration import failed: %s", exc, exc_info=True)
        return
    try:
        if has_active_local_operblock_case():
            logger.info("Operblock offline pre-window migration skipped: active local case exists")
            return
        if pending_completed_local_cases_count() <= 0:
            return
    except Exception as exc:
        logger.warning("Operblock offline migration precheck failed: %s", exc, exc_info=True)
        return

    dialog = _show_operblock_migration_progress_dialog(app, close_startup_splash)
    try:
        result = run_pending_operblock_offline_migration(container.db_manager)
    finally:
        _close_operblock_migration_progress_dialog(dialog, app)
    if result.ok:
        try:
            from rem_card.app.operblock_offline_store import cleanup_verified_operblock_offline_session

            cleanup_verified_operblock_offline_session(container.db_manager)
        except Exception as exc:
            logger.warning("Operblock offline retention cleanup failed: %s", exc, exc_info=True)
        logger.info(
            "Operblock offline migration startup result attempted=%s migrated=%s",
            result.attempted,
            result.migrated_cases,
        )
        return
    _show_custom_warning(
        "Перенос данных оперблока",
        result.user_message or "Перенос не выполнен. Локальные данные сохранены. Повторите позже.",
    )


def main(forced_role: Optional[str] = None, path_setup: bool = False):
    try:
        compiled_role = _infer_compiled_role_from_executable()
        if compiled_role:
            forced_role = compiled_role
        _main_impl(forced_role=forced_role, path_setup=path_setup)
    except Exception:
        import traceback

        crash_path = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "crash.txt")
        try:
            with open(crash_path, "w", encoding="utf-8") as fh:
                fh.write(traceback.format_exc())
        except Exception:
            pass
        sys.exit(1)


def _main_impl(forced_role: Optional[str] = None, path_setup: bool = False):
    startup_started_at = time.perf_counter()
    parser = argparse.ArgumentParser(description=APP_DISPLAY_TITLE)
    parser.add_argument("--role", choices=list(ROLE_KEYS), help="Начальная роль пользователя")
    parser.add_argument("--path-setup", action="store_true", help="Настроить путь к папке базы")
    parser.add_argument("--emergency-startup-request", default="", help=argparse.SUPPRESS)
    args, _unknown = parser.parse_known_args()
    args.role = _resolve_startup_role(args.role, forced_role)
    _opblock_startup_metrics_reset(args.role, startup_started_at)
    path_setup = bool(path_setup or args.path_setup)
    os.environ.pop(STARTUP_GUARD_QUICKCHECK_ENV, None)
    path_setup = _configure_operblock_startup_path(args.role, path_setup)
    preselected_runtime_context, preselected_runtime_reason = (
        _preselect_operblock_offline_context_before_network_probe(args.role)
    )

    if _show_update_in_progress_if_needed():
        sys.exit(0)

    _sync_release_settings_if_needed()

    if path_setup:
        sys.exit(_run_path_setup())

    app, splash, QLocalSocket, QLocalServer, Qt, QTimer, theme_ui_init_ms = _create_startup_qt_context(args.role)

    def close_startup_splash():
        nonlocal splash
        splash = _close_startup_splash(app, splash)

    emergency_startup_state = {"runtime_context": preselected_runtime_context}
    _show_preselected_operblock_offline_notice(preselected_runtime_reason, close_startup_splash)

    if not _validate_compiled_startup_unless_runtime_preselected(
        args.role,
        preselected_runtime_context,
        close_startup_splash=close_startup_splash,
        emergency_startup_state=emergency_startup_state,
        emergency_startup_request=args.emergency_startup_request,
    ):
        close_startup_splash()
        sys.exit(1)

    emergency_runtime_context = _resolve_startup_runtime_context_after_guard(
        args.role,
        emergency_startup_state,
        close_startup_splash,
    )
    _run_pending_emergency_merge_after_startup_guard(emergency_runtime_context, close_startup_splash)

    role_suffix = args.role if args.role else "default"
    server_name = f"rem_card_single_instance_server_{role_suffix}"

    if _notify_existing_instance(QLocalSocket, server_name, role_suffix):
        close_startup_splash()
        sys.exit(0)

    QLocalServer.removeServer(server_name)

    BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)

    role_lock = _acquire_role_lock_for_startup(args.role, emergency_runtime_context, close_startup_splash)

    window = None
    logger = None
    exit_code = 1
    try:
        from rem_card.app.logger import log_exception, logger as _logger, init_crash_handler
        from rem_card.app.bootstrap import bootstrap
        from rem_card.ui.main_window import MainWindow

        logger = _logger
        sys.excepthook = log_exception
        init_crash_handler()
        _startup_trace(logger, startup_started_at, "qt_ready", role=args.role or "default")

        server = QLocalServer()
        server.listen(server_name)
        pending_single_instance_clients = []

        container = None
        if args.role in ROLE_KEYS:
            bootstrap_started = time.perf_counter()
            container, emergency_runtime_context, role_lock = _bootstrap_container_with_emergency_fallback(
                bootstrap,
                role=args.role,
                emergency_runtime_context=emergency_runtime_context,
                before_user_message=close_startup_splash,
                role_lock=role_lock,
                emergency_startup_request=args.emergency_startup_request,
            )
            bootstrap_elapsed_ms = (time.perf_counter() - bootstrap_started) * 1000.0
            _startup_trace(
                logger,
                startup_started_at,
                "bootstrap_done",
                elapsed_ms=f"{bootstrap_elapsed_ms:.1f}",
            )
            _log_startup_phase_metrics(
                container,
                logger,
                startup_started_at,
                theme_ui_init_ms=theme_ui_init_ms,
                total_bootstrap_ms=bootstrap_elapsed_ms,
            )
            _opblock_startup_record_duration(args.role, "bootstrap_ms", bootstrap_elapsed_ms, source="app_main")
            _install_startup_trace_hooks(container, logger, startup_started_at)

        _run_pending_operblock_offline_migration_before_window(
            role=args.role,
            container=container,
            app=app,
            close_startup_splash=close_startup_splash,
            logger=logger,
        )

        main_window_started = _opblock_startup_timer_start(args.role)
        window = MainWindow(
            container=container,
            role=args.role,
            role_session_lock=role_lock,
            role_key=args.role if role_lock else None,
        )
        _opblock_startup_record_since(args.role, "main_window_create_ms", main_window_started, source="app_main")
        _startup_trace(logger, startup_started_at, "main_window_constructed", role=args.role or "default")

        if container is None:
            bootstrap_started = time.perf_counter()
            container, emergency_runtime_context, role_lock = _bootstrap_container_with_emergency_fallback(
                bootstrap,
                role=args.role,
                emergency_runtime_context=emergency_runtime_context,
                before_user_message=close_startup_splash,
                role_lock=role_lock,
                emergency_startup_request=args.emergency_startup_request,
            )
            bootstrap_elapsed_ms = (time.perf_counter() - bootstrap_started) * 1000.0
            set_default_container = getattr(window, "set_default_container", None)
            if callable(set_default_container):
                set_default_container(container)
            else:
                window.container = container
            _startup_trace(
                logger,
                startup_started_at,
                "bootstrap_done",
                elapsed_ms=f"{bootstrap_elapsed_ms:.1f}",
            )
            _log_startup_phase_metrics(
                container,
                logger,
                startup_started_at,
                theme_ui_init_ms=theme_ui_init_ms,
                total_bootstrap_ms=bootstrap_elapsed_ms,
            )
            _opblock_startup_record_duration(args.role, "bootstrap_ms", bootstrap_elapsed_ms, source="app_main")
            _install_startup_trace_hooks(container, logger, startup_started_at)
        else:
            window.container = container
        logger.info("Bootstrap completed")

        initial_role_prepared = False
        if args.role in ROLE_KEYS and hasattr(window, "prepare_initial_role_ui_for_startup"):
            role_ui_started = time.perf_counter()
            prepared = bool(window.prepare_initial_role_ui_for_startup())
            initial_role_prepared = prepared
            _startup_trace(
                logger,
                startup_started_at,
                "initial_role_ui_prepared",
                role=args.role,
                prepared=int(prepared),
                elapsed_ms=f"{(time.perf_counter() - role_ui_started) * 1000.0:.1f}",
            )
            if prepared:
                if hasattr(window, "start_initial_role_refresh"):
                    window.start_initial_role_refresh()
                _wait_for_initial_w1(
                    app,
                    window,
                    logger,
                    startup_started_at,
                    _startup_w1_wait_ms(),
                )

        if splash is not None:
            splash.finish(window)
        window.show()
        _opblock_startup_record_window_shown(args.role, initial_role_prepared, QTimer)
        if initial_role_prepared and hasattr(window, "wake_initial_role_monitor"):
            QTimer.singleShot(250, window.wake_initial_role_monitor)
        _startup_trace(logger, startup_started_at, "window_shown", role=args.role or "default")

        def on_new_connection():
            while server.hasPendingConnections():
                client = server.nextPendingConnection()
                if client is None:
                    continue
                pending_single_instance_clients.append(client)
                try:
                    client.setParent(server)
                    if client.waitForReadyRead(500):
                        data = bytes(client.readAll()).decode("utf-8", errors="replace")
                        if data == "SHOW":
                            if window.isMinimized():
                                window.showNormal()
                            window.setWindowState(window.windowState() & ~Qt.WindowMinimized | Qt.WindowActive)
                            window.activateWindow()
                            window.raise_()
                except RuntimeError as exc:
                    logger.warning("Single-instance socket handling failed: %s", exc)
                finally:
                    if client in pending_single_instance_clients:
                        pending_single_instance_clients.remove(client)
                    try:
                        client.disconnectFromServer()
                    except RuntimeError:
                        pass
                    try:
                        client.deleteLater()
                    except RuntimeError:
                        pass

        server.newConnection.connect(on_new_connection)

        exit_code = app.exec()
        logger.info("Application exiting with code %s", exit_code)
    except Exception as exc:
        close_startup_splash()
        if logger:
            logger.critical("Critical error during startup/runtime: %s", exc, exc_info=True)
        else:
            print(f"Critical startup error: {exc}")
        try:
            from rem_card.app.db_availability import is_database_unavailable_error

            if is_database_unavailable_error(exc) or isinstance(exc, (DataPathConfigurationError, FileNotFoundError, OSError)):
                _write_startup_local_log(f"startup/runtime blocked: {exc}")
                _show_custom_warning("База данных недоступна", str(exc))
        except Exception:
            pass
        exit_code = 1
    finally:
        resources_shutdown_ok = False
        if window and logger:
            resources_shutdown_ok = _shutdown_window_resources(window, logger)
        try:
            if window and hasattr(window, "release_role_lock"):
                window.release_role_lock()
            elif role_lock:
                role_lock.release()
        except Exception:
            pass
        if exit_code == 0 and window and logger and resources_shutdown_ok:
            _run_doctor_exit_db_rotation(window, args.role, logger)
        if logger:
            try:
                from rem_card.app.logger import finalize_crash_handler

                finalize_crash_handler(exit_code=exit_code)
            except Exception as exc:
                logger.warning("Crash handler finalization failed: %s", exc)
        if exit_code == 0:
            _launch_exit_update_if_needed()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()

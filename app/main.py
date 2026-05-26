import argparse
import ctypes
import json
import os
import socket
import sys
import time
from typing import Callable, Optional

from rem_card.app.runtime_paths import (
    DataPathConfigurationError,
    cleanup_old_local_logs,
    create_baza_structure_and_db,
    get_local_logs_dir,
    is_compiled,
    write_configured_baza_dir,
)
from rem_card.app.version import APP_DISPLAY_TITLE, APP_VERSION


STARTUP_TRACE_ENV = "REMCARD_STARTUP_TRACE"
STARTUP_W1_WAIT_MS_ENV = "REMCARD_STARTUP_W1_WAIT_MS"
STARTUP_W1_WAIT_DEFAULT_MS = 300
FULL_RUNTIME_THEME_ENV = "REMCARD_FULL_RUNTIME_THEME"
STARTUP_GUARD_QUICKCHECK_ENV = "REMCARD_STARTUP_GUARD_QUICKCHECK_OK"


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
    if role_widget is None:
        return None
    layout = getattr(role_widget, "layout_manager", None)
    if layout is not None:
        return layout
    remcard_widget = getattr(role_widget, "remcard_widget", None)
    return getattr(remcard_widget, "layout_manager", None)


def _initial_w1_state(window) -> dict:
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
        _show_custom_warning("Обновление программы", _describe_startup_update_lock(payload))
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


def _validate_compiled_role_startup(
    role: Optional[str],
    *,
    before_user_message: Optional[Callable[[], None]] = None,
    on_success: Optional[Callable[[object], None]] = None,
) -> bool:
    if not is_compiled() or role not in ("doctor", "nurse"):
        return True

    try:
        from rem_card.app.startup_db_guard import run_startup_db_guard

        result = run_startup_db_guard(role=role)
    except Exception as exc:
        _write_startup_local_log(f"startup db guard crashed for role={role}: {exc}")
        _call_startup_message_callback(before_user_message)
        _show_custom_warning(
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
    update_candidate = None
    if _startup_block_requires_update(result.user_message, result.technical_reason):
        update_candidate = _find_startup_update_candidate()

    if update_candidate:
        message = (
            f"{result.user_message}\n\n"
            f"После нажатия \"Понятно\" будет запущено обновление до версии {update_candidate.version}."
        )
        _call_startup_message_callback(before_user_message)
        _show_custom_warning("Требуется обновление", message)
        _launch_startup_update(update_candidate, role=role, reason=result.technical_reason)
    else:
        _call_startup_message_callback(before_user_message)
        _show_custom_warning("База данных недоступна", result.user_message)
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
    if role not in ("doctor", "nurse"):
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

    role_name = "Врач" if role == "doctor" else "Медсестра"
    holder = lock.describe_holder()
    _show_native_warning(
        "Роль занята",
        f"Роль \"{role_name}\" уже открыта на другом ПК.\n\n{holder}",
    )
    return None


def _shutdown_window_resources(window, logger):
    if not window:
        return
    container = getattr(window, "container", None)
    if not container:
        return

    logger.info("Application resource shutdown started")
    data_service_shutdown_ok = True
    data_service = getattr(container, "data_service", None)
    if data_service:
        try:
            logger.info("DataService shutdown started")
            data_service_shutdown_ok = bool(data_service.shutdown())
            logger.info("DataService shutdown finished result=%s", "ok" if data_service_shutdown_ok else "incomplete")
        except Exception as exc:
            data_service_shutdown_ok = False
            logger.warning("DataService shutdown failed: %s", exc)

    db_manager = getattr(container, "db_manager", None)
    if db_manager and data_service_shutdown_ok:
        try:
            db_shutdown_ok = bool(db_manager.close())
            if not db_shutdown_ok:
                logger.warning("DB manager close did not complete cleanly")
        except Exception as exc:
            logger.warning("DB manager close failed: %s", exc)
    elif db_manager:
        logger.warning("DB manager close skipped because DataService shutdown did not complete cleanly")
    logger.info("Application resource shutdown finished")


def main(forced_role: Optional[str] = None, path_setup: bool = False):
    try:
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
    parser.add_argument("--role", choices=["doctor", "nurse"], help="Начальная роль пользователя")
    parser.add_argument("--path-setup", action="store_true", help="Настроить путь к папке базы")
    args, _unknown = parser.parse_known_args()
    if forced_role:
        args.role = forced_role
    path_setup = bool(path_setup or args.path_setup)
    os.environ.pop(STARTUP_GUARD_QUICKCHECK_ENV, None)

    if _show_update_in_progress_if_needed():
        sys.exit(0)

    _sync_release_settings_if_needed()

    if path_setup:
        sys.exit(_run_path_setup())

    app, splash, QLocalSocket, QLocalServer, Qt, QTimer, theme_ui_init_ms = _create_startup_qt_context(args.role)

    def close_startup_splash():
        nonlocal splash
        splash = _close_startup_splash(app, splash)

    if not _validate_compiled_role_startup(
        args.role,
        before_user_message=close_startup_splash,
        on_success=_remember_startup_guard_quickcheck_ok,
    ):
        close_startup_splash()
        sys.exit(1)

    role_suffix = args.role if args.role else "default"
    server_name = f"rem_card_single_instance_server_{role_suffix}"

    if _notify_existing_instance(QLocalSocket, server_name, role_suffix):
        close_startup_splash()
        sys.exit(0)

    QLocalServer.removeServer(server_name)

    BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)

    role_lock = _acquire_initial_role_lock(args.role)
    if args.role in ("doctor", "nurse") and role_lock is None:
        close_startup_splash()
        sys.exit(0)

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

        container = None
        if args.role in ("doctor", "nurse"):
            bootstrap_started = time.perf_counter()
            container = bootstrap()
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
            _install_startup_trace_hooks(container, logger, startup_started_at)

        window = MainWindow(
            container=container,
            role=args.role,
            role_session_lock=role_lock,
            role_key=args.role if role_lock else None,
        )
        _startup_trace(logger, startup_started_at, "main_window_constructed", role=args.role or "default")

        if container is None:
            bootstrap_started = time.perf_counter()
            container = bootstrap()
            bootstrap_elapsed_ms = (time.perf_counter() - bootstrap_started) * 1000.0
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
            _install_startup_trace_hooks(container, logger, startup_started_at)
        else:
            window.container = container
        logger.info("Bootstrap completed")

        initial_role_prepared = False
        if args.role in ("doctor", "nurse") and hasattr(window, "prepare_initial_role_ui_for_startup"):
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
        if initial_role_prepared and hasattr(window, "wake_initial_role_monitor"):
            QTimer.singleShot(250, window.wake_initial_role_monitor)
        _startup_trace(logger, startup_started_at, "window_shown", role=args.role or "default")

        def on_new_connection():
            client = server.nextPendingConnection()
            if client.waitForReadyRead(500):
                data = client.readAll().data().decode("utf-8")
                if data == "SHOW":
                    if window.isMinimized():
                        window.showNormal()
                    window.setWindowState(window.windowState() & ~Qt.WindowMinimized | Qt.WindowActive)
                    window.activateWindow()
                    window.raise_()
            client.disconnectFromServer()

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
        if window and logger:
            _shutdown_window_resources(window, logger)
        try:
            if window and hasattr(window, "release_role_lock"):
                window.release_role_lock()
            elif role_lock:
                role_lock.release()
        except Exception:
            pass
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

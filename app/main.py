import argparse
import ctypes
import os
import socket
import sys
from typing import Optional

from rem_card.app.runtime_paths import (
    DataPathConfigurationError,
    cleanup_old_local_logs,
    create_baza_structure_and_db,
    get_local_logs_dir,
    is_compiled,
    write_configured_baza_dir,
)
from rem_card.app.version import APP_DISPLAY_TITLE, APP_VERSION


def _show_native_warning(title: str, message: str):
    try:
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)
    except Exception:
        print(f"{title}: {message}")


def _apply_app_theme(app, role: Optional[str] = None):
    try:
        from rem_card.ui.styles.theme_manager import get_theme_manager

        manager = get_theme_manager()
        manager.load(role or "system")
        manager.apply_to_app(app, role or "system")
    except Exception:
        try:
            from rem_card.ui.styles.theme import GLOBAL_STYLE
            from rem_card.ui.styles.tooltip_style import apply_tooltip_palette

            app.setStyleSheet(GLOBAL_STYLE)
            apply_tooltip_palette(app)
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


def _validate_compiled_role_startup(role: Optional[str]) -> bool:
    if not is_compiled() or role not in ("doctor", "nurse"):
        return True

    try:
        from rem_card.app.startup_db_guard import run_startup_db_guard

        result = run_startup_db_guard(role=role)
    except Exception as exc:
        _write_startup_local_log(f"startup db guard crashed for role={role}: {exc}")
        _show_custom_warning(
            "База данных недоступна",
            "Не удалось проверить базу данных. Работа временно недоступна. Сообщите ответственному.",
        )
        return False

    if result.ok:
        if result.recovered:
            _write_startup_local_log(
                f"startup db auto-recovered for role={role}: "
                f"restored_from={result.restored_from}; quarantine={result.quarantine_path}"
            )
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
        _show_custom_warning("Требуется обновление", message)
        _launch_startup_update(update_candidate, role=role, reason=result.technical_reason)
    else:
        _show_custom_warning("База данных недоступна", result.user_message)
    return False


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

    data_service = getattr(container, "data_service", None)
    if data_service:
        try:
            data_service.shutdown()
        except Exception as exc:
            logger.warning("DataService shutdown failed: %s", exc)

    db_manager = getattr(container, "db_manager", None)
    if db_manager:
        try:
            db_manager.close()
        except Exception as exc:
            logger.warning("DB manager close failed: %s", exc)


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
    parser = argparse.ArgumentParser(description=APP_DISPLAY_TITLE)
    parser.add_argument("--role", choices=["doctor", "nurse"], help="Начальная роль пользователя")
    parser.add_argument("--path-setup", action="store_true", help="Настроить путь к папке базы")
    args, _unknown = parser.parse_known_args()
    if forced_role:
        args.role = forced_role
    path_setup = bool(path_setup or args.path_setup)

    if _show_update_in_progress_if_needed():
        sys.exit(0)

    if path_setup:
        sys.exit(_run_path_setup())

    if not _validate_compiled_role_startup(args.role):
        sys.exit(1)

    from PySide6.QtNetwork import QLocalSocket, QLocalServer
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QPixmap
    from PySide6.QtCore import Qt

    role_suffix = args.role if args.role else "default"
    server_name = f"rem_card_single_instance_server_{role_suffix}"

    socket_client = QLocalSocket()
    socket_client.connectToServer(server_name)

    if socket_client.waitForConnected(500):
        socket_client.write(b"SHOW")
        if socket_client.waitForBytesWritten(500):
            print(f"Приложение с ролью '{role_suffix}' уже запущено. Окно будет развернуто.")
            sys.exit(0)

    QLocalServer.removeServer(server_name)

    BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)

    role_lock = _acquire_initial_role_lock(args.role)
    if args.role in ("doctor", "nurse") and role_lock is None:
        sys.exit(0)

    app = QApplication(sys.argv)
    window = None
    logger = None
    exit_code = 1
    try:
        from rem_card.app.paths import get_icon_dir

        icon_path = os.path.join(get_icon_dir(), "remcardicon.ico")
        splash = None
        if is_compiled():
            from PySide6.QtWidgets import QSplashScreen

            splash_pix = QPixmap(icon_path).scaled(128, 128, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            splash = QSplashScreen(splash_pix, Qt.WindowStaysOnTopHint)
            splash.show()
            app.processEvents()

        _apply_app_theme(app, args.role or "system")

        from rem_card.app.logger import log_exception, logger as _logger, init_crash_handler
        from rem_card.app.bootstrap import bootstrap
        from rem_card.ui.main_window import MainWindow

        logger = _logger
        sys.excepthook = log_exception
        init_crash_handler()
        _apply_app_theme(app, args.role or "system")

        server = QLocalServer()
        server.listen(server_name)

        window = MainWindow(
            container=None,
            role=args.role,
            role_session_lock=role_lock,
            role_key=args.role if role_lock else None,
        )

        container = bootstrap()
        window.container = container
        logger.info("Bootstrap completed")

        if splash is not None:
            splash.finish(window)
        window.show()

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
        if exit_code == 0:
            _launch_exit_update_if_needed()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()

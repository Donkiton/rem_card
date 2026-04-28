import argparse
import ctypes
import os
import socket
import sys
from typing import Optional

from rem_card.app.runtime_paths import (
    BAZA_DIR_NAME,
    DataPathConfigurationError,
    cleanup_old_local_logs,
    create_baza_structure_and_db,
    get_local_logs_dir,
    is_compiled,
    write_configured_baza_dir,
)
from rem_card.app.version import APP_DISPLAY_TITLE


def _show_native_warning(title: str, message: str):
    try:
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)
    except Exception:
        print(f"{title}: {message}")


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
        try:
            from rem_card.ui.styles.theme import GLOBAL_STYLE

            app.setStyleSheet(GLOBAL_STYLE)
        except Exception:
            pass
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        CustomMessageBox.warning(None, title, message)
    except Exception:
        _show_native_warning(title, message)


def _show_custom_info(title: str, message: str):
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication(sys.argv)
        try:
            from rem_card.ui.styles.theme import GLOBAL_STYLE

            app.setStyleSheet(GLOBAL_STYLE)
        except Exception:
            pass
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        CustomMessageBox.information(None, title, message)
    except Exception:
        print(f"{title}: {message}")


def _show_update_in_progress_if_needed() -> bool:
    if not is_compiled():
        return False
    try:
        from rem_card.app.update_launcher import describe_update_lock, is_update_in_progress

        if not is_update_in_progress():
            return False
        _show_custom_warning("Обновление программы", describe_update_lock())
        return True
    except Exception:
        return False


def _launch_startup_update_if_needed() -> bool:
    if not is_compiled():
        return False
    try:
        from rem_card.app.update_checker import find_best_update
        from rem_card.app.update_launcher import current_exe_name, launch_update

        candidate = find_best_update()
        if not candidate:
            return False
        if launch_update(candidate, restart_exe=current_exe_name(), wait_for_parent=True):
            return True
        _show_custom_warning(
            "Обновление программы",
            "Найдена новая версия программы, но не удалось запустить обновление. "
            "Сообщите ответственному.",
        )
        return True
    except DataPathConfigurationError:
        return False
    except Exception as exc:
        _write_startup_local_log(f"startup update check failed: {exc}")
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
        return launch_update(candidate, restart_exe=None, wait_for_parent=True)
    except Exception as exc:
        _write_startup_local_log(f"exit update check failed: {exc}")
        return False


def _run_path_setup():
    os.environ["REMCARD_PATH_SETUP_MODE"] = "1"

    from PySide6.QtWidgets import QApplication, QFileDialog

    app = QApplication.instance() or QApplication(sys.argv)
    try:
        from rem_card.ui.styles.theme import GLOBAL_STYLE

        app.setStyleSheet(GLOBAL_STYLE)
    except Exception:
        pass

    while True:
        selected = QFileDialog.getExistingDirectory(
            None,
            f"Выберите папку {BAZA_DIR_NAME}",
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
            f"Путь к {BAZA_DIR_NAME} сохранен.\n\n{selected}\n\nФайл настроек:\n{config_path}",
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
    parser.add_argument("--path-setup", action="store_true", help="Настроить путь к Baza_rao3_jurnal")
    args, _unknown = parser.parse_known_args()
    if forced_role:
        args.role = forced_role
    path_setup = bool(path_setup or args.path_setup)

    if _show_update_in_progress_if_needed():
        sys.exit(0)

    if not path_setup and _launch_startup_update_if_needed():
        sys.exit(0)

    if path_setup:
        sys.exit(_run_path_setup())

    if not _validate_compiled_role_startup(args.role):
        sys.exit(1)

    from PySide6.QtNetwork import QLocalSocket, QLocalServer
    from PySide6.QtWidgets import QApplication, QSplashScreen
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
        splash_pix = QPixmap(icon_path).scaled(128, 128, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        splash = QSplashScreen(splash_pix, Qt.WindowStaysOnTopHint)
        splash.show()
        app.processEvents()

        from rem_card.app.logger import log_exception, logger as _logger, init_crash_handler
        from rem_card.app.bootstrap import bootstrap
        from rem_card.ui.main_window import MainWindow
        from rem_card.ui.styles.theme import GLOBAL_STYLE

        logger = _logger
        sys.excepthook = log_exception
        init_crash_handler()
        app.setStyleSheet(GLOBAL_STYLE)

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

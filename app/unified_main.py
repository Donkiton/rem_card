"""Lightweight bootstrap: show Qt before importing the clinical application."""
from __future__ import annotations

import argparse
import multiprocessing
import os
import sys


COMPILED_WORKER_SMOKE_TIMEOUT_SECONDS = 20.0


def _compiled_smoke_child() -> None:
    """Importable no-op target that requires a real multiprocessing worker."""


def _run_compiled_worker_smoke(
    timeout_seconds: float = COMPILED_WORKER_SMOKE_TIMEOUT_SECONDS,
) -> bool:
    process = multiprocessing.get_context("spawn").Process(
        target=_compiled_smoke_child,
        name="RemCardCompiledSmoke",
    )
    try:
        process.start()
        process.join(max(0.1, float(timeout_seconds)))
        if process.is_alive():
            process.terminate()
            process.join(5.0)
            return False
        return process.exitcode == 0
    except Exception:
        if process.is_alive():
            process.terminate()
            process.join(5.0)
        return False
    finally:
        try:
            process.close()
        except (AttributeError, ValueError):
            pass


class _GuiSafeArgumentParser(argparse.ArgumentParser):
    """Keep normal CLI errors useful when the frozen GUI has no stderr."""

    def error(self, message):
        if sys.stderr is not None:
            super().error(message)
        _show_argument_error(str(message))
        raise SystemExit(2)


def _show_argument_error(message: str) -> None:
    text = f"Некорректные параметры запуска RemCard:\n\n{message}"
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, text, "RemCard", 0x10)
    except Exception:
        # A frozen GUI process commonly has neither console stream.  Exiting
        # with argparse's status is still preferable to a secondary traceback.
        return


def _parse_args(argv=None):
    parser = _GuiSafeArgumentParser()
    parser.add_argument("--restart-after-pid", type=int, default=0)
    parser.add_argument("--role", default=None)  # Accepted legacy restart hint; always show chooser.
    parser.add_argument("--emergency-startup-request", default="", help=argparse.SUPPRESS)
    parser.add_argument("--compiled-smoke", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.emergency_startup_request and str(args.role or "").strip().casefold() not in {"doctor", "nurse"}:
        parser.error("--emergency-startup-request requires --role doctor or --role nurse")
    return args


def main(argv=None):
    # On Windows a frozen multiprocessing child starts the application EXE with
    # private worker arguments.  Dispatch those before the application parser;
    # otherwise a harmless worker launch is mistaken for an invalid GUI start.
    multiprocessing.freeze_support()
    args = _parse_args(argv)
    if args.compiled_smoke:
        from rem_card.ui.shared.unified_entry_pages import WelcomePage, StartupPage  # noqa: F401
        from rem_card.app.unified_access import MaintenanceStore  # noqa: F401
        if not _run_compiled_worker_smoke():
            # Numeric SystemExit is safe for a console=False executable whose
            # stdout/stderr streams are both None.
            raise SystemExit(3)
        if sys.stdout is not None:
            print("REMCARD_UNIFIED_SMOKE_OK")
        return
    from PySide6.QtWidgets import QApplication
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtCore import QTimer
    from rem_card.app.runtime_paths import is_compiled

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    # Namespace is application-wide, independent of role, installation or selected DB.
    from rem_card.app.main import _prepare_single_instance_server, _connect_single_instance_requests
    from rem_card.app.main import _wait_for_restart_parent, SINGLE_INSTANCE_ACQUIRED
    from rem_card.app.main import _show_unresponsive_single_instance_warning
    from rem_card.app.logger import logger
    from PySide6.QtCore import Qt
    import hashlib

    if args.restart_after_pid and not _wait_for_restart_parent(args.restart_after_pid):
        return
    namespace = os.environ.get("REMCARD_TEST_INSTANCE_NAMESPACE", "")
    server_name = "rem_card_unified" + ("" if is_compiled() else "_dev")
    if namespace:
        server_name += "_test_" + hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:16]
    server, listening, status = _prepare_single_instance_server(QLocalSocket, QLocalServer, server_name, "RemCard")
    if status != SINGLE_INSTANCE_ACQUIRED:
        if status != "shown":
            _show_unresponsive_single_instance_warning("RemCard")
        return
    from rem_card.ui.unified_window import UnifiedWindow
    from rem_card.app.unified_preflight import attach_startup_request, build_startup_request

    window = UnifiedWindow()
    startup_request = build_startup_request(
        role=args.role,
        emergency_startup_request=args.emergency_startup_request,
    )
    attach_startup_request(window, startup_request)
    _connect_single_instance_requests(server, window, Qt, QTimer, logger)
    window.show()
    QTimer.singleShot(0, window.initialize)
    try:
        app.exec()
    finally:
        if listening:
            server.close()
            QLocalServer.removeServer(server_name)


if __name__ == "__main__":
    main()

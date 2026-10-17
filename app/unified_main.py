"""Lightweight bootstrap: show Qt before importing the clinical application."""
from __future__ import annotations

import argparse
import multiprocessing
import os
import sys
from contextlib import contextmanager


COMPILED_WORKER_SMOKE_TIMEOUT_SECONDS = 20.0


@contextmanager
def _crash_session():
    """One diagnostic session for the shell and all role switches."""
    from rem_card.app.logger import init_crash_handler, finalize_crash_handler

    init_crash_handler(role="unified")
    result = {"exit_code": 0}
    recorded = False
    try:
        yield result
    except BaseException:
        result["exit_code"] = 1
        try:
            from rem_card.services.crash_reports import capture_exception

            recorded = bool(capture_exception("unhandled_python_exception", *sys.exc_info()))
            if recorded:
                # The top-level excepthook still writes the human-readable log,
                # but must not create a second report after session finalization.
                sys.exc_info()[1]._remcard_crash_reported = True
        except Exception:
            pass
        raise
    finally:
        finalize_crash_handler(exit_code=result["exit_code"], crash_recorded=recorded)


def _compiled_smoke_child() -> None:
    """Exercise the shipped snapshot path in a real, isolated frozen worker."""
    import json
    import sqlite3
    import tempfile
    from pathlib import Path
    from contextlib import closing

    with tempfile.TemporaryDirectory(prefix="remcard_snapshot_smoke_", ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        central = root / "central"
        source = central / "archiv" / "rao_journal.db"
        source.parent.mkdir(parents=True)
        config = root / "data_path.json"
        config.write_text(json.dumps({"baza_dir": str(central)}), encoding="utf-8")
        os.environ.update({
            "REMCARD_DATA_PATH_CONFIG": str(config), "REMCARD_BAZA_DIR": str(central),
            "REMCARD_DEV_DATABASE_CONFIG": str(config),
            "REMCARD_LOCAL_LOGS_DIR": str(root / "logs"),
            "LOCALAPPDATA": str(root / "profile" / "local"),
            "APPDATA": str(root / "profile" / "roaming"),
            "PROGRAMDATA": str(root / "profile" / "programdata"),
            "USERPROFILE": str(root / "profile" / "user"),
            "REMCARD_LOCAL_METRICS_ENABLED": "0", "REMCARD_LOG_STORAGE_ENABLED": "0",
        })
        with closing(sqlite3.connect(source)) as conn, conn:
            conn.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO meta VALUES ('db_cycle_started_at', 'synthetic-smoke');
                INSERT INTO meta VALUES ('unified_schema_fastpath_rev', '26');
                CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY);
                INSERT INTO schema_migrations VALUES (26);
                CREATE TABLE change_log(id INTEGER PRIMARY KEY, entity_name TEXT,
                    entity_id INTEGER, admission_id INTEGER, action TEXT,
                    changed_at TEXT, changed_by TEXT, version INTEGER);
                CREATE TABLE example(id INTEGER PRIMARY KEY, value TEXT);
                INSERT INTO example VALUES (1, 'synthetic');
            """)
        before = source.read_bytes()
        workspace = root / "snapshot"
        workspace.mkdir()
        from rem_card.app.startup_snapshot import check_snapshot
        ok, payload, corrupt = check_snapshot(str(source), str(workspace))
        if not ok or corrupt or not json.loads(payload).get("snapshot"):
            raise RuntimeError(f"Compiled startup snapshot failed: {payload}")
        if source.read_bytes() != before:
            raise RuntimeError("Compiled startup snapshot changed its source")
        if os.name == "nt":
            # Exercise native DPAPI, persistence and the incremental engine in
            # the shipped executable, rather than merely importing modules.
            first = json.loads(payload)
            for index, expected_mode in ((2, "cache"), (3, "delta")):
                if index == 3:
                    with closing(sqlite3.connect(source)) as conn, conn:
                        conn.execute("UPDATE example SET value='synthetic updated' WHERE id=1")
                    before = source.read_bytes()
                next_workspace = root / f"snapshot_{index}"
                next_workspace.mkdir()
                ok, payload, corrupt = check_snapshot(str(source), str(next_workspace))
                if not ok or corrupt:
                    raise RuntimeError(f"Compiled cached snapshot failed: {payload}")
                result = json.loads(payload)
                if (result.get("sync_mode") != expected_mode
                        or result.get("full_checked_at_epoch") != first["full_checked_at_epoch"]):
                    raise RuntimeError(f"Compiled snapshot mode/TTL failed: {result.get('sync_mode')}")
                if source.read_bytes() != before:
                    raise RuntimeError("Compiled incremental snapshot changed its source")
        import logging
        from rem_card.app.runtime_log_retention import stop_log_maintenance
        stop_log_maintenance()
        logging.shutdown()


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
    parser.add_argument("--resume-role", choices=("doctor", "nurse"), default=None, help=argparse.SUPPRESS)
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
        from rem_card.app.client_build_profile import compiled_profile_matches_expectation
        if not compiled_profile_matches_expectation():
            raise SystemExit(4)
        from rem_card.ui.shared.unified_entry_pages import WelcomePage, StartupPage  # noqa: F401
        from rem_card.app.unified_access import MaintenanceStore  # noqa: F401
        if not _run_compiled_worker_smoke():
            # Numeric SystemExit is safe for a console=False executable whose
            # stdout/stderr streams are both None.
            raise SystemExit(3)
        if sys.stdout is not None:
            print("REMCARD_UNIFIED_SMOKE_OK")
        return
    from rem_card.app.gui_gc import disable_automatic_gui_gc, install_gui_gc
    disable_automatic_gui_gc()
    from rem_card.app.startup_diagnostics import start
    start()
    from PySide6.QtWidgets import QApplication
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtCore import QTimer
    from rem_card.app.runtime_paths import is_compiled

    app = QApplication.instance() or QApplication(sys.argv)
    install_gui_gc(app)
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
    try:
        with _crash_session() as session:
            from rem_card.ui.unified_window import UnifiedWindow
            from rem_card.app.unified_preflight import attach_startup_request, build_startup_request

            window = UnifiedWindow()
            from rem_card.app.exit_watchdog import ExitWatchdog
            from rem_card.app.runtime_paths import get_runtime_logs_dir
            window._exit_guard = ExitWatchdog(diagnostic_dir=get_runtime_logs_dir())
            startup_request = build_startup_request(
                role=args.role,
                emergency_startup_request=args.emergency_startup_request,
                resume_role=getattr(args, "resume_role", None),
            )
            attach_startup_request(window, startup_request)
            _connect_single_instance_requests(server, window, Qt, QTimer, logger)
            window.show()
            QTimer.singleShot(0, window.initialize)
            session["exit_code"] = app.exec()
    finally:
        if listening:
            server.close()
            QLocalServer.removeServer(server_name)


if __name__ == "__main__":
    main()

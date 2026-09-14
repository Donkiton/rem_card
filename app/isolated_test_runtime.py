"""Bootstrap injected into explicitly isolated test builds only.

Production entrypoints never call this module. No user-selected database path is
accepted: the test share and all local state belong to the extracted bundle.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile


PURPOSE = "RemCard emergency sandbox"


def test_share_name(root: Path) -> str:
    key = str(root.absolute()).rstrip("\\").lower()
    return "RemCardTest_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def sandbox_paths(root: Path) -> dict[str, str]:
    root = root.resolve()
    if str(root).startswith("\\\\"):
        raise RuntimeError("Распакуйте тестовую сборку на локальный диск.")
    marker = json.loads((root / "TEST_SANDBOX.json").read_text(encoding="utf-8-sig"))
    if marker.get("schema_version") != 1 or marker.get("purpose") != PURPOSE:
        raise RuntimeError("Повреждён маркер изолированной тестовой сборки.")
    paths = {
        "Database": root / "Database",
        "State": root / "State",
        "Emergency": root / "State" / "Emergency",
        "LocalAppData": root / "State" / "LocalAppData",
        "AppData": root / "State" / "AppData",
        "ProgramData": root / "State" / "ProgramData",
        "UserProfile": root / "State" / "UserProfile",
        "Temp": root / "State" / "Temp",
        "Logs": root / "State" / "Logs",
        "QtSettings": root / "State" / "QtSettings",
    }
    for path in paths.values():
        if not path.resolve().is_relative_to(root):
            raise RuntimeError("Тестовая папка перенаправлена за пределы сборки.")
    return {key: str(value) for key, value in paths.items()}


def user_profile_environment(profile: Path) -> dict[str, str]:
    """Redirect Python home-based preferences before importing application code."""
    profile = profile.resolve()
    return {
        "USERPROFILE": str(profile), "HOME": str(profile),
        "HOMEDRIVE": profile.drive, "HOMEPATH": str(profile)[len(profile.drive):],
    }


def isolate_qsettings(directory: str):
    from PySide6 import QtCore

    original = QtCore.QSettings
    original.setDefaultFormat(original.IniFormat)
    for scope in (original.UserScope, original.SystemScope):
        original.setPath(original.IniFormat, scope, directory)

    class TestSettings(original):
        def __init__(self, *args, **kwargs):
            # Qt's (organization, application) overload ALWAYS uses NativeFormat,
            # even after setDefaultFormat(IniFormat). Application imports must
            # receive the explicit-format overload in this test process.
            if len(args) >= 2 and isinstance(args[0], str) and isinstance(args[1], str):
                super().__init__(original.IniFormat, original.UserScope, *args, **kwargs)
            else:
                super().__init__(*args, **kwargs)

    QtCore.QSettings = TestSettings
    return TestSettings


def configure_test_runtime() -> None:
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Изолированный профиль предназначен для тестового EXE.")
    smoke = "--compiled-smoke" in sys.argv
    recovery_flags = [arg for arg in sys.argv[1:] if arg.startswith("--test-emergency-recovery-smoke=")]
    recovery_mode = recovery_flags[0].split("=", 1)[1] if recovery_flags else ""
    if recovery_flags:
        if len(recovery_flags) != 1 or recovery_mode not in {"owner", "peer"} or smoke:
            raise RuntimeError("Некорректный режим изолированной проверки интерфейса.")
        sys.argv.remove(recovery_flags[0])
    if smoke:
        root = Path(tempfile.mkdtemp(prefix="remcard-compiled-test-"))
        (root / "TEST_SANDBOX.json").write_text(
            json.dumps({"schema_version": 1, "purpose": PURPOSE}), encoding="utf-8"
        )
    else:
        root = Path(sys.executable).resolve().parent.parent
    paths = sandbox_paths(root)
    for key, path in paths.items():
        if key != "Database":
            Path(path).mkdir(parents=True, exist_ok=True)
    (Path(paths["UserProfile"]) / "Desktop").mkdir(exist_ok=True)
    # Ignore inherited production or development overrides in the test process.
    for key in list(os.environ):
        if key.startswith("REMCARD_"):
            os.environ.pop(key, None)
    share = test_share_name(root)
    network_root = "\\\\localhost\\" + share
    config_path = str(Path(paths["State"]) / "remcard_data_path.json")
    overrides = {
        **user_profile_environment(Path(paths["UserProfile"])),
        "REMCARD_BAZA_DIR": network_root,
        "REMCARD_DATA_PATH_CONFIG": config_path,
        "REMCARD_EMERGENCY_DB_ROOT": paths["Emergency"],
        "REMCARD_LOCAL_LOGS_DIR": paths["Logs"],
        "REMCARD_TEST_INSTANCE_NAMESPACE": str(root),
        "LOCALAPPDATA": paths["LocalAppData"],
        "APPDATA": paths["AppData"],
        "ProgramData": paths["ProgramData"],
        "TEMP": paths["Temp"],
        "TMP": paths["Temp"],
        # Keep the manual test short; all storage/merge behavior is unchanged.
        "REMCARD_EMERGENCY_STANDBY_COOLDOWN_SEC": "30",
        "REMCARD_EMERGENCY_STANDBY_FOREGROUND_IDLE_SEC": "5",
    }
    os.environ.update(overrides)
    tempfile.tempdir = None
    Path(config_path).write_text(json.dumps({"baza_dir": network_root}), encoding="utf-8")
    trace_path = Path(paths["Logs"]) / "test-bootstrap.log"
    trace_path.write_text("paths configured; importing QtCore\n", encoding="utf-8")
    if smoke:
        import atexit
        import faulthandler

        diagnostic = (Path(paths["Logs"]) / "smoke-stack.log").open("w", encoding="utf-8")
        atexit.register(diagnostic.close)
        atexit.register(faulthandler.cancel_dump_traceback_later)
        faulthandler.dump_traceback_later(12, file=diagnostic)
    QSettings = isolate_qsettings(paths["QtSettings"])
    with trace_path.open("a", encoding="utf-8") as trace:
        trace.write("QtSettings isolated; importing version\n")
    from rem_card.app import version

    version.APP_DISPLAY_TITLE += " — ТЕСТОВАЯ БАЗА"
    with trace_path.open("a", encoding="utf-8") as trace:
        trace.write("bootstrap completed\n")

    if "--test-profile-report" in sys.argv:
        from rem_card.app import main
        from rem_card.app.runtime_paths import resolve_baza_dir
        from rem_card.services.analytics.platform.core import SavedAnalyticsViewStore

        report = {
            "root": str(root), "paths": paths, "environment": overrides,
            "resolved_database": resolve_baza_dir(),
            "doctor_instance": main._single_instance_server_name("doctor"),
            "nurse_instance": main._single_instance_server_name("nurse"),
            "qt_settings_file": QSettings("MyHospital", "RemCard").fileName(),
            "user_home": str(Path.home()),
            "analytics_views_file": str(SavedAnalyticsViewStore().path),
        }
        (Path(paths["State"]) / "profile-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        raise SystemExit(0)
    if recovery_mode:
        from PySide6.QtWidgets import QApplication
        from rem_card.app.isolated_emergency_smoke import install_emergency_recovery_smoke

        application = QApplication.instance() or QApplication(sys.argv)
        globals()["_recovery_smoke_application"] = application
        install_emergency_recovery_smoke(application, mode=recovery_mode, root=root)
    if not smoke and (
        Path(sys.executable).stem.lower() not in {"remcard", "remcarddoctor", "remcardnurse"}
        or "--path-setup" in sys.argv
    ):
        from PySide6.QtWidgets import QApplication, QMessageBox

        application = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.information(
            None, "Тестовая RemCard",
            "Этот стенд предназначен для ролей врача и медсестры РАО.\n"
            "Управляйте доступом к ней через START_TEST.cmd.",
        )
        application.quit()
        raise SystemExit(0)

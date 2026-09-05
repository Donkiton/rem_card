"""Safety-сценарии: backup."""

from __future__ import annotations

from .common import PROJECT_ROOT
from pathlib import Path
import glob
import json
import os
import shutil
import time


def _check_local_replica_tmp_cleanup(temp_root: str) -> tuple[bool, str]:
    from .database import _create_sqlite_file
    from rem_card.app.local_replica_sync import LocalReplicaSync

    central_path = os.path.join(temp_root, "central_replica_source.db")
    local_path = os.path.join(temp_root, "local_replica_target.db")
    _create_sqlite_file(central_path)

    replica = LocalReplicaSync(
        central_db_path=central_path,
        local_db_path=local_path,
        sync_interval_sec=60.0,
    )
    ok = replica.sync_once()
    replica.stop()

    leftovers = glob.glob(f"{local_path}.sync_tmp.*")
    if leftovers:
        return False, f"temporary replica files were not cleaned: {leftovers[:3]}"
    if not ok:
        return False, "sync_once returned False"
    return True, "ok"


def _check_backup_cleanup_gating(temp_root: str) -> tuple[bool, str]:
    from .database import _create_sqlite_file
    from rem_card.app.backup_and_cleanup import _can_cleanup_old_backups

    db_path = os.path.join(temp_root, "cleanup_gate_primary.db")
    backup_dir = os.path.join(temp_root, "cleanup_gate_backups")
    os.makedirs(backup_dir, exist_ok=True)
    _create_sqlite_file(db_path)

    # No backups -> cleanup must be blocked.
    if _can_cleanup_old_backups(db_path, backup_dir):
        return False, "cleanup gate passed unexpectedly without healthy backups"

    healthy_backup = os.path.join(backup_dir, "healthy_backup.db")
    shutil.copy2(db_path, healthy_backup)
    if not _can_cleanup_old_backups(db_path, backup_dir):
        return False, "cleanup gate failed despite healthy backup"

    # Corrupt backup only -> cleanup must be blocked.
    os.remove(healthy_backup)
    corrupt_backup = os.path.join(backup_dir, "corrupt_backup.db")
    with open(corrupt_backup, "wb") as fh:
        fh.write(b"not_sqlite")
    if _can_cleanup_old_backups(db_path, backup_dir):
        return False, "cleanup gate passed unexpectedly with only corrupt backup"

    return True, "ok"


def _with_isolated_daily_backup_paths(temp_root: str):
    from rem_card.app import backup_and_cleanup
    from rem_card.app import paths as app_paths

    root = Path(temp_root)
    baza_dir = root / "Baza"
    archiv_dir = baza_dir / "archiv"
    report_dir = baza_dir / "report"
    backups_dir = baza_dir / "backups"
    valid_dir = backups_dir / "valid"
    health_dir = baza_dir / "backup_health"
    invalid_dir = health_dir / "invalid_backups"
    logs_dir = root / "logs"
    db_path = archiv_dir / "rao_journal.db"
    lock_path = archiv_dir / "db.lock"

    values = {
        "ARCHIV_DIR": str(archiv_dir),
        "REPORT_DIR": str(report_dir),
        "BACKUPS_RC_DIR": str(backups_dir),
        "BACKUPS_VALID_DIR": str(valid_dir),
        "BACKUP_HEALTH_DIR": str(health_dir),
        "INVALID_BACKUPS_DIR": str(invalid_dir),
        "LOGS_DIR": str(logs_dir),
        "REMCARD_DB_PATH": str(db_path),
        "DB_LOCK_PATH": str(lock_path),
    }
    saved_module = {name: getattr(backup_and_cleanup, name) for name in values}
    saved_paths = {
        "REPORT_DIR": app_paths.REPORT_DIR,
        "LOGS_DIR": app_paths.LOGS_DIR,
        "BAZA_LOGS_DIR": app_paths.BAZA_LOGS_DIR,
    }
    saved_ensure = backup_and_cleanup.ensure_directories

    def fake_ensure_directories():
        for directory in (
            baza_dir,
            archiv_dir,
            report_dir,
            backups_dir,
            valid_dir,
            health_dir,
            invalid_dir,
            logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    for name, value in values.items():
        setattr(backup_and_cleanup, name, value)
    app_paths.REPORT_DIR = str(report_dir)
    app_paths.LOGS_DIR = str(logs_dir)
    app_paths.BAZA_LOGS_DIR = str(logs_dir)
    backup_and_cleanup.ensure_directories = fake_ensure_directories

    def restore():
        for name, value in saved_module.items():
            setattr(backup_and_cleanup, name, value)
        for name, value in saved_paths.items():
            setattr(app_paths, name, value)
        backup_and_cleanup.ensure_directories = saved_ensure

    return backup_and_cleanup, {
        "baza_dir": baza_dir,
        "archiv_dir": archiv_dir,
        "report_dir": report_dir,
        "backups_dir": backups_dir,
        "valid_dir": valid_dir,
        "health_dir": health_dir,
        "invalid_dir": invalid_dir,
        "logs_dir": logs_dir,
        "db_path": db_path,
        "lock_path": lock_path,
    }, restore


def _check_daily_backup_does_not_catch_up_after_window(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from rem_card.services.user_reports import USER_REPORTS_DIR_NAME, UserReportsService

    backup_and_cleanup, paths, restore = _with_isolated_daily_backup_paths(temp_root)
    saved_now = backup_and_cleanup._network_now_or_local
    saved_create = backup_and_cleanup._create_safe_sqlite_backup
    calls: list[tuple[str, str]] = []
    try:
        backup_and_cleanup._network_now_or_local = lambda: (datetime(2026, 7, 9, 8, 0, 0), "regression")
        backup_and_cleanup._create_safe_sqlite_backup = lambda db_path, backup_path: calls.append((db_path, backup_path))

        first = backup_and_cleanup.perform_daily_backup_and_cleanup(role="doctor")
        second = backup_and_cleanup.perform_daily_backup_and_cleanup(role="doctor")

        if calls:
            return False, f"morning startup attempted backup after window: {calls}"
        if first.get("status") != "skipped" or first.get("reason") != "after_night_window":
            return False, f"unexpected first morning result: {first}"
        if second.get("status") != "skipped" or second.get("reason") != "after_night_window":
            return False, f"unexpected second morning result: {second}"

        service = UserReportsService(reports_root=paths["report_dir"] / USER_REPORTS_DIR_NAME, logs_dirs=[])
        reports = service.list_reports()
        if len(reports) != 1:
            return False, f"morning missed backup should create one deduped report, got {len(reports)}"
        report_text = str(reports[0].get("text") or "")
        expected_missing_dates = "Пропущенные даты: 2026-07-06, 2026-07-07, 2026-07-08"
        if (
            "не создавался минимум 3 суток подряд" not in report_text
            or "ночное окно" not in report_text
            or expected_missing_dates not in report_text
        ):
            return False, f"morning missed backup report has unexpected text: {report_text[:300]}"
        if list(paths["valid_dir"].glob("*.db")):
            return False, "morning missed backup created a DB file"
        return True, "ok"
    finally:
        backup_and_cleanup._network_now_or_local = saved_now
        backup_and_cleanup._create_safe_sqlite_backup = saved_create
        restore()


def _check_daily_backup_skips_when_primary_db_unavailable(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime

    from rem_card.services.user_reports import USER_REPORTS_DIR_NAME, UserReportsService

    backup_and_cleanup, paths, restore = _with_isolated_daily_backup_paths(temp_root)
    saved_now = backup_and_cleanup._network_now_or_local
    saved_create = backup_and_cleanup._create_safe_sqlite_backup
    calls: list[tuple[str, str]] = []
    try:
        paths["archiv_dir"].mkdir(parents=True, exist_ok=True)
        paths["db_path"].write_bytes(b"not sqlite")
        backup_and_cleanup._network_now_or_local = lambda: (datetime(2026, 7, 9, 3, 5, 0), "regression")
        backup_and_cleanup._create_safe_sqlite_backup = lambda db_path, backup_path: calls.append((db_path, backup_path))

        result = backup_and_cleanup.perform_daily_backup_and_cleanup(role="doctor")

        if calls:
            return False, f"backup started despite unavailable primary DB: {calls}"
        if result.get("status") != "primary_db_unavailable":
            return False, f"unexpected unavailable primary DB result: {result}"
        if list(paths["valid_dir"].glob("*.db")):
            return False, "unavailable primary DB created a backup file"
        backup_date = str(result.get("backup_date") or "")
        if backup_date:
            daily_paths = backup_and_cleanup._daily_backup_paths(backup_date)
            if os.path.exists(daily_paths["lock"]) or os.path.exists(daily_paths["reserved"]):
                return False, "unavailable primary DB left a daily backup reservation"
            if os.path.exists(daily_paths["done"]):
                return False, "unavailable primary DB marked backup as done"

        service = UserReportsService(reports_root=paths["report_dir"] / USER_REPORTS_DIR_NAME, logs_dirs=[])
        reports = service.list_reports()
        if len(reports) != 1:
            return False, f"unavailable primary DB should create one report, got {len(reports)}"
        report_text = str(reports[0].get("text") or "")
        expected_missing_dates = "Пропущенные даты: 2026-07-06, 2026-07-07, 2026-07-08"
        if (
            "основная база" not in report_text.lower()
            or "не создавался минимум 3 суток подряд" not in report_text
            or expected_missing_dates not in report_text
        ):
            return False, f"unavailable primary DB report has unexpected text: {report_text[:300]}"
        return True, "ok"
    finally:
        backup_and_cleanup._network_now_or_local = saved_now
        backup_and_cleanup._create_safe_sqlite_backup = saved_create
        restore()


def _check_manual_primary_db_backup_button_and_api(temp_root: str) -> tuple[bool, str]:
    from .database import _create_sqlite_file
    from rem_card.app.sqlite_shared import backup_meta_path, validate_sqlite_file

    admin_source = (PROJECT_ROOT / "ui" / "admin_view" / "admin_main_widget.py").read_text(encoding="utf-8")
    for marker in (
        "Создать бекап основной бд",
        "self.btn_backup_main_db.clicked.connect(self.create_main_db_backup)",
        "create_manual_primary_db_backup",
    ):
        if marker not in admin_source:
            return False, f"manual primary DB backup UI marker missing: {marker}"

    backup_and_cleanup, paths, restore = _with_isolated_daily_backup_paths(temp_root)
    try:
        paths["archiv_dir"].mkdir(parents=True, exist_ok=True)
        _create_sqlite_file(str(paths["db_path"]))

        backup_path = backup_and_cleanup.create_manual_primary_db_backup()
        if not os.path.isfile(backup_path):
            return False, f"manual primary DB backup file was not created: {backup_path}"
        if "_manual_" not in os.path.basename(backup_path):
            return False, f"manual primary DB backup name is not marked as manual: {backup_path}"
        ok, reason = validate_sqlite_file(backup_path)
        if not ok:
            return False, f"manual primary DB backup is invalid: {reason}"
        meta_path = backup_meta_path(backup_path)
        if not os.path.isfile(meta_path):
            return False, f"manual primary DB backup metadata missing: {meta_path}"
        with open(meta_path, "r", encoding="utf-8") as handle:
            meta = json.load(handle)
        if meta.get("backup_kind") != "primary_manual":
            return False, f"manual primary DB backup metadata kind is wrong: {meta}"
        if meta.get("source") != "manual_primary_db_backup":
            return False, f"manual primary DB backup metadata source is wrong: {meta}"
        return True, "ok"
    finally:
        restore()


def _check_backup_count_limit_enforcement(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.backup_and_cleanup import BACKUP_MAX_COUNT, _enforce_backup_limits

    backup_dir = os.path.join(temp_root, "count_limit_backups")
    os.makedirs(backup_dir, exist_ok=True)

    files_to_create = int(BACKUP_MAX_COUNT) + 9
    now = time.time()
    for idx in range(files_to_create):
        path = os.path.join(backup_dir, f"backup_{idx:03d}.db")
        with open(path, "wb") as fh:
            fh.write(b"sqlite-mock")
        # Чем меньше idx, тем старше файл.
        file_age_sec = float(files_to_create - idx) * 10.0
        ts = now - file_age_sec
        os.utime(path, (ts, ts))

    _enforce_backup_limits(backup_dir)

    remaining = [
        os.path.join(backup_dir, name)
        for name in os.listdir(backup_dir)
        if name.lower().endswith(".db")
    ]
    if len(remaining) > int(BACKUP_MAX_COUNT):
        return False, f"backup count cap not enforced: {len(remaining)} > {BACKUP_MAX_COUNT}"

    newest_name = f"backup_{files_to_create - 1:03d}.db"
    if not os.path.exists(os.path.join(backup_dir, newest_name)):
        return False, f"newest backup was removed unexpectedly: {newest_name}"

    oldest_name = "backup_000.db"
    if os.path.exists(os.path.join(backup_dir, oldest_name)):
        return False, f"oldest backup was not removed: {oldest_name}"

    return True, "ok"


def _check_report_cleanup_uses_creation_age(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime, timedelta

    from rem_card.app import backup_and_cleanup

    report_dir = os.path.join(temp_root, "reports")
    os.makedirs(report_dir, exist_ok=True)

    old_report = os.path.join(report_dir, "old_report.pdf")
    fresh_report = os.path.join(report_dir, "fresh_report.pdf")
    Path(old_report).write_bytes(b"old")
    Path(fresh_report).write_bytes(b"fresh")

    now = datetime(2026, 5, 9, 12, 0, 0)
    old_created_ts = (now - timedelta(days=8)).timestamp()
    fresh_created_ts = (now - timedelta(days=2)).timestamp()
    fresh_modified_ts = (now - timedelta(hours=1)).timestamp()
    os.utime(old_report, (fresh_modified_ts, fresh_modified_ts))
    os.utime(fresh_report, (fresh_modified_ts, fresh_modified_ts))

    original_getctime = backup_and_cleanup.os.path.getctime
    try:
        old_abs = os.path.abspath(old_report)
        fresh_abs = os.path.abspath(fresh_report)

        def fake_getctime(path):
            abs_path = os.path.abspath(path)
            if abs_path == old_abs:
                return old_created_ts
            if abs_path == fresh_abs:
                return fresh_created_ts
            return original_getctime(path)

        backup_and_cleanup.os.path.getctime = fake_getctime
        backup_and_cleanup._cleanup_old_report_files(report_dir, now - timedelta(days=7))
    finally:
        backup_and_cleanup.os.path.getctime = original_getctime

    if os.path.exists(old_report):
        return False, "old report was not removed by creation age"
    if not os.path.exists(fresh_report):
        return False, "fresh report was removed unexpectedly"
    return True, "ok"


def _check_runtime_backup_rotation_scans_valid_dir(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.paths import BACKUPS_RC_DIR, BACKUPS_VALID_DIR
    from rem_card.data.dao import db_manager as rem_db_manager

    valid_root = os.path.normcase(os.path.abspath(BACKUPS_VALID_DIR))
    isolated_baza_dir = os.environ.get("REMCARD_BAZA_DIR") or temp_root
    isolated_root = os.path.normcase(os.path.abspath(isolated_baza_dir))
    if not valid_root.startswith(isolated_root):
        return False, f"backup test path is not isolated: {BACKUPS_VALID_DIR}"

    def prepare_files(prefix: str, count: int):
        shutil.rmtree(BACKUPS_RC_DIR, ignore_errors=True)
        os.makedirs(BACKUPS_VALID_DIR, exist_ok=True)
        now = time.time()
        for idx in range(count):
            path = os.path.join(BACKUPS_VALID_DIR, f"{prefix}_{idx:03d}.db")
            with open(path, "wb") as fh:
                fh.write(b"sqlite-mock")
            with open(f"{path}.meta.json", "w", encoding="utf-8") as fh:
                json.dump({"idx": idx}, fh)
            ts = now - float(count - idx)
            os.utime(path, (ts, ts))
            os.utime(f"{path}.meta.json", (ts, ts))

    rem_limit = int(rem_db_manager.MAX_RUNTIME_BACKUPS)
    prepare_files("shutdown_remcard_regression", rem_limit + 2)
    rem_instance = rem_db_manager.DatabaseManager.__new__(rem_db_manager.DatabaseManager)
    rem_db_manager.DatabaseManager._rotate_backups(rem_instance)
    rem_remaining = sorted(
        name for name in os.listdir(BACKUPS_VALID_DIR) if name.endswith(".db")
    )
    if len(rem_remaining) > rem_limit:
        return False, f"remcard runtime backup cap not enforced in valid dir: {len(rem_remaining)} > {rem_limit}"
    if os.path.exists(os.path.join(BACKUPS_VALID_DIR, "shutdown_remcard_regression_000.db")):
        return False, "oldest remcard runtime backup was not removed from valid dir"
    if os.path.exists(os.path.join(BACKUPS_VALID_DIR, "shutdown_remcard_regression_000.db.meta.json")):
        return False, "oldest remcard runtime backup metadata was not removed"
    if not os.path.exists(os.path.join(BACKUPS_VALID_DIR, f"shutdown_remcard_regression_{rem_limit + 1:03d}.db")):
        return False, "newest remcard runtime backup was removed unexpectedly"

    return True, "ok"

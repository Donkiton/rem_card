"""Safety-сценарии: settings."""

from __future__ import annotations

from typing import Any
from .common import PROJECT_ROOT
from pathlib import Path
import glob
import json
import os
import shutil
import sqlite3
import time
import uuid


def _check_settings_db_path_is_network_data_root_settings_folder(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.settings_db_paths import get_settings_db_path, get_settings_dir, get_settings_lock_path

    baza_dir = os.path.join(temp_root, "Baza")
    db_path = os.path.abspath(get_settings_db_path(baza_dir))
    settings_dir = os.path.abspath(get_settings_dir(baza_dir))
    lock_path = os.path.abspath(get_settings_lock_path(baza_dir))
    expected_db = os.path.join(settings_dir, "remcard_settings.db")
    if db_path != os.path.abspath(expected_db):
        return False, f"settings DB path mismatch: {db_path}"
    if os.path.basename(os.path.dirname(db_path)).lower() != "settings":
        return False, f"settings DB must be in settings folder: {db_path}"
    if "archiv" in Path(db_path).parts:
        return False, f"settings DB must not be inside archiv: {db_path}"
    if os.path.basename(lock_path) != "settings.db.lock" or os.path.dirname(lock_path) != settings_dir:
        return False, f"settings lock path mismatch: {lock_path}"
    return True, "ok"


def _check_settings_db_schema_and_no_wal(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.data.settings.settings_schema import REQUIRED_TABLES
    from rem_card.services.settings.settings_service import SettingsService

    baza_dir = os.path.join(temp_root, "Baza")
    service = SettingsService(SettingsDatabase(baza_dir=baza_dir))
    info = service.ensure_ready()
    db_path = str(info.get("settings_db_path") or "")
    if not db_path.endswith(os.path.join("settings", "remcard_settings.db")):
        return False, f"unexpected settings DB path: {db_path}"
    if not os.path.isfile(db_path):
        return False, "settings DB was not created"
    conn = service.db.connect(readonly=False)
    try:
        journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        synchronous = int(conn.execute("PRAGMA synchronous").fetchone()[0])
        mmap_size = int(conn.execute("PRAGMA mmap_size").fetchone()[0])
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    finally:
        conn.close()
    if journal_mode == "wal":
        return False, "settings DB must not use WAL"
    if journal_mode != "delete":
        return False, f"settings DB journal_mode should be DELETE, got {journal_mode}"
    if synchronous < 3:
        return False, f"settings DB synchronous must remain EXTRA, got {synchronous}"
    if mmap_size != 0:
        return False, f"settings DB mmap_size must be 0, got {mmap_size}"
    missing = sorted(set(REQUIRED_TABLES) - tables)
    if missing:
        return False, f"settings DB missing tables: {missing}"
    return True, "ok"


def _check_settings_manual_backup_only(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.settings_db_paths import get_settings_backup_dir
    from rem_card.app.sqlite_shared import backup_meta_path
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.services.settings.settings_service import DISPLAY_SETTINGS_KEY, SettingsService

    baza_dir = os.path.join(temp_root, "Baza")
    service = SettingsService(SettingsDatabase(baza_dir=baza_dir))
    service.ensure_ready()
    backup_dir = get_settings_backup_dir(baza_dir)
    before_pre = set(glob.glob(os.path.join(backup_dir, "settings_pre_*.db")))
    before_manual = set(glob.glob(os.path.join(backup_dir, "settings_manual_*.db")))
    first_marker = f"settings_manual_backup_first_{os.getpid()}"
    second_marker = f"settings_manual_backup_second_{os.getpid()}"
    service.set_app_setting(
        "shared",
        "display_settings",
        {"probe": first_marker},
        catalog_key=DISPLAY_SETTINGS_KEY,
        entity_type="display_settings",
        operation="update",
    )
    service.set_app_setting(
        "shared",
        "display_settings",
        {"probe": second_marker},
        catalog_key=DISPLAY_SETTINGS_KEY,
        entity_type="display_settings",
        operation="update",
    )
    after_pre_write = set(glob.glob(os.path.join(backup_dir, "settings_pre_*.db")))
    after_manual_write = set(glob.glob(os.path.join(backup_dir, "settings_manual_*.db")))
    if after_pre_write != before_pre:
        return False, "ordinary settings writes must not create automatic settings_pre backups"
    if after_manual_write != before_manual:
        return False, "ordinary settings writes must not create manual settings backups"

    manual_path = service.create_manual_settings_backup()
    after_manual = set(glob.glob(os.path.join(backup_dir, "settings_manual_*.db")))
    created = sorted(after_manual - before_manual)
    if len(created) != 1 or os.path.abspath(created[0]) != os.path.abspath(manual_path):
        return False, f"expected one manual settings backup, got {len(created)}"

    latest_backup = manual_path
    meta_path = backup_meta_path(latest_backup)
    if not os.path.isfile(meta_path):
        return False, f"missing backup metadata: {meta_path}"
    meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
    if meta.get("quick_check") != "ok" or meta.get("integrity_check") != "ok":
        return False, f"backup was not validated: {meta}"
    if meta.get("backup_kind") != "settings_manual":
        return False, f"manual backup metadata lost backup kind: {meta}"

    with sqlite3.connect(latest_backup) as backup_conn:
        row = backup_conn.execute(
            "SELECT value_json FROM app_settings WHERE scope = ? AND key = ?",
            ("shared", "display_settings"),
        ).fetchone()
    raw_value = str(row[0] if row else "")
    if first_marker in raw_value:
        return False, "manual backup should contain the current state, not stale first write"
    if second_marker not in raw_value:
        return False, "manual backup did not contain current settings state"

    current = service.get_app_setting("shared", "display_settings", default={})
    if not isinstance(current, dict) or current.get("probe") != second_marker:
        return False, "live settings DB did not commit the second write"
    return True, "ok"


def _check_sqlite_backup_handles_long_metadata_path(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.sqlite_shared import backup_connection, backup_meta_path, validate_sqlite_file

    backup_dir = os.path.join(temp_root, "b")
    os.makedirs(backup_dir, exist_ok=True)
    prefix = "settings_pre_"
    suffix = ".db"
    backup_dir_abs = os.path.abspath(backup_dir)
    target_backup_len = 248
    filler_len = target_backup_len - len(backup_dir_abs) - 1 - len(prefix) - len(suffix)
    if filler_len < 1:
        return False, f"test backup directory is too long for this regression: {len(backup_dir_abs)}"
    backup_name = prefix + ("x" * filler_len) + suffix
    backup_path = os.path.join(backup_dir, backup_name)

    old_temp_path = f"{backup_path}.12345678.tmp"
    if len(os.path.abspath(backup_path)) >= 260:
        return False, f"backup path unexpectedly exceeds Windows limit: {len(os.path.abspath(backup_path))}"
    if len(os.path.abspath(old_temp_path)) <= 260:
        return False, f"regression path is not long enough: {len(os.path.abspath(old_temp_path))}"

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE probe(id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO probe(value) VALUES ('ok')")
        conn.commit()
        created_path = backup_connection(
            conn,
            backup_path,
            invalid_dir=os.path.join(backup_dir, "invalid"),
            validate=True,
            source="long_metadata_path_regression",
        )
    finally:
        conn.close()

    if created_path != backup_path or not os.path.isfile(backup_path):
        return False, f"backup was not created at requested path: {created_path}"
    ok, reason = validate_sqlite_file(backup_path)
    if not ok:
        return False, f"long-path backup is not valid: {reason}"
    meta_path = backup_meta_path(backup_path)
    if meta_path == f"{backup_path}.meta.json":
        return False, "long backup metadata did not use shortened meta path"
    if not os.path.isfile(meta_path):
        return False, f"shortened metadata file missing: {meta_path}"
    leftovers = [name for name in os.listdir(backup_dir) if name.startswith(".backup_") and name.endswith(".tmp")]
    if leftovers:
        return False, f"temporary backup files were left behind: {leftovers}"
    return True, "ok"


def _check_settings_backup_cleanup_rotates_old_files(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.settings_db_paths import get_settings_backup_dir
    from rem_card.app.sqlite_shared import backup_connection, backup_meta_path
    from rem_card.data.settings.settings_db import SETTINGS_BACKUP_MAX_COUNT, SettingsDatabase
    from rem_card.services.settings.settings_service import SettingsService

    baza_dir = os.path.join(temp_root, "Baza")
    db = SettingsDatabase(baza_dir=baza_dir)
    service = SettingsService(db)
    service.ensure_ready()
    backup_dir = get_settings_backup_dir(baza_dir)
    os.makedirs(backup_dir, exist_ok=True)

    files_to_create = int(SETTINGS_BACKUP_MAX_COUNT) + 6
    now = time.time()
    conn = db.connect(readonly=True)
    try:
        for idx in range(files_to_create):
            path = os.path.join(backup_dir, f"settings_pre_regression_{idx:03d}.db")
            backup_connection(
                conn,
                path,
                invalid_dir=os.path.join(backup_dir, "invalid"),
                validate=True,
                source="settings_backup_cleanup_regression",
            )
            file_age_sec = float(files_to_create - idx) * 10.0
            ts = now - file_age_sec
            os.utime(path, (ts, ts))
            meta_path = backup_meta_path(path)
            if os.path.exists(meta_path):
                os.utime(meta_path, (ts, ts))
    finally:
        conn.close()

    oldest = os.path.join(backup_dir, "settings_pre_regression_000.db")
    oldest_meta = backup_meta_path(oldest)
    newest = os.path.join(backup_dir, f"settings_pre_regression_{files_to_create - 1:03d}.db")

    conn = db.connect(readonly=True)
    try:
        db._cleanup_settings_backups(conn)
    finally:
        conn.close()

    remaining = [
        name
        for name in os.listdir(backup_dir)
        if name.startswith("settings_pre_regression_") and name.lower().endswith(".db")
    ]
    if len(remaining) > int(SETTINGS_BACKUP_MAX_COUNT):
        return False, f"settings backup cleanup kept too many files: {len(remaining)} > {SETTINGS_BACKUP_MAX_COUNT}"
    if os.path.exists(oldest):
        return False, "oldest settings pre-write backup was not removed"
    if os.path.exists(oldest_meta):
        return False, "oldest settings pre-write backup metadata was not removed"
    if not os.path.exists(newest):
        return False, "newest settings pre-write backup was removed unexpectedly"
    return True, "ok"


def _settings_last_migration_at(db) -> str:
    with db.read_connection() as conn:
        row = conn.execute(
            "SELECT value FROM settings_meta WHERE key = 'last_migration_at'"
        ).fetchone()
    return str(row[0] if row else "")


def _settings_pre_write_backups(baza_dir: str) -> set[str]:
    from rem_card.app.settings_db_paths import get_settings_backup_dir

    return set(glob.glob(os.path.join(get_settings_backup_dir(baza_dir), "settings_pre_*.db")))


def _check_settings_schema_fastpath_current_schema_no_write(temp_root: str) -> tuple[bool, str]:
    import rem_card.data.settings.settings_db as settings_db_module
    from rem_card.data.settings import settings_schema
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.services.settings.settings_service import SettingsService

    baza_dir = os.path.join(temp_root, "Baza")
    service = SettingsService(SettingsDatabase(baza_dir=baza_dir))
    service.ensure_ready()
    before_last_migration = _settings_last_migration_at(service.db)
    before_backups = _settings_pre_write_backups(baza_dir)

    calls = {"apply_schema": 0, "integrity": 0}
    original_apply_schema = settings_schema.apply_schema
    original_integrity = settings_db_module.run_integrity_check

    def counted_apply_schema(conn):
        calls["apply_schema"] += 1
        return original_apply_schema(conn)

    def counted_integrity(conn):
        calls["integrity"] += 1
        return original_integrity(conn)

    try:
        settings_schema.apply_schema = counted_apply_schema
        settings_db_module.run_integrity_check = counted_integrity
        restarted_db = SettingsDatabase(baza_dir=baza_dir)
        info = restarted_db.ensure_ready()
    finally:
        settings_schema.apply_schema = original_apply_schema
        settings_db_module.run_integrity_check = original_integrity

    after_last_migration = _settings_last_migration_at(SettingsDatabase(baza_dir=baza_dir))
    after_backups = _settings_pre_write_backups(baza_dir)
    if not info.get("settings_schema_fastpath_used"):
        return False, f"current schema did not use fastpath: {info}"
    if calls["apply_schema"] != 0:
        return False, "fastpath called settings_schema.apply_schema"
    if calls["integrity"] != 0:
        return False, "fastpath ran post-write integrity_check"
    if before_last_migration != after_last_migration:
        return False, "fastpath changed settings_meta.last_migration_at"
    if after_backups != before_backups:
        return False, "fastpath created settings_pre backup"
    return True, "ok"


def _check_settings_schema_missing_table_uses_migration_path(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.settings import settings_schema
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.services.settings.settings_service import SettingsService

    baza_dir = os.path.join(temp_root, "Baza")
    service = SettingsService(SettingsDatabase(baza_dir=baza_dir))
    service.ensure_ready()
    conn = service.db.connect(readonly=False)
    try:
        conn.execute("DROP TABLE diet_templates")
    finally:
        conn.close()

    calls = {"apply_schema": 0}
    original_apply_schema = settings_schema.apply_schema

    def counted_apply_schema(conn):
        calls["apply_schema"] += 1
        return original_apply_schema(conn)

    try:
        settings_schema.apply_schema = counted_apply_schema
        info = SettingsDatabase(baza_dir=baza_dir).ensure_ready()
    finally:
        settings_schema.apply_schema = original_apply_schema

    with SettingsDatabase(baza_dir=baza_dir).read_connection() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'diet_templates'"
        ).fetchone()
    if info.get("settings_schema_fastpath_used"):
        return False, "missing table incorrectly used fastpath"
    if calls["apply_schema"] <= 0:
        return False, "missing table did not call apply_schema"
    if not row:
        return False, "missing table was not recreated"
    return True, "ok"


def _check_settings_schema_outdated_version_uses_migration_path(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.settings import settings_schema
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.services.settings.settings_service import SettingsService

    baza_dir = os.path.join(temp_root, "Baza")
    service = SettingsService(SettingsDatabase(baza_dir=baza_dir))
    service.ensure_ready()
    conn = service.db.connect(readonly=False)
    try:
        conn.execute(
            "UPDATE settings_meta SET value = '0' WHERE key = ?",
            (settings_schema.SCHEMA_VERSION_KEY,),
        )
    finally:
        conn.close()

    calls = {"apply_schema": 0}
    original_apply_schema = settings_schema.apply_schema

    def counted_apply_schema(conn):
        calls["apply_schema"] += 1
        return original_apply_schema(conn)

    try:
        settings_schema.apply_schema = counted_apply_schema
        info = SettingsDatabase(baza_dir=baza_dir).ensure_ready()
    finally:
        settings_schema.apply_schema = original_apply_schema

    with SettingsDatabase(baza_dir=baza_dir).read_connection() as conn:
        version = settings_schema.get_schema_version(conn)
    if info.get("settings_schema_fastpath_used"):
        return False, "outdated schema_version incorrectly used fastpath"
    if calls["apply_schema"] <= 0:
        return False, "outdated schema_version did not call apply_schema"
    if version != settings_schema.SCHEMA_VERSION:
        return False, f"schema_version was not repaired: {version}"
    return True, "ok"


def _check_settings_schema_first_init_and_second_fastpath(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.data.settings.settings_schema import REQUIRED_CATALOG_KEYS, REQUIRED_TABLES
    from rem_card.services.settings.settings_service import SettingsService

    baza_dir = os.path.join(temp_root, "Baza")
    service = SettingsService(SettingsDatabase(baza_dir=baza_dir))
    first_info = service.ensure_ready()
    if not first_info.get("settings_db_created"):
        return False, f"first init did not create settings DB: {first_info}"
    with service.db.read_connection() as conn:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        catalog_keys = {
            str(row[0])
            for row in conn.execute("SELECT catalog_key FROM settings_catalog_versions").fetchall()
        }
    missing_tables = sorted(set(REQUIRED_TABLES) - tables)
    if missing_tables:
        return False, f"first init missing tables: {missing_tables}"
    missing_catalogs = sorted(set(REQUIRED_CATALOG_KEYS) - catalog_keys)
    if missing_catalogs:
        return False, f"first import missing catalog versions: {missing_catalogs}"

    before_last_migration = _settings_last_migration_at(service.db)
    second_info = SettingsDatabase(baza_dir=baza_dir).ensure_ready()
    after_last_migration = _settings_last_migration_at(SettingsDatabase(baza_dir=baza_dir))
    if not second_info.get("settings_schema_fastpath_used"):
        return False, f"second ensure_ready did not use fastpath: {second_info}"
    if before_last_migration != after_last_migration:
        return False, "second ensure_ready changed last_migration_at"
    return True, "ok"


def _check_settings_schema_invalid_marker_uses_migration_path(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.settings import settings_schema
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.services.settings.settings_service import SettingsService

    baza_dir = os.path.join(temp_root, "Baza")
    service = SettingsService(SettingsDatabase(baza_dir=baza_dir))
    service.ensure_ready()
    conn = service.db.connect(readonly=False)
    try:
        conn.execute(
            "UPDATE settings_meta SET value = 'not-an-int' WHERE key = ?",
            (settings_schema.SCHEMA_VERSION_KEY,),
        )
    finally:
        conn.close()

    calls = {"apply_schema": 0}
    original_apply_schema = settings_schema.apply_schema

    def counted_apply_schema(conn):
        calls["apply_schema"] += 1
        return original_apply_schema(conn)

    try:
        settings_schema.apply_schema = counted_apply_schema
        info = SettingsDatabase(baza_dir=baza_dir).ensure_ready()
    finally:
        settings_schema.apply_schema = original_apply_schema

    with SettingsDatabase(baza_dir=baza_dir).read_connection() as conn:
        version = settings_schema.get_schema_version(conn)
    if info.get("settings_schema_fastpath_used"):
        return False, "invalid schema marker incorrectly used fastpath"
    if calls["apply_schema"] <= 0:
        return False, "invalid schema marker did not call apply_schema"
    if version != settings_schema.SCHEMA_VERSION:
        return False, f"invalid schema marker was not repaired: {version}"
    return True, "ok"


def _check_settings_schema_locked_write_is_controlled_error(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.sqlite_shared import FileWriteLock
    from rem_card.data.settings import settings_schema
    from rem_card.data.settings.settings_db import SettingsDatabase, SettingsDbError
    from rem_card.services.settings.settings_service import SettingsService

    baza_dir = os.path.join(temp_root, "Baza")
    service = SettingsService(SettingsDatabase(baza_dir=baza_dir))
    service.ensure_ready()
    conn = service.db.connect(readonly=False)
    try:
        conn.execute(
            "UPDATE settings_meta SET value = '0' WHERE key = ?",
            (settings_schema.SCHEMA_VERSION_KEY,),
        )
    finally:
        conn.close()

    lock = FileWriteLock(service.db.lock_path, stale_timeout_sec=60)
    if not lock.acquire(owner_id="settings_schema_locked_regression", source="regression"):
        return False, "test setup failed: could not acquire settings write lock"
    try:
        db = SettingsDatabase(baza_dir=baza_dir)
        db.write_controller.max_retries = 2
        try:
            db.ensure_ready()
        except SettingsDbError as exc:
            if "БД настроек временно занята" not in str(exc):
                return False, f"unexpected locked settings error: {exc}"
            return True, "ok"
        return False, "locked settings schema write unexpectedly succeeded"
    finally:
        lock.release()


def _check_edited_drug_catalog_preserved_after_restart(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.services.settings.settings_service import SettingsService

    overrides_path = PROJECT_ROOT / "data" / "dictionaries" / "user_overrides.json"
    overrides = json.loads(overrides_path.read_text(encoding="utf-8"))
    edited_drugs = {
        key: value
        for key, value in (overrides.get("drugs") or {}).items()
        if isinstance(value, dict) and not value.get("_deleted")
    }
    if not edited_drugs:
        return False, "no edited drug overrides found"
    expected_key = sorted(edited_drugs)[0]
    expected_payload = edited_drugs[expected_key]

    baza_dir = os.path.join(temp_root, "Baza")
    db = SettingsDatabase(baza_dir=baza_dir)
    service = SettingsService(db)
    service.ensure_ready()
    first_payload = service.load_prescription_datasets()["drugs"].get(expected_key)
    if first_payload != expected_payload:
        return False, f"edited drug override was not imported for {expected_key}"

    restarted = SettingsService(SettingsDatabase(baza_dir=baza_dir))
    restarted.ensure_ready()
    second_payload = restarted.load_prescription_datasets()["drugs"].get(expected_key)
    if second_payload != expected_payload:
        return False, f"edited drug override was not preserved after restart for {expected_key}"
    report = restarted.get_import_report()
    if int((report.get("counts") or {}).get("drugs") or 0) < len(edited_drugs):
        return False, "settings import report does not include edited drug catalog"
    return True, f"preserved {expected_key}"


def _check_compiled_bundled_overrides_repair_seed_only_settings_db(temp_root: str) -> tuple[bool, str]:
    from rem_card.app import paths as app_paths
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.services.settings.settings_service import SettingsService

    source_dir = PROJECT_ROOT / "data" / "dictionaries"
    source_overrides = json.loads((source_dir / "user_overrides.json").read_text(encoding="utf-8"))
    source_seed_drugs = json.loads((source_dir / "drugs.seed.json").read_text(encoding="utf-8"))
    edited_drugs = source_overrides.get("drugs") or {}
    expected_key = ""
    expected_payload: dict[str, object] = {}
    for key, payload in edited_drugs.items():
        if not isinstance(payload, dict) or payload.get("_deleted"):
            continue
        if source_seed_drugs.get(key) != payload:
            expected_key = str(key)
            expected_payload = dict(payload)
            break
    if not expected_key:
        return False, "no drug override differs from seed"

    compiled_root = Path(temp_root) / "compiled" / "Prog"
    bundled_dict_dir = compiled_root / "_internal" / "rem_card" / "data" / "dictionaries"
    external_dict_dir = compiled_root / "rem_card" / "data" / "dictionaries"
    bundled_dict_dir.mkdir(parents=True, exist_ok=True)
    external_dict_dir.mkdir(parents=True, exist_ok=True)
    for source_path in source_dir.glob("*.json"):
        if source_path.name == "user_overrides.json":
            continue
        shutil.copy2(source_path, bundled_dict_dir / source_path.name)

    baza_dir = os.path.join(temp_root, "Baza")
    original_seed_dir = app_paths.SEED_DIR
    original_user_dict_dir = app_paths.USER_DICT_DIR
    original_baza_dir = app_paths.BAZA_DIR
    original_get_resources_dir = app_paths.get_resources_dir
    original_get_executable_dir = app_paths.get_executable_dir
    try:
        app_paths.SEED_DIR = str(bundled_dict_dir)
        app_paths.USER_DICT_DIR = str(external_dict_dir)
        app_paths.BAZA_DIR = baza_dir
        app_paths.get_resources_dir = lambda: str(compiled_root / "_internal")
        app_paths.get_executable_dir = lambda: str(compiled_root)

        seed_only = SettingsService(SettingsDatabase(baza_dir=baza_dir))
        seed_only.ensure_ready()
        seed_payload = seed_only.load_prescription_datasets()["drugs"].get(expected_key)
        if seed_payload == expected_payload:
            return False, "test setup failed: seed-only import already used user_overrides"

        shutil.copy2(source_dir / "user_overrides.json", bundled_dict_dir / "user_overrides.json")
        repaired = SettingsService(SettingsDatabase(baza_dir=baza_dir))
        repaired.ensure_ready()
        repaired_payload = repaired.load_prescription_datasets()["drugs"].get(expected_key)
        if repaired_payload != expected_payload:
            return False, f"compiled bundled user_overrides were not applied for {expected_key}"

        with repaired.db.read_connection() as conn:
            row = conn.execute("SELECT source FROM drugs WHERE code = ?", (expected_key,)).fetchone()
            meta = conn.execute(
                "SELECT value FROM settings_meta WHERE key = 'prescription_legacy_override_import_hash'"
            ).fetchone()
        if not row or str(row["source"]) != "override":
            return False, f"repaired drug source should be override, got {dict(row) if row else None}"
        if not meta or not str(meta["value"]):
            return False, "legacy override import hash meta was not stored"
        return True, f"repaired {expected_key}"
    finally:
        app_paths.SEED_DIR = original_seed_dir
        app_paths.USER_DICT_DIR = original_user_dict_dir
        app_paths.BAZA_DIR = original_baza_dir
        app_paths.get_resources_dir = original_get_resources_dir
        app_paths.get_executable_dir = original_get_executable_dir


def _check_compiled_external_dictionary_json_does_not_shadow_settings_seed(temp_root: str) -> tuple[bool, str]:
    from rem_card.app import paths as app_paths
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.services.settings.settings_service import SettingsService

    source_dir = PROJECT_ROOT / "data" / "dictionaries"
    source_overrides = json.loads((source_dir / "user_overrides.json").read_text(encoding="utf-8"))
    source_seed_drugs = json.loads((source_dir / "drugs.seed.json").read_text(encoding="utf-8"))
    expected_key = ""
    expected_seed_payload: dict[str, object] = {}
    stale_external_payload: dict[str, object] = {}
    for key, payload in (source_overrides.get("drugs") or {}).items():
        if not isinstance(payload, dict) or payload.get("_deleted"):
            continue
        seed_payload = source_seed_drugs.get(key)
        if isinstance(seed_payload, dict) and seed_payload != payload:
            expected_key = str(key)
            expected_seed_payload = dict(seed_payload)
            stale_external_payload = dict(payload)
            break
    if not expected_key:
        return False, "no drug override differs from seed"

    compiled_root = Path(temp_root) / "compiled_external_shadow" / "Prog"
    bundled_dict_dir = compiled_root / "_internal" / "rem_card" / "data" / "dictionaries"
    bundled_settings_dir = compiled_root / "_internal" / "rem_card" / "settings" / "display_settings"
    external_dict_dir = compiled_root / "rem_card" / "data" / "dictionaries"
    external_settings_dir = compiled_root / "settings" / "display_settings"
    bundled_dict_dir.mkdir(parents=True, exist_ok=True)
    bundled_settings_dir.mkdir(parents=True, exist_ok=True)
    external_dict_dir.mkdir(parents=True, exist_ok=True)
    external_settings_dir.mkdir(parents=True, exist_ok=True)
    for source_path in source_dir.glob("*.json"):
        if source_path.name == "user_overrides.json":
            continue
        shutil.copy2(source_path, bundled_dict_dir / source_path.name)
    (external_dict_dir / "user_overrides.json").write_text(
        json.dumps({"drugs": {expected_key: stale_external_payload}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (bundled_settings_dir / "display_settings.json").write_text(
        json.dumps({"source_marker": "bundled_settings"}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (external_settings_dir / "display_settings.json").write_text(
        json.dumps({"source_marker": "external_settings"}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    baza_dir = os.path.join(temp_root, "BazaExternalJsonIgnored")
    original_seed_dir = app_paths.SEED_DIR
    original_user_dict_dir = app_paths.USER_DICT_DIR
    original_baza_dir = app_paths.BAZA_DIR
    original_get_resources_dir = app_paths.get_resources_dir
    original_get_executable_dir = app_paths.get_executable_dir
    try:
        app_paths.SEED_DIR = str(bundled_dict_dir)
        app_paths.USER_DICT_DIR = str(external_dict_dir)
        app_paths.BAZA_DIR = baza_dir
        app_paths.get_resources_dir = lambda: str(compiled_root / "_internal")
        app_paths.get_executable_dir = lambda: str(compiled_root)

        service = SettingsService(SettingsDatabase(baza_dir=baza_dir))
        service.ensure_ready()
        payload = service.load_prescription_datasets()["drugs"].get(expected_key)
        if payload != expected_seed_payload:
            return False, f"external JSON next to exe shadowed settings seed for {expected_key}"
        display_settings = service.get_app_setting("shared", "display_settings", default={})
        if not isinstance(display_settings, dict) or display_settings.get("source_marker") != "bundled_settings":
            return False, "external settings JSON next to exe shadowed bundled settings seed"
        with service.db.read_connection() as conn:
            meta = conn.execute(
                "SELECT value FROM settings_meta WHERE key = 'prescription_legacy_override_import_hash'"
            ).fetchone()
        if meta:
            return False, "external user_overrides.json was treated as imported legacy override"
        return True, f"ignored {external_dict_dir / 'user_overrides.json'}"
    finally:
        app_paths.SEED_DIR = original_seed_dir
        app_paths.USER_DICT_DIR = original_user_dict_dir
        app_paths.BAZA_DIR = original_baza_dir
        app_paths.get_resources_dir = original_get_resources_dir
        app_paths.get_executable_dir = original_get_executable_dir


def _check_legacy_overrides_do_not_reapply_after_settings_import(temp_root: str) -> tuple[bool, str]:
    from rem_card.app import paths as app_paths
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.services.settings.settings_service import SettingsService

    source_dir = PROJECT_ROOT / "data" / "dictionaries"
    source_overrides = json.loads((source_dir / "user_overrides.json").read_text(encoding="utf-8"))
    first_overrides = json.loads(json.dumps(source_overrides, ensure_ascii=False))
    first_overrides.setdefault("drugs", {}).setdefault("dopamine", {})["default_dilution"] = {
        "base": "nacl_09",
        "volume": 10,
    }
    second_overrides = json.loads(json.dumps(source_overrides, ensure_ascii=False))
    second_overrides.setdefault("drugs", {}).setdefault("dopamine", {})["default_dilution"] = {
        "base": "nacl_09",
        "volume": 15,
    }

    compiled_root = Path(temp_root) / "compiled" / "Prog"
    bundled_dict_dir = compiled_root / "_internal" / "rem_card" / "data" / "dictionaries"
    external_dict_dir = compiled_root / "rem_card" / "data" / "dictionaries"
    bundled_dict_dir.mkdir(parents=True, exist_ok=True)
    external_dict_dir.mkdir(parents=True, exist_ok=True)
    for source_path in source_dir.glob("*.json"):
        shutil.copy2(source_path, bundled_dict_dir / source_path.name)
    (bundled_dict_dir / "user_overrides.json").write_text(
        json.dumps(first_overrides, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    baza_dir = os.path.join(temp_root, "BazaLegacyOverrideNoReapply")
    original_seed_dir = app_paths.SEED_DIR
    original_user_dict_dir = app_paths.USER_DICT_DIR
    original_baza_dir = app_paths.BAZA_DIR
    original_get_resources_dir = app_paths.get_resources_dir
    original_get_executable_dir = app_paths.get_executable_dir
    try:
        app_paths.SEED_DIR = str(bundled_dict_dir)
        app_paths.USER_DICT_DIR = str(external_dict_dir)
        app_paths.BAZA_DIR = baza_dir
        app_paths.get_resources_dir = lambda: str(compiled_root / "_internal")
        app_paths.get_executable_dir = lambda: str(compiled_root)

        first_service = SettingsService(SettingsDatabase(baza_dir=baza_dir))
        first_service.ensure_ready()
        first_payload = first_service.load_prescription_datasets()["drugs"].get("dopamine") or {}
        if (first_payload.get("default_dilution") or {}).get("volume") != 10:
            return False, f"first import did not use initial overrides: {first_payload}"

        (bundled_dict_dir / "user_overrides.json").write_text(
            json.dumps(second_overrides, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        second_service = SettingsService(SettingsDatabase(baza_dir=baza_dir))
        second_service.ensure_ready()
        second_payload = second_service.load_prescription_datasets()["drugs"].get("dopamine") or {}
        if (second_payload.get("default_dilution") or {}).get("volume") != 10:
            return False, "legacy user_overrides were reapplied after settings DB import"
        return True, "ok"
    finally:
        app_paths.SEED_DIR = original_seed_dir
        app_paths.USER_DICT_DIR = original_user_dict_dir
        app_paths.BAZA_DIR = original_baza_dir
        app_paths.get_resources_dir = original_get_resources_dir
        app_paths.get_executable_dir = original_get_executable_dir


def _check_settings_release_snapshot_applies_dev_settings_to_target_db(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.data.settings.settings_release import (
        SETTINGS_RELEASE_APPLIED_HASH_KEY,
        apply_settings_release_snapshot,
        export_settings_release_snapshot,
    )
    from rem_card.services.settings.settings_service import (
        BACKGROUND_SETTINGS_KEY,
        DISPLAY_SETTINGS_KEY,
        PRINT_SETTINGS_KEY,
        SettingsService,
    )

    source_baza = os.path.join(temp_root, "dev_rel")
    target_baza = os.path.join(temp_root, "net_rel")
    marker = f"release_snapshot_{os.getpid()}"
    source_service = SettingsService(SettingsDatabase(baza_dir=source_baza))
    source_service.ensure_ready()
    source_service.save_prescription_item(
        "groups",
        f"group_{marker}",
        {"name": "Релизная группа", "display_name": "Релизная группа"},
    )
    source_service.save_prescription_item(
        "drugs",
        f"drug_{marker}",
        {
            "name": "Релизный препарат",
            "latin": "Releasei",
            "group": f"group_{marker}",
            "unit": "mg",
            "default_dose": "1",
        },
    )
    source_service.set_app_setting(
        "shared",
        "display_settings",
        {"release_marker": marker},
        catalog_key=DISPLAY_SETTINGS_KEY,
        entity_type="display_settings",
        operation="release_probe",
    )
    source_service.set_app_setting(
        "doctor",
        "print_config",
        {"release_marker": marker, "vitals": True},
        catalog_key=PRINT_SETTINGS_KEY,
        entity_type="print_config",
        operation="release_probe",
    )
    source_service.set_app_setting(
        "shared",
        "background_settings",
        {
            "version": 1,
            "backgrounds": [
                {
                    "id": f"background_{marker}",
                    "name": "Релизный фон",
                    "file": "",
                    "start": "01-01",
                    "end": "12-31",
                }
            ],
        },
        catalog_key=BACKGROUND_SETTINGS_KEY,
        entity_type="background_settings",
        operation="release_probe",
    )

    snapshot_path = os.path.join(temp_root, "rel_snapshot.json")
    export_report = export_settings_release_snapshot(
        source_baza,
        snapshot_path,
        release_version="9.9.9",
        release_commit="regression",
    )
    target_service = SettingsService(SettingsDatabase(baza_dir=target_baza))
    target_service.ensure_ready()
    apply_report = apply_settings_release_snapshot(
        target_service.db,
        snapshot_path,
        bump_catalog_version=target_service._bump_catalog_version,
    )
    if not apply_report.get("applied") or int(apply_report.get("changed_rows") or 0) <= 0:
        return False, f"release snapshot did not apply changes: {apply_report}"

    with target_service.db.read_connection() as conn:
        drug = conn.execute("SELECT payload_json FROM drugs WHERE code = ?", (f"drug_{marker}",)).fetchone()
        group = conn.execute("SELECT payload_json FROM drug_groups WHERE code = ?", (f"group_{marker}",)).fetchone()
        display = conn.execute(
            "SELECT value_json FROM app_settings WHERE scope = 'shared' AND key = 'display_settings'"
        ).fetchone()
        print_config = conn.execute(
            "SELECT value_json FROM app_settings WHERE scope = 'doctor' AND key = 'print_config'"
        ).fetchone()
        background = conn.execute(
            "SELECT value_json FROM ui_backgrounds WHERE background_key = ?",
            (f"background_{marker}",),
        ).fetchone()
        meta = conn.execute(
            "SELECT value FROM settings_meta WHERE key = ?",
            (SETTINGS_RELEASE_APPLIED_HASH_KEY,),
        ).fetchone()
        changes = conn.execute(
            """
            SELECT scope, source_client_id
            FROM settings_change_log
            WHERE entity_type = 'settings_release_snapshot'
            ORDER BY id
            """
        ).fetchall()
    if not drug or marker not in str(drug["payload_json"]):
        return False, "release snapshot did not copy dev drug catalog"
    if not group or "Релизная группа" not in str(group["payload_json"]):
        return False, "release snapshot did not copy dev drug group"
    if not display or marker not in str(display["value_json"]):
        return False, "release snapshot did not copy display settings"
    if not print_config or marker not in str(print_config["value_json"]):
        return False, "release snapshot did not copy print settings"
    if not background or marker not in str(background["value_json"]):
        return False, "release snapshot did not copy background rows"
    if not meta or str(meta["value"]) != str(export_report["content_hash"]):
        return False, "release snapshot applied hash meta was not stored"
    if not changes:
        return False, "release snapshot did not write settings_change_log events"
    if any(not str(row["source_client_id"] or "").startswith("settings_release:") for row in changes):
        return False, "release snapshot change_log source_client_id is missing"

    second_report = apply_settings_release_snapshot(
        target_service.db,
        snapshot_path,
        bump_catalog_version=target_service._bump_catalog_version,
    )
    if second_report.get("applied") or second_report.get("reason") != "already_applied":
        return False, f"release snapshot should be idempotent, got: {second_report}"

    saved_apply_env = os.environ.get("REMCARD_APPLY_SETTINGS_RELEASE_SNAPSHOT")
    saved_snapshot_env = os.environ.get("REMCARD_SETTINGS_RELEASE_SNAPSHOT")
    try:
        os.environ["REMCARD_APPLY_SETTINGS_RELEASE_SNAPSHOT"] = "1"
        os.environ["REMCARD_SETTINGS_RELEASE_SNAPSHOT"] = snapshot_path
        startup_target = os.path.join(temp_root, "net_start")
        startup_service = SettingsService(SettingsDatabase(baza_dir=startup_target))
        startup_info = startup_service.ensure_ready()
        startup_report = startup_info.get("settings_release_snapshot") or {}
        if not startup_report.get("applied"):
            return False, f"startup did not apply bundled release snapshot: {startup_report}"
        with startup_service.db.read_connection() as conn:
            startup_drug = conn.execute("SELECT 1 FROM drugs WHERE code = ?", (f"drug_{marker}",)).fetchone()
        if not startup_drug:
            return False, "startup release snapshot did not copy dev drug catalog"
    finally:
        if saved_apply_env is None:
            os.environ.pop("REMCARD_APPLY_SETTINGS_RELEASE_SNAPSHOT", None)
        else:
            os.environ["REMCARD_APPLY_SETTINGS_RELEASE_SNAPSHOT"] = saved_apply_env
        if saved_snapshot_env is None:
            os.environ.pop("REMCARD_SETTINGS_RELEASE_SNAPSHOT", None)
        else:
            os.environ["REMCARD_SETTINGS_RELEASE_SNAPSHOT"] = saved_snapshot_env
    return True, "ok"


def _check_settings_release_snapshot_preserves_runtime_template_edits(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.data.settings.settings_release import (
        SETTINGS_RELEASE_APPLIED_AT_KEY,
        SETTINGS_RELEASE_APPLIED_HASH_KEY,
        SETTINGS_RELEASE_VERSION_KEY,
        apply_settings_release_snapshot,
        export_settings_release_snapshot,
    )
    from rem_card.services.settings.settings_service import SettingsService

    source_baza = os.path.join(temp_root, "dev_release")
    target_baza = os.path.join(temp_root, "network_target")
    template_key = f"release_runtime_edit_{os.getpid()}"
    release_payload = {
        "name": "Регрессия runtime edit",
        "template_type": "simple",
        "drugs": [
            {
                "drug": "fizrastvor",
                "dose": 250.0,
                "unit": "ml",
                "admin_type": "infusion",
                "duration_min": 30,
            }
        ],
    }
    runtime_payload = json.loads(json.dumps(release_payload, ensure_ascii=False))
    runtime_payload["drugs"][0]["dose"] = 200.0

    source_service = SettingsService(SettingsDatabase(baza_dir=source_baza))
    source_service.ensure_ready()
    source_service.save_prescription_item("templates", template_key, release_payload)

    snapshot_path = os.path.join(temp_root, "settings_release_snapshot.json")
    export_settings_release_snapshot(
        source_baza,
        snapshot_path,
        release_version="runtime-edit-regression",
        release_commit="regression",
    )

    target_service = SettingsService(SettingsDatabase(baza_dir=target_baza))
    target_service.ensure_ready()
    first_report = apply_settings_release_snapshot(
        target_service.db,
        snapshot_path,
        bump_catalog_version=target_service._bump_catalog_version,
    )
    if not first_report.get("applied"):
        return False, f"release snapshot did not apply initial template: {first_report}"
    first_payload = target_service.load_prescription_datasets()["templates"].get(template_key) or {}
    if float((first_payload.get("drugs") or [{}])[0].get("dose") or 0) != 250.0:
        return False, "release snapshot did not seed template value 250"

    target_service.save_prescription_item("templates", template_key, runtime_payload)
    with target_service.db.transaction("regression_missing_release_meta") as cursor:
        cursor.execute(
            "UPDATE order_templates SET source = 'manual', updated_at = ? WHERE template_key = ?",
            ("2999-01-01 00:00:00", template_key),
        )
        cursor.execute(
            "DELETE FROM settings_meta WHERE key IN (?, ?, ?)",
            (
                SETTINGS_RELEASE_APPLIED_HASH_KEY,
                SETTINGS_RELEASE_APPLIED_AT_KEY,
                SETTINGS_RELEASE_VERSION_KEY,
            ),
        )
    target_service.invalidate_cache()

    second_report = apply_settings_release_snapshot(
        target_service.db,
        snapshot_path,
        bump_catalog_version=target_service._bump_catalog_version,
    )
    if not second_report.get("applied"):
        return False, f"release snapshot should store missing apply marker: {second_report}"
    table_report = (second_report.get("tables") or {}).get("order_templates") or {}
    if int(table_report.get("preserved") or 0) < 1:
        return False, f"runtime-edited template was not preserved: {second_report}"

    restarted = SettingsService(SettingsDatabase(baza_dir=target_baza))
    restarted.ensure_ready()
    final_payload = restarted.load_prescription_datasets()["templates"].get(template_key) or {}
    final_dose = float((final_payload.get("drugs") or [{}])[0].get("dose") or 0)
    if final_dose != 200.0:
        return False, f"runtime template edit was overwritten by release snapshot: dose={final_dose}"
    with restarted.db.read_connection() as conn:
        meta = conn.execute(
            "SELECT value FROM settings_meta WHERE key = ?",
            (SETTINGS_RELEASE_APPLIED_HASH_KEY,),
        ).fetchone()
    if not meta:
        return False, "release snapshot applied hash meta was not restored after preserving runtime edit"
    return True, "ok"


def _check_settings_release_snapshot_preserves_all_user_settings(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.paths import get_icon_dir
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.data.settings.settings_release import apply_settings_release_snapshot, export_settings_release_snapshot
    from rem_card.services.settings.settings_service import (
        BACKGROUND_SETTINGS_KEY,
        DISPLAY_SETTINGS_KEY,
        EMERGENCY_PASSWORD_CATALOG_KEY,
        EMERGENCY_PASSWORD_KEY,
        LAB_ANALYSIS_KEY,
        OPERBLOCK_ANESTHESIA_TYPES_APP_KEY,
        OPERBLOCK_GROUP_ROUTES_APP_KEY,
        OPERBLOCK_MEDICATION_PRESETS_APP_KEY,
        OPERBLOCK_QUICK_ORDER_BUTTONS_APP_KEY,
        OPERBLOCK_QUICK_ORDERS_APP_KEY,
        OPERBLOCK_SETTINGS_KEY,
        OPERBLOCK_SETTINGS_SCOPE,
        OPERBLOCK_TEAM_APP_KEY,
        PRINT_SETTINGS_KEY,
        STYLE_SETTINGS_KEY,
        SettingsService,
    )
    from rem_card.ui.shared import background_settings as bg

    marker = f"all_user_settings_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    old_time = "2000-01-01 00:00:00"
    source_service = SettingsService(SettingsDatabase(baza_dir=os.path.join(temp_root, "SourceBaza")))
    target_service = SettingsService(SettingsDatabase(baza_dir=os.path.join(temp_root, "TargetBaza")))
    source_service.ensure_ready()
    target_service.ensure_ready()

    def mark_old_row(service: SettingsService, table: str, key_column: str, key: str) -> None:
        with service.db.transaction(f"regression_old_{table}") as cursor:
            cursor.execute(
                f"UPDATE {table} SET source = 'legacy_json', updated_at = ? WHERE {key_column} = ?",
                (old_time, key),
            )
            if cursor.rowcount != 1:
                raise AssertionError(f"строка {table}.{key_column}={key} не найдена")

    def mark_old_app(service: SettingsService, scope: str, key: str) -> None:
        with service.db.transaction(f"regression_old_app_{key}") as cursor:
            cursor.execute(
                "UPDATE app_settings SET updated_at = ? WHERE scope = ? AND key = ?",
                (old_time, scope, key),
            )
            if cursor.rowcount != 1:
                raise AssertionError(f"app_setting {scope}:{key} не найден")

    def row_by_key(service: SettingsService, table: str, key_column: str, key: str) -> sqlite3.Row | None:
        with service.db.read_connection() as conn:
            return conn.execute(f"SELECT * FROM {table} WHERE {key_column} = ?", (key,)).fetchone()

    def json_column(row: sqlite3.Row | None, column: str) -> Any:
        if not row or not row[column]:
            return {}
        decoded = json.loads(row[column])
        return decoded

    def set_app_pair(scope: str, key: str, catalog_key: str, release_value: Any, user_value: Any) -> None:
        source_service.set_app_setting(
            scope,
            key,
            release_value,
            catalog_key=catalog_key,
            entity_type=key,
            operation="release_probe",
            changed_by_role="system",
        )
        target_service.set_app_setting(
            scope,
            key,
            user_value,
            catalog_key=catalog_key,
            entity_type=key,
            operation="user_probe",
            changed_by_role="doctor",
        )
        mark_old_app(target_service, scope, key)

    def app_value(scope: str, key: str) -> Any:
        target_service.invalidate_cache()
        return target_service.get_app_setting(scope, key, default=None)

    group_key = f"group_{marker}"
    form_key = f"form_{marker}"
    route_key = f"route_{marker}"
    solvent_key = f"solvent_{marker}"
    drug_key = f"drug_{marker}"
    template_key = f"template_{marker}"
    new_release_group_key = f"group_new_{marker}"
    stale_release_group_key = f"group_stale_release_{marker}"

    prescription_pairs = (
        ("groups", "drug_groups", "code", group_key, {"name": "Релизная группа"}, {"name": "Пользовательская группа"}),
        ("forms", "dosage_forms", "code", form_key, {"name": "Релизная форма"}, {"name": "Пользовательская форма"}),
        ("admin_types", "administration_routes", "code", route_key, {"name": "Релизный путь"}, {"name": "Пользовательский путь"}),
        ("diluents", "solvents", "code", solvent_key, {"display": "Релизный раствор"}, {"display": "Пользовательский раствор"}),
    )
    for dict_name, table, key_column, key, release_payload, user_payload in prescription_pairs:
        source_service.save_prescription_item(dict_name, key, release_payload)
        target_service.save_prescription_item(dict_name, key, user_payload)
        mark_old_row(target_service, table, key_column, key)
    source_service.save_prescription_item("groups", new_release_group_key, {"name": "Новая релизная группа"})
    source_service.save_prescription_item("groups", stale_release_group_key, {"name": "Обновленная release-строка"})
    with target_service.db.transaction("regression_stale_release_like_manual_row") as cursor:
        cursor.execute(
            """
            INSERT INTO drug_groups (
                code, name, display_name, sort_order, enabled, revision,
                payload_json, source, created_at, updated_at
            )
            VALUES (?, 'Старая release-строка', 'Старая release-строка', 0, 1, 1, ?, 'manual', ?, ?)
            """,
            (
                stale_release_group_key,
                json.dumps({"name": "Старая release-строка"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                old_time,
                old_time,
            ),
        )

    source_service.save_prescription_item(
        "drugs",
        drug_key,
        {
            "name": "Релизный препарат",
            "group": group_key,
            "form_key": form_key,
            "route_code": route_key,
            "unit": "мг",
            "default_dose": "1",
        },
    )
    target_service.save_prescription_item(
        "drugs",
        drug_key,
        {
            "name": "Пользовательский препарат",
            "group": group_key,
            "form_key": form_key,
            "route_code": route_key,
            "unit": "мг",
            "default_dose": "9",
        },
    )
    mark_old_row(target_service, "drugs", "code", drug_key)

    source_service.save_prescription_item(
        "templates",
        template_key,
        {"name": "Релизный шаблон", "template_type": "simple", "drugs": [{"drug": drug_key, "dose": 1}]},
    )
    target_service.save_prescription_item(
        "templates",
        template_key,
        {"name": "Пользовательский шаблон", "template_type": "simple", "drugs": [{"drug": drug_key, "dose": 9}]},
    )
    mark_old_row(target_service, "order_templates", "template_key", template_key)

    doctor_name = f"Доктор {marker}"
    source_service.save_doctor_records([{"full_name": doctor_name, "position": "Релизная должность"}])
    target_service.save_doctor_records([{"full_name": doctor_name, "position": "Пользовательская должность"}])
    mark_old_row(target_service, "doctors", "full_name", doctor_name)

    diet_name = f"Диета {marker}"
    source_diet_id = source_service.create_diet_template(diet_name, "Релизный рацион")
    target_diet_id = target_service.create_diet_template(diet_name, "Пользовательский рацион")
    source_diet_key = row_by_key(source_service, "diet_templates", "id", str(source_diet_id))["template_key"]
    target_diet_key = row_by_key(target_service, "diet_templates", "id", str(target_diet_id))["template_key"]
    if source_diet_key != target_diet_key:
        return False, f"ключи шаблона питания не совпали: {source_diet_key} != {target_diet_key}"
    mark_old_row(target_service, "diet_templates", "template_key", str(target_diet_key))

    analysis_code = f"analysis_{marker}"
    source_service.create_lab_template(name=f"Анализ {marker}", code=analysis_code, comment="Релизный комментарий")
    target_service.create_lab_template(name=f"Анализ {marker}", code=analysis_code, comment="Пользовательский комментарий")
    mark_old_row(target_service, "lab_analysis_templates", "analysis_code", analysis_code)

    icon_dir = get_icon_dir()
    release_icon_path = os.path.join(icon_dir, "gas_izm.png")
    user_icon_path = os.path.join(icon_dir, "bolus.png")
    if not os.path.isfile(release_icon_path) or not os.path.isfile(user_icon_path):
        return False, "не найдены тестовые иконки gas_izm.png/bolus.png"
    icon_key = f"drug:manual:probe:{marker}"
    source_service.save_operblock_icon(
        icon_key=icon_key,
        category="drug",
        target_key=f"manual:probe:{marker}",
        name="Релизная иконка",
        default_file="gas_izm.png",
        image_path=release_icon_path,
        sort_order=9100,
        changed_by_role="system",
    )
    target_service.save_operblock_icon(
        icon_key=icon_key,
        category="drug",
        target_key=f"manual:probe:{marker}",
        name="Пользовательская иконка",
        default_file="bolus.png",
        image_path=user_icon_path,
        sort_order=9100,
        changed_by_role="doctor",
    )
    mark_old_row(target_service, "operblock_icons", "icon_key", icon_key)

    background_key = f"background_{marker}"
    release_background = bg.normalize_background_settings_payload(
        {"backgrounds": [{"id": background_key, "name": "Релизный фон", "file": "", "start": "01-01", "end": "12-31"}]}
    )
    user_background = bg.normalize_background_settings_payload(
        {"backgrounds": [{"id": background_key, "name": "Пользовательский фон", "file": "", "start": "02-01", "end": "02-02"}]}
    )
    set_app_pair("shared", "background_settings", BACKGROUND_SETTINGS_KEY, release_background, user_background)
    with target_service.db.transaction("regression_old_background_row") as cursor:
        cursor.execute("UPDATE ui_backgrounds SET updated_at = ? WHERE background_key = ?", (old_time, background_key))

    app_cases = (
        ("shared", "display_settings", DISPLAY_SETTINGS_KEY, {"value": "release"}, {"value": "user_display"}),
        ("shared", "lab_orders_columns", DISPLAY_SETTINGS_KEY, {"value": "release"}, {"value": "user_columns"}),
        ("shared", "style_settings", STYLE_SETTINGS_KEY, {"value": "release"}, {"value": "user_style"}),
        ("shared", "lab_materials", LAB_ANALYSIS_KEY, [{"code": f"mat_{marker}", "label": "Релизный материал"}], [{"code": f"mat_{marker}", "label": "Пользовательский материал"}]),
        ("doctor", "print_config", PRINT_SETTINGS_KEY, {"marker": marker, "value": "release"}, {"marker": marker, "value": "user_print"}),
        ("shared", EMERGENCY_PASSWORD_KEY, EMERGENCY_PASSWORD_CATALOG_KEY, f"release-secret-{marker}", f"user-secret-{marker}"),
        (OPERBLOCK_SETTINGS_SCOPE, OPERBLOCK_GROUP_ROUTES_APP_KEY, OPERBLOCK_SETTINGS_KEY, {"items": ["release"]}, {"items": ["user_group_routes"]}),
        (OPERBLOCK_SETTINGS_SCOPE, OPERBLOCK_TEAM_APP_KEY, OPERBLOCK_SETTINGS_KEY, {"items": ["release"]}, {"items": ["user_team"]}),
        (OPERBLOCK_SETTINGS_SCOPE, OPERBLOCK_ANESTHESIA_TYPES_APP_KEY, OPERBLOCK_SETTINGS_KEY, {"items": ["release"]}, {"items": ["user_anesthesia"]}),
        (OPERBLOCK_SETTINGS_SCOPE, OPERBLOCK_QUICK_ORDER_BUTTONS_APP_KEY, OPERBLOCK_SETTINGS_KEY, {"items": ["release"]}, {"items": ["user_buttons"]}),
        (OPERBLOCK_SETTINGS_SCOPE, OPERBLOCK_QUICK_ORDERS_APP_KEY, OPERBLOCK_SETTINGS_KEY, {"items": ["release"]}, {"items": ["user_orders"]}),
        (OPERBLOCK_SETTINGS_SCOPE, OPERBLOCK_MEDICATION_PRESETS_APP_KEY, OPERBLOCK_SETTINGS_KEY, {"items": ["release"]}, {"items": ["user_presets"]}),
    )
    for scope, key, catalog_key, release_value, user_value in app_cases:
        set_app_pair(scope, key, catalog_key, release_value, user_value)

    snapshot_path = os.path.join(temp_root, "settings_release_snapshot.json")
    export_settings_release_snapshot(
        os.path.join(temp_root, "SourceBaza"),
        snapshot_path,
        release_version="all-settings-preserve-regression",
        release_commit="regression",
    )
    apply_report = apply_settings_release_snapshot(
        target_service.db,
        snapshot_path,
        bump_catalog_version=target_service._bump_catalog_version,
    )
    if not apply_report.get("applied"):
        return False, f"release snapshot не применился: {apply_report}"
    if int(apply_report.get("preserved_rows") or 0) < 20:
        return False, f"release snapshot сохранил слишком мало пользовательских строк: {apply_report}"

    expected_names = (
        ("drug_groups", "code", group_key, "name", "Пользовательская группа"),
        ("dosage_forms", "code", form_key, "name", "Пользовательская форма"),
        ("administration_routes", "code", route_key, "name", "Пользовательский путь"),
        ("solvents", "code", solvent_key, "name", "Пользовательский раствор"),
        ("doctors", "full_name", doctor_name, "position", "Пользовательская должность"),
        ("diet_templates", "template_key", str(target_diet_key), "description", "Пользовательский рацион"),
    )
    for table, key_column, key, value_column, expected in expected_names:
        row = row_by_key(target_service, table, key_column, key)
        if not row or row[value_column] != expected:
            actual = None if not row else row[value_column]
            return False, f"{table}.{value_column} перезаписан: {actual!r}, ожидалось {expected!r}"

    drug_row = row_by_key(target_service, "drugs", "code", drug_key)
    if not drug_row or str(drug_row["default_dose"]) != "9":
        return False, "release snapshot перезаписал пользовательскую дозу препарата"
    template_payload = json_column(row_by_key(target_service, "order_templates", "template_key", template_key), "params_json")
    if template_payload.get("name") != "Пользовательский шаблон" or template_payload.get("drugs", [{}])[0].get("dose") != 9:
        return False, f"release snapshot перезаписал пользовательский шаблон назначения: {template_payload}"
    lab_payload = json_column(row_by_key(target_service, "lab_analysis_templates", "analysis_code", analysis_code), "payload_json")
    if lab_payload.get("comment") != "Пользовательский комментарий":
        return False, f"release snapshot перезаписал пользовательский анализ: {lab_payload}"

    _icon_version, icon_records = target_service.get_operblock_icon_records(
        [icon_key], include_blob=True, ensure_defaults=False
    )
    icon_row = icon_records.get(icon_key)
    if (
        not icon_row
        or icon_row.get("image_blob") != Path(user_icon_path).read_bytes()
    ):
        return False, "release snapshot перезаписал пользовательскую иконку"
    background_row_payload = json_column(row_by_key(target_service, "ui_backgrounds", "background_key", background_key), "value_json")
    if background_row_payload.get("name") != "Пользовательский фон":
        return False, f"release snapshot перезаписал строку фона: {background_row_payload}"

    for scope, key, _catalog_key, _release_value, user_value in app_cases:
        final_value = app_value(scope, key)
        if final_value != user_value:
            return False, f"app_setting {scope}:{key} перезаписан: {final_value!r}, ожидалось {user_value!r}"
    if app_value("shared", "background_settings") != user_background:
        return False, "release snapshot перезаписал background_settings"

    new_row = row_by_key(target_service, "drug_groups", "code", new_release_group_key)
    if not new_row or new_row["name"] != "Новая релизная группа":
        return False, "новая dev-настройка не была добавлена в целевую БД"
    if str(new_row["source"] or "") != "release":
        return False, f"release-строка не помечена source='release': {new_row['source']!r}"
    stale_row = row_by_key(target_service, "drug_groups", "code", stale_release_group_key)
    if not stale_row or stale_row["name"] != "Обновленная release-строка":
        return False, "старая release-похожая строка без пользовательского журнала ошибочно сохранена"
    if str(stale_row["source"] or "") != "release":
        return False, f"обновленная release-строка не получила source='release': {stale_row['source']!r}"
    return True, "ok"


def _check_runtime_catalog_services_default_to_settings_db(temp_root: str) -> tuple[bool, str]:
    from rem_card.services.diet_service import DietTemplateService
    from rem_card.services.doctor_list_service import DoctorListStore
    from rem_card.services.lab_analysis_catalog_service import LabAnalysisCatalogService

    _ = temp_root
    lab_service = LabAnalysisCatalogService()
    diet_service = DietTemplateService()
    doctor_store = DoctorListStore()
    if getattr(lab_service, "file_store", None) is not None:
        return False, "LabAnalysisCatalogService default must not use JSON file store"
    if getattr(diet_service, "file_store", None) is not None:
        return False, "DietTemplateService default must not use JSON file store"
    if getattr(doctor_store, "settings_service", None) is None:
        return False, "DoctorListStore default must use settings DB"

    prescription_source = (PROJECT_ROOT / "services" / "prescription_engine.py").read_text(encoding="utf-8")
    order_domain_source = (PROJECT_ROOT / "services" / "order_domain_service.py").read_text(encoding="utf-8")
    forbidden = ("user_overrides.json", ".seed.json", "json.load")
    for token in forbidden:
        if token in prescription_source:
            return False, f"PrescriptionEngine runtime still references {token}"
    if ".seed.json" in order_domain_source or "json.load" in order_domain_source:
        return False, "OrderDomainService priority helpers must not read seed JSON"
    return True, "ok"


def _check_settings_change_log_source_client_id_is_non_empty(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.services.settings.settings_service import DISPLAY_SETTINGS_KEY, SettingsService

    service = SettingsService(SettingsDatabase(baza_dir=os.path.join(temp_root, "Baza")))
    service.ensure_ready()
    service.set_app_setting(
        "shared",
        "display_settings",
        {"probe": "source_client_non_empty"},
        catalog_key=DISPLAY_SETTINGS_KEY,
        entity_type="source_client_probe",
        operation="update",
    )
    with service.db.read_connection() as conn:
        row = conn.execute(
            """
            SELECT source_client_id
            FROM settings_change_log
            WHERE entity_type = 'source_client_probe'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        audit_row = conn.execute(
            """
            SELECT source_client_id
            FROM settings_audit_log
            WHERE entity_type = 'source_client_probe'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    source_client_id = str(row["source_client_id"] or "") if row else ""
    if not source_client_id:
        return False, "settings_change_log.source_client_id is empty after settings write"
    audit_source_client_id = str(audit_row["source_client_id"] or "") if audit_row else ""
    if audit_source_client_id != source_client_id:
        return False, "settings_audit_log.source_client_id does not match settings_change_log"
    return True, source_client_id


def _check_settings_source_client_id_is_stable_within_process(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.services.settings.settings_service import DISPLAY_SETTINGS_KEY, SettingsService

    service = SettingsService(SettingsDatabase(baza_dir=os.path.join(temp_root, "Baza")))
    service.ensure_ready()
    for index in (1, 2):
        service.set_app_setting(
            "shared",
            "display_settings",
            {"probe": f"source_client_stable_{index}"},
            catalog_key=DISPLAY_SETTINGS_KEY,
            entity_type="source_client_stable_probe",
            operation="update",
        )
    with service.db.read_connection() as conn:
        rows = conn.execute(
            """
            SELECT source_client_id
            FROM settings_change_log
            WHERE entity_type = 'source_client_stable_probe'
            ORDER BY id DESC
            LIMIT 2
            """
        ).fetchall()
    ids = [str(row["source_client_id"] or "") for row in rows]
    if len(ids) != 2 or not ids[0] or not ids[1]:
        return False, f"expected two non-empty source_client_id values, got {ids}"
    if ids[0] != ids[1]:
        return False, f"source_client_id changed within process: {ids}"
    return True, ids[0]


def _check_lab_materials_change_updates_lab_analysis_hash(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.services.settings.settings_service import LAB_ANALYSIS_KEY, SettingsService

    service = SettingsService(SettingsDatabase(baza_dir=os.path.join(temp_root, "Baza")))
    service.ensure_ready()
    before_version, before_hash = service.get_catalog_version(LAB_ANALYSIS_KEY)
    materials = [
        dict(item)
        for item in service.list_lab_materials()
        if str(item.get("code") or "") != "regression_hash_material"
    ]
    materials.append(
        {
            "code": "regression_hash_material",
            "label": "Материал проверки hash",
            "built_in": False,
            "version": 1,
        }
    )
    service.save_lab_materials(materials)
    after_version, after_hash = service.get_catalog_version(LAB_ANALYSIS_KEY)
    if after_version <= before_version:
        return False, f"lab_analysis version did not increase: {before_version} -> {after_version}"
    if after_hash == before_hash:
        return False, "lab_analysis content_hash did not change after lab_materials update"
    return True, "ok"


def _check_print_config_change_updates_print_settings_hash(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.services.settings.settings_service import PRINT_SETTINGS_KEY, SettingsService

    service = SettingsService(SettingsDatabase(baza_dir=os.path.join(temp_root, "Baza")))
    service.ensure_ready()
    before_version, before_hash = service.get_catalog_version(PRINT_SETTINGS_KEY)
    current = service.get_app_setting("doctor", "print_config", default={})
    if not isinstance(current, dict):
        current = {}
    updated = dict(current)
    updated["labs"] = not bool(updated.get("labs", False))
    service.set_app_setting(
        "doctor",
        "print_config",
        updated,
        catalog_key=PRINT_SETTINGS_KEY,
        entity_type="print_settings",
        operation="update",
    )
    after_version, after_hash = service.get_catalog_version(PRINT_SETTINGS_KEY)
    if after_version <= before_version:
        return False, f"print_settings version did not increase: {before_version} -> {after_version}"
    if after_hash == before_hash:
        return False, "print_settings content_hash did not change after print_config update"
    return True, "ok"


def _check_print_config_outcome_report_reminder(temp_root: str) -> tuple[bool, str]:
    from rem_card.services.settings.settings_service import configure_settings_service, reset_settings_service
    from rem_card.ui.rem_card_sectors.sector_print import PrintConfig

    settings_dir = os.path.join(temp_root, "settings")
    os.makedirs(settings_dir, exist_ok=True)
    configure_settings_service(settings_db_path=os.path.join(settings_dir, "remcard_settings.db"))
    try:
        config = PrintConfig()
        loaded = config.load()
        if "outcome_report_reminder" not in loaded:
            return False, "print config does not expose outcome_report_reminder"
        if loaded.get("outcome_report_reminder") is not False:
            return False, f"outcome report reminder default must be disabled: {loaded!r}"
        if loaded.get("transfusion_protocols") is not False:
            return False, f"transfusion protocols default must be disabled: {loaded!r}"

        config.save(
            loaded["vitals"],
            loaded["balance"],
            loaded["prescriptions"],
            loaded["events"],
            loaded["ventilation"],
            loaded["labs"],
            loaded["procedures"],
            loaded["death_outcome"],
            loaded["death_protocol"],
            loaded["transfusion_registration"],
            outcome_report_reminder=True,
            transfusion_protocols=True,
        )
        enabled = config.load()
        if enabled.get("outcome_report_reminder") is not True:
            return False, f"outcome report reminder was not saved enabled: {enabled!r}"
        if enabled.get("transfusion_protocols") is not True:
            return False, f"transfusion protocols setting was not saved enabled: {enabled!r}"

        config.save(
            enabled["vitals"],
            enabled["balance"],
            enabled["prescriptions"],
            enabled["events"],
            enabled["ventilation"],
            enabled["labs"],
            enabled["procedures"],
            enabled["death_outcome"],
            enabled["death_protocol"],
            enabled["transfusion_registration"],
            outcome_report_reminder=False,
            transfusion_protocols=False,
        )
        disabled = config.load()
        if disabled.get("outcome_report_reminder") is not False:
            return False, f"outcome report reminder was not saved disabled: {disabled!r}"
        if disabled.get("transfusion_protocols") is not False:
            return False, f"transfusion protocols setting was not saved disabled: {disabled!r}"
        return True, "ok"
    finally:
        reset_settings_service()


def _check_outcome_report_reminder_dispatch(temp_root: str) -> tuple[bool, str]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from datetime import datetime

    from PySide6.QtWidgets import QApplication

    from rem_card.ui.rem_card_sectors import sector_events as events_module
    from rem_card.ui.rem_card_sectors.sector_events import (
        OUTCOME_REPORT_DAILY,
        OUTCOME_REPORT_FULL,
        SectorEvents,
    )

    _ = temp_root
    app = QApplication.instance() or QApplication([])

    class FakeReportController:
        def __init__(self):
            self.daily_calls = []
            self.full_calls = []

        def run_daily_report(self, admission_id, shift_date):
            self.daily_calls.append((admission_id, shift_date))

        def run_full_report(self, admission_id):
            self.full_calls.append(admission_id)

    shown = []
    original_warning_with_actions = events_module.CustomMessageBox.warning_with_actions
    widget = SectorEvents()
    controller = FakeReportController()
    try:
        widget.admission_id = 42
        widget.shift_date = datetime(2026, 6, 10, 8, 0)
        widget._outcome_report_reminder_enabled = lambda: True
        widget._get_outcome_report_controller = lambda: controller

        def fake_warning_with_actions(parent, title, message, action_buttons):
            shown.append(
                {
                    "title": title,
                    "message": message,
                    "buttons": [text for text, _code in action_buttons],
                }
            )
            return OUTCOME_REPORT_DAILY if len(shown) == 1 else OUTCOME_REPORT_FULL

        events_module.CustomMessageBox.warning_with_actions = fake_warning_with_actions
        widget._show_outcome_report_reminder(datetime(2026, 6, 10, 12, 30))
        widget._show_outcome_report_reminder(datetime(2026, 6, 10, 12, 30))
        app.processEvents()

        if len(shown) != 2:
            return False, f"outcome report reminder was not shown twice for enabled setting: {shown!r}"
        expected_buttons = ["Отчет за сутки", "Отчет за все время пребывания", "Не печатать отчеты"]
        if shown[0]["buttons"] != expected_buttons:
            return False, f"unexpected reminder buttons: {shown[0]['buttons']!r}"
        if "Какой отчет о пребывании пациента" not in shown[0]["message"]:
            return False, f"reminder message does not ask for report kind: {shown[0]['message']!r}"
        if controller.daily_calls != [(42, widget.shift_date)]:
            return False, f"daily report was not dispatched with current shift date: {controller.daily_calls!r}"
        if controller.full_calls != [42]:
            return False, f"full report was not dispatched: {controller.full_calls!r}"

        widget._outcome_report_reminder_enabled = lambda: False
        widget._show_outcome_report_reminder(datetime(2026, 6, 10, 13, 0))
        if len(shown) != 2:
            return False, "disabled outcome report reminder still showed dialog"
        return True, "ok"
    finally:
        events_module.CustomMessageBox.warning_with_actions = original_warning_with_actions
        widget.close()


def _check_unchanged_catalog_hash_is_stable(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.services.settings.settings_service import (
        LAB_ANALYSIS_KEY,
        PRINT_SETTINGS_KEY,
        SettingsService,
    )

    service = SettingsService(SettingsDatabase(baza_dir=os.path.join(temp_root, "Baza")))
    service.ensure_ready()
    first_lab = service.get_catalog_version(LAB_ANALYSIS_KEY)
    second_lab = service.get_catalog_version(LAB_ANALYSIS_KEY)
    first_print = service.get_catalog_version(PRINT_SETTINGS_KEY)
    second_print = service.get_catalog_version(PRINT_SETTINGS_KEY)
    if first_lab != second_lab:
        return False, f"lab_analysis hash/version changed without write: {first_lab} -> {second_lab}"
    if first_print != second_print:
        return False, f"print_settings hash/version changed without write: {first_print} -> {second_print}"
    return True, "ok"


def _check_lab_materials_management_from_settings_db(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.services.lab_analysis_catalog_service import LabAnalysisCatalogService
    from rem_card.services.settings.settings_service import SettingsService

    service = SettingsService(SettingsDatabase(baza_dir=os.path.join(temp_root, "Baza")))
    service.ensure_ready()
    catalog = LabAnalysisCatalogService(settings_service=service)

    initial = [dict(item) for item in catalog.list_materials()]
    if len(initial) < 2:
        return False, "lab materials fixture must contain at least two materials"
    regression_code = "regression_material"
    initial = [item for item in initial if str(item.get("code") or "") != regression_code]
    first_code = str(initial[0].get("code") or "")
    second_code = str(initial[1].get("code") or "")
    reordered = [
        {"code": regression_code, "label": "Регрессионный материал", "built_in": False, "version": 1},
        dict(initial[1]),
        dict(initial[0]),
        *[dict(item) for item in initial[2:]],
    ]

    before_version, _before_hash = service.get_catalog_version("lab_analysis")
    before_change_id = service.latest_change_id()
    catalog.save_materials(reordered)
    saved = catalog.list_materials()
    saved_codes = [str(item.get("code") or "") for item in saved]
    if saved_codes[:3] != [regression_code, second_code, first_code]:
        return False, f"lab material order was not saved: {saved_codes[:3]}"
    after_version, _after_hash = service.get_catalog_version("lab_analysis")
    if after_version <= before_version or service.latest_change_id() <= before_change_id:
        return False, "lab material save did not bump settings catalog version/change log"

    template_id = catalog.create_template(
        name="Регрессионный анализ материала",
        material=regression_code,
        default_times=["09:00"],
    )
    try:
        catalog.save_materials([item for item in saved if str(item.get("code") or "") != regression_code])
    except ValueError as exc:
        if "используется" not in str(exc):
            return False, f"unexpected used-material error: {exc}"
    else:
        return False, "used lab material was deleted without controlled error"

    created = next((item for item in catalog.list_templates() if int(item.get("id") or 0) == int(template_id)), None)
    if created is None:
        return False, "created lab template was not visible before cleanup"
    catalog.delete_template(template_id, expected_version=created.get("version"))
    catalog.save_materials([item for item in saved if str(item.get("code") or "") != regression_code])
    final_codes = [str(item.get("code") or "") for item in catalog.list_materials()]
    if regression_code in final_codes:
        return False, "unused lab material was not deleted"
    return True, "ok"


def _check_settings_change_log_invalidates_cache(temp_root: str) -> tuple[bool, str]:
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.services.settings.settings_service import SettingsService

    service = SettingsService(SettingsDatabase(baza_dir=os.path.join(temp_root, "Baza")))
    service.ensure_ready()
    before_version, before_hash = service.get_catalog_version("drug_catalog")
    before_change_id = service.latest_change_id()
    service.drug_catalog_snapshot()
    service.save_prescription_item(
        "groups",
        "regression_group",
        {"name": "Регрессионная группа", "priority_level": 77},
    )
    after_version, after_hash = service.get_catalog_version("drug_catalog")
    after_change_id = service.latest_change_id()
    snapshot = service.drug_catalog_snapshot()
    if after_change_id <= before_change_id:
        return False, "settings_change_log id did not advance"
    if after_version <= before_version:
        return False, "catalog version did not advance"
    if after_hash == before_hash:
        return False, "catalog content_hash did not change"
    if "regression_group" not in snapshot.groups:
        return False, "snapshot cache did not reload after catalog version bump"
    return True, "ok"


def _check_operblock_route_settings_order_and_default(temp_root: str) -> tuple[bool, str]:
    from rem_card.services.operblock_route_settings import (
        OPERBLOCK_DEFAULT_ROUTE_CODE,
        load_operblock_drug_groups,
        operblock_default_route_for_drug_group,
        operblock_routes_for_drug_group,
        save_operblock_group_route_settings,
    )
    from rem_card.services.settings.settings_service import configure_settings_service, reset_settings_service

    settings_db_path = os.path.join(temp_root, "Baza", "settings", "remcard_settings.db")
    try:
        service = configure_settings_service(settings_db_path=settings_db_path)
        service.ensure_ready()
        service.save_prescription_item(
            "groups",
            "regression_anesthetics",
            {"name_ru": "Анестетики", "priority_level": 1, "offset_min": 0, "color": "#6b7280"},
        )
        for code, label in (
            ("epidural", "Э/дурально"),
            ("intrathecal", "Интратекально"),
            ("perineural", "Периневрально"),
        ):
            service.save_prescription_item("admin_types", code, {"name_ru": label})

        save_operblock_group_route_settings(
            {"regression_anesthetics": ["epidural", "intrathecal", "perineural"]}
        )
        codes = [item["code"] for item in operblock_routes_for_drug_group("regression_anesthetics")]
        if codes[:3] != ["epidural", "intrathecal", "perineural"]:
            return False, f"route order without IV was not preserved: {codes}"
        if operblock_default_route_for_drug_group("regression_anesthetics") != "epidural":
            return False, "default route without IV should be the first configured route"

        save_operblock_group_route_settings(
            {"regression_anesthetics": ["intrathecal", "epidural", "perineural"]}
        )
        if operblock_default_route_for_drug_group("regression_anesthetics") != "intrathecal":
            return False, "default route did not follow reordered first route"

        save_operblock_group_route_settings(
            {"regression_anesthetics": ["epidural", OPERBLOCK_DEFAULT_ROUTE_CODE, "intrathecal"]}
        )
        if operblock_default_route_for_drug_group("regression_anesthetics") != OPERBLOCK_DEFAULT_ROUTE_CODE:
            return False, "IV route should remain the preferred default when it is available"

        service.save_prescription_item(
            "admin_types",
            "regression_unknown_preset_route",
            {"name_ru": "Регрессионный путь"},
        )
        service.set_app_setting(
            "operblock",
            "group_routes",
            {
                "version": 1,
                "routes_by_group": {"manual_only_group": ["regression_unknown_preset_route"]},
            },
            entity_type="operblock_group_routes",
            operation="regression",
            changed_by_role="regression",
        )
        import rem_card.services.operblock_route_settings as route_settings

        original_loader = route_settings._load_operblock_preset_group_codes
        try:
            route_settings._load_operblock_preset_group_codes = lambda: ["manual_only_group"]
            if "manual_only_group" not in {item["code"] for item in load_operblock_drug_groups()}:
                return False, "manual preset-only group was not exposed in operblock route settings"
            if operblock_default_route_for_drug_group("manual_only_group") != "regression_unknown_preset_route":
                return False, "manual preset-only group routes were not loaded from app setting"
        finally:
            route_settings._load_operblock_preset_group_codes = original_loader
        return True, "ok"
    finally:
        reset_settings_service()


def _check_operblock_runtime_settings_from_settings_db(temp_root: str) -> tuple[bool, str]:
    import sqlite3

    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.data.settings.settings_release import (
        SETTINGS_RELEASE_SNAPSHOT_FILE,
        apply_settings_release_snapshot,
        export_settings_release_snapshot,
    )
    from rem_card.services.operblock_anesthesia_types import (
        load_operblock_anesthesia_types,
        save_operblock_anesthesia_types,
    )
    from rem_card.services.operblock_medication_presets import (
        load_operblock_medication_presets,
        save_operblock_medication_presets,
    )
    from rem_card.services.operblock_quick_order_buttons import (
        load_operblock_quick_order_buttons,
        save_operblock_quick_order_buttons,
    )
    from rem_card.services.operblock_quick_orders import (
        load_operblock_quick_orders,
        save_operblock_quick_orders,
    )
    from rem_card.services.operblock_team import load_operblock_team, save_operblock_team
    from rem_card.services.settings.settings_service import (
        OPERBLOCK_SETTINGS_KEY,
        OPERBLOCK_SETTINGS_SCOPE,
        OPERBLOCK_TEAM_APP_KEY,
        SettingsService,
        configure_settings_service,
        reset_settings_service,
    )

    source_baza = os.path.join(temp_root, "SourceBaza")
    source_db_path = os.path.join(source_baza, "settings", "remcard_settings.db")
    target_baza = os.path.join(temp_root, "TargetBaza")
    preserve_baza = os.path.join(temp_root, "PreserveBaza")
    snapshot_path = os.path.join(temp_root, SETTINGS_RELEASE_SNAPSHOT_FILE)

    expected_team = [
        {"name": "Регресс Хирург", "position": "Хирург"},
        {"name": "Регресс Оперсестра", "position": "Операционная медсестра"},
        {"name": "Регресс Анестезистка", "position": "Анестезистка"},
    ]
    expected_anesthesia = [{"label": "Регрессионное пособие"}]
    expected_buttons = [
        {"key": "bolus", "label": "Болюсы", "built_in": True, "sort_order": 10},
        {"key": "extra:regional", "label": "Регионарные", "built_in": False, "sort_order": 60},
    ]
    expected_quick_orders = [
        {
            "drug_name": "Regressini",
            "label": "Regressini",
            "group": 1,
            "kind": "bolus",
            "doses": ["1 мл", "2 мл"],
        }
    ]
    expected_presets = [
        {
            "preset_id": "manual:bolus:regressini",
            "label": "Regressini",
            "display_name": "S. Regressini",
            "aliases": ["регресс"],
            "kind": "bolus",
            "drug_group": "regression_group",
            "enabled": True,
            "sort_order": 10,
        }
    ]

    def app_setting_payload(db_path: str, key: str) -> dict:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT value_json FROM app_settings WHERE scope = ? AND key = ?",
                (OPERBLOCK_SETTINGS_SCOPE, key),
            ).fetchone()
            return json.loads(row["value_json"]) if row else {}
        finally:
            conn.close()

    try:
        service = configure_settings_service(settings_db_path=source_db_path)
        service.ensure_ready()
        before_version, before_hash = service.get_catalog_version(OPERBLOCK_SETTINGS_KEY)
        before_change_id = service.latest_change_id()

        saved_team = save_operblock_team(expected_team)
        saved_anesthesia = save_operblock_anesthesia_types(expected_anesthesia)
        saved_buttons = save_operblock_quick_order_buttons(expected_buttons)
        saved_quick_orders = save_operblock_quick_orders(expected_quick_orders)
        saved_presets = save_operblock_medication_presets(expected_presets)

        after_version, after_hash = service.get_catalog_version(OPERBLOCK_SETTINGS_KEY)
        after_change_id = service.latest_change_id()
        if after_version <= before_version:
            return False, "operblock_settings catalog version did not advance"
        if after_hash == before_hash:
            return False, "operblock_settings content hash did not change"
        if after_change_id <= before_change_id:
            return False, "settings change log did not advance for operblock settings"

        reset_settings_service()
        configure_settings_service(settings_db_path=source_db_path).ensure_ready()
        if load_operblock_team() != saved_team:
            return False, f"team was not loaded from shared settings DB: {load_operblock_team()!r}"
        if load_operblock_anesthesia_types() != saved_anesthesia:
            return False, "anesthesia types were not loaded from shared settings DB"
        loaded_buttons = load_operblock_quick_order_buttons()
        if loaded_buttons != saved_buttons:
            return False, f"quick buttons were not loaded from shared settings DB: {loaded_buttons!r}"
        if load_operblock_quick_orders() != saved_quick_orders:
            return False, "quick orders were not loaded from shared settings DB"
        loaded_presets = load_operblock_medication_presets(include_disabled=False)
        if [item.get("preset_id") for item in loaded_presets] != [item.get("preset_id") for item in saved_presets]:
            return False, f"medication presets were not loaded from shared settings DB: {loaded_presets!r}"

        export_settings_release_snapshot(source_baza, snapshot_path, release_version="regression", release_commit="")
        target_service = SettingsService(SettingsDatabase(baza_dir=target_baza))
        target_service.ensure_ready()
        report = apply_settings_release_snapshot(
            target_service.db,
            snapshot_path,
            bump_catalog_version=target_service._bump_catalog_version,
        )
        if not report.get("applied"):
            return False, f"release snapshot did not apply: {report!r}"
        target_db_path = os.path.join(target_baza, "settings", "remcard_settings.db")
        target_team = app_setting_payload(target_db_path, "team").get("items") or []
        if target_team != saved_team:
            return False, f"release snapshot did not carry operblock team: {target_team!r}"
        target_presets = app_setting_payload(target_db_path, "medication_presets").get("items") or []
        if [item.get("preset_id") for item in target_presets] != [item.get("preset_id") for item in saved_presets]:
            return False, f"release snapshot did not carry operblock presets: {target_presets!r}"

        preserve_service = SettingsService(SettingsDatabase(baza_dir=preserve_baza))
        preserve_service.ensure_ready()
        edited_team_payload = {
            "version": 1,
            "items": [{"id": "member_runtime", "name": "Рабочий ПК Хирург", "position": "Хирург", "sort_order": 10}],
        }
        preserve_service.set_app_setting(
            OPERBLOCK_SETTINGS_SCOPE,
            OPERBLOCK_TEAM_APP_KEY,
            edited_team_payload,
            catalog_key=OPERBLOCK_SETTINGS_KEY,
            entity_type="operblock_team",
            operation="runtime_edit",
            changed_by_role="doctor",
        )
        preserve_report = apply_settings_release_snapshot(
            preserve_service.db,
            snapshot_path,
            bump_catalog_version=preserve_service._bump_catalog_version,
        )
        if int(preserve_report.get("preserved_rows") or 0) <= 0:
            return False, f"newer runtime app_settings row was not reported as preserved: {preserve_report!r}"
        preserve_db_path = os.path.join(preserve_baza, "settings", "remcard_settings.db")
        preserved_team = app_setting_payload(preserve_db_path, "team").get("items") or []
        if preserved_team != edited_team_payload["items"]:
            return False, f"newer runtime operblock team was overwritten by release snapshot: {preserved_team!r}"
        return True, "ok"
    finally:
        reset_settings_service()

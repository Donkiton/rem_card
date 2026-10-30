"""Schema 29 advances only the mandatory release contract of a healthy 28 DB."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from rem_card.app import client_build_profile, schema_migration_guard, startup_db_guard
from rem_card.app import unified_db_schema as schema


@pytest.fixture
def database_28(tmp_path, monkeypatch):
    monkeypatch.setattr(client_build_profile, "NO_DATABASE_UPGRADES", False)
    monkeypatch.setattr(schema_migration_guard, "APP_VERSION", "5.1.15")
    monkeypatch.setattr(startup_db_guard, "APP_VERSION", "5.1.15")
    monkeypatch.setattr(startup_db_guard, "REQUIRED_CLIENT_POLICY_VERSION", "5.1.15")
    path = tmp_path / "medical.db"
    connection = sqlite3.connect(path)
    schema.ensure_unified_schema(connection)
    connection.execute("DELETE FROM schema_migrations WHERE version > 28")
    connection.execute("UPDATE meta SET value=28 WHERE key=?", (schema.SCHEMA_FASTPATH_META_KEY,))
    connection.execute("INSERT INTO patients(full_name) VALUES ('synthetic release migration')")
    connection.execute(
        "INSERT INTO admissions(patient_id,bed_number,history_number,admission_datetime) "
        "VALUES(1,1,'synthetic','2026-10-30 08:00:00')"
    )
    connection.commit()
    policy = tmp_path / "config" / "client_policy.json"
    policy.parent.mkdir()
    policy.write_text(json.dumps({"min_client_version": "5.1.11", "custom": True}), encoding="utf-8")
    kwargs = dict(db_path=str(path), backup_dir=str(tmp_path / "backups"),
                  policy_path=str(policy), lock_path=str(tmp_path / "db.lock"))
    try:
        yield connection, policy, kwargs
    finally:
        connection.close()


def _clinical_content(connection):
    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('meta','schema_migrations') "
        "ORDER BY name"
    )]
    return {table: connection.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall() for table in tables}


def test_schema_28_upgrade_changes_only_metadata_after_verified_backup(database_28):
    connection, policy, kwargs = database_28
    before = _clinical_content(connection)
    objects = connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    statements = []
    connection.set_trace_callback(statements.append)
    result = schema_migration_guard.ensure_unified_schema_with_migration_backup(connection, **kwargs)
    connection.set_trace_callback(None)
    assert result.migrated and result.policy_updated
    assert schema.is_unified_schema_ready(connection)
    assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (29,)
    assert connection.execute("SELECT value FROM meta WHERE key=?", (schema.SCHEMA_FASTPATH_META_KEY,)).fetchone() == (29,)
    assert _clinical_content(connection) == before
    assert connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall() == objects
    assert not any(sql.lstrip().upper().startswith(("CREATE ", "ALTER ", "DROP ", "VACUUM")) for sql in statements)
    persisted_policy = json.loads(policy.read_text(encoding="utf-8"))
    assert persisted_policy["min_client_version"] == "5.1.15"
    assert persisted_policy["custom"] is True
    with sqlite3.connect(result.backup_path) as backup:
        assert backup.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert backup.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert backup.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (28,)
        assert _clinical_content(backup) == before
    assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    after = list(connection.iterdump())
    backups = list(Path(kwargs["backup_dir"]).glob("*.db"))
    again = schema_migration_guard.ensure_unified_schema_with_migration_backup(connection, **kwargs)
    assert not again.migrated and not again.policy_updated
    assert list(connection.iterdump()) == after
    assert list(Path(kwargs["backup_dir"]).glob("*.db")) == backups


def test_failed_metadata_upgrade_rolls_back_and_preserves_old_backup(database_28, monkeypatch):
    connection, policy, kwargs = database_28
    before = list(connection.iterdump())
    original = schema._set_meta_int_value

    def fail_after_marker(conn, key, value):
        original(conn, key, value)
        if key == schema.SCHEMA_FASTPATH_META_KEY and value == 29:
            raise sqlite3.OperationalError("synthetic metadata failure")

    monkeypatch.setattr(schema, "_set_meta_int_value", fail_after_marker)
    with pytest.raises(sqlite3.OperationalError, match="metadata failure"):
        schema_migration_guard.ensure_unified_schema_with_migration_backup(connection, **kwargs)
    assert not connection.in_transaction
    assert list(connection.iterdump()) == before
    # A failed migration deliberately keeps the raised access floor: an older
    # client must not race an incomplete upgrade. A corrected new client retries.
    assert json.loads(policy.read_text(encoding="utf-8"))["min_client_version"] == "5.1.15"
    backups = list(Path(kwargs["backup_dir"]).glob("*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (28,)


def test_no_upgrade_build_rejects_schema_28_without_any_changes(database_28, monkeypatch):
    connection, policy, kwargs = database_28
    before, policy_before = list(connection.iterdump()), policy.read_bytes()
    monkeypatch.setattr(client_build_profile, "NO_DATABASE_UPGRADES", True)
    with pytest.raises(RuntimeError, match="schema incompatible"):
        schema_migration_guard.ensure_unified_schema_with_migration_backup(connection, **kwargs)
    assert list(connection.iterdump()) == before
    assert policy.read_bytes() == policy_before
    assert not Path(kwargs["backup_dir"]).exists()


@pytest.mark.parametrize("client", ["5.1.11", "5.1.12", "5.1.13", "5.1.14"])
def test_previous_clients_are_blocked_by_mandatory_515_policy(database_28, monkeypatch, client):
    connection, policy, kwargs = database_28
    schema_migration_guard.ensure_unified_schema_with_migration_backup(connection, **kwargs)
    before = list(connection.iterdump())
    monkeypatch.setattr(startup_db_guard, "APP_VERSION", client)
    with pytest.raises(startup_db_guard.StartupPolicyError, match="устарела"):
        startup_db_guard._load_or_create_client_policy(str(policy.parent.parent), None)
    assert list(connection.iterdump()) == before

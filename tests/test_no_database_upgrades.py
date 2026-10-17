import sqlite3
from types import SimpleNamespace
from contextlib import nullcontext

import pytest

from rem_card.app import client_build_profile, unified_db_schema
from rem_card.app.schema_migration_guard import ensure_unified_schema_with_migration_backup
from rem_card.app.startup_db_guard import update_client_policy_min_version, _load_or_create_client_policy
from rem_card.app.operblock_schema import ensure_operblock_schema, _apply_operblock_schema
from rem_card.data.settings import settings_schema


def test_compatible_database_and_policy_are_unchanged(tmp_path, monkeypatch):
    conn = sqlite3.connect(':memory:')
    unified_db_schema.ensure_unified_schema(conn)
    conn.execute('DELETE FROM meta WHERE key = ?', (unified_db_schema.SCHEMA_FASTPATH_META_KEY,))
    conn.commit()
    before = list(conn.iterdump())
    policy = tmp_path / 'client_policy.json'
    policy.write_text('{"min_client_version": "5.1.0"}', encoding='utf-8')
    original_policy = policy.read_bytes()
    monkeypatch.setattr(client_build_profile, 'NO_DATABASE_UPGRADES', True)
    for _ in range(2):
        result = ensure_unified_schema_with_migration_backup(
            conn, db_path='unused', backup_dir=str(tmp_path / 'backup'), policy_path=str(policy))
        assert not result.migrated and not result.policy_updated
        unified_db_schema.ensure_unified_schema(conn)
        assert not update_client_policy_min_version(str(policy), '5.1.1')
    assert list(conn.iterdump()) == before
    assert policy.read_bytes() == original_policy
    assert not (tmp_path / 'backup').exists()


def test_loading_policy_does_not_create_or_repair_file(tmp_path, monkeypatch):
    monkeypatch.setattr(client_build_profile, 'NO_DATABASE_UPGRADES', True)
    _load_or_create_client_policy(str(tmp_path), None)
    assert not (tmp_path / 'config').exists()
    config = tmp_path / 'config'
    config.mkdir()
    policy = config / 'client_policy.json'
    policy.write_text('{}', encoding='utf-8')
    _load_or_create_client_policy(str(tmp_path), None)
    assert policy.read_text(encoding='utf-8') == '{}'


@pytest.mark.parametrize('marker', [26, None])
def test_version_516_database_stays_writable_without_migration(tmp_path, monkeypatch, marker):
    conn = sqlite3.connect(':memory:')
    try:
        unified_db_schema.ensure_unified_schema(conn)
        conn.execute('DROP INDEX idx_admin_committed_planned_expr_order_id')
        conn.execute('DELETE FROM schema_migrations WHERE version >= 27')
        conn.execute("INSERT OR IGNORE INTO schema_migrations (version, note) VALUES (26, '5.1.6')")
        conn.execute('DELETE FROM meta WHERE key = ?', (unified_db_schema.SCHEMA_FASTPATH_META_KEY,))
        if marker is not None:
            conn.execute('INSERT INTO meta (key, value) VALUES (?, ?)',
                         (unified_db_schema.SCHEMA_FASTPATH_META_KEY, marker))
        conn.commit()
        assert not unified_db_schema.is_unified_schema_ready(conn)
        policy = tmp_path / 'config' / 'client_policy.json'
        policy.parent.mkdir()
        policy.write_text('{"min_client_version": "5.1.6"}', encoding='utf-8')
        original_policy = policy.read_bytes()
        before = list(conn.iterdump())
        monkeypatch.setattr(client_build_profile, 'NO_DATABASE_UPGRADES', True)
        for _ in range(2):
            result = ensure_unified_schema_with_migration_backup(
                conn, db_path='unused', backup_dir=str(tmp_path / 'backup'), policy_path=str(policy),
                runtime_context=SimpleNamespace(mode='network', is_network=True))
            assert not result.migrated and not result.policy_updated
            unified_db_schema.ensure_unified_schema(conn)
            assert unified_db_schema.is_unified_schema_ready(conn)
            assert not update_client_policy_min_version(str(policy), '5.1.7')
        assert list(conn.iterdump()) == before
        assert policy.read_bytes() == original_policy
        assert not (tmp_path / 'backup').exists()
        # An old client still passes the existing access policy after new startup.
        monkeypatch.setattr('rem_card.app.startup_db_guard.APP_VERSION', '5.1.6')
        _load_or_create_client_policy(str(tmp_path), None)
        assert policy.read_bytes() == original_policy
        conn.execute("INSERT INTO patients (full_name) VALUES ('synthetic compatibility check')")
        conn.commit()
        assert conn.execute('SELECT full_name FROM patients').fetchone() == ('synthetic compatibility check',)
    finally:
        conn.close()


@pytest.mark.parametrize('defect', ['old_migration', 'required_index', 'required_column'])
def test_no_upgrade_compatibility_keeps_required_storage_checks(monkeypatch, defect):
    conn = sqlite3.connect(':memory:')
    try:
        unified_db_schema.ensure_unified_schema(conn)
        conn.execute('DROP INDEX idx_admin_committed_planned_expr_order_id')
        conn.execute('DELETE FROM schema_migrations WHERE version >= 26')
        conn.execute("INSERT OR IGNORE INTO schema_migrations (version, note) VALUES (?, 'synthetic')",
                     (25 if defect == 'old_migration' else 26,))
        conn.execute('DELETE FROM meta WHERE key = ?', (unified_db_schema.SCHEMA_FASTPATH_META_KEY,))
        if defect == 'required_index':
            conn.execute('DROP INDEX idx_admin_committed_order_planned_id')
        if defect == 'required_column':
            conn.execute('ALTER TABLE patients RENAME COLUMN birth_date TO incompatible_name')
        conn.commit()
        before = list(conn.iterdump())
        monkeypatch.setattr(client_build_profile, 'NO_DATABASE_UPGRADES', True)
        with pytest.raises(RuntimeError, match='schema incompatible'):
            unified_db_schema.ensure_unified_schema(conn)
        assert list(conn.iterdump()) == before
    finally:
        conn.close()


def test_private_operblock_database_can_initialize_without_unlocking_shared_database(tmp_path, monkeypatch):
    from rem_card.app.db_runtime_context import build_operblock_offline_runtime_context
    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.app.operblock_schema import is_operblock_schema_ready

    context = build_operblock_offline_runtime_context(str(tmp_path / 'private-operblock'))
    monkeypatch.setattr(client_build_profile, 'NO_DATABASE_UPGRADES', True)
    manager = DatabaseManager(context.medical_db_path, runtime_context=context)
    try:
        result = ensure_operblock_schema(manager)
        assert result.migrated
        assert unified_db_schema.is_unified_schema_ready(manager._remcard_conn)
        assert is_operblock_schema_ready(manager._remcard_conn)
        assert client_build_profile.database_upgrades_disabled()
        manager.run_write_operation(
            lambda cursor: cursor.execute("INSERT INTO patients (full_name) VALUES ('synthetic local case')"))
        shared = sqlite3.connect(tmp_path / 'shared.db')
        try:
            with pytest.raises(RuntimeError, match='schema incompatible'):
                ensure_unified_schema_with_migration_backup(
                    shared, db_path='unused', backup_dir=str(tmp_path / 'shared-backups'), runtime_context=context)
            assert list(shared.iterdump()) == ['BEGIN TRANSACTION;', 'COMMIT;']
        finally:
            shared.close()
        assert not (tmp_path / 'shared-backups').exists()
    finally:
        assert manager.close(timeout_sec=3)


def test_incompatible_database_rejected_without_writes(tmp_path, monkeypatch):
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE old_data (value TEXT)')
    conn.execute("INSERT INTO old_data VALUES ('preserved')")
    conn.commit()
    before = list(conn.iterdump())
    monkeypatch.setattr(client_build_profile, 'NO_DATABASE_UPGRADES', True)
    controller = SimpleNamespace(connection_guard=lambda connection: nullcontext())
    manager = SimpleNamespace(_remcard_conn=conn, write_controller=controller)
    calls = [
        lambda: unified_db_schema.ensure_unified_schema(conn),
        lambda: ensure_unified_schema_with_migration_backup(
            conn, db_path='unused', backup_dir=str(tmp_path / 'backup')),
        lambda: ensure_operblock_schema(manager),
        lambda: _apply_operblock_schema(conn.cursor()),
        lambda: settings_schema.apply_schema(conn),
    ]
    for call in calls:
        with pytest.raises(RuntimeError, match='schema incompatible'):
            call()
        assert list(conn.iterdump()) == before
    assert not (tmp_path / 'backup').exists()


def test_compatible_settings_and_operblock_do_not_write(monkeypatch):
    settings = sqlite3.connect(':memory:')
    medical = sqlite3.connect(':memory:')
    try:
        settings_schema.apply_schema(settings)
        unified_db_schema.ensure_unified_schema(medical)
        _apply_operblock_schema(medical.cursor())
        medical.commit()
        before = [list(conn.iterdump()) for conn in (settings, medical)]
        monkeypatch.setattr(client_build_profile, 'NO_DATABASE_UPGRADES', True)
        controller = SimpleNamespace(connection_guard=lambda connection: nullcontext())
        manager = SimpleNamespace(_remcard_conn=medical, write_controller=controller)
        settings_schema.apply_schema(settings)
        result = ensure_operblock_schema(manager)
        _apply_operblock_schema(medical.cursor())
        assert not result.migrated
        assert [list(conn.iterdump()) for conn in (settings, medical)] == before
    finally:
        settings.close()
        medical.close()


@pytest.mark.parametrize('embedded,expected,ok', [
    (True, '1', True), (False, '1', False), (True, '0', False),
    (False, '0', True), (True, 'invalid', False),
])
def test_smoke_checks_embedded_policy_without_overriding_it(monkeypatch, embedded, expected, ok):
    monkeypatch.setattr(client_build_profile, 'NO_DATABASE_UPGRADES', embedded)
    monkeypatch.setenv('REMCARD_SMOKE_EXPECT_NO_DATABASE_UPGRADES', expected)
    assert client_build_profile.compiled_profile_matches_expectation() is ok
    assert client_build_profile.database_upgrades_disabled() is embedded

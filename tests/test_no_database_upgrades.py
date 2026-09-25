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

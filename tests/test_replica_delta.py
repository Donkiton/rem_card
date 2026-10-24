from __future__ import annotations

import copy
import sqlite3

import pytest

from rem_card.app.replica_delta import ReplicaDeltaError, apply_delta, collect_delta
from rem_card.app.unified_db_schema import SCHEMA_FASTPATH_REV, ensure_unified_schema


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE meta(key TEXT PRIMARY KEY, value);
INSERT INTO meta VALUES('db_cycle_started_at', 'cycle-a');
INSERT INTO meta VALUES('unified_schema_fastpath_rev', 27);
CREATE TABLE change_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_name TEXT NOT NULL,
    entity_id INTEGER,
    admission_id INTEGER,
    action TEXT NOT NULL,
    changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    changed_by TEXT,
    version INTEGER
);
CREATE TABLE items(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    body BLOB
);
CREATE TABLE children(
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id),
    note TEXT
);
CREATE TABLE preferences(
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY(owner, name)
);
CREATE TABLE vital_settings(
    admission_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(admission_id, date)
);
CREATE TABLE audit_rows(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER,
    action TEXT
);
CREATE TRIGGER trg_items_version_ins AFTER INSERT ON items BEGIN
  INSERT INTO change_log(entity_name, entity_id, action) VALUES('items', NEW.id, 'insert');
END;
CREATE TRIGGER trg_items_version_upd AFTER UPDATE ON items BEGIN
  INSERT INTO change_log(entity_name, entity_id, action) VALUES('items', NEW.id, 'update');
END;
CREATE TRIGGER trg_items_version_del AFTER DELETE ON items BEGIN
  INSERT INTO change_log(entity_name, entity_id, action) VALUES('items', OLD.id, 'delete');
END;
CREATE TRIGGER trg_items_audit AFTER UPDATE ON items BEGIN
  INSERT INTO audit_rows(item_id, action) VALUES(NEW.id, 'update');
END;
CREATE TRIGGER trg_vital_settings_version_ins AFTER INSERT ON vital_settings BEGIN
  INSERT INTO change_log(entity_name, entity_id, action)
  VALUES('vital_settings', NEW.rowid, 'insert');
END;
CREATE TRIGGER trg_vital_settings_version_upd AFTER UPDATE ON vital_settings BEGIN
  INSERT INTO change_log(entity_name, entity_id, action)
  VALUES('vital_settings', NEW.rowid, 'update');
END;
CREATE TRIGGER trg_vital_settings_version_del AFTER DELETE ON vital_settings BEGIN
  INSERT INTO change_log(entity_name, entity_id, action)
  VALUES('vital_settings', OLD.rowid, 'delete');
END;
"""


def make_database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO items(code, body) VALUES('old', X'00FF')")
    conn.execute("INSERT INTO preferences VALUES('ui', 'theme', 'dark')")
    conn.execute("INSERT INTO vital_settings VALUES(10, '2026-10-17', 1)")
    conn.commit()
    return conn


def clone(source: sqlite3.Connection) -> sqlite3.Connection:
    destination = sqlite3.connect(":memory:")
    source.backup(destination)
    destination.execute("PRAGMA foreign_keys=ON")
    return destination


def logical_dump(conn: sqlite3.Connection):
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        if row[0] != "sqlite_stat1"
    ]
    result = {}
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        rows = conn.execute(f"SELECT * FROM {quoted}").fetchall()
        result[table] = sorted(rows, key=repr)
    return result


def generous_delta(source, local):
    return collect_delta(
        source,
        local,
        max_events=1000,
        max_rows=1000,
        max_bytes=1024 * 1024,
        max_fraction=1.0,
    )


def test_delta_matches_full_backup_after_two_commits_and_all_mutation_kinds():
    source = make_database()
    local = clone(source)

    source.execute("UPDATE items SET code='changed', body=? WHERE id=1", (b"\x10\x00",))
    source.execute("INSERT INTO items(code, body) VALUES('new', ?)", (b"blob\x00value",))
    source.execute("INSERT INTO children VALUES(20, 2, 'child')")
    source.execute("UPDATE preferences SET value='light' WHERE owner='ui' AND name='theme'")
    source.commit()
    source.execute("DELETE FROM items WHERE id=1")
    source.execute("INSERT INTO preferences VALUES('doctor', 'scale', '{\"x\":1}')")
    source.execute("UPDATE vital_settings SET date='2026-10-18' WHERE admission_id=10")
    source.commit()

    package = generous_delta(source, local)
    assert package["status"] == "delta"
    assert package["state"]["change_cursor"] > package["base_state"]["change_cursor"]
    apply_delta(local, package)

    assert logical_dump(local) == logical_dump(source)
    assert local.execute("PRAGMA foreign_key_check").fetchall() == []
    assert generous_delta(source, local)["status"] == "unchanged"
    with pytest.raises(ReplicaDeltaError, match="кандидата"):
        apply_delta(local, package)


def test_streamed_reconciliation_uses_sqlite_binary_key_order_and_detects_deletions():
    source = make_database()
    source.executescript("CREATE TABLE mixed_keys(a, b, body, PRIMARY KEY(a,b));")
    source.executemany("INSERT INTO mixed_keys VALUES(?,?,?)", [
        (0, "one", "zero"), (1, "one", "integer"), (1.5, "one", "real"),
        ("a", "one", "text"), (b"a", "one", "blob"), ("A", "two", "binary"),
    ])
    source.commit()
    local = clone(source)
    source.execute("UPDATE mixed_keys SET body='updated' WHERE a=0")
    source.execute("DELETE FROM mixed_keys WHERE a=?", (b"a",))
    source.execute("UPDATE mixed_keys SET a='Z' WHERE a='a'")
    source.commit()
    package = collect_delta(source, local, max_fraction=1)
    assert package["status"] == "delta", package
    apply_delta(local, package)
    assert logical_dump(local) == logical_dump(source)


@pytest.mark.parametrize("limit,reason", [
    ({"max_scan_rows": 1}, "reconciliation_scan_limit"),
    ({"max_scan_bytes": 1}, "reconciliation_byte_limit"),
])
def test_reconciliation_scan_budgets_preserve_baseline_and_table_diagnostics(limit, reason):
    source = make_database()
    local = clone(source)
    before = logical_dump(local)
    result = collect_delta(source, local, max_fraction=1, **limit)
    assert result["status"] == "full_required"
    assert result["reason"] == reason
    assert result["metrics"]["table"]
    assert result["metrics"]["source_rows"] >= 0
    assert logical_dump(local) == before


def test_reconciliation_refuses_unsafe_nullable_primary_key():
    source = make_database()
    source.executescript("CREATE TABLE nullable_keys(a, b, PRIMARY KEY(a,b));"
                         "INSERT INTO nullable_keys VALUES(NULL,'x');"
                         "INSERT INTO nullable_keys VALUES(NULL,'x');")
    local = clone(source)
    result = collect_delta(source, local, max_fraction=1)
    assert (result["status"], result["reason"]) == ("full_required", "unsupported_table_shape")


def test_untracked_composite_update_is_seen_without_cursor_change():
    source = make_database()
    local = clone(source)
    cursor = source.execute("SELECT MAX(id) FROM change_log").fetchone()[0]
    source.execute("UPDATE preferences SET value='blue' WHERE owner='ui' AND name='theme'")
    source.commit()
    assert source.execute("SELECT MAX(id) FROM change_log").fetchone()[0] == cursor

    package = generous_delta(source, local)
    assert package["status"] == "delta"
    apply_delta(local, package)
    assert logical_dump(local) == logical_dump(source)


def test_vital_settings_uses_logical_key_even_if_source_rowid_changes():
    source = make_database()
    local = clone(source)
    source.execute("DELETE FROM vital_settings")
    source.execute("INSERT INTO vital_settings VALUES(10, '2026-10-19', 0)")
    source.commit()

    package = generous_delta(source, local)
    assert "vital_settings" in package["data"]["tables"]
    assert package["data"]["tables"]["vital_settings"]["key_columns"] == [
        "admission_id",
        "date",
    ]
    apply_delta(local, package)
    assert logical_dump(local) == logical_dump(source)


def test_trimmed_prefix_is_pruned_but_missing_new_event_requires_full():
    source = make_database()
    local = clone(source)
    source.execute("UPDATE items SET code='v2' WHERE id=1")
    source.commit()
    first_new = local.execute("SELECT MAX(id) FROM change_log").fetchone()[0] + 1
    source.execute("DELETE FROM change_log WHERE id < ?", (first_new,))
    source.commit()
    package = generous_delta(source, local)
    assert package["status"] == "delta"
    apply_delta(local, package)
    assert logical_dump(local) == logical_dump(source)

    behind = clone(local)
    source.execute("UPDATE items SET code='v3' WHERE id=1")
    source.execute("UPDATE items SET code='v4' WHERE id=1")
    source.commit()
    next_id = behind.execute("SELECT MAX(id) FROM change_log").fetchone()[0] + 1
    source.execute("DELETE FROM change_log WHERE id=?", (next_id,))
    source.commit()
    result = generous_delta(source, behind)
    assert result["status"] == "full_required"
    assert result["reason"] == "change_log_gap"


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        ("UPDATE meta SET value='cycle-b' WHERE key='db_cycle_started_at'", "database_cycle_changed"),
        ("UPDATE meta SET value=28 WHERE key='unified_schema_fastpath_rev'", "schema_revision_changed"),
    ],
)
def test_cycle_and_schema_revision_changes_require_full(statement, reason):
    source = make_database()
    local = clone(source)
    source.execute(statement)
    source.commit()
    result = generous_delta(source, local)
    assert (result["status"], result["reason"]) == ("full_required", reason)


def test_rolled_back_source_write_is_unchanged_and_local_cursor_ahead_is_rejected():
    source = make_database()
    local = clone(source)
    source.execute("UPDATE items SET code='rolled-back' WHERE id=1")
    source.rollback()
    assert generous_delta(source, local)["status"] == "unchanged"

    local.execute("INSERT INTO change_log(entity_name, entity_id, action) VALUES('items', 1, 'update')")
    local.commit()
    result = generous_delta(source, local)
    assert (result["status"], result["reason"]) == ("full_required", "local_cursor_ahead")


def test_limits_and_unsupported_schema_choose_full_without_applying():
    source = make_database()
    local = clone(source)
    source.execute("UPDATE items SET code='large', body=? WHERE id=1", (b"x" * 4096,))
    source.commit()
    assert collect_delta(source, local, max_bytes=64, max_fraction=1.0)["reason"] == "byte_limit"
    assert collect_delta(source, local, max_bytes=1_000_000, max_fraction=0.0)["reason"] == "fraction_limit"

    local.execute("CREATE INDEX local_only ON preferences(value)")
    local.commit()
    result = generous_delta(source, local)
    assert (result["status"], result["reason"]) == ("full_required", "schema_mismatch")


def test_large_blob_is_rejected_by_size_preflight_before_payload_select():
    source = make_database()
    local = clone(source)
    source.execute("UPDATE items SET body=? WHERE id=1", (b"x" * (5 * 1024 * 1024),))
    source.commit()
    statements = []
    source.set_trace_callback(statements.append)

    result = collect_delta(
        source,
        local,
        max_events=100,
        max_rows=100,
        max_bytes=4 * 1024 * 1024,
        max_fraction=1.0,
    )

    assert (result["status"], result["reason"], result["data"]) == (
        "full_required",
        "byte_limit",
        {},
    )
    normalized = [statement.casefold().strip() for statement in statements]
    assert not any(
        statement.startswith('select "id", "code", "body" from "items"')
        for statement in normalized
    )
    assert any("sum(" in statement and 'from "items"' in statement for statement in normalized)


def test_apply_failure_rolls_back_rows_sequence_and_trigger_definitions():
    source = make_database()
    local = clone(source)
    source.execute("UPDATE items SET code='changed' WHERE id=1")
    source.commit()
    package = generous_delta(source, local)
    before = logical_dump(local)
    triggers_before = local.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
    ).fetchall()
    broken = copy.deepcopy(package)
    broken["data"]["tables"]["children"] = {
        "columns": ["id", "item_id", "note"],
        "key_kind": "pk",
        "key_columns": ["id"],
        "upserts": [(99, 999, "broken")],
        "deletes": [],
    }

    with pytest.raises(ReplicaDeltaError, match="внешнего ключа"):
        apply_delta(local, broken)
    assert logical_dump(local) == before
    assert local.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
    ).fetchall() == triggers_before


def test_full_unified_synthetic_schema_is_supported_without_runtime_database_paths():
    source = sqlite3.connect(":memory:")
    ensure_unified_schema(source, allow_local_schema_upgrades=True)
    source.execute("INSERT INTO meta(key, value) VALUES('db_cycle_started_at', 'synthetic')")
    source.commit()
    local = clone(source)

    result = generous_delta(source, local)
    assert result["status"] == "unchanged"
    assert result["state"] == {
        "db_cycle": "synthetic",
        "change_cursor": 0,
        "schema_revision": str(SCHEMA_FASTPATH_REV),
    }


@pytest.mark.parametrize("mutate_untracked", [False, True])
def test_large_untracked_history_reconciles_small_changes_without_full_copy(mutate_untracked):
    source = make_database()
    source.executemany(
        "INSERT INTO audit_rows(item_id, action) VALUES(1, ?)",
        [(f"historical-{index}",) for index in range(6001)],
    )
    source.commit()
    local = clone(source)
    source.execute("UPDATE items SET code='new-value' WHERE id=1")
    if mutate_untracked:
        source.execute("UPDATE audit_rows SET action='corrected' WHERE id=17")
        source.execute("DELETE FROM audit_rows WHERE id=19")
        source.execute("INSERT INTO audit_rows(item_id, action) VALUES(1, 'new-tail')")
    source.commit()

    result = collect_delta(source, local, max_fraction=1.0)

    assert result["status"] == "delta", result
    assert result["metrics"]["row_count"] < 30
    apply_delta(local, result)
    assert logical_dump(local) == logical_dump(source)


def test_empty_untracked_tables_do_not_scan_bytes_or_payload():
    source = make_database()
    local = clone(source)
    statements = []
    source.set_trace_callback(statements.append)

    result = generous_delta(source, local)

    assert result["status"] == "unchanged"
    for table in ("audit_rows", "children"):
        reads = [sql.casefold() for sql in statements if f'from "{table}"' in sql.casefold()]
        assert reads
        assert all("count(*)" in sql for sql in reads)
    assert logical_dump(local) == logical_dump(source)


def test_journal_row_budget_chooses_full_before_scanning_untracked_history():
    source = make_database()
    local = clone(source)
    source.execute("UPDATE items SET code='edited' WHERE id=1")
    source.commit()
    before_source = logical_dump(source)
    before_local = logical_dump(local)
    statements = []
    source.set_trace_callback(statements.append)

    result = collect_delta(source, local, max_rows=0, max_fraction=1)

    assert (result["status"], result["reason"]) == ("full_required", "row_limit")
    assert not any('from "audit_rows"' in sql.casefold() for sql in statements)
    assert source.execute("PRAGMA query_only").fetchone() == (0,)
    assert not source.in_transaction and not local.in_transaction
    assert logical_dump(source) == before_source
    assert logical_dump(local) == before_local

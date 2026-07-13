from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from rem_card.data.dao.orders_dao import OrdersDAO
from rem_card.data.dto.remcard_dto import AdministrationDTO, OrderDTO, OrderStatus, OrderType
from rem_card.services.order_service import OrderConflictError, OrderService


class MemoryDraftDb:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.transaction_count = 0
        self.rollback_count = 0
        self._create_schema()

    def _create_schema(self):
        self.conn.executescript(
            """
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admission_id INTEGER NOT NULL,
                datetime TEXT NOT NULL,
                text TEXT NOT NULL,
                drug_key TEXT,
                latin TEXT,
                type TEXT,
                status TEXT DEFAULT 'active',
                dose_value REAL,
                dose_unit TEXT,
                is_per_kg INTEGER,
                frequency INTEGER,
                specific_times TEXT,
                rate_ml_h REAL,
                volume_total REAL,
                duration_min INTEGER,
                sort_order INTEGER DEFAULT 0,
                draft_sort_order INTEGER,
                is_finalized INTEGER DEFAULT 0,
                is_committed INTEGER DEFAULT 0,
                revision INTEGER DEFAULT 0,
                created_at TEXT,
                comment TEXT,
                last_modified_by TEXT,
                updated_at TEXT
            );
            CREATE TABLE administrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                chain_id TEXT,
                big_chain_id TEXT,
                cell_role TEXT NOT NULL,
                planned_time TEXT NOT NULL,
                actual_time TEXT,
                performer_id INTEGER,
                status TEXT NOT NULL,
                version INTEGER DEFAULT 0,
                comment TEXT,
                dose_given REAL,
                volume_ml REAL DEFAULT 0,
                is_committed INTEGER DEFAULT 0,
                last_modified_by TEXT,
                updated_at TEXT
            );
            CREATE TABLE transfusions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT,
                admission_id INTEGER,
                source_admin_id INTEGER
            );
            """
        )
        self.conn.commit()

    @contextmanager
    def remcard_transaction(self, source="remcard_tx", write_options=None):
        del source, write_options
        self.transaction_count += 1
        cursor = self.conn.cursor()
        try:
            cursor.execute("BEGIN")
            yield cursor
            self.conn.commit()
        except Exception:
            self.rollback_count += 1
            self.conn.rollback()
            raise
        finally:
            cursor.close()

    def fetch_all_remcard(self, query, params=(), **_kwargs):
        return self.conn.execute(query, params).fetchall()

    def fetch_one_remcard(self, query, params=(), **_kwargs):
        return self.conn.execute(query, params).fetchone()

    def execute_remcard(self, query, params=()):
        result = self.conn.execute(query, params)
        self.conn.commit()
        return result


@pytest.fixture()
def draft_context():
    db = MemoryDraftDb()
    shift = datetime(2026, 7, 13, 8, 0)
    planned = shift + timedelta(hours=2)
    cursor = db.conn.execute(
        """
        INSERT INTO orders (
            admission_id, datetime, text, drug_key, latin, type, status,
            dose_value, dose_unit, is_per_kg, frequency, specific_times,
            rate_ml_h, volume_total, duration_min, sort_order, draft_sort_order,
            is_committed, revision, created_at, comment, last_modified_by, updated_at
        ) VALUES (1, ?, 'NaCl 100 ml', 'nacl', 'NaCl', 'medication', 'active',
                  100, 'ml', 0, 1, '[]', NULL, NULL, 0, 0, NULL,
                  1, 2, ?, '', 'doctor', ?)
        """,
        (shift.isoformat(), shift.isoformat(), shift.isoformat()),
    )
    order_id = int(cursor.lastrowid)
    admin_cursor = db.conn.execute(
        """
        INSERT INTO administrations (
            order_id, cell_role, planned_time, status, version, comment,
            volume_ml, is_committed, last_modified_by, updated_at
        ) VALUES (?, 'single', ?, 'planned', 3, '', 0, 1, 'doctor', ?)
        """,
        (order_id, planned.isoformat(), shift.isoformat()),
    )
    db.conn.commit()

    baseline_order = OrdersDAO(db).get_orders(1, shift, only_committed=True)[0]
    baseline_admin = AdministrationDTO(
        id=int(admin_cursor.lastrowid),
        order_id=order_id,
        cell_role="single",
        planned_time=planned,
        status="planned",
        version=3,
        is_committed=1,
    )
    return db, OrderService(OrdersDAO(db)), shift, baseline_order, baseline_admin


def test_local_patch_uses_one_transaction_and_writes_only_final_state(draft_context):
    db, service, shift, baseline_order, baseline_admin = draft_context
    baseline_counts = tuple(
        db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("orders", "administrations")
    )
    assert db.transaction_count == 0

    edited = deepcopy(baseline_order)
    edited.dose_value = 200
    edited.is_committed = 0
    new_order = OrderDTO(
        id=-1,
        admission_id=1,
        drug_key="glucose",
        latin="Glucose",
        type=OrderType.MEDICATION,
        status=OrderStatus.ACTIVE,
        dose_value=500,
        dose_unit="ml",
        is_committed=0,
        created_at=shift,
    )
    existing_key = (baseline_order.id, baseline_admin.planned_time.isoformat())
    new_time = shift + timedelta(hours=4)
    new_key = (-1, new_time.isoformat())
    deleted = deepcopy(baseline_admin)
    deleted.id = -2
    deleted.status = "deleted"
    deleted.is_committed = 0
    new_admin = AdministrationDTO(
        id=-3,
        order_id=-1,
        cell_role="single",
        planned_time=new_time,
        status="planned",
        is_committed=0,
    )

    # Building and editing the in-memory overlay has not touched SQLite.
    assert tuple(
        db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("orders", "administrations")
    ) == baseline_counts

    id_map = service.commit_local_draft(
        1,
        shift,
        orders=[edited, new_order],
        admin_map={existing_key: deleted, new_key: new_admin},
        dirty_admin_keys=[existing_key, new_key],
        baseline_admin_map={existing_key: baseline_admin},
        expected_revisions={baseline_order.id: baseline_order.revision},
    )

    assert db.transaction_count == 1
    assert id_map[-1] > 0
    rows = db.conn.execute("SELECT * FROM orders ORDER BY sort_order, id").fetchall()
    assert len(rows) == 2
    assert [int(row["is_committed"]) for row in rows] == [1, 1]
    assert float(rows[0]["dose_value"]) == 200
    latest_admins = db.conn.execute(
        """
        SELECT a.* FROM administrations a
        WHERE a.id IN (SELECT MAX(id) FROM administrations GROUP BY order_id, planned_time)
        ORDER BY order_id, planned_time
        """
    ).fetchall()
    assert {str(row["status"]) for row in latest_admins} == {"deleted", "planned"}
    assert all(int(row["is_committed"]) == 1 for row in latest_admins)
    assert db.conn.execute("SELECT COUNT(*) FROM orders WHERE is_committed = 0").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM administrations WHERE is_committed = 0").fetchone()[0] == 0


def test_revision_conflict_rolls_back_entire_local_patch(draft_context):
    db, service, shift, baseline_order, baseline_admin = draft_context
    db.conn.execute("UPDATE orders SET revision = revision + 1, dose_value = 150 WHERE id = ?", (baseline_order.id,))
    db.conn.commit()
    edited = deepcopy(baseline_order)
    edited.dose_value = 300
    new_order = OrderDTO(
        id=-1,
        admission_id=1,
        latin="New",
        type=OrderType.MEDICATION,
        status=OrderStatus.ACTIVE,
        dose_value=10,
        dose_unit="ml",
        is_committed=0,
        created_at=shift,
    )

    with pytest.raises(OrderConflictError):
        service.commit_local_draft(
            1,
            shift,
            orders=[edited, new_order],
            admin_map={},
            dirty_admin_keys=[],
            baseline_admin_map={},
            expected_revisions={baseline_order.id: baseline_order.revision},
        )

    assert db.transaction_count == 1
    assert db.rollback_count == 1
    assert db.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    row = db.conn.execute("SELECT dose_value, revision FROM orders WHERE id = ?", (baseline_order.id,)).fetchone()
    assert float(row["dose_value"]) == 150
    assert int(row["revision"]) == baseline_order.revision + 1


def test_committed_reader_never_exposes_legacy_uncommitted_order(draft_context):
    db, _service, shift, baseline_order, _baseline_admin = draft_context
    db.conn.execute("UPDATE orders SET dose_value = 999, is_committed = 0 WHERE id = ?", (baseline_order.id,))
    db.conn.commit()

    dao = OrdersDAO(db)
    assert dao.get_orders(1, shift, only_committed=True) == []
    assert dao.get_orders(1, shift, only_committed=False)[0].dose_value == 999


def test_concurrent_nurse_mark_rejects_doctor_cell_removal_and_rolls_back(draft_context):
    db, service, shift, baseline_order, baseline_admin = draft_context
    key = (baseline_order.id, baseline_admin.planned_time.isoformat())
    db.conn.execute(
        """
        UPDATE administrations
        SET comment = 'nurse_executed', actual_time = ?, performer_id = 77, version = version + 1
        WHERE id = ?
        """,
        ((shift + timedelta(hours=2, minutes=5)).isoformat(), baseline_admin.id),
    )
    db.conn.commit()
    tombstone = deepcopy(baseline_admin)
    tombstone.status = "deleted"
    tombstone.is_committed = 0

    with pytest.raises(OrderConflictError):
        service.commit_local_draft(
            1,
            shift,
            orders=[baseline_order],
            admin_map={key: tombstone},
            dirty_admin_keys=[key],
            baseline_admin_map={key: baseline_admin},
            expected_revisions={baseline_order.id: baseline_order.revision},
        )

    assert db.transaction_count == 1
    assert db.rollback_count == 1
    assert db.conn.execute(
        "SELECT COUNT(*) FROM administrations WHERE order_id = ? AND planned_time = ?",
        key,
    ).fetchone()[0] == 1
    latest = db.conn.execute(
        "SELECT * FROM administrations WHERE order_id = ? AND planned_time = ? ORDER BY id DESC LIMIT 1",
        key,
    ).fetchone()
    assert latest["status"] == "planned"
    assert latest["comment"] == "nurse_executed"
    assert int(latest["performer_id"]) == 77


@pytest.mark.parametrize("delete_order", [False, True], ids=["clinical-edit", "delete"])
def test_nurse_not_executed_blocks_clinical_edit_and_delete(draft_context, delete_order):
    db, service, shift, baseline_order, baseline_admin = draft_context
    db.conn.execute(
        """
        UPDATE administrations
        SET comment = 'nurse_not_executed', actual_time = ?, performer_id = 88,
            version = version + 1
        WHERE id = ?
        """,
        ((shift + timedelta(hours=2, minutes=7)).isoformat(), baseline_admin.id),
    )
    db.conn.commit()
    desired = deepcopy(baseline_order)
    if delete_order:
        desired._pending_delete = True
    else:
        desired.dose_value = 250

    with pytest.raises(OrderConflictError):
        service.commit_local_draft(
            1,
            shift,
            orders=[desired],
            admin_map={},
            dirty_admin_keys=[],
            baseline_admin_map={},
            expected_revisions={baseline_order.id: baseline_order.revision},
        )

    assert db.transaction_count == 1
    assert db.rollback_count == 1
    order_row = db.conn.execute(
        "SELECT dose_value, status, revision FROM orders WHERE id = ?",
        (baseline_order.id,),
    ).fetchone()
    assert float(order_row["dose_value"]) == 100
    assert order_row["status"] == "active"
    assert int(order_row["revision"]) == baseline_order.revision
    admin_row = db.conn.execute(
        "SELECT comment, performer_id FROM administrations WHERE id = ?",
        (baseline_admin.id,),
    ).fetchone()
    assert admin_row["comment"] == "nurse_not_executed"
    assert int(admin_row["performer_id"]) == 88


def test_concurrent_doctor_cell_shape_change_rejects_whole_patch(draft_context):
    db, service, shift, baseline_order, baseline_admin = draft_context
    key = (baseline_order.id, baseline_admin.planned_time.isoformat())
    db.conn.execute(
        """
        INSERT INTO administrations (
            order_id, big_chain_id, cell_role, planned_time, status,
            version, comment, volume_ml, is_committed, last_modified_by, updated_at
        ) VALUES (?, 'remote-chain', 'body', ?, 'planned', 4, '', 0, 1, 'doctor', ?)
        """,
        (baseline_order.id, baseline_admin.planned_time.isoformat(), shift.isoformat()),
    )
    db.conn.commit()
    tombstone = deepcopy(baseline_admin)
    tombstone.status = "deleted"
    tombstone.is_committed = 0
    new_order = OrderDTO(
        id=-1,
        admission_id=1,
        latin="Must rollback",
        type=OrderType.MEDICATION,
        status=OrderStatus.ACTIVE,
        dose_value=10,
        dose_unit="ml",
        is_committed=0,
        created_at=shift,
    )

    with pytest.raises(OrderConflictError):
        service.commit_local_draft(
            1,
            shift,
            orders=[baseline_order, new_order],
            admin_map={key: tombstone},
            dirty_admin_keys=[key],
            baseline_admin_map={key: baseline_admin},
            expected_revisions={baseline_order.id: baseline_order.revision},
        )

    assert db.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    latest = db.conn.execute(
        "SELECT * FROM administrations WHERE order_id = ? ORDER BY id DESC LIMIT 1",
        (baseline_order.id,),
    ).fetchone()
    assert latest["big_chain_id"] == "remote-chain"
    assert latest["cell_role"] == "body"


def test_concurrent_order_insert_rejects_stale_context_fingerprint(draft_context):
    db, service, shift, baseline_order, _baseline_admin = draft_context
    db.conn.execute(
        """
        INSERT INTO orders (
            admission_id, datetime, text, latin, type, status, dose_value, dose_unit,
            is_per_kg, frequency, specific_times, sort_order, is_committed, revision,
            created_at, comment, last_modified_by, updated_at
        ) VALUES (1, ?, 'Remote', 'Remote', 'medication', 'active', 1, 'ml',
                  0, 1, '[]', 1, 1, 0, ?, '', 'doctor', ?)
        """,
        (shift.isoformat(), shift.isoformat(), shift.isoformat()),
    )
    db.conn.commit()

    with pytest.raises(OrderConflictError):
        service.commit_local_draft(
            1,
            shift,
            orders=[baseline_order],
            admin_map={},
            dirty_admin_keys=[],
            baseline_admin_map={},
            expected_revisions={baseline_order.id: baseline_order.revision},
            expected_active_order_ids=[baseline_order.id],
        )

    assert db.rollback_count == 1
    assert db.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 2


def test_temporary_long_chain_is_validated_and_remapped_on_commit(draft_context):
    db, service, shift, baseline_order, _baseline_admin = draft_context
    new_order = OrderDTO(
        id=-1,
        admission_id=1,
        latin="Infusion",
        type=OrderType.INFUSION_CONTINUOUS,
        status=OrderStatus.ACTIVE,
        dose_value=1,
        dose_unit="ml",
        duration_min=180,
        created_at=shift,
    )
    admin_map = {}
    dirty_keys = []
    chain_id = "optimistic:-1:2026-07-13T10:00:00"
    for offset, role in enumerate(("start", "body", "end")):
        planned = shift + timedelta(hours=2 + offset)
        key = (-1, planned.isoformat())
        admin_map[key] = AdministrationDTO(
            id=-(offset + 1),
            order_id=-1,
            big_chain_id=chain_id,
            cell_role=role,
            planned_time=planned,
            status="planned",
        )
        dirty_keys.append(key)

    service.commit_local_draft(
        1,
        shift,
        orders=[baseline_order, new_order],
        admin_map=admin_map,
        dirty_admin_keys=dirty_keys,
        baseline_admin_map={},
        expected_revisions={baseline_order.id: baseline_order.revision},
        expected_active_order_ids=[baseline_order.id],
    )

    saved = db.conn.execute(
        "SELECT DISTINCT big_chain_id FROM administrations WHERE order_id != ?",
        (baseline_order.id,),
    ).fetchall()
    assert len(saved) == 1
    saved_chain_id = saved[0]["big_chain_id"]
    assert saved_chain_id != chain_id
    assert str(uuid.UUID(saved_chain_id)) == saved_chain_id


def test_adding_order_does_not_rewrite_unchanged_baseline_sort(draft_context):
    db, service, shift, baseline_order, _baseline_admin = draft_context
    db.conn.execute("UPDATE orders SET sort_order = 7 WHERE id = ?", (baseline_order.id,))
    db.conn.commit()
    baseline_order = OrdersDAO(db).get_orders(1, shift, only_committed=True)[0]
    new_order = OrderDTO(
        id=-1,
        admission_id=1,
        latin="Append only",
        type=OrderType.MEDICATION,
        status=OrderStatus.ACTIVE,
        dose_value=1,
        dose_unit="ml",
        sort_order=8,
        created_at=shift,
    )

    service.commit_local_draft(
        1,
        shift,
        orders=[baseline_order, new_order],
        admin_map={},
        dirty_admin_keys=[],
        baseline_admin_map={},
        expected_revisions={baseline_order.id: baseline_order.revision},
        expected_active_order_ids=[baseline_order.id],
    )

    saved_baseline = db.conn.execute(
        "SELECT sort_order, revision FROM orders WHERE id = ?",
        (baseline_order.id,),
    ).fetchone()
    assert int(saved_baseline["sort_order"]) == 7
    assert int(saved_baseline["revision"]) == baseline_order.revision


def test_delete_tombstone_is_applied_only_by_atomic_orders_save(draft_context):
    db, service, shift, baseline_order, _baseline_admin = draft_context
    tombstone = deepcopy(baseline_order)
    tombstone._pending_delete = True

    service.commit_local_draft(
        1,
        shift,
        orders=[tombstone],
        admin_map={},
        dirty_admin_keys=[],
        baseline_admin_map={},
        expected_revisions={baseline_order.id: baseline_order.revision},
        expected_active_order_ids=[baseline_order.id],
    )

    saved = db.conn.execute(
        "SELECT status, is_committed FROM orders WHERE id = ?",
        (baseline_order.id,),
    ).fetchone()
    assert saved["status"] == "deleted"
    assert int(saved["is_committed"]) == 1
    assert OrdersDAO(db).get_orders(1, shift, only_committed=True) == []
    assert db.transaction_count == 1

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

from rem_card.app.operblock_local_handoffs import (
    claim_remote_rao_handoff,
    list_remote_rao_handoffs,
    local_rao_claim_workstation_id,
    publish_imported_handoff,
    read_local_rao_claim_queue,
)
from rem_card.app.unified_db_schema import ensure_unified_schema
from rem_card.services.operblock_handoff_service import OperBlockLocalRaoHandoffService


class _Db:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_unified_schema(self.conn)

    def fetch_one_remcard(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def fetch_all_remcard(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def run_write_operation(self, operation, source="test"):
        cursor = self.conn.cursor()
        try:
            result = operation(cursor)
            self.conn.commit()
            return result
        except Exception:
            self.conn.rollback()
            raise


def _remote_case(db: _Db) -> tuple[int, int, int]:
    now = datetime.now().replace(microsecond=0).isoformat(timespec="seconds")
    cursor = db.conn.cursor()
    cursor.execute("INSERT INTO patients (full_name, birth_date) VALUES ('Иванов Иван', '1980-01-01')")
    patient_id = int(cursor.lastrowid)
    cursor.execute(
        """
        INSERT INTO admissions (
            patient_id, bed_number, history_number, admission_datetime,
            patient_gender, diagnosis_code, diagnosis_text, department_profile, is_active
        ) VALUES (?, 0, '42', ?, 'Мужской', 'K35', 'Аппендицит', 'Хирургия', 0)
        """,
        (patient_id, now),
    )
    admission_id = int(cursor.lastrowid)
    db.conn.commit()
    return 91, patient_id, admission_id


def test_fresh_local_transfer_is_one_durable_invitation_and_requires_explicit_bed():
    db = _Db()
    case_id, patient_id, admission_id = _remote_case(db)
    transfer = datetime.now().replace(microsecond=0).isoformat(timespec="seconds")

    def publish(cursor):
        publish_imported_handoff(
            cursor,
            local_case={"transfer_department": "РАО"},
            remote_case_id=case_id,
            remote_patient_id=patient_id,
            remote_admission_id=admission_id,
            source_payload={
                "case": {"transfer_department": "РАО"},
                "patient": {"full_name": "Иванов Иван"},
                "children": {"operblock_timeline_events": [{
                    "event_time": transfer,
                    "payload_json": '{"stage_kind":"anesthesia_end","transfer_department":"РАО"}',
                }]},
            },
        )
        publish_imported_handoff(
            cursor,
            local_case={"transfer_department": "РАО"},
            remote_case_id=case_id,
            remote_patient_id=patient_id,
            remote_admission_id=admission_id,
            source_payload={"transfer_department": "РАО", "transfer_datetime": transfer},
        )
    db.run_write_operation(publish)

    service = OperBlockLocalRaoHandoffService(db)
    pending = service.list_pending(include_snoozed=True)
    assert len(pending) == 1
    assert db.fetch_one_remcard("SELECT COUNT(*) AS n FROM admissions")["n"] == 1
    db.conn.execute("INSERT INTO beds (bed_number, status, current_admission_id) VALUES (7, 'FREE', NULL)")
    db.conn.commit()

    accepted = service.accept_to_bed(int(pending[0]["id"]), 7)
    assert accepted["bed_number"] == 7
    assert db.fetch_one_remcard("SELECT status FROM beds WHERE bed_number = 7")["status"] == "OCCUPIED"
    assert db.fetch_one_remcard("SELECT status FROM operblock_rao_handoff_invitations")["status"] == "accepted"


def test_stale_transfer_creates_no_invitation_and_acceptance_rechecks_window():
    db = _Db()
    case_id, patient_id, admission_id = _remote_case(db)
    stale = (datetime.now() - timedelta(minutes=31)).replace(microsecond=0).isoformat(timespec="seconds")
    db.run_write_operation(lambda cursor: publish_imported_handoff(
        cursor,
        local_case={"transfer_department": "РАО"},
        remote_case_id=case_id,
        remote_patient_id=patient_id,
        remote_admission_id=admission_id,
        source_payload={"transfer_department": "РАО", "transfer_datetime": stale},
    ))
    assert not OperBlockLocalRaoHandoffService(db).available()


def test_aware_fresh_transfer_is_localized_and_cancelled_timeline_end_is_ignored():
    db = _Db()
    case_id, patient_id, admission_id = _remote_case(db)
    fresh_utc = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    db.run_write_operation(lambda cursor: publish_imported_handoff(
        cursor,
        local_case={"transfer_department": "РАО"},
        remote_case_id=case_id,
        remote_patient_id=patient_id,
        remote_admission_id=admission_id,
        source_payload={"transfer_department": "РАО", "transfer_datetime": fresh_utc},
    ))
    assert OperBlockLocalRaoHandoffService(db).available()

    stale = (datetime.now() - timedelta(minutes=31)).replace(microsecond=0).isoformat(timespec="seconds")
    cancelled_fresh = datetime.now().replace(microsecond=0).isoformat(timespec="seconds")
    db.run_write_operation(lambda cursor: publish_imported_handoff(
        cursor,
        local_case={"transfer_department": "РАО"},
        remote_case_id=case_id + 1,
        remote_patient_id=patient_id,
        remote_admission_id=admission_id,
        source_payload={
            "case": {"transfer_department": "РАО"},
            "children": {"operblock_timeline_events": [
                {"event_time": stale, "payload_json": '{"stage_kind":"anesthesia_end"}'},
                {"event_time": cancelled_fresh, "status": "cancelled", "payload_json": '{"stage_kind":"anesthesia_end"}'},
            ]},
        },
    ))
    assert db.fetch_one_remcard("SELECT COUNT(*) AS n FROM operblock_rao_handoff_invitations")["n"] == 1


def test_acceptance_expires_an_invitation_that_became_stale():
    db = _Db()
    case_id, patient_id, admission_id = _remote_case(db)
    fresh = datetime.now().replace(microsecond=0).isoformat(timespec="seconds")
    db.run_write_operation(lambda cursor: publish_imported_handoff(
        cursor,
        local_case={"transfer_department": "РАО"},
        remote_case_id=case_id,
        remote_patient_id=patient_id,
        remote_admission_id=admission_id,
        source_payload={"transfer_department": "РАО", "transfer_datetime": fresh},
    ))
    db.conn.execute("UPDATE operblock_rao_handoff_invitations SET transfer_datetime = ?", (
        (datetime.now() - timedelta(minutes=31)).isoformat(timespec="seconds"),
    ))
    db.conn.execute("INSERT INTO beds (bed_number, status, current_admission_id) VALUES (7, 'FREE', NULL)")
    db.conn.commit()
    service = OperBlockLocalRaoHandoffService(db)
    try:
        service.accept_to_bed(1, 7)
        assert False, "stale handoff must be rejected"
    except RuntimeError as exc:
        assert "30 минут" in str(exc)
    assert db.fetch_one_remcard("SELECT status FROM operblock_rao_handoff_invitations")["status"] == "expired"


def test_claim_is_exactly_once_and_a_failed_bed_claim_keeps_invitation_pending():
    db = _Db()
    case_id, patient_id, admission_id = _remote_case(db)
    fresh = datetime.now().replace(microsecond=0).isoformat(timespec="seconds")
    db.run_write_operation(lambda cursor: publish_imported_handoff(
        cursor, local_case={"transfer_department": "РАО"}, remote_case_id=case_id,
        remote_patient_id=patient_id, remote_admission_id=admission_id,
        source_payload={"transfer_department": "РАО", "transfer_datetime": fresh},
    ))
    service = OperBlockLocalRaoHandoffService(db)
    try:
        service.accept_to_bed(1, 404)
        assert False, "nonexistent bed must fail"
    except RuntimeError:
        pass
    assert db.fetch_one_remcard("SELECT status FROM operblock_rao_handoff_invitations WHERE id = 1")["status"] == "pending"
    db.conn.execute("INSERT INTO beds (bed_number, status, current_admission_id) VALUES (8, 'FREE', NULL)")
    db.conn.commit()
    service.accept_to_bed(1, 8)
    try:
        service.accept_to_bed(1, 8)
        assert False, "accepted invitation must not be claimed twice"
    except RuntimeError as exc:
        assert "обработано" in str(exc)
    assert db.fetch_one_remcard("SELECT COUNT(*) AS n FROM admissions")["n"] == 2


def test_return_to_original_rao_admission_never_creates_a_duplicate_or_overwrites_bed():
    db = _Db()
    case_id, patient_id, remote_admission_id = _remote_case(db)
    now = datetime.now().replace(microsecond=0).isoformat(timespec="seconds")
    cursor = db.conn.cursor()
    cursor.execute("INSERT INTO admissions (patient_id, bed_number, history_number, admission_datetime, is_active) VALUES (?, 3, '42', ?, 1)", (patient_id, now))
    original_admission_id = int(cursor.lastrowid)
    cursor.execute("INSERT INTO beds (bed_number, status, current_admission_id) VALUES (3, 'OCCUPIED', ?)", (original_admission_id,))
    cursor.execute("INSERT INTO patient_status_events (admission_id, status, start_time) VALUES (?, 'OR', ?)", (original_admission_id, now))
    db.conn.commit()
    db.run_write_operation(lambda write_cursor: publish_imported_handoff(
        write_cursor, local_case={"transfer_department": "РАО", "source_rao_admission_id": original_admission_id},
        remote_case_id=case_id, remote_patient_id=patient_id, remote_admission_id=remote_admission_id,
        source_payload={"transfer_department": "РАО", "transfer_datetime": now, "source_rao_admission_id": original_admission_id},
    ))
    result = OperBlockLocalRaoHandoffService(db).accept_to_bed(1, 999)
    assert result == {"invitation_id": 1, "admission_id": original_admission_id, "bed_number": None}
    assert db.fetch_one_remcard("SELECT COUNT(*) AS n FROM admissions")["n"] == 2
    bed = db.fetch_one_remcard("SELECT status, current_admission_id FROM beds WHERE bed_number = 3")
    assert (bed["status"], bed["current_admission_id"]) == ("OCCUPIED", original_admission_id)


def test_remote_rao_claim_is_single_workstation_and_snapshot_is_durable(tmp_path):
    central = _Db()
    central.conn.execute(
        """
        CREATE TABLE operblock_handoffs (
            id INTEGER PRIMARY KEY, source_admission_id INTEGER NOT NULL,
            patient_snapshot_json TEXT, vitals_snapshot_json TEXT, status TEXT,
            dispatched_at TEXT, expected_arrival_at TEXT, last_modified_by TEXT, revision INTEGER
        )
        """
    )
    central.conn.execute("ALTER TABLE admissions ADD COLUMN merged_into_admission_id INTEGER")
    central.conn.execute(
        "INSERT INTO admissions (id, patient_id, bed_number, history_number, admission_datetime) "
        "VALUES (12, 1, 0, 'R', '2026-01-01T00:00:00')"
    )
    central.conn.execute("INSERT INTO operblock_handoffs VALUES (1, 12, '{}', '{}', 'waiting', '2026-01-01T00:00:00', '2026-01-01T00:05:00', NULL, 0)")
    central.conn.commit()
    local_path = tmp_path / "local.db"
    sqlite3.connect(local_path).close()
    claimed = claim_remote_rao_handoff(
        central, local_db_path=str(local_path), handoff_id=1, workstation_id="PC-A", claim_uuid="claim-a"
    )
    assert claimed["claim_uuid"] == "claim-a"
    assert sqlite3.connect(local_path).execute(
        "SELECT remote_handoff_id FROM operblock_local_rao_claim_snapshots"
    ).fetchone()[0] == 1
    retry = claim_remote_rao_handoff(
        central, local_db_path=str(local_path), handoff_id=1, workstation_id="PC-A", claim_uuid="claim-a"
    )
    assert retry["claim_uuid"] == "claim-a"
    try:
        claim_remote_rao_handoff(
            central, local_db_path=str(local_path), handoff_id=1, workstation_id="PC-B", claim_uuid="claim-b"
        )
        assert False, "other workstation must not receive the claimed handoff"
    except RuntimeError as exc:
        assert "другим рабочим местом" in str(exc)


def test_default_remote_claim_identity_is_durable_local_installation_uuid(tmp_path, monkeypatch):
    central = _Db()
    central.conn.execute(
        """
        CREATE TABLE operblock_handoffs (
            id INTEGER PRIMARY KEY, source_admission_id INTEGER NOT NULL,
            patient_snapshot_json TEXT, vitals_snapshot_json TEXT, status TEXT,
            dispatched_at TEXT, expected_arrival_at TEXT, last_modified_by TEXT, revision INTEGER
        )
        """
    )
    central.conn.execute("ALTER TABLE admissions ADD COLUMN merged_into_admission_id INTEGER")
    central.conn.execute(
        "INSERT INTO admissions (id, patient_id, bed_number, history_number, admission_datetime) "
        "VALUES (12, 1, 0, 'R', '2026-01-01T00:00:00')"
    )
    central.conn.execute("INSERT INTO operblock_handoffs VALUES (1, 12, '{}', '{}', 'waiting', '2026-01-01T00:00:00', '2026-01-01T00:05:00', NULL, 0)")
    central.conn.commit()
    local_path = tmp_path / "local.db"
    sqlite3.connect(local_path).close()
    monkeypatch.setenv("COMPUTERNAME", "cloned-hostname")

    origin = local_rao_claim_workstation_id(str(local_path))
    claimed = claim_remote_rao_handoff(
        central, local_db_path=str(local_path), handoff_id=1, claim_uuid="installation-claim"
    )

    assert claimed["claim_uuid"] == "installation-claim"
    remote = central.conn.execute(
        "SELECT local_claim_workstation FROM operblock_handoffs WHERE id = 1"
    ).fetchone()
    assert remote["local_claim_workstation"] == origin
    assert [row["id"] for row in list_remote_rao_handoffs(central, workstation_id=origin)] == [1]
    assert not list_remote_rao_handoffs(central, workstation_id="another-cloned-hostname")


def test_claim_commit_survives_local_snapshot_failure_and_restart_retries_same_uuid(tmp_path):
    central = _Db()
    central.conn.execute("CREATE TABLE operblock_handoffs (id INTEGER PRIMARY KEY, source_admission_id INTEGER NOT NULL, patient_snapshot_json TEXT, vitals_snapshot_json TEXT, status TEXT, dispatched_at TEXT, expected_arrival_at TEXT, last_modified_by TEXT, revision INTEGER)")
    central.conn.execute("ALTER TABLE admissions ADD COLUMN merged_into_admission_id INTEGER")
    central.conn.execute("INSERT INTO admissions (patient_id, bed_number, history_number, admission_datetime) VALUES (1, 0, 'R', '2026-01-01T00:00:00')")
    central.conn.execute("INSERT INTO operblock_handoffs VALUES (1, 1, '{}', '{}', 'waiting', '2026-01-01T00:00:00', '2026-01-01T00:05:00', NULL, 0)")
    central.conn.commit()
    broken_path = tmp_path / "not-a-db"
    broken_path.mkdir()
    try:
        claim_remote_rao_handoff(central, local_db_path=str(broken_path), handoff_id=1, workstation_id="PC-A", claim_uuid="crash-token")
        assert False, "local snapshot persistence must fail for a directory"
    except sqlite3.OperationalError:
        pass
    own = list_remote_rao_handoffs(central, workstation_id="PC-A")
    assert [row["local_claim_uuid"] for row in own] == ["crash-token"]
    try:
        claim_remote_rao_handoff(central, local_db_path=str(tmp_path / "other.db"), handoff_id=1, workstation_id="PC-B", claim_uuid="other")
        assert False, "second workstation must be rejected after central commit"
    except RuntimeError:
        pass
    local_path = tmp_path / "recovered.db"
    retry = claim_remote_rao_handoff(central, local_db_path=str(local_path), handoff_id=1, workstation_id="PC-A", claim_uuid="crash-token")
    assert retry["claim_uuid"] == "crash-token"
    assert sqlite3.connect(local_path).execute("SELECT claim_uuid FROM operblock_local_rao_claim_snapshots").fetchone()[0] == "crash-token"


def test_local_rao_claim_queue_hides_consumed_and_recovers_pending_snapshot(tmp_path):
    local_path = tmp_path / "local.db"
    with sqlite3.connect(local_path) as conn:
        conn.execute(
            """
            CREATE TABLE operblock_local_rao_claim_snapshots (
                claim_uuid TEXT PRIMARY KEY,
                remote_handoff_id INTEGER NOT NULL UNIQUE,
                remote_source_admission_id INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                claimed_at TEXT NOT NULL,
                local_operation_case_id INTEGER,
                consumed_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO operblock_local_rao_claim_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("used", 1, 10, '{"id": 1}', "2026-01-01T00:00:00", 77, "2026-01-01T00:01:00"),
        )
        conn.execute(
            "INSERT INTO operblock_local_rao_claim_snapshots VALUES (?, ?, ?, ?, ?, NULL, NULL)",
            ("retry", 2, 20, '{"id": 2, "patient_snapshot": {"full_name": "Иванов"}}', "2026-01-01T00:00:00"),
        )

    consumed, pending = read_local_rao_claim_queue(str(local_path))

    assert consumed == {"used"}
    assert pending == [{
        "id": 2,
        "patient_snapshot": {"full_name": "Иванов"},
        "local_claim_uuid": "retry",
        "_local_only_claim": True,
    }]


def test_local_rao_claim_queue_does_not_create_missing_database(tmp_path):
    missing = tmp_path / "missing.db"

    assert read_local_rao_claim_queue(str(missing)) == (set(), [])
    assert not missing.exists()

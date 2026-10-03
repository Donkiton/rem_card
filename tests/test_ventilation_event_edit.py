from datetime import datetime, timedelta
import sqlite3

import pytest

from rem_card.app.unified_db_schema import ensure_unified_schema
from rem_card.data.dao.ventilation_dao import VentilationDAO
from rem_card.services.concurrency import DataConflictError
from rem_card.services.ventilation_service import VentilationService


START = datetime(2026, 10, 3, 8)


class MemoryDb:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_unified_schema(self.conn)
        self.conn.execute("INSERT INTO patients(id,full_name) VALUES (1,'Тест')")
        self.conn.execute("INSERT INTO admissions(id,patient_id,bed_number,history_number,admission_datetime) VALUES (1,1,1,'test',?)", (START.isoformat(),))
        self.conn.commit()

    def fetch_one_remcard(self, query, params=()):
        return self.conn.execute(query, params).fetchone()

    def fetch_all_remcard(self, query, params=()):
        return self.conn.execute(query, params).fetchall()

    def run_write_operation(self, operation, **_kwargs):
        with self.conn:
            return operation(self.conn.cursor())


@pytest.fixture
def service():
    db = MemoryDb()
    result = VentilationService(VentilationDAO(db))
    yield result
    db.conn.close()


def open_case(service, **kwargs):
    return service.create_case(1, start_time=START + timedelta(minutes=10), initial_mode="CPAP", **kwargs)


def add_mode(service, case, minute=20):
    return service.add_event(case.id, event_time=START + timedelta(minutes=minute), event_type="MODE_CHANGE", mode="CPAP", parameters={"PEEP": 5})


def edit(service, event, **kwargs):
    payload = dict(admission_id=1, event_time=event.timestamp, event_type=event.event_type,
                   mode=event.mode, parameters=event.parameters, extubation_reason=event.extubation_reason,
                   o2_flow=event.o2_flow, expected_event_revision=event.revision)
    payload.update(kwargs)
    return service.edit_event(event.id, **payload)


def rows(service, table):
    return [dict(row) for row in service.dao.db.conn.execute(f"SELECT * FROM {table} ORDER BY id")]


def test_start_empty_parameters_edit_keeps_event_and_support_ids(service):
    case = open_case(service)
    event = service.dao.get_case_events(case.id)[0]
    before = rows(service, "clinical_events")[0]
    support_id = rows(service, "respiratory_support")[0]["id"]
    result = edit(service, event, parameters={"PEEP": 8, "FiO2": 40}, extubation_reason="Уточнено")
    after = rows(service, "clinical_events")[0]
    assert result.id == event.id and result.revision == event.revision + 1
    assert after["created_at"] == before["created_at"] and after["author"] == before["author"]
    support = rows(service, "respiratory_support")[0]
    assert (support["id"], support["peep"], support["fio2"]) == (support_id, 8, 40)
    assert service.dao.get_case_by_id(case.id).revision > case.revision


def test_old_mode_in_closed_case_updates_only_selected_event(service):
    case = open_case(service)
    event = add_mode(service, case)
    later = add_mode(service, case, 30)
    service.close_case(case.id, end_time=START + timedelta(minutes=40))
    result = edit(service, event, event_time=START + timedelta(minutes=25), mode="PSV", parameters={"PS": 12})
    assert result.id == event.id and result.mode.value == "PSV"
    assert service.dao.get_last_event(case.id).event_type.value == "EXTUBATION"
    assert service.dao.get_case_events(case.id)[2] == later
    assert len(rows(service, "clinical_events")) == 4
    assert next(row for row in rows(service, "respiratory_support") if row["event_id"] == event.id)["datetime"] == result.timestamp.isoformat()


def test_start_time_moves_case_and_first_tube_preserving_ids(service):
    case = open_case(service)
    event = service.dao.get_case_events(case.id)[0]
    tube_id = rows(service, "devices")[0]["id"]
    new_time = START + timedelta(minutes=5)
    edit(service, event, event_time=new_time)
    assert service.dao.get_case_by_id(case.id).start_time == new_time
    assert (rows(service, "devices")[0]["id"], rows(service, "devices")[0]["insertion_date"]) == (tube_id, new_time.isoformat())


def test_extubation_moves_case_and_tube_end_and_recalculates_duration(service):
    case = open_case(service)
    event = service.close_case(case.id, end_time=START + timedelta(minutes=40))
    new_time = START + timedelta(minutes=50)
    edit(service, event, event_time=new_time, extubation_reason="Дыхание восстановлено", o2_flow=3)
    assert service.dao.get_case_by_id(case.id).end_time == new_time
    assert rows(service, "devices")[0]["removal_date"] == new_time.isoformat()
    assert service.get_active_case_summary(1)["total_duration_seconds"] == 40 * 60


@pytest.mark.parametrize("kind", ["TRACHEOSTOMY", "TUBE_REPLACEMENT"])
def test_tube_event_time_moves_adjacent_intervals(service, kind):
    case = open_case(service)
    event = service.add_event(case.id, event_time=START + timedelta(minutes=20), event_type=kind)
    service.replace_tube(case.id, replacement_time=START + timedelta(minutes=30))
    before_ids = [row["id"] for row in rows(service, "devices")]
    moved = START + timedelta(minutes=25)
    edit(service, event, event_time=moved)
    devices = rows(service, "devices")
    assert [row["id"] for row in devices] == before_ids
    assert devices[0]["removal_date"] == devices[1]["insertion_date"] == moved.isoformat()
    assert devices[1]["removal_date"] == devices[2]["insertion_date"]


def test_mode_to_tracheostomy_and_back_rebuilds_tubes_and_support(service):
    case = open_case(service)
    event = add_mode(service, case)
    service.replace_tube(case.id, replacement_time=START + timedelta(minutes=30))
    original_tubes = [row["id"] for row in rows(service, "devices")]
    trach = edit(service, event, event_type="TRACHEOSTOMY", mode=None, parameters={})
    devices = rows(service, "devices")
    assert len(devices) == 3
    assert devices[-1]["device_type"] == "TRACHEOSTOMY_TUBE"
    assert not any(row["event_id"] == event.id for row in rows(service, "respiratory_support"))
    edit(service, trach, event_type="MODE_CHANGE", mode="CPAP", parameters={"PEEP": 7})
    assert [row["id"] for row in rows(service, "devices")] == original_tubes
    assert rows(service, "devices")[0]["removal_date"] == (START + timedelta(minutes=30)).isoformat()
    assert any(row["event_id"] == event.id and row["peep"] == 7 for row in rows(service, "respiratory_support"))


@pytest.mark.parametrize("kind", ["START_VENT", "EXTUBATION", "TUBE_REPLACEMENT"])
def test_fixed_event_type_cannot_change(service, kind):
    case = open_case(service)
    if kind == "START_VENT":
        event = service.dao.get_case_events(case.id)[0]
    else:
        event = service.add_event(case.id, event_time=START + timedelta(minutes=20), event_type=kind)
    before = rows(service, "clinical_events")
    with pytest.raises(ValueError, match="Можно менять тип"):
        edit(service, event, event_type="MODE_CHANGE", mode="CPAP")
    assert rows(service, "clinical_events") == before


@pytest.mark.parametrize("minute", [5, 35])
def test_edit_cannot_cross_neighbor_events(service, minute):
    case = open_case(service)
    event = add_mode(service, case)
    add_mode(service, case, 30)
    before = rows(service, "clinical_events")
    with pytest.raises(ValueError, match="события ИВЛ"):
        edit(service, event, event_time=START + timedelta(minutes=minute))
    assert rows(service, "clinical_events") == before


def test_revision_conflict_and_wrong_patient_do_not_overwrite(service):
    case = open_case(service)
    event = add_mode(service, case)
    edit(service, event, parameters={"PEEP": 8})
    before = rows(service, "clinical_events")
    with pytest.raises(DataConflictError):
        edit(service, event, parameters={"PEEP": 12})
    with pytest.raises(ValueError, match="другому пациенту"):
        edit(service, event, admission_id=2)
    assert rows(service, "clinical_events") == before


def test_case_revision_conflict_blocks_structural_edit(service):
    case = open_case(service)
    event = add_mode(service, case)
    with pytest.raises(DataConflictError):
        edit(service, event, expected_case_revision=case.revision)


def test_inconsistent_tube_history_rolls_back_event_support_and_case(service):
    case = open_case(service)
    event = add_mode(service, case)
    service.dao.db.conn.execute("DELETE FROM devices")
    service.dao.db.conn.commit()
    before = {table: rows(service, table) for table in ("clinical_events", "respiratory_support", "ivl_episodes")}
    with pytest.raises(ValueError, match="История трубок"):
        edit(service, event, event_type="TRACHEOSTOMY", mode=None, parameters={})
    assert {table: rows(service, table) for table in before} == before


def test_admission_start_and_automatic_outcome_time_are_fixed(service):
    case = open_case(service, start_type="ADMISSION")
    event = service.dao.get_case_events(case.id)[0]
    with pytest.raises(ValueError, match="С поступления"):
        edit(service, event, event_time=START + timedelta(minutes=1))
    closed = service.close_case(case.id, end_time=START + timedelta(minutes=40), author=service.OUTCOME_SYNC_AUTHOR)
    with pytest.raises(ValueError, match="исходом пациента"):
        edit(service, closed, event_time=START + timedelta(minutes=50))


def test_closed_case_end_cannot_overlap_next_case(service):
    case = open_case(service)
    closed = service.close_case(case.id, end_time=START + timedelta(minutes=40))
    next_case = service.create_case(1, start_time=START + timedelta(minutes=50), initial_mode="CPAP")
    with pytest.raises(ValueError, match="до следующего случая"):
        edit(service, closed, event_time=next_case.start_time)


def test_extubation_does_not_move_coincident_previous_tube_closure(service):
    case = open_case(service)
    when = START + timedelta(minutes=40)
    service.replace_tube(case.id, replacement_time=when)
    closed = service.close_case(case.id, end_time=when)
    edit(service, closed, event_time=START + timedelta(minutes=50))
    devices = rows(service, "devices")
    assert devices[0]["removal_date"] == devices[1]["insertion_date"] == when.isoformat()
    assert devices[1]["removal_date"] == (START + timedelta(minutes=50)).isoformat()

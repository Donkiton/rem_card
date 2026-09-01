import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest

from rem_card.app.unified_db_schema import ensure_unified_schema, is_unified_schema_ready
from rem_card.data.dao.exceptions import OptimisticLockError
from rem_card.data.dao.diet_dao import DietPlanDAO, DietPlanVersionDAO, OralIntakeDAO
from rem_card.data.dto.remcard_dto import DietTemplateDTO
from rem_card.services.diet_service import DietPlanService, OralIntakeService, diet_details
from rem_card.services.remcard_facade import RemCardService


class SQLiteRemcardDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_unified_schema(self.conn)

    def fetch_all_remcard(self, query, params=()):
        return self.conn.execute(query, params).fetchall()

    def fetch_one_remcard(self, query, params=()):
        return self.conn.execute(query, params).fetchone()

    def execute_remcard(self, query, params=()):
        cursor = self.conn.execute(query, params)
        self.conn.commit()
        return cursor

    @contextmanager
    def remcard_transaction(self, source="test"):
        cursor = self.conn.cursor()
        try:
            yield cursor
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise


class Templates:
    def __init__(self, templates):
        self.templates = {int(item.id): item for item in templates}

    def get_template(self, template_id):
        return self.templates[int(template_id)]


class Vitals:
    @staticmethod
    def get_effective_bounds(_admission_id, shift_date):
        start = shift_date.replace(hour=8, minute=0, second=0, microsecond=0)
        if shift_date.hour < 8:
            start -= timedelta(days=1)
        return start, start + timedelta(days=1)

    @staticmethod
    def validate_timestamp(_admission_id, _event_dt, _shift_start):
        return True, "OK"


def _insert_admission(db, admission_id=1):
    db.conn.execute("INSERT INTO patients(id, full_name) VALUES (?, ?)", (admission_id, "Тестовый пациент"))
    db.conn.execute(
        "INSERT INTO admissions(id, patient_id, bed_number, history_number, admission_datetime) VALUES (?, ?, ?, ?, ?)",
        (admission_id, admission_id, "T1", "H1", "2026-08-29 07:30"),
    )
    db.conn.commit()


def _services():
    db = SQLiteRemcardDB()
    _insert_admission(db)
    table9 = DietTemplateDTO(
        id=9,
        name="Стол № 9",
        diet_text="Диабетический стол",
        schedule_json=(
            '[{"key":"breakfast","meal":"Завтрак","time":"09:00","amount":250,"note":""},'
            '{"key":"lunch","meal":"Обед","time":"12:00","amount":350,"note":""},'
            '{"key":"dinner","meal":"Ужин","time":"17:00","amount":350,"note":""}]'
        ),
        details_json='{"consistency":"Протёртая","no_food":false,"no_fluids":false}',
        version=1,
    )
    hunger = DietTemplateDTO(
        id=10,
        name="Голод",
        diet_text="Питание отсутствует",
        schedule_json="[]",
        details_json='{"no_food":true,"no_fluids":true}',
        version=1,
    )
    plans = DietPlanService(DietPlanDAO(db), Templates([table9, hunger]), DietPlanVersionDAO(db))
    oral = OralIntakeService(OralIntakeDAO(db), Vitals(), plans)
    return db, plans, oral


def test_oral_nutrition_snapshot_uses_shared_read_scope():
    assert hasattr(RemCardService.build_oral_nutrition_snapshot, "__wrapped__")


def test_diet_change_applies_from_effective_time_and_can_be_backdated():
    _db, plans, _oral = _services()
    day = datetime(2026, 8, 29, 10, 0)
    plans.assign_version(1, datetime(2026, 8, 29, 8, 0), template_id=9)
    hunger = plans.assign_version(1, datetime(2026, 8, 29, 13, 0), template_id=10)

    assert [item["key"] for item in plans.planned_items_for_day(1, day)] == ["breakfast", "lunch"]

    plans.assign_version(
        1,
        datetime(2026, 8, 29, 11, 0),
        template_id=10,
        version_id=hunger.id,
        expected_version=hunger.version,
    )
    assert [item["key"] for item in plans.planned_items_for_day(1, day)] == ["breakfast"]


def test_template_assignment_preserves_doctor_schedule_edits():
    _db, plans, _oral = _services()
    day = datetime(2026, 8, 29, 8, 0)

    assigned = plans.assign_version(
        1,
        day,
        template_id=9,
        diet_name="Стол № 9",
        diet_text="Диабетический стол без обеда",
        schedule_json=[
            {"key": "breakfast", "meal": "Завтрак", "time": "09:00", "amount": 250, "note": ""},
            {"key": "dinner", "meal": "Ужин", "time": "17:00", "amount": 350, "note": ""},
        ],
        details_json={"consistency": "Мягкая", "daily_fluid_ml": 900},
    )

    assert assigned.template_id == 9
    assert assigned.diet_text == "Диабетический стол без обеда"
    assert [item["key"] for item in plans.planned_items_for_day(1, day)] == ["breakfast", "dinner"]
    assigned_details = diet_details(assigned.details_json)
    assert assigned_details["consistency"] == "Мягкая"
    assert assigned_details["daily_fluid_ml"] == 900


def test_template_assignment_allows_doctor_to_remove_every_meal():
    _db, plans, _oral = _services()
    day = datetime(2026, 8, 29, 8, 0)

    assigned = plans.assign_version(
        1,
        day,
        template_id=9,
        diet_name="Стол № 9",
        diet_text="Временно без плановых приёмов",
        schedule_json=[],
        details_json={},
    )

    assert assigned.template_id == 9
    assert assigned.schedule_json == "[]"
    assert plans.planned_items_for_day(1, day) == []


def test_diet_assignment_and_later_changes_continue_across_future_medical_days():
    _db, plans, _oral = _services()
    first_day = datetime(2026, 8, 29, 8, 0)
    initial = plans.assign_version(1, first_day, template_id=9)

    for days_ahead in (0, 1, 7, 30):
        check_day = first_day + timedelta(days=days_ahead)
        assert [item["key"] for item in plans.planned_items_for_day(1, check_day)] == [
            "breakfast",
            "lunch",
            "dinner",
        ]
        assert plans.active_at(1, check_day.replace(hour=12)).id == initial.id

    change_time = first_day + timedelta(days=2, hours=5)
    changed = plans.assign_version(
        1,
        change_time,
        diet_name="Вечерняя диета",
        diet_text="Питание вечером",
        schedule_json=[
            {"key": "evening", "meal": "Вечерний приём", "time": "18:00", "amount": 180, "note": ""}
        ],
        details_json={"consistency": "Мягкая", "on_demand": True, "daily_fluid_ml": 1200},
        change_note="Смена диеты",
    )

    change_day_items = plans.planned_items_for_day(1, change_time)
    assert [item["key"] for item in change_day_items] == ["breakfast", "lunch", "evening"]
    assert plans.active_at(1, change_time - timedelta(minutes=1)).id == initial.id
    assert plans.active_at(1, change_time).id == changed.id

    future_day = first_day + timedelta(days=12)
    assert [item["key"] for item in plans.planned_items_for_day(1, future_day)] == ["evening"]
    future_active = plans.active_at(1, future_day.replace(hour=20))
    assert future_active.id == changed.id
    assert future_active.diet_name == "Вечерняя диета"
    assert future_active.change_note == "Смена диеты"
    future_details = diet_details(future_active.details_json)
    assert future_details["consistency"] == "Мягкая"
    assert future_details["on_demand"] is True
    assert future_details["daily_fluid_ml"] == 1200

    edited = plans.assign_version(
        1,
        change_time,
        version_id=changed.id,
        expected_version=changed.version,
        diet_name="Исправленная диета",
        diet_text="Исправленные параметры",
        schedule_json=[
            {"key": "late", "meal": "Поздний приём", "time": "19:00", "amount": 200, "note": ""}
        ],
        details_json={"consistency": "Протёртая", "no_fluids": True},
        change_note="Исправление назначения",
    )

    assert edited.version == changed.version + 1
    assert [item["key"] for item in plans.planned_items_for_day(1, future_day)] == ["late"]
    edited_future = plans.active_at(1, future_day.replace(hour=20))
    assert edited_future.id == changed.id
    assert edited_future.diet_name == "Исправленная диета"
    edited_details = diet_details(edited_future.details_json)
    assert edited_details["consistency"] == "Протёртая"
    assert edited_details["no_fluids"] is True


def test_deleting_diet_change_restores_previous_assignment_and_preserves_intake_fact():
    _db, plans, oral = _services()
    shift_start = datetime(2026, 8, 29, 8, 0)
    initial = plans.assign_version(1, shift_start, template_id=9)
    changed = plans.assign_version(1, datetime(2026, 8, 29, 13, 0), template_id=10)
    fact = oral.create_fact(
        1,
        datetime(2026, 8, 29, 14, 0),
        50,
        plan_version_id=changed.id,
        planned_item_key="water",
        meal_name="Вода",
        entry_kind="planned",
    )

    plans.delete_version(1, changed.id, expected_version=changed.version)

    assert [version.id for version in plans.list_all_versions(1)] == [initial.id]
    assert plans.active_at(1, datetime(2026, 8, 29, 14, 0)).id == initial.id
    assert [item["key"] for item in plans.planned_items_for_day(1, shift_start)] == [
        "breakfast",
        "lunch",
        "dinner",
    ]
    preserved = oral.get_events(1, shift_start)
    assert len(preserved) == 1
    assert preserved[0].id == fact.id
    assert preserved[0].plan_version_id is None


def test_multiple_facts_in_one_minute_are_separate_and_aggregate_in_balance():
    _db, plans, oral = _services()
    plans.assign_version(1, datetime(2026, 8, 29, 8, 0), template_id=9)
    moment = datetime(2026, 8, 29, 12, 20)
    oral.create_fact(1, moment, 100, planned_item_key="lunch", plan_version_id=1, entry_kind="planned")
    oral.create_fact(1, moment, 50, meal_name="Вода", entry_kind="unplanned")

    events = oral.get_events(1, datetime(2026, 8, 29, 12, 0))
    assert len(events) == 2
    assert sum(event.amount_ml for event in events) == 150
    assert oral.get_totals(1, datetime(2026, 8, 29, 12, 0), current_time=moment)["current"] == 150


def test_explicit_zero_intake_is_stored_for_refusal_and_can_replace_a_value():
    _db, _plans, oral = _services()
    first_time = datetime(2026, 8, 29, 10, 0)
    second_time = datetime(2026, 8, 29, 11, 0)

    refused = oral.create_fact(1, first_time, 0, meal_name="Завтрак", note="Отказался")
    consumed = oral.create_fact(1, second_time, 100, meal_name="Второй завтрак")
    corrected = oral.update_fact(
        consumed.id,
        second_time,
        0,
        meal_name=consumed.meal_name,
        note="Отказался",
        expected_version=consumed.version,
    )

    events = oral.get_events(1, first_time)
    assert [(event.event_time, event.amount_ml, event.note) for event in events] == [
        (first_time, 0.0, "Отказался"),
        (second_time, 0.0, "Отказался"),
    ]
    assert refused.id is not None
    assert corrected.id == consumed.id
    assert corrected.version == consumed.version + 1
    assert oral.get_totals(1, first_time, current_time=second_time)["current"] == 0


def test_negative_intake_remains_invalid():
    _db, _plans, oral = _services()

    with pytest.raises(ValueError, match="не может быть меньше 0"):
        oral.create_fact(1, datetime(2026, 8, 29, 10, 0), -1)


def test_later_unplanned_fact_at_same_time_replaces_previous_value():
    _db, plans, oral = _services()
    plans.assign_version(1, datetime(2026, 8, 29, 8, 0), template_id=9)
    moment = datetime(2026, 8, 29, 12, 20)
    planned = oral.create_fact(
        1,
        moment,
        100,
        planned_item_key="lunch",
        plan_version_id=1,
        entry_kind="planned",
    )
    original = oral.create_fact(
        1,
        moment,
        50,
        meal_name="Вода",
        note="первое значение",
        entry_kind="unplanned",
    )

    replacement = oral.create_fact(
        1,
        moment,
        75,
        meal_name="Чай",
        note="исправленное значение",
        entry_kind="unplanned",
    )

    events = oral.get_events(1, datetime(2026, 8, 29, 12, 0))
    unplanned = [event for event in events if event.entry_kind == "unplanned"]
    assert len(events) == 2
    assert len(unplanned) == 1
    assert replacement.id == original.id
    assert replacement.version == original.version + 1
    assert replacement.amount_ml == 75
    assert replacement.meal_name == "Чай"
    assert replacement.note == "исправленное значение"
    assert planned.id != replacement.id
    assert sum(event.amount_ml for event in events) == 175


def test_unplanned_fact_does_not_overwrite_a_concurrent_same_minute_value():
    _db, _plans, oral = _services()
    moment = datetime(2026, 8, 29, 12, 20)
    existing = oral.create_fact(1, moment, 50, meal_name="Вода", entry_kind="unplanned")

    with pytest.raises(OptimisticLockError, match="изменен другим пользователем"):
        oral.create_fact(
            1,
            moment,
            75,
            meal_name="Чай",
            entry_kind="unplanned",
            expected_version=0,
        )

    current = oral.get_events(1, moment)[0]
    assert current.id == existing.id
    assert current.amount_ml == 50


def test_diet_change_fact_retention_modes_preserve_before_and_all():
    _db, plans, oral = _services()
    shift_start = datetime(2026, 8, 29, 8, 0)
    change_time = datetime(2026, 8, 29, 12, 0)
    plans.assign_version(1, shift_start, template_id=9)
    oral.create_fact(1, datetime(2026, 8, 29, 10, 0), 50, meal_name="До изменения")
    oral.create_fact(1, change_time, 75, meal_name="На границе изменения")
    oral.create_fact(1, datetime(2026, 8, 29, 14, 0), 100, meal_name="После изменения")

    plans.assign_version(
        1,
        change_time,
        diet_name="Новая диета",
        diet_text="Изменённое назначение",
        schedule_json=[],
        details_json={"consistency": "Жидкая"},
    )

    preserved = oral.get_events(1, shift_start)
    assert len(preserved) == 3

    assert oral.clear_facts(1, before=change_time) == 1
    after_before_mode = oral.get_events(1, shift_start)
    assert [event.event_time for event in after_before_mode] == [
        change_time,
        datetime(2026, 8, 29, 14, 0),
    ]

    assert oral.clear_facts(1) == 2
    assert oral.get_events(1, shift_start) == []


def test_hunger_is_warning_state_not_database_block():
    _db, plans, oral = _services()
    plans.assign_version(1, datetime(2026, 8, 29, 8, 0), template_id=10)
    restrictions = oral.active_restrictions(1, datetime(2026, 8, 29, 10, 0))
    assert restrictions["no_food"] is True
    assert restrictions["no_fluids"] is True

    event = oral.create_fact(1, datetime(2026, 8, 29, 10, 0), 50, meal_name="Вода")
    assert event.amount_ml == 50


def test_planned_fact_can_be_entered_from_three_hours_before_until_shift_end():
    _db, plans, oral = _services()
    shift_start = datetime(2026, 8, 29, 8, 0)
    planned_time = datetime(2026, 8, 29, 12, 0)
    plans.assign_version(1, shift_start, template_id=9)

    earliest = oral.create_fact(
        1,
        datetime(2026, 8, 29, 9, 0),
        100,
        planned_item_key="lunch",
        planned_time=planned_time,
        entry_kind="planned",
    )
    latest = oral.create_fact(
        1,
        datetime(2026, 8, 30, 7, 59),
        50,
        planned_item_key="lunch",
        planned_time=planned_time,
        entry_kind="planned",
    )

    assert earliest.event_time == datetime(2026, 8, 29, 9, 0)
    assert latest.event_time == datetime(2026, 8, 30, 7, 59)


def test_planned_fact_may_use_a_future_scheduled_time():
    _db, _plans, oral = _services()
    planned_time = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=30)

    event = oral.create_fact(
        1,
        planned_time,
        100,
        planned_item_key="future-meal",
        planned_time=planned_time,
        entry_kind="planned",
    )

    assert event.event_time == planned_time


def test_planned_fact_rejects_too_early_time_with_feeding_guidance():
    _db, plans, oral = _services()
    plans.assign_version(1, datetime(2026, 8, 29, 8, 0), template_id=9)

    try:
        oral.create_fact(
            1,
            datetime(2026, 8, 29, 8, 59),
            100,
            planned_item_key="lunch",
            planned_time=datetime(2026, 8, 29, 12, 0),
            entry_kind="planned",
        )
    except ValueError as exc:
        assert "Не следует кормить пациента раньше назначенного времени" in str(exc)
    else:
        raise AssertionError("Слишком раннее плановое кормление должно быть отклонено")


def test_planned_fact_rejects_time_outside_its_medical_day():
    _db, plans, oral = _services()
    plans.assign_version(1, datetime(2026, 8, 29, 8, 0), template_id=9)

    try:
        oral.create_fact(
            1,
            datetime(2026, 8, 30, 8, 0),
            100,
            planned_item_key="lunch",
            planned_time=datetime(2026, 8, 29, 12, 0),
            entry_kind="planned",
        )
    except ValueError as exc:
        assert "в пределах текущих медицинских суток" in str(exc)
    else:
        raise AssertionError("Время следующей смены должно быть отклонено")


def test_schema_contract_contains_versioned_nutrition_objects():
    db = SQLiteRemcardDB()
    assert is_unified_schema_ready(db.conn)
    columns = {row[1] for row in db.conn.execute("PRAGMA table_info(oral_intake_events)")}
    assert {"plan_version_id", "planned_item_key", "entry_kind", "note", "action_id"} <= columns


def test_legacy_oral_rows_migrate_without_loss_and_same_minute_becomes_available():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_unified_schema(conn)
    for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'oral_intake_events'"
    ).fetchall():
        conn.execute(f"DROP TRIGGER {row['name']}")
    conn.execute("DROP TABLE oral_intake_events")
    conn.executescript(
        """
        CREATE TABLE oral_intake_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admission_id INTEGER NOT NULL,
            shift_start TEXT NOT NULL,
            event_time TEXT NOT NULL,
            amount_ml REAL NOT NULL CHECK(amount_ml > 0),
            created_at TEXT,
            updated_at TEXT,
            version INTEGER DEFAULT 1,
            last_modified_by TEXT,
            UNIQUE(admission_id, event_time)
        );
        INSERT INTO patients(id, full_name, admission_uid)
        VALUES(999, 'Тест миграции', 'nutrition-migration');
        INSERT INTO admissions(id, patient_id, bed_number, history_number, admission_datetime)
        VALUES(999, 999, 'T1', 'H1', '2026-08-29 08:00');
        INSERT INTO oral_intake_events(
            admission_id, shift_start, event_time, amount_ml, created_at, version, last_modified_by
        ) VALUES(999, '2026-08-29 08:00', '2026-08-29 09:00', 100, '2026-08-29 09:01', 1, 'nurse');
        DELETE FROM schema_migrations WHERE version >= 25;
        UPDATE meta SET value = '24' WHERE key = 'unified_schema_fastpath_rev';
        """
    )

    ensure_unified_schema(conn)

    legacy = conn.execute(
        "SELECT amount_ml, entry_kind FROM oral_intake_events WHERE admission_id = 999"
    ).fetchone()
    assert dict(legacy) == {"amount_ml": 100.0, "entry_kind": "legacy"}
    conn.execute(
        """
        INSERT INTO oral_intake_events(admission_id, shift_start, event_time, amount_ml)
        VALUES(999, '2026-08-29 08:00', '2026-08-29 09:00', 50)
        """
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM oral_intake_events WHERE admission_id = 999"
    ).fetchone()[0] == 2
    conn.execute(
        """
        INSERT INTO oral_intake_events(admission_id, shift_start, event_time, amount_ml)
        VALUES(999, '2026-08-29 08:00', '2026-08-29 10:00', 0)
        """
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO oral_intake_events(admission_id, shift_start, event_time, amount_ml)
            VALUES(999, '2026-08-29 08:00', '2026-08-29 11:00', -1)
            """
        )


def test_current_v25_oral_table_is_rebuilt_to_allow_zero_without_data_loss():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_unified_schema(conn)
    for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'oral_intake_events'"
    ).fetchall():
        conn.execute(f"DROP TRIGGER {row['name']}")
    conn.execute("DROP TABLE oral_intake_events")
    conn.executescript(
        """
        CREATE TABLE oral_intake_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admission_id INTEGER NOT NULL,
            shift_start TEXT NOT NULL,
            event_time TEXT NOT NULL,
            amount_ml REAL NOT NULL CHECK(amount_ml > 0),
            plan_version_id INTEGER,
            planned_item_key TEXT,
            entry_kind TEXT NOT NULL DEFAULT 'unplanned',
            meal_name TEXT,
            note TEXT,
            action_id TEXT,
            created_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f', 'now')),
            updated_at TEXT,
            version INTEGER DEFAULT 1,
            last_modified_by TEXT
        );
        INSERT INTO patients(id, full_name, admission_uid)
        VALUES(998, 'Тест v25', 'nutrition-v25');
        INSERT INTO admissions(id, patient_id, bed_number, history_number, admission_datetime)
        VALUES(998, 998, 'T2', 'H2', '2026-08-29 08:00');
        INSERT INTO oral_intake_events(
            admission_id, shift_start, event_time, amount_ml, entry_kind, meal_name, version
        ) VALUES(998, '2026-08-29 08:00', '2026-08-29 09:00', 100, 'planned', 'Завтрак', 3);
        DELETE FROM schema_migrations WHERE version >= 26;
        UPDATE meta SET value = '25' WHERE key = 'unified_schema_fastpath_rev';
        """
    )

    ensure_unified_schema(conn)

    preserved = conn.execute(
        "SELECT amount_ml, meal_name, version FROM oral_intake_events WHERE admission_id = 998"
    ).fetchone()
    assert dict(preserved) == {"amount_ml": 100.0, "meal_name": "Завтрак", "version": 3}
    conn.execute(
        """
        INSERT INTO oral_intake_events(admission_id, shift_start, event_time, amount_ml)
        VALUES(998, '2026-08-29 08:00', '2026-08-29 10:00', 0)
        """
    )

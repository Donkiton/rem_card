import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

from rem_card.app.unified_db_schema import ensure_unified_schema, is_unified_schema_ready
from rem_card.data.dao.diet_dao import DietPlanDAO, DietPlanVersionDAO, OralIntakeDAO
from rem_card.data.dto.remcard_dto import DietTemplateDTO
from rem_card.services.diet_service import DietPlanService, OralIntakeService


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


def test_hunger_is_warning_state_not_database_block():
    _db, plans, oral = _services()
    plans.assign_version(1, datetime(2026, 8, 29, 8, 0), template_id=10)
    restrictions = oral.active_restrictions(1, datetime(2026, 8, 29, 10, 0))
    assert restrictions["no_food"] is True
    assert restrictions["no_fluids"] is True

    event = oral.create_fact(1, datetime(2026, 8, 29, 10, 0), 50, meal_name="Вода")
    assert event.amount_ml == 50


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

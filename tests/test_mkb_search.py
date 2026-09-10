from __future__ import annotations

import sqlite3
from dataclasses import FrozenInstanceError

import pytest

from rem_card.services.mkb import MKBMatch, MKBService, normalize_code_query


LONG_DIAGNOSIS_NAME = "Очень " + "длинное название без сокращения " * 20


@pytest.fixture
def mkb_db(tmp_path):
    path = tmp_path / "mkb test #1.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE class_mkb (code TEXT, name TEXT)
            """
        )
        conn.executemany(
            "INSERT INTO class_mkb (code, name) VALUES (?, ?)",
            [
                ("K42.9", "Пупочная грыжа без непроходимости или гангрены"),
                ("K42.1", "Пупочная грыжа с гангреной"),
                ("A02.2+", "Локализованная сальмонеллезная инфекция"),
                ("J01.9", "Острый синусит с ёмким описанием"),
                ("K42", "Пупочная грыжа"),
                ("D63*", "Анемия при новообразованиях"),
                ("K42.0", "Пупочная грыжа с непроходимостью без гангрены"),
                ("A00-A09", "Кишечные инфекции"),
                ("Z99.9", LONG_DIAGNOSIS_NAME),
            ],
        )
    return path


@pytest.fixture
def mkb_service(mkb_db):
    service = MKBService(mkb_db)
    yield service
    service.close_connection()


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("k", "K"),
        (" k42 ", "K42"),
        ("k42,1", "K42.1"),
        ("л421", "K42.1"),
        ("a022+", "A02.2+"),
        ("d63*", "D63*"),
        ("a00-a09", "A00-A09"),
        ("пупочная грыжа", None),
        ("сахарный диабет 2", None),
        ("", None),
    ],
)
def test_normalize_code_query(query, expected):
    assert normalize_code_query(query) == expected


def test_text_search_refines_by_all_words_and_exact_name_first(mkb_service):
    assert [match.code for match in mkb_service.search_diagnoses("пупочная")] == [
        "K42", "K42.0", "K42.1", "K42.9"
    ]
    assert [match.code for match in mkb_service.search_diagnoses("пупочная грыжа")] == [
        "K42", "K42.0", "K42.1", "K42.9"
    ]
    assert mkb_service.search_diagnoses("пупочная грыжа с гангреной") == [
        MKBMatch("K42.1", "Пупочная грыжа с гангреной")
    ]


def test_text_search_ignores_word_order_case_and_yo_difference(mkb_service):
    assert mkb_service.search_diagnoses("ГАНГРЕНОЙ пУпОч") == [
        MKBMatch("K42.1", "Пупочная грыжа с гангреной")
    ]
    assert mkb_service.search_diagnoses("ЕМКИМ синус") == [
        MKBMatch("J01.9", "Острый синусит с ёмким описанием")
    ]


def test_code_search_uses_normalized_prefix_and_stable_code_order(mkb_service):
    assert [match.code for match in mkb_service.search_diagnoses("л42")] == [
        "K42", "K42.0", "K42.1", "K42.9"
    ]
    assert mkb_service.search_diagnoses("a00-a09") == [
        MKBMatch("A00-A09", "Кишечные инфекции")
    ]
    assert mkb_service.search_diagnoses("a022") == [
        MKBMatch("A02.2+", "Локализованная сальмонеллезная инфекция")
    ]


def test_search_honours_limit_and_empty_query(mkb_service):
    assert [match.code for match in mkb_service.search_diagnoses("пупочная", limit=2)] == [
        "K42", "K42.0"
    ]
    assert mkb_service.search_diagnoses("пупочная", limit=0) == []
    assert mkb_service.search_diagnoses("   ") == []


def test_exact_lookup_preserves_actual_marked_code_and_legacy_name_contract(mkb_service):
    assert mkb_service.find_diagnosis_by_code("a022") == MKBMatch(
        "A02.2+", "Локализованная сальмонеллезная инфекция"
    )
    assert mkb_service.find_diagnosis_by_code("d63") == MKBMatch(
        "D63*", "Анемия при новообразованиях"
    )
    assert mkb_service.find_diagnosis_by_code("A02.2*") is None
    assert mkb_service.get_diagnosis_by_code(" a022 ") == "Локализованная сальмонеллезная инфекция"
    assert mkb_service.get_diagnosis_by_code("unknown") is None


def test_match_is_frozen_and_full_name_is_not_truncated(mkb_service):
    match = mkb_service.find_diagnosis_by_code("Z99.9")

    assert match == MKBMatch("Z99.9", LONG_DIAGNOSIS_NAME)
    assert len(match.name) > 500
    with pytest.raises(FrozenInstanceError):
        match.name = "Сокращено"


def test_cached_search_issues_no_sql_after_initial_load(mkb_service):
    statements = []
    mkb_service.conn.set_trace_callback(statements.append)

    mkb_service.search_diagnoses("пупочная")
    mkb_service.search_diagnoses("пупочная грыжа")
    mkb_service.find_diagnosis_by_code("K42.1")

    assert statements == []


def test_connection_remains_read_only(mkb_service):
    with pytest.raises(sqlite3.OperationalError, match="readonly|read-only"):
        mkb_service.conn.execute(
            "INSERT INTO class_mkb (code, name) VALUES ('X00', 'Запись запрещена')"
        )

"""Оба чтения назначений сохраняют границы медицинских суток 08:00–08:00."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from rem_card.services.order_domain_service import OrderDomainService


@pytest.mark.parametrize("selected, start, end", [
    ("2026-09-28T07:59:59.999999", "2026-09-27T08:00:00", "2026-09-28T08:00:00"),
    ("2026-09-28T08:00:00", "2026-09-28T08:00:00", "2026-09-29T08:00:00"),
    ("2026-09-28T23:59:59", "2026-09-28T08:00:00", "2026-09-29T08:00:00"),
    ("2027-01-01T00:00:00", "2026-12-31T08:00:00", "2027-01-01T08:00:00"),
])
@pytest.mark.parametrize("all_admissions", [False, True])
def test_order_reads_use_same_half_open_shift_without_write_maintenance(selected, start, end, all_admissions):
    queries = []
    maintenance = []
    db = SimpleNamespace(fetch_all_remcard=lambda sql, params: queries.append((sql, params)) or [])
    service = OrderDomainService(db)
    service._sanitize_legacy_statuses_once = lambda *, allow_write: maintenance.append(allow_write)
    service._load_groups_priority = lambda: {}
    service._load_drugs_groups = lambda: {}
    if all_admissions:
        result = service.get_upcoming_orders_across_active_admissions(datetime.fromisoformat(selected))
        expected = (start, end, start, end)
    else:
        result = service.get_nurse_orders_data(17, datetime.fromisoformat(selected))
        expected = (17, start, end, 17, start, end)
    assert result == []
    assert maintenance == [False]
    assert len(queries) == 1
    sql, params = queries[0]
    assert params == expected
    assert "DATETIME(a.planned_time) >= DATETIME(?)" in sql
    assert "DATETIME(a.planned_time) < DATETIME(?)" in sql

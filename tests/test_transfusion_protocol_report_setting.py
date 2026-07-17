from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.services.procedures_print_service import ProceduresPrintService  # noqa: E402
from rem_card.data.dao.procedures_dao import ProceduresDAO  # noqa: E402
from rem_card.ui.rem_card_sectors.sector_print import (  # noqa: E402
    _get_transfusion_protocols_for_report,
)


class _ReportServiceStub:
    def __init__(self):
        self.calls = []

    def get_unprinted_completed_transfusion_protocols(self, admission_id, **kwargs):
        self.calls.append(("unprinted", admission_id, kwargs))
        return [{"procedure_id": 2}]

    def get_completed_transfusion_protocols(self, admission_id, **kwargs):
        self.calls.append(("all", admission_id, kwargs))
        return [{"procedure_id": 1}, {"procedure_id": 2}]


class _ProceduresDaoStub:
    def __init__(self):
        self.calls = []

    def list_unprinted_completed_transfusion_ids(self, admission_id, **kwargs):
        self.calls.append(("unprinted", admission_id, kwargs))
        return [2]

    def list_completed_transfusion_ids(self, admission_id, **kwargs):
        self.calls.append(("all", admission_id, kwargs))
        return [1, 2]

    @staticmethod
    def get_bundle(procedure_id):
        return SimpleNamespace(
            procedure=SimpleNamespace(id=procedure_id),
            transfusion=SimpleNamespace(),
        )


class _DatabaseStub:
    def __init__(self):
        self.calls = []

    def fetch_all_remcard(self, sql, params):
        self.calls.append((sql, params))
        return [{"id": 11}]


class TransfusionProtocolReportSettingTest(unittest.TestCase):
    def test_disabled_setting_keeps_unprinted_only_behavior(self):
        service = _ReportServiceStub()
        start_dt = datetime(2026, 7, 16, 8)
        end_dt = datetime(2026, 7, 17, 8)

        result = _get_transfusion_protocols_for_report(
            service,
            42,
            {"transfusion_protocols": False},
            start_dt=start_dt,
            end_dt=end_dt,
        )

        self.assertEqual(result, [{"procedure_id": 2}])
        self.assertEqual(
            service.calls,
            [("unprinted", 42, {"start_dt": start_dt, "end_dt": end_dt})],
        )

    def test_enabled_setting_includes_previously_printed_protocols(self):
        service = _ReportServiceStub()

        result = _get_transfusion_protocols_for_report(
            service,
            42,
            {"transfusion_protocols": True},
        )

        self.assertEqual(result, [{"procedure_id": 1}, {"procedure_id": 2}])
        self.assertEqual(service.calls, [("all", 42, {})])

    def test_print_service_has_separate_all_and_unprinted_queries(self):
        dao = _ProceduresDaoStub()
        service = ProceduresPrintService(dao)
        service._transfusion_protocol_context = lambda bundle: {
            "procedure_id": str(bundle.procedure.id)
        }

        unprinted = service.unprinted_completed_transfusion_protocols(7)
        all_protocols = service.completed_transfusion_protocols(7)

        self.assertEqual([item["procedure_id"] for item in unprinted], [2])
        self.assertEqual([item["procedure_id"] for item in all_protocols], [1, 2])
        self.assertEqual(
            dao.calls,
            [("unprinted", 7, {"start_dt": None, "end_dt": None}),
             ("all", 7, {"start_dt": None, "end_dt": None})],
        )

    def test_all_protocol_query_does_not_filter_by_print_timestamp(self):
        database = _DatabaseStub()
        dao = ProceduresDAO(database)

        self.assertEqual(dao.list_completed_transfusion_ids(9), [11])
        all_sql, _all_params = database.calls[-1]
        self.assertNotIn("protocol_printed_at IS NULL", all_sql)

        self.assertEqual(dao.list_unprinted_completed_transfusion_ids(9), [11])
        unprinted_sql, _unprinted_params = database.calls[-1]
        self.assertIn("protocol_printed_at IS NULL", unprinted_sql)


if __name__ == "__main__":
    unittest.main()

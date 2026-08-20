from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.app.bootstrap import LazyRemCardServiceProxy  # noqa: E402
from rem_card.services.remcard_facade import RemCardService, _LazyDependency  # noqa: E402


class LazyStartupServicesTest(unittest.TestCase):
    def test_operblock_proxy_exposes_core_services_without_resolving_facade(self):
        status_service = object()
        data_service = Mock()
        created = []

        def factory(*args, **kwargs):
            service = SimpleNamespace(marker="resolved", status_service=None)
            service.maybe_release_due_outcome_beds = Mock()
            created.append((args, kwargs, service))
            return service

        proxy = LazyRemCardServiceProxy(
            object(),
            object(),
            object(),
            object(),
            object(),
            status_service=status_service,
            data_service=data_service,
            factory=factory,
        )

        self.assertIs(proxy.data_service, data_service)
        self.assertIs(proxy.status_service, status_service)
        self.assertFalse(proxy.is_resolved)
        self.assertEqual(created, [])

        self.assertEqual(proxy.marker, "resolved")
        self.assertTrue(proxy.is_resolved)
        self.assertEqual(len(created), 1)
        self.assertIs(proxy.marker, proxy.resolve().marker)
        data_service.add_poll_maintenance_task.assert_called_once()

    def test_lazy_dependency_resolves_only_once(self):
        created = []
        dependency = _LazyDependency(lambda: created.append(SimpleNamespace(value=42)) or created[-1])

        self.assertFalse(dependency.is_resolved)
        self.assertEqual(dependency.value, 42)
        self.assertEqual(dependency.value, 42)
        self.assertTrue(dependency.is_resolved)
        self.assertEqual(len(created), 1)

    def test_remcard_optional_domains_are_not_created_by_constructor(self):
        db = Mock()
        patient_dao = Mock(db=db)
        vitals_dao = Mock(db=db)
        fluids_dao = Mock(db=db)
        orders_dao = Mock(db=db)
        ventilation_dao = Mock(db=db)

        service = RemCardService(
            vitals_dao,
            fluids_dao,
            orders_dao,
            ventilation_dao,
            patient_dao,
        )

        for dependency in (
            service.diet_plan_dao,
            service.oral_intake_dao,
            service._diet_templates,
            service._diet_plan,
            service._oral_intake,
            service.procedures_dao,
            service._procedures,
            service._procedures_print,
            service.lab_orders_dao,
            service._lab_analysis_catalog,
            service._lab_orders,
        ):
            self.assertIsInstance(dependency, _LazyDependency)
            self.assertFalse(dependency.is_resolved)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.app.bootstrap import (  # noqa: E402
    LazyOperBlockServiceProxy,
    create_lazy_operblock_service,
)
from rem_card.app.roles import OPERBLOCK_ROLE_KEYS  # noqa: E402


class _FakeOperBlockService:
    def __init__(self, db):
        self.db = db

    def ping(self, value):
        return (self.db, value)


class LazyOperBlockServiceProxyTest(unittest.TestCase):
    def test_current_access_patterns_resolve_once_and_delegate(self):
        db = object()
        calls = []
        proxy = LazyOperBlockServiceProxy(
            db,
            factory=lambda manager: calls.append(manager) or _FakeOperBlockService(manager),
        )

        self.assertFalse(proxy.is_resolved)
        self.assertTrue(hasattr(proxy, "ping"))
        self.assertTrue(proxy.is_resolved)
        self.assertIs(proxy.db, db)
        self.assertEqual(proxy.ping("ok"), (db, "ok"))
        self.assertIs(proxy.resolve(), proxy.resolve())
        self.assertEqual(calls, [db])

    def test_parallel_first_access_constructs_single_instance(self):
        db = object()
        calls = []
        calls_lock = threading.Lock()

        def factory(manager):
            with calls_lock:
                calls.append(manager)
            time.sleep(0.03)
            return _FakeOperBlockService(manager)

        proxy = LazyOperBlockServiceProxy(db, factory=factory)
        results = []
        threads = [threading.Thread(target=lambda: results.append(proxy.ping("parallel"))) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2.0)

        self.assertEqual(len(calls), 1)
        self.assertEqual(results, [(db, "parallel")] * 8)

    def test_only_operblock_roles_are_eager(self):
        for role in (None, "doctor", "nurse"):
            calls = []
            proxy = create_lazy_operblock_service(
                object(),
                role,
                factory=lambda manager: calls.append(manager) or _FakeOperBlockService(manager),
            )
            self.assertFalse(proxy.is_resolved, role)
            self.assertEqual(calls, [], role)

        for role in OPERBLOCK_ROLE_KEYS:
            calls = []
            proxy = create_lazy_operblock_service(
                object(),
                role,
                factory=lambda manager: calls.append(manager) or _FakeOperBlockService(manager),
            )
            self.assertTrue(proxy.is_resolved, role)
            self.assertEqual(len(calls), 1, role)


if __name__ == "__main__":
    unittest.main()


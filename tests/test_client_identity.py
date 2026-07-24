from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from rem_card.app import client_identity


class ClientIdentityTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        client_identity._CACHED_IDENTITY = None

    def tearDown(self):
        client_identity._CACHED_IDENTITY = None
        self._tmp.cleanup()

    def test_identity_is_persistent_across_cache_reset(self):
        with patch.dict("os.environ", {"LOCALAPPDATA": str(self.root)}, clear=False):
            first = client_identity.get_client_id()
            client_identity._CACHED_IDENTITY = None
            second = client_identity.get_client_id()

        self.assertEqual(second, first)
        self.assertEqual(str(uuid.UUID(first)), first)
        payload = json.loads(
            (self.root / "RemCard" / "client_identity.json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["client_id"], first)

    def test_valid_environment_override_does_not_touch_identity_file(self):
        override = str(uuid.uuid4())
        with patch.dict(
            "os.environ",
            {
                "LOCALAPPDATA": str(self.root),
                client_identity.CLIENT_ID_ENV: override,
            },
            clear=False,
        ):
            result = client_identity.get_client_id()

        self.assertEqual(result, override)
        self.assertFalse((self.root / "RemCard" / "client_identity.json").exists())


if __name__ == "__main__":
    unittest.main()

"""Contract checks for bilingual setup and explicit accepted release profiles."""
from __future__ import annotations

import json
from pathlib import Path
import re
import unittest
from unittest import mock
import hashlib
import tempfile
import zipfile

from build_installer import ROOT, build, validate_release_profile


SOURCE = Path(__file__).resolve().parent
CS_TOKEN = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|\'(?:[^\'\\]|\\.)*\'|@"(?:[^"]|"")*"|"(?:[^"\\]|\\.)*"')


def strings(source: str) -> set[str]:
    result = set()
    for match in CS_TOKEN.finditer(source):
        token = match[0]
        if token.startswith('@"'):
            result.add(token[2:-1].replace('""', '"'))
        elif token.startswith('"'):
            result.add(json.loads(token))
    return result


class InstallerLocalizationTests(unittest.TestCase):
    def test_catalog_covers_all_owned_human_strings(self):
        catalog = strings((SOURCE / "InstallerText.cs").read_text(encoding="utf-8-sig"))
        # Stable registration/shortcut identity is deliberately language independent.
        opaque = {"Ремкарта.lnk", "Ремкарта", "Битюцкий Вадим Алексеевич", "Русский"}
        missing = set()
        for filename in ("App.cs", "Contracts.cs", "EcgMonitor.cs", "InstallerEngine.cs", "InstallerWindow.cs", "PayloadArchive.cs"):
            for text in strings((SOURCE / filename).read_text(encoding="utf-8-sig")):
                if re.search(r"[\u0400-\u04ff]", text) and text not in catalog | opaque:
                    missing.add((filename, text))
        self.assertEqual(missing, set())

    def profile(self, disabled=False, floor="5.1.15"):
        return {"package_type": "full", "schema_version": 2, "version": "5.1.15",
                "source_commit": "a" * 40, "min_client_version": floor,
                "database_upgrades_disabled": disabled}

    def validate(self, manifest, disabled=False, floor="5.1.15"):
        return validate_release_profile(manifest, expected_version="5.1.15",
            expected_min_client_version=floor, database_upgrades_disabled=disabled)

    def test_mandatory_profile_is_accepted_without_mutation(self):
        manifest = self.profile()
        before = json.dumps(manifest, sort_keys=True)
        self.assertEqual(self.validate(manifest)["database_upgrades_disabled"], False)
        self.assertEqual(json.dumps(manifest, sort_keys=True), before)

    def test_optional_profile_requires_explicit_matching_floor(self):
        self.assertTrue(self.validate(self.profile(True, "5.1.11"), True, "5.1.11")["database_upgrades_disabled"])
        with self.assertRaises(RuntimeError):
            self.validate(self.profile(True, "5.1.11"))

    def test_wrong_policy_missing_or_non_boolean_are_rejected(self):
        for value in (True, "false", 0, None):
            manifest = self.profile()
            manifest["database_upgrades_disabled"] = value
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                self.validate(manifest)

    def test_wrong_version_floor_commit_and_schema_are_rejected(self):
        for key, value in (("version", "5.1.14"), ("min_client_version", "5.1.11"),
                           ("source_commit", "main"), ("schema_version", 1), ("package_type", "patch")):
            manifest = self.profile()
            manifest[key] = value
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                self.validate(manifest)

    def test_corrupt_and_duplicate_zip_entries_are_rejected_before_compiling(self):
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        for case in ("hash", "duplicate", "extra", "traversal"):
            with self.subTest(case=case), tempfile.TemporaryDirectory(prefix="RemCard-installer-QA-contract-") as temporary:
                root = Path(temporary)
                content = b"accepted bytes"
                path = "../escape.exe" if case == "traversal" else "RemCard.exe"
                manifest = {**self.profile(), "version": version, "min_client_version": version,
                            "files": [{"path": path, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}]}
                package = root / "accepted.zip"
                with zipfile.ZipFile(package, "w") as archive:
                    archive.writestr(path, b"tampered bytes" if case == "hash" else content)
                    archive.writestr("manifest.json", json.dumps(manifest))
                    archive.writestr("ready.ok", "ready")
                    if case == "duplicate":
                        archive.writestr("remcard.exe", content)
                    if case == "extra":
                        archive.writestr("personal-data.txt", "not accepted")
                with mock.patch("build_installer.compile_stub", side_effect=AssertionError("Compilation must not begin")):
                    with self.assertRaises(RuntimeError):
                        build(package, root / "build", hashlib.sha256(package.read_bytes()).hexdigest(),
                              expected_min_client_version=version, database_upgrades_disabled=False)


if __name__ == "__main__":
    unittest.main()

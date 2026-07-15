from __future__ import annotations

from scripts import security_repo_check as security_check


def test_internal_paths_with_both_separator_styles_are_rejected() -> None:
    backslash_findings = security_check._scan_content(
        "docs/example.md",
        "Путь C:\\Project\\rem_card и Z:\\РАО\\Пациенты".encode("utf-8"),
    )
    slash_findings = security_check._scan_content(
        "docs/example.md",
        "Путь C:/Project/rem_card и Z:/РАО/Пациенты".encode("utf-8"),
    )

    assert "real-project-path" in backslash_findings
    assert "department-drive-path" in backslash_findings
    assert "real-project-path" in slash_findings
    assert "department-drive-path" in slash_findings


def test_documentation_examples_are_allowed() -> None:
    findings = security_check._scan_content(
        "docs/example.md",
        (
            "C:/Test/rem_card\n"
            "T:/TestDepartment/Updates\n"
            r"\\test-fileserver.example.test\share\TestDepartment"
        ).encode("utf-8"),
    )

    assert findings == []


def test_personal_contacts_and_known_private_identity_are_rejected() -> None:
    findings = security_check._scan_content(
        "README.md",
        "Иван Битюцкий, user@acrb-amursk.ru".encode("utf-8"),
    )

    assert "email-address" in findings
    assert "known-private-identity" in findings
    assert "private-organization-domain" in findings


def test_runtime_medical_data_and_key_files_are_rejected() -> None:
    assert security_check._forbidden_path_reason("Baza_rao3_jurnal/rao_journal.db")
    assert security_check._forbidden_path_reason("data/backups/rao_journal_1.db")
    assert security_check._forbidden_path_reason("logs/audit.jsonl")
    assert security_check._forbidden_path_reason("secrets/private.key")


def test_reference_database_is_allowed() -> None:
    assert security_check._forbidden_path_reason("data/mkb/mkb10.db") == ""

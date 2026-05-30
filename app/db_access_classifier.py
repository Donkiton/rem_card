from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from rem_card.app.db_availability import DatabaseUnavailableError


DatabaseAccessCategory = Literal[
    "network_unavailable",
    "locked_busy",
    "corruption",
    "schema_incompatible",
    "policy_block",
    "unknown",
]


@dataclass(frozen=True)
class DatabaseAccessClassification:
    category: DatabaseAccessCategory
    reason: str


def _error_text(exc: object) -> str:
    values = [str(exc or "")]
    for attr in ("technical_reason", "user_message", "reason"):
        try:
            value = getattr(exc, attr)
        except Exception:
            value = ""
        if value:
            values.append(str(value))
    return "\n".join(values).lower()


def classify_database_access_error(exc: object) -> DatabaseAccessCategory:
    return classify_database_access(exc).category


def classify_database_access(exc: object) -> DatabaseAccessClassification:
    text = _error_text(exc)
    if isinstance(exc, DatabaseUnavailableError):
        return DatabaseAccessClassification("network_unavailable", str(exc))

    corruption_markers = (
        "database disk image is malformed",
        "file is not a database",
        "database corruption",
        "sqlite_master",
        "malformed",
        "not a database",
        "quick_check failed",
        "integrity_check failed",
        "поврежден",
        "повреждена",
        "повреждение",
    )
    if any(marker in text for marker in corruption_markers):
        return DatabaseAccessClassification("corruption", str(exc))

    policy_markers = (
        "min_client_version",
        "client_policy",
        "версия программы устарела",
        "профиль доступа к базе",
        "wal для сетевой базы запрещ",
        "работа заблокирована",
    )
    if any(marker in text for marker in policy_markers):
        return DatabaseAccessClassification("policy_block", str(exc))

    schema_markers = (
        "schema version is newer",
        "schema incompatible",
        "schema mismatch",
        "migration failed",
        "schema migration",
        "версия бд настроек новее",
        "схема",
        "миграц",
        "несовместим",
    )
    if any(marker in text for marker in schema_markers):
        return DatabaseAccessClassification("schema_incompatible", str(exc))

    locked_markers = (
        "database is locked",
        "database table is locked",
        "database schema is locked",
        "could not acquire sequential write lock",
        "recovery lock",
        "db lock",
        "locked",
        "busy",
        "занят",
        "заблок",
    )
    if any(marker in text for marker in locked_markers):
        return DatabaseAccessClassification("locked_busy", str(exc))

    if isinstance(exc, (sqlite3.OperationalError, sqlite3.DatabaseError, OSError, PermissionError)):
        network_type_hint = True
    else:
        network_type_hint = False

    network_markers = (
        "unable to open database file",
        "cannot open",
        "database file does not exist",
        "does not exist",
        "not found",
        "no such file",
        "path not found",
        "path inaccessible",
        "network",
        "remote",
        "device is not ready",
        "disk i/o error",
        "disk io error",
        "input/output error",
        "permission denied",
        "access is denied",
        "readonly database",
        "read-only",
        "attempt to write a readonly database",
        "папка базы недоступна",
        "база временно недоступна",
        "база данных недоступна",
        "путь к папке базы недоступен",
        "не удалось подготовить защитный контур",
        "не удалось проверить базу данных",
        "не удается найти",
        "системе не удается",
        "отказано в доступе",
        "недоступ",
        "сетев",
    )
    if network_type_hint and any(marker in text for marker in network_markers):
        return DatabaseAccessClassification("network_unavailable", str(exc))
    if any(marker in text for marker in network_markers):
        return DatabaseAccessClassification("network_unavailable", str(exc))

    return DatabaseAccessClassification("unknown", str(exc))

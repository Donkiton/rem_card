from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Optional

from rem_card.app.paths import MKB_DB_PATH
from rem_card.app.sqlite_uri import build_sqlite_file_uri


@dataclass(frozen=True)
class MKBMatch:
    code: str
    name: str


_RUSSIAN_KEYBOARD_TO_LATIN = str.maketrans(
    {
        "й": "Q", "ц": "W", "у": "E", "к": "R", "е": "T", "н": "Y",
        "г": "U", "ш": "I", "щ": "O", "з": "P", "ф": "A", "ы": "S",
        "в": "D", "а": "F", "п": "G", "р": "H", "о": "J", "л": "K",
        "д": "L", "я": "Z", "ч": "X", "с": "C", "м": "V", "и": "B",
        "т": "N", "ь": "M",
    }
)
_CODE_QUERY_RE = re.compile(
    r"^[A-Z](?:\d*(?:\.\d*)?)?(?:-[A-Z]?\d*(?:\.\d*)?)?[+*]?$"
)
_COMPACT_CODE_RE = re.compile(r"^([A-Z]\d{2})(\d)([+*]?)$")


def normalize_code_query(query: str) -> str | None:
    """Return a normalized MKB code or code prefix, otherwise ``None``."""

    value = unicodedata.normalize("NFKC", str(query or "")).strip()
    if not value:
        return None

    value = value.replace(",", ".")
    first_character = value[0].lower()
    translated_first_character = first_character.translate(_RUSSIAN_KEYBOARD_TO_LATIN)
    if translated_first_character != first_character and any(
        character.isdigit() for character in value
    ):
        value = translated_first_character + value[1:]
    value = value.upper()

    if not _CODE_QUERY_RE.fullmatch(value):
        return None

    compact = _COMPACT_CODE_RE.fullmatch(value)
    if compact:
        value = f"{compact.group(1)}.{compact.group(2)}{compact.group(3)}"
    return value


def _normalize_search_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(normalized.replace("ё", "е").split())


class MKBService:
    def __init__(self, db_path: str = MKB_DB_PATH):
        self.db_path = db_path
        self.conn = sqlite3.connect(
            build_sqlite_file_uri(self.db_path, mode="ro"),
            uri=True,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA query_only = ON")
        self.cursor = self.conn.cursor()

        rows = self.conn.execute(
            """
            SELECT code, name
            FROM class_mkb
            WHERE code IS NOT NULL AND name IS NOT NULL
            ORDER BY code COLLATE NOCASE, code
            """
        ).fetchall()
        self._matches = tuple(MKBMatch(code=row["code"], name=row["name"]) for row in rows)
        self._searchable_names = tuple(_normalize_search_text(match.name) for match in self._matches)
        self._matches_by_code = {match.code.upper(): match for match in self._matches}

    def search_diagnoses(self, query: str, limit: int = 30) -> list[MKBMatch]:
        if limit <= 0:
            return []

        code_query = normalize_code_query(query)
        if code_query is not None:
            matches = [
                match
                for match in self._matches
                if match.code.upper().startswith(code_query)
            ]
            matches.sort(
                key=lambda match: (
                    0
                    if match.code.upper() == code_query
                    else 1
                    if match.code.upper().rstrip("+*") == code_query
                    else 2,
                    match.code.casefold(),
                    match.code,
                )
            )
            return matches[:limit]

        text_query = _normalize_search_text(query)
        if not text_query:
            return []
        words = text_query.split()
        matches_with_names = [
            (match, searchable_name)
            for match, searchable_name in zip(self._matches, self._searchable_names)
            if all(word in searchable_name for word in words)
        ]
        matches_with_names.sort(
            key=lambda item: (
                item[1] != text_query,
                item[0].code.casefold(),
                item[0].code,
            )
        )
        return [match for match, _searchable_name in matches_with_names[:limit]]

    def find_diagnosis_by_code(self, query: str) -> MKBMatch | None:
        code = normalize_code_query(query)
        if code is None:
            return None

        match = self._matches_by_code.get(code)
        if match is not None or code.endswith(("+", "*")):
            return match
        return self._matches_by_code.get(f"{code}+") or self._matches_by_code.get(f"{code}*")

    def get_diagnosis_by_code(self, code: str) -> Optional[str]:
        match = self.find_diagnosis_by_code(code)
        return match.name if match else None

    def close_connection(self):
        if self.conn:
            self.conn.close()
            self.conn = None

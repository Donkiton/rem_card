from __future__ import annotations

import argparse
import csv
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CSV = (
    Path(__file__).resolve().parents[2]
    / "mkb10"
    / "resources"
    / "1.2.643.5.1.13.13.11.1005_2.27.csv"
)
DEFAULT_MKB_DIR = Path(__file__).resolve().parents[1] / "data" / "mkb"


@dataclass(frozen=True)
class CsvRow:
    id: int
    parent_id: int | None
    code: str
    name: str
    addl_code: str


@dataclass(frozen=True)
class MkbRow:
    id: int
    name: str
    code: str
    parent_id: int | None
    parent_code: str | None
    node_count: int
    additional_info: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild data/mkb read-only reference data from the official MKB-10 CSV."
    )
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--mkb-dir", type=Path, default=DEFAULT_MKB_DIR)
    return parser.parse_args()


def read_csv(path: Path) -> list[CsvRow]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file, delimiter=";")
        rows = []
        for raw in reader:
            parent = raw["ID_PARENT"].strip()
            rows.append(
                CsvRow(
                    id=int(raw["ID"]),
                    parent_id=int(parent) if parent else None,
                    code=raw["MKB_CODE"].strip(),
                    name=raw["MKB_NAME"].strip(),
                    addl_code=raw["ADDL_CODE"].strip(),
                )
            )
    return rows


def read_existing_info(db_path: Path) -> dict[str, str]:
    if not db_path.exists():
        return {}

    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            """
            SELECT code, additional_info
            FROM class_mkb
            WHERE additional_info IS NOT NULL AND TRIM(additional_info) != ''
            """
        ).fetchall()
    finally:
        connection.close()

    return {code: info for code, info in rows}


def split_range(code: str) -> tuple[str, str]:
    if "-" not in code:
        return code, code
    start, end = code.split("-", 1)
    return start, end


def code_with_suffix(row: CsvRow) -> str:
    if row.addl_code == "1":
        return f"{row.code}+"
    if row.addl_code == "2":
        return f"{row.code}*"
    return row.code


def build_chapter_codes(rows: list[CsvRow]) -> dict[int, str]:
    children: dict[int, list[CsvRow]] = defaultdict(list)
    for row in rows:
        if row.parent_id is not None:
            children[row.parent_id].append(row)

    chapter_codes = {}
    for row in rows:
        if row.parent_id is not None:
            continue

        direct_children = children[row.id]
        if not direct_children:
            chapter_codes[row.id] = row.code
            continue

        start, _ = split_range(direct_children[0].code)
        _, end = split_range(direct_children[-1].code)
        chapter_codes[row.id] = f"{start}-{end}"

    return chapter_codes


def convert_rows(rows: list[CsvRow], existing_info: dict[str, str]) -> list[MkbRow]:
    by_id = {row.id: row for row in rows}
    children: dict[int, list[CsvRow]] = defaultdict(list)
    for row in rows:
        if row.parent_id is not None:
            children[row.parent_id].append(row)

    chapter_codes = build_chapter_codes(rows)
    converted = []
    for row in rows:
        code = chapter_codes.get(row.id, code_with_suffix(row))
        parent_code = None
        if row.parent_id is not None:
            parent = by_id[row.parent_id]
            parent_code = chapter_codes.get(parent.id, code_with_suffix(parent))

        converted.append(
            MkbRow(
                id=row.id,
                name=row.name,
                code=code,
                parent_id=row.parent_id,
                parent_code=parent_code,
                node_count=len(children[row.id]),
                additional_info=existing_info.get(code),
            )
        )

    return converted


def create_sqlite_db(db_path: Path, rows: list[MkbRow]) -> None:
    temp_path = db_path.with_suffix(".tmp.db")
    if temp_path.exists():
        temp_path.unlink()

    connection = sqlite3.connect(temp_path)
    try:
        connection.executescript(
            """
            CREATE TABLE class_mkb (
                id INTEGER PRIMARY KEY,
                name TEXT,
                code TEXT,
                parent_id INTEGER,
                parent_code TEXT,
                node_count INTEGER,
                additional_info TEXT
            );
            CREATE INDEX class_mkb_code_idx ON class_mkb(code COLLATE NOCASE);
            CREATE INDEX class_mkb_parent_id_idx ON class_mkb(parent_id);
            CREATE INDEX class_mkb_parent_code_idx ON class_mkb(parent_code);
            """
        )
        connection.executemany(
            """
            INSERT INTO class_mkb
                (id, name, code, parent_id, parent_code, node_count, additional_info)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row.id,
                    row.name,
                    row.code,
                    row.parent_id,
                    row.parent_code,
                    row.node_count,
                    row.additional_info,
                )
                for row in rows
            ],
        )
        connection.commit()
    finally:
        connection.close()

    temp_path.replace(db_path)


def sql_literal(value: str | int | None) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)

    escaped = value.replace("'", "''").replace("\r\n", "\n")
    escaped = escaped.replace("\r", "\n").replace("\n", "\\n")
    return f"'{escaped}'"


def create_mysql_dump(sql_path: Path, rows: list[MkbRow]) -> None:
    temp_path = sql_path.with_suffix(".tmp.sql")
    with temp_path.open("w", encoding="utf-8") as file:
        file.write("set names utf8;\n")
        for row in rows:
            values = (
                row.id,
                row.name,
                row.code,
                row.parent_id,
                row.parent_code,
                row.node_count,
                row.additional_info,
            )
            rendered = ", ".join(sql_literal(value) for value in values)
            file.write(
                "INSERT INTO class_mkb "
                "(id, name, code, parent_id, parent_code, node_count, additional_info) "
                f"VALUES({rendered});\n"
            )

    temp_path.replace(sql_path)


def validate(rows: list[MkbRow]) -> None:
    ids = {row.id for row in rows}
    missing_parents = [
        row for row in rows if row.parent_id is not None and row.parent_id not in ids
    ]
    if missing_parents:
        sample = ", ".join(str(row.id) for row in missing_parents[:10])
        raise ValueError(f"Missing parent rows for ids: {sample}")


def main() -> None:
    args = parse_args()
    csv_path = args.csv.resolve()
    mkb_dir = args.mkb_dir.resolve()
    db_path = mkb_dir / "mkb10.db"
    sql_path = mkb_dir / "mkb_data.sql"

    rows = read_csv(csv_path)
    existing_info = read_existing_info(db_path)
    converted = convert_rows(rows, existing_info)
    validate(converted)

    create_sqlite_db(db_path, converted)
    create_mysql_dump(sql_path, converted)

    print(f"source_rows={len(rows)}")
    print(f"converted_rows={len(converted)}")
    print(f"preserved_additional_info={sum(1 for row in converted if row.additional_info)}")
    print(f"sqlite_db={db_path}")
    print(f"mysql_dump={sql_path}")


if __name__ == "__main__":
    main()

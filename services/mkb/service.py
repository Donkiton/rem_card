import sqlite3
from typing import Optional

from rem_card.app.paths import MKB_DB_PATH


class MKBService:
    def __init__(self, db_path: str = MKB_DB_PATH):
        self.db_path = db_path
        uri = f"file:{self.db_path}?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True)
        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()

    def get_diagnosis_by_code(self, code: str) -> Optional[str]:
        cleaned_code = str(code or "").strip().upper()
        if not cleaned_code:
            return None

        candidates = [cleaned_code]
        if not cleaned_code.endswith(("+", "*")):
            candidates.extend([f"{cleaned_code}+", f"{cleaned_code}*"])

        placeholders = ", ".join("?" for _ in candidates)
        self.cursor.execute(
            f"""
            SELECT name
            FROM class_mkb
            WHERE code COLLATE NOCASE IN ({placeholders})
            ORDER BY CASE UPPER(code)
                WHEN ? THEN 0
                WHEN ? THEN 1
                WHEN ? THEN 2
                ELSE 3
            END
            LIMIT 1
            """,
            (*candidates, cleaned_code, f"{cleaned_code}+", f"{cleaned_code}*"),
        )
        row = self.cursor.fetchone()
        return row["name"] if row else None

    def close_connection(self):
        if self.conn:
            self.conn.close()
            self.conn = None

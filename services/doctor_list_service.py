import json
import os
import tempfile
from typing import Any, List, Optional

from rem_card.app.paths import USER_DICT_DIR


DOCTOR_LIST_FILE_NAME = "death_protocol_doctors.json"


class DoctorListStore:
    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(USER_DICT_DIR, DOCTOR_LIST_FILE_NAME)

    def load_doctors(self) -> List[str]:
        payload = self._read_payload()
        raw_items: Any
        if isinstance(payload, list):
            raw_items = payload
        elif isinstance(payload, dict):
            raw_items = payload.get("doctors", payload.get("items", []))
        else:
            raw_items = []
        return self._normalize_doctors(raw_items)

    def save_doctors(self, doctors: List[str]) -> None:
        normalized = self._normalize_doctors(doctors)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        payload = {"doctors": normalized}
        directory = os.path.dirname(self.path)
        fd, tmp_path = tempfile.mkstemp(prefix=".death_protocol_doctors_", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp_path, self.path)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

    def _read_payload(self) -> Any:
        if not os.path.exists(self.path):
            return {"doctors": []}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Файл списка врачей поврежден: {self.path} ({exc})") from exc

    @staticmethod
    def _normalize_doctors(items: Any) -> List[str]:
        if not isinstance(items, list):
            return []
        result = []
        seen = set()
        for item in items:
            text = " ".join(str(item or "").split())
            key = text.lower()
            if not text or key in seen:
                continue
            seen.add(key)
            result.append(text)
        return result

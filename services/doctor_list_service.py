import json
import os
import tempfile
from typing import Any, List, Optional


DOCTOR_LIST_FILE_NAME = "death_protocol_doctors.json"


class DoctorListStore:
    def __init__(self, path: Optional[str] = None):
        self.path = path
        if path is None:
            from rem_card.services.settings.settings_service import get_settings_service

            self.settings_service = get_settings_service()
        else:
            self.settings_service = None

    def load_doctors(self) -> List[str]:
        if self.settings_service is not None:
            return self.settings_service.load_doctors()
        payload = self._read_payload()
        raw_items: Any
        if isinstance(payload, list):
            raw_items = payload
        elif isinstance(payload, dict):
            raw_items = payload.get("doctors", payload.get("items", []))
        else:
            raw_items = []
        return [item["full_name"] for item in self._normalize_doctor_records(raw_items)]

    def load_doctor_records(self) -> List[dict[str, str]]:
        if self.settings_service is not None:
            loader = getattr(self.settings_service, "load_doctor_records", None)
            if callable(loader):
                return loader()
            return [{"full_name": name, "position": ""} for name in self.settings_service.load_doctors()]
        payload = self._read_payload()
        raw_items: Any
        if isinstance(payload, list):
            raw_items = payload
        elif isinstance(payload, dict):
            raw_items = payload.get("doctors", payload.get("items", []))
        else:
            raw_items = []
        return self._normalize_doctor_records(raw_items)

    def save_doctors(self, doctors: List[str]) -> None:
        if self.settings_service is not None:
            self.settings_service.save_doctors(doctors)
            return
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

    def save_doctor_records(self, doctors: List[dict[str, str]]) -> None:
        if self.settings_service is not None:
            saver = getattr(self.settings_service, "save_doctor_records", None)
            if callable(saver):
                saver(doctors)
                return
            self.settings_service.save_doctors([item.get("full_name", "") for item in doctors or []])
            return
        normalized = self._normalize_doctor_records(doctors)
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
        return [item["full_name"] for item in DoctorListStore._normalize_doctor_records(items)]

    @staticmethod
    def _normalize_doctor_records(items: Any) -> List[dict[str, str]]:
        if not isinstance(items, list):
            return []
        result: list[dict[str, str]] = []
        seen = set()
        for item in items:
            if isinstance(item, dict):
                full_name = " ".join(str(
                    item.get("full_name")
                    or item.get("name")
                    or item.get("doctor")
                    or item.get("fio")
                    or ""
                ).split())
                position = " ".join(str(item.get("position") or "").split())
            else:
                full_name = " ".join(str(item or "").split())
                position = ""
            key = full_name.lower()
            if not full_name or key in seen:
                continue
            seen.add(key)
            result.append({"full_name": full_name, "position": position})
        return result

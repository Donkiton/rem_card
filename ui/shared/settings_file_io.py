"""Общие файловые операции настроек фона и компоновки.

Политики загрузки, нормализации и работы с settings.db остаются в хранилищах.
ThemeStorage использует отдельный протокол с уникальным временным файлом.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any


def save_settings_json(path: str, payload: dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, path)


def quarantine_broken_settings_file(path: str) -> None:
    if not os.path.exists(path):
        return
    stamp = time.strftime("%Y%m%d_%H%M%S")
    broken_path = f"{path}.{stamp}.broken"
    try:
        os.replace(path, broken_path)
    except Exception:
        pass

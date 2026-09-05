"""Явное распределение модулей: каждый тест попадает ровно в одну группу."""

from __future__ import annotations

import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
GROUPS_PATH = PROJECT_ROOT / "tests" / "groups.json"


def load_groups(root: Path = PROJECT_ROOT) -> dict[str, str]:
    payload = json.loads((root / "tests" / "groups.json").read_text(encoding="utf-8"))
    if set(payload) != {"core", "ui"}:
        raise ValueError("Группы тестов должны называться core и ui")
    groups: dict[str, str] = {}
    for group, paths in payload.items():
        if not isinstance(paths, list) or not paths:
            raise ValueError(f"Пустая или некорректная группа {group}")
        for path in paths:
            if not isinstance(path, str) or path in groups:
                raise ValueError(f"Повторный или некорректный модуль: {path!r}")
            groups[path] = group
    actual = {
        path.relative_to(root).as_posix()
        for path in (root / "tests").rglob("*.py")
        if path.name.startswith("test_") or path.name.endswith("_test.py")
    }
    if set(groups) != actual:
        raise ValueError(
            "Обновите tests/groups.json: "
            f"без группы={sorted(actual - groups.keys())}; "
            f"отсутствуют={sorted(groups.keys() - actual)}"
        )
    return groups


def test_area(path: str) -> str:
    name = Path(path).stem
    if any(word in name for word in ("sqlite", "replica", "snapshot", "database", "backup", "rotation", "lock", "network")):
        return "database"
    if any(word in name for word in ("updat", "compiled", "release", "startup")):
        return "updates"
    if any(word in name for word in ("burn", "nutrition", "diet", "balance", "analytics", "outcome", "electrolyte")):
        return "clinical"
    return "infrastructure"

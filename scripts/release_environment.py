"""Verify pinned build dependencies and inspect bundled versions without running code."""
from __future__ import annotations

import dis
import hashlib
from importlib import metadata
from pathlib import Path
import platform
import sys

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


def validate_build_environment(root: Path) -> dict:
    requirements = (root / "requirements.txt").read_bytes()
    packages = {}
    failures = []
    for line in requirements.decode("utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        pins = list(requirement.specifier)
        if len(pins) != 1 or pins[0].operator != "==" or "*" in pins[0].version:
            raise RuntimeError(f"Зависимость сборки должна иметь точную версию: {line}")
        name = canonicalize_name(requirement.name)
        try:
            installed = metadata.version(requirement.name)
        except metadata.PackageNotFoundError:
            failures.append(f"{name}: не установлен, требуется {pins[0].version}")
            continue
        if Version(installed) != Version(pins[0].version):
            failures.append(f"{name}: установлен {installed}, требуется {pins[0].version}")
        packages[name] = installed
    if failures:
        raise RuntimeError("Окружение сборки не соответствует requirements.txt:\n" + "\n".join(failures))
    return {
        "schema_version": 1,
        "requirements_sha256": hashlib.sha256(requirements).hexdigest(),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": sys.platform,
        "packages": packages,
        "installed_distributions": {
            canonicalize_name(distribution.metadata["Name"]): distribution.version
            for distribution in metadata.distributions() if distribution.metadata.get("Name")
        },
    }


def _constant_assignment(code, name: str) -> str:
    """Read generated version assignments, never execute a bundled module."""
    instructions = list(dis.get_instructions(code))
    for index, instruction in enumerate(instructions):
        if instruction.opname not in {"STORE_NAME", "STORE_GLOBAL"} or instruction.argval != name:
            continue
        previous = instructions[index - 1] if index else None
        if previous is not None and previous.opname == "LOAD_CONST" and isinstance(previous.argval, str):
            return previous.argval
    raise RuntimeError(f"Не найдена статическая версия {name} в {code.co_name}")


def verify_bundled_runtime_versions(executable: Path, expected: dict) -> dict:
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(executable)).open_embedded_archive("PYZ.pyz")
    modules = {
        "pandas": ("pandas._version_meson", "__version__"),
        "numpy": ("numpy.version", "version"),
        "pyside6": ("PySide6", "__version__"),
    }
    versions = {}
    for package, (module, variable) in modules.items():
        if package not in expected:
            raise RuntimeError(f"Нет ожидаемой версии runtime-зависимости {package}")
        try:
            actual = _constant_assignment(archive.extract(module), variable)
        except (KeyError, AttributeError, TypeError) as exc:
            raise RuntimeError(f"Не удалось проверить упакованную зависимость {package}") from exc
        if Version(actual) != Version(expected[package]):
            raise RuntimeError(
                f"В EXE упакован {package} {actual}, окружение сборки сообщает {expected[package]}"
            )
        versions[package] = actual
    return versions

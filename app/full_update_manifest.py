from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


MANIFEST_FILE_NAME = "manifest.json"
READY_FILE_NAME = "ready.ok"
APP_ID = "rem_card"
PACKAGE_TYPE_FULL = "full"
FULL_MANIFEST_SCHEMA_VERSION = 2
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SERVICE_FILES = {MANIFEST_FILE_NAME.casefold(), READY_FILE_NAME.casefold()}


class FullUpdateManifestError(ValueError):
    pass


def get_manifest_schema_version(manifest: dict[str, Any]) -> int:
    if "schema_version" not in manifest:
        return 1
    value = manifest.get("schema_version")
    if type(value) is not int or value not in (1, FULL_MANIFEST_SCHEMA_VERSION):
        raise FullUpdateManifestError(
            "Неподдерживаемый schema_version full-manifest: "
            f"{value!r}. Поддерживаются только 1 и {FULL_MANIFEST_SCHEMA_VERSION}."
        )
    return value


def get_package_type(manifest: dict[str, Any]) -> str:
    package_type = str(manifest.get("package_type") or PACKAGE_TYPE_FULL).strip().lower()
    return package_type or PACKAGE_TYPE_FULL


def normalize_release_path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or "\x00" in raw:
        raise FullUpdateManifestError("Пустой или некорректный путь файла в full-manifest.")
    if raw.startswith("/") or raw.startswith("//"):
        raise FullUpdateManifestError(f"Абсолютный путь запрещён в full-manifest: {raw}")
    path = PurePosixPath(raw)
    parts = path.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise FullUpdateManifestError(f"Некорректный относительный путь в full-manifest: {raw}")
    if any(":" in part for part in parts):
        raise FullUpdateManifestError(f"Путь с именем диска запрещён в full-manifest: {raw}")
    normalized = "/".join(parts)
    if normalized.casefold() in _SERVICE_FILES:
        raise FullUpdateManifestError(f"Служебный файл не должен входить в inventory: {normalized}")
    return normalized


def release_file_path(root: str | os.PathLike[str], relative_path: str) -> Path:
    base = Path(root).resolve()
    normalized = normalize_release_path(relative_path)
    candidate = (base / Path(*normalized.split("/"))).resolve()
    try:
        common = os.path.commonpath((str(base), str(candidate)))
    except ValueError as exc:
        raise FullUpdateManifestError(f"Путь выходит за границы full-пакета: {relative_path}") from exc
    if os.path.normcase(common) != os.path.normcase(str(base)):
        raise FullUpdateManifestError(f"Путь выходит за границы full-пакета: {relative_path}")
    return candidate


def compute_sha256(path: str | os.PathLike[str], *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _iter_payload_paths(root: str | os.PathLike[str]) -> Iterable[tuple[str, Path]]:
    base = Path(root).resolve()
    if not base.is_dir():
        raise FullUpdateManifestError(f"Папка full-пакета не найдена: {base}")
    for current_dir, dir_names, file_names in os.walk(base):
        dir_names.sort(key=str.casefold)
        file_names.sort(key=str.casefold)
        current = Path(current_dir)
        for file_name in file_names:
            path = current / file_name
            relative = path.relative_to(base).as_posix()
            if relative.casefold() in _SERVICE_FILES:
                continue
            if path.is_symlink():
                raise FullUpdateManifestError(f"Символические ссылки запрещены в full-пакете: {relative}")
            yield normalize_release_path(relative), path


def build_file_inventory(root: str | os.PathLike[str]) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for relative, path in _iter_payload_paths(root):
        inventory.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": compute_sha256(path),
            }
        )
    inventory.sort(key=lambda item: str(item["path"]).casefold())
    return inventory


def normalize_file_inventory(value: Any, *, required: bool = False) -> list[dict[str, Any]]:
    if value is None and not required:
        return []
    if not isinstance(value, list) or (required and not value):
        raise FullUpdateManifestError("Full-manifest должен содержать непустой список files.")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_entry in value:
        if not isinstance(raw_entry, dict):
            raise FullUpdateManifestError("Каждый элемент files должен быть JSON-объектом.")
        relative = normalize_release_path(raw_entry.get("path"))
        key = relative.casefold()
        if key in seen:
            raise FullUpdateManifestError(f"Повторяющийся путь в full-manifest: {relative}")
        seen.add(key)
        try:
            size = int(raw_entry.get("size"))
        except (TypeError, ValueError) as exc:
            raise FullUpdateManifestError(f"Некорректный размер файла: {relative}") from exc
        if size < 0:
            raise FullUpdateManifestError(f"Отрицательный размер файла: {relative}")
        sha256 = str(raw_entry.get("sha256") or "").strip().lower()
        if not _SHA256_RE.fullmatch(sha256):
            raise FullUpdateManifestError(f"Некорректный SHA-256 файла: {relative}")
        normalized.append({"path": relative, "size": size, "sha256": sha256})
    normalized.sort(key=lambda item: str(item["path"]).casefold())
    return normalized


def verify_file_inventory(
    root: str | os.PathLike[str],
    inventory: Any,
    *,
    reject_extra: bool = True,
) -> list[dict[str, Any]]:
    normalized = normalize_file_inventory(inventory, required=True)
    expected = {str(item["path"]).casefold(): item for item in normalized}

    if reject_extra:
        actual = {relative.casefold() for relative, _path in _iter_payload_paths(root)}
        missing = sorted(set(expected) - actual)
        extra = sorted(actual - set(expected))
        if missing:
            raise FullUpdateManifestError(
                "В full-пакете отсутствуют файлы из manifest: " + ", ".join(missing[:10])
            )
        if extra:
            raise FullUpdateManifestError(
                "В full-пакете есть файлы вне manifest: " + ", ".join(extra[:10])
            )

    for entry in normalized:
        path = release_file_path(root, str(entry["path"]))
        if not path.is_file():
            raise FullUpdateManifestError(f"Файл full-пакета не найден: {entry['path']}")
        actual_size = path.stat().st_size
        if actual_size != int(entry["size"]):
            raise FullUpdateManifestError(
                f"Размер файла full-пакета не совпадает: {entry['path']} "
                f"(получено {actual_size}, ожидалось {entry['size']})"
            )
        actual_sha256 = compute_sha256(path)
        if actual_sha256 != str(entry["sha256"]):
            raise FullUpdateManifestError(f"SHA-256 файла full-пакета не совпадает: {entry['path']}")
    return normalized

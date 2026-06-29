import hashlib
import json
import ntpath
import os
import posixpath
import re
from pathlib import Path
from typing import Any, Iterable


APP_ID = "rem_card"
MANIFEST_FILE_NAME = "manifest.json"
PACKAGE_TYPE_FULL = "full"
PACKAGE_TYPE_PATCH = "patch"
PATCH_PAYLOAD_DIR_NAME = "payload"
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[+-][0-9A-Za-z.-]+)?$")


class UpdatePackageError(ValueError):
    pass


def load_manifest(path: str | os.PathLike[str]) -> dict[str, Any]:
    manifest_path = Path(path)
    if manifest_path.is_dir():
        manifest_path = manifest_path / MANIFEST_FILE_NAME
    with open(manifest_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise UpdatePackageError("manifest.json должен быть JSON-объектом.")
    return payload


def get_package_type(manifest: dict[str, Any]) -> str:
    package_type = str(manifest.get("package_type") or PACKAGE_TYPE_FULL).strip().lower()
    return package_type or PACKAGE_TYPE_FULL


def validate_relative_payload_path(path: Any) -> str:
    raw = str(path or "").strip()
    if not raw:
        raise UpdatePackageError("Путь payload не должен быть пустым.")
    if "\x00" in raw:
        raise UpdatePackageError(f"Путь payload содержит NUL byte: {raw!r}")
    if raw.startswith(("\\\\", "//")):
        raise UpdatePackageError(f"UNC-путь запрещен в patch manifest: {raw!r}")
    drive, _tail = ntpath.splitdrive(raw)
    if drive:
        raise UpdatePackageError(f"Drive letter запрещен в patch manifest: {raw!r}")

    normalized = raw.replace("\\", "/")
    if normalized.startswith("/"):
        raise UpdatePackageError(f"Absolute path запрещен в patch manifest: {raw!r}")
    raw_parts = normalized.split("/")
    if any(part == ".." for part in raw_parts):
        raise UpdatePackageError(f"Path traversal запрещен в patch manifest: {raw!r}")
    normalized = posixpath.normpath(normalized)
    if normalized in ("", "."):
        raise UpdatePackageError(f"Путь payload не указывает файл: {raw!r}")

    parts = normalized.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise UpdatePackageError(f"Path traversal запрещен в patch manifest: {raw!r}")
    return "/".join(parts)


def safe_join_install_root(root: str | os.PathLike[str], relative_path: Any) -> str:
    normalized = validate_relative_payload_path(relative_path)
    root_abs = os.path.abspath(os.fspath(root))
    candidate = os.path.abspath(os.path.join(root_abs, *normalized.split("/")))
    try:
        common = os.path.commonpath([root_abs, candidate])
    except ValueError as exc:
        raise UpdatePackageError(f"Путь выходит за пределы install root: {relative_path!r}") from exc
    if os.path.normcase(common) != os.path.normcase(root_abs):
        raise UpdatePackageError(f"Путь выходит за пределы install root: {relative_path!r}")
    return candidate


def compute_sha256(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def patch_payload_path(package_root: str | os.PathLike[str], relative_path: Any) -> str:
    normalized = validate_relative_payload_path(relative_path)
    return os.path.join(os.path.abspath(os.fspath(package_root)), PATCH_PAYLOAD_DIR_NAME, *normalized.split("/"))


def _validate_version_field(manifest: dict[str, Any], field: str) -> str:
    value = str(manifest.get(field) or "").strip()
    if not VERSION_RE.match(value):
        raise UpdatePackageError(f"Некорректное поле {field} в patch manifest: {value!r}")
    return value


def _validate_sha(value: Any, *, field: str, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    text = str(value or "").strip().lower()
    if not SHA256_RE.match(text):
        raise UpdatePackageError(f"Некорректное поле {field} в patch manifest.")
    return text


def _entry_path(entry: Any, *, section: str) -> str:
    if not isinstance(entry, dict):
        raise UpdatePackageError(f"Элемент {section} должен быть JSON-объектом.")
    return validate_relative_payload_path(entry.get("path"))


def validate_no_path_overlap(files: Iterable[dict[str, Any]], delete: Iterable[dict[str, Any]]) -> None:
    file_paths: set[str] = set()
    delete_paths: set[str] = set()
    for entry in files:
        path = _entry_path(entry, section="files")
        if path in file_paths:
            raise UpdatePackageError(f"Дублирующийся файл patch payload: {path}")
        file_paths.add(path)
    for entry in delete:
        path = _entry_path(entry, section="delete")
        if path in delete_paths:
            raise UpdatePackageError(f"Дублирующийся файл patch delete: {path}")
        delete_paths.add(path)
    overlap = sorted(file_paths & delete_paths)
    if overlap:
        raise UpdatePackageError("files/delete пересекаются: " + ", ".join(overlap[:5]))


def validate_patch_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise UpdatePackageError("patch manifest должен быть JSON-объектом.")
    if str(manifest.get("app") or APP_ID) != APP_ID:
        raise UpdatePackageError("Patch package предназначен для другого приложения.")
    if get_package_type(manifest) != PACKAGE_TYPE_PATCH:
        raise UpdatePackageError("manifest не является patch package.")

    _validate_version_field(manifest, "version")
    _validate_version_field(manifest, "base_version")
    for field in ("base_commit", "source_commit", "patch_commit"):
        if not str(manifest.get(field) or "").strip():
            raise UpdatePackageError(f"В patch manifest отсутствует {field}.")

    files = manifest.get("files")
    delete = manifest.get("delete", [])
    if not isinstance(files, list):
        raise UpdatePackageError("Поле files в patch manifest должно быть списком.")
    if not isinstance(delete, list):
        raise UpdatePackageError("Поле delete в patch manifest должно быть списком.")
    validate_no_path_overlap(files, delete)

    normalized_files = []
    for entry in files:
        path = _entry_path(entry, section="files")
        try:
            size = int(entry.get("size"))
        except Exception as exc:
            raise UpdatePackageError(f"Некорректный size для {path}.") from exc
        if size < 0:
            raise UpdatePackageError(f"Некорректный size для {path}.")
        sha256 = _validate_sha(entry.get("sha256"), field=f"files[{path}].sha256")
        if "old_sha256" not in entry:
            raise UpdatePackageError(f"Для {path} отсутствует old_sha256.")
        old_sha256 = _validate_sha(
            entry.get("old_sha256"),
            field=f"files[{path}].old_sha256",
            allow_none=True,
        )
        normalized_files.append({**entry, "path": path, "size": size, "sha256": sha256, "old_sha256": old_sha256})

    normalized_delete = []
    for entry in delete:
        path = _entry_path(entry, section="delete")
        if "old_sha256" not in entry or entry.get("old_sha256") is None:
            raise UpdatePackageError(f"Для удаления {path} отсутствует old_sha256.")
        old_sha256 = _validate_sha(entry.get("old_sha256"), field=f"delete[{path}].old_sha256")
        normalized_delete.append({**entry, "path": path, "old_sha256": old_sha256})

    return {**manifest, "files": normalized_files, "delete": normalized_delete}

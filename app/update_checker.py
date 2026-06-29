import ctypes
import hashlib
import json
import ntpath
import os
import re
import socket
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from rem_card.app.runtime_paths import resolve_baza_dir
from rem_card.app.update_package import (
    PACKAGE_TYPE_FULL,
    PACKAGE_TYPE_PATCH,
    UpdatePackageError,
    compute_sha256,
    get_package_type,
    patch_payload_path,
    validate_patch_manifest,
)
from rem_card.app.version import APP_VERSION


UPDATE_DIR_NAME = "UPD"
RELEASES_DIR_NAME = "releases"
READY_FILE_NAME = "ready.ok"
MANIFEST_FILE_NAME = "manifest.json"
DEFAULT_PROG_DIR_NAME = "."
APP_ID = "rem_card"
UPDATE_LOCK_FILE_NAME = "remcard_update.lock"
UPDATE_STARTING_LOCK_FILE_NAME = "remcard_update_starting.lock"
REQUIRED_RELEASE_EXES = (
    "RemCardDoctor.exe",
    "RemCardNurse.exe",
    "RemCardOperBlockEmergency.exe",
    "RemCardOperBlockPlanned.exe",
    "RemCardPathSetup.exe",
    "RemCardUpdater.exe",
)


def _normalize_target_path(path: str) -> str:
    raw = str(path or "").strip().strip('"')
    if _is_network_target_path(raw):
        return ntpath.normcase(ntpath.normpath(raw))
    return os.path.normcase(os.path.abspath(os.path.normpath(raw)))


def _normalize_host(host: Optional[str] = None) -> str:
    value = str(host or socket.gethostname() or "").strip().lower()
    if not value:
        return "unknown"
    return value.split(".", 1)[0]


def _windows_drive_root(path: str) -> str:
    drive, _tail = ntpath.splitdrive(str(path or ""))
    if not drive:
        return ""
    return drive + "\\"


def _is_network_target_path(path: str) -> bool:
    text = str(path or "").strip()
    drive, _tail = ntpath.splitdrive(text)
    if drive.startswith("\\\\"):
        return True
    if os.name != "nt" or not drive:
        return False

    try:
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(_windows_drive_root(text))
        return int(drive_type) == 4  # DRIVE_REMOTE
    except Exception:
        return False


def update_lock_scope_key(target_dir: str, *, host: Optional[str] = None) -> str:
    target = _normalize_target_path(target_dir)
    if _is_network_target_path(target):
        return f"network:{target}"
    return f"local:{_normalize_host(host)}:{target}"


def update_lock_scope_id(target_dir: str, *, host: Optional[str] = None) -> str:
    raw = update_lock_scope_key(target_dir, host=host).encode("utf-8", "surrogatepass")
    return hashlib.sha256(raw).hexdigest()[:16]


def update_lock_payload_matches_target(
    payload: dict[str, Any],
    target_dir: str,
    *,
    current_host: Optional[str] = None,
) -> bool:
    payload_target = str(payload.get("target") or "").strip()
    if not payload_target:
        return True

    payload_host = str(payload.get("launcher_host") or payload.get("host") or "").strip()
    if not payload_host and not _is_network_target_path(payload_target) and not _is_network_target_path(target_dir):
        return _normalize_target_path(payload_target) == _normalize_target_path(target_dir)

    try:
        payload_key = update_lock_scope_key(payload_target, host=payload_host)
        current_key = update_lock_scope_key(target_dir, host=current_host)
        return payload_key == current_key
    except Exception:
        return True


def _scoped_lock_file_name(file_name: str, target_dir: Optional[str], host: Optional[str]) -> str:
    if not target_dir:
        return file_name
    stem, ext = os.path.splitext(file_name)
    return f"{stem}_{update_lock_scope_id(target_dir, host=host)}{ext}"


@dataclass(frozen=True)
class UpdateCandidate:
    version: str
    release_dir: str
    prog_dir: str
    manifest_path: str
    manifest: dict[str, Any]
    package_type: str = PACKAGE_TYPE_FULL


@dataclass(frozen=True)
class UpdateScanResult:
    candidates: list[UpdateCandidate]
    reasons: list[str]


def _version_tuple(value: str) -> tuple[int, ...]:
    text = str(value or "").strip().split("+", 1)[0].split("-", 1)[0]
    parts: list[int] = []
    for part in text.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        if digits:
            parts.append(int(digits))
    return tuple(parts or [0])


def compare_versions(left: str, right: str) -> int:
    left_tuple = _version_tuple(left)
    right_tuple = _version_tuple(right)
    size = max(len(left_tuple), len(right_tuple))
    left_tuple = left_tuple + (0,) * (size - len(left_tuple))
    right_tuple = right_tuple + (0,) * (size - len(right_tuple))

    if left_tuple < right_tuple:
        return -1
    if left_tuple > right_tuple:
        return 1
    return 0


def is_valid_version(value: str) -> bool:
    return bool(re.match(r"^\d+\.\d+\.\d+(?:[+-][0-9A-Za-z.-]+)?$", str(value or "").strip()))


def get_update_root(baza_dir: Optional[str] = None) -> str:
    root = baza_dir or resolve_baza_dir()
    return os.path.join(os.path.abspath(root), UPDATE_DIR_NAME)


def get_legacy_update_root(baza_dir: Optional[str] = None) -> str:
    root = baza_dir or resolve_baza_dir()
    return os.path.join(os.path.dirname(os.path.abspath(root)), UPDATE_DIR_NAME)


def get_update_lock_path(
    baza_dir: Optional[str] = None,
    *,
    target_dir: Optional[str] = None,
    host: Optional[str] = None,
) -> str:
    root = baza_dir or resolve_baza_dir()
    file_name = _scoped_lock_file_name(UPDATE_LOCK_FILE_NAME, target_dir, host)
    return os.path.join(os.path.abspath(root), "locks", file_name)


def get_update_starting_lock_path(
    baza_dir: Optional[str] = None,
    *,
    target_dir: Optional[str] = None,
    host: Optional[str] = None,
) -> str:
    root = baza_dir or resolve_baza_dir()
    file_name = _scoped_lock_file_name(UPDATE_STARTING_LOCK_FILE_NAME, target_dir, host)
    return os.path.join(os.path.abspath(root), "locks", file_name)


def _release_dirs(update_root: str) -> Iterable[str]:
    direct_manifest = os.path.join(update_root, MANIFEST_FILE_NAME)
    if os.path.isfile(direct_manifest):
        yield update_root

    releases_dir = os.path.join(update_root, RELEASES_DIR_NAME)
    if not os.path.isdir(releases_dir):
        return

    for name in sorted(os.listdir(releases_dir)):
        path = os.path.join(releases_dir, name)
        if os.path.isdir(path):
            yield path


def _load_candidate(release_dir: str) -> Optional[UpdateCandidate]:
    ready_path = os.path.join(release_dir, READY_FILE_NAME)
    manifest_path = os.path.join(release_dir, MANIFEST_FILE_NAME)
    if not os.path.isfile(ready_path) or not os.path.isfile(manifest_path):
        return None

    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except Exception:
        return None

    if not isinstance(manifest, dict):
        return None
    if str(manifest.get("app") or APP_ID) != APP_ID:
        return None
    package_type = get_package_type(manifest)

    version = str(manifest.get("version") or "").strip()
    if not is_valid_version(version):
        return None

    if package_type == PACKAGE_TYPE_PATCH:
        return _load_patch_candidate(release_dir, manifest_path, manifest, version)
    if package_type != PACKAGE_TYPE_FULL:
        return None

    prog_dir_name = str(manifest.get("prog_dir") or DEFAULT_PROG_DIR_NAME).strip() or DEFAULT_PROG_DIR_NAME
    prog_dir = os.path.abspath(os.path.join(release_dir, prog_dir_name))
    if not os.path.isdir(prog_dir):
        return None

    for exe_name in REQUIRED_RELEASE_EXES:
        if not os.path.isfile(os.path.join(prog_dir, exe_name)):
            return None

    return UpdateCandidate(
        version=version,
        release_dir=os.path.abspath(release_dir),
        prog_dir=prog_dir,
        manifest_path=os.path.abspath(manifest_path),
        manifest=manifest,
        package_type=PACKAGE_TYPE_FULL,
    )


def _load_patch_candidate(
    release_dir: str,
    manifest_path: str,
    manifest: dict[str, Any],
    version: str,
) -> Optional[UpdateCandidate]:
    try:
        normalized = validate_patch_manifest(manifest)
        for entry in normalized["files"]:
            path = patch_payload_path(release_dir, entry["path"])
            if not os.path.isfile(path):
                return None
            try:
                if os.path.getsize(path) != int(entry["size"]):
                    return None
            except Exception:
                return None
            if compute_sha256(path) != str(entry["sha256"]).lower():
                return None
    except (OSError, UpdatePackageError):
        return None

    return UpdateCandidate(
        version=version,
        release_dir=os.path.abspath(release_dir),
        prog_dir=os.path.abspath(release_dir),
        manifest_path=os.path.abspath(manifest_path),
        manifest=normalized,
        package_type=PACKAGE_TYPE_PATCH,
    )


def _find_available_updates_with_reasons(
    *,
    current_version: str = APP_VERSION,
    update_root: Optional[str] = None,
) -> UpdateScanResult:
    candidates: list[UpdateCandidate] = []
    reasons: list[str] = []
    roots = [os.path.abspath(update_root)] if update_root else _default_update_roots()
    for root in roots:
        if not os.path.isdir(root):
            continue

        for release_dir in _release_dirs(root):
            candidate = _load_candidate(release_dir)
            if not candidate:
                continue
            if compare_versions(candidate.version, current_version) <= 0:
                continue
            if candidate.package_type == PACKAGE_TYPE_PATCH:
                base_version = str(candidate.manifest.get("base_version") or "").strip()
                if base_version != current_version:
                    reasons.append(
                        f"Патч предназначен для версии {base_version}, "
                        f"установлена {current_version}. Нужен полный релиз."
                    )
                    continue
            candidates.append(candidate)

    return UpdateScanResult(
        candidates=sorted(candidates, key=lambda item: _version_tuple(item.version), reverse=True),
        reasons=reasons,
    )


def find_available_updates(
    *,
    current_version: str = APP_VERSION,
    update_root: Optional[str] = None,
) -> list[UpdateCandidate]:
    return _find_available_updates_with_reasons(
        current_version=current_version,
        update_root=update_root,
    ).candidates


def find_best_update(
    *,
    current_version: str = APP_VERSION,
    update_root: Optional[str] = None,
) -> Optional[UpdateCandidate]:
    updates = find_available_updates(current_version=current_version, update_root=update_root)
    return updates[0] if updates else None


def find_best_update_with_reason(
    *,
    current_version: str = APP_VERSION,
    update_root: Optional[str] = None,
) -> tuple[Optional[UpdateCandidate], str]:
    result = _find_available_updates_with_reasons(
        current_version=current_version,
        update_root=update_root,
    )
    if result.candidates:
        return result.candidates[0], ""
    return None, result.reasons[0] if result.reasons else ""


def _default_update_roots() -> list[str]:
    roots = [get_update_root(), get_legacy_update_root()]
    result: list[str] = []
    seen: set[str] = set()
    for root in roots:
        normalized = os.path.normcase(os.path.abspath(root))
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(os.path.abspath(root))
    return result

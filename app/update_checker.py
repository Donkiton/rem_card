import json
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from rem_card.app.runtime_paths import resolve_baza_dir
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
    "RemCardPathSetup.exe",
    "RemCardUpdater.exe",
)


@dataclass(frozen=True)
class UpdateCandidate:
    version: str
    release_dir: str
    prog_dir: str
    manifest_path: str
    manifest: dict[str, Any]


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


def get_update_lock_path(baza_dir: Optional[str] = None) -> str:
    root = baza_dir or resolve_baza_dir()
    return os.path.join(os.path.abspath(root), "locks", UPDATE_LOCK_FILE_NAME)


def get_update_starting_lock_path(baza_dir: Optional[str] = None) -> str:
    root = baza_dir or resolve_baza_dir()
    return os.path.join(os.path.abspath(root), "locks", UPDATE_STARTING_LOCK_FILE_NAME)


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

    version = str(manifest.get("version") or "").strip()
    if not is_valid_version(version):
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
    )


def find_available_updates(
    *,
    current_version: str = APP_VERSION,
    update_root: Optional[str] = None,
) -> list[UpdateCandidate]:
    candidates: list[UpdateCandidate] = []
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
            candidates.append(candidate)

    return sorted(candidates, key=lambda item: _version_tuple(item.version), reverse=True)


def find_best_update(
    *,
    current_version: str = APP_VERSION,
    update_root: Optional[str] = None,
) -> Optional[UpdateCandidate]:
    updates = find_available_updates(current_version=current_version, update_root=update_root)
    return updates[0] if updates else None


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

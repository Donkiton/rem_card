import json
import os
import sys
from pathlib import Path
from typing import Any


APP_NAME = "РЕМКАРТА"
DEFAULT_APP_VERSION = "3.0.0"


def _clean_text(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def _unique_paths(paths):
    seen: set[str] = set()
    for path in paths:
        try:
            resolved = Path(path).resolve()
        except Exception:
            resolved = Path(path)
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        yield resolved


def _candidate_version_paths():
    explicit = _clean_text(os.environ.get("REMCARD_VERSION_FILE"))
    paths: list[Path] = []
    if explicit:
        paths.append(Path(explicit))

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        root = Path(meipass)
        paths.extend([root / "rem_card" / "VERSION", root / "VERSION"])

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        paths.extend(
            [
                exe_dir / "_internal" / "rem_card" / "VERSION",
                exe_dir / "rem_card" / "VERSION",
                exe_dir / "VERSION",
            ]
        )

    package_root = Path(__file__).resolve().parents[1]
    paths.extend([package_root / "VERSION", package_root.parent / "VERSION"])
    return _unique_paths(paths)


def _candidate_release_info_paths():
    paths: list[Path] = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        root = Path(meipass)
        paths.extend([root / "rem_card" / "app" / "release_info.json", root / "app" / "release_info.json"])

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        paths.extend(
            [
                exe_dir / "_internal" / "rem_card" / "app" / "release_info.json",
                exe_dir / "rem_card" / "app" / "release_info.json",
                exe_dir / "app" / "release_info.json",
            ]
        )

    paths.append(Path(__file__).resolve().with_name("release_info.json"))
    return _unique_paths(paths)


def get_release_info() -> dict[str, Any]:
    for path in _candidate_release_info_paths():
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if isinstance(payload, dict):
                return payload
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return {"schema_version": 1, "version": DEFAULT_APP_VERSION, "date": "", "changes": []}


def get_app_version() -> str:
    override = _clean_text(os.environ.get("REMCARD_APP_VERSION"))
    if override:
        return override

    for path in _candidate_version_paths():
        try:
            with open(path, "r", encoding="utf-8") as fh:
                version = _clean_text(fh.readline())
            if version:
                return version
        except FileNotFoundError:
            continue
        except Exception:
            continue

    release_info_version = _clean_text(str(get_release_info().get("version") or ""))
    return release_info_version or DEFAULT_APP_VERSION


APP_VERSION = get_app_version()
APP_DISPLAY_TITLE = f"{APP_NAME} v{APP_VERSION}"

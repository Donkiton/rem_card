import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from app.full_update_manifest import (
    FULL_MANIFEST_SCHEMA_VERSION,
    FullUpdateManifestError,
    build_file_inventory,
    normalize_file_inventory,
    verify_file_inventory,
)
from app.runtime_paths import DEFAULT_DEV_DATA_ROOT_NAME, get_dev_baza_dir
from bump_version import (
    find_changelog_entry,
    parse_version,
    read_version,
)


CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
MANIFEST_FILE_NAME = "manifest.json"
READY_FILE_NAME = "ready.ok"
TEST_WORKTREE_MARKER_NAME = "TEST_WORKTREE_ONLY.txt"
PROGRESS_JSON_PREFIX = "REMCARD_PROGRESS_JSON:"
PROGRESS_JSON_SCHEMA_VERSION = 1
RELEASES_DIR_NAME = "releases"
REQUIRED_RELEASE_EXES = (
    "RemCardDoctor.exe",
    "RemCardNurse.exe",
    "RemCardOperBlockEmergency.exe",
    "RemCardOperBlockPlanned.exe",
    "RemCardPathSetup.exe",
    "RemCardUpdater.exe",
)
SETTINGS_RELEASE_DIR = Path("_internal") / "rem_card" / "settings_release"
SETTINGS_RELEASE_SNAPSHOT_FILE = "settings_release_snapshot.json"
SETTINGS_RELEASE_MANIFEST_FILE = "settings_release_manifest.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
COMPILED_SMOKE_TIMEOUT_SECONDS = 30
BUILD_ARTIFACT_DIR_NAMES = ("build", "dist")
BUILD_ARTIFACT_CLEANUP_DELAYS = (0.0, 0.25, 0.75)


def _progress_mode(args: argparse.Namespace) -> str:
    if bool(getattr(args, "test_worktree", False)):
        return "test_worktree"
    return "release"


def _current_progress(args: argparse.Namespace) -> int:
    try:
        value = int(getattr(args, "_last_progress", 0))
    except (TypeError, ValueError):
        value = 0
    return max(0, min(99, value))


def emit_progress(
    args: argparse.Namespace,
    *,
    stage: str,
    status: str,
    progress: int,
    message: str,
    path: Path | None = None,
) -> None:
    """Emit one machine-readable UTF-8 JSON line without hiding normal logs."""
    previous_progress = _current_progress(args)
    requested_progress = max(0, min(100, int(progress)))
    event_progress = (
        previous_progress
        if status == "failed"
        else max(previous_progress, requested_progress)
    )
    setattr(args, "_last_progress", event_progress)
    if not bool(getattr(args, "progress_json", False)):
        return
    payload: dict[str, object] = {
        "schema_version": PROGRESS_JSON_SCHEMA_VERSION,
        "mode": _progress_mode(args),
        "stage": str(stage),
        "status": str(status),
        "progress": event_progress,
        "message": str(message),
    }
    if path is not None:
        payload["path"] = str(path.resolve())
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    line = f"{PROGRESS_JSON_PREFIX}{serialized}\n"
    binary_stream = getattr(sys.stdout, "buffer", None)
    if binary_stream is not None:
        sys.stdout.flush()
        binary_stream.write(line.encode("utf-8"))
        binary_stream.flush()
    else:
        print(line, end="", flush=True)

def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def run(
    args: list[str],
    *,
    cwd: Path,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd),
        check=check,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def git_output(root: Path, args: list[str]) -> str:
    result = run(["git", *args], cwd=root, capture=True)
    return str(result.stdout or "").strip()


def ensure_git_repo(root: Path) -> None:
    try:
        inside = git_output(root, ["rev-parse", "--is-inside-work-tree"])
    except Exception as exc:
        raise RuntimeError("Команда должна запускаться внутри git-репозитория.") from exc
    if inside.lower() != "true":
        raise RuntimeError("Команда должна запускаться внутри git-репозитория.")


def ensure_clean_tree(root: Path) -> None:
    status = git_output(root, ["status", "--porcelain"])
    if status:
        raise RuntimeError(
            "Рабочее дерево не чистое. Сначала закоммитьте изменения, затем запускайте релизную сборку.\n\n"
            + status
        )


def head_commit(root: Path) -> str:
    commit = git_output(root, ["rev-parse", "HEAD"])
    if not commit:
        raise RuntimeError("Не удалось определить текущий git-коммит.")
    return commit


def ensure_russian_changelog(changes: list[str]) -> None:
    non_russian = []
    for item in changes:
        text = str(item or "").strip().lstrip("-").strip()
        if text and not CYRILLIC_RE.search(text):
            non_russian.append(text)

    if not non_russian:
        return

    examples = "\n".join(f"  - {item}" for item in non_russian[:10])
    hidden_count = len(non_russian) - 10
    suffix = f"\n  ...и еще {hidden_count}" if hidden_count > 0 else ""
    raise RuntimeError(
        "Релизный changelog должен быть на русском языке. "
        "Найдены пункты без кириллицы:\n"
        f"{examples}{suffix}\n\n"
        "Исправьте текущую запись CHANGELOG.md до отправки версии в GitHub."
    )


def _remove_build_artifact(path: Path) -> None:
    last_error: OSError | None = None
    for delay in BUILD_ARTIFACT_CLEANUP_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
            elif path.exists():
                shutil.rmtree(path)
            return
        except OSError as exc:
            last_error = exc
    raise RuntimeError(f"Не удалось удалить временный артефакт сборки {path}: {last_error}")


def cleanup_build_artifacts(root: Path, *, remove_dist: bool = True) -> None:
    """Remove generated PyInstaller state without touching published releases."""
    names = BUILD_ARTIFACT_DIR_NAMES if remove_dist else ("build",)
    removed: list[str] = []
    for name in names:
        path = root / name
        if not path.exists() and not path.is_symlink():
            continue
        _remove_build_artifact(path)
        removed.append(name)
    if removed:
        print(f"Удалены временные артефакты сборки: {', '.join(removed)}.")


def run_build(root: Path) -> Path:
    if os.environ.get("REMCARD_SKIP_SETTINGS_RELEASE_EXPORT") == "1":
        raise RuntimeError(
            "REMCARD_SKIP_SETTINGS_RELEASE_EXPORT=1 запрещён для release-сборки: "
            "production-пакет обязан содержать snapshot настроек."
        )
    cleanup_build_artifacts(root)
    os.environ.setdefault("REMCARD_SETTINGS_RELEASE_SOURCE_BAZA", get_dev_baza_dir())
    package_dir = root / "dist" / "Prog"
    try:
        run([sys.executable, "-m", "PyInstaller", "RemCard.spec"], cwd=root)
        if not package_dir.is_dir():
            raise RuntimeError(f"PyInstaller не создал пакет: {package_dir}")
    except BaseException:
        try:
            cleanup_build_artifacts(root)
        except RuntimeError as cleanup_exc:
            print(f"Предупреждение: {cleanup_exc}", file=sys.stderr)
        raise
    return package_dir


def run_release_checks(root: Path) -> None:
    """Run mandatory gates for the exact immutable source commit."""
    checks = (
        (
            "architecture safety",
            [sys.executable, str(root / "scripts" / "architecture_safety_check.py")],
        ),
        (
            "fast regression",
            [
                sys.executable,
                str(root / "scripts" / "regression_safety_checks.py"),
                "--profile",
                "fast",
                "--quiet-progress",
                "--json-detail",
                "summary",
            ],
        ),
        (
            "flake8 F821",
            [
                sys.executable,
                "-m",
                "flake8",
                ".",
                "--select=F821",
                "--exclude=.git,__pycache__,build,dist,tmp,.venv,venv,.pytest_cache,.mypy_cache,.ruff_cache",
            ],
        ),
    )
    for name, command in checks:
        print(f"Обязательная release-проверка: {name}...")
        result = run(command, cwd=root, check=False, capture=True)
        if result.returncode != 0:
            details = "\n".join(
                value.strip()
                for value in (str(result.stdout or ""), str(result.stderr or ""))
                if value and value.strip()
            )
            suffix = f"\n\n{details}" if details else ""
            raise RuntimeError(
                f"Обязательная release-проверка не пройдена ({name}), код {result.returncode}. "
                f"Сборка и публикация остановлены.{suffix}"
            )
    print("Все обязательные release-проверки пройдены.")


def run_compiled_smoke(package_dir: Path) -> None:
    """Prove that every shipped role starts its frozen entrypoint successfully."""
    for exe_name in REQUIRED_RELEASE_EXES:
        executable = package_dir / exe_name
        print(f"Smoke-тест собранного EXE: {exe_name}...")
        try:
            result = subprocess.run(
                [str(executable), "--compiled-smoke"],
                cwd=str(package_dir),
                check=False,
                timeout=COMPILED_SMOKE_TIMEOUT_SECONDS,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Smoke-тест {exe_name} не завершился за "
                f"{COMPILED_SMOKE_TIMEOUT_SECONDS} секунд. Сборка остановлена."
            ) from exc
        except OSError as exc:
            raise RuntimeError(f"Не удалось запустить smoke-тест {exe_name}: {exc}") from exc
        if result.returncode != 0:
            stdout = (result.stdout or b"").decode("utf-8", errors="replace").strip()
            stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
            details = "\n".join(value for value in (stdout, stderr) if value)
            suffix = f"\n{details}" if details else ""
            raise RuntimeError(
                f"Smoke-тест {exe_name} завершился с кодом {result.returncode}. "
                f"Сборка остановлена.{suffix}"
            )
    print("Smoke-тест всех шести EXE пройден.")


def _read_json_object(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Не удалось прочитать {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} должен содержать JSON-объект.")
    return payload


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _settings_snapshot_content_hash(snapshot: dict) -> str:
    payload = {
        "schema_version": snapshot.get("schema_version"),
        "tables": snapshot.get("tables") or {},
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _validate_settings_media(value: object, settings_dir: Path) -> None:
    if isinstance(value, list):
        for item in value:
            _validate_settings_media(item, settings_dir)
        return
    if not isinstance(value, dict):
        return

    relative = value.get("__blob_file__")
    if relative is not None:
        normalized = str(relative or "").strip().replace("\\", "/")
        parts = normalized.split("/")
        if not normalized or normalized.startswith("/") or any(part in ("", ".", "..") for part in parts):
            raise RuntimeError(
                "Snapshot настроек содержит небезопасный путь media-файла: "
                f"{relative!r}."
            )
        media_path = settings_dir.joinpath(*parts).resolve()
        settings_root = settings_dir.resolve()
        try:
            media_path.relative_to(settings_root)
        except ValueError as exc:
            raise RuntimeError(
                f"Media-файл snapshot настроек находится вне каталога settings_release: {relative!r}."
            ) from exc
        if not media_path.is_file():
            raise RuntimeError(f"В snapshot настроек отсутствует media-файл: {relative}.")
        expected_hash = str(value.get("sha256") or "").lower()
        if not SHA256_RE.fullmatch(expected_hash):
            raise RuntimeError(f"У media-файла snapshot нет корректного sha256: {relative}.")
        try:
            expected_size = int(value.get("size"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"У media-файла snapshot нет корректного размера: {relative}.") from exc
        if expected_size < 0 or media_path.stat().st_size != expected_size:
            raise RuntimeError(f"Размер media-файла snapshot не совпадает: {relative}.")
        if _sha256_file(media_path) != expected_hash:
            raise RuntimeError(f"SHA-256 media-файла snapshot не совпадает: {relative}.")

    for item in value.values():
        _validate_settings_media(item, settings_dir)


def validate_settings_release_snapshot(
    package_dir: Path,
    *,
    version: str,
    source_commit: str,
    package_manifest: dict,
    require_manifest_entry: bool,
) -> dict:
    settings_dir = package_dir / SETTINGS_RELEASE_DIR
    snapshot_path = settings_dir / SETTINGS_RELEASE_SNAPSHOT_FILE
    settings_manifest_path = settings_dir / SETTINGS_RELEASE_MANIFEST_FILE
    snapshot = _read_json_object(snapshot_path)
    settings_manifest = _read_json_object(settings_manifest_path)

    if snapshot.get("schema_version") != 1:
        raise RuntimeError("Snapshot настроек имеет неподдерживаемую schema_version.")
    expected_snapshot_fields = {
        "release_version": version,
        "release_commit": source_commit,
    }
    mismatches = [
        f"{name}={snapshot.get(name)!r}, ожидалось {expected!r}"
        for name, expected in expected_snapshot_fields.items()
        if snapshot.get(name) != expected
    ]
    if mismatches:
        raise RuntimeError("Snapshot настроек не соответствует релизу: " + "; ".join(mismatches))

    content_hash = str(snapshot.get("content_hash") or "").lower()
    if not SHA256_RE.fullmatch(content_hash):
        raise RuntimeError("Snapshot настроек не содержит корректный content_hash.")
    actual_hash = _settings_snapshot_content_hash(snapshot)
    if content_hash != actual_hash:
        raise RuntimeError(
            "Snapshot настроек повреждён: content_hash не совпадает "
            f"({content_hash} != {actual_hash})."
        )
    _validate_settings_media(snapshot.get("tables") or {}, settings_dir)

    expected_settings_manifest = {
        "schema_version": 1,
        "snapshot_schema_version": 1,
        "snapshot_file": SETTINGS_RELEASE_SNAPSHOT_FILE,
        "content_hash": content_hash,
        "release_version": version,
        "release_commit": source_commit,
        "exported_at": snapshot.get("exported_at"),
        "row_counts": snapshot.get("row_counts") or {},
    }
    settings_mismatches = [
        f"{name}={settings_manifest.get(name)!r}, ожидалось {expected!r}"
        for name, expected in expected_settings_manifest.items()
        if settings_manifest.get(name) != expected
    ]
    if settings_mismatches:
        raise RuntimeError(
            "settings_release_manifest.json не соответствует snapshot: "
            + "; ".join(settings_mismatches)
        )

    metadata = {
        "manifest_schema_version": 1,
        "snapshot_schema_version": 1,
        "snapshot_file": SETTINGS_RELEASE_SNAPSHOT_FILE,
        "content_hash": content_hash,
        "release_version": version,
        "release_commit": source_commit,
    }
    manifest_metadata = package_manifest.get("settings_release")
    if require_manifest_entry and manifest_metadata != metadata:
        raise RuntimeError(
            "Full-manifest не содержит точную метаинформацию settings_release "
            "для этого релиза."
        )
    if manifest_metadata is not None and manifest_metadata != metadata:
        raise RuntimeError("Метаинформация settings_release в full-manifest повреждена.")
    return metadata


def validate_full_package(
    package_dir: Path,
    *,
    version: str,
    source_commit: str,
    allow_ready: bool = False,
    require_inventory: bool = False,
) -> dict:
    if not package_dir.is_dir():
        raise RuntimeError(f"Каталог full-пакета не найден: {package_dir}")

    manifest_path = package_dir / MANIFEST_FILE_NAME
    if not manifest_path.is_file():
        raise RuntimeError(f"В full-пакете нет {MANIFEST_FILE_NAME}: {package_dir}")
    manifest = _read_json_object(manifest_path)

    expected = {
        "app": "rem_card",
        "package_type": "full",
        "version": version,
        "prog_dir": ".",
        "source_commit": source_commit,
    }
    mismatches = [
        f"{name}={manifest.get(name)!r}, ожидалось {value!r}"
        for name, value in expected.items()
        if manifest.get(name) != value
    ]
    if mismatches:
        raise RuntimeError("Неверный manifest full-пакета: " + "; ".join(mismatches))

    validate_settings_release_snapshot(
        package_dir,
        version=version,
        source_commit=source_commit,
        package_manifest=manifest,
        require_manifest_entry=require_inventory,
    )

    schema_version = manifest.get("schema_version")
    if require_inventory and schema_version != FULL_MANIFEST_SCHEMA_VERSION:
        raise RuntimeError(
            f"Неверная схема full-manifest: {schema_version!r}, "
            f"ожидалась {FULL_MANIFEST_SCHEMA_VERSION}."
        )
    try:
        inventory = normalize_file_inventory(
            manifest.get("files"),
            required=require_inventory,
        )
    except FullUpdateManifestError as exc:
        raise RuntimeError(f"Неверный inventory full-пакета: {exc}") from exc
    if require_inventory and not inventory:
        raise RuntimeError("Inventory full-пакета пуст.")
    if require_inventory:
        try:
            verify_file_inventory(package_dir, inventory, reject_extra=True)
        except FullUpdateManifestError as exc:
            raise RuntimeError(f"Full-пакет не прошёл SHA-256 проверку: {exc}") from exc

    missing = [name for name in REQUIRED_RELEASE_EXES if not (package_dir / name).is_file()]
    if missing:
        raise RuntimeError("В full-пакете нет обязательных EXE: " + ", ".join(missing))
    if not (package_dir / "_internal").is_dir():
        raise RuntimeError("В full-пакете нет каталога _internal.")
    if not allow_ready and (package_dir / READY_FILE_NAME).exists():
        raise RuntimeError(
            f"{READY_FILE_NAME} не должен появляться до проверки и публикации пакета."
        )
    return manifest


def local_update_root(root: Path) -> Path:
    override = str(os.environ.get("REMCARD_BUILD_TARGET_DIR") or "").strip()
    if override:
        value = Path(override).expanduser()
        if not value.is_absolute():
            value = root / value
    else:
        value = root.parent / DEFAULT_DEV_DATA_ROOT_NAME / "UPD"
    if _is_network_path(value):
        raise RuntimeError(
            "build_release.py публикует релиз только в локальный UPD. "
            f"Сетевой путь запрещён: {value}"
        )
    return value.resolve()


def _is_network_path(path: Path) -> bool:
    raw = str(path)
    if raw.startswith("\\\\"):
        return True
    if os.name != "nt" or not path.drive:
        return False
    try:
        drive_root = path.drive + "\\"
        return int(ctypes.windll.kernel32.GetDriveTypeW(drive_root)) == 4
    except Exception:
        return False


def _paths_overlap(left: Path, right: Path) -> bool:
    left_resolved = left.resolve()
    right_resolved = right.resolve()
    try:
        common = Path(os.path.commonpath((str(left_resolved), str(right_resolved))))
    except ValueError:
        return False
    common_key = os.path.normcase(str(common))
    return common_key in {
        os.path.normcase(str(left_resolved)),
        os.path.normcase(str(right_resolved)),
    }


def _write_ready_last(path: Path) -> None:
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        with temp_path.open("x", encoding="utf-8") as fh:
            fh.write(datetime.now().astimezone().isoformat(timespec="seconds") + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _write_json_atomic(path: Path, payload: dict) -> None:
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        with temp_path.open("x", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def write_staged_full_manifest(
    staging_dir: Path,
    *,
    version: str,
    source_commit: str,
    file_inventory: list[dict],
) -> dict:
    manifest_path = staging_dir / MANIFEST_FILE_NAME
    manifest = _read_json_object(manifest_path)
    settings_release = validate_settings_release_snapshot(
        staging_dir,
        version=version,
        source_commit=source_commit,
        package_manifest=manifest,
        require_manifest_entry=False,
    )
    manifest.update(
        {
            "schema_version": FULL_MANIFEST_SCHEMA_VERSION,
            "app": "rem_card",
            "package_type": "full",
            "version": version,
            "prog_dir": ".",
            "source_commit": source_commit,
            "settings_release": settings_release,
            "files": normalize_file_inventory(file_inventory, required=True),
        }
    )
    _write_json_atomic(manifest_path, manifest)
    return manifest


def publish_local_release(
    root: Path,
    package_dir: Path,
    *,
    version: str,
    source_commit: str,
) -> Path:
    """Publish only to the local test UPD; network deployment stays manual."""
    test_marker = package_dir / TEST_WORKTREE_MARKER_NAME
    if test_marker.exists():
        raise RuntimeError(
            f"Публикация тестовой сборки запрещена: найден {test_marker}"
        )
    parse_version(version)
    validate_full_package(package_dir, version=version, source_commit=source_commit)

    releases_dir = local_update_root(root) / RELEASES_DIR_NAME
    if _paths_overlap(package_dir, releases_dir):
        raise RuntimeError(
            "Каталог сборки dist\\Prog и локальный UPD\\releases не должны "
            f"совпадать или быть вложены друг в друга: {package_dir} <-> {releases_dir}"
        )
    # Hash the source before copy. The staging verification below then proves
    # end-to-end that copytree transferred exactly those bytes.
    source_inventory = build_file_inventory(package_dir)
    releases_dir.mkdir(parents=True, exist_ok=True)
    final_dir = releases_dir / version
    ready_path = final_dir / READY_FILE_NAME

    if final_dir.exists() and ready_path.is_file():
        existing_manifest = validate_full_package(
            final_dir,
            version=version,
            source_commit=source_commit,
            allow_ready=True,
            require_inventory=True,
        )
        existing_inventory = normalize_file_inventory(
            existing_manifest.get("files"),
            required=True,
        )
        if existing_inventory != source_inventory:
            raise RuntimeError(
                f"Локальный релиз {version} уже существует, но его inventory "
                "отличается от текущей сборки. Автоматическая перезапись запрещена."
            )
        print(f"Локальный релиз уже опубликован и не перезаписан: {final_dir}")
        return final_dir

    if final_dir.exists():
        # A directory without ready.ok cannot be consumed by clients and is a
        # safe-to-replace remainder of an interrupted local publication.
        shutil.rmtree(final_dir)

    staging_dir = releases_dir / f".staging-{version}-{os.getpid()}"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)

    try:
        shutil.copytree(package_dir, staging_dir)
        staging_ready = staging_dir / READY_FILE_NAME
        if staging_ready.exists():
            staging_ready.unlink()
        write_staged_full_manifest(
            staging_dir,
            version=version,
            source_commit=source_commit,
            file_inventory=source_inventory,
        )
        validate_full_package(
            staging_dir,
            version=version,
            source_commit=source_commit,
            require_inventory=True,
        )
        staging_dir.rename(final_dir)
        _write_ready_last(ready_path)
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)

    # staging_dir and final_dir are on the same local volume. rename() preserves
    # the bytes already hash-verified above, so do not hash the whole build a
    # second time after the atomic rename.
    validate_full_package(
        final_dir,
        version=version,
        source_commit=source_commit,
        allow_ready=True,
        require_inventory=False,
    )
    if not ready_path.is_file():
        raise RuntimeError(f"Публикация не завершена: нет {ready_path}")
    return final_dir


def publish_built_release(
    root: Path,
    package_dir: Path,
    *,
    version: str,
    source_commit: str,
) -> Path:
    published_dir = publish_local_release(
        root,
        package_dir,
        version=version,
        source_commit=source_commit,
    )
    print(f"Полный update-пакет готов: {published_dir}")
    print(
        "Для безопасной публикации в сетевой UPD используйте "
        f"scripts\\publish_full_update.py --source \"{published_dir}\" "
        "--config <путь к remcard_data_path.json>."
    )
    return published_dir


def write_test_worktree_marker(package_dir: Path, *, source_commit: str) -> Path:
    """Mark a dirty-worktree package as test-only and never as updater-ready."""
    ready_path = package_dir / READY_FILE_NAME
    if ready_path.exists():
        raise RuntimeError(
            f"Тестовая сборка не может содержать {READY_FILE_NAME}: {ready_path}"
        )
    marker_path = package_dir / TEST_WORKTREE_MARKER_NAME
    marker_path.write_text(
        "ТЕСТОВАЯ СБОРКА — НЕ ДЛЯ ПУБЛИКАЦИИ\n"
        "Пакет собран из текущего рабочего дерева и может содержать "
        "незакоммиченные изменения.\n"
        f"Git HEAD: {source_commit}\n"
        f"Время сборки: {datetime.now().astimezone().isoformat(timespec='seconds')}\n"
        "Не публикуйте этот каталог как production-релиз.\n",
        encoding="utf-8",
    )
    return marker_path


def build_test_worktree(root: Path, args: argparse.Namespace) -> Path:
    """Build and validate the current worktree without changing release state."""
    version = read_version(root)
    source_commit = head_commit(root)
    package_dir: Path | None = None
    build_started = False
    completed = False
    try:
        emit_progress(
            args,
            stage="checks",
            status="started",
            progress=5,
            message="Запуск обязательных проверок.",
        )
        run_release_checks(root)
        emit_progress(
            args,
            stage="checks",
            status="completed",
            progress=25,
            message="Обязательные проверки пройдены.",
        )

        emit_progress(
            args,
            stage="build",
            status="started",
            progress=25,
            message="Запуск PyInstaller.",
        )
        build_started = True
        package_dir = run_build(root)
        emit_progress(
            args,
            stage="build",
            status="completed",
            progress=60,
            message="PyInstaller завершил сборку.",
            path=package_dir,
        )

        emit_progress(
            args,
            stage="validate",
            status="started",
            progress=60,
            message="Проверка полного пакета.",
        )
        validate_full_package(
            package_dir,
            version=version,
            source_commit=source_commit,
        )
        emit_progress(
            args,
            stage="validate",
            status="completed",
            progress=72,
            message="Полный пакет проверен.",
        )

        emit_progress(
            args,
            stage="smoke",
            status="started",
            progress=72,
            message="Запуск smoke-тестов собранных EXE.",
        )
        run_compiled_smoke(package_dir)
        emit_progress(
            args,
            stage="smoke",
            status="completed",
            progress=90,
            message="Smoke-тесты пройдены.",
        )

        emit_progress(
            args,
            stage="marker",
            status="started",
            progress=90,
            message="Маркировка тестового пакета.",
        )
        marker_path = write_test_worktree_marker(
            package_dir,
            source_commit=source_commit,
        )
        if (package_dir / READY_FILE_NAME).exists():
            raise RuntimeError(
                f"Тестовая сборка ошибочно содержит {READY_FILE_NAME}: {package_dir}"
            )
        emit_progress(
            args,
            stage="marker",
            status="completed",
            progress=96,
            message=f"Создан маркер {marker_path.name}.",
            path=marker_path,
        )
        completed = True
    finally:
        if build_started:
            cleanup_progress = 96 if completed else _current_progress(args)
            emit_progress(
                args,
                stage="cleanup",
                status="started",
                progress=cleanup_progress,
                message="Очистка временного каталога build.",
            )
            cleanup_build_artifacts(root, remove_dist=not completed)
            emit_progress(
                args,
                stage="cleanup",
                status="completed",
                progress=99 if completed else cleanup_progress,
                message=(
                    "Временный каталог build удалён; dist\\Prog сохранён."
                    if completed
                    else "Неуспешные артефакты сборки удалены."
                ),
            )

    if package_dir is None:
        raise RuntimeError("PyInstaller не вернул каталог тестовой сборки.")
    exact_path = package_dir.resolve()
    print(f"Тестовая сборка рабочего дерева готова: {exact_path}")
    print(
        f"Пакет помечен файлом {TEST_WORKTREE_MARKER_NAME}; "
        f"в UPD он не опубликован и {READY_FILE_NAME} не создан."
    )
    emit_progress(
        args,
        stage="completed",
        status="completed",
        progress=100,
        message="Тестовая сборка рабочего дерева завершена.",
        path=exact_path,
    )
    return exact_path


def finish_release(root: Path, version: str, args: argparse.Namespace) -> None:
    release_completed = False
    try:
        ensure_clean_tree(root)
        source_commit = head_commit(root)

        # Production-сборка является чистой функцией уже опубликованного исходного
        # коммита: она не меняет release-файлы и не выполняет commit/push.
        emit_progress(
            args,
            stage="checks",
            status="started",
            progress=10,
            message="Запуск обязательных проверок.",
        )
        run_release_checks(root)
        emit_progress(
            args,
            stage="checks",
            status="completed",
            progress=25,
            message="Обязательные проверки пройдены.",
        )
        ensure_clean_tree(root)
        emit_progress(
            args,
            stage="build",
            status="started",
            progress=25,
            message="Запуск PyInstaller.",
        )
        package_dir = run_build(root)
        emit_progress(
            args,
            stage="build",
            status="completed",
            progress=55,
            message="PyInstaller завершил сборку.",
            path=package_dir,
        )
        emit_progress(
            args,
            stage="validate",
            status="started",
            progress=55,
            message="Проверка полного пакета.",
        )
        validate_full_package(package_dir, version=version, source_commit=source_commit)
        emit_progress(
            args,
            stage="validate",
            status="completed",
            progress=65,
            message="Полный пакет проверен.",
        )
        emit_progress(
            args,
            stage="smoke",
            status="started",
            progress=65,
            message="Запуск smoke-тестов собранных EXE.",
        )
        run_compiled_smoke(package_dir)
        emit_progress(
            args,
            stage="smoke",
            status="completed",
            progress=88,
            message="Smoke-тесты пройдены.",
        )
        ensure_clean_tree(root)
        emit_progress(
            args,
            stage="publish",
            status="started",
            progress=88,
            message="Публикация в локальный UPD.",
        )
        published_dir = publish_built_release(
            root,
            package_dir,
            version=version,
            source_commit=source_commit,
        )
        emit_progress(
            args,
            stage="publish",
            status="completed",
            progress=96,
            message="Локальный full-релиз опубликован.",
            path=published_dir,
        )
        release_completed = True
    finally:
        cleanup_progress = 96 if release_completed else _current_progress(args)
        emit_progress(
            args,
            stage="cleanup",
            status="started",
            progress=cleanup_progress,
            message="Очистка временных артефактов сборки.",
        )
        cleanup_build_artifacts(root)
        emit_progress(
            args,
            stage="cleanup",
            status="completed",
            progress=99 if release_completed else cleanup_progress,
            message="Очистка временных артефактов завершена.",
        )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Собирает неизменяемый production-релиз из уже подготовленного git-коммита. "
            "VERSION, changelog и release-info во время сборки не изменяются."
        )
    )
    parser.add_argument(
        "--expected-version",
        help="Ожидаемая версия GitHub в формате MAJOR.MINOR.PATCH.",
    )
    parser.add_argument(
        "--expected-commit",
        help="Ожидаемый 40-символьный git-коммит GitHub.",
    )
    parser.add_argument(
        "--test-worktree",
        action="store_true",
        help=(
            "Собрать текущее рабочее дерево, включая незакоммиченные изменения, "
            "без изменения версии, commit, push и публикации в UPD."
        ),
    )
    parser.add_argument(
        "--progress-json",
        action="store_true",
        help=(
            f"Дополнительно печатать однострочные JSON-события прогресса с префиксом "
            f"{PROGRESS_JSON_PREFIX}"
        ),
    )
    return parser.parse_args(argv)


def validate_cli_args(args: argparse.Namespace) -> None:
    if args.test_worktree:
        conflicts = [
            name
            for name, value in (
                ("--expected-version", args.expected_version),
                ("--expected-commit", args.expected_commit),
            )
            if value
        ]
        if conflicts:
            raise SystemExit(
                "--test-worktree нельзя сочетать со следующими аргументами: "
                + ", ".join(conflicts)
            )
        return
    missing = [
        name
        for name, value in (
            ("--expected-version", args.expected_version),
            ("--expected-commit", args.expected_commit),
        )
        if not value
    ]
    if missing:
        raise SystemExit(
            "Production-сборка требует точный GitHub-источник: "
            + ", ".join(missing)
        )


def validate_release_source_identity(
    root: Path,
    *,
    expected_version: str | None,
    expected_commit: str | None,
) -> tuple[str, str]:
    version = read_version(root)
    commit = head_commit(root)
    if expected_version:
        parse_version(expected_version)
        if version != expected_version:
            raise RuntimeError(
                f"VERSION выбранного коммита равен {version}, "
                f"но менеджер ожидает GitHub-версию {expected_version}."
            )
    if expected_commit:
        normalized_commit = expected_commit.strip().lower()
        if not COMMIT_RE.fullmatch(normalized_commit):
            raise RuntimeError("Ожидаемый GitHub-коммит должен содержать 40 hex-символов.")
        if commit != normalized_commit:
            raise RuntimeError(
                f"Локальный HEAD {commit[:12]} не совпадает с выбранным "
                f"GitHub-коммитом {normalized_commit[:12]}."
            )

    date_text, changes = find_changelog_entry(root, version)
    ensure_russian_changelog(changes)
    release_info = _read_json_object(root / "app" / "release_info.json")
    if str(release_info.get("version") or "").strip() != version:
        raise RuntimeError(
            "app/release_info.json не соответствует VERSION. "
            "Подготовьте версию до отправки изменений в GitHub."
        )
    if str(release_info.get("date") or "").strip() != date_text:
        raise RuntimeError(
            "Дата app/release_info.json не соответствует записи текущей версии в CHANGELOG.md."
        )
    if release_info.get("changes") != changes:
        raise RuntimeError(
            "Список изменений app/release_info.json не соответствует текущей записи CHANGELOG.md."
        )
    return version, commit


def _run_release_main(root: Path, args: argparse.Namespace) -> int:
    version, commit = validate_release_source_identity(
        root,
        expected_version=args.expected_version,
        expected_commit=args.expected_commit,
    )
    print(f"Сборка GitHub-источника: версия {version}, коммит {commit}.")
    emit_progress(
        args,
        stage="prepare",
        status="completed",
        progress=10,
        message="GitHub-коммит, версия и рабочее дерево проверены.",
    )
    finish_release(root, version, args)
    print("Релизная сборка завершена.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    validate_cli_args(args)
    emit_progress(
        args,
        stage="start",
        status="started",
        progress=0,
        message=(
            "Запуск тестовой сборки рабочего дерева."
            if args.test_worktree
            else "Запуск релизной сборки."
        ),
    )
    try:
        root = project_root()
        ensure_git_repo(root)
        if args.test_worktree:
            build_test_worktree(root, args)
            return 0

        cleanup_build_artifacts(root)
        ensure_clean_tree(root)
        result = _run_release_main(root, args)
    except Exception as exc:
        emit_progress(
            args,
            stage="failed",
            status="failed",
            progress=_current_progress(args),
            message=str(exc),
        )
        raise
    if not args.test_worktree:
        emit_progress(
            args,
            stage="completed",
            status="completed",
            progress=100,
            message="Релизная сборка завершена.",
        )
    return result


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        raise SystemExit(1)

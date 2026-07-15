from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.full_update_manifest import (  # noqa: E402
    FULL_MANIFEST_SCHEMA_VERSION,
    FullUpdateManifestError,
    compute_sha256,
    normalize_file_inventory,
    release_file_path,
    verify_file_inventory,
)


APP_ID = "rem_card"
MANIFEST_FILE_NAME = "manifest.json"
READY_FILE_NAME = "ready.ok"
TEST_WORKTREE_MARKER_NAME = "TEST_WORKTREE_ONLY.txt"
RELEASES_DIR_NAME = "releases"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
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
COPY_ATTEMPTS = 4
RETRY_BASE_DELAY_SECONDS = 0.5
STALE_STAGING_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
STALE_LOCK_MAX_AGE_SECONDS = 12 * 60 * 60


class PublishError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PublishError(f"Файл не найден: {path}") from exc
    except Exception as exc:
        raise PublishError(f"Не удалось прочитать JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise PublishError(f"JSON должен содержать объект: {path}")
    return payload


def _settings_snapshot_content_hash(snapshot: dict[str, Any]) -> str:
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
            raise PublishError(
                "Snapshot настроек содержит небезопасный путь media-файла: "
                f"{relative!r}."
            )
        media_path = settings_dir.joinpath(*parts).resolve()
        try:
            media_path.relative_to(settings_dir.resolve())
        except ValueError as exc:
            raise PublishError(
                f"Media-файл snapshot настроек находится вне settings_release: {relative!r}."
            ) from exc
        if not media_path.is_file():
            raise PublishError(f"В snapshot настроек отсутствует media-файл: {relative}.")
        expected_hash = str(value.get("sha256") or "").lower()
        if not SHA256_RE.fullmatch(expected_hash):
            raise PublishError(f"У media-файла snapshot нет корректного sha256: {relative}.")
        try:
            expected_size = int(value.get("size"))
        except (TypeError, ValueError) as exc:
            raise PublishError(f"У media-файла snapshot нет корректного размера: {relative}.") from exc
        if expected_size < 0 or media_path.stat().st_size != expected_size:
            raise PublishError(f"Размер media-файла snapshot не совпадает: {relative}.")
        if compute_sha256(media_path) != expected_hash:
            raise PublishError(f"SHA-256 media-файла snapshot не совпадает: {relative}.")

    for item in value.values():
        _validate_settings_media(item, settings_dir)


def _validate_settings_release(
    source: Path,
    *,
    version: str,
    source_commit: str,
    package_manifest: dict[str, Any],
) -> dict[str, Any]:
    settings_dir = source / SETTINGS_RELEASE_DIR
    snapshot = _read_json(settings_dir / SETTINGS_RELEASE_SNAPSHOT_FILE)
    settings_manifest = _read_json(settings_dir / SETTINGS_RELEASE_MANIFEST_FILE)

    if snapshot.get("schema_version") != 1:
        raise PublishError("Snapshot настроек имеет неподдерживаемую schema_version.")
    for field, expected in (
        ("release_version", version),
        ("release_commit", source_commit),
    ):
        if snapshot.get(field) != expected:
            raise PublishError(
                f"Snapshot настроек не соответствует релизу: {field}="
                f"{snapshot.get(field)!r}, ожидалось {expected!r}."
            )

    content_hash = str(snapshot.get("content_hash") or "").lower()
    if not SHA256_RE.fullmatch(content_hash):
        raise PublishError("Snapshot настроек не содержит корректный content_hash.")
    actual_hash = _settings_snapshot_content_hash(snapshot)
    if actual_hash != content_hash:
        raise PublishError("Snapshot настроек повреждён: content_hash не совпадает.")
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
    mismatches = [
        name
        for name, expected in expected_settings_manifest.items()
        if settings_manifest.get(name) != expected
    ]
    if mismatches:
        raise PublishError(
            "settings_release_manifest.json не соответствует snapshot: "
            + ", ".join(mismatches)
        )

    metadata = {
        "manifest_schema_version": 1,
        "snapshot_schema_version": 1,
        "snapshot_file": SETTINGS_RELEASE_SNAPSHOT_FILE,
        "content_hash": content_hash,
        "release_version": version,
        "release_commit": source_commit,
    }
    if package_manifest.get("settings_release") != metadata:
        raise PublishError(
            "Full-manifest не содержит точную метаинформацию settings_release "
            "для production-релиза."
        )
    return metadata


def _default_local_baza_dir() -> Path:
    return PROJECT_ROOT.parent / "Baza_rao3_jurnal"


def _resolve_source(args: argparse.Namespace) -> Path:
    if args.source:
        return Path(args.source).expanduser().resolve()
    version = str(args.version or "").strip()
    if not VERSION_RE.fullmatch(version):
        raise PublishError(
            "Для production-публикации нужно явно указать принятую версию "
            "через --source или --version."
        )
    local_baza = Path(args.local_baza_dir or _default_local_baza_dir()).expanduser().resolve()
    return local_baza / "UPD" / RELEASES_DIR_NAME / version


def _resolve_production_baza(args: argparse.Namespace) -> Path:
    if args.baza_dir:
        raw_path = args.baza_dir
    else:
        config = _read_json(Path(args.config).expanduser().resolve())
        raw_path = config.get("baza_dir") or config.get("path")
        if not raw_path:
            raise PublishError("В production JSON отсутствует поле baza_dir.")
    baza_dir = Path(str(raw_path).strip().strip('"')).expanduser().resolve()
    if not baza_dir.is_dir():
        raise PublishError(f"Сетевая папка базы недоступна: {baza_dir}")
    if not (baza_dir / "archiv").is_dir():
        raise PublishError(
            "Выбранная папка не похожа на корень базы RemCard: "
            f"нет каталога {baza_dir / 'archiv'}"
        )
    return baza_dir


def _validate_release(source: Path) -> tuple[dict[str, Any], str]:
    if not source.is_dir():
        raise PublishError(f"Локальный full-релиз не найден: {source}")
    test_marker = source / TEST_WORKTREE_MARKER_NAME
    if test_marker.exists():
        raise PublishError(
            f"Production-публикация тестовой сборки запрещена: найден {test_marker}"
        )
    if not (source / READY_FILE_NAME).is_file():
        raise PublishError(f"Локальный релиз не готов: отсутствует {source / READY_FILE_NAME}")

    manifest = _read_json(source / MANIFEST_FILE_NAME)
    if manifest.get("app") != APP_ID:
        raise PublishError("manifest.json относится не к приложению rem_card.")
    if manifest.get("package_type") != "full":
        raise PublishError("Разрешена публикация только полного пакета обновления.")
    try:
        schema_version = int(manifest.get("schema_version") or 0)
    except (TypeError, ValueError) as exc:
        raise PublishError("В manifest.json указана некорректная schema_version.") from exc
    if schema_version != FULL_MANIFEST_SCHEMA_VERSION:
        raise PublishError(
            f"Для публикации нужен точный full-manifest schema_version={FULL_MANIFEST_SCHEMA_VERSION}."
        )
    if manifest.get("prog_dir") != ".":
        raise PublishError("Production full-manifest должен содержать prog_dir='.'.")
    version = str(manifest.get("version") or "").strip()
    if not VERSION_RE.fullmatch(version):
        raise PublishError(f"Некорректная версия в manifest.json: {version!r}")
    source_commit = str(manifest.get("source_commit") or "").strip().lower()
    if not COMMIT_RE.fullmatch(source_commit):
        raise PublishError("В manifest.json отсутствует корректный 40-символьный source_commit.")
    if source.name != version:
        raise PublishError(
            f"Имя папки релиза ({source.name}) не совпадает с версией manifest ({version})."
        )
    for exe_name in REQUIRED_RELEASE_EXES:
        if not (source / exe_name).is_file():
            raise PublishError(f"Full-релиз неполный: отсутствует {exe_name}.")
    bundled_version = source / "_internal" / "rem_card" / "VERSION"
    try:
        bundled_version_text = bundled_version.read_text(encoding="utf-8").splitlines()[0].strip()
    except Exception as exc:
        raise PublishError(f"Не удалось проверить bundled VERSION: {bundled_version}") from exc
    if bundled_version_text != version:
        raise PublishError(
            f"Bundled VERSION ({bundled_version_text}) не совпадает с manifest ({version})."
        )
    _validate_settings_release(
        source,
        version=version,
        source_commit=source_commit,
        package_manifest=manifest,
    )
    try:
        normalize_file_inventory(manifest.get("files"), required=True)
        verify_file_inventory(source, manifest.get("files"), reject_extra=True)
    except FullUpdateManifestError as exc:
        raise PublishError(f"Локальный full-релиз не прошёл SHA-256 проверку: {exc}") from exc
    return manifest, version


def _git_output(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(PROJECT_ROOT),
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return str(result.stdout or "").strip()


def _ensure_source_commit_is_pushed(manifest: dict[str, Any]) -> None:
    source_commit = str(manifest.get("source_commit") or "").strip()
    if not source_commit:
        raise PublishError("В manifest.json отсутствует source_commit.")
    try:
        subprocess.run(
            ["git", "fetch", "--quiet", "--prune", "origin"],
            cwd=str(PROJECT_ROOT),
            check=True,
            timeout=120,
        )
        containing_refs = _git_output(
            "branch",
            "-r",
            "--contains",
            source_commit,
            "--format=%(refname:short)",
        )
    except PublishError:
        raise
    except Exception as exc:
        raise PublishError(f"Не удалось проверить обязательный git push: {exc}") from exc
    remote_refs = [
        line.strip()
        for line in containing_refs.splitlines()
        if line.strip().startswith("origin/") and line.strip() != "origin/HEAD"
    ]
    if not remote_refs:
        raise PublishError(
            f"Коммит сборки {source_commit[:12]} отсутствует в актуальных ветках origin. "
            "Сначала выполните git push."
        )


def _is_network_path(path: Path) -> bool:
    raw = str(path).replace("/", "\\")
    folded = raw.casefold()
    if folded.startswith("\\\\?\\unc\\"):
        return True
    if folded.startswith(("\\\\?\\", "\\\\.\\")):
        return False
    if raw.startswith("\\\\"):
        return True
    if os.name != "nt" or not path.drive:
        return False
    try:
        return int(ctypes.windll.kernel32.GetDriveTypeW(path.drive + "\\")) == 4
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


def _ready_text(version: str) -> str:
    return (
        datetime.now().astimezone().isoformat(timespec="seconds")
        + f" version={version} host={socket.gethostname()}\n"
    )


def _write_ready_atomic(release_dir: Path, version: str) -> None:
    target = release_dir / READY_FILE_NAME
    temporary = release_dir / f".{READY_FILE_NAME}.{os.getpid()}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(_ready_text(version))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except Exception:
            pass


def _verify_destination(
    release_dir: Path,
    manifest: dict[str, Any],
    *,
    verify_inventory: bool = True,
) -> None:
    destination_manifest = _read_json(release_dir / MANIFEST_FILE_NAME)
    if destination_manifest != manifest:
        raise PublishError("manifest.json в сетевой папке отличается от локального релиза.")
    if verify_inventory:
        try:
            verify_file_inventory(release_dir, manifest.get("files"), reject_extra=True)
        except FullUpdateManifestError as exc:
            raise PublishError(f"Сетевой full-релиз не прошёл SHA-256 проверку: {exc}") from exc


def _retry_operation(label: str, operation: Callable[[], Any]) -> Any:
    last_error: BaseException | None = None
    for attempt in range(1, COPY_ATTEMPTS + 1):
        try:
            return operation()
        except (OSError, PublishError, shutil.Error) as exc:
            last_error = exc
            if attempt >= COPY_ATTEMPTS:
                break
            delay = RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
            print(
                f"Сетевая операция не удалась ({label}), повтор "
                f"{attempt + 1}/{COPY_ATTEMPTS} через {delay:.1f} с: {exc}"
            )
            time.sleep(delay)
    raise PublishError(
        f"Сетевая операция не выполнена после {COPY_ATTEMPTS} попыток ({label}): {last_error}"
    ) from last_error


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _publish_lock_is_stale(lock_dir: Path) -> bool:
    try:
        age = max(0.0, time.time() - lock_dir.stat().st_mtime)
    except OSError:
        return False
    if age >= STALE_LOCK_MAX_AGE_SECONDS:
        return True
    try:
        owner = _read_json(lock_dir / "owner.json")
    except PublishError:
        # Give a concurrently starting publisher time to write its owner file.
        return age >= 120
    owner_host = str(owner.get("host") or "")
    try:
        owner_pid = int(owner.get("pid") or 0)
    except (TypeError, ValueError):
        owner_pid = 0
    return owner_host == socket.gethostname() and not _process_is_alive(owner_pid)


def _acquire_publish_lock(releases_dir: Path, version: str) -> Path:
    lock_dir = releases_dir / f".publish-{version}.lock"
    for _attempt in range(2):
        try:
            lock_dir.mkdir()
        except FileExistsError:
            if not _publish_lock_is_stale(lock_dir):
                raise PublishError(
                    f"Релиз {version} уже публикуется другим процессом: {lock_dir}"
                )
            shutil.rmtree(lock_dir, ignore_errors=True)
            continue
        try:
            owner = {
                "host": socket.gethostname(),
                "pid": os.getpid(),
                "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            (lock_dir / "owner.json").write_text(
                json.dumps(owner, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception:
            shutil.rmtree(lock_dir, ignore_errors=True)
            raise
        return lock_dir
    raise PublishError(f"Не удалось захватить блокировку публикации: {lock_dir}")


def _release_publish_lock(lock_dir: Path) -> None:
    shutil.rmtree(lock_dir, ignore_errors=True)


def _cleanup_stale_staging(releases_dir: Path, *, keep: Path) -> None:
    now = time.time()
    for candidate in releases_dir.glob(".staging-*"):
        if candidate == keep or not candidate.is_dir():
            continue
        try:
            age = max(0.0, now - candidate.stat().st_mtime)
        except OSError:
            continue
        if age < STALE_STAGING_MAX_AGE_SECONDS:
            continue
        try:
            shutil.rmtree(candidate)
            print(f"Удалён устаревший staging публикации: {candidate}")
        except OSError as exc:
            print(f"Не удалось удалить устаревший staging {candidate}: {exc}")


def _cleanup_unexpected_staging_files(
    staging: Path,
    inventory: list[dict[str, Any]],
) -> None:
    expected = {MANIFEST_FILE_NAME.casefold()}
    expected.update(str(entry["path"]).casefold() for entry in inventory)
    for current_dir, dir_names, file_names in os.walk(staging, topdown=False):
        current = Path(current_dir)
        for file_name in file_names:
            path = current / file_name
            relative = path.relative_to(staging).as_posix().casefold()
            if relative not in expected:
                path.unlink(missing_ok=True)
        for dir_name in dir_names:
            path = current / dir_name
            if path.is_symlink():
                path.unlink(missing_ok=True)
                continue
            try:
                path.rmdir()
            except OSError:
                pass


def _prepare_resumable_staging(
    source: Path,
    staging: Path,
    manifest: dict[str, Any],
    inventory: list[dict[str, Any]],
) -> None:
    if staging.exists():
        if staging.is_symlink() or not staging.is_dir():
            raise PublishError(f"Путь staging не является обычным каталогом: {staging}")
        try:
            existing_manifest = _read_json(staging / MANIFEST_FILE_NAME)
        except PublishError:
            existing_manifest = None
        if existing_manifest != manifest:
            print(f"Staging относится к другому или повреждённому пакету, пересоздаю: {staging}")
            shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    _cleanup_unexpected_staging_files(staging, inventory)

    manifest_path = staging / MANIFEST_FILE_NAME
    if manifest_path.is_file() and _read_json(manifest_path) == manifest:
        return

    def copy_manifest() -> None:
        temporary = staging / f".{MANIFEST_FILE_NAME}.part"
        temporary.unlink(missing_ok=True)
        try:
            shutil.copyfile(source / MANIFEST_FILE_NAME, temporary)
            os.replace(temporary, manifest_path)
            if _read_json(manifest_path) != manifest:
                raise PublishError("Скопированный manifest.json отличается от источника.")
        finally:
            temporary.unlink(missing_ok=True)

    _retry_operation("manifest.json", copy_manifest)


def _destination_entry_matches(path: Path, entry: dict[str, Any]) -> bool:
    try:
        return (
            path.is_file()
            and path.stat().st_size == int(entry["size"])
            and compute_sha256(path) == str(entry["sha256"])
        )
    except OSError:
        return False


def _copy_inventory_entry(
    source_path: Path,
    destination_path: Path,
    entry: dict[str, Any],
    *,
    index: int,
    total_files: int,
    completed_before: int,
    total_bytes: int,
) -> None:
    size = int(entry["size"])
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    def copy_one() -> None:
        temporary = destination_path.with_name(f".{destination_path.name}.part")
        temporary.unlink(missing_ok=True)
        copied = 0
        last_report = time.monotonic()
        try:
            with source_path.open("rb") as source_handle, temporary.open("xb") as target_handle:
                while True:
                    block = source_handle.read(4 * 1024 * 1024)
                    if not block:
                        break
                    target_handle.write(block)
                    copied += len(block)
                    now = time.monotonic()
                    if now - last_report >= 2.0:
                        overall = completed_before + copied
                        percent = (overall * 100.0 / total_bytes) if total_bytes else 100.0
                        print(
                            f"Копирование [{index}/{total_files}] {entry['path']}: "
                            f"{percent:.1f}% общего объёма"
                        )
                        last_report = now
                target_handle.flush()
                os.fsync(target_handle.fileno())
            if temporary.stat().st_size != size or compute_sha256(temporary) != str(entry["sha256"]):
                raise PublishError(f"Скопированный файл не прошёл SHA-256: {entry['path']}")
            os.replace(temporary, destination_path)
        finally:
            temporary.unlink(missing_ok=True)

    _retry_operation(str(entry["path"]), copy_one)


def _resume_copy_without_ready(
    source: Path,
    staging: Path,
    manifest: dict[str, Any],
    lock_dir: Path,
) -> None:
    try:
        inventory = normalize_file_inventory(manifest.get("files"), required=True)
    except FullUpdateManifestError as exc:
        raise PublishError(f"Некорректный inventory full-релиза: {exc}") from exc
    _prepare_resumable_staging(source, staging, manifest, inventory)

    total_files = len(inventory)
    total_bytes = sum(int(entry["size"]) for entry in inventory)
    completed_bytes = 0
    resumed_files = 0
    for index, entry in enumerate(inventory, start=1):
        source_path = release_file_path(source, str(entry["path"]))
        destination_path = release_file_path(staging, str(entry["path"]))
        if _destination_entry_matches(destination_path, entry):
            resumed_files += 1
        else:
            _copy_inventory_entry(
                source_path,
                destination_path,
                entry,
                index=index,
                total_files=total_files,
                completed_before=completed_bytes,
                total_bytes=total_bytes,
            )
        completed_bytes += int(entry["size"])
        try:
            os.utime(lock_dir, None)
            os.utime(staging, None)
        except OSError:
            pass
        if index == total_files or index % 25 == 0 or (index == 1 and total_files > 1):
            percent = (completed_bytes * 100.0 / total_bytes) if total_bytes else 100.0
            print(f"Публикация: {index}/{total_files} файлов, {percent:.1f}% объёма.")
    if resumed_files:
        print(f"Возобновление публикации: повторная передача не потребовалась для {resumed_files} файлов.")

    # Every destination file was hash-checked above and unexpected files were
    # removed before copying. This avoids another complete read over SMB.
    _verify_destination(staging, manifest, verify_inventory=False)


def publish_release(
    source: Path,
    production_baza: Path,
    *,
    allow_local: bool = False,
    reactivate_existing: bool = False,
) -> Path:
    manifest, version = _validate_release(source)
    _ensure_source_commit_is_pushed(manifest)

    if not allow_local and not _is_network_path(production_baza):
        raise PublishError(
            "Production-публикация разрешена только в UNC или сетевой диск. "
            "Для изолированного локального теста укажите --allow-local-test."
        )

    update_root = production_baza / "UPD"
    releases_dir = update_root / RELEASES_DIR_NAME
    releases_dir.mkdir(parents=True, exist_ok=True)
    final_dir = releases_dir / version
    staging_dir = releases_dir / f".staging-{version}"
    if _paths_overlap(source, releases_dir):
        raise PublishError("Источник full-релиза и production releases не должны быть вложены друг в друга.")

    lock_dir = _acquire_publish_lock(releases_dir, version)
    try:
        _cleanup_stale_staging(releases_dir, keep=staging_dir)
        if final_dir.exists():
            _verify_destination(final_dir, manifest)
            if not (final_dir / READY_FILE_NAME).is_file():
                if not reactivate_existing:
                    raise PublishError(
                        f"Релиз {version} существует без ready.ok. Он мог быть отключён намеренно. "
                        "Для явной повторной активации добавьте --reactivate-existing."
                    )
                _retry_operation(
                    f"активация ready.ok для {version}",
                    lambda: _write_ready_atomic(final_dir, version),
                )
            return final_dir

        _resume_copy_without_ready(source, staging_dir, manifest, lock_dir)
        def finalize_staging() -> None:
            if final_dir.exists():
                if staging_dir.exists():
                    raise PublishError(
                        "Одновременно существуют staging и final каталоги релиза; "
                        "автоматическая перезапись запрещена."
                    )
                # Covers an ambiguous SMB response where rename succeeded but
                # the client received an error. Never trust an unexpected final
                # directory without a complete hash verification.
                _verify_destination(final_dir, manifest)
                return
            os.replace(staging_dir, final_dir)

        _retry_operation(f"атомарное завершение релиза {version}", finalize_staging)
        _retry_operation(
            f"создание ready.ok для {version}",
            lambda: _write_ready_atomic(final_dir, version),
        )
        return final_dir
    finally:
        # A matching staging directory is intentionally preserved after an
        # error: the next run validates completed files and resumes the copy.
        _release_publish_lock(lock_dir)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Проверить локальный full-релиз и безопасно опубликовать его "
            "в сетевой Baza_rao3_jurnal\\UPD."
        )
    )
    source_selection = parser.add_mutually_exclusive_group(required=True)
    source_selection.add_argument(
        "--source",
        help="Явная папка уже принятого локального releases\\<version>.",
    )
    source_selection.add_argument(
        "--version",
        help="Явная версия уже принятого релиза в локальной тестовой базе.",
    )
    parser.add_argument(
        "--local-baza-dir",
        help=(
            "Локальная тестовая Baza_rao3_jurnal; по умолчанию "
            f"{_default_local_baza_dir()}."
        ),
    )
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--baza-dir", help="Путь к сетевой production Baza_rao3_jurnal.")
    destination.add_argument(
        "--config",
        help="Путь к production remcard_data_path.json рядом с установленной программой.",
    )
    parser.add_argument(
        "--reactivate-existing",
        action="store_true",
        help="Явно вернуть ready.ok ранее подготовленному или отключённому релизу.",
    )
    parser.add_argument(
        "--allow-local-test",
        action="store_true",
        help="Разрешить destination на локальном диске только для изолированной проверки.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = _resolve_source(args)
    production_baza = _resolve_production_baza(args)
    final_dir = publish_release(
        source,
        production_baza,
        allow_local=bool(args.allow_local_test),
        reactivate_existing=bool(args.reactivate_existing),
    )
    print("Full-релиз проверен и опубликован.")
    print(f"Источник: {source}")
    print(f"Сетевая публикация: {final_dir}")
    print(f"Маркер готовности создан последним: {final_dir / READY_FILE_NAME}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PublishError, OSError) as exc:
        print(f"Ошибка публикации: {exc}", file=sys.stderr)
        raise SystemExit(1)

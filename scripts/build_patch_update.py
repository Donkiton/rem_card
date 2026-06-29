import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parents[0]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from build_release import (
    VERSIONED_FILES,
    build_changelog_changes,
    collect_commit_subjects,
    detect_level,
    ensure_clean_tree,
    ensure_git_repo,
    ensure_russian_changelog,
    git_output,
    head_commit,
    latest_version_commit,
    project_root,
    push_current_branch,
    update_release_files,
)
from bump_version import BUMP_LEVELS, read_version


if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    from _local_rem_card_bootstrap import bootstrap_local_rem_card

    bootstrap_local_rem_card()
except Exception:
    pass

from rem_card.app.update_package import (  # noqa: E402
    APP_ID,
    MANIFEST_FILE_NAME,
    PACKAGE_TYPE_FULL,
    PACKAGE_TYPE_PATCH,
    compute_sha256,
    get_package_type,
    patch_payload_path,
    validate_patch_manifest,
)
from rem_card.app.updater_main import READY_FILE_NAME  # noqa: E402


PATCH_CACHE_DIR_NAME = ".remcard_patch_cache"
DEFAULT_PATCH_TARGET_DIR = ROOT.parent / "Baza_rao3_jurnal" / "UPD"
DETERMINISTIC_HASH_SEED = "0"
SNAPSHOT_REL_PATH = "_internal/rem_card/settings_release/settings_release_snapshot.json"
GENERATED_POLICY = {
    SNAPSHOT_REL_PATH: {
        "mode": "content_hash",
        "timestamp_fields_ignored": ["exported_at"],
    }
}
TREE_EXCLUDES = {READY_FILE_NAME}
DIFF_EXCLUDES = {READY_FILE_NAME, MANIFEST_FILE_NAME}


@dataclass(frozen=True)
class BaseBuild:
    version: str
    commit: str
    root: Path
    canonical_tree: Path
    raw_full_tree: Path
    manifest: dict[str, Any]


@dataclass(frozen=True)
class PatchDiff:
    files: list[dict[str, Any]]
    delete: list[dict[str, Any]]
    skipped_generated: list[str]


def _run(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    subprocess.run(args, cwd=str(cwd), env=env, check=True)


def _read_json(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Ожидался JSON-объект: {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _rel_path(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _iter_tree_files(root: Path):
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def _tree_hashes(root: Path, *, exclude: set[str] | None = None) -> dict[str, dict[str, Any]]:
    excluded = exclude or set()
    result: dict[str, dict[str, Any]] = {}
    for path in _iter_tree_files(root):
        rel = _rel_path(path, root)
        if rel in excluded:
            continue
        result[rel] = {
            "path": rel,
            "size": path.stat().st_size,
            "sha256": compute_sha256(path),
        }
    return result


def _copy_tree(source: Path, target: Path, *, exclude: set[str] | None = None) -> None:
    excluded = exclude or set()
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    for path in _iter_tree_files(source):
        rel = _rel_path(path, source)
        if rel in excluded:
            continue
        dest = target / Path(*rel.split("/"))
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)


def _cache_root(root: Path) -> Path:
    return root / PATCH_CACHE_DIR_NAME


def _base_key(version: str, commit: str) -> str:
    return f"{version}_{commit[:12]}"


def _base_dir(root: Path, version: str, commit: str) -> Path:
    return _cache_root(root) / "bases" / _base_key(version, commit)


def _work_dir(root: Path, version: str) -> Path:
    return _cache_root(root) / "work" / version


def _load_base_build(root: Path, version: str, commit: str) -> BaseBuild | None:
    base_root = _base_dir(root, version, commit)
    canonical = base_root / "canonical_tree"
    raw = base_root / "raw_full_tree"
    metadata_path = base_root / "metadata.json"
    if not canonical.is_dir() or not metadata_path.is_file():
        return None
    metadata = _read_json(metadata_path)
    manifest = _read_json(base_root / "base_manifest.json")
    return BaseBuild(
        version=str(metadata.get("version") or version),
        commit=str(metadata.get("commit") or commit),
        root=base_root,
        canonical_tree=canonical,
        raw_full_tree=raw,
        manifest=manifest,
    )


def _full_manifest_commit(manifest: dict[str, Any]) -> str:
    return str(
        manifest.get("source_commit")
        or manifest.get("commit")
        or manifest.get("patch_commit")
        or ""
    ).strip()


def _full_manifest_commit_matches_base(root: Path, manifest_commit: str, base_commit: str) -> bool:
    if manifest_commit == base_commit:
        return True
    try:
        raw = git_output(root, ["rev-list", "--parents", "-n", "1", base_commit])
    except Exception:
        return False
    commits = raw.split()
    parents = commits[1:] if commits else []
    return manifest_commit in parents


def register_base_from_full_package(
    root: Path,
    package_dir: Path,
    *,
    version: str,
    commit: str,
    require_patch_capable: bool = True,
) -> BaseBuild:
    manifest = _read_json(package_dir / MANIFEST_FILE_NAME)
    if get_package_type(manifest) != PACKAGE_TYPE_FULL:
        raise RuntimeError("Текущий UPD не является full-пакетом.")
    if str(manifest.get("version") or "").strip() != version:
        raise RuntimeError(
            f"Версия full-пакета в UPD не совпадает с ожидаемой базой: "
            f"{manifest.get('version')!r} != {version!r}"
        )
    if require_patch_capable and not bool(manifest.get("patch_update_capable")):
        raise RuntimeError(
            "Текущий full-пакет не помечен как patch-aware. "
            "Сначала доставьте новый updater через full-релиз."
        )
    manifest_commit = _full_manifest_commit(manifest)
    if require_patch_capable and not manifest_commit:
        raise RuntimeError(
            "В full-пакете UPD нет source_commit. "
            "Сначала пересоберите full-релиз новым build_release.py/RemCard.spec."
        )
    if manifest_commit and not _full_manifest_commit_matches_base(root, manifest_commit, commit):
        raise RuntimeError(
            f"Commit full-пакета в UPD не совпадает с ожидаемой базой: "
            f"{manifest_commit[:12]} не равен {commit[:12]} и не является его прямым родителем"
        )

    base_root = _base_dir(root, version, commit)
    canonical = base_root / "canonical_tree"
    raw = base_root / "raw_full_tree"
    if base_root.exists():
        shutil.rmtree(base_root)
    _copy_tree(package_dir, canonical, exclude=TREE_EXCLUDES)
    _copy_tree(package_dir, raw, exclude=TREE_EXCLUDES)
    hashes = _tree_hashes(canonical)
    _write_json(base_root / "base_manifest.json", manifest)
    _write_json(base_root / "tree_hashes.json", hashes)
    metadata = {
        "version": version,
        "commit": commit,
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "source": str(package_dir),
        "tree_file_count": len(hashes),
    }
    _write_json(base_root / "metadata.json", metadata)
    return BaseBuild(version, commit, base_root, canonical, raw, manifest)


def find_or_register_base(root: Path, *, version: str, commit: str, upd_dir: Path) -> BaseBuild:
    cached = _load_base_build(root, version, commit)
    if cached:
        return cached
    if (upd_dir / READY_FILE_NAME).is_file() and (upd_dir / MANIFEST_FILE_NAME).is_file():
        return register_base_from_full_package(root, upd_dir, version=version, commit=commit)
    raise RuntimeError(
        "Нет базовой сборки для сравнения. Сначала сделайте full-релиз или зарегистрируйте full-базу."
    )


def _commit_timestamp(root: Path, commit: str) -> str:
    return git_output(root, ["show", "-s", "--format=%ct", commit]).strip()


def _deterministic_env(root: Path, patch_commit: str) -> dict[str, str]:
    timestamp = _commit_timestamp(root, patch_commit) or "1700000000"
    return {
        "PYTHONHASHSEED": DETERMINISTIC_HASH_SEED,
        "SOURCE_DATE_EPOCH": timestamp,
    }


def build_temp_full(root: Path, target_dir: Path, deterministic_env: dict[str, str]) -> None:
    if target_dir.exists():
        shutil.rmtree(target_dir)
    env = os.environ.copy()
    env.update(deterministic_env)
    env["REMCARD_BUILD_TARGET_DIR"] = str(target_dir)
    print("Детерминированное окружение сборки:", deterministic_env)
    _run([sys.executable, "-m", "PyInstaller", "RemCard.spec"], cwd=root, env=env)


def commit_patch(root: Path, version: str) -> None:
    _run(["git", "add", *VERSIONED_FILES], cwd=root)
    _run(["git", "commit", "-m", f"Патч {version}"], cwd=root)


def _snapshot_content_hash(path: Path) -> str:
    try:
        payload = _read_json(path)
    except Exception:
        return ""
    return str(payload.get("content_hash") or "").strip()


def canonicalize_full_tree(base_tree: Path, raw_new_tree: Path, canonical_new_tree: Path) -> list[str]:
    _copy_tree(raw_new_tree, canonical_new_tree, exclude=TREE_EXCLUDES)
    skipped: list[str] = []
    base_snapshot = base_tree / Path(*SNAPSHOT_REL_PATH.split("/"))
    new_snapshot = canonical_new_tree / Path(*SNAPSHOT_REL_PATH.split("/"))
    if base_snapshot.is_file() and new_snapshot.is_file():
        if _snapshot_content_hash(base_snapshot) and _snapshot_content_hash(base_snapshot) == _snapshot_content_hash(new_snapshot):
            shutil.copy2(base_snapshot, new_snapshot)
            skipped.append(SNAPSHOT_REL_PATH)
    return skipped


def build_patch_diff(base_tree: Path, canonical_new_tree: Path, skipped_generated: list[str]) -> PatchDiff:
    base_hashes = _tree_hashes(base_tree, exclude=DIFF_EXCLUDES)
    new_hashes = _tree_hashes(canonical_new_tree, exclude=DIFF_EXCLUDES)
    files: list[dict[str, Any]] = []
    delete: list[dict[str, Any]] = []
    for rel, new_info in sorted(new_hashes.items()):
        old_info = base_hashes.get(rel)
        if old_info and old_info["sha256"] == new_info["sha256"]:
            continue
        files.append(
            {
                "path": rel,
                "size": int(new_info["size"]),
                "sha256": new_info["sha256"],
                "old_sha256": old_info["sha256"] if old_info else None,
            }
        )
    for rel, old_info in sorted(base_hashes.items()):
        if rel not in new_hashes:
            delete.append({"path": rel, "old_sha256": old_info["sha256"]})
    return PatchDiff(files=files, delete=delete, skipped_generated=skipped_generated)


def build_patch_manifest(
    *,
    version: str,
    base_version: str,
    base_commit: str,
    source_commit: str,
    patch_commit: str,
    deterministic_env: dict[str, str],
    diff: PatchDiff,
) -> dict[str, Any]:
    manifest = {
        "schema_version": 1,
        "app": APP_ID,
        "package_type": PACKAGE_TYPE_PATCH,
        "version": version,
        "base_version": base_version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "base_commit": base_commit,
        "source_commit": source_commit,
        "patch_commit": patch_commit,
        "builder": "scripts/build_patch_update.py",
        "deterministic_env": deterministic_env,
        "files": diff.files,
        "delete": diff.delete,
        "generated_file_policy": GENERATED_POLICY,
        "generated_skipped_same_content_hash": diff.skipped_generated,
    }
    return validate_patch_manifest(manifest)


def write_patch_payload(patch_dir: Path, canonical_new_tree: Path, manifest: dict[str, Any]) -> None:
    if patch_dir.exists():
        shutil.rmtree(patch_dir)
    patch_dir.mkdir(parents=True, exist_ok=True)
    for entry in manifest["files"]:
        rel = str(entry["path"])
        source_path = canonical_new_tree / Path(*rel.split("/"))
        payload_path = Path(patch_payload_path(patch_dir, rel))
        payload_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, payload_path)
        if rel.casefold() == "remcardupdater.exe":
            support_path = patch_dir / "support" / "RemCardUpdater.exe"
            support_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, support_path)
    _write_json(patch_dir / MANIFEST_FILE_NAME, manifest)


def binary_output_paths(manifest: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for entry in manifest.get("files") or []:
        path = str(entry.get("path") or "")
        lower = path.casefold()
        if lower.endswith((".exe", ".pyz", ".pyd", ".dll", ".zip")):
            result.append(path)
    return result


def dry_run_patch_apply(base_tree: Path, patch_dir: Path, manifest: dict[str, Any], canonical_new_tree: Path) -> None:
    from rem_card.app.updater_main import _apply_patch_package

    dry_run_dir = patch_dir.parent / "dry_run_install"
    _copy_tree(base_tree, dry_run_dir)
    _apply_patch_package(
        source_dir=str(patch_dir),
        target_dir=str(dry_run_dir),
        manifest=manifest,
        current_version=str(manifest["base_version"]),
        status=lambda _text, _progress: None,
    )
    dry_hashes = _tree_hashes(dry_run_dir, exclude=TREE_EXCLUDES)
    canonical_hashes = _tree_hashes(canonical_new_tree, exclude=TREE_EXCLUDES)
    if dry_hashes != canonical_hashes:
        raise RuntimeError("Dry-run patch result does not match canonical patched tree.")


def publish_patch_package(patch_dir: Path, target_dir: Path, manifest: dict[str, Any]) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    ready_path = target_dir / READY_FILE_NAME
    if ready_path.exists():
        ready_path.unlink()
    for name in target_dir.iterdir():
        if name.is_dir():
            shutil.rmtree(name, ignore_errors=True)
        else:
            try:
                name.unlink()
            except FileNotFoundError:
                pass
    for item in patch_dir.iterdir():
        if item.name == READY_FILE_NAME:
            continue
        dest = target_dir / item.name
        if item.is_dir():
            shutil.copytree(item, dest)
        elif item.name != MANIFEST_FILE_NAME:
            shutil.copy2(item, dest)
    _write_json(target_dir / MANIFEST_FILE_NAME, manifest)
    validate_patch_manifest(_read_json(target_dir / MANIFEST_FILE_NAME))
    for entry in manifest["files"]:
        payload_path = Path(patch_payload_path(target_dir, entry["path"]))
        if compute_sha256(payload_path) != str(entry["sha256"]).lower():
            raise RuntimeError(f"Published payload hash mismatch: {entry['path']}")
    ready_path.write_text(datetime.now().astimezone().isoformat(timespec="seconds") + "\n", encoding="utf-8")


def register_published_base(
    root: Path,
    *,
    version: str,
    commit: str,
    canonical_new_tree: Path,
    raw_new_tree: Path,
    manifest: dict[str, Any],
) -> BaseBuild:
    base_root = _base_dir(root, version, commit)
    canonical = base_root / "canonical_tree"
    raw = base_root / "raw_full_tree"
    if base_root.exists():
        shutil.rmtree(base_root)
    _copy_tree(canonical_new_tree, canonical)
    _copy_tree(raw_new_tree, raw, exclude=TREE_EXCLUDES)
    _write_json(base_root / "base_manifest.json", manifest)
    _write_json(base_root / "tree_hashes.json", _tree_hashes(canonical))
    _write_json(
        base_root / "metadata.json",
        {
            "version": version,
            "commit": commit,
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "source": "patch_publish",
        },
    )
    return BaseBuild(version, commit, base_root, canonical, raw, manifest)


def cleanup_old_bases(root: Path, *, keep: int = 3) -> None:
    bases_dir = _cache_root(root) / "bases"
    if not bases_dir.is_dir():
        return
    bases = sorted((path for path in bases_dir.iterdir() if path.is_dir()), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in bases[max(1, keep):]:
        shutil.rmtree(path, ignore_errors=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Собирает patch-update пакет RemCard.")
    parser.add_argument("level", nargs="?", choices=("auto", *BUMP_LEVELS), default="auto")
    parser.add_argument("--set", dest="set_version", help="Задать точную версию MAJOR.MINOR.PATCH")
    parser.add_argument("--change", action="append", default=[], help="Добавить пункт в changelog вручную.")
    parser.add_argument("--allow-empty", action="store_true", help="Разрешить patch без новых git-коммитов.")
    parser.add_argument("--target-dir", default=str(DEFAULT_PATCH_TARGET_DIR), help="Папка публикации UPD.")
    parser.add_argument("--large-threshold-mb", type=float, default=80.0)
    parser.add_argument("--allow-large", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = project_root()
    ensure_git_repo(root)
    ensure_clean_tree(root)

    base_version = read_version(root)
    base_commit = latest_version_commit(root)
    base = find_or_register_base(root, version=base_version, commit=base_commit, upd_dir=Path(args.target_dir))
    source_commit = head_commit(root)

    subjects = collect_commit_subjects(root, base_commit)
    changes = build_changelog_changes(subjects, args.change)
    if not changes and not args.allow_empty:
        raise RuntimeError("Нет изменений для patch. Сначала сделайте рабочий коммит на русском языке.")
    if not changes:
        changes = ["Технический patch без изменений в коде"]
    ensure_russian_changelog(changes)

    level = detect_level(changes) if args.level == "auto" else args.level
    _current, next_version = update_release_files(root, level, changes, set_version=args.set_version)
    commit_patch(root, next_version)
    patch_commit = head_commit(root)
    deterministic_env = _deterministic_env(root, patch_commit)

    work = _work_dir(root, next_version)
    if work.exists():
        shutil.rmtree(work)
    raw_new = work / "full_new_raw"
    canonical_new = work / "full_new_canonical"
    patch_payload = work / "patch_payload"

    build_temp_full(root, raw_new, deterministic_env)
    skipped_generated = canonicalize_full_tree(base.canonical_tree, raw_new, canonical_new)
    diff = build_patch_diff(base.canonical_tree, canonical_new, skipped_generated)
    if not diff.files and not diff.delete:
        raise RuntimeError("Patch diff пустой: нечего публиковать.")

    manifest = build_patch_manifest(
        version=next_version,
        base_version=base_version,
        base_commit=base_commit,
        source_commit=source_commit,
        patch_commit=patch_commit,
        deterministic_env=deterministic_env,
        diff=diff,
    )
    _write_json(canonical_new / MANIFEST_FILE_NAME, manifest)
    write_patch_payload(patch_payload, canonical_new, manifest)
    dry_run_patch_apply(base.canonical_tree, patch_payload, manifest, canonical_new)

    patch_size = sum(Path(patch_payload_path(patch_payload, entry["path"])).stat().st_size for entry in manifest["files"])
    patch_size_mb = patch_size / (1024 * 1024)
    if patch_size_mb > float(args.large_threshold_mb) and not args.allow_large:
        raise RuntimeError(
            f"Patch payload {patch_size_mb:.1f} MB больше порога {args.large_threshold_mb:.1f} MB. "
            "Используйте full-релиз или повторите с --allow-large."
        )
    binary_paths = binary_output_paths(manifest)
    if binary_paths:
        print(
            "Предупреждение: patch содержит PyInstaller/binary output: "
            + ", ".join(binary_paths[:8])
        )
    if len(binary_paths) >= 4 and not args.allow_large:
        raise RuntimeError(
            "Patch содержит массовые изменения EXE/PYZ/DLL/ZIP. "
            "Проверьте детерминированность сборки и повторите с --allow-large "
            "или используйте full-релиз."
        )

    push_current_branch(root)
    publish_patch_package(patch_payload, Path(args.target_dir), manifest)
    register_published_base(
        root,
        version=next_version,
        commit=patch_commit,
        canonical_new_tree=canonical_new,
        raw_new_tree=raw_new,
        manifest=manifest,
    )
    cleanup_old_bases(root)
    print(f"Patch update {next_version} опубликован в {Path(args.target_dir)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        raise SystemExit(1)

import argparse
import json
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parents[0]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from _local_rem_card_bootstrap import bootstrap_local_rem_card

    bootstrap_local_rem_card()
except Exception:
    pass

from build_patch_update import (  # noqa: E402
    DEFAULT_PATCH_TARGET_DIR,
    DETERMINISTIC_HASH_SEED,
    SNAPSHOT_REL_PATH,
    build_patch_diff,
    build_patch_manifest,
    canonicalize_full_tree,
    dry_run_patch_apply,
    patch_package_size_bytes,
    write_patch_payload,
    _tree_hashes,
)
from rem_card.app.update_package import compute_sha256  # noqa: E402


FIXTURE_BASE_VERSION = "1.0.0"
FIXTURE_PATCH_VERSION = "1.0.1"
FIXTURE_BASE_COMMIT = "fixture-base"
FIXTURE_SOURCE_COMMIT = "fixture-source"
FIXTURE_PATCH_COMMIT = "fixture-patch"


@contextmanager
def _work_root(path: str | None, *, keep: bool) -> Iterator[Path]:
    if path:
        root = Path(path).resolve()
        session = root / "verify_patch_update_pipeline"
        if session.exists():
            shutil.rmtree(session)
        session.mkdir(parents=True, exist_ok=True)
        try:
            yield session
        finally:
            if not keep:
                shutil.rmtree(session, ignore_errors=True)
        return

    temp = tempfile.mkdtemp(prefix="remcard_patch_verify_")
    try:
        yield Path(temp)
    finally:
        if not keep:
            shutil.rmtree(temp, ignore_errors=True)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _snapshot(path: Path, *, content_hash: str, exported_at: str) -> None:
    _write_json(
        path,
        {
            "content_hash": content_hash,
            "exported_at": exported_at,
            "rows": {"fixture": 1},
        },
    )


def _create_fixture_trees(work: Path) -> tuple[Path, Path]:
    base = work / "fixture_base"
    raw_new = work / "fixture_raw_new"
    if base.exists():
        shutil.rmtree(base)
    if raw_new.exists():
        shutil.rmtree(raw_new)

    snapshot_rel = Path(*SNAPSHOT_REL_PATH.split("/"))
    _write_bytes(base / "_internal" / "rem_card" / "VERSION", b"1.0.0\n")
    _write_bytes(base / "_internal" / "rem_card" / "stable.txt", b"same\n")
    _write_bytes(base / "_internal" / "rem_card" / "changed.txt", b"old\n")
    _write_bytes(base / "_internal" / "rem_card" / "delete.txt", b"delete\n")
    _snapshot(base / snapshot_rel, content_hash="same-content", exported_at="2026-01-01T00:00:00+10:00")
    _write_json(base / "manifest.json", {"package_type": "full", "version": FIXTURE_BASE_VERSION})
    _write_bytes(base / "ready.ok", b"ok\n")

    _write_bytes(raw_new / "_internal" / "rem_card" / "VERSION", b"1.0.1\n")
    _write_bytes(raw_new / "_internal" / "rem_card" / "stable.txt", b"same\n")
    _write_bytes(raw_new / "_internal" / "rem_card" / "changed.txt", b"new\n")
    _write_bytes(raw_new / "_internal" / "rem_card" / "new.txt", b"added\n")
    _snapshot(raw_new / snapshot_rel, content_hash="same-content", exported_at="2026-01-02T00:00:00+10:00")
    _write_json(raw_new / "manifest.json", {"package_type": "full", "version": FIXTURE_PATCH_VERSION})
    _write_bytes(raw_new / "ready.ok", b"ok\n")
    return base, raw_new


def _tree_size(root: Path) -> int:
    total = 0
    for item in root.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


def _settings_policy_result(skipped_generated: list[str], diff_paths: set[str]) -> str:
    if SNAPSHOT_REL_PATH in skipped_generated:
        return "skipped_same_content_hash"
    if SNAPSHOT_REL_PATH in diff_paths:
        return "included_content_hash_changed"
    return "unchanged_or_absent"


def verify_pipeline(
    *,
    base_tree: Path,
    raw_new_tree: Path,
    work: Path,
    base_version: str,
    patch_version: str,
    base_commit: str,
    source_commit: str,
    patch_commit: str,
) -> dict[str, Any]:
    canonical_new = work / "canonical_new"
    patch_payload = work / "patch_payload"
    skipped_generated = canonicalize_full_tree(base_tree, raw_new_tree, canonical_new)
    diff = build_patch_diff(base_tree, canonical_new, skipped_generated)
    deterministic_env = {
        "PYTHONHASHSEED": DETERMINISTIC_HASH_SEED,
        "SOURCE_DATE_EPOCH": "1700000000",
    }
    manifest = build_patch_manifest(
        version=patch_version,
        base_version=base_version,
        base_commit=base_commit,
        source_commit=source_commit,
        patch_commit=patch_commit,
        deterministic_env=deterministic_env,
        diff=diff,
    )
    _write_json(canonical_new / "manifest.json", manifest)
    write_patch_payload(patch_payload, canonical_new, manifest)
    dry_run_patch_apply(base_tree, patch_payload, manifest, canonical_new)

    payload_size = patch_package_size_bytes(patch_payload)
    full_size = _tree_size(raw_new_tree)
    diff_paths = {str(entry["path"]) for entry in manifest["files"]}
    warnings: list[str] = []
    if payload_size == 0:
        warnings.append("patch payload is empty")
    if full_size and payload_size / full_size > 0.8:
        warnings.append("patch payload is close to full tree size")

    return {
        "status": "ok",
        "mode": "non_mutating_verify",
        "changed_count": len(manifest["files"]),
        "deleted_count": len(manifest["delete"]),
        "payload_size": payload_size,
        "full_size": full_size,
        "patch_ratio": round(payload_size / full_size, 6) if full_size else None,
        "generated_skipped_count": len(skipped_generated),
        "generated_skipped": skipped_generated,
        "settings_snapshot_policy": _settings_policy_result(skipped_generated, diff_paths),
        "would_publish_to": str(DEFAULT_PATCH_TARGET_DIR),
        "work_dir": str(work),
        "blockers": [],
        "warnings": warnings,
        "non_mutating_guarantees": [
            "does_not_bump_version",
            "does_not_write_release_info",
            "does_not_write_changelog_or_version",
            "does_not_commit",
            "does_not_push",
            "does_not_clear_upd",
            "does_not_publish",
            "does_not_create_ready_ok_in_upd",
        ],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Безопасно проверяет patch-update pipeline без bump/commit/push/publish. "
            "Реальный scripts/build_patch_update.py этим скриптом не запускается."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixture-tiny", action="store_true", help="Запустить маленький встроенный fixture.")
    mode.add_argument("--base-tree", help="Путь к базовому canonical tree.")
    parser.add_argument("--new-tree", help="Путь к новой raw/full tree. Требуется вместе с --base-tree.")
    parser.add_argument("--work-dir", help="Рабочая папка для временных файлов проверки.")
    parser.add_argument("--keep-work", action="store_true", help="Не удалять временную папку после проверки.")
    parser.add_argument("--base-version", default=FIXTURE_BASE_VERSION)
    parser.add_argument("--patch-version", default=FIXTURE_PATCH_VERSION)
    parser.add_argument("--base-commit", default=FIXTURE_BASE_COMMIT)
    parser.add_argument("--source-commit", default=FIXTURE_SOURCE_COMMIT)
    parser.add_argument("--patch-commit", default=FIXTURE_PATCH_COMMIT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    with _work_root(args.work_dir, keep=bool(args.keep_work)) as work:
        if args.fixture_tiny:
            base_tree, raw_new_tree = _create_fixture_trees(work)
        else:
            if not args.new_tree:
                raise RuntimeError("--new-tree обязателен вместе с --base-tree.")
            base_tree = Path(args.base_tree).resolve()
            raw_new_tree = Path(args.new_tree).resolve()
            if not base_tree.is_dir():
                raise RuntimeError(f"Base tree не найден: {base_tree}")
            if not raw_new_tree.is_dir():
                raise RuntimeError(f"New tree не найден: {raw_new_tree}")

        report = verify_pipeline(
            base_tree=base_tree,
            raw_new_tree=raw_new_tree,
            work=work,
            base_version=str(args.base_version),
            patch_version=str(args.patch_version),
            base_commit=str(args.base_commit),
            source_commit=str(args.source_commit),
            patch_commit=str(args.patch_commit),
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        raise SystemExit(1)

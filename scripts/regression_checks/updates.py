"""Safety-сценарии: updates."""

from __future__ import annotations

from typing import Any
from .common import PROJECT_ROOT
from pathlib import Path
from .common import _cached_source_segment
import argparse
import ast
import glob
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time


def _write_fake_update_package(path: str, version: str = "9.9.9") -> None:
    os.makedirs(os.path.join(path, "_internal"), exist_ok=True)
    for exe_name in (
        "RemCardDoctor.exe",
        "RemCardNurse.exe",
        "RemCardOperBlockEmergency.exe",
        "RemCardOperBlockPlanned.exe",
        "RemCardPathSetup.exe",
        "RemCardUpdater.exe",
    ):
        Path(path, exe_name).write_text("stub", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "app": "rem_card",
        "version": version,
        "prog_dir": ".",
    }
    Path(path, "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    Path(path, "ready.ok").write_text("ok\n", encoding="utf-8")


def _check_full_without_package_type_still_detected(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.update_checker import find_available_updates

    update_root = os.path.join(temp_root, "UPD_full_legacy")
    _write_fake_update_package(update_root, version="1.0.1")
    candidates = find_available_updates(current_version="1.0.0", update_root=update_root)
    if len(candidates) != 1:
        return False, f"legacy full package was not detected: {candidates}"
    if candidates[0].package_type != "full":
        return False, f"legacy full package type mismatch: {candidates[0].package_type}"
    return True, "ok"


def _check_full_update_manifest_schema_versions_are_fail_closed(temp_root: str) -> tuple[bool, str]:
    from rem_card.app import updater_main
    from rem_card.app.full_update_manifest import build_file_inventory
    from rem_card.app.update_checker import find_available_updates

    legacy_root = Path(temp_root, "UPD_schema_legacy")
    _write_fake_update_package(str(legacy_root), version="2.0.0")
    legacy_manifest = json.loads((legacy_root / "manifest.json").read_text(encoding="utf-8"))
    legacy_manifest.pop("schema_version", None)
    (legacy_root / "manifest.json").write_text(json.dumps(legacy_manifest), encoding="utf-8")
    if len(find_available_updates(current_version="1.0.0", update_root=str(legacy_root))) != 1:
        return False, "legacy schema1 manifest without schema_version was rejected"
    updater_main._validate_source(str(legacy_root))
    if updater_main._load_direct_release(str(legacy_root)) is None:
        return False, "direct updater rejected legacy schema1"

    schema2_root = Path(temp_root, "UPD_schema_2")
    _write_fake_update_package(str(schema2_root), version="2.0.0")
    schema2_manifest = json.loads((schema2_root / "manifest.json").read_text(encoding="utf-8"))
    schema2_manifest["schema_version"] = 2
    schema2_manifest["package_type"] = "full"
    schema2_manifest["files"] = build_file_inventory(schema2_root)
    (schema2_root / "manifest.json").write_text(
        json.dumps(schema2_manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if len(find_available_updates(current_version="1.0.0", update_root=str(schema2_root))) != 1:
        return False, "current schema2 full manifest was rejected"
    updater_main._validate_source(str(schema2_root))
    if updater_main._load_direct_release(str(schema2_root)) is None:
        return False, "direct updater rejected current schema2"

    invalid_values: tuple[object, ...] = (3, 0, -1, "2", None, True)
    future_source: Path | None = None
    future_manifest: dict[str, Any] | None = None
    for index, value in enumerate(invalid_values):
        invalid_root = Path(temp_root, f"UPD_schema_invalid_{index}")
        _write_fake_update_package(str(invalid_root), version="2.0.0")
        manifest = json.loads((invalid_root / "manifest.json").read_text(encoding="utf-8"))
        manifest["schema_version"] = value
        (invalid_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        if find_available_updates(current_version="1.0.0", update_root=str(invalid_root)):
            return False, f"invalid/future schema_version was offered to a client: {value!r}"
        if updater_main._load_direct_release(str(invalid_root)) is not None:
            return False, f"direct updater accepted invalid/future schema_version: {value!r}"
        try:
            updater_main._validate_source(str(invalid_root))
        except RuntimeError as exc:
            if "schema_version" not in str(exc):
                return False, f"invalid schema error is unclear for {value!r}: {exc}"
        else:
            return False, f"updater accepted invalid/future schema_version: {value!r}"
        if value == 3:
            future_source = invalid_root
            future_manifest = manifest

    if future_source is None or future_manifest is None:
        return False, "future schema fixture was not prepared"
    target_dir = Path(temp_root, "Installed_schema_future")
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    try:
        updater_main._replace_program_dir(
            source_dir=str(future_source),
            target_dir=str(target_dir),
            status=lambda _text, _progress: None,
            expected_manifest=future_manifest,
        )
    except RuntimeError as exc:
        if "schema_version" not in str(exc):
            return False, f"future schema replace error is unclear: {exc}"
    else:
        return False, "future schema reached installation"
    if (target_dir / "VERSION").read_text(encoding="utf-8").strip() != "1.0.0":
        return False, "future schema changed the installed version"
    if list(target_dir.glob("__upd_old_*")):
        return False, "future schema reached backup phase"
    return True, "ok"


def _check_update_checker_does_not_require_upd_prog_folder(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.update_checker import find_available_updates

    update_root = os.path.join(temp_root, "UPD_no_prog")
    _write_fake_update_package(update_root, version="1.0.1")
    if os.path.isdir(os.path.join(update_root, "Prog")):
        return False, "test setup unexpectedly created UPD\\Prog"
    candidates = find_available_updates(current_version="1.0.0", update_root=update_root)
    if len(candidates) != 1 or candidates[0].package_type != "full":
        return False, f"full candidate required UPD\\Prog folder: {candidates}"
    return True, "ok"


def _check_update_scan_skips_old_versions_and_sibling_upd(temp_root: str) -> tuple[bool, str]:
    from rem_card.app import update_checker

    baza_dir = Path(temp_root, "arbitrary_update_scan_root")
    update_root = baza_dir / "UPD"
    releases_dir = update_root / "releases"
    releases_dir.mkdir(parents=True, exist_ok=True)
    for version in ("0.9.0", "1.0.0", "1.0.1"):
        _write_fake_update_package(str(releases_dir / version), version=version)

    # A versioned directory whose manifest claims another version must not be
    # accepted, even if that claimed version would be newer.
    _write_fake_update_package(str(releases_dir / "2.0.0"), version="2.0.1")

    sibling_update_root = baza_dir.parent / "UPD"
    _write_fake_update_package(str(sibling_update_root), version="9.9.9")

    loaded_paths: list[str] = []
    original_get_update_root = update_checker.get_update_root
    original_load_candidate = update_checker._load_candidate
    try:
        update_checker.get_update_root = lambda baza_dir=None: str(update_root)

        def tracked_load(path: str):
            loaded_paths.append(os.path.abspath(path))
            return original_load_candidate(path)

        update_checker._load_candidate = tracked_load
        candidates = update_checker.find_available_updates(current_version="1.0.0")
    finally:
        update_checker.get_update_root = original_get_update_root
        update_checker._load_candidate = original_load_candidate

    versions = [candidate.version for candidate in candidates]
    if versions != ["1.0.1"]:
        return False, f"unexpected candidates after optimized scan: {versions}"
    loaded_names = {Path(path).name for path in loaded_paths}
    if loaded_names != {"1.0.1", "2.0.0"}:
        return False, f"old/current version directories were opened over SMB: {loaded_names}"
    sibling_key = os.path.normcase(os.path.abspath(sibling_update_root))
    if any(os.path.normcase(path).startswith(sibling_key) for path in loaded_paths):
        return False, "former sibling UPD was probed despite production Baza/UPD contract"
    return True, "ok"


def _check_compiled_startup_and_exit_full_update_checks(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    source = Path(PROJECT_ROOT, "app", "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    required = (
        "_main_impl",
        "_prepare_startup_before_qt",
        "_launch_startup_update_after_single_instance",
        "_finalize_startup_application",
        "_launch_regular_startup_update_if_needed",
        "_launch_startup_update",
        "_launch_exit_update_if_needed",
    )
    missing = [name for name in required if name not in functions]
    if missing:
        return False, f"update lifecycle helpers are missing: {missing}"

    main_source = _cached_source_segment(source, functions["_main_impl"]) or ""
    startup_gate_source = _cached_source_segment(
        source,
        functions["_launch_startup_update_after_single_instance"],
    ) or ""
    order = [
        main_source.find("_acquire_single_instance_for_startup("),
        main_source.find("_launch_startup_update_after_single_instance("),
        main_source.find("_prepare_runtime_context_for_startup("),
    ]
    if any(position < 0 for position in order) or order != sorted(order):
        return False, f"startup updater must run after single-instance and before runtime startup: {order}"
    gate_tokens = (
        "if active_local_operblock_case:",
        "_show_update_in_progress_if_needed()",
        "_launch_regular_startup_update_if_needed(role)",
    )
    if any(token not in startup_gate_source for token in gate_tokens):
        return False, "post-single-instance update gate lost its offline or update checks"
    if not re.search(r"if _launch_startup_update_after_single_instance\([\s\S]+?\):[\s\S]+?return", main_source):
        return False, "old compiled process does not stop after launching the startup updater"
    if "_prepare_startup_before_qt(" not in main_source:
        return False, "main startup coordinator lost pre-Qt preparation"

    regular_source = _cached_source_segment(
        source,
        functions["_launch_regular_startup_update_if_needed"],
    ) or ""
    regular_tokens = (
        "if not is_compiled()",
        "_find_startup_update_candidate()",
        "_launch_startup_update(",
    )
    if any(token not in regular_source for token in regular_tokens):
        return False, "regular compiled startup does not scan and launch a full update"

    launch_source = _cached_source_segment(source, functions["_launch_startup_update"]) or ""
    launch_tokens = (
        "restart_exe = os.path.abspath(sys.executable)",
        "launch_update(candidate, restart_exe=restart_exe, wait_for_parent=True)",
    )
    if any(token not in launch_source for token in launch_tokens):
        return False, "startup updater does not restart the same installed executable"

    exit_source = _cached_source_segment(source, functions["_launch_exit_update_if_needed"]) or ""
    exit_tokens = (
        "if not is_compiled()",
        "candidate = find_best_update()",
        "launch_update(candidate, restart_exe=None, wait_for_parent=True)",
    )
    if any(token not in exit_source for token in exit_tokens):
        return False, "clean-exit full-update scan/launch contract is missing"
    finalize_source = _cached_source_segment(
        source,
        functions["_finalize_startup_application"],
    ) or ""
    if (
        "if state.exit_code == 0:" not in finalize_source
        or "_launch_exit_update_if_needed()" not in finalize_source
        or "_finalize_startup_application(" not in main_source
    ):
        return False, "clean-exit full-update check is not called from application shutdown"
    return True, "ok"


def _check_preselected_offline_skips_updater_network_probes(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    main_source = Path(PROJECT_ROOT, "app", "main.py").read_text(encoding="utf-8")
    main_tree = ast.parse(main_source)
    functions = {
        node.name: node
        for node in main_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    startup_gate = functions.get("_launch_startup_update_after_single_instance")
    finder = functions.get("_find_startup_update_candidate")
    if startup_gate is None or finder is None:
        return False, "startup update functions are missing"

    def called_name(node: ast.Call) -> str:
        return node.func.id if isinstance(node.func, ast.Name) else ""

    network_probe_calls = {
        "_show_update_in_progress_if_needed",
        "_launch_regular_startup_update_if_needed",
    }
    gate_calls = {
        called_name(node)
        for node in ast.walk(startup_gate)
        if isinstance(node, ast.Call)
    }
    if not network_probe_calls.issubset(gate_calls):
        return False, f"offline gate misses updater calls: {sorted(network_probe_calls - gate_calls)}"
    gate_source = _cached_source_segment(main_source, startup_gate) or ""
    guard_position = gate_source.find("if active_local_operblock_case:")
    return_position = gate_source.find("return False", guard_position)
    first_probe = min(gate_source.find(call) for call in network_probe_calls)
    if min(guard_position, return_position, first_probe) < 0 or not (
        guard_position < return_position < first_probe
    ):
        return False, "preselected offline runtime does not return before updater network probes"

    finder_source = _cached_source_segment(main_source, finder) or ""
    if "is_update_in_progress" in finder_source:
        return False, "startup candidate finder still repeats the active-lock SMB scan"

    launcher_source = Path(PROJECT_ROOT, "app", "update_launcher.py").read_text(encoding="utf-8")
    launcher_tree = ast.parse(launcher_source)
    launch_function = next(
        (
            node
            for node in launcher_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "launch_update"
        ),
        None,
    )
    launch_source = _cached_source_segment(launcher_source, launch_function) if launch_function else ""
    if "is_update_in_progress(target_dir=target_dir)" not in (launch_source or ""):
        return False, "launcher lost the final race-safe active-lock check"
    return True, "ok"


def _check_full_update_inventory_rejects_tamper_missing_and_extra(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.full_update_manifest import (
        FullUpdateManifestError,
        build_file_inventory,
        verify_file_inventory,
    )

    package = Path(temp_root, "full_inventory")
    payload = package / "_internal" / "payload.bin"
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_bytes(b"original")
    (package / "RemCardDoctor.exe").write_bytes(b"doctor")
    (package / "manifest.json").write_text("{}\n", encoding="utf-8")
    (package / "ready.ok").write_text("ok\n", encoding="utf-8")

    inventory = build_file_inventory(package)
    normalized = verify_file_inventory(package, inventory, reject_extra=True)
    if normalized != inventory:
        return False, "full inventory roundtrip changed normalized entries"
    inventory_paths = {str(entry["path"]) for entry in inventory}
    if "manifest.json" in inventory_paths or "ready.ok" in inventory_paths:
        return False, "service files leaked into full inventory"

    payload.write_bytes(b"tampered")
    try:
        verify_file_inventory(package, inventory, reject_extra=True)
    except FullUpdateManifestError:
        pass
    else:
        return False, "tampered full package was accepted"

    payload.write_bytes(b"original")
    payload.unlink()
    try:
        verify_file_inventory(package, inventory, reject_extra=True)
    except FullUpdateManifestError:
        pass
    else:
        return False, "full package with a missing file was accepted"

    payload.write_bytes(b"original")
    extra = package / "unexpected.bin"
    extra.write_bytes(b"extra")
    try:
        verify_file_inventory(package, inventory, reject_extra=True)
    except FullUpdateManifestError:
        return True, "ok"
    return False, "full package with an unlisted extra file was accepted"


def _check_full_update_rejects_uninstallable_root_inventory(temp_root: str) -> tuple[bool, str]:
    from rem_card.app import updater_main
    from rem_card.app.full_update_manifest import FULL_MANIFEST_SCHEMA_VERSION, build_file_inventory

    source_dir = Path(temp_root, "UPD_full_unsupported_root")
    target_dir = Path(temp_root, "Installed_full_unsupported_root")
    _write_fake_update_package(str(source_dir), version="2.0.0")
    (source_dir / "unsupported-root.dll").write_bytes(b"not installed by the updater")
    manifest = {
        "schema_version": FULL_MANIFEST_SCHEMA_VERSION,
        "app": "rem_card",
        "package_type": "full",
        "version": "2.0.0",
        "prog_dir": ".",
        "files": build_file_inventory(source_dir),
    }
    (source_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "VERSION").write_text("1.0.0\n", encoding="utf-8")

    try:
        updater_main._replace_program_dir(
            source_dir=str(source_dir),
            target_dir=str(target_dir),
            status=lambda _text, _progress: None,
            expected_manifest=manifest,
        )
    except RuntimeError as exc:
        if "текущий апдейтер не устанавливает" not in str(exc):
            return False, f"unexpected unsupported root inventory error: {exc}"
    else:
        return False, "v2 inventory with an unsupported root file was installed"

    if (target_dir / "VERSION").read_text(encoding="utf-8").strip() != "1.0.0":
        return False, "target changed before unsupported root inventory was rejected"
    if list(target_dir.glob("__upd_old_*")):
        return False, "backup started before unsupported root inventory was rejected"
    return True, "ok"


def _check_full_update_rejects_manifest_change_during_copy(temp_root: str) -> tuple[bool, str]:
    from rem_card.app import updater_main

    source_dir = Path(temp_root, "UPD_full_manifest_swap")
    target_dir = Path(temp_root, "Installed_full_manifest_swap")
    _write_fake_update_package(str(source_dir), version="2.0.0")
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    expected_manifest = updater_main._validate_source(str(source_dir))

    original_copy = updater_main._copy_source_to_staging

    def copy_then_swap_manifest(source: str, staging: str) -> None:
        original_copy(source, staging)
        changed_manifest = dict(expected_manifest)
        changed_manifest["version"] = "2.0.1"
        Path(staging, "manifest.json").write_text(
            json.dumps(changed_manifest, ensure_ascii=False),
            encoding="utf-8",
        )

    try:
        updater_main._copy_source_to_staging = copy_then_swap_manifest
        try:
            updater_main._replace_program_dir(
                source_dir=str(source_dir),
                target_dir=str(target_dir),
                status=lambda _text, _progress: None,
                expected_manifest=expected_manifest,
            )
        except RuntimeError as exc:
            if "изменился во время сетевого копирования" not in str(exc):
                return False, f"unexpected changed manifest error: {exc}"
        else:
            return False, "manifest changed during copy but the update continued"
    finally:
        updater_main._copy_source_to_staging = original_copy

    if (target_dir / "VERSION").read_text(encoding="utf-8").strip() != "1.0.0":
        return False, "target changed after staged manifest mismatch"
    if list(target_dir.glob("__upd_old_*")):
        return False, "backup started before staged manifest identity was checked"
    return True, "ok"


def _check_full_update_rejects_overlapping_realpaths(temp_root: str) -> tuple[bool, str]:
    from rem_card.app import updater_main

    target_parent = Path(temp_root, "Installed_contains_source")
    source_inside_target = target_parent / "network_release"
    target_parent.mkdir(parents=True, exist_ok=True)
    _write_fake_update_package(str(source_inside_target), version="2.0.0")
    try:
        updater_main._replace_program_dir(
            source_dir=str(source_inside_target),
            target_dir=str(target_parent),
            status=lambda _text, _progress: None,
        )
    except RuntimeError as exc:
        if "вложены друг в друга" not in str(exc):
            return False, f"unexpected source-inside-target error: {exc}"
    else:
        return False, "source nested inside target was accepted"
    if list(target_parent.glob("__upd_new_*")) or list(target_parent.glob("__upd_old_*")):
        return False, "source-inside-target guard ran after staging/backup creation"

    source_parent = Path(temp_root, "Source_contains_installed")
    _write_fake_update_package(str(source_parent), version="2.0.0")
    target_inside_source = source_parent / "installed_program"
    target_inside_source.mkdir(parents=True, exist_ok=True)
    try:
        updater_main._replace_program_dir(
            source_dir=str(source_parent),
            target_dir=str(target_inside_source),
            status=lambda _text, _progress: None,
        )
    except RuntimeError as exc:
        if "вложены друг в друга" not in str(exc):
            return False, f"unexpected target-inside-source error: {exc}"
    else:
        return False, "target nested inside source was accepted"
    if list(target_inside_source.glob("__upd_new_*")) or list(target_inside_source.glob("__upd_old_*")):
        return False, "target-inside-source guard ran after staging/backup creation"

    alias_source = Path(temp_root, "Junction_alias_source")
    real_target = Path(temp_root, "Junction_real_target")
    _write_fake_update_package(str(alias_source), version="2.0.0")
    real_target.mkdir(parents=True, exist_ok=True)
    original_realpath = updater_main.os.path.realpath
    alias_key = os.path.normcase(os.path.abspath(alias_source))
    target_real = original_realpath(os.path.abspath(real_target))

    def junction_realpath(path):
        absolute = os.path.abspath(str(path))
        if os.path.normcase(absolute) == alias_key:
            return target_real
        return original_realpath(absolute)

    try:
        updater_main.os.path.realpath = junction_realpath
        if not updater_main._paths_overlap_by_realpath(str(alias_source), str(real_target)):
            return False, "junction-equivalent source/target were not recognized as overlapping"
        try:
            updater_main._replace_program_dir(
                source_dir=str(alias_source),
                target_dir=str(real_target),
                status=lambda _text, _progress: None,
            )
        except RuntimeError as exc:
            if "вложены друг в друга" not in str(exc):
                return False, f"unexpected junction-equivalent error: {exc}"
        else:
            return False, "junction-equivalent source/target were accepted"
    finally:
        updater_main.os.path.realpath = original_realpath
    return True, "ok"


def _check_full_update_network_publish_is_atomic_and_idempotent(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.full_update_manifest import FULL_MANIFEST_SCHEMA_VERSION, build_file_inventory
    from scripts import publish_full_update

    version = "9.8.7"
    source = Path(temp_root, "local_baza", "UPD", "releases", version)
    source.mkdir(parents=True, exist_ok=True)
    for exe_name in publish_full_update.REQUIRED_RELEASE_EXES:
        (source / exe_name).write_bytes(f"payload {exe_name}".encode("utf-8"))
    internal_dir = source / "_internal"
    internal_dir.mkdir(parents=True, exist_ok=True)
    (internal_dir / "runtime.bin").write_bytes(b"runtime")
    (internal_dir / "rem_card").mkdir(parents=True, exist_ok=True)
    (internal_dir / "rem_card" / "VERSION").write_text(version + "\n", encoding="utf-8")
    settings_dir = internal_dir / "rem_card" / "settings_release"
    settings_dir.mkdir(parents=True, exist_ok=True)
    snapshot_hash = hashlib.sha256(
        json.dumps(
            {"schema_version": 1, "tables": {}},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    settings_snapshot = {
        "schema_version": 1,
        "release_version": version,
        "release_commit": "a" * 40,
        "exported_at": "2026-07-14 12:00:00",
        "tables": {},
        "row_counts": {},
        "content_hash": snapshot_hash,
    }
    settings_manifest = {
        "schema_version": 1,
        "snapshot_schema_version": 1,
        "snapshot_file": "settings_release_snapshot.json",
        "content_hash": snapshot_hash,
        "release_version": version,
        "release_commit": "a" * 40,
        "exported_at": settings_snapshot["exported_at"],
        "row_counts": {},
    }
    (settings_dir / "settings_release_snapshot.json").write_text(
        json.dumps(settings_snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (settings_dir / "settings_release_manifest.json").write_text(
        json.dumps(settings_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": FULL_MANIFEST_SCHEMA_VERSION,
        "app": "rem_card",
        "package_type": "full",
        "version": version,
        "source_commit": "a" * 40,
        "prog_dir": ".",
        "settings_release": {
            "manifest_schema_version": 1,
            "snapshot_schema_version": 1,
            "snapshot_file": "settings_release_snapshot.json",
            "content_hash": snapshot_hash,
            "release_version": version,
            "release_commit": "a" * 40,
        },
        "files": build_file_inventory(source),
    }
    (source / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (source / "ready.ok").write_text("local ready\n", encoding="utf-8")

    snapshot_path = settings_dir / "settings_release_snapshot.json"
    original_snapshot_text = snapshot_path.read_text(encoding="utf-8")
    tampered_snapshot = dict(settings_snapshot)
    tampered_snapshot["release_commit"] = "b" * 40
    snapshot_path.write_text(
        json.dumps(tampered_snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        publish_full_update._validate_release(source)
    except publish_full_update.PublishError:
        pass
    else:
        return False, "production publisher accepted settings snapshot from another commit"
    finally:
        snapshot_path.write_text(original_snapshot_text, encoding="utf-8")

    production_baza = Path(temp_root, "production_baza")
    (production_baza / "archiv").mkdir(parents=True, exist_ok=True)
    releases_dir = production_baza / "UPD" / "releases"
    resumable_staging = releases_dir / f".staging-{version}"
    resumable_staging.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "manifest.json", resumable_staging / "manifest.json")
    shutil.copy2(source / "RemCardDoctor.exe", resumable_staging / "RemCardDoctor.exe")
    resumed_mtime = 946684800
    os.utime(resumable_staging / "RemCardDoctor.exe", (resumed_mtime, resumed_mtime))
    stale_staging = releases_dir / ".staging-obsolete"
    stale_staging.mkdir()
    stale_mtime = time.time() - publish_full_update.STALE_STAGING_MAX_AGE_SECONDS - 60
    os.utime(stale_staging, (stale_mtime, stale_mtime))
    original_push_check = publish_full_update._ensure_source_commit_is_pushed
    original_write_ready = publish_full_update._write_ready_atomic
    state = {"ready_calls": 0, "verified_before_ready": False}

    def probe_ready(release_dir: Path, published_version: str) -> None:
        if (release_dir / "ready.ok").exists():
            raise AssertionError("ready.ok existed before the final package probe")
        publish_full_update._verify_destination(release_dir, manifest)
        state["verified_before_ready"] = True
        state["ready_calls"] += 1
        original_write_ready(release_dir, published_version)

    try:
        publish_full_update._ensure_source_commit_is_pushed = lambda _manifest: None
        publish_full_update._write_ready_atomic = probe_ready
        final_dir = publish_full_update.publish_release(source, production_baza, allow_local=True)
        if not state["verified_before_ready"] or state["ready_calls"] != 1:
            return False, f"ready marker order was not proved: {state}"
        if not (final_dir / "ready.ok").is_file():
            return False, "published full release has no ready.ok"
        if int((final_dir / "RemCardDoctor.exe").stat().st_mtime) != resumed_mtime:
            return False, "valid file from resumable staging was copied again"
        if stale_staging.exists():
            return False, "obsolete network staging directory was not cleaned"

        second_dir = publish_full_update.publish_release(source, production_baza, allow_local=True)
        if second_dir != final_dir or state["ready_calls"] != 1:
            return False, "identical repeated full publication was not idempotent"

        (final_dir / "RemCardDoctor.exe").write_bytes(b"changed existing release")
        try:
            publish_full_update.publish_release(source, production_baza, allow_local=True)
        except publish_full_update.PublishError:
            return True, "ok"
        return False, "changed existing full release was silently overwritten or accepted"
    finally:
        publish_full_update._ensure_source_commit_is_pushed = original_push_check
        publish_full_update._write_ready_atomic = original_write_ready


def _check_full_update_publisher_requires_explicit_accepted_source(temp_root: str) -> tuple[bool, str]:
    from scripts import publish_full_update

    implicit_args = argparse.Namespace(
        source=None,
        version=None,
        local_baza_dir=str(Path(temp_root, "local_baza")),
    )
    try:
        publish_full_update._resolve_source(implicit_args)
    except publish_full_update.PublishError:
        pass
    else:
        return False, "production publisher silently selected the current project VERSION"

    accepted_source = Path(temp_root, "local_baza", "UPD", "releases", "9.8.7").resolve()
    explicit_args = publish_full_update.parse_args(
        [
            "--source",
            str(accepted_source),
            "--baza-dir",
            str(Path(temp_root, "production_baza")),
        ]
    )
    if publish_full_update._resolve_source(explicit_args) != accepted_source:
        return False, "publisher changed the explicitly accepted source directory"
    return True, "ok"


def _check_build_release_cleanup_source_contract(
    text: str,
    functions: dict[str, ast.FunctionDef],
    finish_source: str,
    publication_position: int,
) -> tuple[bool, str]:
    cleanup_position = finish_source.find("cleanup_build_artifacts(root)", publication_position)
    if cleanup_position < publication_position:
        return False, "production build/dist cleanup is missing after local release publication"

    run_build_source = (
        _cached_source_segment(text, functions["run_build"])
        if "run_build" in functions
        else ""
    ) or ""
    initial_cleanup_position = run_build_source.find("cleanup_build_artifacts(root)")
    pyinstaller_position = run_build_source.find('run([sys.executable, "-m", "PyInstaller", "RemCard.spec"]')
    if (
        initial_cleanup_position < 0
        or pyinstaller_position < 0
        or initial_cleanup_position > pyinstaller_position
    ):
        return False, "PyInstaller build does not start from clean build/dist directories"

    main_source = (
        _cached_source_segment(text, functions["main"])
        if "main" in functions
        else ""
    ) or ""
    startup_cleanup_position = main_source.find("cleanup_build_artifacts(root)")
    clean_tree_position = main_source.find("ensure_clean_tree(root)")
    if (
        startup_cleanup_position < 0
        or clean_tree_position < 0
        or startup_cleanup_position > clean_tree_position
    ):
        return False, "production release does not remove stale build/dist before preflight"
    return True, "ok"


def _check_build_release_cleanup_behavior(temp_root: str, build_release: Any) -> tuple[bool, str]:
    cleanup_root = Path(temp_root, "build_artifact_cleanup")
    for name in build_release.BUILD_ARTIFACT_DIR_NAMES:
        artifact_file = cleanup_root / name / "nested" / "artifact.bin"
        artifact_file.parent.mkdir(parents=True, exist_ok=True)
        artifact_file.write_bytes(b"stale")
    build_release.cleanup_build_artifacts(cleanup_root)
    leftovers = [
        name
        for name in build_release.BUILD_ARTIFACT_DIR_NAMES
        if (cleanup_root / name).exists()
    ]
    if leftovers:
        return False, f"release cleanup left generated directories behind: {leftovers}"

    preserved_dist = cleanup_root / "dist" / "Prog"
    preserved_dist.mkdir(parents=True, exist_ok=True)
    (cleanup_root / "build" / "temporary").mkdir(parents=True, exist_ok=True)
    build_release.cleanup_build_artifacts(cleanup_root, remove_dist=False)
    if (cleanup_root / "build").exists() or not preserved_dist.is_dir():
        return False, "test-worktree cleanup did not preserve dist while removing build"
    return True, "ok"


def _check_build_release_static_contract(
    text: str,
) -> tuple[bool, str]:
    required_tokens = (
        'run([sys.executable, "-m", "PyInstaller", "RemCard.spec"], cwd=root)',
        "package_dir = run_build(root)",
        "publish_built_release(",
        "write_staged_full_manifest(",
        "staging_dir.rename(final_dir)",
        "_write_ready_last(ready_path)",
        "require_inventory=True",
        "verify_file_inventory(",
        "run_release_checks(root)",
        "run_compiled_smoke(package_dir)",
        "REMCARD_SKIP_SETTINGS_RELEASE_EXPORT=1 запрещён",
        '"settings_release": settings_release',
        "source_inventory = build_file_inventory(package_dir)",
        "file_inventory=source_inventory",
        "if _paths_overlap(package_dir, releases_dir):",
        "cleanup_build_artifacts(root)",
    )
    missing = [token for token in required_tokens if token not in text]
    if missing:
        return False, f"build_release.py full-release pipeline token missing: {missing}"

    tree = ast.parse(text)
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    finish_source = _cached_source_segment(text, functions.get("finish_release")) if functions.get("finish_release") else ""
    gate_position = finish_source.find("run_release_checks(root)")
    build_position = finish_source.find("package_dir = run_build(root)", gate_position)
    smoke_position = finish_source.find("run_compiled_smoke(package_dir)", build_position)
    positions = [
        gate_position,
        build_position,
        smoke_position,
        finish_source.find("publish_built_release(", smoke_position),
    ]
    if any(position < 0 for position in positions) or positions != sorted(positions):
        return False, (
            "required immutable release order checks -> build -> smoke -> publish "
            f"is broken: {positions}"
        )
    forbidden = ("push_current_branch(", "commit_release(", "update_release_files(")
    present_forbidden = [token for token in forbidden if token in text]
    if present_forbidden:
        return False, (
            "immutable release pipeline still mutates or pushes source: "
            f"{present_forbidden}"
        )
    cleanup_source_ok, cleanup_source_detail = _check_build_release_cleanup_source_contract(
        text,
        functions,
        finish_source,
        positions[-1],
    )
    if not cleanup_source_ok:
        return False, cleanup_source_detail

    validate_source = (
        _cached_source_segment(text, functions["validate_full_package"])
        if "validate_full_package" in functions
        else ""
    ) or ""
    if "if require_inventory:" not in validate_source or "verify_file_inventory(" not in validate_source:
        return False, "required full inventory is not cryptographically verified before publication"

    publisher_text = Path(PROJECT_ROOT, "scripts", "publish_full_update.py").read_text(encoding="utf-8")
    publisher_tokens = (
        "_resume_copy_without_ready(",
        "_destination_entry_matches(",
        "_cleanup_stale_staging(",
        "_acquire_publish_lock(",
        "_retry_operation(",
        "verify_inventory=False",
    )
    publisher_missing = [token for token in publisher_tokens if token not in publisher_text]
    if publisher_missing:
        return False, f"resumable production publisher token missing: {publisher_missing}"
    return True, "ok"


def _import_build_release_for_contract() -> Any:
    scripts_path = str(Path(PROJECT_ROOT, "scripts"))
    added_scripts_path = scripts_path not in sys.path
    if added_scripts_path:
        sys.path.insert(0, scripts_path)
    try:
        from scripts import build_release
    finally:
        if added_scripts_path:
            sys.path.remove(scripts_path)
    return build_release


def _check_build_release_runtime_contract(
    temp_root: str,
    build_release: Any,
) -> tuple[bool, str]:
    package_dir = Path(temp_root, "overlap", "dist", "Prog")
    nested_releases = package_dir / "UPD" / "releases"
    separate_releases = Path(temp_root, "separate", "UPD", "releases")
    if not build_release._paths_overlap(package_dir, nested_releases):
        return False, "local publisher did not detect nested package/release paths"
    if build_release._paths_overlap(package_dir, separate_releases):
        return False, "local publisher rejected independent package/release paths"

    cleanup_behavior_ok, cleanup_behavior_detail = _check_build_release_cleanup_behavior(
        temp_root,
        build_release,
    )
    if not cleanup_behavior_ok:
        return False, cleanup_behavior_detail

    saved_skip = os.environ.get("REMCARD_SKIP_SETTINGS_RELEASE_EXPORT")
    os.environ["REMCARD_SKIP_SETTINGS_RELEASE_EXPORT"] = "1"
    try:
        try:
            build_release.run_build(Path(temp_root, "skip_snapshot_build"))
        except RuntimeError as exc:
            if "snapshot настроек" not in str(exc):
                return False, f"unexpected skipped settings snapshot error: {exc}"
        else:
            return False, "release build accepted REMCARD_SKIP_SETTINGS_RELEASE_EXPORT=1"
    finally:
        if saved_skip is None:
            os.environ.pop("REMCARD_SKIP_SETTINGS_RELEASE_EXPORT", None)
        else:
            os.environ["REMCARD_SKIP_SETTINGS_RELEASE_EXPORT"] = saved_skip
    return True, "ok"


def _check_build_release_gate_contract(
    temp_root: str,
    build_release: Any,
) -> tuple[bool, str]:
    original_run = build_release.run
    gate_calls: list[list[str]] = []

    def probe_run(args: list[str], *, cwd: Path, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        _ = cwd
        gate_calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    try:
        build_release.run = probe_run
        build_release.run_release_checks(Path(temp_root))
    finally:
        build_release.run = original_run
    if len(gate_calls) != 3:
        return False, f"expected three mandatory release gates, got: {gate_calls}"
    joined_gate_calls = [" ".join(call) for call in gate_calls]
    if not any("architecture_safety_check.py" in call for call in joined_gate_calls):
        return False, "architecture gate is missing before release build"
    if not any("regression_safety_checks.py" in call and "--profile fast" in call for call in joined_gate_calls):
        return False, "fast regression gate is missing before release build"
    if not any("--select=F821" in call for call in joined_gate_calls):
        return False, "F821 gate is missing before release build"
    return True, "ok"


def _check_build_release_smoke_contract(
    temp_root: str,
    build_release: Any,
) -> tuple[bool, str]:
    original_subprocess_run = build_release.subprocess.run
    smoke_calls: list[list[str]] = []

    def probe_smoke(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        smoke_calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, b"", b"")

    try:
        build_release.subprocess.run = probe_smoke
        build_release.run_compiled_smoke(Path(temp_root, "fake_package"))
    finally:
        build_release.subprocess.run = original_subprocess_run
    expected_smoke = [
        [str(Path(temp_root, "fake_package", exe_name)), "--compiled-smoke"]
        for exe_name in build_release.REQUIRED_RELEASE_EXES
    ]
    if smoke_calls != expected_smoke:
        return False, f"compiled smoke did not cover all release EXEs exactly once: {smoke_calls}"
    return True, "ok"


def _check_build_release_full_pipeline_contract(temp_root: str) -> tuple[bool, str]:
    text = Path(PROJECT_ROOT, "scripts", "build_release.py").read_text(encoding="utf-8")
    static_result = _check_build_release_static_contract(text)
    if not static_result[0]:
        return static_result

    build_release = _import_build_release_for_contract()
    for check in (
        _check_build_release_runtime_contract,
        _check_build_release_gate_contract,
        _check_build_release_smoke_contract,
    ):
        result = check(temp_root, build_release)
        if not result[0]:
            return result
    return True, "ok"


def _check_role_exe_names_preserved(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    text = Path(PROJECT_ROOT, "RemCard.spec").read_text(encoding="utf-8")
    expected = (
        "RemCardDoctor",
        "RemCardNurse",
        "RemCardOperBlockEmergency",
        "RemCardOperBlockPlanned",
        "RemCardPathSetup",
        "RemCardUpdater",
    )
    missing = [name for name in expected if f"name='{name}'" not in text and f'name="{name}"' not in text]
    if missing:
        return False, f"role EXE names missing from RemCard.spec: {missing}"
    if "--role" in text:
        return False, "RemCard.spec unexpectedly switched to --role model"
    alias_tokens = (
        "ENTRYPOINT_FILES = (",
        "shutil.copy2(source_path, os.path.join(ALIAS_ROOT, entrypoint))",
        "*[os.path.join(ALIAS_ROOT, entrypoint) for entrypoint in ENTRYPOINT_FILES]",
    )
    missing_alias_tokens = [token for token in alias_tokens if token not in text]
    if missing_alias_tokens:
        return False, (
            "PyInstaller entry points must be analyzed from ALIAS_ROOT; otherwise the "
            f"source rem_card shim hides compiled modules: {missing_alias_tokens}"
        )
    return True, "ok"


def _check_updater_does_not_require_UPD_Prog_folder(temp_root: str) -> tuple[bool, str]:
    return _check_update_checker_does_not_require_upd_prog_folder(temp_root)


def _capture_full_update_launch(
    temp_root: str,
    *,
    suffix: str,
    exit_code: int | None = None,
) -> tuple[bool, str, list[str], str, str, str]:
    from rem_card.app import update_launcher
    from rem_card.app.update_checker import find_available_updates, get_update_starting_lock_path

    update_root = os.path.join(temp_root, f"UPD_full_launch_{suffix}")
    target_dir = os.path.join(temp_root, f"Installed_{suffix}")
    baza_dir = os.path.join(temp_root, f"arbitrary_data_root_{suffix}")
    os.makedirs(os.path.join(baza_dir, "locks"), exist_ok=True)
    os.makedirs(target_dir, exist_ok=True)
    _write_fake_update_package(update_root, version="1.0.1")

    candidates = find_available_updates(current_version="1.0.0", update_root=update_root)
    if len(candidates) != 1 or candidates[0].package_type != "full":
        return False, f"full candidate not found: {candidates}", [], "", update_root, target_dir

    class CapturedProcess:
        pid = 99999999

        def poll(self):
            return exit_code

    captured: list[str] = []
    original_is_compiled = update_launcher.is_compiled
    original_resolve_baza_dir = update_launcher.resolve_baza_dir
    original_get_executable_dir = update_launcher.get_executable_dir
    original_popen_hidden = update_launcher.popen_hidden
    original_sleep = update_launcher.time.sleep
    try:
        update_launcher.is_compiled = lambda: True
        update_launcher.resolve_baza_dir = lambda: baza_dir
        update_launcher.get_executable_dir = lambda: target_dir
        update_launcher.time.sleep = lambda _seconds: None

        def fake_popen_hidden(args, **_kwargs):
            captured.extend(str(item) for item in args)
            return CapturedProcess()

        update_launcher.popen_hidden = fake_popen_hidden
        launched = update_launcher.launch_update(candidates[0], wait_for_parent=False)
        lock_path = get_update_starting_lock_path(baza_dir, target_dir=target_dir)
        return launched, "ok", captured, lock_path, update_root, target_dir
    finally:
        update_launcher.is_compiled = original_is_compiled
        update_launcher.resolve_baza_dir = original_resolve_baza_dir
        update_launcher.get_executable_dir = original_get_executable_dir
        update_launcher.popen_hidden = original_popen_hidden
        update_launcher.time.sleep = original_sleep


def _check_updater_target_uses_executable_dir(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.updater_main import _parse_args as parse_updater_args

    launched, message, args, _lock_path, update_root, target_dir = _capture_full_update_launch(
        temp_root,
        suffix="target",
    )
    if not launched:
        return False, message
    if not args:
        return False, "full updater process was not launched"
    expected_updater = os.path.abspath(os.path.join(update_root, "RemCardUpdater.exe"))
    if os.path.abspath(args[0]) != expected_updater:
        return False, f"unexpected full updater path: {args[0]}"
    target_index = args.index("--target") + 1
    if os.path.abspath(args[target_index]) != os.path.abspath(target_dir):
        return False, f"launcher target mismatch: {args[target_index]}"
    source_index = args.index("--source") + 1
    if os.path.abspath(args[source_index]) != os.path.abspath(update_root):
        return False, f"launcher source mismatch: {args[source_index]}"
    if "--runner-dir" in args:
        return False, "full updater unexpectedly uses the removed patch runner"
    legacy_args = parse_updater_args(
        [
            "--source", update_root,
            "--target", target_dir,
            "--baza-dir", os.path.dirname(update_root),
            "--lock", os.path.join(temp_root, "legacy.lock"),
            "--current-version", "1.0.0",
        ]
    )
    if legacy_args.current_version != "1.0.0":
        return False, "legacy --current-version compatibility argument was removed"
    return True, "ok"


def _check_updater_direct_launch_uses_explicit_target(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.update_checker import get_update_lock_path
    from rem_card.app.updater_main import DIRECT_TARGET_DIR_ENV, _build_direct_update_args

    saved_env = {
        key: os.environ.get(key)
        for key in ("REMCARD_BAZA_DIR", "REMCARD_UPDATE_TARGET_DIR")
    }
    try:
        os.environ.pop("REMCARD_BAZA_DIR", None)
        os.environ.pop("REMCARD_UPDATE_TARGET_DIR", None)

        root = os.path.join(temp_root, "share")
        baza_dir = os.path.join(root, "arbitrary_data_root")
        upd_dir = os.path.join(baza_dir, "UPD")
        target_dir = os.path.join(temp_root, "ArbitraryInstall", "RemCard")
        os.makedirs(os.path.join(baza_dir, "locks"), exist_ok=True)
        os.makedirs(target_dir, exist_ok=True)
        Path(target_dir, "VERSION").write_text("1.0.0\n", encoding="utf-8")
        _write_fake_update_package(upd_dir, version="1.0.1")

        try:
            guessed_args = _build_direct_update_args(upd_dir)
        except RuntimeError as exc:
            if "REMCARD_UPDATE_TARGET_DIR" not in str(exc):
                return False, f"unexpected direct launch target error: {exc}"
        else:
            if guessed_args is not None:
                return False, "direct package launch guessed an arbitrary install directory"
        os.environ[DIRECT_TARGET_DIR_ENV] = target_dir
        args = _build_direct_update_args(upd_dir)
        if args is None:
            return False, "direct UPD package with an explicit target was not recognized"

        expected = {
            "source": os.path.abspath(upd_dir),
            "target": os.path.abspath(target_dir),
            "baza_dir": os.path.abspath(baza_dir),
            "lock": os.path.abspath(get_update_lock_path(baza_dir, target_dir=target_dir)),
            "target_version": "1.0.1",
            "current_version": "1.0.0",
        }
        actual = {
            "source": os.path.abspath(args.source),
            "target": os.path.abspath(args.target),
            "baza_dir": os.path.abspath(args.baza_dir),
            "lock": os.path.abspath(args.lock),
            "target_version": args.target_version,
            "current_version": args.current_version,
        }
        if actual != expected:
            return False, f"direct updater args mismatch: {actual}"
        if args.parent_pid != "0" or args.starting_lock != "" or args.local_starting_lock != "":
            return False, f"unexpected direct launcher synchronization args: {args}"
        return True, "ok"
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _check_updater_process_gate_and_rename_only(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    updater_source = Path(PROJECT_ROOT, "app", "updater_main.py").read_text(encoding="utf-8")
    updater_tree = ast.parse(updater_source)
    updater_functions = {
        node.name: node
        for node in updater_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    required = ("_wait_for_target_processes", "_rename_path_with_retry", "_replace_program_dir")
    missing = [name for name in required if name not in updater_functions]
    if missing:
        return False, f"updater safety helpers are missing: {missing}"

    replace_source = _cached_source_segment(
        updater_source,
        updater_functions["_replace_program_dir"],
    ) or ""
    if "shutil.move(" in replace_source:
        return False, "program replacement still permits shutil.move copy/delete fallback"
    if "_rename_path_with_retry(" not in replace_source:
        return False, "program replacement does not use rename-only moves"

    worker = next(
        (node for node in updater_tree.body if isinstance(node, ast.ClassDef) and node.name == "UpdateWorker"),
        None,
    )
    run_method = next(
        (node for node in (worker.body if worker else []) if isinstance(node, ast.FunctionDef) and node.name == "run"),
        None,
    )
    run_source = _cached_source_segment(updater_source, run_method) if run_method else ""
    order = [
        (run_source or "").find("_wait_for_parent("),
        (run_source or "").find("_wait_for_target_processes("),
        (run_source or "").find("_wait_for_active_sessions("),
        (run_source or "").find("_replace_program_dir("),
    ]
    if any(position < 0 for position in order) or order != sorted(order):
        return False, f"target process gate is missing or ordered after replacement: {order}"

    launcher_source = Path(PROJECT_ROOT, "app", "update_launcher.py").read_text(encoding="utf-8")
    launcher_tree = ast.parse(launcher_source)
    launcher_function = next(
        (
            node
            for node in launcher_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "launch_update"
        ),
        None,
    )
    launch_source = _cached_source_segment(launcher_source, launcher_function) if launcher_function else ""
    launcher_tokens = (
        "get_local_update_starting_lock_path(target_dir)",
        "_write_starting_lock(local_starting_lock_path",
        '"--local-starting-lock"',
    )
    if any(token not in (launch_source or "") for token in launcher_tokens):
        return False, "launcher does not hold a host-local per-install starting lock"
    return True, "ok"


def _check_updater_cleanup_retries_old_backup(temp_root: str) -> tuple[bool, str]:
    from rem_card.app import updater_main

    source_dir = os.path.join(temp_root, "UPD")
    target_dir = os.path.join(temp_root, "Prog")
    os.makedirs(target_dir, exist_ok=True)
    _write_fake_update_package(source_dir, version="2.0.0")
    Path(source_dir, "RemCard.exe").write_text("new RemCard.exe", encoding="utf-8")
    Path(source_dir, "VERSION").write_text("2.0.0\n", encoding="utf-8")
    Path(source_dir, "CHANGELOG.md").write_text("new changelog", encoding="utf-8")
    Path(source_dir, "_internal", "new.txt").write_text("new internal", encoding="utf-8")

    for name in updater_main.MANAGED_ROOT_FILES:
        Path(target_dir, name).write_text(f"old {name}", encoding="utf-8")
    os.makedirs(os.path.join(target_dir, "_internal"), exist_ok=True)
    Path(target_dir, "_internal", "old.txt").write_text("old internal", encoding="utf-8")

    stale_dir = os.path.join(target_dir, "__upd_old_20000101_000000_1")
    os.makedirs(stale_dir, exist_ok=True)
    Path(stale_dir, "leftover.txt").write_text("leftover", encoding="utf-8")

    original_rmtree = updater_main.shutil.rmtree
    state = {"backup_failures": 0}

    def flaky_rmtree(path, *args, **kwargs):
        name = os.path.basename(os.path.abspath(path))
        if name.startswith("__upd_old_") and name != os.path.basename(stale_dir) and state["backup_failures"] == 0:
            state["backup_failures"] += 1
            raise PermissionError("simulated transient Windows file lock")
        return original_rmtree(path, *args, **kwargs)

    logs: list[str] = []
    try:
        updater_main.shutil.rmtree = flaky_rmtree
        _staging, backup = updater_main._replace_program_dir(
            source_dir=source_dir,
            target_dir=target_dir,
            status=lambda _text, _progress: None,
            log=logs.append,
        )
    finally:
        updater_main.shutil.rmtree = original_rmtree

    if state["backup_failures"] != 1:
        return False, "cleanup retry scenario was not exercised"
    if os.path.exists(backup):
        return False, f"current backup was left after transient rmtree failure: {backup}"
    leftovers = [
        path
        for path in glob.glob(os.path.join(target_dir, "__upd_*"))
        if os.path.isdir(path)
    ]
    if leftovers:
        return False, f"update temp directories were left behind: {leftovers}"
    if Path(target_dir, "VERSION").read_text(encoding="utf-8").strip() != "2.0.0":
        return False, "new version was not installed"
    unexpected_logs = [message for message in logs if not message.startswith("update phase=")]
    if unexpected_logs:
        return False, f"cleanup logged unexpected failure: {unexpected_logs}"
    return True, "ok"


def _check_full_update_rolls_back_on_install_failure(temp_root: str) -> tuple[bool, str]:
    from rem_card.app import updater_main

    source_dir = os.path.join(temp_root, "UPD_full_rollback")
    target_dir = os.path.join(temp_root, "Installed_full_rollback")
    os.makedirs(target_dir, exist_ok=True)
    _write_fake_update_package(source_dir, version="2.0.0")
    Path(source_dir, "_internal", "new.txt").write_text("new internal", encoding="utf-8")

    old_contents: dict[str, str] = {}
    for name in updater_main.MANAGED_ROOT_FILES:
        content = f"old {name}"
        old_contents[name] = content
        Path(target_dir, name).write_text(content, encoding="utf-8")
    Path(target_dir, "_internal").mkdir(parents=True, exist_ok=True)
    Path(target_dir, "_internal", "old.txt").write_text("old internal", encoding="utf-8")

    original_rename = updater_main._rename_path_with_retry
    state = {"failed": False}

    def is_staged_internal(source_path, target_path) -> bool:
        source_abs = os.path.abspath(str(source_path))
        target_abs = os.path.abspath(str(target_path))
        return (
            os.path.basename(source_abs) == "_internal"
            and os.path.basename(os.path.dirname(source_abs)).startswith("__upd_new_")
            and os.path.normcase(target_abs) == os.path.normcase(os.path.join(target_dir, "_internal"))
        )

    def fail_staged_move(source_path, target_path, description, *args, **kwargs):
        if is_staged_internal(source_path, target_path):
            state["failed"] = True
            raise PermissionError("simulated full update install failure")
        return original_rename(source_path, target_path, description, *args, **kwargs)

    try:
        updater_main._rename_path_with_retry = fail_staged_move
        try:
            updater_main._replace_program_dir(
                source_dir=source_dir,
                target_dir=target_dir,
                status=lambda _text, _progress: None,
            )
        except Exception:
            pass
        else:
            return False, "full update unexpectedly succeeded after simulated install failure"
    finally:
        updater_main._rename_path_with_retry = original_rename

    if not state["failed"]:
        return False, "full update failure point was not exercised"
    for name, expected in old_contents.items():
        path = Path(target_dir, name)
        if not path.is_file() or path.read_text(encoding="utf-8") != expected:
            return False, f"rollback did not restore {name}"
    if not Path(target_dir, "_internal", "old.txt").is_file():
        return False, "rollback did not restore old _internal"
    if Path(target_dir, "_internal", "new.txt").exists():
        return False, "rollback left a new _internal file installed"
    return True, "ok"


def _check_full_update_reports_and_preserves_failed_rollback(temp_root: str) -> tuple[bool, str]:
    from rem_card.app import updater_main

    source_dir = os.path.join(temp_root, "UPD_full_failed_rollback")
    target_dir = os.path.join(temp_root, "Installed_full_failed_rollback")
    os.makedirs(target_dir, exist_ok=True)
    _write_fake_update_package(source_dir, version="2.0.0")
    Path(source_dir, "_internal", "new.txt").write_text("new internal", encoding="utf-8")

    for name in updater_main.MANAGED_ROOT_FILES:
        Path(target_dir, name).write_text(f"old {name}", encoding="utf-8")
    Path(target_dir, "_internal").mkdir(parents=True, exist_ok=True)
    Path(target_dir, "_internal", "old.txt").write_text("old internal", encoding="utf-8")

    original_rename = updater_main._rename_path_with_retry
    state = {"install_failed": False, "restore_failed": False}

    def fail_install_and_one_restore(source_path, target_path, description, *args, **kwargs):
        source_abs = os.path.abspath(str(source_path))
        source_name = os.path.basename(source_abs)
        source_parent = os.path.basename(os.path.dirname(source_abs))
        if source_name == "_internal" and source_parent.startswith("__upd_new_"):
            state["install_failed"] = True
            raise PermissionError("simulated install failure")
        if source_name == "VERSION" and source_parent.startswith("__upd_old_"):
            state["restore_failed"] = True
            raise PermissionError("simulated rollback failure")
        return original_rename(source_path, target_path, description, *args, **kwargs)

    error_message = ""
    try:
        updater_main._rename_path_with_retry = fail_install_and_one_restore
        try:
            updater_main._replace_program_dir(
                source_dir=source_dir,
                target_dir=target_dir,
                status=lambda _text, _progress: None,
            )
        except RuntimeError as exc:
            error_message = str(exc)
        else:
            return False, "update unexpectedly succeeded after install and rollback failures"
    finally:
        updater_main._rename_path_with_retry = original_rename

    if not state["install_failed"] or not state["restore_failed"]:
        return False, f"failed rollback scenario was not exercised: {state}"
    required_fragments = (
        "восстановление старой версии выполнено не полностью",
        "Резервная копия сохранена:",
        "не удалось восстановить VERSION",
    )
    missing = [fragment for fragment in required_fragments if fragment not in error_message]
    if missing:
        return False, f"failed rollback error hides recovery state: missing={missing}; error={error_message}"

    backups = [Path(path) for path in glob.glob(os.path.join(target_dir, "__upd_old_*"))]
    if len(backups) != 1 or not (backups[0] / "VERSION").is_file():
        return False, f"failed rollback backup was not preserved: {backups}"

    updater_source = Path(PROJECT_ROOT, "app", "updater_main.py").read_text(encoding="utf-8")
    if "Старая версия программы оставлена без изменений." in updater_source:
        return False, "updater UI still promises an unchanged old version for every failure"
    return True, "ok"


def _check_updater_releases_lock_before_restart(temp_root: str) -> tuple[bool, str]:
    from rem_card.app import updater_main

    source_dir = Path(temp_root, "UPD_restart_lock")
    target_dir = Path(temp_root, "Installed_restart_lock")
    baza_dir = Path(temp_root, "Baza_restart_lock")
    source_dir.mkdir(parents=True, exist_ok=True)
    target_dir.mkdir(parents=True, exist_ok=True)
    baza_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "RemCardDoctor.exe").write_bytes(b"restart target")

    order: list[str] = []
    state = {"released": False, "completed": False}

    class FakeUpdateLock:
        def __init__(self, _lock_path, _payload):
            pass

        def acquire(self):
            order.append("acquire")

        def release(self, *, completed=False, **_kwargs):
            state["completed"] = bool(completed)
            if not state["released"]:
                state["released"] = True
                order.append("release")

    def fake_replace(**_kwargs):
        order.append("replace")
        return "", ""

    def fake_restart(_args, **_kwargs):
        if not state["released"]:
            raise AssertionError("restart started while update lock was still active")
        order.append("restart")

    originals = {
        "UpdateLock": updater_main.UpdateLock,
        "_validate_source": updater_main._validate_source,
        "_replace_program_dir": updater_main._replace_program_dir,
        "_wait_for_parent": updater_main._wait_for_parent,
        "_wait_for_active_sessions": updater_main._wait_for_active_sessions,
        "_write_log": updater_main._write_log,
        "popen_hidden": updater_main.popen_hidden,
    }
    failures: list[str] = []
    successes: list[str] = []
    try:
        updater_main.UpdateLock = FakeUpdateLock
        updater_main._validate_source = lambda _source: {
            "schema_version": 1,
            "package_type": "full",
            "version": "2.0.0",
        }
        updater_main._replace_program_dir = fake_replace
        updater_main._wait_for_parent = lambda _pid, _status: None
        updater_main._wait_for_active_sessions = lambda _baza, _status: None
        updater_main._write_log = lambda _baza, _message: None
        updater_main.popen_hidden = fake_restart
        args = argparse.Namespace(
            source=str(source_dir),
            target=str(target_dir),
            baza_dir=str(baza_dir),
            target_version="2.0.0",
            launcher_host="test",
            lock=str(baza_dir / "locks" / "update.lock"),
            starting_lock="",
            local_starting_lock="",
            parent_pid="0",
            restart_exe="RemCardDoctor.exe",
        )
        worker = updater_main.UpdateWorker(args)
        worker.failed.connect(failures.append)
        worker.succeeded.connect(successes.append)
        worker.run()
    finally:
        for name, value in originals.items():
            setattr(updater_main, name, value)

    if failures:
        return False, f"worker failed in restart lock probe: {failures}"
    if successes != ["2.0.0"]:
        return False, f"worker success signal mismatch: {successes}"
    if not state["completed"]:
        return False, "worker released the lock without a completed replacement marker"
    if order != ["acquire", "replace", "release", "restart"]:
        return False, f"update lock/restart order is unsafe: {order}"
    return True, "ok"


def _check_restart_failure_after_success_is_warning(temp_root: str) -> tuple[bool, str]:
    from rem_card.app import updater_main

    source_dir = Path(temp_root, "UPD_restart_warning")
    target_dir = Path(temp_root, "Installed_restart_warning")
    baza_dir = Path(temp_root, "Baza_restart_warning")
    source_dir.mkdir(parents=True, exist_ok=True)
    target_dir.mkdir(parents=True, exist_ok=True)
    baza_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "RemCardDoctor.exe").write_bytes(b"installed new executable")

    order: list[str] = []
    state = {"released": False, "completed": False}

    class FakeUpdateLock:
        def __init__(self, _lock_path, _payload):
            pass

        def acquire(self):
            order.append("acquire")

        def release(self, *, completed=False, **_kwargs):
            state["completed"] = bool(completed)
            if not state["released"]:
                state["released"] = True
                order.append("release")

    def fake_replace(**_kwargs):
        order.append("replace")
        return "", ""

    def failed_restart(_args, **_kwargs):
        order.append("restart")
        raise OSError("simulated automatic restart failure")

    originals = {
        "UpdateLock": updater_main.UpdateLock,
        "_validate_source": updater_main._validate_source,
        "_replace_program_dir": updater_main._replace_program_dir,
        "_wait_for_parent": updater_main._wait_for_parent,
        "_wait_for_active_sessions": updater_main._wait_for_active_sessions,
        "_write_log": updater_main._write_log,
        "popen_hidden": updater_main.popen_hidden,
    }
    failures: list[str] = []
    successes: list[str] = []
    warnings: list[str] = []
    logs: list[str] = []
    try:
        updater_main.UpdateLock = FakeUpdateLock
        updater_main._validate_source = lambda _source: {
            "schema_version": 1,
            "package_type": "full",
            "version": "2.0.0",
        }
        updater_main._replace_program_dir = fake_replace
        updater_main._wait_for_parent = lambda _pid, _status: None
        updater_main._wait_for_active_sessions = lambda _baza, _status: None
        updater_main._write_log = lambda _baza, message: logs.append(str(message))
        updater_main.popen_hidden = failed_restart
        args = argparse.Namespace(
            source=str(source_dir),
            target=str(target_dir),
            baza_dir=str(baza_dir),
            target_version="2.0.0",
            launcher_host="test",
            lock=str(baza_dir / "locks" / "update.lock"),
            starting_lock="",
            local_starting_lock="",
            parent_pid="0",
            restart_exe="RemCardDoctor.exe",
        )
        worker = updater_main.UpdateWorker(args)
        worker.failed.connect(failures.append)
        worker.restart_warning.connect(warnings.append)
        worker.succeeded.connect(successes.append)
        worker.run()
    finally:
        for name, value in originals.items():
            setattr(updater_main, name, value)

    if failures:
        return False, f"restart-only failure incorrectly failed the installed update: {failures}"
    if successes != ["2.0.0"]:
        return False, f"installed update did not emit success after restart failure: {successes}"
    if len(warnings) != 1 or "Обновление установлено" not in warnings[0] or "вручную" not in warnings[0]:
        return False, f"restart failure warning is missing or unclear: {warnings}"
    if not state["completed"] or order != ["acquire", "replace", "release", "restart"]:
        return False, f"restart failure changed the safe update order: order={order}; state={state}"
    if not any("update restart failed" in message for message in logs):
        return False, f"restart failure was not logged separately: {logs}"
    if any("update failed" in message for message in logs):
        return False, f"restart-only failure was logged as installation failure: {logs}"
    return True, "ok"


def _check_update_lock_release_retries_and_terminal_fallback(temp_root: str) -> tuple[bool, str]:
    from rem_card.app import update_launcher, updater_main

    lock_dir = Path(temp_root, "update_lock_release", "locks")
    lock_dir.mkdir(parents=True, exist_ok=True)
    original_remove = updater_main.os.remove
    original_sleep = updater_main.time.sleep

    transient_path = lock_dir / "transient.lock"
    transient_lock = updater_main.UpdateLock(
        str(transient_path),
        {"timestamp": time.time(), "host": socket.gethostname(), "pid": os.getpid()},
    )
    transient_lock.acquire()
    transient_attempts = {"count": 0}
    delays: list[float] = []

    def transient_remove(path):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(transient_path)):
            transient_attempts["count"] += 1
            if transient_attempts["count"] <= 2:
                raise PermissionError("simulated transient SMB delete failure")
        return original_remove(path)

    try:
        updater_main.os.remove = transient_remove
        updater_main.time.sleep = delays.append
        transient_lock.release(
            completed=True,
            attempts=5,
            initial_delay_sec=0.01,
            max_delay_sec=0.04,
        )
    finally:
        updater_main.os.remove = original_remove
        updater_main.time.sleep = original_sleep
    if transient_attempts["count"] != 3:
        return False, f"transient lock delete was not retried to success: {transient_attempts}"
    if delays != [0.01, 0.02]:
        return False, f"lock delete backoff is not bounded/exponential: {delays}"
    if transient_path.exists():
        return False, "transiently locked update lock remained after retry success"

    terminal_path = lock_dir / "terminal.lock"
    terminal_lock = updater_main.UpdateLock(
        str(terminal_path),
        {"timestamp": time.time(), "host": socket.gethostname(), "pid": os.getpid()},
    )
    terminal_lock.acquire()

    def persistent_remove(path):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(terminal_path)):
            raise PermissionError("simulated persistent SMB delete failure")
        return original_remove(path)

    try:
        updater_main.os.remove = persistent_remove
        updater_main.time.sleep = lambda _seconds: None
        terminal_lock.release(
            completed=True,
            attempts=2,
            initial_delay_sec=0.01,
            max_delay_sec=0.02,
        )
        terminal_payload = json.loads(terminal_path.read_text(encoding="utf-8"))
        if terminal_payload.get("state") != "completed":
            return False, f"persistent delete did not leave a terminal lock: {terminal_payload}"
        if update_launcher._active_lock_payload(str(terminal_path), 30 * 60) is not None:
            return False, "completed terminal lock remained logically active during SMB cleanup failure"
        if not terminal_path.exists():
            return False, "persistent terminal cleanup failure scenario was not exercised"
    finally:
        updater_main.os.remove = original_remove
        updater_main.time.sleep = original_sleep
    if update_launcher._active_lock_payload(str(terminal_path), 30 * 60) is not None:
        return False, "completed terminal lock was still treated as active"
    if terminal_path.exists():
        return False, "completed terminal lock was not cleaned when SMB recovered"

    failed_path = lock_dir / "failed.lock"
    failed_lock = updater_main.UpdateLock(
        str(failed_path),
        {"timestamp": time.time(), "host": socket.gethostname(), "pid": os.getpid()},
    )
    failed_lock.acquire()

    def failed_remove(path):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(failed_path)):
            raise PermissionError("simulated delete failure before replacement completion")
        return original_remove(path)

    try:
        updater_main.os.remove = failed_remove
        updater_main.time.sleep = lambda _seconds: None
        try:
            failed_lock.release(
                completed=False,
                attempts=1,
                initial_delay_sec=0,
                max_delay_sec=0,
            )
        except updater_main.UpdateLockReleaseError:
            pass
        else:
            return False, "unreleased non-terminal lock did not raise UpdateLockReleaseError"
        failed_payload = json.loads(failed_path.read_text(encoding="utf-8"))
        if failed_payload.get("state") != "active":
            return False, f"terminal state was written before replacement completed: {failed_payload}"
    finally:
        updater_main.os.remove = original_remove
        updater_main.time.sleep = original_sleep
        original_remove(failed_path)
    return True, "ok"


def _check_dead_local_active_update_lock_is_inactive(temp_root: str) -> tuple[bool, str]:
    from rem_card.app import update_launcher

    lock_path = Path(temp_root, "dead_active_updater.lock")
    payload = {
        "state": "active",
        "timestamp": time.time(),
        "host": socket.gethostname(),
        "pid": 99999999,
    }
    lock_path.write_text(json.dumps(payload), encoding="utf-8")
    original_remove = update_launcher.os.remove
    original_is_pid_alive = update_launcher._is_pid_alive

    def unavailable_remove(path):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(lock_path)):
            raise PermissionError("simulated SMB cleanup outage")
        return original_remove(path)

    try:
        update_launcher.os.remove = unavailable_remove
        update_launcher._is_pid_alive = lambda _pid: False
        result = update_launcher._active_lock_payload(str(lock_path), 30 * 60)
    finally:
        update_launcher.os.remove = original_remove
        update_launcher._is_pid_alive = original_is_pid_alive
    if result is not None:
        return False, f"dead local updater lock remained logically active: {result}"
    if not lock_path.exists():
        return False, "dead updater lock cleanup outage scenario was not exercised"
    original_remove(lock_path)
    return True, "ok"


def _check_update_locks_are_scoped_to_target(temp_root: str) -> tuple[bool, str]:
    from rem_card.app import update_launcher
    from rem_card.app.update_checker import get_update_lock_path

    saved_env = os.environ.get("REMCARD_BAZA_DIR")
    original_is_compiled = update_launcher.is_compiled
    try:
        baza_dir = os.path.join(temp_root, "arbitrary_data_root")
        lock_dir = os.path.join(baza_dir, "locks")
        target_dir = os.path.join(temp_root, "Prog")
        os.makedirs(lock_dir, exist_ok=True)
        os.makedirs(target_dir, exist_ok=True)
        os.environ["REMCARD_BAZA_DIR"] = baza_dir
        update_launcher.is_compiled = lambda: True

        pc1_lock = get_update_lock_path(baza_dir, target_dir=target_dir, host="PC1")
        pc2_lock = get_update_lock_path(baza_dir, target_dir=target_dir, host="PC2")
        if os.path.abspath(pc1_lock) == os.path.abspath(pc2_lock):
            return False, "local target locks must differ for different hosts"

        remote_target = r"\\server\share\remcard\Prog"
        remote_pc1_lock = get_update_lock_path(baza_dir, target_dir=remote_target, host="PC1")
        remote_pc2_lock = get_update_lock_path(baza_dir, target_dir=remote_target, host="PC2")
        if os.path.abspath(remote_pc1_lock) != os.path.abspath(remote_pc2_lock):
            return False, "network target locks must be shared across hosts"

        legacy_lock = os.path.join(lock_dir, "remcard_update.lock")
        payload = {
            "timestamp": time.time(),
            "host": "OTHER-PC",
            "target": target_dir,
            "target_version": "1.0.1",
        }
        Path(legacy_lock).write_text(json.dumps(payload), encoding="utf-8")
        if update_launcher.is_update_in_progress(target_dir=target_dir):
            return False, "legacy lock from another host must not block local target startup"

        payload["host"] = socket.gethostname()
        payload["target_version"] = "1.0.2"
        Path(legacy_lock).write_text(json.dumps(payload), encoding="utf-8")
        if not update_launcher.is_update_in_progress(target_dir=target_dir):
            return False, "legacy lock for current host and target must block startup"

        os.remove(legacy_lock)
        scoped_lock = get_update_lock_path(baza_dir, target_dir=target_dir)
        payload["target_version"] = "1.0.3"
        Path(scoped_lock).write_text(json.dumps(payload), encoding="utf-8")
        if not update_launcher.is_update_in_progress(target_dir=target_dir):
            return False, "scoped lock for current target must block startup"

        return True, "ok"
    finally:
        update_launcher.is_compiled = original_is_compiled
        if saved_env is None:
            os.environ.pop("REMCARD_BAZA_DIR", None)
        else:
            os.environ["REMCARD_BAZA_DIR"] = saved_env


def _check_update_starting_lock_dead_pid_clears(temp_root: str) -> tuple[bool, str]:
    from rem_card.app import update_launcher
    from rem_card.app.update_checker import get_update_starting_lock_path

    saved_env = os.environ.get("REMCARD_BAZA_DIR")
    original_is_compiled = update_launcher.is_compiled
    try:
        baza_dir = os.path.join(temp_root, "arbitrary_data_root_dead_start")
        target_dir = os.path.join(temp_root, "Prog_dead_start")
        os.makedirs(os.path.join(baza_dir, "locks"), exist_ok=True)
        os.makedirs(target_dir, exist_ok=True)
        os.environ["REMCARD_BAZA_DIR"] = baza_dir
        update_launcher.is_compiled = lambda: True

        lock_path = get_update_starting_lock_path(baza_dir, target_dir=target_dir)
        payload = {
            "timestamp": time.time(),
            "started_at": "2026-01-01 00:00:00",
            "host": socket.gethostname(),
            "pid": 99999999,
            "updater_pid": 99999999,
            "state": "starting",
            "target": target_dir,
            "target_version": "1.0.1",
        }
        Path(lock_path).write_text(json.dumps(payload), encoding="utf-8")
        if update_launcher.is_update_in_progress(target_dir=target_dir):
            return False, "dead updater_pid starting lock must not block update"
        if os.path.exists(lock_path):
            return False, "dead updater_pid starting lock was not removed"
        return True, "ok"
    finally:
        update_launcher.is_compiled = original_is_compiled
        if saved_env is None:
            os.environ.pop("REMCARD_BAZA_DIR", None)
        else:
            os.environ["REMCARD_BAZA_DIR"] = saved_env


def _check_full_launcher_removes_starting_lock_when_updater_exits_immediately(
    temp_root: str,
) -> tuple[bool, str]:
    from rem_card.app.update_launcher import get_local_update_starting_lock_path

    launched, message, _args, lock_path, _update_root, target_dir = _capture_full_update_launch(
        temp_root,
        suffix="immediate_exit",
        exit_code=1,
    )
    if message != "ok":
        return False, message
    if launched:
        return False, "launch_update must fail when the full updater exits immediately"
    if os.path.exists(lock_path):
        return False, "starting lock was not removed after immediate updater exit"
    local_lock_path = get_local_update_starting_lock_path(target_dir)
    if os.path.exists(local_lock_path):
        return False, "local starting lock was not removed after immediate updater exit"
    return True, "ok"

"""Safety-сценарии: paths."""

from __future__ import annotations

from .common import PROJECT_ROOT
import os
import subprocess
import sys


def _path_is_under(path: str, root: str) -> bool:
    try:
        path_abs = os.path.normcase(os.path.abspath(path))
        root_abs = os.path.normcase(os.path.abspath(root))
        return os.path.commonpath([path_abs, root_abs]) == root_abs
    except Exception:
        return False


def _check_dev_baza_dir_prefers_project_baza_name(temp_root: str) -> tuple[bool, str]:
    from rem_card.app import runtime_paths

    saved_env = os.environ.get(runtime_paths.DEV_BAZA_DIR_ENV)
    saved_dev_config = os.environ.get(runtime_paths.DEV_DATABASE_CONFIG_ENV)
    saved_data_path_config = os.environ.get("REMCARD_DATA_PATH_CONFIG")
    original_get_project_root = runtime_paths.get_project_root
    try:
        os.environ.pop(runtime_paths.DEV_BAZA_DIR_ENV, None)
        os.environ.pop("REMCARD_DATA_PATH_CONFIG", None)
        os.environ[runtime_paths.DEV_DATABASE_CONFIG_ENV] = os.path.join(
            temp_root,
            "dev_database_paths.json",
        )
        project_root = os.path.join(temp_root, "project_root")
        expected = os.path.join(project_root, runtime_paths.DEFAULT_DEV_DATA_ROOT_NAME)
        legacy = os.path.join(project_root, "rework_baza")
        os.makedirs(expected, exist_ok=True)
        os.makedirs(legacy, exist_ok=True)
        runtime_paths.get_project_root = lambda: project_root

        resolved = runtime_paths.get_dev_baza_dir()
        if os.path.abspath(resolved) != os.path.abspath(expected):
            return False, f"dev data root did not use the configured default name: {resolved}"

        configured = os.path.join(temp_root, "configured_network_baza")
        runtime_paths.write_configured_baza_dir(configured)
        resolved_configured = runtime_paths.get_dev_baza_dir()
        if os.path.abspath(resolved_configured) != os.path.abspath(expected):
            return False, f"dev baza dir must ignore remcard_data_path.json, got: {resolved_configured}"

        saved_dev_baza = os.path.join(temp_root, "saved_dev_baza")
        runtime_paths.write_dev_database_config(saved_dev_baza, [saved_dev_baza, expected])
        if os.path.abspath(runtime_paths.get_dev_baza_dir()) != os.path.abspath(saved_dev_baza):
            return False, "saved dev database selection was not honored"

        override = os.path.join(temp_root, "explicit_dev_override")
        os.environ[runtime_paths.DEV_BAZA_DIR_ENV] = override
        if os.path.abspath(runtime_paths.get_dev_baza_dir()) != os.path.abspath(override):
            return False, "explicit REMCARD_DEV_BAZA_DIR override was not honored"
        return True, "ok"
    finally:
        runtime_paths.get_project_root = original_get_project_root
        if saved_env is None:
            os.environ.pop(runtime_paths.DEV_BAZA_DIR_ENV, None)
        else:
            os.environ[runtime_paths.DEV_BAZA_DIR_ENV] = saved_env
        if saved_data_path_config is None:
            os.environ.pop("REMCARD_DATA_PATH_CONFIG", None)
        else:
            os.environ["REMCARD_DATA_PATH_CONFIG"] = saved_data_path_config
        if saved_dev_config is None:
            os.environ.pop(runtime_paths.DEV_DATABASE_CONFIG_ENV, None)
        else:
            os.environ[runtime_paths.DEV_DATABASE_CONFIG_ENV] = saved_dev_config


def _check_arbitrary_baza_dir_name_allowed(temp_root: str) -> tuple[bool, str]:
    from rem_card.app.runtime_paths import (
        create_baza_structure_and_db,
        read_configured_baza_dir,
        validate_baza_dir_for_runtime,
        write_configured_baza_dir,
    )
    from rem_card.app.startup_db_guard import run_startup_db_guard

    saved_env = {
        key: os.environ.get(key)
        for key in ("REMCARD_BAZA_DIR", "REMCARD_DATA_PATH_CONFIG")
    }
    arbitrary_dir = os.path.join(temp_root, "custom_db_folder")
    config_path = os.path.join(temp_root, "runtime_config", "remcard_data_path.json")
    try:
        os.environ.pop("REMCARD_BAZA_DIR", None)
        os.environ["REMCARD_DATA_PATH_CONFIG"] = config_path

        ok, reason = create_baza_structure_and_db(arbitrary_dir)
        if not ok:
            return False, f"arbitrary folder create failed: {reason}"

        stored_config_path = write_configured_baza_dir(arbitrary_dir)
        if os.path.abspath(stored_config_path) != os.path.abspath(config_path):
            return False, f"unexpected config path: {stored_config_path}"
        if read_configured_baza_dir() != os.path.abspath(arbitrary_dir):
            return False, "configured arbitrary folder was not read back"

        valid, message = validate_baza_dir_for_runtime(arbitrary_dir)
        if not valid:
            return False, f"runtime validation rejected arbitrary folder: {message}"

        os.environ["REMCARD_BAZA_DIR"] = arbitrary_dir
        guard_result = run_startup_db_guard(role=None)
        if not guard_result.ok:
            return False, f"startup guard rejected arbitrary folder: {guard_result.user_message}"

        env = os.environ.copy()
        env.pop("REMCARD_BAZA_DIR", None)
        env["REMCARD_DATA_PATH_CONFIG"] = config_path
        env["PYTHONPATH"] = str(PROJECT_ROOT)
        # The probe writes resolved paths to a pipe.  On Windows a child Python
        # process otherwise uses the active ANSI code page, while the parent
        # intentionally decodes worker output as UTF-8.  A non-ASCII user/temp
        # directory would then look like a product path mismatch.
        env["PYTHONIOENCODING"] = "utf-8"
        fake_exe_dir = os.path.join(temp_root, "compiled_probe", "Prog")
        os.makedirs(fake_exe_dir, exist_ok=True)
        env["REMCARD_FAKE_EXE_DIR"] = fake_exe_dir
        script = r"""
from _local_rem_card_bootstrap import bootstrap_local_rem_card
bootstrap_local_rem_card()
import os
import sys
sys.frozen = True
sys.executable = os.path.join(os.environ["REMCARD_FAKE_EXE_DIR"], "RemCardDoctor.exe")
from rem_card.app.runtime_paths import resolve_baza_dir
from rem_card.app import paths
print(resolve_baza_dir())
print(paths.BAZA_DIR)
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(PROJECT_ROOT),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        if result.returncode != 0:
            return False, f"compiled path probe failed: {result.stderr[-500:]}"
        lines = [line.strip() for line in str(result.stdout or "").splitlines() if line.strip()]
        expected = os.path.abspath(arbitrary_dir)
        if lines[-2:] != [expected, expected]:
            return False, f"compiled path probe mismatch: {lines[-2:]}"
        return True, "ok"
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

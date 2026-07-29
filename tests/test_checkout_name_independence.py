from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

from _local_rem_card_bootstrap import (
    build_local_python_subprocess_env,
    get_local_checkout_root,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("checkout_name", ["remcard", "rem_card", "123"])
def test_source_package_import_does_not_depend_on_checkout_name(tmp_path, checkout_name):
    checkout = tmp_path / checkout_name
    shim_dir = checkout / "rem_card"
    probe_dir = checkout / "checkout_probe"
    shim_dir.mkdir(parents=True)
    probe_dir.mkdir()
    shutil.copy2(PROJECT_ROOT / "rem_card" / "__init__.py", shim_dir / "__init__.py")
    (probe_dir / "__init__.py").write_text("", encoding="utf-8")
    (probe_dir / "identity.py").write_text(
        'CHECKOUT_NAME = "' + checkout_name + '"\n',
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = os.fspath(checkout)
    env["PYTHONNOUSERSITE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from rem_card.checkout_probe.identity import CHECKOUT_NAME; "
                "print(CHECKOUT_NAME)"
            ),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert completed.stdout.strip() == checkout_name


def test_local_subprocess_environment_targets_checkout_not_its_parent():
    existing = os.path.join("some", "existing", "pythonpath")
    env = build_local_python_subprocess_env({"PYTHONPATH": existing, "MARKER": "kept"})
    entries = env["PYTHONPATH"].split(os.pathsep)

    assert Path(entries[0]).resolve() == Path(get_local_checkout_root()).resolve()
    assert entries[1:] == [existing]
    assert env["MARKER"] == "kept"


def _rem_card_import_lines(path: Path) -> list[int]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=os.fspath(path))
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("rem_card"):
            lines.append(node.lineno)
        elif isinstance(node, ast.Import) and any(
            alias.name == "rem_card" or alias.name.startswith("rem_card.")
            for alias in node.names
        ):
            lines.append(node.lineno)
    return lines


def _bootstrap_call_lines(path: Path) -> list[int]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=os.fspath(path))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "bootstrap_local_rem_card"
    ]


def test_official_direct_scripts_bootstrap_before_rem_card_imports():
    candidates = [
        *PROJECT_ROOT.glob("*.py"),
        *PROJECT_ROOT.joinpath("scripts").glob("*.py"),
        PROJECT_ROOT / "app" / "main.py",
        PROJECT_ROOT / "app" / "updater_main.py",
    ]
    missing_bootstrap: list[str] = []
    for path in candidates:
        import_lines = _rem_card_import_lines(path)
        if not import_lines:
            continue
        call_lines = _bootstrap_call_lines(path)
        if not call_lines or min(call_lines) > min(import_lines):
            missing_bootstrap.append(path.relative_to(PROJECT_ROOT).as_posix())

    assert missing_bootstrap == []

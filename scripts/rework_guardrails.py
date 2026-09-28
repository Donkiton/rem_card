"""Static boundaries that keep the stage 3 decomposition in place.

The checker deliberately uses only the standard library and reads source
files as text/AST.  It is therefore safe to run without importing RemCard,
Qt, or any database configuration.

Line ceilings are anchored to the stage 3 documented sizes (301, 158, 862,
and 73 lines respectively).  They leave room for composition-root changes,
while preventing the extracted implementations from gradually returning to
the entrypoints.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any


PRODUCTION_ROOTS = ("app", "data", "services", "ui")

REMOVED_SHIM_PATHS = (
    "services/remcard_service.py",
    "data/dao/remcard_dao.py",
    "ui/styles/dialog_styles.py",
)

REMOVED_SHIM_MODULES = frozenset(
    {
        "rem_card.services.remcard_service",
        "rem_card.data.dao.remcard_dao",
        "rem_card.ui.styles.dialog_styles",
    }
)

# Stage 3 baselines: 301, 158, 862, and 73 lines.  The ceilings are
# intentionally explicit so an entrypoint expansion requires a reviewed edit.
ENTRYPOINT_LINE_LIMITS = {
    "ui/doctor_view/doctor_remcard_widget.py": 450,
    "ui/doctor_view/orders_widget.py": 250,
    "ui/operblock_view/operblock_main_widget.py": 1100,
    "services/operblock_service.py": 150,
}


def _read_source(path: Path) -> str:
    return path.read_bytes().decode("utf-8-sig")


def _production_python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for directory in PRODUCTION_ROOTS:
        base = root / directory
        if base.is_dir():
            files.extend(path for path in base.rglob("*.py") if path.is_file())
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def _canonical_module_name(name: str) -> str:
    """Treat checkout-root package names as members of ``rem_card``."""

    first = name.partition(".")[0]
    if first in PRODUCTION_ROOTS:
        return f"rem_card.{name}"
    return name


def _package_for_file(root: Path, path: Path) -> str:
    parent_parts = path.relative_to(root).parent.parts
    return ".".join(("rem_card", *parent_parts))


def _resolve_from_module(
    package: str,
    module: str | None,
    level: int,
) -> str | None:
    if level == 0:
        return _canonical_module_name(module or "")

    package_parts = package.split(".")
    parents_to_drop = level - 1
    if parents_to_drop >= len(package_parts):
        return None
    base_parts = package_parts[: len(package_parts) - parents_to_drop]
    if module:
        base_parts.extend(module.split("."))
    return ".".join(base_parts)


def _is_removed_module(module: str) -> bool:
    return any(
        module == removed or module.startswith(f"{removed}.")
        for removed in REMOVED_SHIM_MODULES
    )


def _source_line(source: str, line_no: int) -> str:
    lines = source.splitlines()
    if 1 <= line_no <= len(lines):
        return lines[line_no - 1].strip()
    return ""


def _forbidden_imports(
    *,
    root: Path,
    path: Path,
    source: str,
    tree: ast.AST,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    rel_path = path.relative_to(root).as_posix()
    package = _package_for_file(root, path)

    for node in ast.walk(tree):
        matched_modules: set[str] = set()
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = _canonical_module_name(alias.name)
                if _is_removed_module(module):
                    matched_modules.add(module)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from_module(package, node.module, node.level)
            if base is None:
                continue
            if _is_removed_module(base):
                matched_modules.add(base)
            for alias in node.names:
                if alias.name == "*":
                    continue
                candidate = f"{base}.{alias.name}" if base else alias.name
                candidate = _canonical_module_name(candidate)
                if _is_removed_module(candidate):
                    matched_modules.add(candidate)

        for module in sorted(matched_modules):
            line_no = getattr(node, "lineno", 1)
            findings.append(
                {
                    "path": rel_path,
                    "line": line_no,
                    "message": (
                        f"import references removed rework shim {module}; "
                        "use the decomposed canonical module"
                    ),
                    "snippet": _source_line(source, line_no),
                }
            )
    return findings


def _check_removed_shims(root: Path) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []

    for rel_path in REMOVED_SHIM_PATHS:
        if (root / rel_path).is_file():
            findings.append(
                {
                    "path": rel_path,
                    "line": 1,
                    "message": "removed rework shim file must not be restored",
                }
            )

    for path in _production_python_files(root):
        rel_path = path.relative_to(root).as_posix()
        try:
            source = _read_source(path)
        except (OSError, UnicodeError) as exc:
            findings.append(
                {
                    "path": rel_path,
                    "line": 1,
                    "message": f"cannot inspect production source: {exc}",
                }
            )
            continue
        try:
            tree = ast.parse(source, filename=rel_path)
        except SyntaxError as exc:
            line_no = exc.lineno or 1
            findings.append(
                {
                    "path": rel_path,
                    "line": line_no,
                    "message": (
                        "cannot verify imports because source has a syntax "
                        f"error: {exc.msg}"
                    ),
                    "snippet": (
                        exc.text or _source_line(source, line_no)
                    ).strip(),
                }
            )
            continue
        findings.extend(
            _forbidden_imports(root=root, path=path, source=source, tree=tree)
        )

    return {
        "name": "removed_rework_shims",
        "ok": not findings,
        "findings": findings,
    }


def _check_entrypoint_line_limits(root: Path) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    measured: dict[str, int] = {}

    for rel_path, limit in ENTRYPOINT_LINE_LIMITS.items():
        path = root / rel_path
        if not path.is_file():
            findings.append(
                {
                    "path": rel_path,
                    "line": 1,
                    "message": (
                        "decomposed entrypoint is missing; "
                        f"line limit is {limit}"
                    ),
                }
            )
            continue
        try:
            source = _read_source(path)
        except (OSError, UnicodeError) as exc:
            findings.append(
                {
                    "path": rel_path,
                    "line": 1,
                    "message": f"cannot measure decomposed entrypoint: {exc}",
                }
            )
            continue

        line_count = len(source.splitlines())
        measured[rel_path] = line_count
        if line_count > limit:
            findings.append(
                {
                    "path": rel_path,
                    "line": limit + 1,
                    "message": (
                        f"decomposed entrypoint has {line_count} lines; "
                        f"limit is {limit}; "
                        "move implementation into the stage 3 feature modules"
                    ),
                }
            )

    return {
        "name": "decomposed_entrypoint_line_limits",
        "ok": not findings,
        "findings": findings,
        "limits": dict(ENTRYPOINT_LINE_LIMITS),
        "measured": measured,
    }


def check_rework_boundaries(root: Path) -> list[dict[str, Any]]:
    """Return architecture-check compatible results for ``root``."""

    root = Path(root).resolve()
    return [
        _check_removed_shims(root),
        _check_entrypoint_line_limits(root),
    ]


__all__ = ["ENTRYPOINT_LINE_LIMITS", "check_rework_boundaries"]

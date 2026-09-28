from __future__ import annotations

from pathlib import Path

import pytest

from scripts.rework_guardrails import (
    ENTRYPOINT_LINE_LIMITS,
    check_rework_boundaries,
)


def _write(root: Path, rel_path: str, source: str = "") -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _clean_root(tmp_path: Path) -> Path:
    for rel_path in ENTRYPOINT_LINE_LIMITS:
        _write(tmp_path, rel_path, "# composition root\n")
    return tmp_path


def _by_name(checks: list[dict[str, object]], name: str) -> dict[str, object]:
    return next(check for check in checks if check["name"] == name)


def test_clean_tree_passes_without_importing_application(
    tmp_path: Path,
) -> None:
    root = _clean_root(tmp_path)
    _write(
        root,
        "app/example.py",
        """\
# import rem_card.services.remcard_service
TEXT = "from rem_card.data.dao import remcard_dao"
from rem_card.services import patient_service as patients
""",
    )

    checks = check_rework_boundaries(root)

    assert [check["name"] for check in checks] == [
        "removed_rework_shims",
        "decomposed_entrypoint_line_limits",
    ]
    assert all(check["ok"] for check in checks)


@pytest.mark.parametrize(
    ("rel_path", "source", "removed_module"),
    [
        (
            "app/absolute_import.py",
            "import rem_card.services.remcard_service as legacy\n",
            "rem_card.services.remcard_service",
        ),
        (
            "services/relative_import.py",
            "from ..data.dao import remcard_dao as legacy\n",
            "rem_card.data.dao.remcard_dao",
        ),
        (
            "ui/doctor_view/relative_import.py",
            "from ..styles.dialog_styles import DIALOG_STYLE as legacy\n",
            "rem_card.ui.styles.dialog_styles",
        ),
    ],
)
def test_import_aliases_and_relative_imports_are_rejected(
    tmp_path: Path,
    rel_path: str,
    source: str,
    removed_module: str,
) -> None:
    root = _clean_root(tmp_path)
    _write(root, rel_path, source)

    check = _by_name(check_rework_boundaries(root), "removed_rework_shims")

    assert check["ok"] is False
    assert any(
        finding["path"] == rel_path
        and finding["line"] == 1
        and removed_module in finding["message"]
        for finding in check["findings"]
    )


@pytest.mark.parametrize(
    "rel_path",
    [
        "services/remcard_service.py",
        "data/dao/remcard_dao.py",
        "ui/styles/dialog_styles.py",
    ],
)
def test_removed_shim_files_cannot_return(
    tmp_path: Path,
    rel_path: str,
) -> None:
    root = _clean_root(tmp_path)
    _write(root, rel_path, "# restored shim\n")

    check = _by_name(check_rework_boundaries(root), "removed_rework_shims")

    assert check["ok"] is False
    assert any(
        finding["path"] == rel_path
        and "must not be restored" in finding["message"]
        for finding in check["findings"]
    )


def test_production_syntax_error_fails_closed(tmp_path: Path) -> None:
    root = _clean_root(tmp_path)
    _write(root, "data/broken.py", "def broken(:\n    pass\n")

    check = _by_name(check_rework_boundaries(root), "removed_rework_shims")

    assert check["ok"] is False
    assert check["findings"] == [
        {
            "path": "data/broken.py",
            "line": 1,
            "message": (
                "cannot verify imports because source has a syntax error: "
                "invalid syntax"
            ),
            "snippet": "def broken(:",
        }
    ]


def test_non_production_fixture_is_not_scanned(tmp_path: Path) -> None:
    root = _clean_root(tmp_path)
    _write(
        root,
        "tests/fixture.py",
        (
            "from rem_card.services import remcard_service\n"
            "this is invalid python\n"
        ),
    )

    check = _by_name(check_rework_boundaries(root), "removed_rework_shims")

    assert check["ok"] is True
    assert check["findings"] == []


def test_entrypoint_line_limit_accepts_exact_boundary_and_rejects_next_line(
    tmp_path: Path,
) -> None:
    root = _clean_root(tmp_path)
    rel_path = "ui/doctor_view/orders_widget.py"
    limit = ENTRYPOINT_LINE_LIMITS[rel_path]
    path = _write(root, rel_path, "# line\n" * limit)

    at_limit = _by_name(
        check_rework_boundaries(root), "decomposed_entrypoint_line_limits"
    )

    assert at_limit["ok"] is True
    assert at_limit["measured"][rel_path] == limit

    path.write_text("# line\n" * (limit + 1), encoding="utf-8")
    over_limit = _by_name(
        check_rework_boundaries(root), "decomposed_entrypoint_line_limits"
    )

    assert over_limit["ok"] is False
    assert over_limit["findings"] == [
        {
            "path": rel_path,
            "line": limit + 1,
            "message": (
                f"decomposed entrypoint has {limit + 1} lines; "
                f"limit is {limit}; "
                "move implementation into the stage 3 feature modules"
            ),
        }
    ]

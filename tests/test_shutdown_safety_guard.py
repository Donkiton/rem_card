from pathlib import Path

import pytest

from scripts.regression_checks import ui


@pytest.fixture
def sources(tmp_path, monkeypatch):
    for relative in (
        "services/data_service.py", "ui/main_window.py", "app/main.py",
        "app/sqlite_shared.py", "data/dao/db_manager.py",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((ui.PROJECT_ROOT / relative).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(ui, "PROJECT_ROOT", tmp_path)
    return tmp_path


def replace(path: Path, old: str, new: str):
    source = path.read_text(encoding="utf-8")
    assert old in source
    path.write_text(source.replace(old, new, 1), encoding="utf-8")


@pytest.mark.parametrize("arguments", ["", "**kwargs", "application_exit=True", "\n                    **kwargs\n                "])
def test_shutdown_guard_accepts_call_arguments(sources, arguments):
    replace(sources / "app/main.py", "data_service.shutdown(**kwargs)", f"data_service.shutdown({arguments})")
    assert ui._check_shutdown_queue_db_ordering_guards("") == (True, "ok")


@pytest.mark.parametrize("call", ["data_service.shutdown(**kwargs)", "db_manager.close()"])
def test_shutdown_guard_rejects_missing_call_even_with_text_decoy(sources, call):
    replace(sources / "app/main.py", call, repr(call))
    ok, details = ui._check_shutdown_queue_db_ordering_guards("")
    assert not ok and "must drain DataService and close DB" in details


def test_shutdown_guard_rejects_database_close_before_drain(sources):
    replace(sources / "app/main.py", '    logger.info("Application resource shutdown started")',
            '    db_manager.close()\n    logger.info("Application resource shutdown started")')
    ok, details = ui._check_shutdown_queue_db_ordering_guards("")
    assert not ok and "before DB close" in details


@pytest.mark.parametrize("call", [
    "self.container.data_service.shutdown(application_exit=True)",
    "data_service.shutdown(**kwargs)", "self.container.db_manager.close()",
])
def test_shutdown_guard_rejects_resource_close_in_close_event(sources, call):
    path = sources / "ui/main_window.py"
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    import ast
    tree = ast.parse(source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "MainWindow")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "closeEvent")
    lines.insert(method.body[0].lineno - 1, " " * method.body[0].col_offset + call + "\n")
    path.write_text("".join(lines), encoding="utf-8")
    ok, details = ui._check_shutdown_queue_db_ordering_guards("")
    assert not ok and "must defer data resource shutdown" in details

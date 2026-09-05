"""Граница редкого инструмента: обновление карты не проверяет МКБ и не читает мониторинг."""
import ast
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def test_doctor_passive_button_refresh_never_checks_diagnosis():
    source = ast.parse((ROOT / "ui/doctor_view/doctor_remcard_widget.py").read_text(encoding="utf-8-sig"))
    method = next(node for node in ast.walk(source)
                  if isinstance(node, ast.FunctionDef) and node.name == "_apply_burn_calculator_button_state")
    namespace = {}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "doctor_passive_refresh", "exec"), namespace)
    calls = []
    def forbidden(**kwargs):
        raise AssertionError("Passive refresh must not inspect patient or diagnosis")
    host = SimpleNamespace(sector8_panel=SimpleNamespace(set_burn_calc_enabled=lambda *args: calls.append(args)),
                           _is_qobject_alive=lambda obj: True, _burn_calculator_availability=forbidden)
    for _ in range(100):
        namespace[method.name](host)
    assert len(calls) == 100


def test_both_roles_build_burn_context_only_in_explicit_burn_action():
    for relative in ("ui/doctor_view/doctor_remcard_widget.py", "ui/nurse_view/nurse_main_widget.py"):
        source = ast.parse((ROOT / relative).read_text(encoding="utf-8-sig"))
        callers = []
        for method in ast.walk(source):
            if not isinstance(method, ast.FunctionDef):
                continue
            if any(isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Attribute) and node.func.attr == "_build_burn_calculator_context"
                or isinstance(node.func, ast.Name) and node.func.id == "build_burn_context"
            ) for node in ast.walk(method)):
                callers.append(method.name)
        # Врач использует тонкий адаптер; медсестра вызывает общий загрузчик напрямую.
        assert set(callers) - {"_build_burn_calculator_context"} == {"on_burn_calculator_clicked"}, (relative, callers)

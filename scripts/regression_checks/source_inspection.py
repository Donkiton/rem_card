"""Исходник класса с его локальными примесями для статических safety-проверок.

Не импортирует приложение и не выполняет код. Тела методов сохраняются дословно,
чтобы существующие проверки порядка операций и маркеров продолжали работать.
"""

from __future__ import annotations

import ast
from pathlib import Path


def read_widget_source(path: Path) -> str:
    """Для двух разделённых виджетов включает только реально подключённые примеси."""
    path = Path(path)
    source = path.read_text(encoding="utf-8-sig")
    targets = {
        "doctor_remcard_widget.py": ("DoctorRemCardWidget", "card_features"),
        "orders_widget.py": ("OrdersWidget", "order_features"),
    }
    if path.name not in targets or path.parent.name != "doctor_view":
        return source
    class_name, feature_dir = targets[path.name]
    tree = ast.parse(source)
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name), None)
    if cls is None:
        return source
    imports = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            parts = node.module.split(".")
            if feature_dir in parts:
                offset = parts.index(feature_dir)
                module_path = path.parent.joinpath(*parts[offset:]).with_suffix(".py")
                for alias in node.names:
                    imports[alias.asname or alias.name] = (module_path, alias.name)
    names = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    bodies = []
    module_preambles = []
    for base in cls.bases:
        if not isinstance(base, ast.Name) or base.id not in imports:
            if isinstance(base, ast.Name) and base.id.endswith("Mixin"):
                raise ValueError(f"Unresolved mixin: {class_name}.{base.id}")
            continue
        module_path, base_name = imports[base.id]
        mixin_source = module_path.read_text(encoding="utf-8-sig")
        mixin = next(n for n in ast.parse(mixin_source).body
                     if isinstance(n, ast.ClassDef) and n.name == base_name)
        if mixin.bases:
            raise ValueError(f"Nested inheritance needs explicit inspection: {base_name}")
        lines = mixin_source.splitlines(keepends=True)
        module_preambles.append("".join(lines[:mixin.lineno - 1]))
        for member in mixin.body:
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if member.name in names:
                    raise ValueError(f"Overridden method needs explicit inspection: {class_name}.{member.name}")
                names.add(member.name)
            start = min([member.lineno] + [d.lineno for d in getattr(member, "decorator_list", [])]) - 1
            bodies.append("".join(lines[start:member.end_lineno]))
    lines = source.splitlines(keepends=True)
    lines.insert(cls.end_lineno, "\n" + "\n".join(bodies) + "\n")
    return "\n".join(module_preambles) + "\n" + "".join(lines)

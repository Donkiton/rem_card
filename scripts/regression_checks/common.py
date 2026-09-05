"""Общие пути, AST-помощник и реестр ресурсов safety-проверок."""

from __future__ import annotations

import ast
from collections import OrderedDict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_AST_SOURCE_LINE_CACHE: OrderedDict[int, tuple[str, list[bytes]]] = OrderedDict()
_AST_SOURCE_LINE_CACHE_LIMIT = 16
_REGRESSION_RESTORE_PROBES: list[Any] = []


def _cached_source_segment(source: str, node: ast.AST, *, padded: bool = False) -> str | None:
    if not all(
        getattr(node, attribute, None) is not None
        for attribute in ("lineno", "end_lineno", "col_offset", "end_col_offset")
    ):
        return None

    cache_key = id(source)
    cached = _AST_SOURCE_LINE_CACHE.get(cache_key)
    if cached is None or cached[0] is not source:
        encoded_lines = [line.encode("utf-8") for line in source.splitlines()]
        _AST_SOURCE_LINE_CACHE[cache_key] = (source, encoded_lines)
        _AST_SOURCE_LINE_CACHE.move_to_end(cache_key)
        while len(_AST_SOURCE_LINE_CACHE) > _AST_SOURCE_LINE_CACHE_LIMIT:
            _AST_SOURCE_LINE_CACHE.popitem(last=False)
    else:
        encoded_lines = cached[1]
        _AST_SOURCE_LINE_CACHE.move_to_end(cache_key)

    start_line = int(node.lineno) - 1
    end_line = int(node.end_lineno) - 1
    if start_line < 0 or end_line >= len(encoded_lines) or end_line < start_line:
        return None
    start_col = int(node.col_offset)
    end_col = int(node.end_col_offset)
    if start_line == end_line:
        return encoded_lines[start_line][start_col:end_col].decode("utf-8")

    first = encoded_lines[start_line][start_col:].decode("utf-8")
    if padded:
        first = " " * start_col + first
    middle = [line.decode("utf-8") for line in encoded_lines[start_line + 1:end_line]]
    last = encoded_lines[end_line][:end_col].decode("utf-8")
    return "\n".join((first, *middle, last))

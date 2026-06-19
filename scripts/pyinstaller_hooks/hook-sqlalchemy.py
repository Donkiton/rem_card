# ------------------------------------------------------------------
# Local RemCard PyInstaller hook for SQLAlchemy.
#
# The upstream hook force-adds common optional DBAPI drivers such as pysqlite2,
# MySQLdb, and psycopg2. RemCard uses SQLite and does not ship those drivers, so
# avoid hidden-import warnings while keeping SQLAlchemy dialect discovery.
# ------------------------------------------------------------------

import importlib.util
import re

from PyInstaller import isolated
from PyInstaller.lib.modulegraph.modulegraph import SourceModule
from PyInstaller.utils.hooks import check_requirement, collect_entry_point, logger

datas = []
excludedimports = ["sqlalchemy.testing"]
hiddenimports = ["sqlalchemy.ext.baked"]

if check_requirement("sqlalchemy >= 1.4"):
    hiddenimports.append("sqlalchemy.sql.default_comparator")


@isolated.decorate
def _get_dialect_modules(module_name):
    import importlib

    module = importlib.import_module(module_name)
    return [f"{module_name}.{submodule_name}" for submodule_name in module.__all__]


if check_requirement("sqlalchemy >= 0.6"):
    hiddenimports += _get_dialect_modules("sqlalchemy.dialects")
else:
    hiddenimports += _get_dialect_modules("sqlalchemy.databases")

for entry_point_name in ("sqlalchemy.dialects", "sqlalchemy.plugins"):
    ep_datas, ep_hiddenimports = collect_entry_point(entry_point_name)
    datas += ep_datas
    hiddenimports += ep_hiddenimports


def hook(hook_api):
    if not check_requirement("sqlalchemy >= 0.9"):
        return

    depend_regex = re.compile(r"@util.dependencies\(['\"](.*?)['\"]\)")
    hidden_imports_set = set()
    known_imports = set()
    for node in hook_api.module_graph.iter_graph(start=hook_api.module):
        if isinstance(node, SourceModule) and node.identifier.startswith("sqlalchemy."):
            known_imports.add(node.identifier)

            with open(node.filename, "rb") as source_file:
                source_code = source_file.read()
            source_code = importlib.util.decode_source(source_code)

            for match in depend_regex.findall(source_code):
                hidden_imports_set.add(match)

    hidden_imports_set -= known_imports
    if hidden_imports_set:
        logger.info("  Found %d sqlalchemy hidden imports", len(hidden_imports_set))
        hook_api.add_imports(*list(hidden_imports_set))

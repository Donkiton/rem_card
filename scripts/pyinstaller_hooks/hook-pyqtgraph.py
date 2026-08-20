# ------------------------------------------------------------------
# Local RemCard PyInstaller hook for pyqtgraph.
#
# The upstream hook scans optional pyqtgraph integrations while collecting
# submodules. RemCard does not use pyqtgraph's OpenGL or Jupyter widgets, so
# skip those optional packages during analysis.
# ------------------------------------------------------------------

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = collect_data_files("pyqtgraph", excludes=["**/examples/*"])


def _include_pyqtgraph_submodule(name: str) -> bool:
    return (
        name != "pyqtgraph.examples"
        and not name.startswith("pyqtgraph.opengl")
        and not name.startswith("pyqtgraph.jupyter")
    )


all_imports = collect_submodules("pyqtgraph", filter=_include_pyqtgraph_submodule)
hiddenimports = [name for name in all_imports if "Template" in name]
hiddenimports += ["pyqtgraph.multiprocess.bootstrap"]

try:
    from PyInstaller.utils.hooks.qt import exclude_extraneous_qt_bindings
except ImportError:
    pass
else:
    excludedimports = exclude_extraneous_qt_bindings(
        hook_name="hook-pyqtgraph",
        qt_bindings_order=None,
    )

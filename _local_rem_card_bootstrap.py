import os
import sys
import types
from importlib.machinery import ModuleSpec
from typing import Mapping


def _is_frozen_app() -> bool:
    return bool(getattr(sys, "frozen", False))


def get_local_checkout_root() -> str:
    """Return this checkout root without relying on its directory name."""
    return os.path.dirname(os.path.abspath(__file__))


def build_local_python_subprocess_env(
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build an environment in which child Python can import this checkout.

    Python subprocesses started with ``-c`` or from another working directory
    do not inherit the in-memory package alias installed by
    :func:`bootstrap_local_rem_card`.  Put the checkout itself (not its parent)
    on ``PYTHONPATH`` so the source package shim can resolve ``rem_card`` no
    matter what the checkout directory is called.
    """
    env = dict(os.environ if base_env is None else base_env)
    checkout_root = get_local_checkout_root()
    existing_entries = [
        entry
        for entry in str(env.get("PYTHONPATH", "")).split(os.pathsep)
        if entry
    ]
    entries = [checkout_root, *existing_entries]
    deduplicated: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        key = os.path.normcase(os.path.abspath(entry))
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(entry)
    env["PYTHONPATH"] = os.pathsep.join(deduplicated)
    return env


def bootstrap_local_rem_card() -> str:
    """
    Bind the package name `rem_card` to this checkout.

    Application modules live directly in the checkout root, while project
    imports use the canonical absolute name ``rem_card.*``.  Bind that name
    explicitly so entrypoints work regardless of the checkout directory name.
    """
    if _is_frozen_app():
        # In PyInstaller builds the real modules live in the bundled archive.
        # Replacing sys.modules["rem_card"] with a filesystem-only alias hides
        # those bundled modules and breaks imports such as rem_card.app.main.
        runtime_root = os.path.abspath(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))
        if runtime_root not in sys.path:
            sys.path.insert(0, runtime_root)
        return runtime_root

    repo_root = get_local_checkout_root()
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    existing = sys.modules.get("rem_card")
    existing_paths = [os.path.abspath(path) for path in getattr(existing, "__path__", [])] if existing else []
    if os.path.abspath(repo_root) in existing_paths:
        return repo_root

    for module_name in list(sys.modules):
        if module_name == "rem_card" or module_name.startswith("rem_card."):
            del sys.modules[module_name]

    package = types.ModuleType("rem_card")
    package.__file__ = os.path.join(repo_root, "__init__.py")
    package.__path__ = [repo_root]
    package.__package__ = "rem_card"
    spec = ModuleSpec("rem_card", loader=None, is_package=True)
    spec.submodule_search_locations = [repo_root]
    package.__spec__ = spec
    sys.modules["rem_card"] = package
    return repo_root

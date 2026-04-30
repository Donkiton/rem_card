import os
import sys
import types
from importlib.machinery import ModuleSpec


def bootstrap_local_rem_card() -> str:
    """
    Bind the package name `rem_card` to this checkout.

    The BARS checkout lives in C:/Project/bars, while another checkout can live
    in C:/Project/rem_card. Most project imports are absolute `rem_card.*`, so
    running from the renamed folder needs an explicit local package alias.
    """
    repo_root = os.path.dirname(os.path.abspath(__file__))
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

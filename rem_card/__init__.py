"""Stable import package for a source checkout.

The repository intentionally keeps its application packages (``app``,
``services``, ``ui`` and others) at the checkout root.  A checkout directory
can have any name, so its filesystem name must not be used as the Python
package name.  This small package exposes the checkout root as the search path
for the canonical ``rem_card`` package.

Frozen builds use the real package assembled by PyInstaller and do not load
this source-checkout shim.
"""

from __future__ import annotations

from pathlib import Path


_CHECKOUT_ROOT = Path(__file__).resolve().parent.parent
__path__ = [str(_CHECKOUT_ROOT)]

# Keep importlib's package metadata consistent with ``__path__``.  This is
# useful to tooling that reads the module spec instead of the attribute.
if __spec__ is not None and __spec__.submodule_search_locations is not None:
    __spec__.submodule_search_locations[:] = __path__

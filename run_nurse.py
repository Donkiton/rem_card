import os
import sys

from _local_rem_card_bootstrap import bootstrap_local_rem_card

PROJECT_ROOT = bootstrap_local_rem_card()

try:
    import PySide6.QtOpenGL  # noqa: F401
    import PySide6.QtOpenGLWidgets  # noqa: F401
except ImportError:
    pass

from rem_card.app.main import main


if __name__ == "__main__":
    main(forced_role="nurse")

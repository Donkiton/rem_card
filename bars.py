import os

os.environ.setdefault("REMCARD_LOG_PREFIX", "bars")

from _local_rem_card_bootstrap import bootstrap_local_rem_card

bootstrap_local_rem_card()

try:
    import PySide6.QtOpenGL  # noqa: F401
    import PySide6.QtOpenGLWidgets  # noqa: F401
except ImportError:
    pass

from rem_card.standalone.bars_button_app import main


if __name__ == "__main__":
    raise SystemExit(main())

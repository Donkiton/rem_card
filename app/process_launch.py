from __future__ import annotations

import os
import subprocess
from typing import Any, Sequence


def hidden_window_creationflags(*, detached: bool = False) -> int:
    if os.name != "nt":
        return 0
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    if detached:
        flags |= int(getattr(subprocess, "DETACHED_PROCESS", 0) or 0)
    return flags


def hidden_window_startupinfo():
    if os.name != "nt":
        return None
    startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_cls is None:
        return None
    startupinfo = startupinfo_cls()
    startupinfo.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0) or 0)
    startupinfo.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0) or 0)
    return startupinfo


def popen_hidden(args: Sequence[str], **kwargs: Any):
    if os.name == "nt":
        kwargs.setdefault("creationflags", hidden_window_creationflags())
        startupinfo = hidden_window_startupinfo()
        if startupinfo is not None:
            kwargs.setdefault("startupinfo", startupinfo)
    return subprocess.Popen(args, **kwargs)

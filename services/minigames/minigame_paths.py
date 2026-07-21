from __future__ import annotations

import os
from typing import Callable, Optional, Union


PathLikeProvider = Optional[Union[str, os.PathLike, Callable[[], Union[str, os.PathLike]]]]


def resolve_data_root(data_root_provider: PathLikeProvider = None) -> str:
    if callable(data_root_provider):
        raw_root = data_root_provider()
    elif data_root_provider:
        raw_root = data_root_provider
    else:
        from rem_card.app.runtime_paths import resolve_baza_dir

        raw_root = resolve_baza_dir()

    if not raw_root:
        raise RuntimeError("Не задан путь к папке данных RemCard.")
    return os.path.abspath(os.path.normpath(str(raw_root)))


def get_minigames_dir(data_root_provider: PathLikeProvider = None) -> str:
    return os.path.join(resolve_data_root(data_root_provider), "minigames")


def get_users_dir(data_root_provider: PathLikeProvider = None) -> str:
    return os.path.join(get_minigames_dir(data_root_provider), "users")


def ensure_minigame_dirs(data_root_provider: PathLikeProvider = None) -> str:
    users_dir = get_users_dir(data_root_provider)
    os.makedirs(users_dir, exist_ok=True)
    return users_dir

"""Build-time policy; the spec embeds the selected value into the executable."""

NO_DATABASE_UPGRADES = False


def database_upgrades_disabled() -> bool:
    return NO_DATABASE_UPGRADES


def require_compatible_schema(ready: bool) -> None:
    if not ready:
        raise RuntimeError(
            "schema incompatible: база требует обновления схемы. "
            "В этой сборке автоматическое обновление базы отключено."
        )

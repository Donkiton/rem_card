"""Build-time policy; the spec embeds the selected value into the executable."""

NO_DATABASE_UPGRADES = False


def database_upgrades_disabled() -> bool:
    return NO_DATABASE_UPGRADES


def compiled_profile_matches_expectation() -> bool:
    """Verify the embedded policy during smoke; never change runtime policy."""
    import os

    expected = os.environ.get("REMCARD_SMOKE_EXPECT_NO_DATABASE_UPGRADES")
    if expected is None:
        return True
    return expected in {"0", "1"} and NO_DATABASE_UPGRADES == (expected == "1")


def require_compatible_schema(ready: bool) -> None:
    if not ready:
        raise RuntimeError(
            "schema incompatible: база требует обновления схемы. "
            "В этой сборке автоматическое обновление базы отключено."
        )

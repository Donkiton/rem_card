"""Build-time policy; the spec embeds the selected value into the executable."""

NO_DATABASE_UPGRADES = False


def database_upgrades_disabled() -> bool:
    return NO_DATABASE_UPGRADES


def local_operblock_schema_upgrades_allowed(conn, runtime_context) -> bool:
    """Limit local schema provisioning to the explicit workstation database."""
    from pathlib import Path
    import os

    if getattr(runtime_context, 'mode', None) != 'opblock_offline':
        return False
    if getattr(runtime_context, 'is_network', True):
        return False
    expected = Path(runtime_context.baza_dir).resolve() / 'operblock_local.db'
    if expected.resolve() != expected or Path(runtime_context.medical_db_path).resolve() != expected:
        return False
    if str(expected).startswith('\\\\'):
        return False
    if os.name == 'nt':
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(expected.anchor) != 3:
            return False
    main_path = next((row[2] for row in conn.execute('PRAGMA database_list') if row[1] == 'main'), '')
    return bool(main_path) and Path(main_path).resolve() == expected


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

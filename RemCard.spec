# -*- mode: python ; coding: utf-8 -*-

import os
import shutil
import json
import sys
import subprocess
from datetime import datetime
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

APP_ROOT = os.path.abspath(SPECPATH)
PROJECT_ROOT = os.path.dirname(APP_ROOT)
DICTIONARIES_TARGET = os.path.join("rem_card", "data", "dictionaries")
SETTINGS_TARGET = os.path.join("rem_card", "settings")
SETTINGS_RELEASE_TARGET = os.path.join("rem_card", "settings_release")

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

HIDDEN_IMPORTS = collect_submodules("rem_card") + collect_submodules("reportlab")


def _data_dir(relative_path):
    return (
        os.path.join(APP_ROOT, relative_path),
        os.path.join("rem_card", relative_path),
    )


def _data_file(relative_path, target_dir="rem_card"):
    return (
        os.path.join(APP_ROOT, relative_path),
        target_dir,
    )


def _dictionary_json_datas():
    source_dir = os.path.join(APP_ROOT, "data", "dictionaries")
    if not os.path.isdir(source_dir):
        raise RuntimeError(f"Dictionaries directory not found: {source_dir}")

    result = []
    for name in sorted(os.listdir(source_dir)):
        source_path = os.path.join(source_dir, name)
        if os.path.isfile(source_path) and name.lower().endswith(".json"):
            result.append((source_path, DICTIONARIES_TARGET))
    return result


def _settings_datas():
    source_dir = os.path.join(APP_ROOT, "settings")
    if not os.path.isdir(source_dir):
        raise RuntimeError(f"Settings directory not found: {source_dir}")

    result = []
    for current_dir, _dir_names, file_names in os.walk(source_dir):
        relative_dir = os.path.relpath(current_dir, source_dir)
        target_dir = SETTINGS_TARGET if relative_dir == "." else os.path.join(SETTINGS_TARGET, relative_dir)
        for name in sorted(file_names):
            source_path = os.path.join(current_dir, name)
            if os.path.isfile(source_path) and name.lower().endswith(".json"):
                result.append((source_path, target_dir))

    required_files = (
        os.path.join(source_dir, "display_settings", "display_settings.json"),
        os.path.join(source_dir, "display_settings", "background_settings.json"),
        os.path.join(source_dir, "color_scheme", "style_settings.json"),
    )
    for required_file in required_files:
        if not any(os.path.normcase(source) == os.path.normcase(required_file) for source, _target in result):
            raise RuntimeError(f"Settings file not found: {required_file}")
    return result


def _read_source_version():
    path = os.path.join(APP_ROOT, "VERSION")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.readline().strip()
    except Exception:
        return ""


def _current_git_commit():
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=APP_ROOT,
            check=True,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return str(result.stdout or "").strip()
    except Exception:
        return ""


def _settings_release_snapshot_datas():
    if os.environ.get("REMCARD_SKIP_SETTINGS_RELEASE_EXPORT") == "1":
        print("===> Settings release snapshot export skipped by REMCARD_SKIP_SETTINGS_RELEASE_EXPORT <===")
        return []

    from rem_card.app.runtime_paths import get_dev_baza_dir
    from rem_card.data.settings.settings_release import (
        SETTINGS_RELEASE_SNAPSHOT_FILE,
        export_settings_release_snapshot,
    )

    source_baza = os.path.abspath(
        os.environ.get("REMCARD_SETTINGS_RELEASE_SOURCE_BAZA")
        or get_dev_baza_dir()
    )
    snapshot_dir = os.path.join(APP_ROOT, "build", "settings_release")
    snapshot_path = os.path.join(snapshot_dir, SETTINGS_RELEASE_SNAPSHOT_FILE)
    report = export_settings_release_snapshot(
        source_baza,
        snapshot_path,
        release_version=_read_source_version(),
        release_commit=_current_git_commit(),
    )
    print(
        "===> Settings release snapshot exported: "
        f"{report['snapshot_path']} hash={report['content_hash']} rows={report['row_counts']} <==="
    )
    return [(snapshot_path, SETTINGS_RELEASE_TARGET)]

a = Analysis(
    [
        os.path.join(APP_ROOT, 'run_doctor.py'),
        os.path.join(APP_ROOT, 'run_nurse.py'),
        os.path.join(APP_ROOT, 'run_path_setup.py'),
        os.path.join(APP_ROOT, 'run_updater.py'),
    ],
    pathex=[PROJECT_ROOT, APP_ROOT],
    binaries=[],
	datas=[
		# версия приложения и журнал изменений
		_data_file('VERSION'),
		_data_file('CHANGELOG.md'),
		_data_file(os.path.join('app', 'release_info.json'), os.path.join('rem_card', 'app')),

		# иконки
		_data_dir('icon'),

		# dictionaries (json): seed для первого импорта в settings DB внутри _internal;
		# наружу рядом с exe эти файлы больше не копируются.
		*_dictionary_json_datas(),

		# settings: seed для первого импорта в settings DB внутри _internal.
		# Runtime-источник истины: <BAZA_DIR>/settings/remcard_settings.db.
		*_settings_datas(),

		# snapshot актуальной dev settings DB: применяется к сетевой settings DB при запуске новой версии.
		*_settings_release_snapshot_datas(),

		# активные ресурсы управления пациентами и МКБ
		_data_dir('data/mkb'),
		_data_dir('data/patient_assets'),
		_data_dir('procedure_templates'),

		# шрифты и служебные данные ReportLab для прямого PDF-рендера
		*collect_data_files('reportlab'),
],
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

def _script_toc(script_name):
    normalized = os.path.normcase(script_name)
    for item in a.scripts:
        candidate = os.path.normcase(os.path.basename(item[1] if len(item) > 1 else item[0]))
        if candidate == normalized:
            return [item]
    raise RuntimeError(f"Entry script not found in Analysis: {script_name}")


doctor_exe = EXE(
    pyz,
    _script_toc('run_doctor.py'),
    [],
    exclude_binaries=True,
    name='RemCardDoctor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=[os.path.join(APP_ROOT, 'icon', 'doctor.ico')],
)

nurse_exe = EXE(
    pyz,
    _script_toc('run_nurse.py'),
    [],
    exclude_binaries=True,
    name='RemCardNurse',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=[os.path.join(APP_ROOT, 'icon', 'nurse.ico')],
)

path_setup_exe = EXE(
    pyz,
    _script_toc('run_path_setup.py'),
    [],
    exclude_binaries=True,
    name='RemCardPathSetup',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=[os.path.join(APP_ROOT, 'icon', 'remcardicon.ico')],
)

updater_exe = EXE(
    pyz,
    _script_toc('run_updater.py'),
    [],
    exclude_binaries=True,
    name='RemCardUpdater',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=[os.path.join(APP_ROOT, 'icon', 'remcardicon.ico')],
)

coll = COLLECT(
    doctor_exe,
    nurse_exe,
    path_setup_exe,
    updater_exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Prog',
)

# === POST BUILD ===

print("===> Post-build: Moving files to target directory <===")

build_root = os.path.dirname(globals().get('WARNFILE', os.path.join(PROJECT_ROOT, 'build', 'RemCard', 'warn-RemCard.txt')))
dist_root = globals().get('DISTPATH', os.path.join(APP_ROOT, 'dist'))
dist_dir = os.path.join(dist_root, 'Prog')
target_dir = os.path.abspath(
    os.environ.get('REMCARD_BUILD_TARGET_DIR')
    or os.path.join(PROJECT_ROOT, 'Baza_rao3_jurnal', 'UPD')
)


def _read_release_info():
    path = os.path.join(APP_ROOT, 'app', 'release_info.json')
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            payload = json.load(fh)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _read_version():
    path = os.path.join(APP_ROOT, 'VERSION')
    with open(path, 'r', encoding='utf-8') as fh:
        return fh.readline().strip()


def _write_update_manifest(directory):
    release_info = _read_release_info()
    version = _read_version()
    manifest = {
        "schema_version": 1,
        "app": "rem_card",
        "version": version,
        "min_client_version": version,
        "prog_dir": ".",
        "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "release_info": release_info,
    }
    with open(os.path.join(directory, 'manifest.json'), 'w', encoding='utf-8') as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

if os.path.exists(dist_dir):
    os.makedirs(target_dir, exist_ok=True)

    print(f"Moving contents of {dist_dir} to {target_dir}...")

    ready_path = os.path.join(target_dir, 'ready.ok')
    if os.path.exists(ready_path):
        os.remove(ready_path)

    for name in os.listdir(target_dir):
        path = os.path.join(target_dir, name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

    # копирование с перезаписью
    shutil.copytree(dist_dir, target_dir, dirs_exist_ok=True)
    _write_update_manifest(target_dir)
    with open(ready_path, 'w', encoding='utf-8') as fh:
        fh.write(datetime.now().astimezone().isoformat(timespec="seconds") + "\n")

    # очистка
    shutil.rmtree(os.path.dirname(build_root), ignore_errors=True)
    shutil.rmtree(dist_root, ignore_errors=True)
    shutil.rmtree(os.path.join(APP_ROOT, '__pycache__'), ignore_errors=True)

    print(f"===> Success! The update package is ready in {target_dir} <===")
else:
    print("Error: dist folder not found")

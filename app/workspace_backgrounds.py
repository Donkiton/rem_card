"""Paired workspace backgrounds. Shared files only; never SQLite image BLOBs."""
from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import date
from pathlib import Path

from PIL import Image
from rem_card.app.paths import get_icon_dir
from rem_card.app.settings_media_cache import media_cache_root, settings_cache_namespace

MAX_BYTES = 10 * 1024 * 1024
STANDARD = '''СТАНДАРТ ФОНА РЕМКАРТЫ
Создай два отдельных согласованных изображения одной композиции: light.png и dark.png.
Размер каждого: 3840 × 2160 пикселей (16:9). PNG или JPEG, sRGB, RGB без прозрачности,
без анимации; до 10 МиБ на файл, желательно до 5 МиБ. Не увеличивай маленькую картинку
вместо создания детализированного оригинала. Светлый вариант — спокойный светлый фон;
тёмный — приглушённый тёмный фон, без ярких пятен. Средняя яркость (0–255):
светлый не ниже 145, тёмный не выше 110. Это фон медицинской программы:
поверх него располагаются таблицы и панели с прозрачностью 40%. Избегай мелкого текста,
рамок, водяных знаков, контрастных узоров и значимых деталей по краям. При изменении
пропорций окна края обрезаются симметрично; изображение не деформируется.
Оба файла обязательны: тема программы всегда выбирает соответствующий вариант.
'''
BUILTIN = {'id': 'builtin', 'name': 'Фон', 'start': '', 'end': ''}


def builtin_paths():
    root = Path(get_icon_dir())
    return {mode: str(root / ('workspace_background_' + mode + '.png')) for mode in ('light', 'dark')}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_catalog(path):
    if Path(path).stat().st_size > 128 * 1024:
        raise ValueError('Каталог фонов слишком большой.')
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    entries = value.get('entries')
    if value.get('version') != 2 or not isinstance(entries, list) or len(entries) > 30:
        raise ValueError('Неизвестный формат каталога фонов.')
    for entry in entries:
        if not re.fullmatch(r'[a-z0-9_-]{1,100}', entry.get('id', '')):
            raise ValueError('Некорректный идентификатор фона.')
        if not isinstance(entry.get('name'), str) or len(entry['name']) > 100:
            raise ValueError('Некорректное имя фона.')
        if entry.get('start') or entry.get('end'):
            if date.fromisoformat(entry['start']) > date.fromisoformat(entry['end']):
                raise ValueError('Некорректный период показа.')
        for mode in ('light', 'dark'):
            asset = entry[mode]
            if asset.get('file') not in ('light.png', 'dark.png', 'light.jpg', 'dark.jpg') or not asset['file'].startswith(mode):
                raise ValueError('Некорректное имя файла.')
            if not re.fullmatch(r'[a-f0-9]{64}', asset.get('sha256', '')) or not 0 < asset.get('size', 0) <= MAX_BYTES:
                raise ValueError('Некорректные параметры файла.')
    if value.get('default', 'builtin') not in ['builtin'] + [x['id'] for x in entries]:
        raise ValueError('Не найден основной фон.')
    return value


def empty_catalog():
    return {'version': 2, 'default': 'builtin', 'entries': []}


def validate_image(path):
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('Размер каждого изображения — не более 10 МиБ.')
    with Image.open(path) as img:
        if img.format not in ('PNG', 'JPEG') or img.size != (3840, 2160):
            raise ValueError('Нужен PNG или JPEG размером 3840 × 2160 пикселей.')
        if getattr(img, 'n_frames', 1) != 1 or img.mode != 'RGB' or 'transparency' in img.info:
            raise ValueError('Нужно статичное RGB-изображение без прозрачности.')
        img.verify()
    return {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'size': path.stat().st_size}


class BackgroundRepository:
    def __init__(self, baza_dir, cache_dir=None):
        self.shared = Path(baza_dir) / 'settings' / 'backgrounds' / 'collections'
        namespace = settings_cache_namespace(str(Path(baza_dir) / 'settings' / 'remcard_settings.db'))
        self.local = Path(cache_dir or media_cache_root()) / namespace / 'workspace_v2'
        self._verified = set()

    def local_catalog(self):
        try:
            return read_catalog(self.local / 'catalog.json')
        except (OSError, ValueError, KeyError, TypeError):
            return empty_catalog()

    def pair(self, entry):
        if entry['id'] == 'builtin':
            return builtin_paths()
        paths = {mode: self.local / entry['id'] / entry[mode]['file'] for mode in ('light', 'dark')}
        if all(path.is_file() and path.stat().st_size == entry[mode]['size'] for mode, path in paths.items()):
            return {mode: str(path) for mode, path in paths.items()}
        return None

    def active(self, catalog=None, today=None):
        catalog = catalog or self.local_catalog()
        today = today or date.today()
        entries = catalog['entries']
        scheduled = [e for e in entries if e.get('start') and date.fromisoformat(e['start']) <= today <= date.fromisoformat(e['end'])]
        selected = scheduled[-1] if scheduled else next((e for e in entries if e['id'] == catalog.get('default')), BUILTIN)
        paths = self.pair(selected)
        if paths:
            return selected, paths
        # An incomplete new pair must not replace the last usable pair.
        try:
            previous = read_catalog(self.local / 'last_good.json')['entries'][0]
            valid_period = not previous.get('start') or date.fromisoformat(previous['start']) <= today <= date.fromisoformat(previous['end'])
            paths = self.pair(previous) if valid_period else None
            if paths:
                return previous, paths
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            pass
        return BUILTIN, builtin_paths()

    def sync(self):
        """Called only on a worker: network outages never block the GUI."""
        try:
            catalog = read_catalog(self.shared / 'catalog.json')
        except FileNotFoundError:
            catalog = self.local_catalog()
            return catalog, {e['id']: 'Только локально; общий каталог отсутствует' for e in catalog['entries']}
        statuses = {}
        for entry in catalog['entries']:
            complete = True
            for mode in ('light', 'dark'):
                asset = entry[mode]
                source = self.shared / entry['id'] / asset['file']
                target = self.local / entry['id'] / asset['file']
                available = source.is_file()
                complete &= available
                if target.is_file() and target.stat().st_size == asset['size']:
                    if str(target) in self._verified or hashlib.sha256(target.read_bytes()).hexdigest() == asset['sha256']:
                        self._verified.add(str(target))
                        continue
                if not available:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_suffix('.' + uuid.uuid4().hex + '.tmp')
                try:
                    if source.stat().st_size != asset['size']:
                        raise ValueError('Размер сетевого файла изменён.')
                    # Bound the transfer even if the shared file changes while read.
                    with source.open('rb') as src, temp.open('wb') as dst:
                        remaining = MAX_BYTES + 1
                        while remaining:
                            chunk = src.read(min(1024 * 1024, remaining))
                            if not chunk:
                                break
                            dst.write(chunk)
                            remaining -= len(chunk)
                    if validate_image(temp) != {'sha256': asset['sha256'], 'size': asset['size']}:
                        raise ValueError('Контрольная сумма фона не совпадает.')
                    os.replace(temp, target)
                    self._verified.add(str(target))
                except (OSError, ValueError):
                    complete = False
                finally:
                    temp.unlink(missing_ok=True)
            statuses[entry['id']] = 'На сервере' if complete else 'Только локально' if self.pair(entry) else 'Файлы недоступны — используется «Фон»'
        atomic_json(self.local / 'catalog.json', catalog)
        selected, _paths = self.active(catalog)
        if selected['id'] != 'builtin':
            atomic_json(self.local / 'last_good.json', {'version': 2, 'default': selected['id'], 'entries': [selected]})
        return catalog, statuses

    def update(self, action, *, light=None, dark=None, name='', start='', end='', entry_id=None):
        """Publish immutable pairs first and switch catalog last, under a writer lock."""
        if action == 'publish':
            if not name.strip() or len(name.strip()) > 100:
                raise ValueError('Укажите название до 100 символов.')
            if bool(start) != bool(end) or (start and date.fromisoformat(start) > date.fromisoformat(end)):
                raise ValueError('Проверьте начало и окончание периода.')
            metadata = {mode: validate_image(path) for mode, path in [('light', light), ('dark', dark)]}
            from PIL import ImageStat
            brightness = {}
            for mode, path in [('light', light), ('dark', dark)]:
                with Image.open(path) as img:
                    img.thumbnail((96, 54))
                    brightness[mode] = ImageStat.Stat(img.convert('L')).mean[0]
            if brightness['light'] < 145 or brightness['dark'] > 110:
                raise ValueError('Проверьте темы: светлый фон должен быть светлым, тёмный — приглушённым тёмным. Возможно, файлы перепутаны.')
        self.shared.mkdir(parents=True, exist_ok=True)
        lock = self.shared / '.publication.lock'
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise ValueError('Другой компьютер публикует фон. Повторите позже. Если публикация прервалась, администратор может удалить .publication.lock.')
        try:
            os.close(fd)
            catalog = read_catalog(self.shared / 'catalog.json') if (self.shared / 'catalog.json').exists() else empty_catalog()
            if action == 'publish':
                if len(catalog['entries']) >= 30:
                    raise ValueError('В каталоге уже 30 комплектов. Уберите ненужные из списка.')
                if start and any(e.get('start') and start <= e['end'] and end >= e['start'] for e in catalog['entries']):
                    raise ValueError('Этот период пересекается с другим праздничным фоном.')
                identifier = date.today().isoformat() + '_' + uuid.uuid4().hex[:12]
                folder = self.shared / identifier
                folder.mkdir()
                entry = {'id': identifier, 'name': name.strip(), 'start': start, 'end': end}
                for mode, source in [('light', light), ('dark', dark)]:
                    with Image.open(source) as img:
                        ext = '.png' if img.format == 'PNG' else '.jpg'
                    filename = mode + ext
                    target = folder / filename
                    shutil.copyfile(source, target)
                    if validate_image(target) != metadata[mode]:
                        raise ValueError('Изображение изменилось во время публикации.')
                    entry[mode] = dict(metadata[mode], file=filename)
                catalog['entries'].append(entry)
                if not start:
                    catalog['default'] = identifier
            elif action == 'default':
                if entry_id != 'builtin' and not any(e['id'] == entry_id and not e.get('start') for e in catalog['entries']):
                    raise ValueError('Выберите основной комплект без периода показа.')
                catalog['default'] = entry_id
            elif action == 'remove':
                catalog['entries'] = [e for e in catalog['entries'] if e['id'] != entry_id]
                if catalog['default'] == entry_id:
                    catalog['default'] = 'builtin'
            else:
                raise ValueError('Неизвестное действие.')
            atomic_json(self.shared / 'catalog.json', catalog)
        finally:
            lock.unlink(missing_ok=True)
        return self.sync()
